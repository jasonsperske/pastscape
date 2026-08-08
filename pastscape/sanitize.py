"""Allow-list HTML sanitiser for message bodies.

The archive is static, so a hostile message body is the only script vector in
the whole site. We rewrite bodies at build time rather than trusting the
browser: unknown tags are dropped, every attribute is checked against an
allow-list, and remote resources are parked in ``data-ps-src`` until the reader
clicks "Show images" -- which also happens to match how Communicator behaved.
"""

from __future__ import annotations

import logging
import re
from html import escape, unescape
from html.parser import HTMLParser

log = logging.getLogger("pastscape.sanitize")

ALLOWED_TAGS = {
    "a", "abbr", "address", "b", "big", "blockquote", "br", "caption", "center",
    "cite", "code", "col", "colgroup", "dd", "del", "dfn", "div", "dl", "dt",
    "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins",
    "kbd", "li", "ol", "p", "pre", "q", "s", "samp", "small", "span", "strike",
    "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "tt", "u", "ul", "var", "wbr",
}

VOID_TAGS = {"br", "hr", "img", "col", "wbr"}

# Tags whose *content* must go too, not just the tag.
#
# Everything here must have a closing tag. A void element like <meta> would
# open a suppression that never closes and swallow the rest of the document --
# and real mail is full of <meta> in an unclosed <head>. Void and structural
# tags (html, head, body, meta, link, base, form, input, embed, frame) are left
# out on purpose: they fall through to the default path for unrecognised tags,
# which drops the tag and keeps whatever is inside it.
DROP_CONTENT_TAGS = {"script", "style", "title", "object", "applet", "iframe",
                     "frameset", "svg", "math", "button", "select", "textarea"}

ALLOWED_ATTRS = {
    "*": {"align", "valign", "dir", "lang", "title", "class"},
    "a": {"href", "name", "target", "rel"},
    "img": {"src", "alt", "width", "height", "border", "hspace", "vspace"},
    "font": {"color", "face", "size"},
    "table": {"border", "cellpadding", "cellspacing", "width", "bgcolor"},
    "td": {"colspan", "rowspan", "width", "height", "bgcolor", "nowrap"},
    "th": {"colspan", "rowspan", "width", "height", "bgcolor", "nowrap"},
    "tr": {"bgcolor"},
    "col": {"span", "width"},
    "colgroup": {"span", "width"},
    "ol": {"start", "type"},
    "ul": {"type"},
    "li": {"value", "type"},
    "blockquote": {"cite", "type"},
    "hr": {"size", "width", "noshade"},
    "div": {"style"},
    "span": {"style"},
    "p": {"style"},
}

SAFE_URL_SCHEMES = {"http", "https", "mailto", "ftp", "news", "nntp", "tel", "cid"}

# Only a handful of harmless declarations survive from inline style.
_STYLE_PROPS = {
    "color", "background-color", "font-weight", "font-style", "font-size",
    "font-family", "text-align", "text-decoration", "margin", "margin-left",
    "padding", "padding-left", "border-left", "white-space",
}
_RE_STYLE_DECL = re.compile(r"([a-zA-Z\-]+)\s*:\s*([^;]+)")
_RE_URL_IN_CSS = re.compile(r"url\s*\(", re.I)
_RE_SCHEME = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9+.\-]*)\s*:")
_RE_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _safe_url(value: str) -> str | None:
    """Return the URL if its scheme is safe, else None. Relative URLs pass."""
    v = _RE_CTRL.sub("", value or "").strip()
    if not v:
        return None
    m = _RE_SCHEME.match(v)
    if m and m.group(1).lower() not in SAFE_URL_SCHEMES:
        return None
    return v


def _safe_style(value: str) -> str:
    out = []
    for prop, val in _RE_STYLE_DECL.findall(value or ""):
        prop = prop.strip().lower()
        val = val.strip()
        if prop in _STYLE_PROPS and not _RE_URL_IN_CSS.search(val) and "expression" not in val.lower():
            out.append(f"{prop}: {val}")
    return "; ".join(out)


