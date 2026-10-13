"""BungeeCord's config.yml: what the manager writes for a new proxy (read back with a real YAML parser), and how it reads and changes the
few settings it owns in a file that BungeeCord itself has rewritten."""

from __future__ import annotations

from pathlib import Path

import pytest

from pumpkin_manager.jvm import proxy
from pumpkin_manager.pumpkin_config import ConfigError

yaml = pytest.importorskip("yaml")

FIXTURES = Path(__file__).parent / "fixtures"
REWRITTEN = (FIXTURES / "bungeecord-config.yml").read_text(encoding="utf-8")  # a short file after BungeeCord started with it
DEFAULT = (FIXTURES / "bungeecord-default-config.yml").read_text(encoding="utf-8")  # what BungeeCord writes when there is none
SERVERS = [{"name": "survival", "address": "127.0.0.1:25566", "motd": "Survival"}, {"name": "creative", "address": "127.0.0.1:25567", "motd": "Creative"}]


def loaded(text: str) -> dict:
    return yaml.safe_load(text)


# ------------------------------------------------------------------------------------------------------------------------------ creating
def test_a_new_proxy_gets_a_config_with_what_the_manager_chose():
    config = loaded(proxy.build_initial(25565, "&aMy network", 40, SERVERS))
    listener = config["listeners"][0]
    assert listener["host"] == "0.0.0.0:25565" and listener["query_port"] == 25565
    assert listener["motd"] == "&aMy network" and listener["max_players"] == 40 and listener["priorities"] == ["survival"]
    assert listener["forced_hosts"] == {}, "BungeeCord would add a sample forced host that names a server that is not there"
    assert list(config["servers"]) == ["survival", "creative"]
    assert config["servers"]["creative"] == {"motd": "Creative", "address": "127.0.0.1:25567", "restricted": False}
    assert config["ip_forward"] is True and config["online_mode"] is True


@pytest.mark.parametrize("motd", ["It's a 'quoted' name", "A: colon, and # a hash", "&1&lBold & more", "", "x" * 200, "ünï©ode ✓"])
def test_whatever_the_message_of_the_day_says_it_comes_back_exactly(motd):
    text = proxy.build_initial(25565, motd, 10, SERVERS)
    assert loaded(text)["listeners"][0]["motd"] == motd
    assert proxy.read_values(text)["motd"] == motd, "and the manager reads it back the same way"


def test_a_proxy_without_any_server_to_list_gets_a_placeholder_because_bungeecord_does_not_start_without_one():
    config = loaded(proxy.build_initial(25565, "x", 10, []))
    assert list(config["servers"]) == ["lobby"] and config["listeners"][0]["priorities"] == ["lobby"]
    assert config["servers"]["lobby"]["address"] == proxy.PLACEHOLDER["address"]


@pytest.mark.parametrize("name", ["no", "On", "true", "null", "123", "0x1F", "y", "e5", "1e5", "007"])
def test_a_server_called_like_a_yaml_word_is_still_a_text(name):
    config = loaded(proxy.build_initial(25565, "x", 10, [{"name": name, "address": "127.0.0.1:25566", "motd": name}]))
    assert list(config["servers"]) == [name] and config["listeners"][0]["priorities"] == [name], "not False, not a number"


@pytest.mark.parametrize("entry", [
    {"name": "has space", "address": "127.0.0.1:25566", "motd": "x"}, {"name": "-x", "address": "127.0.0.1:25566", "motd": "x"},
    {"name": "ok", "address": "127.0.0.1:25566\nevil: 1", "motd": "x"}, {"name": "", "address": "127.0.0.1:25566", "motd": "x"},
    {"name": "a" * 65, "address": "127.0.0.1:25566", "motd": "x"}, {"name": "1.5", "address": "127.0.0.1:25566", "motd": "x"},
    {"name": "~", "address": "127.0.0.1:25566", "motd": "x"},
])
def test_a_server_that_cannot_be_written_is_refused_and_not_guessed_at(entry):
    with pytest.raises(ConfigError, match="cannot be written"):
        proxy.build_initial(25565, "x", 10, [entry])


def test_two_servers_that_differ_only_in_case_are_refused():
    with pytest.raises(ConfigError):
        proxy.build_initial(25565, "x", 10, [SERVERS[0], {**SERVERS[1], "name": "Survival"}])


def test_names_for_servers_are_made_from_what_the_manager_calls_them():
    assert proxy.server_name("cool-pack-2") == "cool-pack-2" and proxy.server_name("My server!") == "My-server"
    assert proxy.server_name("!!!") == "server" and len(proxy.server_name("x" * 100)) == 64


# ------------------------------------------------------------------------------------------------------------------------------- reading
def test_the_settings_are_read_from_a_file_that_bungeecord_rewrote():
    assert proxy.read_values(REWRITTEN) == {"motd": "&aA network run by the manager", "max_players": 20, "online_mode": True, "ip_forward": True}
    assert proxy.read_port(REWRITTEN) == 25590


def test_the_settings_are_read_from_the_file_bungeecord_writes_when_there_is_none():
    """There the host is the last key of the listener, not the first: the first line of a listener is a different key."""
    assert proxy.read_values(DEFAULT) == {"motd": "&1Another Bungee server", "max_players": 1, "online_mode": True, "ip_forward": False}
    assert proxy.read_port(DEFAULT) == 25577


def test_what_a_file_does_not_say_shows_the_default():
    assert proxy.read_values("") == {f.key: f.default for f in proxy.FIELDS} and proxy.read_port("") is None
    assert proxy.read_values("timeout: 30000\n")["online_mode"] is True


