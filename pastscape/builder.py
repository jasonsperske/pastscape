"""Orchestrates a build: read sources, reconcile against the manifest, render."""

from __future__ import annotations

import logging
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .model import Message, compute_uid, ensure_tz, normalize_folder
from .render import SiteBuilder
from .search import build_index
from .sources import read_source, source_fingerprint
from .state import Manifest, MessageRecord, content_hash

log = logging.getLogger("pastscape.build")


@dataclass
class BuildStats:
    total: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    pruned: int = 0
    folders: int = 0
    attachments: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [
            f"{self.total} messages in {self.folders} folders",
            f"{self.added} new",
            f"{self.updated} updated",
            f"{self.unchanged} unchanged",
        ]
        if self.duplicates:
            bits.append(f"{self.duplicates} duplicates skipped")
        if self.pruned:
            bits.append(f"{self.pruned} pruned")
        return ", ".join(bits)


def collect(sources: list[Path], kinds: dict[Path, str] | None = None,
            limit: int = 0, folder_prefix: str = "") -> tuple[list[Message], BuildStats]:
    """Read every source, assign uids, drop cross-source duplicates."""
    stats = BuildStats()
    seen: dict[str, Message] = {}
    kinds = kinds or {}

    for src in sources:
        log.info("reading %s", src)
        count = 0
        try:
            for msg in read_source(src, kinds.get(src)):
                msg.folder = normalize_folder(
                    f"{folder_prefix}/{msg.folder}" if folder_prefix else msg.folder
                )
                msg.date = ensure_tz(msg.date)
                msg.uid = compute_uid(msg)
                if msg.uid in seen:
                    stats.duplicates += 1
                    # Prefer the copy that carries attachments/body over a stub.
                    old = seen[msg.uid]
                    if len(msg.body_text) + len(msg.body_html) > len(old.body_text) + len(old.body_html):
                        seen[msg.uid] = msg
                    continue
                seen[msg.uid] = msg
                count += 1
                if limit and len(seen) >= limit:
                    break
        except Exception as exc:
            log.error("failed reading %s: %s", src, exc)
            stats.errors.append(f"{src}: {exc}")
            continue
        log.info("  %d messages from %s", count, src)
        if limit and len(seen) >= limit:
            log.info("stopping at --limit %d", limit)
            break

    messages = list(seen.values())
    stats.total = len(messages)
    return messages, stats


def build_site(sources: list[Path], site_dir: Path, *, title: str = "Local Mail",
               kinds: dict[Path, str] | None = None, limit: int = 0,
               folder_prefix: str = "", block_remote: bool = True,
               news_host: str = "", prune: bool = False,
               force: bool = False) -> BuildStats:
    site_dir.mkdir(parents=True, exist_ok=True)
    # --force rewrites every page but still reads the manifest: first_seen is
    # history we should not throw away just because the renderer changed.
    manifest = Manifest.load(site_dir)
    known = manifest.messages

    messages, stats = collect(sources, kinds=kinds, limit=limit, folder_prefix=folder_prefix)

    if not messages:
        if known and not prune:
            # A source that suddenly reads as empty is far more likely to be a
            # corrupt file or a wrong path than a mailbox that lost every
            # message. Rewriting the listings now would blank a working archive
            # while leaving its message pages orphaned, so refuse instead.
            detail = "; ".join(stats.errors) if stats.errors else "the sources contained no messages"
            raise RuntimeError(
                f"refusing to rewrite {site_dir}: it has {len(known)} published messages "
                f"but this run read none ({detail}). "
                "Pass --prune to empty the archive on purpose."
            )
        log.warning("no messages found in %s", ", ".join(str(s) for s in sources))

    builder = SiteBuilder(site_dir, title=title, block_remote=block_remote, news_host=news_host)
    builder.write_assets()

    # Group into folders and publish the per-folder listings first: that fixes
    # the display order, which is also the prev/next order on message pages.
    folders: dict[str, list[Message]] = defaultdict(list)
    for msg in messages:
        folders[msg.folder].append(msg)
    stats.folders = len(folders)

    folder_meta, doc_locations = builder.write_folder_data(folders)
    ordered: list[Message] = builder._ordered_messages

    # Neighbour map, per folder, following the published order.
    neighbours: dict[str, tuple[str, str]] = {}
    by_folder: dict[str, list[Message]] = defaultdict(list)
    for msg in ordered:
        by_folder[msg.folder].append(msg)
    for path, msgs in by_folder.items():
        for i, msg in enumerate(msgs):
            prev_uid = msgs[i - 1].uid if i > 0 else ""
            next_uid = msgs[i + 1].uid if i + 1 < len(msgs) else ""
            neighbours[msg.uid] = (prev_uid, next_uid)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_records: dict[str, MessageRecord] = {}

    for msg in ordered:
        hash_ = content_hash(msg)
        prev_uid, next_uid = neighbours.get(msg.uid, ("", ""))
        nav = f"{prev_uid}|{next_uid}"
        record = known.get(msg.uid)
        page_rel = f"msg/{msg.uid[:2]}/{msg.uid}.html"
        page_exists = (site_dir / page_rel).is_file()

        needs_write = (
            force
            or record is None
            or record.hash != hash_
            or record.nav != nav
            or not page_exists
        )

        first_seen = record.first_seen if record and record.first_seen else now

        if needs_write:
            att_rels = builder.write_attachments(msg)
            stats.attachments += len(att_rels)
            meta = builder.message_meta(msg, hash_, first_seen)
            meta["nav"] = nav
            builder.write_message(msg, meta, prev_uid=prev_uid, next_uid=next_uid)
            if record is None:
                stats.added += 1
            else:
                stats.updated += 1
        else:
            att_rels = record.attachments
            stats.unchanged += 1

        rec = MessageRecord(
            uid=msg.uid,
            folder=msg.folder,
            hash=hash_,
            date=msg.date.isoformat() if msg.date else "",
            subject=msg.subject[:200],
            page=page_rel,
            attachments=att_rels,
            first_seen=first_seen,
            last_built=now,
            nav=nav,
        )
        new_records[msg.uid] = rec

    # Messages that vanished from the source. An archive is normally additive,
    # so keep their pages unless the caller explicitly asks to prune.
    gone = [uid for uid in known if uid not in new_records]
    if gone:
        if prune:
            for uid in gone:
                _remove_message_files(site_dir, known[uid])
                stats.pruned += 1
            log.info("pruned %d messages no longer present in the sources", len(gone))
        else:
            log.info("%d messages in the manifest are no longer in the sources "
                     "(kept; pass --prune to remove)", len(gone))
            for uid in gone:
                new_records[uid] = known[uid]

    built = now
    builder.write_folders_json(folder_meta, total=len(ordered), built=built)
    builder.write_index()
    build_index(ordered, site_dir / "data" / "search", doc_locations)

    manifest.messages = new_records
    manifest.title = title
    manifest.built = built
    for src in sources:
        manifest.sources[str(src)] = {
            "fingerprint": source_fingerprint(src),
            "read": built,
        }
    manifest.save(site_dir)
    return stats


def _remove_message_files(site_dir: Path, rec: MessageRecord) -> None:
    if rec.page:
        page = site_dir / rec.page
        if page.is_file():
            page.unlink()
    att_dir = site_dir / "attachments" / rec.uid[:2] / rec.uid
    if att_dir.is_dir():
        shutil.rmtree(att_dir, ignore_errors=True)
