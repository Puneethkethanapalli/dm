import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "components"

// Bar entry point: the icon, a thin progress line, and an eagerly loaded panel
// (built once per monitor). State lives in Service.qml so every monitor agrees.
// The icon moves with the downloads: the arrow drops in when one is added and
// dips now and then while data comes in; a finished download flashes a tick,
// a failed one shakes it. Each of those also brings up a notice underneath.
BarWidget {
  id: root
  moduleName: "io.github.puneethkethanapalli.dm"

  readonly property var service: bar && bar.shell ? bar.shell.serviceFor(root.moduleName) : null
  readonly property bool busy: service ? service.busyCount > 0 : false
  readonly property bool problem: service ? (service.unseenFailures > 0 || service.engineState !== "ok") : false
  readonly property real progress: service && busy ? service.progress : -1
  readonly property bool hideWhenIdle: String(setting("hideWhenIdle", false)) === "true"
  // The tick after a download finishes stays lit even when nothing else is busy.
  property bool celebrating: false
  // Lit while downloading: the bar's own colour for modules that are active.
  // Problems use the theme's red and a warning glyph, so they stand apart
  // even in themes where the two colours are the same.
  readonly property color lit: bar ? bar.urgent : Color.bar.active

  // The bar finds a widget's panel through these members.
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  // The panel is loaded standalone, so hand it what it cannot reach itself.
  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    target.bar = root.bar
    target.settings = root.settings
    target.anchorItem = button
    target.hostWidget = root
  }

  visible: !hideWhenIdle || busy || problem || opened || celebrating || (service && service.pausedCount > 0)
  implicitWidth: vertical ? barSize : Style.bar.statusSlot
  implicitHeight: vertical ? Style.bar.statusSlot : barSize

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    slotSize: Style.bar.statusSlot
    text: "󰇚"
    iconComponent: liveIcon
    dimmed: !root.busy && !root.problem && !root.celebrating
    active: root.busy || root.problem || root.celebrating
    activeColor: root.problem ? Color.urgent : root.lit
    tooltipText: Model.tooltip(root.service)
    onPressed: function(b) {
      // Right click grabs whatever link is on the clipboard.
      if (b === Qt.RightButton && root.service) root.service.addClipboard()
      else root.toggle()
    }
  }

  Component {
    id: liveIcon

    Item {
      id: icon

      readonly property color tint: button.active && button.useActiveColor ? button.activeColor : button.foreground
      readonly property bool receiving: root.service ? root.service.downloading > 0 : false
      property bool ticked: false

      function arrive() {
        finished.stop()
        root.celebrating = false
        icon.ticked = false
        glyph.scale = 1
        landing.restart()
      }

      function finish() {
        landing.complete()
        finished.restart()
      }

      Connections {
        target: root.service
        function onJobAdded() { icon.arrive() }
        function onJobDone() { icon.finish() }
        function onJobFailed() { shake.restart() }
      }

      // Spreads out from the icon as a download lands or finishes.
      Rectangle {
        id: ripple
        anchors.centerIn: parent
        width: parent.width
        height: parent.height
        radius: width / 2
        color: "transparent"
        border.width: 1.5
        border.color: root.lit
        opacity: 0
      }

      OpticalGlyph {
        id: glyph
        anchors.fill: parent
        text: icon.ticked ? "󰄬" : (root.problem ? "󰀦" : button.text)
        fontFamily: button.fontFamily
        fontSize: button.fontSize
        color: icon.ticked ? root.lit : icon.tint
        transform: [
          Translate { id: drop },
          Translate { id: dip },
          Translate { id: jolt }
        ]
      }

      ParallelAnimation {
        id: ring
        NumberAnimation { target: ripple; property: "scale"; from: 0.6; to: 1.6; duration: 650; easing.type: Easing.OutCubic }
        NumberAnimation { target: ripple; property: "opacity"; from: 0.9; to: 0; duration: 650; easing.type: Easing.OutCubic }
      }

      // A download was added: the arrow falls in and bounces on the tray.
      ParallelAnimation {
        id: landing
        NumberAnimation { target: drop; property: "y"; from: -8; to: 0; duration: 550; easing.type: Easing.OutBounce }
        NumberAnimation { target: glyph; property: "opacity"; from: 0; to: 1; duration: 160 }
        SequentialAnimation {
          PauseAnimation { duration: 200 } // the first bounce
          ScriptAction { script: ring.restart() }
        }
      }

      // Data is coming in: a short dip every couple of seconds.
      SequentialAnimation {
        running: icon.receiving
        loops: Animation.Infinite
        onRunningChanged: if (!running) dip.y = 0
        NumberAnimation { target: dip; property: "y"; to: 1.5; duration: 280; easing.type: Easing.InOutSine }
        NumberAnimation { target: dip; property: "y"; to: 0; duration: 320; easing.type: Easing.InOutSine }
        PauseAnimation { duration: 1600 }
      }

      // A download finished: a tick pops up for a moment.
      SequentialAnimation {
        id: finished
        ScriptAction { script: { root.celebrating = true; icon.ticked = true; ring.restart() } }
        NumberAnimation { target: glyph; property: "scale"; from: 0.3; to: 1; duration: 450; easing.type: Easing.OutBack; easing.overshoot: 3 }
        PauseAnimation { duration: 1800 }
        NumberAnimation { target: glyph; property: "opacity"; to: 0; duration: 140 }
        ScriptAction { script: icon.ticked = false }
        NumberAnimation { target: glyph; property: "opacity"; to: 1; duration: 180 }
        ScriptAction { script: root.celebrating = false }
      }

      // A download failed (the icon also turns red until the panel is opened).
      SequentialAnimation {
        id: shake
        NumberAnimation { target: jolt; property: "x"; to: -2; duration: 45 }
        NumberAnimation { target: jolt; property: "x"; to: 2; duration: 90 }
        NumberAnimation { target: jolt; property: "x"; to: -2; duration: 90 }
        NumberAnimation { target: jolt; property: "x"; to: 2; duration: 90 }
        NumberAnimation { target: jolt; property: "x"; to: 0; duration: 45 }
      }
    }
  }

  // Overall progress, under the icon (beside it on a vertical bar).
  Rectangle {
    id: track
    visible: root.progress >= 0
    color: Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.2)
    radius: 1
    width: root.vertical ? 2 : Math.round(root.width * 0.5)
    height: root.vertical ? Math.round(root.height * 0.5) : 2
    anchors.horizontalCenter: root.vertical ? undefined : parent.horizontalCenter
    anchors.verticalCenter: root.vertical ? parent.verticalCenter : undefined
    anchors.bottom: root.vertical ? undefined : parent.bottom
    anchors.bottomMargin: Math.round(root.height * 0.18)
    anchors.right: root.vertical ? parent.right : undefined
    anchors.rightMargin: Math.round(root.width * 0.18)

    Rectangle {
      color: root.lit
      radius: 1
      width: root.vertical ? parent.width : Math.round(parent.width * Math.max(0, root.progress))
      height: root.vertical ? Math.round(parent.height * Math.max(0, root.progress)) : parent.height
      anchors.bottom: parent.bottom
      anchors.left: parent.left
      Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
      Behavior on height { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
    }
  }

  Toast {
    id: toast
    anchorItem: button
    host: root
    service: root.service
    highlight: root.lit
  }

  Connections {
    target: root.service
    function onJobAdded(job) { toast.show("added", job) }
    function onJobDone(job) { toast.show("done", job) }
    function onJobFailed(job) { toast.show("failed", job) }
  }
}
