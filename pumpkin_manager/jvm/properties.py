"""``server.properties``: reading, careful in-place edits and the schema the settings page is drawn from.

A Java server rewrites this file itself, so the manager only touches the keys it is asked to change (and the ones it
owns, such as the port) and leaves every other line, comment and ordering alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..pumpkin_config import ConfigError

FILE_NAME = "server.properties"

# Keys the manager owns, put right again before every start: the dashboard's port always wins, and RCON (a remote console that
# a modpack's own server.properties could switch on, on a port the container publishes) stays off, since nothing here uses it.
MANAGED_KEYS = ("server-port", "enable-rcon")


@dataclass(frozen=True)
class Field:
    key: str  # the property name, which is also the key of the settings the dashboard sends and receives
    type: str  # string | int | bool | enum
    label: str
    group: str
    default: Any
    help: str = ""
    min: int | None = None
    max: int | None = None
    options: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    maxlen: int = 200
    advanced: bool = False
    placeholder: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key, "type": self.type, "label": self.label, "group": self.group, "default": self.default,
            "help": self.help, "min": self.min, "max": self.max, "options": list(self.options), "labels": list(self.labels),
            "advanced": self.advanced, "maxlen": self.maxlen, "placeholder": self.placeholder,
        }


GAMEMODES = ("survival", "creative", "adventure", "spectator")
DIFFICULTIES = ("peaceful", "easy", "normal", "hard")
DEFAULT_OP_LEVEL = 1  # the manager starts operators at level 1, as it does on Pumpkin servers
OP_LEVELS = (1, 2, 3, 4)

FIELDS: tuple[Field, ...] = (
    Field("motd", "string", "Message of the day", "General", "A Minecraft Server", "Shown in the multiplayer server list.", maxlen=120),
    Field("max-players", "int", "Max players", "General", 20, min=1, max=100000),
    Field("gamemode", "enum", "Default game mode", "General", "survival", options=GAMEMODES, labels=("Survival", "Creative", "Adventure", "Spectator")),
    Field("difficulty", "enum", "Difficulty", "General", "easy", options=DIFFICULTIES, labels=("Peaceful", "Easy", "Normal", "Hard")),
    Field("hardcore", "bool", "Hardcore", "General", False, "Players cannot respawn in survival mode."),
    Field("pvp", "bool", "Player versus player", "General", True),
    Field("online-mode", "bool", "Online mode (verify accounts)", "Access", True, "Requires valid Minecraft accounts. Turn off only for offline or LAN play."),
    Field("white-list", "bool", "Whitelist", "Access", False, "Only whitelisted players may join."),
    Field("enforce-whitelist", "bool", "Enforce whitelist", "Access", False, "Kick players who are not whitelisted when the whitelist is turned on."),
    Field("op-permission-level", "int", "Operator permission level", "Access", 4,  # what a server without the key uses
          "Level (1-4) a player gets when you make them an operator: 1 moderator, 2 game master, 3 admin, 4 owner. Operators who already "
          "exist keep theirs; change a single operator in the Players tab.", min=1, max=4),
    Field("level-seed", "string", "World seed", "World", "", "Only used when the world is first created. Leave empty for a random seed.",
          maxlen=64, placeholder="random"),
    Field("level-name", "string", "World folder name", "World", "world", "Changing this makes the server create or load a different world folder.",
          maxlen=64, advanced=True),
    Field("allow-nether", "bool", "Enable the Nether", "World", True),
    Field("spawn-protection", "int", "Spawn protection radius", "World", 16, "0 disables spawn protection.", min=0, max=1000),
    Field("allow-flight", "bool", "Allow flying", "Gameplay", False,
          "Many modpacks add flight (jetpacks, elytra mods, creative-style flying). Without this, such players are kicked for flying.", advanced=True),
    Field("enable-command-block", "bool", "Command blocks", "Gameplay", False, advanced=True),
    Field("force-gamemode", "bool", "Force default game mode", "Gameplay", False, "Players join in the default game mode every time.", advanced=True),
    Field("view-distance", "int", "View distance", "Performance", 10, "Chunks sent to each player. Lower is faster.", min=3, max=32),
    Field("simulation-distance", "int", "Simulation distance", "Performance", 10, "Chunks around players that are ticked.", min=3, max=32),
    Field("max-tick-time", "int", "Watchdog time limit (ms)", "Performance", 60000,
          "The server stops itself when a single tick takes longer than this. Heavy modpacks sometimes need a higher value while the world is "
          "generated; -1 turns the watchdog off.", min=-1, max=2_147_483_647, advanced=True),
    Field("player-idle-timeout", "int", "Idle kick (minutes)", "Gameplay", 0, "0 never kicks idle players.", min=0, max=100000, advanced=True),
)

FIELD_BY_KEY = {f.key: f for f in FIELDS}


def schema() -> list[dict[str, Any]]:
    return [f.public() for f in FIELDS]


# ------------------------------------------------------------------------- the file format
_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
_LINE_RE = re.compile(r"^[ \t\f]*(?P<key>(?:\\.|[^\s=:\\])+)[ \t\f]*(?:[=:][ \t\f]*|[ \t\f]+)?(?P<value>.*)$")


def unescape(text: str) -> str:
    """Undo Java's ``.properties`` escapes (``\\uXXXX``, ``\\n``, ``\\\\`` ...)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\" or i + 1 >= len(text):
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[i + 2:i + 6] or ""):
            out.append(chr(int(text[i + 2:i + 6], 16)))
            i += 6
        else:
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
    # Two escaped halves of a surrogate pair become one character.
    return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def escape_value(value: str) -> str:
    """The form Java's ``Properties.store`` writes: ASCII only, ``\\`` and control characters escaped."""
    out: list[str] = []
    for index, ch in enumerate(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\f":
            out.append("\\f")
        elif ch == " " and index == 0:
            out.append("\\ ")
        elif " " <= ch <= "~":
            out.append(ch)
        else:  # outside printable ASCII: UTF-16 code units, like Java
            data = ch.encode("utf-16-be")
            out.extend(f"\\u{int.from_bytes(data[i:i + 2], 'big'):04X}" for i in range(0, len(data), 2))
    return "".join(out)


def _lines(text: str) -> list[str]:
    """Physical lines. Only CR, LF and CRLF end a line (``str.splitlines`` would also split on exotic separators)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _logical_lines(text: str) -> list[tuple[int, int, str]]:
    """(first physical line, last physical line, text) per logical line; a trailing backslash continues a line."""
    lines = _lines(text)
    result: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        start, current = i, lines[i]
        is_comment = current.lstrip(" \t\f")[:1] in ("#", "!")  # comments are never continued
        while (
            not is_comment and current.endswith("\\") and (len(current) - len(current.rstrip("\\"))) % 2 == 1 and i + 1 < len(lines)
        ):
            i += 1
            current = current[:-1] + lines[i].lstrip(" \t\f")
        result.append((start, i, current))
        i += 1
    return result


def _split(logical: str) -> tuple[str, str] | None:
    stripped = logical.lstrip(" \t\f")
    if not stripped or stripped[0] in "#!":
        return None
    match = _LINE_RE.match(logical)
    return (unescape(match["key"]), match["value"]) if match else None


def parse(text: str) -> dict[str, str]:
    """All properties of the file (the last occurrence of a key wins, as in Java), values unescaped."""
    values: dict[str, str] = {}
    for _first, _last, logical in _logical_lines(text):
        pair = _split(logical)
        if pair:
            values[pair[0]] = unescape(pair[1])
    return values


def apply(text: str, updates: dict[str, str]) -> str:
    """Set the given keys, changing nothing else: existing lines are rewritten in place, new keys are appended."""
    if not updates:
        return text
    lines = _lines(text)
    last_line: dict[str, tuple[int, int]] = {}
    for first, last, logical in _logical_lines(text):
        pair = _split(logical)
        if pair and pair[0] in updates:
            last_line[pair[0]] = (first, last)  # a later duplicate is the one that counts
    replacements = {span: key for key, span in last_line.items()}
    out: list[str] = []
    i = 0
    while i < len(lines):
        span = next(((a, b) for (a, b) in replacements if a == i), None)
        if span:
            key = replacements[span]
            out.append(f"{key}={escape_value(updates[key])}")
            i = span[1] + 1
        else:
            out.append(lines[i])
            i += 1
    missing = [key for key in updates if key not in last_line]
    out.extend(f"{key}={escape_value(updates[key])}" for key in missing)
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------- typed values
def _to_text(field: Field, value: Any) -> str:
    return ("true" if value else "false") if field.type == "bool" else str(value)


def _from_text(field: Field, raw: str | None) -> Any:
    if raw is None:
        return field.default
    raw = raw.strip() if field.type != "string" else raw
    if field.type == "bool":
        return raw.lower() == "true"
    if field.type == "int":
        try:
            return int(raw)
        except ValueError:
            return field.default
    if field.type == "enum":
        return raw.lower() if raw.lower() in field.options else field.default
    return raw


def read_values(text: str) -> dict[str, Any]:
    """Every setting of the schema as a typed value; keys the file does not have yet show the server's default."""
    props = parse(text)
    return {f.key: _from_text(f, props.get(f.key)) for f in FIELDS}


def validate_values(values: dict[str, Any]) -> dict[str, str]:
    """Check settings coming from the dashboard and return them as property strings; raises ``ConfigError``."""
    errors: dict[str, str] = {}
    clean: dict[str, str] = {}
    for key, value in values.items():
        field = FIELD_BY_KEY.get(key)
        if field is None:
            errors[key] = "unknown setting"
            continue
        if field.type == "bool":
            if not isinstance(value, bool):
                errors[key] = "must be true or false"
                continue
        elif field.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                errors[key] = "must be a whole number"
                continue
            if (field.min is not None and value < field.min) or (field.max is not None and value > field.max):
                errors[key] = f"must be between {field.min} and {field.max}"
                continue
        elif field.type == "enum":
            if value not in field.options:
                errors[key] = f"must be one of: {', '.join(field.options)}"
                continue
        else:
            if not isinstance(value, str) or len(value) > field.maxlen or "\n" in value or "\r" in value:
                errors[key] = f"must be one line of text, at most {field.maxlen} characters"
                continue
        clean[key] = _to_text(field, value)
    if errors:
        raise ConfigError(errors)
    return clean


def build_initial(values: dict[str, Any], port: int) -> str:
    """The first ``server.properties``: only what the dashboard decided; the server fills in the rest when it starts."""
    clean = validate_values(values)
    clean["server-port"] = str(port)
    return apply("# Minecraft server properties (created by Pumpkin Server Manager)\n", clean)


def apply_managed(text: str, port: int) -> tuple[str, list[str]]:
    """Re-assert the keys the manager owns. Returns the new text and the keys that had to be changed."""
    current = parse(text)
    wanted: dict[str, str] = {}
    if current.get("server-port") != str(port):
        wanted["server-port"] = str(port)
    if current.get("enable-rcon", "false").strip().lower() == "true":
        wanted["enable-rcon"] = "false"
    return (apply(text, wanted) if wanted else text), list(wanted)


def apply_values(text: str, values: dict[str, Any]) -> str:
    return apply(text, validate_values(values))
