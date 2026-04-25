#!/usr/bin/env python3
"""nexmod — Nexus Mods CLI for Linux (requires Nexus Premium)"""

import click
import requests
import sqlite3
import json
import os
import subprocess
import zipfile
import tarfile
import shutil
import sys
import hashlib
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, DownloadColumn, TransferSpeedColumn, BarColumn, TextColumn
from rich.syntax import Syntax

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
    """)
    conn.commit()
    return conn

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

def nexus_get(endpoint: str, api_key: str) -> dict | list:
    url = f"{NEXUS_API}/{endpoint}"
    log.debug("GET %s", url)
    try:
        r = requests.get(
            url,
            headers={"apikey": api_key, "accept": "application/json"},
            timeout=20,
        )
    except requests.exceptions.Timeout:
        log.error("Timeout fetching %s", url)
        console.print(f"[red]Timeout fetching {endpoint}[/red]")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        log.error("Connection error: %s", e)
        console.print(f"[red]Connection error: {e}[/red]")
        sys.exit(1)

    log.debug("→ HTTP %s  remaining=%s",
              r.status_code, r.headers.get("X-RL-Daily-Remaining", "?"))

    if r.status_code == 429:
        reset = r.headers.get("Retry-After", "unknown")
        log.warning("Rate limited. Retry-After: %s", reset)
        console.print(f"[yellow]Rate limited — retry after {reset}s[/yellow]")
        sys.exit(1)
    if r.status_code == 403:
        log.error("403 Forbidden on %s: %s", url, r.text[:200])
        console.print("[red]403 Forbidden — confirm your account has Nexus Premium.[/red]")
        sys.exit(1)
    if not r.ok:
        snippet = r.text[:300]
        log.error("HTTP %s on %s: %s", r.status_code, url, snippet)
        console.print(f"[red]HTTP {r.status_code} from Nexus API[/red]")
        console.print(f"[dim]{snippet}[/dim]")
        r.raise_for_status()

    return r.json()

def api_mod_info(domain: str, mod_id: int, api_key: str) -> dict:
    return nexus_get(f"games/{domain}/mods/{mod_id}.json", api_key)

def api_mod_files(domain: str, mod_id: int, api_key: str) -> list:
    data = nexus_get(f"games/{domain}/mods/{mod_id}/files.json", api_key)
    return data.get("files", [])

def api_download_urls(domain: str, mod_id: int, file_id: int, api_key: str) -> list:
    return nexus_get(
        f"games/{domain}/mods/{mod_id}/files/{file_id}/download_link.json",
        api_key,
    )

def api_updated_mods(domain: str, api_key: str, period: str = "1w") -> list:
    return nexus_get(f"games/{domain}/mods/updated.json?period={period}", api_key)


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


# ── Download & Extract ────────────────────────────────────────────────────────

def download_file(url: str, dest: Path) -> Path:
    # Atomic: write to <dest>.part, rename on success. A killed/crashed
    # process never leaves a half-written archive at the canonical path.
    log.debug("Downloading %s → %s", url, dest)
    part = dest.with_suffix(dest.suffix + ".part")
    part.unlink(missing_ok=True)
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            with Progress(
                TextColumn("[bold cyan]{task.fields[filename]}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
            ) as progress:
                task = progress.add_task("dl", filename=dest.name, total=total or None)
                with open(part, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                        progress.advance(task, len(chunk))
    except Exception as e:
        part.unlink(missing_ok=True)
        log.error("Download failed for %s: %s", url, e)
        raise RuntimeError(f"Download failed: {e}") from e

    actual_size = part.stat().st_size
    if total and actual_size != total:
        part.unlink(missing_ok=True)
        msg = f"Size mismatch: expected {total} bytes, got {actual_size}"
        log.error(msg)
        raise RuntimeError(msg)

    part.replace(dest)
    log.debug("Downloaded %s (%d bytes)", dest.name, actual_size)
    return dest


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
        console.print(f"[red]Unknown game '{game}'. Run 'nexmod games' to list supported games.[/red]")
        console.print(f"Or set a custom path: nexmod path set {game} /path/to/mod/dir")
        sys.exit(1)

    game_path = find_game_install(info["steam_id"])
    if game_path:
        mod_dir = game_path / info["mod_subdir"]
        db.execute("INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)", (game, str(mod_dir)))
        db.commit()
        console.print(f"[dim]Auto-detected: {mod_dir}[/dim]")
        return mod_dir

    console.print(f"[yellow]Could not find {info['name']} Steam install.[/yellow]")
    console.print(f"Set it manually: nexmod path set {game} /path/to/{info['mod_subdir']}")
    sys.exit(1)


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

        console.print(
            f"\n[yellow]{declaring_mod}[/yellow] requires "
            f"[bold]{dep}[/bold] which is not installed."
        )
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
            dep_name, dep_ver = do_install(dep_game, dep_mod_id, dep_file_id, api_key, db)
            console.print(f"  [green]✓ Installed dependency:[/green] {dep_name} v{dep_ver}")
            any_installed = True
        except SystemExit:
            pass  # parse_nexus_url already printed the error
        except Exception as e:
            log.error("Failed to install dep %s: %s", dep, e)
            console.print(f"  [red]Failed to install {dep}: {e}[/red]")

    return any_installed


# ── Install Helper ────────────────────────────────────────────────────────────

def do_install(game: str, mod_id: int, file_id_override: int | None,
               api_key: str, db: sqlite3.Connection):
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

    console.print(f"  File: [cyan]{chosen['file_name']}[/cyan] ({chosen.get('size_kb', '?')} KB)")

    with console.status("Getting CDN download link..."):
        urls = api_download_urls(domain, mod_id, chosen["file_id"], api_key)

    if not urls:
        raise RuntimeError("Nexus returned no download URLs — check your Premium status")
    download_url = urls[0].get("URI") or urls[0].get("url")
    if not download_url:
        raise RuntimeError(f"Unexpected URL format from Nexus API: {urls[0]}")
    mod_dir = resolve_mod_dir(game, db)
    tmp = DATA_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    archive = tmp / chosen["file_name"]

    dirs_before = {p.name for p in mod_dir.iterdir() if p.is_dir()} if mod_dir.exists() else set()

    extraction_ok = False
    try:
        download_file(download_url, archive)
        if chosen.get("md5"):
            verify_md5(archive, chosen["md5"])
        console.print(f"  Extracting to [dim]{mod_dir}[/dim]...")
        extract_archive(archive, mod_dir)
        extraction_ok = True
    except Exception as e:
        record(db, "install", game, mod_id, mod["name"], mod.get("version"), "fail", str(e))
        raise
    finally:
        archive.unlink(missing_ok=True)

    if not extraction_ok:
        raise RuntimeError("Extraction did not complete — DB not written")

    load_order_file = (info or {}).get("load_order_file")
    if load_order_file:
        new_dirs = sorted({p.name for p in mod_dir.iterdir() if p.is_dir()} - dirs_before)
        added = _ensure_load_order(mod_dir, load_order_file, new_dirs)
        if added:
            console.print(f"  [dim]mod_load_order.txt ← {', '.join(added)}[/dim]")
        elif new_dirs:
            console.print(f"  [dim]mod_load_order.txt: {', '.join(new_dirs)} already listed[/dim]")

    db.execute("""
        INSERT OR REPLACE INTO mods
            (game, mod_id, file_id, name, version, filename, mod_dir, tracked_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        game, mod_id, chosen["file_id"], mod["name"], mod.get("version"),
        chosen["file_name"], str(mod_dir), now_iso(), now_iso(),
    ))
    db.commit()
    record(db, "install", game, mod_id, mod["name"], mod.get("version"), "ok")
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


