// Your account: who you are, what you may do on which server, and changing your password.

import { post } from "../api.js";
import { h, icon } from "../dom.js";
import { ROLES, ROLE_LABEL, isAdministrator, roleBadgeClass } from "../access.js";
import { serverList, state } from "../store.js";
import { busy, field, toast } from "../ui.js";

// Shared with the administrator's Settings page.
export function passwordCard() {
  const current = h("input", { class: "input", type: "password", autocomplete: "current-password" });
  const next = h("input", { class: "input", type: "password", autocomplete: "new-password" });
  return h("div", { class: "card stack" }, h("h2", {}, "Password"),
    h("div", { class: "form-grid" }, field("Current password", current), field("New password", next, "At least 8 characters. Other signed-in devices are signed out.")),
    h("div", {}, h("button", { class: "btn primary sm", onclick: async (e) => {
      await busy(e.currentTarget, async () => {
        await post("/api/auth/password", { current: current.value, new: next.value });
        current.value = next.value = "";
        toast("Password changed", "success");
      });
    } }, icon("key", "sm"), "Change password")));
}

export async function render() {
  const servers = serverList();
  const rows = servers.map((s) => {
    const role = s.access?.role || "player";
    const meaning = ROLES.find((r) => r.id === role)?.summary || "";
    return h("div", { class: "list-item" },
      h("div", { class: "grow" }, h("a", { href: `#/server/${s.id}` }, h("strong", {}, s.name)), h("div", { class: "meta" }, meaning)),
      h("span", { class: `badge ${roleBadgeClass(role)}` }, ROLE_LABEL[role] || role));
  });
  const access = isAdministrator()
    ? h("p", { class: "muted" }, "You are the administrator: you can do everything on every server and manage the other accounts.")
    : h("div", { class: "list" }, rows.length ? rows : h("p", { class: "muted" }, "No servers have been assigned to you yet. Ask the administrator to give you access."));
  return {
    el: h("div", { class: "stack" },
      h("div", { class: "page-head" }, h("div", { class: "titles" }, h("h1", {}, "Account"), h("p", {}, `Signed in as ${state.user.username}.`))),
      h("div", { class: "grid cols-2" },
        h("div", { class: "card stack" }, h("h2", {}, isAdministrator() ? "Access" : "Your servers"), access),
        passwordCard())),
  };
}
