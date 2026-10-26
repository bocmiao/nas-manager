import Foundation

// MARK: - 快照文件夹

/// UGOS 的快照按「文件夹」组织，不是按卷。
/// `folder_type` 只有两种：`share`（共享文件夹）与 `home`（用户家目录）。
struct SnapshotFolder: Hashable, Identifiable {
    let folderType: String
    let folderId: Int
    let name: String
    let path: String?
    let snapshotCount: Int?

    var id: String { "\(folderType)|\(folderId)" }

    var displayName: String {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return trimmed }
        return "\(folderType) #\(folderId)"
    }

    var typeLabel: String { folderType == "home" ? "家目录" : "共享文件夹" }
}

// MARK: - 一条快照

struct SnapshotItem: Identifiable, Hashable {
    /// UGOS 用快照名定位删除/回滚，不是 id
    let name: String
    let folderType: String
    let folderId: Int
    let folderName: String?
    let createdText: String?
    let size: UInt64?
    let remark: String?

    var id: String { "\(folderType)|\(folderId)|\(name)" }

    var displayName: String { name }

    /// 界面按「卷」展示，这里映射到文件夹名，避免视图层大改
    var volumeName: String? { folderName }

    var folderText: String {
        let trimmed = (folderName ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let type = folderType == "home" ? "家目录" : "共享"
        return trimmed.isEmpty ? type : "\(type) · \(trimmed)"
    }
}

// MARK: - 查询结果

struct SnapshotQueryResult {
    var items: [SnapshotItem]
    var folders: [SnapshotFolder] = []
    /// false 表示该机型/固件没有可用快照能力（如 ext4 卷），界面渲染说明而不是「0 条」
    var available: Bool
    var message: String?
    /// 探测/请求命中的端点，便于排查
    var endpoint: String?
}

// MARK: - 服务

/// 存储快照。
///
/// 契约（实测）：快照接口挂在独立应用 `com.ugreen.snapshot` 下，受 **Btrfs 限制**——
/// 卷文件系统不是 btrfs 时，列表返回 `32003 文件夹不存在`，创建/回滚返回
/// `9999 folderServ.GetBaseFolder(): mnt point of xxx is not btrfs filesystem`。
/// 因此 `available` 必须如实反映能力，不能把「不支持」误报成「没有快照」。
final class SnapshotService {
    private let client: UgosClient

    static let folderTypes = ["share", "home"]

    init(client: UgosClient) {
        self.client = client
    }

    // MARK: 文件夹

    /// 拉取全部文件夹（share + home）。任一成功即认为接口存在。
    func folders() async -> (items: [SnapshotFolder], available: Bool, message: String?) {
        var collected: [SnapshotFolder] = []
        var firstError: String?
        var anySucceeded = false

        for folderType in SnapshotService.folderTypes {
            do {
                let data = try await client.get(
                    "v1/snapshot/folder/\(folderType)",
                    params: ["page_number": "1", "page_size": "200"]
                )
                anySucceeded = true
                collected.append(contentsOf: SnapshotService.parseFolders(data, folderType: folderType))
            } catch {
                if firstError == nil { firstError = error.localizedDescription }
            }
        }

        if anySucceeded {
            return (collected, true, nil)
        }
        return ([], false, SnapshotService.friendly(firstError))
    }

    // MARK: 列表

