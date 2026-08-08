"""Read a PST/OST file.

Two backends, tried in order:

1. ``pypff`` (the ``libpff-python`` wheel) -- read directly, in-process. This is
   the good path: we get MAPI properties, so a message that lost its transport
   headers still yields a sender, a date and a body.
2. ``readpst`` (the ``libpst`` package) -- shell out, explode the PST into .eml
   files in a temp directory, then reuse the EML reader.

If neither is present we raise with the install commands, because guessing at
the PST binary format would silently produce a wrong archive.
"""

from __future__ import annotations

import email
import email.policy
import logging
import shutil
import subprocess
import tempfile
from datetime import timezone
from pathlib import Path
from typing import Iterator

from ..model import (
    Address,
    Attachment,
    Message,
    clean_message_id,
    normalize_folder,
    parse_references,
)
from .mime import (
    MAX_ATTACHMENT_BYTES,
    PRIORITY_MAP,
    decode_str,
    parse_addresses,
    parse_date,
    _safe_filename,
)

log = logging.getLogger("pastscape.pst")

MISSING_BACKEND_MSG = (
    "Reading PST files needs one of:\n"
    "  pip install libpff-python      (in-process, recommended)\n"
    "  apt install pst-utils          (provides `readpst`)\n"
    "Alternatively export the mail to .eml files and point Pastscape at that folder."
)

# MAPI property tags we care about, for backends that expose raw record sets.
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_LONG_FILENAME = 0x3707
PR_ATTACH_MIME_TAG = 0x370E
PR_ATTACH_CONTENT_ID = 0x3712


def read_pst(path: Path) -> Iterator[Message]:
    try:
        import pypff  # noqa: F401
    except ImportError:
        if shutil.which("readpst"):
            log.info("pypff unavailable; falling back to readpst")
            yield from _read_via_readpst(path)
            return
        raise RuntimeError(MISSING_BACKEND_MSG) from None
    yield from _read_via_pypff(path)


# ---------------------------------------------------------------------------
# pypff backend
# ---------------------------------------------------------------------------


def _read_via_pypff(path: Path) -> Iterator[Message]:
    import pypff

    pff = pypff.file()
    pff.open(str(path))
    try:
        root = pff.get_root_folder()
        yield from _walk_folder(root, [], path.name)
    finally:
        try:
            pff.close()
        except Exception:
            pass


def _walk_folder(folder, trail: list[str], source: str) -> Iterator[Message]:
    name = _folder_name(folder)
    # The PST root and the "Top of Personal Folders" container add a level of
    # nesting that Communicator never showed; flatten them away.
    new_trail = trail
    if name and name.lower() not in ("", "root", "root - mailbox", "top of personal folders",
                                     "top of outlook data file", "ipm_subtree"):
        new_trail = trail + [name]

    folder_path = normalize_folder("/".join(new_trail)) if new_trail else "Inbox"

    try:
        count = folder.get_number_of_sub_messages()
    except Exception:
        count = 0
    for i in range(count):
        try:
            item = folder.get_sub_message(i)
        except Exception as exc:
            log.warning("%s: message %d unreadable: %s", folder_path, i, exc)
            continue
        try:
            msg = _convert_message(item, folder_path, source)
        except Exception as exc:
            log.warning("%s: message %d conversion failed: %s", folder_path, i, exc)
            continue
        if msg is not None:
            yield msg

    try:
        subcount = folder.get_number_of_sub_folders()
    except Exception:
        subcount = 0
    for i in range(subcount):
        try:
            sub = folder.get_sub_folder(i)
        except Exception as exc:
            log.warning("%s: subfolder %d unreadable: %s", folder_path, i, exc)
            continue
        yield from _walk_folder(sub, new_trail, source)


def _folder_name(folder) -> str:
    for attr in ("name", "get_name"):
        try:
            value = getattr(folder, attr)
            value = value() if callable(value) else value
            if value:
                return str(value)
        except Exception:
            continue
    return ""


def _prop(item, attr: str):
    """pypff raises IOError for absent properties; treat that as None."""
    try:
        value = getattr(item, attr)
    except Exception:
        return None
    try:
        return value() if callable(value) else value
    except Exception:
        return None


