"""Durable Pi-session -> Poe-chat bindings, with fail-closed send acknowledgement."""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .agent import AgentCompletion, Message
from .turn import BrowserTurn


class SessionConflict(Exception):
    pass


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        with closing(self.connect()) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS chats (key TEXT PRIMARY KEY, record TEXT NOT NULL)"
            )
            db.commit()

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def get(self, key: str):
        with closing(self.connect()) as db:
            row = db.execute("SELECT record FROM chats WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def replace(self, key: str, old, new):
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record FROM chats WHERE key = ?", (key,)).fetchone()
            if (json.loads(row[0]) if row else None) != old:
                raise SessionConflict(
                    "Session changed while queued; retry with the acknowledged history"
                )
            if new is None:
                db.execute("DELETE FROM chats WHERE key = ?", (key,))
            else:
                db.execute("INSERT OR REPLACE INTO chats VALUES (?, ?)", (key, json.dumps(new)))
            db.commit()


def session_id(headers) -> str | None:
    values = {
        headers[name]
        for name in ("x-session-affinity", "session_id", "x-session-id")
        if headers.get(name)
    }
    if len(values) > 1:
        raise ValueError("Conflicting session headers")
    value = next(iter(values), None)
    if value is not None and (len(value) > 256 or not value.strip()):
        raise ValueError("Invalid session id")
    return value


class AgentTurn:
    """One transaction: send once, then only re-read that exact acknowledged request."""

    def __init__(
        self,
        body: AgentCompletion,
        identity: str | None,
        store: SessionStore | None,
        *,
        repair_reply_id: str | None = None,
    ):
        self.body = body
        self.store = store
        self.key = digest(identity) if identity else None
        if self.key and not store:
            raise ValueError("Persistent sessions are not configured on this server")
        self.old = store.get(self.key) if store and self.key else None
        history = [m for m in body.messages if m.role not in ("system", "developer")]
        self.history = [digest(item) for item in body.canonical(history)]
        context = [m for m in body.messages if m.role in ("system", "developer")]
        self.context = digest(
            {
                "system": body.canonical(context),
                "tools": [tool.model_dump(exclude_none=True) for tool in body.tools],
                "choice": body.tool_choice,
            }
        )
        self.request = digest(
            {
                "model": body.model,
                "history": self.history,
                "context": self.context,
                "effort": body.reasoning_effort,
            }
        )
        # Operator-only checkpoint: never exposed by the HTTP request schema.
        # The browser must verify this observed terminal reply before sending a
        # new, tools-forbidden recovery note in the SAME chat. No history replay.
        repair = repair_reply_id is not None
        if repair:
            if not self.old or self.old["status"] != "uncertain" or not self.old.get("chat_url"):
                raise SessionConflict("Repair requires an existing uncertain chat")
            if (
                body.tool_choice != "none"
                or body.messages[-1].role != "user"
                or not isinstance(body.messages[-1].content, str)
                or not re.fullmatch(r"message-[A-Za-z0-9_-]+", repair_reply_id)
            ):
                raise ValueError(
                    "Repair requires a tools-forbidden text checkpoint and observed reply ID"
                )
        self.recovering = False
        if self.old:
            if self.old["model"] != body.model:
                raise SessionConflict("A Pi session is bound to one Poe model/chat")
            if (
                not repair
                and self.old.get("request") == self.request
                and self.old.get("chat_url")
                and (self.old["status"] == "ready" or self.old.get("submitted"))
            ):
                self.recovering = True
            if self.old["status"] != "ready" and not self.recovering and not repair:
                raise SessionConflict(
                    "Previous send is unacknowledged; recovery needs the exact original "
                    "request and verified chat evidence. Do not resend its history."
                )
        if repair:
            messages = [*context, body.messages[-1]]
            self.prompt = body.prompt(messages, context=True)
        elif self.recovering:
            # No history, context, attachments, or effort are sent during recovery.
            self.prompt = "[Read-only recovery: no input is sent to Poe]"
            messages = []
        elif self.old:
            previous = self.old["history"]
            if self.history[: len(previous)] != previous or len(self.history) <= len(previous):
                raise SessionConflict(
                    "History diverged or was compacted; refusing to replay history"
                )
            delta = history[len(previous) :]
            changed = self.context != self.old["context"]
            messages = [*context, *delta] if changed else delta
            self.prompt = body.prompt(messages, context=changed)
        else:
            messages = body.messages
            self.prompt = body.prompt()
        owner = digest({"session": self.key, "request": self.request}) if self.key else None
        if owner and not self.recovering:
            self.prompt += "\nPOE-PROXY-REQUEST:" + owner
            if len(self.prompt) > 200000:
                raise ValueError("Serialized agent prompt exceeds 200000 characters")
        old = self.old or {}
        self.browser = BrowserTurn(
            chat_url=old.get("chat_url"),
            reply_id=repair_reply_id
            if repair
            else (
                old.get("base_reply_id")
                if self.recovering and old["status"] == "ready"
                else old.get("reply_id")
            ),
            images=body.attachments(messages),
            reasoning_effort=body.reasoning_effort,
            structured=True,
            request_key=owner,
            recovering=self.recovering,
            submitted=self.recovering,
            user_id=old.get("user_id") if self.recovering else None,
            user_hash=old.get("user_hash") if self.recovering else None,
            base_user_id=old.get("base_user_id") if self.recovering else old.get("user_id"),
            response_id=old.get("response_id") if self.recovering else None,
            marker_required=old.get("marker_required", True) if self.recovering else True,
            on_progress=self.checkpoint,
        )
        self.pending = None
        self.finished = False

    def reserve(self):
        if self.store and self.key:
            pending = {
                "status": "pending",
                "model": self.body.model,
                "request": self.request,
                "history": self.history,
                "context": self.context,
                "chat_url": self.browser.chat_url,
                "reply_id": self.browser.reply_id,
                "base_user_id": self.browser.base_user_id,
                "user_id": self.browser.user_id,
                "user_hash": self.browser.user_hash,
                "response_id": self.browser.response_id,
                "submitted": self.browser.submitted,
                "marker_required": self.browser.marker_required,
                "previous": self.old.get("previous")
                if self.recovering and self.old and self.old["status"] != "ready"
                else self.old,
            }
            self.store.replace(self.key, self.old, pending)
            self.pending = pending

    def checkpoint(self):
        if self.store and self.key and self.pending is not None:
            updated = {
                **self.pending,
                "chat_url": self.browser.chat_url,
                "user_id": self.browser.user_id,
                "user_hash": self.browser.user_hash,
                "base_user_id": self.browser.base_user_id,
                "response_id": self.browser.response_id,
                "submitted": self.browser.submitted,
            }
            if updated != self.pending:
                self.store.replace(self.key, self.pending, updated)
                self.pending = updated

    def commit(self, reply: dict):
        if self.store and self.key:
            if self.pending is None:
                raise SessionConflict("Cannot acknowledge a turn without its reservation")
            if (
                not self.browser.chat_url
                or not re.fullmatch(r"https://poe\.com/chat/[A-Za-z0-9_-]+", self.browser.chat_url)
                or not self.browser.reply_id
                or not self.browser.user_id
                or not self.browser.reply_received
                or self.browser.response_id != self.browser.reply_id
            ):
                raise SessionConflict("Poe request/reply identity was not acknowledged")
            # Identical response delivery retries must not mint another executable call ID.
            for index, call in enumerate(reply.get("tool_calls", [])):
                call["id"] = (
                    "call_" + digest({"owner": self.browser.request_key, "index": index})[:32]
                )
            assistant = self.body.canonical([Message.model_validate(reply)])[0]
            record = {
                "status": "ready",
                "model": self.body.model,
                "chat_url": self.browser.chat_url,
                "reply_id": self.browser.reply_id,
                "history": [*self.history, digest(assistant)],
                "context": self.context,
                "request": self.request,
                "submitted": True,
                "user_id": self.browser.user_id,
                "user_hash": self.browser.user_hash,
                "base_user_id": self.browser.base_user_id,
                "base_reply_id": self.pending["reply_id"],
                "response_id": self.browser.reply_id,
                "marker_required": self.browser.marker_required,
            }
            if (
                self.recovering
                and self.old is not None
                and self.old["status"] == "ready"
                and record["history"] != self.old["history"]
            ):
                raise SessionConflict(
                    "Poe reply changed after delivery; refusing a different replay"
                )
            self.store.replace(self.key, self.pending, record)
        self.finished = True

    def abort(self):
        if not self.finished and self.pending is not None and self.store and self.key:
            self.checkpoint()
            if self.recovering and self.old is not None and self.old["status"] == "ready":
                replacement = self.old
            else:
                replacement = (
                    {**self.pending, "status": "uncertain"} if self.browser.submitted else self.old
                )
            self.store.replace(self.key, self.pending, replacement)
            self.pending = None
