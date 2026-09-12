# -*- coding: utf-8 -*-
"""全局配置：数据目录、常量、扫描参数校验（Pydantic 模型）。"""
import os
from pathlib import Path
from pydantic import BaseModel, Field

# ── 数据目录（FASTCF_HOME 环境变量 > ~/.fastcf）──
DATA_DIR = Path(os.environ.get("FASTCF_HOME", str(Path.home() / ".fastcf")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── 固定口径 ──
RESULT_COUNT = 5          # 固定返回 5 个结果
SPEED_PORT = 443          # 固定 443/TLS
SPEED_HOST = "speed.cloudflare.com"

# ── 数据源 ──
CF_IPS_URL = "https://www.cloudflare.com/ips-v4"   # 官方段（CIDR）
EXT_IPS_URL = "https://zip.cm.edu.kg/all.txt"      # 外部清单（IP:PORT#CC）
CACHE_TTL = 7 * 86400                              # 两源缓存 7 天

# ── colo 参考数据 ──
COLO_DATA_PATH = DATA_DIR / "colo_data.json"
COLO_URLS = (
    "https://cdn.jsdelivr.net/gh/Netrvin/cloudflare-colo-list@master/DC-Colos.json",
    "https://raw.githubusercontent.com/Netrvin/cloudflare-colo-list/master/DC-Colos.json",
)
COLO_TTL = 3 * 86400

# ── ICMP ping ──
PING_TIMES = 4           # 每个 IP 发 4 个包
PING_TIMEOUT = 2.0       # 单包超时（秒）
PING_WORKERS = 200       # ping 并发度
PING_LAT_FACTOR = 2.0    # 平均时延 > 2× 最佳时延 淘汰（零丢包豁免）
LOSS_CUTOFF = 0.75       # 丢包 ≥75% 淘汰 + 剔出池

# ── 下载测速 ──
SPEED_WORKERS = 4
SPEED_CONNS = 4  # 每个 IP 的并发连接数（多连接绕过 GFW 单连接限速）        # 下载测速并发度（按延迟升序提交，凑够达标数即停）
SPEED_SLOW_START_SECS = 1.5   # 首包快速淘汰：观察窗口（秒）
SPEED_SLOW_START_BYTES = 256 * 1024  # 窗口内累计低于此值（且已收到 ≥1 块）→ 起步过慢，提前结束
LOG_LIMIT = 1000         # 扫描日志环形缓冲上限

# ── IP 池 ──
POOL_SIZE = 50           # 每 DC 池上限
TEST_SIZE = 50           # 指定 DC 扫描时从池最多取的数量
POOL_TTL = 7 * 86400     # 池有效期（过期触发事件性重验，不删除）

# ── 历史 ──
HISTORY_LIMIT = 50       # 最多保留 50 条


class ScanParams(BaseModel):
    """扫描参数（入口校验：非法参数直接 422，不启动扫描线程）。"""
    mode: str = Field(pattern="^(DC|RANDOM)$")
    colo: str = ""
    randomCount: int = Field(default=150, ge=10, le=2000)
    speedSecs: float = Field(default=8, ge=3, le=60)
    speedMB: int = Field(default=50, ge=10, le=1000)
    minSpeed: float = Field(default=0, ge=0, le=10000)

    def validate(self) -> str:
        """返回错误信息（空字符串 = 通过）。"""
        if self.mode == "DC" and not (
            len(self.colo.strip().upper()) == 3 and self.colo.strip().isalpha()
        ):
            return "指定 DC 模式需要有效的节点代码（如 HKG）"
        return ""
