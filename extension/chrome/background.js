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
    case "bookmarks": {
      if (a.action === "add") {
        const t = a.tabId ? await chrome.tabs.get(a.tabId) : await activeTab();
        if (!/^https?:|^file:/.test(t.url || "")) throw new Error("Chrome can't bookmark this page.");
        const [had] = await chrome.bookmarks.search({ url: t.url });
        if (had) return { added: false, title: had.title, url: had.url };
        const b = await chrome.bookmarks.create({ parentId: "1", title: a.title || t.title, url: t.url });  // bookmarks bar
        return { added: true, title: b.title, url: b.url };
      }
      const out = [];
      const walk = (nodes, folder) => {
        for (const n of nodes) {
          if (n.url) out.push({ title: n.title, url: n.url, folder, added: n.dateAdded || 0 });
          if (n.children) walk(n.children, n.title || folder);
        }
      };
      walk(await chrome.bookmarks.getTree(), "");
      return out.slice(0, a.max || 5000);
    }
    case "history": {
      const query = { text: a.text || "", startTime: a.since || Date.now() - (a.days || 30) * 86400000, maxResults: a.max || 200 };
      if (a.until) query.endTime = a.until;
      const items = await chrome.history.search(query);
      return items.filter((h) => /^https?:/.test(h.url || "")).map((h) => ({
        title: h.title || "", url: h.url, last: h.lastVisitTime || 0, visits: h.visitCount || 0 }));
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

  // ---- YouTube's own buttons: skip ad, captions, like, subscribe, theater mode
  youtube: function ({ action }) {
    if (!/(^|\.)youtube\.com$/.test(location.hostname)) return { ok: false, reason: "that only works on YouTube", host: location.hostname };
    const shown = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const first = (sel) => [...document.querySelectorAll(sel)].find(shown) || null;
    const pressed = (b) => b.getAttribute("aria-pressed") === "true";
    if (action === "skip_ad") {
      const skip = first(".ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern, " +
                         ".ytp-ad-skip-button-container button, button[id^='skip-button']");
      if (skip) { skip.click(); return { ok: true }; }
      const ad = document.querySelector(".html5-video-player.ad-showing");
      return { ok: false, reason: ad ? "this ad can't be skipped yet" : "there's no ad playing" };
    }
    if (action === "captions_on" || action === "captions_off" || action === "captions") {
      const b = first(".ytp-subtitles-button");
      if (!b || b.getAttribute("aria-disabled") === "true") return { ok: false, reason: "this video has no captions" };
      const want = action === "captions" ? !pressed(b) : action === "captions_on";
      if (pressed(b) === want) return { ok: true, already: true, on: want };
      b.click();
      return { ok: true, on: want };
    }
    if (action === "like" || action === "unlike") {
      const b = first("like-button-view-model button, #segmented-like-button button, #like-button button, " +
                      "ytd-toggle-button-renderer#like-button button, button[aria-label*='like this video' i]");
      if (!b) return { ok: false, reason: "I can't see the like button" };
      if (pressed(b) === (action === "like")) return { ok: true, already: true };
      b.click();
      return { ok: true };
    }
    if (action === "subscribe") {
      const b = first("#subscribe-button button, ytd-subscribe-button-renderer button, yt-subscribe-button-view-model button");
      if (!b) return { ok: false, reason: "I can't see a subscribe button" };
      const said = (b.innerText || b.getAttribute("aria-label") || "").trim();
      if (/subscribed|unsubscribe/i.test(said)) return { ok: true, already: true };
      b.click();
      return { ok: true };
    }
    if (action === "theater") {
      const b = first(".ytp-size-button");
      if (!b) return { ok: false, reason: "theater mode is only on a YouTube video page" };
      const was = !!document.querySelector("ytd-watch-flexy[theater]");
      b.click();
      return { ok: true, on: !was };
    }
    if (action === "channel") {
      const a = first("ytd-video-owner-renderer #channel-name a, #owner #channel-name a, #channel-name a");
      const b = first("#subscribe-button button, ytd-subscribe-button-renderer button, yt-subscribe-button-view-model button");
      return { ok: true, channel: a ? a.innerText.trim() : "", title: document.title.replace(/ - YouTube$/, ""),
               subscribed: !!b && /subscribed|unsubscribe/i.test(b.innerText || b.getAttribute("aria-label") || "") };
    }
    return { ok: false, reason: `unknown action ${action}` };
  },

  // ---- hands-free reading: scroll slowly until told to stop (or the page ends, or you touch the wheel)
  autoscroll: function ({ action = "start", speed = 0 }) {
    const s = window.__jarvisScroll || (window.__jarvisScroll = { timer: 0, speed: 1.5, acc: 0, idle: 0 });
    const stop = () => {
      clearInterval(s.timer);
      s.timer = 0;
      removeEventListener("wheel", s.off, true);
      removeEventListener("keydown", s.off, true);
      removeEventListener("touchstart", s.off, true);
    };
    if (action === "stop") { const was = !!s.timer; stop(); return { ok: true, running: false, was }; }
    if (action === "faster") s.speed = Math.min(25, s.speed * 1.7);
    else if (action === "slower") s.speed = Math.max(0.4, s.speed / 1.7);
    else if (speed) s.speed = speed;
    // the thing that scrolls: the page, or (in some web apps) the biggest scrolling box
    const target = () => {
      const doc = document.scrollingElement || document.documentElement;
      if (doc.scrollHeight > innerHeight + 4) return doc;
      const boxes = [...document.querySelectorAll("*")].filter((el) => {
        const st = getComputedStyle(el);
        return /(auto|scroll)/.test(st.overflowY) && el.scrollHeight > el.clientHeight + 4;
      });
      boxes.sort((a, b) => b.clientHeight * b.clientWidth - a.clientHeight * a.clientWidth);
      return boxes[0] || doc;
    };
    if (!s.timer) {
      const el = target();
      s.idle = 0;
      s.off = (e) => { if (e.isTrusted) stop(); };
      addEventListener("wheel", s.off, true);
      addEventListener("keydown", s.off, true);
      addEventListener("touchstart", s.off, true);
      s.timer = setInterval(() => {
        s.acc += s.speed;
        const step = Math.floor(s.acc);
        if (!step) return;
        s.acc -= step;
        const before = el.scrollTop;
        el.scrollTop = before + step;
        s.idle = el.scrollTop === before ? s.idle + 1 : 0;
        if (s.idle > 250) stop();          // ~4 s at the bottom (feeds get a moment to load more)
      }, 16);
    }
    return { ok: true, running: true, speed: Math.round(s.speed * 10) / 10 };
  },

  // ---- the site's own search box: "search this site for usb cables"
  sitesearch: function ({ text }) {
    const shown = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const sel = "input[type=search], [role=searchbox], input[role=combobox], input[name=q], input[name=query], " +
                "input[name=search], input[name=s], input[name=k], input[name=keyword], input[name=search_query], " +
                "input[placeholder*='search' i], input[aria-label*='search' i], textarea[name=q], textarea[aria-label*='search' i]";
    let box = [...document.querySelectorAll(sel)].find(shown);
    if (!box) {   // a magnifier that opens the box
      const open = [...document.querySelectorAll("button, [role=button], a")].find((b) => shown(b) &&
        /^(search|open search|show search)$/i.test((b.getAttribute("aria-label") || b.title || b.innerText || "").trim()));
      if (open) open.click();
      box = [...document.querySelectorAll(sel)].find(shown);
    }
    if (!box) return { ok: false, reason: "no search box on this page", host: location.hostname };
    box.focus();
    const proto = box.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(box, text);
    box.dispatchEvent(new Event("input", { bubbles: true }));
    box.dispatchEvent(new Event("change", { bubbles: true }));
    const opts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
    const go = box.dispatchEvent(new KeyboardEvent("keydown", opts));
    box.dispatchEvent(new KeyboardEvent("keypress", opts));
    box.dispatchEvent(new KeyboardEvent("keyup", opts));
    if (go && box.form) {
      const before = location.href;
      setTimeout(() => { if (location.href === before) box.form.requestSubmit ? box.form.requestSubmit() : box.form.submit(); }, 400);
    }
    return { ok: true, host: location.hostname };
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

  // ---- WhatsApp Web: the chat list (and its search), unread chats, the open chat (read it, send a message)
  whatsapp: async function ({ action, text = "", name = "", want = "" }) {
    if (location.hostname !== "web.whatsapp.com") return { ok: false, reason: "this tab isn't WhatsApp Web" };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
    const shown = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const first = (sels, root = document) => {
      for (const s of sels) { const el = [...root.querySelectorAll(s)].find(shown); if (el) return el; }
      return null;
    };
    const tap = (el) => {        // WhatsApp opens a chat on mousedown, and its buttons on click
      const r = el.getBoundingClientRect();
      const o = { bubbles: true, cancelable: true, view: window, button: 0, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
      el.dispatchEvent(new PointerEvent("pointerdown", o));
      el.dispatchEvent(new MouseEvent("mousedown", o));
      el.dispatchEvent(new PointerEvent("pointerup", o));
      el.dispatchEvent(new MouseEvent("mouseup", o));
      el.click();
    };
    const enter = (box) => {
      const o = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
      box.dispatchEvent(new KeyboardEvent("keydown", o));
      box.dispatchEvent(new KeyboardEvent("keypress", o));
      box.dispatchEvent(new KeyboardEvent("keyup", o));
    };
    // the chat list (or search results): rows with a titled name, in screen order (WhatsApp positions them itself)
    const listRoot = () => document.querySelector("#pane-side") || document.querySelector("[aria-label='Chat list' i]") ||
                           document.querySelector("#side");
    const rows = () => {
      const root = listRoot();
      if (!root) return [];
      const all = [...root.querySelectorAll("[role=listitem], [role=row]")].filter((el) => shown(el) && el.querySelector("span[title]"));
      return all.filter((el) => !all.some((o) => o !== el && o.contains(el)))
        .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    };
    const nameOf = (row) => { const s = row.querySelector("span[title]"); return norm((s && (s.getAttribute("title") || s.innerText)) || row.innerText.split("\n")[0]); };
    const notReady = () => ({ ok: false, reason: document.querySelector("canvas[aria-label*='scan' i], [data-ref] canvas, canvas")
      ? "WhatsApp Web isn't linked yet; scan the QR code with your phone first" : "WhatsApp is still loading" });
    const main = document.querySelector("#main");
    const chatName = () => {
      const h = main && first(["header span[title]", "header span[dir=auto]", "header [title]"], main);
      return h ? norm(h.getAttribute("title") || h.innerText) : "";
    };

    const searchSels = ["#side div[contenteditable='true'][role='textbox']", "div[contenteditable='true'][data-tab='3']",
                        "[contenteditable='true'][aria-label*='search' i]", "#side input[type='text']",
                        "input[aria-label*='search' i]", "[role='textbox'][aria-label*='search' i]"];
    const typeSearch = (box, words) => {
      box.focus();
      if (box.isContentEditable) {
        document.execCommand("selectAll", false);
        document.execCommand(words ? "insertText" : "delete", false, words);
      } else {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(box, words);
        box.dispatchEvent(new Event("input", { bubbles: true }));
      }
    };

    if (action === "chats" || action === "unread") {
      if (!listRoot()) return notReady();
      const old = first(searchSels);       // a search left from before filters the list: clear it
      if (old && norm(old.isContentEditable ? old.innerText : old.value)) { typeSearch(old, ""); old.blur(); await sleep(800); }
      const list = rows();
      if (action === "chats") return { ok: true, names: list.map(nameOf).slice(0, 40) };
      const chats = list.map((row) => {
        const badge = row.querySelector("span[aria-label*='unread message' i]");
        if (!badge) return null;
        const n = parseInt(((badge.getAttribute("aria-label") || "").match(/\d+/) || [norm(badge.innerText)])[0], 10);
        return { name: nameOf(row), count: n || 1 };
      }).filter(Boolean);
      return { ok: true, chats };
    }
    if (action === "search") {
      let box = first(searchSels);
      if (!box) {
        const b = first(["button[aria-label*='search' i]", "[data-icon='search']", "[data-icon*='search' i]"]);
        if (b) { tap(b.closest("button, [role=button]") || b); await sleep(500); }
        box = first(searchSels);
      }
      if (!box) return listRoot() ? { ok: false, reason: "I can't find WhatsApp's search box" } : notReady();
      typeSearch(box, text);
      await sleep(1600);
      return { ok: true, names: rows().map(nameOf).slice(0, 40) };
    }
    if (action === "open_chat") {
      const list = rows();
      const el = (want && list.find((r) => nameOf(r) === want)) || list[Number(name)];
      if (!el) return { ok: false, reason: "that chat isn't in the list any more" };
      tap(el.querySelector("span[title]") || el);
      await sleep(1000);
      return { ok: true, name: nameOf(el), open: chatName() };
    }
    if (action === "chat_name") {
      if (!main) return { ok: false, reason: "no chat is open; say open my chat with someone first" };
      return { ok: true, chat: chatName() };
    }
    if (action === "read_messages") {
      if (!main) return { ok: true, noChat: true };
      const who = chatName();
      const bubbles = [...main.querySelectorAll("div.message-in, div.message-out")]
        .filter((el) => !el.parentElement.closest(".message-in, .message-out"));
      const messages = bubbles.slice(-6).map((b) => {
        const out = b.classList.contains("message-out");
        const c = b.querySelector(".copyable-text[data-pre-plain-text]");
        const pre = c ? c.getAttribute("data-pre-plain-text") || "" : "";
        const m = pre.match(/^\s*\[([^\]]*)\]\s*(.*?):\s*$/);         // "[10:30, 19/10/2026] Ahmad Khan: "
        const textOf = (el) => {           // emoji are <img alt="😂"> inside the text
          const k = el.cloneNode(true);
          k.querySelectorAll("img[alt]").forEach((i) => i.replaceWith(i.alt));
          return norm(k.textContent);
        };
        let said = c ? textOf(c.querySelector("span.selectable-text") || c) : "";
        if (!said) {
          const plain = norm(b.innerText.replace(/\b\d{1,2}:\d{2}(\s*[ap]\.?m\.?)?/gi, ""));
          said = b.querySelector("audio, [data-icon*='audio' i], [data-icon*='ptt' i]") ? "a voice message"
            : b.querySelector("video, [data-icon*='video' i]") ? "a video"
            : b.querySelector("[data-icon*='document' i]") ? "a document"
            : b.querySelector("img[alt*='sticker' i]") ? "a sticker"
            : b.querySelector("img[src^='blob:']") ? "a photo"
            : plain || (b.querySelector("img") ? "a photo" : "something I can't read");
        }
        return { who: out ? "You" : (m && m[2]) || who || "Them", text: said, time: m ? m[1].split(",")[0] : "" };
      });
      return { ok: true, chat: who, messages };
    }
    if (action === "send_message") {
      if (!main) return { ok: false, reason: "no chat is open; say open my chat with someone first" };
      const box = first(["footer div[contenteditable='true'][role='textbox']", "div[contenteditable='true'][data-tab='10']",
                         "[contenteditable='true'][aria-label^='Type a message' i]", "footer [contenteditable='true']"], main);
      if (!box) return { ok: false, reason: "I can't find WhatsApp's message box" };
      if (norm(box.innerText)) return { ok: false, reason: "there's already a draft in the message box; send or clear it first" };
      const lastOut = () => { const o = main.querySelectorAll(".message-out"); return o.length ? norm(o[o.length - 1].innerText) : ""; };
      const before = lastOut();
      box.focus();
      document.execCommand("insertText", false, text);
      await sleep(300);
      if (!norm(box.innerText)) return { ok: false, reason: "WhatsApp didn't take the text" };
      const foot = box.closest("footer") || main;
      const send = first(["button[aria-label='Send' i]", "[data-icon='send']", "[data-icon*='send' i]"], foot);
      if (send) tap(send.closest("button, [role=button]") || send); else enter(box);
      await sleep(800);
      const went = () => !norm(box.innerText) || (lastOut() !== before && lastOut().includes(norm(text).slice(0, 30)));
      if (!went()) { enter(box); await sleep(600); }     // the button didn't take it: Enter sends in WhatsApp
      return { ok: true, sent: went(), chat: chatName() };
    }
    return { ok: false, reason: `unknown action ${action}` };
  },

  // ---- Reddit (the shreddit UI): the post in view; its vote buttons live in the post's shadow root
  reddit: async function ({ action }) {
    if (!/(^|\.)reddit\.com$/.test(location.hostname)) return { ok: false, reason: "this tab isn't Reddit" };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
    const rect = (el) => {
      const r = el.getBoundingClientRect();
      return r.height || !el.parentElement ? r : el.parentElement.getBoundingClientRect();
    };
    const shown = (el) => { const r = rect(el); return r.width > 0 && r.height > 0; };
    const deep = (root, sel) => {          // querySelectorAll that also looks inside (nested) open shadow roots
      const out = [];
      const walk = (n) => {
        if (!n) return;
        out.push(...n.querySelectorAll(sel));
        for (const e of n.querySelectorAll("*")) if (e.shadowRoot) walk(e.shadowRoot);
      };
      walk(root);
      return out;
    };
    const inPost = (post, sel) => [...deep(post.shadowRoot, sel), ...deep(post, sel)];
    const posts = () => [...document.querySelectorAll("shreddit-post, [data-testid='post-container']")]
      .filter((el) => shown(el) && !(el.parentElement && el.parentElement.closest("shreddit-post, [data-testid='post-container']")));
    const LINE = Math.min(innerHeight * 0.35, 320);
    const current = () => {
      const all = posts();
      const kept = window.__jarvisPost;    // the one Jarvis moved to, while it's still on screen
      if (kept && all.includes(kept)) { const r = rect(kept); if (r.bottom > 90 && r.top < innerHeight - 90) return { item: kept, all }; }
      const dist = (el) => { const r = rect(el); return r.top <= LINE && r.bottom >= LINE ? 0 : Math.min(Math.abs(r.top - LINE), Math.abs(r.bottom - LINE)); };
      return { item: all.slice().sort((a, b) => dist(a) - dist(b))[0] || null, all };
    };
    const flash = (el) => { const o = el.style.outline; el.style.outline = "2px solid #ff4500"; setTimeout(() => { el.style.outline = o; }, 1500); };

    if (action === "next" || action === "previous") {
      const fwd = action === "next";
      const { item, all } = current();
      const ordered = all.slice().sort((a, b) => rect(a).top - rect(b).top);
      const target = item ? ordered[ordered.indexOf(item) + (fwd ? 1 : -1)] : null;
      if (!target) { window.__jarvisPost = null; scrollBy({ top: (fwd ? 1 : -1) * innerHeight * 0.85, behavior: "smooth" }); return { ok: true, did: "scroll" }; }
      scrollBy({ top: rect(target).top - 72, behavior: "smooth" });   // below the sticky header
      window.__jarvisPost = target;
      flash(target);
      return { ok: true, did: "post" };
    }
    const { item } = current();
    if (!item) return { ok: false, reason: "I can't see a post on screen" };
    const pressed = (b) => !!b && b.getAttribute("aria-pressed") === "true";
    const button = (kind) => inPost(item, `button[${kind}]`).find(shown) ||
      inPost(item, "button, [role=button]").find((b) => shown(b) && (b.getAttribute("aria-label") || "").toLowerCase() === kind);

    if (action === "read") {
      const a = (n) => norm(item.getAttribute(n));
      const title = a("post-title") || norm((item.querySelector("[slot=title], h1, h3, a[data-testid='post-title']") || {}).innerText);
      const body = norm((item.querySelector("[slot='text-body'], [data-post-click-location='text-body'], .md") || {}).innerText);
      const sub = a("subreddit-prefixed-name") || norm((item.querySelector("a[href^='/r/']") || {}).innerText);
      return { ok: true, title, author: a("author"), subreddit: sub, score: a("score"), comments: a("comment-count"), body: body.slice(0, 1200) };
    }
    if (action === "upvote" || action === "downvote" || action === "unvote") {
      const up = button("upvote");
      const down = button("downvote");
      if (!up && !down) return { ok: false, reason: "I can't find this post's vote buttons" };
      if (action === "unvote") {
        const on = pressed(up) ? up : pressed(down) ? down : null;
        if (!on) return { ok: true, already: true };
        on.click();
        return { ok: true, was: on === up ? "upvote" : "downvote" };
      }
      const b = action === "upvote" ? up : down;
      if (!b) return { ok: false, reason: `I can't find the ${action} button` };
      if (pressed(b)) return { ok: true, already: true };
      b.click();
      return { ok: true };
    }
    if (action === "save" || action === "unsave") {
      // Save sits in the post's "..." menu: open it, then pick Save / Remove from saved
      const label = (b) => norm(b.innerText || b.getAttribute("aria-label")).toLowerCase();
      const isSave = (b) => /^save$/.test(label(b));
      const isSaved = (b) => /^(unsave|remove from saved|saved)$/.test(label(b));
      const items = () => [...inPost(item, "[role=menuitem], li, button"), ...deep(document, "[role=menuitem]")].filter(shown);
      let opened = false;
      if (!items().some((b) => isSave(b) || isSaved(b))) {
        const more = inPost(item, "button, [role=button]").find((b) => shown(b) &&
          /more options|user actions|overflow|^more$/i.test(b.getAttribute("aria-label") || ""));
        if (more) { more.click(); opened = true; await sleep(700); }
      }
      const close = () => opened && document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      const hit = items().find(action === "save" ? isSave : isSaved);
      if (hit) { hit.click(); return { ok: true }; }
      const other = items().find(action === "save" ? isSaved : isSave);
      close();
      if (other) return { ok: true, already: true };
      return { ok: false, reason: "I can't find Reddit's save option on this post" };
    }
    if (action === "pause" || action === "play") {
      const v = inPost(item, "video")[0] || deep(document, "video").find((x) => !x.paused);
      if (!v) return { ok: false, reason: "this post has no video" };
      action === "pause" ? v.pause() : v.play();
      return { ok: true };
    }
    return { ok: false, reason: `unknown action ${action}` };
  },

  // ---- X (Twitter): the post in view (article[data-testid=tweet]), its like / bookmark / repost buttons
  x: async function ({ action }) {
    if (!/(^|\.)(x|twitter)\.com$/.test(location.hostname)) return { ok: false, reason: "this tab isn't X" };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
    const shown = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const tid = (root, id) => [...root.querySelectorAll(`[data-testid='${id}']`)].find(shown) || null;

    if (action === "my_profile") {
      for (let i = 0; i < 24; i++) {        // X draws its side bar a moment after the page loads
        const a = document.querySelector("a[data-testid='AppTabBar_Profile_Link']");
        if (a) { a.click(); return { ok: true, url: a.href }; }
        await sleep(250);
      }
      return { ok: false, reason: "I can't find your profile link; are you signed in?" };
    }
    const tweets = () => {
      const all = [...document.querySelectorAll("article[data-testid='tweet']")].filter(shown);
      return all.length ? all : [...document.querySelectorAll("article")].filter(shown);
    };
    const LINE = Math.min(innerHeight * 0.35, 320);
    const current = () => {
      const all = tweets();
      const kept = window.__jarvisTweet;    // the one Jarvis moved to, while it's still on screen
      if (kept && all.includes(kept)) { const r = kept.getBoundingClientRect(); if (r.bottom > 90 && r.top < innerHeight - 90) return { item: kept, all }; }
      const dist = (el) => { const r = el.getBoundingClientRect(); return r.top <= LINE && r.bottom >= LINE ? 0 : Math.min(Math.abs(r.top - LINE), Math.abs(r.bottom - LINE)); };
      return { item: all.slice().sort((a, b) => dist(a) - dist(b))[0] || null, all };
    };
    const flash = (el) => { const o = el.style.outline; el.style.outline = "2px solid #1d9bf0"; setTimeout(() => { el.style.outline = o; }, 1500); };

    if (action === "next" || action === "previous") {
      const fwd = action === "next";
      const { item, all } = current();
      const ordered = all.slice().sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
      const target = item ? ordered[ordered.indexOf(item) + (fwd ? 1 : -1)] : null;
      if (!target) { window.__jarvisTweet = null; scrollBy({ top: (fwd ? 1 : -1) * innerHeight * 0.85, behavior: "smooth" }); return { ok: true, did: "scroll" }; }
      scrollBy({ top: target.getBoundingClientRect().top - 60, behavior: "smooth" });   // below the sticky header
      window.__jarvisTweet = target;
      flash(target);
      return { ok: true, did: "post" };
    }
    const { item } = current();
    if (!item) return { ok: false, reason: "I can't see a post on screen" };

    if (action === "read") {
      const nameBox = item.querySelector("[data-testid='User-Name']");
      const parts = nameBox ? nameBox.innerText.split("\n").map(norm).filter(Boolean) : [];
      return { ok: true, author: parts[0] || "", handle: parts.find((p) => p.startsWith("@")) || "",
               text: norm((item.querySelector("[data-testid='tweetText']") || {}).innerText),
               context: norm((item.querySelector("[data-testid='socialContext']") || {}).innerText),
               media: !!item.querySelector("[data-testid='tweetPhoto'], [data-testid='videoPlayer'], video"),
               liked: !!tid(item, "unlike"), reposted: !!tid(item, "unretweet"), saved: !!tid(item, "removeBookmark") };
    }
    if (action === "like" || action === "unlike") {
      const on = tid(item, "unlike");
      const off = tid(item, "like");
      if (action === "like" ? on : !on) return { ok: true, already: true };
      const b = action === "like" ? off : on;
      if (!b) return { ok: false, reason: "I can't find this post's like button" };
      b.click();
      return { ok: true };
    }
    if (action === "save" || action === "unsave") {
      let on = tid(item, "removeBookmark");
      let off = tid(item, "bookmark");
      if (!on && !off) {                    // some layouts keep Bookmark in the share menu
        const share = [...item.querySelectorAll("[aria-label*='share' i]")].find(shown);
        if (share) {
          share.click();
          await sleep(600);
          const pick = (re) => [...document.querySelectorAll("[role=menuitem]")].find((m) => shown(m) && re.test(norm(m.innerText)));
          on = pick(/^remove (from )?bookmarks?$/i);
          off = pick(/^bookmark$/i);
          if ((action === "save" && !off) || (action === "unsave" && !on)) document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        }
      }
      if (action === "save" ? on && !off : off && !on) return { ok: true, already: true };
      const b = action === "save" ? off : on;
      if (!b) return { ok: false, reason: "I can't find this post's bookmark button" };
      b.click();
      return { ok: true };
    }
    if (action === "repost") {
      if (tid(item, "unretweet")) return { ok: true, already: true };
      const b = tid(item, "retweet");
      if (!b) return { ok: false, reason: "this post can't be reposted" };
      b.click();
      let c = null;
      for (let i = 0; i < 12 && !c; i++) { await sleep(150); c = [...document.querySelectorAll("[data-testid='retweetConfirm']")].find(shown); }
      if (!c) {
        document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        return { ok: false, reason: "X didn't show its repost button" };
      }
      c.click();
      return { ok: true };
    }
    if (action === "pause" || action === "play") {
      const v = item.querySelector("video") || [...document.querySelectorAll("video")].find((x) => !x.paused);
      if (!v) return { ok: false, reason: "this post has no video" };
      action === "pause" ? v.pause() : v.play();
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
