"""
Tests for Phase E2 — rollback / snapshots cache:
  - _save_snapshot, _prune_snapshots, _list_snapshots
  - nexmod snapshots <game> [<mod_id>] [--prune]
  - nexmod rollback <game> <mod_id> [--version V] [--list]
"""
import zipfile
import time
import pytest
from pathlib import Path
from click.testing import CliRunner
import nexmod
from nexmod import cli

pytestmark = pytest.mark.usefixtures("isolated_dirs")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cache_redirect(tmp_path, monkeypatch):
    """Redirect snapshot cache to tmp_path so tests don't touch ~/.cache/nexmod."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(nexmod, "CACHE_DIR", cache)
    return cache


def _make_zip(path: Path, content: dict):
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in content.items():
            zf.writestr(name, body)


def _seed_mod(game="darktide", mod_id=1, version="1.0", folder_name="MyMod",
              mod_dir=None):
    db = nexmod.get_db()
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, version, filename,
                          mod_dir, folder_name, tracked_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (game, mod_id, 1, "MyMod", version, "x.zip",
          str(mod_dir or "/tmp/mods"), folder_name,
          nexmod.now_iso(), nexmod.now_iso()))
    db.commit()


# ── _save_snapshot ───────────────────────────────────────────────────────────

def test_save_snapshot_basic(tmp_path, cache_redirect):
    archive = tmp_path / "mod-1.0.zip"
    _make_zip(archive, {"mod/x.txt": "v1"})
    snap = nexmod._save_snapshot("darktide", 1, "1.0", archive)
    assert snap is not None
    assert snap.exists()
    assert snap.name == "1.0.zip"
    # Same content
    with zipfile.ZipFile(snap) as zf:
        assert zf.read("mod/x.txt") == b"v1"


def test_save_snapshot_no_version_returns_none(tmp_path, cache_redirect):
    archive = tmp_path / "x.zip"
    _make_zip(archive, {"a": "x"})
    assert nexmod._save_snapshot("darktide", 1, None, archive) is None
    assert nexmod._save_snapshot("darktide", 1, "", archive) is None


def test_save_snapshot_missing_archive_returns_none(tmp_path, cache_redirect):
    assert nexmod._save_snapshot("darktide", 1, "1.0",
                                  tmp_path / "nonexistent.zip") is None


def test_save_snapshot_preserves_compound_extension(tmp_path, cache_redirect):
    archive = tmp_path / "mod.tar.gz"
    archive.write_bytes(b"\x1f\x8b\x08\x00")  # gzip magic, doesn't matter for copy
    snap = nexmod._save_snapshot("darktide", 1, "2.0", archive)
    assert snap.name == "2.0.tar.gz"


def test_save_snapshot_sanitizes_version_chars(tmp_path, cache_redirect):
    archive = tmp_path / "x.zip"
    _make_zip(archive, {"a": "x"})
    snap = nexmod._save_snapshot("darktide", 1, "1.0/beta+1", archive)
    # Slash and plus should be replaced
    assert "/" not in snap.name
    assert "+" not in snap.name


# ── _prune_snapshots ─────────────────────────────────────────────────────────

def test_prune_keeps_newest_n(tmp_path, cache_redirect, monkeypatch):
    monkeypatch.setattr(nexmod, "SNAPSHOTS_PER_MOD", 2)
    snap_dir = nexmod._snapshot_dir("darktide", 1)
    snap_dir.mkdir(parents=True)
    # Create 4 snapshots with stepped mtimes
    for i, v in enumerate(["1.0", "1.1", "1.2", "1.3"]):
        p = snap_dir / f"{v}.zip"
        p.write_text("x")
        # Older mtime for older versions
        ts = time.time() - (4 - i) * 100
        import os as _os
        _os.utime(p, (ts, ts))

    pruned = nexmod._prune_snapshots("darktide", 1)
    assert pruned == 2
    remaining = sorted(p.name for p in snap_dir.iterdir())
    assert remaining == ["1.2.zip", "1.3.zip"]


def test_save_snapshot_triggers_prune(tmp_path, cache_redirect, monkeypatch):
    monkeypatch.setattr(nexmod, "SNAPSHOTS_PER_MOD", 2)
    archive = tmp_path / "x.zip"
    _make_zip(archive, {"a": "x"})
    nexmod._save_snapshot("darktide", 1, "1.0", archive)
    time.sleep(0.01)
    nexmod._save_snapshot("darktide", 1, "1.1", archive)
    time.sleep(0.01)
    nexmod._save_snapshot("darktide", 1, "1.2", archive)
    snaps = nexmod._list_snapshots("darktide", 1)
    assert len(snaps) == 2
    labels = sorted(nexmod._snapshot_version_label(s) for s in snaps)
    assert labels == ["1.1", "1.2"]


def test_list_snapshots_newest_first(tmp_path, cache_redirect):
    snap_dir = nexmod._snapshot_dir("darktide", 1)
    snap_dir.mkdir(parents=True)
    import os as _os
    p1 = snap_dir / "1.0.zip"; p1.write_text("x"); _os.utime(p1, (1, 1))
    p2 = snap_dir / "2.0.zip"; p2.write_text("x"); _os.utime(p2, (2, 2))
    assert [s.name for s in nexmod._list_snapshots("darktide", 1)] == ["2.0.zip", "1.0.zip"]


# ── snapshots CLI ────────────────────────────────────────────────────────────

def test_snapshots_lists_for_mod(tmp_path, cache_redirect, runner):
    _seed_mod()
    snap_dir = nexmod._snapshot_dir("darktide", 1)
    snap_dir.mkdir(parents=True)
    (snap_dir / "1.0.zip").write_text("x")
    (snap_dir / "1.1.zip").write_text("x")

    result = runner.invoke(cli, ["snapshots", "darktide"])
    assert result.exit_code == 0
    assert "MyMod" in result.output
    assert "1.0" in result.output and "1.1" in result.output


def test_snapshots_no_records(runner):
    result = runner.invoke(cli, ["snapshots", "darktide"])
    assert result.exit_code == 0
    assert "No tracked records" in result.output


def test_snapshots_prune_flag(tmp_path, cache_redirect, runner, monkeypatch):
    monkeypatch.setattr(nexmod, "SNAPSHOTS_PER_MOD", 1)
    _seed_mod()
    snap_dir = nexmod._snapshot_dir("darktide", 1)
    snap_dir.mkdir(parents=True)
    import os as _os
    for i, v in enumerate(["1.0", "1.1", "1.2"]):
        p = snap_dir / f"{v}.zip"; p.write_text("x")
        _os.utime(p, (i + 1, i + 1))
    result = runner.invoke(cli, ["snapshots", "darktide", "--prune"])
    assert result.exit_code == 0
    assert "pruned 2" in result.output
    remaining = list(snap_dir.iterdir())
    assert len(remaining) == 1


# ── rollback CLI ─────────────────────────────────────────────────────────────

def test_rollback_no_snapshots(runner):
    _seed_mod()
    result = runner.invoke(cli, ["rollback", "darktide", "1"])
    assert result.exit_code == 1
    assert "No snapshots cached" in result.output


def test_rollback_only_current_version(tmp_path, cache_redirect, runner):
    _seed_mod(version="1.0")
    snap_dir = nexmod._snapshot_dir("darktide", 1)
    snap_dir.mkdir(parents=True)
    (snap_dir / "1.0.zip").write_text("x")

    result = runner.invoke(cli, ["rollback", "darktide", "1", "--yes"])
    assert result.exit_code == 1
    assert "No prior snapshot" in result.output


def test_rollback_to_previous(tmp_path, cache_redirect, runner):
    mod_dir = tmp_path / "mods"
    (mod_dir / "MyMod").mkdir(parents=True)
    (mod_dir / "MyMod" / "current.txt").write_text("v1.1")
    _seed_mod(version="1.1", mod_dir=mod_dir, folder_name="MyMod")

    snap_dir = nexmod._snapshot_dir("darktide", 1)
    snap_dir.mkdir(parents=True)
    # Older snapshot with different content
    older = snap_dir / "1.0.zip"
    _make_zip(older, {"MyMod/old.txt": "v1.0"})
    import os as _os; _os.utime(older, (1, 1))
    # Current snapshot
    current = snap_dir / "1.1.zip"
    _make_zip(current, {"MyMod/current.txt": "v1.1"})
    _os.utime(current, (2, 2))

    result = runner.invoke(cli, ["rollback", "darktide", "1", "--yes"])
    assert result.exit_code == 0
    assert "Rolled back to 1.0" in result.output
    # Old folder gone, new one extracted from snapshot
    assert (mod_dir / "MyMod" / "old.txt").read_text() == "v1.0"
    assert not (mod_dir / "MyMod" / "current.txt").exists()
    # DB version updated
    db = nexmod.get_db()
    row = db.execute("SELECT version FROM mods WHERE mod_id=1").fetchone()
    assert row["version"] == "1.0"


def test_rollback_to_specific_version(tmp_path, cache_redirect, runner):
    mod_dir = tmp_path / "mods"
    (mod_dir / "MyMod").mkdir(parents=True)
    _seed_mod(version="1.2", mod_dir=mod_dir, folder_name="MyMod")

    snap_dir = nexmod._snapshot_dir("darktide", 1)
    snap_dir.mkdir(parents=True)
    import os as _os
    for i, v in enumerate(["1.0", "1.1", "1.2"]):
        p = snap_dir / f"{v}.zip"
        _make_zip(p, {f"MyMod/v{v}.txt": v})
        _os.utime(p, (i + 1, i + 1))

    result = runner.invoke(cli, ["rollback", "darktide", "1", "--version", "1.0", "--yes"])
    assert result.exit_code == 0
    assert "Rolled back to 1.0" in result.output


def test_rollback_unknown_version_fails(tmp_path, cache_redirect, runner):
    _seed_mod(version="1.0")
    snap_dir = nexmod._snapshot_dir("darktide", 1)
    snap_dir.mkdir(parents=True)
    (snap_dir / "1.0.zip").write_text("x")
    result = runner.invoke(
        cli, ["rollback", "darktide", "1", "--version", "9.9", "--yes"]
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_rollback_list_only(tmp_path, cache_redirect, runner):
    _seed_mod()
    snap_dir = nexmod._snapshot_dir("darktide", 1)
    snap_dir.mkdir(parents=True)
    (snap_dir / "1.0.zip").write_text("x")
    (snap_dir / "1.1.zip").write_text("x")

    result = runner.invoke(cli, ["rollback", "darktide", "1", "--list"])
    assert result.exit_code == 0
    assert "1.0" in result.output and "1.1" in result.output
    # Should not have invoked the rollback path
    assert "Rolled back" not in result.output
