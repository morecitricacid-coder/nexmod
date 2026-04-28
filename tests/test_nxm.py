"""
Tests for Phase E1 — NXM link handler:
  - _parse_nxm_uri
  - nxm-register / nxm-unregister .desktop file lifecycle
  - nxm <uri> dispatch (parses URI, calls do_install)
"""
import pytest
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import nexmod
from nexmod import cli

pytestmark = pytest.mark.usefixtures("isolated_dirs")


@pytest.fixture
def runner():
    return CliRunner()


# ── _parse_nxm_uri ───────────────────────────────────────────────────────────

def test_parse_nxm_basic():
    p = nexmod._parse_nxm_uri(
        "nxm://warhammer40kdarktide/mods/1234/files/5678"
    )
    assert p == {
        "domain": "warhammer40kdarktide",
        "mod_id": 1234,
        "file_id": 5678,
        "key": None, "expires": None, "user_id": None,
    }


def test_parse_nxm_with_query():
    p = nexmod._parse_nxm_uri(
        "nxm://skyrimspecialedition/mods/1/files/2"
        "?key=abc123&expires=1700000000&user_id=99"
    )
    assert p["domain"] == "skyrimspecialedition"
    assert p["mod_id"] == 1
    assert p["file_id"] == 2
    assert p["key"] == "abc123"
    assert p["expires"] == "1700000000"
    assert p["user_id"] == "99"


def test_parse_nxm_wrong_scheme():
    with pytest.raises(ValueError, match="not an nxm"):
        nexmod._parse_nxm_uri("https://www.nexusmods.com/mods/1234")


def test_parse_nxm_malformed_path():
    with pytest.raises(ValueError, match="unexpected NXM path"):
        nexmod._parse_nxm_uri("nxm://game/something/weird")


def test_parse_nxm_non_integer_ids():
    with pytest.raises(ValueError, match="not integers"):
        nexmod._parse_nxm_uri("nxm://game/mods/abc/files/def")


# ── nxm-register / nxm-unregister ────────────────────────────────────────────

def test_nxm_register_writes_desktop_file(tmp_path, runner, monkeypatch):
    """register should write the .desktop file with MimeType=x-scheme-handler/nxm."""
    desktop_path = tmp_path / "nexmod-nxm.desktop"
    monkeypatch.setattr(nexmod, "NXM_DESKTOP_PATH", desktop_path)

    # Stub out the system commands; we only care that the file lands.
    with patch("nexmod.subprocess.run"):
        result = runner.invoke(cli, ["nxm-register"])

    assert result.exit_code == 0
    assert desktop_path.exists()
    content = desktop_path.read_text()
    assert "MimeType=x-scheme-handler/nxm;" in content
    assert "Exec=" in content


def test_nxm_unregister_removes_file(tmp_path, runner, monkeypatch):
    desktop_path = tmp_path / "nexmod-nxm.desktop"
    desktop_path.write_text("[Desktop Entry]\n")
    monkeypatch.setattr(nexmod, "NXM_DESKTOP_PATH", desktop_path)

    with patch("nexmod.subprocess.run"):
        result = runner.invoke(cli, ["nxm-unregister"])
    assert result.exit_code == 0
    assert not desktop_path.exists()


def test_nxm_unregister_when_not_registered(tmp_path, runner, monkeypatch):
    """Unregistering when no .desktop file exists should report cleanly, not crash."""
    desktop_path = tmp_path / "nope.desktop"
    monkeypatch.setattr(nexmod, "NXM_DESKTOP_PATH", desktop_path)
    result = runner.invoke(cli, ["nxm-unregister"])
    assert result.exit_code == 0
    assert "No registration found" in result.output


# ── nxm <uri> dispatch ───────────────────────────────────────────────────────

def test_nxm_dispatches_to_do_install(runner, monkeypatch):
    """nxm command should parse URI and call do_install with parsed args."""
    import json as _json
    nexmod.CONFIG_FILE.write_text(_json.dumps({"api_key": "OK"}))
    captured = {}

    def fake_install(game, mod_id, file_id, api_key, db, *,
                     nxm_key=None, nxm_expires=None, nxm_user_id=None, from_file=None):
        captured["game"]    = game
        captured["mod_id"]  = mod_id
        captured["file_id"] = file_id
        return ("FakeMod", "1.0")

    monkeypatch.setattr(nexmod, "do_install", fake_install)

    result = runner.invoke(
        cli, ["nxm", "nxm://warhammer40kdarktide/mods/1234/files/5678?key=x&expires=1"]
    )
    assert result.exit_code == 0
    assert captured == {"game": "darktide", "mod_id": 1234, "file_id": 5678}
    assert "FakeMod" in result.output


def test_nxm_invalid_uri_exits(runner):
    result = runner.invoke(cli, ["nxm", "https://google.com"])
    assert result.exit_code == 1
    assert "Invalid NXM URI" in result.output
