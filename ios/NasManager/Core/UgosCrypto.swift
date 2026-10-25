import CryptoKit
import Foundation
import Security

/// UGOS 认证相关的加密。
///
/// 实测契约：
/// - `POST /ugreen/v1/verify/check {"username": ...}` 的**响应头 `X-Rsa-Token`** 是 base64 编码的 PEM（PKCS#1）
/// - 密码用 RSA PKCS#1 v1.5 加密，单块上限 = 密钥字节数 - 11
/// - 前端 `JSEncrypt.encryptLong` 会分段加密，每段分别 base64 后拼接
enum UgosCryptoError: LocalizedError {
    case invalidPEM
    case keyCreationFailed(String)
    case encryptFailed(String)

    var errorDescription: String? {
        switch self {
        case .invalidPEM: return "公钥格式无法解析"
        case .keyCreationFailed(let msg): return "公钥创建失败：\(msg)"
        case .encryptFailed(let msg): return "密码加密失败：\(msg)"
        }
    }
}

enum UgosCrypto {
    /// 从 PEM 文本构造 SecKey。PEM 在本场景下是 PKCS#1（`BEGIN RSA PUBLIC KEY`）。
    static func publicKey(fromPEM pem: String) throws -> SecKey {
        let cleaned = pem
            .replacingOccurrences(of: "-----BEGIN RSA PUBLIC KEY-----", with: "")
            .replacingOccurrences(of: "-----END RSA PUBLIC KEY-----", with: "")
            .replacingOccurrences(of: "-----BEGIN PUBLIC KEY-----", with: "")
            .replacingOccurrences(of: "-----END PUBLIC KEY-----", with: "")
            .components(separatedBy: .whitespacesAndNewlines)
            .joined()

        guard let der = Data(base64Encoded: cleaned), !der.isEmpty else {
            throw UgosCryptoError.invalidPEM
        }

        let attrs: [CFString: Any] = [
            kSecAttrKeyType: kSecAttrKeyTypeRSA,
            kSecAttrKeyClass: kSecAttrKeyClassPublic,
            kSecAttrKeySizeInBits: der.count * 8
        ]

        var error: Unmanaged<CFError>?
        guard let key = SecKeyCreateWithData(der as CFData, attrs as CFDictionary, &error) else {
            let msg = error?.takeRetainedValue().localizedDescription ?? "未知错误"
            throw UgosCryptoError.keyCreationFailed(msg)
        }
        return key
    }

    /// 分段 RSA 加密，返回 base64。密码长度远小于单块上限，实际只走一段。
    static func encryptLong(_ plain: String, key: SecKey) throws -> String {
        guard let data = plain.data(using: .utf8), !data.isEmpty else {
            throw UgosCryptoError.encryptFailed("明文为空")
        }

        let blockSize = SecKeyGetBlockSize(key)
        let maxChunk = max(1, blockSize - 11)  // PKCS#1 v1.5 填充开销

        var output = Data()
        var index = data.startIndex
        while index < data.endIndex {
            let end = data.index(index, offsetBy: maxChunk, limitedBy: data.endIndex) ?? data.endIndex
            let chunk = data[index..<end]

            var error: Unmanaged<CFError>?
            guard let encrypted = SecKeyCreateEncryptedData(
                key, .rsaEncryptionPKCS1, chunk as CFData, &error
            ) else {
                let msg = error?.takeRetainedValue().localizedDescription ?? "未知错误"
                throw UgosCryptoError.encryptFailed(msg)
            }
            output.append(encrypted as Data)
            index = end
        }

        return output.base64EncodedString()
    }

    /// 证书指纹（SHA-256），用于 TOFU 固定，避免长期无条件信任自签证书。
    static func fingerprint(of certificate: SecCertificate) -> String {
        let data = SecCertificateCopyData(certificate) as Data
        let digest = SHA256.hash(data: data)
        return digest.map { String(format: "%02x", $0) }.joined()
    }
}
