# NasManager — 绿联 DXP4800 Plus 原生 iOS 管理端

绕过官方「绿联云」App，直接调用 UGOS Pro 私有 API 的原生管理工具。

官方 App 的问题不是功能少，而是**该有的都不好用**：告警不能自定义、看不到容器日志、存储健康度只给一个绿点、RAID 无冗余也不提醒、外网访问要绕中转。这个项目的目标是把这些补上。

## 为什么没有中间层

早期方案里有个云服务器做中间层。砍掉了——理由：

1. NAS 有公网 IPv4 时，App 可以直连 9443，不需要中转。
2. 中间层天然是单点故障：NAS 挂了，跑在 NAS 上的告警发不出去。
3. 多一跳就多一个要维护、要防护、要付费的东西。

现在的结构：

```
iOS App ──直连 HTTPS 9443──> NAS (UGOS Pro 私有 API)
   │
   └─ 可选：NAS 上跑一个 150 行的哨兵，挂了才算它失职
```

中间层没有消失，只是**降级为可选组件**——想用 Docker 部署也行（`server/` 完整实现了），但它不在关键路径上。

## 外网访问：直连，不套穿透

**不用 Tailscale，不用 Cloudflare Tunnel，不用任何第三方穿透。** 有公网 IPv4 就用公网 IPv4，多套一层只会多一个会坏的东西。

路由器上做一条端口映射：

```
外部 19443/tcp  ──>  <NAS 内网地址>:9443
```

App 里填 `nas.example.com:19443`（DDNS 域名）或直接填公网 IP。内网环境走内网地址，外网走公网地址，体验无差别。

几条硬要求：

- **不要映射 9443 本身**，换高位端口。9443 是 UGOS 的默认管理口，全网扫描器天天在扫。
- **必须上 DDNS**。家宽公网 IP 会变，写死 IP 过两天就连不上了；用域名则 IP 变了也能自动跟上。
- **强密码 + 登录失败锁定**。UGOS 自带连续失败锁号，别关掉。
- **别开 UPnP**。让路由器上的映射成为唯一入口，看得见、随时能关。

关于安全性：这条路的风险等于「把 NAS 管理口挂公网」，做上面四条之后在可接受范围。要再提高等级只有两条路——自建 WireGuard（手机需装客户端），或者放弃公网直连。**Tailscale 属于后者，本质上也是自建 VPN，只是代管了配置，因此本项目同样不采用。**

## 目录

| 路径 | 内容 |
|---|---|
| `docs/api-verified.md` | **核心资产**。逐条对着真机验证过的 API 契约、错误码、14 条踩坑记录 |
| `docs/ugos-ios-app-plan.md` | 调研与规划：可复用项目、能力地图、用户评价取证、功能机会矩阵 |
| `ios/` | SwiftUI 原生 App（XcodeGen，iOS 17+） |
| `server/` | 可选中间层：FastAPI，UGOS 数据归一化 + 告警状态机 |
| `scripts/` | 逆向工具链：登录验证、端点探测、SPA bundle 抽取 |

## 已打通

登录链**对着真机端到端跑通**：

1. `GET /ugreen/v1/verify/check`（需带 `{"username":...}`）→ 公钥在**响应头 `X-Rsa-Token`**，不在 body
2. base64 解头 → PEM(PKCS#1) → RSA 分段加密密码（`chunk=245`，base64 拼接）
3. `POST /ugreen/v1/verify/login` → 拿 token
4. 之后 token 走 **query**（`?token=xxx`）。POST 把 token 放 header 会返回 `9404`

30+ 个读端点已验证。路径前缀 `ugreen/` 是**强制**的，漏了会得到空 body 的 404，很容易误判成「端点不存在」。

## 运行

**中间层（可选）**

```bash
cd server
cp ../.env.example ../.env   # 填 NAS 地址与账号
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8787
```

**iOS App**

```bash
cd ios
xcodegen generate
open NasManager.xcodeproj
```

没有 Mac 就交给 CI，或在本机跑同一个脚本：

```bash
./scripts/build-ios.sh          # 无签名 IPA，用 AltStore / Sideloadly 自签安装
./scripts/build-ios.sh --adhoc  # Ad-Hoc 签名，需本机已装证书与描述文件
```

CI 走 `.github/workflows/ios-build.yml`（macOS runner，产物在 Actions artifacts 里留 14 天）。**首次推这个 workflow 需要 token 带 `workflow` scope**，本地执行一次即可：

```bash
gh auth refresh -h github.com -s workflow
```


## 已知限制

- `volume.read_rate` 单位存疑（实测出现 1.09e12，怀疑是采样窗口累计字节而非 B/s），App 不直接渲染该值
- `ShowContainerLogs` 的 body 字段名未确认
- `disk/smart/info` 需要磁盘参数，SMART 明细尚未接入
- APNs 推送通道未实现（后端事件流已就绪）

细节与证据见 `docs/api-verified.md`。

## 安全

凭据只存在 `.env`（已 gitignore）和 iOS Keychain 里。仓库里没有任何真实设备凭据。明文 HTTP 只对局域网 IP 放开（`NSAllowsArbitraryLoads`），公网域名强制 TLS。
