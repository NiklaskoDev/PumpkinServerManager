"""BungeeCord's ``config.yml``: what the manager writes when it creates a proxy, and the few settings it reads and changes afterwards.

BungeeCord completes a short file with its own defaults when it starts and writes it back whole, in its own order. So the manager writes only
what it has a reason to choose, and later changes single lines in place and leaves everything else the way BungeeCord or the person left it.
There is no YAML library in the manager: the file is plain block YAML, and the lines that are touched are few and simple.

One thing a short file cannot be: BungeeCord refuses to start without a server to send players to ("No servers defined"), and then its
Java process stays alive doing nothing. So a new proxy always gets at least one.
"""

from __future__ import annotations

import re
from typing import Any

from ..pumpkin_config import ConfigError
from .properties import Field

FILE_NAME = "config.yml"
DEFAULT_MOTD = "A network of Minecraft servers"
DEFAULT_MAX_PLAYERS = 100  # BungeeCord's own default is 1, which makes the server list say "0/1"
PLACEHOLDER = {"name": "lobby", "address": "127.0.0.1:25566", "motd": "Replace this with your own server"}  # when there is none to list
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ADDRESS_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,255}$")
_TRUE, _FALSE = {"true", "yes", "on"}, {"false", "no", "off"}
# Words and numbers that YAML reads as something other than text when they stand alone: a server called "no" must be written 'no'
_NOT_TEXT = re.compile(r"^(true|false|yes|no|on|off|y|n|null|~|[0-9._+-]+(e[0-9+-]+)?|0x[0-9a-f]+)$", re.IGNORECASE)

FIELDS: tuple[Field, ...] = (
    Field("motd", "string", "Message of the day", "General", "&1Another Bungee server",
          "Shown in the multiplayer server list. &a, &l and the like are colour and style codes.", maxlen=200),
    Field("max_players", "int", "Players shown as the maximum", "General", 100,
          "The number the server list shows next to the players online. It does not stop anybody joining.", min=1, max=100000),
    Field("online_mode", "bool", "Online mode (verify accounts)", "Access", True,
          "The proxy checks that players own the account they use. Keep it on. The servers behind the proxy then have to be in offline mode "
          "(online-mode=false), and nobody may be able to reach them except through the proxy."),
    Field("ip_forward", "bool", "Pass players' addresses and accounts on", "Access", True,
          "The servers behind the proxy see the real address and the real Minecraft account of each player instead of the proxy's."),
)
FIELD_BY_KEY = {f.key: f for f in FIELDS}


def schema() -> list[dict[str, Any]]:
    return [f.public() for f in FIELDS]


# --------------------------------------------------------------------------------------------------------------------- YAML scalars
def quote(text: str) -> str:
    """A text as a single-quoted YAML string, which can hold anything on one line (a quote is written twice)."""
    return "'" + text.replace("'", "''") + "'"


