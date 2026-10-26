import SwiftUI

/// 设置页：服务器 / 安全 / 数据刷新 / 关于
struct SettingsView: View {
    @Bindable var model: AppModel

    @State private var showingClearKeychain = false
    @State private var showingResetPins = false
    @State private var biometryAvailable = BiometricLock.isAvailable()

    var body: some View {
        NavigationStack {
            Form {
                serverSection
                securitySection
                refreshSection
                aboutSection
            }
            .navigationTitle("设置")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("保存") {
                        model.saveSettings()
                    }
                }
            }
            .onAppear {
                biometryAvailable = BiometricLock.isAvailable()
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

    // MARK: 服务器

    @ViewBuilder
    private var serverSection: some View {
        Section {
            // 配置列表：点一行即切换（当前项置灰）
            ForEach(model.serverProfiles) { profile in
                Button {
                    Task { await model.switchProfile(to: profile.id) }
                } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(profile.displayName)
                                .font(.subheadline)
                                .foregroundStyle(.primary)
                            Text(profile.addressSummary)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 8)
                        if profile.id == model.activeProfileID {
                            Image(systemName: "checkmark")
                                .foregroundStyle(.tint)
                        }
                    }
                }
                .disabled(profile.id == model.activeProfileID || model.isConnecting)
            }

            NavigationLink {
                ServerProfilesView(model: model)
            } label: {
                Label("管理服务器配置", systemImage: "server.rack")
            }

            if let notice = model.profileNotice {
                Text(notice)
                    .font(.footnote)
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("服务器")
        } footer: {
            Text("切换服务器会先断开当前连接，再用新配置重新连接。当前使用中的配置不能删除。")
        }

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

            if let preview = EndpointResolver.previewURL(host: model.internalHost, port: model.port) {
                InfoRow(title: "解析为", value: preview)
            }
        } header: {
            Text("当前服务器地址")
        } footer: {
            Text("支持 IPv6：可直接粘贴 2001:db8::1 或 [2001:db8::1]:9443，拼 URL 时会自动补方括号。")
        }

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

            Button(role: .destructive) {
                model.disconnect()
            } label: {
                Text("断开连接")
            }
        } header: {
            Text("账号")
        } footer: {
            Text("密码只写入系统 Keychain，并按服务器分别保存（account 带配置 id），不会落到 UserDefaults。")
        }
    }

    // MARK: 安全

    @ViewBuilder
    private var securitySection: some View {
        Section {
            Toggle(isOn: $model.biometricEnabled) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("启动时验证身份")
                    Text(biometryAvailable
                         ? "使用 \(BiometricLock.displayName()) 解锁已保存的密码"
                         : "此设备不支持生物识别")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            // 设备不支持时不允许新开，但已经开着的要允许关掉，否则用户会被永远要求输密码
            .disabled(!biometryAvailable && !model.biometricEnabled)
            .onChange(of: model.biometricEnabled) { _, enabled in
                model.setBiometricEnabled(enabled)
            }

            if model.biometricEnabled && !model.canLockWithBiometrics {
                Text("还需要勾选「记住密码」并成功连接一次，下次启动才会要求验证。")
                    .font(.footnote)
                    .foregroundStyle(.orange)
            }

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
            Text("安全")
        } footer: {
            Text("首次连接记录服务器证书 SHA-256 指纹（TOFU）；指纹变化时会中断连接并提示确认。生物识别不可用或多次失败时会自动回落到手输密码。")
        }
    }

    // MARK: 数据刷新

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

            InfoRow(
                title: "当前版本",
                value: model.firmware?.currentVersion
                    ?? model.dashboard?.machine?.common?.systemVersion
            )
            InfoRow(title: "最新版本", value: model.firmware?.publishVersion)

            HStack {
                Text("更新状态")
                    .font(.subheadline)
                Spacer()
                if let firmware = model.firmware {
                    if firmware.hasUpdate {
                        StatusBadge(text: "有新版本", tint: .orange)
                    } else {
                        StatusBadge(text: "已是最新", tint: .green)
                    }
                } else {
                    Text("未获取")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }

            Button {
                Task { await model.refreshFirmware() }
            } label: {
                HStack(spacing: 8) {
                    if model.isCheckingFirmware { ProgressView() }
                    Text(model.isCheckingFirmware ? "检查中…" : "检查更新")
                }
            }
            .disabled(model.isCheckingFirmware)
        } header: {
            Text("数据刷新")
        } footer: {
            Text("前台轮询间隔，间隔越短越及时也越费电。固件只做版本提醒，升级请到 UGOS 网页端操作。")
        }
    }

    // MARK: 关于

    @ViewBuilder
    private var aboutSection: some View {
        Section {
            InfoRow(title: "接入方式", value: model.activeLabel ?? "-")
            InfoRow(title: "当前地址", value: model.activeDisplay)
            InfoRow(title: "上次更新", value: Format.timestamp(model.lastUpdated))
            InfoRow(title: "未读告警", value: "\(model.alerts.unreadCount) 条")
            InfoRow(title: "应用版本", value: appVersion)
        } header: {
            Text("关于")
        } footer: {
            Text("支持 Siri 快捷指令「查询 NAS 状态 / 查看告警 / 查询存储」，读取的是最近一次同步的快照，需先打开应用同步。")
        }
    }

    private var appVersion: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "0"
        let build = info?["CFBundleVersion"] as? String ?? "0"
        return "\(version) (\(build))"
    }

    private func shortFingerprint(_ value: String) -> String {
        guard !value.isEmpty else { return "-" }
        let prefix = String(value.prefix(24))
        return prefix + "…"
    }
}
