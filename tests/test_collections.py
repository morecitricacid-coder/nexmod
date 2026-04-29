"""Tests for the `nexmod collection` command group.

Covers: info, list (local + --available), install (happy path, already-tracked,
optional skip, install failure skip, dry-run, free-tier detection).

All network calls are intercepted via monkeypatch on requests.post / requests.get.
No real network traffic ever occurs.
"""
import json
import sys
import pytest
import requests

import nexmod
from nexmod import cli

pytestmark = pytest.mark.usefixtures("isolated_dirs")


# ── Payload factories ─────────────────────────────────────────────────────────

def _col(slug="testslug", name="Test Collection", summary="A fine set of mods.",
         author="TestAuthor", rev=3, mod_count=5, endorsements=42, downloads=1000,
         game_id=4943):
    return {
        "name": name,
        "summary": summary,
        "description": "# Full Description\n\nSome long text.",
        "slug": slug,
        "id": 99999,
        "gameId": game_id,
        "endorsements": endorsements,
        "totalDownloads": downloads,
        "user": {"name": author, "memberId": 123},
        "latestPublishedRevision": {
            "revisionNumber": rev,
            "modCount": mod_count,
            "assetsSizeBytes": "12345",
        },
    }


def _revision(slug="testslug", rev=3, mod_files=None):
    if mod_files is None:
        mod_files = [
            _mod_file(mod_id=100, file_id=200, name="Alpha Mod", version="1.0"),
            _mod_file(mod_id=101, file_id=201, name="Beta Mod", version="2.0"),
        ]
    return {
        "revisionNumber": rev,
        "modCount": len(mod_files),
        "modFiles": mod_files,
    }


def _mod_file(mod_id=100, file_id=200, name="Some Mod", version="1.0",
              optional=False, size_bytes="50000"):
    return {
        "optional": optional,
        "updatePolicy": "latest",
        "file": {
            "modId": mod_id,
            "fileId": file_id,
            "name": name,
            "version": version,
            "sizeInBytes": size_bytes,
        },
    }


def _collections_list(nodes, total=None):
    return {
        "totalCount": total if total is not None else len(nodes),
        "nodes": nodes,
    }


def _col_node(slug="testslug", name="Test Collection", summary="Summary.",
              endorsements=10, downloads=500, rev=1, mod_count=3):
    return {
        "slug": slug,
        "name": name,
        "summary": summary,
        "id": 88888,
        "gameId": 4943,
        "endorsements": endorsements,
        "totalDownloads": downloads,
        "latestPublishedRevision": {
            "revisionNumber": rev,
            "modCount": mod_count,
        },
    }


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _gql_response(*payloads):
    """Return a combined data dict for multiple top-level GQL keys."""
    data = {}
    for p in payloads:
        data.update(p)
    return {"data": data}


class _FakePostResponse:
    """Mimics requests.Response for monkeypatched requests.post."""

    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)[:300]

    def json(self):
        return self._payload

    @property
    def ok(self):
        return self.status_code < 400


