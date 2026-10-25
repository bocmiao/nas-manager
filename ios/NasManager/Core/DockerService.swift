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

    /// 拉日志。POST 字段名在 1.19.1 上没完全确认，按 containerId/ContainerId 依次试，最后退回 GET。
    func logs(containerId: String, tail: Int) async throws -> String {
        var lastError: Error?

        let bodies: [[String: Any]] = [
            ["containerId": containerId, "tail": tail],
            ["ContainerId": containerId, "Tail": tail]
        ]
        for body in bodies {
            do {
                let data = try await client.post("v1/docker/container/ShowContainerLogs", json: body)
                let text = DockerService.logText(from: data)
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
            let text = DockerService.logText(from: data)
            if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return text
            }
            lastError = UgosError.decoding("日志返回为空")
        } catch {
            lastError = error
        }

        throw lastError ?? UgosError.decoding("无法读取容器日志")
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
            for key in ["logs", "log", "content", "lines", "data", "result"] {
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
