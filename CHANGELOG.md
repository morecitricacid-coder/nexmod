# Changelog

All notable changes to nexmod are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added

- **`games --json`** — machine-readable JSON output listing all supported game slugs with
  name, Nexus domain, and Steam ID. Completes the LLM-readable CLI surface.
- **`fsck --fix --with-api` backfills `installed_files` for flat-layout legacy mods** — mods
  installed before v1.2.0 that extract files directly into an existing directory (e.g.
  Starfield `Data/`) have no `folder_name` and no file manifest, making `remove --purge`
  impossible. With `--with-api`, `fsck` now downloads each such archive to a temp file,
  reads its file manifest via `_list_archive_files`, writes the JSON manifest to the
  `installed_files` column, and immediately deletes the temp file. After backfilling,
  `remove --purge` works on those mods. Requires a Premium Nexus account (archive
  download URLs are Premium-gated). Non-Premium mods are skipped with a clear message.

---

## [1.2.0] — 2026-04-29

### Added

- **`collection install --overwrite`** — forces reinstall of mods that are already tracked
  at the same or newer version. Without this flag, mods with a collection-provided version
  that is older than the locally installed version are skipped with a "would downgrade" note.
- **Flat-install file tracking** (`installed_files` column, schema migration 005) — mods that
  extract files into an existing directory without creating a new top-level folder (e.g. SFSE
  plugins landing in `Data/SFSE/Plugins/`) now have every installed file recorded as a JSON
  list. `remove --purge` uses this manifest to delete individual files when no `folder_name`
  is available.
- **`_list_archive_files`** helper — lists file paths inside any supported archive format
  (zip, tar, 7z) without extracting. Used to populate `installed_files` at install time.

### Changed

- **`collection install`** now classifies already-tracked mods into four buckets before
  prompting: _new_, _needs update_ (collection is newer), _would downgrade_ (you have newer),
  and _up to date_. The summary table shows each count separately. Only new mods and
  needs-update mods are queued by default; `--overwrite` queues everything.
- **`do_install`** gains an `auto_confirm` parameter. When `True`, conflict-folder confirmation
  prompts are skipped and the install proceeds automatically. Used by `collection install`
  so batch installs do not stall waiting for interactive input.
- **`remove --purge`** now supports flat-install mods (those without a `folder_name`). If an
  `installed_files` manifest exists, individual files are deleted one by one; missing files
  are skipped gracefully. If neither `folder_name` nor `installed_files` is recorded, the
  original error message and `--force-legacy-purge` advice are shown unchanged.

### Internal

- `_ver_is_newer` — version comparison helper used by `collection install`. Prefers
  `packaging.version.Version` for correct PEP-440 ordering; falls back to `_norm_version`
  string comparison when `packaging` is unavailable or the version string is non-standard.
- MCP `remove_mod`: also cleans `plugin_files` rows for the removed mod and now records a
  `remove` history entry.

---

## [1.1.0] — Collections support — 2026-04-28

### Added

- **`nexmod collection` command group** — browse, install, and track Nexus Mods Collections.
  - `nexmod collection info <game> <slug>` — fetch and display collection metadata (name,
    author, revision, mod count, description, endorsements, download count). `--json` flag
    for machine-readable output.
  - `nexmod collection install <game> <slug>` — install all mods in the collection using
    the existing install pipeline. Options: `--revision N` (pin to a specific revision),
    `--optional` (also install mods the author marked optional), `--dry-run` (preview the
    mod list without downloading), `--yes` (skip confirmation). Mods already tracked in
    the DB are skipped. Failed mods are skipped with a warning; the rest continue. Free-tier
    accounts get a table of mods that require manual download.
  - `nexmod collection list <game>` — list locally installed collections. `--available`
    queries Nexus for published collections for the game (top N by downloads). `--json`
    for machine-readable output.
- **Schema migration 004** — `collections` table (one row per installed collection with
  slug, name, author, revision, mod count, timestamps) and `collection_mods` junction
  table (maps tracked mods back to the collection they came from). Both tables are added
  via the append-only migrations framework; existing installs receive them automatically
  on next run.
- **Three new GraphQL helpers** — `api_collection_info`, `api_collection_revision`,
  `api_list_collections` — all using the Nexus v2 GraphQL endpoint
  (`api.nexusmods.com/v2/graphql`). Each applies the same retry/429-backoff discipline
  as `nexus_get` but over `requests.post`. Rate limit is shared with v1.

