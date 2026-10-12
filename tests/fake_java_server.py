"""A stand-in for a Java Minecraft server (vanilla or modded), used by the test-suite.

It copies what the manager depends on: it refuses to start without ``eula=true`` (and says so like the real server),
reads ``server.properties`` from the working directory, prints the ``Done (...)!`` line once it listens, answers the
Java server-list ping, keeps whitelist/ops/bans in the server folder itself in the vanilla format and stops on the
``stop`` command.

Usage: ``python fake_java_server.py BEHAVIOR_JSON <the real java command line...>``. The command line is stored in
``fake-java.json`` (with the environment's JAVA_HOME) so tests can check what the manager would have started.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

BEHAVIOR = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
ARGV = sys.argv[2:]
players: list[str] = []
KEEP_ALIVE: list[object] = []
stop_event: asyncio.Event
loop: asyncio.AbstractEventLoop


def log(text: str, level: str = "INFO", thread: str = "Server thread") -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{thread}/{level}]: {text}", flush=True)


def properties() -> dict[str, str]:
    path = Path("server.properties")
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value
    return values


PROPS = properties()


def offline_uuid(name: str) -> str:
    return str(uuid.UUID(bytes=hashlib.md5(f"OfflinePlayer:{name}".encode()).digest(), version=3))


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime())


def edit_list(filename: str, key: str, value: str, add: bool, entry: dict | None = None) -> bool:
    path = Path(filename)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    present = any(e.get(key) == value for e in entries)
    if add == present:
        return False
    entries = [e for e in entries if e.get(key) != value] + ([entry] if add else [])
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return True


def read_varint(buf: bytes, pos: int = 0) -> tuple[int, int]:
    result = 0
    for i in range(5):
        byte = buf[pos + i]
        result |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return result, pos + i + 1
    raise ValueError("varint")


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


loading = False  # Forge listens long before the world is loaded and answers pings with an empty status until then


def status_json() -> str:
    if loading:
        return json.dumps({"version": {"name": "", "protocol": 0}, "players": {"max": 0, "online": 0}, "description": {"text": ""}})
    return json.dumps({
        "version": {"name": str(BEHAVIOR.get("minecraft", "1.21.1")), "protocol": 767},
        "players": {"max": int(PROPS.get("max-players", "20")), "online": len(players), "sample": [{"name": p, "id": "0"} for p in players]},
        "description": {"text": PROPS.get("motd", "A Minecraft Server")},
    })


async def slp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            length, _ = read_varint(await reader.readexactly(1) + b"\0" * 0)
            packet = await reader.readexactly(length)
            packet_id, _pos = read_varint(packet)
            if packet_id == 0 and len(packet) > 1:
                continue
            if packet_id == 0:
                if loading:
                    log("Disconnecting Player (server is still starting)", thread="Netty Server IO #1")  # Forge says so for every ping
                body = status_json().encode()
                payload = varint(0) + varint(len(body)) + body
                writer.write(varint(len(payload)) + payload)
                await writer.drain()
            elif packet_id == 1:
                writer.write(varint(len(packet)) + packet)
                await writer.drain()
                break
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, IndexError):
        pass
    finally:
        writer.close()


def handle_command(line: str) -> None:
    parts = line.strip().split()
    if not parts:
        return
    cmd, args = parts[0], parts[1:]
    name = args[1] if cmd == "whitelist" and len(args) >= 2 else (args[0] if args else "")
    level = int(PROPS.get("op-permission-level", "4"))
    if cmd == "stop":
        if BEHAVIOR.get("hang_on_stop"):
            return
        stop_event.set()
    elif cmd == "save-all":
        log("Saving the game (this may take a moment!)")
        if BEHAVIOR.get("no_save_confirmation"):
            return
        delay = float(BEHAVIOR.get("slow_save", 0))
        if delay:
            loop.call_later(delay, log, "Saved the game")
        else:
            log("Saved the game")
    elif cmd == "save-off":
        log("Automatic saving is now disabled")
    elif cmd == "save-on":
        log("Automatic saving is now enabled")
    elif cmd == "say":
        log(f"[Server] {' '.join(args)}")
    elif cmd == "fakejoin" and args:
        players.append(args[0])
        log(f"{args[0]} joined the game")
    elif cmd == "fakeleave" and args:
        if args[0] in players:
            players.remove(args[0])
        log(f"{args[0]} left the game")
    elif cmd == "whitelist" and len(args) == 2 and args[0] in ("add", "remove"):
        add = args[0] == "add"
        if edit_list("whitelist.json", "name", name, add, {"uuid": offline_uuid(name), "name": name}):
            print(f"Added {name} to the whitelist" if add else f"Removed {name} from the whitelist", flush=True)
        else:
            print("Player is already whitelisted" if add else "Player is not whitelisted", flush=True)
    elif cmd == "op" and name:
        entry = {"uuid": offline_uuid(name), "name": name, "level": level, "bypassesPlayerLimit": False}
        print(f"Made {name} a server operator" if edit_list("ops.json", "name", name, True, entry) else "Nothing changed. The player already is an operator", flush=True)
    elif cmd == "deop" and name:
        print(f"Made {name} no longer a server operator" if edit_list("ops.json", "name", name, False) else "Nothing changed. The player is not an operator", flush=True)
    elif cmd == "ban" and name:
        reason = " ".join(args[1:]) or "Banned by an operator."
        entry = {"uuid": offline_uuid(name), "name": name, "created": stamp(), "source": "Server", "expires": "forever", "reason": reason}
        print(f"Banned {name}: {reason}" if edit_list("banned-players.json", "name", name, True, entry) else "Nothing changed. The player is already banned", flush=True)
        if name in players:
            players.remove(name)
    elif cmd == "pardon" and name:
        print(f"Unbanned {name}" if edit_list("banned-players.json", "name", name, False) else "Nothing changed. The player isn't banned", flush=True)
    elif cmd == "ban-ip" and name:
        entry = {"ip": name, "created": stamp(), "source": "Server", "expires": "forever", "reason": "Banned by an operator."}
        print(f"Banned IP {name}" if edit_list("banned-ips.json", "ip", name, True, entry) else "Nothing changed. That IP is already banned", flush=True)
    elif cmd == "pardon-ip" and name:
        print(f"Unbanned IP {name}" if edit_list("banned-ips.json", "ip", name, False) else "Nothing changed. That IP isn't banned", flush=True)
    elif cmd == "kick" and name:
        if name in players:
            players.remove(name)
            print(f"Kicked {name}", flush=True)
        else:
            print("No player was found", flush=True)
    elif cmd == "list":
        log(f"There are {len(players)} of a max of {PROPS.get('max-players', '20')} players online: {', '.join(players)}")
    else:
        print("Unknown or incomplete command, see below for error", flush=True)


def stdin_reader() -> None:
    for line in sys.stdin:
        loop.call_soon_threadsafe(handle_command, line)


def eula_accepted() -> bool:
    path = Path("eula.txt")
    if not path.exists():
        return False
    return any(line.strip().lower() == "eula=true" for line in path.read_text(encoding="utf-8").splitlines())


async def main() -> int:
    global stop_event, loop, loading
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    Path("fake-java.json").write_text(json.dumps({"argv": ARGV, "java_home": os.environ.get("JAVA_HOME"), "path": os.environ.get("PATH", ""), "cwd": os.getcwd()}))
    log("Starting minecraft server version " + str(BEHAVIOR.get("minecraft", "1.21.1")), thread="main")
    await asyncio.sleep(float(BEHAVIOR.get("startup_delay", 0.05)))
    if not eula_accepted():
        log("You need to agree to the EULA in order to run the server. Go to eula.txt for more info.", thread="main")
        return 0
    if BEHAVIOR.get("missing_mods_checker") and Path("config/missing_mods_checker.json").exists():
        # Better MC's Missing Mods Checker finds its list, wants to show a window for the missing mods, and a server has no screen
        for text in (
            "[23:47:29] [main/ERROR]: Error thrown while opening! Exiting",
            "java.awt.HeadlessException:",
            "No X11 DISPLAY variable was set,",
            "or no headful library support was found,",
            "but this program performed an operation which requires it.",
            "\tat java.awt.GraphicsEnvironment.checkHeadless(Unknown Source) ~[?:?]",
            "\tat toni.missingmodschecker.MissingModsWindow.<init>(MissingModsWindow.java:55) ~[missingmodschecker.jar:?]",
        ):
            print(text, flush=True)
        return 1
    if BEHAVIOR.get("old_forge_on_new_java"):  # Forge 1.16.5 before 36.2.23 on a Java released since January 2022
        print("Exception in thread \"main\" java.lang.NoSuchMethodError: sun.security.util.ManifestEntryVerifier.<init>(Ljava/util/jar/Manifest;)V", flush=True)
        return 1
    if BEHAVIOR.get("trace_crash"):  # a Forge 1.12 coremod fails: the cause is in the middle of a long stack trace, and Forge adds noise after it
        for text in (
            "[12:00:01] [main/INFO] [STDERR]: [org.example.LibLoader:init:68]: java.lang.RuntimeException: could not load a library",
            "[12:00:01] [main/INFO] [STDERR]: [org.example.LibLoader:init:68]: \tat org.example.LibLoader.init(LibLoader.java:68)",
            "[12:00:01] [main/INFO] [STDERR]: [org.example.LibLoader:init:68]: Caused by: java.io.FileNotFoundException: https://repo.example/lib-1.1.jar",
            "[12:00:01] [main/INFO] [STDERR]: [org.example.LibLoader:init:68]: \tat sun.net.www.protocol.http.HttpURLConnection.getInputStream0(HttpURLConnection.java:1925)",
            "[12:00:01] [main/INFO] [STDERR]: [org.example.LibLoader:init:68]: \t... 11 more",
            "A problem occurred running the Server launcher.",
            "[12:00:02] [main/INFO] [STDERR]: [net.minecraftforge.Wrapper:run:75]: Caused by: net.minecraftforge.fml.relauncher.FMLSecurityManager$ExitTrappedException",
            "[12:00:02] [main/INFO] [STDERR]: [net.minecraftforge.Wrapper:run:75]: \tat java.lang.Runtime.exit(Runtime.java:101)",
        ):
            print(text, flush=True)
        return 1
    if BEHAVIOR.get("crash_on_start"):
        print("Exception in thread \"main\" java.lang.IllegalStateException: simulated crash", flush=True)
        return 1
    once = Path("fake-first-start.done")
    if BEHAVIOR.get("exit_clean_on_start") or (BEHAVIOR.get("exit_clean_once") and not once.exists()):
        # Better MC downloads its mods at the first start and then ends the server on purpose
        once.write_text("1")
        log("Installed 3 missing mods; the game has to be restarted", thread="missing-mods-download-watcher")
        return 0
    if BEHAVIOR.get("fatal_start"):  # Forge: a client-only mod fails to load. The server says so and then ends ("exit") or hangs ("hang")
        log("Failed to start the minecraft server", "ERROR", "main")
        out = sys.stdout.buffer
        for text in ("net.minecraftforge.fml.LoadingFailedException: Loading errors encountered: [",
                     "\tCIT Resewn (citresewn) has failed to load correctly",
                     "§7java.lang.NoClassDefFoundError: net/minecraft/client/renderer/texture/atlas/SpriteSource,",
                     "\tat net.minecraftforge.fml.ModLoader.gatherAndInitializeMods(ModLoader.java:149)", "]"):
            out.write(text.encode("utf-8") + b"\n")
        out.flush()
        if BEHAVIOR["fatal_start"] == "hang":
            await asyncio.sleep(3600)
        return 1
    if BEHAVIOR.get("crash_if_present") and Path(BEHAVIOR["crash_if_present"]).exists():  # a mod that does not get along with the rest
        print("Exception in thread \"main\" java.lang.NoSuchMethodError: simulated mod conflict", flush=True)
        return 1
    if not Path("server.properties").exists():
        Path("server.properties").write_text("#Minecraft server properties\nserver-port=25565\n", encoding="utf-8")
    for name in ("whitelist.json", "ops.json", "banned-players.json", "banned-ips.json"):
        if not Path(name).exists():
            Path(name).write_text("[]", encoding="utf-8")
    Path("world").mkdir(exist_ok=True)
    Path("world/level.dat").write_text(json.dumps({"saves": 0}), encoding="utf-8")

    port = int(PROPS.get("server-port", "25565"))
    if not BEHAVIOR.get("no_done_line"):
        KEEP_ALIVE.append(await asyncio.start_server(slp_client, "0.0.0.0", port))
    threading.Thread(target=stdin_reader, daemon=True).start()
    log(f"Starting Minecraft server on *:{port}")  # answers_without_done: from here on it answers, and never says Done
    if BEHAVIOR.get("early_empty_status"):
        loading = True
        log('Preparing level "world"')
        await asyncio.sleep(float(BEHAVIOR["early_empty_status"]))
        loading = False
    if BEHAVIOR.get("early_full_status"):  # NeoForge with hundreds of mods answers with its real status while it is still loading
        log('Preparing level "world"')
        await asyncio.sleep(float(BEHAVIOR["early_full_status"]))
    if not BEHAVIOR.get("no_done_line") and not BEHAVIOR.get("answers_without_done"):
        log('Done (1.234s)! For help, type "help"')
    await stop_event.wait()
    log("Stopping the server", thread="Server thread")
    log("Saving worlds")
    return 0


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    os._exit(code)
