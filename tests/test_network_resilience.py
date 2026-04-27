"""
Tests for Phase C — network resilience:
  - nexus_get retry on timeout/connection error / 5xx / 429
  - download_file resume via Range header
  - _try_download_with_mirrors fallback chain
"""
import pytest
import requests
import responses as resp_lib
from pathlib import Path
import nexmod

pytestmark = pytest.mark.usefixtures("isolated_dirs")


NX_URL = f"{nexmod.NEXUS_API}/games/x/mods/1.json"
CDN = "https://cdn.example.com/mod.zip"


# ── nexus_get retries ────────────────────────────────────────────────────────

def test_nexus_get_succeeds_first_try():
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, NX_URL, json={"name": "ok"}, status=200)
        out = nexmod.nexus_get("games/x/mods/1.json", "k")
    assert out == {"name": "ok"}


def test_nexus_get_retries_on_500_then_succeeds():
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, NX_URL, status=500)
        rsps.add(resp_lib.GET, NX_URL, json={"name": "ok"}, status=200)
        out = nexmod.nexus_get("games/x/mods/1.json", "k")
    assert out == {"name": "ok"}


def test_nexus_get_retries_on_429_with_retry_after():
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, NX_URL, status=429,
                 headers={"Retry-After": "1"})
        rsps.add(resp_lib.GET, NX_URL, json={"name": "ok"}, status=200)
        out = nexmod.nexus_get("games/x/mods/1.json", "k")
    assert out == {"name": "ok"}


def test_nexus_get_exhausts_429_then_exits():
    with resp_lib.RequestsMock() as rsps:
        for _ in range(nexmod.NEXUS_MAX_RETRIES):
            rsps.add(resp_lib.GET, NX_URL, status=429,
                     headers={"Retry-After": "0"})
        with pytest.raises(SystemExit):
            nexmod.nexus_get("games/x/mods/1.json", "k")


def test_nexus_get_403_no_retry():
    """403 = auth issue, won't self-heal — must fail immediately."""
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, NX_URL, status=403, body="forbidden")
        with pytest.raises(SystemExit):
            nexmod.nexus_get("games/x/mods/1.json", "k")
        # Confirm we only made 1 request, not max_retries
        assert len(rsps.calls) == 1


def test_nexus_get_retries_on_connection_error_then_succeeds():
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, NX_URL,
                 body=requests.exceptions.ConnectionError("boom"))
        rsps.add(resp_lib.GET, NX_URL, json={"name": "ok"}, status=200)
        out = nexmod.nexus_get("games/x/mods/1.json", "k")
    assert out == {"name": "ok"}


def test_nexus_get_exhausts_5xx_then_exits():
    with resp_lib.RequestsMock() as rsps:
        for _ in range(nexmod.NEXUS_MAX_RETRIES):
            rsps.add(resp_lib.GET, NX_URL, status=503)
        with pytest.raises(SystemExit):
            nexmod.nexus_get("games/x/mods/1.json", "k")


# ── download_file resume + retry ─────────────────────────────────────────────

def test_download_file_completes_on_first_try(tmp_path):
    body = b"abc" * 1000
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, CDN, body=body,
                 headers={"content-length": str(len(body))})
        dest = tmp_path / "x.zip"
        nexmod.download_file(CDN, dest)
    assert dest.read_bytes() == body


def test_download_file_resumes_on_retry(tmp_path):
    """First attempt: connection drops mid-stream. Second attempt: Range
    header sent, server returns 206 with the remainder. File assembled correctly."""
    body = b"0123456789" * 100  # 1000 bytes
    half = body[:500]
    rest = body[500:]

    with resp_lib.RequestsMock() as rsps:
        # Attempt 1: server returns truncated content with full content-length
        # → triggers size mismatch → retry
        rsps.add(resp_lib.GET, CDN, body=half,
                 headers={"content-length": str(len(body))})
        # Attempt 2: Range request → server returns 206 with rest
        rsps.add(
            resp_lib.GET, CDN, body=rest, status=206,
            headers={"Content-Range": f"bytes 500-999/{len(body)}"},
        )
        dest = tmp_path / "x.zip"
        nexmod.download_file(CDN, dest)
    assert dest.read_bytes() == body


