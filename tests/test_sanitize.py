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
