"""
Tests for the load order reconciler:
  _parse_directives, _parse_load_order_file, _classify_entries,
  _apply_pins, reconcile_load_order, atomic write + backup, drift detection.
"""
import pytest
import nexmod

pytestmark = pytest.mark.usefixtures("isolated_dirs")

LOF = "mod_load_order.txt"


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def darktide_dir(tmp_path, monkeypatch):
    """A pretend Darktide mod_dir with empty disk + DB. Caller populates."""
    mod_dir = tmp_path / "Darktide" / "mods"
    mod_dir.mkdir(parents=True)
    monkeypatch.setattr(nexmod, "resolve_mod_dir", lambda g, d: mod_dir)
    return mod_dir


def write_lof(mod_dir, *lines):
    (mod_dir / LOF).write_text("\n".join(lines) + "\n")


def read_entries(mod_dir):
    return [l for l in (mod_dir / LOF).read_text().splitlines()
            if l.strip() and not l.strip().startswith("--")]


# ── _parse_directives ────────────────────────────────────────────────────────

def test_parse_directives_freeze():
    d = nexmod._parse_directives(["-- nexmod:freeze"])
    assert d["frozen"] is True
    assert d["framework"] == []
    assert d["pins"] == []


def test_parse_directives_framework():
    d = nexmod._parse_directives([
        "-- nexmod:framework mod_compat",
        "-- nexmod:framework dmf",
    ])
    assert d["framework"] == ["mod_compat", "dmf"]


@pytest.mark.parametrize("line, expected", [
    ("-- nexmod:pin foo top",            ("foo", "top", None)),
    ("-- nexmod:pin foo bottom",         ("foo", "bottom", None)),
    ("-- nexmod:pin foo before bar",     ("foo", "before", "bar")),
    ("-- nexmod:pin foo after bar",      ("foo", "after", "bar")),
])
def test_parse_directives_pin(line, expected):
    d = nexmod._parse_directives([line])
    assert d["pins"] == [expected]


def test_parse_directives_ignores_unknown():
    d = nexmod._parse_directives([
        "-- nexmod:fizzbuzz arg",          # unknown verb
        "-- File managed by nexmod",       # not a directive
        "ModA",                            # entry, not a directive
        "-- a comment",                    # plain comment
    ])
    assert d == {"frozen": False, "framework": [], "pins": []}


# ── _parse_load_order_file ───────────────────────────────────────────────────

def test_parse_strips_canonical_header():
    parsed = nexmod._parse_load_order_file("-- File managed by nexmod\nModA\nModB\n")
    assert parsed["entries"] == ["ModA", "ModB"]
    assert parsed["header_comments"] == []
    assert parsed["anchored_comments"] == {}


def test_parse_anchors_comments_to_following_entry():
    parsed = nexmod._parse_load_order_file(
        "-- File managed by nexmod\n"
        "-- QoL section\n"
        "ModA\n"
        "ModB\n"
    )
    assert parsed["entries"] == ["ModA", "ModB"]
    assert parsed["anchored_comments"] == {"ModA": ["-- QoL section"]}


def test_parse_separates_directives_from_comments():
    parsed = nexmod._parse_load_order_file(
        "-- nexmod:freeze\n"
        "-- some other comment\n"
        "ModA\n"
    )
    assert parsed["directives"]["frozen"] is True
    # The plain comment should be anchored to ModA, not duplicated.
    assert parsed["anchored_comments"] == {"ModA": ["-- some other comment"]}


# ── _classify_entries ────────────────────────────────────────────────────────

def test_classify_managed_present():
    cls = nexmod._classify_entries(
        ["A"], db_folders={"A"}, disk_folders={"A"}, framework_folders=set(),
    )
    assert cls == {"A": "managed-present"}


def test_classify_managed_missing():
    cls = nexmod._classify_entries(
        ["A"], db_folders={"A"}, disk_folders=set(), framework_folders=set(),
    )
    assert cls == {"A": "managed-missing"}


def test_classify_orphan():
    cls = nexmod._classify_entries(
        ["A"], db_folders=set(), disk_folders=set(), framework_folders=set(),
    )
    assert cls == {"A": "orphan"}


def test_classify_foreign():
    cls = nexmod._classify_entries(
        ["A"], db_folders=set(), disk_folders={"A"}, framework_folders=set(),
    )
    assert cls == {"A": "foreign"}


def test_classify_framework_takes_precedence_over_foreign():
    cls = nexmod._classify_entries(
        ["mod_compat"],
        db_folders=set(),
        disk_folders={"mod_compat"},
        framework_folders={"mod_compat"},
    )
    assert cls == {"mod_compat": "framework"}


# ── _apply_pins ──────────────────────────────────────────────────────────────

