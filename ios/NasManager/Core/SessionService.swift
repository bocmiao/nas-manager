import Foundation

// MARK: - 在线会话

struct OnlineSession: Identifiable, Hashable {
    let id: String
    let sessionId: Int?
    let username: String?
    /// 客户端来源地址，原样展示，不做地理位置推测（踩坑 46：host 不是来源 IP）
    let ip: String?
    let host: String?
    let loginAt: Date?
    let durationSeconds: Int?
    let platformCode: Int?
    let protocolCode: Int?
    let isCurrent: Bool
    let pid: String?

    var platformText: String {
        guard let code = platformCode else { return "未知" }
        switch code {
        case 2: return "iOS"
        case 4: return "Android"
        case 5: return "未标注"
        case 6: return "TV"
        case 7: return "HarmonyOS"
        case 8: return "HyperOS"
        case 9: return "PC / Web"
        case 10: return "车机"
        case 11: return "HDMI"
        default: return "平台 \(code)"
        }
    }

    var protocolText: String {
        guard let code = protocolCode else { return "未知" }
        switch code {
        case 0: return "HTTP"
        case 1: return "HTTPS"
        case 2: return "SMB"
        case 3: return "FTP(S)"
        case 4: return "SFTP"
        case 5: return "SSH"
        case 6: return "未标注"
        default: return "协议 \(code)"
        }
    }

    var durationText: String {
        guard let seconds = durationSeconds, seconds > 0 else { return "刚刚" }
        let hours = seconds / 3600
        let minutes = (seconds % 3600) / 60
        if hours > 0 { return "\(hours) 小时 \(minutes) 分" }
        if minutes > 0 { return "\(minutes) 分" }
        return "\(seconds) 秒"
    }
}

struct SessionQueryResult {
    var sessions: [OnlineSession] = []
    var total: Int? = nil
    var available = false
    var message: String? = nil
}

// MARK: - 登录审计

struct LoginRecord: Identifiable, Hashable {
    let id: String
    let success: Bool
    let username: String?
    let ip: String?
    let createdAt: Date?
    /// Web / App、SSH 或「其他」
    let method: String
    let content: String
}

struct LoginAuditResult {
    var records: [LoginRecord] = []
    var total: Int? = nil
    var available = false
    var message: String? = nil
}

// MARK: - 服务

/// 在线会话 + 登录审计。
///
/// - `v1/user/online/list` **只接受 POST**，GET 返回 9404（踩坑 44），`page` 从 1 起
/// - 登录审计没有独立接口，只能读 `v1/log/query?module=login`（踩坑 48）
/// - 失败记录在日志里是 warning 级别，不是 error
final class SessionService {
    private let client: UgosClient

    init(client: UgosClient) {
        self.client = client
    }

    // MARK: 在线会话

    /// 只用第一页。`size` 实测 50 可用，不冒险传更大值；
    /// 会话数超过一页时界面按 `total` 如实标注，不做静默截断。
    func sessions(page: Int = 1, size: Int = 50) async -> SessionQueryResult {
        let body: [String: Any] = [
            // 空数组也要显式给出类型，避免字面量推断失败
            "uid_list": [Int](),
            "user_type": "user",
            "page": max(page, 1),
            "size": size
        ]
        do {
            let data = try await client.post("v1/user/online/list", json: body)
            let dict = data as? [String: Any] ?? [:]
            return SessionQueryResult(
                sessions: SessionService.parseSessions(dict["list"]),
                total: LogService.int(dict["total"]),
                available: true,
                message: nil
            )
        } catch {
            return SessionQueryResult(
                sessions: [],
                total: nil,
                available: false,
                message: SessionService.friendly(error, path: "v1/user/online/list")
            )
        }
    }

    static func parseSessions(_ any: Any?) -> [OnlineSession] {
        guard let array = any as? [Any] else { return [] }
        var result: [OnlineSession] = []
        result.reserveCapacity(array.count)

        for (index, element) in array.enumerated() {
            guard let item = element as? [String: Any] else { continue }
            let sessionId = LogService.int(item["id"])
            let identifier = sessionId.map { String($0) } ?? "session-\(index)"

            result.append(
                OnlineSession(
                    id: identifier,
                    sessionId: sessionId,
                    username: LogService.string(item["username"]),
                    ip: LogService.string(item["ip"]),
                    host: LogService.string(item["host"]),
                    loginAt: LogService.date(seconds: LogService.int(item["login_time"])),
                    durationSeconds: LogService.int(item["connection_duration"]),
                    platformCode: LogService.int(item["platform"]),
                    protocolCode: LogService.int(item["protocol"]),
                    isCurrent: SessionService.bool(item["is_current_connection"]) ?? false,
                    pid: LogService.string(item["pid"])
                )
            )
        }
        return result
    }

