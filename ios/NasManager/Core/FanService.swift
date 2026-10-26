import Foundation

// MARK: - 档位表

/// 风扇档位表的一行。真机上 `support_custom=false` 且表为空，
/// 这里只在接口确实返回内容时才展示，取不到字段就把原始键值对铺出来。
struct FanLevelPoint: Identifiable, Hashable {
    let index: Int
    let temperature: Int?
    let speed: Int?
    let raw: String

    var id: Int { index }
}

// MARK: - 快照

struct FanSnapshot {
    var mode: Int? = nil
    var supportCustom = false
    var sysFanLevels: [FanLevelPoint] = []
    var cpuFanLevels: [FanLevelPoint] = []
    var deviceSpeed: Int? = nil
    var deviceStatus: Int? = nil
    var cpuSpeed: Int? = nil
    var cpuStatus: Int? = nil
    /// fan/config 是否读到
    var configAvailable = false
    var message: String? = nil

    var available: Bool {
        configAvailable || deviceSpeed != nil || cpuSpeed != nil
    }

    /// 1=静音 2=自动 3=全速 4=自定义（枚举来自前端，接口只给数字，踩坑 50）
    var modeText: String {
        guard let mode = mode else { return "未知" }
        switch mode {
        case 1: return "静音"
        case 2: return "自动"
        case 3: return "全速"
        case 4: return "自定义"
        default: return "模式 \(mode)"
        }
    }

    /// 只有机型支持自定义且确实返回了档位表时才展示曲线区块
    var hasLevelTables: Bool {
        supportCustom && !(sysFanLevels.isEmpty && cpuFanLevels.isEmpty)
    }
}

// MARK: - 服务

/// 风扇（只读）。
///
/// - 策略在 `v1/hardware/fan/config`
/// - 实际转速在 `v1/taskmgr/stat/overview` 的 `cpu_fan` / `device_fan`
/// - 目标转速与 PWM 没有任何接口；`support_custom=false` 时也不显示档位表
/// - `has_sys_fan` / `has_cpu_fan` 为 false 不代表没有风扇（踩坑 51），不要据此隐藏区块
final class FanService {
    private let client: UgosClient

    init(client: UgosClient) {
        self.client = client
    }

    func snapshot() async -> FanSnapshot {
        var result = FanSnapshot()
        var errors: [String] = []

        do {
            let data = try await client.get("v1/hardware/fan/config")
            let dict = data as? [String: Any] ?? [:]
            result.mode = FanService.int(dict["mode"]) ?? FanService.int(dict["result"])
            result.supportCustom = FanService.bool(dict["support_custom"]) ?? false
            result.sysFanLevels = FanService.levelPoints(dict["sys_fan_table"])
            result.cpuFanLevels = FanService.levelPoints(dict["cpu_fan_table"])
            result.configAvailable = true
        } catch {
            errors.append(FanService.friendly(error))
        }

        do {
            let live = try await client.getDecodable(
                LiveOverview.self,
                path: "v1/taskmgr/stat/overview"
            )
            result.deviceSpeed = live.deviceFan?.first?.speed
            result.deviceStatus = live.deviceFan?.first?.status
            result.cpuSpeed = live.cpuFan?.first?.speed
            result.cpuStatus = live.cpuFan?.first?.status
        } catch {
            errors.append("转速读取失败：\(FanService.friendly(error))")
        }

        if !errors.isEmpty {
            result.message = errors.joined(separator: "；")
        }
        return result
    }

    // MARK: - 解析

    private static func levelPoints(_ any: Any?) -> [FanLevelPoint] {
        guard let array = any as? [Any] else { return [] }
        var result: [FanLevelPoint] = []

        for (index, element) in array.enumerated() {
            guard let item = element as? [String: Any] else { continue }
            let temperature = FanService.int(item["temperature"])
                ?? FanService.int(item["temp"])
                ?? FanService.int(item["cpu_temp"])
            let speed = FanService.int(item["speed"])
                ?? FanService.int(item["rpm"])
                ?? FanService.int(item["value"])

            var parts: [String] = []
            for key in item.keys.sorted() {
                if let text = LogService.string(item[key]) {
                    parts.append("\(key)=\(text)")
                }
            }

            result.append(
                FanLevelPoint(
                    index: index,
                    temperature: temperature,
                    speed: speed,
                    raw: parts.joined(separator: " · ")
                )
            )
        }
        return result
    }

    private static func int(_ any: Any?) -> Int? {
        if let value = any as? Int { return value }
        if let value = any as? Double { return Int(value) }
        if let text = any as? String { return Int(text.trimmingCharacters(in: .whitespaces)) }
        return nil
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
            return "当前固件未开放风扇接口，页面只能显示转速部分"
        }
        if raw.contains("1010") || raw.contains("1024") {
            return "登录状态已失效，请到设置页重新连接"
        }
        if raw.contains("HTTP 404") {
            return "风扇接口不存在（HTTP 404）"
        }
        return raw.isEmpty ? "风扇数据读取失败" : raw
    }
}
