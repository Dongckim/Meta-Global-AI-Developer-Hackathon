"""Realtime ASR relay: handshake shape, framing, documented event correlation,
and the measurement VAD. No network.

Run: python -m server.tests.test_live_asr (runs on import).
"""

import array
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from eval.live_asr_measure import match_script, speech_segments  # noqa: E402
from server import live_asr  # noqa: E402

# Handshake: key inside the frame (not a header), 16 kHz PCM, mode fixed here.
h = live_asr.handshake("k", "DIARIZATION", ["Hey Milo"], ["English"])
assert h == {"authorization": {"accessToken": "Bearer k"}, "audioEncoding": "PCM_16KHZ",
             "model": "muse-voice-transcribe-1.0", "mode": "DIARIZATION",
             "keywords": ["Hey Milo"], "languageBias": ["English"]}, h
assert "keywords" not in live_asr.handshake("k")
print("LA1 ok: handshake")

# Frames: 80 ms of s16 mono at 16 kHz, last one padded with silence.
fr = live_asr.frames(b"\x01" * (live_asr.FRAME_BYTES + 10))
assert [len(f) for f in fr] == [2560, 2560] and fr[1].endswith(b"\x00"), [len(f) for f in fr]
print("LA2 ok: frames")

# Correlation: partials/speaker go to the latest speechStart; a later turn
# can open before an earlier one completes (keyed by turnId).
tr = live_asr.TurnTracker()
for ev, t in [
    ({"type": "speechStart", "turnId": 1, "audioProcessedMs": 100}, 0.2),
    ({"type": "transcript", "transcript": "mark", "final": False, "audioProcessedMs": 400}, 0.5),
    ({"type": "speaker", "label": "A", "audioProcessedMs": 900}, 1.0),
    ({"type": "speechEnd", "turnId": 1, "audioProcessedMs": 1000}, 1.1),
    ({"type": "speechStart", "turnId": 2, "audioProcessedMs": 1300}, 1.4),
    ({"type": "speechComplete", "turnId": 1, "transcript": "Mark C2 done.", "audioProcessedMs": 1350}, 1.5),
    ({"type": "speaker", "label": "B", "audioProcessedMs": 2000}, 2.1),
    ({"type": "audioProgress", "audioProcessedMs": 2400}, 2.5),
]:
    tr.feed(ev, t)
t1, t2 = tr.turns[1], tr.turns[2]
assert (t1.speaker, t1.partial, t1.text, t1.t_end, t1.t_complete) == ("A", "mark", "Mark C2 done.", 1.1, 1.5)
assert t2.speaker == "B" and t2.text is None, t2
assert t1.partials == [(0.5, "mark")], t1.partials
assert tr.owner == "A" and tr.labels == ["A", "B"] and tr.max_processed_ms == 2400
print("LA3 ok: turn correlation")

# VAD: two tones separated by 1 s of silence -> two segments near the truth.
def tone(ms):
    n = 16 * ms
    return array.array("h", (int(8000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(n))).tobytes()
silence = lambda ms: b"\x00" * (32 * ms)  # noqa: E731
segs = speech_segments(silence(500) + tone(800) + silence(1000) + tone(600) + silence(500))
assert len(segs) == 2, segs
assert abs(segs[0][0] - 500) <= 40 and abs(segs[0][1] - 1300) <= 40, segs
assert abs(segs[1][1] - 2900) <= 40, segs
print("LA4 ok: vad")

script = [{"who": "owner", "text": "mark C2 done"}, {"who": "other", "text": "what time is lunch"}]
assert match_script("Mark C2 done.", script) == 0
assert match_script("What time is lunch?", script) == 1
assert match_script("completely unrelated words", script) is None
print("LA5 ok: script match")

from eval.live_asr_measure import norm, plan_first  # noqa: E402
assert norm("Mark C2 done.") == norm("mark c2  done") == "mark c2 done"
# speech ends at 10 s, final text at 13 s, model 1.2 s, silence detect 0.8 s
today, planned = plan_first(10.0, 13.0, 1.2, same=True)
assert (round(today, 2), round(planned, 2)) == (4.2, 3.0), (today, planned)  # waits for speechComplete
today, planned = plan_first(10.0, 11.0, 1.2, same=True)
assert round(planned, 2) == 2.0, planned  # plan ready after the final: model time dominates
today, planned = plan_first(10.0, 13.0, 1.2, same=False)
assert round(planned, 2) == round(today, 2) == 4.2  # final differs: re-plan, no gain
print("LA6 ok: plan-first estimate")
print("ALL PASS")
