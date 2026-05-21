"""
Tests for _maybe_promote_wrapper: Cyberpunk (and future games') wrapper-folder
path promotion after archive extraction.
"""
import shutil
import pytest
from pathlib import Path
import nexmod

pytestmark = pytest.mark.usefixtures("isolated_dirs")

_CYBERPUNK_INFO = nexmod.GAMES["cyberpunk2077"]


class TestMaybePromoteWrapper:

    def test_happy_path_promotes_wrapper_folder(self, tmp_path):
        """Single new dir containing a known indicator is promoted to mod_dir."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        # Simulate pre-extraction state: nothing yet
        dirs_before = set()
        # Simulate extraction result: ModName/archive/file.archive
        wrapper = mod_dir / "CoolMod"
        (wrapper / "archive" / "pc" / "mod").mkdir(parents=True)
        (wrapper / "archive" / "pc" / "mod" / "file.archive").write_bytes(b"data")

        result = nexmod._maybe_promote_wrapper(mod_dir, dirs_before, _CYBERPUNK_INFO)

        assert result is True
        # archive/ now lives directly in mod_dir
        assert (mod_dir / "archive" / "pc" / "mod" / "file.archive").exists()
        # wrapper dir is gone
        assert not (mod_dir / "CoolMod").exists()

    def test_no_wrapper_indicators_returns_false(self, tmp_path):
        """If game_info has no wrapper_indicators, function returns False and leaves mod_dir alone."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        dirs_before = set()
        wrapper = mod_dir / "SomeMod"
        (wrapper / "archive").mkdir(parents=True)
        (wrapper / "archive" / "file.archive").write_bytes(b"data")

        game_info_no_indicators = {"mod_subdir": "archive/pc/mod"}
        result = nexmod._maybe_promote_wrapper(mod_dir, dirs_before, game_info_no_indicators)

        assert result is False
        # Nothing moved
        assert (mod_dir / "SomeMod" / "archive" / "file.archive").exists()

    def test_multiple_new_dirs_returns_false(self, tmp_path):
        """If more than one new directory was created, do not promote anything."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        dirs_before = set()
        # Two new dirs created
        (mod_dir / "ModA" / "archive").mkdir(parents=True)
        (mod_dir / "ModB" / "archive").mkdir(parents=True)

        result = nexmod._maybe_promote_wrapper(mod_dir, dirs_before, _CYBERPUNK_INFO)

        assert result is False
        assert (mod_dir / "ModA").exists()
        assert (mod_dir / "ModB").exists()

    def test_wrapper_without_indicator_children_returns_false(self, tmp_path):
        """Wrapper folder containing only non-indicator files is not promoted."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        dirs_before = set()
        wrapper = mod_dir / "SomeMod"
        wrapper.mkdir()
        (wrapper / "readme.txt").write_text("instructions")
        (wrapper / "license.txt").write_text("MIT")

        result = nexmod._maybe_promote_wrapper(mod_dir, dirs_before, _CYBERPUNK_INFO)

        assert result is False
        assert (mod_dir / "SomeMod").exists()

    def test_destination_collision_skips_conflicting_item(self, tmp_path):
        """If a dest name already exists in mod_dir, that item is skipped with a warning."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        # Pre-existing archive/ dir in mod_dir (collision) — note it's in dirs_before
        existing = mod_dir / "archive"
        existing.mkdir()
        (existing / "existing_file.archive").write_bytes(b"old data")
        dirs_before = {"archive"}  # archive/ existed before extraction

        # Wrapper also has archive/ — this will collide
        wrapper = mod_dir / "CoolMod"
        (wrapper / "archive" / "pc" / "mod").mkdir(parents=True)
        (wrapper / "archive" / "pc" / "mod" / "new_file.archive").write_bytes(b"new data")

        result = nexmod._maybe_promote_wrapper(mod_dir, dirs_before, _CYBERPUNK_INFO)

        # Returns True because the wrapper was detected; the collision item is skipped
        assert result is True
        # Original archive/ is untouched (collision was skipped)
        assert (mod_dir / "archive" / "existing_file.archive").exists()
        assert (mod_dir / "archive" / "existing_file.archive").read_bytes() == b"old data"
        # new_file was NOT copied over (collision skipped), and CoolMod/ remains
        # non-empty so rmdir silently fails — that's expected behavior
        assert not (mod_dir / "archive" / "pc" / "mod" / "new_file.archive").exists()

    def test_pre_existing_dir_not_treated_as_new(self, tmp_path):
        """A dir that existed before extraction is not counted as a new wrapper candidate."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        pre_existing = mod_dir / "OldMod"
        (pre_existing / "archive").mkdir(parents=True)
        dirs_before = {"OldMod"}

        # No new dirs were created (dirs_before == dirs_after)
        result = nexmod._maybe_promote_wrapper(mod_dir, dirs_before, _CYBERPUNK_INFO)

        assert result is False

    def test_bin_indicator_also_triggers_promotion(self, tmp_path):
        """Wrapper containing 'bin' child also triggers promotion."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        dirs_before = set()
        wrapper = mod_dir / "AnotherMod"
        (wrapper / "bin" / "x64").mkdir(parents=True)
        (wrapper / "bin" / "x64" / "mod.dll").write_bytes(b"\x00")

        result = nexmod._maybe_promote_wrapper(mod_dir, dirs_before, _CYBERPUNK_INFO)

        assert result is True
        assert (mod_dir / "bin" / "x64" / "mod.dll").exists()
        assert not (mod_dir / "AnotherMod").exists()

    def test_empty_wrapper_indicators_set_returns_false(self, tmp_path):
        """An empty wrapper_indicators set is treated as no indicators."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        dirs_before = set()
        wrapper = mod_dir / "Mod"
        (wrapper / "archive").mkdir(parents=True)

        game_info_empty = {"wrapper_indicators": set()}
        result = nexmod._maybe_promote_wrapper(mod_dir, dirs_before, game_info_empty)

        assert result is False
