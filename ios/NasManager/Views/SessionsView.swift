import Foundation
import SwiftUI

/// 会话与登录审计。
/// 在线会话走 `v1/user/online/list`（只接受 POST），登录审计走 `v1/log/query?module=login`。
struct SessionsView: View {
    let model: AppModel

    @State private var sessions: [OnlineSession] = []
    @State private var records: [LoginRecord] = []
    @State private var sessionTotal: Int?

    @State private var onlineAvailable = true
    @State private var auditAvailable = true
    @State private var onlineError: String?
    @State private var auditError: String?

    @State private var isLoading = true
    /// 0 = 在线会话，1 = 登录审计
    @State private var mode = 0
    @State private var failuresOnly = false

    var body: some View {
        List {
            summarySection

            Section {
                Picker("视图", selection: $mode) {
                    Text("在线会话").tag(0)
                    Text("登录审计").tag(1)
                }
                .pickerStyle(.segmented)
                .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))
            }

            if mode == 0 {
                onlineSection
            } else {
                auditSection
            }
        }
        .listStyle(.plain)
        .navigationTitle("会话与登录审计")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await reload() }
        .task { await reload() }
    }

    // MARK: 汇总

    private var summarySection: some View {
        Section {
            HStack(spacing: 0) {
                summaryItem(value: onlineCountText, title: "在线会话", tint: .blue)
                Divider().frame(height: 34)
                summaryItem(
                    value: failureCountText,
                    title: "近 5 分钟失败",
                    tint: (recentFailures ?? 0) > 0 ? .red : .secondary
                )
            }
            .padding(.vertical, 6)
        } footer: {
            Text("统计基于本次拉取到的会话与审计记录。来源 IP 按接口原样显示，不做地理位置推测。")
        }
    }

    private func summaryItem(value: String, title: String, tint: Color) -> some View {
        VStack(spacing: 4) {
            Text(value)
                .font(.title2.weight(.semibold))
                .foregroundStyle(tint)
            Text(title)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: 在线会话

    @ViewBuilder
    private var onlineSection: some View {
        if !onlineAvailable {
            unavailableBlock(
                title: "会话接口不可用",
                message: onlineError ?? "当前固件未开放在线会话接口。"
            )
        } else if sessions.isEmpty {
            Section {
                if isLoading {
                    loadingRow("正在读取在线会话…")
                } else {
                    EmptyHint(text: "当前没有在线会话")
                }
            } header: {
                Text("在线会话")
            }
        } else {
            Section {
                ForEach(sessions) { session in
                    sessionRow(session)
                }
            } header: {
                Text(sessionHeader)
            } footer: {
                Text("固件不会及时清理未正常登出的会话，记录可能多于实际在线数量。")
            }
        }
    }

    private func sessionRow(_ session: OnlineSession) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Text(session.username ?? "未知账号")
                    .font(.subheadline.weight(.medium))
                    .lineLimit(1)
                if session.isCurrent {
                    StatusBadge(text: "当前连接", tint: .green)
                }
                Spacer(minLength: 0)
                Text(session.protocolText)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            InfoRow(title: "来源 IP", value: session.ip)
            InfoRow(title: "平台", value: session.platformText)
            InfoRow(title: "登录时间", value: Format.timestamp(session.loginAt))
            InfoRow(title: "已连接", value: session.durationText)

            if let pid = session.pid, !pid.isEmpty {
                InfoRow(title: "进程", value: pid)
            }
        }
        .padding(.vertical, 3)
    }

    // MARK: 登录审计

    @ViewBuilder
    private var auditSection: some View {
        if !auditAvailable {
            unavailableBlock(
                title: "登录审计不可用",
                message: auditError ?? "当前固件未开放日志查询接口。"
            )
        } else {
            Section {
                Toggle("只看失败", isOn: $failuresOnly)
            }

            if filteredRecords.isEmpty {
                Section {
                    if isLoading {
                        loadingRow("正在读取登录审计…")
                    } else {
                        EmptyHint(text: failuresOnly ? "这段时间没有登录失败记录" : "这段时间没有登录记录")
                    }
                } header: {
                    Text("登录审计")
                }
            } else {
                Section {
                    ForEach(filteredRecords) { record in
                        auditRow(record)
                    }
                } header: {
                    Text("登录审计（\(filteredRecords.count)）")
                } footer: {
                    Text("数据来自审计日志 module=login。登录失败在日志里是 warning 级别，不是 error；SSH 登录不含来源地址。")
                }
            }
        }
    }

    private func auditRow(_ record: LoginRecord) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                StatusBadge(text: record.success ? "成功" : "失败", tint: record.success ? .green : .red)
                Text(record.username ?? "未知账号")
                    .font(.subheadline.weight(.medium))
                    .lineLimit(1)
                Spacer(minLength: 0)
                Text(Format.timestamp(record.createdAt))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 10) {
                Label(record.ip ?? "无来源地址", systemImage: "network")
                Text(record.method)
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }

    // MARK: 通用

    private func unavailableBlock(title: String, message: String) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 6) {
                Label(title, systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline)
                Text(message)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)

            Button("重试") {
                Task { await reload() }
            }
        }
    }

    private func loadingRow(_ text: String) -> some View {
        HStack(spacing: 8) {
            ProgressView()
            Text(text)
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: 状态

    private var onlineCountText: String {
        onlineAvailable ? "\(sessions.count)" : "—"
    }

    private var sessionHeader: String {
        if let sessionTotal = sessionTotal, sessionTotal > sessions.count {
            return "在线会话 \(sessions.count) / \(sessionTotal)"
        }
        return "在线会话（\(sessions.count)）"
    }

    /// 只统计最近 5 分钟内的失败。审计只拉最近 50 条，极端情况下会少算。
    private var recentFailures: Int? {
        guard auditAvailable else { return nil }
        let threshold = Date().addingTimeInterval(-300)
        return records.filter { record in
            guard !record.success, let time = record.createdAt else { return false }
            return time >= threshold
        }.count
    }

    private var failureCountText: String {
        guard let count = recentFailures else { return "—" }
        return "\(count)"
    }

    private var filteredRecords: [LoginRecord] {
        failuresOnly ? records.filter { !$0.success } : records
    }

    // MARK: 动作

    @MainActor
    private func reload() async {
        isLoading = true

        let online = await model.onlineSessions()
        sessions = online.sessions
        sessionTotal = online.total
        onlineAvailable = online.available
        onlineError = online.message

        let audit = await model.loginAudit()
        records = audit.records
        auditAvailable = audit.available
        auditError = audit.message

        isLoading = false
    }
}
