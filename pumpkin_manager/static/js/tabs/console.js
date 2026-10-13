// Live console: streamed output with colours, command input with history, filter and follow mode.

import { PERM, can } from "../access.js";
import { get, post } from "../api.js";
import { ansiToNodes, stripAnsi } from "../ansi.js";
import { clear, debounce, h, icon } from "../dom.js";
import { bus } from "../store.js";
import { notifyError } from "../ui.js";
import { subscribeConsole, unsubscribeConsole } from "../ws.js";

const MAX_LINES = 2500;

export function render({ id, getServer }) {
  const out = h("div", { class: "console-out wrap", tabindex: "0", role: "log", "aria-live": "off", "aria-label": "Server console output" });
  let follow = true;
  let lastN = 0;
  let filter = "";

  const jump = h("button", { class: "btn primary sm jump hidden", onclick: () => { follow = true; scrollToEnd(); jump.classList.add("hidden"); } }, icon("chevronDown", "sm"), "Latest");
  const filterInput = h("input", { type: "search", placeholder: "Filter output", "aria-label": "Filter console output", oninput: debounce((e) => { filter = e.target.value.toLowerCase(); applyFilter(); }, 150) });
  const wrapBtn = h("button", { class: "btn", "aria-pressed": "true", title: "Wrap long lines", onclick: () => {
    const on = wrapBtn.getAttribute("aria-pressed") !== "true";
    wrapBtn.setAttribute("aria-pressed", String(on));
    out.classList.toggle("wrap", on);
  } }, icon("wrap", "sm"), "Wrap");
  const clearBtn = h("button", { class: "btn", onclick: () => { clear(out); } }, icon("trash", "sm"), "Clear view");
  const status = h("span", { class: "grow" });

  function scrollToEnd() { out.scrollTop = out.scrollHeight; }

  function lineEl(line) {
    const el = h("div", { class: `line ${line.s}` }, ansiToNodes(line.m));
    if (filter && !stripAnsi(line.m).toLowerCase().includes(filter)) el.classList.add("hidden-by-filter");
    el.dataset.text = line.m;
    return el;
  }

  function addLines(lines) {
    let added = false;
    for (const line of lines) {
      if (line.n <= lastN) continue;
      lastN = line.n;
      out.appendChild(lineEl(line));
      added = true;
    }
    if (!added) return;
    while (out.childElementCount > MAX_LINES) out.removeChild(out.firstChild);
    if (follow) scrollToEnd();
  }

  function applyFilter() {
    for (const el of out.children) {
      const match = !filter || stripAnsi(el.dataset.text || "").toLowerCase().includes(filter);
      el.classList.toggle("hidden-by-filter", !match);
    }
    if (follow) scrollToEnd();
  }

  out.addEventListener("scroll", () => {
    const atEnd = out.scrollHeight - out.scrollTop - out.clientHeight < 28;
    follow = atEnd;
    jump.classList.toggle("hidden", atEnd);
  });

  // --- input ------------------------------------------------------------------------------------
  const historyKey = `console-history:${id}`;
  let history = [];
  try { history = JSON.parse(sessionStorage.getItem(historyKey) || "[]"); } catch { /* ignore */ }
  let cursor = history.length;
  const input = h("input", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": "Console command", placeholder: "Type a command, e.g. list or say hello" });

  async function send(command) {
    command = command.trim();
    if (!command) return;
    if (history[history.length - 1] !== command) history.push(command);
    history = history.slice(-60);
    cursor = history.length;
    try { sessionStorage.setItem(historyKey, JSON.stringify(history)); } catch { /* ignore */ }
    follow = true;
    try { await post(`/api/servers/${id}/command`, { command }); } catch (err) { notifyError(err); }
  }

  const form = h("form", { class: "console-form", onsubmit: (e) => { e.preventDefault(); const value = input.value; input.value = ""; send(value); } },
    h("span", { class: "prompt", "aria-hidden": "true" }, ">"), input,
    h("button", { class: "btn primary", type: "submit" }, "Send"));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp" && history.length) { e.preventDefault(); cursor = Math.max(0, cursor - 1); input.value = history[cursor] || ""; }
    else if (e.key === "ArrowDown") { e.preventDefault(); cursor = Math.min(history.length, cursor + 1); input.value = history[cursor] || ""; }
  });

  // Moderators can read the console but not type into it: neither the box nor the shortcut buttons are offered to them.
  const canType = can(getServer(), PERM.CONSOLE_COMMAND);
  // the commands a person types most: a proxy (BungeeCord) has its own, and none of the world's
  const quick = canType ? (getServer().proxy ? ["glist", "server"] : ["list", "tps", "save-all"]).map((c) => h("button", { class: "btn", type: "button", onclick: () => send(c) }, c)) : [];
  const box = h("div", { class: "console-box" },
    h("div", { class: "console-bar" }, status, filterInput, wrapBtn, clearBtn, ...quick),
    out, jump,
    canType ? form : h("div", { class: "console-form" }, h("span", { class: "muted small" }, "You can read the console, but only admins can send commands.")));

  // --- live feed --------------------------------------------------------------------------------
  const off = bus.on(`console:${id}`, (msg) => {
    if (msg.type === "backlog") { clear(out); lastN = 0; }
    addLines(msg.data.lines);
  });
  subscribeConsole(id);
  get(`/api/servers/${id}/console?lines=500`).then((data) => addLines(data.lines)).catch(() => {});

  function refreshStatus(s) {
    status.textContent = s.state === "running" ? "Connected to the server console" : s.state === "stopped" ? (canType ? "Server is stopped: commands are unavailable" : "Server is stopped") : `Server is ${s.state}`;
    input.disabled = !(s.state === "running" || s.state === "starting");
    input.placeholder = input.disabled ? "Start the server to send commands" : "Type a command, e.g. list or say hello";
  }
  refreshStatus(getServer());

  return {
    el: box,
    onServer: refreshStatus,
    dispose() { off(); unsubscribeConsole(id); },
  };
}