class _Sanitizer(HTMLParser):
    def __init__(self, block_remote: bool = True):
        super().__init__(convert_charrefs=True)
        self.block_remote = block_remote
        self.out: list[str] = []
        self.open_stack: list[str] = []
        self.suppress_depth = 0
        self.blocked_remote = 0

    # -- helpers ---------------------------------------------------------
    def _attrs_for(self, tag: str, attrs) -> str:
        allowed = ALLOWED_ATTRS.get("*", set()) | ALLOWED_ATTRS.get(tag, set())
        pieces = []
        for name, value in attrs:
            name = (name or "").lower()
            if name.startswith("on") or name not in allowed:
                continue
            value = value or ""
            if name in ("href", "src"):
                url = _safe_url(value)
                if url is None:
                    continue
                if name == "src" and self.block_remote and not url.lower().startswith("cid:"):
                    self.blocked_remote += 1
                    pieces.append(f'data-ps-src="{escape(url, quote=True)}"')
                    pieces.append('src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw=="')
                    pieces.append('class="ps-blocked-img"')
                    continue
                value = url
            elif name == "style":
                value = _safe_style(value)
                if not value:
                    continue
            elif name == "target":
                value = "_blank"
            pieces.append(f'{name}="{escape(value, quote=True)}"')
        if tag == "a":
            joined = " ".join(pieces)
            if "href=" in joined and "rel=" not in joined:
                pieces.append('rel="noopener noreferrer nofollow"')
            if "href=" in joined and "target=" not in joined:
                pieces.append('target="_blank"')
        return (" " + " ".join(pieces)) if pieces else ""

    # -- HTMLParser hooks ------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "body":
            # Reaching <body> means anything still suppressed was never closed
            # -- a stray <title> or <style> in a broken header. Recover rather
            # than discarding the message.
            self.suppress_depth = 0
            return
        if tag in DROP_CONTENT_TAGS:
            self.suppress_depth += 1
            return
        if self.suppress_depth or tag not in ALLOWED_TAGS:
            return
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}{self._attrs_for(tag, attrs)}>")
        else:
            self.out.append(f"<{tag}{self._attrs_for(tag, attrs)}>")
            self.open_stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self.suppress_depth or tag in DROP_CONTENT_TAGS or tag not in ALLOWED_TAGS:
            return
        self.out.append(f"<{tag}{self._attrs_for(tag, attrs)}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            self.suppress_depth = max(0, self.suppress_depth - 1)
            return
        if self.suppress_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag in self.open_stack:
            # Close everything opened inside the tag being closed.
            while self.open_stack:
                top = self.open_stack.pop()
                self.out.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data):
        if self.suppress_depth:
            return
        self.out.append(escape(data, quote=False))

    def handle_comment(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def result(self) -> str:
        tail = "".join(f"</{t}>" for t in reversed(self.open_stack))
        return "".join(self.out) + tail


_RE_ANY_TAG = re.compile(r"<[^>]*>")
_RE_SCRIPTISH = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")


def _crude_text(html: str) -> str:
    """Tag-strip by regex, ignoring every parsing subtlety.

    Only used to judge how much text a body *should* have contained, and as the
    last-ditch rendering when the parser lost it.
    """
    text = _RE_SCRIPTISH.sub(" ", html or "")
    text = _RE_ANY_TAG.sub(" ", text)
    text = unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _visible_len(html: str) -> int:
    return len(_RE_ANY_TAG.sub(" ", html or "").strip())


def sanitize_html(html: str, block_remote: bool = True) -> tuple[str, int]:
    """Return (safe_html, remote_resources_blocked)."""
    parser = _Sanitizer(block_remote=block_remote)
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        # A malformed body should degrade to text, never break the build.
        return escape(strip_tags(html or ""), quote=False), 0

    result = parser.result()

    # Backstop. HTMLParser treats <title> and <textarea> as RCDATA, so an
    # unclosed one consumes the rest of the document and we would publish an
    # empty message without noticing. Whenever the sanitised output has lost
    # nearly all of the text the source obviously contained, fall back to a
    # plain-text rendering. Losing the markup beats losing the message.
    crude = _crude_text(html)
    if len(crude) >= 40 and _visible_len(result) < len(crude) // 5:
        log.warning("sanitiser recovered %d chars as plain text (markup was unparseable)",
                    len(crude))
        return "\n".join(
            f'<div class="ps-line">{escape(line, quote=False) or "&nbsp;"}</div>'
            for line in crude.splitlines()
        ), 0

    return result, parser.blocked_remote


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppress = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "body":
            self.suppress = 0          # same recovery as the sanitiser
        elif tag in DROP_CONTENT_TAGS:
            self.suppress += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            self.suppress = max(0, self.suppress - 1)
        elif tag in ("p", "div", "tr", "li", "table", "blockquote"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppress:
            self.parts.append(data)


def strip_tags(html: str) -> str:
    p = _Stripper()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "")
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


_RE_URL_IN_TEXT = re.compile(
    r"""(?ix)
    \b(
        (?:https?://|ftp://|www\.)[^\s<>"'()]+[^\s<>"'()\.,;:!?]
      | [a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}
    )""",
)


def text_to_html(text: str) -> str:
    """Render text/plain the way Communicator did: fixed pitch, linkified,
    with ``>`` quote levels coloured."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        depth = 0
        probe = line
        while probe[:1] == ">" or probe[:2] == " >":
            probe = probe.lstrip(" ")[1:]
            depth += 1
            if depth > 5:
                break
        body = _linkify(escape(line, quote=False))
        if depth:
            out.append(f'<div class="ps-quote ps-quote-{min(depth, 5)}">{body or "&nbsp;"}</div>')
        else:
            out.append(f'<div class="ps-line">{body or "&nbsp;"}</div>')
    return "\n".join(out)


def _linkify(escaped_line: str) -> str:
    def repl(m: re.Match) -> str:
        token = m.group(0)
        if "@" in token and "://" not in token and not token.lower().startswith("www."):
            return f'<a href="mailto:{token}">{token}</a>'
        href = token if "://" in token else f"http://{token}"
        return f'<a href="{href}" target="_blank" rel="noopener noreferrer nofollow">{token}</a>'

    return _RE_URL_IN_TEXT.sub(repl, escaped_line)
