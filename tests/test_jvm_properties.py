"""server.properties: parsing like Java, edits that leave everything else alone, and validation of what the dashboard sends."""

from __future__ import annotations

import pytest

from pumpkin_manager.jvm import properties as props
from pumpkin_manager.pumpkin_config import ConfigError

SAMPLE = (
    "#Minecraft server properties\n"
    "#Sat Oct 10 12:00:00 UTC 2026\n"
    "allow-flight=false\n"
    "motd=A \\u00A7bServer\n"
    "max-players=20\n"
    "\n"
    "# a comment that mentions server-port=1\n"
    "server-port=25565\n"
    "white-list=false\n"
)


def test_parse_reads_every_separator_style_and_unescapes():
    text = "a=1\nb: 2\nc 3\nd=\n! comment=x\n# other=y\ne=\\u00e4\\tz\nf=x=y:z\n"
    assert props.parse(text) == {"a": "1", "b": "2", "c": "3", "d": "", "e": "ä\tz", "f": "x=y:z"}


def test_the_last_duplicate_wins_and_continuation_lines_are_joined():
    assert props.parse("a=1\na=2\n")["a"] == "2"
    assert props.parse("a=one \\\n    two\nb=3\n") == {"a": "one two", "b": "3"}
    assert props.parse("# note \\\nb=3\n") == {"b": "3"}, "a comment is never continued"


def test_escape_and_unescape_round_trip_including_characters_outside_the_basic_plane():
    for text in ("plain", " leading space", "§bColour", "tab\there", "back\\slash", "emoji \U0001F600 here", "line\nbreak"):
        escaped = props.escape_value(text)
        assert escaped.isascii() and "\n" not in escaped
        assert props.unescape(escaped) == text
    assert props.escape_value("§") == "\\u00A7"


def test_apply_changes_only_the_requested_lines():
    new = props.apply(SAMPLE, {"max-players": "50", "server-port": "25570", "difficulty": "hard"})
    assert new.splitlines()[:3] == SAMPLE.splitlines()[:3]
    assert "# a comment that mentions server-port=1" in new, "comments are left alone"
    parsed = props.parse(new)
    assert parsed["max-players"] == "50" and parsed["server-port"] == "25570" and parsed["difficulty"] == "hard"
    assert parsed["motd"] == "A §bServer" and parsed["white-list"] == "false"
    assert new.endswith("difficulty=hard\n"), "a key the file did not have yet is appended"
    assert props.apply(SAMPLE, {}) == SAMPLE


def test_apply_edits_the_duplicate_that_counts_and_keeps_windows_line_endings_readable():
    assert props.parse(props.apply("a=1\r\nb=2\r\na=3\r\n", {"a": "9"})) == {"a": "9", "b": "2"}
    assert props.apply("x=1", {"y": "2"}) == "x=1\ny=2\n", "a file without a final newline still ends up well formed"


def test_values_come_back_typed_with_the_servers_defaults_for_missing_keys():
    values = props.read_values(SAMPLE)
    assert values["max-players"] == 20 and values["white-list"] is False and values["motd"] == "A §bServer"
    assert values["difficulty"] == "easy" and values["view-distance"] == 10, "vanilla defaults for keys the file lacks"
    assert props.read_values("difficulty=HARD\nmax-players=lots\n")["difficulty"] == "hard"
    assert props.read_values("max-players=lots\n")["max-players"] == 20, "a value that is not a number falls back to the default"


def test_the_dashboards_settings_are_validated_before_anything_is_written():
    assert props.validate_values({"max-players": 8, "hardcore": True, "gamemode": "creative", "motd": "Hi §a"}) == {
        "max-players": "8", "hardcore": "true", "gamemode": "creative", "motd": "Hi §a",
    }
    bad = {
        "nonsense": 1, "max-players": 0, "view-distance": 99, "hardcore": "yes", "gamemode": "Creative", "difficulty": "extreme",
        "motd": "two\nlines", "max-tick-time": 1.5,
    }
    with pytest.raises(ConfigError) as caught:
        props.validate_values(bad)
    assert set(caught.value.errors) == set(bad)


def test_the_first_file_and_the_keys_the_manager_owns():
    text = props.build_initial({"max-players": 8, "motd": "Hello"}, 25570)
    assert props.parse(text) == {"max-players": "8", "motd": "Hello", "server-port": "25570"}
    fixed, changed = props.apply_managed("server-port=1234\nmotd=x\n", 25570)
    assert changed == ["server-port"] and props.parse(fixed) == {"server-port": "25570", "motd": "x"}
    same, changed = props.apply_managed(fixed, 25570)
    assert changed == [] and same == fixed
    # A modpack's own server.properties must not be able to open a remote console on a port the container publishes.
    safe, changed = props.apply_managed("server-port=25570\nenable-rcon=true\nrcon.password=secret\n", 25570)
    assert changed == ["enable-rcon"] and props.parse(safe)["enable-rcon"] == "false"
    assert props.apply_managed("enable-rcon=false\nserver-port=25570\n", 25570)[1] == [], "a file that already is fine is left alone"


def test_every_field_of_the_schema_is_usable_by_the_settings_page():
    seen = set()
    for field in props.schema():
        assert field["key"] not in seen
        seen.add(field["key"])
        assert field["type"] in ("string", "int", "bool", "enum") and field["group"] and field["label"]
        if field["type"] == "enum":
            assert field["default"] in field["options"] and len(field["labels"]) == len(field["options"])
    assert "server-port" not in seen, "the port is managed from the Server & network card"
