#!/usr/bin/env python3
"""nexmod — Nexus Mods CLI for Linux (requires Nexus Premium)"""

import click
import requests
import sqlite3
import json
import zipfile
import tarfile
import shutil
import sys
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

CONFIG_DIR  = Path.home() / ".config" / "nexmod"
DATA_DIR    = Path.home() / ".local" / "share" / "nexmod"
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE     = DATA_DIR / "mods.db"
LOG_FILE    = DATA_DIR / "nexmod.log"

NEXUS_API = "https://api.nexusmods.com/v1"

GAMES = {
    "darktide": {
        "name": "Warhammer 40,000: Darktide",
        "domain": "warhammer40kdarktide",
        "steam_id": 1361210,
        "mod_subdir": "mods",
        "log_subpath": "Fatshark/Darktide/console_log.txt",
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


# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
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
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path("/usr/local/share/Steam"),
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
    proton_base = Path.home() / ".local/share/Steam/steamapps/compatdata" / str(steam_id)
    candidate = proton_base / "pfx/drive_c/users/steamuser/AppData/Roaming"
    if candidate.exists():
        return candidate
    # Some installs use ~/.steam/steam
    proton_base2 = Path.home() / ".steam/steam/steamapps/compatdata" / str(steam_id)
    candidate2 = proton_base2 / "pfx/drive_c/users/steamuser/AppData/Roaming"
    if candidate2.exists():
        return candidate2
    return None


# ── Download & Extract ────────────────────────────────────────────────────────

def download_file(url: str, dest: Path) -> Path:
    log.debug("Downloading %s → %s", url, dest)
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
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                        progress.advance(task, len(chunk))
    except Exception as e:
        dest.unlink(missing_ok=True)
        log.error("Download failed for %s: %s", url, e)
        raise RuntimeError(f"Download failed: {e}") from e

    actual_size = dest.stat().st_size
    if total and actual_size != total:
        dest.unlink(missing_ok=True)
        msg = f"Size mismatch: expected {total} bytes, got {actual_size}"
        log.error(msg)
        raise RuntimeError(msg)

    log.debug("Downloaded %s (%d bytes)", dest.name, actual_size)
    return dest

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
                zf.extractall(target_dir)
        elif any(name.endswith(ext) for ext in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
            with tarfile.open(archive) as tf:
                tf.extractall(target_dir)
        elif name.endswith(".7z"):
            import subprocess
            result = subprocess.run(
                ["7z", "x", str(archive), f"-o{target_dir}", "-y"],
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
    return max(valid, key=lambda f: f.get("uploaded_timestamp", 0)) if valid else None


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

    download_url = urls[0]["URI"]
    mod_dir = resolve_mod_dir(game, db)
    tmp = DATA_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    archive = tmp / chosen["file_name"]

    try:
        download_file(download_url, archive)
        console.print(f"  Extracting to [dim]{mod_dir}[/dim]...")
        extract_archive(archive, mod_dir)
    except Exception as e:
        archive.unlink(missing_ok=True)
        record(db, "install", game, mod_id, mod["name"], mod.get("version"), "fail", str(e))
        raise
    finally:
        archive.unlink(missing_ok=True)

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
        console.print(f"api_key: {k[:8]}...{k[-4:]}")
    else:
        console.print("[yellow]No API key configured.[/yellow]")
    console.print(f"Log file:  {LOG_FILE}")
    console.print(f"Database:  {DB_FILE}")

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
        import subprocess
        args = ["tail", "-f", str(LOG_FILE)]
        if errors:
            subprocess.run(f"tail -f {LOG_FILE} | grep -E 'ERROR|WARNING'", shell=True)
        else:
            subprocess.run(args)
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
            record(db, "scan", game, mod_id, mod["name"], mod.get("version"), "ok")
            console.print(f"  [green]✓[/green] [{mod_id}] {mod['name']} v{mod.get('version','?')}")
            ok += 1
        except Exception as e:
            record(db, "scan", game, mod_id, vortex_name, None, "fail", str(e))
            console.print(f"  [red]✗[/red] [{mod_id}] {vortex_name}: {e}")
            fail += 1

    console.print(f"\n[bold]Done.[/bold] {ok} tracked, {fail} failed.")


# install ─────────────────────────────────────────────────────────────────────

@cli.command("install")
@click.argument("game")
@click.argument("mod_id", type=int)
@click.option("--file-id", type=int, default=None, help="Force a specific file ID")
def install(game, mod_id, file_id):
    """Download and install a mod. Starts tracking it for updates."""
    api_key = get_api_key()
    db = get_db()
    name, version = do_install(game, mod_id, file_id, api_key, db)
    console.print(f"\n[green]✓ Installed:[/green] {name} v{version}")


# track ───────────────────────────────────────────────────────────────────────

@cli.command("track")
@click.argument("game")
@click.argument("mod_id", type=int)
def track(game, mod_id):
    """Track an already-installed mod so nexmod can check it for updates."""
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

    with console.status("Fetching recently-updated mod list..."):
        try:
            updated_ids = {m["mod_id"] for m in api_updated_mods(domain, api_key)}
            log.debug("Updated mods this week: %d", len(updated_ids))
        except Exception as e:
            log.warning("Could not fetch updated list, falling back to per-mod: %s", e)
            updated_ids = set()

    t = Table(title=f"Update check — {game}", show_lines=False)
    t.add_column("Name")
    t.add_column("Installed", style="dim")
    t.add_column("Latest", style="cyan")
    t.add_column("Status")

    for r in rows:
        if updated_ids and r["mod_id"] not in updated_ids:
            t.add_row(r["name"], r["version"] or "?", r["version"] or "?", "[green]Current[/green]")
            continue
        try:
            mod    = api_mod_info(domain, r["mod_id"], api_key)
            latest = mod.get("version", "?")
            cur    = r["version"] or "?"
            status = "[green]Current[/green]" if latest == cur else f"[yellow]Update → {latest}[/yellow]"
            t.add_row(r["name"], cur, latest, status)
        except Exception as e:
            log.error("Check failed for mod_id=%s: %s", r["mod_id"], e)
            t.add_row(r["name"], r["version"] or "?", "?", f"[red]error: {e}[/red]")

    console.print(t)


# update ──────────────────────────────────────────────────────────────────────

@cli.command("update")
@click.argument("game")
@click.option("--mod-id", type=int, default=None, help="Update only this mod ID")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
def update_mods(game, mod_id, yes):
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

    with console.status("Fetching recently-updated mod list..."):
        try:
            updated_ids = {m["mod_id"] for m in api_updated_mods(domain, api_key)}
        except Exception as e:
            log.warning("Could not fetch updated list: %s", e)
            updated_ids = set()

    updated_count = 0
    for r in rows:
        if updated_ids and r["mod_id"] not in updated_ids:
            console.print(f"[dim]{r['name']}: current[/dim]")
            continue

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

        download_url = urls[0]["URI"]
        mod_dir      = resolve_mod_dir(game, db)
        tmp          = DATA_DIR / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        archive = tmp / chosen["file_name"]

        try:
            download_file(download_url, archive)
            console.print(f"  Extracting to [dim]{mod_dir}[/dim]...")
            extract_archive(archive, mod_dir)
        except Exception as e:
            archive.unlink(missing_ok=True)
            record(db, "update", game, r["mod_id"], r["name"], latest, "fail", str(e))
            console.print(f"  [red]Failed: {e}[/red]")
            continue
        finally:
            archive.unlink(missing_ok=True)

        db.execute(
            "UPDATE mods SET file_id=?, version=?, filename=?, updated_at=? WHERE game=? AND mod_id=?",
            (chosen["file_id"], latest, chosen["file_name"], now_iso(), game, r["mod_id"]),
        )
        db.commit()
        record(db, "update", game, r["mod_id"], r["name"], latest, "ok")
        console.print(f"  [green]✓ Updated to {latest}[/green]")
        updated_count += 1

    console.print(f"\n[bold]Done.[/bold] {updated_count} mod(s) updated.")


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


if __name__ == "__main__":
    cli()
