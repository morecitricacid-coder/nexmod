"""
Tests for load order management:
  _ensure_load_order  — append-only helper
  _topo_sort          — Kahn's topological sort
  reorder_load_order  — full pipeline: parse mod.json + sort + write
"""
import json
import pytest
import nexmod

pytestmark = pytest.mark.usefixtures("isolated_dirs")

LOF = "mod_load_order.txt"


# ── _ensure_load_order ────────────────────────────────────────────────────────

def test_creates_file_when_missing(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    added = nexmod._ensure_load_order(mod_dir, LOF, ["MyMod"])
    assert added == ["MyMod"]
    assert (mod_dir / LOF).read_text() == "MyMod\n"


def test_appends_to_existing(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("ExistingMod\n")
    added = nexmod._ensure_load_order(mod_dir, LOF, ["NewMod"])
    assert added == ["NewMod"]
    lines = (mod_dir / LOF).read_text().splitlines()
    assert lines == ["ExistingMod", "NewMod"]


def test_skips_duplicates(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("AlreadyHere\n")
    added = nexmod._ensure_load_order(mod_dir, LOF, ["AlreadyHere"])
    assert added == []
    assert (mod_dir / LOF).read_text() == "AlreadyHere\n"


def test_partial_dedup(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("ModA\n")
    added = nexmod._ensure_load_order(mod_dir, LOF, ["ModA", "ModB"])
    assert added == ["ModB"]
    assert (mod_dir / LOF).read_text().splitlines() == ["ModA", "ModB"]


def test_empty_folders_list_is_noop(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("ModA\n")
    added = nexmod._ensure_load_order(mod_dir, LOF, [])
    assert added == []
    assert (mod_dir / LOF).read_text() == "ModA\n"


def test_preserves_order_of_existing_entries(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("First\nSecond\nThird\n")
    nexmod._ensure_load_order(mod_dir, LOF, ["Fourth"])
    lines = (mod_dir / LOF).read_text().splitlines()
    assert lines == ["First", "Second", "Third", "Fourth"]


# ── _topo_sort ────────────────────────────────────────────────────────────────

def test_topo_no_deps_preserves_order():
    folders = ["A", "B", "C"]
    deps    = {"A": [], "B": [], "C": []}
    result, cycles = nexmod._topo_sort(folders, deps)
    assert result == ["A", "B", "C"]
    assert cycles == []


def test_topo_simple_chain():
    # C depends on B, B depends on A → A, B, C
    folders = ["C", "B", "A"]
    deps    = {"A": [], "B": ["A"], "C": ["B"]}
    result, cycles = nexmod._topo_sort(folders, deps)
    assert result.index("A") < result.index("B") < result.index("C")
    assert cycles == []


def test_topo_dep_moves_before_dependant():
    # mod_manager must be before plugin even though it appears after in the file
    folders = ["plugin", "mod_manager"]
    deps    = {"plugin": ["mod_manager"], "mod_manager": []}
    result, cycles = nexmod._topo_sort(folders, deps)
    assert result == ["mod_manager", "plugin"]
    assert cycles == []


def test_topo_stable_within_same_level():
    # A, B, C all depend on root with no inter-deps — original order preserved
    folders = ["root", "A", "B", "C"]
    deps    = {"root": [], "A": ["root"], "B": ["root"], "C": ["root"]}
    result, cycles = nexmod._topo_sort(folders, deps)
    assert result[0] == "root"
    assert result[1:] == ["A", "B", "C"]


def test_topo_cycle_detected():
    folders = ["A", "B"]
    deps    = {"A": ["B"], "B": ["A"]}
    result, cycles = nexmod._topo_sort(folders, deps)
    assert set(cycles) == {"A", "B"}
    assert result == []


def test_topo_partial_cycle_rest_sorted():
    # A and B cycle; C has no deps and should sort fine
    folders = ["A", "B", "C"]
    deps    = {"A": ["B"], "B": ["A"], "C": []}
    result, cycles = nexmod._topo_sort(folders, deps)
    assert "C" in result
    assert set(cycles) == {"A", "B"}


# ── reorder_load_order ────────────────────────────────────────────────────────

def _make_mod(mod_dir, folder, deps=None, optional_deps=None):
    d = mod_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    data = {"name": folder, "dependencies": deps or [], "optional_dependencies": optional_deps or []}
    (d / "mod.json").write_text(json.dumps(data))


def test_reorder_no_deps_unchanged(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("A\nB\nC\n")
    _make_mod(mod_dir, "A")
    _make_mod(mod_dir, "B")
    _make_mod(mod_dir, "C")
    result = nexmod.reorder_load_order(mod_dir, LOF)
    assert result["order"] == ["A", "B", "C"]
    assert not result["changed"]


def test_reorder_moves_dep_before_dependant(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    # plugin is listed before mod_manager — should be fixed
    (mod_dir / LOF).write_text("plugin\nmod_manager\n")
    _make_mod(mod_dir, "plugin", deps=["mod_manager"])
    _make_mod(mod_dir, "mod_manager")
    result = nexmod.reorder_load_order(mod_dir, LOF)
    assert result["order"] == ["mod_manager", "plugin"]
    assert result["changed"]
    written = [l for l in (mod_dir / LOF).read_text().splitlines() if not l.startswith("--")]
    assert written == ["mod_manager", "plugin"]


def test_reorder_dry_run_does_not_write(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("plugin\nmod_manager\n")
    _make_mod(mod_dir, "plugin", deps=["mod_manager"])
    _make_mod(mod_dir, "mod_manager")
    result = nexmod.reorder_load_order(mod_dir, LOF, dry_run=True)
    assert result["changed"]
    # File must be unchanged
    assert (mod_dir / LOF).read_text().splitlines() == ["plugin", "mod_manager"]


def test_reorder_missing_dep_reported(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("plugin\n")
    _make_mod(mod_dir, "plugin", deps=["mod_manager"])  # mod_manager not in file
    result = nexmod.reorder_load_order(mod_dir, LOF)
    assert "plugin" in result["missing_deps"]
    assert "mod_manager" in result["missing_deps"]["plugin"]


def test_reorder_optional_dep_included(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("plugin\noptional_lib\n")
    _make_mod(mod_dir, "plugin", optional_deps=["optional_lib"])
    _make_mod(mod_dir, "optional_lib")
    result = nexmod.reorder_load_order(mod_dir, LOF)
    assert result["order"].index("optional_lib") < result["order"].index("plugin")


def test_reorder_no_mod_json_treated_as_no_deps(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("naked\nother\n")
    (mod_dir / "naked").mkdir()   # no mod.json
    _make_mod(mod_dir, "other")
    result = nexmod.reorder_load_order(mod_dir, LOF)
    assert not result["changed"]


def test_reorder_cycle_detected(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / LOF).write_text("A\nB\n")
    _make_mod(mod_dir, "A", deps=["B"])
    _make_mod(mod_dir, "B", deps=["A"])
    result = nexmod.reorder_load_order(mod_dir, LOF)
    assert set(result["cycles"]) == {"A", "B"}


def test_reorder_no_lof_returns_empty(tmp_path):
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    result = nexmod.reorder_load_order(mod_dir, LOF)
    assert result["order"] == []
    assert not result["changed"]
