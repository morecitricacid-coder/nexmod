#!/usr/bin/env python3
"""nexmod — Nexus Mods CLI for Linux"""

import click
import requests
import sqlite3
import json
import io
import os
import struct
import subprocess
import zipfile
import tarfile
import shutil
import sys
import hashlib
import logging
import re
import fcntl
import tempfile
import time
import difflib
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, DownloadColumn, TransferSpeedColumn, BarColumn, TextColumn
from rich.syntax import Syntax

__version__ = "1.0.1"

console = Console()
log = logging.getLogger("nexmod")

CONFIG_DIR   = Path.home() / ".config" / "nexmod"
DATA_DIR     = Path.home() / ".local" / "share" / "nexmod"
CONFIG_FILE  = CONFIG_DIR / "config.json"
PROFILES_DIR = CONFIG_DIR / "profiles"
DB_FILE      = DATA_DIR / "mods.db"
LOG_FILE     = DATA_DIR / "nexmod.log"
WINE_PREFIX  = DATA_DIR / "wine-prefix"

NEXUS_API = "https://api.nexusmods.com/v1"

NEXUS_URL_RE = re.compile(
    r'nexusmods\.com/([^/?#]+)/mods/(\d+)(?:/files/(\d+))?',
    re.IGNORECASE,
)

if sys.version_info < (3, 10):
    print("nexmod requires Python 3.10 or later. "
          "Check your distro's python3 package or use pyenv.", file=sys.stderr)
    sys.exit(1)

GAMES = {
    "darktide": {
        "name": "Warhammer 40,000: Darktide",
        "domain": "warhammer40kdarktide",
        "steam_id": 1361210,
        "mod_subdir": "mods",
        "log_subpath": "Fatshark/Darktide/console_log.txt",
        "load_order_file": "mod_load_order.txt",
    },
    "skyrimse": {
        "name": "Skyrim Special Edition",
        "domain": "skyrimspecialedition",
        "steam_id": 489830,
        "mod_subdir": "Data",
        "log_subpath": None,
    },
    "bg3": {
        "name": "Baldur's Gate 3",
        "domain": "baldursgate3",
        "steam_id": 1086940,
        "mod_subdir": "Mods",
        "log_subpath": "Larian Studios/Baldur's Gate 3/Log.log",
    },
    "cyberpunk2077": {
        "name": "Cyberpunk 2077",
        "domain": "cyberpunk2077",
        "steam_id": 1091500,
        "mod_subdir": "archive/pc/mod",
        "log_subpath": None,
    },
    "fallout4": {
        "name": "Fallout 4",
        "domain": "fallout4",
        "steam_id": 377160,
        "mod_subdir": "Data",
        "log_subpath": None,
    },
    "starfield": {
        "name": "Starfield",
        "domain": "starfield",
        "steam_id": 1716740,
        "mod_subdir": "Data",
        "log_subpath": None,
        # Bethesda plugin system: .esp/.esm/.esl files tracked + Plugins.txt managed
        "plugin_exts": (".esm", ".esp", ".esl"),
        # Relative to AppData/Local inside the Proton prefix
        "appdata_plugins_txt": "Starfield/plugins.txt",
        # These are always first in Plugins.txt — nexmod never touches their order
        "official_masters": (
            "Starfield.esm",
            "BlueprintShips-Starfield.esm",
            "OldMars.esm",
            "SFBGS003.esm",
            "SFBGS004.esm",
            "SFBGS006.esm",
            "SFBGS007.esm",
            "SFBGS008.esm",
        ),
    },
}


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)

    # Always write DEBUG+ to rotating file (5 MB × 3 backups)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)

    # Console handler only when --verbose
    if verbose:
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(logging.Formatter("[dim]%(levelname)s %(message)s[/dim]"))
        log.addHandler(ch)


# ── Now ───────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text())

def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    CONFIG_FILE.chmod(0o600)

def get_api_key() -> str:
    cfg = load_config()
    key = cfg.get("api_key")
    if not key:
        console.print("[red]No API key set.[/red]")
        console.print("Get yours: nexusmods.com → avatar → Settings → API Keys")
        console.print("Then run:  nexmod config set-key <key>")
        sys.exit(1)
    return key

def get_auto_reorder() -> bool:
    return load_config().get("auto_reorder", True)


def _is_interactive() -> bool:
    """True only when both stdin and stdout are real TTYs.

    Used to gate interactive prompts: CLI paths reachable from cron, systemd,
    pipes, or test runners must fall back to non-interactive behavior.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    # WAL: readers don't block writers (e.g. tail-style log views during update)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mods (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game        TEXT NOT NULL,
            mod_id      INTEGER NOT NULL,
            file_id     INTEGER NOT NULL,
            name        TEXT NOT NULL,
            version     TEXT,
            filename    TEXT,
            mod_dir     TEXT,
            tracked_at  TEXT NOT NULL,
            updated_at  TEXT,
            UNIQUE(game, mod_id)
        );
        CREATE TABLE IF NOT EXISTS game_paths (
            game TEXT PRIMARY KEY,
            path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action    TEXT NOT NULL,
            game      TEXT NOT NULL,
            mod_id    INTEGER,
            mod_name  TEXT,
            version   TEXT,
            status    TEXT NOT NULL,
            detail    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_history_game_ts
            ON history(game, timestamp);
        CREATE TABLE IF NOT EXISTS load_order_state (
            game            TEXT PRIMARY KEY,
            file_path       TEXT NOT NULL,
            last_hash       TEXT,
            last_written_at TEXT,
            frozen          INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS load_order_pins (
            game        TEXT NOT NULL,
            folder      TEXT NOT NULL,
            position    TEXT NOT NULL CHECK(position IN ('top','bottom','before','after')),
            relative_to TEXT,
            source      TEXT NOT NULL DEFAULT 'user',
            created_at  TEXT NOT NULL,
            PRIMARY KEY (game, folder)
        );
    """)
    _apply_migrations(conn)
    return conn


