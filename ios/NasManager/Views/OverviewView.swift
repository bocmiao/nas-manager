import Charts
import Foundation
import SwiftUI

/// 总览：设备信息 + 实时负载 + 历史曲线 + 存储/磁盘/容器概览
struct OverviewView: View {
    let model: AppModel

    /// 已关闭提示条的固件版本，避免每次刷新都弹回来
    @AppStorage("firmware.dismissedVersion") private var dismissedFirmwareVersion = ""
    /// 曲线时间范围（采样点数）
    @State private var historyRange = 120

    var body: some View {
        NavigationStack {
            List {
                if let firmware = model.firmware, firmware.hasUpdate,
                   let latest = firmware.publishVersion,
                   dismissedFirmwareVersion != latest {
                    firmwareBannerSection(firmware)
                }

                if let dashboard = model.dashboard {
                    statusSection(dashboard)
                    loadSection(dashboard)
                    historySection
                    fanSection(dashboard)
                    upsSection(dashboard)
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

    /// 固件更新提示条，只提醒不做升级
    @ViewBuilder
    private func firmwareBannerSection(_ firmware: FirmwareInfo) -> some View {
        Section {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "arrow.down.circle.fill")
                    .foregroundStyle(.orange)
                VStack(alignment: .leading, spacing: 3) {
                    Text("固件有新版本")
                        .font(.subheadline.weight(.medium))
                    Text("当前 \(firmware.currentVersion ?? "-") · 最新 \(firmware.publishVersion ?? "-")，请到 UGOS 网页端升级")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                Button {
                    dismissedFirmwareVersion = firmware.publishVersion ?? "-"
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.tertiary)
                }
                .buttonStyle(.plain)
            }
            .padding(.vertical, 2)
            .listRowBackground(Color.orange.opacity(0.12))
        }
    }

    @ViewBuilder
    private var historySection: some View {
        Section {
            Picker("时间范围", selection: $historyRange) {
                Text("30 点").tag(30)
                Text("120 点").tag(120)
                Text("360 点").tag(360)
            }
            .pickerStyle(.segmented)
            .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))

            if model.metrics.isEmpty {
                EmptyHint(text: "还没有采样数据，保持连接并等几次刷新")
            } else {
                UsageHistoryChart(
                    cpu: model.metrics.cpuPoints(limit: historyRange),
                    memory: model.metrics.memoryPoints(limit: historyRange)
                )
                .padding(.vertical, 4)

                TemperatureHistoryChart(
                    points: model.metrics.temperaturePoints(limit: historyRange)
                )
                .padding(.vertical, 4)

                NetworkHistoryChart(
                    send: model.metrics.sendPoints(limit: historyRange),
                    recv: model.metrics.recvPoints(limit: historyRange)
                )
                .padding(.vertical, 4)
            }
        } header: {
            Text("历史曲线")
        } footer: {
            Text("每次刷新记一个采样点，最多保留 360 点，仅存在本机。")
        }
    }

