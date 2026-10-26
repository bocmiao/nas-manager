import SwiftUI
import WidgetKit

/// 注意：类型名不要与 target 同名（NasManagerWidget），否则模块名/类型名解析容易冲突
struct NasManagerStatusWidget: Widget {
    private let kind = "NasManagerStatusWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: WidgetProvider()) { entry in
            NasManagerWidgetView(entry: entry)
                .containerBackground(.background, for: .widget)
        }
        .configurationDisplayName("NAS 状态")
        .description("CPU、内存、温度、存储与容器概览")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}
