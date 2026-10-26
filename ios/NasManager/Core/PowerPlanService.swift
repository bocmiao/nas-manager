import Foundation

// MARK: - 单条计划

struct PowerSwitch: Identifiable, Hashable {
    let index: Int
    /// 星期编号：1=周一 … 7=周日
    let weekdays: [Int]
    /// "08:00"
    let time: String
    /// open / close
    let action: String

    var id: Int { index }

    var actionText: String {
        switch action.lowercased() {
        case "open", "on", "boot": return "开机"
        case "close", "off", "shutdown": return "关机"
        default: return action
        }
    }

    var weekdaysText: String {
        let set = Set(weekdays)
        if set == Set([1, 2, 3, 4, 5]) { return "工作日" }
        if set == Set([6, 7]) { return "周末" }
        if set == Set([1, 2, 3, 4, 5, 6, 7]) { return "每天" }
        guard !set.isEmpty else { return "未设置" }

        let names = ["一", "二", "三", "四", "五", "六", "日"]
        let labels = weekdays.sorted().map { day -> String in
            guard day >= 1, day <= 7 else { return "未知" }
            return "周" + names[day - 1]
        }
        return labels.joined(separator: "、")
    }

    /// 按本机时区推算下一次执行时间。仅用于展示，不写回设备。
    func nextRun(after now: Date = Date()) -> Date? {
        guard !weekdays.isEmpty, let parsed = PowerSwitch.parseTime(time) else { return nil }
        let calendar = Calendar.current
        var best: Date?

        for offset in 0...7 {
            guard let day = calendar.date(byAdding: .day, value: offset, to: now) else { continue }
            guard weekdays.contains(PowerSwitch.weekdayNumber(for: day, calendar: calendar)) else { continue }

            var comps = calendar.dateComponents([.year, .month, .day], from: day)
            comps.hour = parsed.hour
            comps.minute = parsed.minute
            comps.second = 0
            guard let candidate = calendar.date(from: comps), candidate > now else { continue }

            if let current = best {
                if candidate < current { best = candidate }
            } else {
                best = candidate
            }
        }
        return best
    }

    /// Calendar 的 weekday 是 1=周日，这里换算成 1=周一 … 7=周日
    static func weekdayNumber(for date: Date, calendar: Calendar) -> Int {
        let value = calendar.component(.weekday, from: date)
        return value == 1 ? 7 : value - 1
    }

    static func parseTime(_ raw: String) -> (hour: Int, minute: Int)? {
        let parts = raw.split(separator: ":")
        guard parts.count >= 2,
              let hour = Int(parts[0]),
              let minute = Int(parts[1]),
              hour >= 0, hour < 24,
              minute >= 0, minute < 60 else {
            return nil
        }
        return (hour, minute)
    }
}

// MARK: - 查询结果

struct PowerPlanResult {
    var enabled: Bool? = nil
    var powerBoot: Bool? = nil
    var wakeOn: Bool? = nil
    var switches: [PowerSwitch] = []
    var available = false
    var message: String? = nil
}

// MARK: - 服务

/// 电源计划（只读）。
///
/// 没有独立端点（`hardware/power/{plan,schedule,list,time_switch}` 全 9404），
/// 计划表就在 `v1/hardware/power/config` 的 `time_switches` 里，与硬盘休眠策略共用接口。
final class PowerPlanService {
    private let client: UgosClient

    init(client: UgosClient) {
        self.client = client
    }

    func plan() async -> PowerPlanResult {
        do {
            let data = try await client.get("v1/hardware/power/config")
            guard let dict = data as? [String: Any] else {
                return PowerPlanResult(
                    available: false,
                    message: "电源配置返回结构异常，暂时无法解读"
                )
            }

            return PowerPlanResult(
                enabled: PowerPlanService.bool(dict["enable_scheduled_power"]),
                powerBoot: PowerPlanService.bool(dict["power_boot"]),
                wakeOn: PowerPlanService.bool(dict["wake_on"]),
                switches: PowerPlanService.parseSwitches(dict["time_switches"]),
                available: true,
                message: nil
            )
        } catch {
            return PowerPlanResult(
                available: false,
                message: PowerPlanService.friendly(error)
            )
        }
    }

    // MARK: - 解析

    static func parseSwitches(_ any: Any?) -> [PowerSwitch] {
        guard let array = any as? [Any] else { return [] }
        var result: [PowerSwitch] = []

        for (index, element) in array.enumerated() {
            guard let item = element as? [String: Any] else { continue }
            result.append(
                PowerSwitch(
                    index: index,
                    weekdays: PowerPlanService.weekdays(item["frequency"]),
                    time: LogService.string(item["time"]) ?? "-",
                    action: LogService.string(item["action"]) ?? ""
                )
            )
        }
        return result
    }

    /// frequency 可能是 [7,1,2]、["1","2"] 或 "1,2"
    private static func weekdays(_ any: Any?) -> [Int] {
        if let array = any as? [Any] {
            return array.compactMap { element in
                if let value = element as? Int { return value }
                if let text = element as? String { return Int(text.trimmingCharacters(in: .whitespaces)) }
                return nil
            }
        }
        if let text = any as? String {
            return text.split(separator: ",").compactMap { part in
                Int(String(part).trimmingCharacters(in: .whitespaces))
            }
        }
        return []
    }

    private static func bool(_ any: Any?) -> Bool? {
        if let value = any as? Bool { return value }
        if let value = any as? Int { return value != 0 }
        if let text = any as? String {
            switch text.lowercased() {
            case "true", "1": return true
            case "false", "0": return false
            default: return nil
            }
        }
        return nil
    }

    static func friendly(_ error: Error) -> String {
        let raw = error.localizedDescription
        if raw.contains("9404") || raw.contains("9405") {
            return "当前固件未开放电源配置接口"
        }
        if raw.contains("HTTP 404") {
            return "电源配置接口不存在（HTTP 404）"
        }
        if raw.contains("1010") || raw.contains("1024") {
            return "登录状态已失效，请到设置页重新连接"
        }
        return raw.isEmpty ? "电源计划读取失败" : raw
    }
}
