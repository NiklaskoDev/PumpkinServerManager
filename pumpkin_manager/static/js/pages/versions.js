// Versions: what GitHub offers per channel and which Pumpkin binaries are cached on disk.

import { del, get, post } from "../api.js";
import { clear, fmtBytes, fmtDateTime, fmtRelative, h, icon } from "../dom.js";
import { bus, state } from "../store.js";
import { busy, channelChip, confirmDialog, jobDialog, notifyError, toast } from "../ui.js";

const CHANNEL_INFO = {
  stable: ["Stable", "Tagged releases. Recommended for real worlds."],
  nightly: ["Nightly", "Rolling build of the main branch, replaced regularly."],
  canary: ["Canary", "The newest rolling build. Expect surprises."],
};

const serverName = (id) => state.servers.get(id)?.name || id;
const serverChip = (id, text, title) => h("a", { href: `#/server/${id}/version`, class: "chip", style: { marginRight: "4px" }, title }, text);

// Servers that run a build or can roll back to it, then servers that are only waiting to update to it.
function usedBy(build) {
  if (!build.used_by.length && !build.wanted_by.length) return h("span", { class: "muted" }, "nobody");
  return [
    ...build.used_by.map((id) => serverChip(id, serverName(id))),
    ...build.wanted_by.map((id) => serverChip(id, `${serverName(id)} · update waiting`, "This server is waiting to update to this build")),
  ];
}

