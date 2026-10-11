import { PERM, can, canAnywhere, isAdministrator } from "../access.js";
import { post } from "../api.js";
import { clear, copyText, fmtBytes, fmtDuration, h, icon, replaceChildren } from "../dom.js";
import { bus, serverList, state } from "../store.js";
import { activityItem, channelChip, emptyState, jobDialog, quickJob, statusBadge, toast, versionText } from "../ui.js";
import { openCreateServer } from "./create.js";

export function connectAddress(server) {
  return `${server.public_address || location.hostname}:${server.ports.java}`;
}

// What the signed-in account may do to a server: moderators can start it, admins can also stop and restart it.
export function serverActions(server, { compact = false } = {}) {
  const idle = !server.busy;
  const running = server.state === "running" || server.state === "starting";
  const btn = (cls, iconName, label, handler, disabled = false) =>
    h("button", { class: `btn ${cls}`, disabled: disabled || !idle, onclick: handler }, icon(iconName, "sm"), compact ? null : label);
  const act = (path, success) => () => quickJob(post(`/api/servers/${server.id}/${path}`), { success, failure: `Could not ${path}` });
  if (running) {
    return can(server, PERM.CONTROL) ? [
      btn("sm", "stop", "Stop", act("stop", `${server.name} stopped`)),
      btn("sm", "restart", "Restart", act("restart", `${server.name} restarted`)),
    ] : [];
  }
  return can(server, PERM.START) ? [btn("primary sm", "play", "Start", act("start", `${server.name} is running`))] : [];
}

function serverCard(initial) {
  let server = initial;
  const refs = {
    badge: h("span", {}),
    meta: h("div", { class: "row" }),
    update: h("div", {}),
    players: h("div", { class: "v" }),
    uptime: h("div", { class: "v" }),
    cpu: h("div", { class: "v" }),
    mem: h("div", { class: "v" }),
    busy: h("div", {}),
    actions: h("div", { class: "row" }),
    console: h("span", {}),
    openLabel: h("span", {}, "Manage"),
  };
  const name = h("a", { class: "name truncate", href: `#/server/${server.id}` }, server.name);
  const address = h("span", { class: "addr" }, h("span", { class: "truncate" }, ""), h("button", { title: "Copy address", "aria-label": "Copy address", onclick: async () => {
    if (await copyText(connectAddress(server))) toast("Address copied", "success", null, 1800);
  } }, icon("copy", "sm")));
  const fact = (label, el) => h("div", { class: "fact" }, h("div", { class: "k" }, label), el);

  const el = h("div", { class: "card server-card", dataset: { id: server.id } },
    h("div", { class: "row" }, h("div", { class: "grow" }, name), refs.badge),
    refs.meta, address, refs.update,
    h("div", { class: "facts" }, fact("Players", refs.players), fact("Uptime", refs.uptime), fact("CPU", refs.cpu), fact("Memory", refs.mem)),
    refs.busy,
    h("div", { class: "row" }, refs.actions, h("span", { class: "grow" }),
      refs.console,
      h("a", { class: "btn sm", href: `#/server/${server.id}` }, refs.openLabel, icon("chevronRight", "sm"))));

  let lastShape = "";
  function update(next, statsOnly = false) {
    server = next;
    const running = server.state === "running";
    refs.players.textContent = running ? `${server.players.online} / ${server.players.max || "∞"}` : "–";
    refs.uptime.textContent = server.uptime != null ? fmtDuration(server.uptime) : "–";
    refs.cpu.textContent = server.state === "stopped" ? "–" : `${server.stats.cpu.toFixed(1)}%`;
    refs.mem.textContent = server.state === "stopped" ? "–" : fmtBytes(server.stats.rss);
    if (statsOnly) return;

    // Structural parts are rebuilt only when what they show actually changed (keeps hover/focus stable).
    const shape = JSON.stringify([server.state, server.busy, server.name, server.build?.id, server.channel, server.update.available, server.update.pending_build?.id, server.ports, server.public_address, server.access]);
    if (shape === lastShape) return;
    lastShape = shape;
    name.textContent = server.name;
    clear(refs.console);
    if (can(server, PERM.CONSOLE_VIEW)) refs.console.appendChild(h("a", { class: "btn sm", href: `#/server/${server.id}/console` }, icon("terminal", "sm"), "Console"));
    refs.openLabel.textContent = can(server, PERM.SETTINGS) ? "Manage" : "Open";
    address.firstChild.textContent = connectAddress(server);
    clear(refs.badge).appendChild(statusBadge(server));
    replaceChildren(refs.meta,
      h("span", { class: "chip" }, icon("layers", "sm"), versionText(server.build)),
      channelChip(server.channel),
      server.pinned_tag ? h("span", { class: "chip", title: "Pinned: automatic updates are off" }, icon("shield", "sm"), "pinned") : null,
      server.ports.bedrock ? h("span", { class: "chip", title: `Bedrock UDP port ${server.ports.bedrock}` }, "Bedrock") : null);
    clear(refs.update);
    if (server.update.available) {
      refs.update.appendChild(h("div", { class: "update-banner" }, icon("arrowUp"),
        h("div", { class: "grow" }, "Update available: ", h("strong", {}, versionText(server.update.pending_build))),
        server.update.mode === "auto"
          ? h("span", { class: "muted small" }, "installs automatically")
          : h("a", { class: "btn sm primary", href: `#/server/${server.id}/version` }, "Review")));
    }
    clear(refs.busy);
    if (server.busy) refs.busy.appendChild(h("div", { class: "busy-bar" }, h("span", {})));
    clear(refs.actions).append(...serverActions(server));
  }
  update(server);
  return { el, update };
}

