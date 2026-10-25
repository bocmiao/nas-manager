import SwiftUI

/// 存储页：存储池 / 卷（分区）/ 磁盘，点磁盘看 SMART 明细
struct StorageView: View {
    let model: AppModel

    @State private var smartTarget: Disk?

    var body: some View {
        NavigationStack {
            List {
                poolSection
                volumeSection
                diskSection

                if let error = model.errorMessage {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("存储")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable { await model.refresh() }
            .sheet(item: $smartTarget) { disk in
                DiskSMARTSheet(model: model, disk: disk)
            }
        }
    }

    // MARK: 存储池

    @ViewBuilder
    private var poolSection: some View {
        let pools = model.dashboard?.pools ?? []
        Section {
            if pools.isEmpty {
                EmptyHint(text: "未读取到存储池")
            }
            ForEach(pools) { pool in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Text(pool.label ?? pool.name ?? "-")
                            .font(.subheadline.weight(.medium))
                        StatusBadge(text: (pool.level ?? "-").uppercased(), tint: .secondary)
                        Spacer()
                        HealthBadge(status: pool.status, normalValues: [0])
                    }

                    Text("\(pool.totalDiskNum ?? 0) 块盘 · 容量 \(Format.bytes(pool.total))")
                        .font(.caption2)
                        .foregroundStyle(.secondary)

                    if let used = pool.used, let total = pool.total, total > 0 {
                        let percent = Double(used) / Double(total) * 100
                        HStack(spacing: 8) {
                            UsageBar(percent: percent)
                            Text(Format.percent(percent))
                                .font(.caption2.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }

                    if !pool.isRedundant {
                        Label("该阵列无冗余，任一磁盘损坏都会丢数据", systemImage: "exclamationmark.triangle")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                    }
                }
                .padding(.vertical, 2)
            }
        } header: {
            Text("存储池")
        }
    }

    // MARK: 卷 / 分区

    @ViewBuilder
    private var volumeSection: some View {
        let volumes = model.dashboard?.volumes ?? []
        Section {
            if volumes.isEmpty {
                EmptyHint(text: "未读取到卷信息")
            }
            ForEach(volumes) { volume in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Text(volume.label ?? volume.name ?? "-")
                            .font(.subheadline.weight(.medium))
                        StatusBadge(
                            text: (volume.filesystem ?? "-").lowercased(),
                            tint: .secondary
                        )
                        Spacer()
                        Text(Format.percent(volume.usedPercent))
                            .font(.subheadline.monospacedDigit())
                            .foregroundStyle(volume.usedPercent >= 90 ? Color.orange : Color.secondary)
                    }

                    UsageBar(percent: volume.usedPercent)

                    Text("\(Format.bytes(volume.used)) / \(Format.bytes(volume.total)) · 可用 \(Format.bytes(volume.available))")
                        .font(.caption2)
                        .foregroundStyle(.secondary)

                    Text("挂载点 \(volume.mntpath ?? "-")")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                .padding(.vertical, 2)
            }
        } header: {
            Text("卷与分区")
        } footer: {
            Text("UGOS 上每个卷对应存储池里的一个文件系统分区，挂载点即该分区在系统中的路径。")
        }
    }

    // MARK: 磁盘

    @ViewBuilder
    private var diskSection: some View {
        let disks = model.dashboard?.disks ?? []
        Section {
            if disks.isEmpty {
                EmptyHint(text: "未读取到磁盘")
            }
            ForEach(disks) { disk in
                Button {
                    smartTarget = disk
                } label: {
                    diskRow(disk)
                }
                .buttonStyle(.plain)
            }
        } header: {
            Text("磁盘（点按查看 SMART）")
        }
    }

    private func diskRow(_ disk: Disk) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(disk.label ?? disk.name ?? "-")
                    .font(.subheadline.weight(.medium))
                HealthBadge(status: disk.status, normalValues: [0, 1])
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            Text("\(disk.brand ?? "") \(disk.model ?? "")")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)

            HStack(spacing: 12) {
                Label(Format.bytes(disk.size), systemImage: "internaldrive")
                Label(disk.temperature.map { "\($0)°C" } ?? "-", systemImage: "thermometer")
                Label(Format.hours(disk.powerOnHours), systemImage: "clock")
            }
            .font(.caption2)
            .foregroundStyle(.secondary)

            Text("用途：\(disk.usedFor ?? "未分配")")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }
}

// MARK: - SMART 明细

struct DiskSMARTSheet: View {
    let model: AppModel
    let disk: Disk

    @Environment(\.dismiss) private var dismiss
    @State private var info: SMARTInfo?
    @State private var errorText: String?
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            List {
                Section("概要") {
                    HStack {
                        Text("健康状态")
                        Spacer()
                        HealthBadge(status: disk.status, normalValues: [0, 1])
                    }
                    .font(.subheadline)
                    InfoRow(title: "SMART 判定", value: info?.health ?? "-")
                    InfoRow(title: "温度", value: temperatureText)
                    InfoRow(
                        title: "通电时长",
                        value: Format.hours(info?.powerOnHours ?? disk.powerOnHours)
                    )
                    InfoRow(
                        title: "型号",
                        value: "\(disk.brand ?? "") \(disk.model ?? "")"
                    )
                    InfoRow(title: "容量", value: Format.bytes(disk.size))
                    InfoRow(title: "所属", value: disk.usedFor ?? "未分配")
                }

                Section("SMART 属性") {
                    if isLoading {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("读取中…")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                    } else if let errorText = errorText {
                        Text(errorText)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    } else if let info = info, !info.rows.isEmpty {
                        ForEach(info.rows) { row in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(row.title)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                Text(row.detail)
                                    .font(.footnote.monospaced())
                            }
                        }
                    } else {
                        EmptyHint(text: "该磁盘未返回 SMART 明细，可能是固件版本差异或磁盘不支持")
                    }
                }
            }
            .navigationTitle(disk.label ?? "磁盘 SMART")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        Task { await load() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("完成") { dismiss() }
                }
            }
            .task {
                await load()
            }
        }
    }

    private var temperatureText: String {
        if let temperature = info?.temperature {
            return "\(temperature)°C"
        }
        if let temperature = disk.temperature {
            return "\(temperature)°C"
        }
        return "-"
    }

    private func load() async {
        isLoading = true
        errorText = nil
        do {
            info = try await model.smartInfo(for: disk)
        } catch {
            errorText = error.localizedDescription
        }
        isLoading = false
    }
}
