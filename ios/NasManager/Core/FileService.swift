import Foundation

// MARK: - 排序

/// 排序字段。`sortType` 与 NAS 的 `sort_type` 序号严格对应（1 名称 / 2 大小 / 3 类型 /
/// 4 修改 / 5 创建 / 6 访问 / 7 删除）。
enum FileSort: String, CaseIterable, Identifiable {
    case name
    case size
    case type
    case mtime
    case ctime
    case atime
    case dtime

    var id: String { rawValue }

    var sortType: Int {
        switch self {
        case .name: return 1
        case .size: return 2
        case .type: return 3
        case .mtime: return 4
        case .ctime: return 5
        case .atime: return 6
        case .dtime: return 7
        }
    }

    var label: String {
        switch self {
        case .name: return "名称"
        case .size: return "大小"
        case .type: return "类型"
        case .mtime: return "修改时间"
        case .ctime: return "创建时间"
        case .atime: return "访问时间"
        case .dtime: return "删除时间"
        }
    }

    /// 界面上只暴露这三项，其余留给以后
    static let primary: [FileSort] = [.name, .size, .mtime]
}

// MARK: - 根分类

/// 根分类。`root_type` 是 NAS 的数字编号，只有本机真实存在的那些会被返回。
struct FileCategory: Codable, Identifiable, Hashable {
    let rootType: Int
    let key: String
    let name: String

    var id: Int { rootType }

    enum CodingKeys: String, CodingKey {
        case rootType = "root_type"
        case key
        case name
    }

    var icon: String {
        switch rootType {
        case 1: return "star"
        case 2: return "person.crop.circle"
        case 3: return "person.2"
        case 4: return "person.text.rectangle"
        case 5: return "network"
        case 6: return "externaldrive"
        case 7: return "opticaldiscdrive"
        case 8: return "square.and.arrow.down"
        case 9: return "tag"
        default: return "folder"
        }
    }
}

// MARK: - 卷

struct FileVolume: Codable, Identifiable, Hashable {
    let name: String
    let path: String?
    let fsType: String?
    let total: UInt64
    let used: UInt64
    let free: UInt64
    let usedRatio: Double?

    var id: String { path ?? name }

    /// `used_ratio` 缺失（或为 null）时按 used/total 现算，不拿 0 冒充
    var usedPercent: Double {
        if let usedRatio, usedRatio >= 0 { return min(max(usedRatio * 100, 0), 100) }
        guard total > 0 else { return 0 }
        return min(max(Double(used) / Double(total) * 100, 0), 100)
    }

    enum CodingKeys: String, CodingKey {
        case name
        case path
        case fsType = "fs_type"
        case total
        case used
        case free
        case usedRatio = "used_ratio"
        /// NAS 原生 `getVolumes` 用 `all` 表示总量
        case all
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = Lenient.string(c, .name) ?? ""
        path = Lenient.string(c, .path)
        fsType = Lenient.string(c, .fsType)
        total = Lenient.uint64(c, .total) ?? Lenient.uint64(c, .all) ?? 0
        used = Lenient.uint64(c, .used) ?? 0
        free = Lenient.uint64(c, .free) ?? 0
        usedRatio = Lenient.double(c, .usedRatio)
    }
}

// MARK: - 根信息

struct FileRoots: Codable, Hashable {
    let categories: [FileCategory]
    let volumes: [FileVolume]
}

// MARK: - 目录条目

/// 一条文件 / 目录 / 标签。
///
/// 判定规则以实测为准：`is_dir` 是目录的唯一依据（缺字段时退回 `file_type == 1`），
/// `is_tag` 的条目是「标签」而不是文件——既不能下载也不能删除。
struct FileEntry: Codable, Identifiable, Hashable {
    let name: String
    let path: String
    let isDir: Bool
    let fileType: Int?
    let size: UInt64?
    let ext: String?
    let extGroup: String
    let mtime: Int?
    let ctime: Int?
    let permissionMask: Int?
    let icon: Int?
    let intranetShareID: Int?
    let isMount: Bool
    let recycleStatus: Int?
    /// 标签根下的条目本身就是标签定义，未必带 `tag_id`，由服务层按 `root_type` 补标
    var isTag: Bool
    let tagID: Int?
    let tagColour: Int?

