import Foundation
import SwiftUI

/// 电源计划（只读）：是否启用、每条计划与下次执行时间。
/// 计划表藏在 v1/hardware/power/config 的 time_switches 里，不做任何写操作。
struct PowerPlanView: View {
    let model: AppModel

    @State private var plan = PowerPlanResult()
    @State private var isLoading = true

    var body: some View {
        List {
            if isLoading {
                Section {
                    loadingRow("正在读取电源配置…")
                }
            } else if !plan.available {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Label("电源配置不可用", systemImage: "exclamationmark.triangle.fill")
                            .font(.subheadline)
                        Text(plan.message ?? "当前固件未开放电源配置接口。")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)

                    Button("重试") {
                        Task { await load() }
                    }
                }
            } else {
                overviewSection
                scheduleSection
            }
        }
        .navigationTitle("电源计划")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
    }

    // MARK: 概览

    private var overviewSection: some View {
        Section {
            HStack(spacing: 6) {
                Text("定时开关机")
                    .font(.subheadline)
                Spacer(minLength: 8)
                if let enabled = plan.enabled {
                    StatusBadge(text: enabled ? "已启用" : "未启用", tint: enabled ? .green : .secondary)
                } else {
                    Text("未知")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }

            if let powerBoot = plan.powerBoot {
                InfoRow(title: "通电启动", value: powerBoot ? "开启" : "关闭")
            }
            if let wakeOn = plan.wakeOn {
                InfoRow(title: "网络唤醒", value: wakeOn ? "开启" : "关闭")
            }
        } header: {
            Text("概览")
        }
    }

    // MARK: 计划

    private var scheduleSection: some View {
        Section {
            if plan.switches.isEmpty {
                EmptyHint(text: plan.enabled == false ? "定时开关机未启用，也没有配置计划" : "还没有配置开关机计划")
            } else {
                ForEach(plan.switches) { item in
                    row(item)
                }
            }
        } header: {
            Text(plan.switches.isEmpty ? "计划" : "计划（\(plan.switches.count)）")
        } footer: {
            Text("本页只读，修改请到 UGOS 网页端。下次执行时间按本机时区推算，与设备实际行为可能相差数分钟。")
        }
    }

    private func row(_ item: PowerSwitch) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(item.time)
                    .font(.subheadline.monospacedDigit())
                StatusBadge(
                    text: item.actionText,
                    tint: item.action.lowercased() == "close" ? .orange : .green
                )
                Spacer(minLength: 0)
                Text(item.weekdaysText)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            InfoRow(
                title: "下次执行",
                value: item.nextRun().map { Format.timestamp($0) }
            )
        }
        .padding(.vertical, 2)
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
        plan = await model.powerPlan()
        isLoading = false
    }
}
