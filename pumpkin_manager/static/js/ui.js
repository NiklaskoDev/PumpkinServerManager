// Shared UI building blocks: toasts, modals, dialogs, badges, form controls and job tracking.

import { get } from "./api.js";
import { clear, fmtRelative, h, icon, plural } from "./dom.js";
import { bus } from "./store.js";

// ------------------------------------------------------------------- toasts
let toastHost = null;

export function toast(message, kind = "info", title = null, timeout = null) {
  if (!toastHost) {
    toastHost = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.appendChild(toastHost);
  }
  const iconName = { success: "check", error: "alert", warning: "alert", info: "info" }[kind] || "info";
  const el = h("div", { class: `toast ${kind}` },
    icon(iconName),
    h("div", { class: "body" }, title ? h("div", { class: "title" }, title) : null, h("div", {}, message)),
    h("button", { class: "btn ghost icon-only sm", "aria-label": "Dismiss", onclick: () => el.remove() }, icon("x", "sm")));
  toastHost.appendChild(el);
  setTimeout(() => el.remove(), timeout ?? (kind === "error" ? 9000 : 5000));
  return el;
}

export const notifyError = (err) => toast(err && err.message ? err.message : String(err), "error");

// ------------------------------------------------------------------- modals
export function modal({ title, body, footer, size = "", onClose = null, dismissable = true }) {
  const previouslyFocused = document.activeElement;
  const bodyEl = h("div", { class: "modal-body" }, body);
  const footEl = footer ? h("div", { class: "modal-foot" }, footer) : null;
  const closeBtn = dismissable ? h("button", { class: "btn ghost icon-only", "aria-label": "Close", onclick: () => close() }, icon("x")) : null;
  const dialog = h("div", { class: `modal ${size}`, role: "dialog", "aria-modal": "true", "aria-label": title },
    h("div", { class: "modal-head" }, h("h2", {}, title), closeBtn), bodyEl, footEl);
  const wrap = h("div", { class: "modal-wrap" }, dialog);
  let closed = false;

  function close(result) {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    wrap.remove();
    if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
    if (onClose) onClose(result);
  }
  function onKey(event) {
    if (event.key === "Escape" && dismissable) { event.stopPropagation(); close(); }
  }
  wrap.addEventListener("mousedown", (event) => { if (event.target === wrap && dismissable) close(); });
  document.addEventListener("keydown", onKey, true);
  document.body.appendChild(wrap);
  const first = dialog.querySelector("input:not([type=hidden]), select, textarea, button.primary");
  if (first) first.focus();
  return {
    el: dialog,
    close,
    body: bodyEl,
    footer: footEl,
    setFooter(...children) { if (footEl) { clear(footEl); children.forEach((c) => footEl.appendChild(c)); } },
  };
}

// Resolves true/false. With `confirmText` the user must type that text before the button enables.
export function confirmDialog({ title, message, confirmLabel = "Confirm", danger = false, confirmText = null, extra = null }) {
  return new Promise((resolve) => {
    let input = null;
    const confirmBtn = h("button", { class: `btn ${danger ? "danger solid" : "primary"}`, disabled: !!confirmText, onclick: () => { dlg.close(true); } }, confirmLabel);
    if (confirmText) {
      input = h("input", { class: "input", placeholder: confirmText, "aria-label": "Confirmation", autocomplete: "off", spellcheck: "false" });
      input.addEventListener("input", () => { confirmBtn.disabled = input.value.trim() !== confirmText; });
      // preventDefault: closing restores focus to the opener, and the pending keypress would click it again
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); if (!confirmBtn.disabled) dlg.close(true); } });
    }
    const dlg = modal({
      title,
      body: h("div", { class: "stack" },
        typeof message === "string" ? h("p", {}, message) : message,
        extra,
        confirmText ? h("label", { class: "field" }, h("span", {}, "Type ", h("code", {}, confirmText), " to confirm"), input) : null),
      footer: [h("button", { class: "btn", onclick: () => dlg.close(false) }, "Cancel"), confirmBtn],
      onClose: (result) => resolve(result === true),
    });
    if (input) input.focus();
  });
}

export function promptDialog({ title, label, value = "", placeholder = "", confirmLabel = "Save", hint = null }) {
  return new Promise((resolve) => {
    const input = h("input", { class: "input", value, placeholder, autocomplete: "off", spellcheck: "false" });
    const submit = () => dlg.close(input.value);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    const dlg = modal({
      title,
      body: h("label", { class: "field" }, label, input, hint ? h("span", { class: "hint" }, hint) : null),
      footer: [h("button", { class: "btn", onclick: () => dlg.close(null) }, "Cancel"), h("button", { class: "btn primary", onclick: submit }, confirmLabel)],
      onClose: (result) => resolve(typeof result === "string" ? result : null),
    });
    input.focus();
    input.select();
  });
}

// ---------------------------------------------------------------- form bits
export function field(label, control, hint = null, error = null) {
  return h("label", { class: "field" }, h("span", {}, label), control, hint ? h("span", { class: "hint" }, hint) : null,
    error ? h("span", { class: "field-error" }, error) : null);
}