def test_apply_pins_framework_first():
    out = nexmod._apply_pins(
        sorted_managed=["A", "B"], foreign=[], framework=["fw1"], pins=[],
    )
    assert out == ["fw1", "A", "B"]


def test_apply_pins_top_pin_after_framework():
    out = nexmod._apply_pins(
        sorted_managed=["A", "B"], foreign=[], framework=["fw1"],
        pins=[("pinned", "top", None)],
    )
    assert out == ["fw1", "pinned", "A", "B"]


def test_apply_pins_bottom():
    out = nexmod._apply_pins(
        sorted_managed=["A", "B"], foreign=[], framework=[],
        pins=[("zlast", "bottom", None)],
    )
    assert out[-1] == "zlast"


def test_apply_pins_before():
    out = nexmod._apply_pins(
        sorted_managed=["A", "B", "C"], foreign=[], framework=[],
        pins=[("A", "before", "C")],
    )
    # A relocates to right before C
    assert out == ["B", "A", "C"]


def test_apply_pins_after():
    out = nexmod._apply_pins(
        sorted_managed=["A", "B", "C"], foreign=[], framework=[],
        pins=[("A", "after", "C")],
    )
    assert out == ["B", "C", "A"]


# ── reconcile_load_order: end-to-end ────────────────────────────────────────

def test_reconcile_drops_orphan(darktide_dir):
    """The bug we hit: NumericUI listed but folder gone, no DB row → drop."""
    write_lof(darktide_dir, "-- File managed by nexmod", "OnDisk", "OrphanedMod")
    (darktide_dir / "OnDisk").mkdir()
    db = nexmod.get_db()
    result = nexmod.reconcile_load_order("darktide", db, darktide_dir)
    assert result["written"] is True
    assert "OrphanedMod" in result["orphans_dropped"]
    assert read_entries(darktide_dir) == ["OnDisk"]


def test_reconcile_idempotent(darktide_dir):
    write_lof(darktide_dir, "-- File managed by nexmod", "ModA")
    (darktide_dir / "ModA").mkdir()
    db = nexmod.get_db()
    nexmod.reconcile_load_order("darktide", db, darktide_dir)
    snap1 = (darktide_dir / LOF).read_text()
    result2 = nexmod.reconcile_load_order("darktide", db, darktide_dir)
    assert result2["written"] is False
    assert (darktide_dir / LOF).read_text() == snap1


def test_reconcile_auto_adds_framework_folder(darktide_dir):
    """Darktide has hardcoded framework folders. If on disk but unlisted, auto-add at top."""
    write_lof(darktide_dir, "-- File managed by nexmod", "OtherMod")
    (darktide_dir / "OtherMod").mkdir()
    (darktide_dir / "mod_compat").mkdir()  # framework folder
    db = nexmod.get_db()
    result = nexmod.reconcile_load_order("darktide", db, darktide_dir)
    entries = read_entries(darktide_dir)
    assert "mod_compat" in result["missing_added"]
    assert entries.index("mod_compat") < entries.index("OtherMod")


def test_reconcile_preserves_foreign(darktide_dir):
    """Untracked-but-on-disk folders stay verbatim, in original relative order."""
    write_lof(darktide_dir, "-- File managed by nexmod",
              "ForeignA", "ForeignB", "ForeignC")
    for n in ("ForeignA", "ForeignB", "ForeignC"):
        (darktide_dir / n).mkdir()
    db = nexmod.get_db()
    nexmod.reconcile_load_order("darktide", db, darktide_dir)
    assert read_entries(darktide_dir) == ["ForeignA", "ForeignB", "ForeignC"]


def test_reconcile_freeze_directive_blocks_writes(darktide_dir):
    write_lof(darktide_dir,
              "-- File managed by nexmod",
              "-- nexmod:freeze",
              "OrphanThatShouldDropButWont")
    db = nexmod.get_db()
    result = nexmod.reconcile_load_order("darktide", db, darktide_dir)
    assert result["frozen"] is True
    assert result["written"] is False
    assert "OrphanThatShouldDropButWont" in (darktide_dir / LOF).read_text()


