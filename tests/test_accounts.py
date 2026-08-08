"""One root tree per recipient mailbox."""

import json
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from pastscape.builder import assign_accounts, build_site, place
from pastscape.model import OTHER_ACCOUNT, Address, Message, account_candidate


def make(to="you@example.com", sender="them@example.com", folder="Inbox",
         delivered=None, subject="Hi", uid=None, year=2011, cc=()):
    msg = Message(
        uid=uid or f"u{abs(hash((to, sender, folder, subject))) % 10**8}",
        folder=folder,
        subject=subject,
        sender=Address(name="Them", addr=sender),
        to=[Address(addr=a) for a in ([to] if isinstance(to, str) else to) if a],
        cc=[Address(addr=a) for a in cc],
        date=datetime(year, 6, 2, 12, tzinfo=timezone.utc),
        body_text="body",
    )
    msg.headers = [("To", to if isinstance(to, str) else ", ".join(to))]
    if delivered:
        msg.headers.append(("Delivered-To", delivered))
    return msg


def write_eml(root: Path, folder: str, subject: str, *, to: str, sender: str,
              delivered: str | None = None, year: int = 2011) -> None:
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in subject if c.isalnum() or c in " -_")
    head = (
        f"From: Someone <{sender}>\n"
        f"To: {to}\n"
        f"Subject: {subject}\n"
        f"Date: {format_datetime(datetime(year, 6, 2, 12, tzinfo=timezone.utc))}\n"
        f"Message-ID: <{safe.replace(' ', '-')}-{folder.replace('/', '-')}@example.com>\n"
    )
    if delivered:
        head += f"Delivered-To: {delivered}\n"
    (d / f"{safe}.eml").write_text(head + "\nBody.\n", "utf-8")


# --------------------------------------------------------------- which address


def test_delivery_header_beats_the_to_line():
    # List mail is addressed to the list but delivered to you.
    msg = make(to="list@lists.example.org", delivered="jason@example.com")
    assert account_candidate(msg) == "jason@example.com"


def test_sent_mail_is_attributed_to_the_sender():
    msg = make(folder="Sent", sender="jason@example.com", to="someone@example.com")
    assert account_candidate(msg) == "jason@example.com"
    draft = make(folder="Drafts", sender="jason@example.com", to="someone@example.com")
    assert account_candidate(draft) == "jason@example.com"


def test_to_is_used_when_nothing_better_exists():
    # Old mail frequently carries no delivery header at all.
    assert account_candidate(make(to="jason@example.com")) == "jason@example.com"


def test_candidate_is_case_folded():
    assert account_candidate(make(to="Jason@Example.COM")) == "jason@example.com"


def test_falls_back_to_the_sender_with_no_recipients():
    msg = make(to="", sender="someone@example.com")
    assert account_candidate(msg) == "someone@example.com"


# ----------------------------------------------------------------- assignment


def test_two_mailboxes_get_one_root_each():
    messages = ([make(to="a@example.com", uid=f"a{i}") for i in range(5)] +
                [make(to="b@example.com", uid=f"b{i}") for i in range(5)])
    assigned = assign_accounts(messages)
    assert set(assigned.values()) == {"a@example.com", "b@example.com"}


def test_a_single_mailbox_absorbs_the_stragglers():
    """One dominant address should not be split by a couple of oddities."""
    messages = ([make(to="a@example.com", uid=f"a{i}") for i in range(20)] +
                [make(to="stray@example.com", uid="s1")])
    assigned = assign_accounts(messages)
    assert set(assigned.values()) == {"a@example.com"}


def test_an_occasional_list_does_not_earn_a_root():
    messages = ([make(to="a@example.com", uid=f"a{i}") for i in range(30)] +
                [make(to="b@example.com", uid=f"b{i}") for i in range(30)] +
                [make(to="list@lists.example.org", uid=f"l{i}") for i in range(2)])
    assigned = assign_accounts(messages)
    roots = set(assigned.values())
    assert roots == {"a@example.com", "b@example.com", OTHER_ACCOUNT}
    assert assigned["l0"] == OTHER_ACCOUNT


def test_explicit_accounts_are_exact():
    messages = [
        make(to="a@example.com", uid="1"),
        make(to="list@lists.example.org", cc=("b@example.com",), uid="2"),
        make(to="nobody@example.net", uid="3"),
    ]
    assigned = assign_accounts(messages, explicit=["a@example.com", "b@example.com"])
    assert assigned["1"] == "a@example.com"
    assert assigned["2"] == "b@example.com"      # matched via Cc
    assert assigned["3"] == OTHER_ACCOUNT


