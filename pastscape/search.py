"""Client-side search index.

Netscape's search was a server-free "find in messages" dialog; ours has to be
too, because the archive is a pile of files on a static host. So we ship an
inverted index split into shards keyed by token prefix. The browser fetches
only the shards a query actually touches -- typing "andreessen" pulls
``an.json`` and nothing else.

Postings are delta-encoded and carry a field bitmask, which is what lets the
client rank a subject hit above a hit buried in a quoted reply.
"""

from __future__ import annotations

import json
import logging
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


def build_index(messages: list[Message], out_dir: Path, doc_locations: list[list]) -> dict:
    """Write ``out_dir`` (``data/search/``) and return the meta dict.

    ``messages[i]`` is document ``i``; ``doc_locations[i]`` is the compact
    ``[folderSlug, rowIndex]`` pair the client uses to find the row without
    downloading every folder listing.
    """
    postings: dict[str, list[tuple[int, int]]] = {}
    for gid, msg in enumerate(messages):
        for tok, mask in _field_tokens(msg).items():
            postings.setdefault(tok, []).append((gid, mask))

    shard_len = _shard_len(len(postings))
    shards: dict[str, dict[str, list[int]]] = {}
    for token, plist in postings.items():
        flat: list[int] = []
        prev = 0
        for gid, mask in plist:
            flat.append(gid - prev)  # delta-encoded: compresses well, decodes trivially
            flat.append(mask)
            prev = gid
        shards.setdefault(_shard_key(token, shard_len), {})[token] = flat

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("*.json"):
        existing.unlink()

    shard_sizes: dict[str, int] = {}
    for key, table in shards.items():
        path = out_dir / f"{key}.json"
        payload = json.dumps(table, separators=(",", ":"))
        path.write_text(payload, "utf-8")
        shard_sizes[key] = len(payload)

    (out_dir / "docs.json").write_text(
        json.dumps(doc_locations, separators=(",", ":")), "utf-8"
    )

    meta = {
        "shardLen": shard_len,
        "shards": sorted(shards.keys()),
        "docs": len(messages),
        "tokens": len(postings),
        "stopwords": sorted(STOPWORDS),
        "fields": {
            "subject": F_SUBJECT,
            "sender": F_SENDER,
            "recipient": F_RECIPIENT,
            "body": F_BODY,
            "attachment": F_ATTACHMENT,
        },
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, separators=(",", ":")), "utf-8")
    log.info(
        "search index: %d docs, %d tokens, %d shards (prefix %d), %.1f KiB",
        len(messages), len(postings), len(shards), shard_len,
        sum(shard_sizes.values()) / 1024,
    )
    return meta
