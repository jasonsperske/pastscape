"""Generate a sample .eml corpus so the builder can be exercised end to end.

Usage: python tests/make_sample.py sample-mail/
"""

from __future__ import annotations

import sys
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = datetime(1997, 6, 2, 12, 0, 0, tzinfo=timezone(timedelta(hours=-8)))

WELCOME_BODY = """\
Welcome to Netscape Communicator, the complete suite of components for
communication and collaboration.

Netscape Messenger is the mail client you are looking at right now. It reads
POP3 and IMAP4 mail, and it reads news. The three-pane window shows your
folders on the left, your messages at the top right, and the selected message
below.

Some things to try:

  * Click a column heading to sort the message list.
  * Press N to move to the next unread message.
  * Choose Edit > Search Messages to search everything at once.

Marc
"""

THREAD_ROOT = """\
We are seeing about 40 requests per second on the demo server, which is more
than we budgeted for. Do we want to move the static assets to a second host
before the announcement on Thursday?

-- Terry
"""

THREAD_REPLY_1 = """\
> We are seeing about 40 requests per second on the demo server, which is more
> than we budgeted for. Do we want to move the static assets to a second host
> before the announcement on Thursday?

Yes. I would rather over-provision and look silly than watch it fall over in
front of the press. I can have images.example.com pointed at the spare box by
tomorrow afternoon.

Also: the GIFs are 60% of the bytes. Someone should look at that.
"""

THREAD_REPLY_2 = """\
> Also: the GIFs are 60% of the bytes. Someone should look at that.

I re-encoded the whole set with a 64-colour palette last night. 1.2MB down to
430KB with no visible difference at 800x600. Patch attached.

> I can have images.example.com pointed at the spare box by tomorrow afternoon.

Thank you. I will update the templates once DNS settles.
"""

HTML_BODY = """\
<html><head><style>body{font-family:Arial}</style>
<script>alert('this should never run')</script></head>
<body bgcolor="#FFFFFF">
<h2>Quarterly Newsletter</h2>
<p>Dear subscriber,</p>
<p>Our <b>summer issue</b> is now available. Highlights include:</p>
<ul>
  <li>Frames considered harmful, again</li>
  <li>A field guide to <font color="#008000">animated GIFs</font></li>
  <li>Interview: the person who invented the blink tag apologises</li>
</ul>
<p><img src="http://tracker.example.net/open.gif?id=12345" width="1" height="1" alt="">
<img src="http://images.example.net/header.jpg" width="400" height="80" alt="Header"></p>
<p onclick="steal()">Visit <a href="http://www.example.com/newsletter">our site</a>
or <a href="javascript:void(0)">click here</a>.</p>
<p>To unsubscribe, reply with UNSUBSCRIBE in the subject.</p>
</body></html>
"""


