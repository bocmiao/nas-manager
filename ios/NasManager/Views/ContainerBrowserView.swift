import SwiftUI
import WebKit

/// WKWebView 的 SwiftUI 承载。
struct ContainerBrowserView: UIViewRepresentable {
    let url: URL
    let nasHost: String
    let state: ContainerBrowserState

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        // 用 default 而不是 nonPersistent：容器面板的登录态靠 Cookie 维持，
        // 否则每次打开都要重新登录，体验会很差。
        config.websiteDataStore = .default()
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.preferences.javaScriptCanOpenWindowsAutomatically = true

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsLinkPreview = true
        webView.scrollView.keyboardDismissMode = .interactive
        webView.isOpaque = false
        webView.backgroundColor = .systemBackground

        state.webView = webView
        context.coordinator.attach(webView: webView, state: state, nasHost: nasHost)
        webView.load(URLRequest(url: url))
        context.coordinator.loadedURL = url
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        // 只有地址真的变了才重新加载，否则 updateUIView 每次刷新都会打断当前页面
        guard context.coordinator.loadedURL != url else { return }
        context.coordinator.loadedURL = url
        webView.load(URLRequest(url: url))
    }

    func makeCoordinator() -> ContainerBrowserCoordinator {
        ContainerBrowserCoordinator()
    }
}
