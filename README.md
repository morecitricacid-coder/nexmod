# nexmod

A Linux-native CLI mod manager for [Nexus Mods](https://www.nexusmods.com/).

> **Requires a Nexus Mods Premium account** — the download API is Premium-only.

---

## Features

- Track and update mods for any supported game
- Integrates with Vortex's deployment manifest (`vortex.deployment.json`) for import
- Supports Steam (native + Flatpak) and Wine/Proton mod directories
- Handles `.zip`, `.tar.gz`, and `.7z` archives
- Full operation history in a local SQLite database
- No background services, no telemetry, no account required beyond Nexus Premium

---

## Supported Games

| Game | Slug |
|------|------|
| Warhammer 40,000: Darktide | `darktide` |
| The Elder Scrolls V: Skyrim SE | `skyrimse` |
| Baldur's Gate 3 | `bg3` |
| Cyberpunk 2077 | `cyberpunk2077` |
| Fallout 4 | `fallout4` |

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
git clone https://github.com/YOUR_USERNAME/nexmod
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

# 3. List tracked mods for a game
nexmod list darktide

# 4. Install a mod by its Nexus mod ID
nexmod install darktide 1

# 5. Check for updates
nexmod check darktide

# 6. Update all outdated mods
nexmod update darktide -y

# 7. Import mods from an existing Vortex installation
nexmod scan darktide
```

---

## Commands

```
nexmod games                       List supported games
nexmod config set-key <KEY>        Save your Nexus API key
nexmod config show                 Show current configuration
nexmod config verify               Verify API key + Premium status

nexmod list <game>                 List tracked mods
nexmod install <game> <mod_id>     Download and install a mod
nexmod check <game>                Check for available updates
nexmod update <game> [-y]          Update outdated mods (--yes skips confirmation)
nexmod scan <game>                 Import mods from Vortex deployment manifest
nexmod remove <game> <mod_id>      Untrack a mod

nexmod history [--limit N]         Show operation history
nexmod logs [--errors] [--follow]  View the nexmod log file
```

---

## Configuration

| File | Location |
|------|----------|
| Config (API key) | `~/.config/nexmod/config.json` (chmod 600) |
| Database | `~/.local/share/nexmod/mods.db` |
| Log | `~/.local/share/nexmod/nexmod.log` |

---

## Requirements

- Linux
- Python 3.10+
- `7z` / `7zz` / `7za` for `.7z` archives (`sudo apt install p7zip-full`)

---

## License

MIT
