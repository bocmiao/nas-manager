import Foundation

/// 主 App → 主屏小组件的数据快照。
/// App 与 Widget 是两个进程，只能通过 App Group 的 UserDefaults 共享。
/// 字段全部可选：拿不到就显示「—」，不用 0 冒充真实数值。
struct WidgetSnapshot: Codable {
    var cpuPercent: Double?
    var memPercent: Double?
    var memUsed: UInt64?
    var memTotal: UInt64?
    var cpuTemp: Int?
    var fanRpm: Int?
    var poolUsedPercent: Double?
    var runningContainers: Int?
    var totalContainers: Int?
    var alertCount: Int?
    var updatedAt: Date
    var machineName: String?
}

/// 快照读写入口。同一个源文件被加进 App 与 Widget 两个 target。
enum WidgetBridge {
    /// 需与自签时配置的 App Group 一致
    static let appGroupID = "group.dev.nasmanager.shared"

    private static let snapshotKey = "widget.snapshot.v1"

    /// 无签名 / 未配 App Group 时 `UserDefaults(suiteName:)` 可能返回 nil 或指向不可共享的域。
    /// 这里退回 standard：读写都不崩，只是 Widget 侧读不到，会走占位态。
    private static var sharedDefaults: UserDefaults {
        UserDefaults(suiteName: appGroupID) ?? .standard
    }

    static func write(_ snapshot: WidgetSnapshot) {
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        sharedDefaults.set(data, forKey: snapshotKey)
        // 兜底副本：无 App Group 时至少主 App 自己还有一份缓存
        UserDefaults.standard.set(data, forKey: snapshotKey)
    }

    static func read() -> WidgetSnapshot? {
        for defaults in [sharedDefaults, UserDefaults.standard] {
            guard let data = defaults.data(forKey: snapshotKey) else { continue }
            if let snapshot = try? JSONDecoder().decode(WidgetSnapshot.self, from: data) {
                return snapshot
            }
        }
        return nil
    }

    /// 断开连接时清掉，避免 Widget 继续展示已断开设备的数据
    static func clear() {
        sharedDefaults.removeObject(forKey: snapshotKey)
        UserDefaults.standard.removeObject(forKey: snapshotKey)
    }
}
