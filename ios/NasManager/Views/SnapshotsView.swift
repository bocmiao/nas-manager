import Foundation
import SwiftUI

/// 快照页：列表 / 创建 / 删除 / 回滚（回滚必须二次确认）
struct SnapshotsView: View {
    let model: AppModel

    @State private var items: [SnapshotItem] = []
    @State private var isLoading = true
    @State private var unavailable = false
    @State private var errorText: String?
    @State private var actionError: String?

    @State private var showingCreate = false
    @State private var pendingDelete: SnapshotItem?
    @State private var pendingRollback: SnapshotItem?
    @State private var busyId: String?

    var body: some View {
        List {
            if unavailable {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("该固件未开放快照接口", systemImage: "clock.badge.questionmark")
                            .font(.subheadline)
                        Text("已按 storage/snapshot 与 snapshot 两组路径探测，均没有有效返回。本页不会显示占位数据，也不会发起任何写操作。")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                }
            } else if items.isEmpty {
                Section {
                    if isLoading {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("正在探测快照接口…")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                    } else {
                        EmptyHint(text: "还没有快照")
                    }
                }
            } else {
                Section {
                    ForEach(items) { item in
                        row(item)
                    }
                } header: {
                    Text("快照（\(items.count)）")
                } footer: {
                    Text("回滚会把卷内容恢复到所选快照，之后产生的数据会被覆盖，请谨慎操作。")
                }
            }

            if let errorText {
                Section {
                    Label(errorText, systemImage: "exclamationmark.triangle.fill")
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
            }
        }
        .navigationTitle("快照")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task(id: showingCreate) {
            // 首次进入与创建面板关闭后都重新读一次
            if !showingCreate { await load() }
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    showingCreate = true
                } label: {
                    Image(systemName: "plus")
                }
                .disabled(unavailable)
            }
        }
        .sheet(isPresented: $showingCreate) {
            CreateSnapshotSheet(model: model)
        }
        .confirmationDialog("删除快照？", isPresented: deleteBinding, titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                let target = pendingDelete
                pendingDelete = nil
                if let target { Task { await performDelete(target) } }
            }
            Button("取消", role: .cancel) { pendingDelete = nil }
        } message: {
            Text("删除「\(pendingDelete?.displayName ?? "")」后不可恢复。")
        }
        .confirmationDialog("回滚到该快照？", isPresented: rollbackBinding, titleVisibility: .visible) {
            Button("回滚（覆盖现有数据）", role: .destructive) {
                let target = pendingRollback
                pendingRollback = nil
                if let target { Task { await performRollback(target) } }
            }
            Button("取消", role: .cancel) { pendingRollback = nil }
        } message: {
            Text("回滚会把卷 \(pendingRollback?.volumeName ?? "-") 恢复到「\(pendingRollback?.displayName ?? "")」，期间新增的数据会被覆盖，且无法撤销。")
        }
        .alert("操作失败", isPresented: actionErrorBinding) {
            Button("好", role: .cancel) { actionError = nil }
        } message: {
            Text(actionError ?? "")
        }
    }

    // MARK: 行

    private func row(_ item: SnapshotItem) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text(item.displayName)
                    .font(.subheadline.weight(.medium))
                    .lineLimit(1)

                Text(subtitle(item))
                    .font(.caption2)
                    .foregroundStyle(.secondary)

                if let remark = item.remark, !remark.isEmpty {
                    Text(remark)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }

            Spacer(minLength: 0)

            if busyId == item.id {
                ProgressView()
                    .scaleEffect(0.7)
            }

            Menu {
                Button {
                    pendingRollback = item
                } label: {
                    Label("回滚到此快照", systemImage: "arrow.uturn.backward")
                }
                Button(role: .destructive) {
                    pendingDelete = item
                } label: {
                    Label("删除", systemImage: "trash")
                }
            } label: {
                Image(systemName: "ellipsis.circle")
                    .font(.title3)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .swipeActions(edge: .trailing) {
            Button(role: .destructive) {
                pendingDelete = item
            } label: {
                Label("删除", systemImage: "trash")
            }
            Button {
                pendingRollback = item
            } label: {
                Label("回滚", systemImage: "arrow.uturn.backward")
            }
            .tint(.orange)
        }
    }

    private func subtitle(_ item: SnapshotItem) -> String {
        var parts: [String] = []
        if let volume = item.volumeName, !volume.isEmpty {
            parts.append("卷 \(volume)")
        }
        if item.size != nil {
            parts.append(Format.bytes(item.size))
        }
        if let created = item.createdText, !created.isEmpty {
            parts.append(created)
        }
        return parts.isEmpty ? "-" : parts.joined(separator: " · ")
    }

    private var deleteBinding: Binding<Bool> {
        Binding(
            get: { pendingDelete != nil },
            set: { visible in
                if !visible { pendingDelete = nil }
            }
        )
    }

    private var rollbackBinding: Binding<Bool> {
        Binding(
            get: { pendingRollback != nil },
            set: { visible in
                if !visible { pendingRollback = nil }
            }
        )
    }

    private var actionErrorBinding: Binding<Bool> {
        Binding(
            get: { actionError != nil },
            set: { visible in
                if !visible { actionError = nil }
            }
        )
    }

    // MARK: 动作

    @MainActor
    private func load() async {
        isLoading = true
        errorText = nil

        let result = await model.snapshots()
        items = result.items
        unavailable = !result.available
        // 接口在但读失败时把原因显示出来；接口不存在的说明由空状态负责
        errorText = result.available ? result.message : nil

        isLoading = false
    }

    @MainActor
    private func performDelete(_ item: SnapshotItem) async {
        busyId = item.id
        do {
            try await model.deleteSnapshot(item)
            await load()
        } catch {
            actionError = error.localizedDescription
        }
        busyId = nil
    }

    @MainActor
    private func performRollback(_ item: SnapshotItem) async {
        busyId = item.id
        do {
            try await model.rollbackSnapshot(item)
            await load()
        } catch {
            actionError = error.localizedDescription
        }
        busyId = nil
    }
}