def _scalar(raw: str) -> str:
    """The text of a YAML value as written on a line: quotes removed, a trailing comment cut off."""
    raw = raw.strip()
    if raw.startswith("'"):
        out, i = [], 1
        while i < len(raw):
            if raw[i] == "'":
                if raw[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(raw[i])
            i += 1
        return "".join(out)
    if raw.startswith('"'):
        out, i = [], 1
        escapes = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}
        while i < len(raw) and raw[i] != '"':
            if raw[i] == "\\" and i + 1 < len(raw):
                out.append(escapes.get(raw[i + 1], raw[i + 1]))
                i += 2
                continue
            out.append(raw[i])
            i += 1
        return "".join(out)
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _join(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------------------------------- finding lines
_KEY_LINE = re.compile(r"^(?P<prefix>\s*(?:-\s+)?)(?P<key>[A-Za-z0-9_.-]+):(?:\s+(?P<value>.*))?$")


def _top_level(lines: list[str], key: str) -> int | None:
    """The line of a key at the left edge of the file."""
    for i, line in enumerate(lines):
        match = _KEY_LINE.match(line)
        if match and not match["prefix"] and match["key"] == key:
            return i
    return None


def _first_listener(lines: list[str]) -> tuple[int, int, int] | None:
    """(first line, line after the last, indentation of the keys) of the first entry under ``listeners:``, whether the list is written
    flush with the key (as BungeeCord does) or indented."""
    start = _top_level(lines, "listeners")
    if start is None:
        return None
    item = None
    for i in range(start + 1, len(lines)):
        if re.match(r"^\s*-\s", lines[i]):
            item = i
            break
        if lines[i].strip() and not lines[i].startswith((" ", "\t", "#")):  # another key of the file: there is no listener
            break
    if item is None:
        return None
    dash_indent = len(lines[item]) - len(lines[item].lstrip())
    end = item + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.lstrip().startswith("#"):
            indent = len(line) - len(line.lstrip())
            if indent <= dash_indent:  # the next entry, or the next key of the file
                break
        end += 1
    return item, end, dash_indent + 2


def _listener_key(lines: list[str], span: tuple[int, int, int], key: str) -> int | None:
    first, end, indent = span
    for i in range(first, end):
        match = _KEY_LINE.match(lines[i])
        if match and match["key"] == key and len(match["prefix"].replace("-", " ")) == indent:
            return i
    return None


def _replace_value(line: str, value: str) -> str:
    match = _KEY_LINE.match(line)
    assert match is not None
    return f"{match['prefix']}{match['key']}: {value}"


# ------------------------------------------------------------------------------------------------------------------------- reading
def _listener_text(lines: list[str], span: tuple[int, int, int] | None, key: str) -> str | None:
    if span is None:
        return None
    at = _listener_key(lines, span, key)
    match = _KEY_LINE.match(lines[at]) if at is not None else None
    return _scalar(match["value"] or "") if match else None


def read_values(text: str) -> dict[str, Any]:
    """The settings of the schema as typed values; what the file does not say shows BungeeCord's default."""
    lines = _lines(text)
    span = _first_listener(lines)
    values: dict[str, Any] = {f.key: f.default for f in FIELDS}
    motd = _listener_text(lines, span, "motd")
    if motd is not None:
        values["motd"] = motd
    shown = _listener_text(lines, span, "max_players")
    if shown is not None and re.fullmatch(r"-?\d+", shown):
        values["max_players"] = int(shown)
    for key in ("online_mode", "ip_forward"):
        at = _top_level(lines, key)
        match = _KEY_LINE.match(lines[at]) if at is not None else None
        word = _scalar(match["value"] or "").lower() if match else ""
        if word in _TRUE:
            values[key] = True
        elif word in _FALSE:
            values[key] = False
    return values


def read_port(text: str) -> int | None:
    """The port the first listener is on, if the file says."""
    lines = _lines(text)
    host = _listener_text(lines, _first_listener(lines), "host")
    port = host.rsplit(":", 1)[-1] if host and ":" in host else ""
    return int(port) if port.isdigit() else None


# ------------------------------------------------------------------------------------------------------------------------- writing
def validate_values(values: dict[str, Any]) -> dict[str, Any]:
    """Check settings coming from the dashboard; raises ``ConfigError`` naming each wrong one."""
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    for key, value in values.items():
        field = FIELD_BY_KEY.get(key)
        if field is None:
            errors[key] = "unknown setting"
        elif field.type == "bool":
            if isinstance(value, bool):
                clean[key] = value
            else:
                errors[key] = "must be true or false"
        elif field.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                errors[key] = "must be a whole number"
            elif (field.min is not None and value < field.min) or (field.max is not None and value > field.max):
                errors[key] = f"must be between {field.min} and {field.max}"
            else:
                clean[key] = value
        elif not isinstance(value, str) or len(value) > field.maxlen or "\n" in value or "\r" in value or "\x00" in value:
            errors[key] = f"must be one line of text, at most {field.maxlen} characters"
        else:
            clean[key] = value
    if errors:
        raise ConfigError(errors)
    return clean


def _yaml_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return quote(value) if isinstance(value, str) else str(value)


def apply_values(text: str, values: dict[str, Any]) -> str:
    """Set the given settings (see ``FIELDS``), changing nothing else: a line that is there is rewritten in place, one that is not is added."""
    clean = validate_values(values)
    lines = _lines(text)
    for key, value in clean.items():
        if key in ("online_mode", "ip_forward"):
            at = _top_level(lines, key)
            if at is None:
                lines.append(f"{key}: {_yaml_value(key, value)}")
            else:
                lines[at] = _replace_value(lines[at], _yaml_value(key, value))
        else:
            lines = _set_listener_key(lines, key, _yaml_value(key, value))
    return _join(lines)


def _set_listener_key(lines: list[str], key: str, value: str) -> list[str]:
    span = _first_listener(lines)
    if span is None:  # no listener yet: BungeeCord would use its defaults, so say it with a listener of its own
        start = _top_level(lines, "listeners")
        if start is None:
            return [*lines, "listeners:", f"- {key}: {value}"]
        lines[start] = "listeners:"  # it was an empty list ("listeners: []")
        lines.insert(start + 1, f"- {key}: {value}")
        return lines
    at = _listener_key(lines, span, key)
    if at is not None:
        lines[at] = _replace_value(lines[at], value)
    else:
        lines.insert(span[0] + 1, f"{' ' * span[2]}{key}: {value}")
    return lines


def apply_managed(text: str, port: int) -> tuple[str, list[str]]:
    """Put the port right again before every start: the dashboard's port is the one that counts. Returns the text and what was changed."""
    lines = _lines(text)
    span = _first_listener(lines)
    host = _listener_text(lines, span, "host")
    if host is not None and read_port(text) == port:
        return text, []
    address = host.rsplit(":", 1)[0] if host and ":" in host else "0.0.0.0"
    return _join(_set_listener_key(lines, "host", f"{address}:{port}")), ["port"]


# ------------------------------------------------------------------------------------------------------------------------- creating
def server_name(text: str) -> str:
    """A name BungeeCord accepts for a server (letters, digits, - and _), made from what the manager calls it."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:64]
    return name or "server"


def build_initial(port: int, motd: str, max_players: int, servers: list[dict[str, str]]) -> str:
    """A new proxy's config.yml: where it listens, what it says in the server list, the servers it sends players to (the first one is where
    they land), and the settings that make it work with servers behind it. ``servers`` are ``{"name", "address", "motd"}``; none: a
    placeholder, because BungeeCord does not start without one."""
    clean = validate_values({"motd": motd, "max_players": max_players})
    entries = servers or [PLACEHOLDER]
    names = []
    for entry in entries:
        name = entry["name"]
        if not _NAME_RE.match(name) or not _ADDRESS_RE.match(entry["address"]) or name.lower() in names:
            raise ConfigError({"servers": f"'{name}' ({entry['address']}) cannot be written to config.yml"})
        names.append(name.lower())
    def word(name: str) -> str:
        return quote(name) if _NOT_TEXT.match(name) else name

    lines = [
        "# Written by Pumpkin Server Manager. BungeeCord adds its own defaults for everything that is not here when it starts.",
        "listeners:",
        f"- host: 0.0.0.0:{port}",
        f"  query_port: {port}",
        f"  motd: {quote(clean['motd'])}",
        f"  max_players: {clean['max_players']}",
        "  forced_hosts: {}",  # left out, BungeeCord adds a sample ("pvp.md-5.net") that names a server that does not exist, and warns about it
        "  priorities:",
        f"  - {word(entries[0]['name'])}",
        "servers:",
    ]
    for entry in entries:
        lines += [f"  {word(entry['name'])}:", f"    motd: {quote(entry['motd'])}", f"    address: {entry['address']}", "    restricted: false"]
    lines += ["ip_forward: true", "online_mode: true"]
    return _join(lines)
