import Foundation
import Observation

// MARK: - 持久化键

enum SettingsKeys {
    static let internalHost = "nas.internalHost"
    static let publicHost = "nas.publicHost"
    /// 旧版本只存过单一地址，做一次读取兜底
    static let legacyHost = "nas.host"
    static let port = "nas.port"
    static let username = "nas.user"
    static let rememberPassword = "nas.rememberPassword"
    static let refreshInterval = "nas.refreshInterval"
    static let activeAddress = "nas.activeAddress"
    static let passwordAccount = "nas.password"
}

/// 证书指纹变化提示
struct CertificateWarning: Identifiable {
    let id = UUID()
    let host: String
    let port: Int
    let expected: String
    let actual: String
}

// MARK: - 状态容器

@Observable
final class AppModel {
    // 接入配置
    var internalHost: String
    var publicHost: String
    var port: String
    var username: String
    var password: String
    var rememberPassword: Bool
    /// 轮询间隔（秒），0 表示关闭
    var refreshInterval: Double

    // 运行状态
    var isConnecting = false
    var isLoading = false
    var errorMessage: String?
    var dashboard: Dashboard?
    var lastUpdated: Date?
    /// "内网" / "公网"
    var activeLabel: String?
    var activeDisplay: String?
    var certificateWarning: CertificateWarning?
    var notificationDenied = false

    let alerts = AlertCenter()

    private var client: UgosClient?
    private var docker: DockerService?
    private var storage: StorageService?
    private var pollTask: Task<Void, Never>?
    private var evaluator = AlertEvaluator()
    private var activeHost: String?
    private var consecutiveFailures = 0
    private var lastSwitchAttempt = Date.distantPast

    init() {
        let defaults = UserDefaults.standard
        let storedHost = defaults.string(forKey: SettingsKeys.internalHost)
            ?? defaults.string(forKey: SettingsKeys.legacyHost)
            ?? ""
        internalHost = storedHost
        publicHost = defaults.string(forKey: SettingsKeys.publicHost) ?? ""
        port = defaults.string(forKey: SettingsKeys.port) ?? "9443"
        username = defaults.string(forKey: SettingsKeys.username) ?? ""
        // 注意：init 中必须先完成所有存储属性赋值，不能读 self 上的其他属性
        let remember = defaults.bool(forKey: SettingsKeys.rememberPassword)
        rememberPassword = remember
        if defaults.object(forKey: SettingsKeys.refreshInterval) == nil {
            refreshInterval = 30
        } else {
            refreshInterval = defaults.double(forKey: SettingsKeys.refreshInterval)
        }
        password = remember
            ? (KeychainStore.load(account: SettingsKeys.passwordAccount) ?? "")
            : ""
    }

    var isReady: Bool { dashboard != nil }

    // MARK: 连接

    @MainActor
    func connect() async {
        errorMessage = nil
        certificateWarning = nil

        guard let portValue = Int(port.trimmingCharacters(in: .whitespacesAndNewlines)), portValue > 0 else {
            errorMessage = "端口不合法"
            return
        }
        let candidates = buildCandidates(port: portValue)
        guard !candidates.isEmpty else {
            errorMessage = "请至少填写内网或公网地址"
            return
        }
        guard !username.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            errorMessage = "请填写用户名"
            return
        }
        guard !password.isEmpty else {
            errorMessage = "请填写密码"
            return
        }

        isConnecting = true
        defer { isConnecting = false }

        // 内网 / 公网自动择优；都探测不到时仍然试首个地址，好让用户看到真实报错
        let candidate = await EndpointResolver.resolve(candidates) ?? candidates[0]
        let endpoint = candidate.endpoint
        let newClient = UgosClient(endpoint: endpoint)

        do {
            try await newClient.authenticate(username: username, password: password)
        } catch {
            if let warning = takeCertificateWarning(for: endpoint) {
                certificateWarning = warning
                return
            }
            errorMessage = error.localizedDescription
            return
        }

        client = newClient
        docker = DockerService(client: newClient)
        storage = StorageService(client: newClient)
        activeLabel = candidate.label
        activeDisplay = endpoint.displayName
        activeHost = endpoint.host
        consecutiveFailures = 0
        UserDefaults.standard.set(candidate.label, forKey: SettingsKeys.activeAddress)

