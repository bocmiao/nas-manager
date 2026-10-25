import SwiftUI

// MARK: - 根视图

struct RootView: View {
    @State private var model = AppModel()

    var body: some View {
        Group {
            if model.isReady, let _ = model.dashboard {
                OverviewView(model: model)
            } else {
                ConnectView(model: model)
            }
        }
    }
}

// MARK: - 连接页

struct ConnectView: View {
    @Bindable var model: AppModel

    var body: some View {
        NavigationStack {
            Form {
                Section("NAS 地址") {
                    TextField("IP 或域名", text: $model.host)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    TextField("端口", text: $model.port)
                        .keyboardType(.numberPad)
                }

                Section("账号") {
                    TextField("用户名", text: $model.username)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("密码", text: $model.password)
                }

                if let error = model.errorMessage {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.red)
                            .font(.footnote)
                    }
                }

                Section {
                    Button {
                        Task { await model.connect() }
                    } label: {
                        HStack {
                            Spacer()
                            if model.isConnecting {
                                ProgressView().padding(.trailing, 6)
                            }
                            Text(model.isConnecting ? "连接中…" : "连接")
                            Spacer()
                        }
                    }
                    .disabled(model.isConnecting)
                } footer: {
                    Text("密码仅用于本地换取会话 token，不写入磁盘。该设备使用自签证书，连接时暂不做证书校验。")
                }
            }
            .navigationTitle("连接 NAS")
        }
    }
}

// MARK: - 总览页

struct OverviewView: View {
    let model: AppModel

    var body: some View {
        NavigationStack {
            List {
                if let d = model.dashboard {
                    deviceSection(d)
                    loadSection(d)
                    fanSection(d)
                    storageSection(d)
                    diskSection(d)
                    containerSection(d)
                }

                if let error = model.errorMessage {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.red)
                            .font(.footnote)
                    }
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .refreshable { await model.refresh() }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("断开") { model.disconnect() }
                }
            }
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

    // MARK: 各区块

    @ViewBuilder
    private func deviceSection(_ d: Dashboard) -> some View {
        Section("设备") {
            let common = d.machine?.common
            row("型号", common?.model)
            row("固件", common?.systemVersion)
            row("已运行", formatUptime(common?.runTime))
            if let cpu = d.machine?.hardware?.cpu?.first {
                row("CPU", cpu.model)
                row("核心", "\(cpu.core ?? 0) 核 \(cpu.thread ?? 0) 线程 @ \(cpu.ghz ?? 0) MHz")
            }
            if let mem = d.machine?.hardware?.mem?.first {
                row("内存", "\(formatBytes(mem.size)) \(mem.manufacturer ?? "")")
            }
            ForEach(Array((d.machine?.hardware?.net ?? []).enumerated()), id: \.offset) { _, nic in
                row("网口 \(nic.model ?? "")", "\(nic.ip ?? "-") · \(nic.speed ?? 0) Mbps \(nic.duplex ?? "")")
            }
        }
    }

    @ViewBuilder
    private func loadSection(_ d: Dashboard) -> some View {
        Section("负载") {
            let view = d.live
            gauge("CPU", value: view?.cpuUsage, suffix: "\(view?.cpuTemp.map { " · \($0)°C" } ?? "")")
            gauge("内存", value: view?.memUsage, suffix: "")
            row("网络", "↑ \(formatRate(view?.sendRate))  ↓ \(formatRate(view?.recvRate))")
        }
    }

    @ViewBuilder
    private func fanSection(_ d: Dashboard) -> some View {
        Section("风扇") {
            let view = d.live
            row("机箱", view?.deviceFanSpeed.map { "\($0) RPM" } ?? "-")
            row("CPU", (view?.cpuFanSpeed ?? 0) > 0 ? "\(view?.cpuFanSpeed ?? 0) RPM" : "停转")
        }
    }

