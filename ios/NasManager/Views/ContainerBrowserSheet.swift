import SwiftUI
import WebKit

/// 在应用内打开容器 Web UI 的面板。
struct ContainerBrowserSheet: View {
    let containerName: String
    let nasHost: String
    let candidates: [ContainerWebCandidate]
    let onClose: () -> Void

    @State private var state = ContainerBrowserState()
    @State private var selectedCandidate: ContainerWebCandidate
    @State private var showingShare = false
    @State private var toast: String?

    init(
        containerName: String,
        nasHost: String,
        candidates: [ContainerWebCandidate],
        startAt: ContainerWebCandidate,
        onClose: @escaping () -> Void
    ) {
        self.containerName = containerName
        self.nasHost = nasHost
        self.candidates = candidates
        self.onClose = onClose
        _selectedCandidate = State(initialValue: startAt)
    }

    var body: some View {
        NavigationStack {
            ZStack(alignment: .top) {
                ContainerBrowserView(
                    url: selectedCandidate.url,
                    nasHost: nasHost,
                    state: state
                )
                .ignoresSafeArea(edges: .bottom)

                if state.isLoading, state.progress < 1 {
                    ProgressView(value: state.progress)
                        .progressViewStyle(.linear)
                        .tint(.accentColor)
                }

                if let error = state.errorText {
                    errorBanner(error)
                }
            }
            .navigationTitle(navigationTitle)
            .navigationBarTitleDisplayMode(.inline)
            .safeAreaInset(edge: .top, spacing: 0) { addressBar }
            .toolbar { toolbarContent }
            .sheet(isPresented: $showingShare) {
                if let url = state.finishedDownload {
                    ShareSheet(items: [url])
                }
            }
            .overlay(alignment: .bottom) {
                if let toast {
                    Text(toast)
                        .font(.footnote)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(.ultraThinMaterial, in: Capsule())
                        .padding(.bottom, 24)
                }
            }
        }
    }

    // MARK: 顶部

    private var navigationTitle: String {
        let title = state.pageTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        if !title.isEmpty, title != selectedCandidate.url.absoluteString {
            return title
        }
        return containerName
    }

    private var addressBar: some View {
        HStack(spacing: 8) {
            Image(systemName: selectedCandidate.isSecure ? "lock.fill" : "lock.open.fill")
                .font(.caption2)
                .foregroundStyle(selectedCandidate.isSecure ? Color.green : Color.secondary)

            // 一个容器可能映射多个端口，主端口不是 Web UI 时能换一个试
            if candidates.count > 1 {
                Menu {
                    ForEach(candidates) { candidate in
                        Button {
                            guard candidate != selectedCandidate else { return }
                            selectedCandidate = candidate
                            state.errorText = nil
                        } label: {
                            if candidate == selectedCandidate {
                                Label(candidate.url.absoluteString, systemImage: "checkmark")
                            } else {
                                Text(candidate.url.absoluteString)
                            }
                        }
                    }
                } label: {
                    addressLabel
                }
            } else {
                addressLabel
            }

            Spacer(minLength: 0)

            Button {
                UIPasteboard.general.string = selectedCandidate.url.absoluteString
                showToast("已复制链接")
            } label: {
                Image(systemName: "doc.on.doc")
                    .font(.caption)
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(.bar)
        .overlay(alignment: .bottom) { Divider() }
    }

    private var addressLabel: some View {
        Text(displayAddress)
            .font(.caption)
            .lineLimit(1)
            .truncationMode(.middle)
            .foregroundStyle(.primary)
    }

    private var displayAddress: String {
        let url = state.currentURL ?? selectedCandidate.url
        guard let host = url.host else { return selectedCandidate.url.absoluteString }
        let port = url.port.map { ":\($0)" } ?? ""
        let path = url.path.isEmpty ? "/" : url.path
        return "\(host)\(port)\(path)"
    }

    // MARK: 工具栏

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) {
            Button("完成") { onClose() }
        }
        ToolbarItemGroup(placement: .bottomBar) {
            Button {
                state.goBack()
            } label: {
                Image(systemName: "chevron.backward")
            }
            .disabled(!state.canGoBack)

            Spacer()

            Button {
                state.goForward()
            } label: {
                Image(systemName: "chevron.forward")
            }
            .disabled(!state.canGoForward)

            Spacer()

            Button {
                if state.isLoading {
                    state.stop()
                } else {
                    state.reload()
                }
            } label: {
                Image(systemName: state.isLoading ? "xmark" : "arrow.clockwise")
            }

            Spacer()

            Button {
                UIApplication.shared.open(state.currentURL ?? selectedCandidate.url)
            } label: {
                Image(systemName: "safari")
            }

            Spacer()

            Button {
                if state.finishedDownload != nil { showingShare = true }
            } label: {
                Image(systemName: "square.and.arrow.down")
            }
            .disabled(state.finishedDownload == nil)
        }
    }

    private func errorBanner(_ text: String) -> some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                Text(text)
                    .font(.footnote)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button {
                    state.errorText = nil
                } label: {
                    Image(systemName: "xmark")
                        .font(.caption2)
                }
            }
            .padding(10)
            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .padding(.horizontal, 12)
            .padding(.top, 8)
            Spacer()
        }
    }

    private func showToast(_ text: String) {
        toast = text
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            toast = nil
        }
    }
}

/// 下载完成后的系统分享面板
private struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
