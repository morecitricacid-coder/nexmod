"""
Tests for migration framework, infer_folder_name, and the `nexmod fsck` command.
"""
import json
import sqlite3
import pytest
import responses as resp_lib
from pathlib import Path
from click.testing import CliRunner
from unittest.mock import patch
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


# ── fsck --scan ───────────────────────────────────────────────────────────────

def _setup_scan_env(tmp_path):
    """Register darktide mod dir and write API key."""
    mod_dir = tmp_path / "darktide" / "mods"
    mod_dir.mkdir(parents=True)
    db = nexmod.get_db()
    db.execute(
        "INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)",
        ("darktide", str(mod_dir)),
    )
    db.commit()
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    return mod_dir


def test_fsck_scan_no_untracked_folders(tmp_path, runner):
    """--scan with no untracked folders reports all-clear."""
    mod_dir = _setup_scan_env(tmp_path)
    # Create a folder and track it
    (mod_dir / "TrackedMod").mkdir()
    db = nexmod.get_db()
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, version, filename,
                          mod_dir, folder_name, tracked_at, updated_at)
        VALUES ('darktide', 1, 1, 'Tracked', '1.0', 'f.zip', ?, 'TrackedMod', ?, ?)
    """, (str(mod_dir), nexmod.now_iso(), nexmod.now_iso()))
    db.commit()

    result = runner.invoke(cli, ["fsck", "darktide", "--scan"])
    assert result.exit_code == 0
    assert "no untracked" in result.output.lower() or "0 untracked" in result.output.lower() \
           or "✓" in result.output


def test_fsck_scan_requires_game_argument(runner):
    """--scan without a GAME argument prints an error."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    result = runner.invoke(cli, ["fsck", "--scan"])
    assert result.exit_code == 0
    assert "game" in result.output.lower() or "requires" in result.output.lower()


@resp_lib.activate
def test_fsck_scan_skip_unknown_folder(tmp_path, runner):
    """--scan with an untracked folder; user skips it."""
    mod_dir = _setup_scan_env(tmp_path)
    (mod_dir / "UnknownMod").mkdir()

    # GraphQL search returns empty
    resp_lib.add(
        resp_lib.POST,
        "https://api.nexusmods.com/v2/graphql",
        json={"data": {"mods": {"totalCount": 0, "nodes": []}}},
        status=200,
    )

    result = runner.invoke(cli, ["fsck", "darktide", "--scan"], input="s\n")
    assert result.exit_code == 0
    assert "skipped" in result.output.lower() or "1 skipped" in result.output.lower()

    # DB should still be empty
    db = nexmod.get_db()
    count = db.execute("SELECT COUNT(*) FROM mods WHERE game='darktide'").fetchone()[0]
    assert count == 0


@resp_lib.activate
def test_fsck_scan_track_by_mod_id(tmp_path, runner):
    """--scan with an untracked folder; user enters a mod ID to track it."""
    mod_dir = _setup_scan_env(tmp_path)
    (mod_dir / "CoolMod").mkdir()
    api = nexmod.NEXUS_API
    domain = "warhammer40kdarktide"

    # GraphQL search returns one match
    resp_lib.add(
        resp_lib.POST,
        "https://api.nexusmods.com/v2/graphql",
        json={"data": {"mods": {"totalCount": 1, "nodes": [
            {"modId": 42, "name": "CoolMod", "summary": "", "downloads": 100, "endorsements": 50}
        ]}}},
        status=200,
    )
    # mod info + files
    resp_lib.add(resp_lib.GET, f"{api}/games/{domain}/mods/42.json",
                 json={"name": "CoolMod", "version": "1.0", "author": "x"})
    resp_lib.add(resp_lib.GET, f"{api}/games/{domain}/mods/42/files.json",
                 json={"files": [{"file_id": 99, "file_name": "CoolMod.zip",
                                  "category_name": "MAIN", "size_kb": 10,
                                  "uploaded_timestamp": 1}]})

    # User picks option "1" from the list
    result = runner.invoke(cli, ["fsck", "darktide", "--scan"], input="1\n")
    assert result.exit_code == 0

    db = nexmod.get_db()
    row = db.execute(
        "SELECT * FROM mods WHERE game='darktide' AND mod_id=42"
    ).fetchone()
    assert row is not None
    assert row["folder_name"] == "CoolMod"


