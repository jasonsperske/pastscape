"""The build's working set, kept in a SQLite file instead of in RAM.

A full Google Takeout mbox is routinely 20 GB and 300,000 messages. Held as
:class:`~pastscape.model.Message` objects those cost roughly 170 KB each --
about 50 GB, which no laptop has. The source readers already stream, so the
fix is to stop accumulating: each message is written here as it is read, and
every later stage queries this file rather than walking a list.

The split between the two tables is the point of the design. ``bodies`` holds
the one expensive column, zlib'd, and is read exactly once, when a page is
rendered. ``msgs`` holds only what the folder tree, the listings and the
account inference need, which lets those stages sort and group the whole
archive without ever touching a body. Keeping the blob out of ``msgs`` also
means the second pass can stamp a slug and a row index onto every message
without rewriting gigabytes of row pages.

The file is scratch, not state: it lives beside the site while a build runs
and is deleted afterwards. ``manifest.json`` remains the durable record.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import zlib
from datetime import datetime
from pathlib import Path
from typing import Iterator, NamedTuple

from .model import Address, Attachment, Message

log = logging.getLogger("pastscape.spill")

# Level 1: bodies are text and compress well even in a hurry, and this runs
# once per message on the critical path of a multi-hour build.
_ZLIB_LEVEL = 1

# Rows buffered before a commit. Large enough that the per-statement overhead
# disappears, small enough that the write-ahead buffer stays small.
_BATCH = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS msgs (
    id         INTEGER PRIMARY KEY,
    uid        TEXT    NOT NULL UNIQUE,
    raw_folder TEXT    NOT NULL,
    folder     TEXT    NOT NULL DEFAULT '',
    acct       TEXT    NOT NULL DEFAULT '',
    year       TEXT    NOT NULL DEFAULT '',
    date_ts    INTEGER NOT NULL DEFAULT 0,
    unread     INTEGER NOT NULL DEFAULT 0,
    body_len   INTEGER NOT NULL DEFAULT 0,
    row        TEXT    NOT NULL DEFAULT '[]',
    atts       TEXT    NOT NULL DEFAULT '[]',
    slug       TEXT    NOT NULL DEFAULT '',
    row_idx    INTEGER NOT NULL DEFAULT 0,
    prev_uid   TEXT    NOT NULL DEFAULT '',
    next_uid   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bodies (
    id   INTEGER PRIMARY KEY,
    blob BLOB NOT NULL
);
"""


class Published(NamedTuple):
    """One message as the render pass sees it."""

    gid: int
    uid: str
    folder: str
    atts: list[str]
    prev_uid: str
    next_uid: str
    slug: str
    row_idx: int
    blob: bytes

    def message(self) -> Message:
        return loads_message(self.blob)


# ---------------------------------------------------------------------------
# Message <-> blob
# ---------------------------------------------------------------------------


def _addr(a: Address) -> list[str]:
    return [a.name, a.addr]


def dumps_message(msg: Message) -> bytes:
    """Everything the renderer will need, minus the attachment payloads.

    The payloads are already on disk by the time this is called; carrying them
    through the spill would double the largest cost in the build for no gain.
    """
    data = {
        "uid": msg.uid,
        "folder": msg.folder,
        "message_id": msg.message_id,
        "subject": msg.subject,
        "sender": _addr(msg.sender),
        "reply_to": _addr(msg.reply_to) if msg.reply_to else None,
        "to": [_addr(a) for a in msg.to],
        "cc": [_addr(a) for a in msg.cc],
        "date": msg.date.isoformat() if msg.date else "",
        "date_raw": msg.date_raw,
        "organization": msg.organization,
        "newsgroups": msg.newsgroups,
        "in_reply_to": msg.in_reply_to,
        "references": msg.references,
        "priority": msg.priority,
        "unread": msg.unread,
        "flagged": msg.flagged,
        "has_attachments": msg.has_attachments,
        "body_text": msg.body_text,
        "body_html": msg.body_html,
        "attachments": [
            [a.filename, a.content_type, a.size, a.href, a.content_id]
            for a in msg.attachments
        ],
        "headers": [[k, v] for k, v in msg.headers],
        "size": msg.size,
        "source": msg.source,
    }
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8", "replace")
    return zlib.compress(raw, _ZLIB_LEVEL)


