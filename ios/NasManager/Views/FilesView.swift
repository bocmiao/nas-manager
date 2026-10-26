import Foundation
import SwiftUI
import UniformTypeIdentifiers

// MARK: - 路径栈节点

/// 面包屑节点。根分类只有 `rootType`（NAS 要求按编号列），下钻后才有 `path`。
/// `top_path` 是树的根而不是当前目录，所以当前路径只能靠这个栈自己维护。
struct FileNode: Hashable, Identifiable {
    let name: String
    let path: String?
    let rootType: Int?

    var id: String { path ?? "root-\(rootType ?? 0)-\(name)" }
}

/// 下载完成待分享的临时文件
private struct SharedFile: Identifiable {
    let id = UUID()
    let url: URL
    let name: String
}

/// 上传进度。多文件是顺序上传，这里只记「第几个 / 共几个 + 当前文件名」
private struct FileUploadProgress {
    let index: Int
    let total: Int
    let name: String
}

// MARK: - 文件管理

/// 文件页：根分类 → 目录下钻 → 排序 / 下载分享 / 重命名 / 删除 / 新建文件夹。
struct FilesView: View {
    let model: AppModel

    @State private var roots: FileRoots?
    @State private var isLoadingRoots = true
    @State private var rootsError: String?

    @State private var stack: [FileNode] = []
    @State private var entries: [FileEntry] = []
    @State private var total = 0
    @State private var page = 1
    @State private var hasMore = false
    @State private var isLoading = false
    @State private var errorText: String?
    @State private var freeText: String?

    @State private var sort: FileSort = .name
    @State private var reverse = false

    @State private var showingNewFolder = false
    @State private var newFolderName = ""
    @State private var pendingRename: FileEntry?
    @State private var renameText = ""
    @State private var pendingDelete: FileEntry?
    @State private var busyPath: String?
    @State private var actionError: String?
    @State private var shared: SharedFile?

    @State private var showingImporter = false
    @State private var uploadProgress: FileUploadProgress?
    @State private var uploadSummary: String?

    var body: some View {
        List {
            if stack.isEmpty {
                rootsSections
            } else {
                breadcrumbSection
                entriesSection
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle(stack.last?.name ?? "文件")
        .navigationBarTitleDisplayMode(.inline)
        .navigationBarBackButtonHidden(!stack.isEmpty)
        .refreshable { await reload() }
        .task { await loadRootsIfNeeded() }
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                if !stack.isEmpty {
                    Button {
                        pop()
                    } label: {
                        Label("上一级", systemImage: "chevron.left")
                    }
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    showingImporter = true
                } label: {
                    if uploadProgress != nil {
                        ProgressView()
                    } else {
                        Image(systemName: "arrow.up.doc")
                    }
                }
                .disabled(!canUpload)
                .accessibilityLabel("上传文件")
            }
            ToolbarItem(placement: .topBarTrailing) {
                if canCreateFolder {
                    Button {
                        newFolderName = ""
                        showingNewFolder = true
                    } label: {
                        Image(systemName: "folder.badge.plus")
                    }
                    .disabled(uploadProgress != nil)
                }
            }
        }
        .fileImporter(
            isPresented: $showingImporter,
            allowedContentTypes: [.item],
            allowsMultipleSelection: true
        ) { result in
            switch result {
            case .success(let urls):
                Task { await performUploads(urls) }
            case .failure(let error):
                actionError = "选择文件失败：\(error.localizedDescription)"
            }
        }
        .alert("新建文件夹", isPresented: $showingNewFolder) {
            TextField("文件夹名称", text: $newFolderName)
            Button("取消", role: .cancel) { newFolderName = "" }
            Button("创建") {
                let name = newFolderName
                newFolderName = ""
                Task { await performCreateFolder(name) }
            }
        } message: {
            Text("将在「\(stack.last?.name ?? "")」下创建")
        }
        .alert("重命名", isPresented: renameBinding) {
            TextField("新名称", text: $renameText)
            Button("取消", role: .cancel) { pendingRename = nil }
            Button("确定") {
                let target = pendingRename
                let name = renameText
                pendingRename = nil
                if let target { Task { await performRename(target, newName: name) } }
            }
        } message: {
            Text("原名：\(pendingRename?.name ?? "")")
        }
        .confirmationDialog(deleteTitle, isPresented: deleteBinding, titleVisibility: .visible) {
            // 只有该目录确实支持回收站时才给「移入回收站」这个选项。
            // 否则用户会以为能找回，实际 NAS 直接永久删除（实测 recycle_open=false 就是这样）。
            if canRecoverDelete {
                Button("移入回收站") {
                    let target = pendingDelete
                    pendingDelete = nil
                    if let target { Task { await performDelete(target, forever: false) } }
                }
            }
            Button("永久删除（不可恢复）", role: .destructive) {
                let target = pendingDelete
                pendingDelete = nil
                if let target { Task { await performDelete(target, forever: true) } }
            }
            Button("取消", role: .cancel) { pendingDelete = nil }
        } message: {
            Text(deleteMessage)
        }
        .alert("操作失败", isPresented: actionErrorBinding) {
            Button("好", role: .cancel) { actionError = nil }
        } message: {
            Text(actionError ?? "")
        }
        .alert("上传结果", isPresented: uploadSummaryBinding) {
            Button("好", role: .cancel) { uploadSummary = nil }
        } message: {
            Text(uploadSummary ?? "")
        }
        .sheet(item: $shared) { file in
            shareSheet(file)
        }
    }