        saveSettings()
        await loadDashboard()
        startPolling()
    }

    /// 接受新指纹后按原配置重连
    @MainActor
    func acceptNewFingerprint() async {
        guard let warning = certificateWarning else { return }
        CertificatePinStore.shared.store(warning.actual, host: warning.host, port: warning.port)
        certificateWarning = nil
        await connect()
    }

    @MainActor
    func clearCertificateWarning() {
        certificateWarning = nil
    }

    @MainActor
    func disconnect() {
        stopPolling()
        client = nil
        docker = nil
        storage = nil
        dashboard = nil
        lastUpdated = nil
        activeLabel = nil
        activeDisplay = nil
        activeHost = nil
        errorMessage = nil
        certificateWarning = nil
        consecutiveFailures = 0
        evaluator = AlertEvaluator()
    }

    // MARK: 数据刷新

    @MainActor
    func refresh() async {
        guard client != nil else { return }
        await loadDashboard()
    }

    @MainActor
    private func loadDashboard() async {
        guard let client = client else { return }

        isLoading = true
        defer { isLoading = false }

        var result = Dashboard()
        // 单个接口失败不影响其他卡片
        result.machine = try? await client.getDecodable(
            MachineInfo.self, path: "v1/sysinfo/machine/common"
        )
        result.live = try? await client.getDecodable(
            LiveOverview.self, path: "v1/taskmgr/stat/overview"
        )
        result.disks = (try? await client.getDecodable(
            DiskList.self, path: "v2/storage/disk/list"
        ))?.result ?? []
        result.pools = (try? await client.getDecodable(
            PoolList.self, path: "v1/storage/pool/list"
        ))?.result ?? []
        result.volumes = (try? await client.getDecodable(
            VolumeList.self, path: "v1/storage/volume/list"
        ))?.result ?? []
        if let docker = docker {
            result.containers = (try? await docker.containers()) ?? []
        }

        if result.machine == nil && result.live == nil {
            consecutiveFailures += 1
            errorMessage = "未能读取到设备信息，请检查网络或固件版本"
            // 连续失败时尝试换另一个地址（例如离开家里的 Wi-Fi）
            if consecutiveFailures >= 2 {
                await switchToAlternateAddress()
            }
            return
        }

        consecutiveFailures = 0
        dashboard = result
        lastUpdated = Date()
        errorMessage = nil
        processAlerts(result)
    }

    /// 内网/公网互换：只在配了两个地址、且距上次切换超过 1 分钟时执行
    @MainActor
    private func switchToAlternateAddress() async {
        guard Date().timeIntervalSince(lastSwitchAttempt) > 60 else { return }
        guard let portValue = Int(port.trimmingCharacters(in: .whitespacesAndNewlines)), portValue > 0 else { return }

        let all = buildCandidates(port: portValue)
        guard all.count > 1, let currentHost = activeHost else { return }
        guard let alternate = all.first(where: { $0.endpoint.host != currentHost }) else { return }
        guard !password.isEmpty else { return }

        lastSwitchAttempt = Date()
        consecutiveFailures = 0

        let newClient = UgosClient(endpoint: alternate.endpoint)
        do {
            try await newClient.authenticate(username: username, password: password)
            client = newClient
            docker = DockerService(client: newClient)
            storage = StorageService(client: newClient)
            activeLabel = alternate.label
            activeDisplay = alternate.endpoint.displayName
            activeHost = alternate.endpoint.host
            UserDefaults.standard.set(alternate.label, forKey: SettingsKeys.activeAddress)
            errorMessage = nil
        } catch {
            if let warning = takeCertificateWarning(for: alternate.endpoint) {
                certificateWarning = warning
            } else {
                errorMessage = error.localizedDescription
            }
        }
    }

    // MARK: 容器操作

    private enum ContainerAction {
        case start
        case stop
        case restart
    }

    @MainActor
    func startContainer(_ container: Container) async throws {
        try await performContainerAction(.start, container: container)
    }

    @MainActor
    func stopContainer(_ container: Container) async throws {
        try await performContainerAction(.stop, container: container)
    }

    @MainActor
    func restartContainer(_ container: Container) async throws {
        try await performContainerAction(.restart, container: container)
    }

    @MainActor
    private func performContainerAction(_ action: ContainerAction, container: Container) async throws {
        guard let docker = docker else { throw UgosError.auth("尚未连接") }
        guard let containerId = container.containerId, !containerId.isEmpty else {
            throw UgosError.auth("该容器缺少 containerId，无法操作")
        }

        switch action {
        case .start:
            try await docker.start(containerId)
        case .stop:
            try await docker.stop(containerId)
        case .restart:
            try await docker.restart(containerId)
        }
        await reloadContainers()
    }

    @MainActor
    func reloadContainers() async {
        guard let docker = docker else { return }
        do {
            let list = try await docker.containers()
            dashboard?.containers = list
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @MainActor
    func containerLogs(containerId: String, tail: Int) async throws -> String {
        guard let docker = docker else { throw UgosError.auth("尚未连接") }
        guard !containerId.isEmpty else { throw UgosError.auth("容器缺少 containerId") }
        return try await docker.logs(containerId: containerId, tail: tail)
    }

    /// 磁盘 SMART 明细，按需拉取（不进轮询）
    @MainActor
    func smartInfo(for disk: Disk) async throws -> SMARTInfo {
        guard let storage = storage else { throw UgosError.auth("尚未连接") }
        return try await storage.smartInfo(for: disk)
    }

    // MARK: 告警

    @MainActor
    private func processAlerts(_ dashboard: Dashboard) {
        let findings = evaluator.evaluate(dashboard: dashboard)
        guard !findings.isEmpty else { return }

        for finding in findings {
            let event = alerts.add(
                title: finding.title,
                detail: finding.detail,
                severity: finding.severity,
                ruleKey: finding.key
            )
            if let event = event {
                AlertNotifier.shared.post(event)
            }
        }
    }

    // MARK: 轮询

    @MainActor
    func startPolling() {
        stopPolling()
        guard refreshInterval > 0 else { return }

        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let interval = self.refreshInterval
                guard interval > 0 else { return }
                let nanoseconds = UInt64(interval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: nanoseconds)
                if Task.isCancelled { return }
                await self.refresh()
            }
        }
    }

    @MainActor
    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    // MARK: 设置持久化

    @MainActor
    func saveSettings() {
        let defaults = UserDefaults.standard
        defaults.set(internalHost.trimmingCharacters(in: .whitespacesAndNewlines), forKey: SettingsKeys.internalHost)
        defaults.set(publicHost.trimmingCharacters(in: .whitespacesAndNewlines), forKey: SettingsKeys.publicHost)
        defaults.set(port.trimmingCharacters(in: .whitespacesAndNewlines), forKey: SettingsKeys.port)
        defaults.set(username.trimmingCharacters(in: .whitespacesAndNewlines), forKey: SettingsKeys.username)
        defaults.set(rememberPassword, forKey: SettingsKeys.rememberPassword)
        defaults.set(refreshInterval, forKey: SettingsKeys.refreshInterval)

        if rememberPassword, !password.isEmpty {
            KeychainStore.save(password, account: SettingsKeys.passwordAccount)
        } else if !rememberPassword {
            KeychainStore.delete(account: SettingsKeys.passwordAccount)
        }
    }

    /// 刷新间隔变化：落盘并按新间隔重启轮询
    @MainActor
    func applyRefreshInterval() {
        saveSettings()
        if client != nil {
            startPolling()
        }
    }

    @MainActor
    func clearKeychain() {
        KeychainStore.deleteAll()
        password = ""
        rememberPassword = false
        UserDefaults.standard.set(false, forKey: SettingsKeys.rememberPassword)
    }

    // MARK: 地址处理

    private func buildCandidates(port: Int) -> [AddressCandidate] {
        let internalValue = normalizedHost(internalHost)
        let publicValue = normalizedHost(publicHost)

        var list: [AddressCandidate] = []
        if !internalValue.isEmpty {
            list.append(AddressCandidate(endpoint: UgosEndpoint(host: internalValue, port: port), label: "内网"))
        }
        if !publicValue.isEmpty, publicValue != internalValue {
            list.append(AddressCandidate(endpoint: UgosEndpoint(host: publicValue, port: port), label: "公网"))
        }

        // 上次连上的是公网就优先公网，减少一次无效探测
        let lastLabel = UserDefaults.standard.string(forKey: SettingsKeys.activeAddress)
        if lastLabel == "公网", list.count == 2 {
            list.reverse()
        }
        return list
    }

    /// 容忍用户粘贴 "https://host:9443/" 这类写法
    private func normalizedHost(_ raw: String) -> String {
        var host = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty else { return "" }

        if let range = host.range(of: "://") {
            host = String(host[range.upperBound...])
        }
        if let slash = host.firstIndex(of: "/") {
            host = String(host[..<slash])
        }
        if let colon = host.lastIndex(of: ":"), !host.hasSuffix("]") {
            let suffix = String(host[host.index(after: colon)...])
            if !suffix.isEmpty, suffix.allSatisfy({ $0.isNumber }) {
                host = String(host[..<colon])
            }
        }
        return host
    }

    private func takeCertificateWarning(for endpoint: UgosEndpoint) -> CertificateWarning? {
        guard let mismatch = CertificatePinStore.shared.takeMismatch(
            host: endpoint.host,
            port: endpoint.port
        ) else {
            return nil
        }
        return CertificateWarning(
            host: endpoint.host,
            port: endpoint.port,
            expected: mismatch.expected,
            actual: mismatch.actual
        )
    }
}
