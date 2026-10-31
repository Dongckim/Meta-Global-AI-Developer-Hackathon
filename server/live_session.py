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
from dataclasses import dataclass, field, fields

from server.live_asr import Turn, TurnTracker
from server.tone import persona, section


@dataclass
class TurnEndTimes:
    """Seconds. Defaults: pip_voice.json live.turn_end; Diagnostics overrides
    them per session (the `start` frame)."""
    quiet_s: float = 1.2
    extra_s: float = 2.5
    cap_s: float = 3.0
    settle_s: float = 0.25

    @classmethod
    def from_(cls, *sources: dict) -> "TurnEndTimes":
        names = [f.name for f in fields(cls)]
        merged = {k: v for src in sources for k, v in (src or {}).items()
                  if k in names and isinstance(v, (int, float))}
        return cls(**merged)


def turn_end(reading: str, since_word: float, mic_quiet: float, asr_final: bool, speaking: bool,
             t: TurnEndTimes) -> str | None:
    """Is this turn over, and why? The one rule for Full and Private. `reading`
    is what the words are (exact-match / unfinished-extended / plain);
    since_word: seconds since the transcript last changed; mic_quiet: seconds
    the mic has been quiet. Milo's own speech is never silence."""
    if speaking:
        return None
    if since_word >= t.cap_s:
        return "cap"
    if reading == "exact-match":
        return "exact-match" if since_word >= t.settle_s else None
    quiet = since_word >= t.quiet_s and mic_quiet >= t.quiet_s
    if reading == "unfinished-extended":
        return "unfinished-extended" if quiet and since_word >= t.quiet_s + t.extra_s else None
    if asr_final:
        return "asr-final"
    return "silence" if quiet else None


def turn_end_line(reason: str, turn, last_word: float | None, now: float) -> str:
    """[turn-end]: where a turn waited, seconds from its start ("-": never came)."""
    t0 = turn.t_start if turn.t_start is not None else now
    at = lambda t: "-" if t is None else f"{t - t0:.2f}"  # noqa: E731
    return (f"[turn-end] turn={turn.id} reason={reason} last_partial={at(last_word)} asr_end={at(turn.t_end)} "
            f"asr_final={at(turn.t_complete)} decided={at(now)} text=\"{turn.text or turn.partial}\"")


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
    word_at: dict[int, float] = field(default_factory=dict)  # turn -> when its words last changed
    forced: dict[int, str] = field(default_factory=dict)     # turn -> what we ended it on

    def open_turn(self):
        """The turn being said, once there are words in it; else None."""
        turn = self.tracker.turns.get(self.tracker.current)
        return turn if turn and turn.id in self.open_turns and turn.partial.strip() else None

    def since_word(self, turn, now: float) -> float:
        return now - self.word_at.get(turn.id, now)

    def force_end(self, now: float) -> list[dict]:
        """We decided the turn is over before the ASR did: end it on what was
        heard. If the ASR's final later says more, the rest is its own turn."""
        turn = self.open_turn()
        if turn is None:
            return []
        self.forced[turn.id] = turn.partial
        return self.feed({"type": "speechComplete", "turnId": turn.id, "transcript": turn.partial}, now)

    def _rest_after_forced(self, ev: dict) -> list[dict]:
        said, whole = self.forced.pop(ev.get("turnId")), (ev.get("transcript") or "").strip()
        if not whole.lower().startswith(said.strip().lower()):
            return []
        rest = whole[len(said.strip()):].strip(" ,.")
        return [{"type": "owner", "text": rest, "turn": ev.get("turnId")}] if rest else []

    def __post_init__(self):
        if self.ref_at is None:
            self.ref_at = self.opened_at

    def feed(self, ev: dict, now: float) -> list[dict]:
        """Realtime event -> messages for the app / work for the server:
        {"type": "partial"}, {"type": "ignored"}, {"type": "owner"}."""
        kind = ev.get("type")
        if kind == "speechComplete" and ev.get("turnId") in self.forced and ev.get("turnId") in self.handled:
            return self._rest_after_forced(ev)
        before = (self.tracker.turns.get(self.tracker.current) or Turn(0)).partial
        turn = self.tracker.feed(ev, now)
        if kind == "transcript" and turn is not None and turn.partial != before:
            self.word_at[turn.id] = now
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
