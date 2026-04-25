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
from pathlib import Path
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, DownloadColumn, TransferSpeedColumn, BarColumn, TextColumn

console = Console()

CONFIG_DIR  = Path.home() / ".config" / "nexmod"
DATA_DIR    = Path.home() / ".local" / "share" / "nexmod"
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE     = DATA_DIR / "mods.db"

NEXUS_API = "https://api.nexusmods.com/v1"

GAMES = {
    "darktide": {
        "name": "Warhammer 40,000: Darktide",
        "domain": "warhammer40kdarktide",
        "steam_id": 1361210,
        "mod_subdir": "mods",
    },
    "skyrimse": {
        "name": "Skyrim Special Edition",
        "domain": "skyrimspecialedition",
        "steam_id": 489830,
        "mod_subdir": "Data",
    },
    "bg3": {
        "name": "Baldur's Gate 3",
        "domain": "baldursgate3",
        "steam_id": 1086940,
        "mod_subdir": "Mods",
    },
    "cyberpunk2077": {
        "name": "Cyberpunk 2077",
        "domain": "cyberpunk2077",
        "steam_id": 1091500,
        "mod_subdir": "archive/pc/mod",
    },
    "fallout4": {
        "name": "Fallout 4",
        "domain": "fallout4",
        "steam_id": 377160,
        "mod_subdir": "Data",
    },
}


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
    """)
    conn.commit()
    return conn


# ── Nexus API ─────────────────────────────────────────────────────────────────

def nexus_get(endpoint: str, api_key: str) -> dict | list:
    r = requests.get(
        f"{NEXUS_API}/{endpoint}",
        headers={"apikey": api_key, "accept": "application/json"},
        timeout=20,
    )
    if r.status_code == 429:
        console.print("[yellow]Rate limited by Nexus API — wait a moment and retry.[/yellow]")
        sys.exit(1)
    if r.status_code == 403:
        console.print("[red]403 Forbidden — confirm your account has Nexus Premium.[/red]")
        sys.exit(1)
    r.raise_for_status()
    return r.json()

def api_mod_info(domain: str, mod_id: int, api_key: str) -> dict:
    return nexus_get(f"games/{domain}/mods/{mod_id}.json", api_key)

def api_mod_files(domain: str, mod_id: int, api_key: str) -> list:
    data = nexus_get(f"games/{domain}/mods/{mod_id}/files.json", api_key)
    return data.get("files", [])

def api_download_urls(domain: str, mod_id: int, file_id: int, api_key: str) -> list:
    # Premium-only endpoint
    return nexus_get(
        f"games/{domain}/mods/{mod_id}/files/{file_id}/download_link.json",
        api_key,
    )

def api_updated_mods(domain: str, api_key: str, period: str = "1w") -> list:
    # Returns list of {mod_id, latest_file_update, latest_mod_activity}
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
                        return game_path
                except IndexError:
                    pass
    return None


# ── Download & Extract ────────────────────────────────────────────────────────

def download_file(url: str, dest: Path) -> Path:
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
    return dest

def extract_archive(archive: Path, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(target_dir)
    elif any(name.endswith(ext) for ext in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
        with tarfile.open(archive) as tf:
            tf.extractall(target_dir)
    elif name.endswith(".7z"):
        # 7z needs system p7zip
        import subprocess
        result = subprocess.run(
            ["7z", "x", str(archive), f"-o{target_dir}", "-y"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"7z failed: {result.stderr}")
    else:
        # Unknown format — copy as-is
        shutil.copy2(archive, target_dir / archive.name)


# ── File Selection ────────────────────────────────────────────────────────────

def pick_main_file(files: list) -> dict | None:
    """Select the primary downloadable file from a mod's file list."""
    for category in ("MAIN", "UPDATE", "MISCELLANEOUS"):
        for f in files:
            cat = (f.get("category_name") or "").upper()
            if cat == category:
                return f
    # Fallback: newest non-archived file
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