def _convert_message(item, folder_path: str, source: str) -> Message | None:
    msg = Message(folder=folder_path, source=source)

    headers_raw = _prop(item, "transport_headers") or ""
    hdr = None
    if headers_raw:
        try:
            hdr = email.message_from_string(headers_raw, policy=email.policy.compat32)
        except Exception:
            hdr = None

    if hdr is not None:
        msg.subject = decode_str(hdr.get("Subject"))
        senders = parse_addresses(hdr.get("From"))
        msg.sender = senders[0] if senders else Address()
        reply_to = parse_addresses(hdr.get("Reply-To"))
        msg.reply_to = reply_to[0] if reply_to else None
        msg.to = parse_addresses(hdr.get("To"))
        msg.cc = parse_addresses(hdr.get("Cc"))
        msg.date_raw = decode_str(hdr.get("Date"))
        msg.date = parse_date(hdr.get("Date"))
        msg.message_id = clean_message_id(hdr.get("Message-ID") or "")
        msg.in_reply_to = clean_message_id(hdr.get("In-Reply-To") or "")
        msg.references = parse_references(hdr.get("References") or "")
        msg.organization = decode_str(hdr.get("Organization") or hdr.get("Organisation"))
        msg.newsgroups = decode_str(hdr.get("Newsgroups"))
        prio = (hdr.get("X-Priority") or hdr.get("Importance") or "").strip()
        prio_key = prio.split()[0].lower() if prio else ""
        msg.priority = PRIORITY_MAP.get(prio_key, "")
        if msg.priority == "Normal":
            msg.priority = ""
        msg.headers = [(k, decode_str(v)) for k, v in hdr.items()]

    # MAPI properties win where the headers are missing or empty: an item
    # composed in Outlook and never sent has no transport headers at all.
    if not msg.subject:
        msg.subject = decode_str(_prop(item, "subject") or _prop(item, "conversation_topic") or "")
    if not msg.sender.addr and not msg.sender.name:
        msg.sender = Address(
            name=decode_str(_prop(item, "sender_name") or ""),
            addr=_clean_mapi_addr(_prop(item, "sender_email_address")),
        )
    elif not msg.sender.addr:
        msg.sender.addr = _clean_mapi_addr(_prop(item, "sender_email_address"))

    if msg.date is None:
        for attr in ("delivery_time", "client_submit_time", "creation_time", "modification_time"):
            dt = _prop(item, attr)
            if dt is not None and hasattr(dt, "year"):
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if 1970 <= dt.year <= 2100:
                    msg.date = dt
                    if not msg.date_raw:
                        msg.date_raw = dt.strftime("%a, %d %b %Y %H:%M:%S %z")
                    break

    body_text = _prop(item, "plain_text_body")
    body_html = _prop(item, "html_body")
    msg.body_text = _decode_body(body_text)
    msg.body_html = _decode_body(body_html)
    if not msg.body_text and not msg.body_html:
        rtf = _prop(item, "rtf_body")
        if rtf:
            msg.body_html = _decode_body(rtf)
            if "\\rtf" in msg.body_html[:64]:
                # Uncompressed RTF we cannot render; keep the text we can see.
                msg.body_text = _rtf_to_text(msg.body_html)
                msg.body_html = ""

    msg.attachments = _read_attachments(item)
    msg.has_attachments = bool(msg.attachments)

    flags = _prop(item, "flags")
    if isinstance(flags, int):
        msg.unread = not (flags & 0x1)  # MSGFLAG_READ
    imp = _prop(item, "importance")
    if isinstance(imp, int) and not msg.priority:
        msg.priority = {0: "Low", 2: "High"}.get(imp, "")

    msg.size = _prop(item, "size") or (len(msg.body_text) + len(msg.body_html))
    if not msg.headers:
        msg.headers = _synthetic_headers(msg)

    if not (msg.subject or msg.body_text or msg.body_html or msg.sender.display() != "(unknown)"):
        return None
    return msg


