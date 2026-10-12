// What the dashboard knows about the server software the manager installs itself (Vanilla, Paper, Fabric): how to describe it,
// and the notices a person should read before choosing a version. Shared by the "new server" dialog and the Version tab.

import { fmtDate, h, icon } from "./dom.js";

export const SOFTWARE = [
  { id: "paper", name: "Paper", tag: "Recommended", desc: "Fast and widely used. Runs plugins (the Bukkit and Spigot ones) and fixes many problems of the vanilla server. A good choice for most servers." },
  { id: "vanilla", name: "Vanilla", desc: "Mojang's own server, exactly as it ships. No plugins and no mods." },
  { id: "fabric", name: "Fabric", desc: "A light mod loader. Put mods into the mods folder; Fabric API, which most of them need, is installed for you." },
];
export const SOFTWARE_NAMES = Object.fromEntries(SOFTWARE.map((s) => [s.id, s.name]));
// "build" for Paper, "loader" for Fabric: the part of a version that is not the Minecraft version (Vanilla has none)
export const BUILD_WORD = { paper: "build", fabric: "loader" };
// The folder where a person puts what extends the server
export const EXTRAS = { paper: { word: "plugins", folder: "plugins" }, fabric: { word: "mods", folder: "mods" } };

export const UPDATE_MODES = {
  paper: [
    { value: "auto", label: "Install new builds automatically (backup first)" }, { value: "notify", label: "Tell me, I will decide" }, { value: "off", label: "Never check" },
  ],
  fabric: [
    { value: "auto", label: "Install new loader versions automatically (backup first)" }, { value: "notify", label: "Tell me, I will decide" }, { value: "off", label: "Never check" },
  ],
  vanilla: [{ value: "notify", label: "Tell me when a new version is out" }, { value: "off", label: "Never check" }],
};
export const DEFAULT_UPDATE_MODE = { paper: "auto", fabric: "notify", vanilla: "notify" };

export const versionOption = (v, recommended) => `${v.id}${v.type === "snapshot" ? " (snapshot)" : ""}${v.id === recommended ? " · recommended" : ""}`;

// The entries of a select for the builds of one Minecraft version: the newest stable one by default, then every build that is listed.
export function buildChoices(kind, options) {
  const word = BUILD_WORD[kind];
  const newest = options.builds.find((b) => b.build === options.default);
  return [
    { value: "", label: `Newest ${options.stable_only ? "stable " : ""}${word}${newest ? ` (${newest.label})` : ""}` },
    ...options.builds.map((b) => ({ value: b.build, label: `${b.label}${b.stable ? "" : ` · ${b.channel}`}${b.published ? ` · ${fmtDate(b.published)}` : ""}` })),
  ];
}

// What the maker says about a Minecraft version, as notices to show next to the choice.
export function supportNotices(kind, options) {
  const notices = [];
  if (kind === "paper" && options.support?.status === "UNSUPPORTED") {
    notices.push(h("div", { class: "notice warning" }, icon("alert", "sm"),
      `PaperMC no longer maintains this Minecraft version${options.support.end ? ` (since ${fmtDate(options.support.end)})` : ""}. It still runs, but gets no more fixes. A newer version is the safer choice.`));
  }
  if (kind !== "vanilla" && options.builds.length && !options.stable_only) {
    notices.push(h("div", { class: "notice warning" }, icon("alert", "sm"),
      `There is no stable ${kind === "paper" ? "Paper build" : "loader version"} for this Minecraft version yet. The newest one is experimental; ${kind === "paper" ? "PaperMC advises against running those on a server that matters" : "expect problems"}.`));
  }
  return notices;
}
