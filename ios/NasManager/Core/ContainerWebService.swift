import Foundation

/// 容器 Web UI 的一个候选访问地址。
struct ContainerWebCandidate: Identifiable, Hashable {
    /// 宿主机（NAS）端口
    let port: Int
    /// `http` / `https`
    let scheme: String
    let url: URL

    var id: String { url.absoluteString }
    var isSecure: Bool { scheme == "https" }
}

/// 端口探测结论。
enum ContainerWebProbeResult: Equatable {
    /// 是网页服务，可以直接打开
    case web(scheme: String, statusCode: Int)
    /// 端口通，但回的不是 HTTP——比如 Minecraft（TCP 协议）、V2Ray 这类
    case notWebService(String)
    /// 连不上：容器没运行 / 端口没映射 / 当前网络方向不可达（外网多半是没做端口转发）
    case unreachable(String)

    var isWeb: Bool {
        if case .web = self { return true }
        return false
    }

    /// 给用户看的说明
    var summary: String {
        switch self {
        case let .web(scheme, code):
            return "\(scheme.uppercased()) 服务可用（HTTP \(code)）"
        case let .notWebService(reason):
            return reason
        case let .unreachable(reason):
            return reason
        }
    }
}

/// 容器 Web 访问地址推导 + 可达性探测。
///
/// 为什么需要探测：`portMapping` 只告诉我们「哪个宿主端口映射到容器」，
/// **不告诉我们该端口跑的是不是 HTTP**。实测同一台设备上既有能返回 HTML 的
/// Web UI（如 13888 返回 200 + text/html），也有纯 TCP 服务（如 25565 是
/// Minecraft 协议），后者塞进 WebView 只会白屏。所以打开前先问一句。
enum ContainerWebProbe {
    /// 单次探测的超时。容器都在同一台 NAS 上，正常内网 1 秒内就有回音，
    /// 给 5 秒是为了容忍外网绕路 + 冷启动。
    static let defaultTimeout: TimeInterval = 5

    /// 探测某个宿主端口是不是网页服务。
    ///
    /// 顺序是 http → https：容器默认几乎都是明文 http，先试大概率一次命中。
    static func probe(
        host: String,
        port: Int,
        timeout: TimeInterval = defaultTimeout
    ) async -> ContainerWebProbeResult {
        guard port > 0, port <= 65535 else {
            return .unreachable("端口 \(port) 不合法")
        }

        let httpAttempt = await attempt(scheme: "http", host: host, port: port, timeout: timeout)
        if case let .responded(code) = httpAttempt {
            return .web(scheme: "http", statusCode: code)
        }

        // http 没成，可能是 https-only、也可能压根不是 HTTP，两种都再试一次 https
        let httpsAttempt = await attempt(scheme: "https", host: host, port: port, timeout: timeout)
        if case let .responded(code) = httpsAttempt {
            return .web(scheme: "https", statusCode: code)
        }

        // 两个都没拿到 HTTP 响应，按「有没有人监听」区分结论
        if case .notHTTP = httpAttempt {
            return .notWebService("该端口有服务在监听，但返回的不是网页，无法在应用内打开")
        }
        if case let .failed(reason) = httpAttempt {
            return .unreachable(reason)
        }
        return .unreachable("无法连接 \(host):\(port)")
    }

    // MARK: - 内部

    private enum Attempt {
        case responded(Int)
        /// TCP 通了但拿不到合法 HTTP 响应
        case notHTTP
        case failed(String)
    }

