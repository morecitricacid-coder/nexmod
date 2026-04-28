"""NexMod MCP server — exposes nexmod as native LLM tools.

Usage:
    nexmod mcp-server            # via the CLI (recommended)
    python nexmod_mcp.py         # direct execution

Requires:
    pip install nexmod[mcp]      # adds mcp>=1.0
"""
from mcp.server.fastmcp import FastMCP
import shutil
import sys

# Import nexmod functions we'll wrap.
# nexmod.py has `if __name__ == "__main__": cli()` at the bottom, so importing
# it here does NOT launch the CLI — it just loads the module's namespace.
import nexmod as _nm

mcp = FastMCP(
    "nexmod",
    instructions=(
        "Linux-native Nexus Mods mod manager. Install, update, search, and manage "
        "mod profiles for Darktide, Skyrim, BG3, Cyberpunk 2077, Fallout 4, and Starfield."
    ),
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_install(game: str, mod_id: int) -> dict:
    """Call do_install, translating sys.exit() calls to RuntimeError."""
    api_key = _nm.get_api_key()
    db = _nm.get_db()
    try:
        _nm.do_install(game, mod_id, None, api_key, db)
    except SystemExit as exc:
        raise RuntimeError(
            f"Install failed for mod {mod_id} on '{game}' (exit code {exc.code}). "
            "Check nexmod logs with: nexmod logs --errors"
        ) from exc
    row = db.execute(
        "SELECT * FROM mods WHERE game=? AND mod_id=?", (game, mod_id)
    ).fetchone()
    return {
        "mod_id": mod_id,
        "name": row["name"] if row else None,
        "version": row["version"] if row else None,
        "status": "installed",
    }


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def games() -> list[dict]:
    """List all supported games with their slugs, names, and Nexus domain slugs."""
    return [
        {"slug": slug, "name": info["name"], "domain": info["domain"]}
        for slug, info in _nm.GAMES.items()
    ]


@mcp.tool()
def search_mods(game: str, query: str, count: int = 10) -> list[dict]:
    """Search Nexus Mods by name. Returns mods sorted by endorsements, with 'installed' flag.

    Args:
        game:  Game slug (e.g. 'darktide', 'skyrimse', 'bg3'). Call games() for valid values.
        query: Search string.
        count: Number of results (1-50, default 10).
    """
    api_key = _nm.get_api_key()
    info = _nm.GAMES.get(game)
    if not info:
        raise RuntimeError(
            f"Unknown game '{game}'. Call games() for valid slugs."
        )
    domain = info["domain"]
    # api_search_mods returns a list of nodes directly (camelCase keys)
    nodes = _nm.api_search_mods(domain, query, api_key, count=count)
    db = _nm.get_db()
    installed_ids = {
        r["mod_id"]
        for r in db.execute(
            "SELECT mod_id FROM mods WHERE game=?", (game,)
        ).fetchall()
    }
    return [
        {
            "mod_id": n.get("modId"),
            "name": n.get("name"),
            "summary": (n.get("summary") or "")[:120],
            "downloads": n.get("downloads"),
            "endorsements": n.get("endorsements"),
            "installed": (n.get("modId") or 0) in installed_ids,
        }
        for n in nodes
    ]


@mcp.tool()
def list_mods(game: str) -> list[dict]:
    """List all tracked mods for a game.

    Args:
        game: Game slug (e.g. 'darktide'). Call games() for valid values.
    """
    db = _nm.get_db()
    rows = db.execute(
        "SELECT * FROM mods WHERE game=? ORDER BY name", (game,)
    ).fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
def mod_info(game: str, mod_id: int) -> dict:
    """Get detailed info for a mod. Fetches live data from the Nexus API.

    Works even if the mod is not installed locally — useful for inspecting a
    mod before deciding to install it.

    Args:
        game:   Game slug.
        mod_id: Nexus mod ID.
    """
    api_key = _nm.get_api_key()
    info = _nm.GAMES.get(game, {})
    domain = info.get("domain", game)
    upstream = _nm.api_mod_info(domain, mod_id, api_key)
    db = _nm.get_db()
    local = db.execute(
        "SELECT * FROM mods WHERE game=? AND mod_id=?", (game, mod_id)
    ).fetchone()
    return {
        "mod_id": mod_id,
        "name": upstream.get("name"),
        "summary": upstream.get("summary"),
        "version": upstream.get("version"),
        "author": upstream.get("author"),
        "endorsements": upstream.get("endorsement_count"),
        "downloads": upstream.get("mod_downloads"),
        "installed": local is not None,
        "installed_version": local["version"] if local else None,
    }


@mcp.tool()
def install_mod(game: str, mod_id: int) -> dict:
    """Download and install a mod from Nexus Mods. Requires Premium for direct downloads.

    Args:
        game:   Game slug.
        mod_id: Nexus mod ID to install.

    Returns:
        {mod_id, name, version, status: 'installed'}
    """
    if not _nm.GAMES.get(game):
        raise RuntimeError(
            f"Unknown game '{game}'. Call games() for valid slugs."
        )
    return _run_install(game, mod_id)


@mcp.tool()
def check_updates(game: str) -> list[dict]:
    """Check all tracked mods for available updates. Does not download anything.

    Args:
        game: Game slug.

    Returns:
        List of {mod_id, name, installed, latest, update_available, error}.
        'error' is null on success or a string on failure (e.g. network error).
    """
    api_key = _nm.get_api_key()
    db = _nm.get_db()
    rows = db.execute("SELECT * FROM mods WHERE game=?", (game,)).fetchall()
    info = _nm.GAMES.get(game, {})
    domain = info.get("domain", game)
    results = []
    for r in rows:
        try:
            upstream = _nm.api_mod_info(domain, r["mod_id"], api_key)
            latest = upstream.get("version", "?")
            cur = r["version"] or "?"
            results.append({
                "mod_id": r["mod_id"],
                "name": r["name"],
                "installed": cur,
                "latest": latest,
                "update_available": (
                    _nm._norm_version(latest) != _nm._norm_version(cur)
                ),
                "error": None,
            })
        except Exception as exc:
            results.append({
                "mod_id": r["mod_id"],
                "name": r["name"],
                "installed": r["version"],
                "latest": None,
                "update_available": None,
                "error": str(exc),
            })
    return results


@mcp.tool()
def update_mod(game: str, mod_id: int) -> dict:
    """Update a specific mod to the latest version. Downloads and reinstalls if an update exists.

    Args:
        game:   Game slug.
        mod_id: Nexus mod ID.

    Returns:
        {mod_id, name, version, status: 'updated' | 'current'}
    """
    api_key = _nm.get_api_key()
    db = _nm.get_db()
    row = db.execute(
        "SELECT * FROM mods WHERE game=? AND mod_id=?", (game, mod_id)
    ).fetchone()
    if not row:
        raise RuntimeError(
            f"Mod {mod_id} is not tracked for '{game}'. Install it first."
        )
    info = _nm.GAMES.get(game, {})
    domain = info.get("domain", game)
    upstream = _nm.api_mod_info(domain, mod_id, api_key)
    latest = upstream.get("version", "?")
    if _nm._norm_version(latest) == _nm._norm_version(row["version"] or ""):
        return {
            "mod_id": mod_id,
            "name": row["name"],
            "version": row["version"],
            "status": "current",
        }
    return _run_install(game, mod_id)


@mcp.tool()
def remove_mod(game: str, mod_id: int, purge: bool = False) -> dict:
    """Remove a tracked mod from the database. Optionally delete files from disk.

    In MCP context --yes is implied (no TTY, no confirmation prompt).

    Args:
        game:   Game slug.
        mod_id: Nexus mod ID.
        purge:  If True, also delete the mod files from disk (irreversible).

    Returns:
        {mod_id, name, status: 'removed', purged: bool}
    """
    db = _nm.get_db()
    row = db.execute(
        "SELECT * FROM mods WHERE game=? AND mod_id=?", (game, mod_id)
    ).fetchone()
    if not row:
        raise RuntimeError(
            f"Mod {mod_id} is not tracked for '{game}'."
        )
    name = row["name"]
    purged = False
    if purge and row["mod_dir"] and row["folder_name"]:
        target = _nm.Path(row["mod_dir"]) / row["folder_name"]
        if target.exists():
            shutil.rmtree(target)
            purged = True
    db.execute(
        "DELETE FROM mods WHERE game=? AND mod_id=?", (game, mod_id)
    )
    db.commit()
    return {"mod_id": mod_id, "name": name, "status": "removed", "purged": purged}


@mcp.tool()
def get_history(game: str = None, limit: int = 20) -> list[dict]:
    """Get install/update/remove history. Optional game filter.

    Args:
        game:  Game slug to filter by, or None for all games.
        limit: Maximum number of records to return (default 20).
    """
    db = _nm.get_db()
    if game:
        rows = db.execute(
            "SELECT * FROM history WHERE game=? ORDER BY timestamp DESC LIMIT ?",
            (game, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM history ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
def list_profiles(game: str) -> list[dict]:
    """List saved mod profiles for a game.

    Args:
        game: Game slug.

    Returns:
        List of {name, description, updated_at, mod_count}.
    """
    profiles = _nm._list_profiles(game)
    return [
        {
            "name": p["name"],
            "description": p.get("description", ""),
            "updated_at": p.get("updated_at"),
            "mod_count": len(p.get("load_order", [])),
        }
        for p in profiles
    ]


@mcp.tool()
def load_profile(game: str, name: str = "default") -> dict:
    """Apply a saved profile (writes the load order file). Darktide only.

    Creates a .bak backup of the existing load order file before overwriting.

    Args:
        game: Game slug (currently only 'darktide' has a managed load order).
        name: Profile name (default: 'default').

    Returns:
        {profile, game, mods_applied, order}
    """
    p = _nm._read_profile(game, name)
    db = _nm.get_db()
    info = _nm.GAMES.get(game, {})
    if not info or not info.get("load_order_file"):
        raise RuntimeError(
            f"'{game}' does not support managed load order. "
            "Load order profiles currently require Darktide."
        )
    mod_dir = _nm.resolve_mod_dir(game, db)
    lof = mod_dir / info["load_order_file"]
    order = p.get("load_order", [])
    # Atomic write with backup
    bak = lof.with_suffix(".bak")
    if lof.exists():
        shutil.copy2(lof, bak)
    lof.write_text("\n".join(order) + "\n")
    return {"profile": name, "game": game, "mods_applied": len(order), "order": order}


@mcp.tool()
def save_profile(game: str, name: str, description: str = "") -> dict:
    """Save the current load order as a named profile. Darktide only.

    Args:
        game:        Game slug (currently only 'darktide' has a managed load order).
        name:        Profile name to save under.
        description: Optional description for this profile.

    Returns:
        {profile, game, mods_saved}
    """
    db = _nm.get_db()
    info = _nm.GAMES.get(game, {})
    if not info or not info.get("load_order_file"):
        raise RuntimeError(
            f"'{game}' does not support managed load order. "
            "Load order profiles currently require Darktide."
        )
    mod_dir = _nm.resolve_mod_dir(game, db)
    lof = mod_dir / info["load_order_file"]
    lof_text = lof.read_text() if lof.exists() else ""
    parsed = _nm._parse_load_order_file(lof_text)
    # Build mods list from DB for clean-machine restore compatibility
    mods = []
    for folder in parsed["entries"]:
        row = db.execute(
            "SELECT mod_id, name, version, game FROM mods WHERE game=? AND folder_name=?",
            (game, folder),
        ).fetchone()
        if row:
            mods.append({
                "mod_id": row["mod_id"],
                "name": row["name"],
                "version": row["version"],
                "folder_name": folder,
                "domain": _nm.GAMES.get(game, {}).get("domain", game),
            })
    _nm._write_profile(
        game, name, parsed["entries"], description,
        parsed["directive_lines"], mods=mods,
    )
    return {"profile": name, "game": game, "mods_saved": len(parsed["entries"])}


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
