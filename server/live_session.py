"""Live conversation rules over Meta realtime events (no I/O; the /voice/live
endpoint does the sockets). Design: docs/design/pip_agent_v3.md, "Live
conversation".

- Owner = the session's first `speaker` label; only owner turns reach the
  orchestrator. Other speakers are ignored (shown and logged, no reply).
- Partials go to the app as they come (owner unknown until the turn ends).
- Checkpoint 1 close rule: `window_s` of quiet after the last owner reply
  (or after opening, when the owner never spoke).
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
    owner_turns: int = 0
    last_owner_at: float | None = None

    def feed(self, ev: dict, now: float) -> list[dict]:
        """Realtime event -> messages for the app / work for the server:
        {"type": "partial"}, {"type": "ignored"}, {"type": "owner"}."""
        turn = self.tracker.feed(ev, now)
        kind = ev.get("type")
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
        text = strip_wake(text, self.wake_phrase)
        return [{"type": "owner", "text": text, "turn": turn.id}] if text else []

    def owner_replied(self, now: float) -> None:
        self.owner_turns += 1
        self.last_owner_at = now

    def should_close(self, now: float) -> bool:
        since = self.last_owner_at if self.last_owner_at is not None else self.opened_at
        return now - since >= self.window_s
