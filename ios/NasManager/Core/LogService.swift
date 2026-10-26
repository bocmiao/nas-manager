import Foundation

// MARK: - 日志条目

struct LogEntry: Identifiable, Hashable {
    let logId: String
    /// 归一化后的级别：info / warning / critical
    let level: String
    let module: String?
    let operatorName: String?
    let content: String
    let createdAt: Date?

    var id: String { logId }

    var levelText: String { LogLevel.title(level) }
}

/// 日志级别。UGOS 只认 info / warning / critical，
/// 传 `error` 直接返回 `8001 无效的输入参数`（踩坑 47），所以这里统一做别名映射。
enum LogLevel {
    static let all = ["info", "warning", "critical"]

    static func normalize(_ raw: String) -> String {
        switch raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "error", "fatal", "crit", "critical":
            return "critical"
        case "warn", "warning":
            return "warning"
        case "info", "information":
            return "info"
        default:
            return raw.lowercased()
        }
    }

    static func title(_ raw: String) -> String {
        switch normalize(raw) {
        case "info": return "信息"
        case "warning": return "警告"
        case "critical": return "严重"
        default: return raw
        }
    }
}

// MARK: - 查询结果

struct LogQueryResult {
    var entries: [LogEntry] = []
    var total: Int? = nil
    /// 本次请求是否成功；分页失败时保留已加载内容
    var available = false
    var message: String? = nil
}

// MARK: - 服务

/// 系统日志 `v1/log/query`。
///
/// 实测要点：
/// - `page` 从 **0** 开始（会话列表是 1 开始，两者相反）
/// - `level` 可重复出现，只接受 info / warning / critical
/// - 列表字段在 `log_list`，不是 list / result（踩坑 31）
/// - 新增的日志是排在前面，配合 `order=down&order_param=create_time`
final class LogService {
    static let pageSize = 30

    private let client: UgosClient

    init(client: UgosClient) {
        self.client = client
    }

    /// 模块候选。接口不可用时返回空数组，视图回落到「全部模块」。
    func modules() async -> [String] {
        guard let data = try? await client.get("v1/log/usersandmodules"),
              let dict = data as? [String: Any],
              let list = dict["modules"] as? [Any] else {
            return []
        }
        return list.compactMap { $0 as? String }
    }

    /// 单页查询。所有筛选条件都通过 query 传递，不做客户端过滤。
    func query(
        page: Int,
        levels: [String],
        module: String?,
        keyword: String
    ) async -> LogQueryResult {
        var pairs: [(String, String)] = [
            ("page", String(max(page, 0))),
            ("size", String(LogService.pageSize)),
            ("order", "down"),
            ("order_param", "create_time")
        ]

        // 级别多选：三个都选时不传 level（等价于不过滤），
        // 只有收窄到 1~2 个级别时才用重复同名参数。
        let chosen = Set(levels.map { LogLevel.normalize($0) })
        if !chosen.isEmpty && chosen != Set(LogLevel.all) {
            for level in LogLevel.all where chosen.contains(level) {
                pairs.append(("level", level))
            }
        }

        if let module = module?.trimmingCharacters(in: .whitespacesAndNewlines), !module.isEmpty {
            pairs.append(("module", module))
        }

        let keyword = keyword.trimmingCharacters(in: .whitespacesAndNewlines)
        if !keyword.isEmpty {
            pairs.append(("keyword", keyword))
        }

        do {
            let data = try await client.getMulti("v1/log/query", pairs: pairs)
            let dict = data as? [String: Any] ?? [:]
            return LogQueryResult(
                entries: LogService.parseEntries(dict["log_list"]),
                total: LogService.int(dict["total"]),
                available: true,
                message: nil
            )
        } catch {
            return LogQueryResult(
                entries: [],
                total: nil,
                available: false,
                message: LogService.friendly(error)
            )
        }
    }

    // MARK: - 解析

    static func parseEntries(_ any: Any?) -> [LogEntry] {
        guard let array = any as? [Any] else { return [] }
        var result: [LogEntry] = []
        result.reserveCapacity(array.count)

        for (index, element) in array.enumerated() {
            guard let item = element as? [String: Any] else { continue }
            let seconds = LogService.int(item["create_time"])
            // 缺少 log_id 时用时间 + 下标兜底，保证 ForEach 的 id 稳定
            let identifier = LogService.string(item["log_id"]) ?? "\(seconds ?? 0)-\(index)"

            result.append(
                LogEntry(
                    logId: identifier,
                    level: LogLevel.normalize(LogService.string(item["level"]) ?? "info"),
                    module: LogService.string(item["module"]),
                    operatorName: LogService.string(item["operator"]),
                    content: LogService.string(item["content"]) ?? "",
                    createdAt: LogService.date(seconds: seconds)
                )
            )
        }
        return result
    }

    static func date(seconds: Int?) -> Date? {
        guard let seconds = seconds, seconds > 0 else { return nil }
        let value = seconds > 1_000_000_000_000 ? Double(seconds) / 1000 : Double(seconds)
        return Date(timeIntervalSince1970: value)
    }

    static func string(_ any: Any?) -> String? {
        guard let any = any else { return nil }
        if let text = any as? String {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }
        if let number = any as? Int { return String(number) }
        if let number = any as? Double { return String(Int(number)) }
        return nil
    }

    static func int(_ any: Any?) -> Int? {
        guard let any = any else { return nil }
        if let value = any as? Int { return value }
        if let value = any as? Double { return Int(value) }
        if let text = any as? String {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            return Int(trimmed)
        }
        return nil
    }

    /// 把底层错误翻译成人话
    static func friendly(_ error: Error) -> String {
        let raw = error.localizedDescription
        if raw.contains("8001") {
            return "固件拒绝了当前筛选条件（8001）：级别只支持 info / warning / critical，可尝试只选一个级别再查"
        }
        if raw.contains("9404") || raw.contains("9405") {
            return "当前固件未开放日志查询接口（模块路由不存在）"
        }
        if raw.contains("1010") || raw.contains("1024") {
            return "登录状态已失效，请到设置页重新连接"
        }
        if raw.contains("HTTP 404") {
            return "日志接口不存在（HTTP 404）"
        }
        return raw.isEmpty ? "日志读取失败" : raw
    }
}
