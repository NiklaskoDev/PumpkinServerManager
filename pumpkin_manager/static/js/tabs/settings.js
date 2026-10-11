// Server settings: game settings (from the server-side schema), automation, network, raw pumpkin.toml, delete.

import { del, get, patch, post, put } from "../api.js";
import { clear, h, icon } from "../dom.js";
import { confirmDialog, field, jobDialog, notifyError, quickJob, selectControl, switchControl, toast } from "../ui.js";

export function render({ id, getServer }) {
  const host = h("div", { class: "stack" });
  const restartHost = h("div", {});

  function restartNotice() {
    clear(restartHost);
    if (getServer().state === "running") {
      restartHost.appendChild(h("div", { class: "notice warning" }, icon("alert", "sm"),
        h("div", { class: "grow" }, "Restart the server for these changes to take effect."),
        h("button", { class: "btn sm primary", onclick: () => quickJob(post(`/api/servers/${id}/restart`), { success: "Restarted", failure: "Restart failed" }) }, "Restart now")));
    } else {
      toast("Saved. The changes apply the next time the server starts.", "success");
    }
  }

  // ---- game settings ---------------------------------------------------------------------------------
  async function buildGameSettings() {
    const cfg = await get(`/api/servers/${id}/config`);
    const values = { ...cfg.values };
    const changed = {};
    const errors = new Map();
    let showAdvanced = false;
    const body = h("div", { class: "stack" });
    const saveBar = h("div", { class: "savebar hidden" });

    function mark() {
      saveBar.classList.toggle("hidden", Object.keys(changed).length === 0);
      saveBar.querySelector(".count").textContent = `${Object.keys(changed).length} unsaved change${Object.keys(changed).length === 1 ? "" : "s"}`;
    }
    function control(f) {
      const set = (v) => { values[f.key] = v; if (JSON.stringify(v) === JSON.stringify(cfg.values[f.key])) delete changed[f.key]; else changed[f.key] = v; mark(); };
      const err = h("span", { class: "field-error hidden" });
      errors.set(f.key, err);
      let input;
      if (f.type === "bool") {
        input = switchControl({ label: f.label, hint: f.help, checked: !!values[f.key], onChange: set });
        return h("div", {}, input, err);
      }
      if (f.type === "enum") input = selectControl(f.options.map((o) => ({ value: o, label: o })), values[f.key], set);
      else if (f.type === "int" || f.type === "float") {
        input = h("input", { class: "input", type: "number", min: f.min ?? undefined, max: f.max ?? undefined, step: f.type === "float" ? "any" : "1", value: String(values[f.key]),
          oninput: (e) => { const n = e.target.value === "" ? NaN : Number(e.target.value); if (!Number.isNaN(n)) set(n); } });
      } else {
        input = h("input", { class: "input", maxlength: String(f.maxlen || 200), value: values[f.key] ?? "", placeholder: f.key === "seed" ? "random" : "", oninput: (e) => set(e.target.value) });
      }
      return h("label", { class: "field" }, h("span", {}, f.label), input, f.help ? h("span", { class: "hint" }, f.help) : null, err);
    }

    function draw() {
      clear(body);
      const groups = new Map();
      for (const f of cfg.schema) {
        if (f.advanced && !showAdvanced) continue;
        if (!groups.has(f.group)) groups.set(f.group, []);
        groups.get(f.group).push(f);
      }
      for (const [group, fields] of groups) {
        body.appendChild(h("div", { class: "section-title" }, group));
        body.appendChild(h("div", { class: "form-grid" }, fields.map((f) => h("div", { class: f.type === "string" && f.key === "motd" ? "span-2" : "" }, control(f)))));
      }
      body.appendChild(h("div", {}, h("button", { class: "btn ghost sm", onclick: () => { showAdvanced = !showAdvanced; draw(); } }, icon(showAdvanced ? "chevronDown" : "chevronRight", "sm"), showAdvanced ? "Hide advanced settings" : "Show advanced settings")));
    }

    saveBar.append(icon("info", "sm"), h("span", { class: "count grow" }, ""),
      h("button", { class: "btn", onclick: () => { for (const k of Object.keys(changed)) delete changed[k]; Object.assign(values, cfg.values); draw(); mark(); } }, "Discard"),
      h("button", { class: "btn primary", onclick: async () => {
        errors.forEach((e) => e.classList.add("hidden"));
        try {
          const result = await put(`/api/servers/${id}/config`, { values: changed });
          Object.assign(cfg.values, changed);
          for (const k of Object.keys(changed)) delete changed[k];
          mark();
          if (result.restart_required) restartNotice(); else toast("Settings saved", "success");
        } catch (err) {
          notifyError(err);
          for (const [key, message] of Object.entries(err.data?.errors || {})) {
            const el = errors.get(key);
            if (el) { el.textContent = message; el.classList.remove("hidden"); }
          }
        }
      } }, icon("save", "sm"), "Save changes"));
    draw();
    return h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Game settings")),
      cfg.error ? h("div", { class: "notice danger" }, icon("alert", "sm"), cfg.error) : null, body, saveBar);
  }

  // ---- automation & network (manager-level policy) -------------------------------------------------------
  function buildAutomation() {
    const s = getServer();
    const p = s.policy;
    const draft = { autostart: p.autostart, restart_on_crash: p.restart_on_crash, stop_timeout: p.stop_timeout, start_timeout: p.start_timeout };
    let name = s.name;
    let bedrock = !!s.ports.bedrock;
    let port = s.ports.java;
    return h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Server & network")),
      h("div", { class: "form-grid" },
        field("Display name", h("input", { class: "input", maxlength: "40", value: name, oninput: (e) => { name = e.target.value; } })),
        field("When the container starts", selectControl([
          { value: "last", label: "Restore the previous state" }, { value: "always", label: "Always start this server" }, { value: "never", label: "Never start it automatically" },
        ], draft.autostart, (v) => { draft.autostart = v; })),
        field("Game port (TCP)", h("input", { class: "input", type: "number", min: "1024", max: "65535", value: String(port), oninput: (e) => { port = Number(e.target.value); } }),
          s.ports.java_published ? "Inside the range published by the container." : "Outside the published range: add this port to the container's port mappings."),
        h("div", {}, switchControl({ label: "Allow Bedrock players", hint: s.ports.bedrock ? `UDP port ${s.ports.bedrock}` : "Opens an extra UDP port (experimental).", checked: bedrock, onChange: (v) => { bedrock = v; } })),
        field("Graceful stop timeout (seconds)", h("input", { class: "input", type: "number", min: "5", max: "600", value: String(draft.stop_timeout), oninput: (e) => { draft.stop_timeout = Number(e.target.value); } }), "After this the process is terminated."),
        field("Startup timeout (seconds)", h("input", { class: "input", type: "number", min: "15", max: "1800", value: String(draft.start_timeout), oninput: (e) => { draft.start_timeout = Number(e.target.value); } }))),
      switchControl({ label: "Restart automatically after a crash", hint: "Up to three attempts in ten minutes, with growing delays.", checked: draft.restart_on_crash, onChange: (v) => { draft.restart_on_crash = v; } }),
      h("div", {}, h("button", { class: "btn primary sm", onclick: async () => {
        try {
          const body = { name, policy: draft, bedrock };
          if (port !== s.ports.java) body.java_port = port;
          const result = await patch(`/api/servers/${id}`, body);
          if (result.restart_required) restartNotice(); else toast("Saved", "success");
        } catch (err) { notifyError(err); }
      } }, icon("save", "sm"), "Save")));
  }

  // ---- raw config ----------------------------------------------------------------------------------------
  function buildRaw() {
    const area = h("textarea", { class: "input", rows: "18", spellcheck: "false", "aria-label": "pumpkin.toml" });
    const info = h("div", {});
    const details = h("details", { ontoggle: async (e) => {
      if (!e.target.open || area.dataset.loaded) return;
      try {
        const raw = await get(`/api/servers/${id}/config/raw`);
        area.value = raw.content;
        area.dataset.loaded = "1";
        info.appendChild(h("p", { class: "hint" }, "Managed by the dashboard and reset on every start: ", h("code", {}, raw.managed_keys.join(", "))));
      } catch (err) { notifyError(err); }
    } },
      h("summary", {}, "Advanced: edit pumpkin.toml directly"),
      h("div", { class: "stack-sm", style: { marginTop: "12px" } },
        h("div", { class: "notice warning" }, icon("alert", "sm"), "If Pumpkin cannot read this file it silently falls back to default settings. The dashboard checks the syntax before saving, but not every value."),
        info, area,
        h("div", {}, h("button", { class: "btn primary sm", onclick: async () => {
          try {
            const result = await put(`/api/servers/${id}/config/raw`, { content: area.value });
            if (result.reset_managed_keys?.length) toast(`Reset keys owned by the manager: ${result.reset_managed_keys.join(", ")}`, "warning");
            if (result.restart_required) restartNotice(); else toast("pumpkin.toml saved", "success");
          } catch (err) {
            notifyError(err);
            if (err.data?.errors) toast(Object.values(err.data.errors).join("\n"), "error");
          }
        } }, icon("save", "sm"), "Save pumpkin.toml"))));
    return h("div", { class: "card" }, details);
  }

  // ---- danger zone ------------------------------------------------------------------------------------------
  function buildDanger() {
    let finalBackup = true;
    let deleteBackups = false;
    return h("div", { class: "card stack", style: { borderColor: "var(--danger)" } }, h("h2", {}, "Danger zone"),
      h("p", { class: "muted" }, "Deleting a server removes its world and settings from disk."),
      h("div", {}, h("button", { class: "btn danger", onclick: async () => {
        const name = getServer().name;
        const ok = await confirmDialog({ title: `Delete "${name}"?`, confirmLabel: "Delete server", danger: true, confirmText: name,
          message: "The server is stopped and its folder is deleted.",
          extra: h("div", { class: "stack-sm" },
            switchControl({ label: "Keep a final backup", hint: "Stored with your other backups so you can restore it later.", checked: true, onChange: (v) => { finalBackup = v; } }),
            switchControl({ label: "Also delete all of its existing backups", onChange: (v) => { deleteBackups = v; } })) });
        if (!ok) return;
        const job = await jobDialog(del(`/api/servers/${id}?final_backup=${finalBackup}&delete_backups=${deleteBackups}`), { title: `Deleting ${name}` });
        if (job && job.status === "success") location.hash = "#/";
      } }, icon("trash", "sm"), "Delete this server")));
  }

  buildGameSettings().then((card) => {
    host.append(restartHost, card, buildAutomation(), buildRaw(), buildDanger());
  }).catch(notifyError);
  return { el: host };
}
