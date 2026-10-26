import SwiftUI

/// 运维工具汇总入口：五项只读工具 + 已有的镜像 / 快照管理。
struct OpsToolsView: View {
    let model: AppModel

    var body: some View {
        List {
            Section {
                NavigationLink {
                    LogsView(model: model)
                } label: {
                    OpsRow(
                        icon: "doc.text.magnifyingglass",
                        tint: .blue,
                        title: "系统日志",
                        detail: "按级别、模块与关键词查询审计日志"
                    )
                }

                NavigationLink {
                    SessionsView(model: model)
                } label: {
                    OpsRow(
                        icon: "person.2",
                        tint: .indigo,
                        title: "会话与登录审计",
                        detail: "当前在线会话，登录成功与失败记录"
                    )
                }

                NavigationLink {
                    FanView(model: model)
                } label: {
                    OpsRow(
                        icon: "fan",
                        tint: .teal,
                        title: "风扇",
                        detail: "散热模式与机箱 / CPU 转速（只读）"
                    )
                }

                NavigationLink {
                    BackupView(model: model)
                } label: {
                    OpsRow(
                        icon: "arrow.triangle.2.circlepath",
                        tint: .purple,
                        title: "备份任务",
                        detail: "同步与备份任务的状态、进度与运行时间"
                    )
                }

                NavigationLink {
                    PowerPlanView(model: model)
                } label: {
                    OpsRow(
                        icon: "power",
                        tint: .orange,
                        title: "电源计划",
                        detail: "定时开机 / 关机计划（只读）"
                    )
                }
            } header: {
                Text("运维")
            } footer: {
                Text("这些页面只读取数据，所有修改仍需到 UGOS 网页端完成。")
            }

            Section {
                // 文件管理已提到一级 Tab，这里不再重复放入口
                NavigationLink {
                    ImagesView(model: model)
                } label: {
                    OpsRow(
                        icon: "square.stack.3d.down.right",
                        tint: .blue,
                        title: "镜像管理",
                        detail: "本地镜像列表、删除与拉取"
                    )
                }

                NavigationLink {
                    SnapshotsView(model: model)
                } label: {
                    OpsRow(
                        icon: "clock.arrow.circlepath",
                        tint: .green,
                        title: "快照管理",
                        detail: "按文件夹查看、创建与回滚快照"
                    )
                }
            } header: {
                Text("容器与存储")
            } footer: {
                Text("容器页与存储页仍保留各自入口，这里只是汇总，方便从设置页直接进入。文件管理会直接对 NAS 上的文件做写操作，删除默认进回收站。")
            }
        }
        .listStyle(.plain)
        .navigationTitle("运维工具")
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// 运维页的一行：图标 + 名称 + 一句说明
private struct OpsRow: View {
    let icon: String
    let tint: Color
    let title: String
    let detail: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.body)
                .foregroundStyle(tint)
                .frame(width: 26, alignment: .center)

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.subheadline)
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }
}
