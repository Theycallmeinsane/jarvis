// Jarvis Control: a link between Chrome and the Jarvis voice assistant running on this PC.
// It connects out to ws://127.0.0.1:<port> (Jarvis only accepts this extension's own origin) and runs
// a fixed set of commands: tabs, navigation, and the page actions in PAGE below. No arbitrary code.

const DEFAULT_PORT = 8765;
let ws = null;
let pinger = null;

async function getPort() {
  const { port } = await chrome.storage.local.get("port");
  return port || DEFAULT_PORT;
}

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  let sock;
  try {
    sock = new WebSocket(`ws://127.0.0.1:${await getPort()}/chrome`);
  } catch (e) {
    return;
  }
  ws = sock;
  sock.onopen = () => {
    sock.send(JSON.stringify({ hello: "jarvis-chrome", version: chrome.runtime.getManifest().version }));
    // traffic keeps the service worker alive; Jarvis ignores pings
    pinger = setInterval(() => sock.readyState === WebSocket.OPEN && sock.send('{"ping":1}'), 20000);
    chrome.action.setBadgeText({ text: "" });
    chrome.storage.session.set({ connected: true });
  };
  sock.onclose = () => {
    clearInterval(pinger);
    if (ws === sock) ws = null;
    chrome.action.setBadgeText({ text: "off" });
    chrome.storage.session.set({ connected: false });
  };
  sock.onerror = () => {};
  sock.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (!msg.id) return;
    try {
      const result = await handle(msg.cmd, msg.args || {});
      sock.send(JSON.stringify({ id: msg.id, ok: true, result }));
    } catch (e) {
      sock.send(JSON.stringify({ id: msg.id, ok: false, error: String((e && e.message) || e) }));
    }
  };
}

function ensureAlarm() {
  chrome.alarms.get("reconnect", (a) => a || chrome.alarms.create("reconnect", { periodInMinutes: 0.5 }));
}
chrome.runtime.onStartup.addListener(() => { ensureAlarm(); connect(); });
chrome.runtime.onInstalled.addListener(() => { ensureAlarm(); connect(); });
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onMessage.addListener((m, _s, reply) => {   // the popup's "Reconnect" button
  if (m === "reconnect") connect().then(() => reply(true));
  return true;
});
ensureAlarm();
connect();

// ------------------------------------------------------------------ commands

async function activeTab() {
  const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!t) throw new Error("No Chrome tab is open.");
  return t;
}

