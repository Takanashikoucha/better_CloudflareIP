# FastCF — Cloudflare IP 优选测速（Linux · 零依赖 · 直连）

> 基于 Python 3 标准库的 Cloudflare IP 优选工具，提供实时进度、SSE 日志流、本地历史记录、IP 池管理（含入池 CF 官方 IP 段校验 + 实际 colo 探测）。
> 所有测速流量**直连**（模块导入时自动清除 `http_proxy` / `https_proxy` / `all_proxy` 环境变量）。
> 功能与交互参考 [XIU2/CloudflareSpeedTest](https://github.com/XIU2/CloudflareSpeedTest)。

## 特性

- 🚀 **实时进度** — SSE 推送扫描日志、阶段进度、耗时，无需刷新
- 🌍 **首次全量子网遍历** — 启动即后台遍历全部官方 /24 子网（约 5956 个，IPv4）：每子网探 1 个代表 IP 读 `cf-meta-colo`，命中则同子网批量入池。约 15 分钟跑完一轮，带实时进度横幅。**完成前禁止开始测速**
- 🗄️ **DC 级 IP 池 + 后台常驻填充** — 每个 DC 缓存已验证 IP（50/节点）。首次遍历完成后转**缺额维护模式**：池过期自动重探、DC 缺额自动补池、扫描命中 DC 不足顺带补。**任何探测行为只要读到实际服务节点（`cf-meta-colo`），IP 就写回实际命中的 DC**
- 🕓 **池过期自动重探** — 池 7 天 TTL；过期后 IP 不删除，由后台线程重新探测：成功 → 写回实际 DC 池并刷新时间戳；失败 → 剔除
- 📊 **两阶段测速（对齐 CFST 流程）** — 第一阶段 ping + 丢包率（4 次拨号取均值、相对时延过滤）；第二轮按延迟升序串行下载测速，**速度下限达标凑够 N 个即提前停止**
- 🔁 **0Mbps 自动换 IP** — 测速无数据（CF 限流）时直接换备用 IP 重试，不做全局冷却
- 🎛️ **参数可配** — 测速时长 / 流量 / 速度下限 / 候选数（`top_rtt`，3–30）均可在界面调整，历史记录一键复用
- 🏆 **排名结果表** — 延迟 / 丢包率 / 峰值速度 / CF 数据中心中文名称 / 归属地 / 协议，按 **延迟 → 丢包 → 速度** 排序，支持按任意列排序
- 📜 **本地历史** — 自动保存最近 50 次扫描，支持查看 / 复用参数 / 删除 / 下载 CSV
- ✅ **入池两步校验** — 手动入池时：① CF 官方 IP 段校验（非官方 CIDR 拒绝）；② 并发探测 `speed.cloudflare.com` 读 `cf-meta-colo` 实际节点
- 📤 **结果导出** — CSV（与 CFST result.csv 风格对齐）+ JSON
- ℹ️ **状态栏 / 系统信息** — 版本、数据目录、CF 段缓存、池统计一览
- 🌗 **深色 / 浅色主题** — 默认浅色，一键切换
- 🛡️ **直连保障** — 自动清除所有代理环境变量，测速流量不经任何代理

## 快速开始

> 要求 Python 3.10+。零第三方依赖，无需 `pip install`。

```bash
python3 fastcf.py                 # 启动并自动打开浏览器
python3 fastcf.py --port 8080     # 指定端口
python3 fastcf.py --no-browser    # 不自动打开浏览器
python3 fastcf.py --data-dir /x   # 指定数据缓存目录
```

### 入口参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--host` | 监听地址 | 127.0.0.1 |
| `--port` | 监听端口（0 = 自动分配） | 0 |
| `--no-browser` | 不自动打开浏览器 | 关 |
| `--data-dir` | 数据缓存目录（默认 `~/.fastcf`） | — |
| `--version` | 打印版本号 | — |

### 测速流程（两阶段）

```
取 IP（指定 DC → 该 DC 池，池空先同步建池、仍空则报错；
      国家组 → 合并命中 DC 的池，缺额后台补，不足则回退全量随机；
      RANDOM → 全量随机采样）
  → 第一阶段：ping + 丢包率（每 IP 4 次拨号取均值，相对时延 >2× 最佳或丢包严重则淘汰）
  → 取延迟最低的 N 个进入第二轮，其余作为备用
  → 第二阶段：按延迟升序串行下载测速
        · 速度 ≥ 下限 → 计入结果，凑够「结果数量」个立即停止
        · 速度 < 下限 → 不计入，继续下一个
        · 0 Mbps（CF 限流）→ 换备用 IP 试一次
  → 测速成功的 IP 回写其实际 DC 池
  → 最终按 延迟 → 丢包 → 速度 排序输出
```

「速度下限」= 0（默认）时测满候选全部返回；>0 时启用提前停止语义。

## 项目结构

```
fastcf.py              # 入口脚本（代理清除 + web 服务 + 后台填充启动）
fastcf/
  __init__.py          # 包初始化（版本号单一来源）
  data_colos.py        # CF colo → 中文节点名参考表（Netrvin 快照，离线兜底）
  exports.py           # 结果导出（csv / json）
  filler.py            # 后台常驻线程：首次全量子网遍历 → 缺额维护模式
  geoip.py             # colo 参考数据（静态快照 + 在线刷新）/ 中文国家映射
  ipdata.py            # Cloudflare 官方 IP 段获取 / 缓存 / 采样 / 位置探测
  pools.py             # DC 级 IP 池（建池 / 全量子网遍历 / 过期重探 / 管理 / 入池校验）
  scanner.py           # 测速引擎（取池 + ping 预筛选 + 带宽测速 + 排名）
  server.py            # HTTP 服务（静态 UI + JSON API + SSE 流）
  web/
    index.html         # UI 页面
    style.css          # 样式（深色/浅色主题）
    app.js             # 前端逻辑（SSE、导出、历史、IP 池、状态栏）
LICENSE                # 主许可证（MIT）
requirements.txt       # 依赖说明（零第三方依赖）
README.md              # 本文档
```

## 数据源

| 数据 | 来源 | 更新策略 |
|------|------|----------|
| CF 官方 IP 段 | `https://www.cloudflare.com/ips-v4` / `ips-v6` | 7 天缓存，过期自动刷新 |
| CF colo 参考表 | [Netrvin/cloudflare-colo-list](https://github.com/Netrvin/cloudflare-colo-list) `DC-Colos.json`（内置静态快照兜底） | 3 天 TTL，在线失败沿用快照 |
| 测速节点 | `speed.cloudflare.com/__down?bytes=N` | 实时请求（`cf-meta-*` 头返回实际服务地） |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web UI |
| GET | `/api/status` | 当前扫描状态 + 最近一次结果 |
| GET | `/api/history` | 历史记录列表 |
| GET | `/api/stream` | SSE 实时日志流 |
| GET | `/api/colos` | 全部 CF 节点列表（含各 DC 池大小），按国家分组 |
| GET | `/api/pools` | IP 池明细（节点 / 国家 / 数量 / IP 列表） |
| GET | `/api/data-status` | 数据目录 / 缓存 / 池统计 / 版本 |
| GET | `/api/export?fmt=…` | 下载结果（`fmt` ∈ csv/json；`source` ∈ latest/history） |
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

### 扫描参数（POST /api/scan body）

```json
{
  "ipVer": "v4|v6",
  "tls": true,
  "count": 5,
  "colo": "",
  "countries": ["CN", "HK", "JP"],
  "speedSecs": 8,
  "speedMB": 50,
  "top_rtt": 10,
  "minSpeed": 0
}
```

- `colo`：空 = 按 `countries` 就近；指定三字码 = 单点（池空则同步建池，仍空报错）；`RANDOM` = 全局随机。
- `top_rtt`：ping 预筛选后进入第二轮下载测速的候选数（默认 10，范围 3–30；其余 IP 仅作 0Mbps 备用）。
- `minSpeed`：下载速度下限（Mbps）。>0 时「凑够 count 个达标即提前停止」，0 = 测满。

### 时延口径

所有时延（ping 预筛选 / 结果表 / 导出 CSV）统一为 **TCP 连接 + TLS 握手全程**
（HTTP 模式为 TCP 连接）：

- 2 次（预筛选为 4 次）拨号，成功者取平均（单点刷新取 2 次最佳）；
- **TLS 握手失败不计时**——只用 TCP 时延冒充「TLS 可用」会高估该节点，
  此类 IP 按丢包率计入并参与 ≥75% 剔除；
- 丢包率 = 拨号失败次数 / 总次数，≥75% 的 IP 从所属 DC 池剔除。

## 配置

- 数据缓存目录：`~/.fastcf/`（可通过 `FASTCF_HOME` 环境变量或 `--data-dir` 覆盖）
- CF IP 缓存：`~/.fastcf/cf_ips.json`
- colo 参考表：`~/.fastcf/colo_data.json`
- 历史记录：`~/.fastcf/history.json`
- IP 池：`~/.fastcf/ip_pools.json`
- colo 参考表下载加速源（可选）：`FASTCF_PROXY_BASE` 环境变量，前缀形式（如 `https://your-proxy/`），留空则用 jsDelivr / GitHub raw 直连

## 开发

- 测试：`python3 tests/test_units.py`（离线单元测试，无需网络；用 `FASTCF_HOME` 临时目录隔离数据）。
- 实现约定：CIDR 切分（`v4_prefixes` / `v6_prefixes` / `sample_cf_subnets` / `_expand_sample`）
  一律走 `ipaddress` 标准库的字符串 API（`ip_network(str, strict=False)`、`.subnets(new_prefix=)`、
  `.hosts()`），**不做手写的整数位运算**（`<<`、`>>`、`&`、`~`）。部分 Python 构建下
  大整数位运算结果不可靠（曾发现 `<<` 与 `>>` 结果相同、`& 0xFFFFFFF0` 间歇性丢失位），
  交给标准库逐 IP 枚举更安全。
- `pools._subnet_key_v6` 用 `IPv6Network(f"{ip}/48", strict=False)` 取 /48 父网段作为分组键，
  同样避免手写位掩码。
- 版本号单一来源：`fastcf/__init__.py` 的 `__version__`。

## 许可证

- 本项目代码：MIT（见 [LICENSE](LICENSE)）
- colo 参考数据快照：来自 [Netrvin/cloudflare-colo-list](https://github.com/Netrvin/cloudflare-colo-list)（MIT）
