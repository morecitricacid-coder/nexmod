# nexmod

A Linux-native CLI mod manager for [Nexus Mods](https://www.nexusmods.com/).

> **Requires a Nexus Mods Premium account** — the download API is Premium-only.

---

## Features

- Install / track / update mods by **mod ID *or* full Nexus URL**
- Named **profiles** for hotswapping load orders (e.g. `minimal` ↔ `full`)
- Dependency-aware load-order sorting (reads `mod.json` and `<folder>.mod`)
- **Missing-dependency detection** — every `nexmod update` scans the inventory; `--fix-deps` installs the gaps interactively
- Vortex import — reads `vortex.deployment.json`, no re-downloads
- Steam (native + Flatpak) and Wine/Proton paths auto-detected
- Atomic downloads with MD5 verification before extraction
- `.zip`, `.tar.gz`, `.tar.bz2`, `.tar.xz`, `.7z` archives supported
- Darktide: enable/disable mod loading via dtkit-patch through Wine
- `diag` surfaces mod errors from the game's own log
- Full operation history in a local SQLite DB; no telemetry, no daemons

---

## Supported Games

| Game | Slug | Load order | Mod-loader patch |
|------|------|:---:|:---:|
| Warhammer 40,000: Darktide | `darktide` | ✓ | ✓ (dtkit) |
| Skyrim Special Edition | `skyrimse` | — | — |
| Baldur's Gate 3 | `bg3` | — | — |
| Cyberpunk 2077 | `cyberpunk2077` | — | — |
| Fallout 4 | `fallout4` | — | — |

Unknown games still work for download/extract — pass any Nexus domain and set the mod dir manually with `nexmod path set`.

---

## Installation

### Recommended: pipx

```bash
pipx install nexmod
```

### pip (user install)

```bash
pip install --user nexmod
```

### From source

```bash
git clone <repo-url>
cd nexmod
pip install -e . --user
```

### Shell script (no pip)

```bash
bash install.sh
```

---

## Quick Start

```bash
# 1. Get your API key: nexusmods.com → avatar → Settings → API Keys
nexmod config set-key <your-key>

# 2. Confirm your account is Premium
nexmod config verify

# 3a. Install by URL (any Nexus mod page works)
nexmod install https://www.nexusmods.com/warhammer40kdarktide/mods/1234

# 3b. Or by slug + ID
nexmod install darktide 1234

# 4. List, check, update
nexmod list darktide
nexmod check darktide
nexmod update darktide -y

# 5. Snapshot the current load order, then hotswap later
nexmod profile save darktide minimal -d "QoL only"
nexmod profile save darktide full
nexmod profile load darktide minimal     # apply minimal
nexmod profile load darktide full        # back to full
```

---

## Command Reference

### Configuration

| Command | Description |
|---------|-------------|
| `nexmod config set-key <KEY>` | Save your Nexus API key (chmod 600) |
| `nexmod config show` | Show current configuration (key masked) |
| `nexmod config verify` | Hit the API to confirm key + Premium |
| `nexmod config auto-reorder on\|off` | Toggle automatic load-order sort after install/update |

### Installing & tracking

| Command | Flags | Description |
|---------|-------|-------------|
| `nexmod install <game> <mod_id>` | `--file-id N`, `--no-reorder` | Download, extract, track. Accepts a Nexus URL in place of `<game> <mod_id>`. |
| `nexmod install <nexus-url>` | (same) | URL form — game and mod ID parsed from the URL. |
| `nexmod track <game> <mod_id>` | — | Record an already-installed mod for update tracking. URL form supported. |
| `nexmod scan <game>` | `--dry-run` | Import all mods from `vortex.deployment.json`. |
| `nexmod remove <game> <mod_id>` | `--purge` | Untrack. `--purge` also deletes the mod folder. |

### Updates

| Command | Flags | Description |
|---------|-------|-------------|
| `nexmod check <game>` | — | Show installed-vs-latest for every tracked mod (no download). |
| `nexmod update <game>` | `--mod-id N`, `-y/--yes`, `--no-reorder`, `--fix-deps` | Download and apply available updates, then scan for missing deps. Reports gaps by default; `--fix-deps` prompts to install each. The dep scan runs **even when nothing was updated** so silent rot is impossible. |

### Listing & inspection

| Command | Flags | Description |
|---------|-------|-------------|
| `nexmod list <game>` | `--json` | Tracked mods. `--json` emits machine-readable rows. |
| `nexmod info <game> <mod_id>` | — | Local DB row + one Nexus call: author, version, deps, install date. |
| `nexmod history [game]` | `--limit N`, `--failures` | Operation history. |
| `nexmod logs` | `--lines N`, `--errors`, `--follow` | Tail nexmod's own log. |
| `nexmod games` | — | List built-in game slugs. |

### Profiles (per-game named load-order snapshots)

| Command | Flags | Description |
|---------|-------|-------------|
| `nexmod profile save <game> <name>` | `-d/--description`, `-f/--force` | Snapshot the current load order. |
| `nexmod profile list <game>` | — | Show all saved profiles. |
| `nexmod profile show <game> <name>` | — | Print a profile's full mod list (flags missing-from-disk). |
| `nexmod profile load <game> <name>` | `--dry-run`, `--install` | Apply a profile. `--install` installs profile mods missing from disk via their tracked mod_id. |
| `nexmod profile delete <game> <name>` | `-f/--force` | Delete a profile. |
| `nexmod profile rename <game> <old> <new>` | — | Rename a profile. |

### Load order

| Command | Flags | Description |
|---------|-------|-------------|
| `nexmod order <game>` | `--dry-run` | Sort `mod_load_order.txt` topologically by declared deps. Cycles → end. |

### Mod loader (Darktide)

| Command | Description |
|---------|-------------|
| `nexmod enable <game>` | Run dtkit-patch via Wine to enable mod loading. |
| `nexmod disable <game>` | Unpatch (disable mod loading). |
| `nexmod toggle <game>` | Flip patch state. |

### Paths & diagnostics

| Command | Flags | Description |
|---------|-------|-------------|
| `nexmod path set <game> <dir>` | — | Override auto-detected mod directory. |
| `nexmod path show <game>` | — | Print the resolved mod directory. |
| `nexmod diag <game>` | `--lines N`, `--all` | Surface mod errors from the game's own log file. |

---

## Common Workflows

### Migrate from Vortex (Wine) without re-downloading

```bash
nexmod scan darktide          # imports every mod from vortex.deployment.json
nexmod update darktide -y     # ensures every entry is at the current Nexus version
```

`scan` stores `version=NULL` intentionally so the next `update` always re-fetches — Vortex-installed files may be older than what Nexus reports.

### Detect and repair missing dependencies

```bash
nexmod update darktide              # scans + reports any missing deps
nexmod update darktide --fix-deps   # same, but interactively installs each missing dep
```

Output when something's missing:

```
⚠ 5 mod(s) missing 6 dependency(ies):
  red_weapons_at_home → modding_tools
  hybrid_sprint → modding_tools, MultiBind, ToggleAltFire
  Skitarius → Mark9
  ...
  Re-run with --fix-deps to install them interactively.
```

The default is report-only because `--yes` automation contexts (cron, scripts) shouldn't block on a Nexus URL prompt.

### Hotswap load orders

```bash
nexmod profile save darktide stable
# ...experiment, install some test mods...
nexmod profile save darktide testing
nexmod profile load darktide stable    # snap back to known-good
```

### Bring a profile to a new machine

```bash
# On new machine: profile JSON is at ~/.config/nexmod/profiles/<game>/<name>.json
# After copying it over and tracking the relevant mod IDs:
nexmod profile load darktide full --install
```

`--install` resolves each missing folder to a `mod_id` from the local DB and installs via the normal pipeline.

### Script tracked mods with jq

```bash
nexmod list darktide --json | jq '.[] | select(.version == null) | .mod_id'
```

The JSON shape is one object per `mods` row:

```json
[
  {
    "id": 1,
    "game": "darktide",
    "mod_id": 1234,
    "file_id": 5678,
    "name": "Some Mod",
    "version": "1.2.3",
    "filename": "SomeMod-1234-1-2-3.zip",
    "mod_dir": "/path/to/Darktide/mods",
    "tracked_at": "2026-04-25T...",
    "updated_at": "2026-04-25T..."
  }
]
```

---

## Configuration

| File | Location |
|------|----------|
| Config (API key, settings) | `~/.config/nexmod/config.json` (chmod 600) |
| Profiles | `~/.config/nexmod/profiles/<game>/<name>.json` |
| Database (SQLite, WAL) | `~/.local/share/nexmod/mods.db` |
| Log (rotating, 5 MB × 3) | `~/.local/share/nexmod/nexmod.log` |
| Wine prefix (dtkit) | `~/.local/share/nexmod/wine-prefix` |

The SQLite DB is safe to read concurrently while nexmod is running — WAL is enabled. Schema:

- `mods(id, game, mod_id, file_id, name, version, filename, mod_dir, tracked_at, updated_at)` — one row per tracked mod, unique on `(game, mod_id)`
- `game_paths(game, path)` — manual mod-dir overrides
- `history(timestamp, action, game, mod_id, mod_name, version, status, detail)` — every install/update/scan/remove

---

## Exit Codes

| Code | Meaning |
|:---:|---------|
| `0` | Success (or non-fatal warning, e.g. account not Premium) |
| `1` | Hard failure: missing API key, API error, rate-limited, malformed URL, mod not tracked, profile not found, etc. |

`config verify` exits `1` only when the API call itself fails. A successful call against a non-Premium account still exits `0` (with a warning) — Premium is enforced at download time, not at validate time.

---

## For Automation / LLMs

- **Read-only / no-network commands:** `list`, `list --json`, `history`, `logs`, `path show`, `games`, `profile list`, `profile show`, `config show`. Safe to invoke without an API key.
- **Network / API key required:** `install`, `track`, `update`, `check`, `scan`, `info`, `config verify`.
- **Inspect state without parsing CLI output:** read `~/.local/share/nexmod/mods.db` directly. WAL mode means concurrent readers are fine.
- **Stable JSON contract:** `nexmod list <game> --json` (full DB row per mod). Other commands do not yet emit JSON — expect Rich-formatted tables.
- **Idempotency:** `track`, `scan`, `path set` are safe to re-run. `install` re-extracts and bumps `updated_at`. `profile save` overwrites without prompt only with `-f`.
- **Determining freshness:** `version IS NULL` means scanned-but-not-yet-updated (Vortex import); a normal `update` will pull the current Nexus version.

---

## Requirements

- Linux (tested on Ubuntu 24.04+, Arch, Fedora 40+)
- Python 3.10+
- `7z` / `7zz` / `7za` for `.7z` archives:
  - Debian/Ubuntu: `sudo apt install p7zip-full`
  - Arch: `sudo pacman -S p7zip`
  - Fedora: `sudo dnf install p7zip p7zip-plugins`
- Wine — only required for Darktide `enable` / `disable` / `toggle`.

---

## License

MIT
