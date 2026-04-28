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
    assert "directives" in p  # always present, defaults to []


def test_write_stores_directives(tmp_path, monkeypatch):
    monkeypatch.setattr("nexmod.PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("testgame", "pinned", ["ModA"], directives=["-- nexmod:pin ModA top"])
    p = nexmod._read_profile("testgame", "pinned")
    assert p["directives"] == ["-- nexmod:pin ModA top"]


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


def test_profile_load_install_prefers_folder_name_column(tmp_path, monkeypatch, runner, api_key_config):
    """--install should prefer folder_name column over archive-basename fallback."""
    from nexmod import cli

    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()

    db = nexmod.get_db()
    # folder_name differs from archive basename — only folder_name is correct.
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, filename, folder_name, mod_dir, tracked_at)
        VALUES ('darktide', 777, 1, 'Custom Folder Mod', 'archive-v2.zip', 'CustomFolder', ?, '2026-01-01')
    """, (str(mod_dir),))
    db.commit()

    monkeypatch.setattr(nexmod, "PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("darktide", "p", ["CustomFolder"])
    monkeypatch.setattr(nexmod, "resolve_mod_dir", lambda g, d: mod_dir)

    calls: list[tuple] = []
    def fake_install(game, mod_id, file_id, api_key, db):
        calls.append((game, mod_id))
        (mod_dir / "CustomFolder").mkdir(exist_ok=True)
        return ("Custom Folder Mod", "2.0")
    monkeypatch.setattr(nexmod, "do_install", fake_install)

    result = runner.invoke(cli, ["profile", "load", "darktide", "p", "--install"],
                           catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert calls == [("darktide", 777)], f"expected folder_name lookup, got: {calls}"


# ── foreign entry warning on save ─────────────────────────────────────────────

def test_profile_save_warns_on_foreign_entries(tmp_path, monkeypatch, runner, api_key_config):
    """profile save should warn when folders in the LOF aren't tracked in the DB."""
    from nexmod import cli

    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "ForeignMod").mkdir()
    (mod_dir / "mod_load_order.txt").write_text("ForeignMod\n")

    monkeypatch.setattr(nexmod, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(nexmod, "resolve_mod_dir", lambda g, d: mod_dir)

    result = runner.invoke(cli, ["profile", "save", "darktide", "snapshot"],
                           catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "not tracked" in result.output


# ── auto-create default profile ───────────────────────────────────────────────

def test_profile_load_auto_creates_default(tmp_path, monkeypatch, runner, api_key_config):
    """profile load <game> (name='default') should auto-create the profile if absent."""
    from nexmod import cli

    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "ModA").mkdir()
    (mod_dir / "mod_load_order.txt").write_text("-- File managed by nexmod\nModA\n")

    monkeypatch.setattr(nexmod, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(nexmod, "resolve_mod_dir", lambda g, d: mod_dir)

    result = runner.invoke(cli, ["profile", "load", "darktide", "default"],
                           catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "Auto-created" in result.output
    default_path = tmp_path / "profiles" / "darktide" / "default.json"
    assert default_path.exists()
    p = json.loads(default_path.read_text())
    assert p["load_order"] == ["ModA"]


# ── directives stored and injected ────────────────────────────────────────────

def test_write_profile_stores_directives_from_lof(tmp_path, monkeypatch):
    monkeypatch.setattr("nexmod.PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("testgame", "pinned", ["ModA"], directives=["-- nexmod:pin ModA top"])
    p = nexmod._read_profile("testgame", "pinned")
    assert "-- nexmod:pin ModA top" in p["directives"]


def test_profile_load_injects_directive_even_when_folders_already_match(tmp_path, monkeypatch, runner, api_key_config):
    """If folders already match but saved directives are missing, reconcile must still run."""
    from nexmod import cli

    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "ModA").mkdir()
    # LOF has correct folders but no directive
    (mod_dir / "mod_load_order.txt").write_text("ModA\n")

    monkeypatch.setattr(nexmod, "PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile(
        "darktide", "pinned", ["ModA"],
        directives=["-- nexmod:pin ModA top"],
    )
    monkeypatch.setattr(nexmod, "resolve_mod_dir", lambda g, d: mod_dir)

    result = runner.invoke(cli, ["profile", "load", "darktide", "pinned"],
                           catch_exceptions=False)

    assert result.exit_code == 0, result.output
    written = (mod_dir / "mod_load_order.txt").read_text()
    assert "-- nexmod:pin ModA top" in written


def test_inject_directives_merged_by_reconciler(tmp_path):
    """reconcile_load_order should inject profile directives missing from the current LOF."""
    db = nexmod.get_db()

    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "ModA").mkdir()

    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, filename, folder_name, mod_dir, tracked_at)
        VALUES ('darktide', 1, 1, 'ModA', 'ModA.zip', 'ModA', ?, '2026-01-01')
    """, (str(mod_dir),))
    db.commit()

    (mod_dir / "mod_load_order.txt").write_text("ModA\n")

    nexmod.reconcile_load_order(
        "darktide", db, mod_dir,
        profile_set=["ModA"],
        inject_directives=["-- nexmod:pin ModA top"],
    )

    written_text = (mod_dir / "mod_load_order.txt").read_text()
    assert "-- nexmod:pin ModA top" in written_text


# ── active profile tracking ───────────────────────────────────────────────────

def test_profile_load_records_active_profile(tmp_path, monkeypatch, runner, api_key_config):
    """After profile load, active_profile in load_order_state should reflect the name."""
    from nexmod import cli

    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "ModA").mkdir()
    (mod_dir / "mod_load_order.txt").write_text("ModA\n")

    monkeypatch.setattr(nexmod, "PROFILES_DIR", tmp_path / "profiles")
    nexmod._write_profile("darktide", "combat", ["ModA"])
    monkeypatch.setattr(nexmod, "resolve_mod_dir", lambda g, d: mod_dir)

    result = runner.invoke(cli, ["profile", "load", "darktide", "combat"],
                           catch_exceptions=False)

    assert result.exit_code == 0, result.output
    db  = nexmod.get_db()
    row = db.execute(
        "SELECT active_profile FROM load_order_state WHERE game='darktide'"
    ).fetchone()
    assert row is not None
    assert row["active_profile"] == "combat"
