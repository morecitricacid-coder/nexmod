"""
Tests for Phase B safety guardrails:
  - _check_disk_space        — pre-flight disk capacity check
  - _archive_top_level_dirs  — peek archive contents for conflict detection
  - _detect_install_conflicts — DB lookup for cross-mod folder collisions
  - remove --purge guards    — confirm prompt, legacy fallback gating
"""
import zipfile
import tarfile
import sqlite3
import shutil
import pytest
from pathlib import Path
from click.testing import CliRunner
from unittest.mock import patch
import nexmod
from nexmod import cli

pytestmark = pytest.mark.usefixtures("isolated_dirs")


@pytest.fixture
def runner():
    return CliRunner()


# ── _check_disk_space ────────────────────────────────────────────────────────

def test_disk_space_passes_when_enough(tmp_path):
    # Real filesystem will have plenty of space for 1 byte
    nexmod._check_disk_space(tmp_path, 1, "test")  # no raise


def test_disk_space_zero_required_is_noop(tmp_path):
    nexmod._check_disk_space(tmp_path, 0, "test")  # no raise
    nexmod._check_disk_space(tmp_path, -1, "test")  # no raise


def test_disk_space_raises_when_insufficient(tmp_path):
    """Mock disk_usage to simulate near-full disk."""
    fake_usage = shutil._ntuple_diskusage(total=1000, used=999, free=1)
    with patch("nexmod.shutil.disk_usage", return_value=fake_usage):
        with pytest.raises(RuntimeError, match="Not enough disk space"):
            nexmod._check_disk_space(tmp_path, 1024, "extraction")


def test_disk_space_handles_oserror_silently(tmp_path):
    """If disk_usage fails (e.g., path doesn't exist), check is skipped."""
    with patch("nexmod.shutil.disk_usage", side_effect=OSError("no such path")):
        nexmod._check_disk_space(tmp_path, 1_000_000_000_000, "test")  # no raise


# ── _archive_top_level_dirs ──────────────────────────────────────────────────

def test_peek_zip_top_dirs(tmp_path):
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ModA/file1.txt", "x")
        zf.writestr("ModA/sub/file2.txt", "x")
        zf.writestr("ModB/file3.txt", "x")
    assert nexmod._archive_top_level_dirs(archive) == ["ModA", "ModB"]


def test_peek_tar_top_dirs(tmp_path):
    archive = tmp_path / "test.tar.gz"
    src = tmp_path / "src"
    (src / "ModC").mkdir(parents=True)
    (src / "ModC" / "f.txt").write_text("x")
    (src / "ModD").mkdir()
    (src / "ModD" / "f.txt").write_text("x")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src / "ModC", arcname="ModC")
        tf.add(src / "ModD", arcname="ModD")
    assert nexmod._archive_top_level_dirs(archive) == ["ModC", "ModD"]


def test_peek_unknown_format_returns_empty(tmp_path):
    archive = tmp_path / "test.7z"
    archive.write_bytes(b"fake 7z content")
    assert nexmod._archive_top_level_dirs(archive) == []


def test_peek_corrupted_zip_returns_empty(tmp_path):
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"not actually a zip")
    assert nexmod._archive_top_level_dirs(archive) == []


# ── _detect_install_conflicts ────────────────────────────────────────────────

def _seed(db, mod_id, folder, name="ModX", game="darktide"):
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, version, filename,
                          mod_dir, folder_name, tracked_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (game, mod_id, mod_id, name, "1.0", "x.zip", "/tmp/mods",
          folder, nexmod.now_iso(), nexmod.now_iso()))
    db.commit()


def test_detect_conflicts_finds_collision():
    db = nexmod.get_db()
    _seed(db, mod_id=1, folder="SharedFolder", name="ModOne")

    conflicts = nexmod._detect_install_conflicts(db, "darktide", 99, ["SharedFolder"])
    assert len(conflicts) == 1
    assert conflicts[0] == ("SharedFolder", "ModOne", 1)


def test_detect_conflicts_excludes_self():
    """A mod's own folder shouldn't conflict with itself on update."""
    db = nexmod.get_db()
    _seed(db, mod_id=1, folder="SelfFolder", name="ModSelf")

    conflicts = nexmod._detect_install_conflicts(db, "darktide", 1, ["SelfFolder"])
    assert conflicts == []


def test_detect_conflicts_per_game_isolation():
    """A folder claimed in skyrimse shouldn't conflict with darktide install."""
    db = nexmod.get_db()
    _seed(db, mod_id=1, folder="Common", name="SkyrimMod", game="skyrimse")

    conflicts = nexmod._detect_install_conflicts(db, "darktide", 99, ["Common"])
    assert conflicts == []


def test_detect_conflicts_empty_top_dirs():
    db = nexmod.get_db()
    assert nexmod._detect_install_conflicts(db, "darktide", 1, []) == []


# ── remove --purge guards ────────────────────────────────────────────────────