export async function render() {
  let data = await get("/api/versions");
  const channelsHost = h("div", { class: "grid cols-3" });
  const buildsHost = h("div", {});
  const statusHost = h("div", { class: "muted small" });

  async function cleanUp(button) {
    await busy(button, async () => {
      const { removed, freed_bytes: freed } = await post("/api/versions/prune");
      toast(removed.length ? `Removed ${removed.length} unused build${removed.length === 1 ? "" : "s"} and freed ${fmtBytes(freed)}` : "Nothing to clean up: every downloaded build is needed by a server", "success");
      await reload();
    });
  }

  function draw() {
    clear(statusHost).textContent = data.last_check ? `Last checked ${fmtRelative(data.last_check)} · ${state.settings?.update_check_enabled ? `every ${state.settings.update_check_minutes} min` : "automatic checks are off"}` : "Not checked yet";

    clear(channelsHost);
    for (const ch of ["stable", "nightly", "canary"]) {
      const latest = data.channels[ch];
      const error = data.errors[ch];
      const users = [...state.servers.values()].filter((s) => s.channel === ch);
      channelsHost.appendChild(h("div", { class: "card stack-sm" },
        h("div", { class: "card-head" }, h("h2", {}, CHANNEL_INFO[ch][0]), users.length ? h("span", { class: "chip" }, `${users.length} server${users.length === 1 ? "" : "s"}`) : null),
        h("p", { class: "muted small" }, CHANNEL_INFO[ch][1]),
        latest ? h("dl", { class: "kv small" },
          h("dt", {}, "Latest"), h("dd", {}, h("strong", {}, latest.label)),
          h("dt", {}, "Published"), h("dd", {}, fmtDateTime(latest.updated_at || latest.published_at)),
          h("dt", {}, "Size"), h("dd", {}, fmtBytes(latest.size)),
          h("dt", {}, "Cached"), h("dd", {}, latest.downloaded ? h("span", { class: "badge success" }, "downloaded") : "not yet"))
          : h("p", { class: "muted" }, error ? "" : "Not checked yet."),
        error ? h("div", { class: "notice danger" }, icon("alert", "sm"), error) : null,
        latest?.release_url ? h("a", { class: "small", href: latest.release_url, target: "_blank", rel: "noopener noreferrer" }, "Release notes ", icon("external", "sm")) : null,
        latest && !latest.downloaded ? h("div", {}, h("button", { class: "btn sm", onclick: () => jobDialog(post("/api/versions/download", { channel: ch }), { title: `Downloading ${CHANNEL_INFO[ch][0]}`, successToast: "Downloaded" }).then(reload) }, icon("download", "sm"), "Download now")) : null));
    }

    clear(buildsHost);
    const cached = data.builds.filter((b) => b.available || b.used_by.length);
    buildsHost.appendChild(h("div", { class: "card tight" },
      h("div", { class: "card-section" }, h("div", { class: "row" }, h("h2", { class: "grow" }, "Downloaded builds"), h("span", { class: "muted small" }, `${fmtBytes(data.disk_bytes)} on disk`),
        h("button", { class: "btn sm", title: "Delete every downloaded build that no server runs, can roll back to or is about to update to", onclick: (e) => cleanUp(e.currentTarget) }, icon("trash", "sm"), "Clean up unused"))),
      cached.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", {}, "Version"), h("th", {}, "Channel"), h("th", {}, "Size"), h("th", {}, "Used by"), h("th", {}, ""))),
        h("tbody", {}, cached.map((b) => h("tr", {},
          h("td", {}, h("strong", {}, b.label), h("div", { class: "muted small mono" }, b.digest.slice(0, 16))),
          h("td", {}, channelChip(b.channel)), h("td", { class: "nowrap" }, fmtBytes(b.size)),
          h("td", {}, usedBy(b)),
          h("td", { class: "actions-cell" }, h("button", { class: "btn ghost sm icon-only", disabled: b.used_by.length > 0 || b.wanted_by.length > 0 || !b.available, title: b.used_by.length ? "In use (current or rollback version)" : b.wanted_by.length ? "A server is waiting to update to this build" : "Delete from disk", "aria-label": "Delete build", onclick: async () => {
            if (!(await confirmDialog({ title: "Delete this build?", message: "It will be downloaded again if a server needs it. Rolling-channel builds cannot be re-downloaded once GitHub replaced them.", confirmLabel: "Delete", danger: true }))) return;
            try { await del(`/api/versions/${b.id}`); await reload(); } catch (err) { notifyError(err); }
          } }, icon("trash", "sm")))))))) : h("p", { class: "muted", style: { padding: "0 18px 18px" } }, "No builds downloaded yet. They are fetched automatically when a server is created.")));
  }

  // ---- Java runtimes: what modpack servers run on ---------------------------------------------------------------
  const javaHost = h("div", {});
  async function drawJava() {
    let info;
    try { info = await get("/api/java/runtimes"); } catch (err) { return clear(javaHost).appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"), err.message)); }
    const missing = info.available.filter((major) => !info.installed.some((j) => j.major === major));
    clear(javaHost).appendChild(h("div", { class: "card tight" },
      h("div", { class: "card-section" }, h("div", { class: "row" }, h("h2", { class: "grow" }, "Java runtimes"), h("span", { class: "muted small" }, `${fmtBytes(info.disk_bytes)} on disk`)),
        h("p", { class: "muted small" }, "Modpack servers run on Java. The version a server needs is downloaded when it first starts; versions no server uses can be removed here.")),
      info.installed.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", {}, "Java"), h("th", {}, "Build"), h("th", {}, "Size"), h("th", {}, "Used by"), h("th", {}, ""))),
        h("tbody", {}, info.installed.map((j) => h("tr", {},
          h("td", {}, h("strong", {}, `Java ${j.major}`)), h("td", { class: "mono small" }, j.release || "–"), h("td", { class: "nowrap" }, fmtBytes(j.size)),
          h("td", {}, j.used_by.length ? j.used_by.map((sid) => h("a", { href: `#/server/${sid}`, class: "chip", style: { marginRight: "4px" } }, state.servers.get(sid)?.name || sid)) : h("span", { class: "muted" }, "nobody")),
          h("td", { class: "actions-cell" }, h("button", { class: "btn ghost sm icon-only", disabled: j.used_by.length > 0, title: j.used_by.length ? "A server uses this version" : "Remove from disk", "aria-label": `Remove Java ${j.major}`, onclick: async () => {
            if (!(await confirmDialog({ title: `Remove Java ${j.major}?`, message: "It is downloaded again when a server needs it.", confirmLabel: "Remove", danger: true }))) return;
            try { await del(`/api/java/runtimes/${j.major}`); await drawJava(); } catch (err) { notifyError(err); }
          } }, icon("trash", "sm")))))))) : h("p", { class: "muted", style: { padding: "0 18px 14px" } }, "No Java downloaded yet."),
      missing.length ? h("div", { class: "card-section row" }, h("span", { class: "muted small" }, "Download in advance:"),
        ...missing.map((major) => h("button", { class: "btn sm", onclick: () => jobDialog(post(`/api/java/runtimes/${major}`), { title: `Downloading Java ${major}`, successToast: "Downloaded" }).then(drawJava) }, icon("download", "sm"), `Java ${major}`))) : null,
      // the kits that compile Spigot: only a build uses one, so they can go whenever no build runs
      info.jdks?.length ? h("div", {},
        h("div", { class: "card-section" }, h("div", { class: "row" }, h("h3", { class: "grow" }, "Java development kits"), h("span", { class: "muted small" }, `${fmtBytes(info.jdk_disk_bytes)} on disk`)),
          h("p", { class: "muted small" }, "Spigot is made with these: a development kit has the compiler that a runtime lacks. One is only used while a Spigot build runs, and it is downloaded again when the next build needs it.")),
        h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", {}, h("tr", {}, h("th", {}, "Java"), h("th", {}, "Build"), h("th", {}, "Size"), h("th", {}, ""))),
          h("tbody", {}, info.jdks.map((j) => h("tr", {},
            h("td", {}, h("strong", {}, `Java ${j.major}`)), h("td", { class: "mono small" }, j.release || "–"), h("td", { class: "nowrap" }, fmtBytes(j.size)),
            h("td", { class: "actions-cell" }, h("button", { class: "btn ghost sm icon-only", title: "Remove from disk", "aria-label": `Remove the Java ${j.major} development kit`, onclick: async () => {
              if (!(await confirmDialog({ title: `Remove the Java ${j.major} development kit?`, message: "It is downloaded again (about 200 MB) when the next Spigot build needs it.", confirmLabel: "Remove", danger: true }))) return;
              try { await del(`/api/java/jdks/${j.major}`); await drawJava(); } catch (err) { notifyError(err); }
            } }, icon("trash", "sm"))))))))) : null,
      info.mods_cache_bytes ? h("div", { class: "card-section muted small" }, `Mods that were downloaded for modpacks stay in a shared cache (${fmtBytes(info.mods_cache_bytes)}), so a mod that several servers or versions use is fetched only once.`) : null));
  }

  async function reload() {
    data = await get("/api/versions");
    draw();
    drawJava();
  }

  const checkBtn = h("button", { class: "btn primary", onclick: async (e) => {
    await busy(e.currentTarget, async () => {
      data = await post("/api/versions/check");
      draw();
      const failed = Object.keys(data.errors || {});
      toast(failed.length ? `Could not check: ${failed.join(", ")}` : "Checked GitHub for new builds", failed.length ? "warning" : "success");
    });
  } }, icon("restart", "sm"), "Check for updates");

  const offs = [bus.on("servers", draw), bus.on("job", ({ job, previous }) => { if (previous?.status === "running" && job.status !== "running" && ["update", "download", "create"].includes(job.kind)) reload(); })];
  draw();
  drawJava();
  return {
    el: h("div", { class: "stack" },
      h("div", { class: "page-head" }, h("div", { class: "titles" }, h("h1", {}, "Versions"), h("p", {}, "Pumpkin builds from GitHub. Servers follow a channel and update automatically with a backup first."), statusHost),
        h("div", { class: "actions" }, checkBtn)),
      channelsHost, buildsHost, javaHost),
    dispose: () => offs.forEach((o) => o()),
  };
}
