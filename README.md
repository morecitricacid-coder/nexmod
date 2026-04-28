# nexmod

[![CI](https://github.com/morecitricacid-coder/nexmod/actions/workflows/ci.yml/badge.svg)](https://github.com/morecitricacid-coder/nexmod/actions/workflows/ci.yml)
[![Pages](https://img.shields.io/badge/site-morecitricacid--coder.github.io%2Fnexmod-f59e0b)](https://morecitricacid-coder.github.io/nexmod/)

**[→ nexmod.github.io](https://morecitricacid-coder.github.io/nexmod/)**

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
pip install -e ".[dev]"
```

### Shell script (no pip)

```bash
bash install.sh
```

---

## Quick Start

```bash
# 0. First-time setup: API key + Steam game scan + pre-flight check in one step
nexmod setup

# If you prefer manual setup:
# 1. Get your API key: nexusmods.com → avatar → Settings → API Keys
nexmod config set-key <your-key>

# 2. Verify everything's wired up (API, Premium, Steam, Wine, 7z, disk, ...)
nexmod doctor

# 3. (optional) Register the nxm:// handler so the "Mod Manager Download"
#     button on Nexus mod pages launches nexmod automatically.
nexmod nxm-register

# 4a. Search for a mod by name before installing
nexmod search darktide "enemy health"
nexmod search darktide "camera shake" --json   # machine-readable for scripting

# 4b. Look up details on a mod ID before committing to install
nexmod info darktide 1234 --remote

# 4c. Install by URL (any Nexus mod page works)
nexmod install https://www.nexusmods.com/warhammer40kdarktide/mods/1234

# 4d. Or by slug + ID (found via search)
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

### Discovery & listing

| Command | Flags | Description |
|---------|-------|-------------|
| `nexmod search <game> <query>` | `--count N` (1–50, default 10), `--json` | Search Nexus Mods for mods by name (v2 GraphQL). Results sorted by endorsements. `--json` emits machine-readable JSON; useful for LLM / scripting workflows. |
| `nexmod list <game>` | `--json` | Tracked mods. `--json` emits machine-readable rows. |
| `nexmod info <game> <mod_id>` | `--remote` | Local DB row + one Nexus call: author, version, deps, install date. `--remote` fetches from Nexus without requiring the mod to be tracked — useful before installing. |
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

### Command categories

| Category | Commands |
|----------|----------|
| **Read-only, no network, no API key** | `list`, `list --json`, `history`, `logs`, `path show`, `games`, `profile list`, `profile show`, `config show`, `snapshots`, `fsck`, `rollback --list` |
| **Network / API key required** | `search`, `install`, `track`, `update`, `check`, `scan`, `info`, `info --remote`, `config verify`, `doctor`, `nxm`, `fsck --with-api` |

### JSON output

Four commands emit machine-readable JSON when passed `--json`:

**`nexmod search <game> <query> --json`** — search results sorted by endorsements:
```json
[{"mod_id":1234,"name":"Enemy Health Bars","summary":"Shows enemy HP above heads.","downloads":50123,"endorsements":1234}]
```

**`nexmod list <game> --json`** — full DB row per tracked mod:
```json
[{"id":1,"game":"darktide","mod_id":1234,"name":"Some Mod","version":"1.2.3","file_id":5678,...}]
```

**`nexmod check <game> --json`** — staleness check per mod, no downloads:
```json
[{"game":"darktide","mod_id":1234,"name":"Some Mod","installed":"1.2.3","latest":"1.3.0","update_available":true,"error":null}]
```

**`nexmod update <game> --json`** — full update run (implies `--yes`), structured result:
```json
{
  "game": "darktide",
  "updated":  [{"mod_id":1234,"name":"Some Mod","from":"1.2.3","to":"1.3.0"}],
  "current":  [{"mod_id":5678,"name":"Other Mod","version":"2.0.0"}],
  "failed":   [],
  "load_order":{"written":true,"cycles":[],"drift_detected":false}
}
```

### Direct DB access

Read `~/.local/share/nexmod/mods.db` directly — WAL mode makes concurrent readers safe while nexmod is running:

```bash
# All tracked mods for a game
sqlite3 ~/.local/share/nexmod/mods.db \
  "SELECT mod_id, name, version FROM mods WHERE game='darktide'"

# Find mods with no recorded version (Vortex imports — need an update run)
sqlite3 ~/.local/share/nexmod/mods.db \
  "SELECT mod_id, name FROM mods WHERE game='darktide' AND version IS NULL"
```

Schema: `mods(id, game, mod_id, file_id, name, version, filename, mod_dir, folder_name, tracked_at, updated_at)`

### Non-interactive flags

- `update --yes` / `-y` — skip per-mod confirmation
- `update --json` — implies `--yes`; never prompts
- `remove --yes` — skip purge confirmation
- `rollback --yes` — skip rollback confirmation
- `install --dry-run`, `remove --dry-run` — no changes made
- `doctor` — exits 1 on required-check failure, 0 on warning-only

### Idempotency

`track`, `scan`, `path set`, `fsck --fix`, `nxm-register` are safe to re-run. `install` re-extracts and bumps `updated_at`. `profile save` overwrites without prompt only with `-f`.

### Resilience

`nexus_get` and `download_file` retry transient errors with exponential backoff and honor `Retry-After` on 429. Callers don't need to retry. Tune via env vars:

| Variable | Default | Effect |
|----------|---------|--------|
| `NEXMOD_API_RETRIES` | `3` | Max retry attempts per request |
| `NEXMOD_API_RETRY_DELAY` | `1.0` | Base backoff delay (seconds) |
| `NEXMOD_429_MAX_WAIT` | `60.0` | Cap on Retry-After sleep |

### Load order directives

Place these as comments in `mod_load_order.txt` to control reconcile behavior:

| Directive | Effect |
|-----------|--------|
| `# NEXMOD:freeze` | nexmod will never modify this file while the directive is present |
| `# NEXMOD:framework ModName` | Pin `ModName` before all non-framework entries (DMF-style core mods) |
| `# NEXMOD:pin:top ModName` | Force `ModName` to top of managed section |
| `# NEXMOD:pin:bottom ModName` | Force `ModName` to bottom |
| `# NEXMOD:pin:before Target ModName` | Place `ModName` immediately before `Target` |
| `# NEXMOD:pin:after Target ModName` | Place `ModName` immediately after `Target` |

Manage pins via `nexmod pin` / `nexmod unpin` / `nexmod pins <game>` rather than editing the file manually.

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

## Contributing

nexmod is a public project. Bug reports, game additions, and fixes are welcome.

### Bug reports

Open an issue at [github.com/morecitricacid-coder/nexmod/issues](https://github.com/morecitricacid-coder/nexmod/issues).

Include:
- OS and distro (e.g. Ubuntu 24.04, Arch Linux)
- Python version: `python3 --version`
- nexmod version: `nexmod --version`
- The full command you ran
- The complete error output
- Game slug (e.g. `darktide`, `skyrimse`)

### Feature requests

Same link as above, label: **enhancement**. Game support requests are especially welcome — include the mod directory path and the Nexus domain slug (from the URL: `nexusmods.com/<domain>/mods/...`).

### Submitting a fix (pull requests)

A *fork* is your personal copy of the project on GitHub. You make changes in your copy, then open a pull request to propose merging them back into nexmod. GitHub handles all the mechanics; you just need a GitHub account and basic git familiarity.

1. Fork the repo on GitHub (big **Fork** button, top right of the repo page)
2. Clone your fork locally:
   ```bash
   git clone https://github.com/<your-username>/nexmod && cd nexmod
   ```
3. Create a branch for your change:
   ```bash
   git checkout -b fix/my-fix
   ```
4. Install dev dependencies:
   ```bash
   pip install -e ".[dev]"
   ```
5. Make your change, then run the test suite:
   ```bash
   pytest -q -m "not smoke"
   ```
6. Push your branch:
   ```bash
   git push origin fix/my-fix
   ```
7. Open a pull request from your branch to `morecitricacid-coder/nexmod:main` on GitHub

**PR guidelines:**
- One fix per PR — keeps review focused
- Add a test if you're fixing a bug (helps catch regressions later)
- Update README and CHANGELOG `[Unreleased]` if the user-facing surface changes
- CI must be green before merge

### Adding a new game

If your game is on Nexus Mods and runs on Linux, it can probably be added in a few lines of code.

**What you need to find:**
- The game's Nexus domain — from the URL: `nexusmods.com/<domain>/mods/...`
- The Steam app ID — from the store URL: `store.steampowered.com/app/<id>/`
- Where mods live on disk — install one mod with Vortex or manually, then find the folder relative to the game's install directory

**How to add it:**

1. Open `nexmod.py` and find the `GAMES` dict near the top
2. Add your entry:
   ```python
   "mygame": {
       "name": "My Game Full Name",
       "domain": "mygame",      # from nexusmods.com/<domain>/mods/...
       "steam_id": 123456,      # from store.steampowered.com/app/<id>/
       "mod_subdir": "Mods",    # relative to game install dir; case-sensitive on Linux
       "log_subpath": None,     # relative path to game log file, or None
   },
   ```
   > Note: only add `load_order_file` if the game uses a plain-text mod list (like Darktide's `mod_load_order.txt`). Most games don't.
3. Verify it shows up: `nexmod games`
4. Test the basics: `nexmod doctor mygame` and `nexmod install mygame <any-mod-id>`
5. Add your game to the Supported Games table in README.md
6. Open a PR with your findings

### Integrating nexmod with other tools

**As a library** — nexmod is intentionally a single-file CLI, not a Python library. Call it as a subprocess rather than importing it. The `--json` flags give you structured output, and the SQLite DB at `~/.local/share/nexmod/mods.db` is WAL-mode safe to read concurrently.

**From an LLM agent** — see the [For Automation / LLMs](#for-automation--llms) section above for the full JSON contract. Primary read paths: `nexmod search --json`, `nexmod list --json`, `nexmod check --json`. Primary write paths: `nexmod install`, `nexmod update --yes --json`. The DB is always safe to query directly for read-only access.

**In a shell script** — all commands exit `0` on success, `1` on failure. `--yes` and `--json` skip prompts. Idempotent commands: `track`, `scan`, `path set`, `fsck --fix`, `nxm-register`.

**Reporting nexmod issues from tool integrations** — include the full subprocess call and captured stderr in your bug report.

---

## License

MIT
