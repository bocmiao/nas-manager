import SwiftUI

/// 复制 / 移动的目标目录选择器。
///
/// 只列目录，不列文件——目标必须是个目录，列文件只会干扰。
/// 顶层是根分类（走 `rootType`），下钻之后一律走 `path`，
/// 两种列表请求的形状不一样，所以栈里要把 `rootType` 一起带着。
///
/// 已经在 `blocked` 里的路径（也就是被操作条目自身）不给选，
/// 免得把目录塞进自己的子目录里。
struct DestinationPickerView: View {
    let model: AppModel
    let actionTitle: String
    /// 这些路径连同它们的子目录都不能选（通常是被操作的条目自身）
    let blocked: Set<String>

    @Environment(\.dismiss) private var dismiss

    @State private var stack: [FileNode] = []
    @State private var roots: FileRoots?
    @State private var entries: [FileEntry] = []
    @State private var isLoading = false
    @State private var errorText: String?

    let onPick: (String) -> Void

    var body: some View {
        NavigationStack {
            List {
                if stack.isEmpty {
                    rootSection
                } else {
                    currentSection
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle(stack.last?.name ?? "选择目标目录")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    if stack.isEmpty {
                        Button("取消") { dismiss() }
                    } else {
                        Button {
                            stack.removeLast()
                            Task { await load() }
                        } label: {
                            Label("上一级", systemImage: "chevron.left")
                        }
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    if let current = currentPath {
                        Button(actionTitle) {
                            onPick(current)
                            dismiss()
                        }
                        .fontWeight(.semibold)
                    }
                }
            }
            .task { await load() }
        }
    }

    // MARK: 内容

    @ViewBuilder
    private var rootSection: some View {
        if let errorText {
            Section {
                Text(errorText).font(.footnote).foregroundStyle(.secondary)
            }
        }
        Section("根目录") {
            if let roots {
                ForEach(roots.categories) { category in
                    Button {
                        stack.append(
                            FileNode(name: category.name, path: nil, rootType: category.rootType)
                        )
                        Task { await load() }
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: category.icon)
                                .foregroundStyle(Color.accentColor)
                                .frame(width: 22)
                            Text(category.name)
                                .foregroundStyle(.primary)
                            Spacer(minLength: 0)
                            Image(systemName: "chevron.right")
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                        }
                    }
                }
            } else if isLoading {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("正在读取目录…").font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
    }

    @ViewBuilder
    private var currentSection: some View {
        Section {
            if isLoading && entries.isEmpty {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("正在读取…").font(.footnote).foregroundStyle(.secondary)
                }
            } else if entries.isEmpty {
                Text("这个目录下没有子目录")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(entries) { entry in
                    Button {
                        guard !isBlocked(entry.path) else { return }
                        stack.append(
                            FileNode(name: entry.name, path: entry.path, rootType: nil)
                        )
                        Task { await load() }
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: isBlocked(entry.path) ? "folder.badge.minus" : "folder")
                                .foregroundStyle(isBlocked(entry.path) ? Color.secondary : Color.accentColor)
                                .frame(width: 22)
                            Text(entry.name)
                                .foregroundStyle(isBlocked(entry.path) ? .secondary : .primary)
                            Spacer(minLength: 0)
                            if isBlocked(entry.path) {
                                Text("被操作的条目")
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                            } else {
                                Image(systemName: "chevron.right")
                                    .font(.caption)
                                    .foregroundStyle(.tertiary)
                            }
                        }
                    }
                    .disabled(isBlocked(entry.path))
                }
            }
        } footer: {
            if let errorText {
                Text(errorText)
            }
        }
    }

    // MARK: 状态

    /// 只有下钻到具体路径后才能选；顶层分类没有可复制的绝对路径
    private var currentPath: String? {
        stack.last?.path
    }

    private func isBlocked(_ path: String) -> Bool {
        blocked.contains { path == $0 || path.hasPrefix($0 + "/") }
    }

    @MainActor
    private func load() async {
        isLoading = true
        errorText = nil
        defer { isLoading = false }
        let node = stack.last
        // 被操作条目自身不能当目标
        guard !(node?.path.map(isBlocked) ?? false) else {
            entries = []
            errorText = "这是被操作的条目本身，不能选"
            return
        }
        do {
            if stack.isEmpty {
                roots = try await model.fileRoots()
                entries = []
            } else {
                let listing = try await model.fileList(
                    path: node?.path,
                    rootType: node?.rootType,
                    page: 1
                )
                entries = listing.entries.filter { $0.isDir }.sorted { $0.name < $1.name }
            }
        } catch {
            entries = []
            errorText = error.localizedDescription
        }
    }
}
