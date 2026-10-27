"""集中配置：全部通过环境变量覆盖，无需改代码。"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


# ---------------------------------------------------------------- 存储
# Railway Volume 挂载点；本地开发退回到仓库内 ./data
_DATA_DIR_EXPLICIT = bool(os.getenv("DATA_DIR", "").strip())
DATA_DIR = Path(os.getenv("DATA_DIR", "/data" if Path("/data").is_dir() else "./data"))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "bis.db")))
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", str(DATA_DIR / "archive")))

# 显式配了 DATA_DIR（线上一定会配）却发现目录不存在，说明 Volume 没挂上。
# 这时**必须直接崩掉**，而不是退回容器内的临时目录 —— 后者会让状态在每次
# 重启后静默丢失，进而重新播种基线并吞掉这期间发布的所有论文。
STRICT_DATA_DIR = _bool("STRICT_DATA_DIR", _DATA_DIR_EXPLICIT)
# 基线播种过一次后会在数据目录留个记号，用来识别"卷丢了"这种情况
BASELINE_MARKER = DATA_DIR / ".baselined"

# ---------------------------------------------------------------- BIS 源
BIS_BASE = "https://www.bis.org"
BIS_LIST_URL = f"{BIS_BASE}/api/document_lists/wppubls.json"
BIS_DOC_JSON = f"{BIS_BASE}/api/documents{{path}}.json"   # path 形如 /publ/work1369
BIS_ABSTRACT_URL = f"{BIS_BASE}{{path}}.htm"
BIS_PDF_URL = f"{BIS_BASE}{{path}}.pdf"
BIS_MENU_ID = 161                                          # Working Papers

USER_AGENT = os.getenv(
    "USER_AGENT",
    "bis-wpapers-monitor/1.0 (research paper monitor)",
)
HTTP_TIMEOUT = _int("HTTP_TIMEOUT", 30)
# 对 BIS 保持礼貌：串行抓取详情页，每次间隔
FETCH_DELAY_MS = _int("FETCH_DELAY_MS", 400)
# 首次基线播种后，回填最近 N 篇的详情用于仪表盘展示（0 = 不回填）
BACKFILL_RECENT = _int("BACKFILL_RECENT", 60)
# 单次运行最多抓取的详情页数量，防止异常情况下打爆 BIS
MAX_DETAIL_FETCH = _int("MAX_DETAIL_FETCH", 120)

# ---------------------------------------------------------------- 完整性护栏
# 基线播种时列表至少要有这么多篇，否则判定为 BIS 返回了残缺响应并中止。
# BIS 实际有 1300+ 篇，正常情况下远超这个值。
MIN_BASELINE_PAPERS = _int("MIN_BASELINE_PAPERS", 500)
# 增量检查时，列表长度不得低于上次成功运行的这个比例，否则视为残缺响应。
# 若不设防，一次部分响应会让大量已知论文"消失"，下次再被当成新论文重发一遍。
MIN_LIST_RATIO = float(os.getenv("MIN_LIST_RATIO", "0.9"))

# ---------------------------------------------------------------- 调度
def _timezone(name: str, default: str = "Asia/Shanghai") -> str:
    """时区名拼错不应该让整个服务起不来 —— 退回默认值并留下日志。"""
    raw = (os.getenv(name, "") or default).strip()
    try:
        ZoneInfo(raw)
        return raw
    except Exception:
        logging.getLogger(__name__).error(
            "时区 %r 无法解析，回退到 %s。请检查 TIMEZONE 环境变量。", raw, default
        )
        return default


TIMEZONE = _timezone("TIMEZONE")
CRON_DAY_OF_WEEK = os.getenv("CRON_DAY_OF_WEEK", "mon")
CRON_HOUR = _int("CRON_HOUR", 9)
CRON_MINUTE = _int("CRON_MINUTE", 0)
# 服务启动时是否立刻跑一次（首次部署用来播种基线）
RUN_ON_STARTUP = _bool("RUN_ON_STARTUP", True)

# 发信由外部代发（GitHub Actions）时置 true。
# 此时**关闭进程内的每周定时器**，由代发方通过 /admin/prepare-digest 驱动整个流程。
# 不关的话两个调度器会在同一时刻抢同一批论文：进程内那次因为 Railway 封了
# SMTP 必然发信失败（论文保持未报告），紧接着代发方又跑一次检查，
# 两次运行互相干扰，runs 表也会出现看不懂的失败记录。
EXTERNAL_MAILER = _bool("EXTERNAL_MAILER", False)
# 外部代发时，控制台需要给出「去哪里发信」的入口
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip("/ ")
WORKFLOW_FILE = os.getenv("WORKFLOW_FILE", "weekly-digest.yml")

# ---------------------------------------------------------------- 邮件
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "").strip()


def _smtp_password() -> str:
    """读取 SMTP 密码，并处理 Gmail 应用专用密码被连空格复制进来的情况。

    Google 把 16 位应用专用密码按 `abcd efgh ijkl mnop` 分四组显示，
    照着复制就会带上 3 个空格，而 Gmail 的 SMTP 登录不接受这种形式，
    表现为 535 认证失败 —— 排查起来很费时间，因为变量"看上去"是对的。

    只在**恰好符合这个形状**（四组四位字母数字）时才去掉空格。
    其他邮件服务的密码可能合法地包含空格，不能一律剥离。
    """
    raw = os.getenv("SMTP_PASSWORD", "").strip()
    if re.fullmatch(r"[A-Za-z0-9]{4}(?: [A-Za-z0-9]{4}){3}", raw):
        # 这里不能直接打日志：config 是在 import 阶段求值的，那时 main.py 的
        # logging.basicConfig() 还没跑，INFO 记录会被根 logger 丢掉。
        # 改为置一个标志，由启动流程在日志系统就绪后再输出。
        return raw.replace(" ", "")
    return raw


_RAW_SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_PASSWORD = _smtp_password()
# 供启动日志使用：密码是否曾以 Gmail 的分组格式提供
SMTP_PASSWORD_NORMALISED = bool(
    _RAW_SMTP_PASSWORD and _RAW_SMTP_PASSWORD != SMTP_PASSWORD
)
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER)
MAIL_TO = _list("MAIL_TO")
MAIL_SUBJECT_PREFIX = os.getenv("MAIL_SUBJECT_PREFIX", "[BIS Working Papers]")
# 没有发现新论文时是否也发一封"本次检查未发现新论文"
MAIL_ON_EMPTY = _bool("MAIL_ON_EMPTY", False)

EMAIL_ENABLED = bool(SMTP_USER and SMTP_PASSWORD and MAIL_TO)

# ---------------------------------------------------------------- 站点
SITE_TITLE = os.getenv("SITE_TITLE", "BIS Working Papers Monitor")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
# 只读仪表盘无需登录；如需保护，设置该变量后 /admin/* 需带 ?token=
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# ---------------------------------------------------------------- 相关性评分
# 面向固收 / 外汇 / 货币政策方向的读者做优先级排序。
# 权重可用 JEL_WEIGHTS_EXTRA='{"G12": 5}' 形式的 JSON 追加覆盖。
JEL_WEIGHTS: dict[str, int] = {
    # 货币政策与利率
    "E4": 10, "E5": 10, "E43": 12, "E44": 10, "E52": 12, "E58": 10,
    # 国际金融 / 外汇
    "F3": 10, "F31": 12, "F32": 10, "F33": 10, "F34": 9, "F41": 9, "F42": 8,
    # 金融市场 / 资产定价
    "G1": 8, "G12": 11, "G14": 8, "G15": 10,
    # 银行与金融机构
    "G2": 6, "G21": 7, "G23": 6, "G28": 5,
    # 主权债务 / 财政
    "H6": 8, "H63": 10,
    # 宏观总量
    "E3": 5, "E31": 6, "E32": 5,
    # 金融危机 / 系统性风险
    "G01": 8, "G18": 5,
}

KEYWORD_WEIGHTS: dict[str, int] = {
    "monetary policy": 8, "interest rate": 8, "yield curve": 12, "term premium": 12,
    "bond": 8, "sovereign": 7, "treasury": 8, "duration": 6, "carry": 7,
    "exchange rate": 10, "currency": 8, "fx": 10, "foreign exchange": 12,
    "swap": 8, "basis": 7, "hedging": 6, "capital flow": 8,
    "inflation": 7, "central bank": 6, "liquidity": 6, "repo": 9,
    "emerging market": 6, "dollar": 7, "funding": 5, "spread": 6,
}
