// "New server" dialog: everything needed to create and deploy a server in one step.

import { get, post } from "../api.js";
import { clear, h, icon } from "../dom.js";
import { OP_LEVELS } from "../oplevels.js";
import { loadServers, state } from "../store.js";
import { field, jobDialog, modal, notifyError, selectControl, switchControl } from "../ui.js";

const CHANNELS = [
  { id: "stable", name: "Stable", desc: "Tagged releases. The safest choice.", tag: "Recommended" },
  { id: "nightly", name: "Nightly", desc: "Rolling build of the main branch. New features, but worlds can break." },
  { id: "canary", name: "Canary", desc: "Newest rolling build. For testing only." },
];

export const SCHEDULES = [
  { value: "daily", label: "Every day at 04:00", schedule: { type: "daily", at: "04:00" } },
  { value: "12h", label: "Every 12 hours", schedule: { type: "interval", hours: 12 } },
  { value: "6h", label: "Every 6 hours", schedule: { type: "interval", hours: 6 } },
  { value: "off", label: "Never (manual only)", schedule: { type: "off" } },
];

export const GAMEMODES = ["Survival", "Creative", "Adventure", "Spectator"].map((v) => ({ value: v, label: v }));
export const DIFFICULTIES = ["Peaceful", "Easy", "Normal", "Hard"].map((v) => ({ value: v, label: v }));

