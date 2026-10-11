// Operator permission levels. They work as in vanilla Minecraft; Pumpkin gives a player the level from the server's
// "Operator permission level" setting when they are made an operator, and a single operator's level can differ.

export const OP_LEVELS = [
  { value: 1, label: "1 · Moderator", help: "Can build inside the spawn protection. Almost no commands." },
  { value: 2, label: "2 · Game master", help: "Also game mode, difficulty, teleport, give and similar commands." },
  { value: 3, label: "3 · Admin", help: "Also making operators, bans, kicks and the whitelist." },
  { value: 4, label: "4 · Owner", help: "Everything, including stopping the server." },
];

export const opLevelHelp = (level) => OP_LEVELS.find((l) => l.value === Number(level))?.help ?? "";
