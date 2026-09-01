"""Sentiment engine contract."""

from __future__ import annotations

import abc

from ..models import Announcement, Signal


class SentimentEngine(abc.ABC):
    name: str = "engine"

    @abc.abstractmethod
    async def score(self, announcement: Announcement) -> Signal | None:
        """Return a signal, or None if this announcement is not worth trading."""
        raise NotImplementedError

    async def aclose(self) -> None:
        return None
