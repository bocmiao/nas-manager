import Foundation
import UserNotifications

/// 本地通知。App 在前台轮询到新告警时立刻发一条，用户切出去也能在通知中心看到。
final class AlertNotifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = AlertNotifier()

    private override init() {
        super.init()
    }

    /// 注册代理，让前台也能弹横幅（App 启动时调用）
    func activate() {
        UNUserNotificationCenter.current().delegate = self
    }

    /// 请求授权，返回是否已获得（拒绝也返回 false，不影响 App 使用）
    func requestAuthorization() async -> Bool {
        let center = UNUserNotificationCenter.current()
        do {
            return try await center.requestAuthorization(options: [.alert, .sound, .badge])
        } catch {
            return false
        }
    }

    func post(_ event: AlertEvent) {
        let content = UNMutableNotificationContent()
        content.title = event.title
        content.body = event.detail
        content.sound = .default
        content.userInfo = ["eventId": event.id.uuidString, "ruleKey": event.ruleKey]

        let request = UNNotificationRequest(
            identifier: event.id.uuidString,
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }

    // MARK: UNUserNotificationCenterDelegate

    /// 前台也弹横幅。用 completionHandler 版本，避免 async 版本在 @objc 代理上的兼容问题。
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }
}
