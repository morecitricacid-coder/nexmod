"""
Tests for Phase D — ergonomics:
  - nexmod doctor pre-flight checks
  - install --dry-run path
"""
import json
import shutil
import pytest
import responses as resp_lib
from unittest.mock import patch
from pathlib import Path
from click.testing import CliRunner
import nexmod
from nexmod import cli

pytestmark = pytest.mark.usefixtures("isolated_dirs")


@pytest.fixture
def runner():
    return CliRunner()


# ── doctor ───────────────────────────────────────────────────────────────────

def test_doctor_no_api_key_fails(runner):
    """Without an API key, doctor should report and exit 1."""
    with patch("nexmod.find_steam_library_paths", return_value=[]):
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "API key configured" in result.output


def test_doctor_with_valid_key_passes(runner):
    """API validate succeeds + Premium = pass."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "OK"}))
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, f"{nexmod.NEXUS_API}/users/validate.json",
                 json={"name": "tester", "is_premium": True}, status=200)
        with patch("nexmod.find_steam_library_paths", return_value=[]):
            result = runner.invoke(cli, ["doctor"])
    assert "user: tester" in result.output
    assert "Premium account" in result.output


def test_doctor_403_invalid_key(runner):
    """Invalid key → validate returns 403 → doctor exits 1."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "BAD"}))
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, f"{nexmod.NEXUS_API}/users/validate.json",
                 status=403, body="forbidden")
        with patch("nexmod.find_steam_library_paths", return_value=[]):
            result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "API key valid" in result.output


def test_doctor_filter_to_one_game(runner):
    """--game flag limits per-game checks to that slug."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "OK"}))
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, f"{nexmod.NEXUS_API}/users/validate.json",
                 json={"name": "x", "is_premium": True}, status=200)
        with patch("nexmod.find_steam_library_paths", return_value=[]), \
             patch("nexmod.find_game_install", return_value=None):
            result = runner.invoke(cli, ["doctor", "--game", "darktide"])
    assert "darktide" in result.output
    assert "skyrimse" not in result.output


def test_doctor_warns_on_legacy_rows(tmp_path, runner):
    """fsck-able rows in the DB should surface as a warning, not a failure."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "OK"}))
    fake_install = tmp_path / "fakegame"
    (fake_install / "mods").mkdir(parents=True)

    db = nexmod.get_db()
    db.execute("""
        INSERT INTO mods (game, mod_id, file_id, name, version, filename,
                          mod_dir, folder_name, tracked_at, updated_at)
        VALUES ('darktide', 1, 1, 'X', '1.0', 'x.zip', '/tmp', NULL, ?, ?)
    """, (nexmod.now_iso(), nexmod.now_iso()))
    db.commit()
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, f"{nexmod.NEXUS_API}/users/validate.json",
                 json={"name": "x", "is_premium": True}, status=200)
        # All required checks pass — only warnings remain
        with patch("nexmod.find_steam_library_paths", return_value=[tmp_path]), \
             patch("nexmod.find_game_install", return_value=fake_install):
            result = runner.invoke(cli, ["doctor", "--game", "darktide"])
    assert result.exit_code == 0
    assert "fsck" in result.output
    assert "warning" in result.output.lower()


# ── install --dry-run ────────────────────────────────────────────────────────

def test_install_dry_run_does_not_download(runner):
    """--dry-run resolves metadata but skips download/extract entirely."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "OK"}))
    api = nexmod.NEXUS_API
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, f"{api}/games/warhammer40kdarktide/mods/1.json",
                 json={"name": "TestMod", "version": "1.0", "author": "x"})
        rsps.add(resp_lib.GET, f"{api}/games/warhammer40kdarktide/mods/1/files.json",
                 json={"files": [
                     {"file_id": 99, "file_name": "test.zip",
                      "category_name": "MAIN", "size_kb": 100,
                      "uploaded_timestamp": 1, "md5": "x"}
                 ]})
        # Critically: no download_link.json mock — would fail if dry-run hit it
        with patch("nexmod.resolve_mod_dir", return_value=Path("/tmp/mods")):
            result = runner.invoke(cli, ["install", "darktide", "1", "--dry-run"])
    assert result.exit_code == 0
    assert "(dry-run)" in result.output
    assert "TestMod" in result.output
    assert "test.zip" in result.output


def test_install_dry_run_warns_missing_wine_for_darktide(runner):
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "OK"}))
    api = nexmod.NEXUS_API
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, f"{api}/games/warhammer40kdarktide/mods/1.json",
                 json={"name": "TestMod", "version": "1.0"})
        rsps.add(resp_lib.GET, f"{api}/games/warhammer40kdarktide/mods/1/files.json",
                 json={"files": [
                     {"file_id": 99, "file_name": "test.zip",
                      "category_name": "MAIN", "size_kb": 100,
                      "uploaded_timestamp": 1}
                 ]})
        with patch("nexmod.resolve_mod_dir", return_value=Path("/tmp/mods")), \
             patch("nexmod.shutil.which", return_value=None):
            result = runner.invoke(cli, ["install", "darktide", "1", "--dry-run"])
    assert result.exit_code == 0
    assert "wine not found" in result.output.lower()
