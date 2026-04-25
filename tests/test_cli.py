"""
CLI integration tests using Click's CliRunner.

All filesystem I/O is redirected to tmp_path via the isolated_dirs fixture.
HTTP calls are mocked with responses.RequestsMock — no real network.
"""
import json
import stat
import pytest
import responses as resp_lib
from nexmod import cli
import nexmod

pytestmark = pytest.mark.usefixtures("isolated_dirs")

NEXUS = "https://api.nexusmods.com/v1"


# ── nexmod games ──────────────────────────────────────────────────────────────

def test_games_lists_all_supported(runner):
    result = runner.invoke(cli, ["games"], catch_exceptions=False)
    assert result.exit_code == 0
    for slug in ("darktide", "skyrimse", "bg3", "cyberpunk2077", "fallout4"):
        assert slug in result.output


# ── nexmod config set-key ─────────────────────────────────────────────────────

def test_config_set_key_writes_file(runner):
    result = runner.invoke(cli, ["config", "set-key", "TESTKEY12345"], catch_exceptions=False)
    assert result.exit_code == 0
    cfg = json.loads(nexmod.CONFIG_FILE.read_text())
    assert cfg["api_key"] == "TESTKEY12345"


def test_config_set_key_sets_chmod_600(runner):
    runner.invoke(cli, ["config", "set-key", "TESTKEY12345"], catch_exceptions=False)
    mode = nexmod.CONFIG_FILE.stat().st_mode
    assert not (mode & stat.S_IRGRP), "group should not have read permission"
    assert not (mode & stat.S_IROTH), "other should not have read permission"


def test_config_set_key_idempotent(runner):
    runner.invoke(cli, ["config", "set-key", "KEY_A"], catch_exceptions=False)
    runner.invoke(cli, ["config", "set-key", "KEY_B"], catch_exceptions=False)
    cfg = json.loads(nexmod.CONFIG_FILE.read_text())
    assert cfg["api_key"] == "KEY_B"


# ── nexmod config show ────────────────────────────────────────────────────────

def test_config_show_masks_key(runner, api_key_config):
    result = runner.invoke(cli, ["config", "show"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "FAKEKEY000" not in result.output  # full key not printed verbatim
    assert "..." in result.output              # ellipsis confirms masking is present


def test_config_show_no_key_message(runner):
    result = runner.invoke(cli, ["config", "show"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No API key" in result.output


# ── nexmod config verify ──────────────────────────────────────────────────────

def test_config_verify_premium(runner, api_key_config):
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, f"{NEXUS}/users/validate.json",
                 json={"name": "TestUser", "is_premium": True, "is_supporter": False})
        result = runner.invoke(cli, ["config", "verify"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "TestUser" in result.output
    assert "YES" in result.output


def test_config_verify_no_premium_warns(runner, api_key_config):
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, f"{NEXUS}/users/validate.json",
                 json={"name": "FreeUser", "is_premium": False, "is_supporter": False})
        result = runner.invoke(cli, ["config", "verify"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "NO" in result.output or "Warning" in result.output


# ── nexmod list ───────────────────────────────────────────────────────────────

def test_list_empty_game(runner):
    result = runner.invoke(cli, ["list", "darktide"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No mods tracked" in result.output


# ── nexmod check (no API key) ─────────────────────────────────────────────────

def test_check_exits_without_api_key(runner):
    result = runner.invoke(cli, ["check", "darktide"])
    assert result.exit_code == 1
    assert "No API key" in result.output


def test_install_exits_without_api_key(runner):
    result = runner.invoke(cli, ["install", "darktide", "12345"])
    assert result.exit_code == 1
    assert "No API key" in result.output


# ── nexmod history ────────────────────────────────────────────────────────────

def test_history_empty(runner):
    result = runner.invoke(cli, ["history"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No history" in result.output


def test_history_records_install(runner, api_key_config, tmp_path, monkeypatch):
    db = nexmod.get_db()
    nexmod.record(db, "install", "darktide", 999, "Test Mod", "1.0", "ok")
    result = runner.invoke(cli, ["history"], catch_exceptions=False)
    assert "install" in result.output
    assert "Test Mod" in result.output


# ── nexmod scan (no manifest) ─────────────────────────────────────────────────

def test_scan_no_manifest_warns(runner, api_key_config, tmp_path, monkeypatch):
    monkeypatch.setattr(nexmod, "resolve_mod_dir",
                        lambda game, db: tmp_path / "mods")
    (tmp_path / "mods").mkdir(parents=True, exist_ok=True)
    result = runner.invoke(cli, ["scan", "darktide"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "vortex.deployment.json" in result.output or "No vortex" in result.output


# ── nexmod logs ───────────────────────────────────────────────────────────────

def test_logs_empty_does_not_crash(runner):
    # setup_logging() runs before the command body and creates the log file via
    # RotatingFileHandler (open mode='a'). So LOG_FILE always exists by the time
    # the logs command body runs — the "No log file" branch is unreachable via CLI.
    # Verify the command at least exits cleanly with an empty log.
    result = runner.invoke(cli, ["logs"], catch_exceptions=False)
    assert result.exit_code == 0


def test_logs_shows_content(runner, api_key_config):
    nexmod.LOG_FILE.write_text("2026-01-01 INFO  some operation\n2026-01-01 ERROR something broke\n")
    result = runner.invoke(cli, ["logs"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "some operation" in result.output


def test_logs_errors_only_filter(runner):
    nexmod.LOG_FILE.write_text(
        "2026-01-01 INFO  routine\n"
        "2026-01-01 ERROR bad thing\n"
        "2026-01-01 WARNING suspicious\n"
    )
    result = runner.invoke(cli, ["logs", "--errors"], catch_exceptions=False)
    assert "routine" not in result.output
    assert "bad thing" in result.output
    assert "suspicious" in result.output
