import AppIntents
import Foundation

// MARK: - 文案

/// Intent 里用到的文案拼装。全部只读，不做任何写操作。
enum NASIntentText {
    /// 快照太旧时提醒用户回 App 同步
    static func freshness(_ updatedAt: Date) -> String? {
        let age = Date().timeIntervalSince(updatedAt)
        guard age > 3600 else { return nil }
        let hours = Int(age / 3600)
        return "（数据为 \(hours) 小时前同步）"
    }

    static func status(_ snapshot: WidgetSnapshot) -> String {
        var parts: [String] = []

        if let cpu = snapshot.cpuPercent {
            parts.append("CPU \(Int(cpu.rounded()))%")
        } else {
            parts.append("CPU 未知")
        }

        if let mem = snapshot.memPercent {
            parts.append("内存 \(Int(mem.rounded()))%")
        } else {
            parts.append("内存未知")
        }

        if let running = snapshot.runningContainers, let total = snapshot.totalContainers {
            parts.append("容器 \(running)/\(total) 运行中")
        } else if let total = snapshot.totalContainers {
            parts.append("容器 \(total) 个")
        } else {
            parts.append("容器未知")
        }

        let name = snapshot.machineName ?? "NAS"
        let suffix = freshness(snapshot.updatedAt) ?? ""
        return "\(name) 当前 \(parts.joined(separator: "，"))\(suffix)。"
    }

    static func alerts(_ snapshot: WidgetSnapshot) -> String {
        let suffix = freshness(snapshot.updatedAt) ?? ""
        guard let count = snapshot.alertCount else {
            return "还没有取到告警数据，请先打开应用同步一次。"
        }
        if count == 0 {
            return "当前没有未读告警\(suffix)。"
        }
        return "有 \(count) 条未读告警\(suffix)，打开应用可以查看详情。"
    }

    static func storage(_ snapshot: WidgetSnapshot) -> String {
        let suffix = freshness(snapshot.updatedAt) ?? ""
        guard let percent = snapshot.poolUsedPercent else {
            return "还没有取到存储占用，请先打开应用同步一次。"
        }
        return "存储池已用 \(Int(percent.rounded()))%\(suffix)。"
    }
}

// MARK: - 查询 NAS 状态

/// Intent 在独立进程执行，拿不到 AppModel 的内存状态，
/// 只能读 WidgetBridge 写在共享容器里的最近一次快照。
struct NASStatusIntent: AppIntent {
    static var title: LocalizedStringResource = "查询 NAS 状态"
    static var description: IntentDescription? = IntentDescription("播报最近一次同步的 CPU、内存与容器概况")

    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        guard let snapshot = WidgetBridge.read() else {
            let text = "还没有 NAS 数据，请先打开应用同步一次。"
            return .result(value: text, dialog: IntentDialog(stringLiteral: text))
        }
        let text = NASIntentText.status(snapshot)
        return .result(value: text, dialog: IntentDialog(stringLiteral: text))
    }
}

// MARK: - 查看告警

struct NASAlertIntent: AppIntent {
    static var title: LocalizedStringResource = "查看 NAS 告警"
    static var description: IntentDescription? = IntentDescription("播报未读告警数量")

    // 返回值统一用 String，避免 returnsValue 的泛型推断出岔子
    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        guard let snapshot = WidgetBridge.read() else {
            let text = "还没有 NAS 数据，请先打开应用同步一次。"
            return .result(value: text, dialog: IntentDialog(stringLiteral: text))
        }
        let text = NASIntentText.alerts(snapshot)
        return .result(value: text, dialog: IntentDialog(stringLiteral: text))
    }
}

// MARK: - 查看存储占用

struct NASStorageIntent: AppIntent {
    static var title: LocalizedStringResource = "查询 NAS 存储"
    static var description: IntentDescription? = IntentDescription("播报存储池占用比例")

    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        guard let snapshot = WidgetBridge.read() else {
            let text = "还没有 NAS 数据，请先打开应用同步一次。"
            return .result(value: text, dialog: IntentDialog(stringLiteral: text))
        }
        let text = NASIntentText.storage(snapshot)
        return .result(value: text, dialog: IntentDialog(stringLiteral: text))
    }
}

// MARK: - Siri 快捷指令

/// 注册到系统的语音短语。每条短语都必须带 `.applicationName`，否则不会被收录。
struct NASShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: NASStatusIntent(),
            phrases: [
                "查询 \(.applicationName) 状态",
                "\(.applicationName) 现在怎么样"
            ],
            shortTitle: "NAS 状态",
            systemImageName: "gauge"
        )

        AppShortcut(
            intent: NASAlertIntent(),
            phrases: [
                "查看 \(.applicationName) 告警",
                "\(.applicationName) 有未读告警吗"
            ],
            shortTitle: "NAS 告警",
            systemImageName: "bell"
        )

        AppShortcut(
            intent: NASStorageIntent(),
            phrases: [
                "查询 \(.applicationName) 存储",
                "\(.applicationName) 空间还剩多少"
            ],
            shortTitle: "NAS 存储",
            systemImageName: "internaldrive"
        )
    }
}
