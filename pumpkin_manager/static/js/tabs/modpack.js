// Modpack tab (Java servers): what the server runs, which mods it has, how it is run, and updating it.

import { PERM, can } from "../access.js";
import { get, patch, post } from "../api.js";
import { clear, fmtDateTime, fmtRelative, h, icon } from "../dom.js";
import { busy, confirmDialog, field, fmtMemoryMb, jobDialog, notifyError, packIcon, selectControl, toast } from "../ui.js";

const LOADERS = { fabric: "Fabric", forge: "Forge", neoforge: "NeoForge", quilt: "Quilt" };
const STATUS = { success: "success", rolled_back: "warning", failed: "error", interrupted: "warning", running: "info" };
const STATUS_LABEL = { success: "done", rolled_back: "rolled back", failed: "failed", interrupted: "interrupted", running: "running" };
const versionLabel = (v, current) => `${v.number}${v.type !== "release" ? ` (${v.type})` : ""} · Minecraft ${v.minecraft.join(", ")} · ${v.loaders.map((l) => LOADERS[l] || l).join(", ")}${v.id === current ? " · installed" : ""}`;

export function render({ id, getServer }) {
  const host = h("div", { class: "stack" });
  let shown = "";

  // ---- what is installed ----------------------------------------------------------------------------------------
  function modsCard(info) {
    const filter = h("input", { class: "input", placeholder: "Filter the list", "aria-label": "Filter mods", autocomplete: "off" });
    const list = h("div", { class: "mod-list" });
    const draw = () => {
      const needle = filter.value.trim().toLowerCase();
      clear(list);
      const shown = info.mods.filter((m) => !needle || m.toLowerCase().includes(needle));
      shown.forEach((m) => list.appendChild(h("div", { class: "mod mono small truncate", title: m }, m.replace(/^mods\//, ""))));
      if (!shown.length) list.appendChild(h("p", { class: "muted" }, "Nothing matches."));
    };
    filter.addEventListener("input", draw);
    draw();
    return h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Installed files"), h("span", { class: "muted small" }, `${info.mod_count} from the modpack`)),
      info.mod_count > info.mods.length ? h("p", { class: "hint" }, `Showing the first ${info.mods.length}.`) : null,
      h("p", { class: "hint" }, "Files you add yourself in the Files tab are not listed here."), filter, list);
  }

  function packCard(info) {
    const m = info.modpack;
    const source = m.source === "modrinth" && m.slug
      ? h("a", { href: `https://modrinth.com/modpack/${encodeURIComponent(m.slug)}`, target: "_blank", rel: "noopener noreferrer" }, "Modrinth ", icon("external", "sm"))
      : (m.source === "upload" ? "an uploaded .mrpack file" : "unknown");
    return h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Modpack")),
      h("div", { class: "row" }, packIcon(m.icon, 52), h("div", { class: "grow" }, h("strong", { class: "big" }, m.title || "Modpack"),
        h("div", { class: "muted" }, m.version_number ? `Version ${m.version_number}` : ""))),
      h("dl", { class: "kv" },
        h("dt", {}, "Source"), h("dd", {}, source),
        h("dt", {}, "Minecraft"), h("dd", {}, m.minecraft || "–"),
        h("dt", {}, "Mod loader"), h("dd", {}, m.loader?.label || "–"),
        h("dt", {}, "Mods"), h("dd", {}, `${info.mod_count}`),
        h("dt", {}, "Configuration files"), h("dd", {}, `${info.config_files} from the modpack`)));
  }

  function leftOutCard(info) {
    const m = info.modpack;
    if (!m.client_only_count) return null;
    return h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Left out"), h("span", { class: "muted small" }, `${m.client_only_count} ${m.client_only_count === 1 ? "mod" : "mods"}`)),
      h("p", { class: "hint" }, "Modrinth says these mods only work on a client (minimaps, menus, shader helpers, ...). The modpack lists them for the server as well, but they can stop a server from starting, so they were not installed. Updates leave them out too."),
      h("details", {}, h("summary", {}, "Show the list"),
        h("div", { class: "mod-list", style: { marginTop: "10px" } }, m.client_only.map((p) => h("div", { class: "mod mono small truncate", title: p }, p.replace(/^mods\//, ""))))),
      m.client_only_count > m.client_only.length ? h("p", { class: "hint" }, `Showing the first ${m.client_only.length}.`) : null);
  }

  function javaCard(info) {
    if (!info.java.major) return null;
    const s = getServer();
    return h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Java"),
      can(s, PERM.SETTINGS) ? h("a", { class: "small", href: `#/server/${id}/settings` }, "Settings") : null),
      h("dl", { class: "kv" },
        h("dt", {}, "Java version"), h("dd", {}, `Java ${info.java.major}${info.java.runtime ? ` (${info.java.runtime})` : " (downloaded when the server starts)"}`),
        h("dt", {}, "Memory"), h("dd", {}, info.java.memory_mb ? fmtMemoryMb(info.java.memory_mb) : "–"),
        h("dt", {}, "Extra options"), h("dd", {}, info.java.jvm_args ? h("code", {}, info.java.jvm_args) : "none")));
  }

  // ---- updates -------------------------------------------------------------------------------------------------------
  async function updatesCard(info, reload) {
    const s = getServer();
    const m = info.modpack;
    if (m.source !== "modrinth") {
      return h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Updates")),
        h("p", { class: "muted" }, "This server was made from an uploaded modpack file, so there is no place to look for newer versions. To use another version, create a new server from it."));
    }
    let versions = { versions: [], pending: null, current: null };
    let history = [];
    try {
      [versions, history] = await Promise.all([get(`/api/servers/${id}/modpack/versions`), get(`/api/servers/${id}/updates`).then((r) => r.updates)]);
    } catch (err) { notifyError(err); }
    const working = !!s.busy;
    const run = async (url, body, title) => {
      const job = await jobDialog(post(url, body), { title });
      await reload();
      return job;
    };

    const pending = m.pending;
    const banner = pending ? h("div", { class: "stack-sm" },
      h("div", { class: "update-banner" }, icon("arrowUp"),
        h("div", { class: "grow" }, "Update available: ", h("strong", {}, `${m.title} ${pending.number}`),
          h("div", { class: "small muted" }, `Minecraft ${pending.minecraft.join(", ")} · ${pending.loaders.map((l) => LOADERS[l] || l).join(", ")} · published ${fmtRelative(pending.published)}`)),
        h("button", { class: "btn primary sm", disabled: working, onclick: () => update(pending.id, `${m.title} ${pending.number}`, pending.minecraft) }, "Update now")),
      pending.minecraft.length && !pending.minecraft.includes(m.minecraft)
        ? h("div", { class: "notice warning" }, icon("alert", "sm"), `This update changes Minecraft from ${m.minecraft} to ${pending.minecraft.join(", ")}. The world is converted when the server starts and the old version cannot open it again; the backup made first is the way back.`)
        : null)
      : h("p", { class: "muted" }, m.pending === null && versions.versions.length ? "This is the newest version of the modpack." : "");

    async function update(versionId, label, minecraft) {
      const changes = minecraft?.length && !minecraft.includes(m.minecraft);
      const ok = await confirmDialog({
        title: `Update to ${label}?`, confirmLabel: "Back up and update",
        message: h("div", { class: "stack-sm" },
          h("p", {}, "The server is stopped, a verified backup is made, then the mods and configuration are switched and the server starts again. If the new version does not start, the backup is restored automatically."),
          h("p", {}, "Files you changed by hand are kept (a configuration file gets the modpack's new version saved next to it)."),
          changes ? h("p", { class: "field-error" }, `This changes Minecraft from ${m.minecraft} to ${minecraft.join(", ")}.`) : null,
          s.state === "running" ? h("p", { class: "muted" }, "Players who are online are told 30 seconds before the restart.") : null),
      });
      if (ok) await run(`/api/servers/${id}/modpack/update`, { version_id: versionId }, `Updating ${s.name} to ${label}`);
    }

    let chosen = versions.current;
    const picker = versions.versions.length ? h("details", {},
      h("summary", {}, "Install a specific version"),
      h("div", { class: "stack-sm", style: { marginTop: "10px" } },
        field("Version", selectControl(versions.versions.map((v) => ({ value: v.id, label: versionLabel(v, versions.current) })), versions.current, (v) => { chosen = v; })),
        h("div", {}, h("button", { class: "btn sm", disabled: working, onclick: () => {
          const v = versions.versions.find((x) => x.id === chosen);
          if (!v || v.id === versions.current) return toast("That version is already installed.", "info");
          update(v.id, `${m.title} ${v.number}`, v.minecraft);
        } }, "Install this version")))) : null;

    const rollback = m.can_roll_back ? h("div", { class: "stack-sm" },
      h("div", { class: "section-title" }, "Undo the last update"),
      h("p", { class: "muted small" }, "Restores the server from the backup made before the last update: mods, configuration and world. Everything that happened since then is lost."),
      h("div", {}, h("button", { class: "btn sm danger", disabled: working, onclick: async () => {
        const ok = await confirmDialog({ title: "Go back to the previous version?", confirmLabel: "Restore the backup", danger: true,
          message: "The server is stopped and restored from the backup made before the last update. A safety backup of the current state is made first." });
        if (ok) await run(`/api/servers/${id}/modpack/rollback`, {}, `Rolling back ${s.name}`);
      } }, icon("rollback", "sm"), "Roll back"))) : null;

    const table = history.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Change"), h("th", {}, "Result"), h("th", {}, "How"))),
      h("tbody", {}, history.slice(0, 10).map((u) => h("tr", {},
        h("td", { class: "muted small nowrap", title: fmtDateTime(u.started_at) }, fmtRelative(u.started_at)),
        h("td", {}, `${u.from_tag || "?"} → ${u.to_tag || "?"}`),
        h("td", { title: u.message || "" }, h("span", { class: `badge ${STATUS[u.status] || ""}` }, STATUS_LABEL[u.status] || u.status)),
        h("td", { class: "muted small" }, u.trigger)))))) : null;

    const mode = h("div", { class: "row" }, h("span", { class: "muted small" }, "New versions:"), selectControl(
      [{ value: "notify", label: "tell me" }, { value: "off", label: "do not look for them" }], s.policy.update_mode === "off" ? "off" : "notify",
      async (v) => { try { await patch(`/api/servers/${id}`, { policy: { update_mode: v } }); toast("Saved", "success"); } catch (err) { notifyError(err); } }));
    const checkBtn = h("button", { class: "btn sm", onclick: (e) => busy(e.currentTarget, async () => { await post(`/api/servers/${id}/modpack/check`); await reload(); toast("Checked Modrinth", "success"); }) }, icon("restart", "sm"), "Check now");
    return h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Updates"), checkBtn),
      banner, mode, picker, rollback, table ? h("div", {}, h("div", { class: "section-title" }, "History"), table) : null);
  }

  // ---- the page -------------------------------------------------------------------------------------------------------
  async function load() {
    let info;
    try { info = await get(`/api/servers/${id}/modpack`); } catch (err) { return notifyError(err); }
    const updates = await updatesCard(info, load);
    shown = shapeOf(getServer());
    clear(host).append(packCard(info), updates, modsCard(info), ...[leftOutCard(info), javaCard(info)].filter(Boolean));
  }

  const shapeOf = (s) => JSON.stringify([s.modpack?.version_number, s.modpack?.pending?.id, s.modpack?.can_roll_back, s.modpack?.client_only_count, s.busy, s.policy?.update_mode]);
  load();
  // The server view changes when a check finds something or an update ends: redraw then (and only then).
  return { el: host, onServer: (s) => { if (shapeOf(s) !== shown) load(); } };
}
