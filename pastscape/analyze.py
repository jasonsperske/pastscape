"""Report which addresses an archive's mail was sent to, without building it.

Answering "which of my addresses actually receive mail, and how much" needs
nothing but the headers, so this reads them and stops: no MIME walk, no
attachment decoding, no pages. On a twenty-gigabyte mbox that is minutes rather
than the best part of an hour.

The report separates two counts that are easy to conflate and that answer
different questions:

``delivered``
    The address appears in a header the receiving server wrote --
    ``Delivered-To`` and friends. This is what mailbox *inference* keys on, so
    an address that is never a delivery address can never earn a root on its
    own however much mail names it.

``to/cc``
    The address appears in ``To`` or ``Cc``. Alias mail often looks like this
    and nothing else: sent to ``you+shopping@example.com``, delivered to
    ``you@example.com``.

An address can be enormous in one column and negligible in the other, which is
exactly the case ``--account`` exists to handle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Iterable, Iterator

from .model import DELIVERY_HEADERS, first_address
from .sources import detect_source, read_source
from .sources.mbox import iter_mbox_header_blocks, open_mailbox
from .sources.mime import parse_addresses

log = logging.getLogger("pastscape.analyze")

_DELIVERY = {name.lower() for name in DELIVERY_HEADERS}
_RECIPIENT = {"to", "cc"}
_SENDER = {"from", "reply-to"}

# Sources whose headers can be read without parsing the whole message.
_FAST_KINDS = {"mbox", "mbox-compressed"}


@dataclass
class AddressCount:
    address: str
    messages: int = 0     # named anywhere -- what --account matches on
    delivered: int = 0    # named in a delivery header -- what inference uses
    to_cc: int = 0
    sender: int = 0

    @property
    def is_recipient(self) -> bool:
        return bool(self.delivered or self.to_cc)


@dataclass
class Report:
    messages: int = 0
    anonymous: int = 0    # messages carrying no address at all
    counts: dict[str, AddressCount] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def recipients(self, min_messages: int = 1) -> list[AddressCount]:
        """Addresses that received mail, most first."""
        rows = [c for c in self.counts.values()
                if c.is_recipient and c.messages >= min_messages]
        return sorted(rows, key=lambda c: (-c.messages, c.address))


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


def parse_aliases(specs: Iterable[str]) -> dict[str, str]:
    """``["you+work@x.com=you@x.com"]`` -> ``{"you+work@x.com": "you@x.com"}``.

    Chains are followed, so ``a=b`` together with ``b=c`` files ``a`` under
    ``c``. A cycle stops rather than hangs; the address is left as it is.
    """
    raw: dict[str, str] = {}
    for spec in specs:
        alias, sep, target = spec.partition("=")
        alias, target = alias.strip().lower(), target.strip().lower()
        if not sep or not alias or not target:
            raise ValueError(
                f"--account-alias needs ALIAS=ADDRESS, got {spec!r}"
            )
        raw[alias] = target

    resolved: dict[str, str] = {}
    for alias in raw:
        seen = {alias}
        target = raw[alias]
        while target in raw and target not in seen:
            seen.add(target)
            target = raw[target]
        resolved[alias] = target
    return resolved


def canonical(address: str, aliases: dict[str, str]) -> str:
    return aliases.get(address, address) if aliases else address


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def address_of(raw: str) -> str:
    """The bare address in a parsed address slot, or "" if there is none.

    Needed because a header without angle brackets gives up its whole value as
    the address: Yahoo Groups writes ``Delivered-To: mailing list
    list@yahoogroups.com``, which otherwise reports as an address of its own
    sitting alongside the real one with identical counts.
    """
    addr = (raw or "").strip().strip("<>").lower()
    if addr and " " not in addr and "@" in addr:
        return addr
    return first_address(addr)


def _tally(report: Report, pairs: Iterable[tuple[str, str]],
           aliases: dict[str, str]) -> None:
    """Fold one message's headers into the report.

    Counted per message, not per occurrence: an address named in both
    ``Delivered-To`` and ``X-Apparently-To`` is one message, not two. Counting
    header lines instead is how a mailing list ends up looking twice its real
    size.
    """
    delivered: set[str] = set()
    to_cc: set[str] = set()
    sender: set[str] = set()

    for name, value in pairs:
        low = name.lower()
        if low in _DELIVERY:
            bucket = delivered
        elif low in _RECIPIENT:
            bucket = to_cc
        elif low in _SENDER:
            bucket = sender
        else:
            continue
        for parsed in parse_addresses(value):
            addr = address_of(parsed.addr)
            if addr:
                bucket.add(canonical(addr, aliases))

    report.messages += 1
    everything = delivered | to_cc | sender
    if not everything:
        report.anonymous += 1
        return

    for group, attr in ((everything, "messages"), (delivered, "delivered"),
                        (to_cc, "to_cc"), (sender, "sender")):
        for address in group:
            row = report.counts.get(address)
            if row is None:
                row = report.counts[address] = AddressCount(address)
            setattr(row, attr, getattr(row, attr) + 1)


def _mbox_header_pairs(path: Path) -> Iterator[list[tuple[str, str]]]:
    parser = BytesHeaderParser()
    with open_mailbox(path) as stream:
        for block in iter_mbox_header_blocks(stream):
            if not block.strip():
                continue
            yield parser.parsebytes(block).items()


def analyze(sources: list[Path], kinds: dict[Path, str] | None = None,
            aliases: dict[str, str] | None = None, limit: int = 0) -> Report:
    """Count the addresses named across every source."""
    report = Report()
    kinds = kinds or {}
    aliases = aliases or {}

    for src in sources:
        kind = kinds.get(src) or detect_source(src)
        report.sources.append(f"{src} ({kind})")
        before = report.messages
        try:
            if kind in _FAST_KINDS:
                stream = _mbox_header_pairs(src)
            else:
                # Containers, PST and .eml directories have no header-only
                # path, so they are read the ordinary way. Correct, just not
                # as quick -- and they are rarely the large source.
                stream = (msg.headers for msg in read_source(src, kind))
            for pairs in stream:
                _tally(report, pairs, aliases)
                if limit and report.messages >= limit:
                    break
        except Exception as exc:
            log.error("failed reading %s: %s", src, exc)
            report.errors.append(f"{src}: {exc}")
            continue
        log.info("  %d messages from %s", report.messages - before, src)
        if limit and report.messages >= limit:
            log.info("stopping at --limit %d", limit)
            break

    return report


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def format_report(report: Report, top: int = 40, min_messages: int = 1) -> str:
    rows = report.recipients(min_messages)
    shown = rows[:top] if top else rows

    width = max([len(r.address) for r in shown] + [24])
    out = [
        f"{report.messages:,} messages from " + ", ".join(report.sources),
        "",
        f"{'address':<{width}} {'messages':>10} {'delivered':>10} "
        f"{'to/cc':>10} {'from':>10}",
        "-" * (width + 44),
    ]
    for row in shown:
        out.append(
            f"{row.address:<{width}} {row.messages:>10,} {row.delivered:>10,} "
            f"{row.to_cc:>10,} {row.sender:>10,}"
        )

    hidden = len(rows) - len(shown)
    if hidden:
        out.append(f"... {hidden:,} more addresses (--top 0 shows every one)")
    if report.anonymous:
        out.append(f"{report.anonymous:,} messages carried no address at all")
    out += [
        "",
        "messages   named anywhere on the message -- what --account matches",
        "delivered  named in a delivery header -- what mailbox inference uses",
        "",
        "An address with a large to/cc and a small delivered is an alias: mail",
        "is addressed to it but delivered elsewhere, so inference will never",
        "give it a root of its own. Name it with --account if you want one.",
    ]
    for err in report.errors:
        out.append(f"! {err}")
    return "\n".join(out)
