import Foundation
import SwiftUI

private enum ContainerAction {
    case start
    case stop
    case restart
}

/// 待打开的内置浏览器面板
private struct ContainerWebTarget: Identifiable {
    let containerName: String
    let nasHost: String
    let candidates: [ContainerWebCandidate]
    let start: ContainerWebCandidate

    var id: String { "\(containerName)#\(start.id)" }
}

/// 容器页：列表 + 启停/重启 + 日志 + 在应用内打开 Web UI
struct ContainersView: View {
    let model: AppModel

    @State private var keyword = ""
    @State private var filter = 0
    @State private var logTarget: Container?
    @State private var busyId: String?
    @State private var actionError: String?
    @State private var showingImages = false
    @State private var webTarget: ContainerWebTarget?
    @State private var probingId: String?
    @State private var webError: String?

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Picker("筛选", selection: $filter) {
                        Text("全部").tag(0)
                        Text("运行中").tag(1)
                        Text("已停止").tag(2)
                    }
                    .pickerStyle(.segmented)
                    .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))
                }

                Section {
                    if filteredContainers.isEmpty {
                        EmptyHint(text: model.dashboard == nil ? "尚未读取到容器" : "没有符合条件的容器")
                    }
                    ForEach(filteredContainers) { container in
                        containerRow(container)
                    }
                } header: {
                    Text("容器（\(filteredContainers.count) / \(allContainers.count)）")
                }

                if let error = model.errorMessage {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("容器")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $keyword, prompt: "搜索容器名或镜像")
            .refreshable { await model.reloadContainers() }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showingImages = true
                    } label: {
                        Image(systemName: "square.stack.3d.down.right")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await model.reloadContainers() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                }
            }
            .navigationDestination(isPresented: $showingImages) {
                ImagesView(model: model)
            }
            .sheet(item: $logTarget) { container in
                ContainerLogSheet(model: model, container: container)
            }
            .sheet(item: $webTarget) { target in
                ContainerBrowserSheet(
                    containerName: target.containerName,
                    nasHost: target.nasHost,
                    candidates: target.candidates,
                    startAt: target.start,
                    onClose: { webTarget = nil }
                )
            }
            .alert("操作失败", isPresented: actionErrorBinding) {
                Button("好", role: .cancel) {
                    actionError = nil
                }
            } message: {
                Text(actionError ?? "")
            }
            .alert("打不开这个容器", isPresented: webErrorBinding) {
                Button("好", role: .cancel) { webError = nil }
            } message: {
                Text(webError ?? "")
            }
        }
    }

    // MARK: 数据

    private var allContainers: [Container] {
        model.dashboard?.containers ?? []
    }

    private var filteredContainers: [Container] {
        var list = allContainers
        switch filter {
        case 1:
            list = list.filter { $0.isRunning }
        case 2:
            list = list.filter { !$0.isRunning }
        default:
            break
        }

        let query = keyword.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return list }
        return list.filter { container in
            let name = (container.name ?? "").lowercased()
            let image = (container.imageName ?? "").lowercased()
            return name.contains(query) || image.contains(query)
        }
    }

    private var actionErrorBinding: Binding<Bool> {
        Binding(
            get: { actionError != nil },
            set: { visible in
                if !visible { actionError = nil }
            }
        )
    }

    private var webErrorBinding: Binding<Bool> {
        Binding(
            get: { webError != nil },
            set: { visible in
                if !visible { webError = nil }
            }
        )
    }

    // MARK: 行

    private func containerRow(_ container: Container) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Text(container.name ?? "未命名")
                        .font(.subheadline.weight(.medium))
                        .lineLimit(1)
                    StatusBadge(
                        text: container.isRunning ? "运行中" : "已停止",
                        tint: container.isRunning ? .green : .gray
                    )
                    if busyId == container.containerId {
                        ProgressView()
                            .scaleEffect(0.7)
                    }
                    Spacer(minLength: 0)
                }

                HStack(spacing: 4) {
                    Text(container.imageName ?? "-")
                        .lineLimit(1)
                    if !container.ports.isEmpty {
                        Text("·")
                        Text(portSummary(container))
                    }
                }
                .font(.caption2)
                .foregroundStyle(.secondary)

                HStack(spacing: 12) {
                    Label(Format.percent(container.cpuPercent, digits: 1), systemImage: "cpu")
                    Label(Format.percent(container.memPercent, digits: 1), systemImage: "memorychip")
                    if let project = container.projectName, !project.isEmpty {
                        Label(project, systemImage: "square.stack.3d.up")
                            .lineLimit(1)
                    }
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
            }

            if canOpenInApp(container) {
                Button {
                    openInApp(container)
                } label: {
                    Group {
                        if probingId == container.id {
                            ProgressView()
                                .scaleEffect(0.7)
                        } else {
                            Image(systemName: "safari")
                                .font(.title3)
                        }
                    }
                    .frame(width: 26, height: 26)
                }
                .buttonStyle(.borderless)
                .tint(.accentColor)
                .disabled(probingId != nil)
                .accessibilityLabel("在应用内打开 \(container.name ?? "该容器")")
            }

            Menu {
                if canOpenInApp(container) {
                    Button {
                        openInApp(container)
                    } label: {
                        Label("在应用内打开", systemImage: "safari")
                    }
                } else if let reason = ContainerWebResolver.blockReason(for: container) {
                    Text(reason)
                }

                Button {
                    logTarget = container
                } label: {
                    Label("查看日志", systemImage: "doc.text.magnifyingglass")
                }

                if container.isRunning {
                    Button {
                        perform(.restart, on: container)
                    } label: {
                        Label("重启", systemImage: "arrow.clockwise")
                    }
                    Button(role: .destructive) {
                        perform(.stop, on: container)
                    } label: {
                        Label("停止", systemImage: "stop.fill")
                    }
                } else {
                    Button {
                        perform(.start, on: container)
                    } label: {
                        Label("启动", systemImage: "play.fill")
                    }
                }
            } label: {
                Image(systemName: "ellipsis.circle")
                    .font(.title3)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .onTapGesture {
            logTarget = container
        }
    }

    private func perform(_ action: ContainerAction, on container: Container) {
        busyId = container.containerId
        Task {
            do {
                switch action {
                case .start:
                    try await model.startContainer(container)
                case .stop:
                    try await model.stopContainer(container)
                case .restart:
                    try await model.restartContainer(container)
                }
            } catch {
                actionError = error.localizedDescription
            }
            busyId = nil
        }
    }

    // MARK: 在应用内打开

    /// 端口摘要：只列宿主机端口，容器内端口对「能不能打开」没意义
    private func portSummary(_ container: Container) -> String {
        let ports = container.ports.map { String($0.nasPort) }
        return ports.isEmpty ? "" : "端口 \(ports.joined(separator: ","))"
    }

    /// 有没有必要显示「打开」入口：必须有 Web 候选端口且容器在跑。
    /// 真正是不是网页服务要点了才知道（探测有成本，不适合列表里批量做）。
    private func canOpenInApp(_ container: Container) -> Bool {
        ContainerWebResolver.blockReason(for: container) == nil
    }

    private func openInApp(_ container: Container) {
        guard probingId == nil else { return }
        guard let host = model.connectedHost, !host.isEmpty else {
            webError = "尚未连接到 NAS，无法确定访问地址"
            return
        }

        probingId = container.id
        Task {
            let outcome = await ContainerWebResolver.openTarget(for: container, host: host)
            probingId = nil
            switch outcome {
            case let .ready(start, all):
                webTarget = ContainerWebTarget(
                    containerName: container.name ?? "容器",
                    nasHost: host,
                    candidates: all,
                    start: start
                )
            case let .notWeb(text):
                webError = "\(text)\n\n如果这个容器本身就不是网页应用，忽略这条提示即可。"
            case let .unreachable(text):
                webError = "\(text)\n\n当前连接的是 \(host)。如果在外部网络访问，需要在路由器上把这个端口转发到 NAS。"
            case let .blocked(text):
                webError = text
            }
        }
    }
}

