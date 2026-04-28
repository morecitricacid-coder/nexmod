"""Tests for `nexmod search` command and `nexmod info --remote` flag.

All tests use isolated_dirs to redirect DB/config paths and monkeypatch
requests.post so no real network calls are made.
"""
import json
import pytest
import requests

import nexmod
from nexmod import cli

pytestmark = pytest.mark.usefixtures("isolated_dirs")

# ── helpers ───────────────────────────────────────────────────────────────────

def _graphql_response(nodes, total=None):
    """Build a minimal v2 GraphQL success payload."""
    return {
        "data": {
            "mods": {
                "totalCount": total if total is not None else len(nodes),
                "nodes": nodes,
            }
        }
    }


def _node(mod_id=1234, name="Test Mod", summary="A helpful summary.",
          downloads=10000, endorsements=500):
    return {
        "modId": mod_id,
        "name": name,
        "summary": summary,
        "downloads": downloads,
        "endorsements": endorsements,
    }


def _mock_post(monkeypatch, payload, status=200):
    """Replace requests.post with a stub returning the given payload."""
    class FakeResponse:
        status_code = status
        text = json.dumps(payload)[:200]

        def json(self):
            return payload

    monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResponse())


# ── search: happy path ────────────────────────────────────────────────────────

class TestSearchHappyPath:
    def test_search_shows_mod_name(self, runner, api_key_config, monkeypatch):
        """Basic search: mod name appears in output."""
        _mock_post(monkeypatch, _graphql_response([_node(name="Enemy Health Bars")]))
        result = runner.invoke(cli, ["search", "darktide", "enemy health"])
        assert result.exit_code == 0, result.output
        assert "Enemy Health Bars" in result.output

    def test_search_shows_mod_id(self, runner, api_key_config, monkeypatch):
        """Mod ID is printed in the table."""
        _mock_post(monkeypatch, _graphql_response([_node(mod_id=9876)]))
        result = runner.invoke(cli, ["search", "darktide", "query"])
        assert result.exit_code == 0
        assert "9876" in result.output

    def test_search_shows_searching_message(self, runner, api_key_config, monkeypatch):
        """Human output includes a 'Searching' status line."""
        _mock_post(monkeypatch, _graphql_response([_node()]))
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code == 0
        assert "Searching" in result.output or "search" in result.output.lower()

    def test_search_shows_install_hint(self, runner, api_key_config, monkeypatch):
        """Output ends with an install hint."""
        _mock_post(monkeypatch, _graphql_response([_node()]))
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code == 0
        assert "nexmod install" in result.output

    def test_search_multiple_results(self, runner, api_key_config, monkeypatch):
        """All returned mods appear in output."""
        nodes = [
            _node(mod_id=100, name="Mod Alpha", endorsements=200),
            _node(mod_id=200, name="Mod Beta", endorsements=100),
        ]
        _mock_post(monkeypatch, _graphql_response(nodes))
        result = runner.invoke(cli, ["search", "darktide", "mod"])
        assert result.exit_code == 0
        assert "Mod Alpha" in result.output
        assert "Mod Beta" in result.output


# ── search: JSON output ───────────────────────────────────────────────────────