async function focus(tab) {
  await chrome.tabs.update(tab.id, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  return tab;
}

function waitLoaded(tabId, ms = 15000) {
  return new Promise((resolve) => {
    const done = () => { chrome.tabs.onUpdated.removeListener(listen); clearTimeout(timer); resolve(); };
    const listen = (id, info) => id === tabId && info.status === "complete" && done();
    const timer = setTimeout(done, ms);
    chrome.tabs.onUpdated.addListener(listen);
    chrome.tabs.get(tabId).then((t) => t.status === "complete" && done()).catch(done);
  });
}

const info = (t) => ({ id: t.id, windowId: t.windowId, title: t.title, url: t.url, active: t.active,
                       audible: !!t.audible, muted: !!(t.mutedInfo && t.mutedInfo.muted), pinned: t.pinned });

async function handle(cmd, a) {
  switch (cmd) {
    case "tabs":
      return (await chrome.tabs.query({})).map(info);
    case "open": {
      let tab;
      const wins = await chrome.windows.getAll();
      if (!wins.length) {
        const w = await chrome.windows.create({ url: a.url, focused: true });
        tab = w.tabs[0];
      } else if (a.newTab === false) {
        tab = await chrome.tabs.update((await activeTab()).id, { url: a.url });
      } else {
        tab = await chrome.tabs.create({ url: a.url, active: true });
      }
      await focus(tab);
      if (a.wait) await waitLoaded(tab.id);
      return info(await chrome.tabs.get(tab.id));
    }
    case "activate":
      return info(await focus(await chrome.tabs.get(a.tabId)));
    case "close":
      await chrome.tabs.remove(a.tabIds);
      return a.tabIds.length;
    case "reopen": {
      const s = await chrome.sessions.restore();
      return s && s.tab ? info(s.tab) : null;
    }
    case "nav": {
      const t = a.tabId ? await chrome.tabs.get(a.tabId) : await activeTab();
      if (a.action === "back") await chrome.tabs.goBack(t.id);
      else if (a.action === "forward") await chrome.tabs.goForward(t.id);
      else if (a.action === "reload") await chrome.tabs.reload(t.id);
      else if (a.action === "zoom_in" || a.action === "zoom_out" || a.action === "zoom_reset") {
        const z = await chrome.tabs.getZoom(t.id);
        const next = a.action === "zoom_reset" ? 1 : Math.min(5, Math.max(0.25, z + (a.action === "zoom_in" ? 0.25 : -0.25)));
        await chrome.tabs.setZoom(t.id, next);
        return Math.round(next * 100);
      } else if (a.action === "mute" || a.action === "unmute") await chrome.tabs.update(t.id, { muted: a.action === "mute" });
      else if (a.action === "pin" || a.action === "unpin") await chrome.tabs.update(t.id, { pinned: a.action === "pin" });
      else if (a.action === "duplicate") await chrome.tabs.duplicate(t.id);
      else if (a.action === "new_window") await chrome.windows.create({ focused: true });
      else throw new Error(`Unknown action ${a.action}`);
      return true;
    }
    case "run": {
      const fn = PAGE[a.fn];
      if (!fn) throw new Error(`Unknown page action ${a.fn}`);
      const t = a.tabId ? await chrome.tabs.get(a.tabId) : await activeTab();
      if (!/^https?:|^file:/.test(t.url || "")) throw new Error("Chrome doesn't let extensions act on this page.");
      const [r] = await chrome.scripting.executeScript({ target: { tabId: t.id }, func: fn, args: [a.args || {}] });
      return r ? r.result : null;
    }
    default:
      throw new Error(`Unknown command ${cmd}`);
  }
}

// ------------------------------------------------------------------ page actions (run inside the page)
// plain `function` expressions: Chrome injects each one's source text, and method shorthand isn't valid there

const PAGE = {
  read: function ({ maxChars = 8000 }) {
    const main = document.querySelector("main, article, [role=main]") || document.body;
    return { title: document.title, url: location.href, selection: String(getSelection() || ""),
             text: (main.innerText || "").replace(/\n{3,}/g, "\n\n").slice(0, maxChars) };
  },

  scroll: function ({ to = "down" }) {
    const h = innerHeight * 0.8;
    if (to === "top") scrollTo({ top: 0, behavior: "smooth" });
    else if (to === "bottom") scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    else scrollBy({ top: to === "up" ? -h : h, behavior: "smooth" });
    return true;
  },

  click: function ({ text, index = 0 }) {
    const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();
    const want = norm(text);
    const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const label = (el) => norm(el.innerText || el.getAttribute("aria-label") || el.value || el.title || el.alt);
    const els = [...document.querySelectorAll("a, button, [role=button], [role=link], [role=tab], [role=menuitem], " +
                                              "input[type=submit], input[type=button], summary, label")].filter(visible);
    const exact = els.filter((el) => label(el) === want);
    const hits = exact.length ? exact : els.filter((el) => label(el).includes(want));
    if (!hits.length) return { clicked: null, options: [...new Set(els.map(label).filter((l) => l && l.length < 40))].slice(0, 15) };
    const el = hits[Math.min(index, hits.length - 1)];
    el.scrollIntoView({ block: "center" });
    el.click();
    return { clicked: label(el), count: hits.length };
  },

  type: function ({ text, field = "", submit = false }) {
    const norm = (s) => (s || "").toLowerCase().trim();
    const editable = (el) => el && (el.isContentEditable || /^(input|textarea)$/i.test(el.tagName));
    const forbidden = (el) => el.type === "password" || /cc-|card|cvc|cvv|password/i.test(el.autocomplete + " " + el.name + " " + el.id);
    const fields = [...document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio])" +
                                                 ":not([type=submit]):not([type=button]), textarea, [contenteditable=true]")]
      .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
    let el = null;
    if (field) {
      const f = norm(field);
      el = fields.find((x) => [x.placeholder, x.getAttribute("aria-label"), x.name, x.id,
                               x.labels && x.labels[0] && x.labels[0].innerText].some((s) => norm(s).includes(f)));
    }
    el = el || (editable(document.activeElement) ? document.activeElement : null) ||
         fields.find((x) => /search|text|^$/.test(x.type || "")) || fields[0];
    if (!el) return { typed: false, reason: "no text box on this page" };
    if (forbidden(el)) return { typed: false, reason: "that's a password or card field" };
    el.focus();
    if (el.isContentEditable) {
      document.execCommand("insertText", false, text);
    } else {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);   // works with React inputs
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    if (submit) {
      const opts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true };
      el.dispatchEvent(new KeyboardEvent("keydown", opts));
      el.dispatchEvent(new KeyboardEvent("keyup", opts));
      if (el.form) el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit();
    }
    return { typed: true, field: el.placeholder || el.getAttribute("aria-label") || el.name || el.tagName.toLowerCase() };
  },

  media: function ({ action, seconds = 10, rate = 1 }) {
    const all = [...document.querySelectorAll("video, audio")];
    if (!all.length) return { ok: false, reason: "no video or audio on this page" };
    const v = all.find((m) => !m.paused) || all.sort((a, b) => b.clientWidth - a.clientWidth)[0];
    if (action === "play") v.play();
    else if (action === "pause") v.pause();
    else if (action === "toggle") (v.paused ? v.play() : v.pause());
    else if (action === "mute") v.muted = true;
    else if (action === "unmute") v.muted = false;
    else if (action === "forward") v.currentTime += seconds;
    else if (action === "back") v.currentTime -= seconds;
    else if (action === "restart") v.currentTime = 0;
    else if (action === "speed") v.playbackRate = rate;
    else if (action === "next") {
      const b = document.querySelector(".ytp-next-button, [aria-label*='Next' i]");
      if (!b) return { ok: false, reason: "this page has no next button" };
      b.click();
    } else if (action === "fullscreen") {
      const b = document.querySelector(".ytp-fullscreen-button");
      if (b) b.click(); else return { ok: false, reason: "I can only switch YouTube to full screen" };
    } else if (action === "status") {
      // fall through to the report below
    } else return { ok: false, reason: `unknown action ${action}` };
    return { ok: true, paused: v.paused, muted: v.muted, time: Math.round(v.currentTime),
             duration: Math.round(v.duration || 0), rate: v.playbackRate, title: document.title };
  },

  // ---- claude.ai: the chat box is a ProseMirror editor; answers carry data-is-streaming while being written
  claudeState: function () {
    const answers = [...document.querySelectorAll("[data-is-streaming], .font-claude-response, .font-claude-message")]
      .filter((el) => !el.parentElement.closest("[data-is-streaming], .font-claude-response, .font-claude-message"));
    const last = answers[answers.length - 1];
    return { ready: !!document.querySelector("div[contenteditable=true]"), url: location.href,
             signedOut: /\/login|\/signup/.test(location.pathname), count: answers.length,
             streaming: !!document.querySelector("[data-is-streaming=true]"),
             text: last ? last.innerText.trim() : "" };
  },

  claudeSend: async function ({ text }) {
    const box = document.querySelector("div[contenteditable=true]");
    if (!box) return { sent: false, reason: "Claude's message box isn't on the page" };
    box.focus();
    document.execCommand("selectAll", false);
    document.execCommand("insertText", false, text);
    await new Promise((r) => setTimeout(r, 400));
    const send = document.querySelector("button[aria-label='Send message'], button[aria-label*='Send' i]");
    if (send && !send.disabled) send.click();
    else box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
    return { sent: true };
  },

  claudeStop: function () {
    const b = document.querySelector("button[aria-label*='Stop' i]");
    if (b) b.click();
    return { stopped: !!b };
  },
};