def msg(folder, subject, from_name, from_addr, to, body, offset_hours,
        *, html=None, org=None, in_reply_to=None, references=None,
        attachments=(), priority=None, unread=True, cc=None, newsgroups=None,
        message_id=None):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = f'"{from_name}" <{from_addr}>' if from_name else from_addr
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Date"] = format_datetime(BASE + timedelta(hours=offset_hours))
    m["Message-ID"] = message_id or make_msgid(domain="example.com")
    if org:
        m["Organization"] = org
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = " ".join(references)
    if priority:
        m["X-Priority"] = priority
    if newsgroups:
        m["Newsgroups"] = newsgroups
    m["Status"] = "O" if unread else "RO"

    if html:
        m.set_content(body)
        m.add_alternative(html, subtype="html")
    else:
        m.set_content(body)

    for name, ctype, data in attachments:
        maintype, _, subtype = ctype.partition("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return folder, m


TINY_GIF = bytes.fromhex(
    "47494638396110001000800000ff0000ffffff21f90401000000002c00000000"
    "1000100000021c8c8fa9cbed0fa39cb4da8bb3debcfb0f86e248966a6ea9bee5"
    "0400003b"
)


def build_messages():
    thread_id = "<perf-thread-1@example.com>"
    out = []

    out.append(msg("Inbox", "Welcome to Netscape Communicator", "Marc Andreessen",
                   "info@netscape.example.com", "Netscape Communicator User <you@example.com>",
                   WELCOME_BODY, 0, org="Netscape Communications", unread=False))

    out.append(msg("Inbox", "Introducing Netscape Messenger", "In-Box Direct",
                   "inbox-direct@netscape.example.com", "you@example.com",
                   "Netscape In-Box Direct delivers HTML-formatted newsletters straight to "
                   "your Inbox. This message was delivered automatically.\n", 1))

    out.append(msg("Inbox", "Demo server load before Thursday", "Terry Nakamura",
                   "terry@example.com", "you@example.com", THREAD_ROOT, 26,
                   priority="2", cc="ops@example.com", message_id=thread_id))
    out.append(msg("Inbox", "Re: Demo server load before Thursday", "You",
                   "you@example.com", "Terry Nakamura <terry@example.com>",
                   THREAD_REPLY_1, 28, in_reply_to=thread_id, references=[thread_id],
                   cc="ops@example.com", unread=False))
    out.append(msg("Inbox", "Re: Demo server load before Thursday", "Dana Whitfield",
                   "dana@example.com", "you@example.com", THREAD_REPLY_2, 31,
                   in_reply_to=thread_id, references=[thread_id],
                   attachments=[("palette.patch", "text/x-diff",
                                 b"--- a/build.sh\n+++ b/build.sh\n@@ -1 +1 @@\n"
                                 b"-convert $f out.gif\n+convert -colors 64 $f out.gif\n")]))

    out.append(msg("Inbox", "Quarterly Newsletter - Summer Issue", "Example Newsletter",
                   "news@example.net", "you@example.com",
                   "Our summer issue is now available. Visit http://www.example.com/newsletter\n",
                   50, html=HTML_BODY))

    out.append(msg("Inbox", "Photos from the offsite", "Robin Alvarez",
                   "robin@example.com", "you@example.com",
                   "Two from Friday. The one of the whiteboard is unreadable but I am "
                   "attaching it anyway for the archive.\n", 96,
                   attachments=[("whiteboard.gif", "image/gif", TINY_GIF),
                                ("group-photo.gif", "image/gif", TINY_GIF)]))

    out.append(msg("Sent", "Re: Demo server load before Thursday", "You",
                   "you@example.com", "terry@example.com",
                   "Confirmed, DNS is updated. images.example.com now resolves to the "
                   "spare box.\n", 33, in_reply_to=thread_id, references=[thread_id],
                   unread=False))
    out.append(msg("Sent", "Expenses for June", "You", "you@example.com",
                   "accounts@example.com",
                   "Attached. The taxi receipt is missing; I will bring the paper copy.\n",
                   120, unread=False))

    out.append(msg("Drafts", "(no subject)", "You", "you@example.com", "",
                   "I have been thinking about what you said and\n", 121, unread=False))

    out.append(msg("Trash", "URGENT BUSINESS PROPOSAL", "Mr. E. Nwosu",
                   "prince@offshore.example.org", "you@example.com",
                   "DEAR FRIEND, I AM WRITING TO YOU IN CONFIDENCE REGARDING THE SUM OF\n"
                   "US$14,500,000 CURRENTLY HELD IN A DORMANT ACCOUNT.\n", 60,
                   priority="1"))

    out.append(msg("Inbox/Projects", "Mozilla source release", "Jamie Zawinski",
                   "jwz@example.org", "you@example.com",
                   "The tarball is up. It is 15 megabytes, so grab it overnight.\n"
                   "Read the LICENSE before you redistribute anything.\n", 200,
                   newsgroups="netscape.public.mozilla.general"))

    out.append(msg("Inbox/Projects", "Re: Mozilla source release", "Terry Nakamura",
                   "terry@example.com", "you@example.com",
                   "> The tarball is up. It is 15 megabytes\n\n"
                   "Downloaded overnight. Builds clean on Solaris with gcc 2.7.2.3 after\n"
                   "the usual makefile surgery.\n", 210))

    return out


def main(dest: str) -> None:
    root = Path(dest)
    for folder, m in build_messages():
        d = root / folder
        d.mkdir(parents=True, exist_ok=True)
        subject = (m["Subject"] or "message").replace("/", "-")[:50]
        name = "".join(c if c.isalnum() or c in " -_" else "_" for c in subject).strip()
        path = d / f"{name}.eml"
        n = 1
        while path.exists():
            n += 1
            path = d / f"{name}-{n}.eml"
        path.write_bytes(m.as_bytes())
    print(f"wrote sample mail to {root}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample-mail")
