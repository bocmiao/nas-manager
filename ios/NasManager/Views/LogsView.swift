import Foundation
import SwiftUI

/// 系统日志：级别多选 / 模块 / 关键词 / 分页。
/// 级别只认 info、warning、critical；「错误」在界面上对应 critical。
struct LogsView: View {
    let model: AppModel

    @State private var entries: [LogEntry] = []
    @State private var modules: [String] = []
    @State private var selectedLevels: Set<String> = Set(LogLevel.all)
    /// 空字符串表示「全部模块」
    @State private var selectedModule = ""
    @State private var keyword = ""

    @State private var page = 0
    @State private var total: Int?
    @State private var isLoading = true
    @State private var isLoadingMore = false
    @State private var errorText: String?
    @State private var unavailable = false
    /// 只要成功过一次，后续偶发失败就当成错误提示而不是「接口不可用」
    @State private var everSucceeded = false

    var body: some View {
        List {
            filterSection

            if unavailable {
                unavailableSection
            }

            listSection

            if let message = errorText, !unavailable {
                Section {
                    Label(message, systemImage: "exclamationmark.triangle.fill")
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
            }
        }
        .listStyle(.plain)
        .navigationTitle("系统日志")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await reload() }
        .task { await firstLoad() }
    }

    // MARK: 筛选

    private var filterSection: some View {
        Section {
            HStack(spacing: 6) {
                ForEach(LogLevel.all, id: \.self) { level in
                    levelChip(level)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 2)

            Picker("模块", selection: $selectedModule) {
                Text("全部模块").tag("")
                ForEach(modules, id: \.self) { item in
                    Text(item).tag(item)
                }
            }

            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("关键词（日志正文为英文）", text: $keyword)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .onSubmit {
                        Task { await reload() }
                    }
                if !keyword.isEmpty {
                    Button {
                        keyword = ""
                        Task { await reload() }
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }

            Button {
                Task { await reload() }
            } label: {
                HStack(spacing: 8) {
                    if isLoading { ProgressView() }
                    Text(isLoading ? "查询中…" : "查询")
                }
            }
            .disabled(isLoading)
        } header: {
            Text("筛选")
        } footer: {
            Text("固件只接受 info / warning / critical 三个级别，应用内的「error」会映射为 critical。日志正文是英文，中文关键词通常查不到结果。")
        }
    }

    private func levelChip(_ level: String) -> some View {
        let selected = selectedLevels.contains(level)
        return Button {
            if selected {
                // 至少保留一个级别，避免把筛选清空后误以为没有日志
                if selectedLevels.count > 1 { selectedLevels.remove(level) }
            } else {
                selectedLevels.insert(level)
            }
            Task { await reload() }
        } label: {
            Text(LogLevel.title(level))
                .font(.caption.weight(.medium))
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background(
                    selected ? LogStyle.color(level).opacity(0.18) : Color.secondary.opacity(0.10),
                    in: Capsule()
                )
                .foregroundStyle(selected ? LogStyle.color(level) : Color.secondary)
        }
        .buttonStyle(.plain)
    }

    // MARK: 列表

    @ViewBuilder
    private var listSection: some View {
        if entries.isEmpty {
            Section {
                if isLoading {
                    loadingRow("正在读取日志…")
                } else if !unavailable {
                    EmptyHint(text: hasFilters ? "没有符合当前筛选条件的日志" : "这段时间没有日志记录")
                }
            }
        } else {
            Section {
                ForEach(entries) { entry in
                    row(entry)
                }

                if canLoadMore {
                    Button {
                        Task { await loadMore() }
                    } label: {
                        HStack(spacing: 8) {
                            if isLoadingMore { ProgressView() }
                            Text(isLoadingMore ? "加载中…" : "加载更多")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .disabled(isLoadingMore)
                }
            } header: {
                Text(headerText)
            } footer: {
                Text("按时间倒序排列。上拉或点「加载更多」继续读取下一页。")
            }
        }
    }

    private func row(_ entry: LogEntry) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                StatusBadge(text: entry.levelText, tint: LogStyle.color(entry.level))
                if let module = entry.module, !module.isEmpty {
                    Text(module)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                Text(Format.timestamp(entry.createdAt))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            Text(entry.content.isEmpty ? "-" : entry.content)
                .font(.footnote)
                .lineLimit(4)

            if let operatorName = entry.operatorName, !operatorName.isEmpty {
                Text("操作者 \(operatorName)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private var unavailableSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 6) {
                Label("日志接口不可用", systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline)
                Text(errorText ?? "当前固件未开放 v1/log/query。")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)

            Button("重试") {
                Task { await reload() }
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

    // MARK: 状态

    private var headerText: String {
        if let total = total { return "日志 \(entries.count) / \(total) 条" }
        return "日志 \(entries.count) 条"
    }

    private var canLoadMore: Bool {
        if let total = total { return entries.count < total }
        return entries.count >= LogService.pageSize
    }

    private var hasFilters: Bool {
        if !selectedModule.isEmpty { return true }
        if !keyword.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return true }
        return selectedLevels.count < LogLevel.all.count
    }

    private var levelList: [String] {
        let list = LogLevel.all.filter { selectedLevels.contains($0) }
        return list.isEmpty ? LogLevel.all : list
    }

    // MARK: 动作

    @MainActor
    private func firstLoad() async {
        if modules.isEmpty {
            modules = await model.logModules()
        }
        await reload()
    }

    @MainActor
    private func reload() async {
        isLoading = true
        page = 0
        // 换筛选条件后先清空，避免旧结果留在屏幕上被误认为新结果
        entries = []
        let result = await model.logs(
            page: 0,
            levels: levelList,
            module: selectedModule.isEmpty ? nil : selectedModule,
            keyword: keyword
        )
        isLoading = false

        if result.available {
            everSucceeded = true
            unavailable = false
            entries = result.entries
            total = result.total
            errorText = nil
        } else {
            unavailable = !everSucceeded
            errorText = result.message
        }
    }

    @MainActor
    private func loadMore() async {
        guard !isLoadingMore else { return }
        isLoadingMore = true
        let next = page + 1
        let result = await model.logs(
            page: next,
            levels: levelList,
            module: selectedModule.isEmpty ? nil : selectedModule,
            keyword: keyword
        )
        isLoadingMore = false

        if result.available {
            page = next
            if let newTotal = result.total { total = newTotal }
            let existing = Set(entries.map { $0.logId })
            let added = result.entries.filter { !existing.contains($0.logId) }
            entries.append(contentsOf: added)
            // 服务端已经没有更多数据时，别再让按钮一直悬着
            if added.isEmpty { total = entries.count }
            errorText = nil
        } else {
            errorText = result.message
        }
    }
}

/// 级别 → 文案与颜色。info 用次要色，warning 橙，critical 红。
enum LogStyle {
    static func color(_ level: String) -> Color {
        switch LogLevel.normalize(level) {
        case "info": return .secondary
        case "warning": return .orange
        case "critical": return .red
        default: return .secondary
        }
    }
}
