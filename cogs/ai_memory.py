"""Safe, bounded persistent memory for CalmBot's LLM conversations.

The store contains only short, model-proposed facts grounded in exact text from
one user's successful request. Raw messages, model replies, attachments, names,
and credentials are never persisted.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

MEMORY_FILE = os.path.join("data", "ai_memories.json")
MAX_MEMORIES_PER_USER = 12
MAX_MEMORY_CHARS = 240
MEMORY_TTL_DAYS = 180
MAX_CONTEXT_CHARS = 2400

# Intentionally conservative: a harmless memory is less important than avoiding
# durable storage of credentials, identifiers, or sensitive personal data.
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d .()\-]{7,}\d)(?!\w)")
_LONG_SECRET_RE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\beyJ[a-zA-Z0-9_-]{20,}\.|\b[a-f0-9]{32,}\b|\b[A-Za-z0-9_\-/+=]{40,}\b)",
    re.I,
)
_FORBIDDEN_RE = re.compile(
    r"\b(?:password|passwd|passphrase|api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"auth(?:entication|orization)?[ _-]?token|client[ _-]?secret|private[ _-]?key|seed[ _-]?phrase|"
    r"recovery[ _-]?phrase|social[ _-]?security|ssn|credit[ _-]?card|debit[ _-]?card|cvv|bank[ _-]?account|"
    r"government[ _-]?id|passport|driver'?s[ _-]?licen[cs]e|date[ _-]?of[ _-]?birth|birthday|"
    r"diagnos(?:is|ed)|medical|medication|therapy|therapist|disability|pregnan(?:t|cy)|"
    r"religion|religious|politic(?:s|al)|vot(?:e|ed|ing)|sexual(?:ity| orientation)?|"
    r"race|ethnicity|criminal|lawsuit|salary|income|debt|home[ _-]?address|street[ _-]?address|"
    r"lives? at|located at|my name is|legal name|phone number|email address|"
    r"ignore (?:all |the )?(?:previous|system)|system prompt|developer message)\b",
    re.I,
)
_ALLOWED_PREFIX_RE = re.compile(
    r"^(?:prefers?|likes?|dislikes?|uses?|works? (?:with|on)|enjoys?|is (?:learning|building|working on)|"
    r"has experience with|often uses?|usually uses?|wants? responses?|plays?|favorite\b|favourite\b)",
    re.I,
)

EXTRACTION_INSTRUCTIONS = """You extract optional long-term user memories after a successful conversation turn.
Return JSON only: an array of objects with keys "memory" and "quote". Return [] when nothing qualifies.
Each quote must be an exact contiguous quote from the supplied user message. Never infer beyond that quote.
Memory must be a short third-person-neutral fragment beginning with one of: Prefers, Likes, Dislikes, Uses,
Works with, Works on, Enjoys, Is learning, Is building, Is working on, Has experience with, Often uses,
Usually uses, Wants responses, Plays, Favorite.
Save only clearly durable, useful preferences, recurring tools/technologies, ongoing projects, hobbies, or response-style preferences.
Do not save transient requests, one-off tasks, guesses, jokes, instructions to the assistant, or facts stated by anyone else.
Never save secrets or credentials; contact details; identifiers; exact location; real name; financial, legal, medical,
political, religious, sexual, racial/ethnic, or similarly sensitive data; or information about another person.
Maximum 3 objects and 180 characters per memory."""


def _plain_text(value: Any, limit: int = MAX_MEMORY_CHARS) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.replace("\x00", " ").split())
    return text[:limit].strip(" -\t\r\n")


def _normalized(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def safe_memory_text(value: Any) -> str | None:
    """Return a normalized memory fragment, or None when it is unsafe/invalid."""
    text = _plain_text(value)
    if not text or len(text) < 4 or not _ALLOWED_PREFIX_RE.match(text):
        return None
    if "<@" in text or _EMAIL_RE.search(text) or _PHONE_RE.search(text):
        return None
    if _LONG_SECRET_RE.search(text) or _FORBIDDEN_RE.search(text):
        return None
    return text


def parse_memory_candidates(raw: Any, user_text: str) -> list[str]:
    """Parse model JSON and require every candidate to cite exact user text."""
    if not isinstance(raw, str) or not raw.strip() or not user_text.strip():
        return []
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("memories", [])
    if not isinstance(payload, list):
        return []

    source = _normalized(user_text)
    result: list[str] = []
    for item in payload[:3]:
        if not isinstance(item, dict):
            continue
        quote = _plain_text(item.get("quote"), limit=500)
        text = safe_memory_text(item.get("memory"))
        # Exact normalized grounding blocks extraction from assistant output or
        # model-invented context. Quotes themselves are never persisted.
        if not quote or _normalized(quote) not in source or text is None:
            continue
        if text.casefold() not in {entry.casefold() for entry in result}:
            result.append(text)
    return result


class MemoryStore:
    """Guild-scoped per-user memory with TTL, caps, and atomic private writes."""

    def __init__(
        self,
        path: str | os.PathLike[str] = MEMORY_FILE,
        *,
        max_entries: int = MAX_MEMORIES_PER_USER,
        ttl_days: int = MEMORY_TTL_DAYS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max(1, min(50, int(max_entries)))
        self.ttl_seconds = max(1, int(ttl_days)) * 86400
        self._now = now
        self._data: dict[str, Any] = {"version": 1, "users": {}}
        self._load()

    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{int(guild_id)}:{int(user_id)}"

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict) and isinstance(loaded.get("users"), dict):
                self._data = {"version": 1, "users": loaded["users"]}
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            # Corrupt/unreadable memory must fail closed, not break LLM chat.
            self._data = {"version": 1, "users": {}}
        self._purge_expired(save=False)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _bucket(self, guild_id: int, user_id: int, create: bool = False) -> dict[str, Any] | None:
        users = self._data["users"]
        key = self._key(guild_id, user_id)
        bucket = users.get(key)
        if not isinstance(bucket, dict):
            if not create:
                return None
            bucket = {"enabled": True, "items": []}
            users[key] = bucket
        if not isinstance(bucket.get("items"), list):
            bucket["items"] = []
        bucket["enabled"] = bool(bucket.get("enabled", True))
        return bucket

    def _purge_expired(self, save: bool = True) -> bool:
        cutoff = int(self._now()) - self.ttl_seconds
        changed = False
        for key, bucket in list(self._data["users"].items()):
            if not isinstance(bucket, dict):
                del self._data["users"][key]
                changed = True
                continue
            items = bucket.get("items", [])
            clean = [item for item in items if isinstance(item, dict) and int(item.get("updated_at", 0) or 0) >= cutoff]
            if clean != items:
                bucket["items"] = clean
                changed = True
            if not clean and bool(bucket.get("enabled", True)):
                del self._data["users"][key]
                changed = True
        if changed and save:
            self._save()
        return changed

    def enabled(self, guild_id: int, user_id: int) -> bool:
        bucket = self._bucket(guild_id, user_id)
        return True if bucket is None else bool(bucket["enabled"])

    def set_enabled(self, guild_id: int, user_id: int, enabled: bool) -> None:
        bucket = self._bucket(guild_id, user_id, create=True)
        assert bucket is not None
        bucket["enabled"] = bool(enabled)
        self._save()

    def list(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        self._purge_expired()
        bucket = self._bucket(guild_id, user_id)
        if bucket is None:
            return []
        return [dict(item) for item in bucket["items"] if isinstance(item, dict)]

    def add_many(self, guild_id: int, user_id: int, values: list[str]) -> int:
        if not self.enabled(guild_id, user_id):
            return 0
        bucket = self._bucket(guild_id, user_id, create=True)
        assert bucket is not None
        items = bucket["items"]
        now = int(self._now())
        added = 0
        for value in values[:3]:
            text = safe_memory_text(value)
            if text is None:
                continue
            duplicate = next((item for item in items if _normalized(item.get("text")) == _normalized(text)), None)
            if duplicate:
                duplicate["updated_at"] = now
                duplicate["text"] = text
                continue
            item_id = f"{now:x}{len(items):02x}"[-12:]
            items.append({"id": item_id, "text": text, "created_at": now, "updated_at": now})
            added += 1
        if len(items) > self.max_entries:
            items[:] = sorted(items, key=lambda item: int(item.get("updated_at", 0)))[-self.max_entries:]
        if added or values:
            self._save()
        return added

    def delete(self, guild_id: int, user_id: int, index: int) -> str | None:
        bucket = self._bucket(guild_id, user_id)
        if bucket is None or index < 1 or index > len(bucket["items"]):
            return None
        removed = bucket["items"].pop(index - 1)
        self._save()
        return str(removed.get("text", ""))

    def clear(self, guild_id: int, user_id: int) -> int:
        bucket = self._bucket(guild_id, user_id)
        if bucket is None:
            return 0
        count = len(bucket["items"])
        bucket["items"] = []
        if bucket["enabled"]:
            self._data["users"].pop(self._key(guild_id, user_id), None)
        self._save()
        return count

    def prompt_context(self, guild_id: int, user_id: int) -> str:
        if not self.enabled(guild_id, user_id):
            return ""
        items = self.list(guild_id, user_id)
        lines: list[str] = []
        used = 0
        for item in items:
            text = safe_memory_text(item.get("text"))
            if text is None or used + len(text) + 3 > MAX_CONTEXT_CHARS:
                continue
            lines.append(f"- {text}")
            used += len(text) + 3
        return "\n".join(lines)


# main.py loads every cogs/*.py file as an extension. This is an import-only
# support module, so expose an intentional no-op extension hook.
async def setup(bot: Any) -> None:
    return None
