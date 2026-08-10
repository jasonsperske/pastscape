"""Client-side search index.

Netscape's search was a server-free "find in messages" dialog; ours has to be
too, because the archive is a pile of files on a static host. So we ship an
inverted index split into shards keyed by token prefix. The browser fetches
only the shards a query actually touches -- typing "andreessen" pulls
``an.json`` and nothing else.

Postings are delta-encoded and carry a field bitmask, which is what lets the
client rank a subject hit above a hit buried in a quoted reply.

Building the index never holds it. A 300,000-message archive has millions of
distinct tokens and a hundred million postings, so instead of one dictionary
in memory the postings are spilled to bucket files keyed by the first two
characters of the token, then folded into shards one bucket at a time. A token
always lands in exactly one bucket, and a bucket is always written in document
order, so the delta encoding falls out of reading a bucket back in order.
"""

from __future__ import annotations

import json
import logging
from array import array
from pathlib import Path

from .model import Message, tokenize
from .sanitize import strip_tags

log = logging.getLogger("pastscape.search")

# Field bits, mirrored in pastscape.js
F_SUBJECT = 1
F_SENDER = 2
F_RECIPIENT = 4
F_BODY = 8
F_ATTACHMENT = 16

# Words that match nearly everything; dropping them keeps shards small. Kept
# deliberately short -- an archive search should still find "the road not taken".
STOPWORDS = {
    "the", "and", "for", "you", "are", "with", "this", "that", "from", "have",
    "was", "not", "but", "all", "can", "will", "has", "our", "your", "they",
}

MAX_BODY_TOKENS = 4000  # per message, keeps pathological messages in check


def _field_tokens(msg: Message) -> dict[str, int]:
    """token -> OR'd field bitmask for one message."""
    fields: dict[str, int] = {}

    def add(text: str, bit: int, limit: int | None = None) -> None:
        for n, tok in enumerate(tokenize(text or "")):
            if limit is not None and n >= limit:
                break
            if tok in STOPWORDS:
                continue
            fields[tok] = fields.get(tok, 0) | bit

    add(msg.subject, F_SUBJECT)
    add(f"{msg.sender.name} {msg.sender.addr} {msg.organization}", F_SENDER)
    add(" ".join(f"{a.name} {a.addr}" for a in msg.to + msg.cc), F_RECIPIENT)
    add(" ".join(a.filename for a in msg.attachments), F_ATTACHMENT)
    # An HTML-only message has no text/plain part; index what it reads as.
    body = msg.body_text or (strip_tags(msg.body_html) if msg.body_html else "")
    add(body, F_BODY, MAX_BODY_TOKENS)
    return fields


def _shard_len(token_count: int) -> int:
    if token_count > 120_000:
        return 3
    if token_count > 12_000:
        return 2
    return 1


def _shard_key(token: str, length: int) -> str:
    key = token[:length]
    # Keep shard names filesystem-safe and case-insensitive-collision-free.
    return "".join(c if c.isalnum() else "_" for c in key) or "_"


# Postings buffered before the bucket files are appended to. Each line is
# short; a million of them is a few tens of megabytes.
_SPILL_LINES = 1_000_000

# Tokens are bucketed on a two-character prefix. That is the finest split that
# still guarantees a whole shard can be built from a single bucket, whichever
# shard length the archive ends up needing.
_BUCKET_LEN = 2


