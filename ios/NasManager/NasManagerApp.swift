import SwiftUI

@main
struct NasManagerApp: App {
    init() {
        // 注册通知代理，让 App 在前台也能弹出告警横幅
        AlertNotifier.shared.activate()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}
