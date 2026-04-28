"""
Tests for `nexmod import` — install a locally-downloaded archive.

Covers: filename parsing, mod-ID detection, extraction flow, DB record,
free-tier NXM fallback.
"""
import json
import zipfile
import pytest
import responses as resp_lib
from pathlib import Path
from unittest.mock import patch, MagicMock
import nexmod
from nexmod import cli

pytestmark = pytest.mark.usefixtures("isolated_dirs")

NEXUS_API = nexmod.NEXUS_API


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_zip(path: Path, members: dict):
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)


def _api_key():
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))


def _register_mod_dir(tmp_path, game="darktide"):
    mod_dir = tmp_path / game / "mods"
    mod_dir.mkdir(parents=True)
    db = nexmod.get_db()
    db.execute(
        "INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)",
        (game, str(mod_dir)),
    )
    db.commit()
    return mod_dir


# ── _parse_nexus_filename ─────────────────────────────────────────────────────

class TestParseNexusFilename:

    def test_standard_nexus_name(self):
        mod_id, file_id = nexmod._parse_nexus_filename("CoolMod-1234-5678-1-2-3.zip")
        assert mod_id == 1234
        assert file_id == 5678

    def test_hyphenated_mod_name(self):
        mod_id, file_id = nexmod._parse_nexus_filename("My-Great-Mod-999-111-2-0.7z")
        assert mod_id == 999
        assert file_id == 111

    def test_single_version_segment(self):
        mod_id, file_id = nexmod._parse_nexus_filename("SimpleMod-42-7-1.zip")
        assert mod_id == 42
        assert file_id == 7

    def test_unrecognised_name_returns_none(self):
        mod_id, file_id = nexmod._parse_nexus_filename("random_archive.zip")
        assert mod_id is None
        assert file_id is None

    def test_plain_name_no_digits_returns_none(self):
        mod_id, file_id = nexmod._parse_nexus_filename("mymod.zip")
        assert mod_id is None
        assert file_id is None


# ── import command ────────────────────────────────────────────────────────────