    var id: String { path.isEmpty ? "\(name)|\(mtime ?? 0)" : path }

    /// 标签与目录都不能下载；删除只对真实文件/目录开放
    var isDownloadable: Bool { !isDir && !isTag }
    var isDeletable: Bool { !isTag }
    var isRenamable: Bool { !isTag }

    var sizeText: String { isDir ? "--" : Format.bytes(size) }

    var mtimeText: String {
        guard let mtime, mtime > 0 else { return "-" }
        return Format.timestamp(Date(timeIntervalSince1970: TimeInterval(mtime)))
    }

    var iconName: String {
        if isTag { return "tag" }
        if isDir { return isMount ? "externaldrive" : "folder.fill" }
        switch extGroup {
        case "image": return "photo"
        case "video": return "film"
        case "audio": return "music.note"
        case "archive": return "archivebox"
        case "document": return "doc.text"
        case "code": return "chevron.left.forwardslash.chevron.right"
        default: return "doc"
        }
    }

    enum CodingKeys: String, CodingKey {
        case name
        case path
        case isDir = "is_dir"
        case fileType = "file_type"
        case size
        case ext
        case extGroup = "ext_group"
        case mtime
        case ctime
        case permissionMask = "permission_mask"
        case icon
        case intranetShareID = "intranet_share_id"
        case isMount = "is_mount"
        case recycleStatus = "recycle_status"
        case isTag = "is_tag"
        case tagID = "tag_id"
        case tagColour = "tag_colour"
        /// NAS 原生条目把标签颜色放在 `colour`，挂载信息放在 `mount.is_mount`
        case colour
        case mount
    }

    struct MountInfo: Codable {
        let isMount: Bool?

        enum CodingKeys: String, CodingKey {
            case isMount = "is_mount"
        }
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)

        // 全部先算成局部值：init 里读已赋值的属性会触发「self used before all
        // stored properties are initialized」，编译不过
        let nameValue = Lenient.string(c, .name) ?? ""
        let pathValue = Lenient.string(c, .path) ?? ""
        let fileTypeValue = Lenient.int(c, .fileType)
        let extValue = Lenient.string(c, .ext)

        // is_dir 是判断目录的唯一依据；缺字段时退回 file_type == 1
        let dirValue = Lenient.bool(c, .isDir) ?? (fileTypeValue == 1)

        // 标签根返回的是标签定义本身，没有 file_type，只能靠 tag_id 识别
        let tagIdValue = Lenient.int(c, .tagID)
        let tagValue = Lenient.bool(c, .isTag) ?? (tagIdValue != nil)

        let mountInfo = (try? c.decodeIfPresent(MountInfo.self, forKey: .mount)) ?? nil
        let mountValue = Lenient.bool(c, .isMount) ?? mountInfo?.isMount ?? false

        let groupValue = Lenient.string(c, .extGroup)
            ?? (dirValue ? "folder" : FileEntry.group(for: extValue))