class TestSearchJson:
    def test_json_flag_parses(self, runner, api_key_config, monkeypatch):
        """`--json` output is valid JSON."""
        _mock_post(monkeypatch, _graphql_response([_node()]))
        result = runner.invoke(cli, ["search", "darktide", "test", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_json_schema(self, runner, api_key_config, monkeypatch):
        """JSON rows contain the expected keys."""
        _mock_post(monkeypatch, _graphql_response([_node(mod_id=42, name="FooMod",
                                                          downloads=1111, endorsements=222)]))
        result = runner.invoke(cli, ["search", "darktide", "foo", "--json"])
        assert result.exit_code == 0
        rows = json.loads(result.output)
        assert len(rows) == 1
        row = rows[0]
        assert row["mod_id"] == 42
        assert row["name"] == "FooMod"
        assert row["downloads"] == 1111
        assert row["endorsements"] == 222
        assert "summary" in row

    def test_json_no_searching_line(self, runner, api_key_config, monkeypatch):
        """`--json` suppresses the 'Searching…' status line."""
        _mock_post(monkeypatch, _graphql_response([_node()]))
        result = runner.invoke(cli, ["search", "darktide", "test", "--json"])
        assert result.exit_code == 0
        # Output must be pure JSON — no prose before the bracket.
        assert result.output.lstrip().startswith("[")

    def test_json_empty_results(self, runner, api_key_config, monkeypatch):
        """`--json` with no results emits an empty JSON array."""
        _mock_post(monkeypatch, _graphql_response([]))
        result = runner.invoke(cli, ["search", "darktide", "nothing", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_json_sorted_by_endorsements_desc(self, runner, api_key_config, monkeypatch):
        """JSON output is sorted by endorsements descending."""
        nodes = [
            _node(mod_id=1, name="Low", endorsements=10),
            _node(mod_id=2, name="High", endorsements=999),
            _node(mod_id=3, name="Mid", endorsements=500),
        ]
        _mock_post(monkeypatch, _graphql_response(nodes))
        result = runner.invoke(cli, ["search", "darktide", "q", "--json"])
        assert result.exit_code == 0
        rows = json.loads(result.output)
        endorsements = [r["endorsements"] for r in rows]
        assert endorsements == sorted(endorsements, reverse=True)


# ── search: no results ────────────────────────────────────────────────────────

class TestSearchNoResults:
    def test_no_results_message(self, runner, api_key_config, monkeypatch):
        """Empty node list prints 'No results found.' and exits 0."""
        _mock_post(monkeypatch, _graphql_response([]))
        result = runner.invoke(cli, ["search", "darktide", "xyzzy"])
        assert result.exit_code == 0
        assert "no results" in result.output.lower()

    def test_no_results_exit_zero(self, runner, api_key_config, monkeypatch):
        """Empty results is not an error — exit code 0."""
        _mock_post(monkeypatch, _graphql_response([]))
        result = runner.invoke(cli, ["search", "darktide", "xyzzy"])
        assert result.exit_code == 0


# ── search: --count flag ──────────────────────────────────────────────────────

class TestSearchCount:
    def test_count_passed_to_api(self, runner, api_key_config, monkeypatch):
        """--count is clamped and forwarded to api_search_mods."""
        captured = {}

        def fake_search(domain, query, api_key, count=10):
            captured["count"] = count
            return []

        monkeypatch.setattr(nexmod, "api_search_mods", fake_search)
        runner.invoke(cli, ["search", "darktide", "q", "--count", "25"])
        assert captured.get("count") == 25

    def test_count_clamped_to_50(self, runner, api_key_config, monkeypatch):
        """Values above 50 are clamped to 50."""
        captured = {}

        def fake_search(domain, query, api_key, count=10):
            captured["count"] = count
            return []

        monkeypatch.setattr(nexmod, "api_search_mods", fake_search)
        runner.invoke(cli, ["search", "darktide", "q", "--count", "999"])
        assert captured.get("count") <= 50

    def test_count_clamped_to_1(self, runner, api_key_config, monkeypatch):
        """Values below 1 are clamped to 1."""
        captured = {}

        def fake_search(domain, query, api_key, count=10):
            captured["count"] = count
            return []

        monkeypatch.setattr(nexmod, "api_search_mods", fake_search)
        runner.invoke(cli, ["search", "darktide", "q", "--count", "0"])
        assert captured.get("count") >= 1


# ── search: formatting ────────────────────────────────────────────────────────

class TestSearchFormatting:
    def test_summary_truncated_at_55_chars(self, runner, api_key_config, monkeypatch):
        """Summaries longer than 55 chars are truncated with an ellipsis."""
        long_summary = "A" * 60
        _mock_post(monkeypatch, _graphql_response([_node(summary=long_summary)]))
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code == 0
        # The full 60-char string should NOT appear; a truncated version with … should.
        assert long_summary not in result.output
        assert "…" in result.output

    def test_short_summary_not_truncated(self, runner, api_key_config, monkeypatch):
        """Summaries at or under 55 chars are shown verbatim."""
        short = "Short summary."
        _mock_post(monkeypatch, _graphql_response([_node(summary=short)]))
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code == 0
        assert short in result.output

    def test_downloads_formatted_with_commas(self, runner, api_key_config, monkeypatch):
        """Download counts are formatted with comma separators."""
        _mock_post(monkeypatch, _graphql_response([_node(downloads=1234567)]))
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code == 0
        assert "1,234,567" in result.output

    def test_endorsements_formatted_with_commas(self, runner, api_key_config, monkeypatch):
        """Endorsement counts are formatted with comma separators."""
        _mock_post(monkeypatch, _graphql_response([_node(endorsements=9876)]))
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code == 0
        assert "9,876" in result.output


# ── search: error paths ───────────────────────────────────────────────────────

class TestSearchErrors:
    def test_missing_api_key_exits_nonzero(self, runner, monkeypatch):
        """Search without an API key exits non-zero."""
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code != 0

    def test_unknown_game_exits_nonzero(self, runner, api_key_config, monkeypatch):
        """Passing a game slug not in GAMES exits non-zero."""
        result = runner.invoke(cli, ["search", "notarealgame", "test"])
        assert result.exit_code != 0
        assert "Unknown game" in result.output or "notarealgame" in result.output

    def test_non_200_response_exits_nonzero(self, runner, api_key_config, monkeypatch):
        """HTTP 500 from GraphQL prints an error and exits non-zero."""
        _mock_post(monkeypatch, {"error": "internal"}, status=500)
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code != 0
        assert "Search failed" in result.output or "error" in result.output.lower()

    def test_missing_data_key_returns_empty(self, runner, api_key_config, monkeypatch):
        """GraphQL response without 'data' key is treated as no results."""
        _mock_post(monkeypatch, {})
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code == 0
        # Should show no results, not crash.
        assert "no results" in result.output.lower()

    def test_missing_nodes_key_returns_empty(self, runner, api_key_config, monkeypatch):
        """GraphQL response with 'data.mods' but no 'nodes' is treated as no results."""
        _mock_post(monkeypatch, {"data": {"mods": {"totalCount": 0}}})
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code == 0
        assert "no results" in result.output.lower()

    def test_network_error_exits_nonzero(self, runner, api_key_config, monkeypatch):
        """requests.post raising ConnectionError exits non-zero with an error message."""
        def raise_conn(*a, **kw):
            raise requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(requests, "post", raise_conn)
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code != 0
        assert "Search failed" in result.output or "error" in result.output.lower()


# ── info --remote ─────────────────────────────────────────────────────────────

class TestInfoRemote:
    def test_remote_shows_mod_info(self, runner, api_key_config, monkeypatch):
        """`info --remote` displays the mod name and version from the API."""
        monkeypatch.setattr(
            nexmod, "nexus_get",
            lambda *a, **kw: {
                "name": "Remote Mod",
                "version": "3.1.4",
                "summary": "A remote mod summary.",
                "author": "SomeAuthor",
            },
        )
        result = runner.invoke(cli, ["info", "darktide", "5555", "--remote"])
        assert result.exit_code == 0, result.output
        assert "Remote Mod" in result.output
        assert "3.1.4" in result.output

    def test_remote_shows_install_hint(self, runner, api_key_config, monkeypatch):
        """`info --remote` includes an install command hint in the output."""
        monkeypatch.setattr(
            nexmod, "nexus_get",
            lambda *a, **kw: {"name": "Any Mod", "version": "1.0"},
        )
        result = runner.invoke(cli, ["info", "darktide", "5555", "--remote"])
        assert result.exit_code == 0
        assert "nexmod install" in result.output

    def test_remote_mod_not_found_exits_nonzero(self, runner, api_key_config, monkeypatch):
        """When the API returns an error (404), --remote exits non-zero."""
        def raise_http(*a, **kw):
            raise Exception("HTTP 404")

        monkeypatch.setattr(nexmod, "nexus_get", raise_http)
        result = runner.invoke(cli, ["info", "darktide", "9999", "--remote"])
        assert result.exit_code != 0
        assert "Could not fetch" in result.output or "404" in result.output

    def test_remote_requires_api_key(self, runner, monkeypatch):
        """`info --remote` without API key configured exits non-zero."""
        result = runner.invoke(cli, ["info", "darktide", "1234", "--remote"])
        assert result.exit_code != 0

    def test_info_without_remote_still_needs_tracking(
            self, runner, api_key_config, monkeypatch):
        """Without --remote, `info` on untracked mod still exits 1 with 'not tracked'."""
        result = runner.invoke(cli, ["info", "darktide", "8888"])
        assert result.exit_code != 0
        assert "not tracked" in result.output.lower()


# ── Fix 2: search shows installed state ──────────────────────────────────────

class TestSearchInstalledState:
    """search --json includes 'installed': true when the mod is in the local DB."""

    def test_json_installed_true_for_tracked_mod(self, runner, api_key_config, monkeypatch):
        """installed=true when the mod_id is already in the DB."""
        db = nexmod.get_db()
        db.execute("""
            INSERT INTO mods (game, mod_id, file_id, name, mod_dir, tracked_at)
            VALUES ('darktide', 1234, 1, 'Installed Mod', '/tmp', '2026-01-01')
        """)
        db.commit()

        _mock_post(monkeypatch, _graphql_response([_node(mod_id=1234)]))
        result = runner.invoke(cli, ["search", "darktide", "test", "--json"])
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert len(out) == 1
        assert out[0]["installed"] is True

    def test_json_installed_false_for_untracked_mod(self, runner, api_key_config, monkeypatch):
        """installed=false when the mod_id is not in the DB."""
        _mock_post(monkeypatch, _graphql_response([_node(mod_id=9999)]))
        result = runner.invoke(cli, ["search", "darktide", "test", "--json"])
        assert result.exit_code == 0
        out = json.loads(result.output)
        assert len(out) == 1
        assert out[0]["installed"] is False

    def test_json_schema_has_installed_field(self, runner, api_key_config, monkeypatch):
        """Every JSON result object must have an 'installed' key."""
        _mock_post(monkeypatch, _graphql_response([_node(mod_id=111), _node(mod_id=222)]))
        result = runner.invoke(cli, ["search", "darktide", "test", "--json"])
        assert result.exit_code == 0
        out = json.loads(result.output)
        for item in out:
            assert "installed" in item

    def test_table_shows_inst_column(self, runner, api_key_config, monkeypatch):
        """Human-readable output should include the 'Inst.' column header."""
        _mock_post(monkeypatch, _graphql_response([_node(mod_id=111)]))
        result = runner.invoke(cli, ["search", "darktide", "test"])
        assert result.exit_code == 0
        assert "Inst." in result.output