    /// 聚合所有文件夹下的快照。本机若是 ext4，会以 available=false 返回。
    func list() async -> SnapshotQueryResult {
        let folderResult = await folders()

        guard folderResult.available else {
            return SnapshotQueryResult(
                items: [],
                folders: [],
                available: false,
                message: folderResult.message ?? "该存储不支持快照（需要 Btrfs 文件系统）",
                endpoint: "v1/snapshot/folder"
            )
        }

        guard !folderResult.items.isEmpty else {
            return SnapshotQueryResult(
                items: [],
                folders: [],
                available: true,
                message: "NAS 上还没有可快照的文件夹",
                endpoint: "v1/snapshot/folder"
            )
        }

        var all: [SnapshotItem] = []
        var firstError: String?

        for folder in folderResult.items {
            do {
                all.append(contentsOf: try await snapshots(in: folder))
            } catch {
                if firstError == nil { firstError = error.localizedDescription }
            }
        }

        return SnapshotQueryResult(
            items: all,
            folders: folderResult.items,
            available: true,
            message: all.isEmpty ? (firstError.map(SnapshotService.friendly) ?? "这些文件夹还没有快照") : nil,
            endpoint: "v1/snapshot/snapshot"
        )
    }

    /// 单个文件夹下的快照
    func snapshots(in folder: SnapshotFolder) async throws -> [SnapshotItem] {
        let data = try await client.get(
            "v1/snapshot/snapshot",
            params: [
                "folder_type": folder.folderType,
                "folder_id": String(folder.folderId),
                "page_number": "1",
                "page_size": "100"
            ]
        )
        return SnapshotService.parseSnapshots(data, folder: folder)
    }

    // MARK: 写操作

    /// 创建快照。`comment` 字段名未验证，服务端可能忽略。
    func create(folder: SnapshotFolder, comment: String) async throws {
        var body: [String: Any] = [
            "folder_type": folder.folderType,
            "folder_id": folder.folderId
        ]
        let note = comment.trimmingCharacters(in: .whitespacesAndNewlines)
        if !note.isEmpty { body["comment"] = note }
        _ = try await client.post("v1/snapshot/snapshot", json: body)
    }

    /// 兼容旧调用：按显示名匹配文件夹。匹配不到直接抛错，绝不退化成「第一个文件夹」。
    func create(volumeName: String, remark: String, folders: [SnapshotFolder]) async throws {
        let target = SnapshotService.matchFolder(name: volumeName, in: folders)
        guard let target else {
            throw UgosError.auth("找不到名为「\(volumeName)」的快照文件夹")
        }
        try await create(folder: target, comment: remark)
    }

    /// 删除快照。UGOS 对不存在的快照同样返回成功，所以调用后应重新拉列表确认。
    func delete(_ item: SnapshotItem) async throws {
        let body: [String: Any] = [
            "folder_type": item.folderType,
            "folder_id": item.folderId,
            "snapshots": [item.name]
        ]
        _ = try await client.delete("v1/snapshot/snapshot", json: body)
    }

    /// 回滚。会覆盖文件夹当前内容且不可撤销，界面必须二次确认后再调用。
    func restore(_ item: SnapshotItem) async throws {
        let body: [String: Any] = [
            "folder_type": item.folderType,
            "folder_id": item.folderId,
            "snapshot": item.name
        ]
        _ = try await client.post("v1/snapshot/snapshot/restore", json: body)
    }

    // MARK: - 解析

    /// 只按文件夹名精确匹配（不区分大小写）。匹配不到返回 nil，由调用方报错。
    static func matchFolder(name: String, in folders: [SnapshotFolder]) -> SnapshotFolder? {
        let target = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !target.isEmpty else { return nil }
        if let hit = folders.first(where: { $0.name == target }) { return hit }
        return folders.first(where: {
            $0.name.compare(target, options: .caseInsensitive) == .orderedSame
        })
    }

    private static func parseFolders(_ any: Any, folderType: String) -> [SnapshotFolder] {
        guard let dict = any as? [String: Any],
              let raw = dict["folders"] as? [Any] else { return [] }

        var result: [SnapshotFolder] = []
        result.reserveCapacity(raw.count)
        for element in raw {
            guard let item = element as? [String: Any] else { continue }
            guard let folderId = SnapshotService.int(item["id"]) ?? SnapshotService.int(item["folder_id"]) else {
                continue
            }
            result.append(
                SnapshotFolder(
                    folderType: folderType,
                    folderId: folderId,
                    name: SnapshotService.string(item["name"]) ?? SnapshotService.string(item["folder_name"]) ?? "",
                    path: SnapshotService.string(item["path"]),
                    snapshotCount: SnapshotService.int(item["snapshot_number"]) ?? SnapshotService.int(item["count"])
                )
            )
        }
        return result
    }