def _clean_mapi_addr(value) -> str:
    """Drop Exchange X.500 addresses -- ``/O=ORG/OU=.../CN=RECIPIENTS/CN=BOB``
    is not something a mailto: link can use."""
    if not value:
        return ""
    text = str(value).strip()
    if text.startswith("/") or text.upper().startswith("EX:"):
        return ""
    return text if "@" in text else ""


def _decode_body(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return value.decode(enc).replace("\x00", "")
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", "replace")
    return str(value).replace("\x00", "")


def _rtf_to_text(rtf: str) -> str:
    import re

    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), rtf)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _read_attachments(item) -> list[Attachment]:
    out: list[Attachment] = []
    try:
        count = item.get_number_of_attachments()
    except Exception:
        return out
    for i in range(count):
        try:
            att = item.get_attachment(i)
        except Exception:
            continue
        try:
            size = att.get_size()
        except Exception:
            size = 0
        data = b""
        if size and size <= MAX_ATTACHMENT_BYTES:
            try:
                data = att.read_buffer(size)
            except Exception:
                data = b""
        props = _record_set_props(att)
        name = props.get(PR_ATTACH_LONG_FILENAME) or props.get(PR_ATTACH_FILENAME) or ""
        if not name:
            name = str(_prop(att, "name") or "") or f"attachment{i + 1}.bin"
        ctype = props.get(PR_ATTACH_MIME_TAG) or "application/octet-stream"
        out.append(
            Attachment(
                filename=_safe_filename(name),
                content_type=ctype,
                size=len(data) or size,
                payload=data,
                content_id=props.get(PR_ATTACH_CONTENT_ID, ""),
            )
        )
    return out


def _record_set_props(item) -> dict[int, str]:
    """Pull string-valued MAPI properties out of a pypff item, when the build
    exposes record sets. Older bindings do not, hence the broad guards."""
    props: dict[int, str] = {}
    try:
        n_sets = item.get_number_of_record_sets()
    except Exception:
        return props
    for si in range(n_sets):
        try:
            rec = item.get_record_set(si)
            n_entries = rec.get_number_of_entries()
        except Exception:
            continue
        for ei in range(n_entries):
            try:
                entry = rec.get_entry(ei)
                tag = entry.get_entry_type()
            except Exception:
                continue
            if tag not in (PR_ATTACH_FILENAME, PR_ATTACH_LONG_FILENAME,
                           PR_ATTACH_MIME_TAG, PR_ATTACH_CONTENT_ID):
                continue
            value = ""
            for getter in ("get_data_as_string", "get_data"):
                try:
                    raw = getattr(entry, getter)()
                except Exception:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-16-le" if b"\x00" in raw[:8] else "utf-8", "replace")
                value = str(raw or "").rstrip("\x00")
                if value:
                    break
            if value:
                props[tag] = value
    return props


def _synthetic_headers(msg: Message) -> list[tuple[str, str]]:
    """Rebuild a plausible header block so the "View Source" pane and the
    reply link have something to work with."""
    hdrs = []
    if msg.date_raw:
        hdrs.append(("Date", msg.date_raw))
    if msg.sender.full():
        hdrs.append(("From", msg.sender.full()))
    if msg.subject:
        hdrs.append(("Subject", msg.subject))
    if msg.to:
        hdrs.append(("To", ", ".join(a.full() for a in msg.to)))
    if msg.cc:
        hdrs.append(("Cc", ", ".join(a.full() for a in msg.cc)))
    if msg.message_id:
        hdrs.append(("Message-ID", f"<{msg.message_id}>"))
    hdrs.append(("X-Pastscape-Source", "MAPI properties (no transport headers in PST)"))
    return hdrs


# ---------------------------------------------------------------------------
# readpst backend
# ---------------------------------------------------------------------------


def _read_via_readpst(path: Path) -> Iterator[Message]:
    from .eml import read_eml_dir

    with tempfile.TemporaryDirectory(prefix="pastscape-pst-") as tmp:
        cmd = ["readpst", "-e", "-D", "-r", "-o", tmp, str(path)]
        log.info("running %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"readpst failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
        yield from read_eml_dir(Path(tmp))
