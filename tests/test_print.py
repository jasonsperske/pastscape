"""The print stylesheet.

These cannot check what a printer does, but they can check the two ways this
breaks silently: a chrome element that never got a hide rule, and a hide rule
pointing at a class name that no longer exists in the generated HTML.
"""

import re

import pytest

from pastscape.render import build_index_page, build_message_page, read_asset

CSS = read_asset("pastscape.css")

# Everything that is emulated application furniture rather than the message.
CHROME_SELECTORS = [
    ".ps-titlebar",      # window caption
    ".ps-menubar",       # File Edit View Go ...
    ".ps-toolbar",       # Reply / Forward / Print buttons
    ".ps-locbar",        # Navigator location bar (message pages)
    ".ps-pane-tree",     # folder tree
    ".ps-pane-list",     # message list
    ".ps-splitter-v",
    ".ps-splitter-h",
    ".ps-statusbar",
    ".ps-modal-back",    # search / about dialogs
    ".ps-breadcrumb",
    ".ps-page-nav",      # << Previous / Next >>
    ".ps-msg-toolbar-note",  # the reply badge
    ".ps-remote-bar",    # "Show Images"
    ".ps-source-toggle",
]

# Containers whose screen layout would otherwise clip a long message to one
# pane-height of paper.
UNPINNED_SELECTORS = [".ps-window", ".ps-body", ".ps-right", ".ps-pane-msg", ".ps-scroll"]


def print_block() -> str:
    start = CSS.index("@media print")
    depth, i = 0, CSS.index("{", start)
    for j in range(i, len(CSS)):
        if CSS[j] == "{":
            depth += 1
        elif CSS[j] == "}":
            depth -= 1
            if depth == 0:
                return CSS[i + 1:j]
    raise AssertionError("unbalanced @media print block")


BLOCK = print_block()


def test_a_print_stylesheet_exists():
    assert "@media print" in CSS
    assert len(BLOCK) > 500


@pytest.mark.parametrize("selector", CHROME_SELECTORS)
def test_chrome_is_hidden_on_paper(selector):
    hide_rules = re.findall(r"([^{}]+)\{[^{}]*display:\s*none[^{}]*\}", BLOCK)
    hidden = " ".join(hide_rules)
    assert selector in hidden, f"{selector} would still print"


@pytest.mark.parametrize("selector", UNPINNED_SELECTORS)
def test_scrolling_containers_are_unpinned(selector):
    assert selector in BLOCK, f"{selector} keeps its screen layout when printed"


def test_the_message_itself_is_never_hidden():
    hide_rules = re.findall(r"([^{}]+)\{[^{}]*display:\s*none[^{}]*\}", BLOCK)
    hidden = " ".join(hide_rules)
    for keep in ("#ps-article", ".ps-msg-headers", ".ps-msg-body", ".ps-attachments"):
        assert keep not in hidden, f"{keep} must survive printing"


def test_overflow_hidden_is_lifted_from_the_page():
    # html/body carry overflow:hidden on screen; a printed page must scroll off
    # the bottom onto page two instead.
    assert re.search(r"html\s*,\s*body\s*\{[^{}]*overflow:\s*visible", BLOCK)
    assert re.search(r"html\s*,\s*body\s*\{[^{}]*height:\s*auto", BLOCK)


def _rendered_pages():
    sprite = read_asset("icons.svg")
    index = build_index_page("Local Mail", sprite)

    from datetime import datetime, timezone
    from pastscape.model import Address, Message
    from pastscape.render import build_article

    msg = Message(
        uid="b" * 20, folder="Inbox", subject="Printable",
        sender=Address(name="A Sender", addr="a@example.com"),
        to=[Address(name="You", addr="you@example.com")],
        date=datetime(2008, 4, 19, 17, 34, tzinfo=timezone.utc),
        body_text="Body text.\n", message_id="p@example.com",
    )
    article = build_article(msg, {"uid": msg.uid}, block_remote=True)
    page = build_message_page(msg, article, sprite, "Local Mail", 2, "", "")
    return index, page


@pytest.mark.parametrize("selector", CHROME_SELECTORS)
def test_hidden_selectors_still_exist_in_the_generated_html(selector):
    """A rename in render.py must not leave the print CSS aiming at nothing."""
    index, page = _rendered_pages()
    cls = selector.lstrip(".")
    assert cls in index or cls in page, f"{selector} matches nothing that is rendered"


def test_printed_link_targets_are_scoped_to_the_body():
    # Header addresses already read as text; annotating them would duplicate.
    rule = re.search(r"([^{}]*a\[href\^=\"http\"\]::after)\s*\{", BLOCK)
    assert rule, "external links should print their URL"
    assert ".ps-msg-body" in rule.group(1)
