"""
nutri_graph —— 食疗养生助手 · 集中配置

技术栈：LangChain / LangGraph / Milvus / Neo4j / PostgreSQL / DashScope / FastAPI
设计要点：证据分层（type × level）、图谱约束 LLM、候选表人工确认后幂等写入。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ============================================================
# Server
# ============================================================
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8810"))
APP_NAME = "nutri_graph"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ============================================================
# Neo4j 图谱
# ============================================================
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "nutrigraph-dev")

# ============================================================
# Milvus 向量库
# ============================================================
MILVUS_URI = os.getenv("MILVUS_URI", "http://localhost:19530")
MILVUS_TOKEN = os.getenv("MILVUS_TOKEN", "")
# 实体链接集合 / 文档块集合（名称带模型版本后缀，避免 embedding 维度漂移）
MILVUS_ENTITY_COLLECTION = os.getenv("MILVUS_ENTITY_COLLECTION", "kg_entity")
MILVUS_CHUNK_COLLECTION = os.getenv("MILVUS_CHUNK_COLLECTION", "kb_chunks")

# ============================================================
# PostgreSQL（候选表 + 记忆 + pgvector）
# ============================================================
PG_DSN = os.getenv(
    "PG_DSN",
    "postgresql://nutri:nutri@localhost:5432/nutri_graph",
)
PGVECTOR_DIM = int(os.getenv("PGVECTOR_DIM", "1024"))

# ============================================================
# DashScope（LLM + Embedding）
# ============================================================
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.getenv(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-v3")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))
# 推理模型（deepseek / qwen-thinking）会把 completion 预算先花在 reasoning 上，
# 预算太小会出现「有 reasoning、content 为空」。中间件遇到这种情况会放大到该上限重试一次。
LLM_MAX_TOKENS_CEILING = int(os.getenv("LLM_MAX_TOKENS_CEILING", "8192"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90.0"))

# ---- 大语言模型池（只收「模型类型 = 大语言模型」）----
# 集合在 .env 里配，代码不写死任何额度 / 到期时间：
# 百炼会不定期送额度，把「剩多少、什么时候到期」写进配置必然过期，
# 所以这里只认「有哪些模型、谁先谁后」，额度用尽靠运行时归因（403 AllocationQuota）
# 自动跳过，冷却到期后再自动回到候选池。
#
#   LLM_MODEL_POOL=deepseek-v4-flash-0731,qwen3.8-max,qwen3.8-2.4t-a95b,deepseek-v4-pro-0813
#
# 顺序 = 优先级（排第一的是主模型）。
# 视觉/视频模型（qwen-image-*、wan*-video）协议不兼容，不要放进来。
LLM_MODEL_POOL: list[str] = [
    m.strip() for m in os.getenv("LLM_MODEL_POOL", "").split(",") if m.strip()
]


# 兜底模型：池里模型全部不可用（额度用尽被移除 / 都在冷却）时才用它。
# 默认取池里最后一个——它平时最后才被用到，额度通常最充裕。
LLM_MODEL = os.getenv("LLM_MODEL", LLM_MODEL_POOL[-1] if LLM_MODEL_POOL else "")

# ---- 模型池行为参数 ----
# 额度/冷却状态的持久化位置：重启后不会再去撞一个已经空了的模型
LLM_POOL_STATE_PATH = os.getenv("LLM_POOL_STATE_PATH", "data/llm_pool_state.json")
# 限流/抖动 → 短冷却后允许重试
LLM_RATE_LIMIT_COOLDOWN_S = float(os.getenv("LLM_RATE_LIMIT_COOLDOWN_S", "60"))
# 模型不存在/无权限 → 冷却多久再试（一般整版都不可用，给长一点）
LLM_INVALID_COOLDOWN_H = float(os.getenv("LLM_INVALID_COOLDOWN_H", "24"))
# 注意：额度用尽**不做冷却**——直接从 LLM_MODEL_POOL 移除，
# 由 `python -m scripts.check_llm_pool --reset` 恢复。

# ============================================================
# 检索配置
# ============================================================
# 实体链接阈值（向量余弦相似度）
ENTITY_LINK_THRESHOLD = float(os.getenv("ENTITY_LINK_THRESHOLD", "0.82"))
ENTITY_LINK_TOP_K = int(os.getenv("ENTITY_LINK_TOP_K", "5"))
# 文档块降级检索
CHUNK_TOP_K = int(os.getenv("CHUNK_TOP_K", "6"))
CHUNK_THRESHOLD = float(os.getenv("CHUNK_THRESHOLD", "0.40"))
# 图查询命中数上限
GRAPH_MAX_RESULTS = int(os.getenv("GRAPH_MAX_RESULTS", "8"))

# ============================================================
# 子图可视化（/chat 返回图结构 + /kg/subgraph 探索）
# ============================================================
# 一次返回给前端画图的规模上限。图再大，屏幕上也是一团毛线球，
# 与其让它画满 300 个点，不如截断并标 truncated。
SUBGRAPH_MAX_NODES = int(os.getenv("SUBGRAPH_MAX_NODES", "40"))
SUBGRAPH_MAX_EDGES = int(os.getenv("SUBGRAPH_MAX_EDGES", "60"))
# 两食材之间找关系时的最大跳数（Neo4j shortestPath 上限，别开太大）
SUBGRAPH_MAX_HOPS = int(os.getenv("SUBGRAPH_MAX_HOPS", "3"))
# depth=2 时最多再展开几个一跳食材（控制组合爆炸）
SUBGRAPH_DEPTH2_SEEDS = int(os.getenv("SUBGRAPH_DEPTH2_SEEDS", "4"))

# ============================================================
# 证据策略
# ============================================================
# strict    = guideline, measured
# balanced  = guideline, measured, traditional
# all       = 全部（含 anecdotal），且强制标注「未验证」
DEFAULT_EVIDENCE_POLICY = os.getenv("DEFAULT_EVIDENCE_POLICY", "balanced")

# 各意图默认允许的 evidence_type（字符串逗号分隔）
EVIDENCE_POLICY_MAP = {
    "pair": os.getenv("EVIDENCE_PAIR", "guideline,measured,traditional"),
    # 不宜同食的主体是民间说法，默认把 anecdotal 放进来，靠证据等级降档，而不是直接滤掉。
    "incompatible": os.getenv(
        "EVIDENCE_INCOMPATIBLE", "guideline,measured,traditional,anecdotal"
    ),
    "contra_population": os.getenv("EVIDENCE_CONTRA_POP", "guideline,measured"),
    "contra_item": os.getenv("EVIDENCE_CONTRA_ITEM", "guideline,measured"),
    "nutrition": os.getenv("EVIDENCE_NUTRITION", "guideline,measured"),
    "substitute": os.getenv("EVIDENCE_SUBSTITUTE", "guideline,measured,traditional"),
    "symptom_remedy": os.getenv("EVIDENCE_SYMPTOM", "guideline,measured"),
}

# ============================================================
# Langfuse（自托管）
# ============================================================
LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "true").lower() == "true"
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

# ============================================================
# 记忆配置（PostgreSQL）
# ============================================================
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() == "true"
SHORT_TERM_WINDOW = int(os.getenv("SHORT_TERM_WINDOW", "10"))  # 最近 N 轮
SESSION_SUMMARY_THRESHOLD = int(os.getenv("SESSION_SUMMARY_THRESHOLD", "30"))  # 超长触发滚动摘要
MEMORY_FACT_TOP_K = int(os.getenv("MEMORY_FACT_TOP_K", "4"))

# ============================================================
# 安全 / 边界控制
# ============================================================
# 红旗症状：命中即拦截，绕过图谱直接建议就医。
# 逗号分隔即可增删，例：RED_FLAG_SYMPTOMS=胸痛,呼吸困难,抽搐
_DEFAULT_RED_FLAG_SYMPTOMS = (
    "胸痛,呼吸困难,意识模糊,意识改变,持续高热,"
    "剧烈腹痛,大出血,便血,呕血,抽搐,昏迷,"
    "孕产急症,心梗,中风"
)
RED_FLAG_SYMPTOMS = [
    s.strip()
    for s in os.getenv("RED_FLAG_SYMPTOMS", _DEFAULT_RED_FLAG_SYMPTOMS).split(",")
    if s.strip()
]

_DEFAULT_DISCLAIMER = (
    "本回答由食疗知识图谱辅助生成，仅供健康科普参考，不能替代医生诊断与用药指导。"
    "如有不适请及时就医。"
)

# ============================================================
# 身份与访问控制（needs 13）
# ============================================================
# true = /chat 等业务接口必须带 access token；false = 开发期允许匿名（沿用 payload.user_id）
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").lower() == "true"

# ---- JWT ----
# 为空时启动会生成一次性随机密钥（进程重启即失效所有令牌），仅适合本地开发；
# 部署必须显式配置，否则多副本/重启后用户被登出。
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALG = os.getenv("JWT_ALG", "HS256")
JWT_ISSUER = os.getenv("JWT_ISSUER", "nutri_graph")
ACCESS_TTL_S = int(os.getenv("ACCESS_TTL_S", "1800"))          # 30 分钟
REFRESH_TTL_D = int(os.getenv("REFRESH_TTL_D", "7"))           # 7 天

# ---- 手机号存储 ----
# PHONE_ENC_KEY：Fernet 密钥（44 字符 urlsafe base64）；为空则由 JWT_SECRET 派生（不推荐，仅开发可用）
PHONE_ENC_KEY = os.getenv("PHONE_ENC_KEY", "")
# PHONE_HASH_PEPPER：手机号唯一索引用的 HMAC 盐，改了这个所有手机号都查不到
PHONE_HASH_PEPPER = os.getenv("PHONE_HASH_PEPPER", "")

# ============================================================
# 短信通道（needs 13）
# ============================================================
# console = 本地开发：不真发短信，验证码打在日志里，自建 sms_code 表校验
# dypns   = 阿里云「短信认证」（号码认证服务 PNVS）：免资质/免签名/免模板，验证码由平台托管
#
# 为什么不是「短信服务 SMS」：个人认证账号申请的自用签名无法通过运营商签名实名制报备，
# 实际发不出去；且官方已停止受理「测试或学习 / 已备案网站 / 公众号」等签名来源。
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "console").lower()

# ---- dypns（短信认证）----
ALIBABA_CLOUD_ACCESS_KEY_ID = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
ALIBABA_CLOUD_ACCESS_KEY_SECRET = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")
# 以下三项都从号码认证控制台的「赠送签名 / 赠送模板」页面复制，不可自定义
SMS_SIGN_NAME = os.getenv("SMS_SIGN_NAME", "")
SMS_TEMPLATE_CODE = os.getenv("SMS_TEMPLATE_CODE", "")
SMS_SCHEME_NAME = os.getenv("SMS_SCHEME_NAME", "")
# 模板变量：##code## 是平台占位符，min 为有效分钟数（随 SMS_VALID_TIME_S 一起改）
SMS_TEMPLATE_PARAM = os.getenv("SMS_TEMPLATE_PARAM", '{"code":"##code##","min":"5"}')
# 让接口把生成的验证码回传（仅本地联调用，生产必须 false）
SMS_RETURN_VERIFY_CODE = os.getenv("SMS_RETURN_VERIFY_CODE", "false").lower() == "true"

# ---- 验证码策略 ----
SMS_CODE_LENGTH = int(os.getenv("SMS_CODE_LENGTH", "6"))       # 平台支持 4~8
SMS_VALID_TIME_S = int(os.getenv("SMS_VALID_TIME_S", "300"))   # 5 分钟
SMS_INTERVAL_S = int(os.getenv("SMS_INTERVAL_S", "60"))        # 平台侧同号最小间隔

# ---- 频控（自己必须做：平台只管验证码本身，不管短信轰炸，费用还是自己出）----
SMS_MAX_PER_PHONE_HOUR = int(os.getenv("SMS_MAX_PER_PHONE_HOUR", "5"))
SMS_MAX_PER_PHONE_DAY = int(os.getenv("SMS_MAX_PER_PHONE_DAY", "10"))
SMS_MAX_PER_IP_HOUR = int(os.getenv("SMS_MAX_PER_IP_HOUR", "10"))
SMS_MAX_PER_DEVICE_DAY = int(os.getenv("SMS_MAX_PER_DEVICE_DAY", "10"))
SMS_MAX_GLOBAL_DAY = int(os.getenv("SMS_MAX_GLOBAL_DAY", "200"))   # 熔断：防被刷爆账单
SMS_MAX_VERIFY_FAILS = int(os.getenv("SMS_MAX_VERIFY_FAILS", "5"))  # 连续错 5 次
SMS_LOCK_MINUTES = int(os.getenv("SMS_LOCK_MINUTES", "15"))         # 锁 15 分钟
# IP 维度的失败阈值要远高于手机号：办公室/校园/运营商 NAT 后面是大量正常用户共用出口 IP，
# 若也按 5 次锁，一个攻击者就能把整栋楼的人锁在门外（自己人打自己人）。50 次只用于识别批量攻击。
SMS_MAX_VERIFY_FAILS_PER_IP = int(os.getenv("SMS_MAX_VERIFY_FAILS_PER_IP", "50"))

# ---- 图形验证码（防「刷别人手机号」的短信轰炸）----
# 首次发码免费；同一出口 IP 在 CAPTCHA_FREE_SENDS_PER_IP 次后，第 N+1 次起强制图形验证码。
# 这是频控之外的第二道闸：频控拦「同一个号/设备高频」，图形码拦「换号批量轰炸」。
CAPTCHA_FREE_SENDS_PER_IP = int(os.getenv("CAPTCHA_FREE_SENDS_PER_IP", "1"))
CAPTCHA_TTL_S = int(os.getenv("CAPTCHA_TTL_S", "300"))          # 图形码 5 分钟有效
CAPTCHA_MAX_ATTEMPTS = int(os.getenv("CAPTCHA_MAX_ATTEMPTS", "5"))  # 单个 challenge 最多试 5 次

# ============================================================
# 未收录食材登记（Phase 9.1）
# ============================================================
# 总开关：关掉后 /chat 遇到未收录食材只回「暂未收录」，不写登记表
FOOD_REQUEST_ENABLED = os.getenv("FOOD_REQUEST_ENABLED", "true").lower() == "true"
# 每人每天最多登记多少个**新**条目（重复问同一个词不算，只累加 hit_count）
FOOD_REQUEST_DAILY_LIMIT_PER_USER = int(os.getenv("FOOD_REQUEST_DAILY_LIMIT_PER_USER", "10"))
# 全站每天新增上限：超了只记录到日志，不再写库，防止被刷爆
FOOD_REQUEST_GLOBAL_DAILY_LIMIT = int(os.getenv("FOOD_REQUEST_GLOBAL_DAILY_LIMIT", "200"))
# 中文没命中时，与已知别名的字级相似度超过这个值就判「疑似别名」而不是新食材
# （pg_trgm 对中文实测全是 0.000，这条走 kg/textsim 的字级相似度）
FOOD_NEAR_MISS_MIN_SIM = float(os.getenv("FOOD_NEAR_MISS_MIN_SIM", "0.62"))

# ============================================================
# 公开演示：Excel 导入食物（/kg/import）
# ============================================================
# 这个入口**不鉴权**（演示要开放给所有人），所以限制只能在服务端做。
# 三道闸：单文件大小 / 单文件行数（在 kg/importer.py 里，属解析契约）、
# 以及下面这个「每 IP 每天几次」。没有它，一个脚本就能把库刷到几十万行。
FOOD_IMPORT_ENABLED = os.getenv("FOOD_IMPORT_ENABLED", "true").lower() == "true"
FOOD_IMPORT_DAILY_LIMIT_PER_IP = int(os.getenv("FOOD_IMPORT_DAILY_LIMIT_PER_IP", "20"))
# 手工录入单条记录：一次提交只有一条，不该和「上传一个 500 行文件」共用次数，
# 但它同样是公开入口，仍然要限。40 ≈ 一个人认真填一天的量。
FOOD_MANUAL_DAILY_LIMIT_PER_IP = int(os.getenv("FOOD_MANUAL_DAILY_LIMIT_PER_IP", "40"))
# 预览结果保留多久。超时的批次不能再 commit，避免「半小时前看的预览」被误提交。
FOOD_IMPORT_PREVIEW_TTL_MIN = int(os.getenv("FOOD_IMPORT_PREVIEW_TTL_MIN", "60"))

# ---- 抓取与校验（Phase 9.2）----
# 检索画像怎么来：auto = 内置优先，没有再调模型（演示/离线最稳）；llm = 只用模型；static = 只用内置
FOOD_DRAFT_MODE = os.getenv("FOOD_DRAFT_MODE", "auto").lower()

# USDA FoodData Central：免费 key 从 api.data.gov 领，1000 次/小时/IP
# DEMO_KEY 只有 30 次/小时、50 次/天，且是共享的 —— 仅够验证链路，别拿去跑批量
FDC_API_KEY = os.getenv("FDC_API_KEY", "DEMO_KEY")
FDC_BASE_URL = os.getenv("FDC_BASE_URL", "https://api.nal.usda.gov/fdc/v1")
FDC_TIMEOUT = float(os.getenv("FDC_TIMEOUT", "20.0"))
# 只要分析型数据，不要 Branded（厂商自填，可信度差）
FDC_DATA_TYPES = os.getenv("FDC_DATA_TYPES", "Foundation,SR Legacy")

# 每日抓取预算（任务数，不是请求数）：超了就不再 claim，防被刷爆
FOOD_FETCH_DAILY_BUDGET = int(os.getenv("FOOD_FETCH_DAILY_BUDGET", "50"))
# 自动通过的门槛。单源一致性恒为 0.6 → 得分封顶 0.88，所以现在永远进人工；
# 等接入第二个来源（中国食物成分表离线导入）才可能真正自动通过。
FOOD_AUTO_APPROVE_MIN_SCORE = float(os.getenv("FOOD_AUTO_APPROVE_MIN_SCORE", "0.9"))
FOOD_AUTO_APPROVE_MIN_SOURCES = int(os.getenv("FOOD_AUTO_APPROVE_MIN_SOURCES", "2"))

# ---- 审核闭环与可信度（Phase 9.3）----
# 几个来源才算「交叉验证过」。少于这个数 → 数据标 provisional（单一来源、未交叉验证）。
# 注意 provisional ≠ 未审核：未审核的数据根本进不了主图。
FOOD_TRUSTED_MIN_SOURCES = int(os.getenv("FOOD_TRUSTED_MIN_SOURCES", "2"))
# 单一来源数据的证据等级封顶。种子里 USDA/中国食物成分表的实测值是 B，
# 但那是「有第二份资料佐证」的情况；单源只给 C，让回答自动降档加提示。
PROVISIONAL_EVIDENCE_LEVEL = os.getenv("PROVISIONAL_EVIDENCE_LEVEL", "C")
# 多源交叉验证过的数据才配 B（实测/研究）。A 留给指南与临床共识，补录永远到不了 A。
TRUSTED_EVIDENCE_LEVEL = os.getenv("TRUSTED_EVIDENCE_LEVEL", "B")
# 检索是否包含 provisional 数据。
#   默认 true：刚补录完就能查到，否则「审核通过了却查不到」等于闭环断掉，
#              用户会重复登记同一个食材。
#   false    ：严格模式，只答交叉验证过的硬证据（对外的严肃场景用）。
# 两种模式下 provisional 数据在上下文里都会显式标注，不会混成硬证据。
INCLUDE_PROVISIONAL = os.getenv("INCLUDE_PROVISIONAL", "true").lower() == "true"

DISCLAIMER = os.getenv("DISCLAIMER", _DEFAULT_DISCLAIMER)

# ============================================================
# 日志
# ============================================================
# LOG_DIR 默认定位到项目根目录下的 logs/，避免「从哪个目录启动就写到哪」的漂移。
# 显式配 LOG_DIR 时按配置值走（可用相对路径，但建议绝对路径）。
_LOG_DIR_DEFAULT = str(Path(__file__).resolve().parent / "logs")
LOG_DIR = os.getenv("LOG_DIR", _LOG_DIR_DEFAULT)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_ROTATION_WHEN = os.getenv("LOG_WHEN", "midnight")
LOG_ROTATION_INTERVAL = int(os.getenv("LOG_INTERVAL", "1"))
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "14"))
