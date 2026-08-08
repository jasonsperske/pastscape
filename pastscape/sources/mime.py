"""Turn a :mod:`email` message object into a Pastscape :class:`Message`.

Shared by the EML, Maildir and mbox readers, and reused by the PST reader for
any item that carries its own RFC-822 transport headers.
"""

from __future__ import annotations

import email
import email.policy  # noqa: F401  -- `import email` alone does not bind this
import logging
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message as PyMessage
from email.utils import getaddresses, parsedate_to_datetime

from ..model import (
    Address,
    Attachment,
    Message,
    clean_message_id,
    parse_references,
)

log = logging.getLogger("pastscape.mime")

MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024

PRIORITY_MAP = {
    "1": "Highest",
    "2": "High",
    "3": "Normal",
    "4": "Low",
    "5": "Lowest",
    "high": "High",
    "normal": "Normal",
    "low": "Low",
    "urgent": "Highest",
    "non-urgent": "Lowest",
}


def decode_str(value) -> str:
    """Decode an RFC 2047 header into text, tolerating broken encodings."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = str(value)
    if "=?" not in value:
        return _clean(value)
    try:
        return _clean(str(make_header(decode_header(value))))
    except Exception:
        return _clean(value)


_RE_HDR_WS = re.compile(r"[\r\n\t]+")


def _clean(value: str) -> str:
    return _RE_HDR_WS.sub(" ", value).strip()


def parse_addresses(raw) -> list[Address]:
    if not raw:
        return []
    if not isinstance(raw, str):
        raw = str(raw)
    out: list[Address] = []
    for name, addr in getaddresses([raw]):
        name = decode_str(name)
        addr = _clean(addr)
        if not name and not addr:
            continue
        # getaddresses sometimes hands back a display name in the addr slot.
        if addr and "@" not in addr and not name:
            name, addr = addr, ""
        out.append(Address(name=name, addr=addr))
    return out


def parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Guard against absurd values that would break sorting/formatting.
    if not (1970 <= dt.year <= 2100):
        return None
    return dt


def _payload_bytes(part: PyMessage) -> bytes:
    try:
        data = part.get_payload(decode=True)
    except Exception:
        data = None
    if data is None:
        raw = part.get_payload()
        if isinstance(raw, str):
            data = raw.encode("utf-8", "replace")
        else:
            data = b""
    return data


def _decode_text(part: PyMessage) -> str:
    data = _payload_bytes(part)
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _is_attachment(part: PyMessage) -> bool:
    disposition = (part.get("Content-Disposition") or "").lower()
    if disposition.startswith("attachment"):
        return True
    if part.get_filename():
        return True
    return False


def walk_parts(msg: PyMessage) -> tuple[str, str, list[Attachment]]:
    """Extract (text, html, attachments) with multipart/alternative handled."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []

    def visit(part: PyMessage, in_alternative: bool = False) -> None:
        ctype = (part.get_content_type() or "text/plain").lower()
        if part.is_multipart():
            subtype = (part.get_content_subtype() or "mixed").lower()
            children = part.get_payload()
            if not isinstance(children, list):
                return
            if subtype == "alternative":
                # Collect every alternative; the renderer prefers HTML.
                for child in children:
                    visit(child, in_alternative=True)
            else:
                for child in children:
                    visit(child, in_alternative=in_alternative)
            return

        if ctype == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list) and payload:
                inner = payload[0]
                raw = inner.as_bytes() if hasattr(inner, "as_bytes") else b""
                subject = decode_str(inner.get("Subject")) or "Forwarded message"
                attachments.append(
                    Attachment(
                        filename=_safe_filename(f"{subject}.eml"),
                        content_type="message/rfc822",
                        size=len(raw),
                        payload=raw[:MAX_ATTACHMENT_BYTES],
                    )
                )
            return

        if _is_attachment(part) or not ctype.startswith("text/"):
            data = _payload_bytes(part)
            name = decode_str(part.get_filename()) or _default_name(ctype)
            cid = (part.get("Content-ID") or "").strip("<> \t")
            attachments.append(
                Attachment(
                    filename=_safe_filename(name),
                    content_type=ctype,
                    size=len(data),
                    payload=data[:MAX_ATTACHMENT_BYTES],
                    content_id=cid,
                )
            )
            return

        body = _decode_text(part)
        if ctype == "text/html":
            html_parts.append(body)
        else:
            text_parts.append(body)

    visit(msg)
    return ("\n".join(text_parts).strip(), "\n".join(html_parts).strip(), attachments)


_RE_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ]+")


def _safe_filename(name: str) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _RE_UNSAFE_NAME.sub("_", name).strip(" .") or "attachment"
    return name[:120]


def _default_name(ctype: str) -> str:
    ext = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "application/pdf": "pdf",
        "text/calendar": "ics",
    }.get(ctype, "bin")
    return f"attachment.{ext}"


def message_from_pymessage(pym: PyMessage, folder: str, source: str, raw_size: int = 0) -> Message:
    msg = Message(folder=folder, source=source)
    msg.subject = decode_str(pym.get("Subject"))
    senders = parse_addresses(pym.get("From"))
    msg.sender = senders[0] if senders else Address()
    reply_to = parse_addresses(pym.get("Reply-To"))
    msg.reply_to = reply_to[0] if reply_to else None
    msg.to = parse_addresses(pym.get("To"))
    msg.cc = parse_addresses(pym.get("Cc"))
    msg.date_raw = decode_str(pym.get("Date"))
    msg.date = parse_date(pym.get("Date"))
    msg.message_id = clean_message_id(pym.get("Message-ID") or pym.get("Message-Id") or "")
    msg.in_reply_to = clean_message_id(pym.get("In-Reply-To") or "")
    msg.references = parse_references(pym.get("References") or "")
    msg.organization = decode_str(pym.get("Organization") or pym.get("Organisation"))
    msg.newsgroups = decode_str(pym.get("Newsgroups"))

    prio = (pym.get("X-Priority") or pym.get("Importance") or pym.get("Priority") or "").strip()
    prio_key = prio.split()[0].lower() if prio else ""
    msg.priority = PRIORITY_MAP.get(prio_key, "")
    if msg.priority == "Normal":
        msg.priority = ""

    status = (pym.get("Status") or "") + (pym.get("X-Status") or "")
    msg.unread = "R" not in status if status else False
    msg.flagged = "F" in status

    msg.body_text, msg.body_html, msg.attachments = walk_parts(pym)
    msg.has_attachments = bool(msg.attachments)
    msg.headers = [(k, decode_str(v)) for k, v in pym.items()]
    msg.size = raw_size or len(msg.body_text) + len(msg.body_html)
    return msg


def message_from_bytes(raw: bytes, folder: str, source: str) -> Message | None:
    try:
        pym = email.message_from_bytes(raw, policy=email.policy.compat32)
    except Exception as exc:
        # Log rather than swallow: a silent None here reads as "empty mailbox",
        # which is the worst possible way for a parsing bug to present.
        log.warning("could not parse %s: %s: %s", source, type(exc).__name__, exc)
        return None
    return message_from_pymessage(pym, folder=folder, source=source, raw_size=len(raw))
