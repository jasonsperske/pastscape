from pastscape.sources import detect_source, read_source
from pastscape.sources.eml import read_eml_dir
from pastscape.sources.mime import message_from_bytes, parse_addresses, parse_date

RAW = (
    b"From: =?utf-8?q?Jos=C3=A9_Garc=C3=ADa?= <jose@example.com>\r\n"
    b"To: One <one@example.com>, two@example.com\r\n"
    b"Subject: =?utf-8?q?Caf=C3=A9_meeting?=\r\n"
    b"Date: Tue, 03 Jun 1997 14:00:00 -0800\r\n"
    b"Message-ID: <cafe@example.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: multipart/mixed; boundary=BOUND\r\n"
    b"\r\n"
    b"--BOUND\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Plain body.\r\n"
    b"--BOUND\r\n"
    b'Content-Type: application/pdf; name="notes.pdf"\r\n'
    b'Content-Disposition: attachment; filename="notes.pdf"\r\n'
    b"Content-Transfer-Encoding: base64\r\n\r\n"
    b"JVBERi0x\r\n"
    b"--BOUND--\r\n"
)


def test_encoded_headers_are_decoded():
    msg = message_from_bytes(RAW, "Inbox", "t.eml")
    assert msg.subject == "Café meeting"
    assert msg.sender.name == "José García"
    assert msg.sender.addr == "jose@example.com"
    assert [a.addr for a in msg.to] == ["one@example.com", "two@example.com"]


def test_attachment_extracted_from_multipart():
    msg = message_from_bytes(RAW, "Inbox", "t.eml")
    assert msg.has_attachments
    att = msg.attachments[0]
    assert att.filename == "notes.pdf"
    assert att.content_type == "application/pdf"
    assert att.payload.startswith(b"%PDF")
    assert msg.body_text.strip() == "Plain body."


def test_directory_structure_becomes_folder_structure(tmp_path):
    (tmp_path / "Sent" / "2004").mkdir(parents=True)
    (tmp_path / "Sent" / "2004" / "a.eml").write_bytes(RAW)
    (tmp_path / "Deleted Items").mkdir()
    (tmp_path / "Deleted Items" / "b.eml").write_bytes(RAW.replace(b"<cafe@", b"<cafe2@"))

    folders = sorted({m.folder for m in read_eml_dir(tmp_path)})
    assert folders == ["Sent/2004", "Trash"]


def test_maildir_layout_is_flattened(tmp_path):
    box = tmp_path / "Archive" / "cur"
    box.mkdir(parents=True)
    (tmp_path / "Archive" / "new").mkdir()
    (box / "1234.host").write_bytes(RAW)
    msgs = list(read_eml_dir(tmp_path))
    assert [m.folder for m in msgs] == ["Archive"]


def test_emlx_length_prefix_is_stripped(tmp_path):
    (tmp_path / "m.emlx").write_bytes(str(len(RAW)).encode() + b"\n" + RAW)
    msgs = list(read_eml_dir(tmp_path))
    assert len(msgs) == 1
    assert msgs[0].subject == "Café meeting"


def test_malformed_file_does_not_break_the_scan(tmp_path):
    (tmp_path / "good.eml").write_bytes(RAW)
    (tmp_path / "junk.eml").write_bytes(b"\x00\x01\x02 not a message at all")
    (tmp_path / "empty.eml").write_bytes(b"")
    msgs = list(read_eml_dir(tmp_path))
    assert any(m.subject == "Café meeting" for m in msgs)


def test_bad_date_falls_back_to_none():
    assert parse_date("not a date") is None
    assert parse_date("") is None
    assert parse_date("Tue, 03 Jun 1997 14:00:00 -0800").year == 1997


def test_group_syntax_addresses():
    addrs = parse_addresses("undisclosed-recipients:;")
    assert all(a.addr or a.name for a in addrs)


def test_detect_source(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    assert detect_source(d) == "eml"
    (d / "cur").mkdir()
    (d / "new").mkdir()
    assert detect_source(d) == "maildir"

    pst = tmp_path / "x.pst"
    pst.write_bytes(b"!BDN" + b"\0" * 100)
    assert detect_source(pst) == "pst"

    unnamed = tmp_path / "weird.bin"
    unnamed.write_bytes(b"!BDN" + b"\0" * 100)
    assert detect_source(unnamed) == "pst"


def test_mbox_roundtrip(tmp_path):
    mbox = tmp_path / "Inbox.mbox"
    mbox.write_bytes(
        b"From sender@example.com Tue Jun  3 14:00:00 1997\r\n" + RAW +
        b"\r\nFrom sender@example.com Tue Jun  3 15:00:00 1997\r\n" +
        RAW.replace(b"<cafe@", b"<cafe3@")
    )
    msgs = list(read_source(mbox))
    assert len(msgs) == 2
    assert all(m.folder == "Inbox" for m in msgs)
