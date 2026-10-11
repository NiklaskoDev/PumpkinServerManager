import { h } from "../dom.js";
import { backupsPanel } from "../tabs/backups.js";

export function render() {
  const panel = backupsPanel({});
  return {
    el: h("div", { class: "stack" },
      h("div", { class: "page-head" }, h("div", { class: "titles" }, h("h1", {}, "Backups"),
        h("p", {}, "Every backup of every server. Open a server to create backups or change its schedule."))),
      panel.el),
    dispose: panel.dispose,
  };
}
