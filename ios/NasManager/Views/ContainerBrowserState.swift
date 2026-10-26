import Foundation
import Observation
import WebKit

/// 内置浏览器面板的共享状态。
///
/// 刻意不整体标 `@MainActor`（项目统一约定），只给需要碰 WKWebView 的成员函数加标注。
///
/// `isLoading` **不由 KVO 驱动**：WKWebView 的 objc 属性名是 `loading`、getter 才是
/// `isLoading`，Swift 导入后 KeyPath 变成 "isLoading"，与真实 KVO key 对不上，
/// 注册会直接抛 "not key value coding-compliant" 崩溃。改由导航回调维护。
@Observable
final class ContainerBrowserState {
    var progress: Double = 0
    var pageTitle: String = ""
    var currentURL: URL?
    var canGoBack = false
    var canGoForward = false
    var isLoading = false
    /// 下载完成后的本地文件，用来弹分享面板
    var finishedDownload: URL?
    var errorText: String?

    @ObservationIgnored weak var webView: WKWebView?

    @MainActor func goBack() { webView?.goBack() }

    @MainActor func goForward() { webView?.goForward() }

    @MainActor func reload() { webView?.reload() }

    @MainActor func stop() {
        webView?.stopLoading()
        // 停止不保证触发 didFail，这里自己收尾，避免进度条卡在中间
        isLoading = false
    }
}
