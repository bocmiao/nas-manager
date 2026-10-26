import UIKit
import WebKit

/// WKWebView 的导航 / UI / 下载三合一代理。
///
/// 容器面板（青龙、qBittorrent、Halo 后台）在 WebView 里「能看不能用」通常是三个原因，
/// 这里逐个补齐：JS 弹窗不实现 → 按钮点了没反应；`createWebViewWith` 不实现 →
/// `target="_blank"` 没反应；下载策略不实现 → 导出文件白屏。
final class ContainerBrowserCoordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    var loadedURL: URL?

    private weak var state: ContainerBrowserState?
    private var nasHost: String = ""
    private var observations: [NSKeyValueObservation] = []
    private var downloadDestinations: [WKDownload: URL] = [:]

    func attach(webView: WKWebView, state: ContainerBrowserState, nasHost: String) {
        self.state = state
        self.nasHost = nasHost

        // 不监听 isLoading —— 见 ContainerBrowserState 里的说明，KeyPath 与 KVO key 对不上
        observations = [
            webView.observe(\.estimatedProgress, options: [.new]) { [weak self] view, _ in
                let value = view.estimatedProgress
                self?.publish { $0.progress = value }
            },
            webView.observe(\.title, options: [.new]) { [weak self] view, _ in
                let title = view.title ?? ""
                self?.publish { $0.pageTitle = title }
            },
            webView.observe(\.url, options: [.new]) { [weak self] view, _ in
                let url = view.url
                self?.publish { $0.currentURL = url }
            },
            webView.observe(\.canGoBack, options: [.new]) { [weak self] view, _ in
                let value = view.canGoBack
                self?.publish { $0.canGoBack = value }
            },
            webView.observe(\.canGoForward, options: [.new]) { [weak self] view, _ in
                let value = view.canGoForward
                self?.publish { $0.canGoForward = value }
            },
        ]
    }

    private func publish(_ mutate: @escaping (ContainerBrowserState) -> Void) {
        let apply = { [weak self] in
            guard let state = self?.state else { return }
            mutate(state)
        }
        if Thread.isMainThread {
            apply()
        } else {
            DispatchQueue.main.async(execute: apply)
        }
    }

    // MARK: - 证书

    /// 只对「当前 NAS 主机」放行自签名证书。
    ///
    /// 边界划在这里是有意的：容器 Web UI 普遍是自签名 https 或者明文 http，
    /// 不支持就一堆页面打不开；但页面内跳到外网 CDN 时仍然走系统默认校验，
    /// 不会因为这一条放行而让任意第三方证书被信任。
    func webView(
        _ webView: WKWebView,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              isNasHost(challenge.protectionSpace.host) else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    private func isNasHost(_ host: String) -> Bool {
        let trimmed = { (value: String) in
            value.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        }
        let right = trimmed(nasHost)
        return !right.isEmpty && trimmed(host) == right
    }

    // MARK: - 加载状态

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        publish {
            $0.errorText = nil
            $0.isLoading = true
            $0.progress = 0
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        publish {
            $0.isLoading = false
            $0.progress = 1
            $0.canGoBack = webView.canGoBack
            $0.canGoForward = webView.canGoForward
            $0.currentURL = webView.url
            $0.pageTitle = webView.title ?? ""
        }
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        handle(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        handle(error)
    }

    private func handle(_ error: Error) {
        let nsError = error as NSError
        // 用户主动取消（点了停止）不该报错
        guard nsError.code != NSURLErrorCancelled else {
            publish { $0.isLoading = false }
            return
        }
        let text: String
        switch nsError.code {
        case NSURLErrorCannotConnectToHost, NSURLErrorTimedOut, NSURLErrorCannotFindHost:
            text = "连接不上该端口。若在外部网络，可能需要在路由器上为这个端口做转发。"
        case NSURLErrorServerCertificateUntrusted, NSURLErrorSecureConnectionFailed:
            text = "证书校验失败。"
        default:
            text = nsError.localizedDescription
        }
        publish {
            $0.errorText = text
            $0.isLoading = false
        }
    }

    // MARK: - 新窗口

    /// `target="_blank"` 或 JS `window.open` 走这里。不实现就是「点了没反应」。
    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if navigationAction.targetFrame == nil, let url = navigationAction.request.url {
            webView.load(URLRequest(url: url))
        }
        return nil
    }
}

// MARK: - JS 弹窗

extension ContainerBrowserCoordinator {
    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        presentAlert(title: frame.request.url?.host ?? "提示", message: message) {
            completionHandler()
        }
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        guard let host = topViewController() else {
            // 拿不到宿主控制器也必须回调，否则页面永久卡死
            completionHandler(false)
            return
        }
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in completionHandler(false) })
        alert.addAction(UIAlertAction(title: "确定", style: .default) { _ in completionHandler(true) })
        host.present(alert, animated: true)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        guard let host = topViewController() else {
            completionHandler(nil)
            return
        }
        let alert = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
        alert.addTextField { field in field.text = defaultText }
        alert.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in completionHandler(nil) })
        alert.addAction(UIAlertAction(title: "确定", style: .default) { _ in
            completionHandler(alert.textFields?.first?.text)
        })
        host.present(alert, animated: true)
    }

    private func presentAlert(title: String, message: String, onDone: @escaping () -> Void) {
        guard let host = topViewController() else {
            onDone()
            return
        }
        let alert = UIAlertController(title: title, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "好", style: .default) { _ in onDone() })
        host.present(alert, animated: true)
    }

    private func topViewController() -> UIViewController? {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        let window = scenes.flatMap(\.windows).first { $0.isKeyWindow }
            ?? scenes.flatMap(\.windows).first
        var controller = window?.rootViewController
        while let presented = controller?.presentedViewController {
            controller = presented
        }
        return controller
    }
}
