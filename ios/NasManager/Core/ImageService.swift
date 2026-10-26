import Foundation

// MARK: - 镜像模型

/// 本地 Docker 镜像。
///
/// 四个端点（ObtainAllImages / ShowLocalImageV2 / DeleteImage / DownloadImage）都是 POST，
/// 字段名在 1.19.1 上没有逐项实测，这里用显式 CodingKeys 覆盖几种常见写法，
/// 取不到就留空，不做猜测性填充。
struct DockerImage: Decodable, Identifiable {
    /// 删除时用的真实镜像 id（部分固件叫 image_id / imageId）
    let imageId: String?
    let repository: String
    let tag: String
    let size: UInt64?
    /// 创建时间，能识别成时间戳就转成 "MM-dd HH:mm:ss"，否则保留原始串
    let createdAt: String?
    let containerCount: Int?

    var id: String { imageId ?? reference }
    var reference: String { tag.isEmpty ? repository : repository + ":" + tag }
    /// 短 id，和 docker CLI 展示习惯一致
    var shortId: String {
        guard let imageId, !imageId.isEmpty else { return "-" }
        return String(imageId.prefix(12))
    }

    enum CodingKeys: String, CodingKey {
        case id
        case imageId = "image_id"
        case imageIdCamel = "imageId"
        case repository
        case repo
        case imageName = "image_name"
        case imageNameCamel = "imageName"
        case name
        case tag
        case version
        case size
        case imageSize = "image_size"
        case createdAt = "created_at"
        case createdAtCamel = "createdAt"
        case createAt = "createAt"
        case createTime = "create_time"
        case created
        case containerCount = "container_count"
        case containerCountCamel = "containerCount"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)

        imageId = DockerImage.text(container, [.id, .imageId, .imageIdCamel])

        let rawRepository = DockerImage.text(
            container,
            [.repository, .repo, .imageName, .imageNameCamel, .name]
        ) ?? ""
        var tagText = DockerImage.text(container, [.tag, .version]) ?? ""
        var base = rawRepository
        // 有些固件把标签并进镜像名（nginx:1.25），拆开才好展示和比较
        if let colon = DockerImage.tagSeparator(in: rawRepository) {
            tagText = String(rawRepository[rawRepository.index(after: colon)...])
            base = String(rawRepository[..<colon])
        }
        repository = base
        tag = tagText.isEmpty ? "latest" : tagText

        size = DockerImage.uint64(container, [.size, .imageSize])
        createdAt = DockerImage.dateText(
            DockerImage.text(container, [.createdAt, .createdAtCamel, .createAt, .createTime, .created])
        )
        containerCount = DockerImage.int(container, [.containerCount, .containerCountCamel])
    }

    // MARK: 宽松取值

    private static func text(
        _ container: KeyedDecodingContainer<CodingKeys>,
        _ keys: [CodingKeys]
    ) -> String? {
        for key in keys {
            if let value = try? container.decode(String.self, forKey: key) {
                let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty { return trimmed }
            }
            if let value = try? container.decode(Int.self, forKey: key) {
                return String(value)
            }
        }
        return nil
    }

    private static func uint64(
        _ container: KeyedDecodingContainer<CodingKeys>,
        _ keys: [CodingKeys]
    ) -> UInt64? {
        for key in keys {
            if let value = try? container.decode(UInt64.self, forKey: key) { return value }
            if let value = try? container.decode(Double.self, forKey: key), value > 0 {
                return UInt64(value)
            }
            if let text = try? container.decode(String.self, forKey: key), let value = UInt64(text) {
                return value
            }
        }
        return nil
    }

    private static func int(
        _ container: KeyedDecodingContainer<CodingKeys>,
        _ keys: [CodingKeys]
    ) -> Int? {
        for key in keys {
            if let value = try? container.decode(Int.self, forKey: key) { return value }
            if let value = try? container.decode(Double.self, forKey: key) { return Int(value) }
        }
        return nil
    }

    /// 找「最后一段路径里的冒号」，避开 registry 端口写法（host:5000/xxx）
    static func tagSeparator(in reference: String) -> String.Index? {
        guard let slash = reference.lastIndex(of: "/") else {
            return reference.contains(":") ? reference.lastIndex(of: ":") : nil
        }
        let tail = reference[reference.index(after: slash)...]
        return tail.lastIndex(of: ":")
    }

    private static func dateText(_ raw: String?) -> String? {
        guard let raw, !raw.isEmpty else { return nil }
        guard raw.allSatisfy({ $0.isNumber }), let number = Double(raw) else { return raw }
        // 秒和毫秒都可能出现
        let seconds = number > 1_000_000_000_000 ? number / 1000 : number
        guard seconds > 1_000_000_000 else { return raw }
        return Format.timestamp(Date(timeIntervalSince1970: seconds))
    }
}

