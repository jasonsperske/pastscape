from datetime import datetime, timezone

from pastscape.model import (
    Address,
    Message,
    clean_message_id,
    compute_uid,
    folder_sort_key,
    normalize_folder,
    normalize_subject,
    parse_references,
    tokenize,
)


def make(**kw) -> Message:
    msg = Message(
        subject=kw.pop("subject", "Hello"),
        sender=Address(name="A", addr=kw.pop("addr", "a@example.com")),
        date=kw.pop("date", datetime(1997, 6, 2, 12, tzinfo=timezone.utc)),
        body_text=kw.pop("body", "body"),
    )
    for k, v in kw.items():
        setattr(msg, k, v)
    return msg


def test_uid_follows_message_id():
    a = make(message_id="abc@example.com", subject="One")
    b = make(message_id="abc@example.com", subject="Two, edited by a mail gateway")
    assert compute_uid(a) == compute_uid(b)


def test_uid_synthesised_when_no_message_id():
    a = make(subject="Same")
    b = make(subject="Same")
    c = make(subject="Different")
    assert compute_uid(a) == compute_uid(b)
    assert compute_uid(a) != compute_uid(c)


def test_uid_is_stable_across_runs():
    # Guards the incremental build: a rebuild must not re-add every message.
    msg = make(message_id="stable@example.com")
    assert compute_uid(msg) == compute_uid(msg)
    assert len(compute_uid(msg)) == 20


def test_normalize_subject_strips_stacked_prefixes():
    assert normalize_subject("Re: Fwd: RE: Budget") == "Budget"
    assert normalize_subject("Re[2]: Budget") == "Budget"
    assert normalize_subject("Regarding the budget") == "Regarding the budget"


def test_normalize_folder_maps_outlook_names():
    assert normalize_folder("Deleted Items") == "Trash"
    assert normalize_folder("Sent Items") == "Sent"
    assert normalize_folder("sent items/2004") == "Sent/2004"
    assert normalize_folder("") == "Inbox"
    assert normalize_folder("Projects") == "Projects"


def test_folder_sort_puts_communicator_folders_first():
    paths = ["Zebra", "Trash", "Inbox", "Drafts", "Alpha"]
    assert sorted(paths, key=folder_sort_key) == ["Inbox", "Drafts", "Trash", "Alpha", "Zebra"]


def test_clean_message_id_and_references():
    assert clean_message_id("  <abc@d.e> ") == "abc@d.e"
    assert parse_references("<a@x> <b@x>") == ["a@x", "b@x"]
    assert parse_references("") == []


def test_tokenize_emits_whole_and_split_tokens():
    toks = list(tokenize("whiteboard.gif"))
    assert "whiteboard.gif" in toks
    assert "whiteboard" in toks and "gif" in toks


def test_tokenize_folds_accents_and_drops_single_chars():
    toks = list(tokenize("Café à Zürich"))
    assert "cafe" in toks and "zurich" in toks
    assert "a" not in toks


def test_thread_key_prefers_references():
    msg = make(references=["root@x"], in_reply_to="parent@x", message_id="me@x")
    assert msg.thread_key() == "root@x"
    msg2 = make(in_reply_to="parent@x", message_id="me@x")
    assert msg2.thread_key() == "parent@x"
    msg3 = make(message_id="me@x")
    assert msg3.thread_key() == "me@x"
