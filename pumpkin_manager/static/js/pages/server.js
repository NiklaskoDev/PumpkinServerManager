// Server detail page: header + tabs. Each tab is a module exporting render(ctx) -> { el, dispose?, onServer? }.

import { get, post } from "../api.js";
import { clear, h, icon } from "../dom.js";
import { bus, state } from "../store.js";
import { channelChip, confirmDialog, emptyState, notifyError, quickJob, statusBadge, versionText } from "../ui.js";
import { connectAddress, serverActions } from "./dashboard.js";
import * as backupsTab from "../tabs/backups.js";
import * as consoleTab from "../tabs/console.js";
import * as filesTab from "../tabs/files.js";
import * as overviewTab from "../tabs/overview.js";
import * as playersTab from "../tabs/players.js";
import * as settingsTab from "../tabs/settings.js";
import * as versionTab from "../tabs/version.js";

const TABS = [
  { id: "overview", label: "Overview", icon: "activity", mod: overviewTab },
  { id: "console", label: "Console", icon: "terminal", mod: consoleTab },
  { id: "players", label: "Players", icon: "users", mod: playersTab },
  { id: "files", label: "Files", icon: "folder", mod: filesTab },
  { id: "backups", label: "Backups", icon: "archive", mod: backupsTab },
  { id: "version", label: "Version", icon: "layers", mod: versionTab },
  { id: "settings", label: "Settings", icon: "sliders", mod: settingsTab },
];

export async function render([id, tabId]) {
  if (!state.servers.has(id)) {
    return { el: h("div", { class: "card" }, emptyState("server", "Server not found", "It may have been deleted.", h("a", { class: "btn primary", href: "#/" }, "Back to the dashboard"))) };
  }
  const tab = TABS.find((t) => t.id === tabId) || TABS[0];
  try {
    const detail = await get(`/api/servers/${id}`);
    state.servers.set(id, { ...state.servers.get(id), ...detail });
  } catch (err) {
    notifyError(err);
  }
  const getServer = () => state.servers.get(id);

  const title = h("h1", { class: "truncate" });
  const badge = h("span", {});
  const meta = h("div", { class: "row", style: { marginTop: "8px" } });
  const actions = h("div", { class: "actions" });
  const content = h("div", {});

  function renderHeader() {
    const server = getServer();
    if (!server) return;
    title.textContent = server.name;
    clear(badge).appendChild(statusBadge(server));
    clear(meta).append(
      h("span", { class: "chip" }, icon("layers", "sm"), versionText(server.build)),
      channelChip(server.channel),
      h("span", { class: "chip mono" }, icon("globe", "sm"), connectAddress(server)));
    clear(actions).append(...serverActions(server));
    if (server.state === "starting" || server.state === "stopping" || (server.busy && server.state !== "stopped")) {
      actions.appendChild(h("button", { class: "btn danger sm", title: "Terminate the server process immediately (the world may not be saved)", onclick: async () => {
        if (!(await confirmDialog({ title: "Force-stop the process?", message: "The server is killed without saving. Use this only if it is stuck.", confirmLabel: "Kill process", danger: true }))) return;
        try { await post(`/api/servers/${id}/kill`); } catch (err) { notifyError(err); }
      } }, icon("skull", "sm"), "Kill"));
    }
  }

  const tabsNav = h("div", { class: "tabs", role: "tablist" }, TABS.map((t) =>
    h("a", { href: `#/server/${id}/${t.id}`, role: "tab", class: t.id === tab.id ? "active" : "", "aria-selected": String(t.id === tab.id) }, icon(t.icon, "sm"), t.label)));

  const view = tab.mod.render({ id, getServer });
  content.appendChild(view.el);
  renderHeader();

  const offs = [
    bus.on("server", (s) => {
      if (s.id !== id) return;
      renderHeader();
      if (view.onServer) view.onServer(getServer());
    }),
    bus.on("stats", (stats) => { if (stats[id] && view.onStats) view.onStats(getServer()); }),
  ];

  const el = h("div", { class: "stack" },
    h("div", { class: "page-head" },
      h("div", { class: "titles" }, h("a", { href: "#/", class: "small muted" }, "← All servers"), h("div", { class: "row", style: { marginTop: "4px" } }, title, badge), meta),
      actions),
    tabsNav, content);

  return {
    el,
    title: getServer()?.name,
    dispose: () => { offs.forEach((off) => off()); if (view.dispose) view.dispose(); },
  };
}
