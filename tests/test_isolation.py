"""Import-hygiene checks that must run in a clean interpreter.

A submodule like ``email.policy`` can be bound as a side effect of some other
import in the same process, so a missing import here passes under pytest and
fails for a user running the CLI. These tests shell out to guarantee a fresh
interpreter.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(ROOT),
    )


def test_eml_parsing_works_in_a_fresh_interpreter():
    proc = run(
        "from pastscape.sources.mime import message_from_bytes\n"
        "m = message_from_bytes(b'From: a@b.c\\nSubject: T\\n\\nbody\\n', 'Inbox', 's')\n"
        "assert m is not None, 'parser returned None'\n"
        "assert m.subject == 'T' and m.sender.addr == 'a@b.c'\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_every_module_imports_on_its_own():
    for module in [
        "pastscape.model", "pastscape.sanitize", "pastscape.search",
        "pastscape.state", "pastscape.render", "pastscape.builder",
        "pastscape.cli", "pastscape.sources", "pastscape.sources.mime",
        "pastscape.sources.eml", "pastscape.sources.mbox", "pastscape.sources.pst",
    ]:
        proc = run(f"import {module}")
        assert proc.returncode == 0, f"{module}: {proc.stderr}"


def test_cli_help_runs():
    proc = run("import sys; sys.argv=['pastscape','--help']; "
               "from pastscape.cli import main; "
               "sys.exit(main(['--help']))")
    assert "build" in proc.stdout