// MARK: - 日志

struct ContainerLogSheet: View {
    let model: AppModel
    let container: Container

    @Environment(\.dismiss) private var dismiss
    @State private var lineCount = 200
    @State private var lines: [String] = []
    @State private var errorText: String?
    @State private var isLoading = false

    private let options = [100, 200, 500, 1000]

    var body: some View {
        NavigationStack {
            Group {
                if isLoading && lines.isEmpty {
                    ProgressView("读取日志…")
                } else if let errorText = errorText {
                    VStack(spacing: 10) {
                        Image(systemName: "exclamationmark.triangle")
                            .font(.title2)
                            .foregroundStyle(.orange)
                        Text(errorText)
                            .font(.footnote)
                            .multilineTextAlignment(.center)
                            .foregroundStyle(.secondary)
                        Button("重试") {
                            Task { await load() }
                        }
                        .buttonStyle(.bordered)
                    }
                    .padding()
                } else {
                    ScrollView {
                        // 全量日志可能有几十 MB，必须逐行懒加载，不能塞进一个 Text
                        LazyVStack(alignment: .leading, spacing: 2) {
                            ForEach(lines.indices, id: \.self) { index in
                                Text(lines[index])
                                    .font(.system(.caption, design: .monospaced))
                                    .textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                        .padding(12)
                    }
                    .refreshable { await load() }
                }
            }
            .navigationTitle(container.name ?? "容器日志")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Menu {
                        Picker("行数", selection: $lineCount) {
                            ForEach(options, id: \.self) { value in
                                Text("\(value) 行").tag(value)
                            }
                        }
                    } label: {
                        Label("\(lineCount) 行", systemImage: "text.alignleft")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("完成") { dismiss() }
                }
            }
            .task(id: lineCount) {
                await load()
            }
        }
    }

    private func load() async {
        isLoading = true
        errorText = nil
        do {
            let text = try await model.containerLogs(
                containerId: container.containerId ?? "",
                tail: lineCount
            )
            // 服务层已解帧并按 tail 截断，这里只拆成行给 LazyVStack 用
            let parsed = text.split(separator: "\n", omittingEmptySubsequences: true).map { String($0) }
            lines = parsed.isEmpty ? ["暂无日志"] : parsed
        } catch {
            errorText = error.localizedDescription
        }
        isLoading = false
    }
}
