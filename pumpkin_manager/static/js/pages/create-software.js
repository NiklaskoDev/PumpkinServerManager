// "New Minecraft server" without a modpack: pick Vanilla, Paper or Fabric and a Minecraft version, give it memory and create the server.
// The manager downloads the software from where its maker publishes it, installs the Java it needs and keeps it up to date.

import { get, post, qs } from "../api.js";
import { clear, h, icon } from "../dom.js";
import { BUILD_WORD, DEFAULT_UPDATE_MODE, SOFTWARE, UPDATE_MODES, buildChoices, supportNotices, versionOption } from "../software.js";
import { loadServers } from "../store.js";
import { field, jobDialog, modal, notifyError, selectControl, switchControl } from "../ui.js";
import { javaServerForm } from "./java-form.js";

export function openCreateSoftwareServer() {
  const form = javaServerForm({
    gib: 2, memoryHint: "A handful of friends are fine with 2-4 GiB on Paper or Vanilla; more players, bigger worlds or many plugins and mods need more.",
    gameHint: "These are written to server.properties; everything else keeps Minecraft's defaults.", onChange: () => refresh(),
  });
  const f = {
    kind: "paper", name: "", nameTouched: false, minecraft: "", build: "", snapshots: false, experimental: false, fabricApi: true, flags: true,
    updateMode: "auto", ready: false,
  };
  const cache = { versions: new Map(), builds: new Map() };

  const nameInput = h("input", { class: "input", maxlength: "40", autocomplete: "off", oninput: (e) => { f.name = e.target.value; f.nameTouched = true; refresh(); } });
  const createBtn = h("button", { class: "btn primary", disabled: true }, icon("plus", "sm"), "Create server");
  const refresh = () => { createBtn.disabled = !(f.ready && f.minecraft && f.name.trim() && form.f.eula); };
  const defaultName = () => `${SOFTWARE.find((s) => s.id === f.kind).name} server`;
  const setName = () => { if (!f.nameTouched) { f.name = defaultName(); nameInput.value = f.name; } };

  // ---- the software -------------------------------------------------------------------------------------------------
  const kindHost = h("div", { class: "option-cards" });
  function drawKinds() {
    clear(kindHost);
    for (const s of SOFTWARE) {
      kindHost.appendChild(h("button", { type: "button", class: "option-card", "aria-pressed": String(f.kind === s.id), onclick: () => choose(s.id) },
        h("span", { class: "name" }, s.name, s.tag ? h("span", { class: "badge success" }, s.tag) : null),
        h("span", { class: "desc" }, s.desc)));
    }
  }

  // ---- the version --------------------------------------------------------------------------------------------------
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
    drawKinds();
    setName();
    drawUpdates();
    drawOptions();
    await loadVersions();
  }

  async function loadVersions() {
    const mine = ++versionTicket;
    f.ready = false;
    refresh();
    clear(versionHost).appendChild(h("p", { class: "muted" }, "Looking up the versions…"));
    clear(buildHost);
    clear(noticeHost);
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
    if (f.kind !== "paper") {
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
    const key = `${f.kind}:${f.minecraft}:${f.experimental}`;
    try {
      if (!cache.builds.has(key)) cache.builds.set(key, await get(`/api/software/${f.kind}/builds${qs({ minecraft: f.minecraft, experimental: f.experimental ? "true" : undefined })}`));
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
    const word = BUILD_WORD[f.kind];
    buildHost.appendChild(h("details", {},
      h("summary", {}, `Choose a specific ${word}`),
      h("div", { class: "stack-sm", style: { marginTop: "10px" } },
        field(f.kind === "paper" ? "Paper build" : "Fabric loader version", selectControl(buildChoices(f.kind, options), f.build, (v) => { f.build = v; })),
        options.has_experimental
          ? switchControl({ label: "Include experimental builds", hint: "Alpha and beta builds are not meant for servers that matter.", checked: f.experimental,
            onChange: (on) => { f.experimental = on; f.build = ""; loadBuilds(); } })
          : null,
        h("span", { class: "hint" }, "Without a choice the newest stable one is installed. Later updates stay on the Minecraft version you choose here."))));
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
    clear(updatesHost).appendChild(field("Updates", selectControl(UPDATE_MODES[f.kind], f.updateMode, (v) => { f.updateMode = v; }),
      f.kind === "vanilla" ? "A new Minecraft version changes the world, so it is never installed without you." : "A new Minecraft version is never installed by itself: it can break plugins or mods."));
  }

  const body = h("div", { class: "stack" },
    h("div", { class: "section-title" }, "Software"), kindHost,
    h("div", { class: "section-title" }, "Version"), versionHost, snapshotsHost, buildHost, noticeHost, optionsHost,
    field("Server name", nameInput, "Shown in this dashboard. The folder name is derived from it.", null),
    ...form.memorySection, ...form.gameSection, ...form.networkSection([updatesHost]),
    h("div", { class: "notice" }, icon("info", "sm"), "The first start downloads what the software needs and generates the world, which takes a minute or two. The server is backed up before every change the manager makes."),
    form.eulaSwitch, form.startSwitch);

  const dlg = modal({ title: "New Minecraft server", size: "wide", body, footer: [h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel"), createBtn] });
  drawKinds();
  drawUpdates();
  drawOptions();
  setName();
  loadVersions();

  createBtn.addEventListener("click", async () => {
    const software = { kind: f.kind, minecraft: f.minecraft };
    if (f.build) software.build = f.build;
    if (f.kind === "fabric") software.fabric_api = f.fabricApi;
    if (f.kind === "paper") software.recommended_flags = f.flags;
    const shared = form.payload();
    const payload = { name: f.name.trim(), engine: "java", eula: true, ...shared, software, policy: { ...shared.policy, update_mode: f.updateMode } };
    dlg.close();
    const job = await jobDialog(post("/api/servers", payload), { title: `Creating "${payload.name}"` });
    if (job && job.status === "success") {
      await loadServers().catch(notifyError);
      if (job.server_id) location.hash = `#/server/${job.server_id}`;
    }
  });
}
