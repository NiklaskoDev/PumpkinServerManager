// "New GT New Horizons server": the pack's own server files from gtnewhorizons.com, in the version and for the Java the person chooses.
// (GT New Horizons has no modpack to search for: its page on CurseForge only lists the pack, and a server made from that cannot start.)

import { get, post, qs } from "../api.js";
import { clear, fmtDate, h, icon } from "../dom.js";
import { loadServers } from "../store.js";
import { field, jobDialog, modal, notifyError, selectControl } from "../ui.js";
import { javaServerForm } from "./java-form.js";

const SITE = "https://www.gtnewhorizons.com";
const versionLabel = (v) => `${v.number}${v.type !== "release" ? " (beta)" : ""}${v.published ? ` · ${fmtDate(v.published)}` : ""}`;
const buildLabel = (b) => `${b.name}${b.variant === "modern" ? " (recommended)" : ""} · runs on Java ${b.runs_on ?? "?"}`;

export function openCreateGtnhServer() {
  // the memory, game, network and EULA parts are the same as for every other Java server
  const form = javaServerForm({
    gib: 6, difficulty: "hard", motd: "GT:New Horizons", onChange: () => refresh(),
    memoryHint: "GT New Horizons needs at least 6 GiB: add 0.5 GiB for every further player early in the game, and 1 GiB each from the late game on.",
    gameHint: "Only what you change here is written; everything else keeps the pack's own settings (Realistic World Gen, hard difficulty, flying and command blocks allowed, whitelist on).",
  });
  const f = { name: "GT New Horizons", nameTouched: false, version: "", variant: "modern", updates: "notify", project: null };

  const nameInput = h("input", { class: "input", maxlength: "40", autocomplete: "off", value: f.name, oninput: (e) => { f.name = e.target.value; f.nameTouched = true; refresh(); } });
  const createBtn = h("button", { class: "btn primary", disabled: true }, icon("plus", "sm"), "Create server");
  const refresh = () => { createBtn.disabled = !(f.project && f.version && f.name.trim() && form.f.eula); };

  // ---- the version and the build ------------------------------------------------------------------------------------
  const versionHost = h("div", { class: "stack-sm" });
  const noteHost = h("div", { class: "stack-sm" });
  const current = () => f.project?.versions.find((v) => v.id === f.version);

  function drawVersion() {
    const v = current();
    const builds = v?.builds || [];
    if (!builds.some((b) => b.variant === f.variant)) f.variant = builds[0]?.variant || "modern";
    clear(versionHost).append(
      field("Version", selectControl(f.project.versions.map((x) => ({ value: x.id, label: versionLabel(x) })), f.version, (value) => { f.version = value; drawVersion(); })),
      field("Server files", selectControl(builds.map((b) => ({ value: b.variant, label: buildLabel(b) })), f.variant, (value) => { f.variant = value; drawVersion(); }),
        f.variant === "modern"
          ? "The pack's build for current Java: what its authors recommend, and faster. The manager installs the Java it needs."
          : "The pack's build for Java 8: older and slower, but it runs where no current Java is available."));
    clear(noteHost);
    if (v && v.type !== "release") {
      noteHost.appendChild(h("div", { class: "notice warning" }, icon("alert", "sm"), `${v.number} is a test version (beta or release candidate). It can break worlds; the stable versions are the ones to play on.`));
    }
  }

  async function load() {
    clear(versionHost).appendChild(h("p", { class: "muted" }, "Looking up the versions…"));
    try {
      f.project = await get(`/api/modpacks/project${qs({ ref: "gtnh", source: "gtnh" })}`);
    } catch (err) {
      clear(versionHost).appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"), err.message));
      return;
    }
    f.version = (f.project.versions.find((v) => v.type === "release") || f.project.versions[0]).id;
    drawVersion();
    refresh();
  }

  const updatesHost = field("Updates", selectControl([{ value: "notify", label: "Tell me about new versions" }, { value: "off", label: "Do not look for them" }], f.updates, (v) => { f.updates = v; }),
    "A new version is never installed by itself: GT New Horizons wants one major version at a time, and every player's client has to match the server.");

  const body = h("div", { class: "stack" },
    h("div", { class: "notice" }, icon("info", "sm"), h("span", {},
      "GT New Horizons is a large expert-mode modpack for Minecraft 1.7.10, built around GregTech. The manager installs the pack's official server files from ",
      h("a", { href: SITE, target: "_blank", rel: "noopener noreferrer" }, "gtnewhorizons.com"),
      " (the pack's CurseForge page only lists it, so a server made from there cannot start). Players need the same version of the pack on their computers.")),
    h("div", { class: "section-title" }, "Version"), versionHost, noteHost,
    field("Server name", nameInput, "Shown in this dashboard. The folder name is derived from it.", null),
    ...form.memorySection, ...form.gameSection, ...form.networkSection([updatesHost]),
    h("div", { class: "notice" }, icon("info", "sm"), "The pack is about 450 MB. The first start loads about 200 mods and generates the world, which takes several minutes. The server is backed up before every update, and GT New Horizons' own backups (ServerUtilities) are switched off because the manager makes them."),
    form.eulaSwitch, form.startSwitch);

  const dlg = modal({ title: "New GT New Horizons server", size: "wide", body, footer: [h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel"), createBtn] });
  load();

  createBtn.addEventListener("click", async () => {
    const shared = form.payload();
    const payload = {
      name: f.name.trim(), engine: "java", eula: true, ...shared, modpack: { source: "gtnh", version: f.version, variant: f.variant },
      policy: { ...shared.policy, update_mode: f.updates },
    };
    dlg.close();
    const job = await jobDialog(post("/api/servers", payload), { title: `Creating "${payload.name}"` });
    if (job && job.status === "success") {
      await loadServers().catch(notifyError);
      if (job.server_id) location.hash = `#/server/${job.server_id}`;
    }
  });
}
