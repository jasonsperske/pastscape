"""mbox: plain, compressed, and Google Takeout shaped."""

import bz2
import gzip
import io
import lzma
import tarfile
import zipfile
from pathlib import Path

import pytest

from pastscape.builder import build_site
from pastscape.model import compute_uid
from pastscape.sources import detect_source, read_source
from pastscape.sources.mbox import (
    folder_for_filename,
    folder_from_labels,
    iter_mbox_messages,
    split_labels,
)


def message(subject: str, labels: str | None = None, body: str = "Body text.") -> bytes:
    head = (
        f"From: Sender <sender@example.com>\r\n"
        f"To: you@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Tue, 03 Jun 2008 14:00:00 -0800\r\n"
        f"Message-ID: <{subject.replace(' ', '-')}@example.com>\r\n"
    )
    if labels is not None:
        head += f"X-Gmail-Labels: {labels}\r\n"
    return (head + "\r\n" + body + "\r\n").encode()


def mbox_bytes(*messages: bytes) -> bytes:
    out = b""
    for i, raw in enumerate(messages):
        out += f"From sender@example.com Tue Jun  3 14:0{i}:00 2008\r\n".encode()
        out += raw
        out += b"\r\n"
    return out


PLAIN = mbox_bytes(message("One"), message("Two"), message("Three"))


# ------------------------------------------------------------------ splitting


def test_splits_on_from_lines():
    msgs = list(iter_mbox_messages(io.BytesIO(PLAIN)))
    assert len(msgs) == 3
    assert b"Subject: One" in msgs[0]
    assert b"Subject: Three" in msgs[2]
    # The envelope separator is not part of the message.
    assert not msgs[0].startswith(b"From sender@example.com Tue")


def test_body_line_starting_with_from_does_not_split_the_message():
    tricky = mbox_bytes(
        message("Tricky", body="I quoted them:\nFrom now on we ship on Fridays.\nEnd."),
        message("After"),
    )
    msgs = list(iter_mbox_messages(io.BytesIO(tricky)))
    assert len(msgs) == 2, "a 'From ' line mid-body split the message"
    assert b"From now on we ship on Fridays." in msgs[0]
    assert b"Subject: After" in msgs[1]


def test_empty_stream_yields_nothing():
    assert list(iter_mbox_messages(io.BytesIO(b""))) == []


# ---------------------------------------------------------------- compression


@pytest.mark.parametrize("suffix,compress", [
    (".gz", gzip.compress),
    (".bz2", bz2.compress),
    (".xz", lzma.compress),
])
def test_compressed_mbox_reads_like_the_plain_one(tmp_path, suffix, compress):
    plain_path = tmp_path / "Inbox.mbox"
    plain_path.write_bytes(PLAIN)
    packed = tmp_path / f"Inbox.mbox{suffix}"
    packed.write_bytes(compress(PLAIN))

    from_plain = list(read_source(plain_path))
    from_packed = list(read_source(packed))

    assert len(from_packed) == 3
    assert [compute_uid(m) for m in from_plain] == [compute_uid(m) for m in from_packed]
    assert {m.folder for m in from_packed} == {"Inbox"}


def test_compression_is_detected_by_content_not_extension(tmp_path):
    # Takeout hands out files whose names do not always match their contents.
    mislabelled = tmp_path / "Archive.mbox"
    mislabelled.write_bytes(gzip.compress(PLAIN))
    assert detect_source(mislabelled) == "mbox-compressed"
    assert len(list(read_source(mislabelled))) == 3


def test_folder_for_filename_strips_both_suffixes():
    assert folder_for_filename("Inbox.mbox.gz") == "Inbox"
    assert folder_for_filename("Sent Items.mbox") == "Sent"
    assert folder_for_filename("Takeout/Mail/Archive.mbox.bz2") == "Archive"


# ------------------------------------------------------------------- takeout


def takeout_mbox() -> bytes:
    return mbox_bytes(
        message("Inbox note", labels="Inbox,Important,Unread"),
        message("Something I sent", labels="Sent"),
        message("A draft", labels="Draft"),
        message("Junk offer", labels="Spam,Unread"),
        message("Deleted thing", labels="Trash"),
        message("Tax receipt", labels="Receipts,Archived"),
        message("Nested label", labels="Projects/Pastscape"),
        message("Starred archived", labels="Archived,Starred"),
        message("Labelled but in inbox", labels="Inbox,Receipts"),
        message("Comma label", labels='"Bills, utilities",Archived'),
    )


def make_takeout_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Takeout/archive_browser.html", "<html>not mail</html>")
        z.writestr("Takeout/Mail/All mail Including Spam and Trash.mbox", takeout_mbox())
    return path


def make_takeout_tgz(path: Path) -> Path:
    with tarfile.open(path, "w:gz") as t:
        data = takeout_mbox()
        info = tarfile.TarInfo("Takeout/Mail/All mail Including Spam and Trash.mbox")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return path


def test_takeout_zip_is_detected_and_read(tmp_path):
    archive = make_takeout_zip(tmp_path / "takeout-20260808.zip")
    assert detect_source(archive) == "mbox-archive"
    msgs = list(read_source(archive))
    assert len(msgs) == 10


def test_takeout_tgz_is_detected_and_read(tmp_path):
    archive = make_takeout_tgz(tmp_path / "takeout.tgz")
    assert detect_source(archive) == "mbox-archive"
    assert len(list(read_source(archive))) == 10


