"""Tests for parse_nexus_url — URL → (game_slug, mod_id, file_id)."""
import pytest
import nexmod


def test_basic_url():
    game, mod_id, file_id = nexmod.parse_nexus_url(
        "https://www.nexusmods.com/warhammer40kdarktide/mods/1234"
    )
    assert game    == "darktide"
    assert mod_id  == 1234
    assert file_id is None


def test_url_with_files_path():
    game, mod_id, file_id = nexmod.parse_nexus_url(
        "https://www.nexusmods.com/warhammer40kdarktide/mods/1234/files/5678"
    )
    assert game    == "darktide"
    assert mod_id  == 1234
    assert file_id == 5678


def test_url_with_file_id_query_param():
    game, mod_id, file_id = nexmod.parse_nexus_url(
        "https://www.nexusmods.com/warhammer40kdarktide/mods/1234?tab=files&file_id=5678"
    )
    assert game    == "darktide"
    assert mod_id  == 1234
    assert file_id == 5678


def test_url_path_file_id_takes_priority_over_query():
    # /files/<id> in path should win over ?file_id= — both present is unusual
    # regex captures /files/ group; query param is only checked when path group is absent
    game, mod_id, file_id = nexmod.parse_nexus_url(
        "https://www.nexusmods.com/warhammer40kdarktide/mods/1234/files/5678?file_id=9999"
    )
    assert file_id == 5678  # path wins


def test_url_trailing_query_and_fragment():
    game, mod_id, file_id = nexmod.parse_nexus_url(
        "https://www.nexusmods.com/skyrimspecialedition/mods/42?tab=description#content"
    )
    assert game    == "skyrimse"
    assert mod_id  == 42
    assert file_id is None


def test_known_game_domains():
    domains_and_slugs = [
        ("warhammer40kdarktide", "darktide"),
        ("skyrimspecialedition", "skyrimse"),
        ("baldursgate3",         "bg3"),
        ("cyberpunk2077",        "cyberpunk2077"),
        ("fallout4",             "fallout4"),
    ]
    for domain, expected_slug in domains_and_slugs:
        slug, _, _ = nexmod.parse_nexus_url(
            f"https://www.nexusmods.com/{domain}/mods/1"
        )
        assert slug == expected_slug, f"{domain} → expected {expected_slug}, got {slug}"


def test_unknown_domain_falls_back_to_domain_name(capsys):
    game, mod_id, file_id = nexmod.parse_nexus_url(
        "https://www.nexusmods.com/someunknowngame/mods/999"
    )
    assert game   == "someunknowngame"
    assert mod_id == 999


def test_invalid_url_exits(capsys):
    with pytest.raises(SystemExit):
        nexmod.parse_nexus_url("https://example.com/not-a-nexus-url")


def test_url_case_insensitive():
    # Domain matching should be case-insensitive
    game, mod_id, _ = nexmod.parse_nexus_url(
        "https://www.NEXUSMODS.COM/Warhammer40KDarktide/mods/1234"
    )
    assert game   == "darktide"
    assert mod_id == 1234
