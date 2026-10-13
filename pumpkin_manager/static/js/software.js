// What the dashboard knows about the server software the manager installs itself (Vanilla, Paper, Fabric, Spigot, BungeeCord): how to
// describe it, and the notices a person should read before choosing a version. Shared by the "new server" dialog and the Version tab.

import { fmtDate, h, icon } from "./dom.js";

// `proxy`: software that sits in front of other servers (no world, no Minecraft version, no EULA). `note` is what a person should know
// before choosing it; the dialog shows it under the choice.
export const SOFTWARE = [
  { id: "paper", name: "Paper", tag: "Recommended", desc: "Fast and widely used. Runs plugins (the Bukkit and Spigot ones) and fixes many problems of the vanilla server. A good choice for most servers." },
  { id: "vanilla", name: "Vanilla", desc: "Mojang's own server, exactly as it ships. No plugins and no mods." },
  { id: "fabric", name: "Fabric", desc: "A light mod loader. Put mods into the mods folder; Fabric API, which most of them need, is installed for you." },
  {
    id: "spigot", name: "Spigot", desc: "The original plugin server (Bukkit and Spigot plugins). SpigotMC does not publish the server to download: the manager builds it for you with their BuildTools.",
    note: "Spigot is built, not downloaded: SpigotMC's BuildTools makes it on this machine from SpigotMC's sources and Mojang's server. That takes five to fifteen minutes, needs the internet and about 2 GB of free disk space while it runs (a Java development kit of about 200 MB is downloaded once). Nothing stays behind except the finished jar. A newer build later takes another run.",
  },
  {
    id: "bungeecord", name: "BungeeCord", proxy: true, desc: "A proxy that joins several servers into one network: players connect to it and it passes them on to your other servers. It has no world and needs very little memory.",
    note: "A proxy has no game of its own: players connect to it, and it passes them on to your servers. It is set up with the servers this manager runs (see config.yml in the Files tab to change that). Those servers have to run in offline mode (online-mode=false) with proxy support turned on, and nobody must be able to reach them except through the proxy: otherwise anybody can join as any player.",
  },
];
export const SOFTWARE_NAMES = Object.fromEntries(SOFTWARE.map((s) => [s.id, s.name]));
export const isProxy = (kind) => !!SOFTWARE.find((s) => s.id === kind)?.proxy;
// "build" for Paper, Spigot and BungeeCord, "loader" for Fabric: the part of a version that is not the Minecraft version (Vanilla has none)
export const BUILD_WORD = { paper: "build", fabric: "loader", spigot: "build", bungeecord: "build" };
export const buildName = (kind) => (kind === "fabric" ? "Fabric loader version" : `${SOFTWARE_NAMES[kind] || "Software"} build`);
// The folder where a person puts what extends the server
export const EXTRAS = {
  paper: { word: "plugins", folder: "plugins" }, fabric: { word: "mods", folder: "mods" },
  spigot: { word: "plugins", folder: "plugins" }, bungeecord: { word: "plugins", folder: "plugins" },
};

export const UPDATE_MODES = {
  paper: [
    { value: "auto", label: "Install new builds automatically (backup first)" }, { value: "notify", label: "Tell me, I will decide" }, { value: "off", label: "Never check" },
  ],
  fabric: [
    { value: "auto", label: "Install new loader versions automatically (backup first)" }, { value: "notify", label: "Tell me, I will decide" }, { value: "off", label: "Never check" },
  ],
  vanilla: [{ value: "notify", label: "Tell me when a new version is out" }, { value: "off", label: "Never check" }],
  spigot: [
    { value: "notify", label: "Tell me, I will decide" }, { value: "auto", label: "Build new builds automatically (backup first)" }, { value: "off", label: "Never check" },
  ],
  bungeecord: [
    { value: "notify", label: "Tell me, I will decide" }, { value: "auto", label: "Install new builds automatically (backup first)" }, { value: "off", label: "Never check" },
  ],
};
export const DEFAULT_UPDATE_MODE = { paper: "auto", fabric: "notify", vanilla: "notify", spigot: "notify", bungeecord: "notify" };
// what is said about new versions next to the choice of how to deal with them
export const UPDATE_HINT = {
  vanilla: "A new Minecraft version changes the world, so it is never installed without you.",
  spigot: "A new Minecraft version is never installed by itself: it can break plugins. A new build of the same version takes as long to make as the first one.",
  bungeecord: "BungeeCord is updated in small steps, and many of them only change libraries. Every update is backed up first and undone by itself if the proxy does not start.",
};
export const updateHint = (kind) => UPDATE_HINT[kind] || "A new Minecraft version is never installed by itself: it can break plugins or mods.";

export const versionOption = (v, recommended) => `${v.id}${v.type === "snapshot" ? " (snapshot)" : ""}${v.id === recommended ? " · recommended" : ""}`;

// The entries of a select for the builds of one Minecraft version: the newest stable one by default, then every build that is listed.
export function buildChoices(kind, options) {
  const word = BUILD_WORD[kind];
  const newest = options.builds.find((b) => b.build === options.default);
  return [
    { value: "", label: `Newest ${options.stable_only && kind !== "bungeecord" ? "stable " : ""}${word}${newest ? ` (${newest.label})` : ""}` }, // BungeeCord has no "stable"
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
