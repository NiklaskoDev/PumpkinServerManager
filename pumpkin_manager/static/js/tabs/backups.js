// Backups: list/create/restore/download/protect/delete. Used for one server (tab) and for all servers (page).

import { del, get, patch, post, qs, upload } from "../api.js";
import { clear, fmtBytes, fmtDateTime, fmtRelative, h, icon } from "../dom.js";
import { bus, state } from "../store.js";
import { confirmDialog, emptyState, field, jobDialog, notifyError, promptDialog, selectControl, switchControl, toast } from "../ui.js";
import { SCHEDULES } from "../pages/create.js";

const KIND_LABEL = {
  manual: ["Manual", "info"], scheduled: ["Scheduled", ""], "pre-update": ["Before update", "accent"], "pre-restore": ["Before restore", "warning"],
  "pre-delete": ["Before delete", "warning"], uploaded: ["Uploaded", "info"], imported: ["Found on disk", ""],
};

export function backupsPanel({ serverId = null } = {}) {
  const host = h("div", { class: "stack" });
  const listHost = h("div", {});
  const summary = h("div", { class: "muted small" });
  let data = { backups: [], total_size: 0, free: 0 };

  async function load() {
    try { data = await get(`/api/backups${qs({ server_id: serverId })}`); } catch (err) { return notifyError(err); }
    render();
  }

  async function restore(b) {
    let safety = true;
    const ok = await confirmDialog({
      title: `Restore "${b.server_name}" from this backup?`, confirmLabel: "Restore", danger: true,
      message: h("div", { class: "stack-sm" },
        h("p", {}, `All files of the server will be replaced with the backup from ${fmtDateTime(b.created_at)}. Anything done since then is lost.`),
        h("p", { class: "muted" }, "The server is stopped for the restore and started again afterwards if it was running.")),
      extra: switchControl({ label: "Save the current state as a safety backup first", checked: true, onChange: (v) => { safety = v; } }),
    });
    if (!ok) return;
    const job = await jobDialog(post(`/api/backups/${b.id}/restore`, { safety_backup: safety }), { title: "Restoring backup", successToast: "Backup restored" });
    if (job) load();
  }

  async function remove(b) {
    if (!(await confirmDialog({ title: "Delete this backup?", message: `${b.file_name} will be permanently removed from disk.`, confirmLabel: "Delete", danger: true }))) return;
    try { await del(`/api/backups/${b.id}`); toast("Backup deleted", "success"); load(); } catch (err) { notifyError(err); }
  }

  async function toggleProtect(b) {
    try { await patch(`/api/backups/${b.id}`, { protected: !b.protected }); load(); } catch (err) { notifyError(err); }
  }

  function render() {
    clear(listHost);
    summary.textContent = `${data.backups.length} backup${data.backups.length === 1 ? "" : "s"} · ${fmtBytes(data.total_size)} used · ${fmtBytes(data.free)} free in the backup folder`;
    if (!data.backups.length) {
      return listHost.appendChild(emptyState("archive", "No backups yet", serverId ? "Create one now, or wait for the schedule." : "Backups of your servers will show up here."));
    }
    listHost.appendChild(h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, !serverId ? h("th", {}, "Server") : null, h("th", {}, "Created"), h("th", {}, "Type"), h("th", {}, "Size"), h("th", {}, "Details"), h("th", {}, ""))),
      h("tbody", {}, data.backups.map((b) => {
        const [label, cls] = KIND_LABEL[b.kind] || [b.kind, ""];
        return h("tr", {},
          !serverId ? h("td", {}, b.server_exists ? h("a", { href: `#/server/${b.server_id}/backups` }, b.server_name || b.server_id) : h("span", { class: "muted", title: "This server no longer exists" }, `${b.server_name || b.server_id} (deleted)`)) : null,
          h("td", {}, h("div", { title: fmtDateTime(b.created_at) }, fmtRelative(b.created_at)), h("div", { class: "muted small" }, fmtDateTime(b.created_at))),
          h("td", {}, h("span", { class: `badge ${cls}` }, label), b.protected ? h("span", { class: "chip", title: "Protected from automatic deletion" }, icon("shield", "sm")) : null,
            !b.consistent ? h("span", { class: "chip", title: "Taken while the server was running and not fully paused" }, "crash-consistent") : null),
          h("td", { class: "nowrap" }, fmtBytes(b.size), b.verified ? h("span", { class: "muted", title: "Archive was read back and its checksum verified" }, " ", icon("check", "sm")) : null),
          h("td", { class: "small" }, b.note ? h("div", {}, b.note) : null, h("div", { class: "muted" }, b.build_tag ? (/^(\d+\.\d+|nightly|canary)/.test(b.build_tag) ? `Pumpkin ${b.build_tag}` : b.build_tag) : "–")),
          h("td", { class: "actions-cell" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
            h("button", { class: "btn sm", disabled: !b.server_exists, title: b.server_exists ? "" : "The server no longer exists", onclick: () => restore(b) }, icon("rollback", "sm"), "Restore"),
            h("a", { class: "btn ghost sm icon-only", href: `/api/backups/${b.id}/download`, title: "Download", "aria-label": "Download backup", download: "" }, icon("download", "sm")),
            h("button", { class: "btn ghost sm icon-only", title: b.protected ? "Remove protection" : "Protect from automatic deletion", "aria-label": "Toggle protection", onclick: () => toggleProtect(b) }, icon("shield", "sm")),
            h("button", { class: "btn ghost sm icon-only", title: "Delete", "aria-label": "Delete backup", onclick: () => remove(b) }, icon("trash", "sm")))));
      })))));
  }

  const uploadInput = h("input", { type: "file", accept: ".gz,.tgz,application/gzip", class: "sr-only", "aria-label": "Choose a backup archive", onchange: async () => {
    const file = uploadInput.files[0];
    uploadInput.value = "";
    if (!file) return;
    toast(`Uploading ${file.name}...`, "info", null, 2500);
    try { await upload(`/api/servers/${serverId}/backups/upload${qs({ name: file.name })}`, file); toast("Backup uploaded and verified", "success"); load(); } catch (err) { notifyError(err); }
  } });

  const actions = serverId ? h("div", { class: "row" },
    h("button", { class: "btn primary sm", onclick: async () => {
      const note = await promptDialog({ title: "Create a backup", label: "Note (optional)", placeholder: "e.g. before the big build", confirmLabel: "Back up now" });
      if (note === null) return;
      const job = await jobDialog(post(`/api/servers/${serverId}/backups`, { note: note || null }), { title: "Creating backup", successToast: "Backup created" });
      if (job) load();
    } }, icon("archive", "sm"), "Back up now"),
    h("button", { class: "btn sm", onclick: () => uploadInput.click() }, icon("upload", "sm"), "Upload"), uploadInput) : null;

  host.append(h("div", { class: "row" }, summary, h("span", { class: "grow" }), actions), h("div", { class: "card tight" }, listHost));
  load();
  const offs = [bus.on("job", ({ job, previous }) => { if (previous?.status === "running" && job.status !== "running" && ["backup", "restore", "update", "rollback"].includes(job.kind)) load(); })];
  return { el: host, dispose: () => offs.forEach((o) => o()), reload: load };
}

