import SwiftUI

/// 总览：设备信息 + 实时负载 + 存储/磁盘/容器概览
struct OverviewView: View {
    let model: AppModel

    var body: some View {
        NavigationStack {
            List {
                if let dashboard = model.dashboard {
                    statusSection(dashboard)
                    loadSection(dashboard)
                    fanSection(dashboard)
                    deviceSection(dashboard)
                    storageSection(dashboard)
                    diskSection(dashboard)
                    containerSection(dashboard)
                }

                if let error = model.errorMessage {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .refreshable { await model.refresh() }
            .overlay {
                if model.isLoading && model.dashboard == nil {
                    ProgressView("读取中…")
                }
            }
        }
    }

    private var title: String {
        model.dashboard?.machine?.common?.nasName ?? "NAS"
    }

    // MARK: 区块

    @ViewBuilder
    private func statusSection(_ dashboard: Dashboard) -> some View {
        Section {
            InfoRow(title: "接入方式", value: model.activeLabel ?? "-")
            InfoRow(title: "当前地址", value: model.activeDisplay)
            InfoRow(title: "上次更新", value: Format.timestamp(model.lastUpdated))
            InfoRow(title: "刷新间隔", value: model.refreshInterval > 0 ? "\(Int(model.refreshInterval)) 秒" : "已关闭")
            InfoRow(title: "未读告警", value: "\(model.alerts.unreadCount) 条")
        } header: {
            Text("连接")
        }
    }

    @ViewBuilder
    private func loadSection(_ dashboard: Dashboard) -> some View {
        Section("负载") {
            let live = dashboard.live
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("CPU").font(.subheadline)
                    Spacer()
                    Text(Format.percent(live?.cpuUsage, digits: 1) + temperatureSuffix(live?.cpuTemp))
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                UsageBar(percent: live?.cpuUsage ?? 0)
            }
            .padding(.vertical, 2)

            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("内存").font(.subheadline)
                    Spacer()
                    Text(Format.percent(live?.memUsage, digits: 1))
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                UsageBar(percent: live?.memUsage ?? 0)
            }
            .padding(.vertical, 2)

            InfoRow(
                title: "网络",
                value: "↑ \(Format.rate(live?.sendRate))   ↓ \(Format.rate(live?.recvRate))"
            )
        }
    }

    @ViewBuilder
    private func fanSection(_ dashboard: Dashboard) -> some View {
        Section("风扇") {
            let live = dashboard.live
            InfoRow(title: "机箱", value: live?.deviceFanSpeed.map { "\($0) RPM" } ?? "-")
            InfoRow(title: "CPU", value: cpuFanText(live?.cpuFanSpeed))
        }
    }

    @ViewBuilder
    private func deviceSection(_ dashboard: Dashboard) -> some View {
        Section("设备") {
            let common = dashboard.machine?.common
            InfoRow(title: "型号", value: common?.model)
            InfoRow(title: "固件", value: common?.systemVersion)
            InfoRow(title: "已运行", value: Format.uptime(common?.runTime))

            if let cpu = dashboard.machine?.hardware?.cpu?.first {
                InfoRow(title: "CPU", value: cpu.model)
                InfoRow(
                    title: "核心",
                    value: "\(cpu.core ?? 0) 核 \(cpu.thread ?? 0) 线程 · \(cpu.ghz ?? 0) GHz"
                )
            }
            if let memory = dashboard.machine?.hardware?.mem?.first {
                let size = Format.bytes(memory.size)
                let text = [size, memory.manufacturer ?? "", memory.mhz ?? ""]
                    .filter { !$0.isEmpty && $0 != "-" }
                    .joined(separator: " · ")
                InfoRow(title: "内存", value: text)
            }

            let nets = dashboard.machine?.hardware?.net ?? []
            ForEach(0..<nets.count, id: \.self) { index in
                let nic = nets[index]
                InfoRow(
                    title: "网口 \(nic.model ?? "")",
                    value: "\(nic.ip ?? "-") · \(nic.speed ?? 0) Mbps \(nic.duplex ?? "")"
                )
            }
        }
    }

    @ViewBuilder
    private func storageSection(_ dashboard: Dashboard) -> some View {
        Section("存储池") {
            if dashboard.pools.isEmpty {
                EmptyHint(text: "未读取到存储池")
            }
            ForEach(dashboard.pools) { pool in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Text(pool.label ?? pool.name ?? "-")
                            .font(.subheadline.weight(.medium))
                        StatusBadge(text: (pool.level ?? "-").uppercased(), tint: .secondary)
                        if !pool.isRedundant {
                            StatusBadge(text: "无冗余", tint: .orange)
                        }
                        Spacer()
                    }
                    Text("\(pool.totalDiskNum ?? 0) 块盘 · \(Format.bytes(pool.total))")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    if let used = pool.used, let total = pool.total, total > 0 {
                        UsageBar(percent: Double(used) / Double(total) * 100)
                    }
                }
                .padding(.vertical, 2)
            }
        }
    }

    @ViewBuilder
    private func diskSection(_ dashboard: Dashboard) -> some View {
        Section("磁盘") {
            if dashboard.disks.isEmpty {
                EmptyHint(text: "未读取到磁盘信息")
            }
            ForEach(dashboard.disks) { disk in
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(disk.label ?? disk.name ?? "-")
                            .font(.subheadline)
                        Text("\(disk.brand ?? "") \(disk.model ?? "")")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Text("\(Format.bytes(disk.size)) · 通电 \(Format.hours(disk.powerOnHours)) · \(disk.usedFor ?? "未分配")")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(disk.temperature.map { "\($0)°C" } ?? "-")
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(temperatureTint(disk.temperature))
                }
            }
        }
    }

    @ViewBuilder
    private func containerSection(_ dashboard: Dashboard) -> some View {
        Section("容器") {
            InfoRow(title: "总数", value: "\(dashboard.containers.count) 个")
            InfoRow(title: "运行中", value: "\(dashboard.runningContainers) 个")

            let stopped = dashboard.containers.filter { !$0.isRunning }
            if stopped.isEmpty {
                EmptyHint(text: "所有容器都在运行")
            } else {
                DisclosureGroup("已停止（\(stopped.count)）") {
                    ForEach(stopped) { container in
                        HStack {
                            Text(container.name ?? "-")
                                .font(.footnote)
                            Spacer()
                            Text(container.status ?? "-")
                                .font(.caption2.monospaced())
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    // MARK: 小工具

    private func temperatureSuffix(_ value: Int?) -> String {
        guard let value else { return "" }
        return " · \(value)°C"
    }

    private func cpuFanText(_ speed: Int?) -> String {
        guard let speed, speed > 0 else { return "停转" }
        return "\(speed) RPM"
    }

    private func temperatureTint(_ value: Int?) -> Color {
        guard let value else { return .secondary }
        if value >= 50 { return .red }
        if value >= 45 { return .orange }
        return .secondary
    }
}
