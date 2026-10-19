import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "components"

// The Downloads panel: add a link, then watch, pause, resume, retry or open
// each download. Everything goes through the shared Service.qml.
Panel {
  id: root

  moduleName: "io.github.puneethkethanapalli.dm"

  // Handed over by BarWidget.injectPanel().
  property var anchorItem: null
  property var hostWidget: null

  readonly property var service: bar && bar.shell ? bar.shell.serviceFor(root.moduleName) : null
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  // As on the bar icon: the bar's highlight for progress, the theme's red for failures.
  readonly property color highlight: bar ? bar.urgent : Color.bar.active
  readonly property color urgent: Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool ready: service ? service.engineState === "ok" : false
  readonly property var jobs: service ? service.jobs : []
  readonly property var jobsById: {
    var byId = {}
    for (var i = 0; i < jobs.length; i++) byId[jobs[i].id] = jobs[i]
    return byId
  }
  // One row per download, kept across snapshots (see Model.syncRows).
  onJobsChanged: Model.syncRows(rows, jobs)
  Component.onCompleted: Model.syncRows(rows, jobs)

  ListModel { id: rows }

  property string notice: ""
  property bool noticeError: false

  function say(text, isError) {
    notice = String(text || "")
    noticeError = isError === true
    noticeTimer.restart()
  }

  function submit() {
    var text = input.text.trim()
    if (!service || text === "") return
    service.add(text, function(ok, reply, error) {
      if (!ok) return root.say(error || "Could not add that link", true)
      input.text = ""
      var job = reply && reply.job ? reply.job : null
      if (reply && reply.existing) root.say("Already downloading " + (job ? job.name : "that link") + ".", false)
      else if (job) root.say("Downloading " + job.name + ".", false)
    })
  }

  function fromClipboard() {
    if (!service) return
    service.addClipboard(function(ok, reply, error) {
      if (!ok) return root.say(error || "No link on the clipboard", true)
      var n = reply && reply.results ? reply.results.length : 1
      root.say(n === 1 && reply.job ? "Downloading " + reply.job.name + "." : "Added " + n + " downloads.", false)
    })
  }

  function addToBrowser() {
    if (!service) return
    service.addToBrowser(function(ok, reply, error) {
      if (!ok) return root.say(error || "Could not add the extension to the browser", true)
      root.say(Model.browserMessage(reply), false)
    })
  }

  onOpenedChanged: {
    if (service) service.acknowledgeFailures()
    if (opened) {
      notice = ""
      Qt.callLater(function() { input.forceActiveFocus() })
    }
  }

  Timer {
    id: noticeTimer
    interval: 6000
    repeat: false
    onTriggered: root.notice = ""
  }

  // ------------------------------------------------------------------ UI

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: input
    contentWidth: panel.fittedContentWidth(Style.space(460))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    Item {
      anchors.fill: parent
      focus: true
      Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Escape) {
          root.close()
          event.accepted = true
        }
      }

      Flickable {
        id: scroller
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: column
          width: scroller.width
          spacing: Style.space(10)

          PanelHero {
            width: parent.width
            title: "Downloads"
            meta: Model.heroLine(root.service)
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                text: "󰇚"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              Row {
                spacing: Style.space(2)
                visible: root.ready

                PanelActionButton {
                  visible: root.service && root.service.busyCount > 0
                  iconText: "󰏤"
                  tooltipText: "Pause all"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  onClicked: root.service.pause("all")
                }
                PanelActionButton {
                  visible: root.service && root.service.pausedCount > 0
                  iconText: "󰐊"
                  tooltipText: "Resume all"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  onClicked: root.service.resume("all")
                }
                PanelActionButton {
                  iconText: "󰉋"
                  tooltipText: "Open the download folder"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  onClicked: root.service.openFolder(null)
                }
              }
            }
          }

          // ---------- Engine not running ----------
          Column {
            width: parent.width
            spacing: Style.space(8)
            visible: root.service !== null && (!root.ready || root.service.outdated)

            Text {
              width: parent.width
              text: root.service ? (root.service.outdated
                ? "The plugin was updated, but the engine is still running the old version."
                : Model.engineMessage(root.service)) : ""
              textFormat: Text.PlainText
              wrapMode: Text.WordWrap
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Button {
              text: !root.service ? "" : root.service.engineState === "missing" ? "Set up"
                : (root.service.engineState === "offline" ? "Start engine" : "Restart engine")
              iconText: "󰐊"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: {
                if (root.service.engineState === "missing") {
                  root.close()
                  root.service.runSetup()
                } else if (root.service.engineState === "offline") {
                  root.service.startEngine()
                } else {
                  root.service.restartEngine()
                }
              }
            }
          }

          // ---------- Add ----------
          Row {
            width: parent.width
            spacing: Style.space(6)

            TextField {
              id: input
              width: parent.width - clipButton.width - parent.spacing
              enabled: root.ready
              placeholderText: root.ready ? "Paste a link or a video page, then Enter" : "Waiting for the engine"
              foreground: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              onAccepted: root.submit()
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) {
                  root.close()
                  event.accepted = true
                }
              }
            }

            Button {
              id: clipButton
              anchors.verticalCenter: parent.verticalCenter
              enabled: root.ready
              text: "Clipboard"
              iconText: "󰅍"
              tooltipText: "Download the link on the clipboard (or right-click the bar icon)"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.fromClipboard()
            }
          }

          Text {
            width: parent.width
            visible: text !== ""
            text: root.notice !== "" ? root.notice : (root.service ? root.service.lastError : "")
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: root.noticeError || (root.notice === "" && root.service && root.service.lastError !== "") ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          PanelSeparator { foreground: root.foreground; visible: root.jobs.length > 0 }

          // ---------- Jobs ----------
          Text {
            width: parent.width
            visible: root.ready && root.jobs.length === 0
            text: "No downloads yet. Paste a link or a video page above, start a download in your browser, or right-click the bar icon to download the link on your clipboard."
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Repeater {
            model: rows

            JobRow {
              required property string jobId
              width: column.width
              job: root.jobsById[jobId] || ({})
              service: root.service
              now: root.service ? root.service.clock : 0
              foreground: root.foreground
              urgent: root.urgent
              highlight: root.highlight
              fontFamily: root.fontFamily
            }
          }

          PanelSeparator { foreground: root.foreground; visible: root.ready }

          // ---------- Speed limit ----------
          Row {
            width: parent.width
            spacing: Style.space(10)
            visible: root.ready

            Text {
              anchors.verticalCenter: parent.verticalCenter
              text: "SPEED LIMIT"
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }

            ButtonGroup {
              anchors.verticalCenter: parent.verticalCenter
              options: Model.limitOptions(root.service ? root.service.speedLimit : "0")
              value: root.service ? root.service.speedLimit : "0"
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              focusable: false
              onChanged: function(value) { if (root.service) root.service.setLimit(value) }
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(6)
            visible: root.ready

            Button {
              visible: root.service && root.service.finishedCount > 0
              text: "Clear finished"
              iconText: "󰆴"
              tooltipText: "Remove finished downloads from the list; the files stay"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.service.clearFinished()
            }

            Button {
              text: "Add to browser"
              iconText: "󰐱"
              tooltipText: "Send Brave and Chromium downloads to dm (restart the browser afterwards)"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.addToBrowser()
            }
          }
        }
      }
    }
  }
}