function schedulePicker(server, save) {
  const current = server.policy.backup_schedule;
  const matched = SCHEDULES.find((s) => JSON.stringify(s.schedule) === JSON.stringify({ type: current.type, ...(current.type === "daily" ? { at: current.at } : current.type === "interval" ? { hours: current.hours } : {}) }));
  let value = matched ? matched.value : "daily";
  let keep = server.policy.backup_keep;
  let mode = server.policy.backup_mode;
  return h("div", { class: "card stack" },
    h("div", { class: "card-head" }, h("h2", {}, "Schedule & retention")),
    h("div", { class: "form-grid" },
      field("Automatic backups", selectControl(SCHEDULES, value, (v) => { value = v; })),
      field("Keep the newest", h("input", { class: "input", type: "number", min: "1", max: "365", value: String(keep), oninput: (e) => { keep = Number(e.target.value); } }), "Older automatic backups are deleted. Protected backups and the one from your last update are always kept."),
      field("While the server is running", selectControl([
        { value: "hot", label: "Pause saving briefly (no downtime)" },
        { value: "cold", label: "Stop the server for a perfect copy" },
      ], mode, (v) => { mode = v; }), "Backups before an update or restore always use a stopped server.")),
    h("div", {}, h("button", { class: "btn primary sm", onclick: () => save({ backup_schedule: SCHEDULES.find((s) => s.value === value).schedule, backup_keep: keep, backup_mode: mode }) }, icon("save", "sm"), "Save")));
}

export function render({ id, getServer }) {
  const panel = backupsPanel({ serverId: id });
  const scheduleHost = h("div", {});
  const showSchedule = () => {
    clear(scheduleHost).appendChild(schedulePicker(getServer(), async (policy) => {
      try { await patch(`/api/servers/${id}`, { policy }); toast("Backup settings saved", "success"); } catch (err) { notifyError(err); }
    }));
  };
  showSchedule();
  return { el: h("div", { class: "stack" }, panel.el, scheduleHost), dispose: panel.dispose };
}
