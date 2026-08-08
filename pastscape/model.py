"""Core data model shared by every source reader and the renderer."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

# Folders Netscape Communicator always showed, in its order. Any folder we do
# not recognise is appended after these.
#
# Junk is the one addition. Communicator 4 predates junk filtering, but we do
# map Gmail's Spam and Outlook's Junk onto it, and a system folder reads badly
# sorted alphabetically in among someone's own labels.
CANONICAL_FOLDER_ORDER = [
    "Inbox",
    "Unsent Messages",
    "Drafts",
    "Templates",
    "Sent",
    "Trash",
    "Junk",
    "Samples",
]

# Names other mail systems use for the same concept, lowercased.
FOLDER_ALIASES = {
    "inbox": "Inbox",
    "in": "Inbox",
    "outbox": "Unsent Messages",
    "unsent messages": "Unsent Messages",
    "drafts": "Drafts",
    "draft": "Drafts",
    "templates": "Templates",
    "sent": "Sent",
    "sent items": "Sent",
    "sent mail": "Sent",
    "sent messages": "Sent",
    "deleted items": "Trash",
    "trash": "Trash",
    "junk": "Junk",
    "junk e-mail": "Junk",
    "spam": "Junk",
    "archive": "Archive",
}


@dataclass(slots=True)
class Address:
    name: str = ""
    addr: str = ""

    def display(self) -> str:
        """What Communicator put in the Sender column: name if known, else address."""
        return self.name or self.addr or "(unknown)"

    def full(self) -> str:
        if self.name and self.addr:
            return f'"{self.name}" <{self.addr}>'
        return self.addr or self.name

    def as_json(self) -> dict:
        return {"n": self.name, "a": self.addr}


@dataclass(slots=True)
class Attachment:
    filename: str
    content_type: str
    size: int
    payload: bytes = b""
    # Filled in by the renderer once the blob is written to disk.
    href: str = ""
    content_id: str = ""

    def as_json(self) -> dict:
        return {
            "name": self.filename,
            "type": self.content_type,
            "size": self.size,
            "href": self.href,
        }


@dataclass(slots=True)
class Message:
    """One archived message, normalised away from whatever source produced it."""

    uid: str = ""
    folder: str = "Inbox"
    message_id: str = ""
    subject: str = ""
    sender: Address = field(default_factory=Address)
    reply_to: Address | None = None
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    date: datetime | None = None
    date_raw: str = ""
    organization: str = ""
    newsgroups: str = ""
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    priority: str = ""
    unread: bool = False
    flagged: bool = False
    has_attachments: bool = False
    body_text: str = ""
    body_html: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    size: int = 0
    source: str = ""

    # ---------------------------------------------------------------- helpers

    def sort_key(self) -> tuple[float, str]:
        ts = self.date.timestamp() if self.date else 0.0
        return (ts, self.uid)

    def thread_key(self) -> str:
        """First entry of References, else In-Reply-To, else own id.

        Good enough to group a conversation the way Communicator's threaded
        view did, without needing a full JWZ threading pass.
        """
        if self.references:
            return self.references[0]
        if self.in_reply_to:
            return self.in_reply_to
        return self.message_id or self.uid

    def normalized_subject(self) -> str:
        return normalize_subject(self.subject)

    def search_text(self) -> str:
        parts = [
            self.subject,
            self.sender.name,
            self.sender.addr,
            self.organization,
        ]
        parts += [a.name for a in self.to] + [a.addr for a in self.to]
        parts += [a.name for a in self.cc] + [a.addr for a in self.cc]
        parts += [a.filename for a in self.attachments]
        parts.append(self.body_text)
        return "\n".join(p for p in parts if p)


_RE_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv|vs|antwort)\s*(?:\[\d+\])?\s*:\s*)+", re.I)
_RE_WS = re.compile(r"\s+")


def normalize_subject(subject: str) -> str:
    s = _RE_SUBJECT_PREFIX.sub("", subject or "")
    return _RE_WS.sub(" ", s).strip()


def normalize_folder(name: str) -> str:
    """Map a source folder name onto Communicator's vocabulary."""
    clean = (name or "").strip().strip("/")
    if not clean:
        return "Inbox"
    head, _, tail = clean.partition("/")
    mapped = FOLDER_ALIASES.get(head.strip().lower())
    if mapped:
        head = mapped
    else:
        head = head.strip()
    return f"{head}/{tail}" if tail else head


