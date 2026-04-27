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
    monkeypatch.setattr("nexmod._is_interactive", lambda: True)
    # User answers No to the "Install it now?" [Y/n] gate
    monkeypatch.setattr("click.confirm", lambda *a, **kw: False)
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
    monkeypatch.setattr("nexmod._is_interactive", lambda: True)
    # User answers Yes to "Install it now?", then provides the URL
    monkeypatch.setattr("click.confirm", lambda *a, **kw: True)
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

    # User says Yes to install, then skips the URL — we just test that the gate
    # and prompts fire only once despite two mods declaring the same dep.
    monkeypatch.setattr("nexmod._is_interactive", lambda: True)
    confirm_calls = []
    monkeypatch.setattr("click.confirm", lambda *a, **kw: (confirm_calls.append(1) or False))
    monkeypatch.setattr("click.prompt", lambda *a, **kw: "")

    nexmod._handle_missing_deps(
        "testgame",
        {"modA": ["shared_dep"], "modB": ["shared_dep"]},
        mod_dir, "fake_key", None,
    )

    # The [Y/n] gate should fire exactly once (dedup keeps only one entry)
    assert len(confirm_calls) == 1


# ── dep in DB by folder_name → do_install called without URL prompt ───────────

def test_dep_in_db_installs_without_url_prompt(tmp_path, monkeypatch):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "plugin").mkdir()

    monkeypatch.setattr("nexmod.GAMES", {
        "testgame": {"domain": "testgame", "steam_id": 0, "mod_subdir": "mods",
                     "load_order_file": None, "log_subpath": None}
    })

    # Seed DB with a row for the missing dep folder_name
    db = nexmod.get_db()
    db.execute(
        "INSERT INTO mods (game, mod_id, file_id, name, folder_name, tracked_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("testgame", 42, 0, "Framework Mod", "framework", "2026-01-01T00:00:00"),
    )
    db.commit()

    monkeypatch.setattr("nexmod._is_interactive", lambda: True)
    # User says Yes to the install gate
    monkeypatch.setattr("click.confirm", lambda *a, **kw: True)

    install_calls = []
    def fake_do_install(game, mod_id, file_id, api_key, db):
        install_calls.append((game, mod_id))
        return ("Framework Mod", "1.0")
    monkeypatch.setattr("nexmod.do_install", fake_do_install)

    # URL prompt must not be called because the DB row resolves the dep
    url_prompt_calls = []
    monkeypatch.setattr("click.prompt", lambda *a, **kw: url_prompt_calls.append(1) or "")

    result = nexmod._handle_missing_deps(
        "testgame", {"plugin": ["framework"]}, mod_dir, "fake_key", db
    )

    assert result is True
    assert install_calls == [("testgame", 42)]
    assert url_prompt_calls == [], "URL prompt should not be called when dep is in DB"
