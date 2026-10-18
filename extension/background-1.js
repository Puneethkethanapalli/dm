// dm Downloads: hands browser downloads to dm, the Omarchy bar download manager.
//
// When a download starts, the browser's copy is paused while dm checks it can
// fetch the file itself (with the page's cookies, referrer and user agent).
// Only once dm is actually receiving data is the browser's copy cancelled.
// If dm is offline, the site refuses it, or it can't get going, the browser
// simply carries on: a download is never lost in the hand-over.
//
// The file name is versioned because Omarchy loads extensions with
// --load-extension: a new script URL forces the browser to pick up new code.

const HOST = "io.github.puneethkethanapalli.dm";
const WAIT_MS = 15000;
const POLL_MS = 500;

function native(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST, message, (reply) => {
      const error = chrome.runtime.lastError;
      if (error || !reply) {
        resolve({ ok: false, offline: true, error: error ? error.message : "No reply from dm" });
      } else {
        resolve(reply);
      }
    });
  });
}

async function takeoverEnabled() {
  const { takeover = true } = await chrome.storage.local.get("takeover");
  return takeover;
}

// The cookies the browser itself would send to this URL, logins included.
async function cookieHeader(url) {
  try {
    const cookies = await chrome.cookies.getAll({ url });
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (e) {
    return "";
  }
}

async function details(url, referrer) {
  return {
    url,
    cookies: await cookieHeader(url),
    referer: referrer || "",
    user_agent: navigator.userAgent,
  };
}

function baseName(path) {
  return path ? path.split(/[\\/]/).pop() : "";
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// True once dm is really downloading (or finished, or queued behind other
// downloads); false if it failed straight away.
async function startedInDm(id) {
  const deadline = Date.now() + WAIT_MS;
  let last = null;
  while (Date.now() < deadline) {
    const reply = await native({ cmd: "job", id });
    last = reply.ok ? reply.job : null;
    if (!last) return false;
    if (last.status === "done") return true;
    if (last.status === "active" && last.completed > 0) return true;
    if (["failed", "cancelled", "retrying"].includes(last.status)) return false;
    await sleep(POLL_MS);
  }
  return last !== null && ["queued", "active", "checking"].includes(last.status);
}

async function flag(text, title) {
  await chrome.action.setBadgeBackgroundColor({ color: "#a55555" });
  await chrome.action.setBadgeText({ text });
  await chrome.action.setTitle({ title: `dm Downloads: ${title}` });
  setTimeout(() => {
    chrome.action.setBadgeText({ text: "" });
    chrome.action.setTitle({ title: "dm Downloads" });
  }, 8000);
}

// ------------------------------------------------------- automatic takeover

const handling = new Set();

chrome.downloads.onCreated.addListener(async (item) => {
  if (handling.has(item.id) || !(await takeoverEnabled())) return;
  // Private windows stay private; other extensions' downloads are theirs.
  if (item.incognito || item.byExtensionId) return;
  const url = item.finalUrl || item.url;
  // blob:, data: and friends only exist inside the browser.
  if (!/^https?:\/\//i.test(url) || item.state !== "in_progress") return;

  handling.add(item.id);
  try {
    try {
      await chrome.downloads.pause(item.id);
    } catch (e) {
      return; // already finished: tiny files beat us to it
    }
    const reply = await native({
      cmd: "add",
      ...(await details(url, item.referrer)),
      mime: item.mime || "",
      name: baseName(item.filename),
      verify: true,
    });
    if (reply.ok && (reply.existing || (await startedInDm(reply.job.id)))) {
      await chrome.downloads.cancel(item.id);
      await chrome.downloads.erase({ id: item.id });
      return;
    }
    if (reply.ok && !reply.existing) await native({ cmd: "remove", id: reply.job.id });
    try {
      await chrome.downloads.resume(item.id);
    } catch (e) {
      // the user cancelled it meanwhile
    }
    if (!reply.offline) await flag("↩", `kept in the browser (${reply.error || "dm couldn't start it"})`);
  } finally {
    handling.delete(item.id);
  }
});

// ---------------------------------------------------------- right-click menu

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "dm-download",
    title: "Download with dm",
    contexts: ["link", "image", "video", "audio"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== "dm-download") return;
  const url = info.linkUrl || info.srcUrl;
  if (!url || !/^https?:\/\//i.test(url)) {
    await flag("!", "only web links can be sent to dm");
    return;
  }
  const reply = await native({ cmd: "add", ...(await details(url, info.pageUrl)) });
  if (!reply.ok) await flag("!", reply.error || "dm isn't running");
});
