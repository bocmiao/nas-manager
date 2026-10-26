import Foundation
import SwiftUI

/// 备份任务（只读）。任务来自「同步与备份」应用，本机没有任务时给明确空态。
struct BackupView: View {
    let model: AppModel

    @State private var tasks: [BackupTask] = []
    @State private var available = true
    @State private var message: String?
    @State private var isLoading = true

    var body: some View {
        List {
            if !available && tasks.isEmpty {
                unavailableSection
            } else if isLoading && tasks.isEmpty {
                Section {
                    loadingRow("正在读取备份任务…")
                }
            } else if tasks.isEmpty {
                emptySection
            } else {
                taskSection

                if let message = message {
                    Section {
                        Label(message, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }
            }
        }
        .listStyle(.plain)
        .navigationTitle("备份任务")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
    }

    // MARK: 区块

    private var taskSection: some View {
        Section {
            ForEach(tasks) { task in
                row(task)
            }
        } header: {
            Text("备份任务（\(tasks.count)）")
        } footer: {
            Text("本页只读。立即运行、暂停与删除请到 UGOS 的「同步与备份」里操作。")
        }
    }

    /// 0 任务时刻意不写「0 个任务」，避免被理解成读取失败
    private var emptySection: some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                Label("还没有备份任务", systemImage: "tray")
                    .font(.subheadline)
                Text("这台 NAS 的「同步与备份」里没有已配置的任务。在 UGOS 网页端创建任务后，这里会显示状态、进度与运行时间。")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 6)
        } footer: {
            Text("能读到任务列表但列表为空，说明接口正常，只是没有任务。")
        }
    }

    private var unavailableSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 6) {
                Label("备份任务接口不可用", systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline)
                Text(message ?? "当前固件未开放「同步与备份」的任务接口。")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)

            Button("重试") {
                Task { await load() }
            }
        }
    }

    // MARK: 行

    private func row(_ task: BackupTask) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Text(task.name)
                    .font(.subheadline.weight(.medium))
                    .lineLimit(1)
                Spacer(minLength: 0)
                StatusBadge(text: task.statusText, tint: BackupStyle.tint(task.statusCode))
            }

            if let type = task.typeText {
                Text(type)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            if let progress = task.progress {
                if task.isRunning {
                    UsageBar(percent: Double(min(max(progress, 0), 100)))
                    Text("\(progress)%")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                } else {
                    InfoRow(title: "进度", value: "\(progress)%")
                }
            }

            InfoRow(title: "上次运行", value: Format.timestamp(task.lastRun))
            InfoRow(title: "下次运行", value: Format.timestamp(task.nextRun))

            if let detail = task.detail, !detail.isEmpty {
                Text(detail)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
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
        let result = await model.backupTasks()
        tasks = result.tasks
        available = result.available
        message = result.message
        isLoading = false
    }
}

/// 状态码 → 颜色：0 完成绿、1 失败红、2/4/6 进行类橙、3 暂停灰
enum BackupStyle {
    static func tint(_ code: Int?) -> Color {
        switch code {
        case 0: return .green
        case 1: return .red
        case 2, 4, 6: return .orange
        case 3: return .secondary
        default: return .secondary
        }
    }
}
