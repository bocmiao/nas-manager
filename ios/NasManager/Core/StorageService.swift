import Foundation

// MARK: - SMART 结果

struct SMARTRow: Identifiable, Hashable {
    let id = UUID()
    let title: String
    let detail: String
}

struct SMARTInfo {
    var health: String?
    var temperature: Int?
    var powerOnHours: Int?
    var rows: [SMARTRow] = []

    var isEmpty: Bool {
        rows.isEmpty && health == nil && temperature == nil && powerOnHours == nil
    }
}

// MARK: - 服务

/// 存储相关：SMART 明细。
///
/// `v1/storage/disk/smart/info` 必须带磁盘参数，否则回 code 9999（踩坑第 14 条）。
/// 具体参数名各固件不一致，按 name / disk / disk_name 依次兜底。
final class StorageService {
    private let client: UgosClient

    init(client: UgosClient) {
        self.client = client
    }

    func smartInfo(for disk: Disk) async throws -> SMARTInfo {
        var identifiers: [String] = []
        if let name = disk.name, !name.isEmpty {
            identifiers.append(name)
        }
        if let label = disk.label, !label.isEmpty, !identifiers.contains(label) {
            identifiers.append(label)
        }

        var lastError: Error = UgosError.decoding("磁盘缺少可用标识，无法查询 SMART")
        for identifier in identifiers {
            for key in ["name", "disk", "disk_name"] {
                do {
                    let data = try await client.get(
                        "v1/storage/disk/smart/info",
                        params: [key: identifier]
                    )
                    let info = SMARTInfo.parse(data)
                    if !info.isEmpty {
                        return info
                    }
                    lastError = UgosError.decoding("SMART 返回内容为空")
                } catch {
                    lastError = error
                }
            }
        }
        throw lastError
    }
}

// MARK: - SMART 解析

extension SMARTInfo {
    /// 接口返回结构在不同固件上差异较大，这里做宽松解析：
    /// 先按常见键名取健康/温度/通电时长，再把属性表或剩余标量铺成明细。
    static func parse(_ any: Any) -> SMARTInfo {
        var info = SMARTInfo()
        let root = unwrap(any)

        guard let dict = root as? [String: Any] else {
            let text = String(describing: root).trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty, text != "0" {
                info.rows = [SMARTRow(title: "原始返回", detail: text)]
            }
            return info
        }

        var scalars: [(String, String)] = []
        collect(dict, prefix: "", depth: 0, into: &scalars)

        info.health = normalizeHealth(
            lookup(scalars, keys: ["health", "health_status", "smart_health", "smart_status", "status"])
        )
        info.temperature = intValue(
            lookup(scalars, keys: ["temperature", "temp", "temperature_celsius", "temp_celsius", "current_temperature"])
        )
        info.powerOnHours = intValue(
            lookup(scalars, keys: ["power_on_hours", "poweronhours", "power_on_time", "power_on_time_hours", "used_hours", "running_hours"])
        )

        info.rows = attributeRows(from: dict)
        if info.rows.isEmpty {
            let sorted = scalars.sorted { $0.0 < $1.0 }
            info.rows = sorted.prefix(40).map { SMARTRow(title: $0.0, detail: $0.1) }
        }
        return info
    }

    /// 常见的一层包装：{"data": {...}} / {"result": {...}}
    private static func unwrap(_ any: Any) -> Any {
        guard let dict = any as? [String: Any], dict.count == 1 else { return any }
        guard let key = dict.keys.first else { return any }
        let wrappers = ["data", "result", "smart", "info", "smart_info"]
        guard wrappers.contains(key), let inner = dict[key] else { return any }
        return inner
    }

    private static func collect(_ any: Any, prefix: String, depth: Int, into out: inout [(String, String)]) {
        guard depth <= 2, let dict = any as? [String: Any] else { return }
        for (key, value) in dict {
            let path = prefix.isEmpty ? key : prefix + "." + key
            if let nested = value as? [String: Any] {
                collect(nested, prefix: path, depth: depth + 1, into: &out)
            } else if let array = value as? [Any] {
                // 属性表单独渲染，这里跳过
                if let first = array.first, first is [String: Any] { continue }
                let text = array.map { stringify($0) }.joined(separator: ", ")
                if !text.isEmpty { out.append((path, text)) }
            } else {
                let text = stringify(value)
                if !text.isEmpty { out.append((path, text)) }
            }
        }
    }

    private static func stringify(_ value: Any) -> String {
        if let text = value as? String { return text }
        if let number = value as? NSNumber { return number.stringValue }
        return String(describing: value)
    }

    private static func lookup(_ scalars: [(String, String)], keys: [String]) -> String? {
        for key in keys {
            let lowered = key.lowercased()
            for pair in scalars {
                let path = pair.0.lowercased()
                if path == lowered || path.hasSuffix("." + lowered) {
                    let text = pair.1.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !text.isEmpty { return text }
                }
            }
        }
        return nil
    }

    private static func intValue(_ text: String?) -> Int? {
        guard let text else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        // "36 C" / "36°C" 这类带单位的取值，只保留数字前缀
        let numeric = String(trimmed.prefix { $0.isNumber || $0 == "." || $0 == "-" })
        guard !numeric.isEmpty else { return nil }
        if let value = Int(numeric) { return value }
        if let value = Double(numeric) { return Int(value) }
        return nil
    }

    private static func normalizeHealth(_ text: String?) -> String? {
        guard let text else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        switch trimmed.lowercased() {
        case "ok", "good", "passed", "pass", "healthy", "normal":
            return "正常"
        case "bad", "failed", "fail", "caution", "warning", "critical":
            return "异常"
        default:
            return trimmed
        }
    }

    private static func attributeRows(from dict: [String: Any]) -> [SMARTRow] {
        for key in ["attributes", "smart_attributes", "attrs", "table", "items", "list"] {
            guard let array = dict[key] as? [[String: Any]], !array.isEmpty else { continue }
            return array.map { item in
                let name = text(item["name"]) ?? text(item["attribute"]) ?? text(item["id"]) ?? "属性"
                let value = text(item["value"]) ?? "-"
                var detail = "值 " + value
                if let worst = text(item["worst"]) {
                    detail += " · 最差 " + worst
                }
                if let threshold = text(item["threshold"]) ?? text(item["thresh"]) {
                    detail += " · 阈值 " + threshold
                }
                if let raw = text(item["raw"]) ?? text(item["raw_value"]) {
                    detail += " · RAW " + raw
                }
                return SMARTRow(title: name, detail: detail)
            }
        }
        return []
    }

    private static func text(_ value: Any?) -> String? {
        guard let value else { return nil }
        if let text = value as? String {
            return text.isEmpty ? nil : text
        }
        if let number = value as? NSNumber {
            return number.stringValue
        }
        return String(describing: value)
    }
}
