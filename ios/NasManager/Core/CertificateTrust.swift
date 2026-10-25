import CryptoKit
import Foundation
import Security

/// TOFU（首次使用即信任）证书指纹仓库。
///
/// UGOS 用自签 X.509 证书，系统校验必然失败，所以策略是：
/// - 首次见到某主机证书时记录 SHA-256 指纹
/// - 之后每次比对，一致才继续，不一致就中断并把差异留给 UI 提示
final class CertificatePinStore {
    static let shared = CertificatePinStore()

    /// 记录一次指纹不匹配，供上层在连接失败后读取
    struct Mismatch {
        let expected: String
        let actual: String
    }

    private let defaults = UserDefaults.standard
    private let lock = NSLock()
    private let prefix = "tls.pin."
    private var mismatches: [String: Mismatch] = [:]

    private init() {}

    static func key(host: String, port: Int) -> String {
        "\(host.lowercased()):\(port)"
    }

    func pin(host: String, port: Int) -> String? {
        lock.lock()
        defer { lock.unlock() }
        return defaults.string(forKey: prefix + Self.key(host: host, port: port))
    }

    func store(_ fingerprint: String, host: String, port: Int) {
        lock.lock()
        defer { lock.unlock() }
        defaults.set(fingerprint, forKey: prefix + Self.key(host: host, port: port))
    }

    func remove(host: String, port: Int) {
        lock.lock()
        defer { lock.unlock() }
        defaults.removeObject(forKey: prefix + Self.key(host: host, port: port))
    }

    /// 全部已固定指纹（key 为 host:port）
    func allPins() -> [String: String] {
        lock.lock()
        defer { lock.unlock() }
        var result: [String: String] = [:]
        for (key, value) in defaults.dictionaryRepresentation() where key.hasPrefix(prefix) {
            if let pin = value as? String {
                result[String(key.dropFirst(prefix.count))] = pin
            }
        }
        return result
    }

    func removeAll() {
        lock.lock()
        defer { lock.unlock() }
        for (key, _) in defaults.dictionaryRepresentation() where key.hasPrefix(prefix) {
            defaults.removeObject(forKey: key)
        }
        mismatches.removeAll()
    }

    func recordMismatch(host: String, port: Int, expected: String, actual: String) {
        lock.lock()
        defer { lock.unlock() }
        mismatches[Self.key(host: host, port: port)] = Mismatch(expected: expected, actual: actual)
    }

    /// 取出并清空该主机的指纹变化记录
    func takeMismatch(host: String, port: Int) -> Mismatch? {
        lock.lock()
        defer { lock.unlock() }
        let key = Self.key(host: host, port: port)
        let value = mismatches[key]
        mismatches.removeValue(forKey: key)
        return value
    }
}

/// 带 TOFU 校验的 URLSession 信任代理。
///
/// 注意：代理回调在 URLSession 的后台队列执行，只做指纹读写，不碰 UI。
final class TOFUTrustDelegate: NSObject, URLSessionDelegate {
    private let store: CertificatePinStore

    init(store: CertificatePinStore = .shared) {
        self.store = store
        super.init()
    }

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

        let host = challenge.protectionSpace.host
        let port = challenge.protectionSpace.port

        guard let fingerprint = TOFUTrustDelegate.fingerprint(of: trust) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }

        if let pinned = store.pin(host: host, port: port) {
            if pinned == fingerprint {
                completionHandler(.useCredential, URLCredential(trust: trust))
            } else {
                // 指纹变了：中断连接，并把新旧指纹留给人来判断
                store.recordMismatch(host: host, port: port, expected: pinned, actual: fingerprint)
                completionHandler(.cancelAuthenticationChallenge, nil)
            }
        } else {
            // 首次连接，记住指纹
            store.store(fingerprint, host: host, port: port)
            completionHandler(.useCredential, URLCredential(trust: trust))
        }
    }

    /// 叶子证书的 SHA-256 指纹（小写 hex）
    static func fingerprint(of trust: SecTrust) -> String? {
        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf = chain.first else {
            return nil
        }
        let data = SecCertificateCopyData(leaf) as Data
        let digest = SHA256.hash(data: data)
        return digest.map { String(format: "%02x", $0) }.joined()
    }
}
