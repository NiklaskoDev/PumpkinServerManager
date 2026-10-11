// File manager for the server folder: browse, edit text files, upload, download, rename, delete.

import { api, del, get, post, put, qs, upload } from "../api.js";
import { clear, fmtBytes, fmtRelative, h, icon } from "../dom.js";
import { confirmDialog, modal, notifyError, promptDialog, toast } from "../ui.js";

export function render({ id }) {
  let path = "";
  const base = `/api/servers/${id}/files`;
  const crumbs = h("div", { class: "crumbs" });
  const tableHost = h("div", {});
  const uploadStatus = h("div", { class: "hidden" });
  const fileInput = h("input", { type: "file", multiple: true, class: "sr-only", "aria-label": "Choose files to upload", onchange: () => { uploadFiles([...fileInput.files]); fileInput.value = ""; } });

  const join = (name) => (path ? `${path}/${name}` : name);

  async function load(next = path) {
    let data;
    try {
      data = await get(`${base}${qs({ path: next })}`);
    } catch (err) {
      notifyError(err);
      return;
    }
    path = data.path;
    renderCrumbs();
    renderTable(data.entries);
  }

  function renderCrumbs() {
    clear(crumbs);
    crumbs.appendChild(h("button", { onclick: () => load("") }, icon("server", "sm")));
    const parts = path ? path.split("/") : [];
    parts.forEach((part, i) => {
      crumbs.appendChild(h("span", { class: "sep" }, "/"));
      crumbs.appendChild(h("button", { onclick: () => load(parts.slice(0, i + 1).join("/")) }, part));
    });
  }

  function renderTable(entries) {
    clear(tableHost);
    if (!entries.length) return tableHost.appendChild(h("p", { class: "muted", style: { padding: "20px" } }, "This folder is empty. Drop files here to upload them."));
    tableHost.appendChild(h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Size"), h("th", {}, "Modified"), h("th", {}, ""))),
      h("tbody", {}, entries.map((e) => {
        const isDir = e.type === "dir";
        const open = () => (isDir ? load(e.path) : e.editable ? editFile(e) : download(e));
        return h("tr", {},
          h("td", {}, h("button", { class: `file-name ${isDir ? "dir" : ""}`, onclick: open }, icon(isDir ? "folder" : "file", "sm"), e.name)),
          h("td", { class: "muted nowrap" }, isDir ? "–" : fmtBytes(e.size)),
          h("td", { class: "muted nowrap" }, fmtRelative(new Date(e.modified * 1000).toISOString())),
          h("td", { class: "actions-cell" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
            !isDir && e.editable ? h("button", { class: "btn ghost sm icon-only", title: "Edit", "aria-label": `Edit ${e.name}`, onclick: () => editFile(e) }, icon("edit", "sm")) : null,
            !isDir ? h("button", { class: "btn ghost sm icon-only", title: "Download", "aria-label": `Download ${e.name}`, onclick: () => download(e) }, icon("download", "sm")) : null,
            h("button", { class: "btn ghost sm icon-only", title: "Rename", "aria-label": `Rename ${e.name}`, onclick: () => rename(e) }, icon("more", "sm")),
            h("button", { class: "btn ghost sm icon-only", title: "Delete", "aria-label": `Delete ${e.name}`, onclick: () => remove(e) }, icon("trash", "sm")))));
      })))));
  }

  function download(entry) {
    const a = h("a", { href: `${base}/download${qs({ path: entry.path })}`, download: entry.name });
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function rename(entry) {
    const name = await promptDialog({ title: `Rename ${entry.name}`, label: "New name", value: entry.name, confirmLabel: "Rename" });
    if (!name || name === entry.name) return;
    try { await post(`${base}/rename`, { source: entry.path, destination: join(name) }); load(); } catch (err) { notifyError(err); }
  }

  async function remove(entry) {
    const ok = await confirmDialog({
      title: `Delete ${entry.name}?`, danger: true, confirmLabel: "Delete",
      message: entry.type === "dir" ? "The folder and everything inside it will be deleted. This cannot be undone." : "This file will be deleted. This cannot be undone.",
    });
    if (!ok) return;
    try { await del(`${base}${qs({ path: entry.path })}`); load(); } catch (err) { notifyError(err); }
  }

  async function newFolder() {
    const name = await promptDialog({ title: "New folder", label: "Folder name", confirmLabel: "Create" });
    if (name) try { await post(`${base}/mkdir`, { path: join(name) }); load(); } catch (err) { notifyError(err); }
  }

  async function newFile() {
    const name = await promptDialog({ title: "New file", label: "File name", placeholder: "notes.txt", confirmLabel: "Create" });
    if (name) try { await put(`${base}/content`, { path: join(name), content: "" }); load(); editFile({ path: join(name), name }); } catch (err) { notifyError(err); }
  }

  async function editFile(entry) {
    let file;
    try { file = await get(`${base}/content${qs({ path: entry.path })}`); } catch (err) { return notifyError(err); }
    const area = h("textarea", { class: "input", spellcheck: "false", value: file.content, rows: "22", "aria-label": `Contents of ${entry.name}` });
    let dirty = false;
    area.addEventListener("input", () => { dirty = true; });
    const notes = h("div", {});
    const save = async () => {
      try {
        const result = await put(`${base}/content`, { path: entry.path, content: area.value });
        dirty = false;
        toast(`${entry.name} saved`, "success", null, 2000);
        clear(notes);
        if (result.reset_managed_keys?.length) notes.appendChild(h("div", { class: "notice warning" }, icon("alert", "sm"), `Reset keys owned by the manager: ${result.reset_managed_keys.join(", ")}`));
        if (result.restart_required) notes.appendChild(h("div", { class: "notice" }, icon("info", "sm"), "Restart the server for this change to take effect."));
      } catch (err) {
        clear(notes);
        notes.appendChild(h("div", { class: "notice danger" }, icon("alert", "sm"), err.message));
        if (err.data?.errors) notes.appendChild(h("ul", {}, Object.values(err.data.errors).map((m) => h("li", {}, m))));
      }
    };
    area.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); save(); } });
    const dlg = modal({
      title: entry.path, size: "xl", dismissable: false,
      body: h("div", { class: "stack-sm" }, notes, area, h("p", { class: "hint" }, "Ctrl+S saves. Changes to config files apply after a restart.")),
      footer: [
        h("button", { class: "btn", onclick: async () => { if (!dirty || await confirmDialog({ title: "Discard changes?", message: "You have unsaved changes.", confirmLabel: "Discard", danger: true })) dlg.close(); } }, "Close"),
        h("button", { class: "btn primary", onclick: save }, icon("save", "sm"), "Save"),
      ],
    });
    area.focus();
  }

  async function uploadFiles(files) {
    if (!files.length) return;
    clear(uploadStatus).classList.remove("hidden");
    for (const file of files) {
      const bar = h("span", { style: { width: "0%" } });
      const label = h("div", { class: "small" }, `Uploading ${file.name}`);
      uploadStatus.append(label, h("div", { class: "progress" }, bar));
      try {
        await upload(`${base}/upload${qs({ path, name: file.name })}`, file, (f) => { bar.style.width = `${Math.round(f * 100)}%`; });
      } catch (err) {
        notifyError(err);
      }
    }
    uploadStatus.classList.add("hidden");
    toast(`${files.length} file${files.length === 1 ? "" : "s"} uploaded`, "success");
    load();
  }

  const zone = h("div", { class: "card tight" },
    h("div", { class: "card-section" }, h("div", { class: "row" }, crumbs, h("span", { class: "grow" }),
      h("button", { class: "btn sm", onclick: newFile }, icon("file", "sm"), "New file"),
      h("button", { class: "btn sm", onclick: newFolder }, icon("folderPlus", "sm"), "New folder"),
      h("button", { class: "btn sm primary", onclick: () => fileInput.click() }, icon("upload", "sm"), "Upload"), fileInput)),
    h("div", { class: "card-section" }, uploadStatus), tableHost);
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("over"); uploadFiles([...e.dataTransfer.files]); });

  load("");
  return {
    el: h("div", { class: "stack" },
      h("div", { class: "notice warning" }, icon("alert", "sm"), "Editing files of a running server can be overwritten when it saves. Stop the server before changing world data."),
      zone),
  };
}
