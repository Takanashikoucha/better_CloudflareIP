# FastCF 重构进度

> 继续本任务前，先读取 `docs/DESIGN.md` 与本文档。

## 当前状态

**已完成**：v5.0.0 重构。后端（并行测速 + 增量 SSE + last_error）/ 前端（深石墨蓝 OKLCH 令牌 + lime 强调 + rank 徽章 + logDelta 增量渲染）/ 测试（24 个全绿）/ 端到端验证 / README 全部完成。

v5.0.0 变更：
- **并行下载测速**：串行 → 4 路并发（`SPEED_WORKERS=4`），按延迟升序提交，凑够达标数即取消未开始的 future
- **首包快速淘汰**：前 1.5s 累计 < 256KB → 起步过慢，提前结束返回 0（不浪费整个测速窗口）
- **SSE 增量日志**：帧带 `logDelta`（新增日志）+ `logTotal`；迟到订阅者首帧全量 last_state（前端重置本地日志数组）
- **last_error**：扫描错误在 scanner 被替换后仍可见（`status().error`）
- **rank 字段**：结果按 延迟→丢包→速度 排序名次（1-5），前端 rank 徽章（第 1 名强调色高亮）
- **UI 全新设计语言**：深石墨蓝底 + lime 青柠单一强调（OKLCH 令牌，对比度 12.8:1）+ 明度分层 + `color-mix(in oklab)` 派生 hover/soft 态
- **遗留清理**：删除 `pools.py` / `geoip.py` / `ipdata.py`（v4 零引用）

## 真实网络验证（v5.0.0）

**诊断结论**：本环境在中国大陆，**Cloudflare 被 GFW 干扰/限速**，而中国镜像站速度正常。
- 清华 TUNA（中国镜像）：**464-573 Mbps**（正常）
- kernel.org（国际）：3.4 Mbps（极低）
- CF speed（Cloudflare）：0.02-2 Mbps（几乎为 0，GFW 干扰）
- 历史数据（8 月底）本环境曾跑到 374Mbps → 当时 CF 没被限速（GFW 策略变化）

**多连接测速**（v5.0.0 新增）：
- 单连接：2.0 Mbps（GFW 单连接限速）
- 4 连接：**22-39 Mbps**（11-20 倍提升）
- 10 连接：48.6 Mbps（24 倍提升）
- 结论：多连接可以绕过 GFW 单连接限速，但无法完全绕过（4 连接 = 22-39 Mbps，仍远低于正常网络的 100+ Mbps）

**axel 工具测试**：
- 清华 TUNA（HTTP）：573 Mbps（完美）
- 清华 TUNA（HTTPS）：400 Bad Request（axel HTTPS 支持有问题）
- CF speed（HTTP/HTTPS）：卡住 / 400（GFW 干扰，axel 无效）
- 结论：axel 对中国镜像站完美，但对 CF 无效；CF 测速用 Python 多连接

**建议**：
- 在中国大陆使用 FastCF 时，CF 测速会受 GFW 影响（22-39 Mbps 波动）
- 如果需要准确测速，建议：
  1. 使用海外 VPS 部署 FastCF
  2. 或者接受 CF 在中国大陆的速度限制
- 在 UI 中加提示："如果在中国大陆使用，CF 测速可能受 GFW 影响"

## 已完成

- [x] 梳理现有代码，识别 7 类重构问题（见 DESIGN.md §2）
- [x] 设计文档 `docs/DESIGN.md`（架构 / 状态机 / 持久化 / API / UI 方向）
- [x] 进度文档 `docs/PROGRESS.md`
- [x] 后端核心模块重构：
  - `store.py`（统一持久化：原子写 + 单锁）
  - `zhnames.py`（从 colos.py 拆出的静态中文映射）
  - `colos.py`（精简为参考表 + 刷新 + 分组）
  - `sources.py`（双源获取 / 缓存 / 合并采样 / 已知来源校验；去掉全局锁，避免慢下载阻塞）
  - `pool.py` / `history.py`（基于 store）
  - `scanner.py`（依赖注入 EngineContext；取消路径统一 finalize）
  - `appstate.py`（修复 start() 持锁启动线程的死锁；_run 兜底 done.set()）
  - `net.py`（direct_download 加总时间预算，防慢握手拖死调用方）
- [x] API 层重构：`server.py`（FastAPI 路由 + SSE，与原版兼容）
- [x] 前端 UI 重构：浅色 Linear 式（`index.html` / `style.css` / `app.js`）
  - `[hidden]` 用 `display:none !important` 防弹窗 bug 复发
  - 前端只保留表单草稿状态，运行态/结果/历史/池全部来自后端
- [x] 单元测试：21 个全部通过（`python3 tests/test_units.py`）
- [x] 端到端验证：
  - 服务启动 / 静态资源 / 全部 API 端点
  - 真实扫描（RANDOM 10 IP → 5 个达标结果，168s）
  - DC 模式（LHR 池 3 IP → 4 个达标 + 随机回退，31s）
  - 参数校验（422）/ 取消（保留部分结果）/ 历史增删 / 池增删 / CSV+JSON 导出
  - 发现并修复 3 个 bug：
    1. `appstate.start()` 持锁启动线程 → 与 `cancel()` 死锁
    2. `sources` 全局锁 + 慢下载 → 阻塞其它源获取 / 扫描线程
    3. 取消时无结果 → `result_payload` 为 None，前端看不到取消状态

## 待办

- [x] 更新 README（结构 / 依赖说明 / 当前状态）
- [x] 界面审美升级（v4.1.0）：深色精密控制台风格
  - 深空黑底（#0a0b0d）+ 单一暖琥珀强调（#e8a33d，克制使用）
  - 等宽数据字体（JetBrains Mono）+ 低饱和状态色
  - 克制动效（fadeUp 入场、pulse 状态点、progress 填充）+ `prefers-reduced-motion` 支持
  - 所有 53 个 JS 引用的元素 ID 与 106 个 CSS 类全部对齐验证

## 阻塞点

无。

## 关键决策记录

- 技术栈保持 Python + FastAPI（.vendor 兜底方案保留）
- 状态管理：`AppState` 为唯一进程级状态源；`store.py` 统一持久化（原子写）
- 扫描语义与原版完全一致（两阶段测速 / 回退 / 池规则 / 取消保留部分结果）
- UI 改为浅色 Linear 式（与 README 描述一致），`[hidden]` 用 `!important` 防弹窗 bug 复发
- `scanner` 通过 `EngineContext` 注入依赖，单测可离线跑完整流程
- `direct_download` 加总时间预算（`timeout × retries + 5` 秒），防慢握手拖死调用方

## 网络环境说明（测试时）

本环境到 Cloudflare 的 TCP/TLS 握手较慢（~17-20s），导致：
- 单元测试 `test_appstate` / `test_sample_fallback` 耗时 ~2-3 分钟（真实网络 ping 预筛）
- 真实扫描 10 个 IP 耗时 ~168s（ping 预筛 ~130s + 下载测速 ~38s）
- 功能全部正常，只是慢；生产环境网络正常时速度会快很多