    private static func attempt(
        scheme: String,
        host: String,
        port: Int,
        timeout: TimeInterval
    ) async -> Attempt {
        let hostPart = EndpointResolver.bracketIfNeeded(host)
        guard let url = URL(string: "\(scheme)://\(hostPart):\(port)/") else {
            return .failed("地址不合法")
        }

        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.timeoutIntervalForResource = timeout
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpCookieStorage = nil
        config.urlCredentialStorage = nil
        config.waitsForConnectivity = false

        let delegate = ProbeTrustDelegate()
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        // 只要状态行和响应头就够判断，不必拉整个首页
        request.setValue("bytes=0-0", forHTTPHeaderField: "Range")
        request.setValue("NasManager-Probe/1.0", forHTTPHeaderField: "User-Agent")

        do {
            let (_, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse {
                return .responded(http.statusCode)
            }
            return .notHTTP
        } catch let error as URLError {
            return classify(error)
        } catch {
            return .failed(error.localizedDescription)
        }
    }

    private static func classify(_ error: URLError) -> Attempt {
        switch error.code {
        case .cannotConnectToHost, .cannotFindHost, .timedOut, .networkConnectionLost,
             .notConnectedToInternet, .dataNotAllowed, .internationalRoamingOff:
            return .failed("连接不上，端口未开放或容器未运行")
        case .cannotParseResponse, .badServerResponse:
            // 有人在监听，但说的不是 HTTP
            return .notHTTP
        case .secureConnectionFailed, .serverCertificateUntrusted,
             .serverCertificateHasBadDate, .serverCertificateNotYetValid,
             .serverCertificateHasUnknownRoot, .clientCertificateRejected:
            // TLS 层面的事，交给上层换 https 再试
            return .failed("TLS 握手失败")
        case .appTransportSecurityRequiresSecureConnection:
            return .failed("被传输安全策略拦截")
        default:
            return .failed(error.localizedDescription)
        }
    }
}

/// 探测专用：只为「判断是不是 HTTP 服务」而放行自签名证书。
///
/// 这里不做固定指纹，是因为探测**不加载页面内容、不发送任何凭据**，
/// 只读状态码；真正加载 Web UI 时用的是 `ContainerWebView` 里带确认的信任逻辑。
private final class ProbeTrustDelegate: NSObject, URLSessionDelegate {
    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

/// 把容器 + 当前 NAS 地址拼成可尝试的访问地址。
enum ContainerWebResolver {
    /// 优先顺序：http 在前（容器默认明文），https 兜底。
    static func candidates(for container: Container, host: String) -> [ContainerWebCandidate] {
        guard !host.isEmpty else { return [] }
        let hostPart = EndpointResolver.bracketIfNeeded(host)
        var seen = Set<Int>()
        var out: [ContainerWebCandidate] = []

        for port in container.webCandidatePorts where seen.insert(port.nasPort).inserted {
            for scheme in ["http", "https"] {
                guard let url = URL(string: "\(scheme)://\(hostPart):\(port.nasPort)/") else { continue }
                out.append(ContainerWebCandidate(port: port.nasPort, scheme: scheme, url: url))
            }
        }
        return out
    }

    /// 该容器为什么打不开——给 UI 做禁用说明用。返回 nil 表示「有端口可以试」。
    static func blockReason(for container: Container) -> String? {
        if !container.isRunning {
            return "容器未运行，启动后才能打开"
        }
        if container.webCandidatePorts.isEmpty {
            return "该容器没有映射 TCP 端口，无法从外部访问"
        }
        return nil
    }
}

/// 「打开容器应用」这一步的结论。
enum ContainerWebOpenOutcome {
    /// 探测通过，可以打开。`all` 带上其它候选地址，供面板内切换端口。
    case ready(ContainerWebCandidate, all: [ContainerWebCandidate])
    /// 端口有服务但不是网页
    case notWeb(String)
    /// 连不上
    case unreachable(String)
    /// 结构性原因（容器没运行 / 没映射端口），不需要探测
    case blocked(String)
}

extension ContainerWebResolver {
    /// 先探再开：容器端口不等于网页端口，直接塞进 WebView 会是白屏。
    ///
    /// 多个映射端口时按顺序试，第一个探测成功的就用它——用户装的多端口容器
    /// （比如面板 + 代理）常常只有其中一个是真的 Web UI。
    static func openTarget(
        for container: Container,
        host: String,
        timeout: TimeInterval = 4
    ) async -> ContainerWebOpenOutcome {
        if let reason = blockReason(for: container) {
            return .blocked(reason)
        }
        let all = candidates(for: container, host: host)
        guard !all.isEmpty else {
            return .blocked("没有可用的访问地址")
        }

        var sawNotWeb = false
        var lastUnreachable = "无法连接该端口"

        for port in container.webCandidatePorts {
            switch await ContainerWebProbe.probe(host: host, port: port.nasPort, timeout: timeout) {
            case let .web(scheme, _):
                if let match = all.first(where: { $0.port == port.nasPort && $0.scheme == scheme }) {
                    return .ready(match, all: all)
                }
            case let .notWebService(text):
                sawNotWeb = true
                lastUnreachable = text
            case let .unreachable(text):
                lastUnreachable = text
            }
        }

        if sawNotWeb {
            return .notWeb("该端口有服务在监听，但返回的不是网页，无法在应用内打开")
        }
        return .unreachable(lastUnreachable)
    }
}
