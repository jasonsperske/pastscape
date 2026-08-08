from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

from pastscape.model import Address, Message
from pastscape.render import _reply_mailto, slugify


def sample() -> Message:
    return Message(
        uid="a" * 20,
        folder="Inbox",
        subject="Demo server load before Thursday",
        sender=Address(name="Terry Nakamura", addr="terry@example.com"),
        to=[Address(name="You", addr="you@example.com"),
            Address(name="Ops", addr="ops@example.com")],
        cc=[Address(name="Dana", addr="dana@example.com")],
        date=datetime(1997, 6, 3, 14, tzinfo=timezone.utc),
        message_id="perf-thread-1@example.com",
        body_text="We are seeing about 40 requests per second.\nDo we move the assets?",
    )


def parts(url: str):
    parsed = urlparse(url)
    return unquote(parsed.path), parse_qs(parsed.query)


def test_reply_goes_to_the_sender_only():
    to, q = parts(_reply_mailto(sample(), "reply"))
    assert to == "terry@example.com"
    assert q["subject"] == ["Re: Demo server load before Thursday"]
    assert q["in-reply-to"] == ["<perf-thread-1@example.com>"]
    assert q["body"][0].startswith("\n\nTerry Nakamura wrote:\n> We are seeing")


def test_reply_prefers_reply_to_header():
    msg = sample()
    msg.reply_to = Address(name="List", addr="list@example.com")
    to, _ = parts(_reply_mailto(msg, "reply"))
    assert to == "list@example.com"


def test_reply_all_includes_recipients_and_cc():
    to, q = parts(_reply_mailto(sample(), "replyall"))
    assert set(to.split(",")) == {"terry@example.com", "you@example.com", "ops@example.com"}
    assert q["cc"] == ["dana@example.com"]


def test_subject_is_not_double_prefixed():
    msg = sample()
    msg.subject = "Re: Already a reply"
    _, q = parts(_reply_mailto(msg, "reply"))
    assert q["subject"] == ["Re: Already a reply"]


def test_forward_has_no_recipient_but_quotes_the_original():
    url = _reply_mailto(sample(), "forward")
    to, q = parts(url)
    assert q["subject"] == ["Fwd: Demo server load before Thursday"]
    assert "in-reply-to" not in q


def test_reply_is_empty_when_there_is_no_address():
    msg = sample()
    msg.sender = Address(name="Nobody", addr="")
    assert _reply_mailto(msg, "reply") == ""


def test_quoted_body_is_capped():
    msg = sample()
    msg.body_text = "x" * 50_000
    _, q = parts(_reply_mailto(msg, "reply"))
    assert len(q["body"][0]) < 1600
    assert q["body"][0].endswith("truncated …]")


def test_mailto_survives_special_characters_in_the_subject():
    msg = sample()
    msg.subject = "Budget & forecast: Q3 (draft) 100% #1"
    _, q = parts(_reply_mailto(msg, "reply"))
    assert q["subject"] == ["Re: Budget & forecast: Q3 (draft) 100% #1"]


def test_slugify_is_url_safe_and_stable():
    assert slugify("Inbox/Projects") == "inbox-projects"
    assert slugify("Café & Co.") == "cafe-co"
    assert slugify("") == "folder"
    assert slugify("!!!") == "folder"