### Internal

- `_gql_post` helper centralises GraphQL POST logic (headers, retry, error extraction).
- `_record_collection` / `_record_collection_mod` — idempotent DB upsert helpers used by
  `collection install`.

---

## [1.0.2] — Auto update check — 2026-04-28

### Added

- Self-update check: nexmod queries PyPI once per 24 h and prints a one-line
  notice when a newer version is available. Fully silent on network errors.

---

## [1.0.1] — Native dtkit-patch, free-tier import, fsck scan — 2026-04-28

### Added
- **`nexmod import <game> <path>`** — free-tier workflow for installing a locally-downloaded
  archive. Parses the Nexus filename convention (`<Name>-<mod_id>-<file_id>-<ver>.zip`) to
  auto-detect the mod ID; prompts to confirm or enter manually. Fetches metadata from the
  free Nexus API, extracts, records in DB, saves a rollback snapshot, and reconciles
  Darktide load order. Supports `--mod-id N`, `-y/--yes`, `--no-reorder`.
- **`nexmod fsck --scan`** — detect untracked subdirectories in the game's mod folder.
  For each unknown folder, searches Nexus (free v2 GraphQL endpoint) for possible matches,
  shows results in a numbered table, and offers to track by selecting a result or entering
  a mod ID directly. Skipping and tracking are both non-destructive. Reports summary at end.
- **`_download_dtkit`** helper — downloads the native Linux static `dtkit-patch` binary from
  the official GitHub release (`ManShanko/dtkit-patch`) and places it in
  `<game_dir>/tools/dtkit-patch` with `0o755` permissions. Called automatically by
  `nexmod setup` for Darktide.

### Changed
- **`nexmod enable` / `disable` / `toggle`** no longer require Wine. `_run_dtkit` now
  detects whether the binary in `<game_dir>/tools/` is native (no `.exe` extension) or a
  Windows binary (`.exe`). Native binary is invoked directly with the real bundle path;
  `.exe` falls back to Wine with the `Z:` path prefix as before.
- `_find_dtkit` now prefers the native Linux binary over `dtkit-patch.exe` when both are
  present in the `tools/` directory.
- `nexmod setup --game darktide` now offers to download the native `dtkit-patch` binary
  automatically after registering the game path. Skips gracefully when declined.
- `nexmod doctor` Darktide check now reports dtkit-patch status (native / Wine .exe /
  missing) instead of a generic Wine availability check.
- `nexmod install darktide` dry-run and pre-check now warn about missing `dtkit-patch`
  rather than missing Wine.
- `nexmod nxm` now catches the "no download URLs / Premium required" error from `do_install`
  and opens the mod's Nexus files page (`?tab=files`) in the system browser, then prints
  the `nexmod import <game> <path>` next-step instruction. Non-Premium-related errors still
  propagate normally.

### Fixed
- Wine was a hard requirement for Darktide `enable`/`disable`/`toggle`; it is now an
  optional fallback used only when the legacy `dtkit-patch.exe` is present and no native
  binary exists.

---

## [1.0.0] — First stable release — 2026-04-28

First stable release. All critical features shipped, tested, and documented.
The install/update/rollback pipeline works end-to-end on a clean Linux machine.
This version also adds native LLM/Claude integration via an MCP server, making
nexmod the first Linux mod manager with typed tool-call support.

