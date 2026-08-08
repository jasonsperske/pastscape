"""End-to-end: sample corpus -> site, then the incremental re-run."""

import json
from email.utils import format_datetime, make_msgid
from datetime import timedelta
from pathlib import Path

import pytest

from pastscape.builder import build_site
from pastscape.state import Manifest, reconstruct_from_site

from make_sample import BASE, main as write_sample


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("mail")
    write_sample(str(root))
    return root


@pytest.fixture(scope="module")
def site(tmp_path_factory, sample) -> Path:
    out = tmp_path_factory.mktemp("site")
    build_site([sample], out, title="Local Mail")
    return out


def read_json(path: Path):
    return json.loads(path.read_text("utf-8"))


# ------------------------------------------------------------------ structure


def test_core_files_exist(site):
    for rel in ("index.html", "manifest.json", "assets/pastscape.css",
                "assets/pastscape.js", "assets/icons.svg",
                "data/folders.json", "data/search/meta.json", "data/search/docs.json"):
        assert (site / rel).is_file(), f"missing {rel}"


def test_folders_json_shape(site):
    cfg = read_json(site / "data" / "folders.json")
    assert cfg["title"] == "Local Mail"
    assert cfg["totalMessages"] == 13
    paths = [f["path"] for f in cfg["folders"]]
    # Mailbox root, then year, then the folders. The sample corpus is one
    # mailbox and all from 1997.
    assert cfg["accounts"] == ["you@example.com"]
    assert paths[0] == "you@example.com"
    assert paths[1] == "you@example.com/1997"
    assert paths[2] == "you@example.com/1997/Inbox"   # Communicator order, not A-Z
    assert "you@example.com/1997/Inbox/Projects" in paths
    assert "you@example.com/1997/Trash" in paths
    for f in cfg["folders"]:
        assert (site / f["file"]).is_file()


def test_every_message_has_a_page(site):
    man = Manifest.load(site)
    assert len(man.messages) == 13
    for rec in man.messages.values():
        assert (site / rec.page).is_file()


def test_message_page_embeds_its_own_metadata(site):
    man = Manifest.load(site)
    rec = next(r for r in man.messages.values() if r.subject.startswith("Welcome"))
    html = (site / rec.page).read_text("utf-8")
    meta = json.loads(html.split('id="ps-meta">')[1].split("</script>")[0])
    assert meta["uid"] == rec.uid
    assert meta["from"]["a"] == "info@netscape.example.com"
    assert meta["hash"] == rec.hash
    assert meta["date"] > 0


def test_reply_link_is_a_mailto_to_the_sender(site):
    man = Manifest.load(site)
    rec = next(r for r in man.messages.values() if r.subject.startswith("Welcome"))
    html = (site / rec.page).read_text("utf-8")
    assert "mailto:info@netscape.example.com?subject=Re%3A" in html


def test_attachments_are_extracted(site):
    paths = list((site / "attachments").rglob("*"))
    names = {p.name for p in paths if p.is_file()}
    assert "whiteboard.gif" in names
    assert "palette.patch" in names


def test_html_body_is_sanitised_in_the_published_page(site):
    man = Manifest.load(site)
    rec = next(r for r in man.messages.values() if "Newsletter" in r.subject)
    html = (site / rec.page).read_text("utf-8")
    body = html.split('class="ps-msg-body')[1]
    assert "alert(" not in body
    assert "javascript:" not in body
    assert "data-ps-src=" in body          # remote images parked


# --------------------------------------------------------------------- search


def test_search_index_is_sharded_and_queryable(site):
    meta = read_json(site / "data" / "search" / "meta.json")
    assert meta["docs"] == 13
    assert meta["shards"]

    shard = read_json(site / "data" / "search" / f"{'a' if meta['shardLen'] == 1 else 'an'}.json")
    assert "andreessen" in shard

    docs = read_json(site / "data" / "search" / "docs.json")
    assert len(docs) == 13
    slug, idx = docs[0]
    rows = read_json(site / "data" / "msgs" / f"{slug}.json")["rows"]
    assert 0 <= idx < len(rows)


def test_search_postings_resolve_to_the_right_message(site):
    meta = read_json(site / "data" / "search" / "meta.json")
    shard = read_json(site / "data" / "search" / f"{'a' if meta['shardLen'] == 1 else 'an'}.json")
    docs = read_json(site / "data" / "search" / "docs.json")

    flat, gid, hits = shard["andreessen"], 0, []
    for i in range(0, len(flat), 2):
        gid += flat[i]
        hits.append(gid)
    assert hits

    subjects = []
    for g in hits:
        slug, idx = docs[g]
        rows = read_json(site / "data" / "msgs" / f"{slug}.json")["rows"]
        subjects.append(rows[idx][1])
    assert any("Welcome" in s for s in subjects)


def test_attachment_names_are_searchable(site):
    meta = read_json(site / "data" / "search" / "meta.json")
    key = "w" if meta["shardLen"] == 1 else "wh"
    shard = read_json(site / "data" / "search" / f"{key}.json")
    assert "whiteboard" in shard


