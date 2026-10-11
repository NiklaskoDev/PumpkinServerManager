// Client-side state fed by the REST API and the live WebSocket.

import { get } from "./api.js";

export class Emitter {
  constructor() { this.handlers = new Map(); }
  on(event, fn) {
    if (!this.handlers.has(event)) this.handlers.set(event, new Set());
    this.handlers.get(event).add(fn);
    return () => this.handlers.get(event).delete(fn);
  }
  emit(event, payload) {
    for (const fn of [...(this.handlers.get(event) || [])]) {
      try { fn(payload); } catch (e) { console.error(`handler for "${event}" failed`, e); }
    }
  }
}

export const bus = new Emitter();

export const state = {
  user: null,
  servers: new Map(),
  jobs: new Map(),
  activity: [],
  system: null,
  settings: null,
  connected: false,
};

export const serverList = () => [...state.servers.values()];

export async function loadServers() {
  const list = await get("/api/servers");
  state.servers = new Map(list.map((s) => [s.id, s]));
  bus.emit("servers");
}

export async function loadSystem() {
  state.system = await get("/api/system");
  bus.emit("system");
}

export async function loadSettings() {
  state.settings = await get("/api/settings");
  bus.emit("settings");
}

export async function loadActivity(limit = 60) {
  state.activity = await get(`/api/activity?limit=${limit}`);
  bus.emit("activity");
}

export async function loadJobs() {
  const jobs = await get("/api/jobs?limit=30");
  state.jobs = new Map(jobs.map((j) => [j.id, j]));
  bus.emit("jobs");
}

// Applies one message from the WebSocket.
export function handleEvent(msg) {
  switch (msg.type) {
    case "server":
      state.servers.set(msg.data.id, { ...(state.servers.get(msg.data.id) || {}), ...msg.data });
      bus.emit("server", msg.data);
      bus.emit("servers");
      break;
    case "stats": {
      for (const [id, s] of Object.entries(msg.data)) {
        const server = state.servers.get(id);
        if (!server) continue;
        server.stats = { ...(server.stats || {}), cpu: s.cpu, rss: s.rss };
        server.uptime = s.uptime;
        server.players = { online: s.players, max: s.max_players, names: s.names };
      }
      bus.emit("stats", msg.data);
      break;
    }
    case "deleted":
      state.servers.delete(msg.data.id);
      bus.emit("servers");
      bus.emit("deleted", msg.data);
      break;
    case "job": {
      const previous = state.jobs.get(msg.data.id);
      state.jobs.set(msg.data.id, msg.data);
      bus.emit("job", { job: msg.data, previous });
      bus.emit("jobs");
      break;
    }
    case "activity":
      state.activity.unshift(msg.data);
      state.activity.length = Math.min(state.activity.length, 200);
      bus.emit("activity", msg.data);
      break;
    case "console":
    case "backlog":
      bus.emit(`console:${msg.data.server_id}`, msg);
      break;
    default:
      break;
  }
}
