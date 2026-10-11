// Roles and permissions, as the dashboard shows them. The server decides what is allowed; this only decides what to offer.
// The permission names mirror pumpkin_manager/access.py (tests/test_packaging.py checks that the two stay in step).

import { state } from "./store.js";

export const PERM = {
  VIEW: "view",
  CONSOLE_VIEW: "console.view",
  ACTIVITY: "activity.view",
  START: "server.start",
  MODERATE: "players.moderate",
  CONTROL: "server.control",
  CONSOLE_COMMAND: "console.command",
  OPERATORS: "players.operators",
  FILES: "files",
  SETTINGS: "settings",
  BACKUPS: "backups",
  VERSIONS: "versions",
  DELETE: "server.delete",
};

export const ROLE_LABEL = { owner: "Administrator", player: "Player", moderator: "Moderator", admin: "Admin" };

// What each role means, in the words used when handing out access.
export const ROLES = [
  { id: "player", label: "Player", summary: "Sees the server's status: running or not, who is online, load.", badge: "info" },
  {
    id: "moderator", label: "Moderator", badge: "accent",
    summary: "Everything a player can, plus: start the server (not stop it), read the console (not type into it), kick, ban and whitelist players (not make operators).",
  },
  {
    id: "admin", label: "Admin", badge: "success",
    summary: "Everything on that server except deleting it: stop, restart, console commands, operators, files, settings, backups and versions.",
  },
];

export const roleBadgeClass = (role) => ROLES.find((r) => r.id === role)?.badge || "";

export const can = (server, perm) => !!server && !!server.access && server.access.can.includes(perm);
export const canAnywhere = (perm) => [...state.servers.values()].some((s) => can(s, perm));
export const isAdministrator = () => state.user?.kind === "admin";