    /// 未接 UPS 时整块隐藏，避免出现一堆「未知」占位
    @ViewBuilder
    private func upsSection(_ dashboard: Dashboard) -> some View {
        if let ups = dashboard.machine?.hardware?.ups {
            Section("UPS") {
                HStack {
                    Text("供电")
                        .font(.subheadline)
                    Spacer()
                    if let onBattery = ups.onBattery {
                        StatusBadge(
                            text: onBattery ? "电池供电" : "市电供电",
                            tint: onBattery ? .orange : .green
                        )
                    } else {
                        Text("-")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                }

                if let battery = ups.batteryPercent {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text("电量").font(.subheadline)
                            Spacer()
                            Text("\(battery)%")
                                .font(.subheadline.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                        UsageBar(percent: Double(battery))
                    }
                    .padding(.vertical, 2)
                }

                if let minutes = ups.runtimeMinutes {
                    InfoRow(title: "剩余续航", value: runtimeText(minutes))
                }
                if let name = ups.model, !name.isEmpty {
                    InfoRow(title: "型号", value: name)
                }
            }
        }
    }

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
            if !nets.isEmpty {
                InfoRow(
                    title: "实时流量",
                    value: "↑ \(Format.rate(dashboard.live?.sendRate))   ↓ \(Format.rate(dashboard.live?.recvRate))"
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

    private func runtimeText(_ minutes: Int) -> String {
        guard minutes > 0 else { return "-" }
        if minutes >= 60 { return "\(minutes / 60) 小时 \(minutes % 60) 分" }
        return "\(minutes) 分"
    }
}

// MARK: - 曲线

/// 显式声明 KeyValuePairs 类型，避免 chartForegroundStyleScale 的重载推断歧义
private let usageSeriesColors: KeyValuePairs<String, Color> = [
    "CPU": .accentColor,
    "内存": .green
]

private let networkSeriesColors: KeyValuePairs<String, Color> = [
    "上行": .orange,
    "下行": .blue
]

/// CPU / 内存使用率（0-100%）
private struct UsageHistoryChart: View {
    let cpu: [MetricsHistory.Point]
    let memory: [MetricsHistory.Point]

    var body: some View {
        Chart {
            ForEach(cpu) { point in
                LineMark(
                    x: .value("时间", point.date),
                    y: .value("使用率", point.value)
                )
                .foregroundStyle(by: .value("指标", "CPU"))
                .interpolationMethod(.monotone)
            }
            ForEach(memory) { point in
                LineMark(
                    x: .value("时间", point.date),
                    y: .value("使用率", point.value)
                )
                .foregroundStyle(by: .value("指标", "内存"))
                .interpolationMethod(.monotone)
            }
        }
        .chartForegroundStyleScale(usageSeriesColors)
        .chartYScale(domain: 0.0...100.0)
        .chartYAxis {
            AxisMarks(position: .leading, values: .automatic(desiredCount: 4)) { value in
                AxisGridLine()
                AxisValueLabel {
                    if let number = value.as(Double.self) {
                        Text("\(Int(number))%")
                    }
                }
            }
        }
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 3)) { _ in
                AxisGridLine()
                AxisValueLabel(format: Date.FormatStyle.dateTime.hour().minute())
            }
        }
        .frame(height: 180)
    }
}

/// CPU 温度（℃），没有温度数据时不画线
private struct TemperatureHistoryChart: View {
    let points: [MetricsHistory.Point]

    var body: some View {
        Chart {
            ForEach(points) { point in
                LineMark(
                    x: .value("时间", point.date),
                    y: .value("温度", point.value)
                )
                .foregroundStyle(Color.orange)
                .interpolationMethod(.monotone)
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading, values: .automatic(desiredCount: 4)) { value in
                AxisGridLine()
                AxisValueLabel {
                    if let number = value.as(Double.self) {
                        Text("\(Int(number))°")
                    }
                }
            }
        }
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 3)) { _ in
                AxisGridLine()
                AxisValueLabel(format: Date.FormatStyle.dateTime.hour().minute())
            }
        }
        .chartLegend(.hidden)
        .frame(height: 180)
    }
}

/// 网口收发速率（MB/s）
private struct NetworkHistoryChart: View {
    let send: [MetricsHistory.Point]
    let recv: [MetricsHistory.Point]

    var body: some View {
        Chart {
            ForEach(send) { point in
                LineMark(
                    x: .value("时间", point.date),
                    y: .value("速率", point.value)
                )
                .foregroundStyle(by: .value("方向", "上行"))
                .interpolationMethod(.monotone)
            }
            ForEach(recv) { point in
                LineMark(
                    x: .value("时间", point.date),
                    y: .value("速率", point.value)
                )
                .foregroundStyle(by: .value("方向", "下行"))
                .interpolationMethod(.monotone)
            }
        }
        .chartForegroundStyleScale(networkSeriesColors)
        .chartYAxis {
            AxisMarks(position: .leading, values: .automatic(desiredCount: 4)) { value in
                AxisGridLine()
                AxisValueLabel {
                    if let number = value.as(Double.self) {
                        Text(String(format: "%.1f", number))
                    }
                }
            }
        }
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 3)) { _ in
                AxisGridLine()
                AxisValueLabel(format: Date.FormatStyle.dateTime.hour().minute())
            }
        }
        .frame(height: 180)
    }
}
