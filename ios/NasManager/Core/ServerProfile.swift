import Foundation

/// 一台 NAS 的接入配置。
/// 密码不进这里，按 `id` 单独存 Keychain，避免明文落 UserDefaults。
struct ServerProfile: Identifiable, Codable, Hashable {
    var id: UUID
    var name: String
    var internalHost: String
    var publicHost: String
    var port: String
    var username: String
    var rememberPassword: Bool

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case internalHost
        case publicHost
        case port
        case username
        case rememberPassword
    }

    init(
        id: UUID = UUID(),
        name: String = "",
        internalHost: String = "",
        publicHost: String = "",
        port: String = "9443",
        username: String = "",
        rememberPassword: Bool = false
    ) {
        self.id = id
        self.name = name
        self.internalHost = internalHost
        self.publicHost = publicHost
        self.port = port
        self.username = username
        self.rememberPassword = rememberPassword
    }

    /// 列表展示名：没填名称时退回地址
    var displayName: String {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return trimmed }
        let raw = internalHost.isEmpty ? publicHost : internalHost
        if !raw.isEmpty { return raw }
        return "未命名服务器"
    }

    /// 形如 `192.0.2.10:9443` 的摘要，IPv6 会自动补方括号
    var addressSummary: String {
        let raw = internalHost.isEmpty ? publicHost : internalHost
        guard !raw.isEmpty else { return "未填写地址" }
        guard let address = EndpointResolver.normalize(host: raw, port: port) else { return raw }
        return "\(address.urlHost):\(address.port)"
    }

    var isConfigured: Bool {
        !internalHost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !publicHost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}