    // MARK: 登录审计

    func loginAudit(size: Int = 50) async -> LoginAuditResult {
        let params: [String: String] = [
            "page": "0",
            "size": String(size),
            "module": "login",
            "order": "down",
            "order_param": "create_time"
        ]
        do {
            let data = try await client.get("v1/log/query", params: params)
            let dict = data as? [String: Any] ?? [:]
            return LoginAuditResult(
                records: SessionService.parseLoginRecords(dict["log_list"]),
                total: LogService.int(dict["total"]),
                available: true,
                message: nil
            )
        } catch {
            return LoginAuditResult(
                records: [],
                total: nil,
                available: false,
                message: SessionService.friendly(error, path: "v1/log/query")
            )
        }
    }

    /// 从审计正文里抽用户名与来源地址。
    /// 已知三种形态：
    /// - `User [x] from [ip] logged in successfully.`
    /// - `User [x] from [ip] failed to log in due to ...`
    /// - `User x logged into the system via SSH.`（没有 IP）
    /// 形态不认识时按级别兜底：info 视为成功，warning / critical 视为失败。
    static func parseLoginRecords(_ any: Any?) -> [LoginRecord] {
        guard let array = any as? [Any] else { return [] }
        var result: [LoginRecord] = []
        result.reserveCapacity(array.count)

        for (index, element) in array.enumerated() {
            guard let item = element as? [String: Any] else { continue }
            let content = LogService.string(item["content"]) ?? ""
            let lower = content.lowercased()
            let seconds = LogService.int(item["create_time"])
            let level = LogLevel.normalize(LogService.string(item["level"]) ?? "info")

            let isSSH = lower.contains("via ssh")
            let failed = lower.contains("failed to log in") || lower.contains("login failed")
            let succeeded = lower.contains("logged in successfully")
                || lower.contains("logged into the system")
            let success = failed ? false : (succeeded ? true : level == "info")

            var username = SessionService.bracketValue(content, after: "User [", before: "]")
            if username == nil, isSSH {
                username = SessionService.bracketValue(content, after: "User ", before: " logged")
            }
            if username == nil {
                let operatorName = LogService.string(item["operator"])
                username = operatorName == "system" ? nil : operatorName
            }

            let ip = SessionService.bracketValue(content, after: "from [", before: "]")
            let method: String
            if isSSH {
                method = "SSH"
            } else if ip != nil {
                method = "Web / App"
            } else {
                method = "其他"
            }

            let identifier = LogService.string(item["log_id"])
                ?? "login-\(seconds ?? 0)-\(index)"

            result.append(
                LoginRecord(
                    id: identifier,
                    success: success,
                    username: username,
                    ip: ip,
                    createdAt: LogService.date(seconds: seconds),
                    method: method,
                    content: content
                )
            )
        }
        return result
    }

    // MARK: 工具

    private static func bracketValue(_ text: String, after start: String, before end: String) -> String? {
        guard let startRange = text.range(of: start) else { return nil }
        let rest = text[startRange.upperBound...]
        guard let endRange = rest.range(of: end) else { return nil }
        let value = String(rest[..<endRange.lowerBound]).trimmingCharacters(in: .whitespaces)
        return value.isEmpty ? nil : value
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

    static func friendly(_ error: Error, path: String) -> String {
        let raw = error.localizedDescription
        if raw.contains("9404") || raw.contains("9405") {
            return "当前固件未开放该接口（\(path)），可能版本不支持"
        }
        if raw.contains("1010") || raw.contains("1024") {
            return "登录状态已失效，请到设置页重新连接"
        }
        if raw.contains("HTTP 404") {
            return "接口不存在（HTTP 404），可能未安装对应应用"
        }
        return raw.isEmpty ? "读取失败" : raw
    }
}
