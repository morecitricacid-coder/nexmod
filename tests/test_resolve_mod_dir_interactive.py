"""Tests for the interactive fallback paths in resolve_mod_dir."""
import pytest
import nexmod
from nexmod import resolve_mod_dir

pytestmark = pytest.mark.usefixtures("isolated_dirs")

FAKE_GAMES = {
    "knowngame": {
        "domain": "knowngame",
        "steam_id": 99999,
        "mod_subdir": "mods",
        "load_order_file": None,
        "log_subpath": None,
        "name": "Known Game",
    }
}


# ── Non-interactive + unknown game → exit 1 with clear message ───────────────

def test_non_interactive_unknown_game_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr(nexmod, "_is_interactive", lambda: False)
    monkeypatch.setattr(nexmod, "GAMES", {})

    db = nexmod.get_db()

    with pytest.raises(SystemExit) as exc_info:
        resolve_mod_dir("unknowngame", db)

    assert exc_info.value.code == 1


# ── Interactive + unknown game + empty Enter → exit 0, no DB row ─────────────

def test_interactive_unknown_game_empty_input_exits_0(tmp_path, monkeypatch):
    monkeypatch.setattr(nexmod, "_is_interactive", lambda: True)
    monkeypatch.setattr(nexmod, "GAMES", {})
    monkeypatch.setattr("click.prompt", lambda *a, **kw: "")

    db = nexmod.get_db()

    with pytest.raises(SystemExit) as exc_info:
        resolve_mod_dir("unknowngame", db)

    assert exc_info.value.code == 0

    # No row should have been inserted
    row = db.execute("SELECT path FROM game_paths WHERE game='unknowngame'").fetchone()
    assert row is None


# ── Interactive + valid path entered → row inserted in game_paths ─────────────

def test_interactive_unknown_game_valid_path_registers(tmp_path, monkeypatch):
    monkeypatch.setattr(nexmod, "_is_interactive", lambda: True)
    monkeypatch.setattr(nexmod, "GAMES", {})

    mod_dir = tmp_path / "mymods"
    mod_dir.mkdir()

    monkeypatch.setattr("click.prompt", lambda *a, **kw: str(mod_dir))

    db = nexmod.get_db()
    result = resolve_mod_dir("unknowngame", db)

    assert result == mod_dir.resolve()

    row = db.execute("SELECT path FROM game_paths WHERE game='unknowngame'").fetchone()
    assert row is not None
    assert str(mod_dir.resolve()) in row["path"]