UNDATED_FOLDER = "Undated"
_RE_YEAR = re.compile(r"(?:19|20)\d\d")


def year_folder(msg: "Message") -> str:
    """Which year bucket a message belongs to."""
    return str(msg.date.year) if msg.date else UNDATED_FOLDER


def folder_sort_key(path: str) -> tuple:
    """Years newest-first, then Communicator's folders in its order, then A-Z.

    Applied per path segment and at every depth, so "2011/Sent" sorts after
    "2011/Inbox" the same way a top-level Sent would -- and an archive that
    spans twenty years opens on the most recent one rather than on 2004.
    """
    key: list = []
    for part in path.split("/"):
        if _RE_YEAR.fullmatch(part):
            key.append((0, -int(part), ""))
        elif part in CANONICAL_FOLDER_ORDER:
            key.append((1, CANONICAL_FOLDER_ORDER.index(part), ""))
        else:
            key.append((2, 0, part.lower()))
    return tuple(key)


# ---------------------------------------------------------------------------
# Stable identity
# ---------------------------------------------------------------------------

_RE_MSGID_CLEAN = re.compile(r"[<>\s]")


def clean_message_id(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(r"<([^<>]+)>", raw)
    token = m.group(1) if m else raw
    return _RE_MSGID_CLEAN.sub("", token).strip()


def parse_references(raw: str) -> list[str]:
    if not raw:
        return []
    return [clean_message_id(tok) for tok in re.findall(r"<[^<>]+>", raw)]


def compute_uid(msg: Message) -> str:
    """Content-addressed id, stable across rebuilds and across sources.

    A Message-ID is authoritative when present; otherwise we fingerprint the
    fields a mail system cannot rewrite. This is what lets a second run over a
    grown PST recognise the messages it already published.
    """
    h = hashlib.sha1()
    if msg.message_id:
        h.update(b"mid\0")
        h.update(msg.message_id.encode("utf-8", "replace"))
    else:
        h.update(b"syn\0")
        for piece in (
            msg.date.isoformat() if msg.date else msg.date_raw,
            msg.sender.addr.lower(),
            msg.normalized_subject().lower(),
            ",".join(sorted(a.addr.lower() for a in msg.to)),
            (msg.body_text or msg.body_html)[:4096],
        ):
            h.update((piece or "").encode("utf-8", "replace"))
            h.update(b"\0")
    return h.hexdigest()[:20]


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------


def ensure_tz(dt: datetime | None) -> datetime | None:
    """Make a datetime comparable without rewriting it.

    The sender's offset is part of the record -- Communicator showed
    "12:00:00 -0800", not the UTC equivalent -- so we only fill in a zone when
    the source gave us none. Sorting and JSON both go through .timestamp(),
    which is offset-correct either way.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def html_to_text(html: str) -> str:
    """Cheap HTML -> text, used only when a message has no text/plain part."""
    from .sanitize import strip_tags

    return strip_tags(html)


_RE_TOKEN = re.compile(r"[a-z0-9][a-z0-9'._+\-]*", re.I)


_RE_SUBTOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> Iterable[str]:
    """Fold accents, lowercase, split on non-word runs, drop 1-char noise.

    Dotted tokens are emitted whole *and* in pieces: ``whiteboard.gif`` also
    yields ``whiteboard`` and ``gif``, and ``jwz@example.org`` yields ``jwz``
    and ``example``. Keeping the whole token means an exact address still
    matches as one term; the pieces are what make a search for "gif" find the
    attachment.
    """
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c)).lower()
    for match in _RE_TOKEN.finditer(folded):
        tok = match.group(0).strip("'._+-")
        if not (1 < len(tok) <= 60):
            continue
        yield tok
        if any(c in tok for c in "._+-"):
            for part in _RE_SUBTOKEN.findall(tok):
                if 1 < len(part) <= 40 and part != tok:
                    yield part