def loads_message(blob: bytes) -> Message:
    data = json.loads(zlib.decompress(blob).decode("utf-8", "replace"))
    reply_to = data.get("reply_to")
    return Message(
        uid=data["uid"],
        folder=data["folder"],
        message_id=data["message_id"],
        subject=data["subject"],
        sender=Address(*data["sender"]),
        reply_to=Address(*reply_to) if reply_to else None,
        to=[Address(*a) for a in data["to"]],
        cc=[Address(*a) for a in data["cc"]],
        date=datetime.fromisoformat(data["date"]) if data["date"] else None,
        date_raw=data["date_raw"],
        organization=data["organization"],
        newsgroups=data["newsgroups"],
        in_reply_to=data["in_reply_to"],
        references=list(data["references"]),
        priority=data["priority"],
        unread=data["unread"],
        flagged=data["flagged"],
        has_attachments=data["has_attachments"],
        body_text=data["body_text"],
        body_html=data["body_html"],
        attachments=[
            Attachment(filename=a[0], content_type=a[1], size=a[2],
                       href=a[3], content_id=a[4])
            for a in data["attachments"]
        ],
        headers=[(k, v) for k, v in data["headers"]],
        size=data["size"],
        source=data["source"],
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class MessageStore:
    """Every message in the build, on disk, queryable, in insert order.

    A message's ``gid`` is its position in insert order, which is also the
    document id the search index uses. Publication order -- what the folder
    listings show -- is a separate thing, recorded per message as a slug and a
    row index once the listings are written.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        self.db = sqlite3.connect(path, isolation_level=None)
        # This file is rebuilt from the sources if anything goes wrong, so
        # durability buys nothing and costs a great deal.
        self.db.execute("PRAGMA journal_mode = OFF")
        self.db.execute("PRAGMA synchronous = OFF")
        self.db.execute("PRAGMA temp_store = MEMORY")
        self.db.execute("PRAGMA cache_size = -32000")  # 32 MiB
        self.db.executescript(_SCHEMA)
        self._next_id = 1
        self._pending = 0
        self.db.execute("BEGIN")

    # ------------------------------------------------------------------ life
    def close(self) -> None:
        if self.db is not None:
            try:
                self.db.execute("COMMIT")
            except sqlite3.OperationalError:
                pass
            self.db.close()
            self.db = None

    def discard(self) -> None:
        """Close and delete the spill file."""
        self.close()
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "MessageStore":
        return self

    def __exit__(self, *exc) -> None:
        self.discard()

    def _tick(self) -> None:
        self._pending += 1
        if self._pending >= _BATCH:
            self.db.execute("COMMIT")
            self.db.execute("BEGIN")
            self._pending = 0

    # --------------------------------------------------------------- ingest
    def existing_body_len(self, uid: str) -> int | None:
        """Length of the body already stored for ``uid``, or None if unseen.

        Checked before the attachments are extracted so a duplicate costs one
        indexed lookup rather than a directory of blob writes.
        """
        cur = self.db.execute("SELECT body_len FROM msgs WHERE uid = ?", (uid,))
        found = cur.fetchone()
        return found[0] if found else None

    def add(self, msg: Message, *, row: list, atts: list[str],
            year: str, acct: str) -> None:
        """Store ``msg``, replacing any copy already held under the same uid."""
        blob = dumps_message(msg)
        body_len = len(msg.body_text) + len(msg.body_html)
        ts = int(msg.date.timestamp()) if msg.date else 0
        fields = (
            msg.folder, ts, int(msg.unread), body_len,
            json.dumps(row, separators=(",", ":"), ensure_ascii=False),
            json.dumps(atts, separators=(",", ":")), year, acct,
        )

        cur = self.db.execute("SELECT id FROM msgs WHERE uid = ?", (msg.uid,))
        found = cur.fetchone()
        if found:
            mid = found[0]
            self.db.execute(
                "UPDATE msgs SET raw_folder=?, date_ts=?, unread=?, body_len=?, "
                "row=?, atts=?, year=?, acct=? WHERE id=?",
                fields + (mid,),
            )
            self.db.execute("UPDATE bodies SET blob=? WHERE id=?", (blob, mid))
        else:
            mid = self._next_id
            self._next_id += 1
            self.db.execute(
                "INSERT INTO msgs (id, uid, raw_folder, date_ts, unread, body_len, "
                "row, atts, year, acct) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mid, msg.uid) + fields,
            )
            self.db.execute("INSERT INTO bodies (id, blob) VALUES (?, ?)", (mid, blob))
        self._tick()

    def flush(self) -> None:
        self.db.execute("COMMIT")
        self.db.execute("BEGIN")
        self._pending = 0

    @property
    def total(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]

    def report(self) -> None:
        """Say how much scratch space the read cost, for a build worth watching."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        log.info("spill file: %.1f GiB at %s", size / 1073741824, self.path)

    # ------------------------------------------------------------- accounts
    def account_counts(self) -> dict[str, int]:
        """How many messages name each candidate address, "" included.

        Grouped by SQLite rather than by a :class:`~collections.Counter` over
        every message, so the only thing that reaches memory is one entry per
        distinct address.
        """
        return {
            addr: n
            for addr, n in self.db.execute(
                "SELECT acct, COUNT(*) FROM msgs GROUP BY acct"
            )
        }

    def place(self, resolve, folder_prefix: str, year_folders: bool) -> None:
        """Write each message's final folder path.

        ``resolve`` maps a stored account candidate onto the mailbox the
        message belongs to, or to "" when the tree is not split by account.
        """
        prefix = folder_prefix.strip("/")
        seen = 0
        while True:
            # Read a chunk, close the cursor, then write. Updating a table
            # while a scan of it is still open is undefined behaviour in
            # SQLite, and a chunk at a time costs nothing to avoid it.
            chunk = self.db.execute(
                "SELECT id, acct, year, raw_folder FROM msgs WHERE id > ? "
                "ORDER BY id LIMIT 50000", (seen,),
            ).fetchall()
            if not chunk:
                break
            seen = chunk[-1][0]
            updates = []
            for mid, acct, year, raw_folder in chunk:
                parts: list[str] = []
                if prefix:
                    parts.append(prefix)
                account = resolve(acct)
                if account:
                    parts.append(account.replace("/", "_"))
                if year_folders:
                    parts.append(year)
                parts.append(raw_folder)
                updates.append(("/".join(parts), mid))
            self.db.executemany("UPDATE msgs SET folder = ? WHERE id = ?", updates)
        self.flush()
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS msgs_folder ON msgs(folder, date_ts, uid)"
        )

    # ------------------------------------------------------------- listings
    def folders(self) -> list[tuple[str, int, int]]:
        """(path, message count, unread count) for every folder holding mail."""
        return [
            (path, n, unread)
            for path, n, unread in self.db.execute(
                "SELECT folder, COUNT(*), SUM(unread) FROM msgs GROUP BY folder"
            )
        ]

    def folder_rows(self, folder: str) -> list[tuple[str, list]]:
        """(uid, listing row) for one folder, newest first.

        Matches ``Message.sort_key()`` reversed, which is what fixes both the
        display order and the prev/next order on the message pages.

        Read out in full rather than streamed, so the caller can write back to
        the same connection while it walks the result. One folder is a bounded
        cost -- rows carry no body -- where the whole archive was not.
        """
        cur = self.db.execute(
            "SELECT uid, row FROM msgs WHERE folder = ? ORDER BY date_ts DESC, uid DESC",
            (folder,),
        )
        return [(uid, json.loads(row)) for uid, row in cur.fetchall()]

    def set_publication(self, rows: Iterator[tuple[str, int, str, str, str]]) -> None:
        """Record (slug, row_idx, prev_uid, next_uid) against each uid."""
        self.db.executemany(
            "UPDATE msgs SET slug=?, row_idx=?, prev_uid=?, next_uid=? WHERE uid=?",
            rows,
        )
        self.flush()

    # --------------------------------------------------------------- render
    def published(self) -> Iterator[Published]:
        """Every message with its body, in gid order.

        Two tables read in step, so this is a sequential pass over both files
        rather than 300,000 random seeks into a five-gigabyte one.
        """
        cur = self.db.execute(
            "SELECT m.id, m.uid, m.folder, m.atts, m.prev_uid, m.next_uid, "
            "m.slug, m.row_idx, b.blob "
            "FROM msgs m JOIN bodies b ON b.id = m.id ORDER BY m.id"
        )
        for mid, uid, folder, atts, prev_uid, next_uid, slug, row_idx, blob in cur:
            yield Published(mid - 1, uid, folder, json.loads(atts),
                            prev_uid, next_uid, slug, row_idx, blob)
