# nexmod

[![CI](https://github.com/morecitricacid-coder/nexmod/actions/workflows/ci.yml/badge.svg)](https://github.com/morecitricacid-coder/nexmod/actions/workflows/ci.yml)

A Linux-native CLI mod manager for [Nexus Mods](https://www.nexusmods.com/).

> **Requires a Nexus Mods Premium account** — the download API is Premium-only.

---

## Features

- Install / track / update mods by **mod ID *or* full Nexus URL**
- **`nxm://` link handler** — register once, then click "Mod Manager Download" on any Nexus mod page
- **Rollback** — every install/update is snapshotted; one command restores the previous version
- Named **profiles** for hotswapping load orders (e.g. `minimal` ↔ `full`)
- Dependency-aware load-order sorting (reads `mod.json` and `<folder>.mod`)
- **Robust `mod_load_order.txt` management** — orphan detection, foreign-entry preservation, framework auto-pinning, atomic writes with backup, external-edit drift detection, in-file `nexmod:freeze` directive
- **Pin folders** to top/bottom or relative to other folders — pins persist in the DB and survive every reconcile
- **Missing-dependency detection** — every `nexmod update` scans the inventory; `--fix-deps` installs the gaps interactively
- **Conflict detection** — peeks archives before extracting; warns when a mod would overwrite another tracked mod's folder
- **Network resilience** — Range-resume on download interruption, retries with exponential backoff, honors `Retry-After` on 429, falls back across CDN mirrors
- **Pre-flight `nexmod doctor`** — verifies API + Premium + Steam + Wine + 7z + disk + DB before you install anything
- **`nexmod fsck`** — audits and repairs DB drift (legacy rows missing `folder_name`, version mismatches)
- Vortex import — reads `vortex.deployment.json`, no re-downloads
- Steam (native + Flatpak) and Wine/Proton paths auto-detected
- Atomic downloads with MD5 verification before extraction
- `.zip`, `.tar.gz`, `.tar.bz2`, `.tar.xz`, `.7z` archives supported
- Disk-space pre-check before download/extract
- Path-traversal protection (rejects `../` and absolute paths in archives)
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
git clone https://github.com/morecitricacid-coder/nexmod
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

# 2. Verify everything's wired up (API, Premium, Steam, Wine, 7z, disk, ...)
nexmod doctor

# 3. (optional) Register the nxm:// handler so the "Mod Manager Download"
#     button on Nexus mod pages launches nexmod automatically.
nexmod nxm-register

# 4a. Install by URL (any Nexus mod page works)
nexmod install https://www.nexusmods.com/warhammer40kdarktide/mods/1234

# 4b. Or by slug + ID
nexmod install darktide 1234

# 5. List, check, update
nexmod list darktide
nexmod check darktide
nexmod update darktide -y

# 6. If a new version breaks the mod, roll back to the previous snapshot.
nexmod rollback darktide 1234

# 7. Snapshot the current load order, then hotswap later
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
| `nexmod install <game> <mod_id>` | `--file-id N`, `--no-reorder`, `--dry-run` | Download, extract, track. Accepts a Nexus URL in place of `<game> <mod_id>`. `--dry-run` resolves metadata + prints what would happen without fetching. Conflict detection: if the archive's top-level folder is already claimed by another tracked mod, you'll be prompted before overwriting. |
| `nexmod install <nexus-url>` | (same) | URL form — game and mod ID parsed from the URL. |
| `nexmod track <game> <mod_id>` | — | Record an already-installed mod for update tracking. URL form supported. |
| `nexmod scan <game>` | `--dry-run` | Import all mods from `vortex.deployment.json`. |
| `nexmod remove <game> <mod_id>` | `--purge`, `--yes`, `--dry-run`, `--force-legacy-purge` | Untrack. `--purge` also deletes the mod folder (with confirmation; `--yes` skips). Rows missing `folder_name` (legacy installs) refuse to purge unless `--force-legacy-purge` is set — run `nexmod fsck --fix` first. |

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
| `nexmod profile load <game> <name>` | `--dry-run`, `--install`, `--strict` | Apply a profile. `--install` installs profile mods missing from disk via their tracked mod_id. `--strict` also drops foreign (untracked) entries — default preserves them. |
| `nexmod profile delete <game> <name>` | `-f/--force` | Delete a profile. |
| `nexmod profile rename <game> <old> <new>` | — | Rename a profile. |

### Load order

| Command | Flags | Description |
|---------|-------|-------------|
| `nexmod order <game>` | `--dry-run`, `--check`, `--fsck`, `--freeze`, `--unfreeze`, `--adopt`, `--auto-merge` | Reconcile `mod_load_order.txt` against DB + disk + pins. |
| `nexmod pin <game> <folder> top\|bottom\|before\|after [other]` | — | Pin a folder to a load-order position. |
| `nexmod unpin <game> <folder>` | — | Remove a pin. |
| `nexmod pins <game>` | — | List active pins. |

**`order` flags:**

| Flag | What it does |
|------|--------------|
| `--check` | Print classification table + diff. No write. |
| `--dry-run` | Same, but quieter. |
| `--fsck` | Restore from `.bak` (kept by every reconcile). |
| `--freeze` | Insert `-- nexmod:freeze` directive. nexmod stops touching the file. |
| `--unfreeze` | Remove the freeze directive. |
| `--adopt` | Promote foreign (untracked) entries: prompts for a Nexus URL per entry. |
| `--auto-merge` | Force overwrite even if external drift is detected. |

### How the reconciler classifies entries

| Class | Meaning | Action |
|-------|---------|--------|
| `framework` | Foreign + matches game's framework set (e.g. `mod_compat`, `dmf`) | Pin to top |
| `managed-present` | Tracked in DB, folder on disk | Topo-sort by deps |
| `foreign` | Folder on disk, not tracked | Preserve verbatim |
| `managed-missing` | Tracked in DB, folder gone | Drop |
| `orphan` | Listed in file, no folder, no DB row | Drop |

### In-file directives

The reconciler parses these `--`-prefixed comment directives and re-emits them on every write:

```
-- nexmod:freeze                          # disables nexmod auto-mutation
-- nexmod:framework custom_framework_dir  # adds to the framework set (auto-pinned to top)
-- nexmod:pin my_mod top                  # pin to top of file
-- nexmod:pin my_mod bottom               # pin to bottom of file
-- nexmod:pin my_mod before mod_compat    # pin immediately before another folder
-- nexmod:pin my_mod after  dmf           # pin immediately after another folder
```

Pins set via `nexmod pin` go into the DB; pins via directives go in the file. Both are honored.

### Rollback & snapshots

| Command | Flags | Description |
|---------|-------|-------------|
| `nexmod snapshots <game> [<mod_id>]` | `--prune` | List cached version snapshots. With `--prune`, force-trims each mod to the most-recent `SNAPSHOTS_PER_MOD` (default 3). |
| `nexmod rollback <game> <mod_id>` | `--version V`, `--list`, `--yes` | Restore a tracked mod from a cached snapshot. Defaults to the most-recent prior version; `--version` picks a specific one; `--list` shows what's available without restoring. |

Snapshots are written automatically after every successful install/update to
`~/.cache/nexmod/<game>/<mod_id>/<version>.<ext>` and capped to the most-recent
`SNAPSHOTS_PER_MOD` per mod (override via the `NEXMOD_SNAPSHOTS_PER_MOD` env var).

### NXM links

| Command | Description |
|---------|-------------|
| `nexmod nxm-register` | Write `~/.local/share/applications/nexmod-nxm.desktop` and register it via `xdg-mime` as the system handler for `nxm://` links. |
| `nexmod nxm-unregister` | Remove the registration. |
| `nexmod nxm <uri>` | Manually handle an `nxm://` URI. Usually invoked by the system handler after registration; safe to paste a URI yourself. |

After `nxm-register`, clicking "Mod Manager Download" on any Nexus mod page
launches nexmod and starts the install.

### Mod loader (Darktide)

| Command | Description |
|---------|-------------|
| `nexmod enable <game>` | Run dtkit-patch via Wine to enable mod loading. |
| `nexmod disable <game>` | Unpatch (disable mod loading). |
| `nexmod toggle <game>` | Flip patch state. |

### Paths & diagnostics

| Command | Flags | Description |
|---------|-------|-------------|
| `nexmod doctor` | `--game <slug>` | Pre-flight environment check: API key + Premium, Steam libraries, per-game install paths, Wine + 7z presence, disk space, DB integrity. Exits 1 if any required check fails; warnings are advisory. |
| `nexmod fsck [<game>]` | `--fix`, `--with-api` | Audit the local DB. Reports legacy rows missing `folder_name` and version. With `--fix`, auto-backfills folder names by 4-strategy inference (filename stem → mod.json name → .mod title → fuzzy match), refusing collisions. With `--with-api --fix`, also re-fetches missing version strings from Nexus. |
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

### Recover from a bad update

```bash
nexmod update darktide -y                       # mod ID 1234 ships v1.2 — breaks something
nexmod snapshots darktide 1234                  # see what versions are cached
nexmod rollback darktide 1234                   # restore most recent prior version
# or pin a specific version:
nexmod rollback darktide 1234 --version 1.0
```

Snapshots are saved automatically after every install/update; you don't need
to opt in. The cache caps to 3 versions per mod by default.

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
| Snapshot cache (rollback) | `~/.cache/nexmod/<game>/<mod_id>/<version>.<ext>` |
| NXM handler (after `nxm-register`) | `~/.local/share/applications/nexmod-nxm.desktop` |

### Tunable env vars

| Variable | Default | Purpose |
|----------|---------|---------|
| `NEXMOD_API_RETRIES` | `3` | Total attempts for `nexus_get` requests |
| `NEXMOD_API_RETRY_DELAY` | `1.0` | Base delay (seconds) for exponential backoff |
| `NEXMOD_429_MAX_WAIT` | `60.0` | Cap on `Retry-After` honored on HTTP 429 |
| `NEXMOD_DOWNLOAD_RETRIES` | `3` | Total attempts for `download_file` (with Range-resume) |
| `NEXMOD_SNAPSHOTS_PER_MOD` | `3` | Max snapshots kept per mod in rollback cache |
| `XDG_CACHE_HOME` | `~/.cache` | Base for snapshot cache directory |

The SQLite DB is safe to read concurrently while nexmod is running — WAL is enabled. Schema:

- `mods(id, game, mod_id, file_id, name, version, filename, mod_dir, folder_name, tracked_at, updated_at)` — one row per tracked mod, unique on `(game, mod_id)`. `folder_name` is captured at install time and is required for reliable `remove --purge`; legacy rows backfill on the next install/update.
- `game_paths(game, path)` — manual mod-dir overrides
- `history(timestamp, action, game, mod_id, mod_name, version, status, detail)` — every install/update/scan/remove
- `load_order_state(game, file_path, last_hash, last_written_at, frozen)` — sha256 of the load order at last write; used to detect external edits.
- `load_order_pins(game, folder, position, relative_to, source, created_at)` — persistent pins set via `nexmod pin`.

---

## Exit Codes

| Code | Meaning |
|:---:|---------|
| `0` | Success (or non-fatal warning, e.g. account not Premium) |
| `1` | Hard failure: missing API key, API error, rate-limited, malformed URL, mod not tracked, profile not found, etc. |

`config verify` exits `1` only when the API call itself fails. A successful call against a non-Premium account still exits `0` (with a warning) — Premium is enforced at download time, not at validate time.

---

## For Automation / LLMs

- **Read-only / no-network commands:** `list`, `list --json`, `history`, `logs`, `path show`, `games`, `profile list`, `profile show`, `config show`, `snapshots`, `fsck` (without `--fix`), `rollback --list`. Safe to invoke without an API key.
- **Network / API key required:** `install`, `track`, `update`, `check`, `scan`, `info`, `config verify`, `doctor`, `nxm`, `fsck --with-api`.
- **Inspect state without parsing CLI output:** read `~/.local/share/nexmod/mods.db` directly. WAL mode means concurrent readers are fine.
- **Stable JSON contract:** `nexmod list <game> --json` (full DB row per mod). Other commands do not yet emit JSON — expect Rich-formatted tables.
- **Idempotency:** `track`, `scan`, `path set`, `fsck --fix`, `nxm-register` are safe to re-run. `install` re-extracts and bumps `updated_at`. `profile save` overwrites without prompt only with `-f`.
- **Non-interactive flags:** `update -y`, `remove --yes`, `rollback --yes` skip confirmations. `install --dry-run` and `remove --dry-run` make no changes. `nexmod doctor` exits 1 on any required-check failure (warnings still exit 0).
- **Determining freshness:** `version IS NULL` means scanned-but-not-yet-updated (Vortex import); a normal `update` will pull the current Nexus version.
- **Network resilience is automatic:** clients don't need to retry — `nexus_get` and `download_file` retry transient errors internally with exponential backoff and respect `Retry-After`. Tune via env vars (see Configuration).

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
