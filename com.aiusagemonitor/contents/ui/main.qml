import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasma5support as P5Support

PlasmoidItem {
    id: root

    // Parsed data from Python script
    property var claudeData: ({})
    property var codexData: ({})
    property var geminiData: ({})
    property var additionalData: ({})
    property int pendingRefreshes: 0
    readonly property bool isLoading: pendingRefreshes > 0
    property string lastError: ""
    property string lastUpdated: ""

    readonly property int claudeRefreshMs: (Plasmoid.configuration.claudeRefreshSecs || 600) * 1000
    readonly property int codexRefreshMs:  (Plasmoid.configuration.codexRefreshSecs  || 60)  * 1000
    readonly property int geminiRefreshMs: (Plasmoid.configuration.geminiRefreshSecs || 300) * 1000
    readonly property int additionalRefreshMs: (Plasmoid.configuration.additionalRefreshSecs || 300) * 1000

    // Visibility settings
    readonly property bool showClaude: Plasmoid.configuration.showClaude !== false
    readonly property bool showCodex: Plasmoid.configuration.showCodex !== false
    readonly property bool showGemini: Plasmoid.configuration.showGemini !== false
    readonly property bool showZai: Plasmoid.configuration.showZai !== false
    readonly property bool showKimi: Plasmoid.configuration.showKimi !== false
    readonly property bool showMinimax: Plasmoid.configuration.showMinimax !== false
    readonly property bool showQwen: Plasmoid.configuration.showQwen !== false
    readonly property bool showCursor: Plasmoid.configuration.showCursor !== false

    readonly property var additionalProviders: ["zai", "kimi", "minimax", "qwen", "cursor"]

    function showAdditionalProvider(provider) {
        if (provider === "zai") return showZai
        if (provider === "kimi") return showKimi
        if (provider === "minimax") return showMinimax
        if (provider === "qwen") return showQwen
        if (provider === "cursor") return showCursor
        return false
    }

    // Path to the Python script (resolved relative to this QML file)
    readonly property string scriptPath: {
        var url = Qt.resolvedUrl("../scripts/fetch_all_usage.py").toString()
        return url.replace(/^file:\/\//, "")
    }

    // In windowed/planar mode, show the full view by default.
    // In panel mode, keep compact view.
    preferredRepresentation: Plasmoid.formFactor === PlasmaCore.Types.Planar
        ? fullRepresentation
        : compactRepresentation
    compactRepresentation: CompactRepresentation { }
    fullRepresentation: FullRepresentation { }

    // Tell the panel exactly how much space this widget needs
    Layout.fillWidth: false
    Layout.minimumWidth: 66
    Layout.preferredWidth: 66
    Layout.maximumWidth: 66

    // Executable data source — runs the Python script
    P5Support.DataSource {
        id: runner
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName)
            root.pendingRefreshes = Math.max(0, root.pendingRefreshes - 1)
            var stdout = (data["stdout"] || "").trim()
            var stderr = (data["stderr"] || "").trim()
            if (stdout === "") {
                root.lastError = stderr || "No output from script"
                return
            }
            try {
                var result = JSON.parse(stdout)
                if (result.claude !== undefined) root.claudeData = result.claude || {}
                if (result.codex  !== undefined) root.codexData  = result.codex  || {}
                if (result.gemini !== undefined) root.geminiData = result.gemini || {}
                for (var provider of root.additionalProviders) {
                    if (result[provider] === undefined) continue
                    var updated = Object.assign({}, root.additionalData)
                    updated[provider] = result[provider] || {}
                    root.additionalData = updated
                }
                root.lastError = ""
                var now = new Date()
                root.lastUpdated = now.getHours().toString().padStart(2, "0") + ":" +
                                   now.getMinutes().toString().padStart(2, "0") + ":" +
                                   now.getSeconds().toString().padStart(2, "0")
            } catch (e) {
                root.lastError = "Parse error: " + e.message
            }
        }
    }

    function refreshProvider(provider) {
        if (scriptPath === "") return
        root.pendingRefreshes += 1
        runner.connectSource("python3 \"" + scriptPath + "\" " + provider)
    }

    function refresh() {
        refreshProvider("claude")
        refreshProvider("codex")
        refreshProvider("gemini")
        refreshAdditionalProviders()
    }

    function refreshAdditionalProviders() {
        for (var provider of additionalProviders)
            refreshProvider(provider)
    }

    // Per-provider timers
    Timer {
        id: claudeTimer
        interval: root.claudeRefreshMs
        running: true; repeat: true
        onTriggered: root.refreshProvider("claude")
    }
    Timer {
        id: codexTimer
        interval: root.codexRefreshMs
        running: true; repeat: true
        onTriggered: root.refreshProvider("codex")
    }
    Timer {
        id: geminiTimer
        interval: root.geminiRefreshMs
        running: true; repeat: true
        onTriggered: root.refreshProvider("gemini")
    }
    Timer {
        id: additionalTimer
        interval: root.additionalRefreshMs
        running: true; repeat: true
        onTriggered: root.refreshAdditionalProviders()
    }

    onClaudeRefreshMsChanged: { claudeTimer.interval = root.claudeRefreshMs; claudeTimer.restart() }
    onCodexRefreshMsChanged:  { codexTimer.interval  = root.codexRefreshMs;  codexTimer.restart()  }
    onGeminiRefreshMsChanged: { geminiTimer.interval = root.geminiRefreshMs; geminiTimer.restart() }
    onAdditionalRefreshMsChanged: { additionalTimer.interval = root.additionalRefreshMs; additionalTimer.restart() }

    // Initial load
    Component.onCompleted: root.refresh()
}
