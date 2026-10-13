// "New Minecraft server" without a modpack: pick the software and a Minecraft version, give it memory and create the server.
// The manager downloads the software from where its maker publishes it (Spigot it builds with SpigotMC's tool), installs the Java it
// needs and keeps it up to date. Two choices of software share this dialog: Vanilla, Paper or Fabric, and Spigot or BungeeCord.
// BungeeCord is a proxy: it has no Minecraft version, no world and no EULA, so the dialog shows what applies to it instead.

import { get, post, qs } from "../api.js";
import { clear, h, icon } from "../dom.js";
import { BUILD_WORD, DEFAULT_UPDATE_MODE, SOFTWARE, UPDATE_MODES, buildChoices, buildName, isProxy, supportNotices, updateHint, versionOption } from "../software.js";
import { loadServers, state } from "../store.js";
import { field, jobDialog, modal, notifyError, selectControl, switchControl } from "../ui.js";
import { javaServerForm } from "./java-form.js";

export const CLASSIC = ["paper", "vanilla", "fabric"];
export const SPIGOT_FAMILY = ["spigot", "bungeecord"];
const DEFAULT_GIB = { proxy: 0.5, server: 2 };

export function openCreateSoftwareServer({ kinds = CLASSIC, title = "New Minecraft server" } = {}) {
  const choices = kinds.map((id) => SOFTWARE.find((s) => s.id === id));
  const withProxy = kinds.some(isProxy);
  const form = javaServerForm({
    gib: DEFAULT_GIB.server,
    memoryHint: withProxy
      ? "A Spigot server is fine with 2-4 GiB for a handful of friends; more players or plugins need more. A BungeeCord proxy needs very little: half a GiB is plenty."
      : "A handful of friends are fine with 2-4 GiB on Paper or Vanilla; more players, bigger worlds or many plugins and mods need more.",
    gameHint: "These are written to server.properties; everything else keeps Minecraft's defaults.", onChange: () => refresh(),
  });
  const f = {
    kind: kinds[0], name: "", nameTouched: false, minecraft: "", build: "", snapshots: false, experimental: false, fabricApi: true, flags: true,
    updateMode: DEFAULT_UPDATE_MODE[kinds[0]], ready: false, motd: "", maxPlayers: 100,
  };
  const cache = { versions: new Map(), builds: new Map() };
  const proxy = () => isProxy(f.kind);
  const named = () => SOFTWARE.find((s) => s.id === f.kind).name;

  const nameInput = h("input", { class: "input", maxlength: "40", autocomplete: "off", oninput: (e) => { f.name = e.target.value; f.nameTouched = true; refresh(); } });
  const createBtn = h("button", { class: "btn primary", disabled: true }, icon("plus", "sm"), "Create server");
  const refresh = () => { createBtn.disabled = !(f.ready && (proxy() || f.minecraft) && f.name.trim() && (proxy() || form.f.eula)); };
  const setName = () => { if (!f.nameTouched) { f.name = proxy() ? `${named()} proxy` : `${named()} server`; nameInput.value = f.name; } };
  let lastGib = DEFAULT_GIB[proxy() ? "proxy" : "server"];

  // ---- the software -------------------------------------------------------------------------------------------------
  const kindHost = h("div", { class: "option-cards" });
  const noteHost = h("div", {});
  function drawKinds() {
    clear(kindHost);
    for (const s of choices) {
      kindHost.appendChild(h("button", { type: "button", class: "option-card", "aria-pressed": String(f.kind === s.id), onclick: () => choose(s.id) },
        h("span", { class: "name" }, s.name, s.tag ? h("span", { class: "badge success" }, s.tag) : null),
        h("span", { class: "desc" }, s.desc)));
    }
    const note = SOFTWARE.find((s) => s.id === f.kind).note;
    clear(noteHost);
    if (note) noteHost.appendChild(h("div", { class: `notice${proxy() ? " warning" : ""}` }, icon(proxy() ? "alert" : "info", "sm"), note));
  }

  // ---- the version --------------------------------------------------------------------------------------------------
  const versionTitle = h("div", { class: "section-title" }, "Version");
  const versionHost = h("div", {});
  const snapshotsHost = h("div", {});
  const buildHost = h("div", {});
  const noticeHost = h("div", { class: "stack-sm" });
  const optionsHost = h("div", { class: "stack-sm" });
  let versionTicket = 0;
  let buildTicket = 0;

  async function choose(kind) {
    f.kind = kind;
    f.minecraft = f.build = "";
    f.snapshots = f.experimental = false;
    f.updateMode = DEFAULT_UPDATE_MODE[kind];
    const wanted = DEFAULT_GIB[proxy() ? "proxy" : "server"];
    if (form.f.gib === lastGib) form.setMemory(wanted); // what the person typed is left alone
    lastGib = wanted;
    drawKinds();
    setName();
    drawShape();
    drawUpdates();
    drawOptions();
    await loadVersions();
  }

  // what differs between a server and a proxy: its settings, whether there is a EULA to accept, what the first start does
  const gameHost = h("div", { class: "stack" }, ...form.gameSection);
  const proxyHost = h("div", { class: "stack hidden" },
    h("div", { class: "section-title" }, "Proxy"),
    h("div", { class: "form-grid" },
      h("div", { class: "span-2" }, field("Message of the day", h("input", { class: "input", placeholder: "A network of Minecraft servers", maxlength: "200", oninput: (e) => { f.motd = e.target.value; } }),
        "Shown in the multiplayer server list. &a, &l and the like are colour and style codes.")),
      field("Players shown as the maximum", h("input", { class: "input", type: "number", min: "1", max: "100000", value: String(f.maxPlayers), oninput: (e) => { if (e.target.value) f.maxPlayers = Number(e.target.value); } }),
        "The number the server list shows. It does not stop anybody joining.")));
  const eulaHost = h("div", {}, form.eulaSwitch);
  const firstStartHost = h("div", {});
  const serversHost = h("div", {});
  function drawShape() {
    gameHost.classList.toggle("hidden", proxy());
    proxyHost.classList.toggle("hidden", !proxy());
    eulaHost.classList.toggle("hidden", proxy());
    versionTitle.textContent = proxy() ? "Build" : "Version";
    clear(firstStartHost).appendChild(h("div", { class: "notice" }, icon("info", "sm"),
      proxy() ? "The first start downloads BungeeCord's libraries and modules, which takes a moment. The proxy is backed up before every change the manager makes."
        : f.kind === "spigot" ? "Making Spigot takes five to fifteen minutes (the job shows how far it is). The first start then generates the world, which takes a minute or two. The server is backed up before every change the manager makes."
        : "The first start downloads what the software needs and generates the world, which takes a minute or two. The server is backed up before every change the manager makes."));
    clear(serversHost);
    if (proxy()) {
      const servers = [...state.servers.values()].filter((s) => !s.proxy);
      serversHost.appendChild(h("div", { class: "notice" }, icon("info", "sm"), servers.length
        ? `The proxy sends players to the servers this manager runs now: ${servers.slice(0, 6).map((s) => s.name).join(", ")}${servers.length > 6 ? ` and ${servers.length - 6} more` : ""}. Servers you add later go into config.yml by hand.`
        : "This manager runs no other server yet, so the proxy starts with a placeholder server that you replace in config.yml (Files tab) once there is one."));
    }
  }

  async function loadVersions() {
    const mine = ++versionTicket;
    f.ready = false;
    refresh();
    clear(buildHost);
    clear(noticeHost);
    if (proxy()) { // no Minecraft version: its builds are all there is
      clear(versionHost);
      clear(snapshotsHost);
      return loadBuilds();
    }
    clear(versionHost).appendChild(h("p", { class: "muted" }, "Looking up the versions…"));
    const key = `${f.kind}:${f.snapshots}`;
    try {
      if (!cache.versions.has(key)) cache.versions.set(key, await get(`/api/software/${f.kind}/versions${qs({ snapshots: f.snapshots ? "true" : undefined })}`));
    } catch (err) {
      if (mine === versionTicket) clear(versionHost).appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"), err.message));
      return;
    }
    if (mine !== versionTicket) return;
    const data = cache.versions.get(key);
    if (!data.versions.some((v) => v.id === f.minecraft)) f.minecraft = data.recommended;
    clear(versionHost).appendChild(field("Minecraft version",
      selectControl(data.versions.map((v) => ({ value: v.id, label: versionOption(v, data.recommended) })), f.minecraft, (v) => { f.minecraft = v; f.build = ""; loadBuilds(); })));
    clear(snapshotsHost);
    if (f.kind === "vanilla" || f.kind === "fabric") {
      snapshotsHost.appendChild(switchControl({ label: "Show snapshots", hint: "Test versions of the next Minecraft release.", checked: f.snapshots, onChange: (on) => { f.snapshots = on; loadVersions(); } }));
    }
    await loadBuilds();
  }

  async function loadBuilds() {
    const mine = ++buildTicket;
    clear(buildHost);
    clear(noticeHost);
    f.ready = false;
    refresh();
    const key = `${f.kind}:${proxy() ? "" : f.minecraft}:${f.experimental}`;
    try {
      if (!cache.builds.has(key)) cache.builds.set(key, await get(`/api/software/${f.kind}/builds${qs({ minecraft: proxy() ? undefined : f.minecraft, experimental: f.experimental ? "true" : undefined })}`));
    } catch (err) {
      if (mine === buildTicket) noticeHost.appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"), err.message));
      return;
    }
    if (mine !== buildTicket) return;
    const options = cache.builds.get(key);
    f.ready = true;
    refresh();
    supportNotices(f.kind, options).forEach((n) => noticeHost.appendChild(n));
    if (f.kind === "vanilla") return;
    if (f.kind === "spigot") { // SpigotMC offers one build for a Minecraft version: the newest, which is the one that gets built
      buildHost.appendChild(h("p", { class: "hint" }, `SpigotMC's current build for Minecraft ${f.minecraft} is ${options.builds[0]?.label || "unknown"}. Later updates stay on the Minecraft version you choose here.`));
      drawOptions(options);
      return;
    }
    buildHost.appendChild(h("details", { open: proxy() },
      h("summary", {}, `Choose a specific ${BUILD_WORD[f.kind]}`),
      h("div", { class: "stack-sm", style: { marginTop: "10px" } },
        field(buildName(f.kind), selectControl(buildChoices(f.kind, options), f.build, (v) => { f.build = v; })),
        options.has_experimental
          ? switchControl({ label: "Include experimental builds", hint: "Alpha and beta builds are not meant for servers that matter.", checked: f.experimental,
            onChange: (on) => { f.experimental = on; f.build = ""; loadBuilds(); } })
          : null,
        h("span", { class: "hint" }, proxy()
          ? "Without a choice the newest build is installed."
          : "Without a choice the newest stable one is installed. Later updates stay on the Minecraft version you choose here."))));
    drawOptions(options);
  }

  function drawOptions(options) {
    clear(optionsHost);
    if (f.kind === "fabric") {
      optionsHost.appendChild(switchControl({ label: "Install Fabric API", checked: f.fabricApi, onChange: (on) => { f.fabricApi = on; },
        hint: "Nearly every Fabric mod needs it. The manager keeps it matching the Minecraft version when you switch versions." }));
    }
    if (f.kind === "paper" && (!options || options.recommended_flags.length)) {
      optionsHost.appendChild(switchControl({ label: "Use the Java options PaperMC recommends", checked: f.flags, onChange: (on) => { f.flags = on; },
        hint: "Garbage collection settings that PaperMC tests with its servers. You can change them later in the server's settings." }));
    }
  }

  // ---- updates ---------------------------------------------------------------------------------------------------------
  const updatesHost = h("div", {});
  function drawUpdates() {
    clear(updatesHost).appendChild(field("Updates", selectControl(UPDATE_MODES[f.kind], f.updateMode, (v) => { f.updateMode = v; }), updateHint(f.kind)));
  }

  const body = h("div", { class: "stack" },
    h("div", { class: "section-title" }, "Software"), kindHost, noteHost,
    versionTitle, versionHost, snapshotsHost, buildHost, noticeHost, optionsHost,
    field("Server name", nameInput, "Shown in this dashboard. The folder name is derived from it.", null),
    ...form.memorySection, gameHost, proxyHost, ...form.networkSection([updatesHost]),
    serversHost, firstStartHost,
    eulaHost, form.startSwitch);

  const dlg = modal({ title, size: "wide", body, footer: [h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel"), createBtn] });
  choose(kinds[0]);

  createBtn.addEventListener("click", async () => {
    const software = { kind: f.kind };
    if (!proxy()) software.minecraft = f.minecraft;
    if (f.build) software.build = f.build;
    if (f.kind === "fabric") software.fabric_api = f.fabricApi;
    if (f.kind === "paper") software.recommended_flags = f.flags;
    const shared = form.payload();
    // a proxy has no server.properties (its settings are those of config.yml) and needs no EULA
    const settings = proxy() ? { max_players: f.maxPlayers, ...(f.motd.trim() ? { motd: f.motd } : {}) } : shared.settings;
    const payload = {
      name: f.name.trim(), engine: "java", ...(proxy() ? {} : { eula: true }), ...shared, settings, software, policy: { ...shared.policy, update_mode: f.updateMode },
    };
    dlg.close();
    const job = await jobDialog(post("/api/servers", payload), { title: `Creating "${payload.name}"` });
    if (job && job.status === "success") {
      await loadServers().catch(notifyError);
      if (job.server_id) location.hash = `#/server/${job.server_id}`;
    }
  });
}