        name = nameValue
        path = pathValue
        fileType = fileTypeValue
        size = Lenient.uint64(c, .size)
        ext = extValue
        mtime = Lenient.int(c, .mtime)
        ctime = Lenient.int(c, .ctime)
        permissionMask = Lenient.int(c, .permissionMask)
        icon = Lenient.int(c, .icon)
        intranetShareID = Lenient.int(c, .intranetShareID)
        recycleStatus = Lenient.int(c, .recycleStatus)
        isDir = dirValue
        tagID = tagIdValue
        isTag = tagValue
        tagColour = Lenient.int(c, .tagColour) ?? Lenient.int(c, .colour)
        isMount = mountValue
        extGroup = groupValue
    }

    /// 后缀 → 分组，仅用于选图标；与 NAS 语义无关
    static func group(for ext: String?) -> String {
        guard let ext, !ext.isEmpty else { return "other" }
        switch ext.lowercased() {
        case "jpg", "jpeg", "png", "gif", "webp", "heic", "bmp", "svg", "tiff", "avif":
            return "image"
        case "mp4", "mkv", "mov", "avi", "wmv", "flv", "webm", "m4v", "rmvb":
            return "video"
        case "mp3", "flac", "wav", "aac", "m4a", "ogg", "ape", "wma":
            return "audio"
        case "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "zst":
            return "archive"
        case "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv", "rtf", "epub", "mobi", "log":
            return "document"
        case "py", "js", "json", "yml", "yaml", "sh", "go", "rs", "java", "c", "cpp", "h", "swift", "kt", "html", "css", "sql", "toml", "ini":
            return "code"
        default:
            return "other"
        }
    }
}

// MARK: - 目录列表

struct FileListing: Codable, Hashable {
    let path: String?
    let rootType: Int?
    /// 树根，**不是**当前目录；面包屑由视图自己维护的栈负责
    let topPath: String
    let permissionMask: Int
    let page: Int
    let limit: Int
    let total: Int
    let hasMore: Bool
    let sort: String
    let reverse: Bool
    let entries: [FileEntry]

    enum CodingKeys: String, CodingKey {
        case path
        case rootType = "root_type"
        case topPath = "top_path"
        case permissionMask = "permission_mask"
        case page
        case limit
        case total
        case hasMore = "has_more"
        case sort
        case reverse
        case entries
    }
}

// MARK: - 剩余空间 / 删除结果

struct FileSpace: Codable, Hashable {
    let path: String
    let free: UInt64?

    enum CodingKeys: String, CodingKey {
        case path
        case free
        /// NAS 原生 `getPathFreeSize` 把字节数放在 `result` 里
        case result
    }

    init(path: String, free: UInt64?) {
        self.path = path
        self.free = free
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = Lenient.string(c, .path) ?? ""
        free = Lenient.uint64(c, .free) ?? Lenient.uint64(c, .result)
    }
}

struct FileDeleteResult: Codable, Hashable {
    var deleted: [String]
    var forever: Bool
}

// MARK: - 原始返回包装

/// `v2/filemgr/getDirFileListV2` 的原始返回：只取右侧列表，左侧目录树传 `data_type: 2` 不给。
struct FileDirListPayload: Decodable {
    let rightFiles: RightFiles?

    struct RightFiles: Decodable {
        let files: [FileEntry]
        let total: Int
        let topPath: String?
        let permissionMask: Int?

        enum CodingKeys: String, CodingKey {
            case files
            case total
            case topPath = "top_path"
            case permissionMask = "permission_mask"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            let fileList = (try? c.decodeIfPresent([FileEntry].self, forKey: .files)) ?? nil ?? []
            files = fileList
            total = Lenient.int(c, .total) ?? fileList.count
            topPath = Lenient.string(c, .topPath)
            permissionMask = Lenient.int(c, .permissionMask)
        }
    }

    enum CodingKeys: String, CodingKey {
        case rightFiles = "right_files"
    }
}

/// `v2/filemgr/getRootDirs` 的原始返回：`root_list` 是数字编号数组
struct FileRootDirsPayload: Decodable {
    let rootList: [RootNumber]

    /// 兼容两种固件写法：纯数字，或 `{root_type: 2}` 这样的对象
    struct RootNumber: Decodable {
        let value: Int

        enum CodingKeys: String, CodingKey {
            case rootType = "root_type"
            case rootTypeCamel = "rootType"
            case id
            case number
            case category
        }

        init(from decoder: Decoder) throws {
            if let single = try? decoder.singleValueContainer(),
               let number = try? single.decode(Int.self) {
                value = number
                return
            }
            let c = try decoder.container(keyedBy: CodingKeys.self)
            value = Lenient.int(c, .rootType)
                ?? Lenient.int(c, .rootTypeCamel)
                ?? Lenient.int(c, .id)
                ?? Lenient.int(c, .number)
                ?? Lenient.int(c, .category)
                ?? 0
        }
    }

