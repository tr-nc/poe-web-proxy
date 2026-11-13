"""Mutable evidence for one browser send, shared with the session transaction owner."""

from dataclasses import dataclass
from typing import Literal

from .images import Attachment

ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass
class BrowserTurn:
    chat_url: str | None = None
    reply_id: str | None = None
    images: tuple[Attachment, ...] = ()
    submitted: bool = False
    reasoning_effort: ReasoningEffort | None = None