    @ViewBuilder
    private func storageSection(_ d: Dashboard) -> some View {
        Section("存储") {
            ForEach(d.pools) { pool in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(pool.label ?? pool.name ?? "-").font(.subheadline.weight(.medium))
                        Spacer()
                        Text((pool.level ?? "").uppercased())
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if !pool.isRedundant {
                            Text("无冗余")
                                .font(.caption2)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Color.orange.opacity(0.18), in: Capsule())
                                .foregroundStyle(.orange)
                        }
                    }
                    Text("\(pool.totalDiskNum ?? 0) 块盘 · \(formatBytes(pool.total))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            ForEach(d.volumes) { volume in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(volume.label ?? volume.name ?? "-").font(.subheadline)
                        Spacer()
                        Text(String(format: "%.0f%%", volume.usedPercent))
                            .font(.subheadline.monospacedDigit())
                    }
                    ProgressView(value: min(max(volume.usedPercent / 100, 0), 1))
                        .tint(volume.usedPercent > 90 ? .red : .accentColor)
                    Text("\(formatBytes(volume.used)) / \(formatBytes(volume.total)) · \(volume.mntpath ?? "-")")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 2)
            }
        }
    }

    @ViewBuilder
    private func diskSection(_ d: Dashboard) -> some View {
        Section("磁盘") {
            if d.disks.isEmpty {
                Text("未读取到磁盘信息").foregroundStyle(.secondary).font(.footnote)
            }
            ForEach(d.disks) { disk in
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(disk.label ?? disk.name ?? "-").font(.subheadline)
                        Text("\(disk.brand ?? "") \(disk.model ?? "")")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Text("\(formatBytes(disk.size)) · 通电 \(disk.powerOnHours ?? 0) h · \(disk.usedFor ?? "未使用")")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(disk.temperature.map { "\($0)°C" } ?? "-")
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(tempColor(disk.temperature))
                }
            }
        }
    }

    @ViewBuilder
    private func containerSection(_ d: Dashboard) -> some View {
        Section("容器") {
            row("总数", "\(d.containers.count) 个")
            row("运行中", "\(d.runningContainers) 个")
            let stopped = d.containers.filter { !$0.isRunning }
            if !stopped.isEmpty {
                DisclosureGroup("已停止（\(stopped.count)）") {
                    ForEach(stopped) { c in
                        HStack {
                            Text(c.name ?? "-").font(.footnote)
                            Spacer()
                            Text(c.status ?? "-")
                                .font(.caption2.monospaced())
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    // MARK: 组件

    private func row(_ label: String, _ value: String?) -> some View {
        HStack {
            Text(label)
            Spacer()
            Text(value ?? "-")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.trailing)
        }
        .font(.subheadline)
    }

    private func gauge(_ label: String, value: Double?, suffix: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label).font(.subheadline)
                Spacer()
                Text(String(format: "%.1f%%", value ?? 0) + suffix)
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            ProgressView(value: min(max((value ?? 0) / 100, 0), 1))
        }
    }

    private func tempColor(_ temp: Int?) -> Color {
        guard let temp else { return .secondary }
        if temp >= 50 { return .red }
        if temp >= 45 { return .orange }
        return .secondary
    }
}

// MARK: - 格式化

private func formatBytes(_ value: UInt64?) -> String {
    guard let value, value > 0 else { return "-" }
    let units = ["B", "KB", "MB", "GB", "TB", "PB"]
    var v = Double(value)
    var i = 0
    while v >= 1024, i < units.count - 1 {
        v /= 1024
        i += 1
    }
    return i <= 1 ? String(format: "%.0f %@", v, units[i]) : String(format: "%.1f %@", v, units[i])
}

private func formatRate(_ value: UInt64?) -> String {
    guard let value, value > 0 else { return "0 B/s" }
    return formatBytes(value) + "/s"
}

private func formatUptime(_ seconds: Int?) -> String {
    guard let seconds, seconds > 0 else { return "-" }
    let days = seconds / 86400
    let hours = (seconds % 86400) / 3600
    let minutes = (seconds % 3600) / 60
    if days > 0 { return "\(days) 天 \(hours) 小时" }
    if hours > 0 { return "\(hours) 小时 \(minutes) 分" }
    return "\(minutes) 分"
}
