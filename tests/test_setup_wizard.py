"""Tests for the `nexmod setup` wizard command."""
import json
import pytest
from click.testing import CliRunner
from nexmod import cli
import nexmod

pytestmark = pytest.mark.usefixtures("isolated_dirs")


# ── Non-interactive mode exits 1 ──────────────────────────────────────────────

def test_setup_non_interactive_exits_1(runner, monkeypatch):
    monkeypatch.setattr(nexmod, "_is_interactive", lambda: False)
    result = runner.invoke(cli, ["setup"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "setup requires a terminal" in result.output


# ── Fresh run with known game found by Steam ──────────────────────────────────

def test_setup_steam_found_registers_game(tmp_path, monkeypatch):
    monkeypatch.setattr(nexmod, "_is_interactive", lambda: True)

    # Stub Steam detection to return a fake path for darktide only
    fake_game_path = tmp_path / "steam" / "darktide"
    fake_game_path.mkdir(parents=True)

    def fake_find_game_install(steam_id):
        # darktide steam_id from GAMES dict
        if steam_id == nexmod.GAMES["darktide"]["steam_id"]:
            return fake_game_path
        return None

    monkeypatch.setattr(nexmod, "find_game_install", fake_find_game_install)

    # Suppress doctor invocation so we don't need a full env
    monkeypatch.setattr(nexmod.doctor, "callback", lambda game=None: None)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["setup", "--game", "darktide"],
        # API key + confirm "Manage it with nexmod?" + decline dtkit download
        input="TESTKEY1234\ny\nn\n",
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    db = nexmod.get_db()
    row = db.execute("SELECT path FROM game_paths WHERE game='darktide'").fetchone()
    assert row is not None
    expected_subdir = nexmod.GAMES["darktide"]["mod_subdir"]
    assert expected_subdir in row["path"]


# ── Re-run shows "already registered", no second DB write ────────────────────

def test_setup_already_registered_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(nexmod, "_is_interactive", lambda: True)
    monkeypatch.setattr(nexmod, "find_game_install", lambda _: None)
    monkeypatch.setattr(nexmod.doctor, "callback", lambda game=None: None)

    # Pre-register darktide
    db = nexmod.get_db()
    db.execute(
        "INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)",
        ("darktide", "/some/existing/path"),
    )
    db.commit()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["setup", "--game", "darktide"],
        input="TESTKEY5678\n",  # API key prompt only
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "already registered" in result.output

    # Only one row should exist
    rows = db.execute("SELECT path FROM game_paths WHERE game='darktide'").fetchall()
    assert len(rows) == 1
    assert rows[0]["path"] == "/some/existing/path"


# ── --game flag limits wizard to a single slug ───────────────────────────────

def test_setup_game_flag_single_slug(monkeypatch):
    monkeypatch.setattr(nexmod, "_is_interactive", lambda: True)
    monkeypatch.setattr(nexmod, "find_game_install", lambda _: None)
    monkeypatch.setattr(nexmod.doctor, "callback", lambda game=None: None)

    scanned_slugs = []
    real_find = nexmod.find_game_install

    def tracking_find(steam_id):
        # Map steam_id back to slug to track which were checked
        for slug, info in nexmod.GAMES.items():
            if info["steam_id"] == steam_id:
                scanned_slugs.append(slug)
        return None

    monkeypatch.setattr(nexmod, "find_game_install", tracking_find)

    runner = CliRunner()
    runner.invoke(
        cli,
        ["setup", "--game", "darktide"],
        input="MYKEY1234\n\n",  # key + skip path prompt
        catch_exceptions=False,
    )

    assert "darktide" in scanned_slugs
    # Other games must not have been scanned
    other_games = [s for s in nexmod.GAMES if s != "darktide"]
    for other in other_games:
        assert other not in scanned_slugs