def test_fsck_scan_mod_dir_missing(tmp_path, runner):
    """--scan reports clearly when mod directory does not exist."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    # Register a path that doesn't exist
    db = nexmod.get_db()
    db.execute(
        "INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)",
        ("darktide", str(tmp_path / "nonexistent")),
    )
    db.commit()
    result = runner.invoke(cli, ["fsck", "darktide", "--scan"])
    assert result.exit_code == 0
    assert "does not exist" in result.output.lower() or "not exist" in result.output.lower()


# ── fsck --fix --with-api (installed_files backfill) ─────────────────────────

def _seed_flat_orphan(mod_dir: Path, **overrides):
    """Insert a flat-layout legacy row: folder_name=NULL, installed_files=NULL."""
    db = nexmod.get_db()
    fields = {
        "game": "starfield", "mod_id": 100, "file_id": 200,
        "name": "Better Loading", "version": "1.0",
        "filename": "BetterLoading-100-200-1.0.zip",
        "mod_dir": str(mod_dir),
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


_FAKE_FILE_LIST = ["Data/BetterLoading.esm", "Data/BetterLoading.ba2"]
_FAKE_NEXUS_FILES = [{"file_id": 200, "file_name": "BetterLoading-100-200-1.0.zip",
                      "category_name": "MAIN", "size_kb": 512,
                      "uploaded_timestamp": 1}]
_FAKE_DOWNLOAD_URLS = [{"URI": "https://cdn.example.com/BetterLoading.zip",
                        "short_name": "cdn1"}]


def test_fsck_with_api_backfills_installed_files(tmp_path, runner):
    """--fix --with-api downloads archive + writes installed_files for flat-layout mods."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    mod_dir = tmp_path / "starfield" / "Data"
    mod_dir.mkdir(parents=True)
    _seed_flat_orphan(mod_dir)

    with (
        patch("nexmod.api_mod_files", return_value=_FAKE_NEXUS_FILES),
        patch("nexmod.api_download_urls", return_value=_FAKE_DOWNLOAD_URLS),
        patch("nexmod._try_download_with_mirrors"),
        patch("nexmod._list_archive_files", return_value=_FAKE_FILE_LIST),
    ):
        result = runner.invoke(cli, ["fsck", "starfield", "--fix", "--with-api"])

    assert result.exit_code == 0
    assert "installed_files" in result.output

    db = nexmod.get_db()
    row = db.execute(
        "SELECT installed_files FROM mods WHERE game='starfield' AND mod_id=100"
    ).fetchone()
    assert row is not None
    assert row["installed_files"] is not None
    stored = json.loads(row["installed_files"])
    assert stored == _FAKE_FILE_LIST


def test_fsck_with_api_skips_no_premium(tmp_path, runner):
    """--fix --with-api skips mods when Premium is required (api_download_urls exits 1)."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    mod_dir = tmp_path / "starfield" / "Data"
    mod_dir.mkdir(parents=True)
    _seed_flat_orphan(mod_dir)

    def _download_urls_raises(*args, **kwargs):
        raise SystemExit(1)

    with (
        patch("nexmod.api_mod_files", return_value=_FAKE_NEXUS_FILES),
        patch("nexmod.api_download_urls", side_effect=_download_urls_raises),
        patch("nexmod._list_archive_files") as mock_list,
    ):
        result = runner.invoke(cli, ["fsck", "starfield", "--fix", "--with-api"])

    # _list_archive_files must NOT be called (no download happened)
    mock_list.assert_not_called()
    assert result.exit_code == 0
    # Should mention Premium skip
    assert "Premium" in result.output or "premium" in result.output.lower()

    # installed_files should still be NULL
    db = nexmod.get_db()
    row = db.execute(
        "SELECT installed_files FROM mods WHERE game='starfield' AND mod_id=100"
    ).fetchone()
    assert row["installed_files"] is None


def test_fsck_with_api_skips_already_populated(tmp_path, runner):
    """--fix --with-api does not re-download mods that already have installed_files."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    mod_dir = tmp_path / "starfield" / "Data"
    mod_dir.mkdir(parents=True)
    _seed_flat_orphan(mod_dir)

    # Manually set installed_files on the row before invoking fsck
    db = nexmod.get_db()
    existing = json.dumps(["Data/Existing.esm"])
    db.execute(
        "UPDATE mods SET installed_files = ? WHERE game='starfield' AND mod_id=100",
        (existing,),
    )
    db.commit()
    db.close()

    with (
        patch("nexmod.api_mod_files") as mock_files,
        patch("nexmod.api_download_urls") as mock_urls,
        patch("nexmod._try_download_with_mirrors") as mock_dl,
    ):
        result = runner.invoke(cli, ["fsck", "starfield", "--fix", "--with-api"])

    # Row already has installed_files — should not hit API at all
    mock_files.assert_not_called()
    mock_urls.assert_not_called()
    mock_dl.assert_not_called()
    assert result.exit_code == 0


