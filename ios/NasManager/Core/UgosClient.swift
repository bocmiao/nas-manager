import Foundation

// MARK: - 错误

enum UgosError: LocalizedError {
    case transport(String)
    case http(Int)
    case business(code: Int, message: String, path: String)
    case auth(String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .transport(let msg): return "网络错误：\(msg)"
        case .http(let code): return "HTTP \(code)"
        case .business(let code, let msg, let path):
            return "[\(code)] \(msg.isEmpty ? "接口返回异常" : msg)" + (path.isEmpty ? "" : "（\(path)）")
        case .auth(let msg): return msg
        case .decoding(let msg): return "响应解析失败：\(msg)"
        }
    }

    /// 需要重新登录的错误码（实测清单）
    var requiresRelogin: Bool {
        if case .business(let code, _, _) = self {
            return [401, 1000, 1001, 1002, 1003, 1010, 1013, 1024, 1204].contains(code)
        }
        return false
    }
}

// MARK: - 端点

struct UgosEndpoint: Hashable, Codable {
    var host: String
    var port: Int

    /// IPv6 字面量必须包方括号才不会被当成 host:port
    var urlHost: String { EndpointResolver.bracketIfNeeded(host) }

    var baseURL: URL? {
        URL(string: "https://\(urlHost):\(port)")
    }

    var displayName: String { "\(urlHost):\(port)" }
}

// MARK: - 客户端

