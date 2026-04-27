"""
Integration tests — end-to-end flows that exercise multiple subsystems together.

Each test wires real archive fixtures + mocked HTTP responses through the full
public CLI. The point is to catch regressions where individual unit tests pass
but the wired-up flow has a gap (DB state vs filesystem vs load order, etc.).
"""
import json
import zipfile
import tarfile
import io
import pytest
import responses as resp_lib
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import nexmod
from nexmod import cli

pytestmark = pytest.mark.usefixtures("isolated_dirs")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cache_redirect(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(nexmod, "CACHE_DIR", cache)
    return cache


@pytest.fixture
def mod_dir_fixture(tmp_path, monkeypatch):
    """Stub resolve_mod_dir to return a temp dir we control."""
    mods = tmp_path / "game_mods"
    mods.mkdir()
    monkeypatch.setattr(nexmod, "resolve_mod_dir", lambda g, db: mods)
    return mods


def _make_zip(payload: dict) -> bytes:
    """Build an in-memory zip from {path_in_archive: text_content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in payload.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _make_tar_gz(payload: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in payload.items():
            data = body.encode()
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _set_api_key():
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "TESTKEY"}))


def _mock_install_endpoints(rsps, mod_id, file_id, archive_bytes,
                            game_domain="warhammer40kdarktide",
                            mod_name="TestMod", version="1.0",
                            file_name="testmod.zip", category="MAIN", md5=None):
    """Wire up the four API endpoints needed for a full install flow."""
    api = nexmod.NEXUS_API
    rsps.add(resp_lib.GET, f"{api}/games/{game_domain}/mods/{mod_id}.json",
             json={"name": mod_name, "version": version, "author": "x"})
    rsps.add(resp_lib.GET, f"{api}/games/{game_domain}/mods/{mod_id}/files.json",
             json={"files": [{
                 "file_id": file_id, "file_name": file_name,
                 "category_name": category, "size_kb": len(archive_bytes) // 1024 or 1,
                 "uploaded_timestamp": 1, "md5": md5 or "",
             }]})
    rsps.add(
        resp_lib.GET,
        f"{api}/games/{game_domain}/mods/{mod_id}/files/{file_id}/download_link.json",
        json=[{"URI": "https://cdn.example.com/test.zip", "short_name": "primary"}],
    )
    rsps.add(resp_lib.GET, "https://cdn.example.com/test.zip",
             body=archive_bytes,
             headers={"content-length": str(len(archive_bytes))})


# ── happy-path install ───────────────────────────────────────────────────────

def test_install_zip_end_to_end(runner, mod_dir_fixture, cache_redirect):
    _set_api_key()
    archive = _make_zip({
        "MyMod/init.lua": "return {}",
        "MyMod/MyMod.mod": 'return { name = "Custom HUD" }',
    })

    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=10, file_id=99,
                                archive_bytes=archive,
                                mod_name="Custom HUD")
        result = runner.invoke(cli, ["install", "darktide", "10"])

    assert result.exit_code == 0
    # File extracted
    assert (mod_dir_fixture / "MyMod" / "init.lua").read_text() == "return {}"
    # DB row written with all the right fields
    db = nexmod.get_db()
    row = db.execute("SELECT * FROM mods WHERE mod_id=10").fetchone()
    assert row["name"] == "Custom HUD"
    assert row["version"] == "1.0"
    assert row["folder_name"] == "MyMod"
    # Snapshot saved
    assert any(p.name == "1.0.zip"
               for p in nexmod._list_snapshots("darktide", 10))


def test_install_tar_gz_end_to_end(runner, mod_dir_fixture, cache_redirect):
    _set_api_key()
    archive = _make_tar_gz({
        "TarMod/data.txt": "tar content",
    })

    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(
            rsps, mod_id=11, file_id=100, archive_bytes=archive,
            mod_name="TarMod", file_name="tarmod.tar.gz",
        )
        # Override URL to .tar.gz so extension routing picks tarfile path
        rsps.replace(resp_lib.GET, "https://cdn.example.com/test.zip",
                     body=archive,
                     headers={"content-length": str(len(archive))})
        result = runner.invoke(cli, ["install", "darktide", "11"])

    assert result.exit_code == 0
    assert (mod_dir_fixture / "TarMod" / "data.txt").read_text() == "tar content"


# ── path-traversal rejection ─────────────────────────────────────────────────

def test_install_rejects_path_traversal(runner, mod_dir_fixture, cache_redirect):
    """Zip with ../escape entry must be rejected before extraction."""
    _set_api_key()
    # Build a zip with a malicious ".." entry
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/passwd", "pwned")
    bad_zip = buf.getvalue()

    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=12, file_id=101,
                                archive_bytes=bad_zip)
        result = runner.invoke(cli, ["install", "darktide", "12"])

    assert result.exit_code != 0
    # Make sure no file escaped into a parent path
    assert not (mod_dir_fixture.parent / ".." / "etc" / "passwd").exists()


def test_install_rejects_absolute_path(runner, mod_dir_fixture, cache_redirect):
    _set_api_key()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/abs/path", "x")
    bad_zip = buf.getvalue()

    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=13, file_id=102,
                                archive_bytes=bad_zip)
        result = runner.invoke(cli, ["install", "darktide", "13"])
    assert result.exit_code != 0


# ── corrupted archive ────────────────────────────────────────────────────────

def test_install_corrupted_zip_fails_cleanly(runner, mod_dir_fixture, cache_redirect):
    _set_api_key()
    bad = b"this is not a real zip file"

    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=14, file_id=103,
                                archive_bytes=bad)
        result = runner.invoke(cli, ["install", "darktide", "14"])

    assert result.exit_code != 0
    # No DB row — install must not have committed
    db = nexmod.get_db()
    assert db.execute("SELECT COUNT(*) FROM mods WHERE mod_id=14").fetchone()[0] == 0


# ── multi-mod install + collision detection ──────────────────────────────────

def test_second_install_detects_collision(runner, mod_dir_fixture, cache_redirect):
    """Install mod A → folder MyMod. Install mod B with archive that contains
    MyMod → conflict warning fires."""
    _set_api_key()
    archive_a = _make_zip({"MyMod/file_a.txt": "a"})
    archive_b = _make_zip({"MyMod/file_b.txt": "b"})  # same top dir

    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=20, file_id=200,
                                archive_bytes=archive_a, mod_name="ModA")
        result_a = runner.invoke(cli, ["install", "darktide", "20"])
    assert result_a.exit_code == 0

    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=21, file_id=201,
                                archive_bytes=archive_b, mod_name="ModB")
        # Decline the conflict prompt → install aborts, ModA's file untouched
        result_b = runner.invoke(cli, ["install", "darktide", "21"], input="n\n")

    assert "already claimed" in result_b.output
    assert (mod_dir_fixture / "MyMod" / "file_a.txt").exists()
    db = nexmod.get_db()
    assert db.execute("SELECT COUNT(*) FROM mods WHERE mod_id=21").fetchone()[0] == 0


# ── full update flow ─────────────────────────────────────────────────────────

def test_update_picks_up_new_version(runner, mod_dir_fixture, cache_redirect):
    """Install v1.0, then mock the API to return v1.1, run update — verify
    DB version + file content updated, snapshot saved for both."""
    _set_api_key()
    a_v1 = _make_zip({"MyMod/v.txt": "version1"})
    a_v2 = _make_zip({"MyMod/v.txt": "version2"})

    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=30, file_id=300,
                                archive_bytes=a_v1, version="1.0")
        runner.invoke(cli, ["install", "darktide", "30"])

    # Now mock v1.1
    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=30, file_id=301,
                                archive_bytes=a_v2, version="1.1")
        result = runner.invoke(cli, ["update", "darktide", "--mod-id", "30", "-y"])

    assert result.exit_code == 0
    assert (mod_dir_fixture / "MyMod" / "v.txt").read_text() == "version2"

    db = nexmod.get_db()
    row = db.execute("SELECT version FROM mods WHERE mod_id=30").fetchone()
    assert row["version"] == "1.1"

    # Both versions snapshotted
    snaps = nexmod._list_snapshots("darktide", 30)
    labels = {nexmod._snapshot_version_label(s) for s in snaps}
    assert {"1.0", "1.1"}.issubset(labels)


# ── rollback flow after update ───────────────────────────────────────────────

def test_install_update_rollback_round_trip(runner, mod_dir_fixture, cache_redirect):
    _set_api_key()
    a_v1 = _make_zip({"MyMod/v.txt": "v1"})
    a_v2 = _make_zip({"MyMod/v.txt": "v2"})

    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=40, file_id=400,
                                archive_bytes=a_v1, version="1.0")
        runner.invoke(cli, ["install", "darktide", "40"])
    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=40, file_id=401,
                                archive_bytes=a_v2, version="1.1")
        runner.invoke(cli, ["update", "darktide", "--mod-id", "40", "-y"])

    assert (mod_dir_fixture / "MyMod" / "v.txt").read_text() == "v2"

    result = runner.invoke(cli, ["rollback", "darktide", "40", "--yes"])
    assert result.exit_code == 0
    assert (mod_dir_fixture / "MyMod" / "v.txt").read_text() == "v1"
    db = nexmod.get_db()
    row = db.execute("SELECT version FROM mods WHERE mod_id=40").fetchone()
    assert row["version"] == "1.0"


# ── remove --purge flow ──────────────────────────────────────────────────────

def test_install_then_purge_removes_files_and_db(runner, mod_dir_fixture, cache_redirect):
    _set_api_key()
    archive = _make_zip({"MyMod/x.txt": "hi"})
    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=50, file_id=500,
                                archive_bytes=archive)
        runner.invoke(cli, ["install", "darktide", "50"])

    assert (mod_dir_fixture / "MyMod" / "x.txt").exists()

    result = runner.invoke(
        cli, ["remove", "darktide", "50", "--purge", "--yes"]
    )
    assert result.exit_code == 0
    assert not (mod_dir_fixture / "MyMod").exists()
    db = nexmod.get_db()
    assert db.execute("SELECT COUNT(*) FROM mods WHERE mod_id=50").fetchone()[0] == 0


# ── NXM URI routes through install ───────────────────────────────────────────

def test_nxm_uri_dispatches_to_install(runner, mod_dir_fixture, cache_redirect):
    _set_api_key()
    archive = _make_zip({"NxmMod/x.txt": "from-nxm"})
    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(rsps, mod_id=60, file_id=600,
                                archive_bytes=archive, mod_name="NxmMod")
        result = runner.invoke(cli, [
            "nxm",
            "nxm://warhammer40kdarktide/mods/60/files/600?key=k&expires=1&user_id=2",
        ])
    assert result.exit_code == 0
    assert (mod_dir_fixture / "NxmMod" / "x.txt").read_text() == "from-nxm"
    db = nexmod.get_db()
    row = db.execute("SELECT * FROM mods WHERE mod_id=60").fetchone()
    assert row["name"] == "NxmMod"
    # NXM dispatch should pin the exact file_id, not let pick_main_file choose
    assert row["file_id"] == 600


# ── disk-space pre-flight blocks install ─────────────────────────────────────

def test_install_blocked_by_disk_space(runner, mod_dir_fixture, cache_redirect):
    _set_api_key()
    archive = _make_zip({"MyMod/x.txt": "y"})
    fake = __import__("shutil")._ntuple_diskusage(total=1, used=1, free=0)
    # assert_all_requests_are_fired=False because the disk check correctly
    # aborts BEFORE the CDN GET — that's the entire point of the test.
    with resp_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _mock_install_endpoints(rsps, mod_id=70, file_id=700,
                                archive_bytes=archive)
        with patch("nexmod.shutil.disk_usage", return_value=fake):
            result = runner.invoke(cli, ["install", "darktide", "70"])
    assert result.exit_code != 0
    # Error path raises a RuntimeError whose message contains "disk space"
    assert result.exception is not None
    assert "disk space" in str(result.exception).lower()
    db = nexmod.get_db()
    assert db.execute("SELECT COUNT(*) FROM mods WHERE mod_id=70").fetchone()[0] == 0


# ── md5 mismatch aborts cleanly ──────────────────────────────────────────────

def test_install_md5_mismatch_aborts(runner, mod_dir_fixture, cache_redirect):
    _set_api_key()
    archive = _make_zip({"MyMod/x.txt": "y"})
    with resp_lib.RequestsMock() as rsps:
        _mock_install_endpoints(
            rsps, mod_id=80, file_id=800, archive_bytes=archive,
            md5="deadbeefdeadbeefdeadbeefdeadbeef",  # wrong
        )
        result = runner.invoke(cli, ["install", "darktide", "80"])
    assert result.exit_code != 0
    assert result.exception is not None
    assert "md5" in str(result.exception).lower() or "mismatch" in str(result.exception).lower()
    db = nexmod.get_db()
    assert db.execute("SELECT COUNT(*) FROM mods WHERE mod_id=80").fetchone()[0] == 0
