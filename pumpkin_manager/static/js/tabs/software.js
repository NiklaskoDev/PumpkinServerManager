// Version tab of a Vanilla, Paper or Fabric server: what is installed, updates, switching to another version, undoing an update.

import { PERM, can } from "../access.js";
import { get, patch, post } from "../api.js";
import { clear, fmtDate, fmtDateTime, fmtRelative, h, icon } from "../dom.js";
import { BUILD_WORD, EXTRAS, UPDATE_MODES, buildChoices, supportNotices, versionOption } from "../software.js";
import { busy, confirmDialog, field, jobDialog, notifyError, selectControl, switchControl, toast } from "../ui.js";

const STATUS = { success: "success", rolled_back: "warning", failed: "error", interrupted: "warning", running: "info" };
const STATUS_LABEL = { success: "done", rolled_back: "rolled back", failed: "failed", interrupted: "interrupted", running: "running" };

export function render({ id, getServer }) {
  const host = h("div", { class: "stack" });
  let shown = "";

  const run = async (url, body, title) => {
    const job = await jobDialog(post(url, body), { title });
    await load();
    return job;
  };

  // ---- what is installed ---------------------------------------------------------------------------------------------
  function installedCard(info) {
    const sw = info.software;
    const word = BUILD_WORD[sw.kind];
    const extras = EXTRAS[sw.kind];
    const maintained = sw.support?.status === "UNSUPPORTED"
      ? h("span", { class: "badge warning", title: "PaperMC no longer builds this Minecraft version" }, `no longer maintained${sw.support.end ? ` since ${fmtDate(sw.support.end)}` : ""}`)
      : sw.support?.status ? h("span", { class: "badge success" }, "maintained") : null;
    return h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Installed")),
      h("div", { class: "row" }, icon("layers"), h("strong", { class: "big grow" }, sw.label)),
      h("dl", { class: "kv" },
        h("dt", {}, "Software"), h("dd", {}, sw.name, maintained ? [" ", maintained] : null),
        h("dt", {}, "Minecraft"), h("dd", {}, sw.minecraft || "–"),
        sw.build ? [h("dt", {}, word === "build" ? "Paper build" : "Fabric loader"), h("dd", {}, `${sw.build}${sw.channel && sw.channel !== "stable" ? ` (${sw.channel})` : ""}`)] : null,
        sw.fabric_api ? [h("dt", {}, "Fabric API"), h("dd", {}, sw.fabric_api.number)] : null,
        h("dt", {}, "Java"), h("dd", {}, `Java ${info.java.major}${info.java.runtime ? ` (${info.java.runtime})` : " (downloaded when the server starts)"}`),
        sw.installed_at ? [h("dt", {}, "Installed"), h("dd", {}, fmtDateTime(sw.installed_at))] : null),
      extras ? h("p", { class: "hint" }, `Put ${extras.word} into the ${extras.folder} folder`,
        can(getServer(), PERM.FILES) ? [": upload them in the ", h("a", { href: `#/server/${id}/files` }, "Files tab"), "."] : ".") : null);
  }

  // ---- updates -------------------------------------------------------------------------------------------------------
  function updatesCard(info, history, change) {
    const s = getServer();
    const sw = info.software;
    const working = !!s.busy;
    const word = BUILD_WORD[sw.kind];
    const { pending, upgrade } = sw;

    async function applyUpdate(request, label, minecraft) {
      const converts = minecraft && minecraft !== sw.minecraft;
      const ok = await confirmDialog({
        title: `Update to ${label}?`, confirmLabel: "Back up and update",
        message: h("div", { class: "stack-sm" },
          h("p", {}, "The server is stopped, a verified backup is made, then the new version is installed and the server starts again. If it does not start, the backup is restored automatically."),
          converts ? h("p", { class: "field-error" }, `This changes Minecraft from ${sw.minecraft} to ${minecraft}. The world is converted when the server starts and the old version cannot open it again; the backup made first is the way back.${sw.kind === "vanilla" ? "" : " Plugins and mods have to support the new version as well."}`) : null,
          s.state === "running" ? h("p", { class: "muted" }, "Players who are online are told 30 seconds before the restart.") : null),
      });
      if (ok) await run(`/api/servers/${id}/software/update`, request, `Updating ${s.name} to ${label}`);
    }

    const banner = pending
      ? h("div", { class: "update-banner" }, icon("arrowUp"),
        h("div", { class: "grow" }, "Update available: ", h("strong", {}, pending.label),
          h("div", { class: "small muted" }, pending.kind === "minecraft"
            ? `A new Minecraft release${pending.published ? `, published ${fmtRelative(pending.published)}` : ""}. It converts the world, so it is only installed when you say so.`
            : `A newer ${word} for Minecraft ${sw.minecraft}${pending.published ? ` · published ${fmtRelative(pending.published)}` : ""}`)),
        h("button", { class: "btn primary sm", disabled: working, onclick: () => applyUpdate({}, pending.label, pending.minecraft) }, "Update now"))
      : h("p", { class: "muted" }, sw.kind === "vanilla" ? "This is the newest Minecraft release." : `This is the newest stable ${word} for Minecraft ${sw.minecraft}.`);

    const news = upgrade
      ? h("div", { class: "notice" }, icon("info", "sm"), h("span", {}, `Minecraft ${upgrade.minecraft} is out${upgrade.published ? ` (${fmtRelative(upgrade.published)})` : ""}. Plugins or mods must support it before you switch, and the world is converted. `,
        h("button", { type: "button", class: "btn sm", onclick: () => change.choose(upgrade.minecraft) }, "Choose it")))
      : null;

    // how the manager deals with news
    let mode = UPDATE_MODES[sw.kind].some((m) => m.value === s.policy.update_mode) ? s.policy.update_mode : "notify";
    let onlyEmpty = s.policy.update_only_when_empty;
    let defer = s.policy.update_max_defer_hours;
    const waitHost = h("div", { class: "stack-sm" });
    const drawWait = () => {
      clear(waitHost);
      if (mode !== "auto") return;
      waitHost.append(
        switchControl({ label: "Wait until nobody is online", hint: "Automatic updates are postponed while players are connected.", checked: onlyEmpty, onChange: (v) => { onlyEmpty = v; } }),
        field("…but not longer than (hours)", h("input", { class: "input", type: "number", min: "0", max: "720", value: String(defer), oninput: (e) => { defer = Number(e.target.value); } }),
          "After this the players get a warning and the update goes ahead. 0 updates immediately."));
    };
    drawWait();
    const policy = h("div", { class: "stack-sm" },
      field("New versions", selectControl(UPDATE_MODES[sw.kind], mode, (v) => { mode = v; drawWait(); }),
        sw.kind === "vanilla" ? "A new Minecraft version changes the world, so it is never installed without you." : "A new Minecraft version is never installed by itself: it can break plugins or mods."),
      waitHost,
      h("div", {}, h("button", { class: "btn sm", onclick: async () => {
        try {
          await patch(`/api/servers/${id}`, { policy: { update_mode: mode, update_only_when_empty: onlyEmpty, update_max_defer_hours: defer } });
          toast("Saved", "success");
        } catch (err) { notifyError(err); }
      } }, icon("save", "sm"), "Save")));

    const checkBtn = h("button", { class: "btn sm", onclick: (e) => busy(e.currentTarget, async () => {
      await post(`/api/servers/${id}/software/check`);
      await load();
      toast("Checked for newer versions", "success");
    }) }, icon("restart", "sm"), "Check now");

    const rollback = sw.can_roll_back ? h("div", { class: "stack-sm" },
      h("div", { class: "section-title" }, "Undo the last update"),
      h("p", { class: "muted small" }, "Restores the server from the backup made before the last update: the software, the world and everything in the server folder. Everything that happened since then is lost."),
      h("div", {}, h("button", { class: "btn sm danger", disabled: working, onclick: async () => {
        const ok = await confirmDialog({ title: "Go back to the previous version?", confirmLabel: "Restore the backup", danger: true,
          message: "The server is stopped and restored from the backup made before the last update. A safety backup of the current state is made first." });
        if (ok) await run(`/api/servers/${id}/software/rollback`, {}, `Rolling back ${s.name}`);
      } }, icon("rollback", "sm"), "Roll back"))) : null;

    const table = history.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Change"), h("th", {}, "Result"), h("th", {}, "How"))),
      h("tbody", {}, history.slice(0, 10).map((u) => h("tr", {},
        h("td", { class: "muted small nowrap", title: fmtDateTime(u.started_at) }, fmtRelative(u.started_at)),
        h("td", {}, `${u.from_tag || "?"} → ${u.to_tag || "?"}`),
        h("td", { title: u.message || "" }, h("span", { class: `badge ${STATUS[u.status] || ""}` }, STATUS_LABEL[u.status] || u.status)),
        h("td", { class: "muted small" }, u.trigger)))))) : null;

    return { applyUpdate, el: h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Updates"), checkBtn),
      banner, news, policy, rollback, table ? h("div", {}, h("div", { class: "section-title" }, "History"), table) : null) };
  }

  // ---- switching to another version -----------------------------------------------------------------------------------
  function changeCard(info, applyUpdate) {
    const s = getServer();
    const sw = info.software;
    const word = BUILD_WORD[sw.kind];
    const f = { minecraft: sw.minecraft, build: "", snapshots: false, experimental: false };
    let versions = null;
    let ticket = 0;
    const versionHost = h("div", {});
    const snapshotHost = h("div", {});
    const buildHost = h("div", {});
    const noticeHost = h("div", { class: "stack-sm" });
    const actionHost = h("div", {});

    async function loadVersions() {
      const mine = ++ticket;
      clear(versionHost).appendChild(h("p", { class: "muted" }, "Looking up the versions…"));
      try { versions = await get(`/api/servers/${id}/software/versions${f.snapshots ? "?snapshots=true" : ""}`); } catch (err) { return void clear(versionHost).appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"), err.message)); }
      if (mine !== ticket) return;
      if (!versions.versions.some((v) => v.id === f.minecraft)) f.minecraft = versions.current;
      drawVersions();
      await loadBuilds();
    }

    function drawVersions() {
      const options = versions.versions.map((v) => ({ value: v.id, label: `${versionOption(v)}${v.id === sw.minecraft ? " · installed" : ""}` }));
      if (!options.some((o) => o.value === sw.minecraft)) options.unshift({ value: sw.minecraft, label: `${sw.minecraft} · installed` });
      clear(versionHost).appendChild(field("Minecraft version", selectControl(options, f.minecraft, (v) => { f.minecraft = v; f.build = ""; loadBuilds(); })));
      clear(snapshotHost);
      if (sw.kind !== "paper") {
        snapshotHost.appendChild(switchControl({ label: "Show snapshots", hint: "Test versions of the next Minecraft release.", checked: f.snapshots, onChange: (on) => { f.snapshots = on; loadVersions(); } }));
      }
    }

    // is the chosen Minecraft version older than the installed one? (the list is newest first)
    const isOlder = () => {
      if (!versions || f.minecraft === sw.minecraft) return false;
      const chosen = versions.versions.findIndex((v) => v.id === f.minecraft);
      const current = versions.versions.findIndex((v) => v.id === sw.minecraft);
      return chosen >= 0 && current >= 0 && chosen > current;
    };

    async function loadBuilds() {
      const mine = ++ticket;
      clear(buildHost);
      clear(noticeHost);
      clear(actionHost);
      let options = null;
      if (sw.kind !== "vanilla" || f.minecraft !== sw.minecraft) {
        try { options = await get(`/api/servers/${id}/software/builds?minecraft=${encodeURIComponent(f.minecraft)}${f.experimental ? "&experimental=true" : ""}`); } catch (err) {
          if (mine === ticket) noticeHost.appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"), err.message));
          return;
        }
      }
      if (mine !== ticket) return;
      if (options) supportNotices(sw.kind, options).forEach((n) => noticeHost.appendChild(n));
      if (isOlder()) {
        noticeHost.appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"),
          `Minecraft ${f.minecraft} is older than the ${sw.minecraft} this server runs. A world that was opened by a newer version usually cannot be opened by an older one. The backup made first is the only way back.`));
      }
      if (options && sw.kind !== "vanilla") {
        const list = buildChoices(sw.kind, options).map((c) => ({ ...c, label: c.value === sw.build && f.minecraft === sw.minecraft ? `${c.label} · installed` : c.label }));
        buildHost.appendChild(field(sw.kind === "paper" ? "Paper build" : "Fabric loader version", selectControl(list, f.build, (v) => { f.build = v; })));
        if (options.has_experimental) {
          buildHost.appendChild(switchControl({ label: "Include experimental builds", hint: "Alpha and beta builds are not meant for servers that matter.", checked: f.experimental,
            onChange: (on) => { f.experimental = on; f.build = ""; loadBuilds(); } }));
        }
      }
      actionHost.appendChild(h("button", { class: "btn sm primary", disabled: !!s.busy, onclick: () => {
        const sameVersion = f.minecraft === sw.minecraft && (sw.kind === "vanilla" || f.build === sw.build || (f.build === "" && options?.default === sw.build));
        if (sameVersion) return toast("That version is already installed.", "info");
        const request = { minecraft: f.minecraft };
        if (f.build) request.build = f.build;
        if (isOlder()) request.confirm_downgrade = true;
        const label = `${sw.name} ${f.minecraft}${f.build ? ` (${word} ${f.build})` : ""}`;
        applyUpdate(request, label, f.minecraft);
      } }, "Install this version"));
    }

    const details = h("details", {},
      h("summary", {}, "Switch to another version"),
      h("div", { class: "stack-sm", style: { marginTop: "10px" } },
        h("p", { class: "hint" }, "A verified backup is made first, and the server is restored if the new version does not start."),
        versionHost, snapshotHost, buildHost, noticeHost, actionHost));
    details.addEventListener("toggle", () => { if (details.open && !versions) loadVersions(); });
    return {
      el: h("div", { class: "card stack" }, h("div", { class: "card-head" }, h("h2", {}, "Change version")), details),
      choose(minecraft) {
        f.minecraft = minecraft;
        f.build = "";
        details.open = true;
        if (versions) { if (!versions.versions.some((v) => v.id === minecraft)) f.snapshots = true; loadVersions(); } else loadVersions();
        details.scrollIntoView({ block: "center", behavior: "smooth" });
      },
    };
  }

  // ---- the page -------------------------------------------------------------------------------------------------------
  async function load() {
    let info;
    let history = [];
    try {
      [info, history] = await Promise.all([get(`/api/servers/${id}/software`), get(`/api/servers/${id}/updates`).then((r) => r.updates)]);
    } catch (err) { return notifyError(err); }
    const holder = {};
    const change = changeCard(info, (request, label, minecraft) => holder.updates.applyUpdate(request, label, minecraft));
    holder.updates = updatesCard(info, history, change);
    shown = shapeOf(getServer());
    clear(host).append(installedCard(info), holder.updates.el, change.el);
  }

  const shapeOf = (s) => JSON.stringify([s.software?.build, s.software?.minecraft, s.software?.pending?.label, s.software?.upgrade?.minecraft, s.software?.can_roll_back, s.software?.fabric_api?.number, s.busy, s.policy?.update_mode]);
  load();
  // The server view changes when a check finds something or an update ends: redraw then (and only then).
  return { el: host, onServer: (s) => { if (shapeOf(s) !== shown) load(); } };
}
