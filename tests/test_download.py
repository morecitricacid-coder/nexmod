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
    archive = tmp_path / "mod.rar"
    archive.write_bytes(b"fake rar data")
    target = tmp_path / "out"
    target.mkdir()
    nexmod.extract_archive(archive, target)
    assert (target / "mod.rar").exists()


# ── extract_archive — 7z not installed ───────────────────────────────────────

def test_extract_7z_not_found_gives_install_hint(tmp_path, mocker):
    archive = tmp_path / "mod.7z"
    archive.write_bytes(b"fake 7z data")
    mocker.patch("shutil.which", return_value=None)
    with pytest.raises(RuntimeError, match="7z not found"):
        nexmod.extract_archive(archive, tmp_path / "out")
