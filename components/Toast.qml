import QtQuick
import Quickshell
import Quickshell.Hyprland
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// A short notice under the bar icon when a download is added, finishes or
// fails. It's passive: it takes no focus, never closes a panel you have open,
// stays while the pointer is on it, and a click opens the Downloads panel.
PopupCard {
  id: toast

  property var host: null // the BarWidget
  property var service: null
  property color highlight: Color.accent

  property string kind: "" // "added", "done" or "failed"
  property var shown: null // the job as it was when the notice came up
  property int count: 0 // downloads added while the notice was up

  // The job as it is now, so the progress moves while the notice is up.
  readonly property var job: {
    var jobs = service ? service.jobs : []
    for (var i = 0; i < jobs.length; i++) {
      if (shown && jobs[i].id === shown.id) return jobs[i]
    }
    return shown || ({})
  }
  readonly property color foreground: host && host.bar ? host.bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property string fontFamily: host && host.bar ? host.bar.fontFamily : Style.font.family
  readonly property real fraction: Model.fraction(job)

  function show(kind, job) {
    if (!host || host.opened) return // the panel shows it already
    if (host.bar && host.bar.activePopout) return // don't cover another open panel
    if (!onFocusedScreen()) return
    var another = kind === "added" && open && toast.kind === "added" && shown && shown.id !== job.id
    toast.count = another ? toast.count + 1 : 1
    toast.kind = kind
    toast.shown = job
    toast.open = true
    hide.interval = kind === "failed" ? 8000 : 5000
    hide.restart()
  }

  // One notice, on the screen you're using, not one per monitor.
  function onFocusedScreen() {
    var window = anchorItem ? anchorItem.QsWindow.window : null
    var focused = Hyprland.focusedMonitor
    return !window || !window.screen || !focused || window.screen.name === focused.name
  }

  // PopupCard tells its bar when it opens, and the bar then closes whatever
  // other panel is open. A notice mustn't, so it reports to a stand-in.
  bar: QtObject {
    readonly property string position: toast.host && toast.host.bar ? toast.host.bar.position : "top"
    property var activePopout: null
    function requestPopout(owner) {}
    function releasePopout(owner) {}
  }
  triggerMode: "hover"
  contentWidth: toast.fittedContentWidth(Style.space(300))
  contentHeight: toast.fittedContentHeight(body.implicitHeight)

  // The card only takes visual children, so the helpers live in here too.
  Item {
    anchors.fill: parent

    Timer {
      id: hide
      onTriggered: if (toast.containsMouse) restart(); else toast.open = false
    }

    Connections {
      target: toast.host
      function onOpenedChanged() { if (toast.host.opened) toast.open = false }
    }

    Row {
      id: body
      width: parent.width
      spacing: Style.space(10)

      Text {
        id: glyph
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(30)
        horizontalAlignment: Text.AlignHCenter
        text: toast.kind === "done" ? "󰄬" : (toast.kind === "failed" ? "󰀦" : "󰇚")
        textFormat: Text.PlainText
        color: toast.kind === "failed" ? Color.urgent : toast.highlight
        font.family: toast.fontFamily
        font.pixelSize: Style.font.display
      }

      Column {
        width: parent.width - glyph.width - parent.spacing
        spacing: Style.space(3)

        Text {
          width: parent.width
          text: toast.kind === "done" ? "Download complete"
            : toast.kind === "failed" ? "Download failed"
            : toast.count > 1 ? toast.count + " downloads added"
            : toast.job.source === "browser" ? "Downloading from the browser" : "Downloading"
          textFormat: Text.PlainText
          elide: Text.ElideRight
          color: toast.foreground
          font.family: toast.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }

        Text {
          width: parent.width
          text: Model.displayName(toast.job)
          textFormat: Text.PlainText
          elide: Text.ElideMiddle
          color: toast.foreground
          font.family: toast.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Rectangle {
          width: parent.width
          height: Style.space(3)
          radius: height / 2
          visible: toast.kind === "added" && toast.fraction >= 0
          color: Qt.rgba(toast.foreground.r, toast.foreground.g, toast.foreground.b, 0.12)

          Rectangle {
            height: parent.height
            radius: parent.radius
            width: parent.width * Math.max(0, toast.fraction)
            color: toast.highlight
            Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
          }
        }

        Text {
          width: parent.width
          text: !toast.job.id ? ""
            : toast.kind === "failed"
            ? (toast.job.error && toast.job.error.text ? toast.job.error.text : "Unknown error")
            : Model.statusLine(toast.job, toast.service ? toast.service.clock : 0)
          textFormat: Text.PlainText
          wrapMode: Text.WordWrap
          maximumLineCount: 2
          elide: Text.ElideRight
          color: toast.kind === "failed" ? Color.urgent : toast.dim
          font.family: toast.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }

    MouseArea {
      anchors.fill: parent
      cursorShape: Qt.PointingHandCursor
      onClicked: {
        toast.open = false
        if (toast.host) toast.host.open()
      }
    }
  }
}
