const HOST = "io.github.puneethkethanapalli.dm";

function native(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST, message, (reply) => {
      const error = chrome.runtime.lastError;
      resolve(error || !reply ? { ok: false, error: error ? error.message : "No reply" } : reply);
    });
  });
}

const status = document.getElementById("status");
const takeover = document.getElementById("takeover");

chrome.storage.local.get("takeover").then(({ takeover: on = true }) => {
  takeover.checked = on;
});
takeover.addEventListener("change", () => chrome.storage.local.set({ takeover: takeover.checked }));

const videoButton = document.getElementById("video");
const note = document.getElementById("note");

function say(text, bad) {
  note.textContent = text;
  note.classList.toggle("bad", !!bad);
  note.hidden = false;
}

chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
  const url = tab && tab.url;
  if (!url || !/^https?:\/\//i.test(url)) {
    videoButton.disabled = true;
    return;
  }
  videoButton.addEventListener("click", async () => {
    videoButton.disabled = true;
    const reply = await chrome.runtime.sendMessage({ cmd: "video", url });
    if (reply && reply.ok) say(reply.existing ? "Already downloading this video." : "Sent to dm. Watch it in the bar.");
    else say((reply && reply.error) || "dm couldn't take this video.", true);
  });
});

document.getElementById("show").addEventListener("click", async () => {
  await native({ cmd: "show" });
  window.close();
});

native({ cmd: "ping" }).then((reply) => {
  if (reply.ok) {
    status.textContent = `Connected to dm ${reply.version}`;
  } else {
    status.classList.add("bad");
    status.textContent = /not found|forbidden/i.test(reply.error || "")
      ? "dm's browser connector isn't installed. Run dm's setup, then restart the browser."
      : "dm isn't running, so downloads stay in the browser.";
  }
});
