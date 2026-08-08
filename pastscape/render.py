"""Turn parsed messages into the static site.

Output layout::

    index.html                 the Messenger application shell
    manifest.json              incremental-build state
    assets/                    css, js, icon sprite
    data/folders.json          folder tree + counts
    data/msgs/<slug>.json      one compact row tuple per message
    data/search/*.json         sharded inverted index + doc locations
    msg/<ab>/<uid>.html        one page per message, in Navigator chrome
    attachments/<ab>/<uid>/…   extracted attachment blobs

The per-message pages are the single source of truth for message content: the
JS client fetches them and lifts out ``#ps-article`` rather than keeping a
parallel copy of every body in JSON.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote, urlencode

from . import __version__
from .model import Address, Message, folder_sort_key
from .sanitize import sanitize_html, strip_tags, text_to_html

log = logging.getLogger("pastscape.render")

ASSETS = Path(__file__).parent / "assets"

FLAG_ATTACH = 1
FLAG_FLAGGED = 2
FLAG_UNREAD = 4

MAX_QUOTE_CHARS = 1400


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    slug = re.sub(r"[^A-Za-z0-9]+", "-", norm).strip("-").lower()
    return slug or "folder"


def e(text) -> str:
    return escape(str(text if text is not None else ""), quote=True)


def read_asset(name: str) -> str:
    return (ASSETS / name).read_text("utf-8")


def _addr_link(addr: Address) -> str:
    if addr.addr:
        label = addr.full() if addr.name else addr.addr
        return f'<a href="mailto:{e(addr.addr)}">{e(label)}</a>'
    return e(addr.name or "(unknown)")


def _addr_list(addrs: list[Address]) -> str:
    return ", ".join(_addr_link(a) for a in addrs) or "(undisclosed recipients)"


def _fmt_date_long(msg: Message) -> str:
    if msg.date:
        return msg.date.strftime("%a, %d %b %Y %H:%M:%S %z").strip()
    return msg.date_raw or "(no date)"


def _fmt_size(n: int) -> str:
    if not n:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1048576:.1f} MB"


# ---------------------------------------------------------------------------
# chrome fragments
# ---------------------------------------------------------------------------


def _titlebar(title: str, icon_id: str = "ic-logo") -> str:
    return f"""<div class="ps-titlebar">
      <svg class="ps-title-icon"><use href="#{icon_id}"></use></svg>
      <span class="ps-title-text" id="ps-title-text">{e(title)}</span>
      <span class="ps-title-buttons">
        <button class="ps-title-btn" type="button" title="Minimize">_</button>
        <button class="ps-title-btn" type="button" title="Maximize">□</button>
        <button class="ps-title-btn ps-close" type="button" title="Close">×</button>
      </span>
    </div>"""


def _menu(label: str, accel_index: int, items: list) -> str:
    """items: list of (label, shortcut, action, extra_class) or None for a separator."""
    lab = e(label)
    if 0 <= accel_index < len(label):
        lab = e(label[:accel_index]) + "<u>" + e(label[accel_index]) + "</u>" + e(label[accel_index + 1:])
    rows = []
    for item in items:
        if item is None:
            rows.append('<div class="ps-menu-sep"></div>')
            continue
        text, key, action, cls = item
        rows.append(
            f'<div class="ps-menu-item {cls}" data-action="{e(action)}">'
            f"<span>{e(text)}</span><span class=\"key\">{e(key)}</span></div>"
        )
    return (
        f'<div class="ps-menu"><span>{lab}</span>'
        f'<div class="ps-menu-panel">{"".join(rows)}</div></div>'
    )


MESSENGER_MENUS = [
    ("File", 0, [
        ("New Message", "Ctrl+M", "noop", "disabled"),
        ("Open Message", "Enter", "open", ""),
        None,
        ("Print…", "Ctrl+P", "print", ""),
        None,
        ("Close", "Ctrl+W", "noop", "disabled"),
    ]),
    ("Edit", 0, [
        ("Copy", "Ctrl+C", "noop", "disabled"),
        None,
        ("Search Messages…", "Ctrl+F", "search", ""),
        ("Select All", "Ctrl+A", "noop", "disabled"),
    ]),
    ("View", 0, [
        ("Sort by Date", "", "sortdate", ""),
        ("Sort by Sender", "", "sortsender", ""),
        ("Sort by Subject", "", "sortsubject", ""),
        None,
        ("Threaded", "", "threaded", ""),
        ("Unread Only", "", "unreadonly", ""),
        None,
        ("Page Source", "Ctrl+U", "noop", "disabled"),
    ]),
    ("Go", 0, [
        ("Next Message", "F", "next", ""),
        ("Next Unread Message", "N", "nextunread", ""),
    ]),
    ("Message", 0, [
        ("Reply to Sender", "R", "reply", ""),
        ("Reply to All", "Shift+R", "replyall", ""),
        ("Forward", "", "forward", ""),
        None,
        ("Mark Folder Read", "", "markread", ""),
        ("Clear All Read Marks", "", "markunread", ""),
        None,
        ("Delete Message", "Del", "delete", ""),
    ]),
    ("Communicator", 0, [
        ("Messenger Mailbox", "Ctrl+2", "noop", "disabled"),
        ("Search Messages…", "Ctrl+F", "search", ""),
    ]),
    ("Help", 0, [
        ("About Pastscape", "", "about", ""),
    ]),
]

MESSENGER_TOOLBAR = [
    ("btn-getmsg", "ic-getmsg", "Get Msg", "getmsg", False),
    ("btn-newmsg", "ic-newmsg", "New Msg", "noop", True),
    ("btn-reply", "ic-reply", "Reply", "reply", False),
    ("btn-replyall", "ic-replyall", "Reply All", "replyall", False),
    ("btn-forward", "ic-forward", "Forward", "forward", False),
    ("btn-file", "ic-file", "File", "file", False),
    ("btn-next", "ic-next", "Next", "nextunread", False),
    ("btn-print", "ic-print", "Print", "print", False),
    ("btn-search", "ic-search", "Search", "search", False),
    ("btn-delete", "ic-delete", "Delete", "delete", False),
    ("btn-stop", "ic-stop", "Stop", "noop", True),
]

NAVIGATOR_TOOLBAR = [
    ("ic-back", "Back", "history.back(); return false;"),
    ("ic-fwd", "Forward", "history.forward(); return false;"),
    ("ic-reload", "Reload", "location.reload(); return false;"),
    ("ic-home", "Home", None),
    ("ic-search", "Search", None),
    ("ic-guide", "Pastscape", None),
    ("ic-print", "Print", "window.print(); return false;"),
    ("ic-security", "Security", None),
    ("ic-shop", "Shop", None),
    ("ic-stop", "Stop", "return false;"),
]


def _menubar(menus) -> str:
    return '<div class="ps-menubar">' + "".join(_menu(*m) for m in menus) + "</div>"


def _messenger_toolbar() -> str:
    buttons = []
    for btn_id, icon_id, label, action, disabled in MESSENGER_TOOLBAR:
        dis = " disabled" if disabled else ""
        buttons.append(
            f'<button class="ps-tbtn" id="{btn_id}" type="button" data-action="{action}"{dis}>'
            f'<svg><use href="#{icon_id}"></use></svg><span class="lbl">{e(label)}</span></button>'
        )
    return (
        '<div class="ps-toolbar"><span class="ps-grabber"></span>'
        + "".join(buttons)
        + '<span class="ps-toolbar-spacer"></span>'
        '<svg class="ps-logo"><use href="#ic-logo"></use></svg>'
        "</div>"
    )


def _navigator_toolbar(home_href: str) -> str:
    buttons = []
    for icon_id, label, onclick in NAVIGATOR_TOOLBAR:
        if label == "Home":
            buttons.append(
                f'<a class="ps-tbtn" href="{e(home_href)}">'
                f'<svg><use href="#{icon_id}"></use></svg><span class="lbl">{e(label)}</span></a>'
            )
            continue
        if label == "Search":
            buttons.append(
                f'<a class="ps-tbtn" href="{e(home_href)}#q=">'
                f'<svg><use href="#{icon_id}"></use></svg><span class="lbl">{e(label)}</span></a>'
            )
            continue
        handler = f' onclick="{onclick}"' if onclick else ""
        buttons.append(
            f'<button class="ps-tbtn" type="button"{handler}>'
            f'<svg><use href="#{icon_id}"></use></svg><span class="lbl">{e(label)}</span></button>'
        )
    return (
        '<div class="ps-toolbar"><span class="ps-grabber"></span>'
        + "".join(buttons)
        + '<span class="ps-toolbar-spacer"></span>'
        '<svg class="ps-logo"><use href="#ic-logo"></use></svg>'
        "</div>"
    )


def _statusbar(main_html: str, extra: str = "") -> str:
    return f"""<div class="ps-statusbar">
      <span class="ps-status-cell ps-status-main" id="ps-status-text">{main_html}</span>
      <span class="ps-progress"><i id="ps-progress-bar"></i></span>
      {extra}
      <span class="ps-status-cell"><svg><use href="#ic-security"></use></svg></span>
      <span class="ps-status-cell"><svg><use href="#ic-mail-read"></use></svg></span>
    </div>"""


# ---------------------------------------------------------------------------
# message body + page
# ---------------------------------------------------------------------------


def _render_body(msg: Message, cid_map: dict[str, str], block_remote: bool) -> tuple[str, int, bool]:
    """Return (html, blocked_count, is_plain)."""
    if msg.body_html.strip():
        html, blocked = sanitize_html(msg.body_html, block_remote=block_remote)
        html = _resolve_cids(html, cid_map)
        return html, blocked, False
    return text_to_html(msg.body_text), 0, True


_RE_CID_SRC = re.compile(r'(data-ps-src|src)="cid:([^"]+)"', re.I)


def _resolve_cids(html: str, cid_map: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        href = cid_map.get(m.group(2).strip().strip("<>"))
        return f'src="{e(href)}"' if href else m.group(0)

    return _RE_CID_SRC.sub(repl, html)


def _reply_mailto(msg: Message, kind: str = "reply") -> str:
    target = (msg.reply_to.addr if msg.reply_to and msg.reply_to.addr else msg.sender.addr)
    if not target and kind != "forward":
        return ""
    subject = msg.subject or ""
    if kind == "forward":
        subject = subject if re.match(r"^\s*fwd?:", subject, re.I) else f"Fwd: {subject}"
    else:
        subject = subject if re.match(r"^\s*re:", subject, re.I) else f"Re: {subject}"

    quote_src = msg.body_text or strip_tags(msg.body_html)
    quoted = "\n".join("> " + line for line in quote_src.splitlines()[:40])
    who = msg.sender.name or msg.sender.addr or "someone"
    body = f"\n\n{who} wrote:\n{quoted}"
    if len(body) > MAX_QUOTE_CHARS:
        body = body[:MAX_QUOTE_CHARS] + "\n[… quoted text truncated …]"

    params = {"subject": subject, "body": body}
    if kind == "replyall":
        cc = [a.addr for a in msg.cc if a.addr]
        others = [a.addr for a in msg.to if a.addr and a.addr != target]
        if others:
            target = ",".join([target] + others) if target else ",".join(others)
        if cc:
            params["cc"] = ",".join(cc)
    if kind != "forward" and msg.message_id:
        params["in-reply-to"] = f"<{msg.message_id}>"

    recipients = quote(target, safe="@,")
    return f"mailto:{recipients}?{urlencode(params, quote_via=quote)}"


def _headers_block(msg: Message, reply_href: str) -> str:
    rows = [("Subject:", e(msg.subject or "(no subject)"))]
    rows.append(("Date:", e(_fmt_date_long(msg))))
    rows.append(("From:", _addr_link(msg.sender)))
    if msg.organization:
        rows.append(("Organization:", e(msg.organization)))
    if msg.to:
        rows.append(("To:", _addr_list(msg.to)))
    if msg.cc:
        rows.append(("CC:", _addr_list(msg.cc)))
    if msg.newsgroups:
        rows.append(("Newsgroups:", e(msg.newsgroups)))
    if msg.priority:
        rows.append(("Priority:", e(msg.priority)))

    badge = ""
    if reply_href:
        badge = (
            f'<div class="ps-msg-toolbar-note"><a class="ps-edit-badge" href="{e(reply_href)}" '
            f'title="Reply to sender"><svg><use href="#ic-newmsg"></use></svg></a></div>'
        )
    cells = "".join(
        f'<div class="h-label">{e(label)}</div><div class="h-value">{value}</div>'
        for label, value in rows
    )
    return f'{badge}<div class="ps-msg-headers">{cells}</div>'


def _attachments_block(msg: Message) -> str:
    if not msg.attachments:
        return ""
    items = []
    for att in msg.attachments:
        size = _fmt_size(att.size)
        link = (
            f'<a href="{e(att.href)}" download="{e(att.filename)}">{e(att.filename)}</a>'
            if att.href else e(att.filename)
        )
        items.append(
            f'<li><svg><use href="#ic-attach"></use></svg>{link}'
            f'<span class="size">{e(size)}</span></li>'
        )
    return (
        '<div class="ps-attachments"><h4>Attachments</h4><ul>'
        + "".join(items)
        + "</ul></div>"
    )


def _source_block(msg: Message) -> str:
    if not msg.headers:
        return ""
    text = "\n".join(f"{k}: {v}" for k, v in msg.headers)
    return (
        '<p style="margin-top:14px"><a href="#" class="ps-source-toggle">Show original headers</a></p>'
        f'<div class="ps-source" style="display:none">{e(text)}</div>'
    )


def build_article(msg: Message, meta: dict, block_remote: bool) -> str:
    cid_map = {a.content_id: a.href for a in msg.attachments if a.content_id and a.href}
    body_html, blocked, is_plain = _render_body(msg, cid_map, block_remote)
    reply_href = _reply_mailto(msg, "reply")

    remote_bar = ""
    if blocked:
        remote_bar = (
            '<div class="ps-remote-bar"><span>This message contains '
            f'{blocked} remote image{"" if blocked == 1 else "s"} that were not downloaded.</span>'
            '<button class="ps-btn" type="button">Show Images</button></div>'
        )

    meta_json = json.dumps(meta, separators=(",", ":"), ensure_ascii=False)
    body_class = "ps-msg-body plain" if is_plain else "ps-msg-body"
    return f"""<article id="ps-article" class="ps-article" data-uid="{e(msg.uid)}">
