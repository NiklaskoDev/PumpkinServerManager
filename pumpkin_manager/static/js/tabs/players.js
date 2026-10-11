// Players: who is online, plus whitelist / operators / bans. Changes go through the server console.

import { get, post } from "../api.js";
import { clear, h, icon } from "../dom.js";
import { OP_LEVELS, opLevelHelp } from "../oplevels.js";
import { confirmDialog, emptyState, jobDialog, notifyError, promptDialog, segmented, selectControl, toast } from "../ui.js";

const LISTS = [
  { id: "whitelist", label: "Whitelist", remove: "whitelist_remove", key: "name" },
  { id: "ops", label: "Operators", remove: "deop", key: "name" },
  { id: "banned_players", label: "Banned players", remove: "pardon", key: "name" },
  { id: "banned_ips", label: "Banned IPs", remove: "pardon_ip", key: "ip" },
];

// What each console command should leave behind in the lists. Kicking changes no list.
const OUTCOME = {
  whitelist_add: { list: "whitelist", present: true, key: "name", done: "Added to the whitelist" },
  whitelist_remove: { list: "whitelist", present: false, key: "name", done: "Removed from the whitelist" },
  op: { list: "ops", present: true, key: "name", done: "Now an operator" },
  deop: { list: "ops", present: false, key: "name", done: "No longer an operator" },
  ban: { list: "banned_players", present: true, key: "name", done: "Player banned" },
  pardon: { list: "banned_players", present: false, key: "name", done: "Player pardoned" },
  ban_ip: { list: "banned_ips", present: true, key: "ip", done: "IP address banned" },
  pardon_ip: { list: "banned_ips", present: false, key: "ip", done: "IP address pardoned" },
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function render({ id, getServer }) {
  let data = { online: { count: 0, max: 0, names: [] }, lists: {}, running: false };
  let active = "whitelist";
  let disposed = false;
  const onlineHost = h("div", {});
  const listHost = h("div", {});
  const nameInput = h("input", { class: "input", placeholder: "Player name", maxlength: "32", autocomplete: "off", "aria-label": "Player name" });
  const notice = h("div", {});

  async function act(action, target, reason) {
    try {
      await post(`/api/servers/${id}/players`, { action, target, reason: reason || null });
    } catch (err) {
      return notifyError(err);
    }
    const outcome = OUTCOME[action];
    if (!outcome) {
      toast("Command sent", "success", null, 1500);
      return setTimeout(load, 900);
    }
    // The command only goes to the server's console, so check that the list really changed. The server writes its files a moment
    // later, and looking up a Mojang account name can take several seconds (we give up after about 12).
    const wanted = target.trim().toLowerCase();
    const listed = () => (data.lists[outcome.list] || []).some((e) => String(e[outcome.key] ?? "").toLowerCase() === wanted);
    for (const delay of [700, 1300, 2000, 3000, 5000]) {
      await sleep(delay);
      if (disposed) return;
      await load();
      if (listed() === outcome.present) return toast(outcome.done, "success", null, 2000);
    }
    toast("The list did not change. The server's answer is in the Console tab.", "warning");
  }

  function renderOnline() {
    clear(onlineHost);
    const { names, count } = data.online;
    if (!data.running) return onlineHost.appendChild(h("p", { class: "muted" }, "The server is not running."));
    if (!names.length) return onlineHost.appendChild(h("p", { class: "muted" }, count ? `${count} online (names unavailable).` : "Nobody is online."));
    names.forEach((name) => onlineHost.appendChild(h("div", { class: "player" }, h("div", { class: "head" }), h("strong", { class: "grow" }, name),
      h("button", { class: "btn sm", onclick: async () => {
        const reason = await promptDialog({ title: `Kick ${name}`, label: "Reason (optional)", confirmLabel: "Kick" });
        if (reason !== null) act("kick", name, reason);
      } }, "Kick"),
      h("button", { class: "btn sm danger", onclick: async () => {
        if (await confirmDialog({ title: `Ban ${name}?`, message: "They will be removed and cannot rejoin until pardoned.", confirmLabel: "Ban", danger: true })) act("ban", name);
      } }, "Ban"))));
  }

  // Pumpkin reads operator levels only when it starts, so on a running server this restarts it (the manager edits data/ops.json
  // between the stop and the start). `select` is the dropdown to put back if the change is cancelled or fails.
  async function setLevel(name, level, select = null, previous = null) {
    const revert = () => { if (select) select.value = String(previous); };
    if (data.running) {
      const ok = await confirmDialog({
        title: `Make ${name} operator level ${level}?`, confirmLabel: "Restart and apply",
        message: "Pumpkin only reads operator levels when it starts, so the server restarts to apply this. Players who are online are disconnected for a few seconds and can rejoin afterwards.",
      });
      if (!ok) return revert();
    }
    const job = await jobDialog(post(`/api/servers/${id}/operators/level`, { name, level }), { title: `${name}: operator level ${level}` });
    if (!job || job.status !== "success") revert();
    await load();
  }

  function levelSelect(value, onChange) {
    const known = OP_LEVELS.some((l) => l.value === value);
    const options = OP_LEVELS.map(({ value: v, label }) => ({ value: v, label }));
    if (!known) options.unshift({ value, label: `${value}` }); // an unusual value written by hand into ops.json
    return selectControl(options, value, (v) => onChange(Number(v)));
  }

  function renderList() {
    clear(listHost);
    const def = LISTS.find((l) => l.id === active);
    const entries = data.lists[active] || [];
    if (!entries.length) {
      return listHost.appendChild(emptyState("users", `${def.label} is empty`, active === "whitelist" ? "Enable the whitelist in Settings to restrict who can join." : null));
    }
    const ops = active === "ops";
    listHost.appendChild(h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, def.key === "ip" ? "IP address" : "Player"), ops ? h("th", {}, "Level") : null, h("th", {}, "Details"), h("th", {}, ""))),
      h("tbody", {}, entries.map((e) => {
        const name = e[def.key] || e.name || e.ip || "?";
        const details = [e.uuid, e.reason, e.source, e.expires && e.expires !== "forever" ? `until ${e.expires}` : null].filter(Boolean).join(" · ");
        let level = null;
        if (ops) {
          const select = levelSelect(e.level, (value) => setLevel(name, value, select, e.level));
          select.title = opLevelHelp(e.level);
          level = h("td", {}, select);
        }
        return h("tr", {}, h("td", {}, h("strong", {}, name)), level, h("td", { class: "muted small mono" }, details || "–"),
          h("td", { class: "actions-cell" }, h("button", { class: "btn sm danger", disabled: !data.running, title: data.running ? "Remove" : "Start the server to change this list", onclick: () => act(def.remove, name) }, icon("trash", "sm"), "Remove")));
      })))));
  }

  const tabs = segmented(LISTS.map((l) => ({ value: l.id, label: l.label })), active, (v) => { active = v; renderList(); });

  // The level a new operator gets: the server's own default unless the user picked another one in the form.
  let levelTouched = false;
  const levelPick = levelSelect(1, () => { levelTouched = true; describeLevel(); });
  const levelHint = h("span", { class: "hint" });
  function describeLevel() {
    const level = Number(levelPick.value);
    const differs = level !== data.op_level && data.running;
    levelHint.textContent = `${opLevelHelp(level)}${differs ? ` The server's default is ${data.op_level}, so applying ${level} restarts the server.` : ""}`;
  }

  let shown = "";
  async function load() {
    try {
      data = await get(`/api/servers/${id}/players`);
    } catch { return; }
    if (!levelTouched && data.op_level) levelPick.value = String(data.op_level);
    describeLevel();
    // Redraw only when something changed, so a dropdown the user has open is not closed by the periodic refresh.
    const now = JSON.stringify(data);
    if (now !== shown) {
      shown = now;
      renderOnline();
      renderList();
    }
    clear(notice);
    if (!data.running) notice.appendChild(h("div", { class: "notice" }, icon("info", "sm"), "Player changes are applied through the running server's console, so start the server to edit these lists."));
  }

  const add = (action) => () => { const name = nameInput.value.trim(); if (name) { act(action, name); nameInput.value = ""; } else nameInput.focus(); };
  const makeOperator = () => {
    const name = nameInput.value.trim();
    if (!name) return nameInput.focus();
    nameInput.value = "";
    const level = Number(levelPick.value);
    // The server's default level is what /op gives, so only another level needs the restart route.
    return level === data.op_level ? act("op", name) : setLevel(name, level);
  };
  const el = h("div", { class: "stack" }, notice,
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Online now"), h("button", { class: "btn ghost sm", onclick: load }, icon("restart", "sm"), "Refresh")), onlineHost),
      h("div", { class: "card stack" }, h("h2", {}, "Add a player"),
        nameInput,
        h("div", { class: "row" },
          h("button", { class: "btn", onclick: add("whitelist_add") }, "Add to whitelist"),
          h("button", { class: "btn", onclick: makeOperator }, "Make operator"),
          h("button", { class: "btn danger", onclick: add("ban") }, "Ban")),
        h("label", { class: "field" }, h("span", {}, "Operator level"), levelPick, levelHint),
        h("p", { class: "hint" }, "Names are looked up by the server, so Mojang account names resolve to the right UUID."))),
    h("div", { class: "card tight" }, h("div", { class: "card-section" }, tabs), listHost));

  load();
  const timer = setInterval(load, 10000);
  return { el, dispose: () => { disposed = true; clearInterval(timer); }, onServer: () => load() };
}
