# FastCF — Cloudflare IP 优选测速（Linux · 零依赖 · 直连）

> 基于 Python 3 标准库的 Cloudflare IP 优选工具，提供实时进度、SSE 日志流、本地历史记录、IP 池管理（含入池 CF 官方 IP 段校验 + 实际 colo 探测）。
> 所有测速流量**直连**（模块导入时自动清除 `http_proxy` / `https_proxy` / `all_proxy` 环境变量）。
> 功能与交互参考 [XIU2/CloudflareSpeedTest](https://github.com/XIU2/CloudflareSpeedTest)（命令行参数命名、CSV 结果、过滤排序流程）。

## 特性

- 🚀 **实时进度** — SSE 推送扫描日志、阶段进度、耗时，无需刷新
- 🌍 **就近地理过滤** — 使用 [ip2region](https://github.com/lionsoul2014/ip2region) 本地解析 IP 真实归属国，按国家筛选候选（默认：中国大陆、港澳台、日韩新马）
- 📊 **两阶段测速（对齐 CFST 流程）** — 第一阶段并发 ping + 丢包率（4 次拨号取均值、相对时延过滤）；第二轮按延迟升序串行下载测速，**速度下限达标凑够 N 个即提前停止**
- 🏆 **排名结果表** — 延迟 / 丢包率 / 峰值速度 / CF 数据中心中文名称 / 归属地，按 **延迟 → 丢包 → 速度** 排序，支持按任意列排序
- 📜 **本地历史** — 自动保存最近 50 次扫描，支持查看 / 复用参数 / 删除 / 下载 CSV
- 🗄️ **IP 池管理** — 每个 DC 缓存已验证 IP（100/节点，7 天 TTL）；手动入池时自动做两步校验：
  1. **CF 官方 IP 段校验** — 非 Cloudflare 官方 CIDR 内的 IP 直接拒绝；
  2. **实际 colo 探测** — 并发拨号 `speed.cloudflare.com`，读 `cf-meta-colo` 头；留空节点时按探测结果归池，指定节点时仅命中者入池。
- 💻 **CLI 模式** — `--cli` 进入终端测速（对齐 CFST 的参数风格：`-n/-t/-dn/-p/-tl/-o`），结果表 + CSV 文件
- 📤 **结果导出** — 后端支持 iplist / ipport / csv / json / mihomo / singbox 六种格式（Web UI 当前提供 CSV 下载）
- ℹ️ **状态栏 / 系统信息** — 版本、数据目录、xdb 缓存、池统计一览，一键查看
- 🌗 **深色 / 浅色主题** — 默认浅色，一键切换
- 🛡️ **直连保障** — 自动清除所有代理环境变量，测速流量不经任何代理

## 快速开始

> 要求 Python 3.10+（Web UI 浏览器端运行）。零第三方依赖，无需 `pip install`。

```bash
# 直接运行（Web 模式，默认自动打开浏览器）
python3 fastcf.py

# 或指定端口 / 不自动打开浏览器
python3 fastcf.py --port 8080
python3 fastcf.py --no-browser

# CLI 模式（参考 XIU2/CloudflareSpeedTest）
python3 fastcf.py --cli                          # 终端实时日志 + 结果表 + result.csv
python3 fastcf.py --cli -n 200 -t 10 -dn 20      # 候选 200、测速 10s、下载测 20 个
python3 fastcf.py --cli -tl 100 -o /tmp/r.csv    # 只输出延迟 <100ms 的结果
python3 fastcf.py --cli --colo HKG -v6           # 指定 HKG 节点 / IPv6
```

首次启动会自动下载 ip2region 地理数据（v4: ~11 MB，v6: ~36 MB）到 `~/.fastcf/`，之后完全离线可用。

### 入口参数（Web 模式）

| 参数 | 说明 | 默认 |
|------|------|------|
| `--host` | 监听地址 | 127.0.0.1 |
| `--port` | 监听端口（0 = 自动分配） | 0 |
| `--no-browser` | 不自动打开浏览器 | 关 |
| `--data-dir` | 数据缓存目录（默认 `~/.fastcf`） | — |
| `--cli` | 进入 CLI 模式（其后参数交给 CLI 解析） | — |
| `--version` | 打印版本号 | — |

### CLI 参数（与 CFST 对齐的命名）

| 参数 | 说明 | 默认 |
|------|------|------|
| `-n` | 候选池大小（每轮扫描测速的 IP 数，20–300） | 100 |
| `-t` | 测速时长（秒，3–60） | 8 |
| `-mb` | 测速流量（MB，10–1000） | 50 |
| `-dn` | 下载测速数量（ping 预筛选后取延迟最低的 N 个，3–30） | 10 |
| `-p` | 显示结果数量（0 = 不显示直接退出） | 5 |
| `-tl` | 延迟上限（ms），只输出低于该值的结果 | 不过滤 |
| `-o` | 结果 CSV 文件（空字符串 = 不写） | result.csv |
| `-v6` / `-no-tls` | 使用 IPv6 / HTTP:80 | IPv4 / TLS:443 |
| `--colo` | 指定 DC 三字码（如 `HKG`）或 `RANDOM` 全局随机 | 按国家组就近 |
| `--countries` | 就近国家组（逗号分隔 ISO 码） | CN,HK,MO,TW,JP,KR,SG,MY |
| `-d` | 调试输出 | 关 |

### 测速流程（两阶段，对齐 CFST）

```
选 IP（池/随机）
  → 第一阶段：ping + 丢包率（每 IP 4 次拨号取均值，相对时延 >2× 最佳或丢包严重则淘汰）
  → 取延迟最低的 N 个进入第二轮
  → 第二阶段：按延迟升序串行下载测速
        · 速度 ≥ 下限 → 计入结果，凑够「结果数量」个立即停止
        · 速度 < 下限 → 不计入，继续下一个
        · 0 Mbps（CF 限流）→ 换备用 IP 试一次；连续 2 个 0 → 全局冷却 60s
  → 最终按 延迟 → 丢包 → 速度 排序输出
```

「速度下限」= 0（默认）时测满候选全部返回；>0 时启用提前停止语义。

## 项目结构

```
fastcf.py              # 入口脚本（Web 模式 + CLI 模式分发 + 代理清除）
fastcf/
  __init__.py          # 包初始化（版本号单一来源）
  cli.py               # CLI 模式（参考 CFST：结果表 + CSV + 过滤）
  exports.py           # 结果导出（iplist/ipport/csv/json/mihomo/singbox）
  ipdata.py            # Cloudflare 官方 IP 段获取 / 缓存 / 采样 / 位置探测
  geoip.py             # ip2region xdb 下载 / 本地查询 / 中文国家映射 / DC 中文名
  data_colos.py        # CF colo → 中文节点名参考表（Netrvin 快照，离线兜底）
  pools.py             # DC 级 IP 池缓存（建池 / 预热 / 管理 / 入池校验）
  scanner.py           # 测速引擎（ping 预筛选 + 带宽测速 + 排名）
  server.py            # HTTP 服务（静态 UI + JSON API + SSE 流）
  ip2region/           # ip2region Python binding（已内嵌，Apache 2.0）
    __init__.py
    util.py
    searcher.py
    LICENSE
  web/
    index.html         # UI 页面
    style.css          # 样式（深色/浅色主题）
    app.js             # 前端逻辑（SSE、导出、历史、IP 池、状态栏）
LICENSE                # 主许可证（MIT）+ 第三方许可说明
requirements.txt       # 依赖说明（零第三方依赖）
README.md              # 本文档
```

## 数据源

| 数据 | 来源 | 更新策略 |
|------|------|----------|
| CF 官方 IP 段 | `https://www.cloudflare.com/ips-v4` / `ips-v6` | 7 天缓存，过期自动刷新 |
| 地理归属 | ip2region xdb（jsDelivr CDN → GitHub raw；可用 `FASTCF_PROXY_BASE` 插入自建加速源） | 首次启动下载，本地离线查询 |
| CF colo 参考表 | [Netrvin/cloudflare-colo-list](https://github.com/Netrvin/cloudflare-colo-list) `DC-Colos.json`（内置静态快照兜底） | 3 天 TTL，在线失败沿用快照 |
| 测速节点 | `speed.cloudflare.com/__down?bytes=N` | 实时请求 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web UI |
| GET | `/api/status` | 当前扫描状态 + 最近一次结果 |
| GET | `/api/history` | 历史记录列表 |
| GET | `/api/stream` | SSE 实时日志流 |
| GET | `/api/info` | 版本 / 是否运行中 |
| GET | `/api/colos` | 全部 CF 节点列表（含各 DC 池大小），按国家分组 |
| GET | `/api/pools` | IP 池明细（节点 / 国家 / 数量 / IP 列表） |
| GET | `/api/data-status` | 数据目录 / xdb / 池统计 / 版本 |
| GET | `/api/export?fmt=…` | 下载结果（`fmt` ∈ iplist/ipport/csv/json/mihomo/singbox；`source` ∈ latest/history） |
| POST | `/api/scan` | 开始扫描（body: JSON 参数） |
| POST | `/api/cancel` | 取消当前扫描 |
| POST | `/api/history` | 历史操作（`{action: "delete"|"clear", id}`） |
| POST | `/api/pools` | 池操作（`{action: "add"|"clear"|"clear_all"}`；`add` 时 `code` 可省略，IP 按实际探测的 colo 归池，并先校验 CF 官方 IP 段） |

### IP 池入池校验

手动入池（`action: add`）时，后端对每个 IP 做两步校验：

1. **CF 官方 IP 段** — 用 `ipdata.fetch_cf_ips()` 加载 Cloudflare 官方 CIDR（7 天缓存），不在段内的 IP 直接拒绝（计入 `rejected`）。
2. **实际 colo 探测** — 并发拨号 `speed.cloudflare.com/__down?bytes=1MB`，读 `cf-meta-colo` 头。
   - `code` 为空：按探测到的 colo 归入对应节点池；
   - `code` 非空：探测结果必须匹配 `code`，否则拒绝（计入 `mismatch`）。
   - 探测失败（超时/无响应）计入 `failed`。

响应示例：

```json
{
  "ok": true,
  "added": 1,
  "rejected": 1,
  "mismatch": 0,
  "failed": 0,
  "by_colo": {"SJC": ["2606:4700::6810:1"]},
  "details": [
    {"ip": "1.2.3.4", "ok": false, "reason": "不在 CF 官方 IP 段"},
    {"ip": "104.16.0.1", "ok": false, "reason": "实际 colo=LAX 与目标 HKG 不符"}
  ]
}
```

### 扫描参数（POST /api/scan body）

```json
{
  "ipVer": "v4|v6",
  "tls": true,
  "count": 5,
  "colo": "",
  "countries": ["CN", "HK", "JP"],
  "sample": 150,
  "speedSecs": 8,
  "speedMB": 50,
  "top_rtt": 10,
  "minSpeed": 0
}
```

- `colo`：空 = 按 `countries` 就近；指定三字码 = 单点；`RANDOM` = 全局随机。
- `top_rtt`：进入第二轮下载测速的候选数（默认 10）。
- `minSpeed`：下载速度下限（Mbps）。>0 时「凑够 count 个达标即提前停止」，0 = 测满。

## 配置

- 数据缓存目录：`~/.fastcf/`（可通过 `FASTCF_HOME` 环境变量或 `--data-dir` 覆盖）
- 地理库文件：`~/.fastcf/ip2region_v4.xdb` / `ip2region_v6.xdb`
- CF IP 缓存：`~/.fastcf/cf_ips.json`
- colo 参考表：`~/.fastcf/colo_data.json`
- 历史记录：`~/.fastcf/history.json`
- IP 池：`~/.fastcf/ip_pools.json`
- xdb 下载加速源（可选）：`FASTCF_PROXY_BASE` 环境变量，前缀形式（如 `https://your-proxy/`），留空则用 jsDelivr / GitHub raw 直连

## 许可证

- 本项目代码：MIT（见 [LICENSE](LICENSE)）
- 内嵌 ip2region Python binding：Apache 2.0（[lionsoul2014/ip2region](https://github.com/lionsoul2014/ip2region)，见 `fastcf/ip2region/LICENSE`）