# ---------------------------------------------------------------- incremental


def test_rebuild_is_a_no_op(tmp_path, sample):
    out = tmp_path / "site"
    first = build_site([sample], out, title="Local Mail")
    assert first.added == 13 and first.unchanged == 0

    second = build_site([sample], out, title="Local Mail")
    assert second.added == 0
    assert second.updated == 0
    assert second.unchanged == 13


def test_new_message_is_added_without_touching_the_rest(tmp_path, sample):
    out = tmp_path / "site2"
    build_site([sample], out, title="Local Mail")

    new_mail = tmp_path / "more" / "Inbox"
    new_mail.mkdir(parents=True)
    (new_mail / "later.eml").write_text(
        "From: Later Sender <later@example.com>\n"
        "To: you@example.com\n"
        "Subject: A message that arrived afterwards\n"
        f"Date: {format_datetime(BASE + timedelta(days=400))}\n"
        f"Message-ID: {make_msgid(domain='example.com')}\n"
        "\nThis one is new.\n",
        "utf-8",
    )

    stats = build_site([sample, tmp_path / "more"], out, title="Local Mail")
    assert stats.added == 1
    # Only the neighbour whose prev/next changed gets rewritten.
    assert stats.updated <= 2
    assert stats.unchanged >= 11

    cfg = json.loads((out / "data" / "folders.json").read_text("utf-8"))
    assert cfg["totalMessages"] == 14

    man = Manifest.load(out)
    added = next(r for r in man.messages.values() if r.subject.startswith("A message that"))
    assert (out / added.page).is_file()


def test_removed_messages_are_kept_unless_pruned(tmp_path, sample):
    out = tmp_path / "site3"
    build_site([sample], out, title="Local Mail")

    trimmed = tmp_path / "trimmed"
    trimmed.mkdir()
    src_inbox = sample / "Inbox"
    dst_inbox = trimmed / "Inbox"
    dst_inbox.mkdir()
    keep = sorted(src_inbox.glob("*.eml"))[:2]
    for path in keep:
        (dst_inbox / path.name).write_bytes(path.read_bytes())

    kept = build_site([trimmed], out, title="Local Mail")
    assert kept.pruned == 0
    assert len(Manifest.load(out).messages) == 13   # nothing lost

    pruned = build_site([trimmed], out, title="Local Mail", prune=True)
    assert pruned.pruned == 11
    assert len(Manifest.load(out).messages) == 2


def test_manifest_can_be_rebuilt_from_the_published_pages(site):
    original = Manifest.load(site)
    recovered = reconstruct_from_site(site)
    assert set(recovered.messages) == set(original.messages)
    for uid, rec in recovered.messages.items():
        assert rec.hash == original.messages[uid].hash
        assert rec.folder == original.messages[uid].folder


def test_force_rewrites_but_keeps_first_seen(tmp_path, sample):
    out = tmp_path / "site4"
    build_site([sample], out, title="Local Mail")
    before = {uid: r.first_seen for uid, r in Manifest.load(out).messages.items()}

    stats = build_site([sample], out, title="Local Mail", force=True)
    assert stats.updated == 13 and stats.added == 0
    after = {uid: r.first_seen for uid, r in Manifest.load(out).messages.items()}
    assert before == after


def test_duplicate_sources_are_deduplicated(tmp_path, sample):
    out = tmp_path / "site5"
    stats = build_site([sample, sample], out, title="Local Mail")
    assert stats.total == 13
    assert stats.duplicates == 13


def test_folder_prefix_nests_everything(tmp_path, sample):
    out = tmp_path / "site6"
    build_site([sample], out, title="Old Mail", folder_prefix="Archive 2004")
    cfg = json.loads((out / "data" / "folders.json").read_text("utf-8"))
    assert all(f["path"].startswith("Archive 2004") for f in cfg["folders"])


def test_empty_read_does_not_blank_an_existing_archive(tmp_path, sample):
    out = tmp_path / "site7"
    build_site([sample], out, title="Local Mail")
    before = (out / "data" / "folders.json").read_text("utf-8")

    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="refusing to rewrite"):
        build_site([empty], out, title="Local Mail")

    # The published archive is untouched, not half-rewritten.
    assert (out / "data" / "folders.json").read_text("utf-8") == before
    assert len(Manifest.load(out).messages) == 13


def test_corrupt_source_does_not_blank_an_existing_archive(tmp_path, sample):
    out = tmp_path / "site8"
    build_site([sample], out, title="Local Mail")

    broken = tmp_path / "broken.pst"
    broken.write_bytes(b"!BDN" + b"\x00" * 8)
    with pytest.raises(RuntimeError, match="refusing to rewrite"):
        build_site([broken], out, title="Local Mail")
    assert len(Manifest.load(out).messages) == 13


def test_first_build_of_an_empty_source_is_allowed(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    stats = build_site([empty], tmp_path / "site9", title="Empty")
    assert stats.total == 0
    assert (tmp_path / "site9" / "index.html").is_file()