export function switchControl({ label, hint, checked = false, onChange, disabled = false }) {
  const input = h("input", { type: "checkbox", checked, disabled });
  if (onChange) input.addEventListener("change", () => onChange(input.checked));
  const el = h("label", { class: "switch" }, input, h("span", { class: "track" }),
    h("span", { class: "text" }, label, hint ? h("span", { class: "hint" }, hint) : null));
  el.input = input;
  return el;
}

export function selectControl(options, value, onChange) {
  const select = h("select", { class: "input" },
    options.map((o) => h("option", { value: o.value, selected: o.value === value }, o.label)));
  select.value = value;
  if (onChange) select.addEventListener("change", () => onChange(select.value));
  return select;
}

export function segmented(options, value, onChange) {
  const wrap = h("div", { class: "segmented", role: "group" });
  const buttons = options.map((o) => {
    const b = h("button", { type: "button", "aria-pressed": String(o.value === value), onclick: () => select(o.value) }, o.label);
    b.dataset.value = o.value;
    return b;
  });
  function select(v) {
    value = v;
    buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.value === v)));
    onChange(v);
  }
  buttons.forEach((b) => wrap.appendChild(b));
  return wrap;
}

// Runs an async action while showing a spinner on `button`; errors become toasts.
export async function busy(button, fn) {
  const original = [...button.childNodes];
  button.disabled = true;
  clear(button);
  button.appendChild(h("span", { class: "spinner" }));
  try {
    return await fn();
  } catch (err) {
    notifyError(err);
    return undefined;
  } finally {
    clear(button);
    original.forEach((n) => button.appendChild(n));
    button.disabled = false;
  }
}

// --------------------------------------------------------------- status UI
const BUSY_LABELS = {
  create: "Creating", delete: "Deleting", backup: "Backing up", restore: "Restoring", update: "Updating",
  rollback: "Rolling back", start: "Starting", stop: "Stopping", restart: "Restarting", download: "Downloading",
  operator: "Setting level",
};

export function statusBadge(server) {
  if (server.busy) return h("span", { class: "badge busy pulse" }, h("span", { class: "dot" }), BUSY_LABELS[server.busy] || "Working");
  const state = server.state;
  const label = { running: "Running", starting: "Starting", stopping: "Stopping", stopped: "Stopped", crashed: "Crashed" }[state] || state;
  const pulse = state === "starting" || state === "stopping" ? " pulse" : "";
  return h("span", { class: `badge ${state}${pulse}` }, h("span", { class: "dot" }), label);
}

export function channelChip(channel) {
  return h("span", { class: "chip", title: { stable: "Tagged releases", nightly: "Rolling nightly build", canary: "Rolling canary build (newest)" }[channel] }, channel);
}

export function versionText(build) {
  if (!build) return "no version";
  return build.label || build.tag;
}

// What an update banner names: the Pumpkin build, or the modpack version.
export function pendingText(server) {
  if (server.engine === "java") {
    const pending = server.update?.pending_pack;
    return pending ? `${server.modpack?.title || "modpack"} ${pending.number}` : "";
  }
  return versionText(server.update?.pending_build);
}

export const fmtMemoryMb = (mb) => (mb % 1024 === 0 ? `${mb / 1024} GiB` : `${mb} MiB`);

// The chips under a server's name: a Pumpkin server shows its build and channel, a modpack server the pack and its loader.
export function serverChips(server) {
  if (server.engine === "java") {
    const pack = server.modpack || {};
    return [
      h("span", { class: "chip", title: "Modpack" }, icon("layers", "sm"), [pack.title, pack.version_number].filter(Boolean).join(" ") || "Modpack"),
      pack.loader?.label ? h("span", { class: "chip", title: "Mod loader" }, pack.loader.label) : null,
      pack.minecraft ? h("span", { class: "chip" }, `Minecraft ${pack.minecraft}`) : null,
      server.java ? h("span", { class: "chip", title: `Java ${server.java.major}, ${fmtMemoryMb(server.java.memory_mb)} of memory` }, icon("cpu", "sm"), `Java ${server.java.major}`) : null,
    ];
  }
  return [
    h("span", { class: "chip" }, icon("layers", "sm"), versionText(server.build)),
    channelChip(server.channel),
    server.pinned_tag ? h("span", { class: "chip", title: "Pinned: automatic updates are off" }, icon("shield", "sm"), "pinned") : null,
    server.ports?.bedrock ? h("span", { class: "chip", title: `Bedrock UDP port ${server.ports.bedrock}` }, "Bedrock") : null,
  ];
}

// A modpack's icon. It comes through the manager (the dashboard may not contact other sites) and falls back to a plain symbol.
export function packIcon(url, size = 40) {
  const fallback = () => h("span", { class: "pack-icon pack-icon-empty", style: { width: `${size}px`, height: `${size}px` } }, icon("layers"));
  if (!url) return fallback();
  const img = h("img", { class: "pack-icon", src: `/api/modpacks/icon?url=${encodeURIComponent(url)}`, alt: "", width: String(size), height: String(size), loading: "lazy" });
  img.addEventListener("error", () => img.replaceWith(fallback()));
  return img;
}

