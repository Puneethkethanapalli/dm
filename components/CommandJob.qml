import QtQuick
import Quickshell.Io

// Runs one argv at a time with a deadline and reports exactly once through
// finished(ok, output, error). Stdout and the exit status arrive on separate
// signals in no fixed order, so completion waits for both.
Item {
  id: root

  property int timeoutMs: 10000
  readonly property bool busy: root._busy
  signal finished(bool ok, string output, string error)

  property bool _busy: false
  property bool _exited: false
  property bool _streamDone: false
  property bool _success: false
  property string _failure: ""

  function run(argv) {
    if (root._busy || !Array.isArray(argv) || argv.length === 0) return false
    root._busy = true
    root._exited = false
    root._streamDone = false
    root._success = false
    root._failure = ""
    proc.command = argv
    proc.running = true
    deadline.restart()
    return true
  }

  function _finish() {
    if (!root._busy || !root._exited || !root._streamDone) return
    deadline.stop()
    var output = String(out.text || "").trim()
    var error = root._failure || String(err.text || "").trim()
    root._busy = false
    root.finished(root._success && !root._failure, output, error)
  }

  Timer {
    id: deadline
    repeat: false
    interval: root.timeoutMs
    onTriggered: {
      root._failure = "command timed out"
      if (proc.running) proc.signal(15)
    }
  }

  Process {
    id: proc
    running: false
    onExited: function(code, status) {
      root._success = code === 0
      root._exited = true
      root._finish()
    }
    // A command that never starts flips running back without exited().
    onRunningChanged: if (!running) Qt.callLater(function() {
      if (!root._busy || root._exited) return
      root._exited = true
      root._streamDone = true
      if (!root._failure) root._failure = "command failed to start"
      root._finish()
    })
    stdout: StdioCollector {
      id: out
      waitForEnd: true
      onStreamFinished: { root._streamDone = true; root._finish() }
    }
    stderr: StdioCollector { id: err }
  }
}
