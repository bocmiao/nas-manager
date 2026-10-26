import Foundation

/// Docker 容器操作。
///
/// 实测（docs/api-verified.md 第 12、13 条）：
/// - 启停/重启是 **GET + ?containerId=**
/// - 日志是 POST，字段名未确认，需要多组兜底
final class DockerService {
    private let client: UgosClient

    init(client: UgosClient) {
        self.client = client
    }

    func containers() async throws -> [Container] {
        let list = try await client.postDecodable(
            ContainerList.self,
            path: "v1/docker/container/ContainerListV2",
            json: ["PageNum": 1, "PageSize": 200]
        )
        return list.result ?? []
    }

    func start(_ containerId: String) async throws {
        try await operate("StartContainer", containerId: containerId)
    }

    func stop(_ containerId: String) async throws {
        try await operate("StopContainer", containerId: containerId)
    }

    func restart(_ containerId: String) async throws {
        try await operate("RestartContainer", containerId: containerId)
    }

    private func operate(_ action: String, containerId: String) async throws {
        _ = try await client.get(
            "v1/docker/container/\(action)",
            params: ["containerId": containerId]
        )
    }

    /// 拉日志。
    ///
    /// `result.message` 是 Docker 多路复用流，且**服务端忽略 `tail`、永远返回全量**，
    /// 因此必须先在客户端解帧，再按 `tail` 截断（踩坑 13）。
    func logs(containerId: String, tail: Int) async throws -> String {
        var lastError: Error?

        let bodies: [[String: Any]] = [
            ["containerId": containerId, "tail": tail],
            ["ContainerId": containerId, "Tail": tail]
        ]
        for body in bodies {
            do {
                let data = try await client.post("v1/docker/container/ShowContainerLogs", json: body)
                let text = DockerService.render(DockerService.logText(from: data), tail: tail)
                if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    return text
                }
                lastError = UgosError.decoding("日志返回为空")
            } catch {
                lastError = error
            }
        }

        do {
            let data = try await client.get(
                "v1/docker/container/ShowContainerLogs",
                params: ["containerId": containerId, "tail": "\(tail)"]
            )
            let text = DockerService.render(DockerService.logText(from: data), tail: tail)
            if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return text
            }
            lastError = UgosError.decoding("日志返回为空")
        } catch {
            lastError = error
        }

        throw lastError ?? UgosError.decoding("无法读取容器日志")
    }

    /// 解帧 + 截断。解帧失败说明不是多路复用流，按纯文本处理。
    static func render(_ raw: String, tail: Int) -> String {
        tailLines(demuxDockerLog(raw), count: tail)
    }

    /// 移植 server 侧的 `demux_docker_log`。
    ///
    /// 每帧 = 1 字节流类型 + 3 字节保留 + 4 字节大端「字节长度」+ 载荷。
    /// 长度按 **UTF-8 字节**计，所以这里统一在 `[UInt8]` 上切，天然按字节对齐，
    /// 不会因为中文日志被截断成半个字符。任何一帧不合法就整体退回原文。
    static func demuxDockerLog(_ raw: String) -> String {
        guard !raw.isEmpty else { return raw }
        let bytes = Array(raw.utf8)
        let total = bytes.count

        var output: [UInt8] = []
        var frames = 0
        var i = 0
        while i + 8 <= total {
            // 4 字节大端长度
            var length = 0
            for offset in 4...7 {
                length = (length << 8) | Int(bytes[i + offset])
            }
            // 长度不合常理（负数/为 0/超出体量）说明不是帧结构
            if length <= 0 || length > total * 4 { return raw }
            guard i + 8 + length <= total else { return raw }
            output.append(contentsOf: bytes[(i + 8)..<(i + 8 + length)])
            i += 8 + length
            frames += 1
        }

        guard frames > 0 else { return raw }
        return String(decoding: output, as: UTF8.self)
    }

    /// 取最后 `count` 行；不足则原样返回
    static func tailLines(_ text: String, count: Int) -> String {
        guard count > 0 else { return text }
        let lines = text.split(separator: "\n", omittingEmptySubsequences: true)
        guard lines.count > count else { return text }
        return lines.suffix(count).joined(separator: "\n")
    }

    /// 从任意 JSON 结构里抠出日志文本
    static func logText(from any: Any) -> String {
        if let text = any as? String {
            return text
        }
        if let array = any as? [Any] {
            let parts = array.map { logText(from: $0) }.filter { !$0.isEmpty }
            return parts.joined(separator: "\n")
        }
        if let dict = any as? [String: Any] {
            // 实测 `ShowContainerLogs` 的日志正文在 message 里
            for key in ["message", "logs", "log", "content", "lines", "data", "result"] {
                if let value = dict[key] {
                    let text = logText(from: value)
                    if !text.isEmpty { return text }
                }
            }
            // 兜底：把所有字符串值拼起来，至少不至于空白
            let parts = dict.values.compactMap { $0 as? String }.filter { !$0.isEmpty }
            if !parts.isEmpty { return parts.joined(separator: "\n") }
        }
        return ""
    }
}