    enum CodingKeys: String, CodingKey {
        case rootList = "root_list"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rootList = (try? c.decodeIfPresent([RootNumber].self, forKey: .rootList)) ?? nil ?? []
    }
}

/// `v1/filemgr/getVolumes` 的原始返回
struct FileVolumePayload: Decodable {
    let result: [FileVolume]

    enum CodingKeys: String, CodingKey {
        case result
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        result = (try? c.decodeIfPresent([FileVolume].self, forKey: .result)) ?? nil ?? []
    }
}

// MARK: - 宽松取值

/// NAS 同一字段在不同固件里可能是 Int / String / Double，逐个试，取不到就 nil。
/// 缺字段或类型不符都不能让整份解码失败。
private enum Lenient {
    static func string<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> String? {
        if let value = try? c.decodeIfPresent(String.self, forKey: key) {
            let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return trimmed.isEmpty ? nil : trimmed
        }
        if let value = try? c.decodeIfPresent(Int.self, forKey: key) { return value.map { String($0) } }
        if let value = try? c.decodeIfPresent(Double.self, forKey: key) { return value.map { String(Int($0)) } }
        return nil
    }

    static func int<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> Int? {
        if let value = try? c.decodeIfPresent(Int.self, forKey: key) { return value }
        if let value = try? c.decodeIfPresent(String.self, forKey: key) {
            return value.flatMap { Int($0.trimmingCharacters(in: .whitespaces)) }
        }
        if let value = try? c.decodeIfPresent(Double.self, forKey: key) { return value.map { Int($0) } }
        return nil
    }

    static func uint64<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> UInt64? {
        if let value = try? c.decodeIfPresent(UInt64.self, forKey: key) { return value }
        if let value = try? c.decodeIfPresent(Int.self, forKey: key) {
            return value.flatMap { $0 >= 0 ? UInt64($0) : nil }
        }
        if let value = try? c.decodeIfPresent(Double.self, forKey: key) {
            return value.flatMap { $0 >= 0 ? UInt64($0) : nil }
        }
        if let value = try? c.decodeIfPresent(String.self, forKey: key) {
            return value.flatMap { UInt64($0.trimmingCharacters(in: .whitespaces)) }
        }
        return nil
    }

    static func double<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> Double? {
        if let value = try? c.decodeIfPresent(Double.self, forKey: key) { return value }
        if let value = try? c.decodeIfPresent(Int.self, forKey: key) { return value.map { Double($0) } }
        if let value = try? c.decodeIfPresent(String.self, forKey: key) {
            return value.flatMap { Double($0.trimmingCharacters(in: .whitespaces)) }
        }
        return nil
    }

    static func bool<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> Bool? {
        if let value = try? c.decodeIfPresent(Bool.self, forKey: key) { return value }
        if let value = try? c.decodeIfPresent(Int.self, forKey: key) { return value.map { $0 != 0 } }
        if let value = try? c.decodeIfPresent(String.self, forKey: key) {
            return value.flatMap { text -> Bool? in
                switch text.lowercased() {
                case "true", "1", "yes": return true
                case "false", "0", "no": return false
                default: return nil
                }
            }
        }
        return nil
    }
}

// MARK: - 服务

/// 文件管理：浏览 / 下载 / 新建目录 / 重命名 / 删除 / 剩余空间。
///
/// 走的是 NAS 自己的 `filemgr` 应用接口（`v2/filemgr/*`），因为 App 是直连 NAS 的：
/// `UgosClient` 只认 `ugreen/` 前缀 + `token` query + `{code: 200}` 外壳，既到不了别的
/// base path，也没有 Bearer 之类的第二套鉴权。字段语义仍以下游契约的实测结论为准：
/// - `path` 与 `root_type` 互斥（同时给会被判「参数不完整」直接 1301）
/// - `top_path` 是树的根，不是当前目录
/// - 传卷路径（如 `/volume1`）拿 1301「不允许访问」属正常权限行为，要给人话而不是崩
/// - `file_type == 1` 才是目录；标签条目不参与下载/删除
final class FileService {
    private let client: UgosClient
    private let endpoint: UgosEndpoint
    /// 下载第三步要裸取字节，而 UgosClient 的通道只解析 JSON，所以单独开一个会话
    /// （与 `EndpointResolver.isReachable` 同款：TOFU 校验 + 临时会话）
    private let session: URLSession
    private let trustDelegate: TOFUTrustDelegate

