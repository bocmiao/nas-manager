import Foundation
import SwiftUI

/// 镜像管理：列表 / 滑动删除 / 拉取。由容器页 toolbar 推入。
struct ImagesView: View {
    let model: AppModel

    @State private var images: [DockerImage] = []
    @State private var isLoading = true
    @State private var errorText: String?
    @State private var actionError: String?
    @State private var busyId: String?
    @State private var showingPull = false

    var body: some View {
        List {
            Section {
                if images.isEmpty {
                    if isLoading {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("读取镜像列表…")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                    } else {
                        EmptyHint(text: "本地没有镜像，或该固件未开放镜像接口")
                    }
                }

                ForEach(images) { image in
                    row(image)
                }
                .onDelete(perform: delete)
            } header: {
                Text("本地镜像（\(images.count)）")
            } footer: {
                Text("左滑删除镜像。正被容器使用的镜像可能被服务端拒绝删除。")
            }

            if let errorText {
                Section {
                    Label(errorText, systemImage: "exclamationmark.triangle.fill")
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
            }
        }
        .navigationTitle("镜像")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task(id: showingPull) {
            // 首次进入与拉取面板关闭后都刷新一次
            if !showingPull { await load() }
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    showingPull = true
                } label: {
                    Image(systemName: "arrow.down.circle")
                }
            }
        }
        .sheet(isPresented: $showingPull) {
            PullImageSheet(model: model)
        }
        .alert("操作失败", isPresented: actionErrorBinding) {
            Button("好", role: .cancel) { actionError = nil }
        } message: {
            Text(actionError ?? "")
        }
    }

    // MARK: 行

    private func row(_ image: DockerImage) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(image.repository.isEmpty ? image.reference : image.repository)
                        .font(.subheadline.weight(.medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    StatusBadge(text: image.tag, tint: .secondary)
                    Spacer(minLength: 0)
                }

                Text(image.shortId)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)

                HStack(spacing: 10) {
                    Label(Format.bytes(image.size), systemImage: "externaldrive")
                    if let count = image.containerCount {
                        Label("\(count) 个容器", systemImage: "shippingbox")
                    }
                }
                .font(.caption2)
                .foregroundStyle(.secondary)

                if let created = image.createdAt {
                    Text("创建 \(created)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }

            if busyId == image.id {
                ProgressView()
                    .scaleEffect(0.7)
            }
        }
        .padding(.vertical, 2)
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
        do {
            images = try await model.images()
        } catch {
            errorText = error.localizedDescription
        }
        isLoading = false
    }

    private func delete(at offsets: IndexSet) {
        let targets = offsets.compactMap { index in
            images.indices.contains(index) ? images[index] : nil
        }
        guard !targets.isEmpty else { return }
        Task { await performDelete(targets) }
    }

    @MainActor
    private func performDelete(_ targets: [DockerImage]) async {
        for image in targets {
            busyId = image.id
            do {
                try await model.deleteImage(image)
            } catch {
                actionError = error.localizedDescription
            }
        }
        busyId = nil
        await load()
    }
}

// MARK: - 拉取

/// 拉取镜像。服务端不返回进度，这里按「提交 → 等镜像出现在列表 → 完成」展示阶段，
/// 不编造百分比。
struct PullImageSheet: View {
    let model: AppModel

    @Environment(\.dismiss) private var dismiss

    @State private var reference = ""
    @State private var isPulling = false
    @State private var stage: Stage?
    @State private var message: String?
    @State private var errorText: String?

    private enum Stage: String, CaseIterable {
        case submit = "提交拉取请求"
        case wait = "等待镜像出现在本地列表"
        case done = "完成"
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("仓库:标签", text: $reference)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .disabled(isPulling)
                } header: {
                    Text("拉取镜像")
                } footer: {
                    Text("例如 nginx:latest。耗时取决于镜像大小与网络。")
                }

                Section {
                    Button {
                        Task { await start() }
                    } label: {
                        HStack(spacing: 8) {
                            if isPulling { ProgressView() }
                            Text(isPulling ? "拉取中…" : "开始拉取")
                        }
                    }
                    .disabled(isPulling || trimmedReference.isEmpty)
                }

                if isPulling || message != nil || errorText != nil {
                    Section("进度") {
                        ForEach(Array(Stage.allCases.indices), id: \.self) { index in
                            stageRow(Stage.allCases[index], index: index)
                        }
                        if let message {
                            Text(message)
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                        if let errorText {
                            Text(errorText)
                                .font(.footnote)
                                .foregroundStyle(.red)
                        }
                    }
                }
            }
            .navigationTitle("拉取镜像")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("完成") { dismiss() }
                        .disabled(isPulling)
                }
            }
        }
    }

    private var trimmedReference: String {
        reference.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    @ViewBuilder
    private func stageRow(_ item: Stage, index: Int) -> some View {
        let currentIndex = stage.flatMap { value in
            Stage.allCases.firstIndex(of: value)
        }
        HStack(spacing: 8) {
            if let currentIndex, index < currentIndex {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
            } else if stage == item {
                if isPulling {
                    ProgressView()
                        .scaleEffect(0.7)
                } else if errorText != nil {
                    Image(systemName: "exclamationmark.circle.fill")
                        .foregroundStyle(.red)
                } else {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                }
            } else {
                Image(systemName: "circle")
                    .foregroundStyle(.tertiary)
            }
            Text(item.rawValue)
                .font(.subheadline)
        }
    }

    @MainActor
    private func start() async {
        let spec = trimmedReference
        guard !spec.isEmpty else { return }

        isPulling = true
        errorText = nil
        message = nil
        stage = .submit

        do {
            try await model.pullImage(spec)
            stage = .wait
            message = "请求已提交，正在等待镜像出现在本地列表…"
            if await waitForImage(spec) {
                stage = .done
                message = "已拉取 \(spec)"
            } else {
                message = "请求已提交，但列表里还没有该镜像，可能仍在后台下载，稍后回到列表下拉刷新。"
            }
        } catch {
            errorText = error.localizedDescription
        }
        isPulling = false
    }

    /// 轮询本地列表，最多 10 次 × 3 秒
    @MainActor
    private func waitForImage(_ spec: String) async -> Bool {
        for _ in 0..<10 {
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            if Task.isCancelled { return false }
            guard let list = try? await model.images() else { continue }
            if list.contains(where: { PullImageSheet.matches($0, spec: spec) }) {
                return true
            }
        }
        return false
    }

    /// 粗略比对："nginx" 命中 "nginx:latest" 或 "docker.io/library/nginx:latest"
    static func matches(_ image: DockerImage, spec: String) -> Bool {
        let target = spec.lowercased()
        let reference = image.reference.lowercased()
        if reference == target { return true }
        if reference.hasSuffix("/" + target) { return true }
        if !target.contains(":") {
            let withTag = target + ":latest"
            return reference == withTag || reference.hasSuffix("/" + withTag)
        }
        return false
    }
}
