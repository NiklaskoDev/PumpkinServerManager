"""Reading, validating and writing ``pumpkin.toml``.

Why this module is strict: when Pumpkin cannot parse its config it logs the error *before* its logger
is initialised and then silently starts with default settings (default port, no management API, ...).
Out-of-range ``view_distance`` values even trip an assertion at startup. So everything the dashboard
writes is validated first, and the keys the manager owns (ports, management secret) are re-applied
before every start.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from .errors import UserError

MISSING = object()


class ConfigError(UserError):
    """Validation failure. ``errors`` maps a field key (or ``"_"``) to a message."""

    status = 422

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(v if k == "_" else f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass(frozen=True)
class Field:
    key: str
    path: tuple[str, ...]
    type: str  # string | int | float | bool | enum
    label: str
    group: str
    default: Any
    help: str = ""
    min: float | None = None
    max: float | None = None
    options: tuple[str, ...] = ()
    also: tuple[tuple[str, ...], ...] = ()
    maxlen: int = 200
    advanced: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key, "type": self.type, "label": self.label, "group": self.group,
            "default": self.default, "help": self.help, "min": self.min, "max": self.max,
            "options": list(self.options), "advanced": self.advanced, "maxlen": self.maxlen,
        }


GAMEMODES = ("Survival", "Creative", "Adventure", "Spectator")
DIFFICULTIES = ("Peaceful", "Easy", "Normal", "Hard")

FIELDS: tuple[Field, ...] = (
    Field("motd", ("networking", "java", "motd"), "string", "Message of the day", "General",
          "A blazingly fast Pumpkin server!", "Shown in the multiplayer server list.",
          also=(("networking", "bedrock", "motd"),), maxlen=120),
    Field("max_players", ("networking", "java", "max_players"), "int", "Max players", "General", 1000,
          "0 means no limit.", min=0, max=100000, also=(("networking", "bedrock", "max_players"),)),
    Field("default_gamemode", ("default_gamemode",), "enum", "Default game mode", "General", "Survival",
          options=GAMEMODES),
    Field("default_difficulty", ("default_difficulty",), "enum", "Difficulty", "General", "Normal",
          "Pumpkin ignores this in its config file, so the manager sets it with the /difficulty command when the "
          "server first starts and as soon as you change it here.", options=DIFFICULTIES),
    Field("hardcore", ("hardcore",), "bool", "Hardcore", "General", False,
          "Players cannot respawn in survival mode."),
    Field("online_mode", ("networking", "java", "online_mode"), "bool", "Online mode (verify accounts)",
          "Access", True, "Requires valid Minecraft accounts. Turn off only for offline/LAN play."),
    Field("white_list", ("white_list",), "bool", "Whitelist", "Access", False,
          "Only whitelisted players may join."),
    Field("enforce_whitelist", ("enforce_whitelist",), "bool", "Enforce whitelist", "Access", False,
          "Kick players who are not whitelisted when the whitelist is turned on."),
    Field("op_permission_level", ("op_permission_level",), "int", "Operator permission level", "Access", 4,
          "Level (1-4) given to operators.", min=1, max=4),
    Field("accepts_transfers", ("accepts_transfers",), "bool", "Accept transfers", "Access", True,
          "Allow players to be transferred here from other servers.", advanced=True),
    Field("seed", ("seed",), "string", "World seed", "World", "",
          "Only used when the world is first created. Leave empty for a random seed; text is converted "
          "like vanilla Minecraft does.", maxlen=64),
    Field("default_level_name", ("default_level_name",), "string", "World folder name", "World", "world",
          "Changing this makes the server create or load a different world folder.", maxlen=64, advanced=True),
    Field("allow_nether", ("allow_nether",), "bool", "Enable the Nether", "World", True),
    Field("allow_end", ("allow_end",), "bool", "Enable the End", "World", True),
    Field("spawn_protection", ("spawn_protection",), "int", "Spawn protection radius", "World", 16,
          "0 disables spawn protection.", min=0, max=1000),
    Field("view_distance", ("networking", "java", "view_distance"), "int", "View distance", "Performance", 16,
          "Chunks sent to each player (2-64). Lower is faster.", min=2, max=64,
          also=(("networking", "bedrock", "view_distance"),)),
    Field("simulation_distance", ("networking", "java", "simulation_distance"), "int", "Simulation distance",
          "Performance", 10, "Chunks around players that are ticked (1-32).", min=1, max=32,
          also=(("networking", "bedrock", "simulation_distance"),)),
    Field("tps", ("tps",), "float", "Target ticks per second", "Performance", 20.0, min=1, max=200, advanced=True),
    Field("autosave_ticks", ("world", "autosave_ticks"), "int", "Autosave interval (ticks)", "Performance", 6000,
          "20 ticks = 1 second. 0 disables autosave (not recommended).", min=0, max=10_000_000, advanced=True),
    Field("force_gamemode", ("force_gamemode",), "bool", "Force default game mode", "Gameplay", False,
          "Players join in the default game mode every time.", advanced=True),
    Field("scrub_ips", ("scrub_ips",), "bool", "Hide player IPs in logs", "Privacy", True),
    Field("allow_chat_reports", ("allow_chat_reports",), "bool", "Signed chat reporting", "Privacy", False,
          "Requires online mode.", advanced=True),
    Field("telemetry", ("telemetry", "enabled"), "bool", "Telemetry heartbeat", "Privacy", False,
          "Pumpkin's own telemetry to market.pumpkinmc.org (off by default in this manager)."),
    Field("query", ("networking", "query", "enabled"), "bool", "GameSpy4 query", "Network", False,
          "Enables the UDP query protocol on the server port (publish the UDP port to use it).", advanced=True),
)

FIELD_BY_KEY = {f.key: f for f in FIELDS}


def schema() -> list[dict[str, Any]]:
    return [f.public() for f in FIELDS]


# ----------------------------------------------------------------------- seeds


def normalize_seed(text: str) -> str:
    """Numeric seeds pass through; anything else is hashed like Java's ``String.hashCode``."""
    text = text.strip()
    if not text:
        return ""
    if re.fullmatch(r"-?\d{1,19}", text) and -(2**63) <= int(text) < 2**63:
        return str(int(text))
    units = text.encode("utf-16-le")
    h = 0
    for i in range(0, len(units), 2):
        h = (31 * h + (units[i] | (units[i + 1] << 8))) & 0xFFFFFFFF
    return str(h - (1 << 32) if h >= 1 << 31 else h)


