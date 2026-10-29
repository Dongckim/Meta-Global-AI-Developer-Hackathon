"""Meta realtime ASR measurement (piece 1): stream a 16 kHz mono WAV at real
time through server/live_asr.py and report, per turn, latency from the end
of speech (found by an energy VAD on the file) to speechEnd / speaker /
speechComplete, plus speaker-label accuracy against an expected script.

Run from repo root (needs MODEL_API_KEY in .env):
    .venv/bin/python eval/live_asr_measure.py rec1.wav --script rec1.json --mode both
Script JSON: [{"who": "owner" | "other", "text": "mark C2 done"}, ...] in order.
Appends to eval/results/live_asr.md.
"""
from __future__ import annotations

import argparse
import array
import asyncio
import difflib
import json
import os
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import live_asr  # noqa: E402

VAD_FRAME_MS = 20


def read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise SystemExit(f"{path}: need 16 kHz mono 16-bit PCM WAV, got "
                             f"{w.getframerate()} Hz {w.getnchannels()} ch {8 * w.getsampwidth()}-bit")
        return w.readframes(w.getnframes())


def speech_segments(pcm: bytes, rate: int = 16000, gap_ms: int = 400,
                    min_ms: int = 200) -> list[tuple[int, int]]:
    """Energy VAD: 20 ms RMS over an adaptive floor; gaps < gap_ms merge,
    blips < min_ms drop. Returns [(start_ms, end_ms)] of speech."""
    s = array.array("h", pcm)
    n = rate * VAD_FRAME_MS // 1000
    rms = []
    for i in range(0, len(s) - n + 1, n):
        chunk = s[i:i + n]
        rms.append((sum(x * x for x in chunk) / n) ** 0.5)
    if not rms:
        return []
    floor = sorted(rms)[len(rms) // 5]  # 20th percentile = room noise
    thr = max(floor * 4, 300.0)
    segs: list[list[int]] = []
    for i, r in enumerate(rms):
        if r < thr:
            continue
        t = i * VAD_FRAME_MS
        if segs and t - segs[-1][1] <= gap_ms:
            segs[-1][1] = t + VAD_FRAME_MS
        else:
            segs.append([t, t + VAD_FRAME_MS])
    return [(a, b) for a, b in segs if b - a >= min_ms]


def overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def norm(text: str) -> str:
    """Compare form: lowercase words only (punctuation and spacing ignored)."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def partial_at(turn, t: float) -> str:
    """Latest cumulative partial received by time t."""
    out = ""
    for ti, text in turn.partials:
        if ti <= t:
            out = text
    return out


def plan_first(end_s: float, t_complete: float, model_s: float, same: bool,
               detect_s: float = 0.8) -> tuple[float, float]:
    """(today, plan-first) seconds from end of speech to the action running.
    Today: the model starts at speechComplete. Plan-first: it starts when the
    server sees `detect_s` of silence (partial so far), and the plan runs at
    max(plan ready, speechComplete); a final text that differs re-plans."""
    today = t_complete + model_s - end_s
    planned = end_s + detect_s + model_s
    run_at = max(planned, t_complete) if same else t_complete + model_s
    return today, run_at - end_s


def match_script(text: str, script: list[dict]) -> int | None:
    """Index of the expected line closest to a turn's text (None if weak)."""
    best, best_i = 0.0, None
    for i, line in enumerate(script):
        r = difflib.SequenceMatcher(None, text.lower(), line["text"].lower()).ratio()
        if r > best:
            best, best_i = r, i
    return best_i if best >= 0.5 else None


async def run(pcm: bytes, mode: str, keywords: list[str], tail_ms: int):
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise SystemExit("MODEL_API_KEY is not set (.env)")
    tracker = live_asr.TurnTracker()
    progress: list[tuple[float, int]] = []

    def on_event(ev, t):
        tracker.feed(ev, t)
        if isinstance(ev.get("audioProcessedMs"), int):
            progress.append((t, ev["audioProcessedMs"]))

    silence = b"\x00" * (16 * 2 * tail_ms)
    res = await live_asr.stream_pcm(key, live_asr.frames(pcm + silence), mode=mode,
                                    keywords=keywords, language_bias=["English"],
                                    on_event=on_event)
    return tracker, res, progress


def model_seconds(texts: list[str]) -> list[float]:
    """Real orchestrator time per text (grammar or Spark), on a throwaway
    demo-run store so the measurement never touches server/data."""
    from fastapi.testclient import TestClient
    import server.api as api
    from server.store import RecordStore
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.start_demo_run()
    c = TestClient(api.app)
    out = []
    for i, text in enumerate(texts):
        t0 = time.perf_counter()
        c.post("/orchestrate", json={"text": text, "session": f"sim-{i}"})
        out.append(time.perf_counter() - t0)
    return out


def simulate(tracker, segs, detect_s: float) -> list[str]:
    """Plan-first estimate for owner turns (DIARIZATION)."""
    rows = []
    owner_turns = [t for t in sorted(tracker.turns.values(), key=lambda x: x.id)
                   if t.speaker == tracker.owner and t.text and t.t_complete is not None]
    if not owner_turns:
        return rows
    secs = model_seconds([t.text for t in owner_turns])
    rows += ["", f"plan-first simulation (silence detect {detect_s:.1f}s, real model time):", "",
             "| turn | model s | partial = final | end→action today | end→action plan-first | saved |",
             "|---|---|---|---|---|---|"]
    for t, m in zip(owner_turns, secs):
        span = (t.start_ms or 0, t.end_ms or t.complete_ms or 0)
        seg = max(segs, key=lambda s: overlap(s, span), default=None)
        if not seg or overlap(seg, span) == 0:
            continue
        end_s = seg[1] / 1000
        same = norm(partial_at(t, end_s + detect_s)) == norm(t.text)
        today, planned = plan_first(end_s, t.t_complete, m, same, detect_s)
        rows.append(f"| {t.id} | {m:.2f} | {'y' if same else 'n'} | {today:.2f} | {planned:.2f} | "
                    f"{today - planned:.2f} |")
    return rows


def report(name: str, mode: str, pcm: bytes, tracker, res, progress, script, tail_ms: int,
           detect_s: float | None = None) -> str:
    audio_ms = len(pcm) // 32
    segs = speech_segments(pcm)
    lines = [f"\n## {name} · {mode}\n",
             f"handshake: {'ok session ' + str(res.session_id) if res.session_id else 'FAILED'}"
             f" · close {res.close_code} {res.close_reason!r}"
             + (f" · error {res.error}" if res.error else ""),
             f"audio {audio_ms} ms + silence {tail_ms} ms · sent {res.sent_ms} ms · "
             f"max audioProcessedMs {tracker.max_processed_ms} · labels {tracker.labels} "
             f"(owner = {tracker.owner})", ""]
    # f) silence counted: progress past the end of speech audio
    in_tail = [ms for t, ms in progress if ms > audio_ms]
    lines.append(f"silence in audioProcessedMs: {'yes, up to ' + str(max(in_tail)) + ' ms' if in_tail else 'no'}")
    lines.append("")
    lines.append("| turn | speech ms (VAD) | end→speechEnd | speechEnd→Complete | end→Complete | "
                 "speaker vs speechEnd | label | expected | text |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    right = judged = 0
    for t in sorted(tracker.turns.values(), key=lambda x: x.id):
        span = (t.start_ms or 0, t.end_ms or t.complete_ms or 0)
        seg = max(segs, key=lambda s: overlap(s, span), default=None)
        seg = seg if seg and overlap(seg, span) > 0 else None
        end_s = seg[1] / 1000 if seg else None

        def d(a, b):
            return f"{(a - b) * 1000:.0f}" if a is not None and b is not None else "-"
        exp = "-"
        if script and t.text:
            i = match_script(t.text, script)
            if i is not None:
                who = script[i]["who"]
                exp = f"{who} #{i + 1}"
                if t.speaker:
                    judged += 1
                    right += (t.speaker == tracker.owner) == (who == "owner")
        lines.append(f"| {t.id} | {seg[0] if seg else '-'}–{seg[1] if seg else '-'} | {d(t.t_end, end_s)} | "
                     f"{d(t.t_complete, t.t_end)} | {d(t.t_complete, end_s)} | {d(t.t_speaker, t.t_end)} | "
                     f"{t.speaker or '-'} | {exp} | {(t.text or t.partial or '').replace('|', '/')} |")
    if script:
        lines.append(f"\nlabel accuracy (owner vs other): {right}/{judged}")
    if detect_s is not None and mode == "DIARIZATION":
        lines += simulate(tracker, segs, detect_s)
    return "\n".join(lines) + "\n"


def main():
    from dotenv import load_dotenv
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="+")
    ap.add_argument("--script", action="append", default=[],
                    help="expected-lines JSON per wav, in the same order")
    ap.add_argument("--mode", default="DIARIZATION", choices=["DIARIZATION", "ENDPOINTING", "both"])
    ap.add_argument("--keywords", action="append", default=[])
    ap.add_argument("--tail-ms", type=int, default=4000, help="silence appended after the audio")
    ap.add_argument("--simulate", type=float, default=None, metavar="SECONDS",
                    help="plan-first estimate with this server silence detect (e.g. 0.8)")
    a = ap.parse_args()
    modes = ["DIARIZATION", "ENDPOINTING"] if a.mode == "both" else [a.mode]
    out = []
    for i, path in enumerate(a.wav):
        pcm = read_wav(path)
        script = json.loads(Path(a.script[i]).read_text()) if i < len(a.script) else None
        for mode in modes:
            tracker, res, progress = asyncio.run(run(pcm, mode, a.keywords, a.tail_ms))
            text = report(Path(path).name, mode, pcm, tracker, res, progress, script, a.tail_ms,
                          a.simulate)
            print(text)
            out.append(text)
    with open("eval/results/live_asr.md", "a") as f:
        f.write("".join(out))
    print("appended eval/results/live_asr.md")


if __name__ == "__main__":
    main()
