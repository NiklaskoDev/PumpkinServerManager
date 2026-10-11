// Application shell: boot, routing, navigation and global notifications.

import { get, post, setCsrf, setUnauthorizedHandler } from "./api.js";
import { clear, h, icon, logoSvg } from "./dom.js";
import { bus, loadActivity, loadJobs, loadServers, loadSettings, loadSystem, serverList, state } from "./store.js";
import { notifyError, toast, trackedJobs } from "./ui.js";
import { connect, disconnect } from "./ws.js";
import * as activityPage from "./pages/activity.js";
import * as backupsPage from "./pages/backups.js";
import * as dashboardPage from "./pages/dashboard.js";
import { renderLogin } from "./pages/login.js";
import * as serverPage from "./pages/server.js";
import * as settingsPage from "./pages/settings.js";
import * as versionsPage from "./pages/versions.js";

const root = document.getElementById("app");
const THEME_KEY = "pumpkin-manager-theme";

// ---------------------------------------------------------------------- theme
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
function savedTheme() { try { return localStorage.getItem(THEME_KEY); } catch { return null; } }
function currentTheme() {
  return document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch { /* private mode */ }
}
applyTheme(savedTheme());

// --------------------------------------------------------------------- routes
const routes = [
  { re: /^\/?$/, nav: "dashboard", page: dashboardPage, title: "Dashboard" },
  { re: /^\/server\/([a-z0-9-]+)(?:\/([a-z]+))?\/?$/, nav: "dashboard", page: serverPage, title: "Server" },
  { re: /^\/backups\/?$/, nav: "backups", page: backupsPage, title: "Backups" },
  { re: /^\/versions\/?$/, nav: "versions", page: versionsPage, title: "Versions" },
  { re: /^\/activity\/?$/, nav: "activity", page: activityPage, title: "Activity" },
  { re: /^\/settings\/?$/, nav: "settings", page: settingsPage, title: "Settings" },
];

const NAV = [
  { id: "dashboard", href: "#/", label: "Dashboard", icon: "server" },
  { id: "backups", href: "#/backups", label: "Backups", icon: "archive" },
  { id: "versions", href: "#/versions", label: "Versions", icon: "layers" },
  { id: "activity", href: "#/activity", label: "Activity", icon: "activity" },
  { id: "settings", href: "#/settings", label: "Settings", icon: "sliders" },
];

let currentPage = null;
let shell = null;
let navigationToken = 0;

