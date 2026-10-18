import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Bar entry point: the icon, a thin progress line, and an eagerly loaded panel
// (built once per monitor). State lives in Service.qml so every monitor agrees.
BarWidget {
  id: root
  moduleName: "io.github.puneethkethanapalli.dm"

  readonly property var service: bar && bar.shell ? bar.shell.serviceFor(root.moduleName) : null
  readonly property bool busy: service ? service.busyCount > 0 : false
  readonly property bool problem: service ? (service.unseenFailures > 0 || service.engineState !== "ok") : false
  readonly property real progress: service && busy ? service.progress : -1
  readonly property bool hideWhenIdle: String(setting("hideWhenIdle", false)) === "true"

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

  visible: !hideWhenIdle || busy || problem || opened || (service && service.pausedCount > 0)
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
    dimmed: !root.busy && !root.problem
    active: root.busy || root.problem
    activeColor: root.problem ? (root.bar ? root.bar.urgent : Color.urgent) : Color.accent
    tooltipText: Model.tooltip(root.service)
    onPressed: function(b) {
      // Right click grabs whatever link is on the clipboard.
      if (b === Qt.RightButton && root.service) root.service.addClipboard()
      else root.toggle()
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
      color: Color.accent
      radius: 1
      width: root.vertical ? parent.width : Math.round(parent.width * Math.max(0, root.progress))
      height: root.vertical ? Math.round(parent.height * Math.max(0, root.progress)) : parent.height
      anchors.bottom: parent.bottom
      anchors.left: parent.left
    }
  }
}
