import Foundation
import LocalAuthentication

/// 一次身份校验的结果。
/// 每种失败都带明确语义，调用方据此决定「留在解锁页」还是「降级为手输密码」。
enum BiometricResult {
    /// 通过
    case success
    /// 用户主动取消：留在解锁页，不算失败，也不降级
    case cancelled
    /// 设备不支持 / 未录入 / 已被系统锁定：降级为手输密码，绝不能把人锁在门外
    case unavailable(String)
    /// 本次没认出来：留在解锁页，可重试
    case failed(String)
}

/// 只负责本地身份校验，不碰网络与凭据。
enum BiometricLock {
    enum Kind: Equatable {
        case none
        case faceID
        case touchID
        case opticID
        case other
    }

    /// 当前设备可用的生物识别类型（同步查询，仅看能力，不弹框）
    static func availableKind() -> Kind {
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) else {
            return .none
        }
        switch context.biometryType {
        case .faceID: return .faceID
        case .touchID: return .touchID
        case .opticID: return .opticID
        case .none: return .none
        @unknown default: return .other
        }
    }

    static func isAvailable() -> Bool {
        availableKind() != .none
    }

    /// 界面文案里用的名称
    static func displayName() -> String {
        switch availableKind() {
        case .faceID: return "Face ID"
        case .touchID: return "Touch ID"
        case .opticID: return "Optic ID"
        case .other, .none: return "生物识别"
        }
    }

    /// 弹系统验证框。统一走 completion 版本，用 continuation 转成 async，
    /// 不与 `canEvaluatePolicy` 的 async 重载混用，避免重载解析歧义。
    static func authenticate(reason: String) async -> BiometricResult {
        let context = LAContext()
        context.localizedCancelTitle = "取消"

        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) else {
            return .unavailable(unavailableMessage(error))
        }

        let outcome: BiometricResult = await withCheckedContinuation { (continuation: CheckedContinuation<BiometricResult, Never>) in
            context.evaluatePolicy(
                .deviceOwnerAuthenticationWithBiometrics,
                localizedReason: reason
            ) { success, evaluationError in
                if success {
                    continuation.resume(returning: .success)
                } else {
                    continuation.resume(returning: BiometricLock.classify(evaluationError))
                }
            }
        }
        return outcome
    }

    // MARK: 错误归类

    private static func classify(_ error: Error?) -> BiometricResult {
        guard let laError = error as? LAError else {
            return .failed("身份验证未通过，可重试或改用密码")
        }
        switch laError.code {
        case .userCancel, .appCancel, .systemCancel:
            return .cancelled
        case .userFallback:
            // 系统弹框里选了「输入密码」
            return .unavailable("已选择手动输入密码")
        case .biometryNotAvailable, .biometryNotEnrolled, .biometryLockout, .passcodeNotSet:
            return .unavailable("此设备当前无法使用\(displayName())，请手动输入密码")
        case .authenticationFailed:
            return .failed("身份验证未通过，可重试或改用密码")
        default:
            return .failed("身份验证未通过，可重试或改用密码")
        }
    }

    private static func unavailableMessage(_ error: NSError?) -> String {
        guard error != nil else {
            return "此设备不支持生物识别，请手动输入密码"
        }
        return "此设备未启用生物识别（可能未录入或已锁定），请手动输入密码"
    }
}
