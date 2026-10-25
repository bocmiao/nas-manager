import Foundation

// MARK: - 固件信息

/// 固件版本摘要。只用于提醒，不包含任何升级动作。
struct FirmwareInfo {
    var currentVersion: String?
    var publishVersion: String?
    var hasUpdate: Bool
}

/// `v1/firmware/version/is_new`
struct FirmwareVersionPayload: Decodable {
    let currentVersion: String?
    let publishVersion: String?
    let hasVersion: Bool?

    enum CodingKeys: String, CodingKey {
        case currentVersion = "current_version"
        case publishVersion = "publish_version"
        case hasVersion = "has_version"
        case hasNewVersion = "has_new_version"
        case isNew = "is_new"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        currentVersion = try? container.decode(String.self, forKey: .currentVersion)
        publishVersion = try? container.decode(String.self, forKey: .publishVersion)
        hasVersion = FirmwareVersionPayload.flag(
            container,
            keys: [.hasVersion, .hasNewVersion, .isNew]
        )
    }

    private static func flag(
        _ container: KeyedDecodingContainer<CodingKeys>,
        keys: [CodingKeys]
    ) -> Bool? {
        for key in keys {
            if let value = try? container.decode(Bool.self, forKey: key) { return value }
            if let value = try? container.decode(Int.self, forKey: key) { return value != 0 }
            if let text = try? container.decode(String.self, forKey: key) {
                switch text.lowercased() {
                case "true", "1", "yes": return true
                case "false", "0", "no": return false
                default: continue
                }
            }
        }
        return nil
    }
}

/// `v1/firmware/status`
struct FirmwareStatusPayload: Decodable {
    let currentVersion: String?
    let publishVersion: String?
    let upgradeStatus: Int?

    enum CodingKeys: String, CodingKey {
        case currentVersion = "current_version"
        case publishVersion = "publish_version"
        case upgradeStatus = "upgrade_status"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        currentVersion = try? container.decode(String.self, forKey: .currentVersion)
        publishVersion = try? container.decode(String.self, forKey: .publishVersion)
        upgradeStatus = try? container.decode(Int.self, forKey: .upgradeStatus)
    }
}

// MARK: - 服务

/// 固件版本查询（只读）。
///
/// 两个接口都是 GET 只读接口，实测见 docs/api-verified.md 第二节。
/// 任一接口成功即返回；两个都失败返回 nil，界面显示「未获取」。
final class FirmwareService {
    private let client: UgosClient

    init(client: UgosClient) {
        self.client = client
    }

    func info(fallbackVersion: String?) async -> FirmwareInfo? {
        var current = fallbackVersion
        var publish: String?
        var hasUpdate = false
        var reachable = false

        if let payload = try? await client.getDecodable(
            FirmwareVersionPayload.self,
            path: "v1/firmware/version/is_new"
        ) {
            reachable = true
            if let value = payload.currentVersion, !value.isEmpty { current = value }
            if let value = payload.publishVersion, !value.isEmpty { publish = value }
            if let flag = payload.hasVersion { hasUpdate = flag }
        }

        if let status = try? await client.getDecodable(
            FirmwareStatusPayload.self,
            path: "v1/firmware/status"
        ) {
            reachable = true
            if let value = status.currentVersion, !value.isEmpty { current = value }
            if publish == nil, let value = status.publishVersion, !value.isEmpty {
                publish = value
            }
        }

        guard reachable else { return nil }

        // 兜底判断：两个版本号都在且不一致，就是有更新
        if let current, let publish, current != publish {
            hasUpdate = true
        }
        return FirmwareInfo(currentVersion: current, publishVersion: publish, hasUpdate: hasUpdate)
    }
}
