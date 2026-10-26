import SwiftUI

/// 解锁页：开启「启动时验证身份」且已保存密码时挡在所有页面之前
struct LockView: View {
    @Bindable var model: AppModel

    @State private var isAuthenticating = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 18) {
                Spacer()

                Image(systemName: "lock.shield.fill")
                    .font(.system(size: 56))
                    .foregroundStyle(.tint)

                VStack(spacing: 4) {
                    Text("NAS 已锁定")
                        .font(.title3.weight(.semibold))
                    Text(model.profiles.active?.displayName ?? "已保存的服务器")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                if let message = model.lockMessage {
                    Text(message)
                        .font(.footnote)
                        .foregroundStyle(.orange)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 24)
                }

                Button {
                    authenticate()
                } label: {
                    HStack(spacing: 8) {
                        if isAuthenticating {
                            ProgressView()
                        }
                        Text("使用\(BiometricLock.displayName())解锁")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(isAuthenticating)
                .padding(.horizontal, 32)

                Button("改用密码") {
                    model.fallbackToPassword()
                }
                .font(.footnote)

                Spacer()
            }
            .padding(.vertical, 32)
            .navigationTitle("解锁")
        }
        .task {
            // 进入即弹一次，用户取消后仍可点上面的按钮重试
            authenticate()
        }
    }

    private func authenticate() {
        guard !isAuthenticating else { return }
        isAuthenticating = true
        Task { @MainActor in
            await model.unlockWithBiometrics()
            isAuthenticating = false
        }
    }
}
