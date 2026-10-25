import SwiftUI

/// 告警页：本地规则 + 事件列表
struct AlertsView: View {
    let model: AppModel

    @State private var showingClearConfirm = false

    var body: some View {
        NavigationStack {
            List {
                if model.notificationDenied {
                    Section {
                        Label(
                            "通知权限未开启，新告警只会记录在这里。可在系统设置中允许通知。",
                            systemImage: "bell.slash"
                        )
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    }
                }

                rulesSection
                eventsSection
            }
            .navigationTitle("告警")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable { await model.refresh() }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button {
                            model.alerts.markAllRead()
                        } label: {
                            Label("全部标记已读", systemImage: "checkmark.circle")
                        }
                        Button(role: .destructive) {
                            showingClearConfirm = true
                        } label: {
                            Label("清空事件", systemImage: "trash")
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                    }
                }
            }
            .confirmationDialog("清空全部告警事件？", isPresented: $showingClearConfirm, titleVisibility: .visible) {
                Button("清空", role: .destructive) {
                    model.alerts.clear()
                }
                Button("取消", role: .cancel) {}
            } message: {
                Text("仅清除本机记录，不影响 NAS 侧日志。")
            }
        }
    }

    // MARK: 规则

    @ViewBuilder
    private var rulesSection: some View {
        Section {
            ForEach(AlertRuleKey.allCases, id: \.self) { rule in
                AlertRuleRow(rule: rule)
            }
        } header: {
            Text("本地规则")
        } footer: {
            Text("规则在本机判定，随每次刷新执行；同一条规则 10 分钟内只记一次。")
        }
    }

    // MARK: 事件

    @ViewBuilder
    private var eventsSection: some View {
        Section {
            if model.alerts.events.isEmpty {
                EmptyHint(text: "还没有告警事件。规则触发后会自动记录，并发送本地通知。")
            }
            ForEach(model.alerts.events) { event in
                eventRow(event)
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        Button(role: .destructive) {
                            model.alerts.delete(event)
                        } label: {
                            Label("删除", systemImage: "trash")
                        }
                        if !event.isRead {
                            Button {
                                model.alerts.markRead(event)
                            } label: {
                                Label("已读", systemImage: "checkmark")
                            }
                            .tint(Color.blue)
                        }
                    }
            }
        } header: {
            HStack {
                Text("事件")
                Spacer()
                Text("未读 \(model.alerts.unreadCount)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func eventRow(_ event: AlertEvent) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: event.severity.symbolName)
                .font(.body)
                .foregroundStyle(tint(for: event.severity))

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(event.title)
                        .font(.subheadline.weight(event.isRead ? .regular : .semibold))
                    if !event.isRead {
                        Circle()
                            .fill(Color.accentColor)
                            .frame(width: 6, height: 6)
                    }
                    Spacer(minLength: 0)
                    Text(Format.relative(event.date))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }

                Text(event.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Text("\(event.severity.label) · \(Format.timestamp(event.date))")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .onTapGesture {
            model.alerts.markRead(event)
        }
    }

    private func tint(for severity: AlertSeverity) -> Color {
        switch severity {
        case .info: return .blue
        case .warning: return .orange
        case .critical: return .red
        }
    }
}

// MARK: - 单条规则

/// 阈值与开关都用 @AppStorage 直接落 UserDefaults，
/// 键名来自 AlertRuleKey，和告警引擎读取的完全一致。
struct AlertRuleRow: View {
    let rule: AlertRuleKey

    @AppStorage private var enabled: Bool
    @AppStorage private var threshold: Double

    init(rule: AlertRuleKey) {
        self.rule = rule
        // 显式写出泛型参数，避免 _enabled / _threshold 的类型推断依赖上下文
        _enabled = AppStorage<Bool>(wrappedValue: true, rule.enabledKey)
        _threshold = AppStorage<Double>(wrappedValue: rule.defaultThreshold, rule.thresholdKey)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle(isOn: $enabled) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(rule.title)
                        .font(.subheadline)
                    Text(rule.summary)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }

            if rule.usesThreshold, enabled {
                Stepper(value: $threshold, in: rule.thresholdRange, step: 1) {
                    HStack {
                        Text("阈值")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text("\(Int(threshold))\(rule.unit)")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .padding(.vertical, 2)
    }
}
