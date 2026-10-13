// "New server": which kind? A Pumpkin server (fast, light, no mods), a Vanilla, Paper or Fabric server, or a Java modpack server.
// The large expert modpacks that come with server files of their own (GT New Horizons) are listed together below them.

import { h, icon } from "../dom.js";
import { modal } from "../ui.js";
import { openCreateGtnhServer } from "./create-gtnh.js";
import { openCreateModpackServer } from "./create-modpack.js";
import { openCreateSoftwareServer } from "./create-software.js";
import { openCreateServer } from "./create.js";

export function openNewServer() {
  const choose = (open) => () => { dlg.close(); open(); };
  const card = (open, glyph, name, badge, desc) => h("button", { type: "button", class: "option-card", onclick: choose(open) },
    h("span", { class: "name" }, icon(glyph), name, badge),
    h("span", { class: "desc" }, desc));

  const dlg = modal({
    title: "New server",
    body: h("div", { class: "stack" },
      h("div", { class: "option-cards kinds" },
        card(openCreateServer, "server", "Pumpkin server", h("span", { class: "badge success" }, "light"),
          "Minecraft written in Rust. Starts in seconds and needs very little memory. Vanilla gameplay: no mods or plugins."),
        card(openCreateSoftwareServer, "layers", "Vanilla, Paper or Fabric server", h("span", { class: "badge accent" }, "Java"),
          "Java Edition without a modpack: Mojang's own server, Paper for plugins, or Fabric for mods. The manager downloads the software and the Java it needs, and keeps it up to date."),
        card(openCreateModpackServer, "layers", "Modpack server", h("span", { class: "badge accent" }, "Java"),
          "Java Edition with mods. Pick a modpack from Modrinth or CurseForge, or upload one: Java, the mod loader and the mods are set up for you. Needs several GB of memory.")),
      // packs of this size are not found by the search of the modpack server: the manager installs them from the pack's own server files
      h("div", { class: "stack-sm" },
        h("div", { class: "section-title" }, "Large expert modpacks"),
        h("p", { class: "hint" }, "Huge packs for hundreds of hours of play. They come with server files of their own, so the manager installs them from there and the modpack search does not list them."),
        h("div", { class: "option-cards kinds" },
          card(openCreateGtnhServer, "layers", "GT New Horizons", h("span", { class: "badge accent" }, "Java"),
            "The big GregTech expert pack for Minecraft 1.7.10, installed from the pack's official server files (its CurseForge page only lists it). Needs at least 6 GB of memory.")))),
    footer: [h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel")],
  });
}
