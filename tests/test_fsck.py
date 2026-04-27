"""
Tests for migration framework, infer_folder_name, and the `nexmod fsck` command.
"""
import json
import sqlite3
import pytest
from pathlib import Path
from click.testing import CliRunner
import nexmod
from nexmod import cli

pytestmark = pytest.mark.usefixtures("isolated_dirs")


# ── _apply_migrations ────────────────────────────────────────────────────────

def test_migrations_apply_on_fresh_db():
    db = nexmod.get_db()
    rows = db.execute("SELECT name FROM schema_migrations").fetchall()
    names = {r["name"] for r in rows}
    assert "001_mods_folder_name" in names


def test_migrations_idempotent():
    nexmod.get_db().close()
    nexmod.get_db().close()
    db = nexmod.get_db()
    cnt = db.execute(
        "SELECT COUNT(*) AS c FROM schema_migrations WHERE name='001_mods_folder_name'"
    ).fetchone()["c"]
    assert cnt == 1


def test_migrations_handle_legacy_schema(tmp_path, monkeypatch):
    """Pre-existing schemas with the column already added must still be recorded."""
    # Build a "legacy" DB that has the column but no schema_migrations row
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(nexmod, "DB_FILE", db_path)

    raw = sqlite3.connect(db_path)
    raw.executescript("""
        CREATE TABLE mods (
            id INTEGER PRIMARY KEY,
            game TEXT, mod_id INTEGER, file_id INTEGER, name TEXT,
            version TEXT, filename TEXT, mod_dir TEXT,
            tracked_at TEXT, updated_at TEXT, folder_name TEXT
        );
    """)
    raw.commit()
    raw.close()

    # First get_db() should not blow up on the duplicate-column ALTER
    db = nexmod.get_db()
    names = {r["name"] for r in db.execute("SELECT name FROM schema_migrations").fetchall()}
    assert "001_mods_folder_name" in names


# ── infer_folder_name ────────────────────────────────────────────────────────

def _row(filename=None, name="MyMod"):
    return {"filename": filename, "name": name}


def _mkdir(p: Path, **files):
    p.mkdir(parents=True, exist_ok=True)
    for fname, content in files.items():
        (p / fname).write_text(content)


def test_infer_filename_stem_match(tmp_path):
    (tmp_path / "CustomHUD-10-2.1.3-1.zip").touch()  # decoy
    _mkdir(tmp_path / "CustomHUD-10-2.1.3-1")
    folder, cands, strat = nexmod.infer_folder_name(
        tmp_path, _row(filename="CustomHUD-10-2.1.3-1.zip", name="Custom HUD")
    )
    assert folder == "CustomHUD-10-2.1.3-1"
    assert strat == "filename_stem"


def test_infer_mod_json_name(tmp_path):
    _mkdir(tmp_path / "decoy")
    _mkdir(tmp_path / "real_folder",
           **{"mod.json": json.dumps({"name": "Custom HUD"})})
    folder, _, strat = nexmod.infer_folder_name(
        tmp_path, _row(filename="something-unrelated.zip", name="Custom HUD")
    )
    assert folder == "real_folder"
    assert strat == "mod_json_name"


def test_infer_dot_mod_title(tmp_path):
    _mkdir(tmp_path / "CustomHUD",
           **{"CustomHUD.mod": 'return { name = "Custom HUD", run = function() end }'})
    folder, _, strat = nexmod.infer_folder_name(
        tmp_path, _row(filename="x.zip", name="Custom HUD")
    )
    assert folder == "CustomHUD"
    assert strat == "dot_mod_title"


def test_infer_fuzzy_match(tmp_path):
    # "AFK" → "afk" — single dir, normalized strings match
    _mkdir(tmp_path / "afk")
    folder, _, strat = nexmod.infer_folder_name(
        tmp_path, _row(filename="x.zip", name="AFK")
    )
    assert folder == "afk"
    assert strat == "fuzzy_match"


def test_infer_no_match_returns_candidates(tmp_path):
    base = tmp_path / "mods"
    _mkdir(base / "totally_unrelated")
    _mkdir(base / "also_different")
    folder, cands, strat = nexmod.infer_folder_name(
        base, _row(filename="x.zip", name="MyMod")
    )
    assert folder is None
    assert strat == "no_match"
    assert set(cands) == {"totally_unrelated", "also_different"}