class _PostDispatcher:
    """Dispatch multiple sequential GQL calls from a list of payloads."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self._idx = 0

    def __call__(self, *args, **kwargs):
        if self._idx < len(self._payloads):
            p = self._payloads[self._idx]
            self._idx += 1
        else:
            p = self._payloads[-1]
        return _FakePostResponse(p)


def _mock_post_single(monkeypatch, payload, status=200):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakePostResponse(payload, status))


def _mock_post_sequence(monkeypatch, payloads):
    dispatcher = _PostDispatcher(payloads)
    monkeypatch.setattr(requests, "post", dispatcher)


def _mock_validate_get(monkeypatch, premium=True):
    """Mock nexus_get for users/validate.json only."""
    orig_get = nexmod.nexus_get

    def patched_get(endpoint, api_key):
        if "validate" in endpoint:
            return {"name": "TestUser", "is_premium": premium, "is_supporter": False}
        return orig_get(endpoint, api_key)

    monkeypatch.setattr(nexmod, "nexus_get", patched_get)


# ── collection info ───────────────────────────────────────────────────────────

class TestCollectionInfo:
    def test_info_shows_name(self, runner, api_key_config, monkeypatch):
        _mock_post_single(monkeypatch, _gql_response({"collection": _col(name="Awesome Mods")}))
        result = runner.invoke(cli, ["collection", "info", "darktide", "testslug"])
        assert result.exit_code == 0, result.output
        assert "Awesome Mods" in result.output

    def test_info_shows_author(self, runner, api_key_config, monkeypatch):
        _mock_post_single(monkeypatch, _gql_response({"collection": _col(author="TheAuthor")}))
        result = runner.invoke(cli, ["collection", "info", "darktide", "testslug"])
        assert result.exit_code == 0
        assert "TheAuthor" in result.output

    def test_info_shows_revision(self, runner, api_key_config, monkeypatch):
        _mock_post_single(monkeypatch, _gql_response({"collection": _col(rev=17, mod_count=24)}))
        result = runner.invoke(cli, ["collection", "info", "darktide", "testslug"])
        assert result.exit_code == 0
        assert "17" in result.output
        assert "24" in result.output

    def test_info_json_output(self, runner, api_key_config, monkeypatch):
        _mock_post_single(monkeypatch, _gql_response({
            "collection": _col(slug="abc123", name="JSON Col", rev=5, mod_count=10)
        }))
        result = runner.invoke(cli, ["collection", "info", "darktide", "abc123", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["slug"] == "abc123"
        assert data["name"] == "JSON Col"
        assert data["revision"] == 5
        assert data["mod_count"] == 10

    def test_info_not_found_exits_1(self, runner, api_key_config, monkeypatch):
        _mock_post_single(monkeypatch, {
            "errors": [{"message": "Collection not found",
                        "extensions": {"code": "NOT_FOUND"}}],
            "data": None,
        })
        result = runner.invoke(cli, ["collection", "info", "darktide", "badslug"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_info_works_for_unknown_game(self, runner, api_key_config, monkeypatch):
        """Unknown game slug falls back to using it as the domain."""
        _mock_post_single(monkeypatch, _gql_response({"collection": _col(name="Generic Collection")}))
        result = runner.invoke(cli, ["collection", "info", "unknowngame", "testslug"])
        assert result.exit_code == 0
        assert "Generic Collection" in result.output


# ── collection list (local) ───────────────────────────────────────────────────

class TestCollectionListLocal:
    def test_list_empty(self, runner, api_key_config):
        result = runner.invoke(cli, ["collection", "list", "darktide"])
        assert result.exit_code == 0
        assert "No collections installed" in result.output

    def test_list_shows_installed(self, runner, api_key_config, monkeypatch):
        # Pre-install a collection into the DB via the helper.
        db = nexmod.get_db()
        nexmod._record_collection(
            db, "darktide", "myslug",
            _col(slug="myslug", name="My Collection", author="Author1"),
            _revision(slug="myslug"),
        )
        result = runner.invoke(cli, ["collection", "list", "darktide"])
        assert result.exit_code == 0
        assert "myslug" in result.output
        assert "My Collection" in result.output

    def test_list_json_output(self, runner, api_key_config):
        db = nexmod.get_db()
        nexmod._record_collection(
            db, "darktide", "slugjson",
            _col(slug="slugjson", name="JSON Collection"),
            _revision(slug="slugjson"),
        )
        result = runner.invoke(cli, ["collection", "list", "darktide", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["game"] == "darktide"
        assert len(data["installed"]) == 1
        assert data["installed"][0]["slug"] == "slugjson"

    def test_list_empty_json(self, runner, api_key_config):
        result = runner.invoke(cli, ["collection", "list", "darktide", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["installed"] == []


# ── collection list --available ───────────────────────────────────────────────

class TestCollectionListAvailable:
    def test_available_shows_slugs(self, runner, api_key_config, monkeypatch):
        payload = _gql_response({
            "collectionsV2": _collections_list([
                _col_node(slug="abc", name="Collection A"),
                _col_node(slug="xyz", name="Collection Z"),
            ])
        })
        _mock_post_single(monkeypatch, payload)
        result = runner.invoke(cli, ["collection", "list", "darktide", "--available"])
        assert result.exit_code == 0
        assert "abc" in result.output
        assert "Collection A" in result.output
        assert "xyz" in result.output

    def test_available_json(self, runner, api_key_config, monkeypatch):
        payload = _gql_response({
            "collectionsV2": _collections_list([
                _col_node(slug="s1", name="First", endorsements=99, downloads=5000),
            ], total=1)
        })
        _mock_post_single(monkeypatch, payload)
        result = runner.invoke(cli, ["collection", "list", "darktide", "--available", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total_available"] == 1
        assert data["collections"][0]["slug"] == "s1"
        assert data["collections"][0]["endorsements"] == 99

    def test_available_api_error(self, runner, api_key_config, monkeypatch):
        _mock_post_single(monkeypatch, {
            "errors": [{"message": "Server error"}],
            "data": None,
        })
        result = runner.invoke(cli, ["collection", "list", "darktide", "--available"])
        assert result.exit_code == 1


# ── collection install ────────────────────────────────────────────────────────

class TestCollectionInstallDryRun:
    def test_dry_run_shows_mod_ids(self, runner, api_key_config, monkeypatch):
        """--dry-run prints mod table without downloading."""
        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision(mod_files=[
                _mod_file(mod_id=111, file_id=222, name="Mod One"),
                _mod_file(mod_id=333, file_id=444, name="Mod Two"),
            ])}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=True)
        result = runner.invoke(cli, ["collection", "install", "darktide", "testslug", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "111" in result.output
        assert "333" in result.output
        assert "Dry run" in result.output

    def test_dry_run_no_download(self, runner, api_key_config, monkeypatch):
        """Ensure do_install is never called during dry-run."""
        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision()}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=True)
        install_called = []
        monkeypatch.setattr(nexmod, "do_install", lambda *a, **kw: install_called.append(1))
        runner.invoke(cli, ["collection", "install", "darktide", "testslug", "--dry-run"])
        assert install_called == [], "do_install must not be called during --dry-run"

    def test_dry_run_shows_optional_flag(self, runner, api_key_config, monkeypatch):
        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision(mod_files=[
                _mod_file(mod_id=10, optional=False),
                _mod_file(mod_id=11, optional=True),
            ])}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=True)
        result = runner.invoke(
            cli,
            ["collection", "install", "darktide", "testslug", "--dry-run", "--optional"],
        )
        assert result.exit_code == 0
        # both mod IDs appear in the table
        assert "10" in result.output
        assert "11" in result.output


class TestCollectionInstallAlreadyTracked:
    def test_already_tracked_skipped(self, runner, api_key_config, monkeypatch):
        """Mods already in the DB are skipped; nothing is downloaded."""
        # Pre-seed mod 100 as already tracked.
        db = nexmod.get_db()
        db.execute(
            "INSERT INTO mods (game, mod_id, file_id, name, version, filename, mod_dir, tracked_at) "
            "VALUES ('darktide', 100, 200, 'Pre-installed Mod', '1.0', 'x.zip', '/mods', '2024-01-01')"
        )
        db.commit()

        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision(mod_files=[
                _mod_file(mod_id=100, file_id=200, name="Pre-installed Mod"),
            ])}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=True)

        install_called = []
        monkeypatch.setattr(nexmod, "do_install", lambda *a, **kw: install_called.append(1))

        result = runner.invoke(
            cli, ["collection", "install", "darktide", "testslug", "--yes"]
        )
        assert result.exit_code == 0
        assert install_called == [], "Already-tracked mod should not trigger do_install"
        assert "Nothing to install" in result.output or "already tracked" in result.output.lower()

    def test_already_tracked_recorded_in_junction(self, runner, api_key_config, monkeypatch):
        """Junction table entry is written even for already-tracked mods."""
        db = nexmod.get_db()
        db.execute(
            "INSERT INTO mods (game, mod_id, file_id, name, version, filename, mod_dir, tracked_at) "
            "VALUES ('darktide', 100, 200, 'Pre-installed Mod', '1.0', 'x.zip', '/mods', '2024-01-01')"
        )
        db.commit()

        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision(mod_files=[
                _mod_file(mod_id=100, file_id=200, name="Pre-installed Mod"),
            ])}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=True)
        monkeypatch.setattr(nexmod, "do_install", lambda *a, **kw: None)

        runner.invoke(cli, ["collection", "install", "darktide", "testslug", "--yes"])

        # Junction row should exist.
        row = db.execute(
            "SELECT * FROM collection_mods WHERE game='darktide' AND slug='testslug' AND mod_id=100"
        ).fetchone()
        assert row is not None


class TestCollectionInstallHappyPath:
    def test_installs_mods_and_records_collection(self, runner, api_key_config, monkeypatch):
        """Successful install records collection + mod rows in DB."""
        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision(mod_files=[
                _mod_file(mod_id=200, file_id=300, name="Good Mod"),
            ])}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=True)

        install_called_with = []

        def fake_install(game, mod_id, file_id, api_key, db_conn, **kwargs):
            install_called_with.append((game, mod_id, file_id))

        monkeypatch.setattr(nexmod, "do_install", fake_install)

        result = runner.invoke(
            cli, ["collection", "install", "darktide", "testslug", "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert ("darktide", 200, 300) in install_called_with

        db = nexmod.get_db()
        col_row = db.execute(
            "SELECT * FROM collections WHERE game='darktide' AND slug='testslug'"
        ).fetchone()
        assert col_row is not None
        assert col_row["name"] == "Test Collection"

    def test_optional_excluded_by_default(self, runner, api_key_config, monkeypatch):
        """Optional mods are not installed unless --optional is passed."""
        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision(mod_files=[
                _mod_file(mod_id=10, optional=False, name="Required"),
                _mod_file(mod_id=11, optional=True, name="Extras"),
            ])}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=True)

        install_called_with = []
        monkeypatch.setattr(nexmod, "do_install",
                            lambda game, mod_id, *a, **kw: install_called_with.append(mod_id))

        runner.invoke(cli, ["collection", "install", "darktide", "testslug", "--yes"])
        assert 10 in install_called_with
        assert 11 not in install_called_with, "Optional mod must not be installed by default"

    def test_optional_included_with_flag(self, runner, api_key_config, monkeypatch):
        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision(mod_files=[
                _mod_file(mod_id=10, optional=False),
                _mod_file(mod_id=11, optional=True),
            ])}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=True)

        install_called_with = []
        monkeypatch.setattr(nexmod, "do_install",
                            lambda game, mod_id, *a, **kw: install_called_with.append(mod_id))

        runner.invoke(
            cli, ["collection", "install", "darktide", "testslug", "--yes", "--optional"]
        )
        assert 10 in install_called_with
        assert 11 in install_called_with


class TestCollectionInstallFailureHandling:
    def test_failed_mod_skipped_rest_continues(self, runner, api_key_config, monkeypatch):
        """A RuntimeError on one mod is skipped; subsequent mods still install."""
        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision(mod_files=[
                _mod_file(mod_id=10, file_id=100, name="Mod A"),
                _mod_file(mod_id=20, file_id=200, name="Mod B"),
                _mod_file(mod_id=30, file_id=300, name="Mod C"),
            ])}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=True)

        installed = []

        def fake_install(game, mod_id, file_id, api_key, db_conn, **kwargs):
            if mod_id == 20:
                raise RuntimeError("something went wrong")
            installed.append(mod_id)

        monkeypatch.setattr(nexmod, "do_install", fake_install)

        result = runner.invoke(
            cli, ["collection", "install", "darktide", "testslug", "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert 10 in installed
        assert 20 not in installed
        assert 30 in installed
        assert "skip" in result.output.lower() or "fail" in result.output.lower() or "warn" in result.output.lower() or "20" in result.output

    def test_sysexit_from_do_install_treated_as_failure(self, runner, api_key_config, monkeypatch):
        """sys.exit from do_install is caught and treated as a per-mod failure."""
        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision(mod_files=[
                _mod_file(mod_id=10, file_id=100),
                _mod_file(mod_id=20, file_id=200),
            ])}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=True)

        installed = []

        def fake_install(game, mod_id, file_id, api_key, db_conn, **kwargs):
            if mod_id == 10:
                sys.exit(1)
            installed.append(mod_id)

        monkeypatch.setattr(nexmod, "do_install", fake_install)

        result = runner.invoke(
            cli, ["collection", "install", "darktide", "testslug", "--yes"]
        )
        # CLI runner catches SystemExit; collection command should swallow it.
        assert 20 in installed

    def test_free_user_queued_for_manual(self, runner, api_key_config, monkeypatch):
        """Free accounts get a manual-install list for Premium-gated mods."""
        post_payloads = [
            _gql_response({"collection": _col()}),
            _gql_response({"collectionRevision": _revision(mod_files=[
                _mod_file(mod_id=55, file_id=66, name="PremiumOnlyMod"),
            ])}),
        ]
        _mock_post_sequence(monkeypatch, post_payloads)
        _mock_validate_get(monkeypatch, premium=False)

        def fake_install_free(game, mod_id, file_id, api_key, db_conn):
            raise RuntimeError("no download urls — Premium required for direct downloads")

        monkeypatch.setattr(nexmod, "do_install", fake_install_free)

        result = runner.invoke(
            cli, ["collection", "install", "darktide", "testslug", "--yes"]
        )
        assert result.exit_code == 0
        # Should surface the mod in the manual-install table.
        assert "55" in result.output or "Premium" in result.output


# ── collection DB helpers ─────────────────────────────────────────────────────

class TestCollectionDbHelpers:
    def test_record_collection_idempotent(self):
        """_record_collection is safe to call twice (upsert)."""
        db = nexmod.get_db()
        col = _col(slug="dup")
        rev = _revision()
        nexmod._record_collection(db, "darktide", "dup", col, rev)
        nexmod._record_collection(db, "darktide", "dup", col, rev)
        rows = db.execute(
            "SELECT COUNT(*) FROM collections WHERE slug='dup'"
        ).fetchone()[0]
        assert rows == 1

    def test_record_collection_mod_idempotent(self):
        """_record_collection_mod is safe to call twice (INSERT OR IGNORE)."""
        db = nexmod.get_db()
        nexmod._record_collection_mod(db, "darktide", "slu", 999)
        nexmod._record_collection_mod(db, "darktide", "slu", 999)
        rows = db.execute(
            "SELECT COUNT(*) FROM collection_mods WHERE slug='slu' AND mod_id=999"
        ).fetchone()[0]
        assert rows == 1

    def test_schema_migrations_applied(self):
        """Opening the DB applies migration 004 (collections tables exist)."""
        db = nexmod.get_db()
        # These will raise OperationalError if the table doesn't exist.
        db.execute("SELECT * FROM collections LIMIT 0")
        db.execute("SELECT * FROM collection_mods LIMIT 0")

    def test_collections_unique_constraint(self):
        """Duplicate (game, slug) does an upsert, not a second row."""
        db = nexmod.get_db()
        col_v1 = _col(slug="unique", name="Version One")
        col_v2 = _col(slug="unique", name="Version Two")
        rev = _revision()
        nexmod._record_collection(db, "darktide", "unique", col_v1, rev)
        nexmod._record_collection(db, "darktide", "unique", col_v2, rev)
        row = db.execute(
            "SELECT name FROM collections WHERE slug='unique'"
        ).fetchone()
        assert row["name"] == "Version Two"