    /// 这些头在 UgosClient 里是私有的，下载端点不在它的 JSON 通道上，只能在此重复一份
    private static let agentHeaders: [String: String] = [
        "UG-Agent": "PC/WEB",
        "Client-Version": "78471",
        "Client-Version-Str": "1.19.0",
        "X-Specify-Language": "zh-CN"
    ]

    /// 标签根编号：这一档下返回的是标签定义而不是文件
    static let tagRootType = 9

    /// 数字编号 → 分类名（NAS 只给数字，文案来自 UGOS 前端静态表）
    static let rootCategories: [Int: (key: String, name: String)] = [
        1: ("collect", "收藏"),
        2: ("personal", "个人文件夹"),
        3: ("common", "共享文件夹"),
        4: ("users", "用户文件夹"),
        5: ("network", "远程挂载"),
        6: ("device", "外部设备"),
        7: ("mirroring", "镜像"),
        8: ("share", "共享给我"),
        9: ("label", "标签")
    ]

    init(client: UgosClient, endpoint: UgosEndpoint) {
        self.client = client
        self.endpoint = endpoint
        let delegate = TOFUTrustDelegate()
        self.trustDelegate = delegate
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 600
        config.httpCookieStorage = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        self.session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
    }

    // MARK: 根分类与卷

    /// 可浏览的根分类 + 卷容量。分类只返回本机 `root_list` 里真实存在的那些。
    func roots() async throws -> FileRoots {
        let payload = try await client.getDecodable(
            FileRootDirsPayload.self,
            path: "v2/filemgr/getRootDirs",
            params: ["client": "pc"]
        )

        var categories: [FileCategory] = []
        var seen: Set<Int> = []
        for item in payload.rootList {
            guard item.value > 0, !seen.contains(item.value) else { continue }
            guard let meta = FileService.rootCategories[item.value] else { continue }
            seen.insert(item.value)
            categories.append(
                FileCategory(rootType: item.value, key: meta.key, name: meta.name)
            )
        }

        var volumes: [FileVolume] = []
        do {
            let volumePayload = try await client.getDecodable(
                FileVolumePayload.self, path: "v1/filemgr/getVolumes"
            )
            volumes = volumePayload.result
        } catch {
            // 卷信息是锦上添花，取不到不影响浏览
        }

        return FileRoots(categories: categories, volumes: volumes)
    }

    // MARK: 列表