def test_infer_no_dir_returns_no_dir(tmp_path):
    folder, cands, strat = nexmod.infer_folder_name(
        tmp_path / "missing", _row(filename="x.zip")
    )
    assert folder is None
    assert strat == "no_dir"


def test_infer_empty_dir(tmp_path):
    base = tmp_path / "mods"
    base.mkdir()
    folder, _, strat = nexmod.infer_folder_name(base, _row(filename="x.zip"))
    assert folder is None
    assert strat == "empty_dir"


# ── fsck CLI ─────────────────────────────────────────────────────────────────

@pytest.fixture
def runner():
    return CliRunner()


def _seed_legacy_row(mod_dir: Path, **overrides):
    """Insert a row mimicking pre-folder_name install."""
    db = nexmod.get_db()
    fields = {
        "game": "darktide", "mod_id": 1, "file_id": 1,
        "name": "Custom HUD", "version": None,
        "filename": "CustomHUD.zip", "mod_dir": str(mod_dir),
        "folder_name": None,
    }
    fields.update(overrides)
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, version, filename,
                          mod_dir, folder_name, tracked_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        fields["game"], fields["mod_id"], fields["file_id"], fields["name"],
        fields["version"], fields["filename"], fields["mod_dir"],
        fields["folder_name"], nexmod.now_iso(), nexmod.now_iso(),
    ))
    db.commit()
    db.close()


def test_fsck_dry_run_no_changes(tmp_path, runner):
    mods = tmp_path / "mods"
    _mkdir(mods / "CustomHUD",
           **{"CustomHUD.mod": 'return { name = "Custom HUD" }'})
    _seed_legacy_row(mods)

    result = runner.invoke(cli, ["fsck", "darktide"])
    assert result.exit_code == 0
    assert "Inferred folder names" in result.output
    assert "CustomHUD" in result.output
    # Dry-run: DB still NULL
    db = nexmod.get_db()
    row = db.execute("SELECT folder_name FROM mods").fetchone()
    assert row["folder_name"] is None


def test_fsck_fix_applies(tmp_path, runner):
    mods = tmp_path / "mods"
    _mkdir(mods / "CustomHUD",
           **{"CustomHUD.mod": 'return { name = "Custom HUD" }'})
    _seed_legacy_row(mods)

    result = runner.invoke(cli, ["fsck", "darktide", "--fix"])
    assert result.exit_code == 0
    assert "Applied:" in result.output
    db = nexmod.get_db()
    row = db.execute("SELECT folder_name FROM mods").fetchone()
    assert row["folder_name"] == "CustomHUD"


def test_fsck_collision_blocks_apply(tmp_path, runner):
    """Two rows that infer to the same folder must not both be applied."""
    mods = tmp_path / "mods"
    _mkdir(mods / "Shared",
           **{"Shared.mod": 'return { name = "Shared Mod" }'})
    _seed_legacy_row(mods, mod_id=1, name="Shared Mod", filename="a.zip")
    _seed_legacy_row(mods, mod_id=2, name="Shared Mod", filename="b.zip")

    result = runner.invoke(cli, ["fsck", "darktide", "--fix"])
    assert result.exit_code == 0
    assert "collision" in result.output.lower() or "Collision" in result.output
    db = nexmod.get_db()
    rows = db.execute("SELECT folder_name FROM mods").fetchall()
    # Neither should have been applied due to collision
    assert all(r["folder_name"] is None for r in rows)


def test_fsck_orphan_mod_dir(tmp_path, runner):
    _seed_legacy_row(tmp_path / "missing_dir")
    result = runner.invoke(cli, ["fsck", "darktide"])
    assert result.exit_code == 0
    assert "Orphan" in result.output or "orphan" in result.output


def test_fsck_no_mods(runner):
    result = runner.invoke(cli, ["fsck", "darktide"])
    assert result.exit_code == 0
    assert "No mods tracked" in result.output


def test_fsck_filters_by_game(tmp_path, runner):
    mods = tmp_path / "mods"
    _mkdir(mods / "ModA")
    _seed_legacy_row(mods, game="darktide", name="ModA")
    _seed_legacy_row(mods, game="skyrimse", mod_id=2, name="ModB")
    result = runner.invoke(cli, ["fsck", "darktide"])
    assert result.exit_code == 0
    assert "scanned 1 mod" in result.output
