// Converts a line of terminal output with ANSI colour codes into DOM nodes (never HTML strings).

const SGR = /\x1b\[([0-9;]*)m/g;
const OTHER_CSI = /\x1b\[[0-9;?]*[A-Za-ln-z]/g;
const OSC = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;

function xterm256(n) {
  if (n < 16) return null; // handled by the palette classes
  if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
  n -= 16;
  const steps = [0, 95, 135, 175, 215, 255];
  return `rgb(${steps[Math.floor(n / 36)]},${steps[Math.floor((n % 36) / 6)]},${steps[n % 6]})`;
}

export function ansiToNodes(input) {
  const text = input.replace(OSC, "").replace(OTHER_CSI, "");
  const fragment = document.createDocumentFragment();
  let state = { classes: [], color: null, bg: null };
  let last = 0;

  const emit = (chunk) => {
    if (!chunk) return;
    if (!state.classes.length && !state.color && !state.bg) {
      fragment.appendChild(document.createTextNode(chunk));
      return;
    }
    const span = document.createElement("span");
    span.className = state.classes.join(" ");
    if (state.color) span.style.color = state.color;
    if (state.bg) span.style.backgroundColor = state.bg;
    span.textContent = chunk;
    fragment.appendChild(span);
  };

  const apply = (codes) => {
    const list = codes.length ? codes : [0];
    for (let i = 0; i < list.length; i++) {
      const c = list[i];
      const dropClass = (prefix) => (state.classes = state.classes.filter((x) => !x.startsWith(prefix)));
      if (c === 0) state = { classes: [], color: null, bg: null };
      else if (c === 1) state.classes.push("ansi-b");
      else if (c === 2) state.classes.push("ansi-d");
      else if (c === 3) state.classes.push("ansi-i");
      else if (c === 4) state.classes.push("ansi-u");
      else if (c === 22) state.classes = state.classes.filter((x) => x !== "ansi-b" && x !== "ansi-d");
      else if (c >= 30 && c <= 37) { dropClass("ansi-fg-"); state.color = null; state.classes.push(`ansi-fg-${c - 30}`); }
      else if (c >= 90 && c <= 97) { dropClass("ansi-fg-"); state.color = null; state.classes.push(`ansi-fg-${c - 90 + 8}`); }
      else if (c === 39) { dropClass("ansi-fg-"); state.color = null; }
      else if (c >= 40 && c <= 47) state.bg = ["#4b5263", "#ef6b73", "#6fcf97", "#f2c94c", "#6aa0ff", "#c792ea", "#56d4dd", "#d6dae2"][c - 40];
      else if (c === 49) state.bg = null;
      else if (c === 38 || c === 48) {
        const mode = list[i + 1];
        let color = null;
        if (mode === 5) { color = xterm256(list[i + 2]); if (color === null && c === 38) { dropClass("ansi-fg-"); state.color = null; state.classes.push(`ansi-fg-${list[i + 2]}`); } i += 2; }
        else if (mode === 2) { color = `rgb(${list[i + 2] | 0},${list[i + 3] | 0},${list[i + 4] | 0})`; i += 4; }
        if (color) { if (c === 38) { dropClass("ansi-fg-"); state.color = color; } else state.bg = color; }
      }
    }
  };

  for (const match of text.matchAll(SGR)) {
    emit(text.slice(last, match.index));
    apply(match[1] === "" ? [] : match[1].split(";").map((x) => parseInt(x, 10) || 0));
    last = match.index + match[0].length;
  }
  emit(text.slice(last));
  return fragment;
}

export function stripAnsi(text) {
  return text.replace(OSC, "").replace(OTHER_CSI, "").replace(SGR, "");
}