# ----------------------------------------------------------------- validation


def _coerce(field: Field, value: Any) -> Any:
    if field.type == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("must be true or false")
    if field.type == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ValueError("must be a whole number")
        value = int(value)
    elif field.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        value = float(value)
    elif field.type in ("string", "enum"):
        if not isinstance(value, str):
            raise ValueError("must be text")
        value = value.strip() if field.type == "enum" else value
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("must be a single line")
        if len(value) > field.maxlen:
            raise ValueError(f"must be at most {field.maxlen} characters")
        if field.type == "enum" and value not in field.options:
            raise ValueError(f"must be one of: {', '.join(field.options)}")
        if field.key == "seed":
            value = normalize_seed(value)
        if field.key == "default_level_name" and not re.fullmatch(r"[A-Za-z0-9_. -]{1,64}", value):
            raise ValueError("may only contain letters, digits, spaces, '.', '_' and '-'")
        if field.key == "default_level_name" and (value.strip(". ") == "" or ".." in value):
            raise ValueError("is not a valid folder name")
    if field.type in ("int", "float"):
        if field.min is not None and value < field.min:
            raise ValueError(f"must be at least {field.min:g}")
        if field.max is not None and value > field.max:
            raise ValueError(f"must be at most {field.max:g}")
    return value


def validate_values(values: dict[str, Any]) -> dict[str, Any]:
    """Validate a ``{field key: value}`` mapping. Unknown keys are rejected."""
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    for key, value in values.items():
        field = FIELD_BY_KEY.get(key)
        if field is None:
            errors[key] = "unknown setting"
            continue
        try:
            clean[key] = _coerce(field, value)
        except ValueError as exc:
            errors[key] = str(exc)
    if clean.get("allow_chat_reports") and clean.get("online_mode") is False:
        errors["allow_chat_reports"] = "requires online mode"
    if errors:
        raise ConfigError(errors)
    return clean


# ------------------------------------------------------------------ TOML access


