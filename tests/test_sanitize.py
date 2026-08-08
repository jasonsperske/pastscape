from pastscape.sanitize import sanitize_html, strip_tags, text_to_html


def test_script_and_style_content_removed():
    html, _ = sanitize_html("<p>ok</p><script>alert(1)</script><style>b{}</style>")
    assert "alert" not in html
    assert "b{}" not in html
    assert "<p>ok</p>" in html


def test_event_handlers_and_js_urls_dropped():
    html, _ = sanitize_html('<a href="javascript:evil()" onclick="evil()">x</a>')
    assert "javascript:" not in html
    assert "onclick" not in html
    assert ">x</a>" in html


def test_safe_link_gains_rel_and_target():
    html, _ = sanitize_html('<a href="http://example.com">e</a>')
    assert 'rel="noopener noreferrer nofollow"' in html
    assert 'target="_blank"' in html


def test_remote_images_parked_until_asked_for():
    html, blocked = sanitize_html('<img src="http://tracker.example/x.gif">')
    assert blocked == 1
    assert 'data-ps-src="http://tracker.example/x.gif"' in html
    assert "ps-blocked-img" in html


def test_cid_images_are_not_blocked():
    html, blocked = sanitize_html('<img src="cid:part1@local">')
    assert blocked == 0
    assert 'src="cid:part1@local"' in html


def test_allow_remote_when_requested():
    html, blocked = sanitize_html('<img src="http://x/y.gif">', block_remote=False)
    assert blocked == 0
    assert 'src="http://x/y.gif"' in html


def test_style_attribute_filtered_to_safe_properties():
    html, _ = sanitize_html('<div style="color:red; behavior:url(x.htc); position:fixed">t</div>')
    assert "color: red" in html
    assert "behavior" not in html
    assert "position" not in html


def test_unbalanced_tags_are_closed():
    html, _ = sanitize_html("<b><i>text")
    assert html.count("</i>") == 1 and html.count("</b>") == 1


def test_unknown_tags_dropped_but_text_kept():
    html, _ = sanitize_html("<marquee>hello</marquee>")
    assert "marquee" not in html
    assert "hello" in html


def test_strip_tags_produces_readable_text():
    text = strip_tags("<p>one</p><p>two</p><script>x</script>")
    assert "one" in text and "two" in text and "x" not in text


def test_text_to_html_marks_quote_levels_and_links():
    html = text_to_html("> quoted\nplain http://example.com/a\nmail me@example.com")
    assert "ps-quote-1" in html
    assert '<a href="http://example.com/a"' in html
    assert '<a href="mailto:me@example.com">' in html


def test_text_to_html_escapes_markup():
    html = text_to_html("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# A real HTML email, shaped the way mail clients actually emit them: a DOCTYPE,
# an unclosed-in-practice <head>, and void <meta>/<link> tags. Every one of
# these swallowed the entire body before the drop-list was split by whether a
# tag can actually close.
REAL_WORLD_DOC = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2//EN">
<HTML>
<HEAD>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=utf-8">
<META NAME="Generator" CONTENT="Webmail">
<LINK REL="stylesheet" HREF="http://example.com/mail.css">
<TITLE>Should not appear</TITLE>
<STYLE>body { color: red }</STYLE>
</HEAD>
<BODY>
<P>The actual message text.</P>
<P>A second paragraph.</P>
</BODY>
</HTML>"""


def test_meta_tag_does_not_swallow_the_document():
    html, _ = sanitize_html('<meta charset="utf-8"><p>kept</p>')
    assert "kept" in html


def test_full_html_document_keeps_its_body():
    html, _ = sanitize_html(REAL_WORLD_DOC)
    assert "The actual message text." in html
    assert "A second paragraph." in html
    # ...without leaking anything from the head
    assert "Should not appear" not in html
    assert "color: red" not in html
    assert "<meta" not in html.lower()


def test_unclosed_title_still_yields_the_message():
    # HTMLParser treats <title> as RCDATA, so an unclosed one eats the rest of
    # the document and no <body> tag is ever reported. The backstop has to
    # catch this, or the message publishes blank.
    html, _ = sanitize_html(
        "<html><head><title>Subject line that never closes"
        "<body><p>The message body has real content in it that must survive.</p>"
        "<p>And a second paragraph as well.</p></body></html>"
    )
    assert "The message body has real content in it that must survive." in html
    assert "And a second paragraph as well." in html


def test_unclosed_style_does_not_eat_the_body():
    html, _ = sanitize_html(
        "<html><head><style>body { color: red }"
        "<body><p>Body text that has to come through the sanitiser intact.</p>"
    )
    assert "Body text that has to come through the sanitiser intact." in html


def test_backstop_does_not_fire_on_a_body_that_is_legitimately_markup():
    # Mostly-image messages have little text; that is not data loss.
    html, blocked = sanitize_html(
        '<p><img src="http://example.com/a.gif" width="600" height="400"></p>'
    )
    assert blocked == 1
    assert "data-ps-src" in html


def test_link_and_input_do_not_suppress_following_content():
    for void in ('<link rel="stylesheet" href="http://x/y.css">',
                 '<input type="text" name="q">',
                 '<base href="http://x/">'):
        html, _ = sanitize_html(void + "<p>after</p>")
        assert "after" in html, void


def test_form_contents_survive_but_controls_do_not():
    html, _ = sanitize_html("<form><p>please reply</p><button>Send</button></form>")
    assert "please reply" in html
    assert "Send" not in html
    assert "<form" not in html


def test_strip_tags_of_a_full_document_returns_the_body():
    text = strip_tags(REAL_WORLD_DOC)
    assert "The actual message text." in text
    assert "Should not appear" not in text
    assert "color: red" not in text
