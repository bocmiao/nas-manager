import SwiftUI
import UIKit

/// 全屏图片预览。
///
/// 走 `mediaPlayer` 通道拿原图字节——不是缩略图。实测缩略图接口无视宽高，
/// 恒定回 112×96，放大了就是一团马赛克；`mediaPlayer` 回的才是源文件本体。
///
/// 大图整份读进内存，所以超过 `previewLimit` 就直接劝退，让人走「下载并分享」。
struct ImagePreviewView: View {
    let model: AppModel
    let entry: FileEntry

    @Environment(\.dismiss) private var dismiss

    @State private var image: UIImage?
    @State private var errorText: String?
    @State private var isLoading = true

    @State private var scale: CGFloat = 1
    @State private var committedScale: CGFloat = 1
    @State private var offset: CGSize = .zero
    @State private var committedOffset: CGSize = .zero

    /// 超过这个大小就不预览了——原图整份进内存没意义
    private static let previewLimit: UInt64 = 64 * 1024 * 1024

    var body: some View {
        NavigationStack {
            ZStack {
                Color.black.ignoresSafeArea()
                content
            }
            .navigationTitle(entry.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbarColorScheme(.dark, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("关闭") { dismiss() }
                        .tint(.white)
                }
            }
        }
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if let image {
            Image(uiImage: image)
                .resizable()
                .aspectRatio(contentMode: .fit)
                .scaleEffect(scale)
                .offset(offset)
                .gesture(zoom)
                .gesture(pan)
                .onTapGesture(count: 2) { toggleZoom() }
                .overlay(alignment: .bottom) { footer }
        } else if isLoading {
            VStack(spacing: 12) {
                ProgressView().tint(.white)
                Text("正在读取原图…")
                    .font(.footnote)
                    .foregroundStyle(.white.opacity(0.7))
                if let size = entry.size {
                    Text(Format.bytes(size))
                        .font(.caption2)
                        .foregroundStyle(.white.opacity(0.5))
                }
            }
        } else {
            VStack(spacing: 14) {
                Image(systemName: "photo.badge.exclamationmark")
                    .font(.largeTitle)
                    .foregroundStyle(.white.opacity(0.6))
                Text(errorText ?? "这张图片没能加载出来")
                    .font(.footnote)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.white.opacity(0.7))
                    .padding(.horizontal, 32)
                Button("关闭") { dismiss() }
                    .buttonStyle(.borderedProminent)
                    .tint(.white.opacity(0.2))
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Text(entry.mtimeText)
            Text("·")
            Text(entry.sizeText)
            if scale > 1.01 {
                Text("·")
                Text(String(format: "%.1f×", scale))
            }
        }
        .font(.caption2)
        .foregroundStyle(.white.opacity(0.65))
        .padding(.vertical, 8)
        .padding(.horizontal, 14)
        .background(.black.opacity(0.35), in: Capsule())
        .padding(.bottom, 18)
    }

    // MARK: 手势

    private var zoom: some Gesture {
        MagnifyGesture()
            .onChanged { value in
                scale = min(max(committedScale * value.magnification, 1), 6)
            }
            .onEnded { _ in
                committedScale = scale
                if scale <= 1.01 { resetPan() }
            }
    }

    /// 只有放大之后才允许拖动，否则会和关闭/滚动手势打架
    private var pan: some Gesture {
        DragGesture()
            .onChanged { value in
                guard scale > 1.01 else { return }
                offset = CGSize(
                    width: committedOffset.width + value.translation.width,
                    height: committedOffset.height + value.translation.height
                )
            }
            .onEnded { _ in
                guard scale > 1.01 else { return }
                committedOffset = offset
            }
    }

    private func toggleZoom() {
        withAnimation(.easeOut(duration: 0.2)) {
            if scale > 1.01 {
                scale = 1
                committedScale = 1
                resetPan()
            } else {
                scale = 2.5
                committedScale = 2.5
            }
        }
    }

    private func resetPan() {
        offset = .zero
        committedOffset = .zero
    }

    // MARK: 加载

    @MainActor
    private func load() async {
        guard let size = entry.size, size > Self.previewLimit else {
            await fetch()
            return
        }
        isLoading = false
        errorText = "图片有 \(Format.bytes(size))，整张读进内存不划算，用「下载并分享」吧"
    }

    @MainActor
    private func fetch() async {
        do {
            let data = try await model.filePreview(entry: entry)
            if let decoded = UIImage(data: data) {
                image = decoded
            } else {
                errorText = "这个格式 iOS 解不了，改用「下载并分享」"
            }
        } catch {
            errorText = error.localizedDescription
        }
        isLoading = false
    }
}
