import Foundation

/// 一个候选接入地址
struct AddressCandidate {
    let endpoint: UgosEndpoint
    /// "内网" / "公网"
    let label: String
}

/// 双地址探测：谁先响应就用谁。
///
/// 判据是「能否拿到任意 HTTP 响应」——UGOS 对无 token 的请求照样回 200 + code 1010，
/// 所以只要 TCP/TLS 通了就算可达，不需要真的登录成功。
enum EndpointResolver {
    static func resolve(_ candidates: [AddressCandidate], timeout: TimeInterval = 4) async -> AddressCandidate? {
        for candidate in candidates {
            if await isReachable(candidate.endpoint, timeout: timeout) {
                return candidate
            }
        }
        return nil
    }

    static func isReachable(_ endpoint: UgosEndpoint, timeout: TimeInterval = 4) async -> Bool {
        guard let base = endpoint.baseURL else { return false }
        guard let url = URL(string: base.absoluteString + "/ugreen/v1/verify/sn") else { return false }

        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.timeoutIntervalForResource = timeout
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpCookieStorage = nil

        let delegate = TOFUTrustDelegate()
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }

        do {
            let (_, response) = try await session.data(for: URLRequest(url: url))
            return response is HTTPURLResponse
        } catch {
            return false
        }
    }
}
