"""Orchestrates a build: read sources, reconcile against the manifest, render.

Nothing here keeps the archive in memory. The sources are streamed into a
:class:`~pastscape.spill.MessageStore` as they are read, and each later stage
queries that file: which mailboxes exist, what each folder lists, what to
render. A twenty-gigabyte mbox and a twenty-message maildir cost the same
resident memory, which is the only way the first of those finishes at all.

The passes, in order:

1. **ingest** -- read every source, extract attachments, spill each message
2. **place** -- infer the mailboxes and write each message's folder path
3. **listings** -- write ``data/msgs/*.json`` and record the published order
4. **render** -- write the message pages and feed the search index
5. **shard** -- fold the spilled postings into ``data/search/*.json``
"""

from __future__ import annotations

import gc
import logging
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .model import (
    OTHER_ACCOUNT,
    UNKNOWN_ACCOUNT,
    Message,
    account_addresses,
    account_candidate,
    compute_uid,
    ensure_tz,
    normalize_folder,
    year_folder,
)
from .analyze import canonical, parse_aliases
from .render import SiteBuilder, listing_row
from .search import IndexWriter
from .sources import read_source, source_fingerprint
from .spill import MessageStore
from .state import Manifest, MessageRecord, content_hash

log = logging.getLogger("pastscape.build")

# Publication rows pushed back into the store at a time.
_PUBLISH_BATCH = 20_000

WORK_DIR_NAME = ".pastscape-build"


def _rss(phase: str) -> None:
    """Log memory at a pass boundary. -vv on a long build shows the shape.

    Both numbers matter and they mean different things: the resident figure is
    what the build is actually holding, while the peak is dominated by the
    largest single message that passed through it -- one 60 MB attachment costs
    more, briefly, than a hundred thousand ordinary messages do at rest.
    """
    if not log.isEnabledFor(logging.DEBUG):
        return
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        resident = pages * 4096 / 1048576
    except (ImportError, OSError, IndexError, ValueError):
        return
    log.debug("after %s: rss %.0f MiB (peak %.0f MiB)", phase, resident, peak)


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


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def folder_path(raw_folder: str, account: str = "", year: str = "",
                folder_prefix: str = "") -> str:
    """Assemble a folder path: prefix / account / year / folder."""
    parts: list[str] = []
    if folder_prefix:
        parts.append(folder_prefix.strip("/"))
    if account:
        parts.append(account.replace("/", "_"))
    if year:
        parts.append(year)
    parts.append(raw_folder)
    return "/".join(parts)


