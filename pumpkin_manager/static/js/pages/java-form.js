// The parts of the "new Java server" dialogs that are the same whatever the server runs (a modpack, Vanilla, Paper, Fabric, Spigot or
// BungeeCord): memory, game settings, network and automation, the Minecraft EULA, and the request they add up to.

import { clear, h, icon } from "../dom.js";
import { OP_LEVELS } from "../oplevels.js";
import { state } from "../store.js";
import { field, selectControl, switchControl } from "../ui.js";
import { SCHEDULES } from "./create.js";

export const EULA_URL = "https://aka.ms/MinecraftEULA";
const GAMEMODES = [["survival", "Survival"], ["creative", "Creative"], ["adventure", "Adventure"], ["spectator", "Spectator"]].map(([value, label]) => ({ value, label }));
const DIFFICULTIES = [["peaceful", "Peaceful"], ["easy", "Easy"], ["normal", "Normal"], ["hard", "Hard"]].map(([value, label]) => ({ value, label }));
const gib = (bytes) => `${(bytes / 1024 ** 3).toFixed(bytes >= 10 * 1024 ** 3 ? 0 : 1)} GiB`;

// `memoryHint` ends the sentence about the machine's memory ("Most modpacks need 4-8 GiB."), `gameHint` introduces the game settings,
// `onChange` hears about anything that can change whether the dialog may create the server (the EULA). `difficulty` is the one the
// server starts with when nothing is changed (a pack's own: GT New Horizons runs on hard).
export function javaServerForm({ gib: startGib, memoryHint, gameHint, motd = "A Minecraft Server", difficulty = "easy", onChange = () => {} }) {
  const machine = state.system?.memory;
  const f = { gib: startGib, touched: {}, op: 1, manualPort: false, port: "", autostart: "last", schedule: "daily", keep: 7, eula: false, start: true };
  const setting = (key, value) => { f.touched[key] = value; };

  // ---- memory -----------------------------------------------------------------------------------------------------
  const memNotice = h("div", {});
  const memInput = h("input", { class: "input", type: "number", min: "0.5", max: "1024", step: "0.5", value: String(f.gib), "aria-label": "Memory in GiB", oninput: (e) => { f.gib = Number(e.target.value) || 0; memCheck(); } });
  function memCheck() {
    clear(memNotice);
    if (!machine) return;
    const wanted = f.gib * 1024 ** 3;
    const ceiling = machine.limit || machine.total;
    if (wanted > ceiling) memNotice.appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"), `That is more than this ${machine.limit ? "container may use" : "machine has"} (${gib(ceiling)}). The server would not start.`));
    else if (wanted > machine.available * 0.9) memNotice.appendChild(h("div", { class: "notice warning" }, icon("alert", "sm"), `Only ${gib(machine.available)} is free right now. The server may be slow, or other programs may be pushed out of memory.`));
  }
  memCheck();
  const memorySection = [
    h("div", { class: "section-title" }, "Memory"),
    field("Memory for the server (GiB)", memInput, machine
      ? `This machine has ${gib(machine.total)} (${gib(machine.available)} free)${machine.limit ? `; the container is limited to ${gib(machine.limit)}` : ""}. ${memoryHint}`
      : memoryHint),
    memNotice,
  ];

  // ---- game ---------------------------------------------------------------------------------------------------------
  const gameSection = [
    h("div", { class: "section-title" }, "Game"),
    h("p", { class: "hint" }, gameHint),
    h("div", { class: "form-grid" },
      field("Game mode", selectControl(GAMEMODES, "survival", (v) => setting("gamemode", v))),
      field("Difficulty", selectControl(DIFFICULTIES, difficulty, (v) => setting("difficulty", v))),
      field("Max players", h("input", { class: "input", type: "number", min: "1", max: "100000", value: "20", oninput: (e) => { if (e.target.value) setting("max-players", Number(e.target.value)); } })),
      field("World seed", h("input", { class: "input", placeholder: "random", maxlength: "64", oninput: (e) => setting("level-seed", e.target.value) })),
      field("Operator level", selectControl(OP_LEVELS.map(({ value, label }) => ({ value, label })), f.op, (v) => { f.op = Number(v); }),
        "The level players get when you make them an operator. You can change it per player later."),
      h("div", { class: "span-2" }, field("Message of the day", h("input", { class: "input", placeholder: motd, maxlength: "120", oninput: (e) => setting("motd", e.target.value) }))),
      switchControl({ label: "Verify player accounts (online mode)", hint: "Turn off only for LAN or offline play.", checked: true, onChange: (v) => setting("online-mode", v) }),
      switchControl({ label: "Whitelist", hint: "Only listed players can join.", checked: false, onChange: (v) => setting("white-list", v) })),
  ];

  // ---- network and automation -----------------------------------------------------------------------------------------
  const ranges = state.system?.ports;
  const portInput = h("input", { class: "input", type: "number", min: "1024", max: "65535", placeholder: "automatic", disabled: true, oninput: (e) => { f.port = e.target.value; } });
  const portSwitch = switchControl({ label: "Choose the port myself", hint: ranges ? `Automatic ports come from ${ranges.java_range[0]}–${ranges.java_range[1]} (the range published by the container).` : null,
    onChange: (on) => { f.manualPort = on; portInput.disabled = !on; if (on) portInput.focus(); } });
  // `extra` adds controls of its own (the kind of updates, for instance) to the automation grid
  const networkSection = (extra = []) => [
    h("div", { class: "section-title" }, "Network & automation"),
    h("div", { class: "form-grid" },
      h("div", { class: "stack-sm" }, portSwitch, portInput),
      ...extra,
      field("Backups", selectControl(SCHEDULES, f.schedule, (v) => { f.schedule = v; })),
      field("When the container starts", selectControl([
        { value: "last", label: "Restore the previous state" }, { value: "always", label: "Always start this server" }, { value: "never", label: "Never start it automatically" },
      ], f.autostart, (v) => { f.autostart = v; })),
      field("Backups to keep", h("input", { class: "input", type: "number", min: "1", max: "365", value: String(f.keep), oninput: (e) => { f.keep = e.target.value; } }))),
  ];

  const eulaSwitch = switchControl({
    label: h("span", {}, "I accept the ", h("a", { href: EULA_URL, target: "_blank", rel: "noopener noreferrer" }, "Minecraft End User License Agreement")),
    hint: "Mojang requires this for every Minecraft server. The manager writes eula.txt for you once you accept.",
    onChange: (v) => { f.eula = v; onChange(); },
  });
  const startSwitch = switchControl({ label: "Start the server right away", checked: true, onChange: (v) => { f.start = v; } });

  // for a dialog that changes what it makes (a proxy needs far less memory than a server): the person's own number is not touched
  const setMemory = (value) => { f.gib = value; memInput.value = String(value); memCheck(); };

  return {
    f, memorySection, gameSection, networkSection, eulaSwitch, startSwitch, setMemory,
    // what the server is created with (the caller adds its name, what it runs, and `eula: true`)
    payload: () => ({
      memory_mb: Math.round(f.gib * 1024), start: f.start, java_port: f.manualPort && f.port ? Number(f.port) : null,
      settings: { "op-permission-level": f.op, ...f.touched },
      policy: { autostart: f.autostart, backup_schedule: SCHEDULES.find((s) => s.value === f.schedule).schedule, backup_keep: Number(f.keep) || 7 },
    }),
  };
}
