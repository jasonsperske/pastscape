"""Build manifest: what makes a second run incremental.

``manifest.json`` sits at the root of the generated site and records, per
message, the identity and content hash of what was published. On the next run
we can tell new from unchanged and only touch what moved.

The manifest is a cache, not the truth. Every message page also carries its own
metadata block, so :func:`reconstruct_from_site` can rebuild the manifest from
the published HTML if the file is lost, or if the site was copied somewhere
without it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .model import Message

log = logging.getLogger("pastscape.state")

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1


def content_hash(msg: Message) -> str:
    """Fingerprint of everything we render, so a re-import that changes a body
    or gains an attachment is detected as a change rather than a no-op."""
    h = hashlib.sha1()
    for piece in (
        msg.folder,
        msg.subject,
        msg.sender.full(),
        msg.date.isoformat() if msg.date else "",
        ",".join(a.full() for a in msg.to),
        ",".join(a.full() for a in msg.cc),
        msg.body_text,
        msg.body_html,
        msg.priority,
    ):
        h.update((piece or "").encode("utf-8", "replace"))
        h.update(b"\0")
    for att in msg.attachments:
        h.update(f"{att.filename}:{att.size}:{att.content_type}\0".encode("utf-8", "replace"))
    return h.hexdigest()[:16]


@dataclass
class MessageRecord:
    uid: str
    folder: str
    hash: str
    date: str = ""
    subject: str = ""
    page: str = ""
    attachments: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_built: str = ""
    # "<prev-uid>|<next-uid>" in the published folder order. Stored because a
    # new message changes its neighbours' pages even though their content did
    # not change, and we would otherwise leave dead prev/next links behind.
    nav: str = ""

    def as_json(self) -> dict:
        return {
            "uid": self.uid,
            "folder": self.folder,
            "hash": self.hash,
            "date": self.date,
            "subject": self.subject,
            "page": self.page,
            "attachments": self.attachments,
            "first_seen": self.first_seen,
            "last_built": self.last_built,
            "nav": self.nav,
        }

    @classmethod
    def from_json(cls, data: dict) -> "MessageRecord":
        return cls(
            uid=data.get("uid", ""),
            folder=data.get("folder", ""),
            hash=data.get("hash", ""),
            date=data.get("date", ""),
            subject=data.get("subject", ""),
            page=data.get("page", ""),
            attachments=list(data.get("attachments", [])),
            first_seen=data.get("first_seen", ""),
            last_built=data.get("last_built", ""),
            nav=data.get("nav", ""),
        )


@dataclass
class Manifest:
    version: int = MANIFEST_VERSION
    generator: str = "pastscape"
    built: str = ""
    title: str = "Pastscape"
    sources: dict[str, dict] = field(default_factory=dict)
    messages: dict[str, MessageRecord] = field(default_factory=dict)
    # Per source file: {"size": int, "mtime": int, "uids": [...]} -- lets a
    # directory source skip files that have not been touched since last build.
    files: dict[str, dict] = field(default_factory=dict)

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, site_dir: Path) -> "Manifest":
        path = site_dir / MANIFEST_NAME
        if not path.is_file():
            recovered = reconstruct_from_site(site_dir)
            if recovered.messages:
                log.info("no manifest.json; recovered %d messages from published pages",
                         len(recovered.messages))
            return recovered
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("manifest unreadable (%s); rebuilding from published pages", exc)
            return reconstruct_from_site(site_dir)
        if data.get("version") != MANIFEST_VERSION:
            log.info("manifest version %s != %s; treating as full rebuild",
                     data.get("version"), MANIFEST_VERSION)
            return cls()
        man = cls(
            version=data.get("version", MANIFEST_VERSION),
            generator=data.get("generator", "pastscape"),
            built=data.get("built", ""),
            title=data.get("title", "Pastscape"),
            sources=data.get("sources", {}),
            files=data.get("files", {}),
        )
        man.messages = {
            uid: MessageRecord.from_json(rec)
            for uid, rec in (data.get("messages") or {}).items()
        }
        return man

    def save(self, site_dir: Path) -> None:
        payload = {
            "version": self.version,
            "generator": self.generator,
            "built": self.built or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "title": self.title,
            "sources": self.sources,
            "files": self.files,
            "messages": {uid: rec.as_json() for uid, rec in sorted(self.messages.items())},
        }
        site_dir.mkdir(parents=True, exist_ok=True)
        tmp = site_dir / (MANIFEST_NAME + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=False), "utf-8")
        tmp.replace(site_dir / MANIFEST_NAME)


_RE_META_BLOCK = re.compile(
    r'<script type="application/json" id="ps-meta">(.*?)</script>', re.S
)


def reconstruct_from_site(site_dir: Path) -> Manifest:
    """Rebuild a manifest by reading the metadata embedded in message pages."""
    man = Manifest()
    msg_dir = site_dir / "msg"
    if not msg_dir.is_dir():
        return man
    for page in msg_dir.rglob("*.html"):
        try:
            text = page.read_text("utf-8", errors="replace")
        except OSError:
            continue
        m = _RE_META_BLOCK.search(text)
        if not m:
            continue
        try:
            meta = json.loads(m.group(1))
        except ValueError:
            continue
        uid = meta.get("uid")
        if not uid:
            continue
        man.messages[uid] = MessageRecord(
            uid=uid,
            folder=meta.get("folder", ""),
            hash=meta.get("hash", ""),
            date=meta.get("date", ""),
            subject=meta.get("subject", ""),
            page=str(page.relative_to(site_dir)).replace("\\", "/"),
            attachments=[a.get("href", "") for a in meta.get("attachments", [])],
            first_seen=meta.get("first_seen", ""),
            last_built=meta.get("built", ""),
            nav=meta.get("nav", ""),
        )
    return man
