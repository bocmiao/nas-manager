# NasManager

绿联（UGREEN）NAS 的原生 iOS 管理客户端，直接对接 UGOS Pro 的私有 API。

官方「绿联云」App 的问题不是功能少，而是该有的不好用：告警不能自定义、容器日志看不到、存储健康只给一个绿点、RAID 无冗余不提醒、外网访问绕中转。这个项目把这些补上。

> **非官方项目**。接口来自对 UGOS Pro Web 前端的逆向分析，属于未公开 API，固件升级后可能失效。仅供自有设备管理与技术研究使用，请自行评估风险，遵守你所在地区的法律法规与设备服务条款。

## 项目包含什么

| 部分 | 说明 |
|---|---|
| **iOS App** | SwiftUI 原生客户端，App 直连 NAS，不走中间层。iOS 17+ |
| **中间层（可选）** | FastAPI 服务，把 UGOS 数据归一化 + 告警状态机 + APNs 推送。适合想接 Webhook / 推送，或多设备共享的场景 |
| **契约文档** | `docs/api-verified.md`：逐条对着真机验证过的接口契约、错误码表、18 条踩坑记录 |

## 功能

**iOS 客户端**

- **总览** — 设备信息、CPU / 内存 / 风扇、网口速率、存储池与卷、磁盘温度、容器摘要
- **容器** — 列表与筛选、启动 / 停止 / 重启、日志查看（支持关键词过滤与行数切换）
- **存储** — 存储池（RAID 级别与冗余状态）、卷与分区、磁盘 SMART 明细（健康 / 温度 / 通电时长 / 关键属性）
- **告警** — 8 条本地规则可开关与调阈值，事件列表、未读角标、本地通知
- **设置** — 内网 + 公网双地址自动切换、密码存 Keychain、TOFU 证书指纹校验、刷新间隔

**中间层**

- 28 个 REST 接口，覆盖系统、监控、存储、容器、日志、告警、推送
- 15 条告警规则，状态机去重（只在 raised / resolved 跳变时发事件）
- 磁盘测速异步任务、SMART 明细、容器日志解帧、Docker 资源、进程与服务列表
- APNs 推送通道（未配凭据时自动降级为日志）
- 单文件哨兵 `scripts/sentinel.py`：仅标准库，用于「NAS 本身挂了」这种中间层无法自救的场景

## 为什么没有云服务器中间层

早期方案里有个云服务器做中转。砍掉了：

1. NAS 有公网 IPv4 时，App 可以直连 9443，不需要中转。
2. 中间层天然是单点故障：NAS 挂了，跑在 NAS 上的告警发不出去。
3. 多一跳就多一个要维护、要防护、要付费的东西。

所以中间层**降级为可选组件**——想用可以 Docker 部署，但它不在关键路径上。

### 外网访问

直连，不套任何穿透层：

```
外部 <高位端口>/tcp  ──路由器端口映射──>  <NAS 内网地址>:9443
```

几条硬要求：

- **不要映射 9443 本身**，换高位端口。9443 是 UGOS 默认管理口，扫描器常客。
- **上 DDNS**。家宽公网 IP 会变，写死会失效。
- **强密码 + 保持登录失败锁定**。
- **不开 UPnP**，让路由器映射是唯一入口。

风险等价于「把 NAS 管理口挂公网」。要更高安全性，自建 WireGuard（手机需装客户端），或放弃公网直连。

## 目录

```
docs/api-verified.md        实测接口契约（核心资产）
docs/ugos-ios-app-plan.md   调研：能力地图、用户反馈、功能机会矩阵
ios/                        SwiftUI App（XcodeGen，iOS 17+）
server/                     可选中间层（FastAPI）
scripts/                    逆向工具链 + 构建脚本 + 哨兵
```

## 快速开始

### iOS App

```bash
cd ios
xcodegen generate
open NasManager.xcodeproj
```

没有 Mac 就用 CI 出包，或本地跑同一个脚本：

```bash
./scripts/build-ios.sh          # 无签名 IPA，用 AltStore / Sideloadly 自签安装
./scripts/build-ios.sh --adhoc  # Ad-Hoc 签名，需本机已装证书与描述文件
```

CI 走 `.github/workflows/ios-build.yml`（macOS runner，产物留在 Actions artifacts）。

### 中间层（可选）

```bash
cp .env.example .env    # 填 NAS 地址与账号
cd server
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8787
```

接口文档在 http://127.0.0.1:8787/docs

### 哨兵

```bash
python scripts/sentinel.py --host <NAS 地址> --token <中间层 token> \
  --webhook https://example.com/hook
```

## 接口要点（逆向所得，易踩）

1. `GET /ugreen/v1/verify/check` 需带 `{"username": ...}`，**公钥在响应头 `X-Rsa-Token`**，不在 body。
2. 密码用 RSA 分段加密（`chunk = 245`，base64 拼接后提交）。
3. token 走 **query**（`?token=xxx`）。放 header 会返回 `9404`。
4. 路径前缀 `ugreen/` 是**强制**的，漏了会得到空 body 的 404，很容易误判成端点不存在。
5. 磁盘 `status` 的 `0` 和 `1` 都表示正常（`1` = 已激活在用），只把其他值当异常。
6. SMART 查询的 `disk` 参数必须传 `dev_name`（`/dev/sda`），传裸名或序列号返回 `5001`。
7. 容器启停是 **GET** 带 `?containerId=`，删除才是 POST。

完整清单见 `docs/api-verified.md`。

## 已知限制

- `volume.read_rate` 单位存疑（实测量级不稳定，疑似采样窗口累计字节而非 B/s），客户端不渲染该值
- 磁盘测速对存储池内的磁盘会被 UGOS 拒绝（`disk is busy`），需要空闲盘
- UPS 字段在未接 UPS 时恒为 `null`，接入后需用真实返回校准
- 进程 / 服务启停接口存在但属高危写操作，本期只做只读
- iOS 端依赖 iOS 17+（使用了 Observation 框架）

## 开发

```bash
# 后端（需 Python 3.11+）
cd server && pip install -r requirements.txt
python -m uvicorn app.main:app --reload

# 逆向工具链
python scripts/nas_login_test.py <user> <password>   # 验证登录链路
python scripts/probe_endpoints.py                     # 批量探测端点
```

凭据只从环境变量或 `.env` 读取，仓库内不含任何真实设备信息。

## 许可

MIT。与绿联（UGREEN）无关联，未获其认可或支持。「UGREEN」「UGOS」是其各自权利人的商标，仅用于说明兼容对象。
