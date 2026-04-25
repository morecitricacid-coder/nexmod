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