def test_gmail_labels_become_folders(tmp_path):
    archive = make_takeout_zip(tmp_path / "takeout.zip")
    by_subject = {m.subject: m for m in read_source(archive)}

    assert by_subject["Inbox note"].folder == "Inbox"
    assert by_subject["Something I sent"].folder == "Sent"
    assert by_subject["A draft"].folder == "Drafts"
    assert by_subject["Junk offer"].folder == "Junk"
    assert by_subject["Deleted thing"].folder == "Trash"
    # An archived message with a user label files under the label...
    assert by_subject["Tax receipt"].folder == "Receipts"
    # ...but one still in the inbox stays in Inbox.
    assert by_subject["Labelled but in inbox"].folder == "Inbox"
    # Gmail's nested labels are already folder paths.
    assert by_subject["Nested label"].folder == "Projects/Pastscape"
    # Nothing but state labels means it was simply archived.
    assert by_subject["Starred archived"].folder == "Archive"
    # Labels containing commas are quoted by Gmail.
    assert by_subject["Comma label"].folder == "Bills, utilities"


def test_gmail_state_labels_set_flags(tmp_path):
    archive = make_takeout_zip(tmp_path / "takeout.zip")
    by_subject = {m.subject: m for m in read_source(archive)}
    assert by_subject["Inbox note"].unread is True
    assert by_subject["Something I sent"].unread is False
    assert by_subject["Starred archived"].flagged is True
    assert by_subject["Tax receipt"].flagged is False


def test_label_parsing_units():
    assert split_labels('Inbox,Important') == ["Inbox", "Important"]
    assert split_labels('"Bills, utilities",Archived') == ["Bills, utilities", "Archived"]
    assert split_labels("") == []

    assert folder_from_labels(["Inbox", "Important"]) == "Inbox"
    assert folder_from_labels(["Category Promotions", "Archived"]) == "Archive"
    assert folder_from_labels(["IMAP_Flagged", "Archived"]) == "Archive"
    assert folder_from_labels(["Sent", "Receipts"]) == "Sent"
    assert folder_from_labels([]) == "Archive"


def test_container_without_mail_says_so(tmp_path):
    empty = tmp_path / "photos.zip"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("Takeout/Photos/img.jpg", b"\xff\xd8\xff")
    with pytest.raises(RuntimeError, match="no .mbox or .eml"):
        list(read_source(empty))


def test_takeout_builds_a_site_with_a_folder_tree(tmp_path):
    archive = make_takeout_zip(tmp_path / "takeout.zip")
    out = tmp_path / "site"
    stats = build_site([archive], out, title="Gmail")
    assert stats.total == 10

    import json
    cfg = json.loads((out / "data" / "folders.json").read_text("utf-8"))
    paths = [f["path"] for f in cfg["folders"]]
    # Grouped by year, then Communicator's own folders, then the Gmail labels.
    assert paths[:6] == ["2008", "2008/Inbox", "2008/Drafts", "2008/Sent",
                         "2008/Trash", "2008/Junk"]
    assert "2008/Receipts" in paths
    assert "2008/Projects/Pastscape" in paths


def test_takeout_rebuild_is_incremental(tmp_path):
    archive = make_takeout_zip(tmp_path / "takeout.zip")
    out = tmp_path / "site"
    build_site([archive], out, title="Gmail")
    again = build_site([archive], out, title="Gmail")
    assert again.added == 0 and again.unchanged == 10


def test_plain_mbox_still_uses_status_headers(tmp_path):
    raw = mbox_bytes(
        b"From: a@example.com\r\nSubject: Read one\r\nStatus: RO\r\n"
        b"Date: Tue, 03 Jun 2008 14:00:00 -0800\r\n\r\nbody\r\n",
        b"From: a@example.com\r\nSubject: Unread one\r\nStatus: O\r\n"
        b"Date: Tue, 03 Jun 2008 15:00:00 -0800\r\n\r\nbody\r\n",
    )
    path = tmp_path / "Inbox.mbox"
    path.write_bytes(raw)
    by_subject = {m.subject: m for m in read_source(path)}
    assert by_subject["Read one"].unread is False
    assert by_subject["Unread one"].unread is True


def test_intermediate_label_folders_are_created(tmp_path):
    """Gmail's "Projects/Pastscape" needs a "Projects" row, even though no
    message is filed directly in it, or the child renders orphaned."""
    import json
    archive = make_takeout_zip(tmp_path / "takeout.zip")
    out = tmp_path / "site"
    build_site([archive], out, title="Gmail")

    cfg = json.loads((out / "data" / "folders.json").read_text("utf-8"))
    by_path = {f["path"]: f for f in cfg["folders"]}

    assert "2008/Projects" in by_path, "parent folder missing from the tree"
    assert by_path["2008/Projects"]["count"] == 0
    assert by_path["2008/Projects"]["depth"] == 1
    assert by_path["2008/Projects"]["parent"] == "2008"

    child = by_path["2008/Projects/Pastscape"]
    assert child["depth"] == 2
    assert child["parent"] == "2008/Projects"
    assert child["count"] == 1

    # Placeholder folders still get a listing file, so clicking one works.
    assert (out / by_path["2008/Projects"]["file"]).is_file()
    assert json.loads((out / by_path["2008/Projects"]["file"]).read_text())["rows"] == []