    // MARK: 根页面

    @ViewBuilder
    private var rootsSections: some View {
        if isLoadingRoots && roots == nil {
            Section {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("正在读取目录…")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        }

        if let rootsError {
            Section {
                Label(rootsError, systemImage: "exclamationmark.triangle.fill")
                    .font(.footnote)
                    .foregroundStyle(.red)
            }
        }

        if let roots {
            if roots.categories.isEmpty {
                Section {
                    EmptyHint(text: "这台 NAS 没有可浏览的分类")
                } header: {
                    Text("位置")
                }
            } else {
                Section {
                    ForEach(roots.categories) { category in
                        Button {
                            push(FileNode(name: category.name, path: nil, rootType: category.rootType))
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: category.icon)
                                    .foregroundStyle(.tint)
                                    .frame(width: 24, alignment: .center)
                                Text(category.name)
                                    .foregroundStyle(.primary)
                                Spacer(minLength: 0)
                                Image(systemName: "chevron.right")
                                    .font(.caption)
                                    .foregroundStyle(.tertiary)
                            }
                        }
                        .buttonStyle(.plain)
                    }
                } header: {
                    Text("位置")
                }
            }

            if !roots.volumes.isEmpty {
                Section {
                    ForEach(roots.volumes) { volume in
                        volumeRow(volume)
                    }
                } header: {
                    Text("我的存储")
                } footer: {
                    Text("这里只展示卷的容量。浏览请在「位置」里选分类——直接访问卷路径会被 NAS 判为越权。")
                }
            }

            Section {
                Label("上传需要先进入具体文件夹", systemImage: "arrow.up.doc")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } footer: {
                Text("根分类只是入口，不是可写路径，所以这里的上传按钮是灰的。上传时同名文件会自动改名为「名称(1).后缀」，不会覆盖已有文件。")
            }
        }
    }

    private func volumeRow(_ volume: FileVolume) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "internaldrive")
                    .foregroundStyle(.teal)
                Text(volume.name)
                    .font(.subheadline.weight(.medium))
                if let fsType = volume.fsType, !fsType.isEmpty {
                    StatusBadge(text: fsType.uppercased(), tint: .secondary)
                }
                Spacer(minLength: 0)
                Text(Format.percent(volume.usedPercent))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            UsageBar(percent: volume.usedPercent)

            Text("已用 \(Format.bytes(volume.used)) · 共 \(Format.bytes(volume.total)) · 可用 \(Format.bytes(volume.free))")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }

    // MARK: 面包屑

    private var breadcrumbSection: some View {
        Section {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    Button("全部") { resetToRoots() }
                        .font(.caption)
                        .buttonStyle(.plain)
                        .foregroundStyle(.tint)

                    ForEach(stack.indices, id: \.self) { index in
                        Image(systemName: "chevron.right")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                        Button(stack[index].name) { truncate(to: index) }
                            .font(.caption)
                            .buttonStyle(.plain)
                            .foregroundStyle(index == stack.count - 1 ? Color.primary : Color.accentColor)
                            .lineLimit(1)
                    }
                }
                .padding(.vertical, 2)
            }
        }
    }

    // MARK: 列表

    @ViewBuilder
    private var entriesSection: some View {
        if let progress = uploadProgress {
            Section {
                HStack(spacing: 10) {
                    ProgressView()
                    VStack(alignment: .leading, spacing: 2) {
                        Text("正在上传…（\(progress.index)/\(progress.total)）")
                            .font(.footnote)
                        Text(progress.name)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
            } footer: {
                Text("同名文件会自动改名为「名称(1).后缀」，不会覆盖已有文件。")
            }
        }

        if isLoading && entries.isEmpty {
            Section {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("正在读取…")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        }

        if let errorText {
            Section {
                Label(errorText, systemImage: "exclamationmark.triangle.fill")
                    .font(.footnote)
                    .foregroundStyle(.red)
            }
        }

        if !isLoading && errorText == nil && entries.isEmpty {
            Section {
                EmptyHint(text: "这个目录是空的")
            }
        }

        if !entries.isEmpty {
            Section {
                ForEach(entries) { entry in
                    row(entry)
                }
                if hasMore {
                    loadMoreRow
                }
            } header: {
                HStack {
                    Text("\(entries.count) / \(total) 项")
                    Spacer(minLength: 8)
                    sortMenu
                }
            } footer: {
                if let freeText {
                    Text(freeText)
                } else {
                    Text("长按或左滑可重命名 / 删除；点文件下载后通过系统面板保存或分享。")
                }
            }
        }
    }

    private var loadMoreRow: some View {
        Button {
            Task { await loadDirectory(reset: false) }
        } label: {
            HStack(spacing: 8) {
                if isLoading {
                    ProgressView()
                        .scaleEffect(0.7)
                }
                Text("加载更多（已显示 \(entries.count) / \(total)）")
                    .font(.footnote)
            }
        }
    }

    private var sortMenu: some View {
        Menu {
            ForEach(FileSort.primary) { option in
                Button {
                    guard sort != option else { return }
                    sort = option
                    Task { await loadDirectory(reset: true) }
                } label: {
                    if sort == option {
                        Label(option.label, systemImage: "checkmark")
                    } else {
                        Text(option.label)
                    }
                }
            }
            Button {
                reverse.toggle()
                Task { await loadDirectory(reset: true) }
            } label: {
                Label(reverse ? "当前降序，改为升序" : "当前升序，改为降序",
                      systemImage: reverse ? "arrow.up" : "arrow.down")
            }
        } label: {
            Label("\(sort.label)\(reverse ? " ↓" : " ↑")", systemImage: "arrow.up.arrow.down")
                .font(.caption)
                .labelStyle(.titleAndIcon)
        }
    }

    private func row(_ entry: FileEntry) -> some View {
        Button {
            open(entry)
        } label: {
            HStack(spacing: 10) {
                Image(systemName: entry.iconName)
                    .font(.body)
                    .foregroundStyle(entry.isDir ? Color.accentColor : Color.secondary)
                    .frame(width: 24, alignment: .center)

                VStack(alignment: .leading, spacing: 2) {
                    Text(entry.name)
                        .font(.subheadline)
                        .lineLimit(1)
                    Text(subtitle(entry))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                Spacer(minLength: 0)

                if busyPath == entry.path {
                    ProgressView()
                        .scaleEffect(0.7)
                } else if entry.isDir {
                    Image(systemName: "chevron.right")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .swipeActions(edge: .trailing) {
            if entry.isDeletable {
                Button(role: .destructive) {
                    pendingDelete = entry
                } label: {
                    Label("删除", systemImage: "trash")
                }
            }
            if entry.isRenamable {
                Button {
                    renameText = entry.name
                    pendingRename = entry
                } label: {
                    Label("重命名", systemImage: "pencil")
                }
                .tint(.orange)
            }
        }
        .contextMenu {
            if entry.isDownloadable {
                Button {
                    Task { await performDownload(entry) }
                } label: {
                    Label("下载并分享", systemImage: "square.and.arrow.down")
                }
            }
            if entry.isRenamable {
                Button {
                    renameText = entry.name
                    pendingRename = entry
                } label: {
                    Label("重命名", systemImage: "pencil")
                }
            }
            if entry.isDeletable {
                Button(role: .destructive) {
                    pendingDelete = entry
                } label: {
                    Label("删除", systemImage: "trash")
                }
            }
            if entry.isTag {
                Text("标签不是文件，不能下载或删除")
            }
        }
    }

    private func subtitle(_ entry: FileEntry) -> String {
        if entry.isTag {
            return "标签 · 不能下载或删除"
        }
        var parts: [String] = []
        parts.append(entry.isDir ? "文件夹" : entry.sizeText)
        if entry.isMount {
            parts.append("挂载点")
        }
        parts.append(entry.mtimeText)
        return parts.joined(separator: " · ")
    }

    // MARK: 分享面板

    private func shareSheet(_ file: SharedFile) -> some View {
        NavigationStack {
            VStack(spacing: 16) {
                Image(systemName: "doc.fill")
                    .font(.system(size: 44))
                    .foregroundStyle(.tint)
                Text(file.name)
                    .font(.subheadline)
                    .multilineTextAlignment(.center)
                    .lineLimit(3)
                ShareLink(item: file.url) {
                    Label("分享 / 存储到「文件」", systemImage: "square.and.arrow.up")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                Text("文件已下载到本机临时目录，分享后可长期保存。")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .padding()
            .navigationTitle("已下载")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("完成") { shared = nil }
                }
            }
        }
    }

    // MARK: 绑定

    private var renameBinding: Binding<Bool> {
        Binding(
            get: { pendingRename != nil },
            set: { visible in
                if !visible { pendingRename = nil }
            }
        )
    }

    private var deleteBinding: Binding<Bool> {
        Binding(
            get: { pendingDelete != nil },
            set: { visible in
                if !visible { pendingDelete = nil }
            }
        )
    }

    /// 删除后能不能从回收站找回，取决于条目所在目录的 `recycle_open`
    private var canRecoverDelete: Bool {
        pendingDelete?.isDeletionRecoverable ?? false
    }

    private var deleteTitle: String {
        let name = pendingDelete?.name ?? ""
        return canRecoverDelete ? "删除「\(name)」？" : "永久删除「\(name)」？"
    }

    private var deleteMessage: String {
        if canRecoverDelete {
            return "移入回收站后可以从 NAS 回收站找回；选「永久删除」则无法恢复。"
        }
        return "该目录没有开启回收站，删除后无法找回，文件会被立即清除。"
    }

    private var actionErrorBinding: Binding<Bool> {
        Binding(
            get: { actionError != nil },
            set: { visible in
                if !visible { actionError = nil }
            }
        )
    }

    private var uploadSummaryBinding: Binding<Bool> {
        Binding(
            get: { uploadSummary != nil },
            set: { visible in
                if !visible { uploadSummary = nil }
            }
        )
    }

    // MARK: 导航

    /// 只有当前目录有真实路径才能建子目录：根分类没有 path，NAS 也不接受 path=/ 的写法
    private var canCreateFolder: Bool {
        guard let current = stack.last else { return false }
        return current.path?.isEmpty == false
    }

    /// 上传同理：根分类只是入口，不是可写路径；上传进行中也不允许再次触发
    private var canUpload: Bool {
        guard uploadProgress == nil else { return false }
        guard let current = stack.last, let path = current.path else { return false }
        return !path.isEmpty
    }

    private func push(_ node: FileNode) {
        stack.append(node)
        entries = []
        total = 0
        page = 1
        hasMore = false
        errorText = nil
        freeText = nil
        Task { await loadDirectory(reset: true) }
    }

    private func pop() {
        guard !stack.isEmpty else { return }
        stack.removeLast()
        if stack.isEmpty {
            resetToRoots()
        } else {
            entries = []
            total = 0
            page = 1
            hasMore = false
            errorText = nil
            freeText = nil
            Task { await loadDirectory(reset: true) }
        }
    }

    private func resetToRoots() {
        stack = []
        entries = []
        total = 0
        page = 1
        hasMore = false
        errorText = nil
        freeText = nil
        Task { await loadRoots(force: true) }
    }

    private func truncate(to index: Int) {
        guard index >= 0, index < stack.count - 1 else { return }
        stack = Array(stack.prefix(index + 1))
        entries = []
        total = 0
        page = 1
        hasMore = false
        errorText = nil
        freeText = nil
        Task { await loadDirectory(reset: true) }
    }

    private func open(_ entry: FileEntry) {
        if entry.isDir {
            push(FileNode(name: entry.name, path: entry.path, rootType: nil))
        } else if entry.isTag {
            actionError = "标签不是文件，不能下载"
        } else {
            Task { await performDownload(entry) }
        }
    }

    // MARK: 动作

    @MainActor
    private func loadRootsIfNeeded() async {
        if roots != nil { return }
        await loadRoots(force: false)
    }

    @MainActor
    private func loadRoots(force: Bool) async {
        if !force, roots != nil { return }
        isLoadingRoots = true
        rootsError = nil
        do {
            roots = try await model.fileRoots()
        } catch {
            rootsError = error.localizedDescription
        }
        isLoadingRoots = false
    }

    @MainActor
    private func loadDirectory(reset: Bool) async {
        guard let current = stack.last else { return }
        if reset {
            isLoading = true
            errorText = nil
        }

        let nextPage = reset ? 1 : page + 1
        do {
            let listing = try await model.fileList(
                path: current.path,
                rootType: current.rootType,
                page: nextPage,
                limit: 200,
                sort: sort,
                reverse: reverse
            )
            if reset {
                entries = listing.entries
            } else {
                entries.append(contentsOf: listing.entries)
            }
            page = nextPage
            total = listing.total
            hasMore = listing.hasMore
            errorText = nil
        } catch {
            if reset {
                entries = []
                total = 0
                hasMore = false
            }
            errorText = error.localizedDescription
        }
        isLoading = false

        if reset, let currentPath = current.path, errorText == nil {
            await loadFreeSpace(currentPath)
        }
    }

    @MainActor
    private func loadFreeSpace(_ path: String) async {
        // 剩余空间是附加信息，取不到就不显示，不打断浏览
        guard let space = try? await model.fileFreeSpace(path: path) else {
            freeText = nil
            return
        }
        if let free = space.free {
            freeText = "该目录所在卷剩余空间 \(Format.bytes(free))"
        } else {
            freeText = nil
        }
    }

    @MainActor
    private func reload() async {
        if stack.isEmpty {
            await loadRoots(force: true)
        } else {
            await loadDirectory(reset: true)
        }
    }

    @MainActor
    private func performCreateFolder(_ name: String) async {
        guard let parent = stack.last?.path, !parent.isEmpty else {
            actionError = "当前目录不支持新建文件夹"
            return
        }
        do {
            _ = try await model.createFolder(parent: parent, name: name)
            await loadDirectory(reset: true)
        } catch {
            actionError = error.localizedDescription
        }
    }

    @MainActor
    private func performRename(_ entry: FileEntry, newName: String) async {
        let trimmed = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        busyPath = entry.path
        do {
            _ = try await model.renameFile(path: entry.path, newName: trimmed)
            await loadDirectory(reset: true)
        } catch {
            actionError = error.localizedDescription
        }
        busyPath = nil
    }

    @MainActor
    private func performDelete(_ entry: FileEntry, forever: Bool) async {
        busyPath = entry.path
        do {
            _ = try await model.deleteFiles(paths: [entry.path], forever: forever)
            await loadDirectory(reset: true)
        } catch {
            actionError = error.localizedDescription
        }
        busyPath = nil
    }

    @MainActor
    private func performDownload(_ entry: FileEntry) async {
        guard busyPath == nil else { return }
        busyPath = entry.path
        defer { busyPath = nil }
        do {
            let data = try await model.downloadFile(path: entry.path)
            let url = try writeTemporary(data: data, name: entry.name)
            shared = SharedFile(url: url, name: entry.name)
        } catch {
            actionError = error.localizedDescription
        }
    }

    /// 顺序上传选中的文件：多选时逐个来，避免同时开多条大流量连接把 NAS 打满。
    @MainActor
    private func performUploads(_ urls: [URL]) async {
        guard uploadProgress == nil else { return }
        guard let dest = stack.last?.path, !dest.isEmpty else {
            actionError = "根分类不是可写路径，请先进入具体文件夹再上传"
            return
        }
        guard !urls.isEmpty else { return }

        var landed: [String] = []
        var failures: [String] = []

        for (index, url) in urls.enumerated() {
            let filename = url.lastPathComponent
            uploadProgress = FileUploadProgress(
                index: index + 1, total: urls.count, name: filename
            )

            // 文件选择器给出的 URL 是 security-scoped：不申请就读取会被系统直接拒绝，
            // 申请后必须配对释放，否则引用计数泄漏。
            _ = url.startAccessingSecurityScopedResource()
            defer { url.stopAccessingSecurityScopedResource() }

            do {
                guard !filename.isEmpty else { throw UgosError.auth("文件名缺失") }
                let data = try Data(contentsOf: url, options: .mappedIfSafe)
                let result = try await model.uploadFile(
                    data: data, filename: filename, destPath: dest
                )
                landed.append(result.name)
            } catch {
                failures.append("\(filename)：\(error.localizedDescription)")
            }
        }

        uploadProgress = nil

        if !landed.isEmpty {
            await loadDirectory(reset: true)
        }

        var lines: [String] = []
        if !landed.isEmpty {
            lines.append("已上传 \(landed.count) 个文件：\n" + landed.joined(separator: "\n"))
        }
        if !failures.isEmpty {
            lines.append("失败 \(failures.count) 个：\n" + failures.joined(separator: "\n"))
        }
        if !lines.isEmpty {
            uploadSummary = lines.joined(separator: "\n\n")
        }
    }

    /// 落一个临时文件给 ShareLink 用；同名文件先删掉，避免拿到上一次的旧内容
    private func writeTemporary(data: Data, name: String) throws -> URL {
        let manager = FileManager.default
        let directory = manager.temporaryDirectory.appendingPathComponent("nas-files", isDirectory: true)
        if !manager.fileExists(atPath: directory.path) {
            try manager.createDirectory(at: directory, withIntermediateDirectories: true, attributes: nil)
        }
        let fileName = name.isEmpty ? "download" : name.replacingOccurrences(of: "/", with: "_")
        let url = directory.appendingPathComponent(fileName)
        if manager.fileExists(atPath: url.path) {
            try manager.removeItem(at: url)
        }
        try data.write(to: url, options: .atomic)
        return url
    }
}