def do_install(game: str, mod_id: int, file_id_override: int | None, api_key: str, db: sqlite3.Connection):
    """Download and extract a mod, then record it in the DB. Returns (mod_name, version)."""
    info = GAMES.get(game)
    domain = info["domain"] if info else game

    with console.status(f"Fetching mod {mod_id} info..."):
        mod  = api_mod_info(domain, mod_id, api_key)
        files = api_mod_files(domain, mod_id, api_key)

    console.print(f"  [bold]{mod['name']}[/bold] by {mod.get('author', '?')} — v{mod.get('version', '?')}")

    if file_id_override:
        chosen = next((f for f in files if f["file_id"] == file_id_override), None)
        if not chosen:
            console.print(f"[red]File ID {file_id_override} not found for mod {mod_id}.[/red]")
            sys.exit(1)
    else:
        chosen = pick_main_file(files)
        if not chosen:
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

    download_file(download_url, archive)

    console.print(f"  Extracting to [dim]{mod_dir}[/dim]...")
    extract_archive(archive, mod_dir)
    archive.unlink(missing_ok=True)

    db.execute("""
        INSERT OR REPLACE INTO mods
            (game, mod_id, file_id, name, version, filename, mod_dir, tracked_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        game, mod_id, chosen["file_id"], mod["name"], mod.get("version"),
        chosen["file_name"], str(mod_dir),
        datetime.utcnow().isoformat(), datetime.utcnow().isoformat(),
    ))
    db.commit()
    return mod["name"], mod.get("version", "?")


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """nexmod — download and update Nexus mods on Linux (Premium required)"""
    pass


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

@config.command("verify")
def config_verify():
    """Hit the Nexus API to confirm key + premium status."""
    api_key = get_api_key()
    try:
        data = nexus_get("users/validate.json", api_key)
        name = data.get("name", "?")
        premium = data.get("is_premium", False)
        supporter = data.get("is_supporter", False)
        console.print(f"[green]Authenticated as:[/green] {name}")
        console.print(f"Premium: {'[green]YES[/green]' if premium else '[red]NO[/red]'}")
        console.print(f"Supporter: {'[green]YES[/green]' if supporter else 'no'}")
        if not premium:
            console.print("[yellow]Warning: download commands require Premium.[/yellow]")
    except Exception as e:
        console.print(f"[red]API error: {e}[/red]")


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

def parse_vortex_manifest(game_dir: Path) -> dict[int, tuple[str, str]]:
    """Parse vortex.deployment.json → {mod_id: (display_name, folder_name)}"""
    manifest = game_dir / "vortex.deployment.json"
    if not manifest.exists():
        return {}
    with open(manifest) as f:
        data = json.load(f)
    seen: dict[int, tuple[str, str]] = {}
    for entry in data.get("files", []):
        src = entry.get("source", "")
        parts = src.split("-")
        mod_id = None
        name_end = 0
        for i, p in enumerate(parts):
            if p.isdigit() and len(p) < 10:
                mod_id = int(p)
                name_end = i
                break
        if mod_id is None or mod_id in seen:
            continue
        name = "-".join(parts[:name_end]).strip()
        rel = entry.get("relPath", "")
        folder = rel.split("/")[1] if rel.startswith("mods/") else ""
        seen[mod_id] = (name, folder)
    return seen

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

    found = parse_vortex_manifest(game_dir)
    if not found:
        console.print(f"[yellow]No vortex.deployment.json found in {game_dir}[/yellow]")
        return

    already = {
        row["mod_id"]
        for row in db.execute("SELECT mod_id FROM mods WHERE game=?", (game,)).fetchall()
    }

    to_add = {mid: v for mid, v in found.items() if mid not in already}
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
                str(mod_dir), datetime.utcnow().isoformat(),
            ))
            db.commit()
            console.print(f"  [green]✓[/green] [{mod_id}] {mod['name']} v{mod.get('version','?')}")
            ok += 1
        except Exception as e:
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
    db = get_db()
    info = GAMES.get(game)
    domain = info["domain"] if info else game

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
        str(mod_dir), datetime.utcnow().isoformat(),
    ))
    db.commit()
    console.print(f"[green]Tracking:[/green] {mod['name']} v{mod.get('version', '?')}")


# list ────────────────────────────────────────────────────────────────────────

@cli.command("list")
@click.argument("game")
def list_mods(game):
    """List tracked mods for GAME."""
    db = get_db()
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
    db = get_db()
    rows = db.execute("SELECT * FROM mods WHERE game = ?", (game,)).fetchall()
    if not rows:
        console.print(f"[yellow]No mods tracked for '{game}'.[/yellow]")
        return

    info   = GAMES.get(game, {})
    domain = info.get("domain", game)

    # Bulk check: get all updated mod IDs this week first (1 API call vs N)
    with console.status("Fetching recently-updated mod list..."):
        try:
            updated_ids = {m["mod_id"] for m in api_updated_mods(domain, api_key)}
        except Exception:
            updated_ids = set()  # fallback to per-mod check

    t = Table(title=f"Update check — {game}", show_lines=False)
    t.add_column("Name")
    t.add_column("Installed", style="dim")
    t.add_column("Latest", style="cyan")
    t.add_column("Status")

    for r in rows:
        # Fast path: if mod not in updated list, it's current
        if updated_ids and r["mod_id"] not in updated_ids:
            t.add_row(r["name"], r["version"] or "?", r["version"] or "?", "[green]Current[/green]")
            continue
        try:
            mod    = api_mod_info(domain, r["mod_id"], api_key)
            latest = mod.get("version", "?")
            cur    = r["version"] or "?"
            if latest == cur:
                status = "[green]Current[/green]"
            else:
                status = f"[yellow]Update → {latest}[/yellow]"
            t.add_row(r["name"], cur, latest, status)
        except Exception as e:
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
    db = get_db()

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
        except Exception:
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
                console.print(f"[red]{r['name']}: error — {e}[/red]")
                continue

        if latest == r["version"]:
            console.print(f"[dim]{r['name']}: current ({latest})[/dim]")
            continue

        console.print(f"[cyan]{r['name']}:[/cyan] {r['version']} → {latest}")
        if not yes:
            if not click.confirm("  Download update?", default=True):
                continue

        with console.status("Getting files..."):
            files  = api_mod_files(domain, r["mod_id"], api_key)
            chosen = pick_main_file(files)

        if not chosen:
            console.print(f"  [red]Could not determine update file — skipping.[/red]")
            continue

        with console.status("Getting CDN link..."):
            urls = api_download_urls(domain, r["mod_id"], chosen["file_id"], api_key)

        download_url = urls[0]["URI"]
        mod_dir      = resolve_mod_dir(game, db)
        tmp          = DATA_DIR / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        archive = tmp / chosen["file_name"]

        download_file(download_url, archive)
        console.print(f"  Extracting to [dim]{mod_dir}[/dim]...")
        extract_archive(archive, mod_dir)
        archive.unlink(missing_ok=True)

        db.execute(
            "UPDATE mods SET file_id=?, version=?, filename=?, updated_at=? WHERE game=? AND mod_id=?",
            (chosen["file_id"], latest, chosen["file_name"],
             datetime.utcnow().isoformat(), game, r["mod_id"]),
        )
        db.commit()
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
    db = get_db()
    row = db.execute("SELECT * FROM mods WHERE game=? AND mod_id=?", (game, mod_id)).fetchone()
    if not row:
        console.print(f"[red]Mod {mod_id} is not tracked for '{game}'.[/red]")
        sys.exit(1)

    if purge and row["mod_dir"] and row["filename"]:
        mod_dir = Path(row["mod_dir"])
        # Strip extension to get the folder name nexmod would have created
        folder_name = row["filename"].rsplit(".", 1)[0]
        folder = mod_dir / folder_name
        if folder.exists() and folder.is_dir():
            shutil.rmtree(folder)
            console.print(f"[dim]Deleted {folder}[/dim]")
        else:
            console.print(f"[yellow]Could not find {folder} — remove files manually if needed.[/yellow]")

    db.execute("DELETE FROM mods WHERE game=? AND mod_id=?", (game, mod_id))
    db.commit()
    console.print(f"[green]Removed:[/green] {row['name']}")


if __name__ == "__main__":
    cli()