@pytest.mark.parametrize("line,expected", [("online_mode: yes", True), ("online_mode: off", False), ("online_mode: 'false'", False), ("online_mode: true # comment", True)])
def test_yaml_spellings_of_yes_and_no_are_understood(line, expected):
    assert proxy.read_values(line + "\n")["online_mode"] is expected


def test_a_list_that_a_person_indented_is_found_too():
    text = "listeners:\n  - host: 0.0.0.0:25570\n    motd: 'Mine'\n    max_players: 7\n    forced_hosts:\n      motd: nested\nservers:\n  a:\n    address: x:1\n"
    assert proxy.read_values(text)["motd"] == "Mine" and proxy.read_values(text)["max_players"] == 7 and proxy.read_port(text) == 25570


def test_crlf_files_are_read_like_the_rest():
    assert proxy.read_values(REWRITTEN.replace("\n", "\r\n"))["max_players"] == 20


# ------------------------------------------------------------------------------------------------------------------------------- changing
def lines_changed(before: str, after: str) -> list[str]:
    return [line for line in after.splitlines() if line not in before.splitlines()]


def test_changing_settings_touches_only_their_lines():
    after = proxy.apply_values(REWRITTEN, {"motd": "New: 'one'", "max_players": 55, "online_mode": False, "ip_forward": False})
    assert proxy.read_values(after) == {"motd": "New: 'one'", "max_players": 55, "online_mode": False, "ip_forward": False}
    assert lines_changed(REWRITTEN, after) == ["online_mode: false", "ip_forward: false", "  motd: 'New: ''one'''", "  max_players: 55"]
    before_data, after_data = loaded(REWRITTEN), loaded(after)
    after_data["listeners"][0].update(motd=before_data["listeners"][0]["motd"], max_players=20)
    after_data.update(online_mode=True, ip_forward=True)
    assert after_data == before_data, "everything else is the same data"


def test_a_setting_that_the_file_lacks_is_added():
    after = proxy.apply_values("servers:\n  a:\n    address: x:1\n", {"max_players": 9, "ip_forward": True})
    assert loaded(after)["ip_forward"] is True and loaded(after)["listeners"][0]["max_players"] == 9


def test_changing_a_setting_in_a_listener_that_lacks_the_key_adds_it_to_that_listener():
    after = proxy.apply_values(REWRITTEN.replace("  max_players: 20\n", ""), {"max_players": 3})
    assert loaded(after)["listeners"][0]["max_players"] == 3 and loaded(after)["listeners"][0]["motd"] == "&aA network run by the manager"


def test_a_second_listener_is_left_alone():
    text = "listeners:\n- host: 0.0.0.0:25565\n  motd: 'one'\n- host: 0.0.0.0:25566\n  motd: 'two'\nservers:\n  a:\n    address: x:1\n"
    after = proxy.apply_values(text, {"motd": "changed"})
    assert [item["motd"] for item in loaded(after)["listeners"]] == ["changed", "two"]


@pytest.mark.parametrize("bad", [{"motd": "two\nlines"}, {"motd": "x" * 201}, {"max_players": 0}, {"max_players": "5"}, {"max_players": True}, {"online_mode": "yes"}, {"nope": 1}])
def test_settings_that_make_no_sense_are_refused(bad):
    with pytest.raises(ConfigError):
        proxy.apply_values(REWRITTEN, bad)


# ----------------------------------------------------------------------------------------------------------------------- the port
def test_the_port_is_put_right_again_and_nothing_else_changes():
    text, changed = proxy.apply_managed(REWRITTEN, 25600)
    assert changed == ["port"] and proxy.read_port(text) == 25600
    assert lines_changed(REWRITTEN, text) == ["- host: 0.0.0.0:25600"]
    assert proxy.apply_managed(text, 25600) == (text, []), "nothing to do the second time"


def test_the_address_the_proxy_listens_on_is_kept_when_only_the_port_is_wrong():
    text, _ = proxy.apply_managed("listeners:\n- host: 127.0.0.1:1\n  motd: x\nservers:\n  a:\n    address: x:1\n", 25570)
    assert loaded(text)["listeners"][0]["host"] == "127.0.0.1:25570"


def test_a_listener_without_a_host_gets_one():
    text, changed = proxy.apply_managed("listeners:\n- motd: x\nservers:\n  a:\n    address: x:1\n", 25570)
    assert changed == ["port"] and loaded(text)["listeners"][0] == {"host": "0.0.0.0:25570", "motd": "x"}


@pytest.mark.parametrize("start", ["", "servers:\n  a:\n    address: x:1\n", "listeners: []\nservers:\n  a:\n    address: x:1\n"])
def test_a_file_without_a_listener_gets_one(start):
    text, changed = proxy.apply_managed(start, 25570)
    assert changed == ["port"] and loaded(text)["listeners"] == [{"host": "0.0.0.0:25570"}]
    if "servers" in start:
        assert loaded(text)["servers"]["a"]["address"] == "x:1"


def test_a_list_that_belongs_to_another_key_is_not_taken_for_the_listeners():
    text, _ = proxy.apply_managed("listeners:\ndisabled_commands:\n- end\n", 25570)
    assert loaded(text)["disabled_commands"] == ["end"] and loaded(text)["listeners"] == [{"host": "0.0.0.0:25570"}]


def test_the_schema_describes_what_the_settings_page_draws():
    keys = [f["key"] for f in proxy.schema()]
    assert keys == ["motd", "max_players", "online_mode", "ip_forward"]
    assert {f["type"] for f in proxy.schema()} == {"string", "int", "bool"}