# Schema migrations: append-only list. Each entry runs exactly once and is
# recorded in schema_migrations. To change schema, ADD a new entry — never
# reorder, never delete. Forward-fix instead.
def _migration_003_plugin_files(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS plugin_files (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game        TEXT NOT NULL,
            mod_id      INTEGER NOT NULL,
            plugin_name TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            added_at    TEXT NOT NULL,
            UNIQUE(game, plugin_name)
        );
        CREATE INDEX IF NOT EXISTS idx_plugin_files_game_mod
            ON plugin_files(game, mod_id);
    """)


SCHEMA_MIGRATIONS: list = [
    ("001_mods_folder_name", "ALTER TABLE mods ADD COLUMN folder_name TEXT"),
    ("002_load_order_active_profile", "ALTER TABLE load_order_state ADD COLUMN active_profile TEXT"),
    ("003_plugin_files", _migration_003_plugin_files),
]


def _apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply pending schema migrations. Returns names applied this call.

    Idempotent: legacy schemas where the change already exists (from prior
    defensive ALTER statements) are recorded as applied without re-running.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    fresh: list[str] = []
    for name, action in SCHEMA_MIGRATIONS:
        if name in applied:
            continue
        try:
            if callable(action):
                action(conn)
            else:
                conn.execute(action)
        except sqlite3.OperationalError as e:
            # Legacy schema where column/table already exists from an earlier
            # defensive ALTER. Record as applied so we don't retry forever.
            msg = str(e).lower()
            if "duplicate column" not in msg and "already exists" not in msg:
                raise
            log.info("migration %s already applied (legacy schema)", name)
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (name, now_iso()),
        )
        fresh.append(name)
        log.info("migration applied: %s", name)
    conn.commit()
    return fresh

def record(db: sqlite3.Connection, action: str, game: str, mod_id: int | None,
           mod_name: str | None, version: str | None, status: str, detail: str | None = None):
    db.execute(
        "INSERT INTO history (timestamp,action,game,mod_id,mod_name,version,status,detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (now_iso(), action, game, mod_id, mod_name, version, status, detail),
    )
    db.commit()
    log.info("history action=%s game=%s mod_id=%s name=%r version=%s status=%s detail=%s",
             action, game, mod_id, mod_name, version, status, detail)


# ── Nexus API ─────────────────────────────────────────────────────────────────

# Network resilience tunables. Override via env for tests/diagnostics.
NEXUS_MAX_RETRIES = int(os.environ.get("NEXMOD_API_RETRIES", "3"))
NEXUS_RETRY_BASE_DELAY = float(os.environ.get("NEXMOD_API_RETRY_DELAY", "1.0"))
NEXUS_429_MAX_WAIT = float(os.environ.get("NEXMOD_429_MAX_WAIT", "60.0"))


def _backoff_delay(attempt: int, base: float = NEXUS_RETRY_BASE_DELAY) -> float:
    """Exponential backoff: base, base*2, base*4, ..."""
    return base * (2 ** (attempt - 1))


def _norm_version(v: str) -> str:
    """Normalize a mod version string for comparison.

    Strips leading 'v', lowercases, and removes trailing '.0' segments so that
    "1.0" == "1.00" == "1" and "v1.2" == "1.2" while "1.2.1" stays "1.2.1".
    """
    v = (v or "").strip().lower().lstrip("v")
    parts = v.split(".")
    while len(parts) > 1 and parts[-1] in ("0", "00", "000"):
        parts.pop()
    return ".".join(parts)


def nexus_get(endpoint: str, api_key: str) -> dict | list:
    """GET a Nexus API endpoint with retry + 429 backoff.

    Retries on transient errors (timeouts, connection drops, HTTP 5xx, 429).
    Hard-fails (no retry) on 403/401/404 — those won't self-heal. 429 honors
    Retry-After up to NEXUS_429_MAX_WAIT to avoid surprise long sleeps.
    """
    url = f"{NEXUS_API}/{endpoint}"
    log.debug("GET %s", url)

    for attempt in range(1, NEXUS_MAX_RETRIES + 1):
        try:
            r = requests.get(
                url,
                headers={"apikey": api_key, "accept": "application/json"},
                timeout=20,
            )
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            if attempt == NEXUS_MAX_RETRIES:
                log.error("Network error after %d attempts on %s: %s", attempt, url, e)
                console.print(f"[red]Network error after {attempt} attempts: {e}[/red]")
                sys.exit(1)
            delay = _backoff_delay(attempt)
            log.warning("Attempt %d/%d failed (%s) — retrying in %.1fs",
                        attempt, NEXUS_MAX_RETRIES, e.__class__.__name__, delay)
            console.print(
                f"  [yellow]Network blip ({e.__class__.__name__}) — retry "
                f"{attempt}/{NEXUS_MAX_RETRIES} in {delay:.1f}s[/yellow]"
            )
            time.sleep(delay)
            continue

        log.debug("→ HTTP %s remaining=%s",
                  r.status_code, r.headers.get("X-RL-Daily-Remaining", "?"))

        # 429 — honor Retry-After (capped to NEXUS_429_MAX_WAIT)
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After", "30"))
            except ValueError:
                wait = 30.0
            wait = min(wait, NEXUS_429_MAX_WAIT)
            if attempt == NEXUS_MAX_RETRIES:
                hint = r.headers.get("Retry-After", "?")
                log.error("Rate limited; %d retries exhausted (last Retry-After=%s)",
                          attempt, hint)
                console.print(
                    f"[red]Rate limited — exhausted {attempt} retries. "
                    f"Try again in {hint}s.[/red]"
                )
                sys.exit(1)
            log.warning("Rate limited on attempt %d/%d — sleeping %.1fs",
                        attempt, NEXUS_MAX_RETRIES, wait)
            console.print(
                f"  [yellow]Rate limited — sleeping {wait:.0f}s "
                f"(attempt {attempt}/{NEXUS_MAX_RETRIES})[/yellow]"
            )
            time.sleep(wait)
            continue

        # 5xx — transient server error, retry
        if 500 <= r.status_code < 600:
            if attempt == NEXUS_MAX_RETRIES:
                log.error("HTTP %s after %d attempts on %s", r.status_code, attempt, url)
                console.print(
                    f"[red]HTTP {r.status_code} from Nexus API after {attempt} retries[/red]"
                )
                sys.exit(1)
            delay = _backoff_delay(attempt)
            log.warning("HTTP %s on attempt %d/%d — retrying in %.1fs",
                        r.status_code, attempt, NEXUS_MAX_RETRIES, delay)
            console.print(
                f"  [yellow]HTTP {r.status_code} — retry "
                f"{attempt}/{NEXUS_MAX_RETRIES} in {delay:.1f}s[/yellow]"
            )
            time.sleep(delay)
            continue

        # 403/401/404 — hard fail, no retry (won't self-heal)
        if r.status_code == 403:
            log.error("403 Forbidden on %s: %s", url, r.text[:200])
            if "download_link" in url:
                console.print(
                    "[red]403 Forbidden — direct downloads require Nexus Premium.[/red]\n"
                    "  Free users: click [cyan]Mod Manager Download[/cyan] on nexusmods.com "
                    "(requires [cyan]nexmod nxm-register[/cyan]) or use "
                    "[cyan]nexmod install <game> <id> --from-file <archive>[/cyan]."
                )
            else:
                console.print("[red]403 Forbidden — check your Nexus API key.[/red]")
            sys.exit(1)
        if not r.ok:
            snippet = r.text[:300]
            log.error("HTTP %s on %s: %s", r.status_code, url, snippet)
            console.print(f"[red]HTTP {r.status_code} from Nexus API[/red]")
            console.print(f"[dim]{snippet}[/dim]")
            r.raise_for_status()

        return r.json()

    # Unreachable — every loop branch either returns or sys.exits.
    sys.exit(1)

def api_mod_info(domain: str, mod_id: int, api_key: str) -> dict:
    return nexus_get(f"games/{domain}/mods/{mod_id}.json", api_key)

def api_mod_files(domain: str, mod_id: int, api_key: str) -> list:
    data = nexus_get(f"games/{domain}/mods/{mod_id}/files.json", api_key)
    return data.get("files", [])

def api_download_urls(domain: str, mod_id: int, file_id: int, api_key: str, *,
                      nxm_key: str | None = None,
                      nxm_expires: str | None = None,
                      nxm_user_id: str | None = None) -> list:
    endpoint = f"games/{domain}/mods/{mod_id}/files/{file_id}/download_link.json"
    params = [(k, v) for k, v in [("key", nxm_key), ("expires", nxm_expires), ("user_id", nxm_user_id)] if v]
    if params:
        from urllib.parse import urlencode
        endpoint += "?" + urlencode(params)
    return nexus_get(endpoint, api_key)

def api_updated_mods(domain: str, api_key: str, period: str = "1w") -> list:
    return nexus_get(f"games/{domain}/mods/updated.json?period={period}", api_key)

def api_search_mods(domain: str, query: str, api_key: str, count: int = 10) -> list:
    """Search Nexus mods via the v2 GraphQL API.

    Uses ``nameStemmed`` with a WILDCARD ``*query*`` pattern — the ``name``
    field returns zero hits for WILDCARD queries as of 2026-04.

    Args:
        domain:  Nexus game domain slug (e.g. ``warhammer40kdarktide``).
        query:   Free-text search string.
        api_key: Nexus API key.
        count:   Number of results to return (1–50).

    Returns:
        List of result dicts with keys ``modId``, ``name``, ``summary``,
        ``downloads``, ``endorsements``.

    Raises:
        RuntimeError: on non-200 HTTP responses or malformed responses.
    """
    count = max(1, min(50, count))
    url = "https://api.nexusmods.com/v2/graphql"
    gql_query = """
query ModSearch($filter: ModsFilter!, $offset: Int, $count: Int) {
  mods(filter: $filter, offset: $offset, count: $count) {
    totalCount
    nodes {
      modId
      name
      summary
      downloads
      endorsements
    }
  }
}
"""
    payload = {
        "query": gql_query,
        "variables": {
            "filter": {
                "op": "AND",
                "gameDomainName": [{"value": domain, "op": "EQUALS"}],
                "nameStemmed": [{"value": f"*{query}*", "op": "WILDCARD"}],
            },
            "offset": 0,
            "count": count,
        },
    }
    headers = {
        "APIKEY": api_key,
        "Application-Name": "nexmod",
        "Application-Version": __version__,
        "Protocol-Version": "1.0.0",
        "Content-Type": "application/json",
    }
    log.debug("GraphQL search: domain=%s query=%r count=%d", domain, query, count)
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        raise RuntimeError(f"Network error contacting Nexus v2 API: {e}") from e
    if r.status_code != 200:
        raise RuntimeError(
            f"Nexus v2 GraphQL returned HTTP {r.status_code}: {r.text[:200]}"
        )
    try:
        body = r.json()
    except ValueError as e:
        raise RuntimeError(f"Nexus v2 GraphQL returned non-JSON response: {e}") from e
    data = body.get("data") or {}
    mods_block = data.get("mods") or {}
    return mods_block.get("nodes") or []


# ── Steam Path Detection ──────────────────────────────────────────────────────

def find_steam_library_paths() -> list[Path]:
    base_candidates = [
        Path.home() / ".local/share/Steam",                                        # native deb/rpm
        Path.home() / ".steam/steam",                                              # symlink variant
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",       # Flatpak
        Path("/usr/local/share/Steam"),                                            # system-wide
    ]
    libraries = []
    for base in base_candidates:
        if not base.exists():
            continue
        default = base / "steamapps"
        if default.exists() and default not in libraries:
            libraries.append(default)
        vdf = base / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            for line in vdf.read_text().splitlines():
                line = line.strip()
                if '"path"' in line.lower():
                    try:
                        path = line.split('"')[3]
                        p = Path(path) / "steamapps"
                        if p.exists() and p not in libraries:
                            libraries.append(p)
                    except IndexError:
                        pass
    return libraries

def find_game_install(steam_id: int) -> Path | None:
    for lib in find_steam_library_paths():
        manifest = lib / f"appmanifest_{steam_id}.acf"
        if not manifest.exists():
            continue
        for line in manifest.read_text().splitlines():
            if '"installdir"' in line.lower():
                try:
                    dirname = line.split('"')[3]
                    game_path = lib / "common" / dirname
                    if game_path.exists():
                        log.debug("Found game %s at %s", steam_id, game_path)
                        return game_path
                except IndexError:
                    pass
    return None

def find_proton_appdata(steam_id: int) -> Path | None:
    """Return the Wine drive_c/users/steamuser/AppData/Roaming path for a Proton game."""
    _pfx = f"steamapps/compatdata/{steam_id}/pfx/drive_c/users/steamuser/AppData/Roaming"
    search_roots = [
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",  # Flatpak
    ]
    for root in search_roots:
        candidate = root / _pfx
        if candidate.exists():
            return candidate
    return None


def find_appdata_local(steam_id: int) -> Path | None:
    """Return the Wine AppData/Local path for a Proton game (different from Roaming)."""
    _pfx = f"steamapps/compatdata/{steam_id}/pfx/drive_c/users/steamuser/AppData/Local"
    search_roots = [
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]
    for root in search_roots:
        candidate = root / _pfx
        if candidate.exists():
            return candidate
    return None


def find_plugins_txt(game: str) -> Path | None:
    """Locate Plugins.txt for a Bethesda game running under Proton.

    Uses 'appdata_plugins_txt' from GAMES config (relative to AppData/Local).
    Returns None if the Proton prefix doesn't exist yet (game never launched).
    """
    info = GAMES.get(game, {})
    rel = info.get("appdata_plugins_txt")
    steam_id = info.get("steam_id")
    if not rel or not steam_id:
        return None
    local = find_appdata_local(steam_id)
    if local is None:
        return None
    return local / rel


# ── Download & Extract ────────────────────────────────────────────────────────

DOWNLOAD_MAX_RETRIES = int(os.environ.get("NEXMOD_DOWNLOAD_RETRIES", "3"))


def download_file(url: str, dest: Path, max_retries: int | None = None) -> Path:
    """Stream-download to <dest>.part, then atomic rename.

    Resumes from the .part file's current size on retry via HTTP Range. Server
    may ignore Range and return 200 instead of 206 — we treat that as a fresh
    restart (delete .part, start over). Atomic rename ensures the canonical
    dest path never holds a partial archive.
    """
    if max_retries is None:
        max_retries = DOWNLOAD_MAX_RETRIES
    log.debug("Downloading %s → %s", url, dest)
    part = dest.with_suffix(dest.suffix + ".part")

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        existing = part.stat().st_size if part.exists() else 0
        headers: dict[str, str] = {}
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"
            log.debug("Resuming from byte %d (attempt %d)", existing, attempt)

        try:
            with requests.get(url, stream=True, timeout=120, headers=headers) as r:
                # If we asked for a range but server ignored it, restart fresh.
                if existing > 0 and r.status_code == 200:
                    log.warning("Server ignored Range header — restarting download")
                    part.unlink(missing_ok=True)
                    existing = 0
                if r.status_code not in (200, 206):
                    r.raise_for_status()

                # Total size: 206 reports it in Content-Range "bytes a-b/total";
                # 200 in Content-Length.
                if r.status_code == 206:
                    cr = r.headers.get("Content-Range", "")
                    total = int(cr.split("/")[-1]) if "/" in cr and cr.split("/")[-1].isdigit() else 0
                else:
                    total = int(r.headers.get("content-length", 0))

                mode = "ab" if existing > 0 else "wb"
                with Progress(
                    TextColumn("[bold cyan]{task.fields[filename]}"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                ) as progress:
                    task = progress.add_task(
                        "dl", filename=dest.name,
                        total=total or None, completed=existing,
                    )
                    with open(part, mode) as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            f.write(chunk)
                            progress.advance(task, len(chunk))

            actual_size = part.stat().st_size
            if total and actual_size != total:
                # Could be premature EOF; retry by resuming from current size.
                if attempt == max_retries:
                    part.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"Size mismatch after {attempt} attempts: "
                        f"expected {total} bytes, got {actual_size}"
                    )
                log.warning("Size mismatch on attempt %d: %d/%d — retrying",
                            attempt, actual_size, total)
                continue

            part.replace(dest)
            log.debug("Downloaded %s (%d bytes)", dest.name, actual_size)
            return dest

        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as e:
            # Transient — keep .part for resume on next attempt.
            last_exc = e
            if attempt == max_retries:
                part.unlink(missing_ok=True)
                log.error("Download failed for %s after %d attempts: %s", url, attempt, e)
                raise RuntimeError(f"Download failed after {attempt} attempts: {e}") from e
            delay = _backoff_delay(attempt)
            log.warning("Download attempt %d/%d failed (%s) — resuming in %.1fs",
                        attempt, max_retries, e.__class__.__name__, delay)
            console.print(
                f"  [yellow]Download blip — resuming in {delay:.1f}s "
                f"({attempt}/{max_retries})[/yellow]"
            )
            time.sleep(delay)
        except Exception as e:
            # Non-transient (e.g., 403, 404, disk-full): no retry.
            part.unlink(missing_ok=True)
            log.error("Download failed for %s: %s", url, e)
            raise RuntimeError(f"Download failed: {e}") from e

    # Unreachable — loop either returns or raises.
    raise RuntimeError(f"Download failed after {max_retries} attempts: {last_exc}")


def _try_download_with_mirrors(urls: list, archive: Path) -> None:
    """Try each mirror URL until one fully completes (with internal retries).

    Nexus's download_link.json returns multiple CDN mirrors. download_file()
    handles transient errors per-mirror; this layer handles whole-mirror
    outages by trying the next one.
    """
    if not urls:
        raise RuntimeError("No download URLs provided")
    last_exc: Exception | None = None
    for i, entry in enumerate(urls):
        download_url = entry.get("URI") or entry.get("url")
        if not download_url:
            continue
        try:
            download_file(download_url, archive)
            return
        except Exception as e:
            last_exc = e
            short = entry.get("short_name") or f"mirror-{i+1}"
            log.warning("Mirror %s failed: %s", short, e)
            if i + 1 < len(urls):
                console.print(
                    f"  [yellow]Mirror '{short}' failed — trying next "
                    f"({i + 2}/{len(urls)})[/yellow]"
                )
    raise RuntimeError(f"All {len(urls)} mirrors failed. Last error: {last_exc}")


def verify_md5(path: Path, expected: str) -> None:
    # Nexus exposes md5 in files.json. A mismatch means CDN corruption,
    # truncated download, or a swapped/tampered file — never extract.
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest().lower()
    expected_l = (expected or "").lower()
    if actual != expected_l:
        path.unlink(missing_ok=True)
        msg = f"MD5 mismatch: expected {expected_l}, got {actual}"
        log.error(msg)
        raise RuntimeError(msg)
    log.debug("MD5 verified for %s", path.name)


# ── Snapshots / rollback cache ───────────────────────────────────────────────

CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "nexmod"
SNAPSHOTS_PER_MOD = int(os.environ.get("NEXMOD_SNAPSHOTS_PER_MOD", "3"))


def _snapshot_dir(game: str, mod_id: int) -> Path:
    return CACHE_DIR / game / str(mod_id)


def _safe_filename_part(s: str) -> str:
    """Sanitize a string for use as a filename component (no / \\ : etc.)."""
    return re.sub(r'[^\w.\-]', '_', s) if s else ""


def _save_snapshot(game: str, mod_id: int, version: str | None,
                   archive: Path) -> Path | None:
    """Copy archive into the snapshot cache as <version>.<ext>.

    Called after a successful install/update so a future `nexmod rollback`
    can restore. Returns None when version is empty (we'd have nothing to
    name the snapshot after) or when the source archive is missing. Prunes
    oldest snapshots once over SNAPSHOTS_PER_MOD.
    """
    if not version or not archive.exists():
        return None
    snap_dir = _snapshot_dir(game, mod_id)
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Preserve compound extensions (.tar.gz etc.) so re-extraction works.
    name_lower = archive.name.lower()
    ext = next(
        (e for e in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz") if name_lower.endswith(e)),
        archive.suffix or ".zip",
    )
    snap_path = snap_dir / f"{_safe_filename_part(version)}{ext}"
    try:
        shutil.copy2(archive, snap_path)
    except OSError as e:
        log.warning("snapshot copy failed: %s", e)
        return None
    log.info("snapshot saved: %s", snap_path)

    _prune_snapshots(game, mod_id)
    return snap_path


def _prune_snapshots(game: str, mod_id: int) -> int:
    """Keep only the SNAPSHOTS_PER_MOD newest snapshots. Returns # pruned."""
    snap_dir = _snapshot_dir(game, mod_id)
    if not snap_dir.exists():
        return 0
    files = sorted(
        (p for p in snap_dir.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    pruned = 0
    for old in files[SNAPSHOTS_PER_MOD:]:
        try:
            old.unlink()
            log.info("snapshot pruned: %s", old)
            pruned += 1
        except OSError as e:
            log.warning("Could not prune snapshot %s: %s", old, e)
    return pruned


def _list_snapshots(game: str, mod_id: int) -> list[Path]:
    """Return cached snapshots newest-first by mtime."""
    snap_dir = _snapshot_dir(game, mod_id)
    if not snap_dir.exists():
        return []
    return sorted(
        (p for p in snap_dir.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _snapshot_version_label(snap: Path) -> str:
    """Strip compound extensions to recover the version label from filename."""
    name = snap.name
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return snap.stem


def _check_disk_space(path: Path, required_bytes: int, label: str) -> None:
    """Raise RuntimeError if path's filesystem has < required_bytes free.

    Pre-flight check before download/extract so users get an actionable error
    upfront instead of a partial-state failure halfway through.
    """
    if required_bytes <= 0:
        return
    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        log.warning("Could not check disk space at %s: %s", path, e)
        return
    if usage.free < required_bytes:
        need_mb = required_bytes // (1024 * 1024)
        free_mb = usage.free // (1024 * 1024)
        raise RuntimeError(
            f"Not enough disk space for {label}: need ~{need_mb} MB, "
            f"have {free_mb} MB free at {path}"
        )


def _archive_top_level_dirs(archive: Path) -> list[str]:
    """Peek at archive and return its top-level directory names.

    Used for conflict detection before extraction. Returns [] for formats we
    can't peek without extraction (7z) or on read errors — caller treats empty
    as "no conflict info available, proceed."
    """
    name = archive.name.lower()
    tops: set[str] = set()
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                for n in zf.namelist():
                    parts = n.split("/")
                    if parts[0]:
                        tops.add(parts[0])
        elif any(name.endswith(ext) for ext in
                 (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
            with tarfile.open(archive) as tf:
                for m in tf.getmembers():
                    parts = m.name.split("/")
                    if parts[0]:
                        tops.add(parts[0])
        # 7z would require subprocess invoke; skip — conflict detection is
        # best-effort, not load-bearing.
    except Exception as e:
        log.warning("Could not peek archive %s: %s", archive.name, e)
        return []
    return sorted(tops)


def _detect_install_conflicts(
    db: sqlite3.Connection, game: str, mod_id: int, top_dirs: list[str]
) -> list[tuple[str, str, int]]:
    """Return [(folder, owner_name, owner_mod_id), ...] for any top-level dir
    in the archive that's already tracked by a *different* mod in this game.

    Catches the silent-overwrite case where two mods extract to the same folder.
    """
    if not top_dirs:
        return []
    placeholders = ",".join("?" * len(top_dirs))
    rows = db.execute(
        f"SELECT mod_id, name, folder_name FROM mods "
        f"WHERE game = ? AND mod_id != ? AND folder_name IN ({placeholders})",
        (game, mod_id, *top_dirs),
    ).fetchall()
    return [(r["folder_name"], r["name"], r["mod_id"]) for r in rows]


def extract_archive(archive: Path, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    log.debug("Extracting %s → %s", archive.name, target_dir)
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                bad = zf.testzip()
                if bad:
                    raise RuntimeError(f"Corrupt zip entry: {bad}")
                for member in zf.infolist():
                    mpath = member.filename
                    if os.path.isabs(mpath) or ".." in mpath.split("/"):
                        raise RuntimeError(f"Unsafe path in archive: {mpath}")
                zf.extractall(target_dir)
        elif any(name.endswith(ext) for ext in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
            with tarfile.open(archive) as tf:
                if sys.version_info >= (3, 12):
                    tf.extractall(target_dir, filter="data")
                else:
                    tf.extractall(target_dir)
        elif name.endswith(".7z"):
            _7z = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
            if not _7z:
                raise RuntimeError(
                    "7z not found. Install it:\n"
                    "  Ubuntu/Debian: sudo apt install p7zip-full\n"
                    "  Arch:          sudo pacman -S p7zip\n"
                    "  Fedora:        sudo dnf install p7zip p7zip-plugins\n"
                    "  openSUSE:      sudo zypper install p7zip"
                )
            # Path-traversal check: list contents first and reject any unsafe member.
            list_result = subprocess.run(
                [_7z, "l", "-slt", str(archive)],
                capture_output=True, text=True,
            )
            for line in list_result.stdout.splitlines():
                if line.startswith("Path = "):
                    mpath = line[7:].strip()
                    if os.path.isabs(mpath) or ".." in mpath.replace("\\", "/").split("/"):
                        raise RuntimeError(f"Unsafe path in archive: {mpath}")
            result = subprocess.run(
                [_7z, "x", str(archive), f"-o{target_dir}", "-y"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"7z failed: {result.stderr.strip()}")
        else:
            shutil.copy2(archive, target_dir / archive.name)
    except Exception as e:
        log.error("Extraction failed for %s: %s", archive.name, e)
        raise


# ── File Selection ────────────────────────────────────────────────────────────

def pick_main_file(files: list) -> dict | None:
    for category in ("MAIN", "UPDATE", "MISCELLANEOUS"):
        for f in files:
            cat = (f.get("category_name") or "").upper()
            if cat == category:
                return f
    valid = [f for f in files if "OLD_VERSION" not in (f.get("category_name") or "").upper()]
    if not valid:
        return None
    chosen = max(valid, key=lambda f: f.get("uploaded_timestamp", 0))
    console.print(
        "[yellow]No MAIN/UPDATE/MISCELLANEOUS file — falling back to most recent: "
        f"{chosen.get('file_name', '?')} ({chosen.get('category_name', '?')})[/yellow]"
    )
    log.warning("pick_main_file fallback: chose file_id=%s category=%s",
                chosen.get("file_id"), chosen.get("category_name"))
    return chosen


# ── Nexus URL Parser ─────────────────────────────────────────────────────────

def parse_nexus_url(url: str) -> tuple[str, int, int | None]:
    """Extract (game_slug, mod_id, file_id_or_None) from a Nexus Mods page URL.

    Supports all common formats:
      https://www.nexusmods.com/warhammer40kdarktide/mods/1234
      https://www.nexusmods.com/warhammer40kdarktide/mods/1234/files/5678
      https://www.nexusmods.com/warhammer40kdarktide/mods/1234?tab=files&file_id=5678
    """
    m = NEXUS_URL_RE.search(url)
    if not m:
        console.print(f"[red]Cannot parse Nexus URL: {url}[/red]")
        console.print("[dim]Expected format: https://www.nexusmods.com/<game>/mods/<id>[/dim]")
        sys.exit(1)

    nexus_domain = m.group(1).lower()
    mod_id       = int(m.group(2))
    file_id      = int(m.group(3)) if m.group(3) else None

    # Also check ?file_id= query param (Files tab with a file selected)
    if file_id is None:
        fid_m = re.search(r'[?&]file_id=(\d+)', url)
        if fid_m:
            file_id = int(fid_m.group(1))

    domain_to_game = {info["domain"]: slug for slug, info in GAMES.items()}
    game_slug = domain_to_game.get(nexus_domain)

    if not game_slug:
        game_slug = nexus_domain
        console.print(f"[yellow]Unknown game domain '{nexus_domain}' — will try using it directly.[/yellow]")
        console.print(f"[dim]If install fails, run: nexmod path set {nexus_domain} /path/to/mods[/dim]")

    return game_slug, mod_id, file_id


# ── Mod Dir Resolution ────────────────────────────────────────────────────────

def resolve_mod_dir(game: str, db: sqlite3.Connection) -> Path:
    row = db.execute("SELECT path FROM game_paths WHERE game = ?", (game,)).fetchone()
    if row:
        return Path(row["path"])

    info = GAMES.get(game)
    if not info:
        if not _is_interactive():
            console.print(f"[red]Unknown game '{game}'. Run 'nexmod games' to list supported games.[/red]")
            console.print(f"Or set a custom path: nexmod path set {game} /path/to/mod/dir")
            sys.exit(1)
        # Interactive: prompt for manual path
        entered = click.prompt(
            f"Unknown game '{game}'. Enter the mod directory path to register it, or press Enter to cancel",
            default="",
            show_default=False,
        )
        if not entered.strip():
            console.print("[dim]Cancelled.[/dim]")
            sys.exit(0)
        path = Path(entered.strip()).expanduser().resolve()
        if not path.exists():
            console.print(f"[yellow]⚠ Path does not exist: {path}[/yellow]")
            if not click.confirm("Register it anyway?", default=False):
                sys.exit(0)
        db.execute("INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)", (game, str(path)))
        db.commit()
        console.print(f"[green]Registered:[/green] {game} → {path}")
        return path

    game_path = find_game_install(info["steam_id"])
    if game_path:
        mod_dir = game_path / info["mod_subdir"]
        db.execute("INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)", (game, str(mod_dir)))
        db.commit()
        console.print(f"[dim]Auto-detected: {mod_dir}[/dim]")
        return mod_dir

    if not _is_interactive():
        console.print(f"[yellow]Could not find {info['name']} Steam install.[/yellow]")
        console.print(f"Set it manually: nexmod path set {game} /path/to/{info['mod_subdir']}")
        sys.exit(1)
    # Interactive: prompt for manual path
    entered = click.prompt(
        f"Can't find {info['name']} Steam install. Enter path manually, or press Enter to skip",
        default="",
        show_default=False,
    )
    if not entered.strip():
        console.print("[dim]Skipping.[/dim]")
        sys.exit(0)
    path = Path(entered.strip()).expanduser().resolve()
    if not path.exists():
        console.print(f"[yellow]⚠ Path does not exist: {path}[/yellow]")
        if not click.confirm("Register it anyway?", default=False):
            sys.exit(0)
    db.execute("INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)", (game, str(path)))
    db.commit()
    console.print(f"[green]Registered:[/green] {game} → {path}")
    return path


# ── Load Order ───────────────────────────────────────────────────────────────

def _ensure_load_order(mod_dir: Path, load_order_file: str, folders: list[str]) -> list[str]:
    """Append any folder names not already in load_order_file. Returns newly added names."""
    lof = mod_dir / load_order_file
    existing_lines = lof.read_text().splitlines() if lof.exists() else []
    existing = {l.strip() for l in existing_lines if l.strip()}
    added = [f for f in folders if f not in existing]
    if added:
        lines = existing_lines + added
        lof.write_text("\n".join(lines) + "\n")
        log.info("load_order: added %s", added)
    return added


def _parse_mod_deps(mod_dir: Path, folder: str) -> list[str]:
    """Return declared dependency folder names for a mod.

    Tries two formats in order:
      1. mod.json  — {"dependencies": [...], "optional_dependencies": [...]}
      2. <folder>.mod — Lua file with top-level require = {...} and load_after = {...} tables
         require   = hard deps (mod won't work without them)
         load_after = soft ordering hints (load after these if present)
    """
    deps: list[str] = []

    # ── mod.json (BG3, generic) ───────────────────────────────────────────────
    mod_json = mod_dir / folder / "mod.json"
    if mod_json.exists():
        try:
            data = json.loads(mod_json.read_text(encoding="utf-8", errors="replace"))
            deps += list(data.get("dependencies") or [])
            deps += list(data.get("optional_dependencies") or [])
        except Exception as e:
            log.warning("Could not parse mod.json for %s: %s", folder, e)

    # ── .mod Lua table (Darktide / DMF) ──────────────────────────────────────
    # Top-level table fields: require = {"ModA", "ModB"}, load_after = {"ModC"}
    # Both influence load order; require is hard, load_after is soft.
    mod_file = mod_dir / folder / f"{folder}.mod"
    if mod_file.exists():
        try:
            text = mod_file.read_text(encoding="utf-8", errors="replace")
            for block in re.findall(
                r'(?:require|load_after)\s*=\s*\{([^}]*)\}', text, re.DOTALL
            ):
                # Strip Lua line comments before extracting strings
                block = re.sub(r'--[^\n]*', '', block)
                deps.extend(re.findall(r'"([^"]+)"', block))
        except Exception as e:
            log.warning("Could not parse .mod file for %s: %s", folder, e)

    # Dedup preserving first-seen order
    seen: set[str] = set()
    result = []
    for d in (s.strip() for s in deps if s.strip()):
        if d not in seen:
            seen.add(d)
            result.append(d)
    return result


def _topo_sort(
    folders: list[str], deps_map: dict[str, list[str]]
) -> tuple[list[str], list[str]]:
    """Kahn's topological sort. Stable: ties broken by original position in `folders`.
    deps_map[f] = list of folders that must appear BEFORE f (pre-filtered to known set).
    Returns (sorted_list, cycle_nodes). Cycle nodes are appended after sorted_list by callers."""
    rank = {f: i for i, f in enumerate(folders)}
    dependents: dict[str, list[str]] = {f: [] for f in folders}
    in_degree: dict[str, int] = {f: 0 for f in folders}

    for f in folders:
        for pred in deps_map.get(f, []):
            if pred != f:
                dependents[pred].append(f)
                in_degree[f] += 1

    queue = sorted([f for f in folders if in_degree[f] == 0], key=rank.__getitem__)
    result: list[str] = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        newly_ready = []
        for dep in dependents[node]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                newly_ready.append(dep)
        if newly_ready:
            queue = sorted(queue + newly_ready, key=rank.__getitem__)

    cycles = [f for f in folders if f not in set(result)]
    return result, cycles


def reorder_load_order(
    mod_dir: Path, load_order_file: str, dry_run: bool = False
) -> dict:
    """Sort mod_load_order.txt by mod.json dependency declarations using a topological sort.

    Returns:
        order        — final ordered list (sorted + cycles appended at end)
        cycles       — folders involved in dependency cycles
        missing_deps — {folder: [dep, ...]} for deps declared but absent from the file
        deps_map     — {folder: [known_deps]} used for display
        changed      — True if the order differs from the current file
    """
    lof = mod_dir / load_order_file
    if not lof.exists():
        return {"order": [], "cycles": [], "missing_deps": {}, "deps_map": {}, "changed": False}

    lines = lof.read_text().splitlines()
    # Strip all comment lines — nexmod writes its own header on every save.
    folder_lines = [l.strip() for l in lines if l.strip() and not l.strip().startswith("--")]
    header_comments = ["-- File managed by nexmod"]

    original = folder_lines
    if not original:
        return {"order": [], "cycles": [], "missing_deps": {}, "deps_map": {}, "changed": False}

    folder_set = set(original)
    deps_map: dict[str, list[str]] = {}
    missing_deps: dict[str, list[str]] = {}

    for folder in original:
        raw = _parse_mod_deps(mod_dir, folder)
        known   = [d for d in raw if d in folder_set]
        missing = [d for d in raw if d not in folder_set]
        deps_map[folder] = known
        if missing:
            missing_deps[folder] = missing

    sorted_list, cycles = _topo_sort(original, deps_map)
    order = sorted_list + cycles

    existing_header = [l.strip() for l in lines if l.strip().startswith("--")]
    header_changed  = existing_header != header_comments
    changed = order != original
    if (changed or header_changed) and not dry_run:
        all_lines = header_comments + order
        lof.write_text("\n".join(all_lines) + "\n")
        if changed:
            log.info("load_order reordered: %s", order)
        if header_changed:
            log.info("load_order header updated")

    return {
        "order": order,
        "cycles": cycles,
        "missing_deps": missing_deps,
        "deps_map": deps_map,
        "changed": changed,
    }


# ── Missing Dependency Prompt ────────────────────────────────────────────────

def _handle_missing_deps(
    game: str,
    missing_deps: dict[str, list[str]],
    mod_dir: Path,
    api_key: str,
    db: sqlite3.Connection,
    yes: bool = False,
) -> bool:
    """Prompt the user to install any missing mod dependencies.

    Two sub-cases:
      - Dep folder exists on disk but not in load order → add silently.
      - Dep not on disk → prompt for a Nexus URL, install if provided.

    Returns True if any new mods were installed (caller should re-sort).
    """
    if not missing_deps:
        return False

    info            = GAMES.get(game, {})
    load_order_file = info.get("load_order_file")
    any_installed   = False

    all_missing: dict[str, str] = {}  # dep_folder → declaring_mod
    for declaring_mod, deps in missing_deps.items():
        for dep in deps:
            if dep not in all_missing:
                all_missing[dep] = declaring_mod

    for dep, declaring_mod in all_missing.items():
        dep_path = mod_dir / dep
        if dep_path.exists():
            console.print(f"  [dim]{dep} is on disk but not in load order — adding automatically[/dim]")
            if load_order_file:
                _ensure_load_order(mod_dir, load_order_file, [dep])
            continue

        # Dep is missing from disk
        console.print(f"  [yellow]⚠[/yellow] [bold]{declaring_mod}[/bold] requires [cyan]{dep}[/cyan] — not installed")
        if yes or not _is_interactive():
            # non-interactive: proceed to DB lookup / URL prompt without gating
            pass
        elif not click.confirm("  → Install it now?", default=True):
            console.print(f"  [dim]Skipped. Install manually: nexmod install <nexus-url>[/dim]")
            continue

        # Try DB resolution first (folder_name → mod_id)
        row = db.execute(
            "SELECT mod_id, name FROM mods WHERE game=? AND folder_name=?",
            (game, dep),
        ).fetchone() if db is not None else None
        if row:
            console.print(f"  [dim]Found in DB → mod {row['mod_id']} ({row['name']})[/dim]")
            try:
                do_install(game, row["mod_id"], None, api_key, db)
                any_installed = True
            except Exception as e:
                console.print(f"  [red]✗[/red] Install failed: {e}")
            continue

        # DB miss — prompt for URL
        console.print(f"  [dim]'{dep}' not in local DB. Paste a Nexus URL to install it:[/dim]")
        console.print(f"  [dim]Or search for it: nexmod search {game} {dep}[/dim]")
        url = click.prompt(
            f"  Paste the Nexus URL for '{dep}' to install it (Enter to skip)",
            default="",
            show_default=False,
        ).strip()

        if not url:
            console.print(f"  [dim]Skipping {dep}[/dim]")
            continue

        try:
            dep_game, dep_mod_id, dep_file_id = parse_nexus_url(url)
            if dep_game != game:
                console.print(
                    f"  [yellow]Warning: URL points to game '{dep_game}', "
                    f"installing for '{game}' instead.[/yellow]"
                )
                dep_game = game
            do_install(dep_game, dep_mod_id, dep_file_id, api_key, db)
            any_installed = True
        except SystemExit:
            pass  # parse_nexus_url already printed the error
        except Exception as e:
            log.error("Failed to install dep %s: %s", dep, e)
            console.print(f"  [red]Failed to install {dep}: {e}[/red]")

    return any_installed


# ── Install Helper ────────────────────────────────────────────────────────────

def do_install(game: str, mod_id: int, file_id_override: int | None,
               api_key: str, db: sqlite3.Connection, *,
               nxm_key: str | None = None,
               nxm_expires: str | None = None,
               nxm_user_id: str | None = None,
               from_file: "Path | None" = None):
    info   = GAMES.get(game)
    domain = info["domain"] if info else game

    with console.status(f"Fetching mod {mod_id} info..."):
        mod   = api_mod_info(domain, mod_id, api_key)
        files = api_mod_files(domain, mod_id, api_key)

    console.print(f"  [bold]{mod['name']}[/bold] by {mod.get('author', '?')} — v{mod.get('version', '?')}")
    log.info("Installing mod_id=%s name=%r version=%s game=%s", mod_id, mod["name"], mod.get("version"), game)

    if file_id_override:
        chosen = next((f for f in files if f["file_id"] == file_id_override), None)
        if not chosen:
            msg = f"File ID {file_id_override} not found for mod {mod_id}"
            log.error(msg)
            console.print(f"[red]{msg}[/red]")
            sys.exit(1)
    else:
        chosen = pick_main_file(files)
        if not chosen:
            log.error("No main file found for mod_id=%s, files: %s",
                      mod_id, [(f["file_id"], f.get("category_name")) for f in files])
            console.print("[red]Could not determine main file. Use --file-id to pick one.[/red]")
            for f in files:
                console.print(f"  [{f['file_id']}] {f['file_name']} ({f.get('category_name')})")
            sys.exit(1)

    mod_dir = resolve_mod_dir(game, db)

    if from_file:
        archive = from_file.resolve()
        if not archive.exists():
            console.print(f"[red]File not found: {archive}[/red]")
            sys.exit(1)
        console.print(f"  File: [cyan]{archive.name}[/cyan] (local) — skipping download")
        size_bytes = archive.stat().st_size
        try:
            if mod_dir.exists():
                _check_disk_space(mod_dir, size_bytes * 3, "extraction")
        except RuntimeError as e:
            record(db, "install", game, mod_id, mod["name"], mod.get("version"), "fail", str(e))
            raise
    else:
        console.print(f"  File: [cyan]{chosen['file_name']}[/cyan] ({chosen.get('size_kb', '?')} KB)")

        with console.status("Getting CDN download link..."):
            urls = api_download_urls(domain, mod_id, chosen["file_id"], api_key,
                                     nxm_key=nxm_key, nxm_expires=nxm_expires, nxm_user_id=nxm_user_id)

        if not urls:
            raise RuntimeError(
                "Nexus returned no download URLs — Premium required for direct downloads. "
                "Free users: click 'Mod Manager Download' on nexusmods.com (requires nxm-register) "
                "or use --from-file <archive> after downloading manually."
            )
        tmp = DATA_DIR / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        archive = tmp / chosen["file_name"]

        # Disk-space pre-flight. size_kb is the only size hint Nexus reliably
        # exposes; allow a 50 MB buffer for the .part file overhead, and 3× for
        # extraction headroom (compressed archives expand significantly).
        size_kb = chosen.get("size_kb") or 0
        if size_kb > 0:
            size_bytes = size_kb * 1024
            try:
                _check_disk_space(tmp, size_bytes + 50 * 1024 * 1024, "download")
                if mod_dir.exists():
                    _check_disk_space(mod_dir, size_bytes * 3, "extraction")
            except RuntimeError as e:
                record(db, "install", game, mod_id, mod["name"], mod.get("version"), "fail", str(e))
                raise

    dirs_before = {p.name for p in mod_dir.iterdir() if p.is_dir()} if mod_dir.exists() else set()
    plugin_exts = (info or {}).get("plugin_exts", ())
    plugins_before = _get_disk_plugins(mod_dir, plugin_exts) if (mod_dir.exists() and plugin_exts) else set()

    extraction_ok = False
    try:
        if not from_file:
            _try_download_with_mirrors(urls, archive)
            if chosen.get("md5"):
                verify_md5(archive, chosen["md5"])

        # Conflict detection: peek archive contents and warn if any top-level
        # folder is already claimed by a different tracked mod. Best-effort
        # (returns [] for 7z and read errors — extraction will still happen
        # and overwrite, matching legacy behavior in those cases).
        top_dirs = _archive_top_level_dirs(archive)
        conflicts = _detect_install_conflicts(db, game, mod_id, top_dirs)
        if conflicts:
            console.print(
                f"\n[yellow]⚠ Archive contains folders already claimed by other tracked mods:[/yellow]"
            )
            for folder, other_name, other_id in conflicts:
                console.print(
                    f"  [yellow]{folder}/[/yellow] is owned by [cyan]{other_name}[/cyan] (mod {other_id})"
                )
            console.print("  Continuing will overwrite those mods' files.")
            if not click.confirm("Continue?", default=False):
                console.print("[dim]Aborted.[/dim]")
                record(db, "install", game, mod_id, mod["name"], mod.get("version"),
                       "skip", f"conflict with mods: {','.join(str(c[2]) for c in conflicts)}")
                raise click.Abort()

        console.print(f"  Extracting to [dim]{mod_dir}[/dim]...")
        extract_archive(archive, mod_dir)
        extraction_ok = True
        # Save rollback snapshot before the finally clause unlinks the archive.
        # Best-effort: failures here don't fail the install — user just has
        # one fewer rollback target.
        try:
            _save_snapshot(game, mod_id, mod.get("version"), archive)
        except Exception as e:
            log.warning("snapshot save failed: %s", e)
    except click.Abort:
        raise
    except Exception as e:
        record(db, "install", game, mod_id, mod["name"], mod.get("version"), "fail", str(e))
        raise
    finally:
        if not from_file:
            archive.unlink(missing_ok=True)

    if not extraction_ok:
        raise RuntimeError("Extraction did not complete — DB not written")

    new_dirs = sorted({p.name for p in mod_dir.iterdir() if p.is_dir()} - dirs_before)
    # Multi-folder mods (rare but real — e.g., DMF ships dmf + mod_compat) get
    # the alphabetically-first folder recorded as the primary; --purge will
    # only delete that one. Surface the others so the user knows.
    if len(new_dirs) > 1:
        console.print(
            f"  [yellow]Multi-folder install:[/yellow] {len(new_dirs)} new folders — "
            f"primary tracked as [cyan]{new_dirs[0]}[/cyan]; siblings: "
            f"{', '.join(new_dirs[1:])}"
        )
        log.info("Multi-folder install for mod_id=%s: %s", mod_id, new_dirs)
    folder_name = new_dirs[0] if new_dirs else None

    # When installing from a local file the recorded filename is the actual
    # archive, not the API's canonical file_name (which may differ).
    recorded_filename = archive.name if from_file else chosen["file_name"]

    db.execute("""
        INSERT INTO mods
            (game, mod_id, file_id, name, version, filename, mod_dir,
             folder_name, tracked_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game, mod_id) DO UPDATE SET
            file_id     = excluded.file_id,
            name        = excluded.name,
            version     = excluded.version,
            filename    = excluded.filename,
            mod_dir     = excluded.mod_dir,
            folder_name = COALESCE(excluded.folder_name, mods.folder_name),
            updated_at  = excluded.updated_at
    """, (
        game, mod_id, chosen["file_id"], mod["name"], mod.get("version"),
        recorded_filename, str(mod_dir), folder_name, now_iso(), now_iso(),
    ))
    db.commit()
    record(db, "install", game, mod_id, mod["name"], mod.get("version"), "ok")

    load_order_file = (info or {}).get("load_order_file")
    if load_order_file:
        try:
            result = reconcile_load_order(game, db, mod_dir)
            if new_dirs and result["written"]:
                console.print(f"  [dim]mod_load_order.txt ← {', '.join(new_dirs)}[/dim]")
            elif new_dirs and result["drift_detected"]:
                console.print("  [yellow]Load order: external edit detected — not modified.[/yellow]")
            if result.get("cycles"):
                console.print(
                    f"  [yellow]⚠ Dependency cycle in load order: "
                    f"{', '.join(result['cycles'])}. "
                    f"Cycle members appended at end — break with [cyan]nexmod pin[/cyan].[/yellow]"
                )
        except Exception as e:
            log.warning("reconcile after install failed: %s", e)
            console.print(f"  [yellow]Load order reconcile failed: {e}[/yellow]")

    # ── Bethesda plugin tracking (Starfield, etc.) ────────────────────────────
    if plugin_exts:
        new_plugins = sorted(_get_disk_plugins(mod_dir, plugin_exts) - plugins_before)
        official_lower = {m.lower() for m in (info or {}).get("official_masters", ())}
        tracked_plugins = [p for p in new_plugins if p.lower() not in official_lower]
        if tracked_plugins:
            for pname in tracked_plugins:
                db.execute(
                    "INSERT INTO plugin_files (game, mod_id, plugin_name, enabled, added_at) "
                    "VALUES (?, ?, ?, 1, ?) "
                    "ON CONFLICT(game, plugin_name) DO UPDATE SET mod_id = excluded.mod_id, added_at = excluded.added_at",
                    (game, mod_id, pname, now_iso()),
                )
            db.commit()
            try:
                result = reconcile_plugins_txt(game, db, mod_dir)
                if result.get("written"):
                    console.print(f"  [dim]Plugins.txt ← {', '.join(tracked_plugins)}[/dim]")
                if result.get("cycles"):
                    console.print(
                        f"  [yellow]⚠ Plugin dependency cycle: {', '.join(result['cycles'])}. "
                        f"Appended at end — check load order with [cyan]nexmod plugin-order {game}[/cyan].[/yellow]"
                    )
                if result.get("error"):
                    console.print(f"  [yellow]Plugins.txt: {result['error']}[/yellow]")
            except Exception as e:
                log.warning("plugins.txt reconcile after install failed: %s", e)
                console.print(f"  [yellow]Plugins.txt update failed: {e}[/yellow]")
        elif new_plugins:
            console.print(f"  [dim]No plugin files (.esp/.esm/.esl) found — mod uses loose files or BA2 archives.[/dim]")

    return mod["name"], mod.get("version", "?")


# ── Vortex Manifest Parser ────────────────────────────────────────────────────

def parse_vortex_manifest(game_dir: Path) -> dict[int, tuple[str, str]]:
    manifest = game_dir / "vortex.deployment.json"
    if not manifest.exists():
        return {}
    with open(manifest) as f:
        data = json.load(f)
    seen: dict[int, tuple[str, str]] = {}
    for entry in data.get("files", []):
        src   = entry.get("source", "")
        parts = src.split("-")
        mod_id, name_end = None, 0
        for i, p in enumerate(parts):
            if p.isdigit() and len(p) < 10:
                mod_id   = int(p)
                name_end = i
                break
        if mod_id is None or mod_id in seen:
            continue
        name   = "-".join(parts[:name_end]).strip()
        rel    = entry.get("relPath", "")
        folder = rel.split("/")[1] if rel.startswith("mods/") else ""
        seen[mod_id] = (name, folder)
    return seen


# ── Bethesda Plugin Management ────────────────────────────────────────────────
#
# For games like Starfield that use a Plugins.txt load order file located in
# the Wine/Proton AppData/Local prefix (outside the mod directory). These games
# have plugin files (.esp/.esm/.esl) that must be listed in Plugins.txt to load,
# plus loose files and BA2 archives that are auto-loaded without any entry.
#
# Architecture mirrors the Darktide load order system:
#   reconcile_plugins_txt()  ↔  reconcile_load_order()
#   plugin_files DB table    ↔  load_order_state DB table
#   _read_plugin_masters()   ↔  _parse_mod_deps()
# ─────────────────────────────────────────────────────────────────────────────

PLUGINS_TXT_HEADER = (
    "# This file is used by Starfield to keep track of your downloaded content.\n"
    "# Please do not modify this file.\n"
)


def _get_disk_plugins(mod_dir: Path, plugin_exts: tuple) -> set[str]:
    """Return set of plugin filenames (case-insensitive extensions) at root of mod_dir."""
    if not mod_dir.exists():
        return set()
    return {
        p.name for p in mod_dir.iterdir()
        if p.is_file() and p.suffix.lower() in plugin_exts
    }


def _read_plugin_masters(plugin_path: Path) -> list[str]:
    """Parse the TES4 record header of a .esp/.esm/.esl to extract master (MAST) dependencies.

    Returns list of master filenames declared in the plugin header.
    Returns [] on any parse failure (corrupt file, wrong format, etc.).

    TES4 record layout (24-byte header):
      [0:4]   record type ("TES4")
      [4:8]   data_size uint32 LE  — subrecord area size (excludes this 24-byte header)
      [8:12]  flags uint32 LE
      [12:16] FormID uint32 LE (always 0 for TES4)
      [16:24] version-control info (8 bytes, ignored)

    Subrecord layout (6-byte header + data):
      [0:4]   subrecord type (e.g. "MAST", "HEDR", "CNAM")
      [4:6]   data_size uint16 LE
      [6:N]   data (N = data_size)

    MAST subrecord data: null-terminated UTF-8 string (master filename).
    """
    masters: list[str] = []
    try:
        with open(plugin_path, "rb") as f:
            hdr = f.read(24)
            if len(hdr) < 24 or hdr[:4] != b"TES4":
                return masters
            data_size = struct.unpack_from("<I", hdr, 4)[0]
            read = 0
            while read < data_size:
                sub_hdr = f.read(6)
                if len(sub_hdr) < 6:
                    break
                sub_type = sub_hdr[:4]
                sub_size = struct.unpack_from("<H", sub_hdr, 4)[0]
                sub_data = f.read(sub_size)
                if len(sub_data) < sub_size:
                    break
                read += 6 + sub_size
                if sub_type == b"MAST":
                    name = sub_data.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
                    if name:
                        masters.append(name)
    except (OSError, struct.error) as e:
        log.debug("plugin master parse failed for %s: %s", plugin_path, e)
    return masters


def _parse_plugins_txt(path: Path) -> dict:
    """Parse Plugins.txt into structured form.

    Returns:
        {"header": [comment lines before first entry],
         "entries": [{"name": "Mod.esm", "enabled": True}, ...]}
    """
    if not path.exists():
        return {"header": [], "entries": []}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    header: list[str] = []
    entries: list[dict] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if not entries:
                header.append(stripped)
        else:
            enabled = stripped.startswith("*")
            entries.append({"name": stripped[1:] if enabled else stripped, "enabled": enabled})
    return {"header": header, "entries": entries}


def _write_plugins_txt(path: Path, entries: list[dict], dry_run: bool = False) -> str:
    """Atomically write Plugins.txt. Returns the rendered content string."""
    lines = [PLUGINS_TXT_HEADER]
    for e in entries:
        prefix = "*" if e["enabled"] else ""
        lines.append(f"{prefix}{e['name']}\n")
    content = "".join(lines)
    if dry_run:
        return content
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = path.with_suffix(".bak")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp_fd = None
    try:
        tmp_fd = os.open(str(tmp), os.O_RDONLY)
        os.fsync(tmp_fd)
    except OSError:
        pass
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
    if path.exists():
        shutil.copy2(path, bak)
    os.replace(tmp, path)
    return content


def _plugins_topo_sort(plugins: list[str], deps_map: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """Topological sort of plugins by their master dependencies (Kahn's algorithm).

    Args:
        plugins:  ordered list of plugin names to sort (non-official only)
        deps_map: {plugin_name: [master_names_it_depends_on]} (only names in `plugins` matter)

    Returns:
        (sorted_list, cycle_members) — cycle members appended at end, not interleaved.
    """
    in_set = set(plugins)
    # Only count deps that are in our set (ignore missing masters — they're official or external)
    filtered_deps: dict[str, list[str]] = {
        p: [d for d in deps_map.get(p, []) if d.lower() in {x.lower() for x in in_set}]
        for p in plugins
    }
    in_degree: dict[str, int] = {p: 0 for p in plugins}
    dependents: dict[str, list[str]] = {p: [] for p in plugins}
    # Use case-insensitive matching for plugin names (Windows filenames)
    lower_map = {p.lower(): p for p in plugins}
    for p, deps in filtered_deps.items():
        for d in deps:
            canonical = lower_map.get(d.lower())
            if canonical:
                in_degree[p] += 1
                dependents[canonical].append(p)

    queue = [p for p in plugins if in_degree[p] == 0]
    sorted_list: list[str] = []
    while queue:
        node = queue.pop(0)
        sorted_list.append(node)
        for dep in dependents[node]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    cycles = [p for p in plugins if p not in sorted_list]
    return sorted_list, cycles


def reconcile_plugins_txt(
    game: str,
    db: sqlite3.Connection,
    mod_dir: Path,
    *,
    dry_run: bool = False,
    force_drop: set[str] | None = None,
) -> dict:
    """Reconcile Plugins.txt for a Bethesda plugin game.

    Classifies all known plugins, dependency-sorts them, and writes the result.
    Mirrors reconcile_load_order() for Darktide-style games.

    Classification:
        official   – in game's official_masters config: always first, protected
        managed    – tracked in plugin_files DB table
        foreign    – on disk but not tracked (preserved, appended after managed)
        orphan     – in Plugins.txt but not on disk (dropped)

    Returns dict with: written, dry_run, added, dropped, cycles, plugins_txt (Path|None)
    """
    empty = {"written": False, "dry_run": dry_run, "added": [], "dropped": [], "cycles": [], "plugins_txt": None}
    info = GAMES.get(game, {})
    plugin_exts = info.get("plugin_exts")
    if not plugin_exts:
        return empty

    plugins_txt_path = find_plugins_txt(game)
    if plugins_txt_path is None:
        log.debug("plugins_txt: cannot locate Plugins.txt for %s (Proton prefix not found?)", game)
        return {**empty, "error": "Plugins.txt location unknown — has the game been launched at least once?"}

    official_masters: tuple = info.get("official_masters", ())
    official_lower = {m.lower() for m in official_masters}

    # Current state
    parsed = _parse_plugins_txt(plugins_txt_path)
    current_entries = {e["name"]: e["enabled"] for e in parsed["entries"]}

    # Ground truth sources
    disk_plugins = _get_disk_plugins(mod_dir, plugin_exts)
    db_plugins: dict[str, int] = {  # plugin_name → mod_id
        r["plugin_name"]: r["mod_id"]
        for r in db.execute("SELECT plugin_name, mod_id FROM plugin_files WHERE game=?", (game,)).fetchall()
    }

    # Classify
    all_known = disk_plugins | set(current_entries.keys())
    managed: list[str] = []
    foreign: list[str] = []
    orphans: list[str] = []
    for name in all_known:
        lower = name.lower()
        if lower in official_lower:
            continue  # official masters handled separately
        on_disk = name in disk_plugins
        in_db = name in db_plugins
        in_file = name in current_entries
        if force_drop and name in force_drop:
            orphans.append(name)
        elif on_disk and in_db:
            managed.append(name)
        elif on_disk and not in_db:
            foreign.append(name)
        elif not on_disk and in_file:
            orphans.append(name)

    # Build dep graph for managed plugins
    deps_map: dict[str, list[str]] = {}
    for name in managed:
        plugin_path = mod_dir / name
        if plugin_path.exists():
            deps_map[name] = _read_plugin_masters(plugin_path)

    sorted_managed, cycles = _plugins_topo_sort(managed, deps_map)

    # Official masters: preserve existing order from Plugins.txt, add any missing
    officials_ordered = [e["name"] for e in parsed["entries"] if e["name"].lower() in official_lower]
    for om in official_masters:
        if om not in officials_ordered and (mod_dir / om).exists():
            officials_ordered.append(om)

    # Build final entries list: officials → sorted managed → foreign
    final: list[dict] = []
    for name in officials_ordered:
        final.append({"name": name, "enabled": True})  # officials always enabled
    for name in sorted_managed + cycles:
        enabled = current_entries.get(name, True)  # preserve existing enabled/disabled state
        final.append({"name": name, "enabled": enabled})
    for name in sorted(foreign):
        enabled = current_entries.get(name, True)
        final.append({"name": name, "enabled": enabled})

    # Detect changes
    old_names = [e["name"] for e in parsed["entries"]]
    new_names = [e["name"] for e in final]
    added = [n for n in new_names if n not in set(old_names) and n.lower() not in official_lower]
    dropped = orphans

    changed = (old_names != new_names or
               any(current_entries.get(e["name"]) != e["enabled"] for e in final))

    if not changed and plugins_txt_path.exists():
        return {**empty, "written": False, "plugins_txt": plugins_txt_path}

    _write_plugins_txt(plugins_txt_path, final, dry_run=dry_run)
    log.info("plugins_txt reconciled: +%d -%d cycles=%d path=%s",
             len(added), len(dropped), len(cycles), plugins_txt_path)

    return {
        "written": not dry_run,
        "dry_run": dry_run,
        "added": added,
        "dropped": dropped,
        "cycles": cycles,
        "plugins_txt": plugins_txt_path,
        "final": final,
    }


# ── Profiles ─────────────────────────────────────────────────────────────────

def _profile_path(game: str, name: str) -> Path:
    return PROFILES_DIR / game / f"{name}.json"


def _read_profile(game: str, name: str) -> dict:
    path = _profile_path(game, name)
    if not path.exists():
        console.print(f"[red]Profile '{name}' not found for {game}.[/red]")
        console.print(f"Run 'nexmod profile list {game}' to see available profiles.")
        sys.exit(1)
    return json.loads(path.read_text())


def _write_profile(
    game: str, name: str, load_order: list[str],
    description: str = "", directives: list[str] | None = None,
    mods: "list[dict] | None" = None,
) -> Path:
    path = _profile_path(game, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_created = None
    if path.exists():
        try:
            existing_created = json.loads(path.read_text()).get("created_at")
        except Exception:
            pass
    data = {
        "name":        name,
        "game":        game,
        "description": description,
        "created_at":  existing_created or now_iso(),
        "updated_at":  now_iso(),
        "load_order":  load_order,
        "directives":  directives or [],
    }
    if mods is not None:
        data["mods"] = mods
    path.write_text(json.dumps(data, indent=2))
    return path


def _list_profiles(game: str) -> list[dict]:
    d = PROFILES_DIR / game
    if not d.exists():
        return []
    profiles = []
    for f in sorted(d.glob("*.json")):
        try:
            profiles.append(json.loads(f.read_text()))
        except Exception:
            pass
    return profiles



_ARCHIVE_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip", ".7z", ".tar", ".rar")


def _archive_basename(filename: str) -> str:
    """Strip a known archive extension. Falls back to single-suffix split."""
    lower = filename.lower()
    for ext in _ARCHIVE_EXTS:
        if lower.endswith(ext):
            return filename[: -len(ext)]
    return filename.rsplit(".", 1)[0]


def _normalize_name(s: str) -> str:
    """Lowercase + strip non-alphanumerics for fuzzy folder/name comparison."""
    return re.sub(r'[^a-z0-9]', '', (s or "").lower())


def infer_folder_name(mod_dir: Path, row) -> tuple[str | None, list[str], str]:
    """Best-effort guess of a tracked mod's on-disk folder.

    Used by `nexmod fsck` to backfill `folder_name` for legacy rows installed
    before the column existed. Returns (best_match, all_candidates, strategy).

    Strategy ladder (first match wins):
      1. filename_stem    — archive basename matches a folder exactly
      2. mod_json_name    — mod.json "name"/"id" matches the Nexus mod name
      3. dot_mod_title    — Darktide .mod file has matching name = "..."
      4. fuzzy_match      — difflib ratio ≥ 0.85 on normalized names (unique)

    Returns (None, [], "no_dir") when mod_dir is missing.
    Returns (None, candidates, "ambiguous") when multiple equally-plausible
    matches exist — caller must surface for manual resolution.
    """
    if not mod_dir or not mod_dir.exists():
        return None, [], "no_dir"

    all_dirs = sorted(p.name for p in mod_dir.iterdir() if p.is_dir())
    if not all_dirs:
        return None, [], "empty_dir"

    # 1. Exact filename stem match
    if row["filename"]:
        stem = _archive_basename(row["filename"])
        if stem in all_dirs:
            return stem, [stem], "filename_stem"

    name = row["name"] or ""
    name_norm = _normalize_name(name)

    # 2. mod.json name/id match
    json_matches: list[str] = []
    for d in all_dirs:
        mj = mod_dir / d / "mod.json"
        if not mj.exists():
            continue
        try:
            data = json.loads(mj.read_text(encoding="utf-8", errors="replace"))
            for key in ("name", "id", "Name", "Id", "modName"):
                v = data.get(key)
                if isinstance(v, str) and _normalize_name(v) == name_norm and name_norm:
                    json_matches.append(d)
                    break
        except Exception:
            pass
    if len(json_matches) == 1:
        return json_matches[0], json_matches, "mod_json_name"

    # 3. Darktide .mod file: top-level name = "..."
    dot_mod_matches: list[str] = []
    for d in all_dirs:
        mf = mod_dir / d / f"{d}.mod"
        if not mf.exists():
            continue
        try:
            text = mf.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r'name\s*=\s*"([^"]+)"', text):
                if _normalize_name(m.group(1)) == name_norm and name_norm:
                    dot_mod_matches.append(d)
                    break
        except Exception:
            pass
    if len(dot_mod_matches) == 1:
        return dot_mod_matches[0], dot_mod_matches, "dot_mod_title"

    # 4. Fuzzy difflib match on normalized names
    if name_norm:
        scored = sorted(
            ((difflib.SequenceMatcher(None, name_norm, _normalize_name(d)).ratio(), d)
             for d in all_dirs),
            reverse=True,
        )
        top = [d for ratio, d in scored if ratio >= 0.85]
        if len(top) == 1:
            return top[0], top, "fuzzy_match"
        if len(top) > 1:
            return None, top, "ambiguous"

    return None, all_dirs, "no_match"


def _read_lof_folders(mod_dir: Path, load_order_file: str) -> list[str]:
    lof = mod_dir / load_order_file
    if not lof.exists():
        return []
    return [l.strip() for l in lof.read_text().splitlines()
            if l.strip() and not l.strip().startswith("--")]


# ── Load Order Reconciler (v2) ───────────────────────────────────────────────
#
# Robust mod_load_order.txt management.
#
# Provenance taxonomy for each entry in the file:
#   managed-present  : tracked in DB, folder exists on disk         → keep, topo-sort
#   managed-missing  : tracked in DB, folder absent                 → drop
#   orphan           : not tracked, no folder, listed in file       → drop
#   foreign          : not tracked, folder on disk                  → preserve verbatim
#   framework        : foreign + matches game's framework set       → pin to top
#
# Directives (optional, in-file, DMF-comment compatible):
#   -- nexmod:freeze
#   -- nexmod:framework <folder>
#   -- nexmod:pin <folder> top
#   -- nexmod:pin <folder> bottom
#   -- nexmod:pin <folder> before <other>
#   -- nexmod:pin <folder> after  <other>

# Per-game framework folders that are NOT installed via Nexus and must always
# pin to the top of the load order if present on disk. Empirically derived;
# Darktide installs these via dtkit-patch / DMF, not via Nexus mods.
GAME_FRAMEWORK_FOLDERS: dict[str, tuple[str, ...]] = {
    "darktide": ("mod_compat", "dmf", "DarktideLocalServer"),
}

_DIRECTIVE_RE = re.compile(r"^\s*--\s*nexmod:(\S+)(?:\s+(.*))?$")
BASE_HEADER = "-- File managed by nexmod"


def _parse_directives(lines: list[str]) -> dict:
    """Extract '-- nexmod:*' directives from raw lines."""
    out: dict = {"frozen": False, "framework": [], "pins": []}
    for raw in lines:
        m = _DIRECTIVE_RE.match(raw)
        if not m:
            continue
        verb = m.group(1).lower()
        args = (m.group(2) or "").strip().split()
        if verb == "freeze":
            out["frozen"] = True
        elif verb == "framework" and args:
            out["framework"].append(args[0])
        elif verb == "pin" and len(args) >= 2:
            folder, pos = args[0], args[1].lower()
            if pos in ("top", "bottom"):
                out["pins"].append((folder, pos, None))
            elif pos in ("before", "after") and len(args) >= 3:
                out["pins"].append((folder, pos, args[2]))
    return out


def _parse_load_order_file(text: str) -> dict:
    """Parse a load-order file into structured pieces preserving comments.

    The canonical BASE_HEADER ('-- File managed by nexmod') is recognised and
    stripped on parse; it is re-emitted exactly once on render.

    Anchored comments: a run of '--' comment lines immediately above an entry
    is attached to that entry and follows it through reordering.
    Header comments: comments before any entry (after directives).
    Tail comments:   comments after the last entry.
    """
    lines = text.splitlines()
    directives = _parse_directives(lines)
    directive_lines = [l for l in lines if _DIRECTIVE_RE.match(l)]

    entries: list[str] = []
    anchored: dict[str, list[str]] = {}
    header_comments: list[str] = []
    tail_comments: list[str] = []
    pending: list[str] = []
    seen_entry = False

    for raw in lines:
        if _DIRECTIVE_RE.match(raw):
            continue
        stripped = raw.strip()
        # Drop the canonical managed-by header — render emits it deterministically.
        if stripped == BASE_HEADER:
            continue
        if not stripped:
            if pending:
                (header_comments if not seen_entry else tail_comments).extend(pending)
                pending = []
            continue
        if stripped.startswith("--"):
            pending.append(raw)
            continue
        if pending:
            anchored[stripped] = pending
            pending = []
        entries.append(stripped)
        seen_entry = True

    if pending:
        (header_comments if not seen_entry else tail_comments).extend(pending)

    return {
        "directives":        directives,
        "directive_lines":   directive_lines,
        "header_comments":   header_comments,
        "entries":           entries,
        "anchored_comments": anchored,
        "tail_comments":     tail_comments,
    }


def _classify_entries(
    entries: list[str],
    *,
    db_folders: set[str],
    disk_folders: set[str],
    framework_folders: set[str],
    force_drop: set[str] | None = None,
) -> dict[str, str]:
    """Classify each listed folder: managed-present | managed-missing | orphan | foreign | framework."""
    _force_drop = force_drop or set()
    out: dict[str, str] = {}
    for f in entries:
        if f in _force_drop:
            out[f] = "orphan"
            continue
        in_db, on_disk = (f in db_folders), (f in disk_folders)
        if in_db and on_disk:
            out[f] = "managed-present"
        elif in_db and not on_disk:
            out[f] = "managed-missing"
        elif on_disk and f in framework_folders:
            out[f] = "framework"
        elif on_disk:
            out[f] = "foreign"
        else:
            out[f] = "orphan"
    return out


def _db_tracked_folders(
    db: sqlite3.Connection, game: str, *, disk_folders: set[str] | None = None,
) -> dict[str, int]:
    """Return {folder_name: mod_id} for tracked mods.

    Prefers the explicit ``folder_name`` column (captured at install time).
    Falls back to ``_archive_basename(filename)`` only when that name is also
    a real on-disk folder — this avoids classifying never-existed stems as
    DB-managed for legacy rows that never recorded folder_name.
    """
    out: dict[str, int] = {}
    rows = db.execute(
        "SELECT mod_id, filename, folder_name FROM mods WHERE game=?",
        (game,),
    ).fetchall()
    for row in rows:
        folder = row["folder_name"]
        if not folder and row["filename"]:
            stem = _archive_basename(row["filename"])
            if disk_folders is not None and stem in disk_folders:
                folder = stem
        if folder:
            out[folder] = row["mod_id"]
    return out


def _disk_folders(mod_dir: Path) -> set[str]:
    if not mod_dir.exists():
        return set()
    return {p.name for p in mod_dir.iterdir() if p.is_dir()}


def _load_db_pins(db: sqlite3.Connection, game: str) -> list[tuple[str, str, str | None]]:
    rows = db.execute(
        "SELECT folder, position, relative_to FROM load_order_pins WHERE game=? "
        "ORDER BY created_at",
        (game,),
    ).fetchall()
    return [(r["folder"], r["position"], r["relative_to"]) for r in rows]


def _apply_pins(
    *,
    sorted_managed: list[str],
    foreign: list[str],
    framework: list[str],
    pins: list[tuple[str, str, str | None]],
) -> list[str]:
    """Compose final order: framework → pin(top) → middle (managed+foreign, before/after pins applied) → pin(bottom)."""
    head: list[str] = []
    seen: set[str] = set()
    for f in framework:
        if f not in seen:
            head.append(f); seen.add(f)
    for folder, pos, _ in [p for p in pins if p[1] == "top"]:
        if folder not in seen:
            head.append(folder); seen.add(folder)

    middle = [m for m in sorted_managed if m not in seen]
    for f in foreign:
        if f not in seen and f not in middle:
            middle.append(f)

    for folder, pos, rel in [p for p in pins if p[1] in ("before", "after")]:
        if folder in middle and rel in middle:
            middle.remove(folder)
            idx = middle.index(rel) + (1 if pos == "after" else 0)
            middle.insert(idx, folder)

    seen.update(middle)
    tail: list[str] = []
    for folder, pos, _ in [p for p in pins if p[1] == "bottom"]:
        if folder not in seen:
            tail.append(folder); seen.add(folder)

    return head + middle + tail


def _render_load_order(
    *,
    directive_lines: list[str],
    header_comments: list[str],
    ordered: list[str],
    anchored_comments: dict[str, list[str]],
    tail_comments: list[str],
) -> str:
    """Render the final file contents, preserving directives + comments."""
    out: list[str] = []
    has_base = any(BASE_HEADER in l for l in header_comments)
    if not has_base:
        out.append(BASE_HEADER)
    out.extend(directive_lines)
    out.extend(header_comments)
    for folder in ordered:
        out.extend(anchored_comments.get(folder, []))
        out.append(folder)
    out.extend(tail_comments)
    return "\n".join(out) + "\n"


def _atomic_write_with_backup(target: Path, contents: str) -> None:
    """Write CONTENTS to TARGET via tempfile + os.replace + fsync. Keeps a .bak of the previous file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        bak = target.with_suffix(target.suffix + ".bak")
        try:
            shutil.copy2(target, bak)
        except Exception as e:
            log.warning("could not write backup %s: %s", bak, e)
    fd, tmp_path = tempfile.mkstemp(prefix=".nexmod-tmp-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(contents)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
        try:
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # some filesystems don't support directory fsync
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _flock_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".lock")


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def reconcile_load_order(
    game: str,
    db: sqlite3.Connection,
    mod_dir: Path,
    *,
    dry_run: bool = False,
    auto_merge: bool = False,
    profile_set: list[str] | None = None,
    strict_profile: bool = False,
    inject_directives: list[str] | None = None,
    force_drop: set[str] | None = None,
) -> dict:
    """Reconcile mod_load_order.txt against DB + disk + pins + directives.

    Single entrypoint for every state-mutating command.

    Args:
        profile_set:    if not None, treat this list as the desired managed-mod set
                        (used by 'profile load'). Foreign entries are kept by default.
        strict_profile: with profile_set, also wipe foreign entries.
        auto_merge:     proceed even if external drift is detected.
        dry_run:        compute and return diff, do not write.

    Returns dict with keys: changed, written, drift_detected, classification,
    diff, orphans_dropped, missing_added, foreign_kept, cycles, missing_deps, frozen.
    """
    info = GAMES.get(game) or {}
    lof_filename = info.get("load_order_file")
    if not lof_filename:
        return _empty_reconcile_result()

    lof = mod_dir / lof_filename
    lock_path = _flock_path(lof)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)

    try:
        for attempt in range(4):
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == 3:
                    raise RuntimeError(
                        f"Could not acquire lock on {lock_path}; another nexmod process running?"
                    )
                time.sleep(0.2)

        original_text = lof.read_text() if lof.exists() else ""
        h_now = _hash_text(original_text)

        h_db_row = db.execute(
            "SELECT last_hash FROM load_order_state WHERE game=?", (game,)
        ).fetchone()
        h_db = h_db_row["last_hash"] if h_db_row else None
        drift_detected = bool(h_db and h_now and h_now != h_db)

        parsed = _parse_load_order_file(original_text)
        directives        = parsed["directives"]
        entries_in        = parsed["entries"]
        anchored_comments = parsed["anchored_comments"]
        header_comments   = parsed["header_comments"]
        tail_comments     = parsed["tail_comments"]
        directive_lines   = parsed["directive_lines"]

        # Merge profile-saved directives that aren't already in the file.
        # This restores pins/freeze when loading a profile onto a fresh machine.
        if inject_directives:
            existing_dir_set = set(directive_lines)
            new_dirs = [d for d in inject_directives if d not in existing_dir_set]
            if new_dirs:
                directive_lines = directive_lines + new_dirs
                directives = _parse_directives(directive_lines)

        frozen = directives["frozen"]

        disk            = _disk_folders(mod_dir)
        db_folder_to_id = _db_tracked_folders(db, game, disk_folders=disk)
        db_folders      = set(db_folder_to_id.keys())
        framework_set   = set(GAME_FRAMEWORK_FOLDERS.get(game, ())) | set(directives["framework"])

        pins = list(directives["pins"]) + _load_db_pins(db, game)

        classification = _classify_entries(
            entries_in,
            db_folders=db_folders,
            disk_folders=disk,
            framework_folders=framework_set,
            force_drop=force_drop or set(),
        )

        orphans_dropped = [f for f, c in classification.items()
                           if c in ("orphan", "managed-missing")]

        listed = set(entries_in)
        missing_added: list[str] = sorted(f for f in db_folders
                                          if f in disk and f not in listed)
        for fw in framework_set:
            if fw in disk and fw not in listed and fw not in missing_added:
                missing_added.append(fw)

        if frozen:
            return {
                "changed": False, "written": False, "drift_detected": drift_detected,
                "classification": classification, "diff": "",
                "orphans_dropped": orphans_dropped, "missing_added": missing_added,
                "foreign_kept": [f for f, c in classification.items() if c == "foreign"],
                "cycles": [], "missing_deps": {}, "frozen": True,
            }

        dropped_by_profile: list[str] = []
        cycles: list[str] = []
        missing_deps: dict[str, list[str]] = {}

        if profile_set is not None:
            # Profile mode: the profile's order is canonical — no topo reorder.
            # Render: framework_list + (profile order minus framework) + foreign_tail
            foreign_keep: set[str] = set()
            if not strict_profile:
                foreign_keep = {f for f, c in classification.items() if c == "foreign"}

            kept_in_file = [f for f in entries_in
                            if f in framework_set or f in set(profile_set) or f in foreign_keep]
            dropped_by_profile = [f for f in entries_in if f not in kept_in_file]

            framework_list = [f for f in kept_in_file if f in framework_set]
            for fw in framework_set:
                if fw in disk and fw not in framework_list:
                    framework_list.append(fw)

            profile_body = [f for f in profile_set if f not in framework_set]

            foreign_tail = [f for f in kept_in_file
                            if f not in framework_set and f not in set(profile_body)]

            ordered = framework_list + profile_body + foreign_tail
            foreign_list = foreign_tail

            # Diagnostics only — does not affect ordering.
            managed_subset_for_deps = [f for f in ordered if f in db_folders and f in disk]
            for folder in managed_subset_for_deps:
                raw = _parse_mod_deps(mod_dir, folder)
                missing = [d for d in raw if d not in set(managed_subset_for_deps)]
                if missing:
                    missing_deps[folder] = missing
        else:
            entries_after_profile = list(entries_in)

            # Working set: drop orphans/missing, then add discovered managed/framework
            working = [f for f in entries_after_profile
                       if classification.get(f) not in ("orphan", "managed-missing")]
            for f in missing_added:
                if f not in working:
                    working.append(f)

            # Topo-sort the managed-present subset; foreign + framework keep relative order
            managed_subset = [f for f in working if f in db_folders and f in disk]
            managed_set    = set(managed_subset)
            deps_map: dict[str, list[str]] = {}
            for folder in managed_subset:
                raw = _parse_mod_deps(mod_dir, folder)
                known   = [d for d in raw if d in managed_set]
                missing = [d for d in raw if d not in managed_set]
                deps_map[folder] = known
                if missing:
                    missing_deps[folder] = missing
            sorted_managed, cycles = _topo_sort(managed_subset, deps_map)

            foreign_list   = [f for f in working
                              if f not in db_folders and f in disk and f not in framework_set]
            framework_list = [f for f in working if f in framework_set]
            for fw in framework_set:
                if fw in disk and fw not in framework_list:
                    framework_list.append(fw)

            ordered = _apply_pins(
                sorted_managed=sorted_managed + cycles,
                foreign=foreign_list,
                framework=framework_list,
                pins=pins,
            )

        new_text = _render_load_order(
            directive_lines=directive_lines,
            header_comments=header_comments,
            ordered=ordered,
            anchored_comments=anchored_comments,
            tail_comments=tail_comments,
        )

        changed = (new_text != original_text)
        diff_text = ""
        if dry_run and changed:
            diff_text = "".join(difflib.unified_diff(
                original_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"{lof} (current)",
                tofile=f"{lof} (proposed)",
                n=3,
            ))

        if drift_detected and changed and not auto_merge and not dry_run:
            log.warning(
                "Drift detected on %s: file changed externally since last write. "
                "Refusing to overwrite. Pass --auto-merge to proceed.", lof,
            )
            return {
                "changed": False, "written": False, "drift_detected": True,
                "classification": classification, "diff": "",
                "orphans_dropped": orphans_dropped + dropped_by_profile,
                "missing_added": missing_added,
                "foreign_kept": foreign_list, "cycles": cycles,
                "missing_deps": missing_deps, "frozen": False,
            }

        written = False
        if changed and not dry_run:
            _atomic_write_with_backup(lof, new_text)
            new_hash = _hash_text(new_text)
            db.execute("""
                INSERT INTO load_order_state (game, file_path, last_hash, last_written_at, frozen)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(game) DO UPDATE SET
                    file_path       = excluded.file_path,
                    last_hash       = excluded.last_hash,
                    last_written_at = excluded.last_written_at
            """, (game, str(lof), new_hash, now_iso()))
            db.commit()
            written = True
        elif not changed and h_db is None and h_now:
            # First-run adoption: persist current hash for future drift detection.
            db.execute("""
                INSERT INTO load_order_state (game, file_path, last_hash, last_written_at, frozen)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(game) DO UPDATE SET
                    file_path       = excluded.file_path,
                    last_hash       = excluded.last_hash,
                    last_written_at = excluded.last_written_at
            """, (game, str(lof), h_now, now_iso()))
            db.commit()

        return {
            "changed": changed, "written": written, "drift_detected": drift_detected,
            "classification": classification, "diff": diff_text,
            "orphans_dropped": orphans_dropped + dropped_by_profile,
            "missing_added": missing_added, "foreign_kept": foreign_list,
            "cycles": cycles, "missing_deps": missing_deps, "frozen": False,
        }
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _empty_reconcile_result() -> dict:
    return {
        "changed": False, "written": False, "drift_detected": False,
        "classification": {}, "diff": "", "orphans_dropped": [],
        "missing_added": [], "foreign_kept": [], "cycles": [],
        "missing_deps": {}, "frozen": False,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Print debug output to terminal (always logged to file).")
@click.version_option(version=__version__, prog_name="nexmod")
@click.pass_context
def cli(ctx, verbose):
    """nexmod — download and update Nexus mods on Linux"""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    setup_logging(verbose)


# config ──────────────────────────────────────────────────────────────────────

@cli.group()
def config():
    """API key and settings."""
    pass

@config.command("set-key")
@click.argument("key")
def config_set_key(key):
    """Store your Nexus Mods API key."""
    cfg = load_config()
    cfg["api_key"] = key
    save_config(cfg)
    log.info("API key updated")
    console.print("[green]API key saved (chmod 600).[/green]")

@config.command("show")
def config_show():
    """Print current config (key masked)."""
    cfg = load_config()
    if "api_key" in cfg:
        k = cfg["api_key"]
        console.print(f"api_key:      {k[:8]}...{k[-4:]}")
    else:
        console.print("[yellow]No API key configured.[/yellow]")
    ar = cfg.get("auto_reorder", True)
    console.print(f"auto_reorder: {'[green]on[/green]' if ar else '[yellow]off[/yellow]'}")
    console.print(f"Log file:     {LOG_FILE}")
    console.print(f"Database:     {DB_FILE}")

@config.command("verify")
def config_verify():
    """Hit the Nexus API to confirm key + premium status."""
    api_key = get_api_key()
    try:
        data    = nexus_get("users/validate.json", api_key)
        name    = data.get("name", "?")
        premium = data.get("is_premium", False)
        console.print(f"[green]Authenticated as:[/green] {name}")
        console.print(f"Premium:   {'[green]YES[/green]' if premium else '[yellow]NO[/yellow]'}")
        console.print(f"Supporter: {'yes' if data.get('is_supporter') else 'no'}")
        if not premium:
            console.print(
                "[yellow]Free account: direct downloads unavailable.[/yellow]\n"
                "  Use [cyan]nexmod nxm-register[/cyan] then click 'Mod Manager Download' on nexusmods.com,\n"
                "  or download archives manually and use [cyan]nexmod install <game> <id> --from-file <archive>[/cyan]."
            )
    except Exception as e:
        log.error("API verify failed: %s", e)
        console.print(f"[red]API error: {e}[/red]")
        sys.exit(1)

@config.command("auto-reorder")
@click.argument("state", type=click.Choice(["on", "off"], case_sensitive=False))
def config_auto_reorder(state):
    """Enable or disable automatic load order sorting after install/update (default: on)."""
    cfg = load_config()
    cfg["auto_reorder"] = (state.lower() == "on")
    save_config(cfg)
    label = "[green]on[/green]" if cfg["auto_reorder"] else "[yellow]off[/yellow]"
    console.print(f"auto_reorder → {label}")


# profile ─────────────────────────────────────────────────────────────────────

@cli.group()
def profile():
    """Named mod profiles for hotswapping load order arrangements."""
    pass


@profile.command("save")
@click.argument("game")
@click.argument("name", default="default")
@click.option("--description", "-d", default="", help="Short description stored with the profile")
@click.option("--force", "-f", is_flag=True, help="Overwrite without prompting if profile exists")
def profile_save(game, name, description, force):
    """Snapshot the current load order as a named profile.

    NAME defaults to 'default' if omitted.

    \b
    Examples:
      nexmod profile save darktide
      nexmod profile save darktide full
      nexmod profile save darktide minimal -d "QoL only, no combat changes"
    """
    info = GAMES.get(game)
    if not info or not info.get("load_order_file"):
        console.print(f"[yellow]{game} does not support a managed load order.[/yellow]")
        return

    db      = get_db()
    mod_dir = resolve_mod_dir(game, db)

    lof_path = mod_dir / info["load_order_file"]
    lof_text = lof_path.read_text() if lof_path.exists() else ""
    parsed   = _parse_load_order_file(lof_text)
    folders  = parsed["entries"]
    directive_lines = parsed["directive_lines"]

    if not folders:
        console.print(f"[yellow]Load order file is empty or missing for {game}.[/yellow]")
        return

    path = _profile_path(game, name)
    if path.exists() and not force:
        if not click.confirm(f"Profile '{name}' already exists. Overwrite?", default=False):
            console.print("[dim]Aborted.[/dim]")
            return

    # Warn about foreign (untracked) entries — they can't be auto-installed later.
    tracked = _db_tracked_folders(db, game)
    foreign = [f for f in folders if f not in tracked]
    if foreign:
        label = ", ".join(foreign[:5]) + (" ..." if len(foreign) > 5 else "")
        console.print(
            f"[yellow]Note:[/yellow] {len(foreign)} folder(s) not tracked by nexmod "
            f"(cannot be auto-installed via --install): {label}"
        )

    # Build mod list from DB for restore/share on a clean machine.
    rows = db.execute(
        "SELECT mod_id, name, version, filename, folder_name FROM mods WHERE game = ?",
        (game,),
    ).fetchall()
    folder_to_row: dict[str, object] = {}
    for r in rows:
        fn = r["folder_name"] or (_archive_basename(r["filename"]) if r["filename"] else None)
        if fn:
            folder_to_row[fn] = r
    profile_domain = GAMES.get(game, {}).get("domain", game)
    mods_list: list[dict] = []
    for folder in folders:
        r = folder_to_row.get(folder)
        if r is None:
            continue  # untracked — skip; these were already warned about above
        mods_list.append({
            "mod_id":      r["mod_id"],
            "name":        r["name"] or "",
            "version":     r["version"],
            "folder_name": folder,
            "domain":      profile_domain,
        })

    _write_profile(game, name, folders, description, directive_lines, mods=mods_list)
    log.info("Profile '%s' saved for %s (%d mods)", name, game, len(folders))
    console.print(f"[green]✓ Profile saved:[/green] {name} ({len(folders)} mods)")


@profile.command("list")
@click.argument("game")
def profile_list(game):
    """List all saved profiles for GAME."""
    profiles = _list_profiles(game)
    if not profiles:
        console.print(f"[yellow]No profiles saved for {game}.[/yellow]")
        console.print(f"Create one: nexmod profile save {game}")
        return

    db  = get_db()
    row = db.execute("SELECT active_profile FROM load_order_state WHERE game=?", (game,)).fetchone()
    active = row["active_profile"] if row else None

    t = Table(title=f"Profiles — {game}", show_lines=False)
    t.add_column("Name",        style="bold cyan")
    t.add_column("",            width=8)
    t.add_column("Mods",        justify="right", style="dim")
    t.add_column("Updated",     style="dim")
    t.add_column("Description")
    for p in profiles:
        marker = "[green]active[/green]" if p["name"] == active else ""
        t.add_row(
            p["name"],
            marker,
            str(len(p.get("load_order", []))),
            (p.get("updated_at") or p.get("created_at") or "?")[:10],
            p.get("description") or "",
        )
    console.print(t)


@profile.command("show")
@click.argument("game")
@click.argument("name")
def profile_show(game, name):
    """Print the full mod list stored in a profile."""
    p      = _read_profile(game, name)
    order  = p.get("load_order", [])
    db     = get_db()
    info   = GAMES.get(game, {})

    # Check which profile mods are missing from disk right now
    missing_on_disk: set[str] = set()
    if info.get("load_order_file"):
        mod_dir = resolve_mod_dir(game, db)
        missing_on_disk = {f for f in order if not (mod_dir / f).exists()}

    t = Table(
        title=f"Profile: [bold]{name}[/bold] ({game})",
        show_lines=False, box=None, padding=(0, 1),
    )
    t.add_column("#",      style="dim", width=4, justify="right")
    t.add_column("Folder", style="bold")
    t.add_column("",       width=10)
    for i, folder in enumerate(order, 1):
        flag = "[red]missing[/red]" if folder in missing_on_disk else ""
        t.add_row(str(i), folder, flag)
    console.print(t)

    if p.get("description"):
        console.print(f"\n[dim]{p['description']}[/dim]")
    if missing_on_disk:
        console.print(f"\n[yellow]{len(missing_on_disk)} mod(s) in this profile are not on disk.[/yellow]")


@profile.command("load")
@click.argument("game")
@click.argument("name", default="default")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing anything")
@click.option("--install", "do_install_missing", is_flag=True,
              help="Install profile mods not on disk (uses tracked mod_id from the DB).")
@click.option("--strict", is_flag=True,
              help="Also drop foreign entries (untracked folders the user/game added). "
                   "Default preserves them.")
@click.option("--auto-merge", "auto_merge", is_flag=True,
              help="Apply even if the load order file was edited externally since last write.")
def profile_load(game, name, dry_run, do_install_missing, strict, auto_merge):
    """Apply a profile — sets the managed load order to the profile's mod list.

    NAME defaults to 'default'. If 'default' has not been saved yet, it is
    auto-created from the current load order before loading.

    Mods in the profile are pinned to load. Foreign entries (folders not
    tracked by nexmod) are preserved by default; pass --strict to drop them.

    \b
    Examples:
      nexmod profile load darktide
      nexmod profile load darktide minimal
      nexmod profile load darktide full --dry-run
      nexmod profile load darktide full --install
      nexmod profile load darktide minimal --strict
    """
    if dry_run and do_install_missing:
        console.print("[red]--dry-run and --install are mutually exclusive.[/red]")
        sys.exit(1)

    info = GAMES.get(game)
    if not info or not info.get("load_order_file"):
        console.print(f"[yellow]{game} does not support a managed load order.[/yellow]")
        return

    db      = get_db()
    mod_dir = resolve_mod_dir(game, db)

    # Auto-create "default" from current LOF if it hasn't been saved yet.
    if name == "default" and not _profile_path(game, "default").exists():
        lof_text = ""
        lof_path = mod_dir / info["load_order_file"]
        if lof_path.exists():
            lof_text = lof_path.read_text()
        pdefault = _parse_load_order_file(lof_text)
        _write_profile(
            game, "default", pdefault["entries"],
            "Auto-created default profile", pdefault["directive_lines"],
        )
        console.print(f"[dim]Auto-created 'default' profile ({len(pdefault['entries'])} mods).[/dim]")

    p        = _read_profile(game, name)
    lof_file = info["load_order_file"]

    profile_order = p.get("load_order", [])
    current_order = _read_lof_folders(mod_dir, lof_file)

    profile_set  = set(profile_order)
    current_set  = set(current_order)
    added        = [m for m in profile_order if m not in current_set]
    removed      = [m for m in current_order  if m not in profile_set]

    # Warn about profile mods not on disk
    missing_on_disk = [m for m in profile_order if not (mod_dir / m).exists()]

    console.print(f"[bold]Profile:[/bold] {name}  "
                  f"([cyan]{len(profile_order)}[/cyan] mods)")
    if added:
        console.print(f"  [green]+[/green] Enabling:  {', '.join(added)}")
    if removed:
        console.print(f"  [yellow]-[/yellow] Disabling: {', '.join(removed)}")
    # Check if any saved directives are missing from the current file — if so,
    # we still need reconcile to run even if the folder list is identical.
    saved_directives  = p.get("directives", [])
    lof_text_for_dirs = (mod_dir / lof_file).read_text() if (mod_dir / lof_file).exists() else ""
    current_dir_lines = _parse_load_order_file(lof_text_for_dirs)["directive_lines"]
    pending_directives = [d for d in saved_directives if d not in set(current_dir_lines)]

    if not added and not removed and not missing_on_disk and not pending_directives:
        console.print(f"  [dim]Load order already matches this profile — no changes.[/dim]")
        # Still record this as the active profile.
        db.execute("""
            INSERT INTO load_order_state (game, file_path, last_hash, last_written_at, frozen, active_profile)
            VALUES (?, '', '', '', 0, ?)
            ON CONFLICT(game) DO UPDATE SET active_profile = excluded.active_profile
        """, (game, name))
        db.commit()
        return
    if missing_on_disk:
        console.print(f"\n  [red]Warning:[/red] {len(missing_on_disk)} profile mod(s) not on disk "
                      f"(not installed): {', '.join(missing_on_disk[:5])}"
                      + (" ..." if len(missing_on_disk) > 5 else ""))

    if do_install_missing and missing_on_disk:
        api_key = get_api_key()
        # If the profile carries a "mods" list (saved by profile_save ≥ 0.9.x),
        # use it directly — mod_id is right there, no DB lookup needed.
        profile_mods = p.get("mods")
        if profile_mods:
            folder_to_mod_id: dict[str, int] = {
                m["folder_name"]: m["mod_id"]
                for m in profile_mods
                if m.get("folder_name") and m.get("mod_id") is not None
            }
        else:
            # Backward compat: old profiles without "mods" key fall back to DB lookup.
            rows = db.execute(
                "SELECT mod_id, name, filename, folder_name FROM mods WHERE game = ?", (game,)
            ).fetchall()
            # Prefer folder_name column (written at install time); fall back to archive basename.
            folder_to_mod_id = {}
            for r in rows:
                fn = r["folder_name"] or (_archive_basename(r["filename"]) if r["filename"] else None)
                if fn:
                    folder_to_mod_id[fn] = r["mod_id"]

        installed = 0
        skipped: list[str] = []
        for folder in missing_on_disk:
            mod_id = folder_to_mod_id.get(folder)
            if mod_id is None:
                skipped.append(folder)
                continue
            console.print(f"\n[cyan]Installing[/cyan] {folder} (mod_id={mod_id})...")
            try:
                do_install(game, mod_id, None, api_key, db)
                installed += 1
            except Exception as e:
                log.error("profile load --install failed for %s: %s", folder, e)
                console.print(f"  [red]Install failed: {e}[/red]")
                skipped.append(folder)

        if skipped:
            console.print(
                f"\n[yellow]{len(skipped)} mod(s) could not be auto-installed "
                f"(no DB record or install failed):[/yellow] {', '.join(skipped)}"
            )
        console.print(f"[green]Installed {installed}/{len(missing_on_disk)} missing profile mod(s).[/green]")

    result = reconcile_load_order(
        game, db, mod_dir,
        dry_run=dry_run,
        auto_merge=auto_merge,
        profile_set=profile_order,
        strict_profile=strict,
        inject_directives=p.get("directives", []),
    )

    if result["drift_detected"] and not result["written"]:
        console.print(
            "[yellow]External edit to load order detected — refusing to overwrite.\n"
            f"Review with 'nexmod order {game}' first, "
            "or pass --auto-merge to apply anyway.[/yellow]"
        )
        return

    if dry_run:
        if result["diff"]:
            console.print(Syntax(result["diff"], "diff", theme="ansi_dark"))
        console.print("\n[dim]Dry run — no changes made.[/dim]")
        return

    log.info("Profile '%s' applied for %s (%d mods)", name, game, len(profile_order))

    # Always record the active profile — even when no write was needed.
    db.execute("""
        INSERT INTO load_order_state (game, file_path, last_hash, last_written_at, frozen, active_profile)
        VALUES (?, '', '', '', 0, ?)
        ON CONFLICT(game) DO UPDATE SET active_profile = excluded.active_profile
    """, (game, name))
    db.commit()

    if result["written"]:
        console.print(f"\n[green]✓ Profile '{name}' applied.[/green]")
        if result["foreign_kept"] and not strict:
            console.print(f"[dim]  ({len(result['foreign_kept'])} foreign "
                          f"entr{'y' if len(result['foreign_kept']) == 1 else 'ies'} "
                          f"preserved — pass --strict to drop them.)[/dim]")
    else:
        console.print(f"[dim]Load order already matches profile '{name}'.[/dim]")


@profile.command("delete")
@click.argument("game")
@click.argument("name")
@click.option("--force", "-f", is_flag=True, help="Skip confirmation")
def profile_delete(game, name, force):
    """Delete a saved profile."""
    path = _profile_path(game, name)
    if not path.exists():
        console.print(f"[red]Profile '{name}' not found for {game}.[/red]")
        sys.exit(1)
    if not force and not click.confirm(f"Delete profile '{name}'?", default=False):
        console.print("[dim]Aborted.[/dim]")
        return
    path.unlink()
    log.info("Profile '%s' deleted for %s", name, game)
    console.print(f"[green]✓ Deleted profile '{name}'.[/green]")


@profile.command("status")
@click.argument("game")
def profile_status(game):
    """Show which profile is currently active for GAME."""
    db  = get_db()
    row = db.execute("SELECT active_profile FROM load_order_state WHERE game=?", (game,)).fetchone()
    active = row["active_profile"] if row else None
    if not active:
        console.print(f"[dim]No profile loaded yet for {game}.[/dim]")
        console.print(f"Load one: nexmod profile load {game} <name>")
        return
    profiles = {p["name"]: p for p in _list_profiles(game)}
    p = profiles.get(active)
    if p:
        console.print(f"[bold]Active:[/bold] [cyan]{active}[/cyan] "
                      f"({len(p.get('load_order', []))} mods)")
        if p.get("description"):
            console.print(f"[dim]{p['description']}[/dim]")
    else:
        console.print(f"[bold]Active:[/bold] [cyan]{active}[/cyan] "
                      f"[yellow](profile file missing — run 'nexmod profile save {game} {active}' to recreate)[/yellow]")


@profile.command("rename")
@click.argument("game")
@click.argument("old_name")
@click.argument("new_name")
def profile_rename(game, old_name, new_name):
    """Rename a profile."""
    old_path = _profile_path(game, old_name)
    new_path = _profile_path(game, new_name)
    if not old_path.exists():
        console.print(f"[red]Profile '{old_name}' not found for {game}.[/red]")
        sys.exit(1)
    if new_path.exists():
        console.print(f"[red]A profile named '{new_name}' already exists.[/red]")
        sys.exit(1)
    data = json.loads(old_path.read_text())
    data["name"]       = new_name
    data["updated_at"] = now_iso()
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text(json.dumps(data, indent=2))
    old_path.unlink()
    log.info("Profile renamed %s → %s for %s", old_name, new_name, game)
    console.print(f"[green]✓ Renamed '{old_name}' → '{new_name}'.[/green]")


# logs ────────────────────────────────────────────────────────────────────────

@cli.command("logs")
@click.option("--lines", "-n", default=50, help="Number of log lines to show (default 50)")
@click.option("--errors", "-e", is_flag=True, help="Show only ERROR and WARNING lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log in real time (like tail -f)")
def show_logs(lines, errors, follow):
    """Show nexmod's own operation log."""
    if not LOG_FILE.exists():
        console.print("[yellow]No log file yet — run some commands first.[/yellow]")
        return

    if follow:
        if errors:
            tail = subprocess.Popen(["tail", "-f", str(LOG_FILE)], stdout=subprocess.PIPE)
            grep = subprocess.Popen(
                ["grep", "--line-buffered", "-E", "ERROR|WARNING"],
                stdin=tail.stdout,
            )
            try:
                grep.wait()
            except KeyboardInterrupt:
                tail.terminate()
                grep.terminate()
        else:
            subprocess.run(["tail", "-f", str(LOG_FILE)])
        return

    text = LOG_FILE.read_text()
    all_lines = text.splitlines()
    if errors:
        all_lines = [l for l in all_lines if " ERROR " in l or " WARNING " in l]
    tail = "\n".join(all_lines[-lines:])
    console.print(Syntax(tail, "text", theme="monokai", line_numbers=False, word_wrap=True))


# history ─────────────────────────────────────────────────────────────────────

@cli.command("history")
@click.argument("game", required=False, default=None)
@click.option("--limit", "-n", default=30, help="Number of entries (default 30)")
@click.option("--failures", "-f", is_flag=True, help="Show only failures")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def show_history(game, limit, failures, as_json):
    """Show install/update history."""
    db = get_db()
    q  = "SELECT * FROM history"
    p: list = []
    conditions = []
    if game:
        conditions.append("game = ?")
        p.append(game)
    if failures:
        conditions.append("status != 'ok'")
    if conditions:
        q += " WHERE " + " AND ".join(conditions)
    q += " ORDER BY timestamp DESC LIMIT ?"
    p.append(limit)

    rows = db.execute(q, p).fetchall()
    if not rows:
        if as_json:
            click.echo("[]")
        else:
            console.print("[yellow]No history records.[/yellow]")
        return

    if as_json:
        click.echo(json.dumps([dict(r) for r in rows]))
        return

    t = Table(title="Operation history", show_lines=False)
    t.add_column("Time", style="dim")
    t.add_column("Action")
    t.add_column("Game")
    t.add_column("Mod")
    t.add_column("Version", style="cyan")
    t.add_column("Status")
    for r in rows:
        status_str = "[green]ok[/green]" if r["status"] == "ok" else f"[red]{r['status']}[/red]"
        t.add_row(
            r["timestamp"][:16].replace("T", " "),
            r["action"], r["game"] or "",
            f"[{r['mod_id']}] {r['mod_name'] or ''}"[:45],
            r["version"] or "?", status_str,
        )
    console.print(t)


# diag ────────────────────────────────────────────────────────────────────────

@cli.command("diag")
@click.argument("game")
@click.option("--lines", "-n", default=80, help="Lines of game log to scan (default 80)")
@click.option("--all", "show_all", is_flag=True, help="Show full log tail, not just errors")
def diag(game, lines, show_all):
    """Surface mod errors from the game's own log file."""
    info = GAMES.get(game)
    if not info:
        console.print(f"[red]Unknown game '{game}'.[/red]")
        sys.exit(1)

    log_subpath = info.get("log_subpath")
    game_log: Path | None = None

    if log_subpath:
        # Check Proton AppData first
        appdata = find_proton_appdata(info["steam_id"])
        if appdata:
            candidate = appdata / log_subpath
            if candidate.exists():
                game_log = candidate

        # Also check native XDG paths
        if not game_log:
            for xdg in [Path.home() / ".local/share", Path.home() / ".config"]:
                candidate = xdg / log_subpath
                if candidate.exists():
                    game_log = candidate
                    break

    if not game_log:
        console.print(f"[yellow]No game log found for '{game}'.[/yellow]")
        if log_subpath:
            console.print(f"[dim]Expected at: AppData/Roaming/{log_subpath}[/dim]")
            console.print("[dim]Launch the game at least once to generate it.[/dim]")
        else:
            console.print(f"[dim]Log path not configured for {info['name']}.[/dim]")
        return

    console.print(f"[dim]Game log: {game_log}[/dim]")
    size_kb = game_log.stat().st_size // 1024
    console.print(f"[dim]Size: {size_kb} KB[/dim]\n")

    # Darktide: check if the game bundle was updated more recently than dtkit-patch.
    # After a Fatshark patch the bundle is reset and mods silently stop loading.
    if game == "darktide":
        try:
            db = get_db()
            _mod_dir = resolve_mod_dir(game, db)
            _game_dir = _mod_dir.parent
            _bundle_dir = _game_dir / "bundle"
            _dtkit = _find_dtkit(_game_dir)
            if _bundle_dir.exists() and _dtkit and _dtkit.exists():
                _dtkit_mtime = _dtkit.stat().st_mtime
                _bundle_files = list(_bundle_dir.iterdir())
                if _bundle_files:
                    _newest_bundle = max(f.stat().st_mtime for f in _bundle_files)
                    if _newest_bundle > _dtkit_mtime:
                        console.print(
                            "[yellow]Warning:[/yellow] Game bundle files are newer than dtkit-patch — "
                            "a game update may have reset mod support.\n"
                            "  If mods are not loading, re-run: [cyan]nexmod enable darktide[/cyan]"
                        )
        except Exception:
            pass  # never crash diag due to this optional check

    text      = game_log.read_text(errors="replace")
    all_lines = text.splitlines()
    tail      = all_lines[-lines:]

    if show_all:
        console.print(Syntax("\n".join(tail), "text", theme="monokai", word_wrap=True))
        return

    # Filter for mod-related errors
    error_patterns = [
        re.compile(r'\[error\]', re.I),
        re.compile(r'\[warning\]', re.I),
        re.compile(r'mod.*error', re.I),
        re.compile(r'error.*mod', re.I),
        re.compile(r'failed to load', re.I),
        re.compile(r'script error', re.I),
        re.compile(r'lua.*error', re.I),
        re.compile(r'exception', re.I),
        re.compile(r'crash', re.I),
        re.compile(r'dmf', re.I),
    ]

    hits = []
    for i, line in enumerate(tail):
        if any(p.search(line) for p in error_patterns):
            # Include a line of context above
            if i > 0:
                hits.append(f"[dim]{tail[i-1]}[/dim]")
            hits.append(f"[yellow]{line}[/yellow]")

    if hits:
        console.print(f"[red bold]Found {len(hits)} potential mod issues:[/red bold]\n")
        console.print("\n".join(hits))
    else:
        console.print(f"[green]No mod errors found in last {lines} log lines.[/green]")

    console.print(f"\n[dim]Run with --all to see the full tail, or --lines N for more context.[/dim]")


# path ────────────────────────────────────────────────────────────────────────

@cli.group()
def path():
    """Override auto-detected mod directory paths."""
    pass

@path.command("set")
@click.argument("game")
@click.argument("mod_dir")
def path_set(game, mod_dir):
    """Manually set the mod directory for GAME."""
    db = get_db()
    normalised = str(Path(mod_dir).expanduser().resolve())
    db.execute("INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)", (game, normalised))
    db.commit()
    log.info("mod dir for %s set to %s", game, normalised)
    console.print(f"[green]{game} mod dir → {normalised}[/green]")

@path.command("show")
@click.argument("game")
def path_show(game):
    """Show the mod directory nexmod will use for GAME."""
    db = get_db()
    d = resolve_mod_dir(game, db)
    console.print(str(d))


# games ───────────────────────────────────────────────────────────────────────

@cli.command("games")
def games_list():
    """List built-in game profiles."""
    t = Table(title="Supported Games", show_lines=False)
    t.add_column("slug", style="bold cyan")
    t.add_column("name")
    t.add_column("nexus domain", style="dim")
    for slug, info in GAMES.items():
        t.add_row(slug, info["name"], info["domain"])
    console.print(t)


# search ──────────────────────────────────────────────────────────────────────

@cli.command("search")
@click.argument("game")
@click.argument("query")
@click.option("--count", default=10, show_default=True,
              help="Number of results to return (1–50).")
@click.option("--json", "as_json", is_flag=True,
              help="Emit machine-readable JSON (sorted by endorsements desc).")
def search_mods(game, query, count, as_json):
    """Search Nexus Mods for mods by name (v2 GraphQL).

    \b
    Examples:
      nexmod search darktide "enemy health"
      nexmod search darktide "camera shake" --count 20
      nexmod search darktide "auspex" --json

    Results are sorted by endorsements (descending). Install a result with:
      nexmod install <game> <ID>
    """
    if game not in GAMES:
        console.print(f"[red]Unknown game '{game}'. Run 'nexmod games' to list supported slugs.[/red]")
        sys.exit(1)

    count = max(1, min(50, count))
    api_key = get_api_key()
    domain = GAMES[game]["domain"]

    if not as_json:
        console.print(f'Searching [bold]"{query}"[/bold] on {domain}…')

    try:
        nodes = api_search_mods(domain, query, api_key, count=count)
    except RuntimeError as e:
        console.print(f"[red]Search failed: {e}[/red]")
        sys.exit(1)

    # Sort by endorsements descending
    nodes = sorted(nodes, key=lambda n: n.get("endorsements") or 0, reverse=True)

    # Build the set of installed mod_ids for this game.
    db = get_db()
    installed_ids: set[int] = {
        r[0] for r in db.execute(
            "SELECT mod_id FROM mods WHERE game = ?", (game,)
        ).fetchall()
    }

    if as_json:
        import json as _json
        out = [
            {
                "mod_id": n.get("modId"),
                "name": n.get("name", ""),
                "summary": n.get("summary", "") or "",
                "downloads": n.get("downloads") or 0,
                "endorsements": n.get("endorsements") or 0,
                "installed": (n.get("modId") or 0) in installed_ids,
            }
            for n in nodes
        ]
        click.echo(_json.dumps(out))
        return

    if not nodes:
        console.print("[yellow]No results found.[/yellow]")
        return

    console.print(f"[dim]{len(nodes)} result(s)[/dim]\n")

    def _fmt_num(n) -> str:
        try:
            return f"{int(n):,}"
        except (TypeError, ValueError):
            return "—"

    def _trunc(s: str, length: int = 55) -> str:
        if not s:
            return ""
        s = s.strip()
        return s if len(s) <= length else s[:length - 1] + "…"

    t = Table(show_lines=False, box=None, padding=(0, 1))
    t.add_column("ID", style="bold cyan", no_wrap=True)
    t.add_column("Name", style="bold", no_wrap=False)
    t.add_column("Summary", no_wrap=False)
    t.add_column("DL", justify="right", style="dim")
    t.add_column("[green]✓[/green]", justify="right")
    t.add_column("Inst.", justify="center", no_wrap=True)

    for n in nodes:
        mid = n.get("modId") or 0
        inst_mark = "[green]✓[/green]" if mid in installed_ids else ""
        t.add_row(
            str(mid or ""),
            n.get("name") or "",
            _trunc(n.get("summary") or ""),
            _fmt_num(n.get("downloads")),
            _fmt_num(n.get("endorsements")),
            inst_mark,
        )

    console.print(t)
    console.print(f"\n[dim]Install: nexmod install {game} <ID>[/dim]")


# setup ───────────────────────────────────────────────────────────────────────

@cli.command("setup")
@click.option("--game", default=None, help="Wizard-onboard a single game slug instead of all.")
@click.option("--reset", is_flag=True, help="Re-prompt for API key even if one is already set.")
@click.pass_context
def setup_wizard(ctx, game, reset):
    """Interactive first-run wizard: API key, game paths, then doctor check."""
    if not _is_interactive():
        console.print("[red]setup requires a terminal.[/red]")
        console.print("For scripted setup use: nexmod config set-key && nexmod path set <game> <path>")
        sys.exit(1)

    console.rule("[bold cyan]nexmod setup[/bold cyan]")

    # ── API key ───────────────────────────────────────────────────────────────
    cfg = load_config()
    existing_key = cfg.get("api_key", "")
    if existing_key and not reset:
        masked = f"{existing_key[:8]}…{existing_key[-4:]}"
        console.print(f"[green]✓[/green] API key already set ({masked}). Use --reset to change it.")
    else:
        key = click.prompt("Nexus Mods API key", hide_input=True)
        cfg["api_key"] = key.strip()
        save_config(cfg)
        console.print("[green]✓[/green] API key saved.")

    # ── Game scan ─────────────────────────────────────────────────────────────
    db = get_db()
    targets = [game] if game else list(GAMES.keys())

    console.print()
    console.rule("[bold]Game paths[/bold]")

    for slug in targets:
        info = GAMES.get(slug)
        if not info:
            console.print(f"[yellow]Unknown game slug '{slug}' — skipping.[/yellow]")
            continue

        # Already registered?
        row = db.execute("SELECT path FROM game_paths WHERE game=?", (slug,)).fetchone()
        if row:
            console.print(f"[green]✓[/green] {info['name']} — already registered at {row['path']}")
            continue

        # Try Steam auto-detect
        game_path = find_game_install(info["steam_id"])
        if game_path:
            mod_path = game_path / info["mod_subdir"]
            console.print(f"  Found [bold]{info['name']}[/bold] at {game_path}")
            if click.confirm(f"  Manage it with nexmod?", default=True):
                db.execute(
                    "INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)",
                    (slug, str(mod_path)),
                )
                db.commit()
                console.print(f"  [green]✓[/green] Registered: {slug} → {mod_path}")
                # Darktide: offer to download dtkit-patch native binary
                if slug == "darktide":
                    existing = _find_dtkit(game_path)
                    if existing and existing.suffix != ".exe":
                        console.print(f"  [green]✓[/green] dtkit-patch (native) already present.")
                    elif existing:
                        console.print(
                            f"  [yellow]dtkit-patch.exe found — native Linux binary preferred.[/yellow]"
                        )
                        if click.confirm("  Download native Linux binary now?", default=True):
                            try:
                                _download_dtkit(game_path)
                            except RuntimeError as e:
                                console.print(f"  [red]Download failed:[/red] {e}")
                    else:
                        console.print("  dtkit-patch not found (needed for enable/disable).")
                        if click.confirm("  Download native Linux binary now?", default=True):
                            try:
                                _download_dtkit(game_path)
                            except RuntimeError as e:
                                console.print(f"  [red]Download failed:[/red] {e}")
                        else:
                            console.print(
                                "  [dim]Skipped. Run later: nexmod setup --game darktide[/dim]"
                            )
            else:
                console.print(f"  [dim]Skipped {slug}.[/dim]")
        else:
            console.print(f"  [yellow]Could not find {info['name']} Steam install.[/yellow]")
            entered = click.prompt(
                f"  Enter mod directory path manually, or press Enter to skip",
                default="",
                show_default=False,
            )
            if not entered.strip():
                console.print(f"  [dim]Skipped {slug}.[/dim]")
                continue
            path = Path(entered.strip()).expanduser().resolve()
            if not path.exists():
                console.print(f"  [yellow]⚠ Path does not exist: {path}[/yellow]")
                if not click.confirm("  Register it anyway?", default=False):
                    console.print(f"  [dim]Skipped {slug}.[/dim]")
                    continue
            db.execute(
                "INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)",
                (slug, str(path)),
            )
            db.commit()
            console.print(f"  [green]✓[/green] Registered: {slug} → {path}")
            # Darktide: offer dtkit download for manually-entered paths too
            if slug == "darktide":
                existing = _find_dtkit(path.parent)
                if not (existing and existing.suffix != ".exe"):
                    if click.confirm("  Download native dtkit-patch binary now?", default=True):
                        try:
                            _download_dtkit(path.parent)
                        except RuntimeError as e:
                            console.print(f"  [red]Download failed:[/red] {e}")

    # ── Doctor check ──────────────────────────────────────────────────────────
    console.print()
    console.rule("[bold]Pre-flight check[/bold]")
    ctx.invoke(doctor)


# doctor ──────────────────────────────────────────────────────────────────────

@cli.command("doctor")
@click.option("--game", help="Limit game-install checks to this slug.")
def doctor(game):
    """Pre-flight environment check.

    Verifies the things that cause cryptic errors later: API key + Premium
    status, Steam library detection, per-game install paths, dtkit-patch + 7z
    binaries, disk space, and config/data directory writability.

    Exit code 0 when everything is OK, 1 when at least one check fails.
    """
    ok = True
    warnings = 0

    def status(label: str, passed: bool, detail: str = "", warn: bool = False) -> None:
        nonlocal ok, warnings
        if passed:
            mark = "[green]✓[/green]"
        elif warn:
            mark = "[yellow]![/yellow]"
            warnings += 1
        else:
            mark = "[red]✗[/red]"
            ok = False
        msg = f"  {mark} {label}"
        if detail:
            msg += f" — [dim]{detail}[/dim]"
        console.print(msg)

    console.print("[bold]nexmod doctor[/bold] — environment pre-flight\n")

    # ── Config + data directories ────────────────────────────────────────────
    console.print("[bold]Filesystem[/bold]")
    cfg_writable = os.access(CONFIG_DIR.parent, os.W_OK)
    status(f"Config dir writable ({CONFIG_DIR})", cfg_writable)
    data_writable = os.access(DATA_DIR.parent, os.W_OK)
    status(f"Data dir writable ({DATA_DIR})", data_writable)
    try:
        usage = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else DATA_DIR.parent)
        free_gb = usage.free / (1024 ** 3)
        status(f"Disk space at data dir", free_gb >= 1.0,
               f"{free_gb:.1f} GB free", warn=(0.1 < free_gb < 1.0))
    except OSError as e:
        status("Disk space at data dir", False, str(e))

    # ── API key + Premium ────────────────────────────────────────────────────
    console.print("\n[bold]Nexus API[/bold]")
    cfg = load_config()
    has_key = bool(cfg.get("api_key"))
    status("API key configured", has_key,
           "" if has_key else "run: nexmod config set-key <key>")
    if has_key:
        try:
            user = nexus_get("users/validate.json", cfg["api_key"])
            name = user.get("name") or user.get("username") or "?"
            status(f"API key valid", True, f"user: {name}")
            premium = user.get("is_premium") or user.get("isPremium")
            status("Premium account", bool(premium),
                   "" if premium else "Premium required for downloads")
        except SystemExit:
            status("API key valid", False, "validate.json failed")
        except Exception as e:
            status("API key valid", False, str(e))

    # ── External binaries ────────────────────────────────────────────────────
    console.print("\n[bold]External tools[/bold]")
    has_7z = bool(shutil.which("7z") or shutil.which("7zz") or shutil.which("7za"))
    status("7z (for .7z archives)", has_7z,
           "" if has_7z else "install p7zip-full", warn=not has_7z)

    # ── Steam libraries ──────────────────────────────────────────────────────
    console.print("\n[bold]Steam[/bold]")
    libs = find_steam_library_paths()
    status(f"Steam library detected", bool(libs),
           f"{len(libs)} library path(s)" if libs else "no libraries found")
    for lib in libs:
        console.print(f"    [dim]{lib}[/dim]")

    # ── Per-game installs ────────────────────────────────────────────────────
    console.print("\n[bold]Game installs[/bold]")
    targets = [game] if game else list(GAMES.keys())
    for slug in targets:
        info = GAMES.get(slug)
        if not info:
            status(slug, False, "unknown game slug")
            continue
        path = find_game_install(info["steam_id"])
        if path:
            mod_subdir = path / info["mod_subdir"]
            status(f"{slug}: install found", True, str(path))
            status(f"{slug}: mod dir exists", mod_subdir.exists(),
                   str(mod_subdir),
                   warn=not mod_subdir.exists())
            # Darktide: check for dtkit-patch (native preferred; Wine .exe fallback)
            if slug == "darktide":
                _dtkit_check = _find_dtkit(path)
                if _dtkit_check and _dtkit_check.suffix != ".exe":
                    status(f"{slug}: dtkit-patch (native)", True, str(_dtkit_check))
                elif _dtkit_check:
                    has_wine = bool(shutil.which("wine"))
                    status(f"{slug}: dtkit-patch (Wine .exe)", has_wine,
                           str(_dtkit_check) if has_wine else
                           "replace with native binary — run: nexmod setup --game darktide",
                           warn=not has_wine)
                else:
                    status(f"{slug}: dtkit-patch", False,
                           "run: nexmod setup --game darktide  to auto-download",
                           warn=True)
            # Starfield: check SFSE + Plugins.txt
            if slug == "starfield":
                sfse_loader = path / "sfse_loader.exe"
                sfse_dll    = path / "sfse_steam_loader.dll"
                sfse_ok = sfse_loader.exists() and sfse_dll.exists()
                status(f"{slug}: SFSE installed",
                       sfse_ok,
                       str(sfse_loader) if sfse_ok else
                       "run: nexmod sfse-install starfield",
                       warn=not sfse_ok)
                plugins_txt = find_plugins_txt(slug)
                if plugins_txt:
                    status(f"{slug}: plugins.txt exists",
                           plugins_txt.exists(),
                           str(plugins_txt) if plugins_txt.exists() else
                           "launch the game once to create it",
                           warn=not plugins_txt.exists())
                else:
                    status(f"{slug}: Proton prefix found", False,
                           "launch the game once to create the prefix", warn=True)
        else:
            status(f"{slug}: install found", False,
                   "not installed via Steam (or different library)", warn=True)

    # ── DB integrity ─────────────────────────────────────────────────────────
    console.print("\n[bold]Database[/bold]")
    try:
        db = get_db()
        n_mods = db.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
        n_null = db.execute(
            "SELECT COUNT(*) FROM mods WHERE folder_name IS NULL"
        ).fetchone()[0]
        status("DB readable", True, f"{n_mods} tracked mods")
        status("All folder_name backfilled", n_null == 0,
               f"{n_null} legacy rows — run: nexmod fsck --fix" if n_null else "",
               warn=(n_null > 0))
    except Exception as e:
        status("DB readable", False, str(e))

    console.print()
    if ok:
        if warnings:
            console.print(f"[green]All required checks passed[/green] "
                          f"[yellow]({warnings} warning{'s' if warnings != 1 else ''})[/yellow]")
        else:
            console.print("[green]All checks passed.[/green]")
        console.print(
            "[dim]→ Next: nexmod install <game> <mod_id>  "
            "or  nexmod install <nexus-url>[/dim]"
        )
        sys.exit(0)
    else:
        console.print("[red]One or more checks failed. Address above before running install/update.[/red]")
        sys.exit(1)


# scan ────────────────────────────────────────────────────────────────────────

@cli.command("scan")
@click.argument("game")
@click.option("--dry-run", is_flag=True, help="Show what would be tracked without doing it")
def scan_vortex(game, dry_run):
    """Import all mods from a Vortex deployment manifest (no re-download needed)."""
    api_key = get_api_key()
    db      = get_db()
    info    = GAMES.get(game)
    domain  = info["domain"] if info else game
    mod_dir  = resolve_mod_dir(game, db)
    game_dir = mod_dir.parent
    found    = parse_vortex_manifest(game_dir)

    if not found:
        console.print(f"[yellow]No vortex.deployment.json found in {game_dir}[/yellow]")
        return

    already = {r["mod_id"] for r in db.execute("SELECT mod_id FROM mods WHERE game=?", (game,)).fetchall()}
    to_add  = {mid: v for mid, v in found.items() if mid not in already}

    console.print(f"Found [cyan]{len(found)}[/cyan] mods in manifest, "
                  f"[green]{len(already)}[/green] already tracked, "
                  f"[yellow]{len(to_add)}[/yellow] new.")

    if dry_run:
        for mid, (name, folder) in sorted(to_add.items()):
            console.print(f"  would track: [{mid}] {name}  ({folder})")
        return

    ok = fail = 0
    for mod_id, (vortex_name, folder) in sorted(to_add.items()):
        try:
            with console.status(f"  [{mod_id}] {vortex_name}..."):
                mod   = api_mod_info(domain, mod_id, api_key)
                files = api_mod_files(domain, mod_id, api_key)
            chosen = pick_main_file(files)
            # Store version=None intentionally: the files on disk came from Vortex/Windows
            # and may be older than the current Nexus version. Storing the Nexus version
            # would make 'update' say "current" when the actual installed files are outdated.
            # With version=None, 'update' will download and install the current version.
            db.execute("""
                INSERT OR IGNORE INTO mods
                    (game, mod_id, file_id, name, version, filename, mod_dir,
                     folder_name, tracked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                game, mod_id,
                chosen["file_id"] if chosen else 0,
                mod["name"], None,
                chosen["file_name"] if chosen else None,
                str(mod_dir), (folder or None), now_iso(),
            ))
            db.commit()
            record(db, "scan", game, mod_id, mod["name"], None, "ok",
                   f"nexus_version={mod.get('version','?')} (disk version unknown until update)")
            console.print(f"  [green]✓[/green] [{mod_id}] {mod['name']} (nexus: v{mod.get('version','?')} — run update to install)")
            ok += 1
        except Exception as e:
            record(db, "scan", game, mod_id, vortex_name, None, "fail", str(e))
            console.print(f"  [red]✗[/red] [{mod_id}] {vortex_name}: {e}")
            fail += 1

    console.print(f"\n[bold]Done.[/bold] {ok} tracked, {fail} failed.")

    # Reconcile load order: discovered Vortex folders are now both in DB
    # (we just inserted) and on disk (Vortex left them) — reconciler will
    # add any that aren't yet listed.
    info_cur = GAMES.get(game) or {}
    if info_cur.get("load_order_file") and ok > 0:
        try:
            result = reconcile_load_order(game, db, mod_dir)
            if result["written"]:
                added = result["missing_added"]
                if added:
                    console.print(f"[dim]Load order ← {len(added)} new entr"
                                  f"{'y' if len(added) == 1 else 'ies'}.[/dim]")
            elif result["drift_detected"]:
                console.print("[yellow]Load order: external edit detected — not modified.[/yellow]")
        except Exception as e:
            log.warning("reconcile after scan failed: %s", e)


# install ─────────────────────────────────────────────────────────────────────

@cli.command("install")
@click.argument("game")
@click.argument("mod_id", type=int, required=False, default=None)
@click.option("--file-id", type=int, default=None, help="Force a specific file ID")
@click.option("--no-reorder", is_flag=True, help="Skip automatic load order sort after install")
@click.option("--dry-run", is_flag=True,
              help="Resolve mod metadata + show what would be downloaded; do not fetch or extract.")
@click.option("--from-file", "from_file", type=click.Path(path_type=Path), default=None,
              help="Install from a local archive instead of downloading (free accounts, manual downloads).")
def install(game, mod_id, file_id, no_reorder, dry_run, from_file):
    """Download and install a mod. Starts tracking it for updates.

    \b
    Accepts a Nexus Mods URL or a game slug + mod ID:
      nexmod install https://www.nexusmods.com/warhammer40kdarktide/mods/1234
      nexmod install darktide 1234
      nexmod install darktide 1234 --from-file ~/Downloads/mymod-1.2.zip
    """
    if "nexusmods.com" in game or game.startswith("http"):
        parsed_game, parsed_mod_id, parsed_file_id = parse_nexus_url(game)
        game   = parsed_game
        mod_id = parsed_mod_id
        if parsed_file_id and file_id is None:
            file_id = parsed_file_id
        console.print(f"  [dim]Parsed URL → game=[cyan]{game}[/cyan]  mod_id=[cyan]{mod_id}[/cyan]"
                      + (f"  file_id=[cyan]{file_id}[/cyan]" if file_id else "") + "[/dim]")
    elif mod_id is None:
        console.print("[red]Usage:  nexmod install <game> <mod_id>[/red]")
        console.print("        nexmod install <nexus-url>")
        sys.exit(1)

    api_key = get_api_key()
    db = get_db()

    if dry_run:
        info_g = GAMES.get(game, {})
        domain = info_g.get("domain", game)
        mod = api_mod_info(domain, mod_id, api_key)
        files = api_mod_files(domain, mod_id, api_key)
        chosen = next((f for f in files if f["file_id"] == file_id), None) if file_id else pick_main_file(files)
        if not chosen:
            console.print("[red](dry-run) Could not determine main file.[/red]")
            sys.exit(1)
        target_dir = resolve_mod_dir(game, db)
        console.print(f"\n[bold](dry-run) would install:[/bold]")
        console.print(f"  Mod:        [cyan]{mod['name']}[/cyan] v{mod.get('version', '?')}")
        console.print(f"  File:       {chosen['file_name']} ({chosen.get('size_kb', '?')} KB, "
                      f"category={chosen.get('category_name')})")
        console.print(f"  Target dir: {target_dir}")
        # Darktide: warn if dtkit-patch is missing (needed for enable/disable)
        if game == "darktide":
            _dtkit_hint = _find_dtkit(resolve_mod_dir(game, db).parent)
            if not _dtkit_hint:
                console.print(
                    "  [yellow]⚠ dtkit-patch not found — run: nexmod setup --game darktide "
                    "to download it (needed for enable/disable later).[/yellow]"
                )
        return

    # Darktide: hint if dtkit-patch is absent (install proceeds regardless)
    if game == "darktide":
        _dtkit_hint = _find_dtkit(resolve_mod_dir(game, db).parent)
        if not _dtkit_hint:
            console.print(
                "[yellow]⚠ dtkit-patch not found.[/yellow] Mods will install fine, but "
                "you'll need it to run [cyan]nexmod enable darktide[/cyan]. "
                "Download it with: [cyan]nexmod setup --game darktide[/cyan]"
            )

    name, version = do_install(game, mod_id, file_id, api_key, db, from_file=from_file)
    console.print(f"\n[green]✓ Installed:[/green] {name} v{version}")

    info = GAMES.get(game)
    if info and info.get("load_order_file") and not no_reorder and get_auto_reorder():
        mod_dir = resolve_mod_dir(game, db)
        # do_install already reconciled. Re-run to surface missing deps from the
        # post-install state and to handle any that need user prompting.
        result = reconcile_load_order(game, db, mod_dir, dry_run=True)
        if result["cycles"]:
            console.print(f"  [yellow]Dependency cycle detected: {', '.join(result['cycles'])}[/yellow]")
        if result["missing_deps"]:
            newly = _handle_missing_deps(game, result["missing_deps"], mod_dir, api_key, db, yes=False)
            if newly:
                # Fresh reconcile after dep installs (do_install already ran reconcile
                # for each dep, but a final pass ensures topo order is canonical).
                reconcile_load_order(game, db, mod_dir)


# track ───────────────────────────────────────────────────────────────────────

@cli.command("track")
@click.argument("game")
@click.argument("mod_id", type=int, required=False, default=None)
def track(game, mod_id):
    """Track an already-installed mod so nexmod can check it for updates.

    \b
    Accepts a Nexus Mods URL or a game slug + mod ID:
      nexmod track https://www.nexusmods.com/warhammer40kdarktide/mods/1234
      nexmod track darktide 1234
    """
    if "nexusmods.com" in game or game.startswith("http"):
        game, mod_id, _ = parse_nexus_url(game)
        console.print(f"  [dim]Parsed URL → game=[cyan]{game}[/cyan]  mod_id=[cyan]{mod_id}[/cyan][/dim]")
    elif mod_id is None:
        console.print("[red]Usage:  nexmod track <game> <mod_id>[/red]")
        console.print("        nexmod track <nexus-url>")
        sys.exit(1)

    api_key = get_api_key()
    db      = get_db()
    info    = GAMES.get(game)
    domain  = info["domain"] if info else game

    with console.status(f"Fetching mod {mod_id}..."):
        mod   = api_mod_info(domain, mod_id, api_key)
        files = api_mod_files(domain, mod_id, api_key)

    chosen  = pick_main_file(files)
    mod_dir = resolve_mod_dir(game, db)

    db.execute("""
        INSERT OR IGNORE INTO mods
            (game, mod_id, file_id, name, version, filename, mod_dir, tracked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        game, mod_id,
        chosen["file_id"] if chosen else 0,
        mod["name"], mod.get("version"),
        chosen["file_name"] if chosen else None,
        str(mod_dir), now_iso(),
    ))
    db.commit()
    record(db, "track", game, mod_id, mod["name"], mod.get("version"), "ok")
    console.print(f"[green]Tracking:[/green] {mod['name']} v{mod.get('version', '?')}")


# import ──────────────────────────────────────────────────────────────────────

def _parse_nexus_filename(filename: str) -> tuple[int | None, int | None]:
    """Try to extract (mod_id, file_id) from a Nexus Mods archive filename.

    Nexus uses: <ModName>-<mod_id>-<file_id>-<version>.<ext>
    e.g. "CoolMod-1234-5678-1-2-3.zip" → mod_id=1234, file_id=5678

    Strategy: split the stem on "-", find the first two purely-numeric segments.
    These are the mod_id and file_id. The leading name portion and trailing
    version portion may also contain digits, but version segments are never the
    first numeric segment encountered after the name.

    Returns (mod_id, file_id) as ints if at least two numeric segments are found,
    (None, None) otherwise.
    """
    stem = os.path.splitext(filename)[0]
    parts = stem.split("-")
    digit_parts = [p for p in parts if p.isdigit()]
    if len(digit_parts) >= 2:
        return int(digit_parts[0]), int(digit_parts[1])
    return None, None


@cli.command("import")
@click.argument("game")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--mod-id", "mod_id_opt", type=int, default=None,
              help="Nexus mod ID (skip detection prompt).")
@click.option("--yes", "-y", is_flag=True,
              help="Accept auto-detected mod ID without prompting.")
@click.option("--no-reorder", is_flag=True,
              help="Skip automatic load order reconcile after install.")
def import_archive(game, path, mod_id_opt, yes, no_reorder):
    """Install a locally-downloaded archive (free-tier workflow).

    \b
    Free Nexus users cannot use the API download endpoint. Download the archive
    manually from nexusmods.com, then run:

      nexmod import darktide /path/to/ModName-1234-1-2-3.zip

    nexmod will try to detect the mod ID from the filename, fetch metadata from
    the Nexus API (free, no Premium required), extract the archive, and register
    the mod in the local database.
    """
    archive = Path(path).resolve()
    api_key = get_api_key()
    db      = get_db()
    info    = GAMES.get(game)
    domain  = info["domain"] if info else game

    # ── Determine mod_id ─────────────────────────────────────────────────────
    if mod_id_opt is not None:
        mod_id = mod_id_opt
        console.print(f"  Using provided mod ID: [cyan]{mod_id}[/cyan]")
    else:
        detected_id, detected_file_id = _parse_nexus_filename(archive.name)
        if detected_id is not None:
            if yes or click.confirm(
                f"  Detected mod ID [cyan]{detected_id}[/cyan] from filename — is that right?",
                default=True,
            ):
                mod_id = detected_id
            else:
                mod_id = click.prompt("  Enter the Nexus mod ID for this file", type=int)
        else:
            console.print(
                f"  [yellow]Could not detect mod ID from filename:[/yellow] [dim]{archive.name}[/dim]"
            )
            mod_id = click.prompt("  Enter the Nexus mod ID for this file", type=int)

    console.print(
        f"[bold]Importing:[/bold] [dim]{archive.name}[/dim] → [cyan]{game}[/cyan] mod [cyan]{mod_id}[/cyan]"
    )

    # ── Fetch mod metadata (free endpoint) ───────────────────────────────────
    with console.status(f"Fetching mod {mod_id} info from Nexus..."):
        try:
            mod = api_mod_info(domain, mod_id, api_key)
        except SystemExit:
            console.print(f"[red]Could not fetch mod info for mod {mod_id}.[/red]")
            sys.exit(1)

    console.print(
        f"  [bold]{mod['name']}[/bold] by {mod.get('author', '?')} — v{mod.get('version', '?')}"
    )

    # ── Fetch file list to get file_id ────────────────────────────────────────
    # If we detected a file_id from the filename, prefer it; otherwise pick the
    # main file from the API list for the file_id record.
    detected_id, detected_file_id = _parse_nexus_filename(archive.name)
    file_id_for_db = detected_file_id

    with console.status("Fetching file list..."):
        try:
            files = api_mod_files(domain, mod_id, api_key)
        except SystemExit:
            files = []

    if file_id_for_db is None and files:
        chosen_file = pick_main_file(files)
        file_id_for_db = chosen_file["file_id"] if chosen_file else 0
    elif file_id_for_db is None:
        file_id_for_db = 0

    # ── Extract archive ───────────────────────────────────────────────────────
    mod_dir = resolve_mod_dir(game, db)
    mod_dir.mkdir(parents=True, exist_ok=True)

    size_bytes = archive.stat().st_size
    try:
        if mod_dir.exists():
            _check_disk_space(mod_dir, size_bytes * 3, "extraction")
    except RuntimeError as e:
        record(db, "install", game, mod_id, mod["name"], mod.get("version"), "fail", str(e))
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    dirs_before = {p.name for p in mod_dir.iterdir() if p.is_dir()} if mod_dir.exists() else set()
    plugin_exts = (info or {}).get("plugin_exts", ())
    plugins_before = _get_disk_plugins(mod_dir, plugin_exts) if (mod_dir.exists() and plugin_exts) else set()

    top_dirs = _archive_top_level_dirs(archive)
    conflicts = _detect_install_conflicts(db, game, mod_id, top_dirs)
    if conflicts:
        console.print(
            f"\n[yellow]⚠ Archive contains folders already claimed by other tracked mods:[/yellow]"
        )
        for folder, other_name, other_id in conflicts:
            console.print(
                f"  [yellow]{folder}/[/yellow] is owned by [cyan]{other_name}[/cyan] (mod {other_id})"
            )
        console.print("  Continuing will overwrite those mods' files.")
        if not click.confirm("Continue?", default=False):
            console.print("[dim]Aborted.[/dim]")
            sys.exit(0)

    console.print(f"  Extracting to [dim]{mod_dir}[/dim]...")
    try:
        extract_archive(archive, mod_dir)
    except Exception as e:
        record(db, "install", game, mod_id, mod["name"], mod.get("version"), "fail", str(e))
        console.print(f"[red]Extraction failed: {e}[/red]")
        sys.exit(1)

    # ── Snapshot for rollback ─────────────────────────────────────────────────
    try:
        _save_snapshot(game, mod_id, mod.get("version"), archive)
    except Exception as e:
        log.warning("snapshot save failed for imported archive: %s", e)

    # ── DB record ─────────────────────────────────────────────────────────────
    new_dirs = sorted({p.name for p in mod_dir.iterdir() if p.is_dir()} - dirs_before)
    if len(new_dirs) > 1:
        console.print(
            f"  [yellow]Multi-folder install:[/yellow] {len(new_dirs)} new folders — "
            f"primary tracked as [cyan]{new_dirs[0]}[/cyan]; siblings: "
            f"{', '.join(new_dirs[1:])}"
        )
    folder_name = new_dirs[0] if new_dirs else None

    db.execute("""
        INSERT INTO mods
            (game, mod_id, file_id, name, version, filename, mod_dir,
             folder_name, tracked_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game, mod_id) DO UPDATE SET
            file_id     = excluded.file_id,
            name        = excluded.name,
            version     = excluded.version,
            filename    = excluded.filename,
            mod_dir     = excluded.mod_dir,
            folder_name = COALESCE(excluded.folder_name, mods.folder_name),
            updated_at  = excluded.updated_at
    """, (
        game, mod_id, file_id_for_db, mod["name"], mod.get("version"),
        archive.name, str(mod_dir), folder_name, now_iso(), now_iso(),
    ))
    db.commit()
    record(db, "install", game, mod_id, mod["name"], mod.get("version"), "ok")

    # ── Load order / plugins ──────────────────────────────────────────────────
    load_order_file = (info or {}).get("load_order_file")
    if load_order_file and not no_reorder and get_auto_reorder():
        try:
            result_lo = reconcile_load_order(game, db, mod_dir)
            if new_dirs and result_lo["written"]:
                console.print(f"  [dim]mod_load_order.txt ← {', '.join(new_dirs)}[/dim]")
            if result_lo.get("cycles"):
                console.print(
                    f"  [yellow]⚠ Dependency cycle in load order: "
                    f"{', '.join(result_lo['cycles'])}.[/yellow]"
                )
        except Exception as e:
            log.warning("reconcile after import failed: %s", e)
            console.print(f"  [yellow]Load order reconcile failed: {e}[/yellow]")

    if plugin_exts:
        new_plugins = sorted(_get_disk_plugins(mod_dir, plugin_exts) - plugins_before)
        official_lower = {m.lower() for m in (info or {}).get("official_masters", ())}
        tracked_plugins = [p for p in new_plugins if p.lower() not in official_lower]
        if tracked_plugins:
            for pname in tracked_plugins:
                db.execute(
                    "INSERT INTO plugin_files (game, mod_id, plugin_name, enabled, added_at) "
                    "VALUES (?, ?, ?, 1, ?) "
                    "ON CONFLICT(game, plugin_name) DO UPDATE SET "
                    "mod_id = excluded.mod_id, added_at = excluded.added_at",
                    (game, mod_id, pname, now_iso()),
                )
            db.commit()
            try:
                reconcile_plugins_txt(game, db, mod_dir)
            except Exception as e:
                log.warning("plugins.txt reconcile after import failed: %s", e)

    log.info("import ok: game=%s mod_id=%s name=%r version=%s",
             game, mod_id, mod["name"], mod.get("version"))
    console.print(
        f"\n[green]✓ Imported:[/green] {mod['name']} v{mod.get('version', '?')}"
    )


# list ────────────────────────────────────────────────────────────────────────

@cli.command("list")
@click.argument("game")
@click.option("--json", "as_json", is_flag=True,
              help="Emit machine-readable JSON instead of a rich table.")
def list_mods(game, as_json):
    """List tracked mods for GAME."""
    db   = get_db()
    rows = db.execute("SELECT * FROM mods WHERE game = ? ORDER BY name", (game,)).fetchall()

    if as_json:
        # Use stdlib print so JSON isn't decorated by Rich; scripts can pipe to jq.
        print(json.dumps([dict(r) for r in rows]))
        return

    if not rows:
        console.print(f"[yellow]No mods tracked for '{game}'.[/yellow]")
        return
    t = Table(title=f"Tracked mods — {game}", show_lines=False)
    t.add_column("Mod ID", style="dim", justify="right")
    t.add_column("Name")
    t.add_column("Version", style="cyan")
    t.add_column("Last updated", style="dim")
    for r in rows:
        t.add_row(
            str(r["mod_id"]), r["name"],
            r["version"] or "?",
            (r["updated_at"] or r["tracked_at"] or "?")[:10],
        )
    console.print(t)


# info ────────────────────────────────────────────────────────────────────────

@cli.command("info")
@click.argument("game")
@click.argument("mod_id", type=int)
@click.option("--remote", is_flag=True,
              help="Fetch info from Nexus without requiring the mod to be tracked locally.")
def show_mod_info(game, mod_id, remote):
    """Show detailed info for a mod (local DB + one Nexus lookup).

    By default the mod must be tracked locally. Pass --remote to look up any
    mod by ID directly from Nexus without installing or tracking it first.

    \b
    Examples:
      nexmod info darktide 1234
      nexmod info darktide 1234 --remote
    """
    info_game = GAMES.get(game, {})
    domain    = info_game.get("domain", game)

    if remote:
        # --remote path: API key required upfront; skip DB entirely.
        api_key = get_api_key()
        with console.status(f"Fetching mod {mod_id} from Nexus..."):
            try:
                upstream = api_mod_info(domain, mod_id, api_key)
            except Exception as e:
                log.error("info --remote: api_mod_info failed for %s/%s: %s", game, mod_id, e)
                console.print(f"[red]Could not fetch mod {mod_id}: {e}[/red]")
                sys.exit(1)

        t = Table(title=f"Mod info (remote) — {upstream.get('name', str(mod_id))}",
                  show_lines=False, box=None, padding=(0, 1))
        t.add_column("Field", style="dim")
        t.add_column("Value")

        t.add_row("Game",    game)
        t.add_row("Mod ID",  str(mod_id))
        t.add_row("Name",    upstream.get("name") or "—")
        if upstream.get("author"):
            t.add_row("Author", upstream["author"])
        if upstream.get("version"):
            t.add_row("Latest", upstream["version"])
        if upstream.get("summary"):
            t.add_row("Summary", upstream["summary"])
        t.add_row("Status", "not tracked locally")
        t.add_row("Install", f"nexmod install {game} {mod_id}")

        console.print(t)
        return

    db  = get_db()
    row = db.execute(
        "SELECT * FROM mods WHERE game=? AND mod_id=?", (game, mod_id)
    ).fetchone()
    if not row:
        console.print(f"[red]Mod {mod_id} is not tracked for '{game}'.[/red]")
        console.print(
            f"[dim]Run 'nexmod track {game} {mod_id}' first, "
            f"or use 'nexmod info {game} {mod_id} --remote' to look up without tracking.[/dim]"
        )
        sys.exit(1)

    # API key needed only once we know the mod is tracked.
    api_key = get_api_key()

    with console.status(f"Fetching {row['name']} from Nexus..."):
        try:
            upstream = api_mod_info(domain, mod_id, api_key)
        except Exception as e:
            log.error("info: api_mod_info failed for %s/%s: %s", game, mod_id, e)
            upstream = {}
            console.print(f"[yellow]Could not fetch upstream info: {e}[/yellow]")

    folder_name = _archive_basename(row["filename"]) if row["filename"] else None

    t = Table(title=f"Mod info — {row['name']}", show_lines=False, box=None, padding=(0, 1))
    t.add_column("Field", style="dim")
    t.add_column("Value")

    t.add_row("Game",          game)
    t.add_row("Mod ID",        str(row["mod_id"]))
    t.add_row("Name",          row["name"])
    if upstream.get("author"):
        t.add_row("Author",    upstream["author"])
    t.add_row("Installed",     row["version"] or "—")
    if upstream.get("version"):
        latest = upstream["version"]
        if row["version"] and row["version"] != latest:
            t.add_row("Latest", f"[yellow]{latest}  (update available)[/yellow]")
        else:
            t.add_row("Latest", latest)
    t.add_row("File ID",       str(row["file_id"]) if row["file_id"] else "—")
    t.add_row("Filename",      row["filename"] or "—")
    t.add_row("Folder",        folder_name or "—")
    t.add_row("Mod dir",       row["mod_dir"] or "—")
    t.add_row("Tracked at",    (row["tracked_at"] or "")[:19].replace("T", " "))
    t.add_row("Last updated",  (row["updated_at"] or "—")[:19].replace("T", " "))

    console.print(t)

    if folder_name and row["mod_dir"]:
        deps = _parse_mod_deps(Path(row["mod_dir"]), folder_name)
        if deps:
            console.print(f"\n[bold]Declared dependencies:[/bold] {', '.join(deps)}")


# check ───────────────────────────────────────────────────────────────────────

@cli.command("check")
@click.argument("game")
@click.option("--json", "output_json", is_flag=True,
              help="Emit machine-readable JSON instead of a table.")
def check_updates(game, output_json):
    """Check all tracked mods for available updates (no download)."""
    from contextlib import nullcontext
    api_key = get_api_key()
    db      = get_db()
    rows    = db.execute("SELECT * FROM mods WHERE game = ?", (game,)).fetchall()
    if not rows:
        if output_json:
            click.echo("[]")
        else:
            console.print(f"[yellow]No mods tracked for '{game}'.[/yellow]")
        return

    info   = GAMES.get(game, {})
    domain = info.get("domain", game)

    if not output_json:
        t = Table(title=f"Update check — {game}", show_lines=False)
        t.add_column("Name")
        t.add_column("Installed", style="dim")
        t.add_column("Latest", style="cyan")
        t.add_column("Status")
    results = []

    for r in rows:
        ctx = nullcontext() if output_json else console.status(f"Checking {r['name']}...")
        try:
            with ctx:
                mod    = api_mod_info(domain, r["mod_id"], api_key)
            latest = mod.get("version", "?")
            cur    = r["version"] or "?"
            _versions_match = _norm_version(latest) == _norm_version(cur)
            if output_json:
                results.append({
                    "game": game, "mod_id": r["mod_id"], "name": r["name"],
                    "installed": cur, "latest": latest,
                    "update_available": not _versions_match and latest != "?",
                    "error": None,
                })
            else:
                label = "[green]Current[/green]" if _versions_match else f"[yellow]Update → {latest}[/yellow]"
                t.add_row(r["name"], cur, latest, label)
        except Exception as e:
            log.error("Check failed for mod_id=%s: %s", r["mod_id"], e)
            if output_json:
                results.append({
                    "game": game, "mod_id": r["mod_id"], "name": r["name"],
                    "installed": r["version"] or "?", "latest": None,
                    "update_available": None, "error": str(e),
                })
            else:
                t.add_row(r["name"], r["version"] or "?", "?", f"[red]error: {e}[/red]")

    if output_json:
        click.echo(json.dumps(results, indent=2))
    else:
        console.print(t)


# order ───────────────────────────────────────────────────────────────────────

@cli.command("order")
@click.argument("game")
@click.option("--dry-run", is_flag=True, help="Preview new order without writing to disk.")
@click.option("--check", is_flag=True, help="Print classification table + diff, do not write.")
@click.option("--fsck", is_flag=True, help="Restore mod_load_order.txt from .bak if present.")
@click.option("--freeze", is_flag=True, help="Add the freeze directive (nexmod will refuse to modify the file).")
@click.option("--unfreeze", is_flag=True, help="Remove the freeze directive.")
@click.option("--adopt", is_flag=True, help="Promote foreign entries: prompt for mod_id per untracked folder.")
@click.option("--auto-merge", is_flag=True, help="Proceed even if external edit drift is detected.")
def order_mods(game, dry_run, check, fsck, freeze, unfreeze, adopt, auto_merge):
    """Show, sort, and manage the load order file.

    Reconciles mod_load_order.txt against the DB, on-disk folders, pins, and
    in-file directives. Drops orphaned entries, adds discovered framework
    folders, preserves foreign (untracked) entries, topo-sorts the rest by
    declared dependencies. Atomic write with .bak retained.

    \b
    Examples:
      nexmod order darktide              # reconcile and write
      nexmod order darktide --dry-run    # preview the proposed order
      nexmod order darktide --check      # detailed classification + diff
      nexmod order darktide --freeze     # disable nexmod auto-mutation
      nexmod order darktide --adopt      # promote foreign entries to tracked
      nexmod order darktide --fsck       # restore from .bak
    """
    info = GAMES.get(game)
    if not info or not info.get("load_order_file"):
        console.print(f"[yellow]{game} does not use a managed load order file.[/yellow]")
        return

    db      = get_db()
    mod_dir = resolve_mod_dir(game, db)
    lof_filename = info["load_order_file"]
    lof     = mod_dir / lof_filename

    # ── --fsck: restore from .bak ────────────────────────────────────────────
    if fsck:
        bak = lof.with_suffix(lof.suffix + ".bak")
        if not bak.exists():
            console.print(f"[red]No backup at {bak}.[/red]")
            sys.exit(1)
        if lof.exists() and not click.confirm(
            f"Restore {lof.name} from {bak.name}? Current file will be overwritten.",
            default=False,
        ):
            console.print("[dim]Aborted.[/dim]")
            return
        _atomic_write_with_backup(lof, bak.read_text())
        # Reset hash so reconciler sees the restored state as canonical.
        new_hash = _hash_text(bak.read_text())
        db.execute("""
            INSERT INTO load_order_state (game, file_path, last_hash, last_written_at, frozen)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(game) DO UPDATE SET
                last_hash = excluded.last_hash,
                last_written_at = excluded.last_written_at
        """, (game, str(lof), new_hash, now_iso()))
        db.commit()
        console.print(f"[green]✓ Restored {lof.name} from backup.[/green]")
        return

    # ── --freeze / --unfreeze: directive toggle ──────────────────────────────
    if freeze or unfreeze:
        if freeze and unfreeze:
            console.print("[red]--freeze and --unfreeze are mutually exclusive.[/red]")
            sys.exit(1)
        if not lof.exists():
            console.print(f"[yellow]No {lof_filename} found in {mod_dir}[/yellow]")
            return
        text  = lof.read_text()
        lines = text.splitlines()
        directive = "-- nexmod:freeze"
        already   = any(_DIRECTIVE_RE.match(l) and l.strip().lower() == directive.lower()
                        for l in lines)
        if freeze and already:
            console.print("[dim]Already frozen.[/dim]")
            return
        if unfreeze and not already:
            console.print("[dim]Not frozen.[/dim]")
            return
        if freeze:
            new_lines = [BASE_HEADER, directive] + [l for l in lines if l.strip() != BASE_HEADER]
            new_text  = "\n".join(new_lines) + "\n"
        else:
            new_lines = [
                l for l in lines
                if not (_DIRECTIVE_RE.match(l) and l.strip().lower() == directive.lower())
            ]
            new_text  = "\n".join(new_lines) + ("\n" if new_lines and not new_lines[-1].endswith("\n") else "")
        _atomic_write_with_backup(lof, new_text)
        new_hash = _hash_text(new_text)
        db.execute("""
            INSERT INTO load_order_state (game, file_path, last_hash, last_written_at, frozen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(game) DO UPDATE SET
                last_hash = excluded.last_hash,
                last_written_at = excluded.last_written_at,
                frozen = excluded.frozen
        """, (game, str(lof), new_hash, now_iso(), 1 if freeze else 0))
        db.commit()
        console.print(f"[green]✓ Load order {'frozen' if freeze else 'unfrozen'}.[/green]")
        return

    # ── --adopt: promote foreign entries to tracked ─────────────────────────
    if adopt:
        if not lof.exists():
            console.print(f"[yellow]No {lof_filename} found in {mod_dir}[/yellow]")
            return
        api_key = get_api_key()
        result  = reconcile_load_order(game, db, mod_dir, dry_run=True)
        foreign = [f for f, c in result["classification"].items() if c == "foreign"]
        if not foreign:
            console.print("[dim]No foreign entries to adopt.[/dim]")
            return
        console.print(f"Found [yellow]{len(foreign)}[/yellow] foreign entry(ies). "
                      "Paste a Nexus URL for each (Enter to skip).")
        adopted = 0
        for folder in foreign:
            url = click.prompt(f"  {folder}", default="", show_default=False).strip()
            if not url:
                continue
            try:
                a_game, a_mod_id, a_file_id = parse_nexus_url(url)
                if a_game != game:
                    console.print(f"  [yellow]URL is for '{a_game}' — using '{game}' instead.[/yellow]")
                do_install(game, a_mod_id, a_file_id, api_key, db)
                adopted += 1
            except Exception as e:
                console.print(f"  [red]Adopt failed: {e}[/red]")
        console.print(f"[green]Adopted {adopted}/{len(foreign)} entry(ies).[/green]")
        return

    # ── default / --check / --dry-run: reconcile flow ───────────────────────
    if not lof.exists():
        console.print(f"[yellow]No {lof_filename} found in {mod_dir}[/yellow]")
        return

    do_dry = dry_run or check
    result = reconcile_load_order(game, db, mod_dir, dry_run=do_dry, auto_merge=auto_merge)

    if result["frozen"]:
        console.print(f"[yellow]Load order is frozen ('-- nexmod:freeze' directive).[/yellow] "
                      f"No changes made. Run with --unfreeze to lift.")
        if check:
            _print_classification(result["classification"])
        return

    if result["drift_detected"] and not result["written"]:
        console.print("[yellow]External edit to load order detected since nexmod last wrote it.[/yellow]")
        if check or do_dry:
            console.print(f"[dim]Run with --auto-merge to apply nexmod's plan over the edit.[/dim]")
        else:
            console.print(f"[red]Refusing to overwrite. Pass --auto-merge to proceed.[/red]")
            return

    if check:
        _print_classification(result["classification"])
        if result["missing_added"]:
            console.print(f"\n[bold]Would add ({len(result['missing_added'])}):[/bold] "
                          + ", ".join(result["missing_added"]))
        if result["orphans_dropped"]:
            console.print(f"\n[bold]Would drop ({len(result['orphans_dropped'])}):[/bold] "
                          + ", ".join(result["orphans_dropped"]))
        if result["cycles"]:
            console.print(f"\n[yellow]Cycles ({len(result['cycles'])}):[/yellow] "
                          + ", ".join(result["cycles"]))
        if result["missing_deps"]:
            console.print(f"\n[yellow]Missing deps:[/yellow]")
            for declaring, deps in result["missing_deps"].items():
                console.print(f"  {declaring} → {', '.join(deps)}")
        if result["diff"]:
            console.print("\n[bold]Diff:[/bold]")
            console.print(Syntax(result["diff"], "diff", theme="ansi_dark"))
        else:
            console.print("\n[dim]No changes — file is already in canonical form.[/dim]")
        return

    if do_dry and result["diff"]:
        console.print(Syntax(result["diff"], "diff", theme="ansi_dark"))
        console.print("\n[dim]Dry run — no changes made.[/dim]")
        return

    if result["written"]:
        added   = len(result["missing_added"])
        dropped = len(result["orphans_dropped"])
        bits = []
        if added:   bits.append(f"+{added}")
        if dropped: bits.append(f"-{dropped}")
        suffix = f" ({', '.join(bits)})" if bits else ""
        console.print(f"[green]✓ {lof_filename} reconciled{suffix}.[/green]")
    else:
        console.print("[dim]Load order is already in canonical form.[/dim]")


# plugins / plugin-enable / plugin-disable / plugin-order ────────────────────

@cli.command("plugins")
@click.argument("game")
@click.option("--json", "output_json", is_flag=True, help="Output JSON array")
def list_plugins(game, output_json):
    """List plugins tracked in Plugins.txt for a Bethesda game (Starfield, etc.)."""
    info = GAMES.get(game, {})
    if not info.get("plugin_exts"):
        console.print(f"[red]{game} does not use a plugin load order.[/red]")
        sys.exit(1)

    plugins_txt_path = find_plugins_txt(game)
    if plugins_txt_path is None or not plugins_txt_path.exists():
        console.print(f"[yellow]Plugins.txt not found.[/yellow] Has {game} been launched at least once?")
        console.print(f"  Expected: {plugins_txt_path or '(Proton prefix not found)'}")
        sys.exit(1)

    db = get_db()
    parsed = _parse_plugins_txt(plugins_txt_path)
    db_plugins = {
        r["plugin_name"]: r["mod_id"]
        for r in db.execute("SELECT plugin_name, mod_id FROM plugin_files WHERE game=?", (game,)).fetchall()
    }
    official_lower = {m.lower() for m in info.get("official_masters", ())}
    mod_dir = resolve_mod_dir(game, db)

    if output_json:
        out = []
        for e in parsed["entries"]:
            out.append({
                "name": e["name"],
                "enabled": e["enabled"],
                "type": "official" if e["name"].lower() in official_lower else (
                    "managed" if e["name"] in db_plugins else "foreign"
                ),
                "mod_id": db_plugins.get(e["name"]),
                "on_disk": (mod_dir / e["name"]).exists(),
                "masters": _read_plugin_masters(mod_dir / e["name"]) if (mod_dir / e["name"]).exists() else [],
            })
        import json as _json
        console.print(_json.dumps(out, indent=2))
        return

    t = Table(title=f"Plugins.txt — {info.get('name', game)}", show_header=True, header_style="bold")
    t.add_column("#", style="dim", width=4)
    t.add_column("Plugin", style="cyan")
    t.add_column("State", width=8)
    t.add_column("Type", width=10)
    t.add_column("Masters (deps)", style="dim")

    for i, e in enumerate(parsed["entries"], 1):
        name = e["name"]
        state = "[green]enabled[/green]" if e["enabled"] else "[red]disabled[/red]"
        if name.lower() in official_lower:
            typ = "[dim]official[/dim]"
        elif name in db_plugins:
            typ = "[blue]managed[/blue]"
        else:
            typ = "[yellow]foreign[/yellow]"
        plugin_path = mod_dir / name
        masters = _read_plugin_masters(plugin_path) if plugin_path.exists() else []
        non_official_masters = [m for m in masters if m.lower() not in official_lower]
        deps_str = ", ".join(non_official_masters) if non_official_masters else ""
        t.add_row(str(i), name, state, typ, deps_str)

    console.print(t)
    console.print(f"[dim]Plugins.txt: {plugins_txt_path}[/dim]")


@cli.command("plugin-enable")
@click.argument("game")
@click.argument("plugin_name")
def plugin_enable(game, plugin_name):
    """Enable a plugin in Plugins.txt."""
    info = GAMES.get(game, {})
    if not info.get("plugin_exts"):
        console.print(f"[red]{game} does not use a plugin load order.[/red]")
        sys.exit(1)
    plugins_txt_path = find_plugins_txt(game)
    if not plugins_txt_path or not plugins_txt_path.exists():
        console.print("[red]Plugins.txt not found.[/red]")
        sys.exit(1)
    parsed = _parse_plugins_txt(plugins_txt_path)
    found = False
    for e in parsed["entries"]:
        if e["name"].lower() == plugin_name.lower():
            e["enabled"] = True
            found = True
            break
    if not found:
        console.print(f"[red]{plugin_name} not found in Plugins.txt.[/red]")
        sys.exit(1)
    _write_plugins_txt(plugins_txt_path, parsed["entries"])
    console.print(f"[green]Enabled:[/green] {plugin_name}")


@cli.command("plugin-disable")
@click.argument("game")
@click.argument("plugin_name")
def plugin_disable(game, plugin_name):
    """Disable a plugin in Plugins.txt (keeps it in the file but won't load)."""
    info = GAMES.get(game, {})
    if not info.get("plugin_exts"):
        console.print(f"[red]{game} does not use a plugin load order.[/red]")
        sys.exit(1)
    official_lower = {m.lower() for m in info.get("official_masters", ())}
    if plugin_name.lower() in official_lower:
        console.print(f"[red]{plugin_name} is an official master — cannot disable.[/red]")
        sys.exit(1)
    plugins_txt_path = find_plugins_txt(game)
    if not plugins_txt_path or not plugins_txt_path.exists():
        console.print("[red]Plugins.txt not found.[/red]")
        sys.exit(1)
    parsed = _parse_plugins_txt(plugins_txt_path)
    found = False
    for e in parsed["entries"]:
        if e["name"].lower() == plugin_name.lower():
            e["enabled"] = False
            found = True
            break
    if not found:
        console.print(f"[red]{plugin_name} not found in Plugins.txt.[/red]")
        sys.exit(1)
    _write_plugins_txt(plugins_txt_path, parsed["entries"])
    console.print(f"[yellow]Disabled:[/yellow] {plugin_name}")


@cli.command("plugin-order")
@click.argument("game")
@click.option("--dry-run", is_flag=True, help="Show proposed order without writing")
@click.option("--check", is_flag=True, help="Show classification table and diff, don't write")
def plugin_order(game, dry_run, check):
    """Reconcile and dependency-sort Plugins.txt for a Bethesda game.

    \b
    Classification:
      official  – game master files, always first, never reordered
      managed   – tracked by nexmod, dependency-sorted
      foreign   – on disk but not tracked, appended at end
      orphan    – in Plugins.txt but missing from disk, dropped
    """
    info = GAMES.get(game, {})
    if not info.get("plugin_exts"):
        console.print(f"[red]{game} does not use a plugin load order.[/red]")
        sys.exit(1)

    db = get_db()
    mod_dir = resolve_mod_dir(game, db)
    plugins_txt_path = find_plugins_txt(game)

    if plugins_txt_path is None:
        console.print("[red]Cannot locate Plugins.txt (Proton prefix not found).[/red]")
        console.print("  Launch the game at least once, then retry.")
        sys.exit(1)

    if check or dry_run:
        result = reconcile_plugins_txt(game, db, mod_dir, dry_run=True)
        if result.get("error"):
            console.print(f"[red]{result['error']}[/red]")
            sys.exit(1)

        # Show classification table
        parsed_before = _parse_plugins_txt(plugins_txt_path)
        official_lower = {m.lower() for m in info.get("official_masters", ())}
        db_plugins = {
            r["plugin_name"] for r in
            db.execute("SELECT plugin_name FROM plugin_files WHERE game=?", (game,)).fetchall()
        }
        disk_plugins = _get_disk_plugins(mod_dir, info["plugin_exts"])

        if check:
            t = Table(title="Plugin classification", show_header=True, header_style="bold")
            t.add_column("Plugin", style="cyan")
            t.add_column("Class", width=10)
            t.add_column("On Disk", width=8)
            t.add_column("In DB", width=7)
            t.add_column("In File", width=7)
            all_names = {e["name"] for e in parsed_before["entries"]} | disk_plugins
            for name in sorted(all_names):
                lower = name.lower()
                on_disk = name in disk_plugins
                in_db = name in db_plugins
                in_file = any(e["name"] == name for e in parsed_before["entries"])
                if lower in official_lower:
                    cls = "[dim]official[/dim]"
                elif on_disk and in_db:
                    cls = "[blue]managed[/blue]"
                elif on_disk and not in_db:
                    cls = "[yellow]foreign[/yellow]"
                elif not on_disk and in_file:
                    cls = "[red]orphan[/red]"
                else:
                    cls = "[dim]unknown[/dim]"
                t.add_row(name, cls,
                          "[green]yes[/green]" if on_disk else "[red]no[/red]",
                          "[green]yes[/green]" if in_db else "no",
                          "[green]yes[/green]" if in_file else "no")
            console.print(t)

        # Show diff
        old_names = [e["name"] for e in parsed_before["entries"]]
        new_names = [e["name"] for e in result.get("final", [])]
        if old_names != new_names:
            diff = list(difflib.unified_diff(
                [n + "\n" for n in old_names],
                [n + "\n" for n in new_names],
                fromfile="current", tofile="proposed", lineterm=""
            ))
            if diff:
                console.print("\n[bold]Proposed changes:[/bold]")
                for line in diff:
                    if line.startswith("+") and not line.startswith("+++"):
                        console.print(f"[green]{line}[/green]")
                    elif line.startswith("-") and not line.startswith("---"):
                        console.print(f"[red]{line}[/red]")
                    else:
                        console.print(f"[dim]{line}[/dim]")
        else:
            console.print("[dim]No changes needed.[/dim]")
        return

    result = reconcile_plugins_txt(game, db, mod_dir)
    if result.get("error"):
        console.print(f"[red]{result['error']}[/red]")
        sys.exit(1)
    if result["written"]:
        msgs = []
        if result["added"]:
            msgs.append(f"+{len(result['added'])} added")
        if result["dropped"]:
            msgs.append(f"-{len(result['dropped'])} dropped")
        if result["cycles"]:
            msgs.append(f"{len(result['cycles'])} cycles")
        console.print(f"[green]✓ Plugins.txt reconciled[/green]" +
                      (f" ({', '.join(msgs)})" if msgs else ""))
        if result["cycles"]:
            console.print(f"  [yellow]Dependency cycles: {', '.join(result['cycles'])}[/yellow]")
    else:
        console.print("[dim]Plugins.txt already up to date.[/dim]")


# sfse-install ────────────────────────────────────────────────────────────────

SFSE_NEXUS_MOD_ID = 106
SFSE_STEAM_LAUNCH_OPTION = "bash -c 'exec \"${@/Starfield.exe/sfse_loader.exe}\"' -- %command%"

@cli.command("sfse-install")
@click.argument("game", default="starfield")
@click.option("--from-file", "from_file", type=click.Path(path_type=Path), default=None,
              help="Install from a local SFSE archive instead of downloading.")
@click.option("--dry-run", is_flag=True, help="Show what would be installed, don't extract.")
def sfse_install(game, from_file, dry_run):
    """Install Starfield Script Extender (SFSE) to the game root directory.

    \b
    SFSE files go to the game root (alongside Starfield.exe), not Data/.
    After installation, set this Steam launch option on Starfield:

      bash -c 'exec "${@/Starfield.exe/sfse_loader.exe}"' -- %command%

    Nexus Premium: downloads SFSE (mod 106) automatically.
    Free account:  download the archive from nexusmods.com/starfield/mods/106
                   and pass it with --from-file <archive>.

    Note: the 'Plugins.txt Enabler' mod is NOT needed — Bethesda added native
    plugins.txt support in patch 1.12.30 (June 2024).
    """
    if game != "starfield":
        console.print(f"[red]sfse-install only supports Starfield (got: {game})[/red]")
        sys.exit(1)

    info = GAMES["starfield"]
    db = get_db()

    # Resolve game root (parent of Data/)
    try:
        mod_dir  = resolve_mod_dir(game, db)
        game_root = mod_dir.parent
    except SystemExit:
        console.print("[red]Starfield install not found. Run: nexmod doctor starfield[/red]")
        sys.exit(1)

    api_key = get_api_key()
    domain  = info["domain"]

    # Fetch mod metadata so we know the file to download
    with console.status("Fetching SFSE metadata..."):
        mod   = api_mod_info(domain, SFSE_NEXUS_MOD_ID, api_key)
        files = api_mod_files(domain, SFSE_NEXUS_MOD_ID, api_key)

    console.print(f"  [bold]{mod['name']}[/bold] v{mod.get('version', '?')}")

    chosen = pick_main_file(files)
    if not chosen:
        console.print("[red]Could not determine main file. Use --from-file.[/red]")
        for f in files:
            console.print(f"  [{f['file_id']}] {f['file_name']} ({f.get('category_name')})")
        sys.exit(1)

    if dry_run:
        console.print(f"\n[bold](dry-run) Would install to:[/bold] {game_root}")
        console.print(f"  File: {chosen['file_name']} ({chosen.get('size_kb', '?')} KB)")
        console.print(f"  Target: {game_root}/sfse_loader.exe  +  sfse_steam_loader.dll  +  versioned DLL")
        console.print(f"\n[bold]After installation, set this Steam launch option:[/bold]")
        console.print(f"  [cyan]{SFSE_STEAM_LAUNCH_OPTION}[/cyan]")
        return

    if from_file:
        archive = from_file.resolve()
        if not archive.exists():
            console.print(f"[red]File not found: {archive}[/red]")
            sys.exit(1)
        console.print(f"  Installing from local file: [dim]{archive}[/dim]")
        _do_sfse_extract(archive, game_root, own_archive=False)
    else:
        with console.status("Getting CDN download link..."):
            urls = api_download_urls(domain, SFSE_NEXUS_MOD_ID, chosen["file_id"], api_key)
        if not urls:
            console.print(
                "[red]Direct download requires Premium.[/red]\n"
                "  Download from [cyan]nexusmods.com/starfield/mods/106[/cyan] "
                "and use [cyan]--from-file <archive>[/cyan]."
            )
            sys.exit(1)

        tmp = DATA_DIR / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        archive = tmp / chosen["file_name"]
        try:
            _try_download_with_mirrors(urls, archive)
            _do_sfse_extract(archive, game_root, own_archive=True)
        finally:
            archive.unlink(missing_ok=True)

    # Verify key files landed
    sfse_loader = game_root / "sfse_loader.exe"
    sfse_dll    = game_root / "sfse_steam_loader.dll"
    if sfse_loader.exists() and sfse_dll.exists():
        console.print(f"\n[green]✓ SFSE installed to {game_root}[/green]")
    else:
        console.print(f"\n[yellow]⚠ Installation complete but sfse_loader.exe not found at {game_root}.[/yellow]")
        console.print("  The archive may have a different layout. Check manually.")

    console.print(f"\n[bold]Next: set this Steam launch option on Starfield:[/bold]")
    console.print(f"  [cyan]{SFSE_STEAM_LAUNCH_OPTION}[/cyan]")
    console.print("\n[dim]Right-click Starfield → Properties → General → Launch Options[/dim]")
    console.print("[dim]Note: 'Plugins.txt Enabler' is NOT needed — Bethesda added native support in patch 1.12.30.[/dim]")


def _do_sfse_extract(archive: Path, game_root: Path, own_archive: bool) -> None:
    """Extract SFSE archive, placing root-level files into game_root.

    SFSE archives typically contain files at the root level (sfse_loader.exe,
    sfse_steam_loader.dll, etc.) and optionally a Data/ subtree. Root-level
    files and Data/ both land correctly when extracted to game_root.
    """
    console.print(f"  Extracting to [dim]{game_root}[/dim]...")
    extract_archive(archive, game_root)
    log.info("SFSE extracted to %s", game_root)


# pin / unpin ─────────────────────────────────────────────────────────────────

@cli.command("pin")
@click.argument("game")
@click.argument("folder")
@click.argument("position", type=click.Choice(["top", "bottom", "before", "after"], case_sensitive=False))
@click.argument("relative_to", required=False)
def pin_folder(game, folder, position, relative_to):
    """Pin FOLDER to a load-order position. Persisted in the DB; applied on every reconcile.

    \b
    Examples:
      nexmod pin darktide my_mod top
      nexmod pin darktide my_mod bottom
      nexmod pin darktide my_mod before mod_compat
      nexmod pin darktide my_mod after dmf
    """
    position = position.lower()
    if position in ("before", "after") and not relative_to:
        console.print(f"[red]'{position}' requires a relative_to folder name.[/red]")
        sys.exit(1)
    if position in ("top", "bottom") and relative_to:
        console.print(f"[yellow]Ignoring relative_to='{relative_to}' for '{position}' pin.[/yellow]")
        relative_to = None

    db = get_db()
    db.execute("""
        INSERT INTO load_order_pins (game, folder, position, relative_to, source, created_at)
        VALUES (?, ?, ?, ?, 'user', ?)
        ON CONFLICT(game, folder) DO UPDATE SET
            position    = excluded.position,
            relative_to = excluded.relative_to,
            source      = 'user'
    """, (game, folder, position, relative_to, now_iso()))
    db.commit()
    rel = f" {relative_to}" if relative_to else ""
    console.print(f"[green]✓ Pinned[/green] {folder} → {position}{rel}")
    console.print(f"[dim]Run 'nexmod order {game}' to apply.[/dim]")


@cli.command("unpin")
@click.argument("game")
@click.argument("folder")
def unpin_folder(game, folder):
    """Remove a pin previously set with 'nexmod pin'."""
    db = get_db()
    cur = db.execute(
        "DELETE FROM load_order_pins WHERE game=? AND folder=?", (game, folder)
    )
    db.commit()
    if cur.rowcount:
        console.print(f"[green]✓ Unpinned[/green] {folder}")
        console.print(f"[dim]Run 'nexmod order {game}' to apply.[/dim]")
    else:
        console.print(f"[yellow]No pin found for {folder} on {game}.[/yellow]")


@cli.command("pins")
@click.argument("game")
def list_pins(game):
    """Show current load-order pins for GAME."""
    db = get_db()
    rows = db.execute(
        "SELECT folder, position, relative_to, source, created_at "
        "FROM load_order_pins WHERE game=? ORDER BY created_at",
        (game,),
    ).fetchall()
    if not rows:
        console.print(f"[dim]No pins set for {game}.[/dim]")
        return
    t = Table(title=f"Load-order pins — {game}", show_lines=False, box=None, padding=(0, 1))
    t.add_column("Folder",      style="bold")
    t.add_column("Position",    style="cyan")
    t.add_column("Relative to", style="dim")
    t.add_column("Source",      style="dim")
    for r in rows:
        t.add_row(r["folder"], r["position"], r["relative_to"] or "—", r["source"])
    console.print(t)


def _print_classification(classification: dict[str, str]) -> None:
    """Pretty-print the {folder: class} classification map as a table."""
    if not classification:
        console.print("[dim]No entries in load order.[/dim]")
        return
    by_class: dict[str, list[str]] = {}
    for folder, cls in classification.items():
        by_class.setdefault(cls, []).append(folder)
    t = Table(title="Load order classification", show_lines=False, box=None, padding=(0, 1))
    t.add_column("Class",   style="bold")
    t.add_column("Count",   style="dim", justify="right")
    t.add_column("Folders", style="dim")
    order = ["framework", "managed-present", "foreign", "managed-missing", "orphan"]
    for cls in order:
        if cls in by_class:
            folders = by_class[cls]
            preview = ", ".join(folders[:6]) + (f", … (+{len(folders)-6})" if len(folders) > 6 else "")
            t.add_row(cls, str(len(folders)), preview)
    console.print(t)


# update ──────────────────────────────────────────────────────────────────────

@cli.command("update")
@click.argument("game")
@click.option("--mod-id", type=int, default=None, help="Update only this mod ID")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--no-reorder", is_flag=True, help="Skip automatic load order sort after update")
@click.option("--fix-deps", is_flag=True, help="Interactively install any missing dependencies surfaced after the update scan (default: report only).")
@click.option("--json", "output_json", is_flag=True,
              help="Emit machine-readable JSON summary. Implies --yes.")
def update_mods(game, mod_id, yes, no_reorder, fix_deps, output_json):
    """Download and apply all available updates for GAME.

    Always scans for missing dependencies after the update pass — even when
    no mods needed updating. By default, missing deps are reported only;
    pass --fix-deps to be prompted to install each one.
    """
    from contextlib import nullcontext
    if output_json:
        yes = True  # non-interactive when machine-readable output requested

    api_key = get_api_key()
    db      = get_db()

    q = "SELECT * FROM mods WHERE game = ?"
    p: list = [game]
    if mod_id:
        q += " AND mod_id = ?"
        p.append(mod_id)
    rows = db.execute(q, p).fetchall()

    if not rows:
        if output_json:
            click.echo(json.dumps({"game": game, "updated": [], "current": [],
                                   "failed": [], "missing_deps": {}, "load_order": {}}))
        else:
            console.print(f"[yellow]No mods tracked for '{game}'.[/yellow]")
        return

    info   = GAMES.get(game, {})
    domain = info.get("domain", game)

    updated_count  = 0
    json_updated:  list = []
    json_current:  list = []
    json_failed:   list = []

    for r in rows:
        ctx = nullcontext() if output_json else console.status(f"Checking {r['name']}...")
        with ctx:
            try:
                mod    = api_mod_info(domain, r["mod_id"], api_key)
                latest = mod.get("version", "?")
            except Exception as e:
                log.error("Update check failed for mod_id=%s: %s", r["mod_id"], e)
                if not output_json:
                    console.print(f"[red]{r['name']}: error — {e}[/red]")
                record(db, "update", game, r["mod_id"], r["name"], None, "fail", str(e))
                json_failed.append({"mod_id": r["mod_id"], "name": r["name"], "error": str(e)})
                continue

        if _norm_version(latest) == _norm_version(r["version"] or ""):
            if not output_json:
                console.print(f"[dim]{r['name']}: current ({latest})[/dim]")
            json_current.append({"mod_id": r["mod_id"], "name": r["name"], "version": latest})
            continue

        if not output_json:
            console.print(f"[cyan]{r['name']}:[/cyan] {r['version']} → {latest}")
        if not yes and not click.confirm("  Download update?", default=True):
            record(db, "update", game, r["mod_id"], r["name"], latest, "skip")
            json_current.append({"mod_id": r["mod_id"], "name": r["name"],
                                  "version": r["version"], "skipped": True})
            continue

        ctx2 = nullcontext() if output_json else console.status("Getting files...")
        with ctx2:
            files  = api_mod_files(domain, r["mod_id"], api_key)
            chosen = pick_main_file(files)

        if not chosen:
            log.error("No main file for update: mod_id=%s", r["mod_id"])
            if not output_json:
                console.print(f"  [red]Could not determine update file — skipping.[/red]")
            record(db, "update", game, r["mod_id"], r["name"], latest, "fail", "no main file")
            json_failed.append({"mod_id": r["mod_id"], "name": r["name"],
                                 "error": "no main file found"})
            continue

        ctx3 = nullcontext() if output_json else console.status("Getting CDN link...")
        with ctx3:
            urls = api_download_urls(domain, r["mod_id"], chosen["file_id"], api_key)

        if not urls:
            record(db, "update", game, r["mod_id"], r["name"], latest, "fail", "no download URLs")
            if not output_json:
                console.print(f"  [red]No download URLs returned — check Premium status.[/red]")
            json_failed.append({"mod_id": r["mod_id"], "name": r["name"],
                                 "error": "no download URLs"})
            continue

        mod_dir = resolve_mod_dir(game, db)
        tmp     = DATA_DIR / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        archive = tmp / chosen["file_name"]

        size_kb = chosen.get("size_kb") or 0
        if size_kb > 0:
            try:
                size_bytes = size_kb * 1024
                _check_disk_space(tmp, size_bytes + 50 * 1024 * 1024, "download")
                if mod_dir.exists():
                    _check_disk_space(mod_dir, size_bytes * 3, "extraction")
            except RuntimeError as e:
                record(db, "update", game, r["mod_id"], r["name"], latest, "fail", str(e))
                if not output_json:
                    console.print(f"  [red]Skipping: {e}[/red]")
                json_failed.append({"mod_id": r["mod_id"], "name": r["name"], "error": str(e)})
                continue

        extraction_ok = False
        try:
            _try_download_with_mirrors(urls, archive)
            if chosen.get("md5"):
                verify_md5(archive, chosen["md5"])
            if not output_json:
                console.print(f"  Extracting to [dim]{mod_dir}[/dim]...")
            extract_archive(archive, mod_dir)
            extraction_ok = True
            try:
                _save_snapshot(game, r["mod_id"], latest, archive)
            except Exception as e:
                log.warning("snapshot save failed: %s", e)
        except Exception as e:
            record(db, "update", game, r["mod_id"], r["name"], latest, "fail", str(e))
            if not output_json:
                console.print(f"  [red]Failed: {e}[/red]")
            json_failed.append({"mod_id": r["mod_id"], "name": r["name"], "error": str(e)})
        finally:
            archive.unlink(missing_ok=True)

        if not extraction_ok:
            continue

        db.execute(
            "UPDATE mods SET file_id=?, version=?, filename=?, updated_at=? WHERE game=? AND mod_id=?",
            (chosen["file_id"], latest, chosen["file_name"], now_iso(), game, r["mod_id"]),
        )
        db.commit()
        record(db, "update", game, r["mod_id"], r["name"], latest, "ok")
        if not output_json:
            console.print(f"  [green]✓ Updated to {latest}[/green]")
        json_updated.append({"mod_id": r["mod_id"], "name": r["name"],
                              "from": r["version"], "to": latest})
        updated_count += 1

    if not output_json:
        console.print(f"\n[bold]Done.[/bold] {updated_count} mod(s) updated.")

    # Always run the dep/order scan — even when nothing updated.
    info = GAMES.get(game, {})
    json_lof: dict = {}
    if info.get("load_order_file") and not no_reorder and get_auto_reorder():
        mod_dir = resolve_mod_dir(game, db)
        result  = reconcile_load_order(game, db, mod_dir)
        json_lof = {
            "written":        result["written"],
            "cycles":         result.get("cycles", []),
            "drift_detected": result.get("drift_detected", False),
        }
        if not output_json:
            if result["written"]:
                console.print(f"[dim]Load order sorted.[/dim]")
            if result["cycles"]:
                console.print(f"[yellow]Dependency cycle detected: {', '.join(result['cycles'])}[/yellow]")
            if result["drift_detected"] and not result["written"]:
                console.print("[yellow]Load order: external edit detected — not modified.[/yellow]")
        if result["missing_deps"]:
            if fix_deps:
                newly = _handle_missing_deps(game, result["missing_deps"], mod_dir, api_key, db, yes=yes)
                if newly:
                    reconcile_load_order(game, db, mod_dir)
            elif not output_json:
                n_mods = len(result["missing_deps"])
                n_deps = sum(len(d) for d in result["missing_deps"].values())
                console.print(
                    f"\n[yellow]⚠ {n_mods} mod(s) missing {n_deps} dependency(ies):[/yellow]"
                )
                for declaring, deps in result["missing_deps"].items():
                    console.print(f"  [yellow]{declaring}[/yellow] → {', '.join(deps)}")
                console.print(
                    f"  [dim]Re-run with [cyan]--fix-deps[/cyan] to install them interactively.[/dim]"
                )
            if output_json:
                json_lof["missing_deps"] = {
                    k: v for k, v in result["missing_deps"].items()
                }

    if output_json:
        click.echo(json.dumps({
            "game":         game,
            "updated":      json_updated,
            "current":      json_current,
            "failed":       json_failed,
            "load_order":   json_lof,
        }, indent=2))


# remove ──────────────────────────────────────────────────────────────────────

@cli.command("remove")
@click.argument("game")
@click.argument("mod_id", type=int)
@click.option("--purge", is_flag=True, help="Also delete mod files from disk")
@click.option("--yes", "-y", is_flag=True, help="Skip the --purge confirmation prompt")
@click.option("--force-legacy-purge", is_flag=True,
              help="Allow --purge on legacy rows that have no recorded folder_name "
                   "(falls back to filename-stem inference — may delete the wrong folder).")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without making changes")
def remove_mod(game, mod_id, purge, yes, force_legacy_purge, dry_run):
    """Stop tracking a mod. Use --purge to also delete its files."""
    db  = get_db()
    row = db.execute("SELECT * FROM mods WHERE game=? AND mod_id=?", (game, mod_id)).fetchone()
    if not row:
        console.print(f"[red]Mod {mod_id} is not tracked for '{game}'.[/red]")
        sys.exit(1)

    if purge and row["mod_dir"]:
        mod_dir = Path(row["mod_dir"])
        # Prefer the folder_name column (captured at install). Filename-stem is
        # the legacy fallback — gated behind --force-legacy-purge because it
        # can delete the wrong folder when archive names don't map cleanly to
        # extracted folders (UUID paths, vortex names, etc.).
        candidates: list[str] = []
        used_legacy_fallback = False
        if row["folder_name"]:
            candidates.append(row["folder_name"])
        elif force_legacy_purge and row["filename"]:
            stem = row["filename"].rsplit(".", 1)[0]
            candidates.append(stem)
            used_legacy_fallback = True

        if not candidates:
            console.print(
                f"[yellow]Cannot purge:[/yellow] no recorded folder_name for [cyan]{row['name']}[/cyan]."
            )
            console.print(
                "  Run [cyan]nexmod fsck[/cyan] to backfill, or pass "
                "[cyan]--force-legacy-purge[/cyan] to use filename-stem inference (may delete the wrong folder)."
            )
            sys.exit(1)

        target = mod_dir / candidates[0]
        if not target.exists() or not target.is_dir():
            console.print(
                f"[yellow]Folder to purge not found:[/yellow] {target}\n"
                "  Files may already be gone, or the folder was renamed."
            )
            # Fall through to DB removal — nothing on disk to delete.
        else:
            warn = " [yellow](legacy fallback — verify before confirming)[/yellow]" if used_legacy_fallback else ""
            console.print(f"[bold red]About to delete:[/bold red] {target}{warn}")
            try:
                file_count = sum(1 for _ in target.rglob("*"))
                console.print(f"  [dim]{file_count} file(s) under that folder[/dim]")
            except Exception:
                pass

            if dry_run:
                console.print("[dim](dry-run — no changes made)[/dim]")
                return
            if not yes and not click.confirm("Proceed?", default=False):
                console.print("[dim]Aborted.[/dim]")
                sys.exit(0)
            shutil.rmtree(target)
            log.info("Purged mod folder %s", target)
            console.print(f"[dim]Deleted {target}[/dim]")

    if dry_run:
        console.print(f"[dim](dry-run) Would untrack {row['name']} (mod_id={mod_id})[/dim]")
        return

    # Remove plugin_files rows before deleting the mod (for Bethesda games)
    info = GAMES.get(game) or {}
    removed_plugins = []
    if info.get("plugin_exts"):
        removed_plugins = [
            r["plugin_name"] for r in
            db.execute("SELECT plugin_name FROM plugin_files WHERE game=? AND mod_id=?",
                       (game, mod_id)).fetchall()
        ]
        db.execute("DELETE FROM plugin_files WHERE game=? AND mod_id=?", (game, mod_id))

    db.execute("DELETE FROM mods WHERE game=? AND mod_id=?", (game, mod_id))
    db.commit()
    record(db, "remove", game, mod_id, row["name"], row["version"], "ok")
    console.print(f"[green]Removed:[/green] {row['name']}")

    # Reconcile the load order: drops orphaned entries, preserves foreign,
    # auto-adds discovered framework folders, topo-sorts.
    # force_drop ensures the removed mod's folder is treated as an orphan even
    # if its files are still on disk (files-on-disk → "foreign" → kept otherwise).
    if info.get("load_order_file") and row["mod_dir"]:
        removed_folder = row["folder_name"] or None
        try:
            result = reconcile_load_order(
                game, db, Path(row["mod_dir"]),
                force_drop={removed_folder} if removed_folder else None,
            )
            dropped = result["orphans_dropped"]
            if dropped:
                console.print(f"[dim]Load order: dropped {len(dropped)} orphaned "
                              f"entr{'y' if len(dropped) == 1 else 'ies'} "
                              f"({', '.join(dropped)}).[/dim]")
            elif result["drift_detected"]:
                console.print("[yellow]Load order: external edit detected — not modified. "
                              "Run 'nexmod order <game> --check' to review.[/yellow]")
        except Exception as e:
            log.warning("reconcile after remove failed: %s", e)
            console.print(f"[yellow]Load order reconcile failed: {e}[/yellow]")

    # Bethesda: reconcile Plugins.txt — removed plugins become orphans → dropped
    if removed_plugins and row["mod_dir"]:
        try:
            result = reconcile_plugins_txt(
                game, db, Path(row["mod_dir"]),
                force_drop=set(removed_plugins),
            )
            if result.get("dropped"):
                console.print(f"[dim]Plugins.txt: removed {', '.join(result['dropped'])}[/dim]")
        except Exception as e:
            log.warning("plugins.txt reconcile after remove failed: %s", e)
            console.print(f"[yellow]Plugins.txt update failed: {e}[/yellow]")


# uninstall ───────────────────────────────────────────────────────────────────

@cli.command("uninstall")
@click.argument("game")
@click.argument("mod_id", type=int)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@click.pass_context
def uninstall_mod(ctx, game, mod_id, yes):
    """Remove a mod and delete its files from disk (alias for: remove --purge)."""
    ctx.invoke(remove_mod, game=game, mod_id=mod_id, purge=True, yes=yes,
               force_legacy_purge=False, dry_run=False)


# fsck ────────────────────────────────────────────────────────────────────────

@cli.command("fsck")
@click.argument("game", required=False)
@click.option("--fix", is_flag=True, help="Apply fixes (default: dry-run report only).")
@click.option("--with-api", is_flag=True,
              help="Also re-fetch missing version strings from Nexus API "
                   "(requires --fix; one API call per NULL-version row).")
@click.option("--scan", is_flag=True,
              help="Detect subdirectories in the mod folder not tracked in the DB, "
                   "and offer to track them (requires GAME argument and an API key).")
def fsck(game, fix, with_api, scan):
    """Audit the local mod database and repair drift.

    Scans tracked mods for: missing folder_name (legacy rows installed before
    that column existed), missing version, and orphaned mod_dirs (the game's
    mod folder no longer exists). Reports findings; with --fix, applies them.
    """
    db = get_db()
    q = "SELECT * FROM mods"
    p: list = []
    if game:
        q += " WHERE game = ?"
        p.append(game)
    rows = db.execute(q, p).fetchall()

    if not rows:
        scope = f"for '{game}'" if game else "in DB"
        console.print(f"[yellow]No mods tracked {scope}.[/yellow]")
        if scan:
            _fsck_scan(game, db)
        return

    # Cache mod_dir scans so repeated rows in the same dir don't re-walk it.
    mod_dir_cache: dict[str, Path | None] = {}
    def _resolve(mod_dir_str: str | None) -> Path | None:
        if not mod_dir_str:
            return None
        if mod_dir_str not in mod_dir_cache:
            p = Path(mod_dir_str)
            mod_dir_cache[mod_dir_str] = p if p.exists() else None
        return mod_dir_cache[mod_dir_str]

    null_folder: list = []
    null_version: list = []
    orphan: list = []
    inferred: dict[int, tuple[str, str]] = {}        # row_id → (folder, strategy)
    ambiguous: dict[int, list[str]] = {}              # row_id → candidate folders

    for r in rows:
        mod_dir = _resolve(r["mod_dir"])
        if r["mod_dir"] and not mod_dir:
            orphan.append(r)
        if not r["folder_name"]:
            null_folder.append(r)
            if mod_dir:
                folder, candidates, strategy = infer_folder_name(mod_dir, r)
                if folder:
                    inferred[r["id"]] = (folder, strategy)
                elif strategy == "ambiguous":
                    ambiguous[r["id"]] = candidates
        if not r["version"]:
            null_version.append(r)

    # Collision check: if two rows point to the same inferred folder, we don't
    # know which one really owns it — bail out on both rather than guess wrong.
    folder_to_rids: dict[str, list[int]] = {}
    for rid, (folder, _) in inferred.items():
        folder_to_rids.setdefault(folder, []).append(rid)
    collisions = {f: rs for f, rs in folder_to_rids.items() if len(rs) > 1}
    for rid_list in collisions.values():
        for rid in rid_list:
            inferred.pop(rid, None)

    # Report
    console.print(f"[bold]nexmod fsck:[/bold] scanned {len(rows)} mod(s)"
                  f"{' for ' + game if game else ''}")
    console.print(f"  Missing folder_name : [yellow]{len(null_folder)}[/yellow]")
    if null_folder:
        console.print(f"    inferable        : [green]{len(inferred)}[/green]")
        console.print(f"    ambiguous        : [yellow]{len(ambiguous)}[/yellow]")
        console.print(f"    collisions       : [red]{len(collisions)}[/red]")
        console.print(f"    no match         : [dim]{len(null_folder) - len(inferred) - len(ambiguous) - sum(len(v) for v in collisions.values())}[/dim]")
    console.print(f"  Missing version     : [yellow]{len(null_version)}[/yellow]")
    console.print(f"  Orphan mod_dir      : [red]{len(orphan)}[/red]")

    if inferred:
        console.print("\n[bold]Inferred folder names:[/bold]")
        t = Table(box=None, padding=(0, 1), show_header=True)
        t.add_column("Mod", style="cyan", overflow="fold")
        t.add_column("Folder", style="green")
        t.add_column("Strategy", style="dim")
        rows_by_id = {r["id"]: r for r in rows}
        for rid in sorted(inferred):
            folder, strategy = inferred[rid]
            t.add_row(rows_by_id[rid]["name"], folder, strategy)
        console.print(t)

    if ambiguous:
        console.print("\n[yellow]Ambiguous matches — fix manually with "
                      "[cyan]sqlite3 ~/.local/share/nexmod/mods.db[/cyan]:[/yellow]")
        rows_by_id = {r["id"]: r for r in rows}
        for rid, cands in ambiguous.items():
            console.print(f"  [cyan]{rows_by_id[rid]['name']}[/cyan]: candidates = {', '.join(cands)}")

    if collisions:
        console.print("\n[red]Folder collisions (multiple mods would claim the same folder):[/red]")
        rows_by_id = {r["id"]: r for r in rows}
        for folder, rid_list in collisions.items():
            names = [rows_by_id[rid]["name"] for rid in rid_list]
            console.print(f"  [red]{folder}[/red]: {' | '.join(names)}")

    if orphan:
        console.print("\n[red]Orphan rows (mod_dir does not exist):[/red]")
        for r in orphan[:10]:
            console.print(f"  [dim]{r['name']} → {r['mod_dir']}[/dim]")
        if len(orphan) > 10:
            console.print(f"  [dim]…+{len(orphan) - 10} more[/dim]")

    if not fix:
        console.print("\n[dim]Dry-run only. Re-run with [cyan]--fix[/cyan] to apply.[/dim]")
        if scan:
            _fsck_scan(game, db)
        return

    # Apply
    applied_folders = 0
    for rid, (folder, _) in inferred.items():
        db.execute("UPDATE mods SET folder_name = ? WHERE id = ?", (folder, rid))
        applied_folders += 1

    applied_versions = 0
    if with_api and null_version:
        api_key = get_api_key()
        with console.status(f"Re-fetching {len(null_version)} version(s)..."):
            for r in null_version:
                info = GAMES.get(r["game"], {})
                domain = info.get("domain", r["game"])
                try:
                    mod = api_mod_info(domain, r["mod_id"], api_key)
                    latest = mod.get("version")
                    if latest:
                        db.execute("UPDATE mods SET version = ? WHERE id = ?", (latest, r["id"]))
                        applied_versions += 1
                except Exception as e:
                    log.warning("fsck: version refetch failed for %s: %s", r["name"], e)

    db.commit()
    console.print(
        f"\n[green]Applied:[/green] {applied_folders} folder_name, {applied_versions} version"
    )
    record(db, "fsck", game or "*", None, None, None, "ok",
           f"folder_name={applied_folders} version={applied_versions}")

    # ── --scan: detect untracked folders ─────────────────────────────────────
    if scan:
        _fsck_scan(game, db)


def _fsck_scan(game: str | None, db: sqlite3.Connection) -> None:
    """Walk the mod directory for untracked subdirectories and offer to track them.

    Requires a game argument and an API key (search and mod_info are free endpoints).
    """
    if not game:
        console.print("[red]--scan requires a GAME argument.[/red]")
        return

    api_key = get_api_key()
    info    = GAMES.get(game, {})
    domain  = info.get("domain", game)
    mod_dir = resolve_mod_dir(game, db)

    if not mod_dir.exists():
        console.print(f"[yellow]Mod directory does not exist: {mod_dir}[/yellow]")
        return

    # Build set of already-tracked folder names for this game.
    tracked_folders = {
        r["folder_name"]
        for r in db.execute(
            "SELECT folder_name FROM mods WHERE game = ? AND folder_name IS NOT NULL",
            (game,),
        ).fetchall()
    }

    console.print(f"\n[bold]fsck --scan:[/bold] scanning [dim]{mod_dir}[/dim]")

    unknown = [
        d for d in mod_dir.iterdir()
        if d.is_dir() and d.name not in tracked_folders
    ]

    if not unknown:
        console.print("[green]✓ No untracked folders found.[/green]")
        return

    console.print(f"  Found [yellow]{len(unknown)}[/yellow] untracked folder(s).\n")

    n_found    = len(unknown)
    n_tracked  = 0
    n_skipped  = 0

    for folder in unknown:
        console.print(f"[bold]Unknown folder:[/bold] [cyan]{folder.name}[/cyan]")

        # Search Nexus for possible matches.
        try:
            with console.status(f"  Searching Nexus for '{folder.name}'..."):
                nodes = api_search_mods(domain, folder.name, api_key, count=5)
        except Exception as e:
            console.print(f"  [yellow]Search failed: {e}[/yellow]")
            nodes = []

        if nodes:
            t = Table(box=None, padding=(0, 1), show_header=True)
            t.add_column("#", style="dim", width=2)
            t.add_column("Mod name", style="cyan")
            t.add_column("Mod ID", justify="right")
            t.add_column("Endorsements", justify="right", style="dim")
            for i, node in enumerate(nodes[:5], 1):
                t.add_row(
                    str(i),
                    node.get("name", "?"),
                    str(node.get("modId", "?")),
                    str(node.get("endorsements", "?")),
                )
            console.print(t)

            choice = click.prompt(
                "  Track this folder? Enter mod ID, 1-5 to pick from list, or [s]kip",
                default="s",
            ).strip().lower()

            if choice == "s" or choice == "":
                console.print("  [dim]Skipped.[/dim]")
                n_skipped += 1
                continue

            mod_id: int | None = None
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(nodes):
                    mod_id = nodes[idx - 1].get("modId")
                else:
                    # Treat as a literal mod ID
                    mod_id = int(choice)
            else:
                console.print("  [dim]Skipped.[/dim]")
                n_skipped += 1
                continue
        else:
            console.print("  [dim]No search results.[/dim]")
            raw = click.prompt(
                "  Enter mod ID to track, or [s]kip",
                default="s",
            ).strip().lower()
            if raw == "s" or not raw.isdigit():
                console.print("  [dim]Skipped.[/dim]")
                n_skipped += 1
                continue
            mod_id = int(raw)

        if mod_id is None:
            console.print("  [dim]Skipped.[/dim]")
            n_skipped += 1
            continue

        # Fetch mod info and insert into DB.
        try:
            with console.status(f"  Fetching mod {mod_id} info..."):
                mod   = api_mod_info(domain, mod_id, api_key)
                files = api_mod_files(domain, mod_id, api_key)
        except SystemExit:
            console.print(f"  [red]Could not fetch mod {mod_id}.[/red]")
            n_skipped += 1
            continue

        chosen = pick_main_file(files)
        db.execute("""
            INSERT OR IGNORE INTO mods
                (game, mod_id, file_id, name, version, filename, mod_dir,
                 folder_name, tracked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            game, mod_id,
            chosen["file_id"] if chosen else 0,
            mod["name"], mod.get("version"),
            chosen["file_name"] if chosen else None,
            str(mod_dir),
            folder.name,
            now_iso(),
        ))
        db.commit()
        record(db, "track", game, mod_id, mod["name"], mod.get("version"), "ok")
        console.print(
            f"  [green]✓[/green] Tracked: [bold]{mod['name']}[/bold] "
            f"v{mod.get('version', '?')} (folder: [cyan]{folder.name}[/cyan])"
        )
        n_tracked += 1

    console.print(
        f"\n[bold]Scan complete:[/bold] {n_found} folder(s) found, "
        f"[green]{n_tracked} tracked[/green], [dim]{n_skipped} skipped[/dim]."
    )


# rollback / snapshots ────────────────────────────────────────────────────────

@cli.command("snapshots")
@click.argument("game")
@click.argument("mod_id", type=int, required=False, default=None)
@click.option("--prune", is_flag=True,
              help=f"Force-prune to {SNAPSHOTS_PER_MOD} most-recent snapshots per mod.")
def snapshots(game, mod_id, prune):
    """List cached version snapshots used by `nexmod rollback`.

    Snapshots are written automatically after each successful install/update,
    capped to the most-recent SNAPSHOTS_PER_MOD per mod (default 3).
    """
    db = get_db()
    if mod_id is None:
        rows = db.execute(
            "SELECT mod_id, name FROM mods WHERE game = ? ORDER BY name", (game,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT mod_id, name FROM mods WHERE game = ? AND mod_id = ?",
            (game, mod_id),
        ).fetchall()

    if not rows:
        scope = f"mod {mod_id}" if mod_id else f"any mod"
        console.print(f"[yellow]No tracked records found for {scope} in {game}.[/yellow]")
        return

    total_snaps = 0
    total_pruned = 0
    t = Table(title=f"Snapshot cache — {game}", box=None, padding=(0, 1))
    t.add_column("Mod", style="cyan")
    t.add_column("Versions", style="dim")
    t.add_column("Total size", style="dim", justify="right")
    for r in rows:
        snaps = _list_snapshots(game, r["mod_id"])
        if not snaps:
            continue
        if prune:
            total_pruned += _prune_snapshots(game, r["mod_id"])
            snaps = _list_snapshots(game, r["mod_id"])
        labels = ", ".join(_snapshot_version_label(s) for s in snaps)
        size_kb = sum(s.stat().st_size for s in snaps) // 1024
        t.add_row(r["name"], labels, f"{size_kb} KB")
        total_snaps += len(snaps)

    if total_snaps == 0:
        console.print(f"[dim]No snapshots cached yet for {game}.[/dim]")
        return
    console.print(t)
    console.print(f"\n[dim]Total: {total_snaps} snapshot(s)"
                  f"{f', pruned {total_pruned}' if prune else ''}.[/dim]")


@cli.command("rollback")
@click.argument("game")
@click.argument("mod_id", type=int)
@click.option("--version", help="Specific version to restore "
              "(default: most recent prior to current).")
@click.option("--list", "list_only", is_flag=True,
              help="Just list snapshots; don't restore anything.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def rollback(game, mod_id, version, list_only, yes):
    """Restore a previous version of a tracked mod from snapshot cache.

    Use [cyan]nexmod snapshots[/cyan] to see what's available. If no --version
    is passed, rolls back to the most recent prior version.
    """
    db = get_db()
    row = db.execute(
        "SELECT * FROM mods WHERE game=? AND mod_id=?", (game, mod_id)
    ).fetchone()
    if not row:
        console.print(f"[red]Mod {mod_id} is not tracked for '{game}'.[/red]")
        sys.exit(1)

    snaps = _list_snapshots(game, mod_id)
    if not snaps:
        console.print(
            f"[yellow]No snapshots cached for {row['name']}.[/yellow]\n"
            "Snapshots are saved automatically after each install/update — "
            "this mod was likely installed before snapshot caching existed."
        )
        sys.exit(1)

    console.print(f"[bold]Snapshots for {row['name']} (mod {mod_id}):[/bold]")
    for snap in snaps:
        ts = datetime.fromtimestamp(snap.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        label = _snapshot_version_label(snap)
        marker = " [green](current)[/green]" if label == _safe_filename_part(row["version"] or "") else ""
        console.print(f"  [cyan]{label}[/cyan]  [dim]{ts}, "
                      f"{snap.stat().st_size // 1024} KB[/dim]{marker}")

    if list_only:
        return

    # Pick target
    if version:
        target = next((s for s in snaps if _snapshot_version_label(s) == version), None)
        if not target:
            console.print(f"\n[red]Version {version} not found in snapshots.[/red]")
            sys.exit(1)
    else:
        current_norm = _safe_filename_part(row["version"] or "")
        non_current = [s for s in snaps if _snapshot_version_label(s) != current_norm]
        if not non_current:
            console.print(
                f"\n[yellow]No prior snapshot to roll back to "
                f"(only the current version is cached).[/yellow]"
            )
            sys.exit(1)
        target = non_current[0]

    target_label = _snapshot_version_label(target)
    console.print(f"\n[bold]Will restore:[/bold] {target_label}")

    mod_dir = Path(row["mod_dir"])
    if row["folder_name"]:
        existing = mod_dir / row["folder_name"]
        if existing.exists():
            console.print(f"  Will first remove [dim]{existing}[/dim] for clean restore")

    if not yes and not click.confirm("Proceed?", default=False):
        console.print("[dim]Aborted.[/dim]")
        sys.exit(0)

    # Best-effort clean-out of existing folder so leftover files from the
    # newer version don't shadow the older version's files.
    if row["folder_name"]:
        existing = mod_dir / row["folder_name"]
        if existing.exists():
            shutil.rmtree(existing)
            log.info("Removed existing folder for rollback: %s", existing)

    extract_archive(target, mod_dir)
    db.execute(
        "UPDATE mods SET version=?, updated_at=? WHERE game=? AND mod_id=?",
        (target_label, now_iso(), game, mod_id),
    )
    db.commit()
    record(db, "rollback", game, mod_id, row["name"], target_label, "ok")
    console.print(f"[green]✓ Rolled back to {target_label}[/green]")


# nxm:// link handler ─────────────────────────────────────────────────────────

NXM_DESKTOP_FILE = """[Desktop Entry]
Type=Application
Name=nexmod NXM Handler
Comment=Handle nxm:// download links from Nexus Mods
Exec={nexmod_bin} nxm %u
Icon=applications-internet
Terminal=true
NoDisplay=true
MimeType=x-scheme-handler/nxm;
"""

NXM_DESKTOP_PATH = Path.home() / ".local/share/applications/nexmod-nxm.desktop"


def _parse_nxm_uri(uri: str) -> dict:
    """Parse nxm://<domain>/mods/<mod_id>/files/<file_id>?key=…&expires=…&user_id=… .

    Raises ValueError on malformed URIs. The query-string params are forwarded
    to the Nexus API for free-user downloads but are unused for Premium.
    """
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(uri)
    if parsed.scheme != "nxm":
        raise ValueError(f"not an nxm:// URI (scheme={parsed.scheme!r})")
    domain = parsed.netloc
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 4 or parts[0] != "mods" or parts[2] != "files":
        raise ValueError(f"unexpected NXM path: {parsed.path!r}")
    try:
        mod_id = int(parts[1])
        file_id = int(parts[3])
    except ValueError as e:
        raise ValueError(
            f"mod_id/file_id not integers: {parts[1]!r}, {parts[3]!r}"
        ) from e
    qs = parse_qs(parsed.query)
    return {
        "domain":  domain,
        "mod_id":  mod_id,
        "file_id": file_id,
        "key":     qs.get("key",     [None])[0],
        "expires": qs.get("expires", [None])[0],
        "user_id": qs.get("user_id", [None])[0],
    }


@cli.command("nxm")
@click.argument("uri")
@click.option("--no-reorder", is_flag=True,
              help="Skip automatic load order sort after install")
def nxm_handle(uri, no_reorder):
    """Handle an nxm:// URI (Nexus 'Mod Manager Download' button).

    Usually invoked by the system handler after running
    [cyan]nexmod nxm-register[/cyan]. You can also paste a URI manually.
    """
    try:
        parsed = _parse_nxm_uri(uri)
    except ValueError as e:
        console.print(f"[red]Invalid NXM URI:[/red] {e}")
        sys.exit(1)

    domain_to_game = {info["domain"]: slug for slug, info in GAMES.items()}
    game = domain_to_game.get(parsed["domain"], parsed["domain"])
    if game == parsed["domain"] and parsed["domain"] not in GAMES:
        console.print(
            f"[yellow]Unknown game domain[/yellow] [cyan]{parsed['domain']}[/cyan] — "
            f"will try as game slug. Set mod dir with: "
            f"nexmod path set {parsed['domain']} /path/to/mods"
        )

    console.print(
        f"[bold]NXM:[/bold] {parsed['domain']}/mods/{parsed['mod_id']}/files/{parsed['file_id']}"
    )

    api_key = get_api_key()
    db = get_db()
    try:
        name, version = do_install(
            game, parsed["mod_id"], parsed["file_id"], api_key, db,
            nxm_key=parsed["key"], nxm_expires=parsed["expires"], nxm_user_id=parsed["user_id"],
        )
    except RuntimeError as e:
        msg = str(e)
        if "no download urls" in msg.lower() or "premium required" in msg.lower():
            # Free users cannot get API download URLs even with a valid NXM key.
            # Open the mod's files page so the user can download manually.
            info = GAMES.get(game, {})
            domain = info.get("domain", parsed["domain"])
            mod_page = (
                f"https://www.nexusmods.com/{domain}/mods/{parsed['mod_id']}?tab=files"
            )
            console.print(
                f"\n[yellow]This download requires a Nexus Premium account.[/yellow]\n"
                f"Opening mod files page in your browser so you can download manually.\n"
                f"Then install with:\n"
                f"  [cyan]nexmod import {game} <path-to-downloaded-file>[/cyan]"
            )
            webbrowser.open(mod_page)
            sys.exit(0)
        raise
    console.print(f"\n[green]✓ Installed:[/green] {name} v{version}")


@cli.command("nxm-register")
def nxm_register():
    """Register nexmod as the system handler for nxm:// links.

    Writes a .desktop file to ~/.local/share/applications/, updates the MIME
    database, and sets nexmod as the default handler for x-scheme-handler/nxm.
    After this, the 'Mod Manager Download' button on Nexus mod pages launches
    nexmod automatically.
    """
    nexmod_bin = shutil.which("nexmod") or sys.argv[0]
    if not Path(nexmod_bin).is_absolute():
        console.print(
            f"[yellow]Warning:[/yellow] nexmod binary path is not absolute "
            f"({nexmod_bin}). The handler may break if your shell PATH changes."
        )

    NXM_DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    NXM_DESKTOP_PATH.write_text(NXM_DESKTOP_FILE.format(nexmod_bin=nexmod_bin))
    console.print(f"[green]✓[/green] Wrote {NXM_DESKTOP_PATH}")

    # update-desktop-database picks up the new MimeType= entry
    try:
        subprocess.run(
            ["update-desktop-database", str(NXM_DESKTOP_PATH.parent)],
            check=True, capture_output=True,
        )
        console.print("[green]✓[/green] Updated desktop database")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        console.print(f"[yellow]Could not update desktop database: {e}[/yellow]")
        console.print("  Install [cyan]desktop-file-utils[/cyan] for full integration.")

    # xdg-mime sets us as default for the scheme
    try:
        subprocess.run(
            ["xdg-mime", "default", "nexmod-nxm.desktop", "x-scheme-handler/nxm"],
            check=True, capture_output=True,
        )
        console.print("[green]✓[/green] Set as default handler for nxm:// links")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        console.print(f"[yellow]Could not set default handler: {e}[/yellow]")
        console.print(
            "  Run manually: [cyan]xdg-mime default nexmod-nxm.desktop "
            "x-scheme-handler/nxm[/cyan]"
        )

    # Flatpak Steam / Flatpak browser warning: XDG data dirs are isolated,
    # so the browser inside Flatpak won't dispatch to the system nexmod handler.
    flatpak_steam = Path.home() / ".var/app/com.valvesoftware.Steam"
    if flatpak_steam.exists():
        console.print(
            "\n[yellow]Warning:[/yellow] Flatpak Steam detected. NXM links may not work in "
            "Flatpak browsers because Flatpak apps have isolated XDG data dirs.\n"
            "  If clicking NXM links does nothing, open a terminal and run:\n"
            "    [cyan]nexmod nxm <paste-the-nxm-url>[/cyan]\n"
            "  Or use a native (non-Flatpak) browser."
        )

    console.print(
        "\n[bold]Done.[/bold] Click 'Mod Manager Download' on any Nexus mod "
        "page to test."
    )


@cli.command("nxm-unregister")
def nxm_unregister():
    """Remove the nexmod NXM handler registration."""
    if NXM_DESKTOP_PATH.exists():
        NXM_DESKTOP_PATH.unlink()
        console.print(f"[green]✓[/green] Removed {NXM_DESKTOP_PATH}")
        try:
            subprocess.run(
                ["update-desktop-database", str(NXM_DESKTOP_PATH.parent)],
                check=True, capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    else:
        console.print(f"[yellow]No registration found at {NXM_DESKTOP_PATH}[/yellow]")


# enable / disable / toggle ───────────────────────────────────────────────────

_DTKIT_NATIVE_URL = (
    "https://github.com/ManShanko/dtkit-patch/releases/latest/download/"
    "dtkit-patch-x86_64-unknown-linux-musl.tar.gz"
)


def _find_dtkit(game_dir: Path) -> Path | None:
    """Return the dtkit-patch binary path, preferring the native Linux binary."""
    # Native Linux binary takes priority; .exe is the legacy Wine fallback.
    for name in ("dtkit-patch", "dtkit-patch.exe"):
        p = game_dir / "tools" / name
        if p.exists():
            return p
    return None


def _download_dtkit(game_dir: Path) -> Path:
    """Download the native Linux dtkit-patch binary into <game_dir>/tools/.

    Fetches the musl-static release tarball from GitHub, extracts the binary,
    marks it executable, and returns its path.

    Raises:
        RuntimeError: if the download, extraction, or binary-not-found step fails.
    """
    tools_dir = game_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    dest = tools_dir / "dtkit-patch"

    console.print(f"  Downloading dtkit-patch (native Linux binary)...")
    try:
        r = requests.get(_DTKIT_NATIVE_URL, stream=True, timeout=60)
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to download dtkit-patch: {e}") from e

    data = r.content
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            # Find the binary member — could be at root or in a subdirectory.
            member = None
            for m in tf.getmembers():
                if m.name.split("/")[-1] in ("dtkit-patch", "dtkit-patch-x86_64-unknown-linux-musl"):
                    member = m
                    break
            if not member:
                raise RuntimeError(
                    "dtkit-patch binary not found inside downloaded tarball. "
                    "Download it manually from https://github.com/ManShanko/dtkit-patch/releases"
                )
            fobj = tf.extractfile(member)
            if fobj is None:
                raise RuntimeError("Could not read binary from tarball")
            dest.write_bytes(fobj.read())
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to extract dtkit-patch: {e}") from e

    dest.chmod(0o755)
    console.print(f"  [green]✓[/green] dtkit-patch installed at {dest}")
    log.info("dtkit-patch downloaded and installed at %s", dest)
    return dest


def _run_dtkit(game_dir: Path, action: str) -> tuple[bool, str]:
    """Run dtkit-patch. Uses the native Linux binary if present; falls back to Wine + .exe.

    action: --patch | --unpatch | --toggle
    """
    dtkit = _find_dtkit(game_dir)
    if not dtkit:
        return False, (
            "dtkit-patch not found in {}/tools/.\n"
            "Run: nexmod setup --game darktide  (auto-downloads the native binary)\n"
            "Or download manually from "
            "https://github.com/ManShanko/dtkit-patch/releases"
        ).format(game_dir)

    is_native = dtkit.suffix != ".exe"

    if is_native:
        bundle_dir = str(game_dir / "bundle")
        cmd = [str(dtkit), action, bundle_dir]
        result = subprocess.run(cmd, capture_output=True, text=True)
    else:
        # Legacy Wine path for users who still have dtkit-patch.exe.
        if not shutil.which("wine"):
            return False, (
                "dtkit-patch.exe found but Wine is not installed.\n"
                "Either install Wine or replace dtkit-patch.exe with the native "
                "Linux binary — run: nexmod setup --game darktide"
            )
        bundle_win = f"Z:{game_dir}/bundle"
        env = {**os.environ, "WINEPREFIX": str(WINE_PREFIX), "WINEDEBUG": "-all"}
        result = subprocess.run(
            ["wine", str(dtkit), action, bundle_win],
            capture_output=True, text=True, env=env,
        )

    output = (result.stdout + result.stderr).strip()
    # filter radv / Wine debug noise
    clean = "\n".join(l for l in output.splitlines() if "radv" not in l.lower())
    log.debug("dtkit exit=%s output=%r", result.returncode, clean)
    return result.returncode == 0, clean

@cli.command("enable")
@click.argument("game")
def enable_mods(game):
    """Patch the game bundle to enable mod loading (runs dtkit-patch via Wine)."""
    if game != "darktide":
        console.print(f"[yellow]{game} does not use dtkit-patch — enable/disable/toggle is Darktide-only.[/yellow]")
        sys.exit(1)
    db       = get_db()
    mod_dir  = resolve_mod_dir(game, db)
    game_dir = mod_dir.parent
    console.print(f"Enabling mods for [bold]{game}[/bold]...")
    ok, output = _run_dtkit(game_dir, "--patch")
    if output:
        console.print(f"[dim]{output}[/dim]")
    if ok or "already" in output.lower() or "patched" in output.lower():
        log.info("Mods enabled for %s", game)
        console.print("[green]✓ Mods enabled. Launch the game and they should load.[/green]")
    else:
        log.error("dtkit-patch failed: %s", output)
        console.print(f"[red]✗ Failed.[/red]")

@cli.command("disable")
@click.argument("game")
def disable_mods(game):
    """Unpatch the game bundle to disable mod loading."""
    if game != "darktide":
        console.print(f"[yellow]{game} does not use dtkit-patch — enable/disable/toggle is Darktide-only.[/yellow]")
        sys.exit(1)
    db       = get_db()
    mod_dir  = resolve_mod_dir(game, db)
    game_dir = mod_dir.parent
    console.print(f"Disabling mods for [bold]{game}[/bold]...")
    ok, output = _run_dtkit(game_dir, "--unpatch")
    if output:
        console.print(f"[dim]{output}[/dim]")
    if ok or "unpatch" in output.lower():
        log.info("Mods disabled for %s", game)
        console.print("[green]✓ Mods disabled.[/green]")
    else:
        log.error("dtkit-patch failed: %s", output)
        console.print(f"[red]✗ Failed.[/red]")

@cli.command("toggle")
@click.argument("game")
def toggle_mods(game):
    """Toggle mod loading on/off (patch ↔ unpatch)."""
    if game != "darktide":
        console.print(f"[yellow]{game} does not use dtkit-patch — enable/disable/toggle is Darktide-only.[/yellow]")
        sys.exit(1)
    db       = get_db()
    mod_dir  = resolve_mod_dir(game, db)
    game_dir = mod_dir.parent
    console.print(f"Toggling mods for [bold]{game}[/bold]...")
    ok, output = _run_dtkit(game_dir, "--toggle")
    if output:
        console.print(f"[dim]{output}[/dim]")
    if ok:
        log.info("Mods toggled for %s: %s", game, output)
        console.print("[green]✓ Done.[/green]")
    else:
        log.error("dtkit-patch toggle failed: %s", output)
        console.print(f"[red]✗ Failed.[/red]")


@cli.command("mcp-server")
def mcp_server():
    """Start the NexMod MCP server (requires: pip install nexmod[mcp])."""
    try:
        from nexmod_mcp import main as _mcp_main
    except ImportError:
        console.print(
            "[red]MCP dependencies not installed.[/red]\n"
            r"Install them: pip install nexmod\[mcp]" + "\n"
            "Or:          pip install mcp"
        )
        sys.exit(1)
    _mcp_main()


if __name__ == "__main__":
    cli()