def test_explicit_accounts_keep_the_spelling_you_gave():
    messages = [make(to="Jason@Example.com", uid="1")]
    assigned = assign_accounts(messages, explicit=["jason@example.com"])
    assert assigned["1"] == "jason@example.com"


def test_disabled_assigns_nothing():
    assert assign_accounts([make()], enabled=False) == {}


def test_no_messages_is_not_an_error():
    assert assign_accounts([]) == {}


# ---------------------------------------------------------------------- paths


def test_account_sits_above_the_year():
    msg = make(year=2011)
    assert place(msg, "jason@example.com") == "jason@example.com/2011/Inbox"


def test_folder_prefix_still_wraps_everything():
    msg = make(year=2011)
    assert place(msg, "a@example.com", folder_prefix="Work") == "Work/a@example.com/2011/Inbox"


def test_slash_in_an_address_cannot_forge_a_folder_level():
    msg = make(year=2011)
    assert place(msg, "we/ird@example.com").startswith("we_ird@example.com/")


# ----------------------------------------------------------------- the build


@pytest.fixture
def two_mailboxes(tmp_path) -> Path:
    root = tmp_path / "mail"
    for i in range(4):
        write_eml(root, "Inbox", f"To jason {i}", to="jason@example.com",
                  sender="them@example.com", year=2011)
        write_eml(root, "Inbox", f"To dineane {i}", to="dineane@example.com",
                  sender="them@example.com", year=2012)
    write_eml(root, "Sent", "From jason", to="them@example.com",
              sender="jason@example.com", year=2011)
    return root


def cfg_of(site: Path):
    return json.loads((site / "data" / "folders.json").read_text("utf-8"))


def test_each_mailbox_becomes_a_root(tmp_path, two_mailboxes):
    out = tmp_path / "site"
    build_site([two_mailboxes], out, title="Local Mail")
    cfg = cfg_of(out)

    assert cfg["accounts"] == ["dineane@example.com", "jason@example.com"]
    paths = [f["path"] for f in cfg["folders"]]
    assert "dineane@example.com/2012/Inbox" in paths
    assert "jason@example.com/2011/Inbox" in paths

    roots = [f for f in cfg["folders"] if f["depth"] == 0]
    assert {f["path"] for f in roots} == set(cfg["accounts"])
    assert all(f["count"] == 0 for f in roots), "a mailbox root holds no mail itself"


def test_sent_mail_files_under_the_sending_mailbox(tmp_path, two_mailboxes):
    from pastscape.state import Manifest

    out = tmp_path / "site"
    build_site([two_mailboxes], out, title="Local Mail")
    by_subject = {r.subject: r.folder for r in Manifest.load(out).messages.values()}
    assert by_subject["From jason"] == "jason@example.com/2011/Sent"


def test_no_account_folders_keeps_years_at_the_root(tmp_path, two_mailboxes):
    out = tmp_path / "site"
    build_site([two_mailboxes], out, title="Local Mail", account_folders=False)
    cfg = cfg_of(out)
    assert cfg["accounts"] == []
    assert [f["path"] for f in cfg["folders"]][0] in ("2012", "2011")


def test_explicit_accounts_from_the_build(tmp_path, two_mailboxes):
    out = tmp_path / "site"
    build_site([two_mailboxes], out, title="Local Mail", accounts=["jason@example.com"])
    cfg = cfg_of(out)
    assert cfg["accounts"] == [OTHER_ACCOUNT, "jason@example.com"]
    paths = [f["path"] for f in cfg["folders"]]
    assert any(p.startswith("jason@example.com/") for p in paths)
    assert any(p.startswith(OTHER_ACCOUNT + "/") for p in paths)


def test_single_mailbox_gets_one_root(tmp_path):
    root = tmp_path / "mail"
    for i in range(5):
        write_eml(root, "Inbox", f"Note {i}", to="solo@example.com", sender="x@example.com")
    out = tmp_path / "site"
    build_site([root], out, title="Local Mail")
    cfg = cfg_of(out)
    assert cfg["accounts"] == ["solo@example.com"]
    assert [f["path"] for f in cfg["folders"]][0] == "solo@example.com"


def test_rebuild_stays_incremental_with_accounts(tmp_path, two_mailboxes):
    out = tmp_path / "site"
    first = build_site([two_mailboxes], out, title="Local Mail")
    again = build_site([two_mailboxes], out, title="Local Mail")
    assert first.added == 9
    assert again.added == 0 and again.unchanged == 9
