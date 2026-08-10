"""analyze: who the mail was addressed to, read from headers alone."""

import io
from pathlib import Path

import pytest

from pastscape.analyze import analyze, canonical, parse_aliases
from pastscape.sources import read_source
from pastscape.sources.mbox import iter_mbox_header_blocks, iter_mbox_messages


def msg(to="you@example.com", delivered=None, cc=None, sender="them@example.com",
        subject="Hi", body="Body text.\nFrom the top.\n") -> bytes:
    head = (
        f"From: Sender <{sender}>\r\n"
        f"To: {to}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Tue, 03 Jun 2008 14:00:00 -0800\r\n"
        f"Message-ID: <{subject.replace(' ', '-')}@example.com>\r\n"
    )
    if cc:
        head += f"Cc: {cc}\r\n"
    if delivered:
        head += f"Delivered-To: {delivered}\r\n"
    return (head + "\r\n" + body).encode()


def mbox_bytes(*messages: bytes) -> bytes:
    out = b""
    for i, raw in enumerate(messages):
        out += f"From sender@example.com Tue Jun  3 14:0{i}:00 2008\r\n".encode()
        out += raw + b"\r\n"
    return out


def write_mbox(tmp_path: Path, *messages: bytes) -> Path:
    path = tmp_path / "mail.mbox"
    path.write_bytes(mbox_bytes(*messages))
    return path


# --------------------------------------------------------------------- aliases


def test_alias_spec_is_alias_equals_address():
    assert parse_aliases(["a+work@x.com=a@x.com"]) == {"a+work@x.com": "a@x.com"}


def test_alias_spec_is_case_folded_and_stripped():
    assert parse_aliases([" A+Work@X.com = A@X.com "]) == {"a+work@x.com": "a@x.com"}


def test_alias_chains_are_followed():
    aliases = parse_aliases(["a@x.com=b@x.com", "b@x.com=c@x.com"])
    assert aliases["a@x.com"] == "c@x.com"
    assert aliases["b@x.com"] == "c@x.com"


def test_alias_cycle_terminates():
    aliases = parse_aliases(["a@x.com=b@x.com", "b@x.com=a@x.com"])
    assert set(aliases) == {"a@x.com", "b@x.com"}


@pytest.mark.parametrize("spec", ["no-equals-sign", "=b@x.com", "a@x.com="])
def test_malformed_alias_is_rejected(spec):
    with pytest.raises(ValueError, match="ALIAS=ADDRESS"):
        parse_aliases([spec])


def test_canonical_passes_through_unknown_addresses():
    assert canonical("z@x.com", {"a@x.com": "b@x.com"}) == "z@x.com"


# ---------------------------------------------------------------------- counts


def test_delivered_and_addressed_are_counted_separately(tmp_path):
    """The distinction --account exists for: addressed here, delivered there."""
    path = write_mbox(tmp_path, msg(to="alias@x.com", delivered="me@x.com"))
    report = analyze([path])

    alias = report.counts["alias@x.com"]
    assert (alias.to_cc, alias.delivered) == (1, 0)
    me = report.counts["me@x.com"]
    assert (me.to_cc, me.delivered) == (0, 1)
    # Either way both are addresses --account would match on.
    assert alias.messages == me.messages == 1


def test_an_address_named_twice_counts_as_one_message(tmp_path):
    """Counting header lines is how a list looks twice its real size."""
    raw = (b"From: s@x.com\r\nTo: me@x.com\r\nDelivered-To: me@x.com\r\n"
           b"X-Original-To: me@x.com\r\nSubject: Hi\r\n\r\nBody\r\n")
    report = analyze([write_mbox(tmp_path, raw)])
    assert report.counts["me@x.com"].messages == 1
    assert report.counts["me@x.com"].delivered == 1


def test_aliases_join_their_counts(tmp_path):
    path = write_mbox(
        tmp_path,
        msg(to="me+work@x.com", subject="A"),
        msg(to="me+shop@x.com", subject="B"),
        msg(to="me@x.com", subject="C"),
    )
    plain = analyze([path])
    assert plain.counts["me+work@x.com"].messages == 1
    assert plain.counts["me@x.com"].messages == 1

    joined = analyze([path], aliases=parse_aliases(
        ["me+work@x.com=me@x.com", "me+shop@x.com=me@x.com"]))
    assert joined.counts["me@x.com"].messages == 3
    assert "me+work@x.com" not in joined.counts