class IndexWriter:
    """Writes ``data/search/`` incrementally, one message at a time.

    Feed it every message in document order and call :meth:`finish`. Peak
    memory is one bucket's worth of postings, not the archive's.
    """

    def __init__(self, out_dir: Path, work_dir: Path):
        self.out_dir = out_dir
        self.work_dir = work_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        for existing in out_dir.glob("*.json"):
            existing.unlink()
        work_dir.mkdir(parents=True, exist_ok=True)
        for existing in work_dir.glob("*.txt"):
            existing.unlink()

        self._buffer: dict[str, list[str]] = {}
        self._buffered = 0
        self._buckets: set[str] = set()
        self.docs = 0
        # docs.json is written as we go: it is one [slug, rowIndex] pair per
        # document in document order, so it never needs to exist as a list.
        self._docs_fh = (out_dir / "docs.json").open("w", encoding="utf-8")
        self._docs_fh.write("[")

    # ----------------------------------------------------------------- feed
    def add(self, msg: Message, slug: str, row_idx: int) -> None:
        gid = self.docs
        self.docs += 1
        self._docs_fh.write(
            ("," if gid else "") + json.dumps([slug, row_idx], separators=(",", ":"))
        )
        for token, mask in _field_tokens(msg).items():
            bucket = _shard_key(token, _BUCKET_LEN)
            self._buffer.setdefault(bucket, []).append(f"{token}\t{gid}\t{mask}\n")
            self._buffered += 1
        if self._buffered >= _SPILL_LINES:
            self._spill()

    def _spill(self) -> None:
        for bucket, lines in self._buffer.items():
            with (self.work_dir / f"{bucket}.txt").open("a", encoding="utf-8") as fh:
                fh.writelines(lines)
            self._buckets.add(bucket)
        self._buffer.clear()
        self._buffered = 0

    # --------------------------------------------------------------- finish
    def finish(self) -> dict:
        self._spill()
        self._docs_fh.write("]")
        self._docs_fh.close()

        # The shard width depends on how many distinct tokens the archive has,
        # so count them before writing anything. A bucket at a time keeps this
        # to one small set in memory rather than one enormous one.
        tokens = sum(self._distinct(bucket) for bucket in sorted(self._buckets))
        shard_len = _shard_len(tokens)

        groups: dict[str, list[str]] = {}
        for bucket in sorted(self._buckets):
            groups.setdefault(bucket[: min(shard_len, _BUCKET_LEN)], []).append(bucket)

        shards: list[str] = []
        written = 0
        for buckets in groups.values():
            for key, size in self._write_group(buckets, shard_len):
                shards.append(key)
                written += size

        meta = {
            "shardLen": shard_len,
            "shards": sorted(shards),
            "docs": self.docs,
            "tokens": tokens,
            "stopwords": sorted(STOPWORDS),
            "fields": {
                "subject": F_SUBJECT,
                "sender": F_SENDER,
                "recipient": F_RECIPIENT,
                "body": F_BODY,
                "attachment": F_ATTACHMENT,
            },
        }
        (self.out_dir / "meta.json").write_text(
            json.dumps(meta, separators=(",", ":")), "utf-8"
        )
        log.info(
            "search index: %d docs, %d tokens, %d shards (prefix %d), %.1f KiB",
            self.docs, tokens, len(shards), shard_len, written / 1024,
        )
        self._cleanup()
        return meta

    def _distinct(self, bucket: str) -> int:
        seen: set[str] = set()
        with (self.work_dir / f"{bucket}.txt").open("r", encoding="utf-8") as fh:
            for line in fh:
                seen.add(line[: line.index("\t")])
        return len(seen)

    def _write_group(self, buckets: list[str], shard_len: int):
        """Fold whole bucket files into shard JSON, yielding (key, bytes)."""
        postings: dict[str, array] = {}
        for bucket in buckets:
            with (self.work_dir / f"{bucket}.txt").open("r", encoding="utf-8") as fh:
                for line in fh:
                    token, gid, mask = line.rstrip("\n").split("\t")
                    # array('i') rather than a list of tuples: the same postings
                    # cost about a tenth of the memory, which is what keeps the
                    # biggest bucket from being the new ceiling.
                    postings.setdefault(token, array("i")).extend(
                        (int(gid), int(mask))
                    )

        by_shard: dict[str, list[str]] = {}
        for token in postings:
            by_shard.setdefault(_shard_key(token, shard_len), []).append(token)

        for key, tokens in by_shard.items():
            path = self.out_dir / f"{key}.json"
            size = 0
            with path.open("w", encoding="utf-8") as fh:
                size += fh.write("{")
                for n, token in enumerate(sorted(tokens)):
                    flat: list[int] = []
                    prev = 0
                    plist = postings[token]
                    for i in range(0, len(plist), 2):
                        gid = plist[i]
                        # delta-encoded: compresses well, decodes trivially
                        flat.append(gid - prev)
                        flat.append(plist[i + 1])
                        prev = gid
                    size += fh.write(
                        ("," if n else "")
                        + json.dumps(token)
                        + ":"
                        + json.dumps(flat, separators=(",", ":"))
                    )
                size += fh.write("}")
            yield key, size

    def _cleanup(self) -> None:
        for spilled in self.work_dir.glob("*.txt"):
            try:
                spilled.unlink()
            except OSError:
                pass
        try:
            self.work_dir.rmdir()
        except OSError:
            pass

    def close(self) -> None:
        if not self._docs_fh.closed:
            self._docs_fh.close()
        self._cleanup()


def build_index(messages: list[Message], out_dir: Path,
                doc_locations: list[list]) -> dict:
    """Whole-list convenience wrapper around :class:`IndexWriter`."""
    writer = IndexWriter(out_dir, out_dir.parent / ".search-work")
    for msg, (slug, row_idx) in zip(messages, doc_locations):
        writer.add(msg, slug, row_idx)
    return writer.finish()
