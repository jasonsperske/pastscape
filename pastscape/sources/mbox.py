"""Read mbox files, plain or compressed, including Google Takeout exports.

Netscape's own Local Mail store was mbox, so this is the path for anyone who
still has a real ``Inbox`` file from Communicator. It is also the path for
Google Takeout, which is the same format wearing two extra layers:

    Takeout.zip
      └── Takeout/Mail/All mail Including Spam and Trash.mbox

Two things follow from that. First, the mbox is read as a stream rather than
decompressed to a temp file -- a full Gmail export is routinely tens of
gigabytes uncompressed, and nobody wants that in /tmp. Second, a Takeout mbox
has no folder structure at all: everything is in one file and the real
organisation lives in ``X-Gmail-Labels``. Splitting on those labels is what
turns a Takeout dump back into a folder tree.
"""

from __future__ import annotations

import bz2
import csv
import gzip
import logging
import lzma
import re
import tarfile
import zipfile
from pathlib import Path
from typing import BinaryIO, Iterator

from ..model import Message, normalize_folder
from .mime import message_from_bytes

log = logging.getLogger("pastscape.mbox")

# Suffixes stripped when turning a filename into a folder name.
_COMPRESSION_SUFFIXES = {".gz", ".bz2", ".xz", ".lzma", ".z"}
_MBOX_SUFFIXES = {".mbox", ".mbx", ".mbs"}

_OPENERS = {
    "gzip": gzip.open,
    "bz2": bz2.open,
    "xz": lzma.open,
    "none": lambda p, mode="rb": open(p, mode),
}

_MAGIC = [
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bz2"),
    (b"\xfd7zXZ\x00", "xz"),
]


