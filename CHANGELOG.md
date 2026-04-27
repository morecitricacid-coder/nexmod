# Changelog

All notable changes to nexmod are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added

- `nexmod setup` — interactive first-run wizard: prompts for API key, auto-scans Steam for all supported games (or a single `--game <slug>`), and runs `doctor` to confirm the environment. Use `--reset` to re-enter an existing key.
- Interactive game path prompt on unknown or undetected games in `resolve_mod_dir`: when stdin/stdout are TTYs, nexmod prompts the user to enter a path instead of exiting immediately. Non-interactive callers (cron, scripts, pipes) still get a hard exit with a clear `nexmod path set` hint.
- Inline dep install prompt in `--fix-deps`: a `[Y/n]` gate now appears before each missing dep, and the DB is queried by `folder_name` before prompting for a URL — known mods install directly without requiring a Nexus URL.
- `nexmod check <game> --json` — machine-readable staleness check; emits one object per mod with `mod_id`, `installed`, `latest`, `update_available`, `error`
- `nexmod update <game> --json` — machine-readable update run; implies `--yes`; emits `updated`/`current`/`failed`/`load_order` summary object
- `doctor` now prints a "→ Next:" hint after a clean pass

### Fixed

- README "From source" install used `pip install -e . --user`; corrected to `pip install -e ".[dev]"` so dev dependencies (pytest, responses) are included

---

## [0.3.0] — Public-Readiness Hardening

Pre-launch hardening pass before nexmod becomes a public PyPI package.

### Added

- **`nexmod doctor`** — pre-flight environment check. Verifies API key + Premium
  status, Steam library detection, per-game install paths, Wine + 7z binaries,
  disk space, and config/data directory writability. Exits 1 if any check fails.
- **`nexmod fsck [--fix] [--with-api]`** — audits the local DB for missing
  `folder_name` and `version` fields. Backfills inferred folder names via
  4-strategy ladder (filename stem → mod.json → .mod title → fuzzy difflib
  match). Detects collisions and refuses to apply ambiguous matches.
- **`nexmod rollback <game> <mod_id> [--version V] [--list] [--yes]`** —
  restore a tracked mod from a cached snapshot of a previous version.
  Defaults to most-recent prior version.
- **`nexmod snapshots <game> [<mod_id>] [--prune]`** — list cached version
  snapshots. Snapshots are saved automatically after each install/update,
  capped to `SNAPSHOTS_PER_MOD` (default 3) per mod.
- **`nexmod nxm <uri>`** + **`nexmod nxm-register`** / **`nexmod nxm-unregister`** —
  handler for `nxm://` links from Nexus Mods' "Mod Manager Download" button.
  `nxm-register` writes a `.desktop` file and registers it via xdg-mime.
- **`install --dry-run`** — resolve metadata + show what would be downloaded
  without fetching or extracting.
- **`remove --dry-run`** / **`remove --yes`** / **`remove --force-legacy-purge`**
  flags for safer purge semantics.
- **Schema migrations framework** — append-only `SCHEMA_MIGRATIONS` list
  recorded in `schema_migrations` table. Idempotent + legacy-aware.

### Changed

- **`remove --purge` now requires confirmation** by default. Skip with `--yes`.
  Rows with `NULL folder_name` (legacy installs) refuse to purge unless
  `--force-legacy-purge` is passed — filename-stem inference can delete the
  wrong folder for archives with non-obvious naming (UUID paths, Vortex
  conventions). Use `nexmod fsck --fix` to backfill first.
- **`nexus_get` now retries** transient errors with exponential backoff
  (3 attempts default). Honors `Retry-After` on HTTP 429 (capped at 60s).
  Retries 5xx; hard-fails 401/403/404 (won't self-heal).
- **`download_file` now resumes** interrupted downloads via HTTP `Range` header.
  Treats server-ignored Range (HTTP 200 instead of 206) as a fresh restart.
- **Mirror fallback** — installs/updates try every CDN URL Nexus returns,
  not just the first.
- **Install conflict detection** — peeks archive contents before extraction;
  warns and prompts when any top-level folder is already claimed by a
  different tracked mod. Decline aborts cleanly, no DB write.
- **Multi-folder install warning** — surfaces all extracted folders when an
  archive produces more than one (alphabetically-first folder is recorded
  as primary for `--purge`).
- **Disk-space pre-flight** — install/update checks free space at tmp
  (download size + 50 MB buffer) and at mod_dir (3× compressed size for
  extraction headroom). Bails out before any state changes if insufficient.
- **Cycle warnings** — load-order dependency cycles are surfaced consistently
  across `install`, `update`, and `order` commands instead of being silently
  appended to the file.
- **Wine pre-check at install time** — Darktide installs warn if Wine is
  missing, since `enable`/`disable` will fail later without it.
- **Doctor warning summary** — final line reports warning count when all
  required checks pass but advisory issues exist.

### Fixed

- **`folder_name` backfill** for legacy rows installed before that column
  existed (the column was added by an earlier defensive `ALTER TABLE` but
  never populated for existing rows). Run `nexmod fsck --fix` once to
  repair. 60/77 rows backfill automatically via fuzzy match on author's
  Darktide install; remainder need manual SQL.

### Internal

- 144 → 238 passing tests (+94). New test files:
  - `tests/test_fsck.py` — migration framework + folder inference
  - `tests/test_safety.py` — disk space, archive peek, conflict detection,
    purge guards
  - `tests/test_network_resilience.py` — retry, 429, Range resume, mirrors
  - `tests/test_doctor_and_dryrun.py` — doctor checks + install dry-run
  - `tests/test_nxm.py` — URI parsing, .desktop registration, dispatch
  - `tests/test_rollback.py` — snapshot save/prune/list, rollback flows
  - `tests/integration/test_full_flows.py` — end-to-end install / update /
    rollback / NXM / collision / disk-space / md5 mismatch / path traversal
- Test suite runtime kept under 0.5s via mocked `time.sleep` in `isolated_dirs`
  fixture (retry backoff would otherwise dominate runtime).
- Tunable env vars: `NEXMOD_API_RETRIES`, `NEXMOD_API_RETRY_DELAY`,
  `NEXMOD_429_MAX_WAIT`, `NEXMOD_DOWNLOAD_RETRIES`, `NEXMOD_SNAPSHOTS_PER_MOD`.

### Bundled (was uncommitted)

This release also lands the load-order reconciler v2 work that had been
sitting in the working tree:

- `load_order_state` + `load_order_pins` tables
- `_classify_entries`, `_apply_pins`, `reconcile_load_order`
- `pin` / `unpin` / `pins` commands
- External-edit drift detection via sha256
- In-file `-- nexmod:freeze` / `-- nexmod:framework` / `-- nexmod:pin`
  directives parsed and re-emitted
- `tests/test_reconciler.py` — 363 lines of reconciler test coverage

---

## [0.2.0] — Profiles + Dependencies

- Named profile system for hotswapping load orders
- Missing-dependency detection during `update` with `--fix-deps` flag
- `info` command for detailed mod view
- `list --json` machine-readable output
- `profile load --install` installs missing profile mods
- WAL journal mode + history index
- Atomic downloads with MD5 verification
- Accepts Nexus Mods URLs directly in `install` and `track`
- Mod folder name capture at install time (for reliable `--purge`)

## [0.1.0] — Initial Release

- Install / track / update / remove mods by ID
- Vortex import via `vortex.deployment.json`
- Darktide enable/disable via dtkit-patch through Wine
- Steam (native + Flatpak) auto-detection
- SQLite history log
