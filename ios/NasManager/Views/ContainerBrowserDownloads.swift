import UIKit
import WebKit

/// 下载通道：容器面板里的「导出配置」「下载种子」这类响应必须走这里，
/// 否则 WebView 只会给一片白。
extension ContainerBrowserCoordinator {
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        decisionHandler(navigationAction.shouldPerformDownload ? .download : .allow)
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        // 容器面板里的「导出配置」「下载种子」这类非网页响应交给下载通道，
        // 否则 WebView 只会白屏
        decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download)
    }

    func webView(
        _ webView: WKWebView,
        navigationAction: WKNavigationAction,
        didBecome download: WKDownload
    ) {
        download.delegate = self
    }

    func webView(
        _ webView: WKWebView,
        navigationResponse: WKNavigationResponse,
        didBecome download: WKDownload
    ) {
        download.delegate = self
    }

    func download(
        _ download: WKDownload,
        decideDestinationUsing response: URLResponse,
        suggestedFilename: String,
        completionHandler: @escaping (URL?) -> Void
    ) {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("NasManagerDownloads", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let name = suggestedFilename.isEmpty ? "download" : suggestedFilename
        let destination = uniqueURL(in: dir, name: name)
        downloadDestinations[download] = destination
        completionHandler(destination)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let destination = downloadDestinations[download] else { return }
        downloadDestinations[download] = nil
        publish { $0.finishedDownload = destination }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        downloadDestinations[download] = nil
        publish { $0.errorText = "下载失败：\(error.localizedDescription)" }
    }

    private func uniqueURL(in dir: URL, name: String) -> URL {
        var candidate = dir.appendingPathComponent(name)
        var index = 1
        let base = (name as NSString).deletingPathExtension
        let ext = (name as NSString).pathExtension
        while FileManager.default.fileExists(atPath: candidate.path) {
            let suffix = ext.isEmpty ? "\(base)(\(index))" : "\(base)(\(index)).\(ext)"
            candidate = dir.appendingPathComponent(suffix)
            index += 1
        }
        return candidate
    }
}
