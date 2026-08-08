"""Read a folder of .eml files (or a Maildir) into messages.

Directory structure becomes folder structure, so ``export/Sent/2003/x.eml``
lands in the ``Sent/2003`` folder of the archive.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

from ..model import Message, normalize_folder
from .mime import message_from_bytes

log = logging.getLogger("pastscape.eml")

SKIP_NAMES = {".DS_Store", "Thumbs.db"}
MAILDIR_PARTS = {"cur", "new", "tmp"}

# Maildir names files things like "1085causal.host:2,S", so an extension
# allow-list would throw the whole mailbox away. Deny the things that are
# obviously not messages instead, and let the header sniff below reject the
# rest.
SKIP_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".ico",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf",
    ".zip", ".gz", ".bz2", ".xz", ".tar", ".7z", ".rar",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".exe", ".dll", ".so", ".dylib",
    ".json", ".css", ".js", ".py", ".db", ".sqlite", ".pst", ".ost", ".mbox",
}

# A file is treated as a message only if one of these appears in its head.
_RE_LOOKS_LIKE_MAIL = re.compile(
    rb"^(?:From |Received:|From:|Date:|Subject:|Message-ID:|MIME-Version:|To:|Return-Path:|X-)",
    re.I | re.M,
)


def _folder_for(path: Path, root: Path) -> str:
    rel = path.relative_to(root).parent
    parts = [p for p in rel.parts if p not in MAILDIR_PARTS]
    if not parts:
        return "Inbox"
    return normalize_folder("/".join(parts))


def read_eml_file(path: Path, folder: str = "Inbox", root: Path | None = None) -> Message | None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return None
    if not raw.strip():
        return None
    # .emlx (Apple Mail) prefixes the message with a byte-count line.
    if path.suffix.lower() == ".emlx":
        first, _, rest = raw.partition(b"\n")
        if first.strip().isdigit():
            raw = rest
    src = str(root and path.relative_to(root) or path)
    return message_from_bytes(raw, folder=folder, source=src)


def looks_like_message(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    if not head.strip():
        return False
    # .emlx starts with a decimal byte count on its own line.
    if path.suffix.lower() == ".emlx":
        first, _, rest = head.partition(b"\n")
        if first.strip().isdigit():
            head = rest
    return bool(_RE_LOOKS_LIKE_MAIL.search(head))


def read_eml_dir(root: Path) -> Iterator[Message]:
    root = root.resolve()
    count = skipped = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in SKIP_NAMES or path.name.startswith("."):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if not looks_like_message(path):
            skipped += 1
            continue
        msg = read_eml_file(path, folder=_folder_for(path, root), root=root)
        if msg is None:
            continue
        if not msg.sender.addr and not msg.subject and not msg.headers:
            continue
        count += 1
        yield msg
    log.info("read %d messages from %s%s", count, root,
             f" ({skipped} non-message files skipped)" if skipped else "")
