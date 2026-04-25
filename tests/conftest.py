"""
Shared fixtures for nexmod tests.

isolated_dirs: redirects all nexmod path constants (CONFIG_FILE, DB_FILE, LOG_FILE, etc.)
               to a fresh tmp_path directory, and clears log handlers after each test.
               Apply per-module with: pytestmark = pytest.mark.usefixtures("isolated_dirs")
"""
import json
import pytest
from click.testing import CliRunner
import nexmod


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """Redirect nexmod's data/config paths to a temp dir for full test isolation."""
    config_dir = tmp_path / "config"
    data_dir   = tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()

    monkeypatch.setattr(nexmod, "CONFIG_DIR",  config_dir)
    monkeypatch.setattr(nexmod, "DATA_DIR",    data_dir)
    monkeypatch.setattr(nexmod, "CONFIG_FILE", config_dir / "config.json")
    monkeypatch.setattr(nexmod, "DB_FILE",     data_dir  / "mods.db")
    monkeypatch.setattr(nexmod, "LOG_FILE",    data_dir  / "nexmod.log")
    monkeypatch.setattr(nexmod, "WINE_PREFIX", data_dir  / "wine-prefix")

    yield tmp_path

    # Prevent RotatingFileHandler objects from accumulating across tests.
    nexmod.log.handlers.clear()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def api_key_config():
    """Write a fake API key into the already-redirected config file."""
    nexmod.CONFIG_FILE.write_text(json.dumps({"api_key": "FAKEKEY000"}))
