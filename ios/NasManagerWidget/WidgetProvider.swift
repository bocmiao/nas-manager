import Foundation
import WidgetKit

/// Widget 的时间线条目。`snapshot` 为 nil 表示拿不到共享数据，界面走占位态。
struct WidgetEntry: TimelineEntry {
    let date: Date
    let snapshot: WidgetSnapshot?

    /// 快照超过 10 分钟即认为陈旧，界面打角标
    var isStale: Bool {
        guard let snapshot else { return false }
        return date.timeIntervalSince(snapshot.updatedAt) > 600
    }
}

/// 无 async、无 AppIntent 的基础 TimelineProvider，兼容性最好
struct WidgetProvider: TimelineProvider {
    /// 系统对小组件的刷新有配额限制，15 分钟一次足够
    private let refreshInterval: TimeInterval = 15 * 60

    func placeholder(in context: Context) -> WidgetEntry {
        WidgetEntry(date: Date(), snapshot: WidgetSnapshot.sample)
    }

    func getSnapshot(in context: Context, completion: @escaping (WidgetEntry) -> Void) {
        if context.isPreview {
            completion(WidgetEntry(date: Date(), snapshot: WidgetSnapshot.sample))
            return
        }
        completion(WidgetEntry(date: Date(), snapshot: WidgetBridge.read()))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<WidgetEntry>) -> Void) {
        let now = Date()
        let entry = WidgetEntry(date: now, snapshot: WidgetBridge.read())
        let next = now.addingTimeInterval(refreshInterval)
        completion(Timeline(entries: [entry], policy: .after(next)))
    }
}

extension WidgetSnapshot {
    /// 占位与预览用的示例数据，不含任何真实设备信息
    static let sample = WidgetSnapshot(
        cpuPercent: 18,
        memPercent: 42,
        memUsed: 8_000_000_000,
        memTotal: 19_000_000_000,
        cpuTemp: 46,
        fanRpm: 900,
        poolUsedPercent: 63,
        runningContainers: 3,
        totalContainers: 5,
        alertCount: 0,
        updatedAt: Date(),
        machineName: "示例设备"
    )
}
