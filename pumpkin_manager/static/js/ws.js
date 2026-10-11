// Live updates over one WebSocket, with automatic reconnect.

import { get } from "./api.js";
import { bus, handleEvent, state } from "./store.js";

let socket = null;
let attempts = 0;
let stopped = true;
let timer = null;
const consoleTopics = new Set();

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

function open() {
  const scheme = location.protocol === "https:" ? "wss://" : "ws://";
  socket = new WebSocket(`${scheme}${location.host}/api/ws`);
  socket.onopen = () => {
    const wasReconnect = attempts > 0;
    attempts = 0;
    state.connected = true;
    bus.emit("connection", true);
    for (const topic of consoleTopics) send({ op: "subscribe", topic });
    if (wasReconnect) bus.emit("reconnected");
  };
  socket.onmessage = (event) => {
    try { handleEvent(JSON.parse(event.data)); } catch (err) { console.error("bad event", err); }
  };
  socket.onclose = () => {
    state.connected = false;
    bus.emit("connection", false);
    if (!stopped) schedule();
  };
  socket.onerror = () => { /* onclose follows */ };
}

function schedule() {
  const delay = Math.min(15000, 600 * 2 ** Math.min(attempts, 5));
  attempts += 1;
  timer = setTimeout(async () => {
    try {
      await get("/api/auth/me"); // a 401 here sends the user back to the login page
    } catch (err) {
      if (err.status === 401) return;
    }
    if (!stopped) open();
  }, delay);
}

export function connect() {
  stopped = false;
  open();
}

export function disconnect() {
  stopped = true;
  clearTimeout(timer);
  if (socket) socket.close();
}

export function subscribeConsole(serverId) {
  const topic = `console:${serverId}`;
  consoleTopics.add(topic);
  send({ op: "subscribe", topic });
}

export function unsubscribeConsole(serverId) {
  const topic = `console:${serverId}`;
  consoleTopics.delete(topic);
  send({ op: "unsubscribe", topic });
}
