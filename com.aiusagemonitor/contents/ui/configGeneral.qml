import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

QQC2.ScrollView {
    id: configPage

    property int cfg_claudeRefreshSecs: 600
    property int cfg_codexRefreshSecs: 60
    property int cfg_geminiRefreshSecs: 300
    property int cfg_additionalRefreshSecs: 300
    property string cfg_panelTool: "claude"
    property int cfg_panelDisplayMode: 0

    // Visibility settings
    property bool cfg_showClaude: true
    property bool cfg_showCodex: true
    property bool cfg_showGemini: true
    property bool cfg_showZai: true
    property bool cfg_showKimi: true
    property bool cfg_showMinimax: true
    property bool cfg_showQwen: true
    property bool cfg_showCursor: true

    clip: true
    contentWidth: availableWidth
    QQC2.ScrollBar.horizontal.policy: QQC2.ScrollBar.AlwaysOff

    Kirigami.FormLayout {
        width: configPage.availableWidth

        // ── Refresh intervals ──────────────────────────────────────────────
        Kirigami.Separator {
            Kirigami.FormData.label: "Refresh intervals"
            Kirigami.FormData.isSection: true
        }

        QQC2.ComboBox {
            Kirigami.FormData.label: "Claude Code:"
            model: [
                { text: "1 minute",   value: 60   },
                { text: "5 minutes",  value: 300  },
                { text: "10 minutes", value: 600  },
                { text: "30 minutes", value: 1800 },
            ]
            textRole: "text"
            currentIndex: { var v = configPage.cfg_claudeRefreshSecs; for (var i = 0; i < model.length; i++) { if (model[i].value === v) return i } return 2 }
            onActivated: configPage.cfg_claudeRefreshSecs = model[currentIndex].value
        }

        QQC2.ComboBox {
            Kirigami.FormData.label: "OpenAI Codex:"
            model: [
                { text: "10 seconds", value: 10  },
                { text: "30 seconds", value: 30  },
                { text: "1 minute",   value: 60  },
                { text: "5 minutes",  value: 300 },
            ]
            textRole: "text"
            currentIndex: { var v = configPage.cfg_codexRefreshSecs; for (var i = 0; i < model.length; i++) { if (model[i].value === v) return i } return 2 }
            onActivated: configPage.cfg_codexRefreshSecs = model[currentIndex].value
        }

        QQC2.ComboBox {
            Kirigami.FormData.label: "Gemini CLI:"
            model: [
                { text: "1 minute",   value: 60   },
                { text: "5 minutes",  value: 300  },
                { text: "10 minutes", value: 600  },
                { text: "30 minutes", value: 1800 },
            ]
            textRole: "text"
            currentIndex: { var v = configPage.cfg_geminiRefreshSecs; for (var i = 0; i < model.length; i++) { if (model[i].value === v) return i } return 1 }
            onActivated: configPage.cfg_geminiRefreshSecs = model[currentIndex].value
        }

        QQC2.ComboBox {
            Kirigami.FormData.label: "Other providers:"
            model: [
                { text: "1 minute",   value: 60   },
                { text: "5 minutes",  value: 300  },
                { text: "10 minutes", value: 600  },
                { text: "30 minutes", value: 1800 },
            ]
            textRole: "text"
            currentIndex: { var v = configPage.cfg_additionalRefreshSecs; for (var i = 0; i < model.length; i++) { if (model[i].value === v) return i } return 1 }
            onActivated: configPage.cfg_additionalRefreshSecs = model[currentIndex].value
        }

        QQC2.Label {
            Kirigami.FormData.label: "Gemini auth:"
            text: "OAuth shows quota usage. API key and Vertex AI can show project usage with Cloud Monitoring and configured limits; Vertex AI can also show quota metrics."
            wrapMode: Text.Wrap
            Layout.fillWidth: true
            Layout.preferredWidth: Kirigami.Units.gridUnit * 24
            Layout.maximumWidth: Kirigami.Units.gridUnit * 30
            font.pixelSize: 10
            color: Kirigami.Theme.disabledTextColor
        }

        // ── Tool shown in panel ────────────────────────────────────────────
        Kirigami.Separator {
            Kirigami.FormData.label: "Panel tool"
            Kirigami.FormData.isSection: true
        }

        ColumnLayout {
            Kirigami.FormData.label: "Show:"
            spacing: 4

            QQC2.RadioButton {
                text: "Claude Code"
                checked: configPage.cfg_panelTool === "claude"
                onToggled: if (checked) configPage.cfg_panelTool = "claude"
            }
            QQC2.RadioButton {
                text: "OpenAI Codex"
                checked: configPage.cfg_panelTool === "codex"
                onToggled: if (checked) configPage.cfg_panelTool = "codex"
            }
            QQC2.RadioButton {
                text: "Gemini CLI"
                checked: configPage.cfg_panelTool === "gemini"
                onToggled: if (checked) configPage.cfg_panelTool = "gemini"
            }
            QQC2.RadioButton {
                text: "Z.AI"
                checked: configPage.cfg_panelTool === "zai"
                onToggled: if (checked) configPage.cfg_panelTool = "zai"
            }
            QQC2.RadioButton {
                text: "Kimi Code"
                checked: configPage.cfg_panelTool === "kimi"
                onToggled: if (checked) configPage.cfg_panelTool = "kimi"
            }
            QQC2.RadioButton {
                text: "MiniMax"
                checked: configPage.cfg_panelTool === "minimax"
                onToggled: if (checked) configPage.cfg_panelTool = "minimax"
            }
            QQC2.RadioButton {
                text: "QwenCloud"
                checked: configPage.cfg_panelTool === "qwen"
                onToggled: if (checked) configPage.cfg_panelTool = "qwen"
            }
            QQC2.RadioButton {
                text: "Cursor"
                checked: configPage.cfg_panelTool === "cursor"
                onToggled: if (checked) configPage.cfg_panelTool = "cursor"
            }
        }

        // ── Display style ──────────────────────────────────────────────────
        Kirigami.Separator {
            Kirigami.FormData.label: "Display style"
            Kirigami.FormData.isSection: true
        }

        ColumnLayout {
            Kirigami.FormData.label: "Style:"
            spacing: 4

            QQC2.RadioButton {
                text: "Ring and percentage"
                checked: configPage.cfg_panelDisplayMode === 0
                onToggled: if (checked) configPage.cfg_panelDisplayMode = 0
            }
            QQC2.RadioButton {
                text: "Ring only"
                checked: configPage.cfg_panelDisplayMode === 1
                onToggled: if (checked) configPage.cfg_panelDisplayMode = 1
            }
            QQC2.RadioButton {
                text: "Percentage only"
                checked: configPage.cfg_panelDisplayMode === 2
                onToggled: if (checked) configPage.cfg_panelDisplayMode = 2
            }
        }

        // ── Visible tools ──────────────────────────────────────────────────
        Kirigami.Separator {
            Kirigami.FormData.label: "Visible tools"
            Kirigami.FormData.isSection: true
        }

        ColumnLayout {
            Kirigami.FormData.label: "Show in popup:"
            spacing: 4

            QQC2.CheckBox {
                text: "Claude Code"
                checked: configPage.cfg_showClaude
                onToggled: configPage.cfg_showClaude = checked
            }
            QQC2.CheckBox {
                text: "OpenAI Codex"
                checked: configPage.cfg_showCodex
                onToggled: configPage.cfg_showCodex = checked
            }
            QQC2.CheckBox {
                text: "Gemini CLI"
                checked: configPage.cfg_showGemini
                onToggled: configPage.cfg_showGemini = checked
            }
            QQC2.CheckBox {
                text: "Z.AI"
                checked: configPage.cfg_showZai
                onToggled: configPage.cfg_showZai = checked
            }
            QQC2.CheckBox {
                text: "Kimi Code"
                checked: configPage.cfg_showKimi
                onToggled: configPage.cfg_showKimi = checked
            }
            QQC2.CheckBox {
                text: "MiniMax"
                checked: configPage.cfg_showMinimax
                onToggled: configPage.cfg_showMinimax = checked
            }
            QQC2.CheckBox {
                text: "QwenCloud"
                checked: configPage.cfg_showQwen
                onToggled: configPage.cfg_showQwen = checked
            }
            QQC2.CheckBox {
                text: "Cursor"
                checked: configPage.cfg_showCursor
                onToggled: configPage.cfg_showCursor = checked
            }
        }
    }
}
