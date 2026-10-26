import Foundation
import Observation

/// 多台 NAS 配置的持久化。
///
/// - 配置整体以 JSON 数组存 `UserDefaults`
/// - 密码仍走 Keychain，account 为 `nas.password.<profile.id>`，与配置一一对应
/// - 首次启动时把旧的单套 `SettingsKeys.*` 迁移成一条配置
@Observable
final class ProfileStore {
    private(set) var profiles: [ServerProfile] = []
    private(set) var activeID: UUID?

    private let defaults: UserDefaults
    private let profilesKey = "nas.profiles.v1"
    private let activeKey = "nas.activeProfileID"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        load()
        if profiles.isEmpty {
            migrateLegacyConfiguration()
        }
        // 选中项缺失或指向已删除的配置时兜底到第一条
        if activeID == nil || !profiles.contains(where: { $0.id == activeID }) {
            activeID = profiles.first?.id
            persistActive()
        }
    }

    // MARK: 查询

    var active: ServerProfile? {
        guard let activeID else { return nil }
        return profiles.first { $0.id == activeID }
    }

    func profile(id: UUID) -> ServerProfile? {
        profiles.first { $0.id == id }
    }

    // MARK: 增删改

    func add(_ profile: ServerProfile) {
        profiles.append(profile)
        if activeID == nil {
            activeID = profile.id
            persistActive()
        }
        persistProfiles()
    }

    @discardableResult
    func update(_ profile: ServerProfile) -> Bool {
        guard let index = profiles.firstIndex(where: { $0.id == profile.id }) else { return false }
        profiles[index] = profile
        persistProfiles()
        return true
    }

    /// 删除配置。返回 nil 表示成功，否则是给用户看的错误文案。
    /// 当前使用中的配置不允许删，必须先切走，避免连接状态与配置对不上。
    func delete(id: UUID) -> String? {
        guard profiles.contains(where: { $0.id == id }) else { return "配置不存在" }
        guard id != activeID else { return "当前使用中的配置不能删除，请先切换到其他服务器" }
        profiles.removeAll { $0.id == id }
        KeychainStore.delete(account: Self.passwordAccount(for: id))
        persistProfiles()
        return nil
    }

    func select(_ id: UUID) {
        guard profiles.contains(where: { $0.id == id }) else { return }
        activeID = id
        persistActive()
    }

    /// 清空 Keychain 后把各配置的「记住密码」标记一起复位
    func clearRememberFlags() {
        var changed = false
        for index in profiles.indices where profiles[index].rememberPassword {
            profiles[index].rememberPassword = false
            changed = true
        }
        if changed {
            persistProfiles()
        }
    }

    // MARK: 密码（按配置隔离）

    static func passwordAccount(for id: UUID) -> String {
        "nas.password.\(id.uuidString)"
    }

    func loadPassword(for id: UUID) -> String {
        KeychainStore.load(account: Self.passwordAccount(for: id)) ?? ""
    }

    func savePassword(_ password: String, for id: UUID) {
        KeychainStore.save(password, account: Self.passwordAccount(for: id))
    }

    func deletePassword(for id: UUID) {
        KeychainStore.delete(account: Self.passwordAccount(for: id))
    }

    // MARK: 持久化

    private func load() {
        if let data = defaults.data(forKey: profilesKey),
           let decoded = try? JSONDecoder().decode([ServerProfile].self, from: data) {
            profiles = decoded
        }
        if let raw = defaults.string(forKey: activeKey), let id = UUID(uuidString: raw) {
            activeID = id
        }
    }

    private func persistProfiles() {
        guard let data = try? JSONEncoder().encode(profiles) else { return }
        defaults.set(data, forKey: profilesKey)
    }

    private func persistActive() {
        if let activeID {
            defaults.set(activeID.uuidString, forKey: activeKey)
        } else {
            defaults.removeObject(forKey: activeKey)
        }
    }

    // MARK: 旧配置迁移

    /// 旧版只有一套 `SettingsKeys.*`。这里搬成一条配置，地址、端口、用户名都不丢。
    /// 调用时机必须晚于 AppModel.init 读旧键，否则 AppModel 会读到空值。
    private func migrateLegacyConfiguration() {
        let internalHost = defaults.string(forKey: SettingsKeys.internalHost)
            ?? defaults.string(forKey: SettingsKeys.legacyHost)
            ?? ""
        let publicHost = defaults.string(forKey: SettingsKeys.publicHost) ?? ""
        let storedPort = defaults.string(forKey: SettingsKeys.port) ?? ""
        let username = defaults.string(forKey: SettingsKeys.username) ?? ""
        let remember = defaults.bool(forKey: SettingsKeys.rememberPassword)

        let profile = ServerProfile(
            name: "我的 NAS",
            internalHost: internalHost,
            publicHost: publicHost,
            port: storedPort.isEmpty ? "9443" : storedPort,
            username: username,
            rememberPassword: remember
        )

        // 旧密码从共享 account 搬到该配置专属 account
        if remember, let legacy = KeychainStore.load(account: SettingsKeys.passwordAccount), !legacy.isEmpty {
            KeychainStore.save(legacy, account: Self.passwordAccount(for: profile.id))
            KeychainStore.delete(account: SettingsKeys.passwordAccount)
        }

        profiles = [profile]
        activeID = profile.id
        persistProfiles()
        persistActive()
    }
}