def get_path(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = data
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node


def parse(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError({"_": f"pumpkin.toml is not valid TOML: {exc}"}) from exc


def read_values(text: str) -> dict[str, Any]:
    """Current value of every known field (defaults where the file has none)."""
    data = parse(text) if text.strip() else {}
    result: dict[str, Any] = {}
    for field in FIELDS:
        value = get_path(data, field.path)
        result[field.key] = field.default if value is MISSING else value
    return result


def _set(node: Any, path: tuple[str, ...], value: Any) -> None:
    for part in path[:-1]:
        if part not in node:
            node[part] = tomlkit.table()
        node = node[part]
    node[path[-1]] = value


@dataclass(frozen=True)
class ManagedConfig:
    """Values owned by the manager; rewritten before every start."""

    java_port: int
    mgmt_port: int
    mgmt_secret: str
    bedrock_port: int | None = None

    def items(self) -> list[tuple[tuple[str, ...], Any]]:
        items: list[tuple[tuple[str, ...], Any]] = [
            (("networking", "java", "enabled"), True),
            (("networking", "java", "address"), f"0.0.0.0:{self.java_port}"),
            (("networking", "query", "address"), f"0.0.0.0:{self.java_port}"),
            (("networking", "management", "enabled"), True),
            (("networking", "management", "address"), f"127.0.0.1:{self.mgmt_port}"),
            (("networking", "management", "secret"), self.mgmt_secret),
            (("networking", "bedrock", "enabled"), self.bedrock_port is not None),
            # No interactive terminal exists inside the manager; use the plain stdin console.
            (("commands", "use_tty"), False),
            (("commands", "use_console"), True),
        ]
        if self.bedrock_port is not None:
            items.append((("networking", "bedrock", "nethernet", "address"), f"0.0.0.0:{self.bedrock_port}"))
        return items


def apply_managed(text: str, managed: ManagedConfig) -> tuple[str, list[str]]:
    """Return (new text, list of managed keys whose value had to change)."""
    doc = _load_doc(text)
    current = doc.unwrap()
    changed: list[str] = []
    for path, value in managed.items():
        existing = get_path(current, path)
        if existing == value:
            continue
        _set(doc, path, value)
        # Pumpkin builds without the management API drop the section whenever they rewrite the file. Re-adding it
        # is routine, so only report it when a user's value was overridden.
        if not (path[:2] == ("networking", "management") and existing is MISSING):
            changed.append(".".join(path))
    return tomlkit.dumps(doc), changed


def apply_values(text: str, values: dict[str, Any]) -> str:
    """Write validated field values into ``text`` while preserving the rest of the document."""
    doc = _load_doc(text)
    for key, value in values.items():
        field = FIELD_BY_KEY[key]
        if field.key == "seed" and value == "":
            continue  # empty means "random": never write a blank seed
        for path in (field.path, *field.also):
            _set(doc, path, value)
    if values.get("online_mode") is True:
        _set(doc, ("networking", "java", "encryption"), True)  # Pumpkin refuses online mode without encryption
    return tomlkit.dumps(doc)


def _load_doc(text: str) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(text) if text.strip() else tomlkit.document()
    except TOMLKitError as exc:
        raise ConfigError({"_": f"pumpkin.toml is not valid TOML: {exc}"}) from exc


def build_initial(values: dict[str, Any], managed: ManagedConfig) -> str:
    text = apply_values("", validate_values(values))
    return apply_managed(text, managed)[0]


def validate_raw(text: str) -> dict[str, Any]:
    """Syntax plus type/range checks for every known key present in a hand-edited file."""
    data = parse(text)
    errors: dict[str, str] = {}
    for field in FIELDS:
        value = get_path(data, field.path)
        if value is MISSING:
            continue
        try:
            _coerce(field, value)
        except ValueError as exc:
            errors[field.key] = f"{'.'.join(field.path)} {exc}"
    for path in (("networking", "bedrock", "view_distance"), ("networking", "java", "view_distance")):
        value = get_path(data, path)
        if value is not MISSING and not (isinstance(value, int) and 2 <= value <= 64):
            errors[".".join(path)] = f"{'.'.join(path)} must be a whole number between 2 and 64"
    if errors:
        raise ConfigError(errors)
    return data


def strip_secrets(text: str) -> str:
    """Hide the management secret in text shown to users."""
    return re.sub(r'(?m)^(\s*secret\s*=\s*)"[^"\n]*"', r'\1"********"', text)


def restore_secrets(new_text: str, managed: ManagedConfig) -> str:
    """After a raw edit, put the real management secret back (and re-assert the other managed keys)."""
    return apply_managed(new_text, managed)[0]
