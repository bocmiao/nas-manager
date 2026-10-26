import SwiftUI
import UIKit

/// 缩略图加载器（缓存 + 并发合并 + 限流）。
///
/// 全部跑在主线程上：`AppModel.fileThumbnail` 本身是 `@MainActor`，
/// 真正发请求的地方在 actor 内部，主线程只做等待，不会阻塞 UI。
/// 这样也彻底避开了把非 `Sendable` 的 `AppModel` 塞进后台闭包的并发告警。
@MainActor
final class ThumbnailLoader {
    static let shared = ThumbnailLoader()

    /// NAS 每次请求都要现转码（首次还会回 423 重试），压不住会一起变慢。
    /// List 懒加载，可见行十来行，3 路足够填满一屏。
    private let limit = 3
    private var active = 0
    private var waiters: [CheckedContinuation<Void, Never>] = []

    /// 键用 `path|mtime|size`：文件被覆盖后 mtime 变了，缓存自然失效
    private let cache: NSCache<NSString, UIImage> = {
        let c = NSCache<NSString, UIImage>()
        c.countLimit = 240
        c.totalCostLimit = 48 * 1024 * 1024
        return c
    }()

    /// 同一张图同时被多处请求时只发一次
    private var inflight: [String: Task<UIImage?, Never>] = [:]

    private func key(_ entry: FileEntry) -> String {
        "\(entry.path)|\(entry.mtime ?? 0)|\(entry.size ?? 0)"
    }

    /// 只查缓存，不发请求（视图首帧用，避免闪一下图标再变成图）
    func cachedImage(for entry: FileEntry) -> UIImage? {
        cache.object(forKey: key(entry) as NSString)
    }

    func image(for entry: FileEntry, model: AppModel) async -> UIImage? {
        let k = key(entry)
        if let hit = cache.object(forKey: k as NSString) { return hit }
        if let running = inflight[k] { return await running.value }

        let task = Task<UIImage?, Never> { [weak self] in
            guard let self else { return nil }
            await self.acquire()
            defer { self.release() }
            guard let data = try? await model.fileThumbnail(entry: entry),
                  let image = UIImage(data: data) else { return nil }
            return image
        }
        inflight[k] = task
        let result = await task.value
        inflight[k] = nil

        if let result {
            let cost = Int(result.size.width * result.size.height * result.scale * 4)
            cache.setObject(result, forKey: k as NSString, cost: cost)
        }
        return result
    }

    private func acquire() async {
        if active < limit {
            active += 1
            return
        }
        // 挂起等名额。名额由 release() 直接转交，醒来后不要再自增，
        // 否则 release 减一 + 这里加一之间会被新来的插队，实际并发会超限。
        await withCheckedContinuation { waiters.append($0) }
    }

    private func release() {
        if waiters.isEmpty {
            active = max(0, active - 1)
            return
        }
        // 名额就地转交，active 不变
        waiters.removeFirst().resume()
    }
}

/// 文件列表左侧图标位：能取到缩略图就显示缩略图，取不到退回系统图标。
///
/// 失败一律静默——显示系统图标本来就是可接受的降级，不该因为一张图
/// 拉不下来就在列表里弹错误。
struct FileThumbnailView: View {
    let model: AppModel
    let entry: FileEntry
    var side: CGFloat = 24

    @State private var image: UIImage?

    var body: some View {
        content
            .frame(width: side, height: side)
            .task(id: cacheKey) { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if let image {
            Image(uiImage: image)
                .resizable()
                .aspectRatio(contentMode: .fill)
                .frame(width: side, height: side)
                .clipShape(RoundedRectangle(cornerRadius: side * 0.2, style: .continuous))
        } else {
            Image(systemName: entry.iconName)
                .font(.body)
                .foregroundStyle(entry.isDir ? Color.accentColor : Color.secondary)
        }
    }

    /// 文件被覆盖后 mtime / size 会变，需要重新取图
    private var cacheKey: String {
        "\(entry.path)|\(entry.mtime ?? 0)|\(entry.size ?? 0)"
    }

    @MainActor
    private func load() async {
        guard FileService.supportsThumbnail(entry) else { return }
        if let hit = ThumbnailLoader.shared.cachedImage(for: entry) {
            image = hit
            return
        }
        // 已经显示过就不再覆盖，避免滚动回来闪一下
        guard image == nil else { return }
        if let loaded = await ThumbnailLoader.shared.image(for: entry, model: model) {
            image = loaded
        }
    }
}
