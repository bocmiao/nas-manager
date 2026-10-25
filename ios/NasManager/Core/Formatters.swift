import Foundation

/// 统一的数值格式化，避免各视图各写一套。
enum Format {
    static func bytes(_ value: UInt64?) -> String {
        guard let value, value > 0 else { return "-" }
        let units = ["B", "KB", "MB", "GB", "TB", "PB"]
        var size = Double(value)
        var index = 0
        while size >= 1024, index < units.count - 1 {
            size /= 1024
            index += 1
        }
        if index <= 1 {
            return String(format: "%.0f %@", size, units[index])
        }
        return String(format: "%.1f %@", size, units[index])
    }

    static func rate(_ value: UInt64?) -> String {
        guard let value, value > 0 else { return "0 B/s" }
        return bytes(value) + "/s"
    }

    static func uptime(_ seconds: Int?) -> String {
        guard let seconds, seconds > 0 else { return "-" }
        let days = seconds / 86400
        let hours = (seconds % 86400) / 3600
        let minutes = (seconds % 3600) / 60
        if days > 0 { return "\(days) 天 \(hours) 小时" }
        if hours > 0 { return "\(hours) 小时 \(minutes) 分" }
        return "\(minutes) 分"
    }

    static func percent(_ value: Double?, digits: Int = 0) -> String {
        guard let value else { return "-" }
        return String(format: "%.\(digits)f%%", value)
    }

    static func hours(_ value: Int?) -> String {
        guard let value, value > 0 else { return "-" }
        if value >= 48 { return "\(value) 小时（\(value / 24) 天）" }
        return "\(value) 小时"
    }

    static func relative(_ date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.unitsStyle = .short
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    static func timestamp(_ date: Date?) -> String {
        guard let date else { return "-" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "MM-dd HH:mm:ss"
        return formatter.string(from: date)
    }
}
