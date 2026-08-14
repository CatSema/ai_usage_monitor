import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

Item {
    id: compactRoot

    property var cd: root.claudeData || ({})
    property var od: root.codexData || ({})
    property var gd: root.geminiData || ({})

    property string configuredPanelTool: Plasmoid.configuration.panelTool || "codex"
    property int displayMode: Plasmoid.configuration.panelDisplayMode || 0

    function providerData(provider) {
        if (provider === "codex") return od
        if (provider === "gemini") return gd
        return cd
    }

    function providerIsAvailable(provider) {
        var data = providerData(provider)
        return data && data.installed === true
    }

    function pickPanelTool() {
        if (providerIsAvailable(configuredPanelTool))
            return configuredPanelTool

        if (providerIsAvailable("codex"))
            return "codex"

        if (providerIsAvailable("gemini"))
            return "gemini"

        if (providerIsAvailable("claude"))
            return "claude"

        return configuredPanelTool || "codex"
    }

    property string panelTool: pickPanelTool()
    property var activeData: providerData(panelTool)

    function hasUsableUsage(data) {
        if (!data || data.installed !== true) return false
        if (data.error) return false
        if (data.has_usage === false || data.usage_supported === false) return false
        if (data.used_pct === undefined || data.used_pct === null) return false
        return true
    }

    function geminiModeText() {
        var mode = (gd.auth_type || "").toLowerCase()

        if (mode === "api-key")
            return "API"

        if (mode === "vertex-ai")
            return "VTX"

        if (mode === "oauth-personal")
            return "OAuth"

        return "GEM"
    }

    property real activePct: {
        if (panelTool === "gemini") {
            if (!hasUsableUsage(gd))
                return 0

            return Math.min((gd.used_pct || 0), 100)
        }

        return Math.min((activeData.five_hour_pct || 0), 100)
    }

    property string activeText: {
        if (root.isLoading)
            return "…"

        if (!activeData || activeData.installed !== true)
            return "—"

        if (activeData.error)
            return "!"

        if (panelTool === "gemini") {
            if (!hasUsableUsage(gd))
                return gd.authenticated === true ? geminiModeText() : "!"

            return gd.used_pct !== undefined ? Math.round(gd.used_pct) + "%" : "—"
        }

        return activeData.five_hour_pct !== undefined ? Math.round(activeData.five_hour_pct) + "%" : "—"
    }

    property string iconSource: {
        if (panelTool === "codex")
            return Qt.resolvedUrl("../images/codex_icon.png")

        if (panelTool === "gemini")
            return Qt.resolvedUrl("../images/gemini_icon.png")

        return Qt.resolvedUrl("../images/claude-icon-22.png")
    }

    function usageColor(pct) {
        if (pct >= 90) return "#ef4444"
        if (pct >= 70) return "#f97316"
        if (pct >= 40) return "#eab308"
        return "#22c55e"
    }

    function ringColor() {
        if (!activeData || activeData.installed !== true)
            return Kirigami.Theme.disabledTextColor

        if (activeData.error)
            return "#ef4444"

        if (panelTool === "gemini") {
            if (gd.authenticated !== true)
                return "#ef4444"

            if (!hasUsableUsage(gd))
                return Kirigami.Theme.highlightColor

            return usageColor(activePct)
        }

        return usageColor(activePct)
    }

    MouseArea {
        anchors.fill: parent
        onClicked: root.expanded = !root.expanded
    }

    RowLayout {
        id: row
        anchors.centerIn: parent
        spacing: 5

        Image {
            source: compactRoot.iconSource
            Layout.preferredWidth: 16
            Layout.preferredHeight: 16
            fillMode: Image.PreserveAspectFit
            smooth: true
            visible: activeData && activeData.installed === true
        }

        Item {
            visible: activeData && activeData.installed === true && displayMode < 2
            Layout.preferredWidth: 34
            Layout.preferredHeight: 34

            Canvas {
                id: progressRing
                anchors.fill: parent

                property real pct: compactRoot.activePct
                property color color: Qt.color(compactRoot.ringColor())
                property bool noUsage: panelTool === "gemini"
                    && !compactRoot.hasUsableUsage(gd)
                    && gd.authenticated === true

                onPctChanged: requestPaint()
                onColorChanged: requestPaint()
                onNoUsageChanged: requestPaint()

                onPaint: {
                    var ctx = getContext("2d")
                    ctx.clearRect(0, 0, width, height)

                    var cx = width / 2
                    var cy = height / 2
                    var r = cx - 3
                    var lw = 3

                    ctx.beginPath()
                    ctx.arc(cx, cy, r, 0, 2 * Math.PI)
                    ctx.strokeStyle = Qt.rgba(1, 1, 1, 0.18)
                    ctx.lineWidth = lw
                    ctx.stroke()

                    if (noUsage) {
                        ctx.beginPath()
                        ctx.arc(cx, cy, r, 0, 2 * Math.PI)
                        ctx.strokeStyle = color
                        ctx.lineWidth = lw
                        ctx.stroke()
                        return
                    }

                    if (pct > 0) {
                        ctx.beginPath()
                        ctx.arc(
                            cx,
                            cy,
                            r,
                            -Math.PI / 2,
                            -Math.PI / 2 + 2 * Math.PI * (pct / 100)
                        )
                        ctx.strokeStyle = color
                        ctx.lineWidth = lw
                        ctx.stroke()
                    }
                }
            }

            Text {
                visible: displayMode === 0
                anchors.centerIn: parent
                text: compactRoot.activeText
                font.pixelSize: compactRoot.activeText.length > 3 ? 8 : 9
                font.bold: true
                color: Qt.color(compactRoot.ringColor())
                horizontalAlignment: Text.AlignHCenter
            }
        }

        PC3.Label {
            visible: activeData && activeData.installed === true && displayMode === 2
            text: compactRoot.activeText
            font.pixelSize: 12
            font.bold: true
            color: Qt.color(compactRoot.ringColor())
        }

        PC3.Label {
            visible: root.isLoading && (!activeData || activeData.installed !== true)
            text: "…"
            font.pixelSize: 11
            color: Kirigami.Theme.disabledTextColor
        }

        PC3.Label {
            visible: !root.isLoading && (!activeData || activeData.installed !== true)
            text: root.lastError !== "" ? "!" : "—"
            font.pixelSize: 12
            font.bold: true
            color: root.lastError !== "" ? "#ef4444" : Kirigami.Theme.disabledTextColor
        }
    }
}
