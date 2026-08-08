from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

from pastscape.model import Address, Message
from pastscape.render import (
    MESSENGER_TOOLBAR,
    _reply_mailto,
    build_index_page,
    read_asset,
    slugify,
)


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


# ------------------------------------------------------------------- toolbar

def toolbar_ids():
    return [btn[0] for btn in MESSENGER_TOOLBAR]


def test_get_msg_is_not_in_the_toolbar():
    # There is no server to poll; the button would be a lie.
    assert "btn-getmsg" not in toolbar_ids()
    assert not any(b[2] == "Get Msg" for b in MESSENGER_TOOLBAR)

    page = build_index_page("Local Mail", read_asset("icons.svg"))
    assert 'id="btn-getmsg"' not in page
    assert ">Get Msg<" not in page


def test_read_only_buttons_are_disabled():
    disabled = {b[0] for b in MESSENGER_TOOLBAR if b[4]}
    assert {"btn-delete", "btn-newmsg", "btn-stop"} <= disabled

    page = build_index_page("Local Mail", read_asset("icons.svg"))
    delete_btn = page.split('id="btn-delete"')[1].split(">")[0]
    assert "disabled" in delete_btn


def test_actionable_buttons_are_not_disabled():
    enabled = {b[0] for b in MESSENGER_TOOLBAR if not b[4]}
    assert {"btn-reply", "btn-replyall", "btn-forward", "btn-search"} <= enabled


def test_delete_menu_item_is_greyed_out_too():
    page = build_index_page("Local Mail", read_asset("icons.svg"))
    item = page.split("Delete Message")[0].split('<div class="ps-menu-item')[-1]
    assert "disabled" in item


def test_every_toolbar_icon_exists_in_the_sprite():
    sprite = read_asset("icons.svg")
    for _, icon_id, label, _, _ in MESSENGER_TOOLBAR:
        assert f'id="{icon_id}"' in sprite, f"{label} references missing {icon_id}"
