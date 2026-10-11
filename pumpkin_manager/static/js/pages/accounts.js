// Accounts for friends: who may sign in, which servers they may use and in which role. Administrator only.

import { del, get, post, put } from "../api.js";
import { clear, copyText, fmtRelative, h, icon } from "../dom.js";
import { ROLES, ROLE_LABEL, roleBadgeClass } from "../access.js";
import { serverList } from "../store.js";
import { confirmDialog, emptyState, field, modal, notifyError, selectControl, toast } from "../ui.js";

function roleLegend() {
  return h("div", { class: "role-legend" }, ROLES.map((r) => h("div", {}, h("span", { class: `badge ${r.badge}` }, r.label), " ", r.summary)));
}

// One row per server: "No access" or one of the roles.
function accessPicker(current = {}) {
  const chosen = { ...current };
  const servers = serverList();
  const options = [{ value: "", label: "No access" }, ...ROLES.map((r) => ({ value: r.id, label: r.label }))];
  const rows = servers.flatMap((s) => {
    const select = selectControl(options, chosen[s.id] || "", (value) => { if (value) chosen[s.id] = value; else delete chosen[s.id]; });
    select.setAttribute("aria-label", `Role on ${s.name}`);
    return [h("div", { class: "truncate" }, h("strong", {}, s.name)), select];
  });
  const el = servers.length
    ? h("div", { class: "access-rows" }, rows)
    : h("p", { class: "muted" }, "There are no servers yet. Create one first, then give your friends access to it.");
  return { el, value: () => ({ ...chosen }) };
}

// The password is only ever shown here, once: the manager keeps a hash and cannot show it again.
function showTemporaryPassword({ username, password, created }) {
  const details = `Sign in at ${location.origin}\nUsername: ${username}\nTemporary password: ${password}`;
  const copy = (text, done) => async () => { if (await copyText(text)) toast(done, "success", null, 1800); };
  const dlg = modal({
    title: created ? `Account created for ${username}` : `New temporary password for ${username}`,
    dismissable: false,
    body: h("div", { class: "stack" },
      h("div", { class: "notice warning" }, icon("alert", "sm"),
        h("div", {}, `Hand this password over now. It is shown only once and cannot be looked up later (if it gets lost, reset the password). ${username} chooses their own password when signing in for the first time.`)),
      h("div", { class: "stack-sm" }, h("div", { class: "muted small" }, "Temporary password"), h("div", { class: "secret" }, password)),
      h("dl", { class: "kv" }, h("dt", {}, "Username"), h("dd", { class: "mono" }, username), h("dt", {}, "Sign in at"), h("dd", { class: "mono" }, location.origin))),
    footer: [
      h("button", { class: "btn", onclick: copy(password, "Password copied") }, icon("copy", "sm"), "Copy password"),
      h("button", { class: "btn", onclick: copy(details, "Sign-in details copied") }, icon("copy", "sm"), "Copy sign-in details"),
      h("button", { class: "btn primary", onclick: () => dlg.close() }, "Done"),
    ],
  });
}

