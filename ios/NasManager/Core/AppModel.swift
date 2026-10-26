import Foundation
import Observation
import WidgetKit

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
    /// 启动时是否要求 Face ID / Touch ID
    static let biometricEnabled = "nas.biometricEnabled"
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

    /// 多台 NAS 配置
    let profiles: ProfileStore
    /// 启动时校验身份
    var biometricEnabled: Bool
    /// 已上锁：界面只显示解锁页
    var isLocked = false
    /// 解锁页上的提示（降级原因 / 重试原因）
    var lockMessage: String?
    /// 服务器配置操作的结果提示
    var profileNotice: String?

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
    /// 固件版本提醒（只读，不做任何升级动作）
    var firmware: FirmwareInfo?
    var isCheckingFirmware = false

    let alerts = AlertCenter()
    /// 本地历史采样，供总览页曲线使用
    let metrics = MetricsHistory()

    private var client: UgosClient?
    private var docker: DockerService?
    private var storage: StorageService?
    private var imageService: ImageService?
    private var snapshotService: SnapshotService?
    private var fileService: FileService?
    private var firmwareService: FirmwareService?
    private var logService: LogService?
    private var sessionService: SessionService?
    private var fanService: FanService?
    private var backupService: BackupService?
    private var powerPlanService: PowerPlanService?
    private var pollTask: Task<Void, Never>?
    private var evaluator = AlertEvaluator()
    private var activeHost: String?
    private var consecutiveFailures = 0
    private var lastSwitchAttempt = Date.distantPast
    private var lastFirmwareCheck = Date.distantPast
    private var biometricFailures = 0
    /// 上次通过验证的时间，用于避免频繁切前后台就被反复要求验证
    private var lastUnlockAt = Date.distantPast

    /// 最近一次拉到的可快照文件夹，创建快照时作为目标候选
    var snapshotFolders: [SnapshotFolder] = []

    /// 部分数据源加载失败的说明。空数组表示全部成功，界面据此区分「真的没有」和「没读到」
    var partialErrors: [String] = []

    /// 当前生效的 NAS 主机地址。
    ///
    /// 给「在应用内打开容器 Web UI」用：容器端口是宿主机端口，所以访问地址必须是
    /// **当前正在连的那台 NAS**。内网连的就是内网地址，切到公网就跟着变成公网地址，
    /// 不需要单独再配一份。
    var connectedHost: String? { activeHost }

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

        // ProfileStore 内部会做旧配置迁移，必须放在上面读完旧键之后：
        // 迁移会把 SettingsKeys 里的地址搬进 profile，并清掉共享的旧密码 account。
        let store = ProfileStore()
        profiles = store
        biometricEnabled = defaults.bool(forKey: SettingsKeys.biometricEnabled)

        // 有选中配置时以配置为准覆盖上面读到的旧值
        if let active = store.active {
            internalHost = active.internalHost
            publicHost = active.publicHost
            port = active.port
            username = active.username
            rememberPassword = active.rememberPassword
            password = active.rememberPassword ? store.loadPassword(for: active.id) : ""
        }

        // 冷启动就上锁，避免先闪一帧连接页；没有保存密码则照常走手输流程
        isLocked = biometricEnabled && rememberPassword && !password.isEmpty
    }

    var isReady: Bool { dashboard != nil }

    // MARK: 连接

    @MainActor
    func connect() async {
        // 上锁状态不允许建连，必须先通过身份验证
        guard !isLocked else { return }

        errorMessage = nil
        certificateWarning = nil

        guard let portValue = Int(port.trimmingCharacters(in: .whitespacesAndNewlines)), portValue > 0 else {
            errorMessage = "端口不合法"
            return
        }
        // 把粘贴进来的 URL / 带方括号的 IPv6 整理成规范主机
        normalizeAddressFields(defaultPort: portValue)
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
        imageService = ImageService(client: newClient)
        snapshotService = SnapshotService(client: newClient)
        fileService = FileService(client: newClient, endpoint: endpoint)
        firmwareService = FirmwareService(client: newClient)
        logService = LogService(client: newClient)
        sessionService = SessionService(client: newClient)
        fanService = FanService(client: newClient)
        backupService = BackupService(client: newClient)
        powerPlanService = PowerPlanService(client: newClient)
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
        imageService = nil
        snapshotService = nil
        fileService = nil
        firmwareService = nil
        logService = nil
        sessionService = nil
        fanService = nil
        backupService = nil
        powerPlanService = nil
        dashboard = nil
        lastUpdated = nil
        // 切服务器后必须清空，否则会拿上一台 NAS 的 folder_id 去建快照
        snapshotFolders = []
        partialErrors = []
        activeLabel = nil
        activeDisplay = nil
        activeHost = nil
        errorMessage = nil
        certificateWarning = nil
        firmware = nil
        lastFirmwareCheck = Date.distantPast
        consecutiveFailures = 0
        evaluator = AlertEvaluator()

        // 断开后清空快照，Widget 回到占位态而不是继续展示旧设备的数字
        WidgetBridge.clear()
        WidgetCenter.shared.reloadAllTimelines()
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
        var failures: [String] = []

        // 单个接口失败不影响其他卡片，但失败原因要记下来：
        // 空数组只能代表「真的没有」，不能替「没读到」背锅
        do {
            result.machine = try await client.getDecodable(
                MachineInfo.self, path: "v1/sysinfo/machine/common"
            )
        } catch {
            result.machine = nil
            failures.append("设备信息加载失败")
        }
        do {
            result.live = try await client.getDecodable(
                LiveOverview.self, path: "v1/taskmgr/stat/overview"
            )
        } catch {
            result.live = nil
            failures.append("实时负载加载失败")
        }
        do {
            result.disks = (try await client.getDecodable(
                DiskList.self, path: "v2/storage/disk/list"
            )).result ?? []
        } catch {
            result.disks = []
            failures.append("磁盘列表加载失败")
        }
        do {
            result.pools = (try await client.getDecodable(
                PoolList.self, path: "v1/storage/pool/list"
            )).result ?? []
        } catch {
            result.pools = []
            failures.append("存储池列表加载失败")
        }
        do {
            result.volumes = (try await client.getDecodable(
                VolumeList.self, path: "v1/storage/volume/list"
            )).result ?? []
        } catch {
            result.volumes = []
            failures.append("卷列表加载失败")
        }
        if let docker = docker {
            do {
                result.containers = try await docker.containers()
            } catch {
                result.containers = []
                failures.append("容器列表加载失败")
            }
        }

        if result.machine == nil && result.live == nil {
            consecutiveFailures += 1
            errorMessage = "未能读取到设备信息，请检查网络或固件版本"
            partialErrors = []
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
        partialErrors = failures
        if let live = result.live {
            metrics.record(live: live)
        }
        processAlerts(result)
        publishWidgetSnapshot(result, updatedAt: lastUpdated ?? Date())
        await loadFirmwareIfNeeded()
    }

    /// 组装主屏小组件快照。取不到的字段留 nil，Widget 侧显示「—」，不用 0 冒充。
    @MainActor
    private func publishWidgetSnapshot(_ dashboard: Dashboard, updatedAt: Date) {
        let live = dashboard.live

        // 内存总量由各条内存条容量相加，已用量按占比推算
        let memoryTotal = (dashboard.machine?.hardware?.mem ?? []).compactMap(\.size).reduce(0, +)
        let memPercent = live?.memUsage
        var memoryUsed: UInt64?
        if memoryTotal > 0, let percent = memPercent {
            memoryUsed = UInt64(Double(memoryTotal) * min(max(percent, 0), 100) / 100)
        }

        // 多个存储池合并成一个总占用率
        let poolTotal = dashboard.pools.compactMap(\.total).reduce(0, +)
        let poolUsed = dashboard.pools.compactMap(\.used).reduce(0, +)
        let poolPercent = poolTotal > 0 ? Double(poolUsed) / Double(poolTotal) * 100 : nil

        // 容器列表拉取失败时也是空数组，此时不写 0，避免显示「0 个容器」
        let hasContainers = !dashboard.containers.isEmpty

        let snapshot = WidgetSnapshot(
            cpuPercent: live?.cpuUsage,
            memPercent: memPercent,
            memUsed: memoryUsed,
            memTotal: memoryTotal > 0 ? memoryTotal : nil,
            cpuTemp: live?.cpuTemp,
            fanRpm: live?.cpuFanSpeed ?? live?.deviceFanSpeed,
            poolUsedPercent: poolPercent,
            runningContainers: hasContainers ? dashboard.runningContainers : nil,
            totalContainers: hasContainers ? dashboard.containers.count : nil,
            alertCount: alerts.unreadCount,
            updatedAt: updatedAt,
            machineName: dashboard.machine?.common?.nasName
        )

        WidgetBridge.write(snapshot)
        WidgetCenter.shared.reloadAllTimelines()
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
            imageService = ImageService(client: newClient)
            snapshotService = SnapshotService(client: newClient)
            fileService = FileService(client: newClient, endpoint: alternate.endpoint)
            firmwareService = FirmwareService(client: newClient)
            logService = LogService(client: newClient)
            sessionService = SessionService(client: newClient)
            fanService = FanService(client: newClient)
            backupService = BackupService(client: newClient)
            powerPlanService = PowerPlanService(client: newClient)
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

    // MARK: 镜像

    @MainActor
    func images() async throws -> [DockerImage] {
        guard let imageService = imageService else { throw UgosError.auth("尚未连接") }
        return try await imageService.list()
    }

    @MainActor
    func deleteImage(_ image: DockerImage) async throws {
        guard let imageService = imageService else { throw UgosError.auth("尚未连接") }
        try await imageService.delete(image)
    }

    @MainActor
    func pullImage(_ reference: String) async throws {
        guard let imageService = imageService else { throw UgosError.auth("尚未连接") }
        try await imageService.pull(reference)
    }

    // MARK: 快照

    /// 快照接口是否可用由服务层探测决定，这里原样透传结果
    @MainActor
    func snapshots() async -> SnapshotQueryResult {
        guard let snapshotService = snapshotService else {
            return SnapshotQueryResult(items: [], folders: [], available: false, message: "尚未连接", endpoint: nil)
        }
        let result = await snapshotService.list()
        snapshotFolders = result.folders
        return result
    }

    @MainActor
    func createSnapshot(folder: SnapshotFolder, remark: String) async throws {
        guard let snapshotService = snapshotService else { throw UgosError.auth("尚未连接") }
        try await snapshotService.create(folder: folder, comment: remark)
    }

    @MainActor
    func deleteSnapshot(_ item: SnapshotItem) async throws {
        guard let snapshotService = snapshotService else { throw UgosError.auth("尚未连接") }
        try await snapshotService.delete(item)
    }

    /// 回滚到指定快照，覆盖当前数据且不可撤销
    @MainActor
    func rollbackSnapshot(_ item: SnapshotItem) async throws {
        guard let snapshotService = snapshotService else { throw UgosError.auth("尚未连接") }
        try await snapshotService.restore(item)
    }

    // MARK: 文件管理

    /// 根分类 + 卷容量。错误文案统一走 FileService.friendly，把 1301 之类翻成人话。
    @MainActor
    func fileRoots() async throws -> FileRoots {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.roots()
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    @MainActor
    func fileList(
        path: String?,
        rootType: Int?,
        page: Int = 1,
        limit: Int = 200,
        sort: FileSort = .name,
        reverse: Bool = false
    ) async throws -> FileListing {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.list(
                path: path,
                rootType: rootType,
                page: page,
                limit: limit,
                sort: sort,
                reverse: reverse
            )
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    @MainActor
    func createFolder(parent: String, name: String) async throws -> FileEntry? {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.createFolder(parent: parent, name: name)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    @MainActor
    func renameFile(path: String, newName: String) async throws -> FileEntry? {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.rename(path: path, newName: newName)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    /// 删除文件。默认进回收站，forever 为 true 才直接删。
    @MainActor
    func deleteFiles(paths: [String], forever: Bool = false) async throws -> FileDeleteResult {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.delete(paths: paths, forever: forever)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    /// 复制 / 移动。返回后任务还在后台跑，要等落地调 `fileWaitTransfer`。
    @MainActor
    func fileTransfer(paths: [String], to destination: String, move: Bool) async throws -> FileTransferResult {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.copyOrMove(paths: paths, to: destination, move: move)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    @MainActor
    func fileWaitTransfer(_ result: FileTransferResult) async throws {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            try await fileService.waitForTransfer(result)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    /// 压缩。返回后任务还在后台跑，要等落地调 `fileWaitArchive`。
    @MainActor
    func fileCompress(paths: [String], name: String) async throws -> FileArchiveResult {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.compress(paths: paths, name: name)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    /// 压缩包要不要密码
    @MainActor
    func fileArchiveEncrypted(path: String) async throws -> Bool {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.isEncryptedArchive(path: path)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    /// 解压到同名目录
    @MainActor
    func fileExtract(path: String, password: String?) async throws -> FileArchiveResult {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.extract(path: path, password: password)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    @MainActor
    func fileWaitArchive(_ result: FileArchiveResult) async throws {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            try await fileService.waitForArchive(result)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    @MainActor
    func downloadFile(path: String) async throws -> Data {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.download(path: path)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    /// 图片缩略图（列表小图标用）。失败一律抛出，由视图层吞掉退回系统图标。
    @MainActor
    func fileThumbnail(entry: FileEntry) async throws -> Data {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        return try await fileService.thumbnail(for: entry)
    }

    /// 原图字节（大图预览用）。走 `mediaPlayer`，不用三步下载链路。
    @MainActor
    func filePreview(entry: FileEntry) async throws -> Data {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.previewBytes(for: entry)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    @MainActor
    func fileFreeSpace(path: String) async throws -> FileSpace {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.freeSpace(path: path)
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    /// 上传文件（分块追加）。默认 `actionType: 3` = 同名自动改名，不覆盖已有文件。
    @MainActor
    func uploadFile(
        data: Data,
        filename: String,
        destPath: String,
        actionType: Int = 3
    ) async throws -> FileUploadResult {
        guard let fileService = fileService else { throw UgosError.auth("尚未连接") }
        do {
            return try await fileService.upload(
                fileData: data,
                filename: filename,
                destPath: destPath,
                actionType: actionType
            )
        } catch {
            throw UgosError.auth(FileService.friendly(error))
        }
    }

    // MARK: 运维工具（只读）

    @MainActor
    func logModules() async -> [String] {
        guard let logService = logService else { return [] }
        return await logService.modules()
    }

    @MainActor
    func logs(
        page: Int,
        levels: [String],
        module: String?,
        keyword: String
    ) async -> LogQueryResult {
        guard let logService = logService else {
            return LogQueryResult(entries: [], total: nil, available: false, message: "尚未连接")
        }
        return await logService.query(page: page, levels: levels, module: module, keyword: keyword)
    }

    @MainActor
    func onlineSessions() async -> SessionQueryResult {
        guard let sessionService = sessionService else {
            return SessionQueryResult(sessions: [], total: nil, available: false, message: "尚未连接")
        }
        return await sessionService.sessions()
    }

    @MainActor
    func loginAudit() async -> LoginAuditResult {
        guard let sessionService = sessionService else {
            return LoginAuditResult(records: [], total: nil, available: false, message: "尚未连接")
        }
        return await sessionService.loginAudit()
    }

    @MainActor
    func fanSnapshot() async -> FanSnapshot {
        guard let fanService = fanService else {
            return FanSnapshot(message: "尚未连接，请先连接 NAS")
        }
        return await fanService.snapshot()
    }

    @MainActor
    func backupTasks() async -> BackupQueryResult {
        guard let backupService = backupService else {
            return BackupQueryResult(available: false, message: "尚未连接，请先连接 NAS")
        }
        return await backupService.tasks()
    }

    @MainActor
    func powerPlan() async -> PowerPlanResult {
        guard let powerPlanService = powerPlanService else {
            return PowerPlanResult(available: false, message: "尚未连接，请先连接 NAS")
        }
        return await powerPlanService.plan()
    }

    // MARK: 固件

    @MainActor
    func refreshFirmware() async {
        guard let firmwareService = firmwareService else { return }
        isCheckingFirmware = true
        defer { isCheckingFirmware = false }
        let fallback = dashboard?.machine?.common?.systemVersion
        firmware = await firmwareService.info(fallbackVersion: fallback)
        lastFirmwareCheck = Date()
    }

    /// 轮询里最多 10 分钟查一次，避免每 30 秒打一次固件接口
    @MainActor
    private func loadFirmwareIfNeeded() async {
        guard Date().timeIntervalSince(lastFirmwareCheck) >= 600 else { return }
        await refreshFirmware()
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
        // 先同步进当前配置，再写一份旧键做兜底（万一配置 JSON 损坏还能靠它恢复）
        syncActiveProfile()

        let defaults = UserDefaults.standard
        defaults.set(internalHost.trimmingCharacters(in: .whitespacesAndNewlines), forKey: SettingsKeys.internalHost)
        defaults.set(publicHost.trimmingCharacters(in: .whitespacesAndNewlines), forKey: SettingsKeys.publicHost)
        defaults.set(port.trimmingCharacters(in: .whitespacesAndNewlines), forKey: SettingsKeys.port)
        defaults.set(username.trimmingCharacters(in: .whitespacesAndNewlines), forKey: SettingsKeys.username)
        defaults.set(rememberPassword, forKey: SettingsKeys.rememberPassword)
        defaults.set(refreshInterval, forKey: SettingsKeys.refreshInterval)
    }

    /// 把编辑中的地址 / 账号写回当前配置，密码按配置 id 存 Keychain
    @MainActor
    private func syncActiveProfile() {
        guard var active = profiles.active else { return }
        active.internalHost = internalHost.trimmingCharacters(in: .whitespacesAndNewlines)
        active.publicHost = publicHost.trimmingCharacters(in: .whitespacesAndNewlines)
        active.port = port.trimmingCharacters(in: .whitespacesAndNewlines)
        active.username = username.trimmingCharacters(in: .whitespacesAndNewlines)
        active.rememberPassword = rememberPassword
        profiles.update(active)

        if rememberPassword, !password.isEmpty {
            profiles.savePassword(password, for: active.id)
        } else if !rememberPassword {
            profiles.deletePassword(for: active.id)
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
        profiles.clearRememberFlags()
        UserDefaults.standard.set(false, forKey: SettingsKeys.rememberPassword)
    }

    // MARK: 多配置

    var serverProfiles: [ServerProfile] { profiles.profiles }
    var activeProfileID: UUID? { profiles.activeID }

    /// 用当前配置覆盖编辑字段与密码
    @MainActor
    func applyActiveProfile() {
        guard let active = profiles.active else { return }
        internalHost = active.internalHost
        publicHost = active.publicHost
        port = active.port
        username = active.username
        rememberPassword = active.rememberPassword
        password = active.rememberPassword ? profiles.loadPassword(for: active.id) : ""
    }

    /// 切换配置：先断开旧连接，再用新配置重建 client 并重连
    @MainActor
    func switchProfile(to id: UUID) async {
        guard id != profiles.activeID, profiles.profile(id: id) != nil else { return }
        profileNotice = nil
        disconnect()
        profiles.select(id)
        applyActiveProfile()
        await connect()
    }

    /// 新增或更新一条配置；改的是当前配置时同步回编辑字段
    @MainActor
    func saveProfile(_ profile: ServerProfile) {
        if profiles.profile(id: profile.id) != nil {
            profiles.update(profile)
        } else {
            profiles.add(profile)
        }
        if profile.id == profiles.activeID {
            applyActiveProfile()
        }
        profileNotice = nil
    }

    /// 删除配置。当前使用中的那条会被拒绝并给出提示。
    @MainActor
    @discardableResult
    func deleteProfile(id: UUID) -> Bool {
        if let message = profiles.delete(id: id) {
            profileNotice = message
            return false
        }
        profileNotice = nil
        return true
    }

    // MARK: 生物识别解锁

    /// 是否具备「启动验证」的条件：开关已开、且确实记住了密码
    var canLockWithBiometrics: Bool {
        biometricEnabled && rememberPassword && !password.isEmpty
    }

    @MainActor
    func setBiometricEnabled(_ enabled: Bool) {
        guard enabled == false || BiometricLock.isAvailable() else {
            biometricEnabled = false
            return
        }
        biometricEnabled = enabled
        UserDefaults.standard.set(enabled, forKey: SettingsKeys.biometricEnabled)
        if !enabled {
            isLocked = false
            lockMessage = nil
        }
    }

    /// 冷启动与回前台时按需上锁。没有保存密码时保持原有手输流程，避免把用户关在门外。
    /// 只读写自身状态，调用方都在主线程（视图的 task / onChange），因此不标 @MainActor。
    func lockIfNeeded() {
        guard canLockWithBiometrics else { return }
        // 刚验证过 30 秒内不算重新进入，避免下拉一下通知中心就要再验一次
        guard Date().timeIntervalSince(lastUnlockAt) > 30 else { return }
        isLocked = true
        lockMessage = nil
        biometricFailures = 0
    }

    @MainActor
    func unlockWithBiometrics() async {
        guard isLocked else { return }
        let result = await BiometricLock.authenticate(reason: "验证身份后连接 NAS")

        switch result {
        case .success:
            isLocked = false
            lockMessage = nil
            biometricFailures = 0
            lastUnlockAt = Date()
            if !isReady {
                await connect()
            }
        case .cancelled:
            // 用户取消：停留解锁页，不降级
            lockMessage = nil
        case .unavailable(let message):
            fallbackToPassword(message: message)
        case .failed(let message):
            biometricFailures += 1
            if biometricFailures >= 2 {
                fallbackToPassword(message: "多次验证未通过，请改用密码")
            } else {
                lockMessage = message
            }
        }
    }

    /// 降级为手输密码：解除锁定、清空内存中的密码，让用户重新输入
    @MainActor
    func fallbackToPassword(message: String? = nil) {
        isLocked = false
        lockMessage = nil
        biometricFailures = 0
        password = ""
        errorMessage = message
    }

    // MARK: 地址处理

    /// 连接前把输入框整理成规范主机（IPv6 去掉方括号、去掉协议与路径），
    /// 顺带把结果显示回输入框，用户能看到最终用的是哪个地址。
    @MainActor
    private func normalizeAddressFields(defaultPort: Int) {
        if let value = EndpointResolver.normalize(host: internalHost, port: port, defaultPort: defaultPort) {
            internalHost = value.host
        }
        if let value = EndpointResolver.normalize(host: publicHost, port: port, defaultPort: defaultPort) {
            publicHost = value.host
        }
    }

    private func buildCandidates(port portValue: Int) -> [AddressCandidate] {
        var list: [AddressCandidate] = []
        var seen: Set<String> = []

        for (raw, label) in [(internalHost, "内网"), (publicHost, "公网")] {
            guard let address = EndpointResolver.normalize(
                host: raw,
                port: String(portValue),
                defaultPort: portValue
            ) else { continue }

            // 内外网填成同一个地址时只探测一次
            let key = "\(address.host.lowercased()):\(address.port)"
            guard !seen.contains(key) else { continue }
            seen.insert(key)

            list.append(
                AddressCandidate(
                    endpoint: UgosEndpoint(host: address.host, port: address.port),
                    label: label
                )
            )
        }

        // 上次连上的是公网就优先公网，减少一次无效探测
        let lastLabel = UserDefaults.standard.string(forKey: SettingsKeys.activeAddress)
        if lastLabel == "公网", list.count == 2 {
            list.reverse()
        }
        return list
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