def test_reconcile_drift_blocks_overwrite(darktide_dir):
    """If the file changed externally since last write, refuse to overwrite."""
    write_lof(darktide_dir, "-- File managed by nexmod", "ModA")
    (darktide_dir / "ModA").mkdir()
    db = nexmod.get_db()
    # First reconcile establishes the baseline hash.
    nexmod.reconcile_load_order("darktide", db, darktide_dir)
    # Simulate external edit: append a foreign entry user typed by hand.
    (darktide_dir / "UserAdded").mkdir()
    with (darktide_dir / LOF).open("a") as f:
        f.write("UserAdded\n")
    # Add an orphan that reconciler would normally drop.
    with (darktide_dir / LOF).open("a") as f:
        f.write("Orphan\n")
    # Reconcile WITHOUT auto_merge: should detect drift, refuse, return changed=False.
    result = nexmod.reconcile_load_order("darktide", db, darktide_dir)
    assert result["drift_detected"] is True
    assert result["written"] is False
    assert "Orphan" in (darktide_dir / LOF).read_text()  # untouched
    # auto_merge=True forces the reconcile through.
    result2 = nexmod.reconcile_load_order(
        "darktide", db, darktide_dir, auto_merge=True,
    )
    assert result2["written"] is True
    assert "Orphan" not in (darktide_dir / LOF).read_text()
    assert "UserAdded" in (darktide_dir / LOF).read_text()


def test_reconcile_atomic_write_keeps_backup(darktide_dir):
    write_lof(darktide_dir, "-- File managed by nexmod", "ModA", "Orphan")
    (darktide_dir / "ModA").mkdir()
    db = nexmod.get_db()
    nexmod.reconcile_load_order("darktide", db, darktide_dir)
    bak = (darktide_dir / LOF).with_suffix(".txt.bak")
    assert bak.exists()
    assert "Orphan" in bak.read_text()  # backup has the pre-reconcile state


def test_reconcile_unsupported_game_is_noop(darktide_dir):
    db = nexmod.get_db()
    # skyrimse has no load_order_file in GAMES
    result = nexmod.reconcile_load_order("skyrimse", db, darktide_dir)
    assert result == nexmod._empty_reconcile_result()


def test_reconcile_first_run_records_hash(darktide_dir):
    """First run with file present and no DB state should persist the hash even with no changes."""
    write_lof(darktide_dir, "-- File managed by nexmod")
    db = nexmod.get_db()
    nexmod.reconcile_load_order("darktide", db, darktide_dir)
    row = db.execute(
        "SELECT last_hash FROM load_order_state WHERE game='darktide'"
    ).fetchone()
    assert row is not None
    assert row["last_hash"]


def test_reconcile_profile_preserves_order(darktide_dir):
    """Profile order is canonical — no topo reorder."""
    for n in ("A", "B", "C"):
        (darktide_dir / n).mkdir()
    write_lof(darktide_dir, "-- File managed by nexmod")
    db = nexmod.get_db()
    nexmod.reconcile_load_order(
        "darktide", db, darktide_dir, profile_set=["C", "A", "B"],
    )
    assert read_entries(darktide_dir) == ["C", "A", "B"]


def test_reconcile_profile_strict_drops_foreign(darktide_dir):
    for n in ("ProfileMod", "ForeignMod"):
        (darktide_dir / n).mkdir()
    write_lof(darktide_dir, "-- File managed by nexmod", "ProfileMod", "ForeignMod")
    db = nexmod.get_db()
    nexmod.reconcile_load_order(
        "darktide", db, darktide_dir,
        profile_set=["ProfileMod"], strict_profile=True,
    )
    assert read_entries(darktide_dir) == ["ProfileMod"]


def test_reconcile_profile_default_keeps_foreign(darktide_dir):
    for n in ("ProfileMod", "ForeignMod"):
        (darktide_dir / n).mkdir()
    write_lof(darktide_dir, "-- File managed by nexmod", "ProfileMod", "ForeignMod")
    db = nexmod.get_db()
    nexmod.reconcile_load_order(
        "darktide", db, darktide_dir,
        profile_set=["ProfileMod"], strict_profile=False,
    )
    entries = read_entries(darktide_dir)
    assert "ProfileMod" in entries
    assert "ForeignMod" in entries  # preserved (not strict)


def test_reconcile_dry_run_does_not_write(darktide_dir):
    write_lof(darktide_dir, "-- File managed by nexmod", "Orphan")
    snap = (darktide_dir / LOF).read_text()
    db = nexmod.get_db()
    result = nexmod.reconcile_load_order("darktide", db, darktide_dir, dry_run=True)
    assert result["changed"] is True
    assert (darktide_dir / LOF).read_text() == snap   # untouched
    assert result["diff"]                              # diff emitted


# ── _atomic_write_with_backup ────────────────────────────────────────────────

def test_atomic_write_creates_backup(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("v1")
    nexmod._atomic_write_with_backup(target, "v2")
    assert target.read_text() == "v2"
    assert (target.with_suffix(".txt.bak")).read_text() == "v1"


def test_atomic_write_no_backup_on_first_write(tmp_path):
    target = tmp_path / "f.txt"
    nexmod._atomic_write_with_backup(target, "first")
    assert target.read_text() == "first"
    assert not target.with_suffix(".txt.bak").exists()
