import SwiftUI

/// 通用进度条：占用越高颜色越警示
struct UsageBar: View {
    let percent: Double

    var body: some View {
        ProgressView(value: min(max(percent / 100, 0), 1))
            .tint(tint)
    }

    private var tint: Color {
        if percent >= 95 { return .red }
        if percent >= 85 { return .orange }
        return .accentColor
    }
}

/// 左标题右数值
struct InfoRow: View {
    let title: String
    let value: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title)
            Spacer(minLength: 8)
            Text(value ?? "-")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.trailing)
        }
        .font(.subheadline)
    }
}

/// 小胶囊标签
struct StatusBadge: View {
    let text: String
    let tint: Color

    var body: some View {
        Text(text)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(tint.opacity(0.15), in: Capsule())
            .foregroundStyle(tint)
    }
}

/// 状态码 → 文案/颜色。nil 视为「未知」而不是异常，避免字段缺失时误报。
struct HealthBadge: View {
    let status: Int?
    let normalValues: [Int]

    var body: some View {
        StatusBadge(text: text, tint: tint)
    }

    private var text: String {
        guard let status = status else { return "未知" }
        return normalValues.contains(status) ? "正常" : "异常（\(status)）"
    }

    private var tint: Color {
        guard let status = status else { return Color.secondary }
        return normalValues.contains(status) ? Color.green : Color.red
    }
}

/// 空状态占位
struct EmptyHint: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 6)
    }
}
