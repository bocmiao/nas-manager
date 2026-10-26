import Foundation
import SwiftUI
import WidgetKit

/// 颜色全部走语义色 / 系统色，深浅色模式自动适配
struct NasManagerWidgetView: View {
    @Environment(\.widgetFamily) private var family

    let entry: WidgetEntry

    var body: some View {
        Group {
            if entry.snapshot == nil {
                emptyState
            } else if family == .systemMedium {
                mediumLayout
            } else {
                smallLayout
            }
        }
    }

    // MARK: - 小尺寸

    private var smallLayout: some View {
        VStack(alignment: .leading, spacing: 6) {
            header(compact: true)
            meter("CPU", entry.snapshot?.cpuPercent)
            meter("内存", entry.snapshot?.memPercent)
            Spacer(minLength: 0)
            Text(footerText)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
    }

    // MARK: - 中尺寸

    private var mediumLayout: some View {
        VStack(alignment: .leading, spacing: 8) {
            header(compact: false)
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 8) {
                    meter("CPU", entry.snapshot?.cpuPercent)
                    meter("内存", entry.snapshot?.memPercent)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                VStack(alignment: .leading, spacing: 3) {
                    statRow("温度", tempText(entry.snapshot?.cpuTemp))
                    statRow("风扇", fanText(entry.snapshot?.fanRpm))
                    statRow("存储", percentText(entry.snapshot?.poolUsedPercent))
                    statRow("容器", containerText)
                    statRow("告警", alertText)
                }
                .frame(width: 92, alignment: .leading)
            }
            Spacer(minLength: 0)
        }
    }

    // MARK: - 无数据占位

    private var emptyState: some View {
        VStack(spacing: 4) {
            Text("未获取到数据")
                .font(.caption.weight(.semibold))
            Text("打开 NAS 客户端同步一次")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - 片段

    private func header(compact: Bool) -> some View {
        HStack(spacing: 4) {
            Text(machineName)
                .font(.caption.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            if entry.isStale {
                staleBadge(compact: compact)
            }
            Spacer(minLength: 0)
            if !compact {
                Text(updatedText)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }

    private func staleBadge(compact: Bool) -> some View {
        Text(compact ? "过期" : "数据可能过期")
            .font(.caption2.weight(.medium))
            .foregroundStyle(Color.orange)
            .lineLimit(1)
            .fixedSize()
    }

    private func meter(_ title: String, _ percent: Double?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 4) {
                Text(title)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer(minLength: 2)
                Text(percentText(percent))
                    .font(.caption2.weight(.semibold))
                    .monospacedDigit()
            }
            ProgressView(value: barValue(percent))
                .progressViewStyle(.linear)
                .tint(barTint(percent))
        }
    }

    private func statRow(_ title: String, _ value: String) -> some View {
        HStack(spacing: 4) {
            Text(title)
                .foregroundStyle(.secondary)
            Spacer(minLength: 4)
            Text(value)
                .monospacedDigit()
        }
        .font(.caption2)
        .lineLimit(1)
    }

    // MARK: - 文案

    private var machineName: String {
        guard let name = entry.snapshot?.machineName, !name.isEmpty else { return "NAS" }
        return name
    }

    private var footerText: String {
        var parts: [String] = [tempText(entry.snapshot?.cpuTemp)]
        if let running = entry.snapshot?.runningContainers {
            parts.append("\(running) 容器")
        }
        if let alerts = entry.snapshot?.alertCount {
            parts.append("\(alerts) 告警")
        }
        return parts.joined(separator: " · ")
    }

    private var containerText: String {
        guard let running = entry.snapshot?.runningContainers else { return "—" }
        guard let total = entry.snapshot?.totalContainers else { return "\(running)" }
        return "\(running) / \(total)"
    }

    private var alertText: String {
        guard let alerts = entry.snapshot?.alertCount else { return "—" }
        return "\(alerts)"
    }

    private var updatedText: String {
        guard let snapshot = entry.snapshot else { return "" }
        return WidgetTime.clock.string(from: snapshot.updatedAt)
    }

    private func percentText(_ value: Double?) -> String {
        guard let value else { return "—" }
        return String(format: "%.0f%%", value)
    }

    private func tempText(_ value: Int?) -> String {
        guard let value else { return "—" }
        return "\(value)°C"
    }

    private func fanText(_ value: Int?) -> String {
        guard let value else { return "—" }
        return "\(value)"
    }

    // MARK: - 进度条

    private func barValue(_ percent: Double?) -> Double {
        guard let percent else { return 0 }
        return min(max(percent / 100, 0), 1)
    }

    private func barTint(_ percent: Double?) -> Color {
        guard let percent else { return Color.secondary.opacity(0.35) }
        if percent >= 95 { return Color.red }
        if percent >= 85 { return Color.orange }
        return Color.accentColor
    }
}

private enum WidgetTime {
    static let clock: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter
    }()
}
