"""
Tests for download_file and extract_archive.

HTTP is mocked via the `responses` library — no real network calls.
Archive operations use real zip/tar files created in tmp_path.
"""
import tarfile
import zipfile
import pytest
import responses as resp_lib
from pathlib import Path
import nexmod

pytestmark = pytest.mark.usefixtures("isolated_dirs")

CDN = "https://cdn.example.com/mod.zip"


# ── download_file ─────────────────────────────────────────────────────────────

def test_download_success(tmp_path):
    content = b"fake mod archive data" * 500
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, CDN, body=content,
                 headers={"content-length": str(len(content))})
        dest = tmp_path / "mod.zip"
        nexmod.download_file(CDN, dest)

    assert dest.exists()
    assert dest.stat().st_size == len(content)


def test_download_size_mismatch_cleans_up(tmp_path):
    # When content-length doesn't match the actual body, requests raises IncompleteRead
    # before our size-check code runs. Either way a RuntimeError is raised and the
    # partial file must be cleaned up.
    content = b"short"
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, CDN, body=content,
                 headers={"content-length": "99999"})
        dest = tmp_path / "mod.zip"
        with pytest.raises(RuntimeError):
            nexmod.download_file(CDN, dest)

    assert not dest.exists()  # partial file must be cleaned up regardless


def test_download_no_content_length_succeeds(tmp_path):
    content = b"data without length header"
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, CDN, body=content)  # no content-length header
        dest = tmp_path / "mod.zip"
        nexmod.download_file(CDN, dest)

    assert dest.exists()


def test_download_writes_via_part_file(tmp_path):
    # The canonical path must not exist while bytes are streaming. We can't
    # observe the inflight state easily, but we can confirm there's no
    # leftover .part file after a successful download.
    content = b"final bytes"
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, CDN, body=content,
                 headers={"content-length": str(len(content))})
        dest = tmp_path / "mod.zip"
        nexmod.download_file(CDN, dest)

    assert dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_download_failure_leaves_no_partial(tmp_path):
    # On a size-mismatch failure, neither dest nor dest.part should remain.
    content = b"short"
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, CDN, body=content,
                 headers={"content-length": "99999"})
        dest = tmp_path / "mod.zip"
        with pytest.raises(RuntimeError):
            nexmod.download_file(CDN, dest)

    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


# ── verify_md5 ────────────────────────────────────────────────────────────────

def test_verify_md5_passes_on_match(tmp_path):
    import hashlib
    archive = tmp_path / "mod.zip"
    archive.write_bytes(b"some payload")
    expected = hashlib.md5(b"some payload").hexdigest()
    nexmod.verify_md5(archive, expected)  # must not raise
    assert archive.exists()


def test_verify_md5_raises_on_mismatch_and_unlinks(tmp_path):
    archive = tmp_path / "mod.zip"
    archive.write_bytes(b"some payload")
    with pytest.raises(RuntimeError, match="MD5 mismatch"):
        nexmod.verify_md5(archive, "0" * 32)
    # File is unlinked so a stale archive can't be reused on retry.
    assert not archive.exists()


def test_verify_md5_is_case_insensitive(tmp_path):
    import hashlib
    archive = tmp_path / "mod.zip"
    archive.write_bytes(b"payload")
    expected = hashlib.md5(b"payload").hexdigest().upper()  # API may give upper-case
    nexmod.verify_md5(archive, expected)  # must not raise


# ── extract_archive — zip ─────────────────────────────────────────────────────

def make_zip(path: Path, members: dict) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_extract_zip_normal(tmp_path):
    archive = tmp_path / "mod.zip"
    make_zip(archive, {
        "ModName/init.lua":  "-- mod code",
        "ModName/data.json": "{}",
    })
    target = tmp_path / "out"
    nexmod.extract_archive(archive, target)
    assert (target / "ModName" / "init.lua").exists()
    assert (target / "ModName" / "data.json").exists()


def test_extract_zip_path_traversal_blocked(tmp_path):
    archive = tmp_path / "evil.zip"
    make_zip(archive, {"../../../evil.txt": "malicious"})
    with pytest.raises(RuntimeError, match="Unsafe path"):
        nexmod.extract_archive(archive, tmp_path / "out")


def test_extract_zip_absolute_path_blocked(tmp_path):
    archive = tmp_path / "evil.zip"
    make_zip(archive, {"/etc/passwd": "malicious"})
    with pytest.raises(RuntimeError, match="Unsafe path"):
        nexmod.extract_archive(archive, tmp_path / "out")


# ── extract_archive — tar ─────────────────────────────────────────────────────

def make_tar_gz(path: Path, members: dict) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            import io
            buf = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name=name)
            info.size = len(buf)
            tf.addfile(info, io.BytesIO(buf))


def test_extract_tar_gz_normal(tmp_path):
    archive = tmp_path / "mod.tar.gz"
    make_tar_gz(archive, {"ModName/init.lua": "-- mod code"})
    target = tmp_path / "out"
    nexmod.extract_archive(archive, target)
    assert (target / "ModName" / "init.lua").exists()


# ── extract_archive — unknown extension ──────────────────────────────────────

def test_extract_unknown_extension_copies_file(tmp_path):
    """Unknown extensions (not .rar) are still copied as-is."""
    archive = tmp_path / "mod.dat"
    archive.write_bytes(b"fake data")
    target = tmp_path / "out"
    target.mkdir()
    nexmod.extract_archive(archive, target)
    assert (target / "mod.dat").exists()


def test_extract_rar_raises_clear_error(tmp_path):
    """.rar archives must raise RuntimeError instead of silently copying."""
    archive = tmp_path / "mod.rar"
    archive.write_bytes(b"fake rar data")
    target = tmp_path / "out"
    target.mkdir()
    with pytest.raises(RuntimeError, match=r"\.rar"):
        nexmod.extract_archive(archive, target)
    # Raw .rar must NOT be left in target_dir
    assert not (target / "mod.rar").exists()


# ── extract_archive — 7z not installed ───────────────────────────────────────

def test_extract_7z_not_found_gives_install_hint(tmp_path, mocker):
    archive = tmp_path / "mod.7z"
    archive.write_bytes(b"fake 7z data")
    mocker.patch("shutil.which", return_value=None)
    with pytest.raises(RuntimeError, match="7z not found"):
        nexmod.extract_archive(archive, tmp_path / "out")
