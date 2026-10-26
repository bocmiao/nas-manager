import SwiftUI

/// 服务器配置列表：切换 / 新增 / 编辑 / 删除
struct ServerProfilesView: View {
    @Bindable var model: AppModel

    @State private var editorProfile: ServerProfile?
    @State private var editorIsNew = false
    @State private var deleteError: String?

    var body: some View {
        List {
            Section {
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
                                if !profile.username.isEmpty {
                                    Text("用户 \(profile.username)")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Spacer(minLength: 8)
                            if profile.id == model.activeProfileID {
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundStyle(.tint)
                            }
                        }
                    }
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        Button(role: .destructive) {
                            if !model.deleteProfile(id: profile.id) {
                                deleteError = model.profileNotice
                            }
                        } label: {
                            Label("删除", systemImage: "trash")
                        }

                        Button {
                            editorIsNew = false
                            editorProfile = profile
                        } label: {
                            Label("编辑", systemImage: "pencil")
                        }
                        .tint(.blue)
                    }
                }
            } header: {
                Text("已保存的服务器")
            } footer: {
                Text("点一行切换。当前使用中的配置需要先切走才能删除。")
            }

            Section {
                Button {
                    editorIsNew = true
                    editorProfile = ServerProfile(name: "")
                } label: {
                    Label("新增服务器", systemImage: "plus.circle")
                }
            } footer: {
                Text("新增后回到列表点一下即可切换；密码在设置页输入并按服务器分别存 Keychain。")
            }
        }
        .navigationTitle("服务器")
        .sheet(item: $editorProfile) { profile in
            ProfileEditorView(profile: profile, isNew: editorIsNew) { saved in
                model.saveProfile(saved)
            }
        }
        .alert("无法删除", isPresented: deleteErrorBinding) {
            Button("好", role: .cancel) { deleteError = nil }
        } message: {
            Text(deleteError ?? "")
        }
    }

    private var deleteErrorBinding: Binding<Bool> {
        Binding(
            get: { deleteError != nil },
            set: { visible in
                if !visible { deleteError = nil }
            }
        )
    }
}

/// 新增 / 编辑单台服务器
struct ProfileEditorView: View {
    @Environment(\.dismiss) private var dismiss

    @State private var draft: ServerProfile
    private let isNew: Bool
    private let onSave: (ServerProfile) -> Void

    init(profile: ServerProfile, isNew: Bool, onSave: @escaping (ServerProfile) -> Void) {
        _draft = State(initialValue: profile)
        self.isNew = isNew
        self.onSave = onSave
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("名称（如 家里的 NAS）", text: $draft.name)
                        .autocorrectionDisabled()
                    TextField("内网地址", text: $draft.internalHost)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    TextField("公网地址（可留空）", text: $draft.publicHost)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    TextField("端口", text: $draft.port)
                        .keyboardType(.numberPad)
                } header: {
                    Text("地址")
                } footer: {
                    Text("支持 IPv6：可粘贴裸地址 2001:db8::1，或已带方括号与端口的 [2001:db8::1]:9443。")
                }

                Section {
                    TextField("用户名", text: $draft.username)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Toggle("记住密码（Keychain）", isOn: $draft.rememberPassword)
                } header: {
                    Text("账号")
                } footer: {
                    Text("密码不在这里填写：切换到该服务器后在设置页输入即可。")
                }
            }
            .navigationTitle(isNew ? "新增服务器" : "编辑服务器")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        onSave(cleanedDraft())
                        dismiss()
                    }
                }
            }
        }
    }

    /// 保存前把地址规范化，IPv6 去掉方括号只留主机部分
    private func cleanedDraft() -> ServerProfile {
        var result = draft
        result.name = draft.name.trimmingCharacters(in: .whitespacesAndNewlines)
        result.port = draft.port.trimmingCharacters(in: .whitespacesAndNewlines)
        result.username = draft.username.trimmingCharacters(in: .whitespacesAndNewlines)

        let internalRaw = draft.internalHost.trimmingCharacters(in: .whitespacesAndNewlines)
        let publicRaw = draft.publicHost.trimmingCharacters(in: .whitespacesAndNewlines)
        result.internalHost = EndpointResolver.normalize(host: internalRaw, port: result.port)?.host ?? internalRaw
        result.publicHost = EndpointResolver.normalize(host: publicRaw, port: result.port)?.host ?? publicRaw
        if result.port.isEmpty {
            result.port = "9443"
        }
        return result
    }
}