def test_download_file_server_ignores_range_restarts_fresh(tmp_path):
    """If we request a Range but server returns 200 (full content), .part is
    deleted and the download starts over."""
    body = b"X" * 1000
    half = b"X" * 500

    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, CDN, body=half,
                 headers={"content-length": str(len(body))})  # mismatch → retry
        rsps.add(resp_lib.GET, CDN, body=body,
                 headers={"content-length": str(len(body))}, status=200)
        dest = tmp_path / "x.zip"
        nexmod.download_file(CDN, dest)
    assert dest.read_bytes() == body


def test_download_file_exhausts_retries_then_raises(tmp_path):
    with resp_lib.RequestsMock() as rsps:
        for _ in range(nexmod.DOWNLOAD_MAX_RETRIES):
            rsps.add(resp_lib.GET, CDN,
                     body=requests.exceptions.ConnectionError("net down"))
        dest = tmp_path / "x.zip"
        with pytest.raises(RuntimeError, match="Download failed"):
            nexmod.download_file(CDN, dest)
    assert not dest.exists()
    assert not dest.with_suffix(".zip.part").exists()


# ── _try_download_with_mirrors ───────────────────────────────────────────────

def test_mirrors_first_succeeds(tmp_path):
    body = b"x" * 100
    urls = [
        {"URI": "https://mirror1.example.com/x.zip", "short_name": "m1"},
        {"URI": "https://mirror2.example.com/x.zip", "short_name": "m2"},
    ]
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, urls[0]["URI"], body=body,
                 headers={"content-length": str(len(body))})
        nexmod._try_download_with_mirrors(urls, tmp_path / "x.zip")
        # Mirror 2 was never called
        assert len(rsps.calls) == 1


def test_mirrors_first_fails_second_succeeds(tmp_path):
    body = b"x" * 100
    urls = [
        {"URI": "https://mirror1.example.com/x.zip", "short_name": "m1"},
        {"URI": "https://mirror2.example.com/x.zip", "short_name": "m2"},
    ]
    with resp_lib.RequestsMock() as rsps:
        # Mirror 1: fail all retries
        for _ in range(nexmod.DOWNLOAD_MAX_RETRIES):
            rsps.add(resp_lib.GET, urls[0]["URI"],
                     body=requests.exceptions.ConnectionError("dead"))
        # Mirror 2: success
        rsps.add(resp_lib.GET, urls[1]["URI"], body=body,
                 headers={"content-length": str(len(body))})

        nexmod._try_download_with_mirrors(urls, tmp_path / "x.zip")
    assert (tmp_path / "x.zip").read_bytes() == body


def test_mirrors_all_fail_raises(tmp_path):
    urls = [
        {"URI": "https://m1.example.com/x.zip"},
        {"URI": "https://m2.example.com/x.zip"},
    ]
    with resp_lib.RequestsMock() as rsps:
        for u in urls:
            for _ in range(nexmod.DOWNLOAD_MAX_RETRIES):
                rsps.add(resp_lib.GET, u["URI"],
                         body=requests.exceptions.ConnectionError("dead"))
        with pytest.raises(RuntimeError, match="All 2 mirrors failed"):
            nexmod._try_download_with_mirrors(urls, tmp_path / "x.zip")


def test_mirrors_skips_entries_with_no_url(tmp_path):
    body = b"x"
    urls = [
        {"foo": "bar"},  # no URI/url key
        {"URI": "https://m2.example.com/x.zip"},
    ]
    with resp_lib.RequestsMock() as rsps:
        rsps.add(resp_lib.GET, urls[1]["URI"], body=body,
                 headers={"content-length": "1"})
        nexmod._try_download_with_mirrors(urls, tmp_path / "x.zip")
    assert (tmp_path / "x.zip").read_bytes() == body