def test_recipients_excludes_senders_and_sorts_by_volume(tmp_path):
    path = write_mbox(tmp_path, msg(to="a@x.com", sender="loud@x.com"),
                      msg(to="a@x.com", sender="loud@x.com"),
                      msg(to="b@x.com", sender="loud@x.com"))
    report = analyze([path])
    assert [r.address for r in report.recipients()] == ["a@x.com", "b@x.com"]
    assert report.counts["loud@x.com"].sender == 3


def test_limit_stops_early(tmp_path):
    path = write_mbox(tmp_path, *[msg(subject=f"m{i}") for i in range(10)])
    assert analyze([path], limit=4).messages == 4


# ------------------------------------------------------- the header-only path


def test_header_blocks_skip_bodies_but_keep_the_boundaries(tmp_path):
    escaped = (
        b"From: s@x.com\r\nTo: me@x.com\r\nSubject: Body says From\r\n\r\n"
        # Properly From-escaped, as a well-formed mbox stores it.
        b">From here on it is prose.\r\nNot a separator.\r\n"
    )
    raw = mbox_bytes(msg(subject="One"), escaped, msg(subject="Three"))

    full = list(iter_mbox_messages(io.BytesIO(raw)))
    heads = list(iter_mbox_header_blocks(io.BytesIO(raw)))
    assert len(heads) == len(full) == 3
    for head, whole in zip(heads, full):
        assert whole.startswith(head)
    assert not any(b"prose" in head for head in heads)   # bodies really skipped


def test_header_blocks_split_exactly_where_the_full_reader_splits(tmp_path):
    """An unescaped "From " line starts a new message -- for both readers.

    That is inherent to the format rather than a choice either reader makes,
    and the point of the test is that the fast path does not disagree: a
    report that counted different messages than a build would be worse than
    no report.
    """
    unescaped = (
        b"From: s@x.com\r\nTo: me@x.com\r\nSubject: Unescaped\r\n\r\n"
        b"From here on it is prose.\r\nNot a separator.\r\n"
    )
    raw = mbox_bytes(msg(subject="One"), unescaped, msg(subject="Three"))

    full = list(iter_mbox_messages(io.BytesIO(raw)))
    heads = list(iter_mbox_header_blocks(io.BytesIO(raw)))
    assert len(heads) == len(full)
    for head, whole in zip(heads, full):
        assert whole.startswith(head)


def test_analyze_agrees_with_a_full_read(tmp_path):
    """Header-only counting must not drift from what a real build would see."""
    from pastscape.analyze import Report, _tally

    path = write_mbox(
        tmp_path,
        msg(to="a@x.com", delivered="me@x.com", cc="c@x.com"),
        msg(to="b@x.com", sender="a@x.com"),
        msg(to="me@x.com", delivered="me@x.com", cc="a@x.com, c@x.com"),
    )

    fast = analyze([path])
    slow = Report()
    for message in read_source(path):
        _tally(slow, message.headers, {})

    assert fast.messages == slow.messages == 3
    assert set(fast.counts) == set(slow.counts)
    for address, row in fast.counts.items():
        other = slow.counts[address]
        assert (row.messages, row.delivered, row.to_cc, row.sender) == (
            other.messages, other.delivered, other.to_cc, other.sender
        ), address


# ------------------------------------------------- addresses without brackets


@pytest.mark.parametrize("raw,expected", [
    ("me@x.com", "me@x.com"),
    ("<me@x.com>", "me@x.com"),
    ("  Me@X.COM ", "me@x.com"),
    # Yahoo Groups writes the delivery header this way.
    ("mailing list list@yahoogroups.com", "list@yahoogroups.com"),
    ("undisclosed recipients", ""),
    ("", ""),
])
def test_bare_address_is_recovered_from_the_slot(raw, expected):
    from pastscape.analyze import address_of
    assert address_of(raw) == expected


def test_a_bracketless_delivery_header_is_not_a_second_address(tmp_path):
    raw = (b"From: s@x.com\r\nTo: list@yahoogroups.com\r\n"
           b"Delivered-To: mailing list list@yahoogroups.com\r\n"
           b"Subject: Hi\r\n\r\nBody\r\n")
    report = analyze([write_mbox(tmp_path, raw)])
    assert set(report.counts) == {"s@x.com", "list@yahoogroups.com"}
    assert report.counts["list@yahoogroups.com"].delivered == 1