/// 直连 UGOS Pro 的客户端。
///
/// 关键实测约定（见 docs/api-verified.md）：
/// - 路径前缀 `ugreen/` 必须带，漏掉会得到空 body 的 404
/// - 读操作主通道是 `GET` + `?token=<明文>`
/// - POST 的 token 同样走 query
/// - 会话过期返回 1024 / 1010，需静默重登
/// - 自签证书走 TOFU（`TOFUTrustDelegate`），不再无条件信任
actor UgosClient {
    private let endpoint: UgosEndpoint
    private let session: URLSession
    private let trustDelegate: TOFUTrustDelegate

    private var token: String = ""
    private var credentials: (username: String, password: String)?
    private var loginInFlight: Task<String, Error>?

    private static let agentHeaders: [String: String] = [
        "UG-Agent": "PC/WEB",
        "Client-Version": "78471",
        "Client-Version-Str": "1.19.0",
        "X-Specify-Language": "zh-CN",
        "Cache-Control": "no-cache",
        "Accept": "application/json"
    ]

    init(endpoint: UgosEndpoint) {
        self.endpoint = endpoint
        let delegate = TOFUTrustDelegate()
        self.trustDelegate = delegate
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 40
        config.httpCookieStorage = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        self.session = URLSession(
            configuration: config,
            delegate: delegate,
            delegateQueue: nil
        )
    }

    var isLoggedIn: Bool { !token.isEmpty }

    // MARK: URL 构造

    private func url(_ path: String, query: [String: String]) throws -> URL {
        try url(path, queryItems: query.map { ($0.key, $0.value) })
    }

    /// 用有序键值对建 URL，允许同名参数重复出现（`?level=info&level=warning`）。
    private func url(_ path: String, queryItems: [(String, String)]) throws -> URL {
        guard let base = endpoint.baseURL else {
            throw UgosError.transport("地址非法：\(endpoint.displayName)")
        }
        var p = path.hasPrefix("/") ? String(path.dropFirst()) : path
        if !p.hasPrefix("ugreen/") { p = "ugreen/" + p }
        guard var comps = URLComponents(url: base.appendingPathComponent(p), resolvingAgainstBaseURL: false) else {
            throw UgosError.transport("URL 构造失败")
        }
        if !queryItems.isEmpty {
            comps.queryItems = queryItems.map { URLQueryItem(name: $0.0, value: $0.1) }
        }
        guard let final = comps.url else { throw UgosError.transport("URL 构造失败") }
        return final
    }

    private func makeRequest(
        path: String,
        method: String,
        query: [String: String],
        body: Data?
    ) throws -> URLRequest {
        try makeRequest(
            path: path,
            method: method,
            queryItems: query.map { ($0.key, $0.value) },
            body: body
        )
    }

    private func makeRequest(
        path: String,
        method: String,
        queryItems: [(String, String)],
        body: Data?
    ) throws -> URLRequest {
        var req = URLRequest(url: try url(path, queryItems: queryItems))
        req.httpMethod = method
        for (k, v) in Self.agentHeaders {
            req.setValue(v, forHTTPHeaderField: k)
        }
        if body != nil {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = body
        }
        return req
    }

    @discardableResult
    private func rawExecute(_ request: URLRequest) async throws -> (HTTPURLResponse, Data) {
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw UgosError.transport("响应不是 HTTP")
            }
            return (http, data)
        } catch let e as UgosError {
            throw e
        } catch {
            throw UgosError.transport(error.localizedDescription)
        }
    }

    private func decodeJSON(_ data: Data, path: String) throws -> [String: Any] {
        guard !data.isEmpty else { throw UgosError.decoding("响应体为空（\(path)）") }
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw UgosError.decoding("不是 JSON 对象（\(path)）")
        }
        return obj
    }

    // MARK: 登录

    /// 完整登录流程。成功后 token 保存在内存，并记住凭据用于静默重登。
    @discardableResult
    func authenticate(username: String, password: String) async throws -> String {
        // 1) 取 RSA 公钥：公钥在响应头 X-Rsa-Token（base64 PEM）
        let checkReq = try makeRequest(
            path: "ugreen/v1/verify/check",
            method: "POST",
            query: [:],
            body: try JSONSerialization.data(withJSONObject: ["username": username])
        )
        let (checkHTTP, _) = try await rawExecute(checkReq)
        guard checkHTTP.statusCode == 200 else {
            throw UgosError.http(checkHTTP.statusCode)
        }
        guard let rsaB64 = checkHTTP.value(forHTTPHeaderField: "X-Rsa-Token"),
              let pemData = Data(base64Encoded: rsaB64),
              let pem = String(data: pemData, encoding: .utf8) else {
            throw UgosError.auth("verify/check 未返回 X-Rsa-Token 响应头，固件可能已变更")
        }

        // 2) RSA PKCS#1 v1.5 加密密码
        let key = try UgosCrypto.publicKey(fromPEM: pem)
        let encryptedPassword = try UgosCrypto.encryptLong(password, key: key)

        // 3) 登录
        let payload: [String: Any] = [
            "username": username,
            "password": encryptedPassword,
            "keepalive": false,
            "otp": true,
            "is_simple": false
        ]
        let loginReq = try makeRequest(
            path: "ugreen/v1/verify/login",
            method: "POST",
            query: [:],
            body: try JSONSerialization.data(withJSONObject: payload)
        )
        let (loginHTTP, loginData) = try await rawExecute(loginReq)
        guard loginHTTP.statusCode == 200 else { throw UgosError.http(loginHTTP.statusCode) }

        let json = try decodeJSON(loginData, path: "verify/login")
        let code = json["code"] as? Int ?? -1
        guard code == 200 else {
            let msg = json["msg"] as? String ?? ""
            throw UgosError.business(code: code, message: msg, path: "verify/login")
        }

        let body = json["data"] as? [String: Any] ?? [:]
        guard let newToken = body["token"] as? String, !newToken.isEmpty else {
            if (body["enable_otp"] as? Bool) == true {
                throw UgosError.auth("该账号开启了二次验证，请在 UGOS 设置中关闭后重试")
            }
            throw UgosError.auth("登录成功但未返回 token")
        }

        token = newToken
        credentials = (username, password)
        return newToken
    }

    /// 静默重登（并发请求只触发一次）
    private func relogin() async throws {
        if let existing = loginInFlight {
            _ = try await existing.value
            return
        }
        guard let creds = credentials else {
            throw UgosError.auth("会话已失效且无可用凭据，请重新登录")
        }
        token = ""
        let task = Task<String, Error> {
            try await authenticate(username: creds.username, password: creds.password)
        }
        loginInFlight = task
        defer { loginInFlight = nil }
        _ = try await task.value
    }

    // MARK: 请求

    /// GET，token 走 query。自动处理会话过期。
    func get(_ path: String, params: [String: String] = [:]) async throws -> Any {
        try await perform(path: path, method: "GET", params: params, body: nil)
    }

    /// GET，允许同名参数重复（日志筛选的 level / module 都是可重复参数）。
    func getMulti(_ path: String, pairs: [(String, String)]) async throws -> Any {
        try await perform(path: path, method: "GET", queryItems: pairs, body: nil)
    }

    /// POST，token 同样走 query（实测 header 方式多返回 9404）。
    /// params 用于少数既需要 body 又需要 query 的接口，默认空。
    func post(
        _ path: String,
        json: [String: Any] = [:],
        params: [String: String] = [:]
    ) async throws -> Any {
        let body = try JSONSerialization.data(withJSONObject: json)
        return try await perform(path: path, method: "POST", params: params, body: body)
    }

    /// DELETE。部分接口要求把参数放 body（如快照删除），这里允许 json 为 nil。
    func delete(
        _ path: String,
        json: [String: Any]? = nil,
        params: [String: String] = [:]
    ) async throws -> Any {
        var body: Data?
        if let json {
            body = try JSONSerialization.data(withJSONObject: json)
        }
        return try await perform(path: path, method: "DELETE", params: params, body: body)
    }

    /// 泛型便捷方法：把接口返回的 data 直接解码成模型。
    func getDecodable<T: Decodable>(
        _ type: T.Type, path: String, params: [String: String] = [:]
    ) async throws -> T {
        try decode(try await get(path, params: params))
    }

    func postDecodable<T: Decodable>(
        _ type: T.Type, path: String, json: [String: Any] = [:]
    ) async throws -> T {
        try decode(try await post(path, json: json))
    }

    private func decode<T: Decodable>(_ any: Any) throws -> T {
        guard JSONSerialization.isValidJSONObject(any) else {
            throw UgosError.decoding("返回结构无法序列化")
        }
        let data = try JSONSerialization.data(withJSONObject: any)
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw UgosError.decoding(error.localizedDescription)
        }
    }

    private func perform(
        path: String,
        method: String,
        params: [String: String],
        body: Data?,
        allowRetry: Bool = true
    ) async throws -> Any {
        try await perform(
            path: path,
            method: method,
            queryItems: params.map { ($0.key, $0.value) },
            body: body,
            allowRetry: allowRetry
        )
    }

    private func perform(
        path: String,
        method: String,
        queryItems: [(String, String)],
        body: Data?,
        allowRetry: Bool = true
    ) async throws -> Any {
        if token.isEmpty { try await relogin() }

        var query = queryItems
        query.append(("token", token))
        let request = try makeRequest(path: path, method: method, queryItems: query, body: body)
        let (http, data) = try await rawExecute(request)

        // 业务错误通常仍是 200，但 400/401 也要能读到 JSON 细节
        let json: [String: Any]
        do {
            json = try decodeJSON(data, path: path)
        } catch {
            throw UgosError.http(http.statusCode)
        }

        let code = json["code"] as? Int ?? -1
        if code == 200 {
            return json["data"] ?? [:]
        }

        let message = (json["msg"] as? String) ?? (json["debug"] as? String) ?? ""
        let error = UgosError.business(code: code, message: message, path: path)

        if allowRetry, error.requiresRelogin {
            try await relogin()
            return try await perform(
                path: path,
                method: method,
                queryItems: queryItems,
                body: body,
                allowRetry: false
            )
        }
        throw error
    }
}