def sniff_compression(path: Path) -> str:
    """Return 'gzip' | 'bz2' | 'xz' | 'none', by content not by extension."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return "none"
    for magic, name in _MAGIC:
        if head.startswith(magic):
            return name
    return "none"


def is_container(path: Path) -> bool:
    """True for a zip or tar holding mailboxes, e.g. a Takeout download."""
    try:
        if zipfile.is_zipfile(path):
            return True
    except OSError:
        return False
    try:
        return tarfile.is_tarfile(path)
    except (OSError, tarfile.TarError):
        return False


# ---------------------------------------------------------------------------
# Splitting an mbox stream into messages
# ---------------------------------------------------------------------------

# A real From_ line: "From <envelope-sender> Mon Jan  1 00:00:00 2020".
_RE_FROM_LINE = re.compile(rb"^From \S*\s+(Mon|Tue|Wed|Thu|Thr|Fri|Sat|Sun)[, ]", re.I)
_BLANK = (b"\n", b"\r\n", b"")


def iter_mbox_messages(stream: BinaryIO) -> Iterator[bytes]:
    """Yield each message's raw bytes from an mbox stream.

    A message starts at a ``From `` line that either looks like a real From_
    line or follows a blank line. Requiring one or the other keeps a body that
    happens to contain "From " at the start of a line from splitting a message
    in half, which is the classic way naive mbox readers corrupt an archive.
    """
    current: list[bytes] = []
    prev_blank = True  # start of file counts as a separator
    for line in stream:
        if line.startswith(b"From ") and (prev_blank or _RE_FROM_LINE.match(line)):
            if current:
                yield b"".join(current)
            current = []
            prev_blank = False
            continue  # the From_ line is an envelope separator, not a header
        current.append(line)
        prev_blank = line in _BLANK
    if current:
        yield b"".join(current)


# ---------------------------------------------------------------------------
# Gmail labels
# ---------------------------------------------------------------------------

# Labels that describe state rather than location.
GMAIL_STATE_LABELS = {
    "important", "starred", "unread", "opened", "archived", "all mail",
    "category personal", "category social", "category promotions",
    "category updates", "category forums",
}

# Gmail's system labels, in Communicator's vocabulary.
GMAIL_SYSTEM_FOLDERS = {
    "inbox": "Inbox",
    "sent": "Sent",
    "draft": "Drafts",
    "drafts": "Drafts",
    "trash": "Trash",
    "bin": "Trash",
    "spam": "Junk",
    "junk": "Junk",
}

# A message carries several labels at once; the first of these wins. Inbox
# beats a user label deliberately: mail sitting in the inbox belongs in Inbox,
# while a label on *archived* mail (which has no Inbox label) becomes a folder.
_FOLDER_PRIORITY = ["Drafts", "Sent", "Trash", "Junk", "Inbox"]


def split_labels(value: str) -> list[str]:
    """Gmail comma-separates labels and quotes any that contain a comma."""
    if not value:
        return []
    try:
        parts = next(csv.reader([value]))
    except Exception:
        parts = value.split(",")
    return [p.strip() for p in parts if p and p.strip()]


def folder_from_labels(labels: list[str]) -> str:
    """Pick one folder for a message that Gmail filed under several labels."""
    system, user = [], []
    for label in labels:
        low = label.lower()
        if low in GMAIL_SYSTEM_FOLDERS:
            system.append(GMAIL_SYSTEM_FOLDERS[low])
        elif low in GMAIL_STATE_LABELS or label.startswith("IMAP_"):
            continue
        else:
            user.append(label)

    for candidate in _FOLDER_PRIORITY:
        if candidate in system:
            return candidate
    if user:
        # Gmail writes nested labels as "Parent/Child", which is already the
        # shape our folder paths use.
        return normalize_folder(user[0])
    return "Archive"


def _header(msg: Message, name: str) -> str:
    lname = name.lower()
    for key, value in msg.headers:
        if key.lower() == lname:
            return value
    return ""


def apply_gmail_labels(msg: Message) -> bool:
    """Re-file a Takeout message by its labels. True if labels were present."""
    raw = _header(msg, "X-Gmail-Labels")
    if not raw:
        return False
    labels = split_labels(raw)
    lowered = {label.lower() for label in labels}
    msg.folder = folder_from_labels(labels)
    msg.unread = "unread" in lowered
    msg.flagged = "starred" in lowered
    return True


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def folder_for_filename(name: str) -> str:
    """'All mail Including Spam and Trash.mbox.gz' -> 'All mail …'."""
    stem = Path(name).name
    while True:
        suffix = Path(stem).suffix.lower()
        if suffix in _COMPRESSION_SUFFIXES or suffix in _MBOX_SUFFIXES:
            stem = stem[: -len(suffix)]
            continue
        break
    return normalize_folder(stem or "Inbox")


def read_mbox_stream(stream: BinaryIO, folder: str, source: str) -> Iterator[Message]:
    seen = 0
    for raw in iter_mbox_messages(stream):
        if not raw.strip():
            continue
        msg = message_from_bytes(raw, folder=folder, source=source)
        if msg is None:
            continue
        if not apply_gmail_labels(msg):
            # Plain mbox: the Status/X-Status headers are authoritative.
            flags = _header(msg, "Status") + _header(msg, "X-Status")
            msg.unread = "R" not in flags if flags else False
            msg.flagged = "F" in flags
        seen += 1
        yield msg
    log.info("  %d messages from %s", seen, source)


def read_mbox(path: Path) -> Iterator[Message]:
    if is_container(path):
        yield from _read_container(path)
        return

    compression = sniff_compression(path)
    opener = _OPENERS[compression]
    if compression != "none":
        log.info("reading %s (%s)", path.name, compression)
    with opener(path, "rb") as stream:
        yield from read_mbox_stream(stream, folder_for_filename(path.name), path.name)


def _is_mailbox_member(name: str) -> bool:
    lower = name.lower()
    while True:
        suffix = Path(lower).suffix
        if suffix in _COMPRESSION_SUFFIXES:
            lower = lower[: -len(suffix)]
            continue
        break
    return Path(lower).suffix in _MBOX_SUFFIXES


def _member_stream(raw: BinaryIO, name: str) -> BinaryIO:
    """Wrap a container member in a decompressor if it is itself compressed."""
    suffix = Path(name.lower()).suffix
    if suffix == ".gz":
        return gzip.open(raw, "rb")
    if suffix == ".bz2":
        return bz2.open(raw, "rb")
    if suffix in (".xz", ".lzma"):
        return lzma.open(raw, "rb")
    return raw


def _read_container(path: Path) -> Iterator[Message]:
    """Walk a zip or tar, reading every mailbox and .eml inside it."""
    members = 0
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if _is_mailbox_member(info.filename):
                    members += 1
                    with archive.open(info) as raw:
                        stream = _member_stream(raw, info.filename)
                        yield from read_mbox_stream(
                            stream, folder_for_filename(info.filename),
                            f"{path.name}:{info.filename}",
                        )
                elif info.filename.lower().endswith(".eml"):
                    members += 1
                    msg = message_from_bytes(
                        archive.read(info), folder=_eml_folder(info.filename),
                        source=f"{path.name}:{info.filename}",
                    )
                    if msg:
                        apply_gmail_labels(msg)
                        yield msg
    else:
        with tarfile.open(path, "r:*") as archive:
            for info in archive:
                if not info.isfile():
                    continue
                if _is_mailbox_member(info.name):
                    members += 1
                    raw = archive.extractfile(info)
                    if raw is None:
                        continue
                    stream = _member_stream(raw, info.name)
                    yield from read_mbox_stream(
                        stream, folder_for_filename(info.name),
                        f"{path.name}:{info.name}",
                    )
                elif info.name.lower().endswith(".eml"):
                    members += 1
                    raw = archive.extractfile(info)
                    if raw is None:
                        continue
                    msg = message_from_bytes(
                        raw.read(), folder=_eml_folder(info.name),
                        source=f"{path.name}:{info.name}",
                    )
                    if msg:
                        apply_gmail_labels(msg)
                        yield msg

    if not members:
        raise RuntimeError(
            f"{path.name} contains no .mbox or .eml files. If this is a Google "
            "Takeout download, check that Mail was included in the export."
        )


def _eml_folder(name: str) -> str:
    parent = Path(name).parent.name
    return normalize_folder(parent) if parent else "Inbox"