<script type="application/json" id="ps-meta">{meta_json}</script>
{_headers_block(msg, reply_href)}
{remote_bar}
<div class="{body_class}">{body_html}</div>
{_attachments_block(msg)}
{_source_block(msg)}
</article>"""


def build_message_page(msg: Message, article: str, sprite: str, site_title: str,
                       depth: int, prev_link: str, next_link: str) -> str:
    up = "../" * depth
    home = f"{up}index.html"
    reply = _reply_mailto(msg, "reply")
    reply_all = _reply_mailto(msg, "replyall")
    forward = _reply_mailto(msg, "forward")

    # Shown in the Location bar. The inline script swaps in the real URL once
    # loaded; this is the sensible thing to print if scripting is off.
    loc = f"msg/{msg.uid[:2]}/{msg.uid}.html"
    nav_links = []
    if prev_link:
        nav_links.append(f'<a href="{e(prev_link)}">&lt;&lt; Previous</a>')
    else:
        nav_links.append("<span></span>")
    nav_links.append(f'<a href="{e(home)}#msg={e(msg.uid)}">Open in Messenger</a>')
    if next_link:
        nav_links.append(f'<a href="{e(next_link)}">Next &gt;&gt;</a>')
    else:
        nav_links.append("<span></span>")

    actions = []
    if reply:
        actions.append(f'<a href="{e(reply)}">Reply</a>')
    if reply_all and (msg.to or msg.cc):
        actions.append(f'<a href="{e(reply_all)}">Reply All</a>')
    actions.append(f'<a href="{e(forward)}">Forward</a>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(msg.subject or "(no subject)")} - {e(site_title)}</title>
<meta name="generator" content="Pastscape {e(__version__)}">
<meta name="pastscape:uid" content="{e(msg.uid)}">
<meta name="pastscape:folder" content="{e(msg.folder)}">
<link rel="stylesheet" href="{up}assets/pastscape.css">
</head>
<body>
{sprite}
<div class="ps-window">
{_titlebar((msg.subject or "(no subject)") + " - Pastscape", "ic-guide")}
{_menubar([
    ("File", 0, [("Open in Messenger", "", "noop", ""), None, ("Print…", "Ctrl+P", "noop", "")]),
    ("Edit", 0, [("Copy", "Ctrl+C", "noop", "disabled")]),
    ("View", 0, [("Page Source", "Ctrl+U", "noop", "disabled")]),
    ("Go", 0, [("Back", "Alt+Left", "noop", "")]),
    ("Communicator", 0, [("Messenger Mailbox", "Ctrl+2", "noop", "")]),
    ("Help", 0, [("About Pastscape", "", "noop", "")]),
])}
{_navigator_toolbar(home)}
<div class="ps-locbar">
  <span class="ps-chip"><svg><use href="#ic-bookmark"></use></svg>Bookmarks</span>
  <span class="ps-chip"><svg><use href="#ic-folder"></use></svg>Location:</span>
  <span class="ps-loc" id="ps-loc">{e(loc)}</span>
  <span class="ps-chip"><svg><use href="#ic-guide"></use></svg>What's Related</span>
</div>
<div class="ps-page-scroll">
  <div class="ps-page-inner">
    <div class="ps-breadcrumb">
      <a href="{e(home)}">{e(site_title)}</a> &rsaquo;
      <a href="{e(home)}#folder={e(slugify(msg.folder))}">{e(msg.folder)}</a>
      &nbsp;&nbsp; {" &middot; ".join(actions)}
    </div>
    {article}
    <div class="ps-page-nav">{"".join(nav_links)}</div>
  </div>
</div>
{_statusbar(e(msg.folder) + " &mdash; " + e(_fmt_date_long(msg)))}
</div>
<script>
(function () {{
  var loc = document.getElementById("ps-loc");
  if (loc) loc.textContent = location.href;
}})();
document.addEventListener("click", function (ev) {{
  var t = ev.target.closest(".ps-source-toggle");
  if (t) {{
    ev.preventDefault();
    var box = document.querySelector(".ps-source");
    if (box) box.style.display = box.style.display === "none" ? "block" : "none";
  }}
  var show = ev.target.closest(".ps-remote-bar button");
  if (show) {{
    document.querySelectorAll("img[data-ps-src]").forEach(function (img) {{
      img.src = img.getAttribute("data-ps-src");
      img.removeAttribute("data-ps-src");
      img.classList.remove("ps-blocked-img");
    }});
    show.closest(".ps-remote-bar").remove();
  }}
  var menu = ev.target.closest(".ps-menu");
  document.querySelectorAll(".ps-menu").forEach(function (m) {{ if (m !== menu) m.classList.remove("open"); }});
  if (menu) menu.classList.toggle("open");
}});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# application shell
# ---------------------------------------------------------------------------


def build_index_page(site_title: str, sprite: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(site_title)} - Pastscape Messenger</title>
<meta name="generator" content="Pastscape {e(__version__)}">
<link rel="stylesheet" href="assets/pastscape.css">
</head>
<body>
{sprite}
<div class="ps-window">
{_titlebar(e(site_title) + " - Pastscape Messenger")}
{_menubar(MESSENGER_MENUS)}
{_messenger_toolbar()}
<div class="ps-body">
  <div class="ps-pane-tree" id="ps-pane-tree">
    <div class="ps-colhead"><div class="col" style="flex:1 1 auto">Name</div></div>
    <div class="ps-scroll"><div class="ps-tree" id="ps-tree"></div></div>
  </div>
  <div class="ps-splitter-v" id="ps-split-v"></div>
  <div class="ps-right">
    <div class="ps-pane-list" id="ps-pane-list">
      <div class="ps-colhead" id="ps-list-head">
        <div class="col" data-col="subject" style="flex:0 0 300px">
          <svg><use href="#ic-mail-read"></use></svg><span>Subject</span>
          <span class="sortmark"></span><span class="grip"></span>
        </div>
        <div class="col" data-col="flag" style="flex:0 0 20px">
          <svg><use href="#ic-flag"></use></svg>
        </div>
        <div class="col" data-col="sender" style="flex:0 0 170px">
          <span>Sender</span><span class="sortmark"></span><span class="grip"></span>
        </div>
        <div class="col" data-col="date" style="flex:0 0 120px">
          <span>Date</span><span class="sortmark"></span><span class="grip"></span>
        </div>
        <div class="col" data-col="priority" style="flex:1 1 90px">
          <span>Priority</span><span class="sortmark"></span>
        </div>
      </div>
      <div class="ps-scroll" id="ps-list-scroll"><div class="ps-list" id="ps-list"></div></div>
    </div>
    <div class="ps-splitter-h" id="ps-split-h"></div>
    <div class="ps-pane-msg" id="ps-pane-msg">
      <div class="ps-scroll ps-msgpane" id="ps-msgpane">
        <div class="ps-nomsg">Loading archive…</div>
      </div>
    </div>
  </div>
</div>
{_statusbar("Starting Pastscape…",
            '<span class="ps-status-cell" id="ps-folder-title"></span>'
            '<span class="ps-status-cell" id="ps-total"></span>'
            '<span class="ps-status-cell" id="ps-built"></span>')}
</div>

<div class="ps-modal-back" id="ps-search-back">
  <div class="ps-dialog">
    {_titlebar("Search Messages", "ic-search")}
    <div class="ps-dialog-body">
      <div class="ps-field">
        <label for="ps-search-input">Search for:</label>
        <input type="search" id="ps-search-input" style="flex:1 1 auto" placeholder="words in subject, sender or body" autocomplete="off">
        <label for="ps-search-scope">in</label>
        <select id="ps-search-scope"><option value="*">All folders</option></select>
        <button class="ps-btn default" id="ps-search-go" type="button">Search</button>
      </div>
      <div class="ps-hint" id="ps-search-note">
        The index is fetched in shards as you type — only the pieces your words touch are downloaded.
      </div>
      <div class="ps-results" id="ps-results"></div>
      <div class="ps-field" style="justify-content:flex-end">
        <button class="ps-btn" id="ps-search-open" type="button">Go to Message</button>
        <button class="ps-btn" id="ps-search-close" type="button">Close</button>
      </div>
    </div>
  </div>
</div>

<div class="ps-modal-back" id="ps-about-back">
  <div class="ps-dialog" style="width:400px">
    {_titlebar("About Pastscape", "ic-logo")}
    <div class="ps-dialog-body">
      <div style="display:flex; gap:12px; align-items:flex-start">
        <svg style="width:48px;height:48px;flex:none"><use href="#ic-logo"></use></svg>
        <div>
          <p style="margin:0 0 6px"><b>Pastscape Messenger {e(__version__)}</b></p>
          <p style="margin:0 0 6px">A static mail archive that remembers what mail clients
          looked like before the web ate them.</p>
          <p style="margin:0">Search runs entirely in your browser. Reply opens your own mail
          client via <tt>mailto:</tt> — nothing is sent from this page.</p>
        </div>
      </div>
      <div class="ps-field" style="justify-content:flex-end">
        <button class="ps-btn default" id="ps-about-ok" type="button">OK</button>
      </div>
    </div>
  </div>
</div>

<noscript>
  <div class="ps-noscript">
    Scripting is off, so the Messenger interface cannot run. Every message is still
    readable as its own page under <tt>msg/</tt>.
  </div>
</noscript>
<script src="assets/pastscape.js"></script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# the build
# ---------------------------------------------------------------------------


class SiteBuilder:
    def __init__(self, site_dir: Path, title: str = "Local Mail",
                 block_remote: bool = True, news_host: str = ""):
        self.site = site_dir
        self.title = title
        self.block_remote = block_remote
        self.news_host = news_host
        self.sprite = read_asset("icons.svg")

    # -- assets ----------------------------------------------------------
    def write_assets(self) -> None:
        dest = self.site / "assets"
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("pastscape.css", "pastscape.js", "icons.svg"):
            shutil.copyfile(ASSETS / name, dest / name)

    # -- attachments -----------------------------------------------------
    def write_attachments(self, msg: Message) -> list[str]:
        if not msg.attachments:
            return []
        base = self.site / "attachments" / msg.uid[:2] / msg.uid
        base.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        seen: set[str] = set()
        for att in msg.attachments:
            name = att.filename
            stem, dot, ext = name.rpartition(".")
            n = 1
            while name.lower() in seen:
                n += 1
                name = f"{stem}-{n}{dot}{ext}" if dot else f"{name}-{n}"
            seen.add(name.lower())
            path = base / name
            try:
                path.write_bytes(att.payload)
            except OSError as exc:
                log.warning("could not write attachment %s: %s", path, exc)
                continue
            rel = f"attachments/{msg.uid[:2]}/{msg.uid}/{quote(name)}"
            att.href = "../../" + rel  # message pages live two levels down
            written.append(rel)
        return written

    # -- message page ----------------------------------------------------
    def message_meta(self, msg: Message, hash_: str, first_seen: str) -> dict:
        quote_src = msg.body_text or strip_tags(msg.body_html)
        return {
            "uid": msg.uid,
            "folder": msg.folder,
            "subject": msg.subject,
            "from": msg.sender.as_json(),
            "replyTo": msg.reply_to.as_json() if msg.reply_to else None,
            "to": [a.as_json() for a in msg.to],
            "cc": [a.as_json() for a in msg.cc],
            "date": int(msg.date.timestamp()) if msg.date else 0,
            "dateRaw": msg.date_raw,
            "messageId": msg.message_id,
            "inReplyTo": msg.in_reply_to,
            "references": msg.references,
            "priority": msg.priority,
            "size": msg.size,
            "attachments": [a.as_json() for a in msg.attachments],
            "quote": quote_src[:MAX_QUOTE_CHARS],
            "hash": hash_,
            "first_seen": first_seen,
            "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def write_message(self, msg: Message, meta: dict,
                      prev_uid: str = "", next_uid: str = "") -> str:
        article = build_article(msg, meta, self.block_remote)

        def sibling(uid: str) -> str:
            if not uid:
                return ""
            return f"../../msg/{uid[:2]}/{uid}.html"

        page = build_message_page(
            msg, article, self.sprite, self.title,
            depth=2, prev_link=sibling(prev_uid), next_link=sibling(next_uid),
        )
        rel = f"msg/{msg.uid[:2]}/{msg.uid}.html"
        out = self.site / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, "utf-8")
        return rel

    # -- data files ------------------------------------------------------
    def write_folder_data(self, folders: dict[str, list[Message]]) -> tuple[list[dict], list[list]]:
        data_dir = self.site / "data" / "msgs"
        data_dir.mkdir(parents=True, exist_ok=True)
        for stale in data_dir.glob("*.json"):
            stale.unlink()

        folder_meta: list[dict] = []
        doc_locations: list[list] = []
        ordered_messages: list[Message] = []

        used_slugs: set[str] = set()
        for path in sorted(folders.keys(), key=folder_sort_key):
            msgs = sorted(folders[path], key=lambda m: m.sort_key(), reverse=True)
            slug = slugify(path)
            base = slug
            n = 1
            while slug in used_slugs:
                n += 1
                slug = f"{base}-{n}"
            used_slugs.add(slug)

            rows = []
            for idx, msg in enumerate(msgs):
                flags = 0
                if msg.has_attachments:
                    flags |= FLAG_ATTACH
                if msg.flagged:
                    flags |= FLAG_FLAGGED
                if msg.unread:
                    flags |= FLAG_UNREAD
                rows.append([
                    msg.uid,
                    msg.subject,
                    msg.sender.name,
                    msg.sender.addr,
                    int(msg.date.timestamp()) if msg.date else 0,
                    msg.priority,
                    flags,
                    msg.thread_key(),
                    msg.size,
                ])
                doc_locations.append([slug, idx])
                ordered_messages.append(msg)

            (data_dir / f"{slug}.json").write_text(
                json.dumps({"slug": slug, "path": path, "rows": rows},
                           separators=(",", ":"), ensure_ascii=False),
                "utf-8",
            )
            parent = "/".join(path.split("/")[:-1])
            folder_meta.append({
                "slug": slug,
                "path": path,
                "name": path.split("/")[-1],
                "depth": path.count("/"),
                "parent": parent or None,
                "count": len(msgs),
                "unread": sum(1 for m in msgs if m.unread),
                "file": f"data/msgs/{slug}.json",
            })

        self._ordered_messages = ordered_messages
        return folder_meta, doc_locations

    def write_folders_json(self, folder_meta: list[dict], total: int, built: str) -> None:
        payload = {
            "title": self.title,
            "generator": f"pastscape {__version__}",
            "built": built,
            "totalMessages": total,
            "newsHost": self.news_host,
            "folders": folder_meta,
        }
        (self.site / "data").mkdir(parents=True, exist_ok=True)
        (self.site / "data" / "folders.json").write_text(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False), "utf-8"
        )

    def write_index(self) -> None:
        (self.site / "index.html").write_text(
            build_index_page(self.title, self.sprite), "utf-8"
        )
