"""
Tests for parse_vortex_manifest.

Vortex encodes mod IDs in archive filenames as:
  <ModName>-<mod_id>-<file_id>-<version>-<timestamp>

The parser splits on '-' and picks the first all-digit segment with < 10 digits
(timestamps like 1759006144 are 10 digits and must be skipped).
"""
import json
import pytest
from pathlib import Path
import nexmod

pytestmark = pytest.mark.usefixtures("isolated_dirs")


def write_manifest(game_dir: Path, files: list) -> None:
    manifest = game_dir / "vortex.deployment.json"
    manifest.write_text(json.dumps({"files": files}))


# ── Basic behaviour ───────────────────────────────────────────────────────────

def test_no_manifest(tmp_path):
    assert nexmod.parse_vortex_manifest(tmp_path) == {}


def test_empty_files_list(tmp_path):
    write_manifest(tmp_path, [])
    assert nexmod.parse_vortex_manifest(tmp_path) == {}


def test_basic_parse_extracts_id_name_folder(tmp_path):
    write_manifest(tmp_path, [
        {
            "source":  "Power_DI_1.1.15.zip-281-v1-1-15-1759006144",
            "relPath": "mods/PowerDI/PowerDI.mod",
        }
    ])
    result = nexmod.parse_vortex_manifest(tmp_path)
    assert 281 in result
    name, folder = result[281]
    assert "Power" in name
    assert folder == "PowerDI"


def test_multiple_mods_all_parsed(tmp_path):
    write_manifest(tmp_path, [
        {"source": "ModA-100-v1", "relPath": "mods/ModA/a.mod"},
        {"source": "ModB-200-v2", "relPath": "mods/ModB/b.mod"},
        {"source": "ModC-300-v3", "relPath": "mods/ModC/c.mod"},
    ])
    result = nexmod.parse_vortex_manifest(tmp_path)
    assert set(result.keys()) == {100, 200, 300}


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_no_digit_segment_skipped(tmp_path):
    write_manifest(tmp_path, [
        {"source": "NoDigitsHere-abc-def", "relPath": "mods/X/x.mod"},
    ])
    assert nexmod.parse_vortex_manifest(tmp_path) == {}


def test_duplicate_mod_id_first_wins(tmp_path):
    write_manifest(tmp_path, [
        {"source": "ModA-100-file1", "relPath": "mods/ModA/a.mod"},
        {"source": "ModA-100-file2", "relPath": "mods/ModA/b.mod"},
    ])
    result = nexmod.parse_vortex_manifest(tmp_path)
    assert len(result) == 1
    assert 100 in result


def test_relpath_not_starting_with_mods_gives_empty_folder(tmp_path):
    write_manifest(tmp_path, [
        {"source": "Shader-500-v1", "relPath": "Data/shaders/something.hlsl"},
    ])
    result = nexmod.parse_vortex_manifest(tmp_path)
    assert 500 in result
    _, folder = result[500]
    assert folder == ""


def test_ten_digit_timestamp_not_matched_as_mod_id(tmp_path):
    # 1759006144 has exactly 10 digits — must NOT be treated as mod_id
    # 281 has 3 digits — must be matched
    write_manifest(tmp_path, [
        {"source": "SomeMod-281-v1-1759006144", "relPath": "mods/SomeMod/m.mod"},
    ])
    result = nexmod.parse_vortex_manifest(tmp_path)
    assert 281 in result
    assert 1759006144 not in result


def test_missing_source_field_skipped(tmp_path):
    write_manifest(tmp_path, [
        {"relPath": "mods/X/x.mod"},  # no "source" key
    ])
    assert nexmod.parse_vortex_manifest(tmp_path) == {}
