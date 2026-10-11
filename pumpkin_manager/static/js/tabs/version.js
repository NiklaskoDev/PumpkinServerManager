// Version tab: what is installed, update policy, switching versions, rolling back and the update history.

import { get, patch, post } from "../api.js";
import { clear, fmtBytes, fmtDateTime, fmtRelative, h, icon } from "../dom.js";
import { bus } from "../store.js";
import { channelChip, confirmDialog, field, jobDialog, modal, notifyError, selectControl, switchControl, toast, versionText } from "../ui.js";

const STATUS_BADGE = { success: "success", rolled_back: "warning", failed: "error", running: "busy", interrupted: "warning" };
const STATUS_TEXT = { success: "Updated", rolled_back: "Rolled back", failed: "Failed", running: "Running", interrupted: "Interrupted" };

export function render({ id, getServer }) {
  const currentHost = h("div", {});
  const policyHost = h("div", {});
  const rollbackHost = h("div", {});
  const historyHost = h("div", {});
  let history = [];

  const update = (body = {}) => jobDialog(post(`/api/servers/${id}/update`, body), { title: `Updating ${getServer().name}`, successToast: "Update installed" });

  // ---- current version + actions -----------------------------------------------------------------
  function renderCurrent(s) {
    clear(currentHost);
    const b = s.build;
    currentHost.appendChild(h("div", { class: "card stack" },
      h("div", { class: "card-head" }, h("h2", {}, "Installed version"), channelChip(s.channel), s.pinned_tag ? h("span", { class: "chip" }, icon("shield", "sm"), "pinned") : null),
      b ? h("dl", { class: "kv" },
        h("dt", {}, "Version"), h("dd", {}, h("strong", {}, versionText(b))),
        h("dt", {}, "Release"), h("dd", {}, b.release_url ? h("a", { href: b.release_url, target: "_blank", rel: "noopener noreferrer" }, b.tag, " ", icon("external", "sm")) : b.tag),
        h("dt", {}, "Published"), h("dd", {}, fmtDateTime(b.published_at)),
        h("dt", {}, "SHA-256"), h("dd", { class: "mono small" }, b.digest.slice(0, 32) + "…"),
        h("dt", {}, "Size"), h("dd", {}, fmtBytes(b.size)),
        h("dt", {}, "Last check"), h("dd", {}, fmtRelative(s.update.last_check))) : h("p", { class: "muted" }, "No version assigned."),
      s.update.available
        ? h("div", { class: "update-banner" }, icon("arrowUp"), h("div", { class: "grow" }, "Update available: ", h("strong", {}, versionText(s.update.pending_build)), h("div", { class: "small muted" }, "A verified backup is created before anything changes. If the new version fails to start, the manager rolls back automatically.")),
          h("button", { class: "btn primary", disabled: !!s.busy, onclick: () => update({}) }, "Update now"))
        : h("div", { class: "notice success" }, icon("check", "sm"), s.update.mode === "off" || s.pinned_tag ? "Automatic update checks are off for this server." : "You are on the newest version of this channel."),
      h("div", { class: "row" },
        h("button", { class: "btn sm", onclick: async (e) => {
          e.currentTarget.disabled = true;
          try { await post(`/api/servers/${id}/update/check`); toast("Checked GitHub for updates", "success", null, 2000); } catch (err) { notifyError(err); } finally { e.currentTarget.disabled = false; }
        } }, icon("restart", "sm"), "Check now"),
        h("button", { class: "btn sm", disabled: !!s.busy, onclick: openPicker }, icon("layers", "sm"), "Switch version..."))));
  }

  // ---- policy -----------------------------------------------------------------------------------
  function renderPolicy(s) {
    clear(policyHost);
    const p = s.policy;
    let mode = p.update_mode, onlyEmpty = p.update_only_when_empty, defer = p.update_max_defer_hours;
    policyHost.appendChild(h("div", { class: "card stack" }, h("h2", {}, "Update policy"),
      field("When a new version appears", selectControl([
        { value: "auto", label: "Install automatically (backup first)" },
        { value: "notify", label: "Notify me only" },
        { value: "off", label: "Do not check" },
      ], mode, (v) => { mode = v; }), "Checks run every few hours and on demand. Every update starts with a verified backup."),
      switchControl({ label: "Wait until nobody is online", hint: "Automatic updates are postponed while players are connected.", checked: onlyEmpty, onChange: (v) => { onlyEmpty = v; } }),
      field("…but not longer than (hours)", h("input", { class: "input", type: "number", min: "0", max: "720", value: String(defer), oninput: (e) => { defer = Number(e.target.value); } }), "After this the players get a warning and the update goes ahead. 0 updates immediately."),
      h("div", {}, h("button", { class: "btn primary sm", onclick: async () => {
        try { await patch(`/api/servers/${id}`, { policy: { update_mode: mode, update_only_when_empty: onlyEmpty, update_max_defer_hours: defer } }); toast("Update policy saved", "success"); } catch (err) { notifyError(err); }
      } }, icon("save", "sm"), "Save"))));
  }

  // ---- rollback -----------------------------------------------------------------------------------
  function renderRollback(s) {
    clear(rollbackHost);
    if (!s.previous_build) return;
    let restore = false;
    rollbackHost.appendChild(h("div", { class: "card stack" }, h("h2", {}, "Roll back"),
      h("p", {}, "Return to ", h("strong", {}, versionText(s.previous_build)), ". The server is stopped, a safety backup is made, then it starts on the older version."),
      switchControl({ label: "Also restore the world from before the last update", hint: "Use this if the newer version changed or damaged your data. Progress since that update is lost.", onChange: (v) => { restore = v; } }),
      h("div", {}, h("button", { class: "btn danger", disabled: !!s.busy, onclick: async () => {
        const ok = await confirmDialog({ title: "Roll back this server?", confirmLabel: "Roll back", danger: true,
          message: restore ? "The world will be restored from the backup taken before the last update; everything since is lost." : "Only the Pumpkin version changes; your world files are kept as they are." });
        if (ok) jobDialog(post(`/api/servers/${id}/rollback`, { restore_data: restore }), { title: "Rolling back", successToast: "Rolled back" });
      } }, icon("rollback", "sm"), "Roll back"))));
  }

  // ---- history ------------------------------------------------------------------------------------
  function renderHistory() {
    clear(historyHost);
    historyHost.appendChild(h("div", { class: "card tight" }, h("div", { class: "card-section" }, h("h2", {}, "Update history")),
      history.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "From → to"), h("th", {}, "Trigger"), h("th", {}, "Result"), h("th", {}, "Details"))),
        h("tbody", {}, history.map((u) => h("tr", {},
          h("td", { class: "nowrap", title: fmtDateTime(u.started_at) }, fmtRelative(u.started_at)),
          h("td", {}, `${u.from_tag || "–"} → ${u.to_tag || "–"}`),
          h("td", {}, u.trigger),
          h("td", {}, h("span", { class: `badge ${STATUS_BADGE[u.status] || ""}` }, STATUS_TEXT[u.status] || u.status)),
          h("td", { class: "muted small" }, u.message && u.message !== "Updated" ? u.message : u.backup_id ? "backup taken first" : "")))))) : h("p", { class: "muted", style: { padding: "0 18px 18px" } }, "No updates have been applied yet.")));
  }

  async function loadHistory() {
    try { history = (await get(`/api/servers/${id}/updates`)).updates; } catch { history = []; }
    renderHistory();
  }

  // ---- version picker ------------------------------------------------------------------------------
  async function openPicker() {
    let overview, releases;
    try { [overview, releases] = await Promise.all([get("/api/versions"), get("/api/versions/releases")]); } catch (err) { return notifyError(err); }
    const s = getServer();
    let pin = false;
    const rows = [];
    for (const ch of ["stable", "nightly", "canary"]) {
      const latest = overview.channels[ch];
      if (latest) rows.push({ title: `Latest ${ch}`, sub: latest.label, body: { channel: ch }, current: s.build?.id === latest.id });
    }
    for (const r of releases.releases) rows.push({ title: `Release ${r.version || r.tag}`, sub: `${r.mc_version ? `Minecraft ${r.mc_version} · ` : ""}${fmtDateTime(r.published_at)}`, body: { tag: r.tag }, current: s.build?.tag === r.tag });
    const dlg = modal({
      title: "Switch version", size: "wide",
      body: h("div", { class: "stack" },
        h("div", { class: "notice warning" }, icon("alert", "sm"), "Switching to another channel or an older release can break your world. A verified backup is always created first, and the manager rolls back if the new version does not start."),
        switchControl({ label: "Stay on this version (disable automatic updates)", onChange: (v) => { pin = v; } }),
        h("div", { class: "list" }, rows.map((row) => h("div", { class: "list-item" },
          h("div", { class: "grow" }, h("strong", {}, row.title), h("div", { class: "muted small" }, row.sub)),
          row.current ? h("span", { class: "badge success" }, "installed") : h("button", { class: "btn sm primary", onclick: async () => {
            dlg.close();
            await update({ ...row.body, pin });
          } }, "Install"))))),
    });
  }

  // Live server events must not rebuild forms the user may be typing in: the policy card renders once,
  // the others only when something they display has changed.
  let lastShape = "";
  function refresh(s) {
    const shape = JSON.stringify([s.build?.id, s.previous_build?.id, s.update.available, s.update.pending_build?.id, s.update.last_check, s.busy, s.pinned_tag, s.channel, s.update.mode]);
    if (shape === lastShape) return;
    lastShape = shape;
    renderCurrent(s);
    renderRollback(s);
  }

  const s = getServer();
  renderPolicy(s);
  refresh(s);
  loadHistory();
  const off = bus.on("job", ({ job, previous }) => { if (job.server_id === id && previous?.status === "running" && job.status !== "running") loadHistory(); });
  return {
    el: h("div", { class: "stack" }, h("div", { class: "grid cols-2" }, currentHost, h("div", { class: "stack" }, policyHost, rollbackHost)), historyHost),
    dispose: off,
    onServer: refresh,
  };
}
