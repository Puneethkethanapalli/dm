// dm Downloads: hands browser downloads to dm, the Omarchy bar download manager.
//
// When a download starts, the browser asks extensions for a file name before
// it asks you where to save it. In that moment the browser's copy is paused
// while dm checks it can fetch the file itself (with the page's cookies,
// referrer and user agent). Only once dm is actually receiving data is the
// browser's copy cancelled, before any "Save as" dialog can open. If dm is
// offline, the site refuses it, or it can't get going, the browser simply
// carries on, dialog and all: a download is never lost in the hand-over.
//
// The file name is versioned because Omarchy loads extensions with
// --load-extension: a new script URL forces the browser to pick up new code.
// Bump it (and the manifest version) whenever this file changes.

const HOST = "io.github.puneethkethanapalli.dm";
// The browser waits 15s for a file name, then goes ahead without one.
const WAIT_MS = 10000;
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

// The browser holds the download here, file name undecided, until suggest()
// is called. dm's downloads never get a name: they're cancelled instead, so
// the browser never reaches its "Save as" dialog.
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  takeOver(item)
    .catch(() => false)
    .then((taken) => {
      if (!taken) suggest(); // the browser's own name and, if set, its dialog
    });
  return true; // suggest() comes later
});

// True once dm has the download and the browser's copy is gone.
async function takeOver(item) {
  if (!(await takeoverEnabled())) return false;
  // Private windows stay private; other extensions' downloads are theirs.
  if (item.incognito || item.byExtensionId) return false;
  const url = item.finalUrl || item.url;
  // blob:, data: and friends only exist inside the browser.
  if (!/^https?:\/\//i.test(url) || item.state !== "in_progress") return false;

  try {
    await chrome.downloads.pause(item.id);
  } catch (e) {
    return false; // the user cancelled it already
  }
  // Until it has a file name the browser can't finish its copy, so there is
  // no race with it here: dm either takes the download or hands it back.
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
    return true;
  }
  if (reply.ok && !reply.existing) await native({ cmd: "remove", id: reply.job.id });
  try {
    await chrome.downloads.resume(item.id);
  } catch (e) {
    // the user cancelled it meanwhile
  }
  if (!reply.offline) await flag("↩", `kept in the browser (${reply.error || "dm couldn't start it"})`);
  return false;
}

// ---------------------------------------------------------- right-click menu

function registerMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "dm-download",
      title: "Download with dm",
      contexts: ["link", "image", "video", "audio"],
    });
    // Video sites (YouTube and the rest yt-dlp knows): the page, not its stream.
    chrome.contextMenus.create({
      id: "dm-video",
      title: "Download video with dm",
      contexts: ["page", "video", "link"],
    });
  });
}

chrome.runtime.onInstalled.addListener(registerMenus);
chrome.runtime.onStartup.addListener(registerMenus);

// Send one URL to dm; media: true makes it a yt-dlp video download.
async function sendToDm(url, referrer, isMedia) {
  if (!url || !/^https?:\/\//i.test(url)) {
    await flag("!", "only web links can be sent to dm");
    return;
  }
  const message = isMedia ? { cmd: "add", url, media: true } : { cmd: "add", ...(await details(url, referrer)) };
  const reply = await native(message);
  if (!reply.ok) await flag("!", reply.error || "dm isn't running");
}

chrome.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId === "dm-download") {
    await sendToDm(info.linkUrl || info.srcUrl, info.pageUrl, false);
  } else if (info.menuItemId === "dm-video") {
    // A <video> element's source is usually a blob or one stream segment: use the page.
    await sendToDm(info.linkUrl || info.pageUrl, info.pageUrl, true);
  }
});

// The popup's "Download this page's video" button.
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message && message.cmd === "video") {
    native({ cmd: "add", url: message.url, media: true }).then(respond);
    return true;
  }
});