    /// 列目录。根级给 `rootType`，子目录给 `path`，二选一。
    func list(
        path: String? = nil,
        rootType: Int? = nil,
        page: Int = 1,
        limit: Int = 200,
        sort: FileSort = .name,
        reverse: Bool = false
    ) async throws -> FileListing {
        // path 与 root_type 在 NAS 侧互斥：都给会被判「参数不完整」直接 1301
        guard (path?.isEmpty == false) != (rootType != nil) else {
            throw UgosError.auth(rootType == nil ? "请指定要浏览的目录" : "目录与分类只能给一个")
        }

        let safePage = max(1, page)
        let safeLimit = max(1, min(limit, 1000))

        var body: [String: Any] = [
            "limit": safeLimit,
            "page": safePage,
            "is_shield_recycle": false,
            "data_type": 2,          // 2 = 只要右侧列表，不要左侧目录树
            "left_no_page_show": false,
            "left_count": 5000,
            "sort_type": sort.sortType,
            "reverse": reverse,
            "intranet_share_id": 0,
            "permission": 0,
            "version": 1,
            "operate": 0
        ]
        if let path, !path.isEmpty {
            body["path"] = path
        } else if let rootType {
            body["root_type"] = rootType
            // 共享文件夹根必须带 permission=4，否则列不出来
            if rootType == 3 { body["permission"] = 4 }
        }

        let payload = try await client.postDecodable(
            FileDirListPayload.self,
            path: "v2/filemgr/getDirFileListV2",
            json: body
        )
        let right = payload.rightFiles
        var entries = right?.files ?? []
        // 标签根（root_type=9）返回的是标签定义，不是文件：下载/删除一律不开放
        if rootType == FileService.tagRootType {
            entries = entries.map { entry -> FileEntry in
                guard !entry.isTag else { return entry }
                var copy = entry
                copy.isTag = true
                return copy
            }
        }
        let total = right?.total ?? entries.count

        return FileListing(
            path: (path?.isEmpty == false) ? path : nil,
            rootType: rootType,
            topPath: right?.topPath ?? "",
            permissionMask: right?.permissionMask ?? 0,
            page: safePage,
            limit: safeLimit,
            total: total,
            hasMore: safePage * safeLimit < total,
            sort: sort.rawValue,
            reverse: reverse,
            entries: entries
        )
    }

    // MARK: 下载

    /// 下载单个文件。NAS 是三步链路：建任务 → 换 `dl_token` → 用 `dl_token` 取字节。
    /// 第三步的 token 与会话 token 不通用（前端同样关掉了 tokenAuth），只能裸取原始字节。
    func download(path: String) async throws -> Data {
        guard !path.isEmpty else { throw UgosError.auth("缺少要下载的路径") }

        let task = try await client.post(
            "v2/filemgr/addPathsTask",
            json: ["paths": [["path": path, "intranet_share_id": 0] as [String: Any]]]
        )
        guard let taskId = FileService.text(FileService.field(task, "result")), !taskId.isEmpty else {
            throw UgosError.auth("NAS 未返回下载任务号")
        }

        let tokenPayload = try await client.get(
            "v2/filemgr/getDownloadToken",
            params: ["task_uuid": taskId, "coding": "true", "is_livephoto": "false"]
        )
        guard let dlToken = FileService.text(FileService.field(tokenPayload, "dl_token")), !dlToken.isEmpty else {
            throw UgosError.auth("NAS 未返回下载令牌")
        }

        return try await fetchBytes(dlToken: dlToken)
    }