export function levelIcon(level) {
  const name = { success: "check", warning: "alert", error: "alert", info: "info" }[level] || "info";
  return h("span", { class: `ico ${level}` }, icon(name, "sm"));
}

export function emptyState(iconName, title, text, action) {
  return h("div", { class: "empty" }, icon(iconName, "big-icon"), h("h2", {}, title), text ? h("p", {}, text) : null, action || null);
}

export function activityItem(a, servers) {
  const server = a.server_id ? servers.get(a.server_id) : null;
  return h("div", { class: "list-item" }, levelIcon(a.level),
    h("div", { class: "grow" }, h("div", {}, a.message),
      h("div", { class: "meta" }, fmtRelative(a.ts), server ? ` · ${server.name}` : "")));
}

export function sparkline(values, max) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", "0 0 100 40");
  svg.setAttribute("preserveAspectRatio", "none");
  const area = document.createElementNS(NS, "path");
  area.setAttribute("class", "area");
  const line = document.createElementNS(NS, "path");
  line.setAttribute("class", "line");
  svg.append(area, line);
  svg.update = (vals, maximum) => {
    const data = vals.length > 1 ? vals : [0, ...vals, 0];
    const top = Math.max(maximum || 0, Math.max(...data) * 1.25, 1); // headroom so a flat series is not drawn at the very top
    const pts = data.map((v, i) => [(i / (data.length - 1)) * 100, 38 - (v / top) * 34]);
    const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`).join(" ");
    line.setAttribute("d", d);
    area.setAttribute("d", `${d} L100 40 L0 40 Z`);
  };
  svg.update(values, max);
  return svg;
}

// ------------------------------------------------------------ job tracking
// Jobs this browser tab started and reports on itself (the global notifier skips them).
export const trackedJobs = new Set();

// Resolves with the final job row. Uses live events and a slow poll as a safety net.
export function trackJob(jobId, onUpdate) {
  trackedJobs.add(jobId);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (job) => { if (settled) return; settled = true; off(); clearInterval(poll); resolve(job); };
    const handle = (job) => { if (onUpdate) onUpdate(job); if (job.status !== "running") finish(job); };
    const off = bus.on("job", ({ job }) => { if (job.id === jobId) handle(job); });
    const poll = setInterval(async () => {
      try { handle(await get(`/api/jobs/${jobId}`)); } catch { /* keep trying */ }
    }, 2500);
    get(`/api/jobs/${jobId}`).then(handle).catch(() => {});
  });
}

// Shows a progress dialog for a job started by an API call. Returns the final job row.
export async function jobDialog(startPromise, { title, successToast = null, closeOnSuccess = true }) {
  let response;
  try {
    response = await startPromise;
  } catch (err) {
    notifyError(err);
    return null;
  }
  const initial = response.job;
  const bar = h("span", { style: { width: "4%" } });
  const status = h("div", { class: "muted" }, initial.message || "Starting...");
  const steps = h("div", { class: "job-steps" });
  const closeBtn = h("button", { class: "btn primary", disabled: true }, "Working...");
  const dlg = modal({
    title, dismissable: false, size: "wide",
    body: h("div", { class: "stack" }, status, h("div", { class: "progress" }, bar), steps),
    footer: [closeBtn],
  });
  closeBtn.addEventListener("click", () => dlg.close());

  const render = (job) => {
    status.textContent = job.message || "Working...";
    if (typeof job.progress === "number") bar.style.width = `${Math.max(4, Math.round(job.progress * 100))}%`;
    clear(steps);
    for (const s of job.steps || []) {
      steps.appendChild(h("div", { class: `s ${s.level}` }, h("time", {}, new Date(s.ts).toLocaleTimeString()), h("span", {}, s.text)));
    }
    steps.scrollTop = steps.scrollHeight;
  };
  render(initial);
  const final = await trackJob(initial.id, render);
  render(final);
  bar.style.width = "100%";
  if (final.status === "success") {
    if (successToast) toast(successToast, "success");
    if (closeOnSuccess) { dlg.close(); return final; }
    closeBtn.textContent = "Done";
  } else {
    closeBtn.textContent = "Close";
    status.textContent = final.message || "The operation failed.";
    status.classList.remove("muted");
  }
  closeBtn.disabled = false;
  closeBtn.focus();
  await new Promise((resolve) => { closeBtn.addEventListener("click", resolve, { once: true }); });
  return final;
}

// Starts a job and only toasts the outcome (for quick actions like start/stop).
export async function quickJob(startPromise, { success, failure }) {
  let response;
  try {
    response = await startPromise;
  } catch (err) {
    notifyError(err);
    return null;
  }
  const final = await trackJob(response.job.id);
  if (final.status === "success") { if (success) toast(success, "success"); }
  else toast(final.message || failure || "The operation failed.", "error", failure || null);
  return final;
}

export { plural };
