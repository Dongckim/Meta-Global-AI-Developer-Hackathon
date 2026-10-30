"""Meta realtime ASR relay (Muse Voice Transcribe over WebSocket).

Docs (2026-10): https://dev.meta.ai/docs/api-reference/voice/realtime and
.../voice/schemas, https://dev.meta.ai/docs/speech-to-text.
- Handshake: first JSON text frame within 10 s; the key travels inside it
  (`authorization.accessToken = "Bearer <key>"`), not in an HTTP header.
  The ack is the only server frame without `type`.
- Audio: binary s16le mono PCM at ~real time (backlog > 5 s or below real
  time for 10 s closes with 1008). Keep sending silence while connected.
- End: send {"type": "endStream"}, then read until the server closes 1000.
- Partials and `speaker` carry no turnId: they belong to the most recent
  `speechStart`; `speechComplete` (keyed by turnId) is the turn's text.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

REALTIME_URL = "wss://api.meta.ai/v1/asr/realtime"
MODEL = "muse-voice-transcribe-1.0"
RATE = 16000
FRAME_MS = 80
FRAME_BYTES = RATE * 2 * FRAME_MS // 1000  # s16 mono


def handshake(key: str, mode: str = "DIARIZATION", keywords: list[str] | None = None,
              language_bias: list[str] | None = None) -> dict:
    """RealtimeHandshakeRequest for 16 kHz PCM (server-side resampled)."""
    h: dict = {"authorization": {"accessToken": f"Bearer {key}"},
               "audioEncoding": "PCM_16KHZ", "model": MODEL, "mode": mode}
    if keywords:
        h["keywords"] = list(keywords)
    if language_bias:
        h["languageBias"] = list(language_bias)
    return h


def frames(pcm: bytes, frame_bytes: int = FRAME_BYTES) -> list[bytes]:
    """Fixed-size frames; the last one is zero-padded (silence)."""
    out = [pcm[i:i + frame_bytes] for i in range(0, len(pcm), frame_bytes)]
    if out and len(out[-1]) < frame_bytes:
        out[-1] = out[-1] + b"\x00" * (frame_bytes - len(out[-1]))
    return out


@dataclass
class Turn:
    id: int
    start_ms: int | None = None      # audioProcessedMs at speechStart
    end_ms: int | None = None        # at speechEnd
    complete_ms: int | None = None   # at speechComplete
    speaker: str | None = None
    partial: str = ""
    partials: list[tuple[float, str]] = field(default_factory=list)  # (t, cumulative text)
    text: str | None = None
    t_start: float | None = None     # receive time, s since first audio frame
    t_speaker: float | None = None
    t_end: float | None = None
    t_complete: float | None = None


@dataclass
class TurnTracker:
    """Per-turn state from realtime events, correlated the documented way."""
    turns: dict[int, Turn] = field(default_factory=dict)
    current: int | None = None       # turn opened by the latest speechStart
    labels: list[str] = field(default_factory=list)  # in first-seen order
    max_processed_ms: int = 0

    def feed(self, ev: dict, t: float) -> Turn | None:
        kind = ev.get("type")
        ms = ev.get("audioProcessedMs")
        if isinstance(ms, int):
            self.max_processed_ms = max(self.max_processed_ms, ms)
        if kind == "speechStart":
            tid = ev["turnId"]
            turn = self.turns.setdefault(tid, Turn(tid))
            turn.start_ms, turn.t_start = ms, t
            self.current = tid
            return turn
        if kind in ("speechEnd", "speechComplete"):
            turn = self.turns.setdefault(ev["turnId"], Turn(ev["turnId"]))
            if kind == "speechEnd":
                turn.end_ms, turn.t_end = ms, t
            else:
                turn.complete_ms, turn.t_complete = ms, t
                turn.text = ev.get("transcript") or ""
            return turn
        if self.current is None:
            return None
        turn = self.turns[self.current]
        if kind == "speaker":
            turn.speaker, turn.t_speaker = ev.get("label"), t
            if turn.speaker and turn.speaker not in self.labels:
                self.labels.append(turn.speaker)
            return turn
        if kind == "transcript":
            turn.partial = ev.get("transcript") or ""
            turn.partials.append((t, turn.partial))
            return turn
        return None

    @property
    def owner(self) -> str | None:
        """Session's first speaker label (the owner by design)."""
        return self.labels[0] if self.labels else None