// MARK: - 服务

/// Docker 镜像管理。
///
/// 已确认（见 docs/api-verified.md 第二节 Docker / 镜像部分）：
/// - 四个端点全是 POST，token 走 query
/// - 列表用 `ShowLocalImageV2`，分页参数 `PageNum` / `PageSize`
/// - 拉取 body 是 `name` / `version`；删除 body 是 `id` / `status` / `imageName` / `imageVersion`
final class ImageService {
    private let client: UgosClient

    init(client: UgosClient) {
        self.client = client
    }

    /// 分页参数大小写各试一遍
    private static let pageBodies: [[String: Any]] = [
        ["PageNum": 1, "PageSize": 500],
        ["pageNum": 1, "pageSize": 500]
    ]

    // MARK: 列表

    func list() async throws -> [DockerImage] {
        var lastError: Error = UgosError.decoding("镜像列表返回为空")

        // 只认 ShowLocalImageV2：ObtainAllImages 是 Docker Hub 商店搜索，
        // 会返回上千个本机没下载的镜像（踩坑 21）
        let path = "v1/docker/image/ShowLocalImageV2"
        for body in ImageService.pageBodies {
            do {
                let data = try await client.post(path, json: body)
                if let items = ImageService.parse(data) {
                    return items
                }
                lastError = UgosError.decoding("镜像列表结构无法识别（\(path)）")
            } catch {
                lastError = error
            }
        }
        throw lastError
    }

    /// 从 {"result": [...]} / {"list": [...]} / 直接数组里取出条目
    static func parse(_ any: Any) -> [DockerImage]? {
        guard let array = arrayPayload(any, depth: 0) else { return nil }
        var items: [DockerImage] = []
        items.reserveCapacity(array.count)
        for element in array {
            guard let dict = element as? [String: Any],
                  let data = try? JSONSerialization.data(withJSONObject: dict),
                  let image = try? JSONDecoder().decode(DockerImage.self, from: data) else { continue }
            items.append(image)
        }
        return items
    }

    private static func arrayPayload(_ any: Any, depth: Int) -> [Any]? {
        if let array = any as? [Any] { return array }
        guard depth < 2, let dict = any as? [String: Any] else { return nil }
        let keys = ["result", "list", "images", "data", "items", "imageList", "image_list"]
        for key in keys {
            if let value = dict[key], let array = arrayPayload(value, depth: depth + 1) {
                return array
            }
        }
        return nil
    }

    // MARK: 删除 / 拉取

    func delete(_ image: DockerImage) async throws {
        guard let id = image.imageId, !id.isEmpty else {
            throw UgosError.auth("该镜像缺少 id，无法删除")
        }
        // 实测必须四个字段齐全；status 固定 1（踩坑 25：对不存在的镜像也回 successful）
        let body: [String: Any] = [
            "id": id,
            "status": 1,
            "imageName": image.repository,
            "imageVersion": image.tag
        ]
        try await postFirst(
            paths: ["v1/docker/image/DeleteImage"],
            bodies: [body],
            fallbackMessage: "删除镜像失败"
        )
    }

    /// 拉取镜像。服务端若是异步任务，这里只能确认「请求已被接受」，
    /// 真实进度由界面轮询列表是否出现该镜像来判断。
    func pull(_ reference: String) async throws {
        let spec = reference.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !spec.isEmpty else { throw UgosError.auth("请填写镜像名") }

        let split = ImageService.split(spec)
        // 实测字段名是 name / version，写成 imageName / imageVersion 会报 2052（踩坑 21 段表格）
        let body: [String: Any] = ["name": split.repository, "version": split.tag]
        try await postFirst(
            paths: ["v1/docker/image/DownloadImage"],
            bodies: [body],
            fallbackMessage: "拉取镜像失败"
        )
    }

    private func postFirst(
        paths: [String],
        bodies: [[String: Any]],
        fallbackMessage: String
    ) async throws {
        var lastError: Error = UgosError.decoding(fallbackMessage)
        for path in paths {
            for body in bodies {
                do {
                    _ = try await client.post(path, json: body)
                    return
                } catch {
                    lastError = error
                }
            }
        }
        throw lastError
    }

    /// 拆 "repo:tag"；带 registry 端口时按最后一段路径处理
    static func split(_ reference: String) -> (repository: String, tag: String) {
        guard let colon = DockerImage.tagSeparator(in: reference) else {
            return (reference, "latest")
        }
        return (String(reference[..<colon]), String(reference[reference.index(after: colon)...]))
    }
}
