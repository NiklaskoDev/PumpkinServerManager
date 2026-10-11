// Shown instead of the dashboard to an account that signed in with a temporary password: it has to choose its own first.

import { api } from "../api.js";
import { h, logoSvg } from "../dom.js";

export function renderPasswordChange({ username, current = "", onDone, onSignOut }) {
  const error = h("div", { class: "notice danger hidden", role: "alert" });
  const old = h("input", { class: "input", type: "password", autocomplete: "current-password", value: current, required: true, "aria-label": "Temporary password" });
  const next = h("input", { class: "input", type: "password", autocomplete: "new-password", required: true, minlength: "8", "aria-label": "New password" });
  const again = h("input", { class: "input", type: "password", autocomplete: "new-password", required: true, minlength: "8", "aria-label": "New password again" });
  const button = h("button", { class: "btn primary", type: "submit", style: { width: "100%", minHeight: "42px" } }, "Save and continue");

  const show = (message) => { error.textContent = message; error.classList.remove("hidden"); };

  const form = h("form", { class: "stack", onsubmit: async (event) => {
    event.preventDefault();
    error.classList.add("hidden");
    if (next.value !== again.value) { show("The two new passwords are not the same."); again.select(); return; }
    button.disabled = true;
    button.textContent = "Saving...";
    try {
      await api("POST", "/api/auth/password", { current: old.value, new: next.value });
      await onDone();
    } catch (err) {
      show(err.message);
      (old.value ? next : old).select();
    } finally {
      button.disabled = false;
      button.textContent = "Save and continue";
    }
  } },
    h("div", { class: "notice warning" }, "You signed in with a temporary password. Choose your own now; you will use it from here on."),
    h("label", { class: "field" }, "Temporary password", old),
    h("label", { class: "field" }, "New password", next, h("span", { class: "hint" }, "At least 8 characters, and not your username.")),
    h("label", { class: "field" }, "New password again", again),
    error, button,
    h("button", { class: "btn ghost sm", type: "button", onclick: onSignOut }, "Sign out"));

  setTimeout(() => (old.value ? next : old).focus(), 0);
  return h("div", { class: "login-wrap" },
    h("div", { class: "card login-card" },
      h("div", { class: "brand" }, logoSvg(), h("div", {}, "Choose a new password", h("small", {}, `Signed in as ${username}`))),
      form));
}
