"""Tests for _handle_missing_deps — the missing-dependency install prompt."""
import json
import pytest
import nexmod

pytestmark = pytest.mark.usefixtures("isolated_dirs")

LOF = "mod_load_order.txt"


def _make_mod(mod_dir, folder, deps=None):
    d = mod_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    data = {"name": folder, "dependencies": deps or []}
    (d / "mod.json").write_text(json.dumps(data))


# ── dep exists on disk but not in load order ──────────────────────────────────

def test_dep_on_disk_added_silently(tmp_path, monkeypatch):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("plugin\n")
    _make_mod(mod_dir, "plugin", deps=["framework"])
    (mod_dir / "framework").mkdir()  # on disk, just not in load order

    monkeypatch.setattr("nexmod.GAMES", {
        "testgame": {"domain": "testgame", "steam_id": 0, "mod_subdir": "mods",
                     "load_order_file": LOF, "log_subpath": None}
    })

    result = nexmod._handle_missing_deps(
        "testgame", {"plugin": ["framework"]}, mod_dir, "fake_key", None
    )

    assert result is False  # no new installs, just load order fixup
    lof_lines = [l for l in (mod_dir / LOF).read_text().splitlines() if not l.startswith("--")]
    assert "framework" in lof_lines


# ── user skips (presses Enter) ────────────────────────────────────────────────

def test_user_skips_missing_dep(tmp_path, monkeypatch):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "plugin").mkdir()

    monkeypatch.setattr("nexmod.GAMES", {
        "testgame": {"domain": "testgame", "steam_id": 0, "mod_subdir": "mods",
                     "load_order_file": LOF, "log_subpath": None}
    })
    monkeypatch.setattr("click.prompt", lambda *a, **kw: "")

    result = nexmod._handle_missing_deps(
        "testgame", {"plugin": ["missing_framework"]}, mod_dir, "fake_key", None
    )
    assert result is False


# ── user provides a URL → do_install is called ───────────────────────────────

def test_user_provides_url_triggers_install(tmp_path, monkeypatch):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "plugin").mkdir()

    monkeypatch.setattr("nexmod.GAMES", {
        "testgame": {"domain": "testgame", "steam_id": 0, "mod_subdir": "mods",
                     "load_order_file": LOF, "log_subpath": None}
    })

    fake_url = "https://www.nexusmods.com/testgame/mods/999"
    monkeypatch.setattr("click.prompt", lambda *a, **kw: fake_url)
    monkeypatch.setattr("nexmod.parse_nexus_url", lambda url: ("testgame", 999, None))

    install_calls = []
    def fake_do_install(game, mod_id, file_id, api_key, db):
        install_calls.append((game, mod_id, file_id))
        return ("Framework", "1.0")
    monkeypatch.setattr("nexmod.do_install", fake_do_install)

    result = nexmod._handle_missing_deps(
        "testgame", {"plugin": ["framework"]}, mod_dir, "fake_key", None
    )

    assert result is True
    assert install_calls == [("testgame", 999, None)]


# ── dedup: same dep required by multiple mods prompts only once ───────────────

def test_dedup_same_dep_prompts_once(tmp_path, monkeypatch):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()

    monkeypatch.setattr("nexmod.GAMES", {
        "testgame": {"domain": "testgame", "steam_id": 0, "mod_subdir": "mods",
                     "load_order_file": None, "log_subpath": None}
    })

    prompt_calls = []
    def fake_prompt(*a, **kw):
        prompt_calls.append(1)
        return ""
    monkeypatch.setattr("click.prompt", fake_prompt)

    nexmod._handle_missing_deps(
        "testgame",
        {"modA": ["shared_dep"], "modB": ["shared_dep"]},
        mod_dir, "fake_key", None,
    )

    assert len(prompt_calls) == 1