export async function openCreateServer() {
  let versions = null;
  try { versions = await get("/api/versions"); } catch { /* offline: the dialog still works */ }

  const f = {
    name: "", channel: state.settings?.default_channel || "stable", pinned_tag: "", motd: "", max_players: 20,
    default_gamemode: "Survival", default_difficulty: "Normal", seed: "", online_mode: true, white_list: false, op_permission_level: 1,
    manualPort: false, port: "", bedrock: false, autostart: "last", update_mode: "auto", schedule: "daily", keep: 7, start: true,
  };

  const nameInput = h("input", { class: "input", placeholder: "Survival", maxlength: "40", autocomplete: "off", oninput: (e) => { f.name = e.target.value; } });
  const nameError = h("span", { class: "field-error hidden" });

  // --- version -----------------------------------------------------------------------------------
  const channelHost = h("div", { class: "option-cards" });
  const releaseHost = h("div", { class: "hidden" });
  const releaseSelect = h("select", { class: "input", onchange: (e) => { f.pinned_tag = e.target.value; } }, h("option", { value: "" }, "Latest stable release (recommended)"));
  let releasesLoaded = false;
  async function loadReleases() {
    if (releasesLoaded) return;
    releasesLoaded = true;
    try {
      const data = await get("/api/versions/releases");
      for (const r of data.releases) {
        releaseSelect.appendChild(h("option", { value: r.tag }, `${r.version || r.tag}${r.mc_version ? ` · Minecraft ${r.mc_version}` : ""}`));
      }
    } catch (err) {
      releasesLoaded = false;
      releaseHost.appendChild(h("p", { class: "field-error" }, err.message));
    }
  }
  releaseHost.appendChild(h("details", { ontoggle: (e) => { if (e.target.open) loadReleases(); } },
    h("summary", {}, "Use a specific release instead"),
    h("div", { class: "stack-sm", style: { marginTop: "10px" } }, releaseSelect,
      h("span", { class: "hint" }, "Choosing a fixed release pins the server to it: it will not be updated automatically."))));

  function renderChannels() {
    clear(channelHost);
    for (const c of CHANNELS) {
      const latest = versions?.channels?.[c.id];
      channelHost.appendChild(h("button", { type: "button", class: "option-card", "aria-pressed": String(f.channel === c.id), onclick: () => { f.channel = c.id; renderChannels(); } },
        h("span", { class: "name" }, c.name, c.tag ? h("span", { class: "badge success" }, c.tag) : null),
        h("span", { class: "desc" }, c.desc),
        h("span", { class: "hint mono" }, latest ? `latest: ${latest.label}` : "latest: checked on create")));
    }
    releaseHost.classList.toggle("hidden", f.channel !== "stable");
    if (f.channel !== "stable") f.pinned_tag = "";
  }
  renderChannels();

  // --- port ---------------------------------------------------------------------------------------
  const ranges = state.system?.ports;
  const portInput = h("input", { class: "input", type: "number", min: "1024", max: "65535", placeholder: "automatic", disabled: true, oninput: (e) => { f.port = e.target.value; } });
  const portSwitch = switchControl({ label: "Choose the port myself", hint: ranges ? `Automatic ports come from ${ranges.java_range[0]}–${ranges.java_range[1]} (the range published by the container).` : null,
    onChange: (on) => { f.manualPort = on; portInput.disabled = !on; if (on) portInput.focus(); } });

  const body = h("div", { class: "stack" },
    field("Server name", nameInput, "Shown in this dashboard. The folder name is derived from it.", null), nameError,
    h("div", { class: "section-title" }, "Pumpkin version"), channelHost, releaseHost,
    h("div", { class: "section-title" }, "Game"),
    h("div", { class: "form-grid" },
      field("Game mode", selectControl(GAMEMODES, f.default_gamemode, (v) => { f.default_gamemode = v; })),
      field("Difficulty", selectControl(DIFFICULTIES, f.default_difficulty, (v) => { f.default_difficulty = v; })),
      field("Max players", h("input", { class: "input", type: "number", min: "0", max: "100000", value: String(f.max_players), oninput: (e) => { f.max_players = e.target.value; } }), "0 means unlimited."),
      field("World seed", h("input", { class: "input", placeholder: "random", maxlength: "64", oninput: (e) => { f.seed = e.target.value; } })),
      field("Operator level", selectControl(OP_LEVELS.map(({ value, label }) => ({ value, label })), f.op_permission_level, (v) => { f.op_permission_level = Number(v); }),
        "The level players get when you make them an operator. You can change it per player later."),
      h("div", { class: "span-2" }, field("Message of the day", h("input", { class: "input", placeholder: "A blazingly fast Pumpkin server!", maxlength: "120", oninput: (e) => { f.motd = e.target.value; } }))),
      switchControl({ label: "Verify player accounts (online mode)", hint: "Turn off only for LAN or offline play.", checked: f.online_mode, onChange: (v) => { f.online_mode = v; } }),
      switchControl({ label: "Whitelist", hint: "Only listed players can join.", checked: f.white_list, onChange: (v) => { f.white_list = v; } })),
    h("div", { class: "section-title" }, "Network"),
    h("div", { class: "form-grid" },
      h("div", { class: "stack-sm" }, portSwitch, portInput),
      switchControl({ label: "Allow Bedrock players", hint: "Opens an extra UDP port. Bedrock support is still experimental in Pumpkin.", checked: f.bedrock, onChange: (v) => { f.bedrock = v; } })),
    h("div", { class: "section-title" }, "Automation"),
    h("div", { class: "form-grid" },
      field("Updates", selectControl([
        { value: "auto", label: "Install automatically (backup first)" },
        { value: "notify", label: "Tell me, I will decide" },
        { value: "off", label: "Never check" },
      ], f.update_mode, (v) => { f.update_mode = v; })),
      field("Backups", selectControl(SCHEDULES, f.schedule, (v) => { f.schedule = v; })),
      field("When the container starts", selectControl([
        { value: "last", label: "Restore the previous state" },
        { value: "always", label: "Always start this server" },
        { value: "never", label: "Never start it automatically" },
      ], f.autostart, (v) => { f.autostart = v; })),
      field("Backups to keep", h("input", { class: "input", type: "number", min: "1", max: "365", value: String(f.keep), oninput: (e) => { f.keep = e.target.value; } }))),
    h("p", { class: "hint" }, `Telemetry to pumpkinmc.org follows the default in Settings (currently ${state.settings?.telemetry_default ? "on" : "off"}).`),
    switchControl({ label: "Start the server right away", checked: f.start, onChange: (v) => { f.start = v; } }));

  const createBtn = h("button", { class: "btn primary" }, icon("plus", "sm"), "Create server");
  const dlg = modal({ title: "New server", size: "wide", body, footer: [h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel"), createBtn] });
  nameInput.focus();

  createBtn.addEventListener("click", async () => {
    nameError.classList.add("hidden");
    if (!f.name.trim()) {
      nameError.textContent = "Please give the server a name.";
      nameError.classList.remove("hidden");
      nameInput.focus();
      return;
    }
    const settings = {
      max_players: Number(f.max_players), default_gamemode: f.default_gamemode, default_difficulty: f.default_difficulty,
      online_mode: f.online_mode, white_list: f.white_list, op_permission_level: f.op_permission_level,
    };
    if (f.motd.trim()) settings.motd = f.motd.trim();
    if (f.seed.trim()) settings.seed = f.seed.trim();
    const schedule = SCHEDULES.find((s) => s.value === f.schedule).schedule;
    const payload = {
      name: f.name.trim(), channel: f.channel, pinned_tag: f.pinned_tag || null,
      java_port: f.manualPort && f.port ? Number(f.port) : null, bedrock: f.bedrock, start: f.start, settings,
      policy: { autostart: f.autostart, update_mode: f.update_mode, backup_schedule: schedule, backup_keep: Number(f.keep) || 7 },
    };
    dlg.close();
    const job = await jobDialog(post("/api/servers", payload), { title: `Creating "${payload.name}"` });
    if (job && job.status === "success") {
      await loadServers().catch(notifyError);
      if (job.server_id) location.hash = `#/server/${job.server_id}`;
    }
  });
}