async function navigate() {
  if (!shell) return;
  const token = ++navigationToken;
  const path = location.hash.replace(/^#/, "") || "/";
  const route = routes.find((r) => r.re.test(path)) || routes[0];
  const params = (path.match(route.re) || []).slice(1);

  if (currentPage && currentPage.dispose) { try { currentPage.dispose(); } catch (e) { console.error(e); } }
  currentPage = null;
  shell.main.classList.add("loading");
  closeSidebar();
  let result;
  try {
    result = await route.page.render(params);
  } catch (err) {
    console.error(err);
    result = { el: h("div", { class: "card" }, h("h2", {}, "Something went wrong"), h("p", { class: "muted" }, err.message || String(err))) };
  }
  if (token !== navigationToken) { if (result.dispose) result.dispose(); return; } // a newer navigation won
  currentPage = result;
  clear(shell.main).appendChild(result.el);
  shell.main.classList.remove("loading");
  document.title = `${result.title || route.title} · Pumpkin Manager`;
  updateNav(route.nav);
  window.scrollTo(0, 0);
}

// --------------------------------------------------------------------- layout
function closeSidebar() {
  if (!shell) return;
  shell.sidebar.classList.remove("open");
  shell.scrim.classList.add("hidden");
}

function buildShell() {
  const nav = h("nav", { class: "nav", "aria-label": "Main" });
  const navLinks = new Map();
  for (const item of NAV) {
    const countEl = h("span", { class: "count hidden" });
    const link = h("a", { href: item.href, "data-nav": item.id }, icon(item.icon), h("span", {}, item.label), item.id === "versions" ? countEl : null);
    navLinks.set(item.id, link);
    nav.appendChild(link);
  }
  const serverNav = h("div", { class: "nav", "aria-label": "Servers" });
  const connDot = h("div", { class: "conn" }, h("span", { class: "dot" }), h("span", { class: "label" }, "Live"));
  const themeBtn = h("button", { class: "btn ghost sm", title: "Toggle light/dark theme", onclick: toggleTheme }, icon("sun", "sm"), "Theme");
  const logoutBtn = h("button", { class: "btn ghost sm", title: "Sign out", onclick: logout }, icon("logout", "sm"), "Sign out");

  const sidebar = h("aside", { class: "sidebar", "aria-label": "Sidebar" },
    h("div", { class: "brand" }, logoSvg(), h("div", {}, "Pumpkin Manager", h("small", {}, `v${state.user.version}`))),
    nav,
    h("div", { class: "section-title", style: { margin: "18px 10px 6px" } }, "Servers"),
    serverNav,
    h("div", { class: "sidebar-foot" },
      connDot,
      h("div", { class: "userbox" }, h("div", { class: "avatar" }, state.user.username.slice(0, 1).toUpperCase()),
        h("div", { class: "grow truncate" }, state.user.username)),
      h("div", { class: "row" }, themeBtn, logoutBtn)));
  const scrim = h("div", { class: "scrim hidden", onclick: closeSidebar });
  const main = h("main", { class: "main", id: "main" });
  const topbar = h("div", { class: "topbar" },
    h("button", { class: "btn ghost icon-only", "aria-label": "Open menu", onclick: () => { sidebar.classList.add("open"); scrim.classList.remove("hidden"); } }, icon("menu")),
    h("div", { class: "brand", style: { padding: "0" } }, logoSvg(), "Pumpkin Manager"));

  const el = h("div", { class: "shell" }, sidebar, scrim, h("div", {}, topbar, main));
  return { el, sidebar, scrim, main, navLinks, serverNav, connDot };
}

function updateNav(active) {
  for (const [id, link] of shell.navLinks) link.classList.toggle("active", id === active && !location.hash.startsWith("#/server/"));
  renderServerNav();
}

function renderServerNav() {
  if (!shell) return;
  const current = (location.hash.match(/^#\/server\/([a-z0-9-]+)/) || [])[1];
  clear(shell.serverNav);
  const servers = serverList();
  if (!servers.length) shell.serverNav.appendChild(h("div", { class: "muted small", style: { padding: "4px 10px" } }, "No servers yet"));
  for (const s of servers) {
    const dotClass = s.busy ? "busy" : s.state;
    shell.serverNav.appendChild(h("a", { href: `#/server/${s.id}`, class: s.id === current ? "active" : "" },
      h("span", { class: `badge ${dotClass}`, style: { padding: "0", background: "none" } }, h("span", { class: "dot" })),
      h("span", { class: "truncate" }, s.name)));
  }
  const updates = servers.filter((s) => s.update && s.update.available).length;
  const count = shell.navLinks.get("versions").querySelector(".count");
  count.textContent = String(updates);
  count.classList.toggle("hidden", updates === 0);
}

async function logout() {
  try { await post("/api/auth/logout"); } catch { /* already signed out */ }
  showLogin();
}

// ------------------------------------------------------------- notifications
function installGlobalNotifications() {
  // Background work (scheduled backups, automatic updates, crash restarts) reports itself here.
  bus.on("job", ({ job, previous }) => {
    if (!previous || previous.status !== "running" || job.status === "running" || trackedJobs.has(job.id)) return;
    const name = state.servers.get(job.server_id)?.name;
    const where = name && !job.title.includes(name) ? ` (${name})` : "";
    if (job.status === "failed") toast(job.message || "A background task failed.", "error", `${job.title}${where} failed`);
    else if (job.kind === "update") toast(job.message || "Update finished.", "success", `${job.title}${where}`);
  });
  bus.on("connection", (online) => {
    if (!shell) return;
    shell.connDot.classList.toggle("offline", !online);
    shell.connDot.querySelector(".label").textContent = online ? "Live" : "Reconnecting...";
  });
  bus.on("reconnected", () => { loadServers().catch(() => {}); loadJobs().catch(() => {}); });
  bus.on("servers", renderServerNav);
  bus.on("deleted", (d) => { if (location.hash.startsWith(`#/server/${d.id}`)) location.hash = "#/"; });
}

// ----------------------------------------------------------------------- boot
function showLogin() {
  disconnect();
  shell = null;
  navigationToken++;
  if (currentPage && currentPage.dispose) currentPage.dispose();
  currentPage = null;
  clear(root).appendChild(renderLogin(async (session) => {
    setCsrf(session.csrf);
    state.user = session;
    await startApp();
  }));
}

let notificationsInstalled = false;

async function startApp() {
  try {
    await Promise.all([loadServers(), loadSystem(), loadSettings(), loadActivity(), loadJobs()]);
  } catch (err) {
    if (err.status !== 401) notifyError(err);
    return;
  }
  shell = buildShell();
  clear(root).appendChild(shell.el);
  if (!notificationsInstalled) { installGlobalNotifications(); notificationsInstalled = true; }
  connect();
  window.addEventListener("hashchange", navigate);
  await navigate();
}

async function boot() {
  setUnauthorizedHandler(() => { if (shell || !document.querySelector(".login-wrap")) showLogin(); });
  try {
    const me = await get("/api/auth/me");
    setCsrf(me.csrf);
    state.user = me;
    await startApp();
  } catch (err) {
    if (err.status === 401) showLogin();
    else clear(root).appendChild(h("div", { class: "splash" }, h("div", { class: "card" }, h("h2", {}, "Cannot reach the manager"), h("p", { class: "muted" }, err.message))));
  }
}

boot();
