"""Bounded, SSRF-safe preparation of Discord attachments for LLM input.

Only Discord-owned attachment CDN URLs are downloaded.  Arbitrary URLs in
message text are deliberately never fetched here.  Text/code is decoded as
strict UTF-8 and images are converted to data URLs so the model provider does
not need to retrieve a user-controlled URL.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any
from urllib.parse import urljoin, urlsplit

MAX_ATTACHMENTS = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 32 * 1024
MAX_TEXT_CHARS = 16_000
MAX_REDIRECTS = 2

# Discord attachment URLs currently use these hosts. Keep this exact rather
# than accepting arbitrary subdomains; that is the SSRF trust boundary.
TRUSTED_ATTACHMENT_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".swift", ".lua", ".r", ".sql",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".html", ".htm", ".css", ".scss", ".xml", ".graphql", ".gql",
})
TEXT_FILENAMES = frozenset({"dockerfile", "makefile", "justfile", "procfile"})
TEXT_MIME_TYPES = frozenset({
    "application/json", "application/ld+json", "application/xml",
    "application/javascript", "application/x-javascript", "application/sql",
    "application/toml", "application/yaml", "application/x-yaml",
})


@dataclass
class CollectedAttachments:
    text: str = ""
    image_data_urls: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def safe_filename(value: Any) -> str:
    """Return a short display-only filename with paths/control chars removed."""
    name = PurePath(str(value or "attachment").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]+", "_", name).strip(" .")
    return (name or "attachment")[:100]


def is_safe_discord_attachment_url(url: Any) -> bool:
    """Allow HTTPS Discord CDN attachment URLs only, with no URL credentials."""
    try:
        parsed = urlsplit(str(url))
        host = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme.lower() == "https"
            and host in TRUSTED_ATTACHMENT_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and bool(parsed.path)
        )
    except (TypeError, ValueError):
        return False


def _candidate_kind(filename: str, content_type: str) -> str | None:
    mime = content_type.partition(";")[0].strip().lower()
    suffix = PurePath(filename.lower()).suffix
    basename = PurePath(filename.lower()).name
    if mime == "image/svg+xml" or suffix == ".svg":
        return None
    if mime in {"image/png", "image/jpeg", "image/webp", "image/gif"} or suffix in IMAGE_EXTENSIONS:
        return "image"
    if mime.startswith("text/") or mime in TEXT_MIME_TYPES or suffix in TEXT_EXTENSIONS or basename in TEXT_FILENAMES:
        return "text"
    return None


def detect_image_mime(data: bytes) -> str | None:
    """Validate supported raster formats by file signature, not user metadata."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_safe_text(data: bytes) -> str | None:
    """Strictly decode inert UTF-8 text and reject binary/control-heavy payloads."""
    if not data or b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return None
    controls = sum(ord(char) < 32 and char not in "\n\r\t" for char in text)
    if controls > max(2, len(text) // 100):
        return None
    text = "".join(char for char in text if ord(char) >= 32 or char in "\n\t")
    return text.replace("\r\n", "\n").replace("\r", "\n")[:MAX_TEXT_CHARS]


async def _read_limited(session: Any, url: str, limit: int) -> bytes:
    """Download with a hard streaming limit and validated redirect targets."""
    current = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        if not is_safe_discord_attachment_url(current):
            raise ValueError("unsafe attachment URL")
        async with session.get(current, allow_redirects=False) as response:
            if response.status in {301, 302, 303, 307, 308}:
                if redirect_count >= MAX_REDIRECTS:
                    raise ValueError("too many attachment redirects")
                target = urljoin(current, response.headers.get("Location", ""))
                if not is_safe_discord_attachment_url(target):
                    raise ValueError("unsafe attachment redirect")
                current = target
                continue
            if response.status != 200:
                raise ValueError(f"attachment returned HTTP {response.status}")
            try:
                declared = int(response.headers.get("Content-Length", "0") or 0)
            except ValueError:
                declared = 0
            if declared > limit:
                raise ValueError("attachment exceeds byte limit")
            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                if len(body) > limit:
                    raise ValueError("attachment exceeds byte limit")
            return bytes(body)
    raise ValueError("attachment redirect failure")


async def collect_attachments(session: Any, attachments: Any) -> CollectedAttachments:
    """Fetch up to three supported attachments under per-file and aggregate caps."""
    result = CollectedAttachments()
    text_sections: list[str] = []
    total_bytes = 0
    text_chars = 0

    for attachment in list(attachments or ())[:MAX_ATTACHMENTS]:
        filename = safe_filename(getattr(attachment, "filename", "attachment"))
        content_type = str(getattr(attachment, "content_type", "") or "")
        kind = _candidate_kind(filename, content_type)
        if kind is None:
            result.skipped.append(f"{filename} (unsupported type)")
            continue
        url = str(getattr(attachment, "url", "") or "")
        if not is_safe_discord_attachment_url(url):
            result.skipped.append(f"{filename} (untrusted URL)")
            continue

        per_file_limit = MAX_IMAGE_BYTES if kind == "image" else MAX_TEXT_BYTES
        remaining = MAX_TOTAL_BYTES - total_bytes
        limit = min(per_file_limit, remaining)
        declared = int(getattr(attachment, "size", 0) or 0)
        if limit <= 0 or declared < 0 or declared > limit:
            result.skipped.append(f"{filename} (too large)")
            continue
        try:
            data = await _read_limited(session, url, limit)
        except Exception as exc:  # Network errors are deliberately non-fatal per attachment.
            result.skipped.append(f"{filename} ({str(exc)[:80] or 'download failed'})")
            continue
        total_bytes += len(data)

        if kind == "image":
            mime = detect_image_mime(data)
            if mime is None:
                result.skipped.append(f"{filename} (invalid image data)")
                continue
            encoded = base64.b64encode(data).decode("ascii")
            result.image_data_urls.append(f"data:{mime};base64,{encoded}")
            continue

        text = decode_safe_text(data)
        if text is None:
            result.skipped.append(f"{filename} (not UTF-8 text)")
            continue
        remaining_chars = MAX_TEXT_CHARS - text_chars
        if remaining_chars <= 0:
            result.skipped.append(f"{filename} (text budget exhausted)")
            continue
        text = text[:remaining_chars]
        text_chars += len(text)
        text_sections.append(
            f'--- BEGIN UNTRUSTED ATTACHMENT "{filename}" ---\n'
            f"{text}\n"
            f'--- END UNTRUSTED ATTACHMENT "{filename}" ---'
        )

    result.text = "\n\n".join(text_sections)
    return result
