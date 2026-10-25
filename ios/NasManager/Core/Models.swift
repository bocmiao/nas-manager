import Foundation

// MARK: - 系统信息 `v1/sysinfo/machine/common`

struct MachineInfo: Decodable {
    struct Common: Decodable {
        let nasName: String?
        let model: String?
        let serial: String?
        let systemVersion: String?
        let runTime: Int?
        let lastTurnOnTimestamp: Int?

        enum CodingKeys: String, CodingKey {
            case nasName = "nas_name"
            case model
            case serial
            case systemVersion = "system_version"
            case runTime = "run_time"
            case lastTurnOnTimestamp = "last_turn_on_timestamp"
        }
    }

    struct Hardware: Decodable {
        struct CPU: Decodable {
            let model: String?
            let core: Int?
            let thread: Int?
            let ghz: Int?
            let temperature: Int?
        }

        struct Memory: Decodable {
            let manufacturer: String?
            let size: UInt64?
            let mhz: String?
        }

        struct Net: Decodable {
            let model: String?
            let ip: String?
            let speed: Int?
            let duplex: String?
        }

        /// UPS。未接 UPS 时后端给 null，这里就整体为 nil，界面整块隐藏。
        /// 字段名未逐项实测，按常见写法宽松抽取。
        struct UPS: Decodable {
            let model: String?
            let status: Int?
            /// 电量百分比
            let battery: Int?
            /// 剩余续航原始值，单位随固件（见 runtimeMinutes）
            let runtime: Int?
            let onBattery: Bool?

            /// 剩余续航统一换算成分钟：多数固件给秒，小数值当分钟理解
            var runtimeMinutes: Int? {
                guard let runtime, runtime > 0 else { return nil }
                return runtime >= 600 ? runtime / 60 : runtime
            }

            var batteryPercent: Int? {
                guard let battery else { return nil }
                return min(max(battery, 0), 100)
            }

            enum CodingKeys: String, CodingKey {
                case model
                case status
                case battery
                case batteryLevel = "battery_level"
                case batteryPercentKey = "battery_percent"
                case charge
                case runtime
                case remainTime = "remain_time"
                case remainRuntime = "remain_runtime"
                case batteryRuntime = "battery_runtime"
                case onBattery = "on_battery"
                case usingBattery = "using_battery"
                case powerSupply = "power_supply"
            }

            init(from decoder: Decoder) throws {
                let container = try decoder.container(keyedBy: CodingKeys.self)
                model = try? container.decode(String.self, forKey: .model)
                status = UPS.int(container, keys: [.status])
                battery = UPS.int(container, keys: [.battery, .batteryLevel, .batteryPercentKey, .charge])
                runtime = UPS.int(container, keys: [.runtime, .remainTime, .remainRuntime, .batteryRuntime])
                onBattery = UPS.bool(container, keys: [.onBattery, .usingBattery, .powerSupply])
            }

            private static func int(
                _ container: KeyedDecodingContainer<CodingKeys>,
                keys: [CodingKeys]
            ) -> Int? {
                for key in keys {
                    if let value = try? container.decode(Int.self, forKey: key) { return value }
                    if let value = try? container.decode(Double.self, forKey: key) { return Int(value) }
                    if let text = try? container.decode(String.self, forKey: key) {
                        let digits = text.prefix { $0.isNumber || $0 == "-" }
                        if let value = Int(digits) { return value }
                    }
                }
                return nil
            }

            private static func bool(
                _ container: KeyedDecodingContainer<CodingKeys>,
                keys: [CodingKeys]
            ) -> Bool? {
                for key in keys {
                    if let value = try? container.decode(Bool.self, forKey: key) { return value }
                    if let value = try? container.decode(Int.self, forKey: key) { return value != 0 }
                    if let text = try? container.decode(String.self, forKey: key) {
                        switch text.lowercased() {
                        case "true", "1", "yes", "battery", "on": return true
                        case "false", "0", "no", "ac", "line", "off": return false
                        default: continue
                        }
                    }
                }
                return nil
            }
        }

        let cpu: [CPU]?
        let mem: [Memory]?
        let net: [Net]?
        let ups: UPS?
    }

    let common: Common?
    let hardware: Hardware?
}

// MARK: - 实时监控 `v1/taskmgr/stat/overview`

struct LiveOverview: Decodable {
    struct CPUSample: Decodable {
        let usedPercent: Double?
        let temp: Int?
        enum CodingKeys: String, CodingKey {
            case usedPercent = "used_percent"
            case temp
        }
    }

    struct MemSample: Decodable {
        let usedPercent: Double?
        enum CodingKeys: String, CodingKey {
            case usedPercent = "used_percent"
        }
    }

    struct NetSample: Decodable {
        let sendRate: UInt64?
        let recvRate: UInt64?
        enum CodingKeys: String, CodingKey {
            case sendRate = "send_rate"
            case recvRate = "recv_rate"
        }
    }