def _write_profile(game: str, name: str, load_order: list[str], description: str = "") -> Path:
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
    }
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


def _apply_profile(mod_dir: Path, load_order_file: str, load_order: list[str]):
    """Write a profile's load order list to mod_load_order.txt."""
    lof = mod_dir / load_order_file
    header = ["-- File managed by nexmod"]
    lof.write_text("\n".join(header + load_order) + "\n")


_ARCHIVE_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip", ".7z", ".tar", ".rar")


def _archive_basename(filename: str) -> str:
    """Strip a known archive extension. Falls back to single-suffix split."""
    lower = filename.lower()
    for ext in _ARCHIVE_EXTS:
        if lower.endswith(ext):
            return filename[: -len(ext)]
    return filename.rsplit(".", 1)[0]


def _read_lof_folders(mod_dir: Path, load_order_file: str) -> list[str]:
    lof = mod_dir / load_order_file
    if not lof.exists():
        return []
    return [l.strip() for l in lof.read_text().splitlines()
            if l.strip() and not l.strip().startswith("--")]


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Print debug output to terminal (always logged to file).")
@click.pass_context
def cli(ctx, verbose):
    """nexmod — download and update Nexus mods on Linux (Premium required)"""
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
        console.print(f"Premium:   {'[green]YES[/green]' if premium else '[red]NO — downloads will fail[/red]'}")
        console.print(f"Supporter: {'yes' if data.get('is_supporter') else 'no'}")
        if not premium:
            console.print("[yellow]Warning: download commands require Premium.[/yellow]")
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
@click.argument("name")
@click.option("--description", "-d", default="", help="Short description stored with the profile")
@click.option("--force", "-f", is_flag=True, help="Overwrite without prompting if profile exists")
def profile_save(game, name, description, force):
    """Snapshot the current load order as a named profile.

    \b
    Examples:
      nexmod profile save darktide full
      nexmod profile save darktide minimal -d "QoL only, no combat changes"
    """
    info = GAMES.get(game)
    if not info or not info.get("load_order_file"):
        console.print(f"[yellow]{game} does not support a managed load order.[/yellow]")
        return

    db      = get_db()
    mod_dir = resolve_mod_dir(game, db)
    folders = _read_lof_folders(mod_dir, info["load_order_file"])

    if not folders:
        console.print(f"[yellow]Load order file is empty or missing for {game}.[/yellow]")
        return

    path = _profile_path(game, name)
    if path.exists() and not force:
        if not click.confirm(f"Profile '{name}' already exists. Overwrite?", default=False):
            console.print("[dim]Aborted.[/dim]")
            return

    _write_profile(game, name, folders, description)
    log.info("Profile '%s' saved for %s (%d mods)", name, game, len(folders))
    console.print(f"[green]✓ Profile saved:[/green] {name} ({len(folders)} mods)")