class TestImportCommand:

    def _mod_api_responses(self, rsps, domain="warhammer40kdarktide", mod_id=1234):
        rsps.add(
            resp_lib.GET,
            f"{NEXUS_API}/games/{domain}/mods/{mod_id}.json",
            json={"name": "Imported Mod", "version": "2.0", "author": "Auth"},
        )
        rsps.add(
            resp_lib.GET,
            f"{NEXUS_API}/games/{domain}/mods/{mod_id}/files.json",
            json={"files": [
                {"file_id": 5678, "file_name": "CoolMod-1234-5678-2-0.zip",
                 "category_name": "MAIN", "size_kb": 50, "uploaded_timestamp": 1}
            ]},
        )

    @resp_lib.activate
    def test_import_auto_detect_mod_id(self, runner, tmp_path):
        """import detects mod ID from filename and auto-confirms with --yes."""
        _api_key()
        mod_dir = _register_mod_dir(tmp_path)
        archive = tmp_path / "CoolMod-1234-5678-2-0.zip"
        _make_zip(archive, {"CoolMod/mod.lua": "-- mod"})
        self._mod_api_responses(resp_lib)

        result = runner.invoke(cli, [
            "import", "darktide", str(archive), "--yes",
        ])
        assert result.exit_code == 0, result.output
        assert "imported" in result.output.lower() or "✓" in result.output

    @resp_lib.activate
    def test_import_creates_db_record(self, runner, tmp_path):
        """import inserts a row into the mods table."""
        _api_key()
        mod_dir = _register_mod_dir(tmp_path)
        archive = tmp_path / "CoolMod-1234-5678-2-0.zip"
        _make_zip(archive, {"CoolMod/mod.lua": "-- mod"})
        self._mod_api_responses(resp_lib)

        runner.invoke(cli, ["import", "darktide", str(archive), "--yes"])
        db = nexmod.get_db()
        row = db.execute(
            "SELECT * FROM mods WHERE game='darktide' AND mod_id=1234"
        ).fetchone()
        assert row is not None
        assert row["name"] == "Imported Mod"
        assert row["version"] == "2.0"

    @resp_lib.activate
    def test_import_extracts_files_to_mod_dir(self, runner, tmp_path):
        """import extracts archive contents into the game's mod directory."""
        _api_key()
        mod_dir = _register_mod_dir(tmp_path)
        archive = tmp_path / "CoolMod-1234-5678-2-0.zip"
        _make_zip(archive, {"CoolMod/mod.lua": "-- lua content"})
        self._mod_api_responses(resp_lib)

        runner.invoke(cli, ["import", "darktide", str(archive), "--yes"])
        assert (mod_dir / "CoolMod").is_dir()

    @resp_lib.activate
    def test_import_manual_mod_id_option(self, runner, tmp_path):
        """import --mod-id skips filename detection entirely."""
        _api_key()
        mod_dir = _register_mod_dir(tmp_path)
        # Filename with no detectable mod ID
        archive = tmp_path / "my_random_file.zip"
        _make_zip(archive, {"MyMod/mod.lua": "x"})
        self._mod_api_responses(resp_lib)  # still expects mod_id=1234

        result = runner.invoke(cli, [
            "import", "darktide", str(archive), "--mod-id", "1234",
        ])
        assert result.exit_code == 0, result.output

    @resp_lib.activate
    def test_import_records_history(self, runner, tmp_path):
        """import writes a 'install/ok' history entry."""
        _api_key()
        _register_mod_dir(tmp_path)
        archive = tmp_path / "CoolMod-1234-5678-2-0.zip"
        _make_zip(archive, {"CoolMod/mod.lua": "x"})
        self._mod_api_responses(resp_lib)

        runner.invoke(cli, ["import", "darktide", str(archive), "--yes"])
        db = nexmod.get_db()
        row = db.execute(
            "SELECT * FROM history WHERE game='darktide' AND mod_id=1234 AND status='ok'"
        ).fetchone()
        assert row is not None

    @resp_lib.activate
    def test_import_stores_folder_name(self, runner, tmp_path):
        """import records folder_name for the extracted mod dir."""
        _api_key()
        mod_dir = _register_mod_dir(tmp_path)
        archive = tmp_path / "CoolMod-1234-5678-2-0.zip"
        _make_zip(archive, {"CoolMod/mod.lua": "x"})
        self._mod_api_responses(resp_lib)

        runner.invoke(cli, ["import", "darktide", str(archive), "--yes"])
        db = nexmod.get_db()
        row = db.execute(
            "SELECT folder_name FROM mods WHERE game='darktide' AND mod_id=1234"
        ).fetchone()
        assert row["folder_name"] == "CoolMod"

    def test_import_nonexistent_file_rejected(self, runner, tmp_path):
        """import rejects a path that does not exist (click validates)."""
        _api_key()
        _register_mod_dir(tmp_path)
        result = runner.invoke(cli, ["import", "darktide", "/no/such/file.zip"])
        assert result.exit_code != 0


# ── NXM free-user fallback ────────────────────────────────────────────────────

class TestNxmFreeUserFallback:

    @resp_lib.activate
    def test_nxm_free_user_opens_browser(self, runner, tmp_path):
        """nxm command opens browser when Premium is required for download."""
        _api_key()
        _register_mod_dir(tmp_path)
        domain = "warhammer40kdarktide"
        mod_id = 999
        file_id = 123

        # Simulate do_install raising the Premium-required RuntimeError
        with patch("nexmod.do_install",
                   side_effect=RuntimeError("Nexus returned no download URLs — Premium required")), \
             patch("nexmod.webbrowser.open") as mock_open:
            result = runner.invoke(
                cli,
                ["nxm", f"nxm://{domain}/mods/{mod_id}/files/{file_id}?key=k&expires=e&user_id=u"],
            )
        assert result.exit_code == 0
        mock_open.assert_called_once()
        url_opened = mock_open.call_args[0][0]
        assert str(mod_id) in url_opened
        assert domain in url_opened

    @resp_lib.activate
    def test_nxm_other_runtime_error_propagates(self, runner, tmp_path):
        """nxm lets non-Premium RuntimeErrors propagate (not swallowed)."""
        _api_key()
        _register_mod_dir(tmp_path)
        domain = "warhammer40kdarktide"

        with patch("nexmod.do_install",
                   side_effect=RuntimeError("disk full")):
            result = runner.invoke(
                cli,
                ["nxm", f"nxm://{domain}/mods/1/files/2?key=k&expires=e&user_id=u"],
            )
        # Should propagate — exit non-zero or show the error
        assert result.exit_code != 0 or "disk" in result.output.lower() \
               or result.exception is not None