    private func fetchBytes(dlToken: String) async throws -> Data {
        guard let base = endpoint.baseURL,
              var comps = URLComponents(
                url: base.appendingPathComponent("ugreen/v2/filemgr/downloadFile"),
                resolvingAgainstBaseURL: false
              ) else {
            throw UgosError.transport("地址非法：\(endpoint.displayName)")
        }
        comps.queryItems = [URLQueryItem(name: "token", value: dlToken)]
        guard let url = comps.url else { throw UgosError.transport("URL 构造失败") }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        for (key, value) in FileService.agentHeaders {
            request.setValue(value, forHTTPHeaderField: key)
        }

        do {
            // 注意：NAS 用 chunked 传输，响应没有 Content-Length，不能靠它判断完整性
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw UgosError.transport("响应不是 HTTP")
            }
            guard (200..<300).contains(http.statusCode) else {
                let text = String(data: data, encoding: .utf8) ?? ""
                throw UgosError.business(
                    code: http.statusCode,
                    message: FileService.serverMessage(from: text) ?? "下载失败",
                    path: "filemgr/downloadFile"
                )
            }
            return data
        } catch let error as UgosError {
            throw error
        } catch {
            throw UgosError.transport(error.localizedDescription)
        }
    }

    // MARK: 写操作

    /// 新建目录。NAS 的 `createFolder` 要的是**新目录的完整路径**，不是父目录。
    func createFolder(parent: String, name: String) async throws -> FileEntry? {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw UgosError.auth("文件夹名不能为空") }
        guard !trimmed.contains("/"), trimmed != ".", trimmed != ".." else {
            throw UgosError.auth("文件夹名不能包含 / 也不能是 . 或 ..")
        }
        guard !parent.isEmpty else { throw UgosError.auth("缺少父目录路径") }

        let target = parent.hasSuffix("/") ? parent + trimmed : parent + "/" + trimmed
        let result = try await client.post("v2/filemgr/createFolder", json: ["path": target])
        return FileService.decodeEntry(result)
    }

    func rename(path: String, newName: String) async throws -> FileEntry? {
        let trimmed = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw UgosError.auth("新名称不能为空") }
        guard !trimmed.contains("/"), trimmed != ".", trimmed != ".." else {
            throw UgosError.auth("新名称不能包含 / 也不能是 . 或 ..")
        }
        let result = try await client.post(
            "v2/filemgr/rename", json: ["path": path, "new_name": trimmed]
        )
        return FileService.decodeEntry(result)
    }

    /// 删除。`paths` 必须是字符串数组，传对象数组 NAS 会回 `9999 操作失败!`。
    /// 默认进回收站，`forever: true` 才是直接删。
    func delete(paths: [String], forever: Bool = false) async throws -> FileDeleteResult {
        let cleaned = paths.filter { !$0.isEmpty }
        guard !cleaned.isEmpty else { throw UgosError.auth("没有可删除的路径") }
        _ = try await client.post(
            "v2/filemgr/delPaths", json: ["paths": cleaned, "forever": forever]
        )
        return FileDeleteResult(deleted: cleaned, forever: forever)
    }

    // MARK: 空间

    func freeSpace(path: String) async throws -> FileSpace {
        guard !path.isEmpty else { throw UgosError.auth("缺少路径") }
        let payload = try await client.postDecodable(
            FileSpace.self, path: "v1/filemgr/getPathFreeSize", json: ["path": path]
        )
        return FileSpace(path: path, free: payload.free)
    }

    // MARK: - 错误翻译

    /// 把 NAS 的错误码翻成人话。1301 是越权/参数被判「不允许访问」，属正常权限行为。
    static func friendly(_ error: Error) -> String {
        let raw = error.localizedDescription
        if raw.contains("1301") || raw.contains("不允许访问") {
            return "没有权限访问该目录"
        }
        if raw.contains("1327") || raw.contains("存在同名") {
            return "已存在同名的文件或文件夹"
        }
        if raw.contains("9999") {
            return "操作失败，请稍后重试（\(raw)）"
        }
        return raw
    }

    // MARK: - 解析辅助

    private static func decodeEntry(_ any: Any) -> FileEntry? {
        guard JSONSerialization.isValidJSONObject(any),
              let data = try? JSONSerialization.data(withJSONObject: any) else { return nil }
        return try? JSONDecoder().decode(FileEntry.self, from: data)
    }

    private static func field(_ any: Any, _ key: String) -> Any? {
        (any as? [String: Any])?[key]
    }

    private static func text(_ any: Any?) -> String? {
        if let text = any as? String {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }
        if let number = any as? Int { return String(number) }
        if let number = any as? Double { return String(Int(number)) }
        return nil
    }

    /// 错误体可能是 `{"error": {"code", "message"}}`、`{"detail": "..."}` 或 `{"msg": "..."}`
    private static func serverMessage(from raw: String) -> String? {
        guard let data = raw.data(using: .utf8),
              let parsed = try? JSONSerialization.jsonObject(with: data),
              let object = parsed as? [String: Any] else {
            return raw.isEmpty ? nil : raw
        }
        if let error = object["error"] as? [String: Any] {
            if let message = error["message"] as? String, !message.isEmpty { return message }
            if let code = error["code"] as? Int { return "错误码 \(code)" }
        }
        if let detail = object["detail"] as? String, !detail.isEmpty { return detail }
        if let message = object["msg"] as? String, !message.isEmpty { return message }
        return nil
    }
}