@profile.command("list")
@click.argument("game")
def profile_list(game):
    """List all saved profiles for GAME."""
    profiles = _list_profiles(game)
    if not profiles:
        console.print(f"[yellow]No profiles saved for {game}.[/yellow]")
        console.print(f"Create one: nexmod profile save {game} <name>")
        return

    t = Table(title=f"Profiles — {game}", show_lines=False)
    t.add_column("Name",        style="bold cyan")
    t.add_column("Mods",        justify="right", style="dim")
    t.add_column("Updated",     style="dim")
    t.add_column("Description")
    for p in profiles:
        t.add_row(
            p["name"],
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
@click.argument("name")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing anything")
@click.option("--install", "do_install_missing", is_flag=True,
              help="Install profile mods not on disk (uses tracked mod_id from the DB).")
def profile_load(game, name, dry_run, do_install_missing):
    """Apply a profile — rewrites the load order file with the profile's mod list.

    Mods not in the profile stay on disk but won't be loaded by the game.

    \b
    Examples:
      nexmod profile load darktide minimal
      nexmod profile load darktide full --dry-run
      nexmod profile load darktide full --install
    """
    if dry_run and do_install_missing:
        console.print("[red]--dry-run and --install are mutually exclusive.[/red]")
        sys.exit(1)

    info = GAMES.get(game)
    if not info or not info.get("load_order_file"):
        console.print(f"[yellow]{game} does not support a managed load order.[/yellow]")
        return

    p         = _read_profile(game, name)
    db        = get_db()
    mod_dir   = resolve_mod_dir(game, db)
    lof_file  = info["load_order_file"]

    profile_order = p.get("load_order", [])
    current_order = _read_lof_folders(mod_dir, lof_file)

    profile_set  = set(profile_order)
    current_set  = set(current_order)
    added        = [m for m in profile_order if m not in current_set]
    removed      = [m for m in current_order  if m not in profile_set]
    unchanged    = len(profile_order) - len(added)

    # Warn about profile mods not on disk
    missing_on_disk = [m for m in profile_order if not (mod_dir / m).exists()]

    console.print(f"[bold]Profile:[/bold] {name}  "
                  f"([cyan]{len(profile_order)}[/cyan] mods)")
    if added:
        console.print(f"  [green]+[/green] Enabling:  {', '.join(added)}")
    if removed:
        console.print(f"  [yellow]-[/yellow] Disabling: {', '.join(removed)}")
    if not added and not removed and not missing_on_disk:
        console.print(f"  [dim]Load order already matches this profile — no changes.[/dim]")
        return
    if missing_on_disk:
        console.print(f"\n  [red]Warning:[/red] {len(missing_on_disk)} profile mod(s) not on disk "
                      f"(not installed): {', '.join(missing_on_disk[:5])}"
                      + (" ..." if len(missing_on_disk) > 5 else ""))

    if do_install_missing and missing_on_disk:
        api_key = get_api_key()
        rows    = db.execute(
            "SELECT mod_id, name, filename FROM mods WHERE game = ?", (game,)
        ).fetchall()
        # folder name is the archive basename (extension stripped) — same
        # convention used by 'remove --purge'. Build the lookup once.
        folder_to_mod_id: dict[str, int] = {}
        for r in rows:
            if r["filename"]:
                folder_to_mod_id[_archive_basename(r["filename"])] = r["mod_id"]

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

    if dry_run:
        console.print("\n[dim]Dry run — no changes made.[/dim]")
        return

    _apply_profile(mod_dir, lof_file, profile_order)
    log.info("Profile '%s' applied for %s (%d mods)", name, game, len(profile_order))
    console.print(f"\n[green]✓ Profile '{name}' applied.[/green]")


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
def show_history(game, limit, failures):
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
        console.print("[yellow]No history records.[/yellow]")
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
    db.execute("INSERT OR REPLACE INTO game_paths (game, path) VALUES (?, ?)", (game, mod_dir))
    db.commit()
    log.info("mod dir for %s set to %s", game, mod_dir)
    console.print(f"[green]{game} mod dir → {mod_dir}[/green]")

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
                    (game, mod_id, file_id, name, version, filename, mod_dir, tracked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                game, mod_id,
                chosen["file_id"] if chosen else 0,
                mod["name"], None,
                chosen["file_name"] if chosen else None,
                str(mod_dir), now_iso(),
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


# install ─────────────────────────────────────────────────────────────────────

@cli.command("install")
@click.argument("game")
@click.argument("mod_id", type=int, required=False, default=None)
@click.option("--file-id", type=int, default=None, help="Force a specific file ID")
@click.option("--no-reorder", is_flag=True, help="Skip automatic load order sort after install")
def install(game, mod_id, file_id, no_reorder):
    """Download and install a mod. Starts tracking it for updates.

    \b
    Accepts a Nexus Mods URL or a game slug + mod ID:
      nexmod install https://www.nexusmods.com/warhammer40kdarktide/mods/1234
      nexmod install darktide 1234
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
    name, version = do_install(game, mod_id, file_id, api_key, db)
    console.print(f"\n[green]✓ Installed:[/green] {name} v{version}")

    info = GAMES.get(game)
    if info and info.get("load_order_file") and not no_reorder and get_auto_reorder():
        mod_dir = resolve_mod_dir(game, db)
        lof     = info["load_order_file"]
        result  = reorder_load_order(mod_dir, lof)
        if result["changed"]:
            console.print(f"  [dim]Load order sorted ({len(result['order'])} mods)[/dim]")
        if result["cycles"]:
            console.print(f"  [yellow]Dependency cycle detected: {', '.join(result['cycles'])}[/yellow]")
        if result["missing_deps"]:
            newly = _handle_missing_deps(game, result["missing_deps"], mod_dir, api_key, db)
            if newly:
                result2 = reorder_load_order(mod_dir, lof)
                if result2["changed"]:
                    console.print(f"  [dim]Load order re-sorted after dependency install[/dim]")


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


# list ────────────────────────────────────────────────────────────────────────

@cli.command("list")
@click.argument("game")
def list_mods(game):
    """List tracked mods for GAME."""
    db   = get_db()
    rows = db.execute("SELECT * FROM mods WHERE game = ? ORDER BY name", (game,)).fetchall()
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


# check ───────────────────────────────────────────────────────────────────────

@cli.command("check")
@click.argument("game")
def check_updates(game):
    """Check all tracked mods for available updates (no download)."""
    api_key = get_api_key()
    db      = get_db()
    rows    = db.execute("SELECT * FROM mods WHERE game = ?", (game,)).fetchall()
    if not rows:
        console.print(f"[yellow]No mods tracked for '{game}'.[/yellow]")
        return

    info   = GAMES.get(game, {})
    domain = info.get("domain", game)

    t = Table(title=f"Update check — {game}", show_lines=False)
    t.add_column("Name")
    t.add_column("Installed", style="dim")
    t.add_column("Latest", style="cyan")
    t.add_column("Status")

    for r in rows:
        try:
            with console.status(f"Checking {r['name']}..."):
                mod    = api_mod_info(domain, r["mod_id"], api_key)
            latest = mod.get("version", "?")
            cur    = r["version"] or "?"
            status = "[green]Current[/green]" if latest == cur else f"[yellow]Update → {latest}[/yellow]"
            t.add_row(r["name"], cur, latest, status)
        except Exception as e:
            log.error("Check failed for mod_id=%s: %s", r["mod_id"], e)
            t.add_row(r["name"], r["version"] or "?", "?", f"[red]error: {e}[/red]")

    console.print(t)


# order ───────────────────────────────────────────────────────────────────────

@cli.command("order")
@click.argument("game")
@click.option("--dry-run", is_flag=True, help="Preview new order without writing to disk.")
def order_mods(game, dry_run):
    """Show and (re)sort the load order file by mod dependency declarations.

    Reads each mod's mod.json, builds a dependency graph, and sorts using a
    topological sort (Kahn's algorithm). Mods without mod.json keep their
    relative position. Dependency cycles are detected and placed at the end.

    \b
    Examples:
      nexmod order darktide              # sort and write
      nexmod order darktide --dry-run    # preview only
    """
    info = GAMES.get(game)
    if not info or not info.get("load_order_file"):
        console.print(f"[yellow]{game} does not use a managed load order file.[/yellow]")
        return

    db      = get_db()
    mod_dir = resolve_mod_dir(game, db)
    lof     = info["load_order_file"]

    if not (mod_dir / lof).exists():
        console.print(f"[yellow]No {lof} found in {mod_dir}[/yellow]")
        return

    result  = reorder_load_order(mod_dir, lof, dry_run=dry_run)
    order   = result["order"]
    cycles  = set(result["cycles"])
    dm      = result["deps_map"]

    title = f"Load order — {game}" + (" (dry run, not saved)" if dry_run else "")
    t = Table(title=title, show_lines=False, box=None, padding=(0, 1))
    t.add_column("#",       style="dim",   width=4, justify="right")
    t.add_column("Folder",  style="bold",  min_width=20)
    t.add_column("Depends on",             style="dim")
    t.add_column("",        width=8)

    for i, folder in enumerate(order, 1):
        deps_str  = ", ".join(dm.get(folder, [])) or "—"
        flag      = "[red]CYCLE[/red]" if folder in cycles else ""
        t.add_row(str(i), folder, deps_str, flag)

    console.print(t)

    if result["missing_deps"]:
        console.print("\n[yellow]Missing dependencies (declared but not in load order):[/yellow]")
        for folder, missing in result["missing_deps"].items():
            console.print(f"  {folder} → {', '.join(missing)}")

    if cycles:
        console.print(f"\n[yellow]Dependency cycles ({len(cycles)} mod(s)) placed at end:[/yellow] "
                      + ", ".join(sorted(cycles)))

    if result["changed"]:
        if dry_run:
            console.print("\n[dim]Order would change. Run without --dry-run to apply.[/dim]")
        else:
            console.print(f"\n[green]✓ {lof} reordered ({len(order)} mods).[/green]")
    else:
        console.print("\n[dim]Order is already correct — no changes needed.[/dim]")


# update ──────────────────────────────────────────────────────────────────────

@cli.command("update")
@click.argument("game")
@click.option("--mod-id", type=int, default=None, help="Update only this mod ID")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--no-reorder", is_flag=True, help="Skip automatic load order sort after update")
def update_mods(game, mod_id, yes, no_reorder):
    """Download and apply all available updates for GAME."""
    api_key = get_api_key()
    db      = get_db()

    q = "SELECT * FROM mods WHERE game = ?"
    p: list = [game]
    if mod_id:
        q += " AND mod_id = ?"
        p.append(mod_id)
    rows = db.execute(q, p).fetchall()

    if not rows:
        console.print(f"[yellow]No mods tracked for '{game}'.[/yellow]")
        return

    info   = GAMES.get(game, {})
    domain = info.get("domain", game)

    updated_count = 0
    for r in rows:
        with console.status(f"Checking {r['name']}..."):
            try:
                mod    = api_mod_info(domain, r["mod_id"], api_key)
                latest = mod.get("version", "?")
            except Exception as e:
                log.error("Update check failed for mod_id=%s: %s", r["mod_id"], e)
                console.print(f"[red]{r['name']}: error — {e}[/red]")
                record(db, "update", game, r["mod_id"], r["name"], None, "fail", str(e))
                continue

        if latest == r["version"]:
            console.print(f"[dim]{r['name']}: current ({latest})[/dim]")
            continue

        console.print(f"[cyan]{r['name']}:[/cyan] {r['version']} → {latest}")
        if not yes and not click.confirm("  Download update?", default=True):
            record(db, "update", game, r["mod_id"], r["name"], latest, "skip")
            continue

        with console.status("Getting files..."):
            files  = api_mod_files(domain, r["mod_id"], api_key)
            chosen = pick_main_file(files)

        if not chosen:
            log.error("No main file for update: mod_id=%s", r["mod_id"])
            console.print(f"  [red]Could not determine update file — skipping.[/red]")
            record(db, "update", game, r["mod_id"], r["name"], latest, "fail", "no main file")
            continue

        with console.status("Getting CDN link..."):
            urls = api_download_urls(domain, r["mod_id"], chosen["file_id"], api_key)

        if not urls:
            record(db, "update", game, r["mod_id"], r["name"], latest, "fail", "no download URLs")
            console.print(f"  [red]No download URLs returned — check Premium status.[/red]")
            continue
        download_url = urls[0].get("URI") or urls[0].get("url")
        if not download_url:
            record(db, "update", game, r["mod_id"], r["name"], latest, "fail", f"bad URL format: {urls[0]}")
            console.print(f"  [red]Unexpected URL format from Nexus — skipping.[/red]")
            continue
        mod_dir      = resolve_mod_dir(game, db)
        tmp          = DATA_DIR / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        archive = tmp / chosen["file_name"]

        extraction_ok = False
        try:
            download_file(download_url, archive)
            if chosen.get("md5"):
                verify_md5(archive, chosen["md5"])
            console.print(f"  Extracting to [dim]{mod_dir}[/dim]...")
            extract_archive(archive, mod_dir)
            extraction_ok = True
        except Exception as e:
            record(db, "update", game, r["mod_id"], r["name"], latest, "fail", str(e))
            console.print(f"  [red]Failed: {e}[/red]")
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
        console.print(f"  [green]✓ Updated to {latest}[/green]")
        updated_count += 1

    console.print(f"\n[bold]Done.[/bold] {updated_count} mod(s) updated.")

    info = GAMES.get(game, {})
    if info.get("load_order_file") and updated_count > 0 and not no_reorder and get_auto_reorder():
        mod_dir = resolve_mod_dir(game, db)
        lof     = info["load_order_file"]
        result  = reorder_load_order(mod_dir, lof)
        if result["changed"]:
            console.print(f"[dim]Load order sorted ({len(result['order'])} mods)[/dim]")
        if result["cycles"]:
            console.print(f"[yellow]Dependency cycle detected: {', '.join(result['cycles'])}[/yellow]")
        if result["missing_deps"]:
            newly = _handle_missing_deps(game, result["missing_deps"], mod_dir, api_key, db)
            if newly:
                result2 = reorder_load_order(mod_dir, lof)
                if result2["changed"]:
                    console.print(f"[dim]Load order re-sorted after dependency install[/dim]")


# remove ──────────────────────────────────────────────────────────────────────

@cli.command("remove")
@click.argument("game")
@click.argument("mod_id", type=int)
@click.option("--purge", is_flag=True, help="Also delete mod files from disk")
def remove_mod(game, mod_id, purge):
    """Stop tracking a mod. Use --purge to also delete its files."""
    db  = get_db()
    row = db.execute("SELECT * FROM mods WHERE game=? AND mod_id=?", (game, mod_id)).fetchone()
    if not row:
        console.print(f"[red]Mod {mod_id} is not tracked for '{game}'.[/red]")
        sys.exit(1)

    if purge and row["mod_dir"] and row["filename"]:
        mod_dir     = Path(row["mod_dir"])
        folder_name = row["filename"].rsplit(".", 1)[0]
        folder      = mod_dir / folder_name
        if folder.exists() and folder.is_dir():
            shutil.rmtree(folder)
            log.info("Purged mod folder %s", folder)
            console.print(f"[dim]Deleted {folder}[/dim]")
        else:
            console.print(f"[yellow]Could not find {folder} — remove files manually if needed.[/yellow]")

    db.execute("DELETE FROM mods WHERE game=? AND mod_id=?", (game, mod_id))
    db.commit()
    record(db, "remove", game, mod_id, row["name"], row["version"], "ok")
    console.print(f"[green]Removed:[/green] {row['name']}")


# enable / disable / toggle ───────────────────────────────────────────────────

def _find_dtkit(game_dir: Path) -> Path | None:
    for name in ("dtkit-patch.exe", "dtkit-patch"):
        p = game_dir / "tools" / name
        if p.exists():
            return p
    return None

def _run_dtkit(game_dir: Path, action: str) -> tuple[bool, str]:
    """Run dtkit-patch via Wine. action: --patch | --unpatch | --toggle"""
    if not shutil.which("wine"):
        return False, (
            "Wine not found. Install it:\n"
            "  Ubuntu/Debian: sudo apt install wine\n"
            "  Arch:          sudo pacman -S wine\n"
            "  Fedora:        sudo dnf install wine\n"
            "  openSUSE:      sudo zypper install wine\n"
            "  Flatpak:       flatpak install flathub org.winehq.Wine"
        )
    dtkit = _find_dtkit(game_dir)
    if not dtkit:
        return False, f"dtkit-patch.exe not found in {game_dir}/tools/"

    bundle_win = f"Z:{game_dir}/bundle"
    env = {**os.environ, "WINEPREFIX": str(WINE_PREFIX), "WINEDEBUG": "-all"}
    result = subprocess.run(
        ["wine", str(dtkit), action, bundle_win],
        capture_output=True, text=True, env=env,
    )
    output = (result.stdout + result.stderr).strip()
    # filter radv noise
    clean = "\n".join(l for l in output.splitlines() if "radv" not in l.lower())
    log.debug("dtkit exit=%s output=%r", result.returncode, clean)
    return result.returncode == 0, clean

@cli.command("enable")
@click.argument("game")
def enable_mods(game):
    """Patch the game bundle to enable mod loading (runs dtkit-patch via Wine)."""
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


if __name__ == "__main__":
    cli()