function openEditor(user, done) {
  const picker = accessPicker(user?.servers);
  const name = h("input", { class: "input", maxlength: "32", autocomplete: "off", spellcheck: "false", placeholder: "for example alex_k", "aria-label": "Username" });
  const error = h("div", { class: "notice danger hidden", role: "alert" });
  const save = h("button", { class: "btn primary" }, user ? "Save access" : "Create account");
  const dlg = modal({
    title: user ? `Access for ${user.username}` : "New account", size: "wide",
    body: h("div", { class: "stack" },
      user ? null : field("Username", name, "3-32 characters: letters, digits, '.', '_' or '-'. This is what they sign in with."),
      h("div", {}, h("div", { class: "section-title" }, "Servers and roles"), picker.el),
      roleLegend(),
      user ? null : h("p", { class: "hint" }, "A temporary password is generated for the account and shown to you once, to hand over. Your friend has to choose their own password when they first sign in."),
      error),
    footer: [h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel"), save],
  });
  save.addEventListener("click", async () => {
    error.classList.add("hidden");
    save.disabled = true;
    try {
      if (user) {
        await put(`/api/users/${user.id}/servers`, { servers: picker.value() });
        dlg.close();
        toast("Access updated", "success");
        await done();
      } else {
        const result = await post("/api/users", { username: name.value.trim(), servers: picker.value() });
        dlg.close();
        await done();
        showTemporaryPassword({ username: result.user.username, password: result.temporary_password, created: true });
      }
    } catch (err) {
      error.textContent = err.message;
      error.classList.remove("hidden");
    } finally {
      save.disabled = false;
    }
  });
  if (!user) name.focus();
}

export async function render() {
  let users = [];
  const listHost = h("div", {});

  async function reload() {
    try { users = await get("/api/users"); } catch (err) { return notifyError(err); }
    draw();
  }

  async function resetPassword(user) {
    const ok = await confirmDialog({
      title: `Reset the password of ${user.username}?`, confirmLabel: "Reset password",
      message: "They are signed out everywhere and get a new temporary password. They have to choose their own again when signing in.",
    });
    if (!ok) return;
    try {
      const result = await post(`/api/users/${user.id}/reset-password`);
      await reload();
      showTemporaryPassword({ username: user.username, password: result.temporary_password, created: false });
    } catch (err) { notifyError(err); }
  }

  async function remove(user) {
    const ok = await confirmDialog({
      title: `Delete the account ${user.username}?`, confirmLabel: "Delete account", danger: true,
      message: "They are signed out and can no longer sign in. The servers and their worlds are not touched.",
    });
    if (!ok) return;
    try { await del(`/api/users/${user.id}`); toast("Account deleted", "success"); await reload(); } catch (err) { notifyError(err); }
  }

  function draw() {
    clear(listHost);
    const names = new Map(serverList().map((s) => [s.id, s.name]));
    if (users.every((u) => u.kind === "admin")) {
      listHost.appendChild(h("div", { class: "card" }, emptyState("users", "No accounts for friends yet",
        "Create one to let a friend look after a server with you. You choose which servers they get and what they may do on each.",
        h("button", { class: "btn primary", onclick: () => openEditor(null, reload) }, icon("plus", "sm"), "New account"))));
      return;
    }
    listHost.appendChild(h("div", { class: "card tight" }, h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Account"), h("th", {}, "Servers and roles"), h("th", {}, "Last sign-in"), h("th", {}, ""))),
      h("tbody", {}, users.map((u) => {
        const owner = u.kind === "admin";
        const chips = Object.entries(u.servers).map(([sid, role]) =>
          h("span", { class: `badge ${roleBadgeClass(role)}`, style: { marginRight: "6px" } }, `${names.get(sid) || sid} · ${ROLE_LABEL[role] || role}`));
        return h("tr", {},
          h("td", {}, h("strong", {}, u.username), " ",
            owner ? h("span", { class: "badge accent" }, "Administrator") : null,
            u.must_change_password ? h("span", { class: "badge warning", title: "They have not chosen their own password yet" }, "temporary password") : null),
          h("td", {}, owner ? h("span", { class: "muted" }, "Every server, full control") : (chips.length ? chips : h("span", { class: "muted" }, "No servers"))),
          h("td", { class: "muted small nowrap" }, u.last_login_at ? fmtRelative(u.last_login_at) : "never signed in"),
          h("td", { class: "actions-cell" }, owner ? null : h("div", { class: "row", style: { justifyContent: "flex-end" } },
            h("button", { class: "btn sm", onclick: () => openEditor(u, reload) }, icon("edit", "sm"), "Access"),
            h("button", { class: "btn sm", onclick: () => resetPassword(u) }, icon("key", "sm"), "New password"),
            h("button", { class: "btn ghost sm icon-only", title: "Delete account", "aria-label": `Delete ${u.username}`, onclick: () => remove(u) }, icon("trash", "sm")))));
      }))))));
  }

  await reload();
  return {
    el: h("div", { class: "stack" },
      h("div", { class: "page-head" },
        h("div", { class: "titles" }, h("h1", {}, "Accounts"),
          h("p", {}, "Give friends their own sign-in. They only see the servers you assign them, and what they may do there depends on their role.")),
        h("div", { class: "actions" }, h("button", { class: "btn primary", onclick: () => openEditor(null, reload) }, icon("plus", "sm"), "New account"))),
      listHost,
      h("div", { class: "card stack" }, h("h2", {}, "What the roles mean"), roleLegend(),
        h("p", { class: "muted small" }, "A role is given per server, so a friend can be an Admin on one server and just a Player on another. Only you, the administrator, can create servers, delete them, change global settings and manage accounts."))),
  };
}
