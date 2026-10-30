"""Live conversation rules over Meta realtime events (no I/O; the /voice/live
endpoint does the sockets). Design: docs/design/pip_agent_v3.md, "Live
conversation".

- Owner = the session's first `speaker` label; only owner turns reach the
  orchestrator. Other speakers are ignored (shown and logged, no reply).
- Partials go to the app as they come (owner unknown until the turn ends).
- Window: `window_s` after the owner's last turn; only owner turns move
  it; the clock pauses while the agent speaks and restarts when it stops.
  A session closes only at a turn boundary (nobody mid-turn), and at
  `max_s` (55 min, under Meta's 60) at the next boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from server.live_asr import TurnTracker
from server.tone import persona, section


def settings() -> dict:
    """Live settings from pip_voice.json `live` (window, ignored line)."""
    return section("live")


def strip_wake(text: str, phrase: str) -> str:
    """Drop a leading wake phrase ("Hey Milo, mark C2 done" -> "mark C2 done")."""
    words = re.findall(r"[a-z]+", (phrase or "").lower())
    if not words:
        return text
    pat = r"^\s*" + r"[\s,.!?]+".join(map(re.escape, words)) + r"\b[\s,.!?]*"
    return re.sub(pat, "", text or "", flags=re.IGNORECASE).strip()


def device_hints(buttons: list[str], titles: list[str]) -> list[str]:
    """contextualStrings for the iPad's recognizer: every well, then the rest."""
    wells = [f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 13)]
    return list(dict.fromkeys(wells + buttons + titles))


def live_keywords(record: dict, limit: int = 40) -> list[str]:
    """Bias words: wake phrase, agent name, this protocol's reagents and
    conditions, well spellings from the plan, and command words."""
    p = persona()
    out: list[str] = [p.get("wake_phrase") or "", p.get("agent_name") or ""]
    plan = (record or {}).get("plan") or {}
    for c in (plan.get("conditions") or {}).values():
        out.append((c or {}).get("label") or "")
        out += list((c or {}).get("treatments") or [])
    wells: list[str] = []
    for s in plan.get("steps") or []:
        liquid = ((s or {}).get("pipette") or {}).get("liquid")
        if liquid:
            out.append(liquid)
        wells += [w for w in (s or {}).get("wells") or [] if w not in wells]
    out += wells[:6]
    out += ["done", "exclude", "timer", "note", "next step"]
    from server.tone import section
    out += list(section("live").get("keywords") or [])  # common lab words (pip_voice.json live.keywords)
    seen, keep = set(), []
    for k in out:
        k = (k or "").strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            keep.append(k)
    return keep[:limit]


@dataclass
class LiveConversation:
    opened_at: float
    window_s: float = 60.0
    wake_phrase: str = ""
    tracker: TurnTracker = field(default_factory=TurnTracker)
    handled: set[int] = field(default_factory=set)
    max_s: float = 55 * 60
    owner_turns: int = 0
    ref_at: float | None = None      # window clock start (owner turn / speech end)
    speaking: bool = False           # the agent is talking: clock paused
    open_turns: set[int] = field(default_factory=set)

    def __post_init__(self):
        if self.ref_at is None:
            self.ref_at = self.opened_at

    def feed(self, ev: dict, now: float) -> list[dict]:
        """Realtime event -> messages for the app / work for the server:
        {"type": "partial"}, {"type": "ignored"}, {"type": "owner"}."""
        turn = self.tracker.feed(ev, now)
        kind = ev.get("type")
        if kind == "speechStart" and turn is not None:
            self.open_turns.add(turn.id)
        if kind == "speechComplete" and turn is not None:
            self.open_turns.discard(turn.id)
        if kind == "transcript" and turn is not None and turn.text is None:
            return [{"type": "partial", "text": turn.partial}]
        if kind != "speechComplete" or turn is None or turn.id in self.handled:
            return []
        self.handled.add(turn.id)
        text = (turn.text or "").strip()
        if not text:
            return []
        owner = self.tracker.owner
        if turn.speaker is not None and owner is not None and turn.speaker != owner:
            return [{"type": "ignored", "speaker": turn.speaker, "text": text,
                     "line": settings().get("ignored_line") or "Another voice — ignored"}]
        self.ref_at = now  # owner turns (only) restart the window
        rest = strip_wake(text, self.wake_phrase)
        if not rest:
            # only the wake phrase: answer "I'm here" without a model call
            return [{"type": "here", "turn": turn.id}]
        return [{"type": "owner", "text": rest, "turn": turn.id}]

    def owner_replied(self, now: float) -> None:
        """Reply sent; with TTS off this is when the agent's part ends."""
        self.owner_turns += 1
        self.ref_at = max(self.ref_at or now, now)

    def tts(self, speaking: bool, now: float) -> None:
        """The app reports the agent's speech: paused while on, restart at off."""
        self.speaking = speaking
        if not speaking:
            self.ref_at = now

    def should_close(self, now: float) -> str | None:
        """Why to close now ("window" / "max_duration"), or None. Never mid-turn
        or while the agent speaks."""
        if self.speaking or self.open_turns:
            return None
        if now - self.opened_at >= self.max_s:
            return "max_duration"
        if now - (self.ref_at or self.opened_at) >= self.window_s:
            return "window"
        return None


class LiveSender:
    """The one way out to the app's live socket (/voice/live). Everything
    (ready, partials, stages, turns, pings, closed) goes through send();
    after the socket closes, or the app is gone, sends are dropped instead of
    raising "websocket.send after websocket.close"."""

    def __init__(self, ws):
        self.ws = ws
        self.closed = False

    async def send(self, msg: dict) -> bool:
        if self.closed:
            return False
        try:
            await self.ws.send_json(msg)
            return True
        except Exception as e:  # disconnected, or closed under us
            self.closed = True
            print(f"live send dropped ({type(e).__name__}): {msg.get('type')}", flush=True)
            return False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.ws.close()
        except Exception:
            pass  # the app already left
