"""Year-grouped folder tree.

These build with account_folders=False so the year layout is tested on its
own; the interaction between mailbox roots and years lives in test_accounts.
"""

import json
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from pastscape.builder import build_site, place
from pastscape.model import Address, Message, folder_sort_key, year_folder


def msg(folder="Inbox", year=2011, month=6):
    return Message(
        folder=folder,
        subject=f"Message from {year}",
        sender=Address(name="A", addr="a@example.com"),
        date=datetime(year, month, 2, 12, tzinfo=timezone.utc),
        body_text="body",
    )


def write_eml(root: Path, folder: str, subject: str, when: datetime) -> None:
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in subject if c.isalnum() or c in " -_")
    (d / f"{safe}.eml").write_text(
        f"From: Sender <sender@example.com>\n"
        f"To: you@example.com\n"
        f"Subject: {subject}\n"
        f"Date: {format_datetime(when)}\n"
        f"Message-ID: <{safe.replace(' ', '-')}@example.com>\n"
        f"\nBody.\n",
        "utf-8",
    )


# --------------------------------------------------------------------- units


def test_year_comes_from_the_message_date():
    assert year_folder(msg(year=2004)) == "2004"
    undated = msg()
    undated.date = None
    assert year_folder(undated) == "Undated"


def test_place_puts_the_year_at_the_root():
    assert place(msg(folder="Inbox", year=2011)) == "2011/Inbox"
    assert place(msg(folder="Inbox/Projects", year=2011)) == "2011/Inbox/Projects"


def test_place_normalises_before_prepending_the_year():
    # normalize_folder only maps the leading segment, so the order matters.
    assert place(msg(folder="Deleted Items", year=2011)) == "2011/Trash"
    assert place(msg(folder="Sent Items", year=2011)) == "2011/Sent"


def test_place_can_be_turned_off():
    assert place(msg(folder="Inbox", year=2011), year_folders=False) == "Inbox"


def test_folder_prefix_stays_outermost():
    assert place(msg(year=2011), folder_prefix="Work") == "Work/2011/Inbox"


def test_undated_messages_get_their_own_bucket():
    m = msg()
    m.date = None
    assert place(m) == "Undated/Inbox"


# ------------------------------------------------------------------- sorting


def test_years_sort_newest_first():
    paths = ["2004", "2011", "1998", "2026"]
    assert sorted(paths, key=folder_sort_key) == ["2026", "2011", "2004", "1998"]


def test_communicator_order_applies_inside_a_year():
    paths = ["2011/Zebra", "2011/Trash", "2011/Inbox", "2011/Sent", "2011/Alpha"]
    assert sorted(paths, key=folder_sort_key) == [
        "2011/Inbox", "2011/Sent", "2011/Trash", "2011/Alpha", "2011/Zebra",
    ]


def test_undated_sorts_after_every_year():
    paths = ["Undated", "1998", "2026"]
    assert sorted(paths, key=folder_sort_key) == ["2026", "1998", "Undated"]


# ----------------------------------------------------------------- the build


@pytest.fixture
def multi_year(tmp_path) -> Path:
    root = tmp_path / "mail"
    for year in (2004, 2005, 2011):
        write_eml(root, "Inbox", f"Inbox {year}", datetime(year, 6, 2, 12, tzinfo=timezone.utc))
        write_eml(root, "Sent", f"Sent {year}", datetime(year, 7, 2, 12, tzinfo=timezone.utc))
    write_eml(root, "Inbox/Projects", "Nested 2011",
              datetime(2011, 8, 2, 12, tzinfo=timezone.utc))
    return root


def folders_of(site: Path):
    cfg = json.loads((site / "data" / "folders.json").read_text("utf-8"))
    return cfg["folders"], [f["path"] for f in cfg["folders"]]