def place(msg: Message, account: str = "", folder_prefix: str = "",
          year_folders: bool = True) -> str:
    """Where one message belongs in the tree.

    Normalisation has to happen before anything is prepended: it only maps the
    leading segment, so "Deleted Items" becomes "Trash" while "2008/Deleted
    Items" would not.
    """
    return folder_path(
        normalize_folder(msg.folder), account,
        year_folder(msg) if year_folders else "", folder_prefix,
    )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def account_resolver(counts: dict[str, int], total: int) -> Callable[[str], str]:
    """Map a message's candidate address onto the mailbox it belongs to.

    The inference is deliberately conservative -- a mailing list you were on
    for a decade can easily out-number a mailbox, so a candidate has to clear a
    share of the archive before it earns a root of its own, and stragglers are
    gathered under one heading rather than scattering the tree.

    Taking counts rather than messages is what lets the same rule run over an
    archive too large to hold: SQLite does the grouping and only one entry per
    distinct address reaches memory.
    """
    if not counts:
        return lambda candidate: UNKNOWN_ACCOUNT

    floor = max(3, total // 50)  # 2% of the archive
    major = {addr for addr, n in counts.items() if n >= floor}

    if len(major) <= 1:
        # One mailbox (or one that dominates): everything belongs to it, and
        # splitting off a handful of oddities would only add noise.
        only = next(iter(major), min(counts, key=lambda a: (-counts[a], a)))
        return lambda candidate: only

    log.info("detected %d mailboxes: %s", len(major), ", ".join(sorted(major)))
    return lambda candidate: candidate if candidate in major else OTHER_ACCOUNT


def explicit_account(msg: Message, wanted: dict[str, str],
                     aliases: dict[str, str] | None = None) -> str:
    """With --account the answer is exact: match any of the message's addresses.

    A message routinely matches more than one named mailbox -- plus-addressed
    mail to ``jason+imdb@example.com`` is also delivered to
    ``jason@example.com`` -- so the tie has to be broken by something the
    caller controls. It is the order the addresses were given in: the first
    ``--account`` to match wins, which is what lets a specific alias be listed
    ahead of the bare address it forwards to.

    The order matters for a second reason. ``account_addresses`` returns a set,
    and set iteration order for strings is not stable between runs, so picking
    whichever match came out first would move mail between mailboxes on every
    rebuild.
    """
    found = account_addresses(msg)
    if aliases:
        found = {canonical(a, aliases) for a in found}
    for addr, name in wanted.items():  # insertion-ordered: the --account order
        if addr in found:
            return name
    return OTHER_ACCOUNT


def assign_accounts(messages: list[Message], explicit: list[str] | None = None,
                    enabled: bool = True,
                    aliases: dict[str, str] | None = None) -> dict[str, str]:
    """Map each message uid to its mailbox. Whole-list form, for small inputs."""
    if not enabled or not messages:
        return {}

    if explicit:
        wanted = _wanted(explicit)
        return {msg.uid: explicit_account(msg, wanted, aliases) for msg in messages}

    candidates = {msg.uid: canonical(account_candidate(msg), aliases or {})
                  for msg in messages}
    counts = Counter(addr for addr in candidates.values() if addr)
    resolve = account_resolver(counts, len(messages))
    return {uid: resolve(addr) for uid, addr in candidates.items()}


def _wanted(explicit: list[str]) -> dict[str, str]:
    return {addr.lower().strip(): addr.strip() for addr in explicit if addr.strip()}


# ---------------------------------------------------------------------------
# Pass 1: ingest
# ---------------------------------------------------------------------------


def ingest(sources: list[Path], store: MessageStore, builder: SiteBuilder,
           kinds: dict[Path, str] | None = None, limit: int = 0,
           accounts: list[str] | None = None,
           account_folders: bool = True,
           aliases: dict[str, str] | None = None) -> BuildStats:
    """Read every source into ``store``, assigning uids and dropping duplicates.

    Attachments are extracted here rather than at render time. They are the
    largest thing a message carries, and writing them straight to disk is what
    lets the payload be dropped the moment the message has been spilled.

    Folders are only normalised at this point. Final placement happens once
    everything has been read, because which mailboxes exist is a property of
    the whole archive rather than of any one message.
    """
    stats = BuildStats()
    kinds = kinds or {}
    wanted = _wanted(accounts or []) if account_folders else {}
    aliases = aliases or {}
    stored = 0

    for src in sources:
        log.info("reading %s", src)
        count = 0
        try:
            for msg in read_source(src, kinds.get(src)):
                msg.date = ensure_tz(msg.date)
                msg.folder = normalize_folder(msg.folder)
                msg.uid = compute_uid(msg)

                prior = store.existing_body_len(msg.uid)
                if prior is not None:
                    stats.duplicates += 1
                    # Prefer the copy that carries attachments/body over a stub.
                    if len(msg.body_text) + len(msg.body_html) <= prior:
                        continue
                else:
                    stored += 1
                    count += 1

                atts, wrote = builder.write_attachments(msg)
                stats.attachments += wrote

                if not account_folders:
                    acct = ""
                elif wanted:
                    acct = explicit_account(msg, wanted, aliases)
                else:
                    acct = canonical(account_candidate(msg), aliases)

                store.add(msg, row=listing_row(msg), atts=atts,
                          year=year_folder(msg), acct=acct)

                if limit and stored >= limit:
                    break
        except Exception as exc:
            log.error("failed reading %s: %s", src, exc)
            stats.errors.append(f"{src}: {exc}")
            continue
        log.info("  %d messages from %s", count, src)
        if limit and stored >= limit:
            log.info("stopping at --limit %d", limit)
            break

    store.flush()
    stats.total = stored
    return stats


# ---------------------------------------------------------------------------
# Pass 3: listings
# ---------------------------------------------------------------------------


def _publication_rows(stream: Iterable[tuple[str, str, int, str]]) -> Iterator[tuple]:
    """Turn the publication stream into (slug, row_idx, prev, next, uid) rows.

    A message's neighbours are the rows either side of it *within its folder*,
    and the stream visits folders one at a time, so one message of lookahead is
    all this needs.
    """
    held: tuple | None = None
    for path, slug, row_idx, uid in stream:
        if held is not None:
            h_path, h_slug, h_idx, h_uid, h_prev = held
            same = path == h_path
            yield (h_slug, h_idx, h_prev, uid if same else "", h_uid)
            prev = h_uid if same else ""
        else:
            prev = ""
        held = (path, slug, row_idx, uid, prev)
    if held is not None:
        h_path, h_slug, h_idx, h_uid, h_prev = held
        yield (h_slug, h_idx, h_prev, "", h_uid)


def write_listings(store: MessageStore, builder: SiteBuilder,
                   folders: list[tuple[str, int, int]]) -> list[dict]:
    """Write every folder listing and record the published order."""
    batch: list[tuple] = []
    stream = builder.publish_folders(folders, store.folder_rows)
    for row in _publication_rows(stream):
        batch.append(row)
        if len(batch) >= _PUBLISH_BATCH:
            store.set_publication(batch)
            batch = []
    if batch:
        store.set_publication(batch)
    return builder._folder_meta


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def build_site(sources: list[Path], site_dir: Path, *, title: str = "Local Mail",
               kinds: dict[Path, str] | None = None, limit: int = 0,
               folder_prefix: str = "", block_remote: bool = True,
               news_host: str = "", prune: bool = False,
               force: bool = False, year_folders: bool = True,
               accounts: list[str] | None = None,
               account_folders: bool = True,
               account_aliases: list[str] | None = None) -> BuildStats:
    site_dir.mkdir(parents=True, exist_ok=True)
    aliases = parse_aliases(account_aliases or [])
    # --force rewrites every page but still reads the manifest: first_seen is
    # history we should not throw away just because the renderer changed.
    manifest = Manifest.load(site_dir)
    known = manifest.messages
    _rss("manifest load")

    # The readers hand back cyclic structures -- `email`'s parse trees point
    # back at their parents -- so the bodies they carry are freed by the cycle
    # collector rather than by refcount. How often that runs scales with how
    # much *surviving* memory there is, and the manifest of an already-built
    # archive is a great deal of surviving memory: a rebuild of 80,000
    # messages peaked at 2.4 GiB against 0.7 GiB for the first build, purely
    # because the collector had been pushed into running less often. Freezing
    # takes the manifest out of that calculation; it is long-lived by
    # construction and has nothing to collect.
    gc.freeze()

    builder = SiteBuilder(site_dir, title=title, block_remote=block_remote,
                          news_host=news_host)
    work_dir = site_dir / WORK_DIR_NAME

    try:
        stats, folder_meta, new_records, account_roots, now = _run_passes(
            sources, site_dir, builder, work_dir, known,
            kinds=kinds, limit=limit, folder_prefix=folder_prefix,
            force=force, year_folders=year_folders, accounts=accounts,
            account_folders=account_folders, prune=prune, aliases=aliases,
        )
    finally:
        # Scoped to the build: a caller that builds several sites in one
        # process should not accumulate a permanent generation per site.
        gc.unfreeze()
        _cleanup_work_dir(work_dir)

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
    builder.write_folders_json(folder_meta, total=stats.total, built=built,
                               accounts=account_roots)
    builder.write_index()

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


def _run_passes(sources: list[Path], site_dir: Path, builder: SiteBuilder,
                work_dir: Path, known: dict[str, MessageRecord], *,
                kinds, limit, folder_prefix, force, year_folders, accounts,
                account_folders, prune, aliases):
    """Everything that needs the spill file, from reading to the last shard."""
    with MessageStore(work_dir / "spill.sqlite3") as store:
        stats = ingest(sources, store, builder, kinds=kinds, limit=limit,
                       accounts=accounts, account_folders=account_folders,
                       aliases=aliases)
        store.report()
        _rss("ingest")

        if not stats.total:
            if known and not prune:
                # A source that suddenly reads as empty is far more likely to be
                # a corrupt file or a wrong path than a mailbox that lost every
                # message. Rewriting the listings now would blank a working
                # archive while leaving its message pages orphaned, so refuse.
                detail = "; ".join(stats.errors) if stats.errors else \
                    "the sources contained no messages"
                raise RuntimeError(
                    f"refusing to rewrite {site_dir}: it has {len(known)} published "
                    f"messages but this run read none ({detail}). "
                    "Pass --prune to empty the archive on purpose."
                )
            log.warning("no messages found in %s",
                        ", ".join(str(s) for s in sources))

        # -- pass 2: which mailboxes exist, and where everything lands --------
        counts = store.account_counts()
        if not account_folders:
            resolve: Callable[[str], str] = lambda candidate: ""
        elif accounts:
            # Ingest already resolved these exactly against --account.
            resolve = lambda candidate: candidate
        else:
            named = {addr: n for addr, n in counts.items() if addr}
            resolve = account_resolver(named, stats.total)
        store.place(resolve, folder_prefix, year_folders)
        _rss("place")

        # The tree draws these as mailbox roots rather than as ordinary folders.
        prefix = folder_prefix.strip("/")
        account_roots = sorted({
            f"{prefix}/{name}" if prefix else name
            for name in (resolve(a).replace("/", "_") for a in counts)
            if name
        })

        builder.write_assets()

        # -- pass 3: listings, which fix the display and prev/next order ------
        folders = store.folders()
        # Counted before the tree gains its empty parent rows: what a build
        # reports is how many folders hold mail.
        stats.folders = len(folders)
        folder_meta = write_listings(store, builder, folders)
        _rss("listings")

        # -- pass 4: message pages and the search index ----------------------
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new_records: dict[str, MessageRecord] = {}
        index = IndexWriter(site_dir / "data" / "search", work_dir / "search")

        for pub in store.published():
            msg = pub.message()
            msg.folder = pub.folder      # the spill holds the pre-placement one
            hash_ = content_hash(msg)
            nav = f"{pub.prev_uid}|{pub.next_uid}"
            record = known.get(pub.uid)
            page_rel = f"msg/{pub.uid[:2]}/{pub.uid}.html"
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
                meta = builder.message_meta(msg, hash_, first_seen)
                meta["nav"] = nav
                builder.write_message(msg, meta, prev_uid=pub.prev_uid,
                                      next_uid=pub.next_uid)
                if record is None:
                    stats.added += 1
                else:
                    stats.updated += 1
            else:
                stats.unchanged += 1

            index.add(msg, pub.slug, pub.row_idx)

            new_records[pub.uid] = MessageRecord(
                uid=pub.uid,
                folder=pub.folder,
                hash=hash_,
                date=msg.date.isoformat() if msg.date else "",
                subject=msg.subject[:200],
                page=page_rel,
                attachments=pub.atts,
                first_seen=first_seen,
                last_built=now,
                nav=nav,
            )

        _rss("render")

        # -- pass 5: fold the spilled postings into shards --------------------
        index.finish()
        _rss("shards")

    return stats, folder_meta, new_records, account_roots, now


def _cleanup_work_dir(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)


def _remove_message_files(site_dir: Path, rec: MessageRecord) -> None:
    if rec.page:
        page = site_dir / rec.page
        if page.is_file():
            page.unlink()
    att_dir = site_dir / "attachments" / rec.uid[:2] / rec.uid
    if att_dir.is_dir():
        shutil.rmtree(att_dir, ignore_errors=True)
