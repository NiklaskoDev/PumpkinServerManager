import { PERM, can } from "../access.js";
import { copyText, fmtBytes, fmtDuration, fmtRelative, h, icon, clear } from "../dom.js";
import { get, post } from "../api.js";
import { bus, state } from "../store.js";
import { activityItem, fmtMemoryMb, jobDialog, pendingText, sparkline, toast, versionText } from "../ui.js";

export function render({ id, getServer }) {
  const server = getServer();
  const cpuHistory = [...(server.stats?.cpu_history || [])];
  const memHistory = [...(server.stats?.mem_history || [])];
  const cpuSpark = sparkline(cpuHistory, 100);
  const memSpark = sparkline(memHistory, 0);

  const v = {
    state: h("div", { class: "value" }), uptime: h("div", { class: "value" }), players: h("div", { class: "value" }), playersSub: h("div", { class: "sub" }),
    cpu: h("div", { class: "value" }), mem: h("div", { class: "value" }), disk: h("div", { class: "value" }), mc: h("dd", {}), motd: h("dd", {}),
  };
  const tile = (label, iconName, value, extra) => h("div", { class: "card stat" }, h("div", { class: "label" }, icon(iconName, "sm"), label), value, extra);
  const playersHost = h("div", {});
  const versionHost = h("div", {});
  const activityHost = h("div", { class: "list" });
  const connHost = h("dl", { class: "kv" });

  function copyRow(label, value) {
    return [h("dt", {}, label), h("dd", {}, h("span", { class: "addr" }, h("span", { class: "truncate" }, value), h("button", { title: "Copy", "aria-label": `Copy ${label}`, onclick: async () => { if (await copyText(value)) toast("Copied", "success", null, 1500); } }, icon("copy", "sm"))))];
  }

  // Cheap values that change with every stats tick; touching only text nodes keeps buttons clickable.
  function refreshTiles(s) {
    const running = s.state === "running";
    v.state.textContent = s.busy ? s.busy : s.state;
    v.state.style.textTransform = "capitalize";
    v.uptime.textContent = s.uptime != null ? fmtDuration(s.uptime) : "–";
    v.players.textContent = running ? `${s.players.online}${s.players.max ? ` / ${s.players.max}` : ""}` : "–";
    v.playersSub.textContent = running ? (s.ping_ok ? "answering pings" : "not answering pings yet") : "server is not running";
    v.cpu.textContent = s.state === "stopped" ? "–" : `${s.stats.cpu.toFixed(1)}%`;
    v.mem.textContent = s.state === "stopped" ? "–" : fmtBytes(s.stats.rss);
    v.disk.textContent = s.disk_bytes != null ? fmtBytes(s.disk_bytes) : "–";
    v.mc.textContent = s.mc_version || "–";
    v.motd.textContent = s.motd || "–";
  }

  function refresh(s) {
    const running = s.state === "running";
    refreshTiles(s);

    clear(connHost).append(
      ...copyRow("Java Edition", `${s.public_address || location.hostname}:${s.ports.java}`),
      ...(s.ports.bedrock ? copyRow("Bedrock (UDP)", `${s.public_address || location.hostname}:${s.ports.bedrock}`) : []),
      h("dt", {}, "Minecraft"), v.mc, h("dt", {}, "MOTD"), v.motd,
      ...(can(s, PERM.SETTINGS) && s.engine !== "java" ? [h("dt", {}, "Management API"), h("dd", {}, s.state !== "running" ? "–" : s.msmp_ok ? h("span", { class: "badge success" }, "connected") : h("span", { class: "badge", title: "This Pumpkin build has no management API, or it is not reachable. The manager uses console commands and server pings instead." }, "not available"))] : []));
    if (!s.ports.java_published) {
      connHost.appendChild(h("dt", {}, ""));
      connHost.appendChild(h("dd", {}, h("div", { class: "notice warning" }, icon("alert", "sm"), "This port is outside the range the container publishes, so players outside Docker cannot reach it.")));
    }

    clear(playersHost);
    const names = s.players.names || [];
    if (!running) playersHost.appendChild(h("p", { class: "muted" }, "Start the server to see who is online."));
    else if (!names.length) playersHost.appendChild(h("p", { class: "muted" }, s.players.online ? `${s.players.online} online (names unavailable).` : "Nobody is online."));
    else names.forEach((n) => playersHost.appendChild(h("div", { class: "player" }, h("div", { class: "head" }), h("strong", {}, n))));

    clear(versionHost);
    const manage = can(s, PERM.VERSIONS);
    if (s.engine === "java" && s.software) {
      const sw = s.software;
      versionHost.appendChild(h("dl", { class: "kv" },
        h("dt", {}, "Software"), h("dd", {}, h("strong", {}, sw.label)),
        ...(sw.proxy ? [] : [h("dt", {}, "Minecraft"), h("dd", {}, sw.minecraft || "–")]),
        ...(manage && s.java ? [
          h("dt", {}, "Java"), h("dd", {}, `Java ${s.java.major} · ${fmtMemoryMb(s.java.memory_mb)} of memory`),
          h("dt", {}, "Updates"), h("dd", {}, { auto: "automatic, backup first", notify: "notify only", off: "off" }[s.update.mode] || "–"),
          h("dt", {}, "Last backup"), h("dd", {}, fmtRelative(s.last_backup_at)),
        ] : [])));
    } else if (s.engine === "java") {
      const m = s.modpack || {};
      versionHost.appendChild(h("dl", { class: "kv" },
        h("dt", {}, "Modpack"), h("dd", {}, h("strong", {}, [m.title, m.version_number].filter(Boolean).join(" ") || "–")),
        h("dt", {}, "Minecraft"), h("dd", {}, m.minecraft || "–"),
        h("dt", {}, "Mod loader"), h("dd", {}, m.loader?.label || "–"),
        ...(manage && s.java ? [
          h("dt", {}, "Java"), h("dd", {}, `Java ${s.java.major} · ${fmtMemoryMb(s.java.memory_mb)} of memory`),
          h("dt", {}, "Last backup"), h("dd", {}, fmtRelative(s.last_backup_at)),
        ] : [])));
    } else {
      versionHost.appendChild(h("dl", { class: "kv" },
        h("dt", {}, "Installed"), h("dd", {}, h("strong", {}, versionText(s.build))),
        ...(manage ? [
          h("dt", {}, "Channel"), h("dd", {}, s.pinned_tag ? `${s.channel} (pinned)` : s.channel),
          h("dt", {}, "Updates"), h("dd", {}, { auto: "automatic, backup first", notify: "notify only", off: "off" }[s.update.mode]),
          h("dt", {}, "Last backup"), h("dd", {}, fmtRelative(s.last_backup_at)),
        ] : [])));
    }
    if (manage && s.update.available) {
      versionHost.appendChild(h("div", { class: "update-banner", style: { marginTop: "12px" } }, icon("arrowUp"),
        h("div", { class: "grow" }, "Update available: ", h("strong", {}, pendingText(s))),
        h("button", { class: "btn primary sm", disabled: !!s.busy, onclick: () => jobDialog(post(`/api/servers/${id}/${s.engine !== "java" ? "update" : s.software ? "software/update" : "modpack/update"}`, {}), { title: `Updating ${s.name}`, successToast: "Update installed" }) }, "Update now")));
    }
  }

  const followsActivity = can(server, PERM.ACTIVITY);
  function refreshActivity() {
    if (!followsActivity) return;
    clear(activityHost);
    const items = state.activity.filter((a) => a.server_id === id).slice(0, 10);
    if (!items.length) activityHost.appendChild(h("p", { class: "muted" }, "No activity recorded yet."));
    items.forEach((a) => activityHost.appendChild(activityItem(a, state.servers)));
  }

  const offActivity = bus.on("activity", refreshActivity);
  if (followsActivity) {
    get(`/api/activity?server_id=${id}&limit=30`).then((rows) => {
      const others = state.activity.filter((a) => a.server_id !== id);
      state.activity = [...rows, ...others].sort((a, b) => b.id - a.id).slice(0, 200);
      refreshActivity();
    }).catch(() => {});
  }

  refresh(server);
  refreshActivity();

  const el = h("div", { class: "stack" },
    h("div", { class: "grid cols-4" },
      tile("Status", "power", v.state), tile("Uptime", "clock", v.uptime),
      tile("Players", "users", v.players, v.playersSub), tile("Disk", "disk", v.disk)),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "CPU")), v.cpu, cpuSpark),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Memory")), v.mem, memSpark)),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Connect")), connHost),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, server.engine === "java" && !server.software ? "Modpack" : "Version"),
        can(server, PERM.VERSIONS) ? h("a", { class: "small", href: `#/server/${id}/version` }, "Manage") : null), versionHost)),
    h("div", { class: followsActivity ? "grid cols-2" : "grid" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Online players")), playersHost),
      followsActivity ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Recent activity")), activityHost) : null));

  return {
    el,
    dispose: offActivity,
    onServer: refresh,
    onStats(s) {
      cpuHistory.push(s.stats.cpu); memHistory.push(s.stats.rss);
      if (cpuHistory.length > 60) { cpuHistory.shift(); memHistory.shift(); }
      cpuSpark.update(cpuHistory, 100);
      memSpark.update(memHistory, 0);
      refreshTiles(s);
    },
  };
}
