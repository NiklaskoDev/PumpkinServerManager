// "New modpack server": choose a modpack (search Modrinth or CurseForge, paste a link or upload a file), give it memory and create the server.

import { get, post, qs, upload } from "../api.js";
import { clear, fmtBytes, h, icon } from "../dom.js";
import { OP_LEVELS } from "../oplevels.js";
import { loadServers, state } from "../store.js";
import { field, jobDialog, modal, notifyError, packIcon, segmented, selectControl, switchControl } from "../ui.js";
import { SCHEDULES } from "./create.js";

const EULA_URL = "https://aka.ms/MinecraftEULA";
const GAMEMODES = [["survival", "Survival"], ["creative", "Creative"], ["adventure", "Adventure"], ["spectator", "Spectator"]].map(([value, label]) => ({ value, label }));
const DIFFICULTIES = [["peaceful", "Peaceful"], ["easy", "Easy"], ["normal", "Normal"], ["hard", "Hard"]].map(([value, label]) => ({ value, label }));
const LOADERS = { fabric: "Fabric", forge: "Forge", neoforge: "NeoForge", quilt: "Quilt" };
const SOURCES = [{ value: "modrinth", label: "Modrinth" }, { value: "curseforge", label: "CurseForge" }];
const compact = new Intl.NumberFormat("en", { notation: "compact" });
const gib = (bytes) => `${(bytes / 1024 ** 3).toFixed(bytes >= 10 * 1024 ** 3 ? 0 : 1)} GiB`;
const versionLabel = (v) => `${v.number}${v.type !== "release" ? ` (${v.type})` : ""} · Minecraft ${v.minecraft.join(", ")} · ${v.loaders.map((l) => LOADERS[l] || l).join(", ")}${v.server_pack ? " · server files" : ""}`;
const sourceOf = (text) => (/modrinth\.com\//i.test(text) ? "modrinth" : /curseforge\.com\//i.test(text) ? "curseforge" : null);

export function openCreateModpackServer() {
  const memory = state.system?.memory;
  const f = {
    name: "", nameTouched: false, source: "modrinth", pack: null, version: "", serverPack: true, gib: 4, touched: {}, op: 1, manualPort: false, port: "",
    autostart: "last", schedule: "daily", keep: 7, eula: false, start: true, clientOnly: true,
  };
  const setting = (key, value) => { f.touched[key] = value; };

  const nameInput = h("input", { class: "input", placeholder: "My modpack server", maxlength: "40", autocomplete: "off", oninput: (e) => { f.name = e.target.value; f.nameTouched = true; refresh(); } });
  const createBtn = h("button", { class: "btn primary", disabled: true }, icon("plus", "sm"), "Create server");
  const refresh = () => { createBtn.disabled = !(f.pack && f.name.trim() && f.eula) || needsKey(); };

  // ---- the modpack ------------------------------------------------------------------------------------------
  let sources = null; // which sources are ready (CurseForge needs an API key); asked when the dialog opens
  const curseforgeReady = () => !!(sources && sources.curseforge.enabled);
  const fromCurseforge = () => f.pack && (f.pack.source === "curseforge" || (f.pack.source === "upload" && f.pack.summary.source === "curseforge"));
  const needsKey = () => !!fromCurseforge() && sources !== null && !curseforgeReady();
  const usesServerFiles = () => f.pack?.source === "curseforge" && f.serverPack && !!f.pack.versions.find((v) => v.id === f.version)?.server_pack;
  const keyNotice = () => h("div", { class: "notice warning" }, icon("alert", "sm"), h("span", {}, "This modpack comes from CurseForge, which needs an API key. ",
    h("a", { href: "#/settings", onclick: () => dlg.close() }, "Add yours in Settings"), "."));

  const packHost = h("div", { class: "stack" });
  const resultsHost = h("div", { class: "pack-list" });
  const hintHost = h("div", { class: "stack-sm" });
  const searchInput = h("input", { class: "input", autocomplete: "off", spellcheck: "false", "aria-label": "Search modpacks" });
  const fileInput = h("input", { type: "file", accept: ".mrpack,.zip", class: "hidden" });
  const tabs = segmented(SOURCES, f.source, (v) => { f.source = v; searchInput.value = ""; drawPicker(); });
  const picker = h("div", { class: "stack-sm" },
    h("div", { class: "row" }, tabs),
    h("div", { class: "row" }, h("div", { class: "grow" }, searchInput), h("button", { type: "button", class: "btn", onclick: () => fileInput.click() }, icon("upload", "sm"), "Upload a file"), fileInput),
    hintHost, resultsHost);

  function drawHints() {
    const curse = f.source === "curseforge";
    searchInput.placeholder = curse ? "Search CurseForge modpacks, or enter a project number or link" : "Search Modrinth modpacks, or paste a modpack link";
    clear(hintHost);
    if (curse && sources && !curseforgeReady()) {
      hintHost.appendChild(h("div", { class: "notice warning" }, icon("alert", "sm"), h("span", {}, "CurseForge modpacks need an API key. ",
        h("a", { href: "#/settings", onclick: () => dlg.close() }, "Add yours in Settings"), " (it explains how to get one).")));
      return;
    }
    hintHost.appendChild(h("p", { class: "hint" }, curse
      ? "These are CurseForge's modpacks. A link such as curseforge.com/minecraft/modpacks/… works too, and so does the pack's project number (the Project ID on its CurseForge page). "
      : "These are Modrinth's modpacks that can run on a server. A link such as modrinth.com/modpack/… works too. ",
      "A modpack file from your computer works as well: a .mrpack from Modrinth, or the .zip that CurseForge's Download button gives you."));
  }

  function drawPicker() {
    drawHints();
    search();
  }

  let timer = 0;
  let ticket = 0;
  async function search() {
    const text = searchInput.value.trim();
    const mine = ++ticket;
    const linked = sourceOf(text);
    if (linked && linked !== f.source) { f.source = linked; tabs.set(linked); drawHints(); } // a link says where it belongs
    if (f.source === "curseforge" && sources && !curseforgeReady()) return void clear(resultsHost);
    clear(resultsHost).appendChild(h("p", { class: "muted" }, "Searching…"));
    try {
      if (linked || (f.source === "curseforge" && /^\d+$/.test(text))) {
        const project = await get(`/api/modpacks/project${qs({ ref: text, source: f.source })}`);
        if (mine === ticket) choose(project);
        return;
      }
      const data = await get(`/api/modpacks/search${qs({ q: text, source: f.source })}`);
      if (mine !== ticket) return;
      clear(resultsHost);
      if (data.note) resultsHost.appendChild(h("div", { class: "notice" }, icon("info", "sm"), data.note));
      if (!data.hits.length) resultsHost.appendChild(h("p", { class: "muted" }, "No modpack found. Try another name."));
      data.hits.forEach((hit) => resultsHost.appendChild(resultRow(hit)));
    } catch (err) {
      if (mine === ticket) clear(resultsHost).appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"), err.message));
    }
  }
  searchInput.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 350); });
  searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); search(); } });

  function resultRow(hit) {
    return h("button", { type: "button", class: "pack-result", onclick: () => pick(hit.id) },
      packIcon(hit.icon_url, 44),
      h("span", { class: "grow stack-sm" },
        h("strong", { class: "truncate" }, hit.title),
        h("span", { class: "muted small pack-desc" }, hit.description || ""),
        h("span", { class: "row" },
          ...hit.loaders.map((l) => h("span", { class: "chip" }, LOADERS[l] || l)),
          hit.minecraft.length ? h("span", { class: "chip" }, `Minecraft ${hit.minecraft[hit.minecraft.length - 1]}`) : null,
          h("span", { class: "muted small" }, `${compact.format(hit.downloads || 0)} downloads`))));
  }

  async function pick(ref) {
    clear(resultsHost).appendChild(h("p", { class: "muted" }, "Loading…"));
    try { choose(await get(`/api/modpacks/project${qs({ ref, source: f.source })}`)); } catch (err) { notifyError(err); search(); }
  }

  function setName(name) {
    if (f.nameTouched) return;
    f.name = name.slice(0, 40);
    nameInput.value = f.name;
  }

  function choose(project) {
    if (!project.versions.length) { notifyError(new Error("That modpack has no versions that can be installed.")); return search(); }
    const source = project.source || "modrinth";
    f.source = source;
    tabs.set(source);
    f.pack = { source, id: project.id, title: project.title, description: project.description, icon_url: project.icon_url, versions: project.versions };
    f.version = (project.versions.find((v) => v.type === "release") || project.versions[0]).id;
    f.serverPack = true;
    setName(project.title);
    renderPack();
    refresh();
  }

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    clear(resultsHost).appendChild(h("p", { class: "muted" }, `Uploading ${file.name}…`));
    try {
      const result = await upload("/api/modpacks/upload", file);
      f.pack = { source: "upload", upload: result.upload, summary: result.pack };
      setName(result.pack.name);
      renderPack();
      refresh();
    } catch (err) { notifyError(err); search(); }
  });

  function selectedPack() {
    const p = f.pack;
    const change = h("button", { type: "button", class: "btn sm", onclick: () => { f.pack = null; renderPack(); refresh(); updateClientOnly(); } }, "Choose another");
    if (p.source === "upload") {
      const s = p.summary;
      const fromCurse = s.source === "curseforge";
      return h("div", { class: "pack-selected stack-sm" },
        h("div", { class: "row" }, packIcon(null, 44), h("div", { class: "grow" }, h("strong", {}, s.name),
          h("div", { class: "muted small" }, `Version ${s.version || "?"} · Minecraft ${s.minecraft} · ${s.loader.label} · ${s.mods} mods · ${fromCurse ? "downloaded from CurseForge when the server is created" : `${fmtBytes(s.download_bytes)} to download`}`)), change),
        needsKey() ? keyNotice() : null);
    }
    const note = h("div", { class: "muted small" });
    const serverBox = h("div", { class: "stack-sm" });
    const showNote = () => {
      const v = p.versions.find((x) => x.id === f.version);
      const what = v ? `Minecraft ${v.minecraft.join(", ")} · ${v.loaders.map((l) => LOADERS[l] || l).join(", ")}` : "";
      note.textContent = !v ? "" : p.source === "curseforge" && v.server_pack && f.serverPack ? `${what} · installed from the author's server files`
        : `${what} · pack file ${fmtBytes(v.size)} (the mods are downloaded on top)`;
      updateClientOnly();
    };
    const describe = () => {
      const v = p.versions.find((x) => x.id === f.version);
      clear(serverBox);
      if (p.source === "curseforge" && v) {
        if (v.server_pack) {
          serverBox.appendChild(switchControl({
            label: "Use the pack author's server files", checked: f.serverPack, onChange: (on) => { f.serverPack = on; showNote(); },
            hint: "This version comes with the files its author made for servers: the mods they chose and tested together. Turn this off to install from the pack's list of mods instead.",
          }));
        } else {
          serverBox.appendChild(h("p", { class: "hint" }, "This version has no server files of its own, so the mods in its list are downloaded one by one. Mods whose authors do not allow that are listed afterwards for you to add by hand."));
        }
      }
      showNote();
    };
    const card = h("div", { class: "pack-selected stack-sm" },
      h("div", { class: "row" }, packIcon(p.icon_url, 44), h("div", { class: "grow" }, h("strong", {}, p.title), h("div", { class: "muted small pack-desc" }, p.description || "")), change),
      field("Version", selectControl(p.versions.map((v) => ({ value: v.id, label: versionLabel(v) })), f.version, (v) => { f.version = v; describe(); })),
      note, serverBox, needsKey() ? keyNotice() : null);
    describe();
    return card;
  }

  function renderPack() {
    clear(packHost);
    if (f.pack) packHost.appendChild(selectedPack());
    else { packHost.appendChild(picker); drawPicker(); }
  }

  // ---- memory -----------------------------------------------------------------------------------------------------
  const memNotice = h("div", {});
  const memInput = h("input", { class: "input", type: "number", min: "1", max: "1024", step: "0.5", value: String(f.gib), "aria-label": "Memory in GiB", oninput: (e) => { f.gib = Number(e.target.value) || 0; memCheck(); } });
  function memCheck() {
    clear(memNotice);
    if (!memory) return;
    const wanted = f.gib * 1024 ** 3;
    const ceiling = memory.limit || memory.total;
    if (wanted > ceiling) memNotice.appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"), `That is more than this ${memory.limit ? "container may use" : "machine has"} (${gib(ceiling)}). The server would not start.`));
    else if (wanted > memory.available * 0.9) memNotice.appendChild(h("div", { class: "notice warning" }, icon("alert", "sm"), `Only ${gib(memory.available)} is free right now. The server may be slow, or other programs may be pushed out of memory.`));
  }
  const memHint = memory
    ? `This machine has ${gib(memory.total)} (${gib(memory.available)} free)${memory.limit ? `; the container is limited to ${gib(memory.limit)}` : ""}. Most modpacks need 4-8 GiB.`
    : "Most modpacks need 4-8 GiB.";
  memCheck();

  // ---- the rest ---------------------------------------------------------------------------------------------------
  const ranges = state.system?.ports;
  const portInput = h("input", { class: "input", type: "number", min: "1024", max: "65535", placeholder: "automatic", disabled: true, oninput: (e) => { f.port = e.target.value; } });
  const portSwitch = switchControl({ label: "Choose the port myself", hint: ranges ? `Automatic ports come from ${ranges.java_range[0]}–${ranges.java_range[1]} (the range published by the container).` : null,
    onChange: (on) => { f.manualPort = on; portInput.disabled = !on; if (on) portInput.focus(); } });
  const eulaSwitch = switchControl({
    label: h("span", {}, "I accept the ", h("a", { href: EULA_URL, target: "_blank", rel: "noopener noreferrer" }, "Minecraft End User License Agreement")),
    hint: "Mojang requires this for every Minecraft server. The manager writes eula.txt for you once you accept.",
    onChange: (v) => { f.eula = v; refresh(); },
  });

  const clientOnlyNote = h("span", {});
  const clientOnlySwitch = switchControl({ label: "Leave out mods that only work on a client", checked: true, onChange: (v) => { f.clientOnly = v; }, hint: clientOnlyNote });
  function updateClientOnly() {
    const own = usesServerFiles();
    clientOnlySwitch.input.disabled = own;
    clientOnlyNote.textContent = own
      ? "Not needed here: the author's server files only hold what a server needs, and they are used as they are."
      : "Many modpacks are made for players and list minimaps, menus and shader helpers for the server too, which can stop it from starting. Modrinth and CurseForge know which mods cannot run on a server; those are left out (you will see the list afterwards).";
  }
  updateClientOnly();

  const body = h("div", { class: "stack" },
    h("div", { class: "section-title" }, "Modpack"), packHost,
    clientOnlySwitch,
    field("Server name", nameInput, "Shown in this dashboard. Starts out as the modpack's name.", null),
    h("div", { class: "section-title" }, "Memory"),
    field("Memory for the server (GiB)", memInput, memHint), memNotice,
    h("div", { class: "section-title" }, "Game"),
    h("p", { class: "hint" }, "Only what you change here is written; everything else keeps the modpack's own settings."),
    h("div", { class: "form-grid" },
      field("Game mode", selectControl(GAMEMODES, "survival", (v) => setting("gamemode", v))),
      field("Difficulty", selectControl(DIFFICULTIES, "easy", (v) => setting("difficulty", v))),
      field("Max players", h("input", { class: "input", type: "number", min: "1", max: "100000", value: "20", oninput: (e) => { if (e.target.value) setting("max-players", Number(e.target.value)); } })),
      field("World seed", h("input", { class: "input", placeholder: "random", maxlength: "64", oninput: (e) => setting("level-seed", e.target.value) })),
      field("Operator level", selectControl(OP_LEVELS.map(({ value, label }) => ({ value, label })), f.op, (v) => { f.op = Number(v); }),
        "The level players get when you make them an operator. You can change it per player later."),
      h("div", { class: "span-2" }, field("Message of the day", h("input", { class: "input", placeholder: "A Minecraft Server", maxlength: "120", oninput: (e) => setting("motd", e.target.value) }))),
      switchControl({ label: "Verify player accounts (online mode)", hint: "Turn off only for LAN or offline play.", checked: true, onChange: (v) => setting("online-mode", v) }),
      switchControl({ label: "Whitelist", hint: "Only listed players can join.", checked: false, onChange: (v) => setting("white-list", v) })),
    h("div", { class: "section-title" }, "Network & automation"),
    h("div", { class: "form-grid" },
      h("div", { class: "stack-sm" }, portSwitch, portInput),
      field("Backups", selectControl(SCHEDULES, f.schedule, (v) => { f.schedule = v; })),
      field("When the container starts", selectControl([
        { value: "last", label: "Restore the previous state" }, { value: "always", label: "Always start this server" }, { value: "never", label: "Never start it automatically" },
      ], f.autostart, (v) => { f.autostart = v; })),
      field("Backups to keep", h("input", { class: "input", type: "number", min: "1", max: "365", value: String(f.keep), oninput: (e) => { f.keep = e.target.value; } }))),
    h("div", { class: "notice" }, icon("info", "sm"), "The first start generates the world, which can take several minutes for a big modpack. The server is backed up before every change the manager makes."),
    eulaSwitch,
    switchControl({ label: "Start the server right away", checked: true, onChange: (v) => { f.start = v; } }));

  const dlg = modal({ title: "New modpack server", size: "wide", body, footer: [h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel"), createBtn] });
  renderPack();
  get("/api/modpacks/sources").then((found) => { sources = found; if (f.pack) renderPack(); else drawHints(); refresh(); }).catch(notifyError);

  createBtn.addEventListener("click", async () => {
    const payload = {
      name: f.name.trim(), engine: "java", eula: true, memory_mb: Math.round(f.gib * 1024), start: f.start,
      modpack: f.pack.source === "upload" ? { source: "upload", upload: f.pack.upload }
        : f.pack.source === "curseforge" ? { source: "curseforge", project: f.pack.id, version: f.version, server_pack: f.serverPack }
          : { source: "modrinth", project: f.pack.id, version: f.version },
      java_port: f.manualPort && f.port ? Number(f.port) : null, leave_out_client_only: f.clientOnly,
      settings: { "op-permission-level": f.op, ...f.touched },
      policy: { autostart: f.autostart, backup_schedule: SCHEDULES.find((s) => s.value === f.schedule).schedule, backup_keep: Number(f.keep) || 7 },
    };
    dlg.close();
    const job = await jobDialog(post("/api/servers", payload), { title: `Creating "${payload.name}"` });
    if (job && job.status === "success") {
      await loadServers().catch(notifyError);
      if (job.server_id) location.hash = `#/server/${job.server_id}`;
    }
  });
}
