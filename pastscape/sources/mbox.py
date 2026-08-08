"""Read an mbox file. Netscape's own Local Mail store was mbox, so this is the
path for anyone who still has a real ``Inbox`` file from Communicator."""

from __future__ import annotations

import logging
import mailbox
from pathlib import Path
from typing import Iterator

from ..model import Message, normalize_folder
from .mime import message_from_pymessage

log = logging.getLogger("pastscape.mbox")


def read_mbox(path: Path) -> Iterator[Message]:
    folder = normalize_folder(path.stem)
    box = mailbox.mbox(str(path), create=False)
    try:
        for key in box.iterkeys():
            try:
                pym = box.get_message(key)
            except Exception as exc:
                log.warning("skipping message %s in %s: %s", key, path, exc)
                continue
            try:
                raw_size = len(box.get_bytes(key))
            except Exception:
                raw_size = 0
            msg = message_from_pymessage(pym, folder=folder, source=path.name, raw_size=raw_size)
            # mbox status flags are authoritative here.
            flags = pym.get("Status", "") + pym.get("X-Status", "")
            msg.unread = "R" not in flags
            msg.flagged = "F" in flags
            yield msg
    finally:
        box.close()
