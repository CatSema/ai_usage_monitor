pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

Item {
    id: fullRoot
    implicitWidth: 390
    implicitHeight: Math.min(contentCol.implicitHeight + 24, Kirigami.Units.gridUnit * 36)

    property var cd: root.claudeData
    property var od: root.codexData
    property var gd: root.geminiData
    property bool geminiExpanded: false
    property bool vertexQuotasExpanded: false
    property bool geminiAccountsExpanded: false

    onGdChanged: {
        if (!gd || !gd.buckets || gd.buckets.length <= 1)
            geminiExpanded = false
        if (!gd || !gd.quota_metrics || gd.quota_metrics.length <= 0)
            vertexQuotasExpanded = false
        if (!gd || !gd.accounts || gd.accounts.length <= 1)
            geminiAccountsExpanded = false
    }

    function usageColor(pct) {
        if (pct >= 90) return "#ef4444"
        if (pct >= 70) return "#f97316"
        if (pct >= 40) return "#eab308"
        return "#22c55e"
    }

    function formatReset(isoStr) {
        if (!isoStr) return ""
        var now = new Date()
        var reset = new Date(isoStr)
        var diff = reset - now
        if (isNaN(reset.getTime())) return ""
        if (diff <= 0) return "soon"
        var hrs = Math.floor(diff / 3600000)
        var mins = Math.floor((diff % 3600000) / 60000)
        if (hrs >= 24) {
            var days = Math.floor(hrs / 24)
            return "in " + days + "d " + (hrs % 24) + "h"
        }
        if (hrs > 0) return "in " + hrs + "h " + mins + "m"
        return "in " + mins + "m"
    }

    function prettyGeminiModel(id) {
        var m = (id || "").toLowerCase().replace(/^models\//, "")
        if (!m) return ""
        m = m.replace(/^gemini-/, "")

        var suffix = ""
        if (m.indexOf("preview") !== -1 || m.indexOf("exp") !== -1)
            suffix = " (Preview)"

        if (m.indexOf("2.5-flash-lite") === 0) return "Gemini 2.5 Flash Lite" + suffix
        if (m.indexOf("2.5-flash") === 0) return "Gemini 2.5 Flash" + suffix
        if (m.indexOf("2.5-pro") === 0) return "Gemini 2.5 Pro" + suffix
        if (m.indexOf("2.0-flash") === 0) return "Gemini 2.0 Flash" + suffix
        if (m.indexOf("2.0-pro") === 0) return "Gemini 2.0 Pro" + suffix

        return ("Gemini " + m.replace(/-/g, " ")) + suffix
    }

    function prettyCodexModel(id) {
        var m = (id || "").toLowerCase()
        if (!m) return ""
        if (m.indexOf("codex") !== -1) {
            var major = m.match(/gpt-(\d+)(?:\.\d+)?/)
            if (major && major.length > 1)
                return "GPT-" + major[1] + " Codex"
            return "Codex"
        }
        return id
    }

    function geminiAuthLabel() {
        var mode = (gd.auth_type || "oauth-personal").toLowerCase()
        if (mode === "api-key") return "API key"
        if (mode === "vertex-ai") return "Vertex AI"
        if (mode === "oauth-personal") return "OAuth"
        return mode
    }

    function accountLabel(acc, index) {
        if (!acc) return "Account " + (index + 1)
        if (acc.account_label) return acc.account_label
        if (acc.email) return acc.email
        return "Account " + (index + 1)
    }

    function accountSummary(acc) {
        if (!acc) return ""
        if (acc.error) return acc.error
        if (acc.has_usage === false || acc.usage_supported === false)
            return acc.usage_note || "Usage unavailable"
        var model = acc.model ? prettyGeminiModel(acc.model) : "Gemini quota"
        var pct = acc.used_pct !== undefined ? Math.round(acc.used_pct) + "%" : "—"
        var reset = formatReset(acc.reset_time)
        return model + ": " + pct + (reset ? " · " + reset : "")
    }

    function codexSourceLabel() {
        var source = od.credential_source || od.source || ""
        if (source === "codex_oauth") return "OAuth"
        if (source === "codex_api_key") return "API key"
        if (source === "opencode_oauth") return "OpenCode OAuth"
        if (source === "api_usage") return "API usage"
        if (source === "local_jsonl_fallback") return "local fallback"
        return source
    }

    function quotaLimitSummary(metric) {
        if (!metric || !metric.limits || metric.limits.length === 0)
            return ""
        var limit = metric.limits[0]
        var value = limit.effective_limit !== undefined && limit.effective_limit !== null ? limit.effective_limit : "—"
        var name = limit.display_name || metric.display_name || metric.metric || "Quota"
        return name + ": " + value
    }

    function providerDisplayName(provider) {
        if (provider === "zai") return "Z.AI"
        if (provider === "kimi") return "KIMI CODE"
        if (provider === "minimax") return "MINIMAX"
        if (provider === "qwen") return "QWENCLOUD"
        if (provider === "cursor") return "CURSOR"
        return provider.toUpperCase()
    }

    Flickable {
        id: viewport
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentCol.implicitHeight + 24
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        QQC2.ScrollBar.vertical: QQC2.ScrollBar {}

        ColumnLayout {
            id: contentCol
            x: 12
            y: 12
            width: Math.max(0, viewport.width - 24)
            spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.bottomMargin: 10

            PC3.Label {
                text: "AI Usage Monitor"
                font.bold: true
                font.pixelSize: 14
                Layout.fillWidth: true
            }

            PC3.ToolButton {
                icon.name: "view-refresh"
                enabled: !root.isLoading
                onClicked: root.refresh()
                PC3.ToolTip.text: root.lastUpdated ? "Updated " + root.lastUpdated : "Click to refresh"
                PC3.ToolTip.visible: hovered
                PC3.ToolTip.delay: 500
            }
        }

        Loader {
            Layout.fillWidth: true
            Layout.preferredHeight: active ? implicitHeight : 0
            visible: active
            active: cd.installed === true && root.showClaude

            sourceComponent: ColumnLayout {
                spacing: 6

                RowLayout {
                    Image {
                        source: Qt.resolvedUrl("../images/claude-icon-22.png")
                        Layout.preferredWidth: 16
                        Layout.preferredHeight: 16
                        fillMode: Image.PreserveAspectFit
                        smooth: true
                    }
                    PC3.Label { text: "CLAUDE CODE"; font.bold: true; font.pixelSize: 12 }
                    Item { Layout.fillWidth: true }
                }

                PC3.Label {
                    visible: !!cd.error
                    text: cd.error || ""
                    color: Kirigami.Theme.negativeTextColor
                    font.pixelSize: 10
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }

                Loader {
                    Layout.fillWidth: true
                    active: cd.five_hour_pct !== undefined && !cd.error

                    sourceComponent: UsageBar {
                        label: "5h"
                        pct: Math.min(cd.five_hour_pct || 0, 100)
                        pctText: Math.round(cd.five_hour_pct || 0) + "%"
                        resetText: fullRoot.formatReset(cd.five_hour_reset)
                        barColor: fullRoot.usageColor(cd.five_hour_pct || 0)
                    }
                }

                Loader {
                    Layout.fillWidth: true
                    active: cd.seven_day_pct !== null && cd.seven_day_pct !== undefined && !cd.error

                    sourceComponent: UsageBar {
                        label: "7d"
                        pct: Math.min(cd.seven_day_pct || 0, 100)
                        pctText: Math.round(cd.seven_day_pct || 0) + "%"
                        resetText: fullRoot.formatReset(cd.seven_day_reset)
                        barColor: fullRoot.usageColor(cd.seven_day_pct || 0)
                    }
                }

                PC3.Label {
                    visible: !cd.error && (cd.seven_day_pct === null || cd.seven_day_pct === undefined)
                    text: "7-day limit: not tracked on this plan"
                    font.pixelSize: 10
                    color: Kirigami.Theme.disabledTextColor
                }

                Kirigami.Separator { Layout.fillWidth: true; Layout.topMargin: 4; Layout.bottomMargin: 4 }
            }
        }

        Repeater {
            model: {
                var rows = []
                for (var provider of root.additionalProviders) {
                    var data = root.additionalData[provider] || {}
                    if (root.showAdditionalProvider(provider) && data.installed === true)
                        rows.push({provider: provider, data: data})
                }
                return rows
            }

            delegate: ColumnLayout {
                id: providerCard

                required property var modelData
                readonly property string provider: modelData.provider
                readonly property var providerData: modelData.data

                Layout.fillWidth: true
                spacing: 6

                RowLayout {
                    Layout.fillWidth: true

                    PC3.Label {
                        text: fullRoot.providerDisplayName(providerCard.provider)
                        font.bold: true
                        font.pixelSize: 12
                    }
                    Item { Layout.fillWidth: true }
                    PC3.Label {
                        visible: !!providerCard.providerData.account_label
                        text: providerCard.providerData.account_label || ""
                        font.pixelSize: 10
                        color: Kirigami.Theme.disabledTextColor
                        elide: Text.ElideRight
                        Layout.maximumWidth: 220
                    }
                }

                PC3.Label {
                    visible: !!providerCard.providerData.error
                    text: providerCard.providerData.error || ""
                    font.pixelSize: 10
                    color: Kirigami.Theme.negativeTextColor
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }

                Loader {
                    Layout.fillWidth: true
                    active: providerCard.providerData.five_hour_pct !== undefined && !providerCard.providerData.error
                    sourceComponent: UsageBar {
                        label: providerCard.providerData.primary_label || "Usage"
                        pct: Math.min(providerCard.providerData.five_hour_pct || 0, 100)
                        pctText: Math.round(providerCard.providerData.five_hour_pct || 0) + "%"
                        resetText: fullRoot.formatReset(providerCard.providerData.five_hour_reset)
                        barColor: fullRoot.usageColor(providerCard.providerData.five_hour_pct || 0)
                    }
                }

                Loader {
                    Layout.fillWidth: true
                    active: providerCard.providerData.seven_day_pct !== undefined && !providerCard.providerData.error
                    sourceComponent: UsageBar {
                        label: providerCard.providerData.secondary_label || "Secondary"
                        pct: Math.min(providerCard.providerData.seven_day_pct || 0, 100)
                        pctText: Math.round(providerCard.providerData.seven_day_pct || 0) + "%"
                        resetText: fullRoot.formatReset(providerCard.providerData.seven_day_reset)
                        barColor: fullRoot.usageColor(providerCard.providerData.seven_day_pct || 0)
                    }
                }

                InfoBox {
                    visible: !providerCard.providerData.error
                        && providerCard.providerData.authenticated === true
                        && providerCard.providerData.has_usage === false
                    title: fullRoot.providerDisplayName(providerCard.provider) + " authenticated"
                    text: providerCard.providerData.usage_note || "This provider does not expose quota percentages."
                    tone: "info"
                }

                Kirigami.Separator {
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    Layout.bottomMargin: 4
                }
            }
        }

        Loader {
            Layout.fillWidth: true
            Layout.preferredHeight: active ? implicitHeight : 0
            visible: active
            active: od.installed === true && root.showCodex === true

            sourceComponent: ColumnLayout {
                spacing: 6

                RowLayout {
                    Item {
                        Layout.preferredWidth: 16
                        Layout.preferredHeight: 16
                        Image {
                            id: codexFullImg
                            source: Qt.resolvedUrl("../images/codex_icon.png")
                            width: 16; height: 16; fillMode: Image.PreserveAspectFit; smooth: true
                            visible: status === Image.Ready
                        }
                        Rectangle {
                            visible: codexFullImg.status !== Image.Ready
                            width: 14; height: 14; radius: 3; anchors.centerIn: parent
                            color: "#10A37F"
                        }
                    }
                    PC3.Label { text: "OPENAI CODEX"; font.bold: true; font.pixelSize: 12 }
                    PC3.Label {
                        visible: !!od.model
                        text: od.model ? "· " + fullRoot.prettyCodexModel(od.model) : ""
                        font.pixelSize: 10
                        color: Kirigami.Theme.disabledTextColor
                    }
                    Item { Layout.fillWidth: true }
                    PC3.Label {
                        visible: !!od.plan_type
                        text: od.plan_type || ""
                        font.pixelSize: 10
                        color: Kirigami.Theme.disabledTextColor
                    }
                    PC3.Label {
                        visible: !!fullRoot.codexSourceLabel()
                        text: fullRoot.codexSourceLabel()
                        font.pixelSize: 10
                        color: Kirigami.Theme.disabledTextColor
                    }
                }

                PC3.Label {
                    visible: !!od.warning
                    text: od.warning || ""
                    font.pixelSize: 10
                    color: Kirigami.Theme.neutralTextColor
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }

                PC3.Label {
                    visible: !!od.error
                    text: od.error || ""
                    font.pixelSize: 10
                    color: Kirigami.Theme.negativeTextColor
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }

                Loader {
                    Layout.fillWidth: true
                    active: od.five_hour_pct !== undefined && od.has_data !== false && !od.error

                    sourceComponent: UsageBar {
                        label: "5h"
                        pct: Math.min(od.five_hour_pct || 0, 100)
                        pctText: Math.round(od.five_hour_pct || 0) + "%"
                        resetText: fullRoot.formatReset(od.five_hour_reset)
                        barColor: fullRoot.usageColor(od.five_hour_pct || 0)
                    }
                }

                Loader {
                    Layout.fillWidth: true
                    active: od.seven_day_pct !== undefined && od.has_data !== false && !od.error

                    sourceComponent: UsageBar {
                        label: "7d"
                        pct: Math.min(od.seven_day_pct || 0, 100)
                        pctText: Math.round(od.seven_day_pct || 0) + "%"
                        resetText: fullRoot.formatReset(od.seven_day_reset)
                        barColor: fullRoot.usageColor(od.seven_day_pct || 0)
                    }
                }

                PC3.Label {
                    visible: od.has_data === false && !od.error
                    text: "No Codex usage data yet"
                    font.pixelSize: 10
                    color: Kirigami.Theme.disabledTextColor
                }

                Kirigami.Separator { Layout.fillWidth: true; Layout.topMargin: 4; Layout.bottomMargin: 4 }
            }
        }

        Loader {
            Layout.fillWidth: true
            Layout.preferredHeight: active ? implicitHeight : 0
            visible: active
            active: gd.installed === true && root.showGemini === true

            sourceComponent: ColumnLayout {
                id: geminiCard
                spacing: 6
                readonly property bool canExpand: !!(gd.buckets && gd.buckets.length > 1 && !gd.error && gd.has_usage !== false && gd.usage_supported !== false)
                readonly property bool canShowVertexQuotas: !!(gd.quota_metrics && gd.quota_metrics.length > 0 && !gd.error)
                readonly property bool canShowAccounts: !!(gd.accounts && gd.accounts.length > 1)

                RowLayout {
                    Image {
                        source: Qt.resolvedUrl("../images/gemini_icon.png")
                        Layout.preferredWidth: 16
                        Layout.preferredHeight: 16
                        fillMode: Image.PreserveAspectFit
                        smooth: true
                    }
                    PC3.Label { text: "GEMINI CLI"; font.bold: true; font.pixelSize: 12 }
                    PC3.Label {
                        visible: !!gd.auth_type
                        text: "· " + fullRoot.geminiAuthLabel()
                        font.pixelSize: 10
                        color: Kirigami.Theme.disabledTextColor
                    }
                    Item { Layout.fillWidth: true }
                    PC3.Label {
                        visible: !!gd.tier
                        text: gd.tier || ""
                        font.pixelSize: 10
                        color: Kirigami.Theme.disabledTextColor
                    }
                    QQC2.Button {
                        visible: geminiCard.canShowAccounts
                        text: fullRoot.geminiAccountsExpanded ? "Hide accounts" : ("Accounts (" + (gd.accounts ? gd.accounts.length : 0) + ")")
                        onClicked: fullRoot.geminiAccountsExpanded = !fullRoot.geminiAccountsExpanded
                    }
                    QQC2.Button {
                        visible: geminiCard.canExpand
                        text: fullRoot.geminiExpanded ? "Hide models" : ("Models (" + (gd.buckets ? gd.buckets.length : 0) + ")")
                        onClicked: fullRoot.geminiExpanded = !fullRoot.geminiExpanded
                    }
                }

                PC3.Label {
                    visible: !!gd.project_id || !!gd.location
                    text: {
                        var parts = []
                        if (gd.project_id) parts.push("Project: " + gd.project_id)
                        if (gd.location) parts.push("Location: " + gd.location)
                        return parts.join(" · ")
                    }
                    font.pixelSize: 10
                    color: Kirigami.Theme.disabledTextColor
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }

                Loader {
                    Layout.fillWidth: true
                    active: gd.used_pct !== undefined && !gd.error && !fullRoot.geminiExpanded && !fullRoot.geminiAccountsExpanded && gd.has_usage !== false && gd.usage_supported !== false

                    sourceComponent: UsageBar {
                        label: gd.model ? fullRoot.prettyGeminiModel(gd.model) : "Gemini quota"
                        pct: Math.min(gd.used_pct || 0, 100)
                        pctText: Math.round(gd.used_pct || 0) + "%"
                        resetText: fullRoot.formatReset(gd.reset_time)
                        barColor: fullRoot.usageColor(gd.used_pct || 0)
                    }
                }

                Repeater {
                    model: (fullRoot.geminiAccountsExpanded && gd.accounts) ? gd.accounts : []
                    delegate: ColumnLayout {
                        readonly property var acc: modelData
                        readonly property int idx: index
                        spacing: 3
                        Layout.fillWidth: true

                        RowLayout {
                            Layout.fillWidth: true
                            PC3.Label {
                                text: fullRoot.accountLabel(acc, idx)
                                font.pixelSize: 10
                                font.bold: true
                                color: acc.authenticated === true ? Kirigami.Theme.textColor : Kirigami.Theme.negativeTextColor
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                            PC3.Label {
                                visible: !!acc.tier
                                text: acc.tier || ""
                                font.pixelSize: 10
                                color: Kirigami.Theme.disabledTextColor
                            }
                        }

                        Loader {
                            Layout.fillWidth: true
                            active: acc.used_pct !== undefined && !acc.error && acc.has_usage !== false && acc.usage_supported !== false
                            sourceComponent: UsageBar {
                                label: acc.model ? fullRoot.prettyGeminiModel(acc.model) : "Gemini quota"
                                pct: Math.min(acc.used_pct || 0, 100)
                                pctText: Math.round(acc.used_pct || 0) + "%"
                                resetText: fullRoot.formatReset(acc.reset_time)
                                barColor: fullRoot.usageColor(acc.used_pct || 0)
                            }
                        }

                        PC3.Label {
                            visible: acc.used_pct === undefined || !!acc.error || acc.has_usage === false || acc.usage_supported === false
                            text: fullRoot.accountSummary(acc)
                            font.pixelSize: 10
                            color: acc.error ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.disabledTextColor
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                        }
                    }
                }

                Repeater {
                    model: (fullRoot.geminiExpanded && gd.buckets) ? gd.buckets : []
                    delegate: UsageBar {
                        readonly property var bkt: modelData
                        label: fullRoot.prettyGeminiModel(bkt.model || "")
                        pct: Math.min(bkt.used_pct || 0, 100)
                        pctText: Math.round(bkt.used_pct || 0) + "%"
                        resetText: fullRoot.formatReset(bkt.reset_time)
                        barColor: fullRoot.usageColor(bkt.used_pct || 0)
                    }
                }

                InfoBox {
                    visible: !gd.error && gd.authenticated === true
                        && (gd.has_usage === false || gd.usage_supported === false || gd.used_pct === undefined)
                    title: fullRoot.geminiAuthLabel() + " authenticated"
                    text: gd.usage_note || "Usage percentage is not available for this Gemini auth mode."
                    tone: "info"
                }

                InfoBox {
                    visible: !gd.error && gd.auth_type === "api-key" && gd.available_models_count !== undefined
                    title: "Available models"
                    text: "Models visible with this key: " + gd.available_models_count
                    tone: "muted"
                }

                RowLayout {
                    visible: geminiCard.canShowVertexQuotas
                    Layout.fillWidth: true
                    PC3.Label {
                        text: "Vertex quota metrics: " + (gd.quota_metrics ? gd.quota_metrics.length : 0)
                        font.pixelSize: 10
                        color: Kirigami.Theme.disabledTextColor
                        Layout.fillWidth: true
                    }
                    QQC2.Button {
                        text: fullRoot.vertexQuotasExpanded ? "Hide quotas" : "Show quotas"
                        onClicked: fullRoot.vertexQuotasExpanded = !fullRoot.vertexQuotasExpanded
                    }
                }

                Repeater {
                    model: (fullRoot.vertexQuotasExpanded && gd.quota_metrics) ? gd.quota_metrics.slice(0, 8) : []
                    delegate: PC3.Label {
                        readonly property var metric: modelData
                        text: "• " + fullRoot.quotaLimitSummary(metric)
                        font.pixelSize: 10
                        color: Kirigami.Theme.disabledTextColor
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }

                PC3.Label {
                    visible: !!gd.error
                    text: gd.error || ""
                    font.pixelSize: 10
                    color: Kirigami.Theme.negativeTextColor
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }

                Kirigami.Separator { Layout.fillWidth: true; Layout.topMargin: 4; Layout.bottomMargin: 4 }
            }
        }

        Loader {
            Layout.fillWidth: true
            Layout.preferredHeight: active ? implicitHeight : 0
            visible: active
            readonly property bool claudeVisible: cd.installed === true && root.showClaude === true
            readonly property bool codexVisible: od.installed === true && root.showCodex === true
            readonly property bool geminiVisible: gd.installed === true && root.showGemini === true
            readonly property bool additionalVisible: {
                for (var provider of root.additionalProviders) {
                    if (root.showAdditionalProvider(provider)
                        && (root.additionalData[provider] || {}).installed === true)
                        return true
                }
                return false
            }
            active: !claudeVisible && !codexVisible && !geminiVisible && !additionalVisible

            sourceComponent: PC3.Label {
                text: {
                    if (root.isLoading) return "Loading…"
                    var allHidden = (cd.installed === true || od.installed === true || gd.installed === true)
                    for (var provider of root.additionalProviders)
                        allHidden = allHidden || (root.additionalData[provider] || {}).installed === true
                    return allHidden ? "All tools hidden in settings" : "No AI tools detected"
                }
                color: Kirigami.Theme.disabledTextColor
                horizontalAlignment: Text.AlignHCenter
                Layout.fillWidth: true
            }
        }

            Item { Layout.preferredHeight: 4 }
        }
    }

    component InfoBox: Rectangle {
        id: infoBox

        property string title: ""
        property string text: ""
        property string tone: "muted"

        Layout.fillWidth: true
        implicitHeight: boxCol.implicitHeight + 12
        radius: 6
        color: tone === "info"
            ? Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g, Kirigami.Theme.highlightColor.b, 0.12)
            : Qt.rgba(1, 1, 1, 0.06)
        border.width: 1
        border.color: tone === "info"
            ? Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g, Kirigami.Theme.highlightColor.b, 0.35)
            : Qt.rgba(1, 1, 1, 0.10)

        ColumnLayout {
            id: boxCol
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 6 }
            spacing: 2

            PC3.Label {
                visible: infoBox.title !== ""
                text: infoBox.title
                font.bold: true
                font.pixelSize: 10
                color: infoBox.tone === "info" ? Kirigami.Theme.highlightColor : Kirigami.Theme.textColor
                Layout.fillWidth: true
            }
            PC3.Label {
                text: infoBox.text
                font.pixelSize: 10
                color: Kirigami.Theme.disabledTextColor
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
        }
    }

    component UsageBar: RowLayout {
        id: usageBar

        property string label: ""
        property real pct: 0
        property string pctText: "0%"
        property string resetText: ""
        property color barColor: Kirigami.Theme.positiveTextColor

        spacing: 6
        Layout.fillWidth: true

        PC3.Label {
            text: usageBar.label
            font.pixelSize: 10
            color: Kirigami.Theme.disabledTextColor
            Layout.minimumWidth: 90
            Layout.preferredWidth: 110
            elide: Text.ElideRight
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 8
            radius: 4
            color: Kirigami.Theme.backgroundColor

            Rectangle {
                width: parent.width * (usageBar.pct / 100)
                height: parent.height
                radius: parent.radius
                color: usageBar.barColor

                Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
            }
        }

        PC3.Label {
            text: usageBar.pctText
            font.pixelSize: 11
            font.bold: true
            color: usageBar.barColor
            Layout.minimumWidth: 36
            horizontalAlignment: Text.AlignRight
        }

        PC3.Label {
            text: usageBar.resetText
            font.pixelSize: 10
            color: Kirigami.Theme.disabledTextColor
            Layout.minimumWidth: 70
        }
    }
}
