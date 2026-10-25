import Foundation
import Observation

// MARK: - 级别

enum AlertSeverity: String, Codable, Hashable {
    case info
    case warning
    case critical

    var label: String {
        switch self {
        case .info: return "提示"
        case .warning: return "警告"
        case .critical: return "严重"
        }
    }

    var symbolName: String {
        switch self {
        case .info: return "info.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .critical: return "exclamationmark.octagon.fill"
        }
    }
}

// MARK: - 事件

struct AlertEvent: Identifiable, Codable, Hashable {
    let id: UUID
    let date: Date
    let title: String
    let detail: String
    let severity: AlertSeverity
    var isRead: Bool
    let ruleKey: String

    init(
        id: UUID = UUID(),
        date: Date = Date(),
        title: String,
        detail: String,
        severity: AlertSeverity,
        isRead: Bool = false,
        ruleKey: String
    ) {
        self.id = id
        self.date = date
        self.title = title
        self.detail = detail
        self.severity = severity
        self.isRead = isRead
        self.ruleKey = ruleKey
    }
}

// MARK: - 规则定义

enum AlertRuleKey: String, CaseIterable, Hashable {
    case cpuUsage
    case cpuTemperature
    case memoryUsage
    case diskTemperature
    case volumeUsage
    case containerDown
    case poolHealth
    case diskHealth

    var title: String {
        switch self {
        case .cpuUsage: return "CPU 使用率"
        case .cpuTemperature: return "CPU 温度"
        case .memoryUsage: return "内存使用率"
        case .diskTemperature: return "磁盘温度"
        case .volumeUsage: return "存储卷使用率"
        case .containerDown: return "容器异常退出"
        case .poolHealth: return "存储池健康"
        case .diskHealth: return "磁盘健康"
        }
    }

    var summary: String {
        switch self {
        case .cpuUsage: return "持续高于阈值时提醒"
        case .cpuTemperature: return "CPU 温度超过阈值时提醒"
        case .memoryUsage: return "内存占用超过阈值时提醒"
        case .diskTemperature: return "任意一块磁盘温度超过阈值时提醒"
        case .volumeUsage: return "任意卷空间占用超过阈值时提醒"
        case .containerDown: return "运行中的容器转为停止时提醒"
        case .poolHealth: return "存储池降级或异常时提醒"
        case .diskHealth: return "磁盘状态异常时提醒"
        }
    }

    var unit: String {
        switch self {
        case .cpuTemperature, .diskTemperature: return "°C"
        case .cpuUsage, .memoryUsage, .volumeUsage: return "%"
        case .containerDown, .poolHealth, .diskHealth: return ""
        }
    }

    /// 该规则是否需要阈值（否则只有开关）
    var usesThreshold: Bool {
        switch self {
        case .containerDown, .poolHealth, .diskHealth: return false
        default: return true
        }
    }

    var defaultThreshold: Double {
        switch self {
        case .cpuUsage: return 90
        case .cpuTemperature: return 85
        case .memoryUsage: return 90
        case .diskTemperature: return 50
        case .volumeUsage: return 90
        case .containerDown, .poolHealth, .diskHealth: return 0
        }
    }

    var thresholdRange: ClosedRange<Double> {
        switch self {
        case .cpuTemperature: return 40...100
        case .diskTemperature: return 30...80
        default: return 10...100
        }
    }

    var enabledKey: String { "alert.rule.\(rawValue).enabled" }
    var thresholdKey: String { "alert.rule.\(rawValue).threshold" }
}

/// 规则存取的唯一入口。视图用 @AppStorage 写，引擎从这里读，键名保证一致。
enum AlertRuleDefaults {
    static func isEnabled(_ rule: AlertRuleKey) -> Bool {
        let defaults = UserDefaults.standard
        guard defaults.object(forKey: rule.enabledKey) != nil else { return true }
        return defaults.bool(forKey: rule.enabledKey)
    }

    static func threshold(_ rule: AlertRuleKey) -> Double {
        let defaults = UserDefaults.standard
        guard defaults.object(forKey: rule.thresholdKey) != nil else {
            return rule.defaultThreshold
        }
        return defaults.double(forKey: rule.thresholdKey)
    }
}

// MARK: - 事件中心

@Observable
final class AlertCenter {
    private(set) var events: [AlertEvent] = []

    private let storageKey = "alert.events.v1"
    private let maxCount = 200

    init() {
        load()
    }

    var unreadCount: Int {
        var count = 0
        for event in events where !event.isRead {
            count += 1
        }
        return count
    }

    /// 追加事件。同规则同标题 10 分钟内不重复记录，返回 nil 表示被去重。
    @discardableResult
    func add(
        title: String,
        detail: String,
        severity: AlertSeverity,
        ruleKey: String,
        date: Date = Date()
    ) -> AlertEvent? {
        for event in events where event.ruleKey == ruleKey && event.title == title {
            if date.timeIntervalSince(event.date) < 600 {
                return nil
            }
            break
        }

        let event = AlertEvent(
            date: date,
            title: title,
            detail: detail,
            severity: severity,
            ruleKey: ruleKey
        )
        events.insert(event, at: 0)
        if events.count > maxCount {
            events.removeLast(events.count - maxCount)
        }
        save()
        return event
    }

    func markRead(_ event: AlertEvent) {
        guard let index = events.firstIndex(where: { $0.id == event.id }) else { return }
        events[index].isRead = true
        save()
    }

    func markAllRead() {
        var changed = false
        for index in events.indices where !events[index].isRead {
            events[index].isRead = true
            changed = true
        }
        if changed { save() }
    }

    func delete(_ event: AlertEvent) {
        events.removeAll { $0.id == event.id }
        save()
    }

    func clear() {
        events.removeAll()
        save()
    }

    // MARK: 持久化

    private func load() {
        guard let data = UserDefaults.standard.data(forKey: storageKey) else { return }
        guard let decoded = try? JSONDecoder().decode([AlertEvent].self, from: data) else { return }
        events = decoded
    }

    private func save() {
        guard let data = try? JSONEncoder().encode(events) else { return }
        UserDefaults.standard.set(data, forKey: storageKey)
    }
}