### Added
- **MCP server** (`nexmod mcp-server`): optional native LLM integration via the
  [Model Context Protocol](https://modelcontextprotocol.io/). Exposes 12 typed tools
  (`games`, `search_mods`, `list_mods`, `mod_info`, `install_mod`, `check_updates`,
  `update_mod`, `remove_mod`, `get_history`, `list_profiles`, `save_profile`,
  `load_profile`). Install: `pip install nexmod[mcp]`. Add to Claude Code:
  `claude mcp add nexmod -- nexmod mcp-server`.
- `nexmod history` now accepts `--json` / `-j` flag; emits a JSON array of history records for programmatic consumption by LLM agents and scripts.
- `nexmod search` now shows an `Inst.` column (human table) and `"installed": bool` field (JSON) indicating whether each result is already tracked in the local DB.
- `nexmod profile save` now embeds a `"mods"` list in the saved profile JSON — each entry records `mod_id`, `name`, `version`, `folder_name`, and `domain`. Allows `profile load --install` to re-download mods on a clean machine without needing a pre-existing DB.
- `nexmod profile load --install` prefers the embedded `"mods"` list when present; falls back to DB lookup for profiles saved by older versions (backward compatible).

### Fixed
- `nexmod enable`/`disable`/`toggle` on non-Darktide games now exits immediately with a clear message ("does not use dtkit-patch — enable/disable/toggle is Darktide-only") instead of reaching into `_run_dtkit` and producing a confusing `dtkit-patch.exe not found` error.
- `nexmod diag darktide` now warns when bundle files are newer than `dtkit-patch.exe`, indicating a game update may have reset mod support. Re-running `nexmod enable darktide` is suggested.
- Version comparisons in `check` and `update` now normalize versions before comparing: `"1.0"` == `"1.00"` == `"1"`, `"v1.2"` == `"1.2"`. Trailing zero segments and leading `v` prefixes no longer produce false "update available" results.
- `_handle_missing_deps`: when a dep is not in the local DB, the prompt now also prints a `nexmod search <game> <dep>` hint to help users find the dependency.

### Security
- **Security:** `.7z` archives now have path-traversal protection — contents are listed via `7z l -slt` before extraction; any member with an absolute path or `..` component raises `RuntimeError`. Zip and tar already had this check; `.7z` was the gap.

### Notes
- Flatpak Steam users: `nexmod nxm-register` now prints a warning when `~/.var/app/com.valvesoftware.Steam` is detected, explaining that Flatpak browser isolation may prevent NXM link dispatch and providing a manual workaround.
- Internal: schema migration 003 adds `plugin_files` table for Starfield plugin tracking.

---

## [0.9.0] — LLM-first public launch

Public launch release. Built on the hardening work in 0.3.0, this version adds
search, remote mod inspection, and first-run setup — the features that make
nexmod practical both for players and for LLM agents scripting mod management.
The thesis: a mod manager that speaks JSON and can be driven entirely from the
command line, including by automation.

### Added

- `nexmod search <game> <query>` — search Nexus Mods for mods by name using
  the v2 GraphQL API; results sorted by endorsements descending; supports
  `--count N` (1–50, default 10) and `--json` for machine-readable output.
  Enables LLMs and scripts to discover mod IDs without leaving the terminal.
- `nexmod info <game> <mod_id> --remote` — fetch mod info from Nexus without
  requiring the mod to be tracked locally; useful for inspecting a mod before
  deciding to install it. Works without local state.
- `nexmod setup` — interactive first-run wizard: prompts for API key, auto-scans
  Steam for all supported games (or a single `--game <slug>`), and runs `doctor`
  to confirm the environment. `--reset` re-enters an existing key.
- Interactive game path prompt on unknown or undetected games in
  `resolve_mod_dir`: when stdin/stdout are TTYs, nexmod prompts the user to
  enter a path instead of exiting immediately. Non-interactive callers (cron,
  scripts, pipes) still get a hard exit with a clear `nexmod path set` hint.
- Inline dep install prompt in `--fix-deps`: a `[Y/n]` gate now appears before
  each missing dep; the DB is queried by `folder_name` before prompting for a
  URL — known mods install directly without requiring a Nexus URL.
- `nexmod check <game> --json` — machine-readable staleness check; emits one
  object per mod with `mod_id`, `installed`, `latest`, `update_available`,
  `error`. Structured output for automation and LLM pipelines.
- `nexmod update <game> --json` — machine-readable update run; implies `--yes`;
  emits `updated`/`current`/`failed`/`load_order` summary object.
- `doctor` now prints a "→ Next:" hint after a clean pass, guiding first-time
  users to the next step.

### Fixed

- README "From source" install used `pip install -e . --user`; corrected to
  `pip install -e ".[dev]"` so dev dependencies (pytest, responses) are
  included when installing for development.

### Internal

- 430 tests, 0.84s runtime. New test files added this cycle:
  - `tests/test_darktide_actions.py` — 149 tests across 18 test classes covering
    the full Darktide user journey end-to-end (install, enable/disable, order,
    pins, profiles, rollback, history, diag, nxm, search).
  - `tests/test_search.py` — 30 tests for `nexmod search` (v2 GraphQL) and
    `nexmod info --remote` (API lookup without local tracking).
- Nexus API v2 GraphQL search uses `nameStemmed` + WILDCARD `*query*` — not
  `name` — which returns zero hits. Endpoint not Premium-gated.
- `info --remote` defers `get_api_key()` to the latest possible moment so
  untracked-mod errors surface before any API interaction.

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