    private static func parseSnapshots(_ any: Any, folder: SnapshotFolder) -> [SnapshotItem] {
        guard let array = arrayPayload(any, depth: 0) else { return [] }

        var result: [SnapshotItem] = []
        result.reserveCapacity(array.count)
        for element in array {
            guard let item = element as? [String: Any] else { continue }
            guard let name = SnapshotService.string(item["name"])
                ?? SnapshotService.string(item["snapshot_name"]) else { continue }

            let createdRaw = SnapshotService.string(item["create_time"])
                ?? SnapshotService.string(item["ctime"])
                ?? SnapshotService.string(item["created_at"])

            result.append(
                SnapshotItem(
                    name: name,
                    folderType: folder.folderType,
                    folderId: folder.folderId,
                    folderName: folder.name,
                    createdText: SnapshotService.dateText(createdRaw),
                    size: SnapshotService.uint64(item["size"])
                        ?? SnapshotService.uint64(item["snapshot_size"])
                        ?? SnapshotService.uint64(item["used"]),
                    remark: SnapshotService.string(item["comment"])
                        ?? SnapshotService.string(item["remark"])
                        ?? SnapshotService.string(item["description"])
                )
            )
        }
        return result
    }

    /// 列表可能在 `data` / `result` / `snapshots` 等层级下
    private static func arrayPayload(_ any: Any, depth: Int) -> [Any]? {
        if let array = any as? [Any] { return array }
        guard depth < 3, let dict = any as? [String: Any] else { return nil }
        for key in ["result", "list", "snapshots", "snapshot_list", "items", "data"] {
            if let value = dict[key], let array = arrayPayload(value, depth: depth + 1) {
                return array
            }
        }
        return nil
    }

    private static func string(_ any: Any?) -> String? {
        if let text = any as? String {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }
        if let number = any as? Int { return String(number) }
        if let number = any as? Double { return String(Int(number)) }
        return nil
    }

    private static func int(_ any: Any?) -> Int? {
        if let value = any as? Int { return value }
        if let value = any as? Double { return Int(value) }
        if let text = any as? String {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if let value = Int(trimmed) { return value }
        }
        return nil
    }

    private static func uint64(_ any: Any?) -> UInt64? {
        if let value = any as? UInt64 { return value }
        if let value = any as? Int, value >= 0 { return UInt64(value) }
        if let value = any as? Double, value >= 0 { return UInt64(value) }
        if let text = any as? String, let value = UInt64(text) { return UInt64(value) }
        return nil
    }

    private static func dateText(_ raw: String?) -> String? {
        guard let raw, !raw.isEmpty else { return nil }
        guard raw.allSatisfy({ $0.isNumber }), let number = Double(raw) else { return raw }
        let seconds = number > 1_000_000_000_000 ? number / 1000 : number
        guard seconds > 1_000_000_000 else { return raw }
        return Format.timestamp(Date(timeIntervalSince1970: seconds))
    }

    /// 把底层错误翻译成人能看懂的说明
    private static func friendly(_ message: String?) -> String {
        let raw = message ?? ""
        let lower = raw.lowercased()
        if lower.contains("btrfs") {
            return "该存储卷是 ext4 文件系统，UGOS 快照只支持 Btrfs"
        }
        if raw.contains("32003") {
            return "快照文件夹不存在（该卷可能不支持快照）"
        }
        if raw.contains("9404") || raw.contains("9405") {
            return "当前固件未开放快照接口"
        }
        return raw.isEmpty ? "快照接口不可用" : raw
    }
}
