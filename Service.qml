import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "components"

// One instance per shell. The downloads themselves run in the dm engine (a
// systemd user service), never in here: the shell reloads plugins whenever a
// plugin file changes, and anything this process starts dies with it. This
// only reads the engine's snapshot and sends it `dm` commands. Every bar
// widget (one per monitor) shares it through bar.shell.serviceFor(id).
Item {
  id: root

  // Injected by omarchy-shell's service loader.
  property var shell: null
  property var manifest: null

  function localPath(name) {
    return decodeURIComponent(Qt.resolvedUrl(name).toString().replace(/^file:\/\//, ""))
  }

  readonly property string dmPath: localPath("bin/dm")
  readonly property string setupPath: localPath("setup")
  readonly property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR") || "/run/user/1000"
  readonly property string statePath: runtimeDir + "/dm/state.json"

  // ---------------------------------------------------------------- state

  property var snapshot: null
  property bool installed: true
  property real clock: Date.now() / 1000
  property string lastError: ""

  // The engine rewrites the snapshot at least every 5 s; older means it's gone.
  readonly property bool live: snapshot !== null && snapshot.running === true
    && clock - Number(snapshot.updated || 0) < 12
  // "missing": never set up; "offline": set up but not running;
  // "engine": running but aria2 is down; "ok".
  readonly property string engineState: !live ? (installed ? "offline" : "missing")
    : (snapshot.engine && snapshot.engine.ok ? "ok" : "engine")
  readonly property string engineError: live && snapshot.engine ? String(snapshot.engine.error || "") : ""
  readonly property bool online: live ? snapshot.online !== false : true
  readonly property var jobs: snapshot && Array.isArray(snapshot.jobs) ? snapshot.jobs : []
  readonly property var counts: live && snapshot.counts ? snapshot.counts : ({})
  readonly property int downloading: (counts.active || 0) + (counts.checking || 0) + (counts.finishing || 0)
  readonly property int waiting: (counts.queued || 0) + (counts.retrying || 0)
  readonly property int busyCount: downloading + waiting
  readonly property int pausedCount: counts.paused || 0
  readonly property int failedCount: counts.failed || 0
  // Failures stay in the list, but only new ones (since the panel was last
  // open) turn the bar icon red.
  property int seenFailures: 0
  readonly property int unseenFailures: Math.max(0, failedCount - seenFailures)
  onFailedCountChanged: if (failedCount < seenFailures) seenFailures = failedCount
  function acknowledgeFailures() { seenFailures = failedCount }
  readonly property int finishedCount: (counts.done || 0) + (counts.failed || 0) + (counts.cancelled || 0)
  readonly property real speed: live ? Number(snapshot.speed || 0) : 0
  readonly property real progress: live ? Number(snapshot.progress) : -1
  readonly property string downloadDir: live ? String(snapshot.download_dir || "") : ""
  // aria2 syntax: "0" (no limit), "500K", "2M"...
  readonly property string speedLimit: live ? String(snapshot.speed_limit || "0") : "0"
  // After `omarchy plugin update` the old engine keeps running until restarted.
  readonly property bool outdated: live && manifest && manifest.version
    && String(snapshot.version) !== String(manifest.version)

  // ------------------------------------------------------------- commands

  // Commands run one at a time, in order.
  property var _queue: []
  property var _callback: null

  function dm(args, callback) {
    root._queue = root._queue.concat([{ argv: ["python3", "-B", root.dmPath, "--json"].concat(args), callback: callback || null }])
    root._pump()
  }

  function run(argv, callback) {
    root._queue = root._queue.concat([{ argv: argv, callback: callback || null }])
    root._pump()
  }

  function _pump() {
    if (job.busy || root._queue.length === 0) return
    var next = root._queue[0]
    root._queue = root._queue.slice(1)
    root._callback = next.callback
    job.run(next.argv)
  }

  function parse(output) {
    try { return JSON.parse(output) } catch (e) { return null }
  }

  function report(ok, reply, error) {
    root.lastError = ok ? "" : String((reply && reply.error) || error || "Command failed")
  }

  function add(url, callback) {
    var text = String(url || "").trim()
    if (text === "") return
    dm(["add"].concat(text.split(/\s+/)), function(ok, reply, error) {
      root.report(ok, reply, error)
      if (callback) callback(ok, reply, error)
    })
  }

  function addClipboard(callback) {
    dm(["add", "--clipboard"], function(ok, reply, error) {
      root.report(ok, reply, error)
      if (callback) callback(ok, reply, error)
    })
  }

  function act(verb, id) { dm([verb, id], root.report) }
  function pause(id) { act("pause", id) }
  function resume(id) { act("resume", id) }
  function cancel(id) { act("cancel", id) }
  function retry(id) { act("retry", id) }
  function remove(id) { act("remove", id) }
  function clearFinished() { act("remove", "finished") }
  function setLimit(value) { dm(["limit", value], root.report) }

  // Detached, so the file manager or viewer outlives a shell reload.
  function openPath(path) { if (path) Util.execArgv(["xdg-open", path]) }
  function openFolder(job) {
    if (job && job.file) openPath(job.file.substring(0, job.file.lastIndexOf("/")))
    else openPath(job && job.dir ? job.dir : root.downloadDir)
  }

  function startEngine() { run(["systemctl", "--user", "start", "dm.service"], root.afterEngineCommand) }
  function restartEngine() { run(["systemctl", "--user", "restart", "dm.service"], root.afterEngineCommand) }
  function afterEngineCommand(ok, reply, error) {
    root.lastError = ok ? "" : (error || "Could not start the dm engine")
    stateFile.reload()
  }
  function runSetup() {
    Util.execArgv(["omarchy-launch-floating-terminal-with-presentation", "bash " + Util.shellQuote(root.setupPath)])
  }

  CommandJob {
    id: job
    timeoutMs: 20000
    onFinished: function(ok, output, error) {
      var reply = root.parse(output)
      var success = ok && (reply === null || reply.ok !== false)
      var callback = root._callback
      root._callback = null
      if (callback) callback(success, reply, reply && reply.error ? reply.error : error)
      root._pump()
    }
  }

  // ----------------------------------------------------------- snapshot

  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: true
    printErrors: false
    // The engine replaces the file by rename, which drops the watch; reload re-arms it.
    onFileChanged: reload()
    onLoaded: {
      var parsed = root.parse(text())
      if (parsed) root.snapshot = parsed
      root.clock = Date.now() / 1000
    }
    onLoadFailed: root.snapshot = null
  }

  Timer {
    interval: 1000
    repeat: true
    running: true
    onTriggered: {
      root.clock = Date.now() / 1000
      // A missing file can't be watched: look again until the engine appears.
      if (!root.live) stateFile.reload()
    }
  }

  // Is the engine set up at all? Decides between "Set up" and "Start".
  CommandJob {
    id: unitCheck
    timeoutMs: 5000
    onFinished: function(ok) { root.installed = ok }
  }

  Timer {
    interval: 5000
    repeat: true
    running: !root.live
    triggeredOnStart: true
    onTriggered: if (!unitCheck.busy) unitCheck.run(["systemctl", "--user", "cat", "dm.service"])
  }

  // `omarchy-shell downloads add <url>` and friends, e.g. from a key binding.
  IpcHandler {
    target: "downloads"

    function add(url: string): void { root.add(url) }
    function clipboard(): void { root.addClipboard() }
    function pauseAll(): void { root.pause("all") }
    function resumeAll(): void { root.resume("all") }
  }
}
