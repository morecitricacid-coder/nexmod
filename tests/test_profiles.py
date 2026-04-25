"""Tests for profile management helpers."""
import json
import pytest
import nexmod
from pathlib import Path

pytestmark = pytest.mark.usefixtures("isolated_dirs")

LOF = "mod_load_order.txt"

GAME_CFG = {
    "testgame": {
        "domain": "testgame", "steam_id": 0,
        "mod_subdir": "mods", "load_order_file": LOF, "log_subpath": None,
    }
}


# ── _write_profile / _read_profile ────────────────────────────────────────────

def test_write_and_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("nexmod.PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("testgame", "full", ["ModA", "ModB"], "all mods")
    p = nexmod._read_profile("testgame", "full")
    assert p["name"]        == "full"
    assert p["game"]        == "testgame"
    assert p["description"] == "all mods"
    assert p["load_order"]  == ["ModA", "ModB"]
    assert "created_at" in p
    assert "updated_at" in p


def test_write_preserves_created_at_on_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr("nexmod.PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("testgame", "full", ["ModA"])
    first = nexmod._read_profile("testgame", "full")["created_at"]
    nexmod._write_profile("testgame", "full", ["ModA", "ModB"])  # overwrite
    second = nexmod._read_profile("testgame", "full")["created_at"]
    assert first == second  # created_at preserved


def test_read_nonexistent_profile_exits(tmp_path, monkeypatch):
    monkeypatch.setattr("nexmod.PROFILES_DIR", tmp_path / "profiles")
    with pytest.raises(SystemExit):
        nexmod._read_profile("testgame", "ghost")


# ── _list_profiles ────────────────────────────────────────────────────────────

def test_list_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("nexmod.PROFILES_DIR", tmp_path / "profiles")
    assert nexmod._list_profiles("testgame") == []


def test_list_returns_all(tmp_path, monkeypatch):
    monkeypatch.setattr("nexmod.PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("testgame", "alpha", ["A"])
    nexmod._write_profile("testgame", "beta",  ["B", "C"])
    profiles = nexmod._list_profiles("testgame")
    names = [p["name"] for p in profiles]
    assert sorted(names) == ["alpha", "beta"]


def test_list_isolated_by_game(tmp_path, monkeypatch):
    monkeypatch.setattr("nexmod.PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("testgame",  "p1", ["A"])
    nexmod._write_profile("othergame", "p2", ["B"])
    assert len(nexmod._list_profiles("testgame"))  == 1
    assert len(nexmod._list_profiles("othergame")) == 1


# ── _apply_profile ────────────────────────────────────────────────────────────

def test_apply_writes_load_order(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    nexmod._apply_profile(mod_dir, LOF, ["ModA", "ModB", "ModC"])
    lines = (mod_dir / LOF).read_text().splitlines()
    folders = [l for l in lines if not l.startswith("--")]
    assert folders == ["ModA", "ModB", "ModC"]


def test_apply_writes_nexmod_header(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    nexmod._apply_profile(mod_dir, LOF, ["ModA"])
    first_line = (mod_dir / LOF).read_text().splitlines()[0]
    assert first_line.startswith("-- File managed by nexmod")


def test_apply_overwrites_existing(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("OldMod\n")
    nexmod._apply_profile(mod_dir, LOF, ["NewMod"])
    folders = [l for l in (mod_dir / LOF).read_text().splitlines() if not l.startswith("--")]
    assert folders == ["NewMod"]
    assert "OldMod" not in (mod_dir / LOF).read_text()


# ── _read_lof_folders ─────────────────────────────────────────────────────────

def test_read_lof_folders_strips_comments(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("-- header\nModA\nModB\n-- another comment\nModC\n")
    assert nexmod._read_lof_folders(mod_dir, LOF) == ["ModA", "ModB", "ModC"]


def test_read_lof_folders_missing_file(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    assert nexmod._read_lof_folders(mod_dir, LOF) == []


# ── _archive_basename ─────────────────────────────────────────────────────────

def test_archive_basename_strips_zip():
    assert nexmod._archive_basename("MyMod-1-2-3.zip") == "MyMod-1-2-3"


def test_archive_basename_strips_tar_gz():
    assert nexmod._archive_basename("MyMod.tar.gz") == "MyMod"


def test_archive_basename_strips_7z():
    assert nexmod._archive_basename("ScriptMod-final.7z") == "ScriptMod-final"


def test_archive_basename_unknown_extension_falls_back():
    assert nexmod._archive_basename("weird.xyz") == "weird"


# ── profile load --install ────────────────────────────────────────────────────

def test_profile_load_install_missing(tmp_path, monkeypatch, runner, api_key_config):
    """--install should call do_install for missing profile mods using
    the mod_id we already stored from a prior install/scan."""
    from nexmod import cli

    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "OnDisk").mkdir()  # one mod that exists; one missing

    # DB row whose filename strips to "MissingMod" — this is how
    # profile_load --install recovers the mod_id from a folder name.
    db = nexmod.get_db()
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, filename, mod_dir, tracked_at)
        VALUES ('darktide', 555, 1, 'Missing Mod', 'MissingMod.zip', ?, '2026-01-01')
    """, (str(mod_dir),))
    db.commit()

    # Save a profile that references both folders.
    monkeypatch.setattr(nexmod, "PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("darktide", "p", ["OnDisk", "MissingMod"])

    monkeypatch.setattr(nexmod, "resolve_mod_dir", lambda g, d: mod_dir)

    calls: list[tuple] = []
    def fake_install(game, mod_id, file_id, api_key, db):
        calls.append((game, mod_id, file_id))
        # Simulate the install creating the folder so missing→present.
        (mod_dir / "MissingMod").mkdir(exist_ok=True)
        return ("Missing Mod", "1.0")
    monkeypatch.setattr(nexmod, "do_install", fake_install)

    result = runner.invoke(cli, ["profile", "load", "darktide", "p", "--install"],
                           catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert calls == [("darktide", 555, None)], f"unexpected install calls: {calls}"
    # Profile applied and load order written.
    lof_lines = (mod_dir / LOF).read_text().splitlines()
    folders = [l for l in lof_lines if not l.startswith("--")]
    assert folders == ["OnDisk", "MissingMod"]


def test_profile_load_install_skips_unknown_folder(tmp_path, monkeypatch, runner, api_key_config):
    """A folder with no DB record should be skipped with a warning, not crash."""
    from nexmod import cli

    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    monkeypatch.setattr(nexmod, "PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("darktide", "p", ["GhostMod"])  # no matching DB row
    monkeypatch.setattr(nexmod, "resolve_mod_dir", lambda g, d: mod_dir)

    called = False
    def fake_install(*a, **kw):
        nonlocal called
        called = True
        return ("x", "1")
    monkeypatch.setattr(nexmod, "do_install", fake_install)

    result = runner.invoke(cli, ["profile", "load", "darktide", "p", "--install"],
                           catch_exceptions=False)

    assert result.exit_code == 0
    assert not called  # nothing to install — no DB record for GhostMod
    assert "could not be auto-installed" in result.output


def test_profile_load_install_and_dry_run_mutually_exclusive(tmp_path, monkeypatch, runner):
    from nexmod import cli
    monkeypatch.setattr(nexmod, "PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("darktide", "p", ["X"])
    result = runner.invoke(cli, ["profile", "load", "darktide", "p",
                                 "--install", "--dry-run"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
