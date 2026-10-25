import Foundation
import Observation

@Observable
final class AppModel {
    // 连接表单
    var host: String = UserDefaults.standard.string(forKey: "nas.host") ?? ""
    var port: String = UserDefaults.standard.string(forKey: "nas.port") ?? "9443"
    var username: String = UserDefaults.standard.string(forKey: "nas.user") ?? ""
    var password: String = ""

    // 状态
    var isConnecting = false
    var isLoading = false
    var errorMessage: String?
    var dashboard: Dashboard?
    var lastUpdated: Date?

    private var client: UgosClient?

    var isReady: Bool { dashboard != nil }

    // MARK: 连接

    func connect() async {
        errorMessage = nil
        let trimmedHost = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedHost.isEmpty, let portValue = Int(port), portValue > 0 else {
            errorMessage = "地址或端口不合法"
            return
        }
        guard !username.isEmpty, !password.isEmpty else {
            errorMessage = "请填写账号和密码"
            return
        }

        isConnecting = true
        defer { isConnecting = false }

        let endpoint = UgosEndpoint(host: trimmedHost, port: portValue)
        let newClient = UgosClient(endpoint: endpoint)
        do {
            try await newClient.authenticate(username: username, password: password)
            client = newClient

            UserDefaults.standard.set(trimmedHost, forKey: "nas.host")
            UserDefaults.standard.set(port, forKey: "nas.port")
            UserDefaults.standard.set(username, forKey: "nas.user")

            await loadDashboard(using: newClient)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func refresh() async {
        guard let client else { return }
        await loadDashboard(using: client)
    }

    func disconnect() {
        client = nil
        dashboard = nil
        password = ""
        errorMessage = nil
    }

    // MARK: 拉取数据

    private func loadDashboard(using client: UgosClient) async {
        isLoading = true
        defer { isLoading = false }

        var result = Dashboard()

        // 单个接口失败不影响其他卡片，先让界面有东西看
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
        result.containers = (try? await client.postDecodable(
            ContainerList.self,
            path: "v1/docker/container/ContainerListV2",
            json: ["PageNum": 1, "PageSize": 200]
        ))?.result ?? []

        if result.machine == nil && result.live == nil {
            errorMessage = "未能读取到设备信息，请检查网络或固件版本"
            return
        }

        dashboard = result
        lastUpdated = Date()
        errorMessage = nil
    }
}
