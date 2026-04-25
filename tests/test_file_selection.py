"""
Tests for pick_main_file.

Priority order: MAIN > UPDATE > MISCELLANEOUS > max-timestamp fallback (excludes OLD_VERSION).
None category_name must not crash (Nexus API sends explicit null for some files).
"""
import pytest
import nexmod

pytestmark = pytest.mark.usefixtures("isolated_dirs")


def make_file(file_id, category, timestamp=0):
    return {
        "file_id":            file_id,
        "file_name":          f"file_{file_id}.zip",
        "category_name":      category,
        "uploaded_timestamp": timestamp,
    }


# ── Priority order ────────────────────────────────────────────────────────────

def test_prefers_main_over_everything():
    files = [
        make_file(1, "OLD_VERSION"),
        make_file(2, "UPDATE"),
        make_file(3, "MAIN"),
    ]
    assert nexmod.pick_main_file(files)["file_id"] == 3


def test_falls_back_to_update_when_no_main():
    files = [
        make_file(1, "MISCELLANEOUS"),
        make_file(2, "UPDATE"),
    ]
    assert nexmod.pick_main_file(files)["file_id"] == 2


def test_falls_back_to_miscellaneous_when_no_main_or_update():
    files = [make_file(1, "MISCELLANEOUS")]
    assert nexmod.pick_main_file(files)["file_id"] == 1


def test_unknown_category_falls_back_to_max_timestamp():
    files = [
        make_file(1, "OPTIONAL", timestamp=100),
        make_file(2, "OPTIONAL", timestamp=200),
        make_file(3, "OLD_VERSION", timestamp=999),
    ]
    # No MAIN/UPDATE/MISC match, and OLD_VERSION is excluded from valid set.
    # Should return highest-timestamp non-OLD_VERSION file.
    chosen = nexmod.pick_main_file(files)
    assert chosen["file_id"] == 2


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_none_category_does_not_crash():
    files = [
        make_file(1, None),
        make_file(2, "MAIN"),
    ]
    assert nexmod.pick_main_file(files)["file_id"] == 2


def test_all_none_category_falls_to_timestamp_fallback():
    # None is not OLD_VERSION, so all are "valid"; return max timestamp.
    files = [
        make_file(1, None, timestamp=50),
        make_file(2, None, timestamp=99),
    ]
    chosen = nexmod.pick_main_file(files)
    assert chosen["file_id"] == 2


def test_all_old_version_returns_none():
    files = [
        make_file(1, "OLD_VERSION", timestamp=100),
        make_file(2, "OLD_VERSION", timestamp=999),
    ]
    assert nexmod.pick_main_file(files) is None


def test_empty_list_returns_none():
    assert nexmod.pick_main_file([]) is None


def test_category_matching_is_case_insensitive():
    # Nexus API may return lowercase — the code does .upper() before comparing
    files = [make_file(1, "main")]
    assert nexmod.pick_main_file(files)["file_id"] == 1
