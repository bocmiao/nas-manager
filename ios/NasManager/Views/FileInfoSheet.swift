import SwiftUI

/// 条目详情。
///
/// 文件的大小在列表里已经有了；**目录的体积 NAS 不随列表返回**，
/// 要单独问 `getSizeAndCount`，而且是**增量扫描**——同一目录连续问，
/// 数字会一路涨，没有「算完了」的标志。所以这里边算边显示，
/// 数字稳定下来才把「计算中」摘掉。
struct FileInfoSheet: View {
    let model: AppModel
    let entry: FileEntry

    @Environment(\.dismiss) private var dismiss

    @State private var folderSize: FileFolderSize?
    @State private var measured = false
    @State private var settled = false
    @State private var errorText: String?

    var body: some View {
        NavigationStack {
            List {
                Section("基本") {
                    labeled("名称", entry.name)
                    labeled("类型", typeText)
                    if !entry.isDir {
                        labeled("大小", entry.sizeText)
                    }
                    labeled("修改时间", entry.mtimeText)
                }

                if entry.isDir {
                    sizeSection
                }

                Section("路径") {
                    Text(entry.path)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }

                if entry.isDir {
                    Section {
                        Text("目录体积由 NAS 现算，越大越慢。数字稳定前会一直涨，属正常现象。")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("详情")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("关闭") { dismiss() }
                }
            }
            .task { await measure() }
        }
    }

    @ViewBuilder
    private var sizeSection: some View {
        Section("体积") {
            if let errorText {
                Text(errorText)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } else if let folderSize {
                HStack {
                    Text(measured && !settled ? "正在计算…" : "总大小")
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 12)
                    if measured && !settled {
                        ProgressView().scaleEffect(0.7)
                    }
                    Text(folderSize.sizeText).monospacedDigit()
                }
                HStack {
                    Text("实际占用").foregroundStyle(.secondary)
                    Spacer(minLength: 12)
                    Text(folderSize.takesSpaceText).monospacedDigit()
                }
                HStack {
                    Text("文件 / 文件夹").foregroundStyle(.secondary)
                    Spacer(minLength: 12)
                    Text("\(folderSize.fileCount) / \(folderSize.dirCount)").monospacedDigit()
                }
            } else {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("正在计算…").font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func labeled(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .multilineTextAlignment(.trailing)
                .lineLimit(2)
        }
    }

    private var typeText: String {
        if entry.isTag { return "标签" }
        if entry.isDir {
            return entry.isMount ? "挂载点" : "文件夹"
        }
        if let ext = entry.ext, !ext.isEmpty {
            return "文件 · \(ext.uppercased())"
        }
        return "文件"
    }

    @MainActor
    private func measure() async {
        guard entry.isDir else { return }
        do {
            let result = try await model.fileFolderSize(path: entry.path)
            folderSize = result.size
            settled = result.settled
            measured = true
        } catch {
            errorText = error.localizedDescription
            measured = true
        }
    }
}
