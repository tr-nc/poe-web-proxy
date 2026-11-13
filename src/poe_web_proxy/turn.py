"""Mutable evidence for one browser send, shared with the session transaction owner."""

from dataclasses import dataclass

from .images import Attachment


@dataclass
class BrowserTurn:
    chat_url: str | None = None
    reply_id: str | None = None
    images: tuple[Attachment, ...] = ()
    submitted: bool = False