    struct FanSample: Decodable {
        let speed: Int?
        let status: Int?
    }

    let cpu: [CPUSample]?
    let mem: [MemSample]?
    let net: [NetSample]?
    let cpuFan: [FanSample]?
    let deviceFan: [FanSample]?

    enum CodingKeys: String, CodingKey {
        case cpu, mem, net
        case cpuFan = "cpu_fan"
        case deviceFan = "device_fan"
    }

    var cpuUsage: Double? { cpu?.first?.usedPercent }
    var cpuTemp: Int? { cpu?.first?.temp }
    var memUsage: Double? { mem?.first?.usedPercent }
    var sendRate: UInt64? { net?.first?.sendRate }
    var recvRate: UInt64? { net?.first?.recvRate }
    var cpuFanSpeed: Int? { cpuFan?.first?.speed }
    var deviceFanSpeed: Int? { deviceFan?.first?.speed }
}

// MARK: - 磁盘 `v2/storage/disk/list`

struct DiskList: Decodable {
    let result: [Disk]?
}

struct Disk: Decodable, Identifiable {
    let model: String?
    let label: String?
    let name: String?
    let brand: String?
    let size: UInt64?
    let temperature: Int?
    let powerOnHours: Int?
    let status: Int?
    let usedFor: String?

    var id: String { name ?? label ?? "-" }

    /// 实测 status 0 / 1 均为正常（1 = 已激活在用）
    var isHealthy: Bool { status == 0 || status == 1 }

    enum CodingKeys: String, CodingKey {
        case model, label, name, brand, size, temperature, status
        case powerOnHours = "power_on_hours"
        case usedFor = "used_for"
    }
}

// MARK: - 存储池 `v1/storage/pool/list`

struct PoolList: Decodable {
    let result: [Pool]?
}

struct Pool: Decodable, Identifiable {
    let name: String?
    let label: String?
    let level: String?
    let status: Int?
    let total: UInt64?
    let used: UInt64?
    let totalDiskNum: Int?

    var id: String { name ?? label ?? "-" }
    var isHealthy: Bool { status == 0 }
    var isRedundant: Bool {
        ["raid1", "raid5", "raid6", "raid10", "raidz1", "raidz2"].contains((level ?? "").lowercased())
    }

    enum CodingKeys: String, CodingKey {
        case name, label, level, status, total, used
        case totalDiskNum = "total_disk_num"
    }
}

// MARK: - 存储卷 `v1/storage/volume/list`

struct VolumeList: Decodable {
    let result: [Volume]?
}

struct Volume: Decodable, Identifiable {
    let name: String?
    let label: String?
    let filesystem: String?
    let total: UInt64?
    let used: UInt64?
    let available: UInt64?
    let mntpath: String?
    let health: Int?

    var id: String { name ?? label ?? "-" }

    var usedPercent: Double {
        guard let total, total > 0, let used else { return 0 }
        return Double(used) / Double(total) * 100
    }
}

// MARK: - 容器 `v1/docker/container/ContainerListV2`

struct ContainerList: Decodable {
    let total: Int?
    let result: [Container]?
}

struct Container: Decodable, Identifiable {
    let name: String?
    let containerId: String?
    let imageName: String?
    let status: String?
    let projectName: String?
    /// 列表接口部分固件会带资源占用；字段类型不稳定，用宽松解析，取不到就显示 “-”
    let cpuPercent: Double?
    let memPercent: Double?

    var id: String { containerId ?? name ?? "-" }
    var isRunning: Bool { (status ?? "").lowercased() == "running" }

    enum CodingKeys: String, CodingKey {
        case name, status
        case containerId, imageName, projectName
        case cpuPercent = "cpu_percent"
        case memPercent = "mem_percent"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decodeIfPresent(String.self, forKey: .name)
        containerId = try container.decodeIfPresent(String.self, forKey: .containerId)
        imageName = try container.decodeIfPresent(String.self, forKey: .imageName)
        status = try container.decodeIfPresent(String.self, forKey: .status)
        projectName = try container.decodeIfPresent(String.self, forKey: .projectName)
        cpuPercent = Container.flexibleDouble(container, key: .cpuPercent)
        memPercent = Container.flexibleDouble(container, key: .memPercent)
    }

    /// 数字可能是 3 / 3.2 / "3.2"，三种都要能吃下
    private static func flexibleDouble(
        _ container: KeyedDecodingContainer<CodingKeys>,
        key: CodingKeys
    ) -> Double? {
        if let value = try? container.decodeIfPresent(Double.self, forKey: key) {
            return value
        }
        if let text = try? container.decodeIfPresent(String.self, forKey: key) {
            return Double(text)
        }
        return nil
    }
}

// MARK: - 聚合

struct Dashboard {
    var machine: MachineInfo?
    var live: LiveOverview?
    var disks: [Disk] = []
    var pools: [Pool] = []
    var volumes: [Volume] = []
    var containers: [Container] = []

    var runningContainers: Int { containers.filter(\.isRunning).count }
}
