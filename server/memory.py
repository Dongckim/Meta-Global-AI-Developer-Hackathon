"""Conversation memory adapter. Spark response-id chain now;
a Glimmer-style message-history adapter can replace it later behind Memory.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Memory(ABC):
    @abstractmethod
    def params(self) -> dict:
        """Extra request params carrying the conversation ({} when fresh)."""

    @abstractmethod
    def note_response(self, response_id: str | None) -> None:
        """Chain the next turn onto this response."""

    @abstractmethod
    def reset(self, summary: str = "") -> None:
        """Drop the chain. Summary seeds the next fresh turn (or "" for none)."""

    @property
    @abstractmethod
    def turns(self) -> int:
        """Turns since the last reset."""


class SparkChainMemory(Memory):
    """previous_response_id chaining with a turn cap. Caller re-anchors via reset()."""

    def __init__(self, cap: int = 8):
        self.cap = cap
        self._prev: str | None = None
        self._turns = 0
        self.seed = ""

    def params(self) -> dict:
        return {"previous_response_id": self._prev} if self._prev else {}

    def note_response(self, response_id: str | None) -> None:
        if response_id:
            self._prev = response_id
            self._turns += 1

    def reset(self, summary: str = "") -> None:
        self._prev = None
        self._turns = 0
        self.seed = summary

    @property
    def turns(self) -> int:
        return self._turns

    @property
    def needs_reanchor(self) -> bool:
        return self._turns >= self.cap