def test_fsck_with_api_skips_row_with_folder_name(tmp_path, runner):
    """--fix --with-api does not touch mods that already have folder_name."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    mod_dir = tmp_path / "starfield" / "Data"
    mod_dir.mkdir(parents=True)
    # Insert a row WITH folder_name (not a flat-layout orphan)
    _seed_flat_orphan(mod_dir, folder_name="BetterLoadingFolder")

    with (
        patch("nexmod.api_mod_files") as mock_files,
        patch("nexmod.api_download_urls") as mock_urls,
        patch("nexmod._try_download_with_mirrors") as mock_dl,
    ):
        result = runner.invoke(cli, ["fsck", "starfield", "--fix", "--with-api"])

    mock_files.assert_not_called()
    mock_urls.assert_not_called()
    mock_dl.assert_not_called()
    assert result.exit_code == 0


def test_fsck_with_api_empty_manifest_skipped(tmp_path, runner):
    """--fix --with-api skips mods whose archive yields an empty file list."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    mod_dir = tmp_path / "starfield" / "Data"
    mod_dir.mkdir(parents=True)
    _seed_flat_orphan(mod_dir)

    with (
        patch("nexmod.api_mod_files", return_value=_FAKE_NEXUS_FILES),
        patch("nexmod.api_download_urls", return_value=_FAKE_DOWNLOAD_URLS),
        patch("nexmod._try_download_with_mirrors"),
        patch("nexmod._list_archive_files", return_value=[]),
    ):
        result = runner.invoke(cli, ["fsck", "starfield", "--fix", "--with-api"])

    assert result.exit_code == 0
    # installed_files must remain NULL
    db = nexmod.get_db()
    row = db.execute(
        "SELECT installed_files FROM mods WHERE game='starfield' AND mod_id=100"
    ).fetchone()
    assert row["installed_files"] is None


def test_fsck_with_api_no_flat_orphans_no_download(tmp_path, runner):
    """--fix --with-api when all mods have folder_name — no archive download triggered."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    mod_dir = tmp_path / "darktide" / "mods"
    (mod_dir / "HubHotkeys").mkdir(parents=True)
    _seed_legacy_row(mod_dir, folder_name="HubHotkeys")

    with (
        patch("nexmod.api_mod_files") as mock_files,
        patch("nexmod.api_download_urls") as mock_urls,
    ):
        result = runner.invoke(cli, ["fsck", "darktide", "--fix", "--with-api"])

    mock_files.assert_not_called()
    mock_urls.assert_not_called()
    assert result.exit_code == 0


def test_fsck_with_api_api_error_skipped(tmp_path, runner):
    """--fix --with-api gracefully skips mods when api_mod_files returns an error."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    mod_dir = tmp_path / "starfield" / "Data"
    mod_dir.mkdir(parents=True)
    _seed_flat_orphan(mod_dir)

    with (
        patch("nexmod.api_mod_files", side_effect=SystemExit(1)),
        patch("nexmod._try_download_with_mirrors") as mock_dl,
    ):
        result = runner.invoke(cli, ["fsck", "starfield", "--fix", "--with-api"])

    mock_dl.assert_not_called()
    assert result.exit_code == 0
    # installed_files still NULL
    db = nexmod.get_db()
    row = db.execute(
        "SELECT installed_files FROM mods WHERE game='starfield' AND mod_id=100"
    ).fetchone()
    assert row["installed_files"] is None


def test_fsck_with_api_multiple_mods_partial_success(tmp_path, runner):
    """--fix --with-api backfills some mods and skips others in same run."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
    mod_dir = tmp_path / "starfield" / "Data"
    mod_dir.mkdir(parents=True)
    # Two flat-orphan rows
    _seed_flat_orphan(mod_dir, mod_id=100, file_id=200, name="ModA")
    _seed_flat_orphan(mod_dir, mod_id=101, file_id=201, name="ModB")

    call_count = {"n": 0}

    def _files_side_effect(domain, mod_id, api_key):
        call_count["n"] += 1
        if mod_id == 101:
            raise SystemExit(1)  # simulate 404 for ModB
        return [{"file_id": 200, "file_name": "ModA.zip",
                 "category_name": "MAIN", "size_kb": 1, "uploaded_timestamp": 1}]

    with (
        patch("nexmod.api_mod_files", side_effect=_files_side_effect),
        patch("nexmod.api_download_urls", return_value=_FAKE_DOWNLOAD_URLS),
        patch("nexmod._try_download_with_mirrors"),
        patch("nexmod._list_archive_files", return_value=["Data/ModA.esm"]),
    ):
        result = runner.invoke(cli, ["fsck", "starfield", "--fix", "--with-api"])

    assert result.exit_code == 0
    db = nexmod.get_db()
    row_a = db.execute(
        "SELECT installed_files FROM mods WHERE game='starfield' AND mod_id=100"
    ).fetchone()
    row_b = db.execute(
        "SELECT installed_files FROM mods WHERE game='starfield' AND mod_id=101"
    ).fetchone()
    assert row_a["installed_files"] is not None  # ModA backfilled
    assert row_b["installed_files"] is None       # ModB skipped
