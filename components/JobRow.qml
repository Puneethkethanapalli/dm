import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// One download: name, actions, progress line and status.
Item {
  id: row

  property var job: ({})
  property var service: null
  property real now: 0
  property color foreground: Color.foreground
  property color urgent: Color.urgent
  property color highlight: Color.bar.active
  property string fontFamily: Style.font.family

  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property string status: job.status || ""
  readonly property real fraction: status === "done" ? 1 : Model.fraction(job)
  readonly property bool running: ["queued", "active", "checking", "retrying"].indexOf(status) >= 0

  implicitHeight: content.implicitHeight

  Column {
    id: content
    width: parent.width
    spacing: Style.space(4)

    Row {
      width: parent.width
      spacing: Style.space(6)

      Text {
        id: name
        width: parent.width - actions.width - parent.spacing
        anchors.verticalCenter: parent.verticalCenter
        text: Model.displayName(row.job)
        textFormat: Text.PlainText
        elide: Text.ElideMiddle
        color: row.status === "cancelled" ? row.dim : row.foreground
        font.family: row.fontFamily
        font.pixelSize: Style.font.body
      }

      Row {
        id: actions
        anchors.verticalCenter: parent.verticalCenter
        // Commands need the engine; done files can still be opened.
        readonly property bool engineUp: row.service !== null && row.service.engineState === "ok"
        spacing: Style.space(2)

        PanelActionButton {
          visible: row.running
          iconText: "󰏤"
          tooltipText: Model.cantResume(row.job)
            ? "Pause (this link can't resume, so it will start over from the beginning)"
            : "Pause"
          enabled: actions.engineUp
          foreground: row.foreground
          fontFamily: row.fontFamily
          onClicked: row.service.pause(row.job.id)
        }
        PanelActionButton {
          visible: row.status === "paused"
          iconText: "󰐊"
          tooltipText: "Resume"
          enabled: actions.engineUp
          foreground: row.foreground
          fontFamily: row.fontFamily
          onClicked: row.service.resume(row.job.id)
        }
        PanelActionButton {
          visible: row.status === "failed" || row.status === "cancelled"
          iconText: "󰑐"
          tooltipText: "Try again"
          enabled: actions.engineUp
          foreground: row.foreground
          fontFamily: row.fontFamily
          onClicked: row.service.retry(row.job.id)
        }
        PanelActionButton {
          visible: row.status === "done"
          iconText: "󰈔"
          tooltipText: "Open file"
          foreground: row.foreground
          fontFamily: row.fontFamily
          onClicked: row.service.openPath(row.job.file)
        }
        PanelActionButton {
          visible: row.status === "done"
          iconText: "󰉋"
          tooltipText: "Show in folder"
          foreground: row.foreground
          fontFamily: row.fontFamily
          onClicked: row.service.openFolder(row.job)
        }
        PanelActionButton {
          visible: Model.unfinished(row.status) && row.status !== "finishing"
          iconText: "󰅖"
          tooltipText: "Cancel and delete the partial file"
          enabled: actions.engineUp
          foreground: row.foreground
          hoverColor: row.urgent
          fontFamily: row.fontFamily
          onClicked: row.service.cancel(row.job.id)
        }
        PanelActionButton {
          visible: !Model.unfinished(row.status)
          iconText: "󰆴"
          tooltipText: "Remove from the list (the file stays)"
          enabled: actions.engineUp
          foreground: row.foreground
          fontFamily: row.fontFamily
          onClicked: row.service.remove(row.job.id)
        }
      }
    }

    // Progress line: solid while known, absent when the size is unknown.
    Rectangle {
      width: parent.width
      height: Style.space(3)
      radius: height / 2
      // A paused can't-resume download will start over, so its progress means nothing.
      visible: row.fraction >= 0 && row.status !== "done" && row.status !== "cancelled"
        && !(row.status === "paused" && Model.cantResume(row.job))
      color: Qt.rgba(row.foreground.r, row.foreground.g, row.foreground.b, 0.12)

      Rectangle {
        height: parent.height
        radius: parent.radius
        width: parent.width * Math.max(0, row.fraction)
        color: row.status === "failed" ? row.urgent : (row.status === "paused" ? row.dim : row.highlight)
        Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
      }
    }

    Text {
      width: parent.width
      text: Model.statusLine(row.job, row.now)
      textFormat: Text.PlainText
      elide: Text.ElideRight
      color: row.status === "failed" ? row.urgent : row.dim
      font.family: row.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      width: parent.width
      visible: text !== ""
      text: row.status === "failed" && row.job.error && row.job.error.detail ? row.job.error.detail
        : (row.job.note && row.status !== "failed" ? row.job.note : "")
      textFormat: Text.PlainText
      wrapMode: Text.WordWrap
      maximumLineCount: 2
      elide: Text.ElideRight
      color: row.dim
      font.family: row.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}
