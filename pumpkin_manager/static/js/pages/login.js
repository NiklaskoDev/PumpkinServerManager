import { api } from "../api.js";
import { h, logoSvg } from "../dom.js";

const LAST_USER_KEY = "pumpkin-manager-last-user";
function lastUser() { try { return localStorage.getItem(LAST_USER_KEY) || "admin"; } catch { return "admin"; } }
function rememberUser(name) { try { localStorage.setItem(LAST_USER_KEY, name); } catch { /* private mode */ } }

// onSuccess(session, password): the password is handed on so a temporary one need not be typed twice.
export function renderLogin(onSuccess) {
  const error = h("div", { class: "notice danger hidden", role: "alert" });
  const user = h("input", { class: "input", name: "username", autocomplete: "username", value: lastUser(), required: true, "aria-label": "Username" });
  const pass = h("input", { class: "input", name: "password", type: "password", autocomplete: "current-password", required: true, "aria-label": "Password" });
  const button = h("button", { class: "btn primary", type: "submit", style: { width: "100%", minHeight: "42px" } }, "Sign in");

  const form = h("form", { class: "stack", onsubmit: async (event) => {
    event.preventDefault();
    error.classList.add("hidden");
    button.disabled = true;
    button.textContent = "Signing in...";
    try {
      const session = await api("POST", "/api/auth/login", { username: user.value, password: pass.value }, { allowUnauthorized: true });
      rememberUser(user.value.trim());
      await onSuccess(session, pass.value);
    } catch (err) {
      error.textContent = err.message;
      error.classList.remove("hidden");
      pass.select();
    } finally {
      button.disabled = false;
      button.textContent = "Sign in";
    }
  } },
    h("label", { class: "field" }, "Username", user),
    h("label", { class: "field" }, "Password", pass),
    error, button);

  const wrap = h("div", { class: "login-wrap" },
    h("div", { class: "card login-card" },
      h("div", { class: "brand" }, logoSvg(), h("div", {}, "Pumpkin Manager", h("small", {}, "Minecraft server control panel"))),
      form,
      h("p", { class: "hint", style: { textAlign: "center" } }, "Forgot the password? Run ", h("code", {}, "python -m pumpkin_manager reset-password"), " inside the container.")));
  setTimeout(() => (user.value ? pass : user).focus(), 0);
  return wrap;
}