@dataclass
class StreamResult:
    session_id: str | None
    close_code: int | None
    close_reason: str
    error: dict | None
    sent_ms: int


class MetaStream:
    """One realtime session: open (handshake + ack), send audio, end, events.
    Live relays push the app's frames as they come (the app is a real-time
    source); `stream_pcm` paces a file."""

    def __init__(self, key: str, *, mode: str = "DIARIZATION", keywords: list[str] | None = None,
                 language_bias: list[str] | None = None, url: str = REALTIME_URL):
        self.key, self.mode, self.keywords, self.language_bias, self.url = \
            key, mode, keywords, language_bias, url
        self.ws = None
        self.session_id: str | None = None
        self.error: dict | None = None

    async def open(self) -> bool:
        """Connect and handshake. False (with `error`) when Meta refuses."""
        import websockets
        self.ws = await websockets.connect(self.url, max_size=None)
        await self.ws.send(json.dumps(handshake(self.key, self.mode, self.keywords, self.language_bias)))
        ack = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
        if "type" in ack:  # an error instead of the ack
            self.error = ack
            return False
        self.session_id = ack.get("sessionId")
        return True

    async def send_audio(self, pcm: bytes) -> None:
        await self.ws.send(pcm)

    async def end(self) -> None:
        """endStream: half-close; keep reading until the server closes 1000."""
        await self.ws.send(json.dumps({"type": "endStream"}))

    async def events(self):
        """Text events until the socket closes (binary frames skipped)."""
        import websockets
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    continue
                ev = json.loads(msg)
                if ev.get("type") == "error":
                    self.error = ev
                yield ev
        except websockets.exceptions.ConnectionClosed:
            return

    @property
    def close(self) -> tuple[int | None, str]:
        return (self.ws.close_code, self.ws.close_reason or "") if self.ws else (None, "")

    async def aclose(self) -> None:
        if self.ws is not None:
            await self.ws.close()


class DeviceStream:
    """Private: the iPad transcribes and sends the words. MetaStream's shape
    and event names, so LiveConversation doesn't change. No speaker labels:
    every turn is the owner's."""

    session_id = "device"
    error = None
    close = (1000, "")

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.turn = 0
        self.speaking = False

    async def open(self) -> bool:
        return True

    def heard(self, text: str, final: bool) -> None:
        if not self.speaking:
            self.turn += 1
            self.speaking = True
            self.q.put_nowait({"type": "speechStart", "turnId": self.turn})
        kind = "speechComplete" if final else "transcript"
        self.q.put_nowait({"type": kind, "turnId": self.turn, "transcript": text})
        self.speaking = not final

    async def end(self) -> None:
        self.q.put_nowait(None)

    async def events(self):
        while (ev := await self.q.get()) is not None:
            yield ev

    async def aclose(self) -> None:
        pass


async def stream_pcm(key: str, pcm_frames: Iterable[bytes], *, mode: str = "DIARIZATION",
                     keywords: list[str] | None = None, language_bias: list[str] | None = None,
                     on_event: Callable[[dict, float], None] = lambda ev, t: None,
                     url: str = REALTIME_URL, frame_ms: int = FRAME_MS) -> StreamResult:
    """Handshake, send frames on an absolute real-time schedule, endStream,
    read until close. `on_event(event, t)` gets t = seconds since the first
    audio frame was sent (audio time == wall time when paced)."""
    ms = MetaStream(key, mode=mode, keywords=keywords, language_bias=language_bias, url=url)
    sent = 0
    try:
        if await ms.open():
            t0 = time.perf_counter()

            async def send():
                nonlocal sent
                for i, fr in enumerate(pcm_frames):
                    delay = t0 + i * frame_ms / 1000 - time.perf_counter()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    await ms.send_audio(fr)
                    sent += 1
                await ms.end()

            sender = asyncio.create_task(send())
            try:
                async for ev in ms.events():
                    on_event(ev, time.perf_counter() - t0)
            finally:
                sender.cancel()
        code, reason = ms.close
    finally:
        await ms.aclose()
    return StreamResult(ms.session_id, code, reason, ms.error, sent * frame_ms)