def test_tree_is_grouped_by_year(tmp_path, multi_year):
    out = tmp_path / "site"
    build_site([multi_year], out, title="Local Mail", account_folders=False)
    meta, paths = folders_of(out)

    assert paths[0] == "2011"
    assert "2011/Inbox" in paths
    assert "2004/Sent" in paths
    assert "Inbox" not in paths, "an ungrouped folder leaked to the root"

    tops = [f for f in meta if f["depth"] == 0]
    assert [f["path"] for f in tops] == ["2011", "2005", "2004"]


def test_year_folders_hold_no_messages_of_their_own(tmp_path, multi_year):
    out = tmp_path / "site"
    build_site([multi_year], out, title="Local Mail", account_folders=False)
    meta, _ = folders_of(out)
    by_path = {f["path"]: f for f in meta}

    assert by_path["2011"]["count"] == 0
    assert by_path["2011"]["parent"] is None
    assert by_path["2011/Inbox"]["count"] == 1
    assert by_path["2011/Inbox"]["parent"] == "2011"
    assert by_path["2011/Inbox/Projects"]["parent"] == "2011/Inbox"
    assert by_path["2011/Inbox/Projects"]["depth"] == 2


def test_no_year_folders_keeps_the_old_shape(tmp_path, multi_year):
    out = tmp_path / "site"
    build_site([multi_year], out, title="Local Mail", year_folders=False,
               account_folders=False)
    _, paths = folders_of(out)
    assert paths[0] == "Inbox"
    assert not any(p[:4].isdigit() for p in paths)


def test_messages_land_in_the_year_of_their_date(tmp_path, multi_year):
    from pastscape.state import Manifest

    out = tmp_path / "site"
    build_site([multi_year], out, title="Local Mail", account_folders=False)
    by_subject = {r.subject: r.folder for r in Manifest.load(out).messages.values()}
    assert by_subject["Inbox 2004"] == "2004/Inbox"
    assert by_subject["Sent 2011"] == "2011/Sent"
    assert by_subject["Nested 2011"] == "2011/Inbox/Projects"


def test_twenty_year_archive(tmp_path):
    """The stated use case: an archive spanning more than twenty years."""
    root = tmp_path / "mail"
    years = list(range(2003, 2027))          # 24 years
    for year in years:
        write_eml(root, "Inbox", f"Inbox {year}", datetime(year, 3, 1, 9, tzinfo=timezone.utc))
        write_eml(root, "Sent", f"Sent {year}", datetime(year, 9, 1, 9, tzinfo=timezone.utc))

    out = tmp_path / "site"
    stats = build_site([root], out, title="Long Archive", account_folders=False)
    assert stats.total == len(years) * 2

    meta, paths = folders_of(out)
    tops = [f["path"] for f in meta if f["depth"] == 0]
    assert len(tops) == len(years)
    assert tops == [str(y) for y in reversed(years)]   # newest first, back to the oldest
    assert tops[0] == "2026" and tops[-1] == "2003"

    # Every year folder is a real, fetchable listing.
    for f in meta:
        assert (out / f["file"]).is_file()


def test_a_gap_year_gets_no_folder(tmp_path):
    root = tmp_path / "mail"
    write_eml(root, "Inbox", "Old", datetime(2004, 1, 1, tzinfo=timezone.utc))
    write_eml(root, "Inbox", "New", datetime(2008, 1, 1, tzinfo=timezone.utc))
    out = tmp_path / "site"
    build_site([root], out, title="Gappy", account_folders=False)
    _, paths = folders_of(out)
    tops = [p for p in paths if "/" not in p]
    assert tops == ["2008", "2004"], "years with no mail should not appear"


def test_new_year_of_mail_only_adds_that_year(tmp_path, multi_year):
    out = tmp_path / "site"
    build_site([multi_year], out, title="Local Mail", account_folders=False)

    write_eml(multi_year, "Inbox", "Arrived later",
              datetime(2026, 2, 2, 12, tzinfo=timezone.utc))
    stats = build_site([multi_year], out, title="Local Mail", account_folders=False)

    assert stats.added == 1
    _, paths = folders_of(out)
    assert paths[0] == "2026"
    assert "2026/Inbox" in paths
