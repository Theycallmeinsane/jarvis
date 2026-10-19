// Jarvis Control: a link between Chrome and the Jarvis voice assistant running on this PC.
// It connects out to ws://127.0.0.1:<port> (Jarvis only accepts this extension's own origin) and runs
// a fixed set of commands: tabs, navigation, and the page actions in PAGE below. No arbitrary code.

const DEFAULT_PORT = 47815;
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

  // ---- numbered links: "show links", then "click 7" (like Vimium hints)
  hints: function ({ action = "show", n = 0 }) {
    const W = window;
    const clear = () => { const old = document.getElementById("jarvis-hints"); if (old) old.remove(); };
    const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
    const label = (el) => norm(el.innerText || el.getAttribute("aria-label") || el.value || el.title ||
                               el.placeholder || el.alt || (el.querySelector("img") && el.querySelector("img").alt)).slice(0, 60);
    if (action === "hide") { clear(); W.__jarvisHints = []; return { count: 0 }; }
    if (action === "label" || action === "click") {
      const el = (W.__jarvisHints || [])[n - 1];
      if (!el || !el.isConnected) return { ok: false, reason: "there's no number " + n + " on screen; say show links first" };
      if (action === "label") return { ok: true, label: label(el) || el.tagName.toLowerCase() };
      clear(); W.__jarvisHints = [];
      el.scrollIntoView({ block: "center" });
      if (/^(input|textarea|select)$/i.test(el.tagName) || el.isContentEditable) el.focus();
      else el.click();
      return { ok: true, label: label(el) || el.tagName.toLowerCase(), href: el.href || "" };
    }
    clear();
    const sel = "a[href], button, [role=button], [role=link], [role=tab], [role=menuitem], [role=option], " +
                "input:not([type=hidden]), select, textarea, summary, [contenteditable=true], [onclick]";
    const seen = new Set();
    const els = [...document.querySelectorAll(sel)].filter((el) => {
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) return false;
      const s = getComputedStyle(el);
      if (s.visibility === "hidden" || s.display === "none" || Number(s.opacity) === 0) return false;
      const top = document.elementFromPoint(Math.min(innerWidth - 1, Math.max(0, r.left + r.width / 2)),
                                            Math.min(innerHeight - 1, Math.max(0, r.top + r.height / 2)));
      if (top && top !== el && !el.contains(top) && !top.contains(el)) return false;   // covered by something else
      for (const p of seen) if (p.contains(el)) return false;                         // a link inside a counted button
      seen.add(el);
      return true;
    }).slice(0, 150);
    const box = document.createElement("div");
    box.id = "jarvis-hints";
    box.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
    els.forEach((el, i) => {
      const r = el.getBoundingClientRect();
      const tag = document.createElement("span");
      tag.textContent = String(i + 1);
      tag.style.cssText = `position:fixed;left:${Math.max(0, r.left - 4)}px;top:${Math.max(0, r.top - 4)}px;` +
        "background:#ffd400;color:#000;font:bold 12px/1.2 Segoe UI,Arial,sans-serif;padding:1px 4px;" +
        "border:1px solid #a68a00;border-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,.4)";
      box.appendChild(tag);
    });
    document.documentElement.appendChild(box);
    W.__jarvisHints = els;
    if (!W.__jarvisHintsScroll) {          // numbers drift when the page moves: take them away
      W.__jarvisHintsScroll = true;
      addEventListener("scroll", () => { const b = document.getElementById("jarvis-hints"); if (b) b.remove(); }, { passive: true });
    }
    return { count: els.length, labels: els.slice(0, 12).map((el, i) => `${i + 1}: ${label(el)}`) };
  },

  // ---- search results on Google, YouTube, Bing, DuckDuckGo, Amazon, Reddit... (or the page's main links)
  results: function ({ action = "list", n = 1 }) {
    const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
    const h = location.hostname;
    let links = [];
    const pick = (sel, up) => [...document.querySelectorAll(sel)].map((el) => (up ? el.closest("a") : el)).filter(Boolean);
    if (/google\./.test(h)) links = pick("#search a h3, #rso a h3", true);
    else if (/youtube\.com/.test(h)) links = pick("a#video-title, ytd-video-renderer a#video-title-link, a#video-title-link");
    else if (/bing\.com/.test(h)) links = pick("#b_results h2 a");
    else if (/duckduckgo\.com/.test(h)) links = pick("a[data-testid=result-title-a]");
    else if (/amazon\./.test(h)) links = pick("[data-component-type='s-search-result'] h2 a, [data-component-type='s-search-result'] a h2", false)
      .map((el) => (el.tagName === "A" ? el : el.closest("a"))).filter(Boolean);
    else if (/reddit\.com/.test(h)) links = pick("a[data-testid='post-title'], shreddit-post a[slot='full-post-link'], a[slot='title']");
    else if (/wikipedia\.org/.test(h)) links = pick(".mw-search-result-heading a");
    else if (/github\.com/.test(h)) links = pick("[data-testid='results-list'] .search-title a, .search-title a");
    if (!links.length) {
      const main = document.querySelector("main, [role=main], #content, article") || document.body;
      links = [...main.querySelectorAll("a[href]")].filter((a) => {
        const r = a.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && norm(a.innerText).length > 20 && !/^(javascript|#)/.test(a.getAttribute("href"));
      });
    }
    const seen = new Set();
    links = links.filter((a) => a.href && !seen.has(a.href) && seen.add(a.href));
    const title = (a) => norm(a.innerText || a.getAttribute("title") || a.getAttribute("aria-label")).slice(0, 120);
    if (action === "open") {
      const a = links[n - 1];
      if (!a) return { ok: false, reason: `there are only ${links.length} results here` };
      a.scrollIntoView({ block: "center" });
      a.click();
      return { ok: true, title: title(a), url: a.href };
    }
    return { ok: true, results: links.slice(0, 10).map((a) => ({ title: title(a), url: a.href })) };
  },

  find: function ({ text }) {
    getSelection().removeAllRanges();
    const found = window.find(text, false, false, true);
    if (found) {
      const r = getSelection().getRangeAt(0).getBoundingClientRect();
      scrollBy({ top: r.top - innerHeight / 3, behavior: "smooth" });
    }
    return { found };
  },

  // ---- Instagram and Facebook: the post, reel or story in view, and the open chat
  social: async function ({ action, text = "", name = "" }) {
    const site = /instagram\.com/.test(location.hostname) ? "instagram" : /facebook\.com|messenger\.com/.test(location.hostname) ? "facebook" : "";
    if (!site) return { ok: false, reason: "this tab isn't Instagram or Facebook" };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
    const shown = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const press = (el) => { const b = el.closest("button, [role=button], a") || el; b.click(); return b; };
    const byLabel = (root, labels) => {
      for (const l of labels) {
        const el = [...root.querySelectorAll(`[aria-label="${l}"]`)].find(shown);
        if (el) return el;
      }
      return null;
    };
    const story = /\/stories\//.test(location.pathname);
    const reels = /\/reels?\//.test(location.pathname);

    // the item in view: a feed post (article / aria-posinset), else the playing reel's block
    const current = () => {
      const mid = innerHeight / 2;
      let items = [...document.querySelectorAll(site === "instagram" ? "article" : "[aria-posinset], [role=article]")]
        .filter((el) => shown(el) && !el.parentElement.closest("article, [aria-posinset], [role=article]"));
      if (!items.length) {
        items = [...document.querySelectorAll("video")].filter(shown).map((v) => {
          let p = v;
          while (p.parentElement && !p.querySelector("[aria-label='Like'], [aria-label='Unlike'], [aria-label='Remove Like']")) p = p.parentElement;
          return p;
        });
      }
      const dist = (el) => { const r = el.getBoundingClientRect(); return r.top <= mid && r.bottom >= mid ? 0 : Math.min(Math.abs(r.top - mid), Math.abs(r.bottom - mid)); };
      items.sort((a, b) => dist(a) - dist(b));
      return { item: items[0] || null, all: items };
    };
    const typeInto = async (box, words) => {
      box.focus();
      if (box.isContentEditable) document.execCommand("insertText", false, words);
      else {
        const proto = box.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, "value").set.call(box, words);
        box.dispatchEvent(new Event("input", { bubbles: true }));
      }
      await sleep(300);
    };
    const enter = (box) => {
      const o = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
      box.dispatchEvent(new KeyboardEvent("keydown", o));
      box.dispatchEvent(new KeyboardEvent("keypress", o));
      box.dispatchEvent(new KeyboardEvent("keyup", o));
    };

    if (action === "next" || action === "previous") {
      const fwd = action === "next";
      if (story) {
        const b = byLabel(document, fwd ? ["Next"] : ["Previous", "Go back"]);
        if (b) { press(b); return { ok: true, did: "story" }; }
        document.body.dispatchEvent(new KeyboardEvent("keydown", { key: fwd ? "ArrowRight" : "ArrowLeft", bubbles: true }));
        return { ok: true, did: "story" };
      }
      const { item, all } = current();
      if (!item) { scrollBy({ top: (fwd ? 1 : -1) * innerHeight * 0.85, behavior: "smooth" }); return { ok: true, did: "scroll" }; }
      const ordered = all.slice().sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
      const i = ordered.indexOf(item);
      const target = ordered[i + (fwd ? 1 : -1)];
      if (target) target.scrollIntoView({ block: reels ? "center" : "start", behavior: "smooth" });
      else scrollBy({ top: (fwd ? 1 : -1) * innerHeight * 0.85, behavior: "smooth" });
      return { ok: true, did: reels ? "reel" : "post" };
    }

    if (action === "pause" || action === "play") {
      const b = byLabel(document, action === "pause" ? ["Pause"] : ["Play"]);
      if (b) { press(b); return { ok: true }; }
      const v = [...document.querySelectorAll("video")].find((x) => !x.paused) || document.querySelector("video");
      if (!v) return { ok: false, reason: "nothing is playing" };
      action === "pause" ? v.pause() : v.play();
      return { ok: true };
    }

    if (action === "chats" || action === "open_chat") {
      // the conversation list: anchors to threads first, then rows/buttons that carry an avatar
      const seen = new Set();
      const items = [...document.querySelectorAll("a[href*='/direct/t/'], a[href*='/messages/t/'], a[href*='/messages/e2ee/t/'], " +
                                                   "[role=listitem], [role=row], div[role=button]")]
        .filter((el) => shown(el) && el.querySelector("img, svg image") && norm(el.innerText).length > 1 && norm(el.innerText).length < 300)
        .filter((el) => { for (const p of seen) if (p.contains(el) || el.contains(p)) return false; seen.add(el); return true; });
      const names = items.map((el) => norm(el.innerText.split("\n").find((l) => norm(l)) || ""));
      if (action === "chats") return { ok: true, names: names.slice(0, 40) };
      const el = items[Number(name)];
      if (!el) return { ok: false, reason: "that chat isn't in the list any more" };
      el.click();
      await sleep(1200);
      return { ok: true, name: names[Number(name)] };
    }

    if (action === "stories") {
      const ring = [...document.querySelectorAll("[aria-label^='Story by' i], [aria-label*='story' i][role=button], " +
                                                 "[aria-label*='stories' i][role=button]")]
        .find((el) => shown(el) && !/your story|create/i.test(el.getAttribute("aria-label")));
      if (!ring) return { ok: false, reason: "I can't see any stories here" };
      press(ring);
      return { ok: true };
    }

    if (action === "send_message" || action === "read_messages") {
      const boxes = [...document.querySelectorAll("div[role=textbox][contenteditable=true], textarea, [contenteditable=true]")]
        .filter((b) => shown(b) && /message|aa|chat|write/i.test((b.getAttribute("aria-label") || "") + " " + (b.getAttribute("placeholder") || "") + " " + (b.getAttribute("aria-placeholder") || "")));
      const box = boxes[boxes.length - 1];
      if (!box) return { ok: false, reason: "no chat is open; open one first" };
      if (action === "read_messages") {
        // the open thread: grow outwards from the message box until the next step would take in the chat list
        const list = [...document.querySelectorAll("a[href*='/direct/t/'], a[href*='/messages/t/'], [role=listitem], " +
                                                    "[role=row], div[role=button]")].filter((el) => el.querySelector("img"));
        let p = box;
        while (p.parentElement && p.parentElement !== document.body && !list.some((el) => p.parentElement.contains(el))) p = p.parentElement;
        const lines = p.innerText.replace(box.innerText, "").split("\n").map(norm).filter(Boolean);
        return { ok: true, text: lines.map((l) => (/[.!?]$/.test(l) ? l : l + ".")).join(" ").slice(-900) };
      }
      await typeInto(box, text);
      const send = byLabel(document, ["Send", "Press enter to send", "Press Enter to send"]) ||
                   [...document.querySelectorAll("div[role=button], button")].find((b) => shown(b) && norm(b.innerText) === "Send");
      if (send) press(send); else enter(box);
      await sleep(500);
      return { ok: true };
    }

    if (action === "follow" || action === "unfollow") {
      const buttons = [...document.querySelectorAll("button, div[role=button]")].filter(shown);
      const want = action === "follow" ? ["follow", "follow back", "add friend"] : ["following", "unfollow"];
      const b = buttons.find((x) => want.includes(norm(x.innerText).toLowerCase()));
      if (!b) return { ok: false, reason: action === "follow" ? "I don't see a Follow button here" : "you're not following this account" };
      b.click();
      if (action === "unfollow") {
        await sleep(700);
        const u = [...document.querySelectorAll("button, div[role=button], [role=menuitem]")]
          .find((x) => shown(x) && norm(x.innerText).toLowerCase() === "unfollow");
        if (u) u.click();
      }
      return { ok: true };
    }

    const { item } = current();
    if (!item) return { ok: false, reason: "I can't see a post on screen" };

    if (action === "read") {
      const skip = /^(like|unlike|comment|share|send|save|remove|reply|post|follow|following|more|see translation|·|\d+[smhdw]|\d+ (likes?|comments?|shares?))$/i;
      const author = norm((item.querySelector("header a, h2 a, h3 a, h4 a, strong a, a[role=link] span") || {}).innerText);
      const lines = item.innerText.split("\n").map(norm).filter((l) => l && !skip.test(l) && l !== author);
      return { ok: true, author, text: lines.map((l) => (/[.!?]$/.test(l) ? l : l + ".")).join(" ").slice(0, 900) };
    }
    if (action === "like" || action === "unlike") {
      const liked = byLabel(item, ["Unlike", "Remove Like", "Change Like reaction"]);
      if (action === "like") {
        if (liked) return { ok: true, already: true };
        const b = byLabel(item, ["Like"]);
        if (!b) return { ok: false, reason: "I can't find this post's like button" };
        press(b);
      } else {
        if (!liked) return { ok: true, already: true };
        press(liked);
      }
      return { ok: true };
    }
    if (action === "save" || action === "unsave") {
      const b = byLabel(item, action === "save" ? ["Save"] : ["Remove"]);
      if (!b) return { ok: false, reason: action === "save" ? "this post is already saved, or has no save button" : "this post isn't saved" };
      press(b);
      return { ok: true };
    }
    if (action === "comment") {
      let box = [...item.querySelectorAll("textarea, div[role=textbox][contenteditable=true]")].find(shown);
      if (!box) {
        const open = byLabel(item, ["Comment", "Leave a comment", "Write a comment"]);
        if (open) { press(open); await sleep(900); }
        box = [...item.querySelectorAll("textarea, div[role=textbox][contenteditable=true]")].find(shown) ||
              [...document.querySelectorAll("textarea[aria-label*='comment' i], div[role=textbox][aria-label*='comment' i]")].find(shown);
      }
      if (!box) return { ok: false, reason: "I can't find a comment box on this post" };
      await typeInto(box, text);
      const post = [...(item.querySelectorAll("div[role=button], button"))].find((b) => shown(b) && norm(b.innerText) === "Post") ||
                   byLabel(item, ["Comment", "Post comment", "Press Enter to post."]);
      if (post && norm(post.innerText) === "Post") post.click(); else enter(box);
      await sleep(500);
      return { ok: true };
    }
    return { ok: false, reason: `unknown action ${action}` };
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
