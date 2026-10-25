import SwiftUI

/// 设置页：地址 / 账号 / 刷新 / 安全
struct SettingsView: View {
    @Bindable var model: AppModel

    @State private var showingClearKeychain = false
    @State private var showingResetPins = false

    var body: some View {
        NavigationStack {
            Form {
                addressSection
                accountSection
                refreshSection
                statusSection
                securitySection
            }
            .navigationTitle("设置")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("保存") {
                        model.saveSettings()
                    }
                }
            }
            .confirmationDialog(
                "清空 Keychain 中的密码？",
                isPresented: $showingClearKeychain,
                titleVisibility: .visible
            ) {
                Button("清空", role: .destructive) {
                    model.clearKeychain()
                }
                Button("取消", role: .cancel) {}
            } message: {
                Text("清除后需要重新输入密码才能连接。")
            }
            .confirmationDialog(
                "清除已记住的证书指纹？",
                isPresented: $showingResetPins,
                titleVisibility: .visible
            ) {
                Button("清除", role: .destructive) {
                    CertificatePinStore.shared.removeAll()
                }
                Button("取消", role: .cancel) {}
            } message: {
                Text("下次连接会重新记录当前证书指纹。仅在确认 NAS 证书已更换时使用。")
            }
        }
    }

    // MARK: 地址

    @ViewBuilder
    private var addressSection: some View {
        Section {
            TextField("内网地址", text: $model.internalHost)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
            TextField("公网地址（可留空）", text: $model.publicHost)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
            TextField("端口", text: $model.port)
                .keyboardType(.numberPad)
        } header: {
            Text("服务器地址")
        } footer: {
            Text("每次连接先探测内网，超时后自动尝试公网。上次成功的地址会优先使用。")
        }
    }

    // MARK: 账号

    @ViewBuilder
    private var accountSection: some View {
        Section {
            TextField("用户名", text: $model.username)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            SecureField("密码", text: $model.password)
            Toggle("记住密码（Keychain）", isOn: $model.rememberPassword)

            Button {
                model.saveSettings()
                Task { await model.connect() }
            } label: {
                if model.isConnecting {
                    HStack {
                        ProgressView()
                        Text("重新连接中…")
                    }
                } else {
                    Text("保存并重新连接")
                }
            }
            .disabled(model.isConnecting)
        } header: {
            Text("账号")
        } footer: {
            Text("密码只写入系统 Keychain，不会落到 UserDefaults。")
        }
    }

    // MARK: 刷新

    @ViewBuilder
    private var refreshSection: some View {
        Section {
            Picker("刷新间隔", selection: $model.refreshInterval) {
                Text("关闭").tag(0.0)
                Text("15 秒").tag(15.0)
                Text("30 秒").tag(30.0)
                Text("1 分钟").tag(60.0)
                Text("5 分钟").tag(300.0)
            }
            .onChange(of: model.refreshInterval) { _, _ in
                model.applyRefreshInterval()
            }
        } header: {
            Text("刷新")
        } footer: {
            Text("前台轮询间隔。间隔越短越及时，也越费电。")
        }
    }

    // MARK: 状态

    @ViewBuilder
    private var statusSection: some View {
        Section {
            InfoRow(title: "接入方式", value: model.activeLabel ?? "-")
            InfoRow(title: "当前地址", value: model.activeDisplay)
            InfoRow(title: "上次更新", value: Format.timestamp(model.lastUpdated))
            InfoRow(title: "未读告警", value: "\(model.alerts.unreadCount) 条")

            Button(role: .destructive) {
                model.disconnect()
            } label: {
                Text("断开连接")
            }
        } header: {
            Text("连接状态")
        }
    }

    // MARK: 安全

    @ViewBuilder
    private var securitySection: some View {
        Section {
            let pins = CertificatePinStore.shared.allPins()
            if pins.isEmpty {
                EmptyHint(text: "还没有记录任何证书指纹")
            } else {
                ForEach(pins.keys.sorted(), id: \.self) { key in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(key)
                            .font(.caption)
                        Text(shortFingerprint(pins[key] ?? ""))
                            .font(.caption2.monospaced())
                            .foregroundStyle(.secondary)
                    }
                }
            }

            Button {
                showingResetPins = true
            } label: {
                Text("清除证书指纹")
            }

            Button(role: .destructive) {
                showingClearKeychain = true
            } label: {
                Text("清空 Keychain 密码")
            }
        } header: {
            Text("安全（TOFU 证书指纹）")
        } footer: {
            Text("首次连接记录服务器证书 SHA-256 指纹；指纹变化时会中断连接并提示确认。")
        }
    }

    private func shortFingerprint(_ value: String) -> String {
        guard !value.isEmpty else { return "-" }
        let prefix = String(value.prefix(24))
        return prefix + "…"
    }
}