// MARK: - 新建

/// 新建快照：选快照文件夹 + 备注
///
/// 目标必须来自 `snapshotFolders`（`folderType|folderId`），不能用卷名——
/// 快照按文件夹创建，卷名匹配不到时会被服务端落到别的文件夹上。
struct CreateSnapshotSheet: View {
    let model: AppModel

    @Environment(\.dismiss) private var dismiss

    @State private var folderId = ""
    @State private var remark = ""
    @State private var isSaving = false
    @State private var isLoadingFolders = false
    @State private var errorText: String?

    private var folders: [SnapshotFolder] { model.snapshotFolders }

    private var selectedFolder: SnapshotFolder? {
        folders.first { $0.id == folderId }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if folders.isEmpty {
                        if isLoadingFolders {
                            HStack(spacing: 8) {
                                ProgressView()
                                Text("正在读取可快照的文件夹…")
                                    .font(.footnote)
                                    .foregroundStyle(.secondary)
                            }
                        } else {
                            EmptyHint(text: "没有可快照的文件夹（需要 Btrfs 文件系统，且至少有一个共享文件夹或家目录）")
                        }
                    } else {
                        Picker("目标文件夹", selection: $folderId) {
                            ForEach(folders) { folder in
                                Text("\(folder.displayName) · \(folder.typeLabel)")
                                    .tag(folder.id)
                            }
                        }
                    }
                    TextField("备注（可留空）", text: $remark)
                } header: {
                    Text("新建快照")
                } footer: {
                    Text("快照按文件夹创建，会占用所在卷的空间，耗时与数据量相关。")
                }

                if let errorText {
                    Section {
                        Text(errorText)
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("新建快照")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("取消") { dismiss() }
                        .disabled(isSaving)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await create() }
                    } label: {
                        if isSaving {
                            ProgressView()
                        } else {
                            Text("创建")
                        }
                    }
                    .disabled(isSaving || selectedFolder == nil)
                }
            }
            .task {
                if model.snapshotFolders.isEmpty {
                    isLoadingFolders = true
                    _ = await model.snapshots()
                    isLoadingFolders = false
                }
                if folderId.isEmpty {
                    folderId = model.snapshotFolders.first?.id ?? ""
                }
            }
        }
    }

    @MainActor
    private func create() async {
        guard let folder = selectedFolder else { return }
        isSaving = true
        errorText = nil
        do {
            try await model.createSnapshot(folder: folder, remark: remark)
            dismiss()
        } catch {
            errorText = error.localizedDescription
        }
        isSaving = false
    }
}
