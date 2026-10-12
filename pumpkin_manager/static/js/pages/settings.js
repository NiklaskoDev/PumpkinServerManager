// Global settings: update checks, notifications, defaults, account and system information.

import { post, put } from "../api.js";
import { clear, fmtBytes, fmtDuration, h, icon } from "../dom.js";
import { loadSettings, loadSystem, state } from "../store.js";
import { busy, field, notifyError, selectControl, switchControl, toast } from "../ui.js";
import { passwordCard } from "./account.js";

const WEBHOOK_EVENTS = [
  ["update_applied", "A server was updated"],
  ["update_failed", "An update failed or was rolled back"],
  ["update_available", "An update is available (notify-only servers)"],
  ["backup_failed", "A backup failed"],
  ["crashed", "A server crashed or failed to start"],
];

export async function render() {
  await Promise.all([loadSettings(), loadSystem()]);
  const s = { ...state.settings };
  const sys = state.system;
  const draft = { ...s, github_token: undefined };
  let webhookEvents = new Set(s.webhook_events);

  async function save(patch) {
    try {
      state.settings = await put("/api/settings", patch);
      toast("Settings saved", "success");
    } catch (err) { notifyError(err); }
  }

  const tokenInput = h("input", { class: "input", type: "password", autocomplete: "off", placeholder: s.has_github_token ? "A token is saved (type to replace)" : "optional", oninput: (e) => { draft.github_token = e.target.value; } });
  const keyInput = h("input", { class: "input", type: "password", autocomplete: "off", spellcheck: "false", "aria-label": "CurseForge API key" });
  const keyState = h("p", { class: "hint" });
  const removeKey = h("button", { class: "btn sm danger hidden", onclick: (e) => busy(e.currentTarget, () => saveKey("")) }, "Remove the key");
  function showKey() {
    const fromEnvironment = state.settings.curseforge_key_from_environment;
    keyInput.value = "";
    keyInput.placeholder = state.settings.has_curseforge_key ? "A key is saved (type to replace it)" : "Paste your CurseForge API key";
    keyState.textContent = fromEnvironment ? "A key is set with the CURSEFORGE_API_KEY variable of the container. A key saved here is used instead." :
      state.settings.has_curseforge_key ? "A key is saved. It is never shown again." : "No key yet: CurseForge modpacks are not available.";
    removeKey.classList.toggle("hidden", !(state.settings.has_curseforge_key && !fromEnvironment));
  }
  async function saveKey(value) {
    try {
      state.settings = await put("/api/settings", { curseforge_api_key: value });
      toast(value ? "CurseForge accepted the key" : "The key was removed", "success");
    } catch (err) { notifyError(err); }
    showKey();
  }
  showKey();
  const modpacks = h("div", { class: "card stack" }, h("h2", {}, "Modpacks"),
    h("p", { class: "muted" }, "Modrinth modpacks need no setup. CurseForge hands out its modpacks through an API that wants a key: ",
      h("a", { href: "https://console.curseforge.com/", target: "_blank", rel: "noopener noreferrer" }, "create one at console.curseforge.com"),
      " (it starts with $2a$10$). The key is personal, so each person uses their own; it is stored in this manager's database and sent to CurseForge only."),
    field("CurseForge API key", keyInput, null), keyState,
    h("div", { class: "row" },
      h("button", { class: "btn primary sm", onclick: (e) => {
        const value = keyInput.value.trim();
        if (!value) return toast("Paste the key first.", "info");
        return busy(e.currentTarget, () => saveKey(value));
      } }, icon("save", "sm"), "Save and check the key"), removeKey));

  const webhookInput = h("input", { class: "input", type: "url", value: s.webhook_url || "", placeholder: "https://discord.com/api/webhooks/…", oninput: (e) => { draft.webhook_url = e.target.value; } });

  const updates = h("div", { class: "card stack" }, h("h2", {}, "Update checks"),
    switchControl({ label: "Check for new Pumpkin builds automatically", checked: s.update_check_enabled, onChange: (v) => { draft.update_check_enabled = v; } }),
    h("div", { class: "form-grid" },
      field("Check every (minutes)", h("input", { class: "input", type: "number", min: "5", value: String(s.update_check_minutes), oninput: (e) => { draft.update_check_minutes = Number(e.target.value); } }), "GitHub allows 60 anonymous requests an hour; a check uses three."),
      field("Default channel for new servers", selectControl([{ value: "stable", label: "Stable" }, { value: "nightly", label: "Nightly" }, { value: "canary", label: "Canary" }], s.default_channel, (v) => { draft.default_channel = v; })),
      field("GitHub token", tokenInput, "Only needed if you hit rate limits. No permissions are required.", null),
      field("Builds to keep as spares per channel", h("input", { class: "input", type: "number", min: "0", max: "10", value: String(s.keep_old_builds), oninput: (e) => { draft.keep_old_builds = Number(e.target.value); } }), "The newest builds of each channel that stay on disk when old ones are cleaned up automatically after an update. The Clean up button on the Versions page removes them too. A build that a server runs, can roll back to or is about to update to is never removed.")),
    h("div", {}, h("button", { class: "btn primary sm", onclick: () => save({
      update_check_enabled: draft.update_check_enabled, update_check_minutes: draft.update_check_minutes, default_channel: draft.default_channel,
      keep_old_builds: draft.keep_old_builds, ...(draft.github_token !== undefined ? { github_token: draft.github_token } : {}),
    }) }, icon("save", "sm"), "Save")));

  const notifications = h("div", { class: "card stack" }, h("h2", {}, "Notifications"),
    h("p", { class: "muted" }, "Get a message when something needs your attention. Discord and Slack webhooks work out of the box; any other URL receives a JSON POST."),
    field("Webhook URL", webhookInput),
    h("div", { class: "stack-sm" }, WEBHOOK_EVENTS.map(([key, label]) =>
      switchControl({ label, checked: webhookEvents.has(key), onChange: (on) => { if (on) webhookEvents.add(key); else webhookEvents.delete(key); } }))),
    h("div", { class: "row" },
      h("button", { class: "btn primary sm", onclick: () => save({ webhook_url: webhookInput.value.trim(), webhook_events: [...webhookEvents] }) }, icon("save", "sm"), "Save"),
      h("button", { class: "btn sm", onclick: async (e) => {
        await busy(e.currentTarget, async () => { await post("/api/settings/webhook-test", { url: webhookInput.value.trim() || null }); toast("Test message sent", "success"); });
      } }, icon("bell", "sm"), "Send test")));

  const defaults = h("div", { class: "card stack" }, h("h2", {}, "Defaults"),
    switchControl({ label: "Enable Pumpkin's telemetry heartbeat for new servers", hint: "Pumpkin can send an anonymous heartbeat to market.pumpkinmc.org. Off unless you enable it here or per server.", checked: s.telemetry_default, onChange: (v) => { draft.telemetry_default = v; } }),
    field("Public address shown to players", h("input", { class: "input", value: s.public_address || "", placeholder: "defaults to the address you are using now", oninput: (e) => { draft.public_address = e.target.value; } }), "A host name or IP, for example play.example.com. Also used in the sign-in details for friends' accounts."),
    h("div", {}, h("button", { class: "btn primary sm", onclick: () => save({ telemetry_default: draft.telemetry_default, public_address: draft.public_address ?? s.public_address }) }, icon("save", "sm"), "Save")));

  const account = passwordCard();

  const diskRow = (label, free, total) => [h("dt", {}, label), h("dd", {}, h("div", { class: "meter" }, h("span", { style: { width: `${Math.round(((total - free) / total) * 100)}%` } })), h("span", { class: "muted small" }, `${fmtBytes(free)} free of ${fmtBytes(total)}`))];
  const system = h("div", { class: "card stack" }, h("h2", {}, "System"),
    h("dl", { class: "kv" },
      h("dt", {}, "Manager"), h("dd", {}, `v${sys.version} · Python ${sys.python} · ${sys.arch}`),
      h("dt", {}, "Uptime"), h("dd", {}, fmtDuration(sys.uptime)),
      h("dt", {}, "Time zone"), h("dd", {}, sys.timezone, h("span", { class: "muted small" }, " (used for the daily backup time)")),
      h("dt", {}, "Data folder"), h("dd", { class: "mono" }, sys.data_dir),
      ...diskRow("Data disk", sys.disk.data_free, sys.disk.data_total),
      h("dt", {}, "Backup folder"), h("dd", { class: "mono" }, sys.backup_dir),
      ...diskRow("Backup disk", sys.disk.backup_free, sys.disk.backup_total),
      h("dt", {}, "Game ports"), h("dd", {}, `${sys.ports.java_range[0]}–${sys.ports.java_range[1]} (TCP) · Bedrock ${sys.ports.bedrock_range[0]}–${sys.ports.bedrock_range[1]} (UDP)`),
      h("dt", {}, "Binaries"), h("dd", {}, fmtBytes(sys.disk.binaries)),
      h("dt", {}, "GitHub token"), h("dd", {}, sys.github_token ? "configured" : "not set"),
      h("dt", {}, "CurseForge key"), h("dd", {}, sys.curseforge_key ? "configured" : "not set")));

  return {
    el: h("div", { class: "stack" },
      h("div", { class: "page-head" }, h("div", { class: "titles" }, h("h1", {}, "Settings"), h("p", {}, "Applies to the whole manager. Each server has its own settings on its page."))),
      h("div", { class: "grid cols-2" }, h("div", { class: "stack" }, updates, modpacks, defaults, account), h("div", { class: "stack" }, notifications, system))),
  };
}
