// "New server": which kind? A Pumpkin server (fast, light, no mods), a Vanilla, Paper or Fabric server, GT New Horizons, or a Java modpack server.

import { h, icon } from "../dom.js";
import { modal } from "../ui.js";
import { openCreateGtnhServer } from "./create-gtnh.js";
import { openCreateModpackServer } from "./create-modpack.js";
import { openCreateSoftwareServer } from "./create-software.js";
import { openCreateServer } from "./create.js";

export function openNewServer() {
  const choose = (open) => () => { dlg.close(); open(); };
  const dlg = modal({
    title: "New server",
    body: h("div", { class: "option-cards kinds" },
      h("button", { type: "button", class: "option-card", onclick: choose(openCreateServer) },
        h("span", { class: "name" }, icon("server"), "Pumpkin server", h("span", { class: "badge success" }, "light")),
        h("span", { class: "desc" }, "Minecraft written in Rust. Starts in seconds and needs very little memory. Vanilla gameplay: no mods or plugins.")),
      h("button", { type: "button", class: "option-card", onclick: choose(openCreateSoftwareServer) },
        h("span", { class: "name" }, icon("layers"), "Vanilla, Paper or Fabric server", h("span", { class: "badge accent" }, "Java")),
        h("span", { class: "desc" }, "Java Edition without a modpack: Mojang's own server, Paper for plugins, or Fabric for mods. The manager downloads the software and the Java it needs, and keeps it up to date.")),
      h("button", { type: "button", class: "option-card", onclick: choose(openCreateGtnhServer) },
        h("span", { class: "name" }, icon("layers"), "GT New Horizons", h("span", { class: "badge accent" }, "Java")),
        h("span", { class: "desc" }, "The big GregTech expert pack for Minecraft 1.7.10, installed from the pack's official server files (its CurseForge page only lists it). Needs at least 6 GB of memory.")),
      h("button", { type: "button", class: "option-card", onclick: choose(openCreateModpackServer) },
        h("span", { class: "name" }, icon("layers"), "Modpack server", h("span", { class: "badge accent" }, "Java")),
        h("span", { class: "desc" }, "Java Edition with mods. Pick a modpack from Modrinth or CurseForge, or upload one: Java, the mod loader and the mods are set up for you. Needs several GB of memory."))),
    footer: [h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel")],
  });
}