function statTile(label, iconName, valueEl, subEl) {
  return h("div", { class: "card stat" }, h("div", { class: "label" }, icon(iconName, "sm"), label), valueEl, subEl);
}

export function render() {
  const cards = new Map();
  const grid = h("div", { class: "grid servers" });
  const tiles = {
    servers: h("div", { class: "value" }), serversSub: h("div", { class: "sub" }),
    players: h("div", { class: "value" }), playersSub: h("div", { class: "sub" }),
    updates: h("div", { class: "value" }), updatesSub: h("div", { class: "sub" }),
    disk: h("div", { class: "value" }), diskSub: h("div", { class: "sub" }),
  };
  const jobsHost = h("div", {});
  const activityHost = h("div", { class: "list" });
  const activityCard = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Recent activity"),
    isAdministrator() ? h("a", { href: "#/activity", class: "small" }, "View all") : null), activityHost);
  const emptyHost = h("div", {});

  function refreshTiles() {
    const servers = serverList();
    const running = servers.filter((s) => s.state === "running");
    tiles.servers.textContent = `${running.length} / ${servers.length}`;
    tiles.serversSub.textContent = "running";
    tiles.players.textContent = String(running.reduce((n, s) => n + (s.players?.online || 0), 0));
    tiles.playersSub.textContent = "online right now";
    const pending = servers.filter((s) => s.update.available).length;
    tiles.updates.textContent = String(pending);
    tiles.updatesSub.textContent = pending ? "servers can be updated" : "everything is current";
    const sys = state.system;
    tiles.disk.textContent = sys ? fmtBytes(sys.disk.backups) : "–";
    tiles.diskSub.textContent = sys ? `backups · ${fmtBytes(sys.disk.backup_free)} free` : "";
  }

  function refreshGrid() {
    const servers = serverList();
    for (const [id, card] of cards) if (!state.servers.has(id)) { card.el.remove(); cards.delete(id); }
    for (const server of servers) {
      const existing = cards.get(server.id);
      if (existing) existing.update(server);
      else {
        const card = serverCard(server);
        cards.set(server.id, card);
        grid.appendChild(card.el);
      }
    }
    clear(emptyHost);
    if (!servers.length) {
      emptyHost.appendChild(h("div", { class: "card" }, isAdministrator()
        ? emptyState("server", "Create your first server",
          "Pumpkin is a fast Minecraft server written in Rust. The manager downloads it, sets it up and keeps it updated and backed up for you.",
          h("button", { class: "btn primary", onclick: openCreateServer }, icon("plus", "sm"), "New server"))
        : emptyState("server", "No servers yet", "The administrator has not given you access to a server. Once they do, it shows up here.")));
    }
    activityCard.classList.toggle("hidden", !(isAdministrator() || canAnywhere(PERM.ACTIVITY)));
    refreshTiles();
  }

  function refreshJobs() {
    const running = [...state.jobs.values()].filter((j) => j.status === "running");
    clear(jobsHost);
    if (!running.length) return;
    jobsHost.appendChild(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "In progress")),
      h("div", { class: "stack-sm" }, running.map((job) => h("div", { class: "stack-sm" },
        h("div", { class: "row" }, h("strong", { class: "grow" }, job.title), h("span", { class: "muted small truncate" }, job.message || "")),
        typeof job.progress === "number"
          ? h("div", { class: "progress" }, h("span", { style: { width: `${Math.max(3, Math.round(job.progress * 100))}%` } }))
          : h("div", { class: "busy-bar" }, h("span", {})))))));
  }

  function refreshActivity() {
    clear(activityHost);
    const items = state.activity.filter((a) => a.kind !== "player").slice(0, 8);
    if (!items.length) activityHost.appendChild(h("p", { class: "muted" }, "Nothing has happened yet."));
    for (const a of items) activityHost.appendChild(activityItem(a, state.servers));
  }

  const offs = [
    bus.on("servers", refreshGrid),
    bus.on("stats", (stats) => { for (const id of Object.keys(stats)) cards.get(id)?.update(state.servers.get(id), true); refreshTiles(); }),
    bus.on("jobs", refreshJobs),
    bus.on("activity", refreshActivity),
    bus.on("system", refreshTiles),
  ];

  refreshGrid(); refreshJobs(); refreshActivity();
  const owner = isAdministrator();
  const el = h("div", { class: "stack" },
    h("div", { class: "page-head" },
      h("div", { class: "titles" }, h("h1", {}, "Dashboard"), h("p", {}, owner ? "All your Pumpkin servers at a glance." : "The servers you have access to.")),
      owner ? h("div", { class: "actions" }, h("button", { class: "btn primary", onclick: openCreateServer }, icon("plus", "sm"), "New server")) : null),
    h("div", { class: "grid cols-4" },
      statTile("Servers", "server", tiles.servers, tiles.serversSub),
      statTile("Players", "users", tiles.players, tiles.playersSub),
      owner ? statTile("Updates", "arrowUp", tiles.updates, tiles.updatesSub) : null,
      owner ? statTile("Storage", "disk", tiles.disk, tiles.diskSub) : null),
    jobsHost, emptyHost, grid, activityCard);
  return { el, dispose: () => offs.forEach((off) => off()) };
}
