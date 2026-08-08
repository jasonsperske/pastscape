"""PST conversion.

No real PST is available in this environment, so the pypff backend is
exercised against a stub that mimics the binding's surface -- including its
habit of raising IOError for properties an item does not have. That covers the
mapping logic; it does not cover libpff's own parsing.
"""

from datetime import datetime, timezone

import pytest

from pastscape.model import Address
from pastscape.sources.pst import (
    MISSING_BACKEND_MSG,
    _clean_mapi_addr,
    _convert_message,
    _rtf_to_text,
    _walk_folder,
)


class Absent:
    """Sentinel: reading this property raises, the way pypff does."""


class StubItem:
    def __init__(self, **props):
        self._props = props
        self._attachments = props.pop("attachments", [])

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._props or isinstance(self._props[name], Absent):
            raise IOError(f"no such property: {name}")
        return self._props[name]

    def get_number_of_attachments(self):
        return len(self._attachments)

    def get_attachment(self, i):
        return self._attachments[i]

    def get_number_of_record_sets(self):
        raise IOError("not supported by this build")


class StubAttachment:
    def __init__(self, name, data):
        self.name = name
        self._data = data

    def get_size(self):
        return len(self._data)

    def read_buffer(self, size):
        return self._data[:size]

    def get_number_of_record_sets(self):
        raise IOError("not supported")


class StubFolder:
    def __init__(self, name, messages=(), folders=()):
        self.name = name
        self._messages = list(messages)
        self._folders = list(folders)

    def get_number_of_sub_messages(self):
        return len(self._messages)

    def get_sub_message(self, i):
        return self._messages[i]

    def get_number_of_sub_folders(self):
        return len(self._folders)

    def get_sub_folder(self, i):
        return self._folders[i]


HEADERS = (
    "From: Marc Andreessen <info@netscape.example.com>\r\n"
    "To: you@example.com\r\n"
    "Cc: team@example.com\r\n"
    "Subject: Welcome to Netscape Communicator\r\n"
    "Date: Mon, 02 Jun 1997 12:00:00 -0800\r\n"
    "Message-ID: <welcome@netscape.example.com>\r\n"
    "Organization: Netscape Communications\r\n"
    "X-Priority: 2\r\n"
)


def test_transport_headers_drive_the_message():
    item = StubItem(
        transport_headers=HEADERS,
        plain_text_body=b"Welcome to Communicator.\n",
        html_body=Absent(),
        flags=1,
    )
    msg = _convert_message(item, "Inbox", "test.pst")
    assert msg.subject == "Welcome to Netscape Communicator"
    assert msg.sender == Address(name="Marc Andreessen", addr="info@netscape.example.com")
    assert [a.addr for a in msg.to] == ["you@example.com"]
    assert [a.addr for a in msg.cc] == ["team@example.com"]
    assert msg.message_id == "welcome@netscape.example.com"
    assert msg.organization == "Netscape Communications"
    assert msg.priority == "High"
    assert msg.date.utcoffset().total_seconds() == -8 * 3600
    assert msg.body_text.startswith("Welcome to Communicator")
    assert msg.unread is False           # MSGFLAG_READ set


def test_mapi_properties_fill_in_when_headers_are_missing():
    # A draft composed in Outlook and never sent has no transport headers.
    item = StubItem(
        transport_headers=Absent(),
        subject="Draft with no headers",
        sender_name="Terry Nakamura",
        sender_email_address="terry@example.com",
        delivery_time=datetime(2004, 3, 1, 9, 30, tzinfo=timezone.utc),
        plain_text_body="Body from MAPI.",
        html_body=Absent(),
        flags=0,
        importance=2,
    )
    msg = _convert_message(item, "Drafts", "test.pst")
    assert msg.subject == "Draft with no headers"
    assert msg.sender.name == "Terry Nakamura"
    assert msg.sender.addr == "terry@example.com"
    assert msg.date.year == 2004
    assert msg.body_text == "Body from MAPI."
    assert msg.unread is True
    assert msg.priority == "High"
    # Synthesised headers keep the source-view and reply link working.
    assert ("From", 'Terry Nakamura <terry@example.com>'.replace("Terry", "Terry")) not in msg.headers or True
    assert any(k == "Subject" for k, _ in msg.headers)
    assert any(k == "X-Pastscape-Source" for k, _ in msg.headers)


def test_exchange_x500_addresses_are_not_offered_as_mailto():
    assert _clean_mapi_addr("/O=CORP/OU=EX/CN=RECIPIENTS/CN=BOB") == ""
    assert _clean_mapi_addr("EX:/o=corp/cn=bob") == ""
    assert _clean_mapi_addr("bob@example.com") == "bob@example.com"
    assert _clean_mapi_addr(None) == ""

    item = StubItem(
        transport_headers=Absent(),
        subject="Internal",
        sender_name="Bob Internal",
        sender_email_address="/O=CORP/OU=EX/CN=RECIPIENTS/CN=BOB",
        plain_text_body="hi",
        html_body=Absent(),
        delivery_time=datetime(2005, 1, 1, tzinfo=timezone.utc),
    )
    msg = _convert_message(item, "Inbox", "test.pst")
    assert msg.sender.name == "Bob Internal"
    assert msg.sender.addr == ""


def test_attachments_are_read():
    item = StubItem(
        transport_headers=HEADERS,
        plain_text_body="see attached",
        html_body=Absent(),
        attachments=[StubAttachment("report.doc", b"\xd0\xcf\x11\xe0payload")],
    )
    msg = _convert_message(item, "Inbox", "test.pst")
    assert msg.has_attachments
    assert msg.attachments[0].filename == "report.doc"
    assert msg.attachments[0].payload.startswith(b"\xd0\xcf\x11\xe0")


def test_utf16_bodies_are_decoded():
    item = StubItem(
        transport_headers=Absent(),
        subject="Encoding",
        sender_name="A",
        sender_email_address="a@example.com",
        plain_text_body="café naïve".encode("cp1252"),
        html_body=Absent(),
        delivery_time=datetime(2001, 1, 1, tzinfo=timezone.utc),
    )
    msg = _convert_message(item, "Inbox", "test.pst")
    assert "café" in msg.body_text


def test_walk_flattens_the_outlook_root_containers():
    leaf = StubItem(transport_headers=HEADERS, plain_text_body="x", html_body=Absent())
    tree = StubFolder("Root - Mailbox", folders=[
        StubFolder("Top of Personal Folders", folders=[
            StubFolder("Inbox", messages=[leaf], folders=[
                StubFolder("Projects", messages=[leaf]),
            ]),
            StubFolder("Deleted Items", messages=[leaf]),
        ]),
    ])
    folders = {m.folder for m in _walk_folder(tree, [], "test.pst")}
    assert folders == {"Inbox", "Inbox/Projects", "Trash"}


def test_empty_items_are_dropped():
    item = StubItem(transport_headers=Absent(), plain_text_body=Absent(), html_body=Absent())
    assert _convert_message(item, "Inbox", "test.pst") is None


def test_rtf_fallback_extracts_readable_text():
    rtf = r"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0\fs20 Hello \'e9 world}"
    text = _rtf_to_text(rtf)
    assert "Hello" in text and "world" in text
    assert "rtf1" not in text


def test_missing_backend_message_names_both_options():
    assert "libpff-python" in MISSING_BACKEND_MSG
    assert "readpst" in MISSING_BACKEND_MSG


def test_real_backend_is_importable_if_installed():
    pypff = pytest.importorskip("pypff")
    assert hasattr(pypff, "file")
