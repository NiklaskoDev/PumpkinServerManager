// "New server": which kind? A Pumpkin server (fast, light, no mods) or a Java modpack server.

import { h, icon } from "../dom.js";
import { modal } from "../ui.js";
import { openCreateModpackServer } from "./create-modpack.js";
import { openCreateServer } from "./create.js";

export function openNewServer() {
  const choose = (open) => () => { dlg.close(); open(); };
  const dlg = modal({
    title: "New server",
    body: h("div", { class: "option-cards kinds" },
      h("button", { type: "button", class: "option-card", onclick: choose(openCreateServer) },
        h("span", { class: "name" }, icon("server"), "Pumpkin server", h("span", { class: "badge success" }, "light")),
        h("span", { class: "desc" }, "Minecraft written in Rust. Starts in seconds and needs very little memory. Vanilla gameplay: no mods or plugins.")),
      h("button", { type: "button", class: "option-card", onclick: choose(openCreateModpackServer) },
        h("span", { class: "name" }, icon("layers"), "Modpack server", h("span", { class: "badge accent" }, "Java")),
        h("span", { class: "desc" }, "Java Edition with mods. Pick a modpack from Modrinth or upload one: Java, the mod loader and the mods are set up for you. Needs several GB of memory."))),
    footer: [h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel")],
  });
}
