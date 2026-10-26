import SwiftUI

/// 根视图：锁定时是解锁页，未连接时进入连接页，连上后是五页 TabView
/// （总览 / 文件 / 容器 / 存储 / 设置；告警在总览页工具栏）
struct RootView: View {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if model.isLocked {
                LockView(model: model)
            } else if model.isReady {
                mainTabs
            } else {
                ConnectView(model: model)
            }
        }
        .alert(
            "服务器证书指纹已变化",
            isPresented: certificateAlertBinding,
            presenting: model.certificateWarning
        ) { _ in
            Button("接受新指纹并重连") {
                Task { await model.acceptNewFingerprint() }
            }
            Button("取消", role: .cancel) {
                model.clearCertificateWarning()
            }
        } message: { warning in
            Text(certificateMessage(for: warning))
        }
        .task {
            let granted = await AlertNotifier.shared.requestAuthorization()
            model.notificationDenied = !granted
        }
        .onChange(of: scenePhase) { _, phase in
            // 回到前台时按需上锁（lockIfNeeded 内部有 30 秒宽限）
            if phase == .active {
                model.lockIfNeeded()
            }
        }
    }

    private var mainTabs: some View {
        TabView {
            OverviewView(model: model)
                .tabItem { Label("总览", systemImage: "gauge") }

            // 文件管理是这 App 里功能最厚的一块（上传下载 / 复制移动 / 压缩解压 /
            // 缩略图预览），之前藏在「设置 → 运维工具」三层，现在提到一级。
            // 为了不撑出第 6 个 Tab（iOS 会把后两个折进「更多」），
            // 告警改成总览页工具栏的铃铛入口。
            NavigationStack {
                FilesView(model: model)
            }
            .tabItem { Label("文件", systemImage: "folder") }

            ContainersView(model: model)
                .tabItem { Label("容器", systemImage: "shippingbox") }

            StorageView(model: model)
                .tabItem { Label("存储", systemImage: "internaldrive") }

            SettingsView(model: model)
                .tabItem { Label("设置", systemImage: "gearshape") }
        }
    }

    private var certificateAlertBinding: Binding<Bool> {
        Binding(
            get: { model.certificateWarning != nil },
            set: { visible in
                if !visible {
                    model.clearCertificateWarning()
                }
            }
        )
    }

    private func certificateMessage(for warning: CertificateWarning) -> String {
        """
        主机 \(warning.host):\(warning.port) 返回的证书与首次连接时记录的不一致。

        原指纹：\(shortFingerprint(warning.expected))
        新指纹：\(shortFingerprint(warning.actual))

        如果刚更换过证书或重装过系统，可以接受；否则请先确认网络环境安全。
        """
    }

    private func shortFingerprint(_ value: String) -> String {
        if value.count <= 32 { return value }
        let prefix = String(value.prefix(32))
        return prefix + "…"
    }
}