def test_remove_purge_blocks_when_no_folder_name(runner):
    """A row with NULL folder_name should refuse --purge without --force-legacy-purge."""
    db = nexmod.get_db()
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, version, filename,
                          mod_dir, folder_name, tracked_at, updated_at)
        VALUES ('darktide', 1, 1, 'OldMod', '1.0', 'thing.zip', '/tmp/mods',
                NULL, ?, ?)
    """, (nexmod.now_iso(), nexmod.now_iso()))
    db.commit()

    result = runner.invoke(cli, ["remove", "darktide", "1", "--purge"])
    assert result.exit_code == 1
    assert "Cannot purge" in result.output
    # DB row still present
    assert db.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 1


def test_remove_purge_dry_run_makes_no_changes(tmp_path, runner):
    mod_dir = tmp_path / "mods"
    (mod_dir / "MyMod").mkdir(parents=True)
    (mod_dir / "MyMod" / "file.txt").write_text("x")

    db = nexmod.get_db()
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, version, filename,
                          mod_dir, folder_name, tracked_at, updated_at)
        VALUES ('darktide', 1, 1, 'MyMod', '1.0', 'x.zip', ?, 'MyMod', ?, ?)
    """, (str(mod_dir), nexmod.now_iso(), nexmod.now_iso()))
    db.commit()

    result = runner.invoke(cli, ["remove", "darktide", "1", "--purge", "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output.lower()
    # Folder still on disk
    assert (mod_dir / "MyMod").exists()
    # DB row still present
    assert db.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 1


def test_remove_purge_yes_skips_confirm(tmp_path, runner):
    mod_dir = tmp_path / "mods"
    (mod_dir / "MyMod").mkdir(parents=True)
    (mod_dir / "MyMod" / "file.txt").write_text("x")

    db = nexmod.get_db()
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, version, filename,
                          mod_dir, folder_name, tracked_at, updated_at)
        VALUES ('darktide', 1, 1, 'MyMod', '1.0', 'x.zip', ?, 'MyMod', ?, ?)
    """, (str(mod_dir), nexmod.now_iso(), nexmod.now_iso()))
    db.commit()

    result = runner.invoke(cli, ["remove", "darktide", "1", "--purge", "--yes"])
    assert result.exit_code == 0
    assert not (mod_dir / "MyMod").exists()
    assert db.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 0


def test_remove_purge_decline_aborts(tmp_path, runner):
    mod_dir = tmp_path / "mods"
    (mod_dir / "MyMod").mkdir(parents=True)
    (mod_dir / "MyMod" / "file.txt").write_text("x")

    db = nexmod.get_db()
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, version, filename,
                          mod_dir, folder_name, tracked_at, updated_at)
        VALUES ('darktide', 1, 1, 'MyMod', '1.0', 'x.zip', ?, 'MyMod', ?, ?)
    """, (str(mod_dir), nexmod.now_iso(), nexmod.now_iso()))
    db.commit()

    # Click confirm reads from stdin; "n\n" rejects
    result = runner.invoke(cli, ["remove", "darktide", "1", "--purge"], input="n\n")
    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert (mod_dir / "MyMod").exists()
    assert db.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 1


def test_remove_purge_force_legacy_uses_filename_stem(tmp_path, runner):
    mod_dir = tmp_path / "mods"
    (mod_dir / "InferredFromFilename").mkdir(parents=True)

    db = nexmod.get_db()
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, version, filename,
                          mod_dir, folder_name, tracked_at, updated_at)
        VALUES ('darktide', 1, 1, 'OldMod', '1.0', 'InferredFromFilename.zip',
                ?, NULL, ?, ?)
    """, (str(mod_dir), nexmod.now_iso(), nexmod.now_iso()))
    db.commit()

    result = runner.invoke(
        cli, ["remove", "darktide", "1", "--purge", "--force-legacy-purge", "--yes"]
    )
    assert result.exit_code == 0
    assert "legacy fallback" in result.output.lower()
    assert not (mod_dir / "InferredFromFilename").exists()


# ── .rar archive guard ───────────────────────────────────────────────────────

class TestRarGuard:
    """.rar files raise RuntimeError instead of silently copying."""

    def test_rar_raises_runtime_error(self, tmp_path):
        archive = tmp_path / "coolmod.rar"
        archive.write_bytes(b"Rar!\x1a\x07fake rar content")
        target = tmp_path / "target"
        target.mkdir()
        with pytest.raises(RuntimeError) as exc_info:
            nexmod.extract_archive(archive, target)
        msg = str(exc_info.value)
        assert ".rar" in msg
        assert "nexmod import" in msg

    def test_rar_error_mentions_manual_url_hint(self, tmp_path):
        archive = tmp_path / "somemod.rar"
        archive.write_bytes(b"fake rar")
        target = tmp_path / "target"
        target.mkdir()
        with pytest.raises(RuntimeError) as exc_info:
            nexmod.extract_archive(archive, target)
        assert ".zip" in str(exc_info.value) or ".7z" in str(exc_info.value)

    def test_rar_does_not_copy_file_to_target(self, tmp_path):
        """Raw .rar must not land in target_dir after the failed call."""
        archive = tmp_path / "coolmod.rar"
        archive.write_bytes(b"fake rar content")
        target = tmp_path / "target"
        target.mkdir()
        with pytest.raises(RuntimeError):
            nexmod.extract_archive(archive, target)
        assert not (target / "coolmod.rar").exists()

    def test_zip_still_extracts_normally(self, tmp_path):
        """Regression guard: .zip extraction must be unaffected."""
        archive = tmp_path / "mod.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("MyMod/script.lua", "-- mod code")
        target = tmp_path / "target"
        nexmod.extract_archive(archive, target)
        assert (target / "MyMod" / "script.lua").exists()
