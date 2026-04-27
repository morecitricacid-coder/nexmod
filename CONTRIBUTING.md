# Contributing to nexmod

## Dev setup

```bash
git clone https://github.com/morecitricacid-coder/nexmod
cd nexmod
pip install -e ".[dev]"
```

Requires Python 3.10+.

## Running the tests

```bash
pytest -q -m "not smoke"          # fast (no network)
pytest -q -m smoke                 # live Nexus API — requires Premium key in NEXUS_API_KEY
```

Tests are in `tests/`. All filesystem and network I/O is mocked; no Nexus account needed for the default suite.

## Adding a game

Edit the `GAMES` dict near the top of `nexmod.py`. Minimum fields:

```python
"mygame": {
    "name":       "Full Game Name",
    "domain":     "nexus-domain-slug",   # from nexusmods.com/<domain>/mods/...
    "steam_id":   123456,
    "mod_subdir": "Mods",                # relative to the Steam install dir
    "log_subpath": None,                 # or path relative to user data dir
},
```

## Pull requests

- Keep PRs focused — one feature or fix per PR.
- Add or update tests for any changed behaviour.
- Run `pytest -q -m "not smoke"` before opening a PR; CI will catch failures anyway.
- Update `CHANGELOG.md` under `[Unreleased]` with a short entry.
