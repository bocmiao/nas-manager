import SwiftUI

/// 连接页：首次进入 / 断开后都在这里
struct ConnectView: View {
    @Bindable var model: AppModel

    var body: some View {
        NavigationStack {
            Form {
                if let profile = model.profiles.active {
                    Section {
                        InfoRow(title: "当前服务器", value: profile.displayName)
                    } footer: {
                        Text("在设置页可以新增、切换多台 NAS 配置。")
                    }
                }

                Section {
                    TextField("内网地址，如 192.0.2.10", text: $model.internalHost)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    TextField("公网地址，可留空", text: $model.publicHost)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    TextField("端口", text: $model.port)
                        .keyboardType(.numberPad)

                    if let preview = EndpointResolver.previewURL(host: model.internalHost, port: model.port) {
                        InfoRow(title: "解析为", value: preview)
                    }
                } header: {
                    Text("NAS 地址")
                } footer: {
                    Text("内网优先，探测不通时自动切到公网地址。端口默认 9443。IPv6 可直接粘贴 2001:db8::1 或 [2001:db8::1]:9443，会自动补方括号。")
                }

                Section {
                    TextField("用户名", text: $model.username)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("密码", text: $model.password)
                    Toggle("记住密码（写入 Keychain）", isOn: $model.rememberPassword)
                } header: {
                    Text("账号")
                } footer: {
                    Text("密码会用服务器 RSA 公钥加密后换取会话 token。勾选后存入系统 Keychain，可在设置页清除。")
                }

                if let error = model.errorMessage {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }

                Section {
                    Button {
                        Task { await model.connect() }
                    } label: {
                        HStack {
                            Spacer()
                            if model.isConnecting {
                                ProgressView()
                                    .padding(.trailing, 6)
                            }
                            Text(model.isConnecting ? "连接中…" : "连接")
                            Spacer()
                        }
                    }
                    .disabled(model.isConnecting)
                } footer: {
                    Text("首次连接会记住服务器证书指纹（TOFU）；之后指纹变化会中断连接并弹出提示。")
                }
            }
            .navigationTitle("连接 NAS")
        }
    }
}
