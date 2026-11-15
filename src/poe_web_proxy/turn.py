"""Mutable evidence for one browser send, shared with the session transaction owner."""

from collections.abc import Callable
from dataclasses import dataclass, field
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
    structured: bool = False
    plain_text: bool = False
    commentary: str = ""
    reply_received: bool = False
    generation_finished: bool = False
    request_key: str | None = None
    recovering: bool = False
    user_id: str | None = None
    user_hash: str | None = None
    base_user_id: str | None = None
    base_user_hash: str | None = None
    base_marker: str | None = None
    response_id: str | None = None
    marker_required: bool = True
    on_progress: Callable[[], None] | None = field(default=None, repr=False)

    def checkpoint(self) -> None:
        if self.on_progress:
            self.on_progress()
