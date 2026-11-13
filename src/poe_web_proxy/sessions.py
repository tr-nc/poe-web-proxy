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
    def __init__(self, body: AgentCompletion, identity: str | None, store: SessionStore | None):
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
        if self.old:
            if self.old["status"] != "ready":
                raise SessionConflict(
                    "Previous send is unacknowledged; start a new Pi session to avoid duplicate billing"
                )
            if self.old["model"] != body.model:
                raise SessionConflict(
                    "A Pi session is bound to one Poe model/chat; start a new session to switch models"
                )
            previous = self.old["history"]
            if self.history[: len(previous)] != previous or len(self.history) <= len(previous):
                raise SessionConflict(
                    "History diverged or was compacted; start a new Pi session instead of replaying history"
                )
            delta = history[len(previous) :]
            changed = self.context != self.old["context"]
            messages = [*context, *delta] if changed else delta
            self.prompt = body.prompt(messages, context=changed)
        else:
            messages = body.messages
            self.prompt = body.prompt()
        self.browser = BrowserTurn(
            chat_url=self.old["chat_url"] if self.old else None,
            reply_id=self.old["reply_id"] if self.old else None,
            images=body.attachments(messages),
            reasoning_effort=body.reasoning_effort,
        )
        self.pending = None
        self.finished = False

    def reserve(self):
        if self.store and self.key:
            pending = {
                "status": "pending",
                "model": self.body.model,
                "chat_url": self.browser.chat_url,
                "reply_id": self.browser.reply_id,
            }
            self.store.replace(self.key, self.old, pending)
            self.pending = pending

    def commit(self, reply: dict):
        if self.store and self.key:
            if (
                not self.browser.chat_url
                or not re.fullmatch(r"https://poe\.com/chat/[A-Za-z0-9_-]+", self.browser.chat_url)
                or not self.browser.reply_id
            ):
                raise SessionConflict(
                    "Poe chat identity was not acknowledged; start a new Pi session"
                )
            assistant = self.body.canonical([Message.model_validate(reply)])[0]
            record = {
                "status": "ready",
                "model": self.body.model,
                "chat_url": self.browser.chat_url,
                "reply_id": self.browser.reply_id,
                "history": [*self.history, digest(assistant)],
                "context": self.context,
            }
            self.store.replace(self.key, self.pending, record)
        self.finished = True

    def abort(self):
        if not self.finished and self.pending is not None and self.store and self.key:
            replacement = (
                {**self.pending, "status": "uncertain", "chat_url": self.browser.chat_url}
                if self.browser.submitted
                else self.old
            )
            self.store.replace(self.key, self.pending, replacement)
            self.pending = None
