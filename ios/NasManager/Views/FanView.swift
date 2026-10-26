import Foundation
import SwiftUI

/// 风扇（只读）：当前模式 + 机箱 / CPU 转速。
/// 目标转速与档位表在真机上读不到，读不到就整块不显示，不画假曲线。
struct FanView: View {
    let model: AppModel

    @State private var info = FanSnapshot()
    @State private var isLoading = true

    var body: some View {
        List {
            if isLoading {
                Section {
                    loadingRow("正在读取风扇配置…")
                }
            } else if !info.available {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Label("风扇接口不可用", systemImage: "exclamationmark.triangle.fill")
                            .font(.subheadline)
                        Text(info.message ?? "当前固件未开放硬件风扇接口。")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)

                    Button("重试") {
                        Task { await load() }
                    }
                }
            } else {
                modeSection
                speedSection

                if info.hasLevelTables {
                    levelSection
                }

                if let message = info.message {
                    Section {
                        Label(message, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }
            }
        }
        .navigationTitle("风扇")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
    }

    // MARK: 策略

    private var modeSection: some View {
        Section {
            HStack(spacing: 6) {
                Text("当前模式")
                    .font(.subheadline)
                Spacer(minLength: 8)
                StatusBadge(text: info.modeText, tint: .accentColor)
                StatusBadge(text: "只读", tint: .secondary)
            }

            if info.configAvailable {
                InfoRow(title: "自定义曲线", value: info.supportCustom ? "机型支持" : "机型不支持")
            }
        } header: {
            Text("散热策略")
        } footer: {
            Text("模式枚举 1=静音 2=自动 3=全速 4=自定义。本页只读，调速请到 UGOS 网页端。")
        }
    }

    // MARK: 转速

    private var speedSection: some View {
        Section {
            speedRow(title: "机箱风扇", speed: info.deviceSpeed, status: info.deviceStatus)
            speedRow(title: "CPU 风扇", speed: info.cpuSpeed, status: info.cpuStatus)
        } header: {
            Text("转速")
        } footer: {
            Text("转速来自 taskmgr/stat/overview 的采样值。空闲时 CPU 风扇可能停转到 0 RPM。")
        }
    }

    private func speedRow(title: String, speed: Int?, status: Int?) -> some View {
        let state = FanStatus.describe(speed: speed, status: status)
        return HStack(spacing: 8) {
            Text(title)
                .font(.subheadline)
            Spacer(minLength: 8)
            Text(speed.map { "\($0) RPM" } ?? "—")
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(speed == nil ? Color.secondary : Color.primary)
            StatusBadge(text: state.text, tint: state.tint)
        }
    }

    // MARK: 档位表

    private var levelSection: some View {
        Section {
            if !info.sysFanLevels.isEmpty {
                levelRows(title: "机箱档位", points: info.sysFanLevels)
            }
            if !info.cpuFanLevels.isEmpty {
                levelRows(title: "CPU 档位", points: info.cpuFanLevels)
            }
        } header: {
            Text("档位表")
        } footer: {
            Text("档位表按接口原样展示，字段含义未经逐项验证。")
        }
    }

    @ViewBuilder
    private func levelRows(title: String, points: [FanLevelPoint]) -> some View {
        Text(title)
            .font(.caption)
            .foregroundStyle(.secondary)

        ForEach(points) { point in
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(point.temperature.map { "\($0) ℃" } ?? "—")
                    Spacer(minLength: 8)
                    Text(point.speed.map { "\($0) RPM" } ?? "—")
                        .foregroundStyle(.secondary)
                }
                .font(.footnote)

                if point.temperature == nil && point.speed == nil && !point.raw.isEmpty {
                    Text(point.raw)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
        }
    }

    private func loadingRow(_ text: String) -> some View {
        HStack(spacing: 8) {
            ProgressView()
            Text(text)
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: 动作

    @MainActor
    private func load() async {
        isLoading = true
        info = await model.fanSnapshot()
        isLoading = false
    }
}

/// 转速 + 状态位 → 文案与颜色。status 为 0 且转速为 0 时判定为停转。
enum FanStatus {
    static func describe(speed: Int?, status: Int?) -> (text: String, tint: Color) {
        if let speed = speed, speed > 0 { return ("运行", Color.green) }
        if status == 1 { return ("运行", Color.green) }
        if speed == 0 || status == 0 { return ("停转", Color.secondary) }
        return ("未知", Color.secondary)
    }
}
