import Foundation

/// 一个候选接入地址
struct AddressCandidate {
    let endpoint: UgosEndpoint
    /// "内网" / "公网"
    let label: String
}

/// 规范化后的地址：host 一律不带方括号，拼 URL 时再补。
struct NormalizedAddress: Equatable {
    let host: String
    let port: Int

    var isIPv6: Bool { EndpointResolver.isIPv6Literal(host) }
    /// 可直接拼进 URL 的主机片段
    var urlHost: String { EndpointResolver.bracketIfNeeded(host) }
}

/// 双地址探测：谁先响应就用谁。
///
/// 判据是「能否拿到任意 HTTP 响应」——UGOS 对无 token 的请求照样回 200 + code 1010，
/// 所以只要 TCP/TLS 通了就算可达，不需要真的登录成功。
enum EndpointResolver {
    static func resolve(_ candidates: [AddressCandidate], timeout: TimeInterval = 4) async -> AddressCandidate? {
        for candidate in candidates {
            if await isReachable(candidate.endpoint, timeout: timeout) {
                return candidate
            }
        }
        return nil
    }

    static func isReachable(_ endpoint: UgosEndpoint, timeout: TimeInterval = 4) async -> Bool {
        guard let base = endpoint.baseURL else { return false }
        guard let url = URL(string: base.absoluteString + "/ugreen/v1/verify/sn") else { return false }

        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.timeoutIntervalForResource = timeout
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpCookieStorage = nil

        let delegate = TOFUTrustDelegate()
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }

        do {
            let (_, response) = try await session.data(for: URLRequest(url: url))
            return response is HTTPURLResponse
        } catch {
            return false
        }
    }

    // MARK: - 地址规范化（纯函数，无副作用，可单测）

    /// 把用户输入整理成「主机 + 端口」。
    ///
    /// 规则：
    /// - 去掉协议前缀、路径、`user@`、首尾空白
    /// - 已是 `[::1]` / `[::1]:9443` 形式：取方括号内的 IPv6 与括号后的端口
    /// - 裸 IPv6（冒号多于 1 个，不可能是 host:port）：整串当主机
    /// - 只有一个冒号且后缀全是数字：按 host:port 拆开
    /// - 端口非法时回落到 defaultPort
    static func normalize(host rawHost: String, port rawPort: String, defaultPort: Int = 9443) -> NormalizedAddress? {
        var text = rawHost.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }

        if let range = text.range(of: "://") {
            text = String(text[range.upperBound...])
        }
        if let slash = text.firstIndex(of: "/") {
            text = String(text[..<slash])
        }
        if let at = text.lastIndex(of: "@") {
            text = String(text[text.index(after: at)...])
        }
        text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }

        var host = text
        var inlinePort: Int?

        if text.hasPrefix("[") {
            // 带方括号：必须成对，才能确定主机边界
            guard let closing = text.firstIndex(of: "]") else { return nil }
            host = String(text[text.index(after: text.startIndex)..<closing])
            let rest = String(text[text.index(after: closing)...])
            if !rest.isEmpty {
                guard rest.hasPrefix(":") else { return nil }
                inlinePort = Int(rest.dropFirst())
            }
        } else if text.filter({ $0 == ":" }).count == 1 {
            // 恰好一个冒号才可能是 host:port；更多冒号按 IPv6 处理
            guard let colon = text.lastIndex(of: ":") else { return nil }
            let suffix = String(text[text.index(after: colon)...])
            if !suffix.isEmpty, suffix.allSatisfy({ $0.isNumber }) {
                inlinePort = Int(suffix)
                host = String(text[..<colon])
            }
        }

        host = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard isValidHost(host) else { return nil }

        let trimmedPort = rawPort.trimmingCharacters(in: .whitespacesAndNewlines)
        var port = inlinePort ?? (Int(trimmedPort) ?? defaultPort)
        if port < 1 || port > 65535 {
            port = defaultPort
        }
        guard port >= 1, port <= 65535 else { return nil }

        return NormalizedAddress(host: host, port: port)
    }

    /// 冒号多于 1 个 → IPv6 字面量（host:port 最多只有一个冒号）
    static func isIPv6Literal(_ host: String) -> Bool {
        let value = unwrapped(host)
        guard !value.isEmpty else { return false }
        return value.filter({ $0 == ":" }).count > 1
    }

    /// IPv6 拼进 URL 必须包方括号，否则会被当成 host:port
    static func bracketIfNeeded(_ host: String) -> String {
        let value = host.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.hasPrefix("["), value.hasSuffix("]") { return value }
        if isIPv6Literal(value) { return "[" + value + "]" }
        return value
    }

    /// 给界面用的展示串，如 `https://[2001:db8::1]:9443`
    static func previewURL(host: String, port: String, defaultPort: Int = 9443) -> String? {
        guard let address = normalize(host: host, port: port, defaultPort: defaultPort) else { return nil }
        return "https://\(address.urlHost):\(address.port)"
    }

    /// 去掉已成对的方括号
    private static func unwrapped(_ host: String) -> String {
        let value = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard value.hasPrefix("["), value.hasSuffix("]"), value.count > 2 else { return value }
        return String(value.dropFirst().dropLast())
    }

    private static func isValidHost(_ host: String) -> Bool {
        guard !host.isEmpty, host.count <= 255 else { return false }
        if host.contains(":") {
            // IPv6：十六进制、冒号、内嵌 IPv4 的点、作用域 %
            let allowed = CharacterSet(charactersIn: "0123456789abcdefABCDEF:.%")
            return host.unicodeScalars.allSatisfy { allowed.contains($0) }
        }
        // 域名 / IPv4：字母数字、点、连字符、下划线
        let allowed = CharacterSet(charactersIn: "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-._")
        return host.unicodeScalars.allSatisfy { allowed.contains($0) }
    }
}
