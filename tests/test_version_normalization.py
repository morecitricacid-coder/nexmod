"""Tests for _norm_version and its effect on check/update comparisons.

Fix 8: version string normalization — "1.0" == "1.00" == "1", "v1.2" == "1.2".
"""
import json
import pytest
import nexmod
from nexmod import cli

pytestmark = pytest.mark.usefixtures("isolated_dirs")


# ── _norm_version unit tests ──────────────────────────────────────────────────

class TestNormVersion:

    def test_strips_leading_v(self):
        assert nexmod._norm_version("v1.2") == "1.2"

    def test_strips_leading_v_uppercase(self):
        assert nexmod._norm_version("V1.2") == "1.2"

    def test_strips_trailing_zero(self):
        assert nexmod._norm_version("1.0") == "1"

    def test_strips_trailing_double_zero(self):
        assert nexmod._norm_version("1.00") == "1"

    def test_strips_multiple_trailing_zeros(self):
        assert nexmod._norm_version("1.0.0") == "1"

    def test_does_not_strip_meaningful_zero(self):
        # "1.0.1" — the trailing segment is "1", not "0", so "1.0" should stay.
        assert nexmod._norm_version("1.0.1") == "1.0.1"

    def test_preserves_middle_zero(self):
        # "1.0.2" — middle "0" is not trailing, stays.
        assert nexmod._norm_version("1.0.2") == "1.0.2"

    def test_v_prefix_plus_trailing_zero(self):
        assert nexmod._norm_version("v1.2.0") == "1.2"

    def test_lowercases(self):
        # Purely alphabetic versions should be lowercased.
        assert nexmod._norm_version("V1.2A") == "1.2a"

    def test_empty_string_returns_empty(self):
        assert nexmod._norm_version("") == ""

    def test_none_treated_as_empty(self):
        # _norm_version(None) should not raise; it uses (v or "").
        assert nexmod._norm_version(None) == ""  # type: ignore[arg-type]

    def test_equal_after_normalization_same_string(self):
        assert nexmod._norm_version("1.2") == nexmod._norm_version("1.2")

    def test_equal_after_normalization_v_prefix(self):
        assert nexmod._norm_version("v1.2") == nexmod._norm_version("1.2")

    def test_equal_after_normalization_trailing_zero(self):
        assert nexmod._norm_version("1.0") == nexmod._norm_version("1")

    def test_not_equal_different_versions(self):
        assert nexmod._norm_version("1.2") != nexmod._norm_version("1.3")

    def test_not_equal_minor_difference(self):
        assert nexmod._norm_version("1.2.1") != nexmod._norm_version("1.2")


# ── check --json respects normalization ──────────────────────────────────────

def _seed_mod(game="darktide", mod_id=1, version="1.0"):
    db = nexmod.get_db()
    db.execute("""
        INSERT OR REPLACE INTO mods
            (game, mod_id, file_id, name, version, filename, mod_dir, tracked_at, updated_at)
        VALUES (?, ?, 1, 'Test Mod', ?, 'test.zip', '/tmp/mods', '2026-01-01', '2026-01-01')
    """, (game, mod_id, version))
    db.commit()


class TestCheckNormalization:
    """check should treat "1.0" and "1.00" as the same version (no update)."""

    def test_check_no_update_when_versions_differ_only_by_trailing_zero(
            self, runner, api_key_config, monkeypatch):
        """Installed "1.0" vs latest "1" — should show Current, not Update."""
        _seed_mod(version="1.0")
        monkeypatch.setattr(nexmod, "nexus_get",
                            lambda *a, **kw: {"name": "Test Mod", "version": "1"})
        result = runner.invoke(cli, ["check", "darktide", "--json"])
        assert result.exit_code == 0
        rows = json.loads(result.output)
        assert len(rows) == 1
        assert rows[0]["update_available"] is False

    def test_check_no_update_v_prefix_vs_no_prefix(
            self, runner, api_key_config, monkeypatch):
        """Installed "1.2" vs latest "v1.2" — should show Current."""
        _seed_mod(version="1.2")
        monkeypatch.setattr(nexmod, "nexus_get",
                            lambda *a, **kw: {"name": "Test Mod", "version": "v1.2"})
        result = runner.invoke(cli, ["check", "darktide", "--json"])
        assert result.exit_code == 0
        rows = json.loads(result.output)
        assert rows[0]["update_available"] is False

    def test_check_update_available_for_genuinely_different_version(
            self, runner, api_key_config, monkeypatch):
        """Installed "1.2" vs latest "1.3" — should show update available."""
        _seed_mod(version="1.2")
        monkeypatch.setattr(nexmod, "nexus_get",
                            lambda *a, **kw: {"name": "Test Mod", "version": "1.3"})
        result = runner.invoke(cli, ["check", "darktide", "--json"])
        assert result.exit_code == 0
        rows = json.loads(result.output)
        assert rows[0]["update_available"] is True

    def test_check_trailing_double_zero(
            self, runner, api_key_config, monkeypatch):
        """Installed "1.00" vs latest "1.0" — both normalize to "1", no update."""
        _seed_mod(version="1.00")
        monkeypatch.setattr(nexmod, "nexus_get",
                            lambda *a, **kw: {"name": "Test Mod", "version": "1.0"})
        result = runner.invoke(cli, ["check", "darktide", "--json"])
        assert result.exit_code == 0
        rows = json.loads(result.output)
        assert rows[0]["update_available"] is False
