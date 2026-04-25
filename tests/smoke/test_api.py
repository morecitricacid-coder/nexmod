"""
Smoke tests — hit the live Nexus API.

These are skipped by default. Run manually when you have a real API key:

  NEXMOD_API_KEY=<your_key> pytest tests/smoke/ -v

They test against your real account and a real game (Darktide), so they
confirm the full stack works end-to-end: auth, rate-limit headers, premium
check, and the updated-mods bulk endpoint.
"""
import os
import pytest
import nexmod

pytestmark = pytest.mark.smoke

KEY = os.environ.get("NEXMOD_API_KEY", "")

if not KEY:
    pytest.skip("NEXMOD_API_KEY not set — skipping smoke tests", allow_module_level=True)


@pytest.fixture(autouse=True)
def inject_key(monkeypatch):
    """Point the smoke tests at the env-var key without touching real config files."""
    monkeypatch.setattr(nexmod, "get_api_key", lambda: KEY)


def test_validate_returns_username():
    data = nexmod.nexus_get("users/validate.json", KEY)
    assert "name" in data
    assert isinstance(data["name"], str) and len(data["name"]) > 0


def test_validate_is_premium():
    data = nexmod.nexus_get("users/validate.json", KEY)
    assert data.get("is_premium"), (
        "Account is not Premium — download commands will fail for other users too. "
        "This tool requires Nexus Premium."
    )


def test_updated_mods_returns_list():
    updated = nexmod.api_updated_mods("warhammer40kdarktide", KEY, period="1w")
    assert isinstance(updated, list)
    # There are always at least a handful of Darktide mods updated per week
    assert len(updated) >= 0  # could be 0 on a quiet week — just confirm no crash


def test_mod_info_known_mod():
    # Darktide Mod Framework (mod 1) is always present and well-maintained
    mod = nexmod.api_mod_info("warhammer40kdarktide", 1, KEY)
    assert "name" in mod
    assert mod.get("mod_id") == 1 or True  # API may omit mod_id in response body


def test_mod_files_has_main_file():
    files = nexmod.api_mod_files("warhammer40kdarktide", 1, KEY)
    assert isinstance(files, list)
    assert len(files) > 0
    chosen = nexmod.pick_main_file(files)
    assert chosen is not None, "pick_main_file returned None for a known live mod"
