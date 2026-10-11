import { get } from "../api.js";
import { clear, fmtDateTime, h } from "../dom.js";
import { bus, state } from "../store.js";
import { levelIcon, segmented } from "../ui.js";

export async function render() {
  let level = "all";
  let showPlayers = false;
  let rows = [];
  try { rows = await get("/api/activity?limit=300"); } catch { rows = state.activity; }
  const host = h("div", { class: "list" });

  function draw() {
    clear(host);
    const filtered = rows.filter((a) => (level === "all" || a.level === level) && (showPlayers || a.kind !== "player"));
    if (!filtered.length) host.appendChild(h("p", { class: "muted" }, "Nothing to show."));
    for (const a of filtered) {
      const server = a.server_id ? state.servers.get(a.server_id) : null;
      host.appendChild(h("div", { class: "list-item" }, levelIcon(a.level),
        h("div", { class: "grow" }, h("div", {}, a.message), h("div", { class: "meta" }, fmtDateTime(a.ts), server ? ` · ${server.name}` : "", ` · ${a.kind}`))));
    }
  }

  const off = bus.on("activity", (a) => { rows.unshift(a); draw(); });
  draw();
  return {
    el: h("div", { class: "stack" },
      h("div", { class: "page-head" }, h("div", { class: "titles" }, h("h1", {}, "Activity"), h("p", {}, "Starts, stops, backups, updates and sign-ins.")),
        h("div", { class: "actions" },
          segmented([{ value: "all", label: "All" }, { value: "warning", label: "Warnings" }, { value: "error", label: "Errors" }], level, (v) => { level = v; draw(); }),
          h("label", { class: "switch" }, h("input", { type: "checkbox", onchange: (e) => { showPlayers = e.target.checked; draw(); } }), h("span", { class: "track" }), h("span", { class: "text" }, "Player joins")))),
      h("div", { class: "card" }, host)),
    dispose: off,
  };
}
