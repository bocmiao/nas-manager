import Foundation

// MARK: - 备份任务

struct BackupTask: Identifiable, Hashable {
    let id: String
    let name: String
    let statusCode: Int?
    let statusText: String
    let typeText: String?
    let progress: Int?
    let lastRun: Date?
    let nextRun: Date?
    let detail: String?

    var isRunning: Bool { statusCode == 2 || statusCode == 4 }
}

struct BackupQueryResult {
    var tasks: [BackupTask] = []
    var available = false
    var message: String? = nil
}

// MARK: - 服务

/// 「同步与备份」任务列表 `v1/web/sync/task/list`。
///
/// 本机 0 个任务，任务条目的字段名只能按前端归一化逻辑推断（schema_verified=false），
/// 所以这里逐字段宽松解析，取不到就留 nil，界面显示 “-” 而不是编造。
/// 写操作（立即运行 / 暂停 / 删除）一律不实现。
final class BackupService {
    private let client: UgosClient

    init(client: UgosClient) {
        self.client = client
    }

    func tasks() async -> BackupQueryResult {
        do {
            let data = try await client.get("v1/web/sync/task/list")
            let dict = data as? [String: Any] ?? [:]
            return BackupQueryResult(
                tasks: BackupService.parseTasks(dict["list"]),
                available: true,
                message: nil
            )
        } catch {
            return BackupQueryResult(
                tasks: [],
                available: false,
                message: BackupService.friendly(error)
            )
        }
    }

    // MARK: - 解析

    static func parseTasks(_ any: Any?) -> [BackupTask] {
        guard let array = any as? [Any] else { return [] }
        var result: [BackupTask] = []
        result.reserveCapacity(array.count)

        for (index, element) in array.enumerated() {
            guard let item = element as? [String: Any] else { continue }

            let numericStatus = LogService.int(item["status"])
            let rawStatus = LogService.string(item["status"])
            let code = BackupService.statusCode(numeric: numericStatus, raw: rawStatus)
            let name = LogService.string(item["task_name"])
                ?? LogService.string(item["name"])
                ?? "任务 \(index + 1)"

            let identifier = LogService.string(item["id"]) ?? "task-\(index)"

            // 上次 / 下次运行时间的键名未经真机验证，按常见写法逐个尝试
            let lastRun = BackupService.date(
                LogService.int(item["last_run_time"])
                    ?? LogService.int(item["last_time"])
                    ?? LogService.int(item["last_exec_time"])
                    ?? LogService.int(item["last_start_time"])
                    ?? LogService.int(item["end_time"])
            )
            let nextRun = BackupService.date(
                LogService.int(item["next_run_time"])
                    ?? LogService.int(item["next_time"])
                    ?? LogService.int(item["next_exec_time"])
                    ?? LogService.int(item["schedule_time"])
            )

            var detailParts: [String] = []
            if let local = BackupService.joinedText(item["backup_local_paths"]) {
                detailParts.append(local)
            }
            if let remote = BackupService.joinedText(item["backup_remote_paths"]) {
                detailParts.append(remote)
            }

            result.append(
                BackupTask(
                    id: identifier,
                    name: name,
                    statusCode: code,
                    statusText: BackupService.statusText(code: code, raw: rawStatus),
                    typeText: BackupService.typeText(LogService.int(item["backup_type"])),
                    progress: BackupService.progressPercent(item["progress"]),
                    lastRun: lastRun,
                    nextRun: nextRun,
                    detail: detailParts.isEmpty ? nil : detailParts.joined(separator: " → ")
                )
            )
        }
        return result
    }

    /// 状态枚举（前端 TaskStatus）：0 完成 / 1 失败 / 2 扫描 / 3 暂停 / 4 传输中 / 5 未启动 / 6 等待
    static func statusCode(numeric: Int?, raw: String?) -> Int? {
        if let numeric { return numeric }
        switch raw ?? "" {
        case "finished", "partialSuccess": return 0
        case "error": return 1
        case "scanning": return 2
        case "paused": return 3
        case "progress": return 4
        case "unStart": return 5
        case "waiting": return 6
        default: return nil
        }
    }

    static func statusText(code: Int?, raw: String?) -> String {
        guard let code else {
            if let raw, !raw.isEmpty, Int(raw) == nil { return raw }
            return "未知"
        }
        switch code {
        case 0: return "已完成"
        case 1: return "失败"
        case 2: return "扫描中"
        case 3: return "已暂停"
        case 4: return "进行中"
        case 5: return "未开始"
        case 6: return "等待中"
        default: return "状态 \(code)"
        }
    }

    static func typeText(_ code: Int?) -> String? {
        guard let code else { return nil }
        switch code {
        case 1: return "增量备份"
        case 2: return "镜像备份"
        case 3: return "多版本备份"
        default: return "类型 \(code)"
        }
    }

    static func date(_ seconds: Int?) -> Date? {
        guard let seconds, seconds > 0 else { return nil }
        let value = seconds > 1_000_000_000_000 ? Double(seconds) / 1000 : Double(seconds)
        return Date(timeIntervalSince1970: value)
    }

    /// 进度统一成 0~100 的整数。
    /// 后端对 `0 <= progress <= 1` 的浮点给的是比例（0.42 = 42%），
    /// 直接用 `Int()` 会截成 0，必须先乘 100 再钳位。
    static func progressPercent(_ any: Any?) -> Int? {
        guard let any else { return nil }
        let raw: Double?
        if let number = any as? NSNumber {
            raw = number.doubleValue
        } else if let text = any as? String {
            raw = Double(text.trimmingCharacters(in: .whitespacesAndNewlines))
        } else {
            raw = nil
        }
        guard let value = raw, value.isFinite else { return nil }
        let percent = value <= 1 ? value * 100 : value
        // 先钳位再转 Int，避免极端值在 Int() 处溢出崩溃
        return Int(min(max(percent, 0), 100).rounded())
    }

    /// 路径字段可能是字符串也可能是数组，两种都能展示
    private static func joinedText(_ any: Any?) -> String? {
        if let text = LogService.string(any) { return text }
        if let array = any as? [Any] {
            let parts = array.compactMap { LogService.string($0) }
            return parts.isEmpty ? nil : parts.joined(separator: ", ")
        }
        return nil
    }

    static func friendly(_ error: Error) -> String {
        let raw = error.localizedDescription
        if raw.contains("9404") || raw.contains("9405") {
            return "接口返回 9404（模块内路由不存在），当前固件可能未开放备份任务列表"
        }
        if raw.contains("HTTP 404") {
            return "NAS 上没有安装「同步与备份」应用，或该应用未挂载"
        }
        if raw.contains("9999") {
            return "「同步与备份」服务返回失败：\(raw)"
        }
        if raw.contains("1010") || raw.contains("1024") {
            return "登录状态已失效，请到设置页重新连接"
        }
        return raw.isEmpty ? "备份任务读取失败" : raw
    }
}
