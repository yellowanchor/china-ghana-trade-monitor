# -*- coding: utf-8 -*-
"""
================================================================================
中加（纳）贸易智能分析工具 MVP —— 单文件版
================================================================================
功能：
  1. 汇率监控   ：自动抓取 USD/CNY、USD/GHS 汇率（数据源 open.er-api.com，免费无需密钥），
                  存入 SQLite，自动计算塞地(GHS)兑人民币交叉汇率与近期贬值幅度。
  2. 政策监控   ：定时抓取商务部/海关总署/外汇局/重庆人社局/深圳人社局等官网新闻标题，
                  按"出口、外汇、创业、关税"等关键词过滤，自动识别"本次新增"条目。
  3. 利润测算   ：按《中加纳出口贸易创业方案书》表6 的财务模型，
                  一键测算首单 FOB 报价、毛利、定金覆盖、加纳端综合税负（默认35%）。
  4. Web 看板   ：浏览器打开即用的分析面板（汇率折线图 + 政策列表 + 交互式利润计算器）。
  5. AI 智能分析：将当日汇率变动+新增政策喂给大模型，生成"汇率风险研判/政策要点/首单建议"
                  三段式日报，并支持基于政策库的智能问答。
                  —— 使用任意 OpenAI 兼容接口（DeepSeek/通义千问/智谱GLM 等），
                     在 ai_config.json 填入 base_url / api_key / model 即可启用；
                     未配置密钥时自动降级为内置规则引擎分析，开箱即用。

使用方法：
  python trade_monitor.py fetch     # 采集一次数据（汇率 + 政策 + 配件报价），打印摘要
  python trade_monitor.py serve     # 启动 Web 看板（自动先补采当日数据）
  python trade_monitor.py calc      # 命令行利润测算示例
  python trade_monitor.py analyze   # 生成 AI 分析日报
  python trade_monitor.py parts     # 采集配件批发报价（默认关键词）
  python trade_monitor.py parts 摩托车离合器片 汽车雨刮器  # 自定义关键词采集
  python trade_monitor.py history        # 回填最近 30 天历史汇率（有 rate_api_key 全量含GHS，否则 ECB 补 CNY）
  python trade_monitor.py history 90     # 回填最近 90 天
  python trade_monitor.py analyze   # 生成一份 AI（或规则引擎）日报并打印

数据存储：本目录下 trade_monitor.db（SQLite，首次运行自动创建）
依赖：仅 Python 3.8+ 标准库，无需 pip 安装任何包
================================================================================
"""

import gzip
import hashlib
import io
import json
import os
import re
import secrets
import base64
import ssl
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

# 可选的 CLIP 图像校验模块：依赖缺失时自动降级为纯规则过滤
try:
    import parts_vision
    from parts_vision import check_image
except Exception as _e:
    parts_vision = None
    check_image = lambda img_url, kw, **_kw: (None, None)

# ==============================================================================
# 一、全局配置
# ==============================================================================

# 脚本自身所在目录（固定基准，防止进程工作目录漂移导致读写错库/找不到配置）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 数据库文件路径（与脚本同目录，绝对路径）
DB_PATH = os.path.join(_BASE_DIR, "trade_monitor.db")

# 看板服务地址与端口
# 本地开发默认 127.0.0.1（仅本机访问）；服务器部署设环境变量 TM_HOST=0.0.0.0 对外监听
HOST = os.environ.get("TM_HOST", "127.0.0.1")
PORT = int(os.environ.get("TM_PORT", "8765"))

# 免费汇率 API（无需密钥）：返回各国货币兑 1 USD 的汇率
RATE_API = "https://open.er-api.com/v6/latest/USD"

# 汇率历史回填（可选）：allratestoday.com 免费层（160+ 货币含 CNY/GHS、历史免费、300 请求/月，
# 个人非商用；Bearer 认证）。在 ai_config.json 填入 rate_api_key 后自动启用
# （USD/CNY + USD/GHS 全量市场中间价，period=30d 返回含周末的日历日序列）；
# 未填 key 时自动降级：用欧洲央行(ECB)参考汇率经 frankfurter 补 USD/CNY 历史（仅工作日，无 GHS）。
RATE_HISTORY_API = "https://allratestoday.com/api/historical-rates"

# 无 key 兜底：frankfurter.app —— 欧洲央行参考汇率，免费、无需 key、历史可回溯至 1999 年。
# 仅覆盖 ECB 货币（USD/CNY 可用），不支持 GHS；范围查询格式：{start}..{end}?from=USD&to=CNY
FRANKFURTER_API = "https://api.frankfurter.app/{start}..{end}?from=USD&to=CNY"

# 政策监控源（官网新闻/要闻栏目页）。
# 注意：政府网站偶有反爬或改版，单条失败不影响其他源，失败会记录在 fetch_log 中。
# （海关总署官网为 JS 反爬，用"中国政府网政策库"替代，覆盖面更广）
POLICY_SOURCES = [
    {"name": "商务部",   "url": "https://www.mofcom.gov.cn/"},
    {"name": "中国政府网", "url": "https://www.gov.cn/zhengce/"},
    {"name": "外汇管理局", "url": "https://www.safe.gov.cn/safe/whxw/index.html"},
    {"name": "重庆人社局", "url": "https://rlsbj.cq.gov.cn/"},
    {"name": "深圳人社局", "url": "http://hrss.sz.gov.cn/"},
    {"name": "中非合作论坛", "url": "https://www.focac.org.cn/"},
]

# 政策关键词：命中的条目在"重点政策"栏标红显示
POLICY_KEYWORDS = ["出口", "贸易", "外汇", "结汇", "汇率", "创业", "关税",
                   "市场采购", "1039", "退税", "非洲", "加纳", "跨境电商", "担保贷款"]

# 页脚噪音词：标题含这些词的链接（备案号、版权声明等）不入库
NOISE_WORDS = ["公网安备", "ICP备", "备案", "版权所有", "网站地图", "无障碍",
               "联系我们", "登录", "注册", "检索", "索引号", "主办单位", "承办"]


def is_noise(title):
    """判断链接标题是否为页脚/导航噪音。"""
    return any(w in title for w in NOISE_WORDS)

# 利润测算默认参数（取自方案书表6：首单摩配拼柜模型）
CALC_DEFAULTS = {
    "freight_mode": "auto",      # 海运物流费模式：auto 按总重量自动估算 / fixed 固定金额
    "freight_base": 3000,        # auto 模式：基础费（起运港杂+订舱+内装，元）
    "freight_per_kg": 3.25,      # auto 模式：每公斤 LCL 拼柜海运费（元/kg）
    "freight": 9500,             # fixed 模式：国内物流 + LCL 拼柜海运费总额（元）
    "agent_fee": 1500,           # 报关/外贸代理/单证费（元）
    "misc_fee": 2000,            # 寄样 + 验货 + 杂费（元）
    "markup": 21,                # 加价率（%，按总成本加成）
    "deposit_ratio": 30,         # 加纳方定金比例（%，必须先收定金再采购）
    "ghana_tax": 35,             # 加纳端综合税负（%，ECOWAS CET + VAT15% + NHIL/GETFund 各2.5%）
    "equity_a": 51,              # 重庆合伙人股比（%）
}

# 默认商品清单首行（示例：摩托车链条 2000 件 × 1kg/件）
CALC_DEFAULT_ITEM = {
    "name": "摩托车链条",         # 商品名称
    "unit_price": 29.0,          # 采购单价（元/件）
    "quantity": 2000,            # 数量（件）
    "unit_weight": 1.0,          # 单件重量（kg/件）
    "category": "",              # 品类key（选填，对应 CATEGORIES）
}

# ==============================================================================
# 品类主数据（来自《项目落地执行手册》§6.1 品类准入与认证矩阵）
# 每品类含：ECOWAS CET 关税率、EasyPASS CoC 要求、附加合规要求、HS编码前缀
# ==============================================================================

# 加纳进口附加税费率（除CET关税外的固定税费，来源：GRA / TaxLawGH 2026）
# VAT 15% + NHIL 2.5% + GETFund 2.5% + ECOWAS 0.5% + AU 0.2% + EXIM 0.75% + 检验费~1%
GHANA_EXTRA_TAX_PCT = 22.45

CATEGORIES = [
    {"key": "motor_parts",  "name": "摩托车配件",
     "cet_duty": 10,  "coc": "exempt",   "hs_prefix": "8708.30",
     "extra": "GSA高风险货物：加方须注册+建议随附CoA/CoC免抽检；建议按国际标准出中英文质检报告"},
    {"key": "auto_parts",   "name": "汽车配件",
     "cet_duty": 10,  "coc": "exempt",   "hs_prefix": "8708",
     "extra": "同摩配规则；旧件按二手商品规则；GSA高风险须注册"},
    {"key": "vehicle_new",  "name": "摩托车/汽车整车（新车）",
     "cet_duty": 20,  "coc": "exempt_voluntary",  "hs_prefix": "8711/8703",
     "extra": "整车进口商须向GSA注册+车型homologation认证；CBU整车与CKD散件税率差异大；另需DVLA上牌"},
    {"key": "vehicle_used", "name": "二手整车",
     "cet_duty": 35,  "coc": "pvoc_mandatory",   "hs_prefix": "8703/8711",
     "extra": "2026-10-01起强制PVoC：出口前须检验发CoC；车龄≤10年/右舵未认证改装/无km/h里程表拒入"},
    {"key": "general_goods","name": "小商品/日用百货/五金/塑料件",
     "cet_duty": 20,  "coc": "required",  "hs_prefix": "HS25-96",
     "extra": "FOB≥$2,000/票须出口前办CoC(Bureau Veritas在华)；英文标签"},
    {"key": "light_electrical","name": "轻工电器（风扇/小家电等）",
     "cet_duty": 20,  "coc": "required",  "hs_prefix": "HS84-85",
     "extra": "强制英标BS1363三插+220-240V/50Hz；能源委员会能效标签(QR码)出口前贴好才能办CoC"},
    {"key": "textiles",     "name": "纺织品/服装（新）",
     "cet_duty": 20,  "coc": "required",  "hs_prefix": "HS50-63",
     "extra": "高风险货物；仅限Kotoka机场/特马港/塔科拉迪港进口；部分纺织品处35%保护档"},
    {"key": "used_goods",   "name": "二手商品（旧服装/旧货）",
     "cet_duty": 0,   "coc": "exempt",    "hs_prefix": "HS63(旧衣)",
     "extra": "需熏蒸证明/港口卫生证；禁入清单：旧内衣裤/手帕/床垫/洁具/旧冰箱/冷柜/LPG钢瓶/白炽灯灯泡"},
]

# 品类快速索引：key → 品类字典
CATEGORY_MAP = {c["key"]: c for c in CATEGORIES}

# 计算某品类的加纳综合税负率（CET关税 + 附加税费）
def category_ghana_tax(cat_key):
    """返回品类的加纳综合税负率（%）：CET关税 + VAT/NHIL/GETFund等附加税。"""
    cat = CATEGORY_MAP.get(cat_key)
    if not cat:
        return CALC_DEFAULTS["ghana_tax"]    # 未知品类用全局默认
    return round(cat["cet_duty"] + GHANA_EXTRA_TAX_PCT, 2)

# ---------------------------- AI 智能分析配置 ----------------------------------
# 配置文件 ai_config.json（与脚本同目录，首次运行自动生成模板）。
# 兼容所有 OpenAI 风格 /chat/completions 接口：
#   智谱 GLM  : base_url=https://open.bigmodel.cn/api/paas/v4   model=glm-4.7-flash（永久免费）
#   DeepSeek  : base_url=https://api.deepseek.com/v1      model=deepseek-chat
#   通义千问   : base_url=https://dashscope.aliyuncs.com/compatible-mode/v1  model=qwen-plus
# 支持多通道自动容灾：主通道失败（超时/限流/网络错误）时，按 fallbacks 列表顺序
# 依次切换备用通道，全部失败才降级规则引擎。
# api_key 留空时，AI 功能自动降级为内置规则引擎（无需任何费用即可使用）。
AI_CONFIG_FILE = os.path.join(_BASE_DIR, "ai_config.json")
AI_CONFIG_TEMPLATE = {
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": "",          # ← 填入你的 API Key 后即启用真正的大模型分析
    "model": "glm-4.7-flash",
    "timeout": 60,
    # (可选) 汇率历史回填 key：allratestoday.com 免费注册（300 请求/月，个人非商用，无需信用卡），
    # 填入后 `history` 命令可回填 USD/CNY + USD/GHS 最近 30 天；留空自动降级 ECB 参考价补 CNY
    "rate_api_key": "",
    # 备用通道（可选）：主通道失败时按顺序自动切换
    "fallbacks": [
        {"base_url": "https://api.deepseek.com/v1",
         "api_key": "",     # ← 填入备用 Key 后启用
         "model": "deepseek-chat"},
    ],
    # ---- 配件报价数据源（预留多源适配层）----
    # "aigou"（默认）：百度爱采购 b2b.baidu.com 页面采集，无需认证。
    #   官方说明：爱采购 API 仅面向入驻商家/认证服务商开放（商品上架/订单同步等写入型
    #   接口，需 API 白名单），无面向第三方的商品搜索公开 API，故保留页面采集方式。
    # "1688"：1688 开放平台 API（alibaba.item.search）。
    #   接入流程：open.1688.com → 开发者中心 → 企业实名认证（个人权限受限）→
    #   创建应用（需提交 MRD 至 zhaoshang@service.alibaba.com 审核）→ 获取
    #   App Key / App Secret → 申请 alibaba.item.search 接口权限。
    "parts_source": "aigou",
    # 1688 开放平台 API 凭证（parts_source="1688" 时必填）
    "api_1688": {
        "app_key": "",
        "app_secret": "",
        "access_token": "",   # OAuth2.0 令牌（部分接口需要；搜索接口可留空）
    },
    # ===== 鉴权相关（登录/注册/验证码/实名）=====
    # 服务端加密密钥：首次启动随机生成并落盘于此（ai_config.json 已 gitignore，不会入库）。
    # 用于密码两级可逆加密。请勿手动清空，否则已注册账号将无法解密登录。
    "app_secret": "",
    # 邮箱验证码 API（注册/找回密码）—— 免费额度参考：
    #   Resend      3,000 封/月（无需信用卡）  https://resend.com
    #   Brevo       300 封/天                   https://brevo.com
    #   SendGrid    100 封/天（试用）           https://sendgrid.com
    #   阿里云邮件推送  200 封/天（新用户）      https://dm.aliyun.com
    "email_api": {
        "provider": "resend",     # resend | smtp | ""
        "from": "onboarding@resend.dev",
        "resend_key": "",         # Resend API Key（免费档直接填这里即可真实发送）
        # 或用自有 SMTP：
        "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "", "smtp_tls": True,
    },
    # 短信验证码 API（注册/找回密码）—— 免费额度参考（国内均需企业/实名）：
    #   阿里云短信   试用 100 条/月（需企业实名）   https://dysms.console.aliyun.com
    #   腾讯云短信   试用 100 条/月（需企业实名）   https://cloud.tencent.com/product/sms
    #   容联云通讯   试用额度（注册即送）            https://www.yuntongxun.com
    #   聚合数据     按量付费，小额试用            https://www.juhe.cn
    "sms_api": {
        "provider": "",          # aliyun | tencent | ""（留空走 dev 回显模式）
        "key": "", "secret": "", "sign": "", "template_id": "",
        "endpoint": "",
    },
    # 支付宝实名认证 API（预留）—— 资金类实名核验，需企业商户号
    #   文档：https://opendocs.alipay.com/open/201810121004217337
    "alipay_realname": {
        "app_id": "", "private_key": "", "public_key": "",
        "endpoint": "https://openapi.alipay.com/gateway.do",
    },
    # 人机验证（防机器人 / 抗暴力破解）—— 采用 Cloudflare Turnstile（免费、无用量计费、
    # 无可见拼图、隐私友好，是目前最易用的免费方案）。备选：hCaptcha、Google reCAPTCHA v3。
    # site_key 与 secret 留空 → 进入 dev 放行模式（本地联调无需注册即可用）；填入后自动
    # 接入真实人机校验，登录/注册/获取验证码均需先通过验证。
    #   申请：https://dash.cloudflare.com/ → Turnstile → 添加站点（域名填 localhost 亦可）
    "captcha": {
        "provider": "turnstile",   # turnstile | hcaptcha | recaptcha（当前仅实现 turnstile 校验）
        "site_key": "",            # 前端渲染验证框用
        "secret": "",              # 服务端校验用（务必保密，勿外泄）
    },
}

# 最近一次成功调用实际使用的模型名（多通道容灾后用于日报署名）
AI_LAST_ENGINE = None


def load_ai_config():
    """读取 AI 配置；文件不存在时自动生成模板。返回配置字典。"""
    import os
    if not os.path.exists(AI_CONFIG_FILE):
        with open(AI_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(AI_CONFIG_TEMPLATE, f, ensure_ascii=False, indent=2)
        return dict(AI_CONFIG_TEMPLATE)
    try:
        with open(AI_CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        return {**AI_CONFIG_TEMPLATE, **cfg}
    except Exception:
        return dict(AI_CONFIG_TEMPLATE)


def ai_enabled():
    """判断是否已配置可用的 API Key。"""
    return bool(load_ai_config().get("api_key", "").strip())


# ==============================================================================
# 一.x 认证、会话与密码可逆加密（两级）
# ------------------------------------------------------------------------------
# 设计取舍：用户要求「密码至少两级加密，且能解密，方便修改/找回密码」。
# 因此此处采用【可逆】加密而非单向哈希：
#   明文 → XOR(PBKDF2(secret, salt1)) → XOR(PBKDF2(secret, salt2)) → base64
#   salt 随机且随密文存储，解密路径完全可逆。
# 【安全提示】XOR 流密码强度弱于 AES；正式生产建议：
#   1) 用 cryptography 的 Fernet（AES-256+HMAC）作为第一级，本 XOR 作为第二级；
#   2) 同时另存一份 bcrypt/argon2 单向哈希专用于登录校验。
# 当前实现零第三方依赖（纯标准库），且可逆，满足需求；secret 存于 gitignore 的
# ai_config.json，首次运行自动生成。
# ==============================================================================

SESSION_TTL_DAYS = 7
_CODE_TTL_MIN = 5


def _app_secret():
    """服务端密钥：取 ai_config['app_secret']，缺失则随机生成并落盘。"""
    cfg = load_ai_config()
    s = (cfg.get("app_secret") or "").strip()
    if len(s) >= 16:
        return s
    s = secrets.token_hex(32)
    try:
        cfg["app_secret"] = s
        with open(AI_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return s


def _derive_key(secret, salt, length):
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 100_000, dklen=length)


def _xor(data, key):
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def pw_encrypt(plain):
    """两级可逆加密：返回 base64（salt1[16] + salt2[16] + 密文）。"""
    secret = _app_secret()
    pt = (plain or "").encode("utf-8")
    salt1 = secrets.token_bytes(16)
    salt2 = secrets.token_bytes(16)
    k1 = _derive_key(secret, salt1, max(32, len(pt)))
    l1 = _xor(pt, k1)
    k2 = _derive_key(secret, salt2, max(32, len(l1)))
    l2 = _xor(l1, k2)
    return base64.b64encode(salt1 + salt2 + l2).decode("ascii")


def pw_decrypt(blob):
    """解密（可逆）；失败返回 None。"""
    try:
        raw = base64.b64decode(blob)
    except Exception:
        return None
    if len(raw) < 33:
        return None
    salt1, salt2, l2 = raw[:16], raw[16:32], raw[32:]
    k2 = _derive_key(_app_secret(), salt2, max(32, len(l2)))
    l1 = _xor(l2, k2)
    k1 = _derive_key(_app_secret(), salt1, max(32, len(l1)))
    try:
        return _xor(l1, k1).decode("utf-8")
    except Exception:
        return None


def pw_check(blob, plain):
    return pw_decrypt(blob) == plain


def _push_pw_history(user_id, blob, plain, conn=None):
    """写入一条历史密码；每个用户仅保留最近 5 条（含本次）。

    conn 传入时复用调用方连接（不单独提交/关闭），否则自建连接并提交。
    """
    own = conn is None
    if own:
        conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO password_history(user_id,pw_blob,pw_plain,changed_at) VALUES(?,?,?,?)",
        (user_id, blob, plain, now))
    # 仅保留最近 5 条，删除更早的记录（按 id 升序即时间升序）
    conn.execute(
        "DELETE FROM password_history WHERE user_id=? AND id NOT IN "
        "(SELECT id FROM password_history WHERE user_id=? ORDER BY id DESC LIMIT 5)",
        (user_id, user_id))
    if own:
        conn.commit()
        conn.close()


def _pw_recently_used(user_id, plain):
    """校验新密码是否与『当前密码 + 最近 5 次历史密码』重复（改密防复用）。"""
    conn = get_db()
    u = conn.execute("SELECT pw_blob FROM users WHERE id=?", (user_id,)).fetchone()
    if u and pw_check(u["pw_blob"], plain):
        conn.close()
        return True
    rows = conn.execute(
        "SELECT pw_blob FROM password_history WHERE user_id=? ORDER BY id DESC LIMIT 5",
        (user_id,)).fetchall()
    conn.close()
    return any(pw_check(r["pw_blob"], plain) for r in rows)


# 后台直接建号默认密码（不走注册流程、由后台插入的账号统一使用 d12345678）
DEFAULT_BACKEND_PW = "d12345678"


def generate_account():
    """注册成功后自动生成 11 位『易记』登录账号。

    采用高重复规律的号码段（如 52052052052、13981398139），便于人工记忆；
    并在数据库内保证唯一。
    """
    import random
    conn = get_db()
    for _ in range(80):
        if random.random() < 0.5:
            s = str(random.randint(100, 999))        # 3 位种子
            acc = (s * 3 + s[:2])[:11]               # 52052052052
        else:
            s = str(random.randint(1000, 9999))      # 4 位种子
            acc = (s * 2 + s[:3])[:11]               # 13981398139
        if len(acc) == 11 and acc.isdigit() \
                and not conn.execute("SELECT 1 FROM users WHERE account=?", (acc,)).fetchone():
            conn.close()
            return acc
    conn.close()
    return "1" + str(random.randint(10 ** 9, 10 ** 10 - 1))  # 极小概率兜底


def add_backend_user(account, password=DEFAULT_BACKEND_PW, email="", phone="", username=""):
    """后台直接插入账号（不走注册流程），默认密码 d12345678。返回 (ok, msg)。"""
    account = (account or "").strip()
    if not re.match(r"^\d{6,20}$", account):
        return False, "账号应为 6-20 位数字"
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE account=?", (account,)).fetchone():
        conn.close()
        return False, "该账号已存在"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uname = (username or ("用户" + account[-4:])).strip()
    blob = pw_encrypt(password)
    cur = conn.execute(
        "INSERT INTO users(account,username,email,phone,pw_blob,pw_plain,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (account, uname, email or "", phone or "", blob, password, now, now))
    uid = cur.lastrowid
    _push_pw_history(uid, blob, password, conn)   # 初始密码也纳入历史（最近 5 次）
    conn.commit()
    conn.close()
    return True, "已创建账号 %s（默认密码 %s）" % (account, password)


# ---------------- 验证码（邮箱 / 短信）预留接口 ----------------
def _gen_code():
    return "%06d" % secrets.randbelow(1000000)


def _store_code(key, code, purpose):
    conn = get_db()
    now = datetime.now()
    exp = now + timedelta(minutes=_CODE_TTL_MIN)
    conn.execute("DELETE FROM verify_codes WHERE key=? AND purpose=?", (key, purpose))
    conn.execute(
        "INSERT INTO verify_codes(key,code,purpose,created_at,expires_at) VALUES(?,?,?,?,?)",
        (key, code, purpose, now.strftime("%Y-%m-%d %H:%M:%S"),
         exp.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


def _verify_code(key, code, purpose):
    conn = get_db()
    row = conn.execute(
        "SELECT code,expires_at FROM verify_codes WHERE key=? AND purpose=? "
        "ORDER BY id DESC LIMIT 1", (key, purpose)).fetchone()
    conn.close()
    if not row:
        return False
    if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.now():
        return False
    return row["code"] == code


def _send_code_email(target, code):
    """发送验证码邮件。已配置 resend_key / SMTP 则真实发送；否则 dev 模式（返回 False → 调用方回显 code）。"""
    cfg = load_ai_config().get("email_api") or {}
    if cfg.get("resend_key"):
        try:
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=json.dumps({
                    "from": cfg.get("from", "onboarding@resend.dev"),
                    "to": [target],
                    "subject": "验证码",
                    "text": "您的验证码为 %s，%d 分钟内有效。" % (code, _CODE_TTL_MIN),
                }).encode("utf-8"),
                headers={"Authorization": "Bearer " + cfg["resend_key"],
                         "Content-Type": "application/json"},
                method="POST")
            urllib.request.urlopen(req, timeout=10).read()
            return True
        except Exception:
            pass
    if cfg.get("smtp_host") and cfg.get("smtp_user"):
        try:
            import smtplib
            body = ("Subject: 验证码\r\n\r\n您的验证码为 %s，%d 分钟内有效。"
                    % (code, _CODE_TTL_MIN)).encode("utf-8")
            with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587))) as s:
                if cfg.get("smtp_tls", True):
                    s.starttls()
                s.login(cfg["smtp_user"], cfg["smtp_pass"])
                s.sendmail(cfg.get("from", cfg["smtp_user"]), [target], body)
            return True
        except Exception:
            pass
    return False  # dev 模式


def _send_code_sms(target, code):
    """发送验证码短信（预留阿里云/腾讯云等）。未配置 provider 时返回 False → dev 回显。"""
    cfg = load_ai_config().get("sms_api") or {}
    if cfg.get("provider") and cfg.get("key"):
        # TODO: 按所选 provider 调用其短信网关（拼签名 + 模板 + 参数）。
        # 阿里云示例：用 SMS 请求签名 + SendSms API，TemplateParam={"code":"123456"}
        pass
    return False  # dev 模式


# ---------------- 会话（Cookie） ----------------
def _create_session(user_id):
    token = secrets.token_hex(32)
    now = datetime.now()
    exp = now + timedelta(days=SESSION_TTL_DAYS)
    conn = get_db()
    conn.execute(
        "INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        (token, user_id, now.strftime("%Y-%m-%d %H:%M:%S"),
         exp.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    return token


def _session_user(token):
    if not token:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT s.expires_at, u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token=?", (token,)).fetchone()
    conn.close()
    if not row:
        return None
    if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.now():
        return None
    return dict(row)


def _user_public(u):
    """对外只暴露非敏感字段。"""
    return {"id": u["id"], "account": u["account"], "username": u.get("username") or u["account"],
            "email": u.get("email") or "", "phone": u.get("phone") or "",
            "realname_status": u.get("realname_status", 0)}


# ==============================================================================
# 二、数据库初始化
# ==============================================================================

def get_db():
    """获取数据库连接，并确保所有表已创建（幂等操作，可重复调用）。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row  # 让查询结果可以按列名取值
    try:
        # WAL 模式：读写可并发（抓取长事务期间看板/纠正请求不再 database is locked）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rates (          -- 汇率日表
            date     TEXT PRIMARY KEY,              -- 日期 YYYY-MM-DD
            usd_cny  REAL,                          -- 美元兑人民币
            usd_ghs  REAL,                          -- 美元兑塞地
            ghs_cny  REAL                           -- 塞地兑人民币（交叉汇率）
        );
        CREATE TABLE IF NOT EXISTS policies (       -- 政策/新闻条目表
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            source    TEXT,                         -- 来源网站名
            title     TEXT,                         -- 标题
            url       TEXT,                         -- 链接
            keyword   TEXT,                         -- 命中的关键词（多个逗号分隔，未命中为空）
            first_seen TEXT                         -- 首次抓到该条的日期时间
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_policy_url ON policies(source, url);
        CREATE TABLE IF NOT EXISTS fetch_log (      -- 每次采集的运行日志
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            source  TEXT,
            ok      INTEGER,                        -- 1 成功 / 0 失败
            msg     TEXT,                           -- 说明（新增条数 或 失败原因）
            ts      TEXT
        );
        CREATE TABLE IF NOT EXISTS ai_reports (     -- AI/规则引擎生成的分析日报
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT,
            engine  TEXT,                           -- 模型名 或 "rule"（规则引擎）
            content TEXT                            -- 日报正文（Markdown）
        );
        CREATE TABLE IF NOT EXISTS parts_quotes (   -- 配件批发报价（百度爱采购）
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword    TEXT,                        -- 搜索关键词
            title      TEXT,                        -- 商品标题
            price      REAL,                        -- 单价（元）
            moq        TEXT,                        -- 起订量（如 "500件"）
            supplier   TEXT,                        -- 供应商名称
            location   TEXT,                        -- 发货地（"重庆 渝北区" 等）
            url        TEXT,                        -- 商品/落地页链接
            first_seen TEXT                         -- 首次抓到时间
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_part ON parts_quotes(keyword, title, supplier);
        CREATE TABLE IF NOT EXISTS goods (          -- 本地商品主数据（供测算器/报价单引用）
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,              -- 商品名称
            sku         TEXT DEFAULT '',            -- SKU 编码（对齐《数据查询手册》主数据表）
            category    TEXT DEFAULT '',            -- 品类key（对应 CATEGORIES，如 motor_parts）
            unit_price  REAL DEFAULT 0,             -- 采购单价（元/件）
            unit_weight REAL DEFAULT 0,             -- 单件重量（kg/件）
            unit_volume REAL DEFAULT 0,             -- 单件体积（m³/件，拼箱CBM计费用）
            moq         INTEGER DEFAULT 1,          -- 最小起订量（件）
            hs_code     TEXT DEFAULT '',            -- HS 编码（10位，报关用）
            tariff_rate REAL DEFAULT -1,            -- 加纳综合税负率（%，-1=用品类默认）
            source      TEXT DEFAULT 'manual',      -- 来源：manual人工 / ai文档识别 / photo照片识别 / auto抓取
            note        TEXT DEFAULT '',            -- 备注
            created_at  TEXT                        -- 录入时间
        );
        CREATE TABLE IF NOT EXISTS quotes (          -- 报价单快照（人工/AI预测对比用）
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,                       -- 报价单名称（如"首单摩配5SKU"）
            status      TEXT DEFAULT 'manual',      -- manual人工 / ai_predict / compared已对比
            items_json  TEXT DEFAULT '[]',          -- 商品清单JSON（快照）
            params_json TEXT DEFAULT '{}',          -- 费用参数JSON（快照）
            total_cost  REAL DEFAULT 0,             -- 总成本（元）
            quote       REAL DEFAULT 0,             -- 对加方报价（元）
            quote_usd   REAL DEFAULT 0,             -- 报价折美元
            profit      REAL DEFAULT 0,             -- 毛利（元）
            margin_pct  REAL DEFAULT 0,             -- 毛利率（%）
            markup      REAL DEFAULT 0,             -- 加价率（%）
            usd_cny     REAL DEFAULT 7.2,           -- 保存时汇率
            note        TEXT DEFAULT '',            -- 备注
            created_at  TEXT                        -- 创建时间
        );
        CREATE TABLE IF NOT EXISTS ai_predictions (  -- AI报价预测记录（对比人工报价）
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id    INTEGER,                     -- 关联报价单ID（可空=独立预测）
            engine      TEXT,                       -- 模型名 或 "rule"
            predicted_markup   REAL DEFAULT 0,       -- AI建议加价率（%）
            predicted_quote   REAL DEFAULT 0,        -- AI建议报价（元）
            predicted_profit  REAL DEFAULT 0,        -- AI建议毛利（元）
            manual_markup     REAL DEFAULT 0,        -- 人工加价率（%，对比基准）
            manual_quote      REAL DEFAULT 0,        -- 人工报价（元）
            diff_pct          REAL DEFAULT 0,        -- 报价差异率（%，正=AI更高）
            reasoning         TEXT DEFAULT '',       -- AI推理过程（Markdown）
            items_json        TEXT DEFAULT '[]',     -- 预测时的商品清单（快照）
            context_json      TEXT DEFAULT '{}',     -- 预测时的市场上下文（汇率/政策等快照）
            created_at        TEXT
        );
        CREATE TABLE IF NOT EXISTS vision_samples (  -- CLIP 视觉样本库（迭代学习）
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword     TEXT NOT NULL,               -- 抓取关键词（对应 parts_quotes.keyword）
            img_url     TEXT,                         -- 原图 URL
            embedding   BLOB,                         -- CLIP 512 维嵌入（float32 → 2048 字节）
            label       INTEGER NOT NULL,             -- 1=正例（确实是该商品）/ 0=负例（不是）
            source      TEXT DEFAULT 'clip_auto',    -- clip_auto=自动判定 / user_correct=用户纠正
            parts_id    INTEGER,                      -- 关联 parts_quotes.id（可空）
            created_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_vs_kw ON vision_samples(keyword);
        CREATE INDEX IF NOT EXISTS idx_vs_label ON vision_samples(label);
        CREATE TABLE IF NOT EXISTS contracts (       -- 合同审核记录
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT,                         -- 合同标题
            content     TEXT,                         -- 合同原文
            review_result TEXT,                       -- AI审核结果（Markdown）
            engine      TEXT,                        -- 使用的AI引擎
            risk_level  TEXT,                        -- 风险等级：low/medium/high
            risk_score  INTEGER,                     -- 风险扣分（越高越危险）
            created_at  TEXT
        );

        -- ===== 鉴权：用户 / 会话 / 验证码 =====
        CREATE TABLE IF NOT EXISTS users (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            account          TEXT UNIQUE,             -- 登录账号（唯一，4-20位）
            username         TEXT,                    -- 用户名/昵称
            email            TEXT,                    -- 邮箱（预留邮箱验证码）
            phone            TEXT,                    -- 手机号（预留短信验证码）
            pw_blob          TEXT,                    -- 密码两级可逆加密密文
            realname         TEXT,                    -- 实名姓名（预留支付宝实名）
            idno             TEXT,                    -- 证件号（预留，建议另加密）
            realname_status  INTEGER DEFAULT 0,       -- 0未认证 1已认证 2审核中
            created_at       TEXT,
            updated_at       TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_account ON users(account);
        CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT PRIMARY KEY,
            user_id     INTEGER,
            created_at  TEXT,
            expires_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS verify_codes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT,                         -- email 或 phone
            code        TEXT,
            purpose     TEXT,                         -- register | reset
            created_at  TEXT,
            expires_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_vc_key ON verify_codes(key, purpose);
        -- 密码历史：每个用户仅保留最近 5 次用过的密码（改密时验证身份 / 防复用）
        CREATE TABLE IF NOT EXISTS password_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,                     -- 关联 users.id
            pw_blob     TEXT,                        -- 历史密码密文（两级可逆）
            pw_plain    TEXT,                         -- 历史原始密码（仅记录备查）
            changed_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ph_user ON password_history(user_id, id);
    """)
    # 字段迁移：商品展示图、AI 图像校验结果（兼容旧库）
    for col_sql in (
        "ALTER TABLE parts_quotes ADD COLUMN img TEXT",
        "ALTER TABLE parts_quotes ADD COLUMN img_ok INTEGER DEFAULT 1",
        "ALTER TABLE parts_quotes ADD COLUMN img_conf REAL",
        # goods 表新增字段（兼容旧库）
        "ALTER TABLE goods ADD COLUMN unit_volume REAL DEFAULT 0",
        "ALTER TABLE goods ADD COLUMN hs_code TEXT DEFAULT ''",
        "ALTER TABLE goods ADD COLUMN tariff_rate REAL DEFAULT -1",
        # 用户表新增原始密码字段（兼容旧库）
        "ALTER TABLE users ADD COLUMN pw_plain TEXT",
    ):
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass
    # 历史库兼容：为已存在但未记录原始密码的用户回填（密文可逆，可安全还原）
    try:
        for row in conn.execute("SELECT id, pw_blob FROM users WHERE pw_plain IS NULL"):
            p = pw_decrypt(row["pw_blob"])
            if p is not None:
                conn.execute("UPDATE users SET pw_plain=? WHERE id=?", (p, row["id"]))
        conn.commit()
    except Exception:
        pass
    return conn


# ==============================================================================
# 三、数据采集：汇率
# ==============================================================================

def fetch_rates():
    """抓取美元兑人民币/塞地汇率，写入 rates 表。返回 (是否成功, 摘要文本)。"""
    today = datetime.now().strftime("%Y-%m-%d")
    req = urllib.request.Request(RATE_API, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("result") != "success":
            return False, "汇率API返回异常"
        usd_cny = data["rates"]["CNY"]
        usd_ghs = data["rates"]["GHS"]
        ghs_cny = usd_cny / usd_ghs  # 交叉汇率：1 塞地 = ? 人民币
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO rates VALUES (?,?,?,?)",
                     (today, usd_cny, usd_ghs, ghs_cny))
        conn.commit(); conn.close()
        return True, f"USD/CNY={usd_cny:.4f}  USD/GHS={usd_ghs:.4f}  GHS/CNY={ghs_cny:.4f}"
    except Exception as e:
        return False, f"汇率采集失败: {e}"


# ==============================================================================
# 三-b、汇率历史回填（前 N 天）
# ==============================================================================

def _load_rate_api_key():
    """读取 ai_config.json 中的汇率历史 key（rate_api_key 字段）；未配置返回 None。"""
    try:
        cfg = load_ai_config()
        return (cfg.get("rate_api_key") or "").strip() or None
    except Exception:
        return None


def _parse_history_payload(data, target):
    """
    解析 allratestoday.com 历史端点响应，提取 {date: rate}。
    响应结构：{"source":"USD","target":"CNY","data":[{"date":"2026-07-22","rate":6.76745,"timestamp":...}]}
    无法识别返回 None（调用方给出原始响应片段便于排查）。
    """
    if not isinstance(data, dict):
        return None
    items = data.get("data")
    if not isinstance(items, list) or not items:
        return None
    out = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        date = str(item.get("date") or "")[:10]
        rate = item.get("rate")
        if not date or rate is None:
            continue
        try:
            out[date] = float(rate)
        except (TypeError, ValueError):
            continue
    return out or None


def _fetch_history_with_key(key, start, end, period="30d"):
    """
    用 allratestoday.com 免费 key 回填历史汇率（Bearer 认证）。
    该服务一次仅支持单个 target，故对 USD/CNY、USD/GHS 各请求一次再按日期合并。
    period 按缺口天数选择（7d/14d/30d），节省免费请求额度。
    返回 (是否成功, 摘要文本)。
    """
    headers = {"Authorization": "Bearer " + key, "User-Agent": "Mozilla/5.0"}
    merged = {}
    errs = []
    for target in ("CNY", "GHS"):
        q = urllib.parse.urlencode({"source": "USD", "target": target, "period": period})
        try:
            req = urllib.request.Request(RATE_HISTORY_API + "?" + q, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parsed = _parse_history_payload(data, target)
            if not parsed:
                errs.append(target + ": 响应无法解析 " + str(data)[:100])
                continue
            for date, rate in parsed.items():
                merged.setdefault(date, [None, None])
                merged[date][0 if target == "CNY" else 1] = rate
        except Exception as e:
            errs.append(target + ": " + str(e))
    if not merged:
        return False, "allratestoday.com 历史接口全部失败: " + "; ".join(errs)
    rows = [(dt, v[0], v[1]) for dt, v in sorted(merged.items())]
    n = _write_history_rows(rows, start, end)
    tip = ("（部分币种缺失: " + "; ".join(errs) + "）") if errs else ""
    return n > 0, ("allratestoday.com 回填 " + str(n) + " 天 " + tip).rstrip()


def _fetch_history_cny_frankfurter(start, end):
    """
    无 key 兜底：欧洲央行参考汇率（frankfurter.app）回填 USD/CNY 历史（仅工作日有数据）。
    只补缺失日期，不覆盖已入库（保持已采集的市场汇率口径）；GHS 无 ECB 数据，跳过并提示。
    返回 (是否成功, 摘要文本)。
    """
    url = FRANKFURTER_API.format(start=start, end=end)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rates = data.get("rates") or {}
        if not rates:
            return False, "ECB 接口未返回数据"
        conn = get_db()
        filled = skipped = 0
        for date, val in rates.items():
            if not date or not isinstance(val, dict):
                continue
            cny = val.get("CNY")
            if cny is None:
                continue
            if conn.execute("SELECT 1 FROM rates WHERE date=?", (date,)).fetchone():
                skipped += 1   # 已有当日记录：不覆盖，保持原口径
                continue
            conn.execute("INSERT OR REPLACE INTO rates(date, usd_cny, usd_ghs, ghs_cny) "
                         "VALUES (?,?,NULL,NULL)", (date, float(cny)))
            filled += 1
        conn.commit(); conn.close()
        tip = "（GHS 无官方免费历史，留待每日采集积累或配置 rate_api_key 后补全）"
        return True, f"ECB参考汇率回填 {filled} 天、跳过已有 {skipped} 天 {tip}"
    except Exception as e:
        return False, f"ECB 接口调用失败: {e}"


def _write_history_rows(rows, start, end):
    """把 [(date, usd_cny, usd_ghs)] 写入 rates 表（INSERT OR REPLACE，覆盖口径为市场中间价）。返回写入天数。"""
    conn = get_db()
    n = 0
    for date, cny, ghs in rows:
        if not (start <= date <= end):
            continue
        if cny is None or ghs is None:
            continue
        ghs_cny = cny / ghs
        conn.execute("INSERT OR REPLACE INTO rates VALUES (?,?,?,?)",
                     (date, cny, ghs, ghs_cny))
        n += 1
    conn.commit(); conn.close()
    return n


def fetch_rates_history(days=30, start=None, end=None):
    """
    回填历史汇率到 rates 表（多源策略）：
      1) ai_config.json 配置了 rate_api_key → allratestoday.com（USD/CNY + USD/GHS 市场中间价，
         含周末日历日序列，Bearer 认证；period 按「今天距 start 的天数」选择，省免费额度）
      2) 无 key → 欧洲央行参考汇率 frankfurter.app 补 USD/CNY 历史（仅工作日，不覆盖已有记录），
         GHS 无 ECB 数据跳过
    start/end 显式指定回填区间（YYYY-MM-DD）；缺省为最近 days 天。
    返回 (是否成功, 摘要文本)。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    end = end or today
    if not start:
        days = max(1, min(days, 30))
        start = (datetime.now().date() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    # allratestoday 的 period 是「从今天往回 N 天」，须覆盖到 start
    reach = (datetime.now().date() - datetime.strptime(start, "%Y-%m-%d").date()).days + 1
    period = "7d" if reach <= 7 else ("14d" if reach <= 14 else "30d")
    key = _load_rate_api_key()
    if key:
        return _fetch_history_with_key(key, start, end, period)
    return _fetch_history_cny_frankfurter(start, end)


def sync_rates_to_today():
    """
    启动时汇率缺口同步：从今天往回找最长连续数据段，段之前最后一条数据日期即
    「上次停留时间」，把中间缺口一直补齐到今天（含当日）。策略：
      - 库为空        → 先回填 30 天历史，再采当日实时
      - 无缺口且当日已有 → 无事可做
      - 只缺当日      → 采当日实时（fetch_rates）
      - 中间有缺口    → 历史回填缺口区间；缺口早端距今超 30 天时尽力回填并提示
      - 回填后当日仍缺 → 追加一次当日实时采集
    同步失败不影响看板启动（调用方已捕获异常）。
    返回 (是否执行了采集, 摘要文本)。
    """
    today_d = datetime.now().date()
    today = today_d.strftime("%Y-%m-%d")
    conn = get_db()
    dates = {r[0] for r in conn.execute("SELECT date FROM rates")}
    conn.close()

    if not dates:  # 库为空：首次使用
        print("[汇率同步] 库内无数据，先回填 30 天历史 ...")
        ok, msg = fetch_rates_history(30)
        print(f"[汇率同步] {msg}")
        if not _has_rate(today):
            fetch_rates()
        return True, "首次回填完成"

    # 从今天往回找最长连续段的最早一天 streak_start（今天缺则为明天，即空段）
    streak_start = today_d + timedelta(days=1)
    d = today_d
    while d.strftime("%Y-%m-%d") in dates:
        streak_start = d
        d -= timedelta(days=1)

    # 段之前最后一条数据 = 上次停留时间
    prev = max((x for x in dates if x < streak_start.strftime("%Y-%m-%d")), default=None)

    if prev is None:  # 连续段已覆盖库内全部数据
        if today in dates:
            return False, "汇率已最新（%s）" % streak_start.strftime("%Y-%m-%d")
        print("[汇率同步] 仅缺今日，采集实时汇率 ...")
        ok, msg = fetch_rates()
        print("[汇率同步] " + msg)
        return ok, msg

    # 中间存在缺口：prev+1 ~ streak_start-1（若今天也缺，则到今天）
    hole_start_d = datetime.strptime(prev, "%Y-%m-%d").date() + timedelta(days=1)
    hole_end_d = streak_start - timedelta(days=1)
    hole_start = hole_start_d.strftime("%Y-%m-%d")
    hole_end = hole_end_d.strftime("%Y-%m-%d")

    warn = ""
    reach_days = (today_d - hole_start_d).days + 1
    if reach_days > 30:
        print(f"[汇率同步] 上次停留 {prev}，缺口 {hole_start} ~ {hole_end}（早端距今 {reach_days} 天，"
              f"免费源单次仅能覆盖最近 30 天，尽力回填）...")
        warn = "；更早缺口超出免费源 30 天范围，仅能补最近部分"
    else:
        print(f"[汇率同步] 上次停留 {prev}，缺口 {hole_start} ~ {hole_end}，回填中 ...")
    ok, msg = fetch_rates_history(0, start=hole_start, end=hole_end)
    print("[汇率同步] " + msg + warn)

    # 当日仍缺（今天本就在缺口内但历史源未含今日，或今天单独缺）→ 追加实时
    if not _has_rate(today):
        ok2, msg2 = fetch_rates()
        print("[汇率同步] 补采今日实时: " + msg2)
        ok = ok or ok2
    return True, (msg + warn).strip()


def _has_rate(date):
    """rates 表中是否已有指定日期记录。"""
    conn = get_db()
    hit = conn.execute("SELECT 1 FROM rates WHERE date=?", (date,)).fetchone()
    conn.close()
    return hit is not None


# ==============================================================================
# 四、数据采集：政策监控
# ==============================================================================

def http_get(url):
    """
    带浏览器请求头抓取网页；遇到政府网站证书问题时自动降级为"不校验证书"重试。
    返回原始字节流（编码在调用方处理）。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except Exception as e:
        # 部分政府站证书链不规范：降级为忽略证书校验（仅用于公开信息读取）。
        # 注意：证书错误可能以 ssl.SSLError 或被 URLError 包装两种形式抛出，都要识别。
        if isinstance(e, ssl.SSLError) or "CERTIFICATE" in str(e).upper():
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                return resp.read()
        raise


class LinkExtractor(HTMLParser):
    """简易 HTML 解析器：提取页面里所有 <a href> 链接及其文字标题。"""

    def __init__(self):
        super().__init__()
        self.links = []        # 结果：[(href, text), ...]
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = "".join(self._text).strip()
            # 只保留标题长度 >= 8 的链接（过滤导航、图标等噪音）
            if text and len(text) >= 8:
                self.links.append((self._href, text))
            self._href = None
            self._text = []


def fetch_policies():
    """遍历政策源，抓取新闻标题入库，返回各源摘要列表。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = []
    conn = get_db()
    for src in POLICY_SOURCES:
        try:
            raw = http_get(src["url"])
            # 自动尝试常见编码（政府网站多为 utf-8 或 gbk）
            for enc in ("utf-8", "gbk", "gb18030"):
                try:
                    html = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            parser = LinkExtractor()
            parser.feed(html)

            new_count, hit_count, total = 0, 0, 0
            for href, text in parser.links[:80]:    # 每源最多处理 80 条，防噪音
                if is_noise(text):
                    continue                          # 跳过页脚备案/版权等噪音链接
                full_url = urljoin(src["url"], href)  # 相对链接转绝对链接
                total += 1
                keyword = ",".join(k for k in POLICY_KEYWORDS if k in text)
                if keyword:
                    hit_count += 1
                cur = conn.execute(
                    "INSERT OR IGNORE INTO policies (source,title,url,keyword,first_seen) "
                    "VALUES (?,?,?,?,?)",
                    (src["name"], text, full_url, keyword, now))
                if cur.rowcount > 0:                # rowcount>0 说明是新条目
                    new_count += 1
            conn.execute("INSERT INTO fetch_log (source,ok,msg,ts) VALUES (?,?,?,?)",
                         (src["name"], 1, f"抓到{total}条/新增{new_count}/关键词命中{hit_count}", now))
            summary.append((src["name"], True, f"抓取 {total} 条，新增 {new_count}，关键词命中 {hit_count}"))
        except Exception as e:
            conn.execute("INSERT INTO fetch_log (source,ok,msg,ts) VALUES (?,?,?,?)",
                         (src["name"], 0, str(e)[:200], now))
            summary.append((src["name"], False, f"采集失败（可能反爬/改版）: {str(e)[:60]}"))
    conn.commit(); conn.close()
    return summary


def run_fetch():
    """执行一次完整采集（汇率 + 政策 + 配件报价），并打印摘要。"""
    print("=" * 60)
    print("开始数据采集 ...", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    ok, msg = fetch_rates()
    print(f"[汇率] {'成功' if ok else '失败'}: {msg}")
    for name, ok, msg in fetch_policies():
        print(f"[{name}] {'成功' if ok else '失败'}: {msg}")
    for kw, ok, msg in fetch_parts():
        print(f"[配件·{kw}] {'成功' if ok else '失败'}: {msg}")
    print("=" * 60)


# ==============================================================================
# 四点五、数据采集：配件批发报价（百度爱采购，聚合全国/重庆摩配汽配供应商）
# ==============================================================================

# 默认监控的配件关键词（面向加纳市场的通用型摩配 + 汽配，重庆是主要产地）
PART_KEYWORDS = [
    "摩托车刹车片", "摩托车链条", "摩托车轮胎", "摩托车火花塞",
    "摩托车机油滤芯", "汽车刹车片",
]


def _extract_js_var(text, varname):
    """从 HTML 里提取 window.xxx = {...} 的 JSON 字符串（括号配平 + 字符串感知）。"""
    m = re.search(r"window\." + varname + r"\s*=\s*", text)
    if not m:
        return None
    start, depth, instr, esc, i = m.end(), 0, False, False, m.end()
    while i < len(text):
        ch = text[i]
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif instr:
            if ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


def _walk_products(obj, out):
    """递归遍历 SSR JSON，收集所有含 priceList+fullName 的商品对象。"""
    if isinstance(obj, dict):
        if "priceList" in obj and "fullName" in obj:
            out.append(obj)
            return
        for v in obj.values():
            _walk_products(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_products(v, out)


# 辅助配件/工具/服务类噪音词：标题命中即视为"关键词商品的辅助品"而非商品本身
PART_NOISE_WORDS = (
    "设备", "机器", "打磨机", "模具", "冲床", "雕刻机", "包装机", "贴标机",
    "生产线", "检测仪", "测厚仪", "粉碎", "造粒", "回收", "二手", "加盟",
    "工具", "扳手", "套筒", "维修包", "传感器", "报警线", "钢背",
    "摩擦材料", "气压表", "气表", "气门嘴", "平衡块", "补胎", "翻新",
    "清洗剂", "润滑", "贴纸", "说明书",
    # 非目标品类串扰（铁路/矿山/工程机械/船用等混入搜索结果）
    "闸瓦", "铁路", "火车", "矿用", "井下", "轨道", "有轨", "单轨",
    "工程机械", "船用", "摩托艇", "粉末", "锻造", "冲压", "加工厂",
    # 链条类串扰（链条锁/防滑链/链轮/齿轮/仓储输送链/油锯链锯/驻车架等）
    "防滑链", "链轮", "密集柜", "货架", "索具", "吊钩", "起重", "锁",
    "链锯", "油锯", "电锯", "伐木", "锯树", "园林", "驻车架", "起车架",
    "齿轮", "正时齿轮", "传动齿轮", "减速机", "变速箱",
    "修理", "修车", "扎带", "雪地", "防滑钉",
    # 轮胎类串扰（内胎非外胎）
    "内胎",
    # 其他辅助品
    "弹簧", "扭簧", "油膏", "油脂", "气凝胶", "隔热",
    # 机械设备串扰（碎料机/组装机等混入搜索结果）
    "破碎机", "组装机", "螺丝机", "搓球机", "团球机", "压块机",
    "非标定制", "定制加工", "来图加工",
)
# 核心词后紧跟这些字 → 大概率是"配件的配件"（如"链条"→"链条锁"）
PART_AUX_SUFFIX = (
    "油", "脂", "清洗", "清洁", "养护", "保养", "修复", "打磨", "胶", "蜡",
    "调节器", "涨紧器", "张紧器", "护板", "护罩", "垫", "帽", "套", "锁",
    "刷", "纸", "剂",
)


def _part_core(kw):
    """提取关键词核心词：去掉地域前缀（重庆）与车型前缀（摩托车/汽车等）。"""
    core = kw.replace("重庆", "").replace(" ", "")
    for v in ("摩托车", "汽车", "电动车", "三轮车", "踏板车", "卡车", "重卡", "通机"):
        if core.startswith(v) and len(core) > len(v):
            core = core[len(v):]
            break
    return core


def _is_relevant_part(title, kw):
    """
    判断商品标题是否为关键词商品本身（而非辅助配件/工具/服务）。
    规则：① 标题必须包含核心词（如"摩托车刹车片"→必须含"刹车片"）；
         ② 核心词后紧跟"油/工具/护板"等字 → 是配件的配件，剔除；
         ③ 标题含"设备/模具/扳手"等噪音词 → 辅助品或服务，剔除。
    """
    core = _part_core(kw)
    if not core:
        return True
    title = title or ""
    pos = title.find(core)
    if pos < 0:
        return False
    tail = title[pos + len(core): pos + len(core) + 4]   # 核心词后 4 字内
    if any(tail.startswith(a) for a in PART_AUX_SUFFIX):
        return False
    if any(w in title for w in PART_NOISE_WORDS):
        return False
    return True


def _price_band_filter(items, get_price):
    """
    按中位数价格带宽过滤异常值：疑似搜索页里其他商品的价格串扰。
    保留 [中位数/5, 中位数×5] 区间内的报价。
    """
    prices = sorted(get_price(p) for p in items)
    if not prices:
        return items
    n = len(prices)
    median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    if median <= 0:
        return items
    lo, hi = median / 5, median * 5
    return [p for p in items if lo <= get_price(p) <= hi]


# ---- 数据源适配层（多源预留）------------------------------------------------
# 归一化商品字段约定（与爱采购 SSR JSON 保持同构，下游过滤/CLIP/入库零改动）：
#   fullName=标题  price=单价(float)  priceList=[{minValue,unitCode}]=起订量
#   tpProvider.name=供应商  fhLocation=发货地  picUrl=商品图  jUrl=商品页链接
def parts_source():
    """当前配件数据源："aigou"（爱采购页面采集）或 "1688"（开放平台 API）。"""
    try:
        return (load_ai_config().get("parts_source") or "aigou").strip().lower()
    except Exception:
        return "aigou"


def _sign_1688(params, app_secret):
    """
    1688 开放平台 MD5 签名（官方规范）：
    参数按 key 的 ASCII 升序排序 → 拼接为 key1value1key2value2（无分隔符）
    → 首尾各拼接一次 app_secret → MD5 加密 → 转大写。
    """
    joined = "".join(k + str(v) for k, v in sorted(params.items()) if v is not None and k != "sign")
    return hashlib.md5((app_secret + joined + app_secret).encode("utf-8")).hexdigest().upper()


def _search_1688(kw, page_size=40):
    """
    1688 开放平台商品搜索（alibaba.item.search）→ 归一化商品对象列表。
    预留接口：需在 ai_config.json 配置 parts_source="1688" 及 api_1688 凭证
    （企业认证 + 创建应用审核通过后获取）。未配置时抛出带指引的异常。
    """
    cfg = (load_ai_config().get("api_1688") or {})
    app_key = (cfg.get("app_key") or "").strip()
    app_secret = (cfg.get("app_secret") or "").strip()
    if not app_key or not app_secret:
        raise RuntimeError(
            "1688 API 未配置：需在 open.1688.com 完成企业实名认证并创建应用"
            "（提交 MRD 审核），然后在 ai_config.json 填入 api_1688.app_key/app_secret "
            "并将 parts_source 设为 \"1688\"；当前可继续使用爱采购源（parts_source=\"aigou\"）")
    params = {
        "app_key": app_key,
        "method": "alibaba.item.search",
        "timestamp": str(int(time.time())),      # 秒级时间戳（毫秒级会签名失败）
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        "q": kw,                                  # 搜索关键词（需 URL 编码）
        "page": 1,
        "pageSize": min(page_size, 40),           # 官方上限 40
        "sort": "price_asc",
    }
    if cfg.get("access_token"):
        params["access_token"] = cfg["access_token"]
    params["sign"] = _sign_1688(params, app_secret)
    url = "https://gw.open.1688.com/openapi/param2/1/portals.open/api/findItem?" + \
        urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "trade-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
    # 官方响应：{success, result:{items:[...]}, error_code, error_message}
    if not data.get("success"):
        raise RuntimeError(f"1688 API 返回错误: {data.get('error_message') or data.get('error_code')}")
    items = ((data.get("result") or {}).get("items")) or []
    out = []
    for it in items:
        try:
            price = float(it.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        # 发货地：兼容字符串 / {province, city} 对象两种返回格式
        loc = it.get("sendGoodsAddress")
        if not loc:
            raw_loc = it.get("location")
            loc = raw_loc if isinstance(raw_loc, str) else ((raw_loc or {}).get("city") or "")
        out.append({
            "fullName": it.get("subject") or it.get("title") or "",
            "price": price,
            "priceList": [{"minValue": it.get("minOrderQuantity") or "",
                           "unitCode": it.get("unit") or ""}],
            "tpProvider": {"name": it.get("companyName") or it.get("sellerNick") or ""},
            "fhLocation": loc or "",
            "picUrl": (it.get("picUrl") or it.get("image") or ""),
            "jUrl": it.get("detailUrl") or it.get("offerUrl") or "",
        })
    return out


def fetch_parts(keywords=None, save_top=30):
    """
    抓取配件批发报价，写入 parts_quotes 表。多源适配（ai_config.json 的 parts_source）：
      · "aigou"（默认）— 百度爱采购 b2b.baidu.com 页面采集。数据源说明：爱采购聚合了
        1688/各地供应商的公开批发报价，无需登录、无强反爬；重庆本地供应商会出现在
        结果中（发货地字段可筛选）。爱采购官方 API 仅面向入驻商家/认证服务商（写入型
        接口，需白名单认证），无公开搜索 API，故保留页面采集方式。
      · "1688" — 1688 开放平台 API（alibaba.item.search，需企业认证 + 应用审核后
        在 ai_config.json 配置 api_1688 凭证；未配置时报错并提示切回 aigou）。
    过滤规则：① 标题须含关键词核心词且非辅助配件/工具/服务；
             ② 单价 ∈ [1, 10000]，且落在中位数价格带宽 [中位数/5, 中位数×5] 内，
               剔除搜索页混入的其他商品异常价格；
             ③ 过滤后按价格升序入库。
    返回 [(关键词, 是否成功, 摘要), ...]
    """
    keywords = keywords or PART_KEYWORDS
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    summary = []

    def _search(query):
        """单次爱采购搜索，返回商品对象列表；遇 403/429 限流自动退避重试。"""
        url = "https://b2b.baidu.com/s?q=" + urllib.parse.quote(query)
        for attempt in range(5):                    # 最多 5 次，最长退避约 75s
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Cache-Control": "max-age=0",
                })
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                break
            except urllib.error.HTTPError as e:
                if e.code in (403, 429) and attempt < 4:
                    time.sleep(15 * (attempt + 1))  # 退避：15s、30s、45s、60s
                    continue
                raise
        text = raw.decode("utf-8", "ignore")
        js = _extract_js_var(text, "data")          # 商品数据在 SSR 变量 window.data 里
        if not js:
            raise RuntimeError("页面结构变化：未找到 window.data")
        products = []
        _walk_products(json.loads(js), products)
        return products

    for kw in keywords:
        if summary:                                # 关键词之间留间隔，避免触发限流
            time.sleep(2)
        try:
            if parts_source() == "1688":
                # 1688 开放平台 API：单轮搜索（结果自带发货地字段，无需"重庆"前缀二轮）
                products = _search_1688(kw)
            else:
                # 爱采购页面采集：两轮搜索（全网比价 + "重庆"前缀定向，
                # 爱采购 URL 不支持地区参数，用关键词实现）；两次搜索间留 5 秒避免限流（403）
                products = _search(kw)
                time.sleep(5)
                products += _search("重庆 " + kw)
            if not products:
                raise RuntimeError("未解析到商品数据")
            # 合并去重（同名同供应商），过滤 <1 元的低价引流噪音后按价格升序入库
            def get_price(p):
                try:
                    return float(p.get("price") or 0)
                except (TypeError, ValueError):
                    return 0
            seen, uniq = set(), []
            for p in products:
                key = (p.get("fullName"), (p.get("tpProvider") or {}).get("name"))
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(p)
            uniq = [p for p in uniq if 1.0 <= get_price(p) <= 10000]
            raw_n = len(uniq)
            # 相关性过滤：只要关键词商品本身，剔除辅助配件/工具/服务
            uniq = [p for p in uniq if _is_relevant_part(p.get("fullName") or "", kw)]
            rel_n = len(uniq)
            # 价格异常值过滤：剔除疑似页面其他商品串入的价格
            uniq = _price_band_filter(uniq, get_price)
            uniq.sort(key=get_price)
            # 机器学习图像校验：用 CLIP 判断商品展示图是否为目标商品
            # 标题规则先过滤一遍，图像模型作为第二道防线，捕获标题规则无法识别的串扰图
            ml_pass = ml_fail = ml_skip = 0
            new_count = 0
            for p in uniq[:save_top]:
                pl = (p.get("priceList") or [{}])[0]
                moq = ""
                if pl.get("minValue"):
                    moq = f'{pl["minValue"]}{pl.get("unitCode") or ""}'
                supplier = (p.get("tpProvider") or {}).get("name") or ""
                loc = (p.get("fhLocation") or "").strip()
                img = (p.get("picUrl") or "").strip()
                # 默认视为已确认：① 模型不可用时降级 ② 无图时无法判断
                # db_conn=conn：CLIP 样本写入复用本事务连接（WAL 下按条提交，避免长事务锁库）
                img_ok, img_conf = 1, None
                if img:
                    ok, conf = check_image(img, kw, db_conn=conn)
                    if ok is None:
                        ml_skip += 1
                    elif ok:
                        img_ok, img_conf = 1, conf
                        ml_pass += 1
                    else:
                        img_ok, img_conf = 0, conf
                        ml_fail += 1
                else:
                    ml_skip += 1
                cur = conn.execute(
                    "INSERT OR IGNORE INTO parts_quotes "
                    "(keyword,title,price,moq,supplier,location,url,img,img_ok,img_conf,first_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (kw, p.get("fullName") or "", get_price(p), moq, supplier, loc,
                     p.get("jUrl") or "", img, img_ok, img_conf, now))
                if cur.rowcount > 0:
                    new_count += 1
                    # 把刚存的 CLIP 样本关联到这条 parts_quotes（供用户纠正回查）
                    if img and parts_vision and hasattr(parts_vision, "link_last_sample_to_parts"):
                        try:
                            parts_vision.link_last_sample_to_parts(cur.lastrowid, kw, img, conn=conn)
                        except Exception:
                            pass
                # 按条提交：每张图的 CLIP 推理约 100ms，整批一个事务会锁库几十秒，
                # 按条提交后单事务仅毫秒级，看板/纠正请求随时可插入
                conn.commit()
            total = min(len(uniq), save_top)
            cq = conn.execute("SELECT COUNT(*) c FROM parts_quotes WHERE keyword=? AND location LIKE '重庆%'",
                              (kw,)).fetchone()["c"]
            summary.append((kw, True, f"有效{raw_n}条→相关{rel_n}条→价格正常{len(uniq)}条，"
                                      f"AI确认{ml_pass}/否决{ml_fail}/未判定{ml_skip}，入库{total}条，重庆{cq}条"))
        except Exception as e:
            summary.append((kw, False, f"采集失败: {str(e)[:80]}"))
    conn.commit(); conn.close()
    return summary


# ==============================================================================
# 五、利润测算引擎（模型来自方案书表6）
# ==============================================================================

def calc_profit(p=CALC_DEFAULTS, usd_cny=7.2):
    """
    利润测算（多商品合并版 + 分品类税负）：输入商品清单与费用参数，输出报价/毛利/税负全套结果。

    参数结构（兼容两种格式）：
      多商品（推荐）:
        {"items": [{"name","unit_price","quantity","unit_weight","category"}, ...],
         "freight_mode": "auto"|"fixed",
         "freight_base": 基础费(元), "freight_per_kg": 每公斤运费(元/kg),   # auto 用
         "freight": 固定海运物流费(元),                                     # fixed 用
         "agent_fee","misc_fee","markup","deposit_ratio","ghana_tax","equity_a"}
      单商品（旧格式兼容）:
        {"unit_price","quantity","freight", ...}

    模型：总成本 = Σ商品采购成本 + 海运物流费 + 代理费 + 杂费
          报价   = 总成本 × (1 + 加价率)
          毛利   = 报价 − 总成本
    海运物流费：auto 模式 = 基础费 + 每公斤运费 × Σ(单件重量×数量)；fixed 模式直接用固定金额。
    加纳含税到岸：按各商品品类 CET关税 + 附加税费(VAT/NHIL/GETFund等) 分别计算后汇总。
    """
    # —— 商品清单：多商品 items > 旧格式单商品 > 默认示例 ——
    items = p.get("items") if isinstance(p.get("items"), list) and p.get("items") else None
    if items is None:
        if p.get("unit_price") is not None and p.get("quantity") is not None:
            items = [{"name": "商品1", "unit_price": p.get("unit_price", 0),
                      "quantity": p.get("quantity", 0), "unit_weight": p.get("unit_weight", 0) or 0}]
        else:
            items = [dict(CALC_DEFAULT_ITEM)]

    # —— 全局默认加纳税负（未指定品类的商品用此值）——
    global_ghana_tax = float(p.get("ghana_tax") or CALC_DEFAULTS["ghana_tax"])

    detail = []
    total_weight = 0.0
    purchase = 0.0
    ghana_landed_usd = 0.0           # 加纳含税到岸成本（美元）—— 按品类分别计算
    for it in items:
        up = float(it.get("unit_price") or 0)
        qty = float(it.get("quantity") or 0)
        uw = float(it.get("unit_weight") or 0)
        cat_key = str(it.get("category") or "").strip()
        buy = up * qty                       # 该商品采购成本
        w = uw * qty                         # 该商品总重量（kg）
        total_weight += w
        purchase += buy
        # —— 加纳端含税到岸成本（按品类税率分别计算）——
        # 单商品FOB报价 = (采购成本 + 运费分摊 + 代理杂费分摊) × (1 + 加价率)
        # CIF到岸美元 ≈ 单商品FOB报价折美元（FOB下运费/保险由买方承担，此处仅算进口税）
        markup_pct = float(p.get("markup") or 0) / 100
        item_cny = buy * (1 + markup_pct)    # 单商品FOB报价（粗略按成本比例分摊）
        item_usd = item_cny / usd_cny
        # 税率优先级：商品自带 tariff_rate > 用品类默认 > 全局 ghana_tax
        item_tariff = float(it.get("tariff_rate") or -1)
        if item_tariff < 0:                 # 商品未指定 → 用品类默认
            item_tax_pct = category_ghana_tax(cat_key) if cat_key else global_ghana_tax
        else:
            item_tax_pct = item_tariff
        item_landed = item_usd * (1 + item_tax_pct / 100)
        ghana_landed_usd += item_landed
        detail.append({
            "name": str(it.get("name") or f"商品{len(detail)+1}"),
            "category": cat_key,
            "category_name": CATEGORY_MAP.get(cat_key, {}).get("name", ""),
            "unit_price": round(up, 2),
            "quantity": qty,
            "unit_weight": round(uw, 3),
            "purchase": round(buy, 2),
            "weight": round(w, 2),
            "ghana_tax_pct": item_tax_pct,
            "item_landed_usd": round(item_landed, 2),
        })

    # —— 海运物流费：auto 按总重量估算 / fixed 固定金额 ——
    mode = p.get("freight_mode") or ("fixed" if p.get("unit_price") is not None
                                     else CALC_DEFAULTS["freight_mode"])
    if mode == "auto":
        fbase = float(p.get("freight_base") or CALC_DEFAULTS["freight_base"])
        fpkg = float(p.get("freight_per_kg") or CALC_DEFAULTS["freight_per_kg"])
        freight = fbase + fpkg * total_weight
    else:
        fbase, fpkg = 0.0, 0.0
        freight = float(p.get("freight") or CALC_DEFAULTS["freight"])

    total_cost = purchase + freight + float(p.get("agent_fee") or 0) + float(p.get("misc_fee") or 0)
    quote = total_cost * (1 + float(p.get("markup") or 0) / 100)        # 对加纳方报价（元）
    profit = quote - total_cost                                         # 毛利（元）
    quote_usd = quote / usd_cny                                         # 报价折美元
    deposit = quote * float(p.get("deposit_ratio") or 0) / 100          # 定金（先收再采购）
    return {
        "items": detail,
        "count": len(detail),
        "total_weight": round(total_weight, 2),
        "freight_mode": mode,
        "freight": round(freight, 2),
        "freight_base": round(fbase, 2),
        "freight_per_kg": round(fpkg, 4),
        "purchase": round(purchase, 2),
        "total_cost": round(total_cost, 2),
        "quote": round(quote, 2),
        "quote_usd": round(quote_usd, 2),
        "profit": round(profit, 2),
        "margin_pct": round(profit / quote * 100, 2) if quote else 0,
        "deposit": round(deposit, 2),
        "ghana_landed_usd": round(ghana_landed_usd, 2),
        "share_a": round(profit * float(p.get("equity_a") or 0) / 100, 2),         # 重庆合伙人分成
        "share_b": round(profit * (100 - float(p.get("equity_a") or 0)) / 100, 2)  # 深圳合伙人分成
    }


# ==============================================================================
# 六、AI 智能分析（大模型 + 规则引擎降级）
# ==============================================================================

# 业务背景（来自方案书）：作为系统提示词注入，保证 AI 建议不偏离创业方案红线
BUSINESS_BRIEF = """你是对加纳出口贸易的资深顾问，服务对象是两名大学生创业者：
- 模式：按订单采购（Buyer-driven），加纳客户先付30-50%美元定金再采购，零垫资，不压库存
- 品类：摩托车/汽车零配件为主 + 轻工日用品拼柜（首单约1万美元货值，重庆/深圳两地取货，LCL发特马港）
- 四条经营红线（任何建议不得违背）：
  1) 美元计价签约，先收定金再采购
  2) 尾款付清才电放提单放货（提单=货权）
  3) 货款只走对公账户/合规结算账户，严禁个人账户收经营性外汇
  4) 出口走外贸代理或1039市场采购（重庆大足试点），严禁买单出口
- 主要风险：加纳塞地贬值与外汇管制、回款拖延、滞港费（特马港每天50-150美元）"""

RULE_SUGGESTIONS = [
    "汇率方面：坚持美元计价签约；定金到账后立即向供应商下单，不留汇率敞口；回款到账后及时结汇。",
    "回款方面：握紧提单货权，尾款付清才电放；投保中国信保短期出口险（费率约0.5-1%）。",
    "合规方面：货款走外贸代理对公账户或1039平台合规结汇，严禁私人账户收款、严禁买单出口。",
    "节奏方面：出货档期避开两人考试月，单证模板化，提前指定加纳端清关代理，防滞港费。",
]


def _ai_providers():
    """按优先级返回可用的 AI 通道列表：主通道 + fallbacks（跳过未填 Key 的）。"""
    cfg = load_ai_config()
    providers = []
    if str(cfg.get("api_key", "")).strip():
        providers.append(cfg)
    for fb in cfg.get("fallbacks") or []:
        if isinstance(fb, dict) and str(fb.get("api_key", "")).strip():
            merged = dict(cfg)          # 继承 timeout 等公共字段
            merged.update(fb)
            providers.append(merged)
    return providers


def ai_chat(messages):
    """
    调用 OpenAI 兼容的 /chat/completions 接口（多通道自动容灾）。
    messages: [{"role": "system"/"user"/"assistant", "content": "..."}, ...]
    - 主通道失败（超时/网络错误）时按 fallbacks 顺序切换备用通道；
    - 遇限流/过载（HTTP 429/5xx、智谱 1302/1305）在通道内退避重试 2 次；
    - 支持 provider 配置 extra_body（如智谱 {"thinking":{"type":"disabled"}}）；
    - 智谱思考模式吞掉正文时回退取 reasoning_content。
    成功后把实际使用的模型名记入全局 AI_LAST_ENGINE，供日报署名。
    返回模型回复文本；全部通道失败时抛出最后一次的异常。
    """
    global AI_LAST_ENGINE
    providers = _ai_providers()
    if not providers:
        raise RuntimeError("未配置任何可用的 AI API Key")
    last_err = None
    for pv in providers:
        url = pv["base_url"].rstrip("/") + "/chat/completions"
        body = {"model": pv["model"], "messages": messages, "temperature": 0.3}
        body.update(pv.get("extra_body") or {})     # 通道私有参数
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {pv['api_key']}",
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # 兼容部分国内网关证书
        for attempt in range(3):          # 单通道内限流退避重试
            try:
                with urllib.request.urlopen(req, timeout=pv.get("timeout", 60),
                                            context=ctx) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = e
                if attempt < 2 and e.code in (429, 500, 502, 503):
                    time.sleep(8 * (attempt + 1))    # 8s、16s 退避
                    continue
                break                               # 不可重试错误 → 换通道
            except Exception as e:
                last_err = e
                break
            err = data.get("error")
            if err:                                 # 200 但返回错误体（智谱常见）
                last_err = RuntimeError(f'{err.get("code")}: {err.get("message")}')
                if attempt < 2 and str(err.get("code", "")) in ("1302", "1305", "1113"):
                    time.sleep(8 * (attempt + 1))    # 限流/过载 → 退避重试
                    continue
                break
            msg = data["choices"][0]["message"] or {}
            content = str(msg.get("content") or "").strip()
            if not content:                          # 思考模式可能吞掉正文
                content = str(msg.get("reasoning_content") or "").strip()
            if not content:
                last_err = RuntimeError("模型返回空内容")
                break
            AI_LAST_ENGINE = pv["model"]
            return content
    raise last_err


def build_daily_context():
    """汇总当日数据（汇率 + 新增政策），作为 AI 分析的事实依据。"""
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    rates = [dict(r) for r in conn.execute(
        "SELECT * FROM rates ORDER BY date DESC LIMIT 30")][::-1]
    latest = rates[-1] if rates else None
    ghs_change = None
    ghs_vals = [r["usd_ghs"] for r in rates if r.get("usd_ghs") is not None]
    if len(ghs_vals) >= 2:  # 期间塞地兑美元变动（正数=塞地贬值）
        ghs_change = round((ghs_vals[-1] - ghs_vals[0]) / ghs_vals[0] * 100, 2)
    new_policies = [dict(p) for p in conn.execute(
        "SELECT source, title, url FROM policies WHERE first_seen LIKE ? "
        "ORDER BY keyword != '' DESC, id DESC LIMIT 30", (today + "%",))]
    key_policies = [dict(p) for p in conn.execute(
        "SELECT source, title, url, keyword FROM policies WHERE keyword != '' "
        "ORDER BY first_seen DESC LIMIT 20")]
    conn.close()
    return {"date": today, "latest_rate": latest, "ghs_change_pct": ghs_change,
            "new_policies_today": new_policies, "key_policies": key_policies}


def generate_ai_report():
    """
    生成当日分析日报（优先大模型，未配 Key 或调用失败时降级为规则引擎）。
    日报写入 ai_reports 表并返回 (engine, content)。
    """
    ctx = build_daily_context()
    if ai_enabled():
        try:
            prompt = (
                f"以下是贸易监控系统 {ctx['date']} 采集的真实数据（JSON）：\n"
                f"{json.dumps(ctx, ensure_ascii=False, indent=1)}\n\n"
                "请基于以上数据输出三段式日报（Markdown，总长600字内）：\n"
                "## 一、汇率风险研判\n（结合塞地兑美元变动，评估对回款与报价的影响；"
                "数据不足30天时如实说明）\n"
                "## 二、政策要点\n（仅点评与出口/外汇/创业扶持真正相关的条目，"
                "每条注明来源；无相关条目则写'今日无直接相关新政策'）\n"
                "## 三、对首单的可执行建议\n（3条以内，具体到动作；严格守住四条经营红线）"
            )
            content = ai_chat([
                {"role": "system", "content": BUSINESS_BRIEF},
                {"role": "user", "content": prompt},
            ])
            engine = AI_LAST_ENGINE or load_ai_config()["model"]
        except Exception as e:
            content = rule_report(ctx) + f"\n\n> ⚠️ 大模型调用失败（{str(e)[:80]}），已降级为规则引擎。"
            engine = "rule(降级)"
    else:
        content = rule_report(ctx)
        engine = "rule"
    conn = get_db()
    conn.execute("INSERT INTO ai_reports (ts, engine, content) VALUES (?,?,?)",
                 (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), engine, content))
    conn.commit(); conn.close()
    return engine, content


def rule_report(ctx):
    """规则引擎日报：无 API Key 时的内置分析（数据真实、逻辑来自方案书）。"""
    r = ctx["latest_rate"]
    lines = ["## 一、汇率风险研判"]
    if r:
        lines.append(f"- 最新汇率：USD/CNY = {r['usd_cny']:.4f}，"
                     f"USD/GHS = {r['usd_ghs']:.4f}，GHS/CNY = {r['ghs_cny']:.4f}")
    if ctx["ghs_change_pct"] is None:
        lines.append("- 历史数据不足30天，暂无法计算塞地贬值幅度；连续采集后自动补全。")
    else:
        chg = ctx["ghs_change_pct"]
        if chg > 2:
            lines.append(f"- ⚠️ 近30天塞地对美元贬值 {chg}%，贬值偏快：务必美元计价、"
                         "缩短账期、回款到账立即结汇。")
        elif chg > 0:
            lines.append(f"- 近30天塞地对美元贬值 {chg}%，属常态区间：美元计价即可覆盖。")
        else:
            lines.append(f"- 近30天塞地对美元升值 {-chg}%（罕见）：可考虑适度放宽账期。")
    lines.append("\n## 二、政策要点")
    new_list = ctx["new_policies_today"]
    if new_list:
        lines.append(f"- 今日新增政策/要闻 {len(new_list)} 条：")
        for p in new_list[:8]:
            lines.append(f"  - [{p['source']}] {p['title'][:40]}")
    else:
        lines.append("- 今日无新增政策条目。")
    lines.append("\n## 三、对首单的可执行建议")
    for i, s in enumerate(RULE_SUGGESTIONS, 1):
        lines.append(f"{i}. {s}")
    lines.append("\n*（本报告由内置规则引擎生成：填入 ai_config.json 的 API Key 后，"
                 "可升级为大模型深度分析。）*")
    return "\n".join(lines)


def ask_ai(question):
    """
    基于政策库的智能问答：把近期政策条目注入上下文，让 AI 有据回答。
    未配置 Key 时返回提示信息。
    """
    if not ai_enabled():
        return ("尚未配置大模型 API Key。请编辑 ai_config.json 填入 api_key"
                "（支持 DeepSeek / 通义千问 / 智谱 GLM 等 OpenAI 兼容接口）后重试。")
    ctx = build_daily_context()
    knowledge = "\n".join(
        f"- [{p['source']}] {p['title']}（{p['url']}）"
        for p in (ctx["key_policies"] + ctx["new_policies_today"]))[:6000]
    prompt = (f"以下是监控系统近期抓取的政策/新闻条目：\n{knowledge}\n\n"
              f"请基于以上条目并结合你的贸易知识，回答用户问题：{question}\n"
              "要求：涉及具体政策时注明来源；条目中没有的官方细节要提示'以官方最新文件为准'；"
              "建议不得违背四条经营红线。")
    return ai_chat([
        {"role": "system", "content": BUSINESS_BRIEF},
        {"role": "user", "content": prompt},
    ])


# ==============================================================================
# 六·C、合同审核模块（AI 审合同，默认优先 DeepSeek）
# ==============================================================================

CONTRACT_TEMPLATE_EN = """SALES CONTRACT

Contract No.: _____________            Date: _____________

THIS CONTRACT is made and entered into by and between:

SELLER: _________________________________
Address: ____________________________________
Tel: _____________  Email: _____________

BUYER: _________________________________
Address: ____________________________________
Tel: _____________  Email: _____________

The Seller agrees to sell and the Buyer agrees to purchase the following goods
on the terms and conditions set forth below:

ARTICLE 1 - COMMODITY & SPECIFICATION

| # | Description | HS Code | Qty (pcs) | Unit Price (USD) | Amount (USD) |
|---|-------------|---------|-----------|------------------|--------------|
| 1 |             |         |           |                  |              |
| 2 |             |         |           |                  |              |
| Total |        |         |           |                  |              |

ARTICLE 2 - COUNTRY OF ORIGIN & QUALITY
Country of Origin: People's Republic of China.
Quality: as per Seller's specifications and samples approved by Buyer.

ARTICLE 3 - TOTAL VALUE
Total Contract Value: USD ____________ (Say U.S. Dollars ______________ only).
All prices are in USD. This is a firm commitment, not subject to FX revaluation.

ARTICLE 4 - TRADE TERMS
[ ] FOB Chongqing / Shenzhen, China (Incoterms 2020)
[ ] CIF Tema Port, Ghana (Incoterms 2020)

ARTICLE 5 - PACKING & SHIPPING MARKS
Packing: export-standard cartons, suitable for long-distance sea transport.
Shipping Marks: as directed by Buyer, marked on each carton.

ARTICLE 6 - PORT OF LOADING & DISCHARGE
Port of Loading: Chongqing / Shenzhen, China
Port of Discharge: Tema Port, Ghana

ARTICLE 7 - PAYMENT TERMS
Payment shall be made by Telegraphic Transfer (T/T) as follows:
  1) 30% of total contract value as advance payment (deposit)
     within ___ working days after contract signing;
  2) 70% balance payment before shipment (before Bill of Lading date).
     The Seller shall NOT release the cargo / telex-release the B/L
     until 100% payment is received. No cargo release without full
     payment; the B/L is the title of goods.

ARTICLE 8 - COMPLIANCE & CERTIFICATION
The Seller shall provide:
  [ ] Certificate of Conformity (CoC) for Ghana Customs clearance;
  [ ] Commercial Invoice (3 originals);
  [ ] Packing List (3 originals);
  [ ] Bill of Lading (3 originals or telex-release);
  [ ] Certificate of Origin (CO).
CoC cost is borne by: [ ] Seller  [ ] Buyer

ARTICLE 9 - DELIVERY SCHEDULE
Shipment within ___ working days after receipt of advance payment.
Transit time (China to Tema): approximately 35-45 days by sea.

ARTICLE 10 - INSPECTION
Buyer or its representative may inspect the goods at the loading port
before shipment. The inspection cost is borne by the Buyer.

ARTICLE 11 - FORCE MAJEURE
Neither party shall be liable for failure to perform obligations due to
force majeure events (natural disasters, war, strikes, government actions,
epidemics). The affected party shall notify the other within 7 days and
provide supporting documents within 30 days.

ARTICLE 12 - DISPUTE RESOLUTION
All disputes arising from this contract shall be settled through friendly
negotiation. If no settlement is reached within 60 days, the dispute shall
be submitted to arbitration in ____________ (e.g., CIETAC Beijing).
The arbitration award shall be final and binding on both parties.
Applicable law: laws of the People's Republic of China.

ARTICLE 13 - MISCELLANEOUS
1. This contract is made in both Chinese and English, both versions equally
   authentic. In case of discrepancy, the English version prevails.
2. Any amendment must be in writing and signed by both parties.
3. This contract becomes effective upon signing by both parties.

SELLER:                                BUYER:

Authorized Signature:                   Authorized Signature:
Name / Title:                           Name / Title:
Date:                                   Date:
"""

CONTRACT_TEMPLATE_ZH = """销售合同

合同编号：____________            签约日期：____________

本合同由下列买卖双方共同订立：

卖方：_________________________________
地址：___________________________________
电话：____________  邮箱：____________

买方：_________________________________
地址：___________________________________
电话：____________  邮箱：____________

买卖双方同意按下列条款和条件进行交易：

第一条 商品及规格

| 序号 | 品名及规格 | 海关编码 | 数量（件） | 单价（美元） | 金额（美元） |
|------|-----------|---------|-----------|-------------|-------------|
| 1 |           |         |           |             |             |
| 2 |           |         |           |             |             |
| 合计 |          |         |           |             |             |

第二条 原产地及质量
原产地：中华人民共和国。
质量标准：以买方认可的卖方规格及样品为准。

第三条 合同总金额
合同总金额：美元 ____________（大写：______________________ 整）。
本合同所有价格均以美元（USD）计价，为固定价格，不随汇率波动调整。

第四条 贸易术语
[ ] FOB 中国重庆/深圳（国际贸易术语解释通则 Incoterms 2020）
[ ] CIF 加纳特马港（国际贸易术语解释通则 Incoterms 2020）

第五条 包装及唛头
包装：出口标准纸箱包装，适合远洋海运。
唛头：按买方指示刷制于每件外包装箱上。

第六条 装运港及目的港
装运港：中国重庆/深圳
目的港：加纳特马港（Tema Port, Ghana）

第七条 付款条件
货款以电汇（T/T）方式支付：
  1) 合同签订后___个工作日内，支付合同总金额的 30% 作为定金；
  2) 余款 70% 于发货前（提单签发日前）付清。
  卖方在收到 100% 货款之前，不得放货、不得电放提单；
  款未付清一律不放货，提单（B/L）即货权凭证。

第八条 合规与认证
卖方须提供以下单证：
  [ ] 加纳海关清关用符合性证书（COC）
  [ ] 商业发票（3 份正本）
  [ ] 装箱单（3 份正本）
  [ ] 提单（3 份正本或电放）
  [ ] 原产地证（CO）
COC 认证费用承担方：[ ] 卖方  [ ] 买方

第九条 交货期
卖方收到定金后___个工作日内安排发货。
海运航期（中国至特马港）：约 35-45 天。

第十条 检验
买方或其指定代表可在发货前于装运港检验货物，检验费用由买方承担。

第十一条 不可抗力
任何一方因不可抗力事件（自然灾害、战争、罢工、政府行为、疫情等）
不能履行本合同义务的，不承担违约责任。受影响方须在事件发生后 7 日内
通知对方，并在 30 日内提供有关证明文件。

第十二条 争议解决
凡因本合同引起的争议，双方应友好协商解决；自争议提出之日起 60 日内
协商不成的，提交____________仲裁（如：中国国际经济贸易仲裁委员会，
北京）。仲裁裁决是终局的，对双方均有约束力。
适用法律：中华人民共和国法律。

第十三条 其他
1. 本合同以中、英文书就，两种文本具有同等法律效力；如有歧义，以英文版为准。
2. 本合同的任何修改、补充，须经双方书面同意并签字确认。
3. 本合同自双方法定代表人或授权代表签字之日起生效。

卖方（盖章）：                          买方（盖章）：

授权签字：                              授权签字：
姓名/职务：                             姓名/职务：
日期：                                  日期：
"""

# ==============================================================================
# 合同模板库：多类型 × 双语（销售 / 采购 / 独家代理 / 货运代理 / 保密）
# ==============================================================================

CONTRACT_PURCHASE_ZH = """采购合同

合同编号：____________            签订日期：____________
签订地点：____________

需方（买方）：_________________________________
地址：___________________________________
电话：____________  邮箱：____________

供方（卖方）：_________________________________
地址：___________________________________
电话：____________  邮箱：____________

供需双方同意按下列条款和条件进行交易：

第一条 采购标的

| 序号 | 品名及规格 | 型号 | 数量 | 单价（元） | 金额（元） |
|------|-----------|------|------|-----------|-----------|
| 1 |           |      |      |           |           |
| 2 |           |      |      |           |           |
| 合计 |          |      |      |           |           |

第二条 质量标准
符合国家标准 GB/__________ 及双方确认的样品/图纸；供方须随货提供出厂检验报告。

第三条 合同总金额
合同总金额：人民币 ____________ 元（大写：______________________ 整），
含 13% 增值税，供方须开具增值税专用发票。

第四条 交货方式及期限
供方于预付款到账后 ___ 个工作日内，将货物送至需方指定仓库/装运口岸。

第五条 验收
货到 ___ 日内验收：数量、外观异议当场提出；质量异议在 ___ 日内书面提出。

第六条 包装
出口标准纸箱/木箱包装，适合集装箱海运；唛头按需方要求刷制。

第七条 付款条件
1) 签约后 ___ 日内支付 30% 预付款；
2) 验收合格且收到全额增值税专用发票后支付 70% 余款。

第八条 违约责任
逾期交货或逾期付款的，每日按合同总价的 0.5‰ 支付违约金，
累计不超过合同总价的 5%。

第九条 知识产权
按需方图纸/样品生产的货物，其知识产权归需方所有；供方不得向任何第三方
供应相同或类似货物。

第十条 争议解决
协商不成的，提交需方所在地人民法院诉讼解决。
适用法律：中华人民共和国法律。

第十一条 其他
本合同一式两份，双方各执一份，自双方签字盖章之日起生效。
"""

CONTRACT_PURCHASE_EN = """PURCHASE CONTRACT

Contract No.: _____________            Date: _____________
Place of Signing: _____________

PURCHASER (Buyer): _________________________________
Address: ____________________________________
Tel: _____________  Email: _____________

SUPPLIER (Seller): _________________________________
Address: ____________________________________
Tel: _____________  Email: _____________

The Purchaser agrees to purchase and the Supplier agrees to supply the
following goods on the terms and conditions set forth below:

ARTICLE 1 - GOODS

| # | Description | Model | Qty | Unit Price (RMB) | Amount (RMB) |
|---|-------------|-------|-----|------------------|--------------|
| 1 |             |       |     |                  |              |
| 2 |             |       |     |                  |              |
| Total |        |       |     |                  |              |

ARTICLE 2 - QUALITY
The goods shall conform to Chinese National Standard GB/__________ and the
samples / drawings approved by both parties. The Supplier shall provide
factory inspection reports with the goods.

ARTICLE 3 - TOTAL CONTRACT VALUE
Total Contract Value: RMB ____________ (Say ______________ only),
including 13% VAT; the Supplier shall issue a special VAT invoice.

ARTICLE 4 - DELIVERY
The Supplier shall deliver the goods to the warehouse / loading port
designated by the Purchaser within ___ working days after receipt of the
advance payment.

ARTICLE 5 - INSPECTION & ACCEPTANCE
The Purchaser shall inspect the goods within ___ days after delivery:
objections to quantity or packing raised upon delivery; objections to
quality raised in writing within ___ days.

ARTICLE 6 - PACKING
Export-standard cartons / wooden crates suitable for container sea
transport; shipping marks as directed by the Purchaser.

ARTICLE 7 - PAYMENT TERMS
1) 30% advance payment within ___ days after signing;
2) 70% balance after the goods pass acceptance and the Supplier issues
   the full-amount special VAT invoice.

ARTICLE 8 - LIABILITY FOR BREACH
In case of delayed delivery or delayed payment, the defaulting party
shall pay liquidated damages of 0.5%o of the total contract value per
day, capped at 5% of the total contract value.

ARTICLE 9 - INTELLECTUAL PROPERTY
For goods manufactured per the Purchaser's drawings / samples, the
intellectual property belongs to the Purchaser; the Supplier shall not
supply identical or similar goods to any third party.

ARTICLE 10 - DISPUTE RESOLUTION
All disputes shall first be settled through friendly negotiation;
failing that, submitted to the people's court at the place of the
Purchaser. Applicable law: laws of the People's Republic of China.

ARTICLE 11 - MISCELLANEOUS
This contract is made in duplicate, one copy for each party, effective
upon signing and sealing by both parties.
"""

CONTRACT_AGENCY_ZH = """独家经销与代理协议

协议编号：____________            签订日期：____________

甲方（供应商/委托方）：____________________（中国公司）
乙方（代理商/经销方）：____________________（加纳公司）

鉴于甲方拟在加纳市场销售其产品，乙方熟悉当地市场并具备销售能力，
双方经友好协商达成如下协议：

第一条 授权内容
1. 甲方授予乙方在加纳共和国境内【 】独家 /【 】非独家销售代理权，
   产品范围为：____________________________。
2. 授权期限：自 ____ 年 __ 月 __ 日至 ____ 年 __ 月 __ 日（壹年），期满可续签。

第二条 销售任务
乙方年度销售任务：不低于 ______ 美元（或 ______ 台/套）。
连续两个季度未完成任务 60% 的，甲方有权调整授权范围或终止本协议。

第三条 价格与结算
1. 供货价：USD ______（FOB 中国港口 / CIF 加纳特马港，Incoterms 2020）。
2. 结算方式：乙方以电汇（T/T）预付 100% 货款至甲方对公账户后，
   甲方再安排发货；款未付清一律不放货。
3. 乙方当地售价由乙方自行确定，但不得低于甲方建议零售价的 ___ %。

第四条 双方义务
甲方：保证产品质量与供应，提供产品资料、技术支持及必要的培训。
乙方：积极开拓市场，按季度提交销售与库存报告，维护甲方品牌形象；
独家授权期间不得销售竞争产品。

第五条 合资合作（可选）
双方拟在加纳设立合资公司，股比甲方 ____ %、乙方 ____ %，
另行签署合资协议。

第六条 售后与服务
质保期 ___ 个月；质保期内非人为损坏的返修件，由甲方负责维修或更换，
来回运费双方各付一段。

第七条 保密
双方对彼此的商业信息（价格、客户、技术资料）负有保密义务，
协议终止后两年内持续有效。

第八条 违约与终止
一方严重违约的，守约方有权书面通知终止本协议并要求赔偿损失。

第九条 不可抗力
任何一方因不可抗力不能履行义务的，须在事件发生后 7 日内通知对方，
并在 30 日内提供有关证明文件。

第十条 争议解决
协商不成的，提交中国国际经济贸易仲裁委员会（CIETAC）北京仲裁，
仲裁裁决为终局，对双方均有约束力。适用法律：中华人民共和国法律。

第十一条 其他
本协议以中、英文书就，两种文本具有同等效力，如有歧义以英文版为准。
一式两份，双方各执一份，自双方签字之日起生效。
"""

CONTRACT_AGENCY_EN = """EXCLUSIVE DISTRIBUTION & AGENCY AGREEMENT

Agreement No.: _____________            Date: _____________

Party A (Supplier / Principal): ______________ (a Chinese company)
Party B (Agent / Distributor): ______________ (a Ghanaian company)

WHEREAS Party A intends to sell its products in the Ghanaian market, and
Party B is familiar with the local market and capable of distribution,
the parties agree as follows:

ARTICLE 1 - APPOINTMENT
1. Party A appoints Party B as [ ] exclusive / [ ] non-exclusive
   distributor within the Republic of Ghana for the following products:
   ______________________________.
2. Term: from ____ to ____ (one year), renewable upon expiry.

ARTICLE 2 - SALES TARGET
Party B's annual sales target: no less than USD ________ (or ______
units). If the target is below 60% for two consecutive quarters, Party A
may adjust the territory or terminate this Agreement.

ARTICLE 3 - PRICING & SETTLEMENT
1. Supply price: USD ________ (FOB Chinese port / CIF Tema Port,
   Incoterms 2020).
2. Payment: Party B pays 100% of the goods value in advance by T/T to
   Party A's corporate account before shipment. No cargo release before
   full payment.
3. Party B determines local resale prices, which shall not be lower than
   ___ % of Party A's suggested retail price.

ARTICLE 4 - OBLIGATIONS
Party A: guarantee product quality and supply; provide product
materials, technical support and necessary training.
Party B: actively develop the market; submit quarterly sales & inventory
reports; maintain Party A's brand image; during the exclusive term, not
sell competing products.

ARTICLE 5 - JOINT VENTURE (OPTIONAL)
The parties intend to establish a joint venture in Ghana with an equity
structure of Party A ____ % / Party B ____ %, subject to a separate JV
agreement.

ARTICLE 6 - AFTER-SALES & WARRANTY
Warranty period: ___ months. Within the warranty period, Party A repairs
or replaces defective items not damaged by human factors; each party
bears one leg of the freight.

ARTICLE 7 - CONFIDENTIALITY
Each party shall keep confidential the other's commercial information
(prices, customers, technical data) for two years after termination.

ARTICLE 8 - BREACH & TERMINATION
Upon a material breach by either party, the non-defaulting party may
terminate this Agreement by written notice and claim damages.

ARTICLE 9 - FORCE MAJEURE
Neither party shall be liable for failure caused by force majeure; the
affected party shall notify the other within 7 days and provide
supporting documents within 30 days.

ARTICLE 10 - DISPUTE RESOLUTION
Disputes shall first be settled through friendly negotiation; failing
settlement within 60 days, submitted to CIETAC (Beijing) for
arbitration. The award shall be final and binding on both parties.
Applicable law: laws of the People's Republic of China.

ARTICLE 11 - MISCELLANEOUS
This Agreement is made in both Chinese and English, both versions
equally authentic; in case of discrepancy, the English version prevails.
Made in duplicate, one copy for each party, effective upon signing.
"""

CONTRACT_FREIGHT_ZH = """国际货运代理协议

协议编号：____________            签订日期：____________

委托人（甲方）：_________________________________
受托人（乙方，货运代理）：___________________________

第一条 委托事项
甲方委托乙方办理中国至加纳特马港（Tema Port, Ghana）的出口货物运输
代理业务，包括：
  [ ] 订舱与配载（整箱 FCL / 拼箱 LCL）
  [ ] 拖车与内陆运输
  [ ] 报关报检
  [ ] 单证代办（商业发票、装箱单、原产地证 CO、符合性证书 COC）
  [ ] 目的港清关协助与门到门派送

第二条 费用与结算
1. 费用标准见附件《运价表》；运价随市场波动调整的，
   乙方须提前 ___ 个工作日书面通知甲方。
2. 结算方式：【 】预付 /【 】月结（次月 ___ 日前对账并付清）。
3. 乙方收款账户须为对公账户；临时杂费须凭正规发票结算。

第三条 甲方义务
及时提供准确、齐全的托运资料（装箱单、发票、报关单证）；
货物包装须适合海运；按时支付各项费用。

第四条 乙方义务
1. 妥善安排运输，海运段责任期间按提单条款执行；因乙方过失造成
   货损货差的，按《国际货运代理业管理规定》及提单条款赔偿。
2. 及时向甲方通报船期、动态及异常情况；电放提单须凭甲方书面
   放货通知（且甲方确认货款已收讫）方可执行。

第五条 延误与免责
因不可抗力、船公司甩柜、海关查验等非乙方原因造成的延误，乙方免责，
但须及时通知甲方并协助处理。

第六条 协议期限
自 ____ 年 __ 月 __ 日至 ____ 年 __ 月 __ 日；
期满前 30 日双方均无异议的，自动顺延一年。

第七条 争议解决
协商不成的，提交____________（如：中国国际经济贸易仲裁委员会仲裁 /
乙方所在地海事法院）解决。

第八条 其他
本协议一式两份，双方各执一份，自双方签字盖章之日起生效；
附件《运价表》为本协议不可分割的组成部分。
"""

CONTRACT_FREIGHT_EN = """INTERNATIONAL FREIGHT FORWARDING AGREEMENT

Agreement No.: _____________            Date: _____________

Shipper (Party A): _________________________________
Freight Forwarder (Party B): _________________________________

ARTICLE 1 - SCOPE OF SERVICES
Party A entrusts Party B with export freight forwarding from China to
Tema Port, Ghana, covering:
  [ ] Booking & stowage (FCL / LCL)
  [ ] Trucking & inland haulage
  [ ] Customs declaration & inspection
  [ ] Documentation (Commercial Invoice, Packing List, Certificate of
      Origin CO, Certificate of Conformity CoC)
  [ ] Destination clearance assistance & door-to-door delivery

ARTICLE 2 - CHARGES & SETTLEMENT
1. Charges as per the attached Rate Schedule; any market-driven rate
   adjustment shall be notified to Party A ___ working days in advance
   in writing.
2. Settlement: [ ] prepaid / [ ] monthly (reconciled and paid before
   the ___ day of the following month).
3. All payments shall go to Party B's corporate bank account;
   incidental charges shall be settled against official invoices.

ARTICLE 3 - OBLIGATIONS OF PARTY A
Provide accurate and complete shipping documents (packing list,
invoice, customs papers); ensure export-worthy packing; pay charges
on time.

ARTICLE 4 - OBLIGATIONS OF PARTY B
1. Arrange carriage with due care; liability during the ocean leg
   follows the B/L terms. For loss or damage caused by Party B's
   fault, compensation follows the regulations on international
   freight forwarding and the B/L terms.
2. Keep Party A informed of schedules, movements and exceptions.
   Telex release of the B/L may be executed only upon Party A's
   written release instruction (with Party A's confirmation that full
   payment has been received).

ARTICLE 5 - DELAY & EXEMPTION
Party B is not liable for delays caused by force majeure, carrier
rolling, customs inspection or other causes beyond its control, but
shall notify Party A promptly and assist in handling.

ARTICLE 6 - TERM
From ____ to ____; automatically extended for one year if neither
party objects 30 days before expiry.

ARTICLE 7 - DISPUTE RESOLUTION
Disputes shall first be settled through friendly negotiation; failing
that, submitted to ____________ (e.g., CIETAC arbitration / the
maritime court at Party B's domicile).

ARTICLE 8 - MISCELLANEOUS
This Agreement is made in duplicate, one copy for each party,
effective upon signing; the attached Rate Schedule forms an integral
part hereof.
"""

CONTRACT_NDA_ZH = """保密协议（NDA）

协议编号：____________            签订日期：____________

披露方（甲方）：_________________________________
接收方（乙方）：_________________________________

鉴于双方拟就____________________（项目名称）开展合作洽谈，需交换保密
信息，特订立本协议：

第一条 保密信息
指甲方以书面、口头、电子或其他形式披露的下列信息：商业计划、客户
名单、供应商及价格信息、报价与成本数据、技术资料与图纸、合同条款、
本协议内容以及双方正在洽谈这一事实本身。

第二条 保密义务
1. 乙方仅得将保密信息用于评估或履行上述合作目的，不得用于任何
   其他用途。
2. 未经甲方书面同意，乙方不得向任何第三方披露、提供保密信息；
   仅可向因合作目的确有必要知悉的本方员工披露，并确保其遵守
   同等保密义务。

第三条 例外
下列信息不受本协议约束：(1) 披露时已为公众所知悉的；(2) 非因乙方
过错而公开的；(3) 乙方从无保密义务的第三方合法获得的；(4) 乙方
独立开发且有证据证明的。

第四条 返还与销毁
合作未成或甲方要求时，乙方须在 ___ 日内返还或销毁全部保密信息
载体，并书面确认。

第五条 期限
保密义务自知悉之日起 ___ 年内持续有效，不因合作终止而免除。

第六条 违约责任
乙方违反本协议的，须赔偿甲方因此遭受的全部损失（含合理维权费用），
并支付违约金人民币/美元 ____________。

第七条 争议解决
协商不成的，提交____________仲裁 / ____________人民法院解决。
适用法律：中华人民共和国法律。

第八条 其他
本协议一式两份，双方各执一份，自双方签字之日起生效。
"""

CONTRACT_NDA_EN = """NON-DISCLOSURE AGREEMENT (NDA)

Agreement No.: _____________            Date: _____________

Disclosing Party (Party A): _________________________________
Receiving Party (Party B): _________________________________

WHEREAS the parties intend to discuss cooperation on ______________
(the "Project") and need to exchange confidential information, they
agree as follows:

ARTICLE 1 - CONFIDENTIAL INFORMATION
means information disclosed by Party A in written, oral, electronic or
other form, including: business plans, customer lists, supplier and
pricing information, quotations and cost data, technical materials and
drawings, contract terms, the content of this Agreement, and the very
existence of the discussions.

ARTICLE 2 - NON-DISCLOSURE OBLIGATIONS
1. Party B may use the Confidential Information solely to evaluate or
   perform the Project, and for no other purpose.
2. Without Party A's written consent, Party B shall not disclose the
   Confidential Information to any third party, and may disclose it
   only to its own employees who need to know for the Project, ensuring
   that they are bound by equivalent obligations.

ARTICLE 3 - EXCEPTIONS
Information is not protected hereunder if it: (1) is public at the
time of disclosure; (2) becomes public other than through Party B's
fault; (3) is lawfully obtained from a third party free of
confidentiality obligations; (4) is independently developed by Party B
with provable evidence.

ARTICLE 4 - RETURN & DESTRUCTION
If the cooperation fails or upon Party A's request, Party B shall
return or destroy all carriers of the Confidential Information within
___ days and confirm in writing.

ARTICLE 5 - TERM
The confidentiality obligations remain effective for ___ years from
the date of disclosure, and survive termination of the cooperation.

ARTICLE 6 - LIABILITY FOR BREACH
Party B shall compensate Party A for all losses (including reasonable
enforcement costs) and pay liquidated damages of RMB/USD ____________.

ARTICLE 7 - DISPUTE RESOLUTION
Disputes shall first be settled through negotiation; failing that,
submitted to ____________ arbitration / ____________ court.
Applicable law: laws of the People's Republic of China.

ARTICLE 8 - MISCELLANEOUS
This Agreement is made in duplicate, one copy for each party,
effective upon signing.
"""

# 模板注册表：类型 → {zh/en 模板文本, 双语名称}（前端按类型+语言切换）
CONTRACT_TEMPLATE_SETS = {
    "sales": {
        "zh": CONTRACT_TEMPLATE_ZH, "en": CONTRACT_TEMPLATE_EN,
        "zh_label": "销售合同", "en_label": "Sales Contract",
    },
    "purchase": {
        "zh": CONTRACT_PURCHASE_ZH, "en": CONTRACT_PURCHASE_EN,
        "zh_label": "采购合同", "en_label": "Purchase Contract",
    },
    "agency": {
        "zh": CONTRACT_AGENCY_ZH, "en": CONTRACT_AGENCY_EN,
        "zh_label": "独家代理协议", "en_label": "Agency Agreement",
    },
    "freight": {
        "zh": CONTRACT_FREIGHT_ZH, "en": CONTRACT_FREIGHT_EN,
        "zh_label": "货运代理协议", "en_label": "Freight Forwarding Agreement",
    },
    "nda": {
        "zh": CONTRACT_NDA_ZH, "en": CONTRACT_NDA_EN,
        "zh_label": "保密协议", "en_label": "Non-Disclosure Agreement",
    },
}

# 向后兼容旧引用（原双语混排模板已拆分为纯中文/纯英文两版）
CONTRACT_TEMPLATES = {"en": CONTRACT_TEMPLATE_EN, "zh": CONTRACT_TEMPLATE_ZH}
CONTRACT_TEMPLATE = CONTRACT_TEMPLATE_EN


CONTRACT_REVIEW_SYSTEM = """你是一名资深国际贸易合同审核专家，擅长审核中国对非洲（特别是加纳）出口贸易合同。
你的任务是审核用户提供的合同文本，找出风险条款和缺失条款，给出修改建议。

审核标准（四条经营红线，任何条款违背即为高风险）：
1) 必须美元（USD）计价签约，先收定金（≥30%）再采购
2) 尾款付清才电放提单放货（提单=货权，严禁提前电放）
3) 货款只走对公账户/合规结算账户，严禁个人账户收经营性外汇
4) 出口走外贸代理或1039市场采购，严禁买单出口

其他审核要点：
- 品名、数量、单价是否与实际一致，有无模糊表述
- 交货条款（FOB/CIF）是否明确且与实际一致
- 付款方式是否安全（T/T优先，L/C需注明开证行）
- COC/SONCAP等认证要求是否写明，费用归属是否清晰
- 仲裁/争议解决条款是否有利于中方
- 不可抗力条款是否完整
- 合同双方信息是否完整（名称、地址、联系方式）
- 合同是否有双方签字栏
- 币种是否明确为USD
- 是否存在模糊条款（如"到港后付款"、"验收后付款"等不利于卖方的表述）

输出格式（Markdown）：
## 风险等级：[低/中/高]（扣分X分/满分100）

### 一、经营红线检查
逐条检查四条红线，标注 ✅通过 / ❌违反 / ⚠️缺失，附简要说明。

### 二、逐条审核
对合同每个条款给出点评：✅合理 / ⚠️需修改 / ❌有问题，附修改建议。

### 三、缺失条款
列出合同中应当包含但缺失的条款。

### 四、修改建议汇总
按优先级列出需要修改的条款及具体建议措辞。
"""


def _ai_providers_deepseek_first():
    """按 DeepSeek 优先返回 AI 通道列表（合同审核专用）。
    找 fallbacks 中 base_url 含 deepseek 的通道优先；
    其余通道按原顺序排在后面；跳过未填 Key 的。
    """
    cfg = load_ai_config()
    providers = []
    deepseek_pvs, other_pvs = [], []
    # 主通道
    if str(cfg.get("api_key", "")).strip():
        if "deepseek" in cfg.get("base_url", "").lower():
            deepseek_pvs.append(cfg)
        else:
            other_pvs.append(cfg)
    # fallbacks
    for fb in cfg.get("fallbacks") or []:
        if isinstance(fb, dict) and str(fb.get("api_key", "")).strip():
            merged = dict(cfg)
            merged.update(fb)
            if "deepseek" in (fb.get("base_url") or "").lower():
                deepseek_pvs.append(merged)
            else:
                other_pvs.append(merged)
    # DeepSeek 通道在前，其余在后
    providers = deepseek_pvs + other_pvs
    # 如果没有任何 DeepSeek 通道，回退到默认顺序
    if not providers:
        providers = _ai_providers()
    return providers


def ai_chat_deepseek_first(messages):
    """调用 AI（DeepSeek 优先），多通道容灾。
    与 ai_chat() 逻辑相同，但通道顺序改为 DeepSeek 优先。
    """
    global AI_LAST_ENGINE
    providers = _ai_providers_deepseek_first()
    if not providers:
        raise RuntimeError("未配置任何可用的 AI API Key")
    last_err = None
    for pv in providers:
        url = pv["base_url"].rstrip("/") + "/chat/completions"
        body = {"model": pv["model"], "messages": messages, "temperature": 0.3}
        body.update(pv.get("extra_body") or {})
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {pv['api_key']}",
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=pv.get("timeout", 90),
                                            context=ctx) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = e
                if attempt < 2 and e.code in (429, 500, 502, 503):
                    time.sleep(8 * (attempt + 1))
                    continue
                break
            except Exception as e:
                last_err = e
                break
            err = data.get("error")
            if err:
                last_err = RuntimeError(f'{err.get("code")}: {err.get("message")}')
                if attempt < 2 and str(err.get("code", "")) in ("1302", "1305", "1113"):
                    time.sleep(8 * (attempt + 1))
                    continue
                break
            msg = data["choices"][0]["message"] or {}
            content = str(msg.get("content") or "").strip()
            if not content:
                content = str(msg.get("reasoning_content") or "").strip()
            if not content:
                last_err = RuntimeError("模型返回空内容")
                break
            AI_LAST_ENGINE = pv["model"]
            return content
    raise last_err


def ai_contract_review(contract_text):
    """
    AI 审核合同文本（DeepSeek 优先）。
    contract_text: 用户粘贴的合同全文
    返回 (engine, review_result, risk_level, risk_score)
    """
    if not ai_enabled():
        return ("rule", _rule_contract_review(contract_text), "medium", 50)
    try:
        prompt = (
            "请审核以下合同文本。合同全文如下：\n\n"
            "```\n" + contract_text[:12000] + "\n```\n\n"
            "请按系统提示的输出格式审核，不要遗漏任何条款。"
        )
        result = ai_chat_deepseek_first([
            {"role": "system", "content": CONTRACT_REVIEW_SYSTEM},
            {"role": "user", "content": prompt},
        ])
        engine = AI_LAST_ENGINE or "deepseek-chat"
        risk_level, risk_score = _parse_contract_risk(result)
        return (engine, result, risk_level, risk_score)
    except Exception as e:
        fallback = _rule_contract_review(contract_text)
        fallback += f"\n\n> ⚠️ AI 调用失败（{str(e)[:80]}），已降级为规则引擎审核。"
        return ("rule(降级)", fallback, "medium", 50)


def _parse_contract_risk(result):
    """从 AI 审核结果中解析风险等级和扣分。"""
    import re
    risk_level = "medium"
    risk_score = 50
    m = re.search(r"风险等级[：:]\s*[低中高]\D*(\d+)", result)
    if m:
        risk_score = int(m.group(1))
    m2 = re.search(r"风险等级[：:]\s*(低|中|高)", result)
    if m2:
        risk_level = {"低": "low", "中": "medium", "高": "high"}[m2.group(1)]
    # 违反红线自动升级
    if "❌" in result:
        risk_level = "high"
    elif risk_level == "low" and risk_score > 30:
        risk_level = "medium"
    return risk_level, risk_score


def _rule_contract_review(contract_text):
    """规则引擎合同审核（无 API Key 时的内置审核）。"""
    t = contract_text.lower()
    issues = []
    score = 100

    # 经营红线检查
    lines = ["## 风险等级：中（扣分50/满分100）", "", "### 一、经营红线检查"]

    # 红线1：美元计价
    if "usd" in t or "us$" in t or "美元" in t:
        lines.append("1. ✅ 美元计价：合同中包含 USD/美元 字样，符合红线要求。")
    else:
        lines.append("1. ❌ 美元计价：未找到 USD/美元计价条款，必须以美元签约！")
        issues.append("币种未明确为USD")
        score -= 20

    # 红线2：定金
    if "30%" in t or "deposit" in t or "advance" in t or "定金" in t or "预付" in t:
        lines.append("2. ✅ 定金条款：合同中包含定金/预付条款。")
    else:
        lines.append("2. ❌ 定金条款：未找到定金/预付条款（应≥30%），必须先收定金再采购！")
        issues.append("缺失定金条款")
        score -= 20

    # 红线3：尾款/提单
    if "balance" in t or "尾款" in t or "b/l" in t or "提单" in t or "lading" in t:
        lines.append("3. ✅ 尾款/提单：合同中包含尾款或提单相关条款。")
    else:
        lines.append('3. ⚠️ 尾款/提单条款：未找到明确的「尾款付清才放货」条款，建议补充。')
        issues.append("尾款条款不明确")
        score -= 10

    # 红线4：出口方式
    lines.append("4. ⚠️ 出口方式：此条款通常在合同外约定，建议确认走外贸代理或1039平台。")

    # 其他检查
    lines.append("")
    lines.append("### 二、其他要点检查")

    checks = [
        ("trade term", ["fob", "cif", "exw", "贸易术语", "交货条款"], "交货条款"),
        ("port", ["tema", "port", "港", "loading", "discharge"], "装卸港"),
        ("coc", ["coc", "conformity", "符合性", "soncap"], "认证合规"),
        ("arbitration", ["arbitration", "仲裁", "dispute", "争议"], "争议解决"),
        ("force majeure", ["force majeure", "不可抗力"], "不可抗力"),
        ("signature", ["signature", "签字", "authorized"], "签字栏"),
    ]
    for label, kws, name in checks:
        found = any(k in t for k in kws)
        if found:
            lines.append(f"- ✅ {name}：合同中包含相关条款。")
        else:
            lines.append(f"- ⚠️ {name}：未找到相关条款，建议补充。")
            issues.append(f"缺失{name}条款")
            score -= 8

    lines.append("")
    lines.append("### 三、缺失条款")
    if issues:
        for i in issues:
            lines.append(f"- {i}")
    else:
        lines.append("- 未发现明显缺失条款。")

    lines.append("")
    lines.append("### 四、修改建议汇总")
    lines.append("1. 确保合同以 USD 计价，明确定金≥30%。")
    lines.append('2. 明确「尾款100%付清后电放提单」，严禁提前放货。')
    lines.append("3. 补充COC认证条款及费用归属。")
    lines.append("4. 约定争议解决方式（建议CIETAC仲裁）。")
    lines.append("")
    lines.append("*（本报告由内置规则引擎生成：填入 ai_config.json 的 API Key 后，"
                 "可升级为 DeepSeek 大模型深度审核。）*")

    risk = "low" if score >= 80 else "medium" if score >= 50 else "high"
    lines[0] = f"## 风险等级：{'低' if risk == 'low' else '中' if risk == 'medium' else '高'}（扣分{100 - score}/满分100）"
    return "\n".join(lines)


def save_contract_review(title, content, review_result, engine, risk_level, risk_score):
    """保存合同审核记录到 contracts 表。返回新建的 id。"""
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO contracts (title, content, review_result, engine, "
        "risk_level, risk_score, created_at) VALUES (?,?,?,?,?,?,?)",
        (title or "未命名合同", content, review_result, engine, risk_level, risk_score, now))
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def list_contracts():
    """返回合同审核历史列表（倒序，最多100条）。"""
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, title, engine, risk_level, risk_score, created_at "
        "FROM contracts ORDER BY id DESC LIMIT 100")]
    conn.close()
    return rows


def load_contract(cid):
    """加载单条合同审核详情。"""
    conn = get_db()
    r = conn.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
    conn.close()
    return dict(r) if r else None


# ---- 合同 PDF 导出（可选依赖 reportlab，未安装时优雅降级报错提示）----
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Preformatted, Table, TableStyle,
                                    HRFlowable)
    _HAS_REPORTLAB = True
except ImportError:
    _HAS_REPORTLAB = False


# 条款标题识别：中文「第X条」/ 英文「ARTICLE N」
_CONTRACT_HEAD_RE = re.compile(r"^(第[一二三四五六七八九十百零]+条|ARTICLE\s+\d+)")
# 含 CJK（汉字/中文标点/全角符号）的行用宋体，纯西文行用 Helvetica
_CJK_RE = re.compile(r"[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]")


def contract_to_pdf(title, content, lang="zh"):
    """
    把合同文本排版为标准 A4 合同 PDF，返回 bytes。排版规则：
    - 正文首个非空行（如「销售合同」/SALES CONTRACT）作为文档大标题，居中 + 下划粗线；
    - 竖线表格行（| a | b |）渲染为带网格线的规则表格，首行为表头（浅灰底、居中）；
    - 「第X条」/「ARTICLE N」条款标题单独分级（加大字号 + 段前段后间距）；
    - 中文行用 CID 内置字体 STSong-Light（无需字体文件），纯西文行用 Helvetica；
    - 连续空格转为不换行空格，保住签字栏等左右分栏对齐；
    - 页脚居中显示页码。
    依赖 reportlab（可选）：未安装时抛 RuntimeError 并给出安装指引。
    """
    if not _HAS_REPORTLAB:
        raise RuntimeError("PDF 导出需要可选依赖 reportlab，"
                           "请执行: pip install reportlab 后重启服务")

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    st_title = ParagraphStyle("title", fontName="STSong-Light", fontSize=20,
                              leading=28, alignment=TA_CENTER, spaceAfter=8)
    st_h_cn = ParagraphStyle("h_cn", fontName="STSong-Light", fontSize=12,
                             leading=18, spaceBefore=10, spaceAfter=4)
    st_h_en = ParagraphStyle("h_en", fontName="Helvetica", fontSize=12,
                             leading=18, spaceBefore=10, spaceAfter=4)
    st_cn = ParagraphStyle("cn", fontName="STSong-Light", fontSize=10.5,
                           leading=17, spaceAfter=2)
    st_en = ParagraphStyle("en", fontName="Helvetica", fontSize=10.5,
                           leading=16, spaceAfter=2)
    st_ascii = ParagraphStyle("ascii", fontName="Courier", fontSize=9,
                              leading=12.5)
    st_cell = ParagraphStyle("cell", fontName="STSong-Light", fontSize=9.5,
                             leading=13)
    st_cell_h = ParagraphStyle("cell_h", fontName="STSong-Light", fontSize=9.5,
                               leading=13, alignment=TA_CENTER)

    def esc(s):
        s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        # 连续空格（>=2）转不换行空格，保住签字栏对齐排版
        return re.sub(r"  +", lambda m: "&#160;" * len(m.group()), s)

    def body_style(line):
        return st_cn if _CJK_RE.search(line) else st_en

    def head_style(line):
        return st_h_cn if _CJK_RE.search(line) else st_h_en

    def build_table(block):
        """竖线表格行块 → reportlab 规则表格（网格线 + 表头底纹）。"""
        rows = []
        for ln in block:
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if cells and all(c and set(c) <= set("-: ") for c in cells):
                continue  # 分隔行 |---|---|
            rows.append(cells)
        if not rows:
            return None
        ncols = max(len(r) for r in rows)
        data = []
        for ri, r in enumerate(rows):
            r = r + [""] * (ncols - len(r))
            stl = st_cell_h if ri == 0 else st_cell
            data.append([Paragraph(esc(c) or "&#160;", stl) for c in r])
        t = Table(data, hAlign="CENTER", repeatRows=1)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#444444")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#efefef")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    lines = [l.rstrip() for l in (content or "").splitlines()]

    # 文档标题 = 正文首个非空行；正文从其后开始
    doc_title, start = (title or "CONTRACT").strip(), 0
    for i, l in enumerate(lines):
        if l.strip():
            doc_title = l.strip()
            start = i + 1
            break

    story = [Paragraph(esc(doc_title), st_title),
             HRFlowable(width="100%", thickness=1.4,
                        color=colors.HexColor("#222222"), spaceAfter=12)]

    i, n = start, len(lines)
    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            story.append(Spacer(1, 4))
            i += 1
        elif s.startswith("|"):
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            tbl = build_table(block)
            if tbl:
                story.append(tbl)
                story.append(Spacer(1, 6))
            else:
                i += 1
        elif _CONTRACT_HEAD_RE.match(s):
            story.append(Paragraph(esc(s), head_style(s)))
            i += 1
        elif s and set(s) <= set("+-=_ |."):
            # 残留的手工框线/下划线行（旧版模板或外部粘贴）保持等宽对齐
            story.append(Preformatted(line, st_ascii))
            i += 1
        else:
            story.append(Paragraph(esc(s), body_style(s)))
            i += 1

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#999999"))
        canvas.drawCentredString(A4[0] / 2, 8 * mm,
                                 "- %d -  China-Ghana Trade Monitor" % doc.page)
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=20 * mm,
        title=title or "Contract", author="China-Ghana Trade Monitor")
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()

def save_quote(name, items, params, result, note=""):
    """
    保存报价单快照到 quotes 表。
    name: 报价单名称; items: 商品清单; params: 费用参数;
    result: calc_profit() 返回的测算结果; note: 备注
    返回新建的 quote_id。
    """
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = conn.execute(
        "INSERT INTO quotes (name, status, items_json, params_json, "
        "total_cost, quote, quote_usd, profit, margin_pct, markup, usd_cny, note, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, "manual", json.dumps(items, ensure_ascii=False),
         json.dumps(params, ensure_ascii=False),
         result.get("total_cost", 0), result.get("quote", 0), result.get("quote_usd", 0),
         result.get("profit", 0), result.get("margin_pct", 0),
         params.get("markup", 0), result.get("usd_cny", 7.2), note, now))
    quote_id = cur.lastrowid
    conn.commit(); conn.close()
    return quote_id


def list_quotes(limit=50):
    """返回报价单列表（不含商品明细JSON，只含摘要字段）。"""
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, status, total_cost, quote, quote_usd, profit, "
        "margin_pct, markup, usd_cny, note, created_at "
        "FROM quotes ORDER BY id DESC LIMIT ?", (limit,))]
    conn.close()
    return rows


def load_quote(quote_id):
    """加载报价单完整内容（含商品清单和费用参数）。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if not row:
        conn.close()
        return None
    d = dict(row)
    d["items"] = json.loads(d.get("items_json") or "[]")
    d["params"] = json.loads(d.get("params_json") or "{}")
    # 关联的 AI 预测记录
    ai_rows = [dict(r) for r in conn.execute(
        "SELECT id, engine, predicted_markup, predicted_quote, predicted_profit, "
        "manual_markup, manual_quote, diff_pct, reasoning, created_at "
        "FROM ai_predictions WHERE quote_id=? ORDER BY id DESC", (quote_id,))]
    d["ai_predictions"] = ai_rows
    conn.close()
    return d


def save_goods(g):
    """
    保存商品到 goods 表（新增或更新）。
    g: dict with name/sku/category/unit_price/unit_weight/unit_volume/moq/hs_code/tariff_rate/source/note
    返回 goods id。
    """
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    gid = g.get("id")
    fields = {
        "name": g.get("name", ""), "sku": g.get("sku", ""),
        "category": g.get("category", ""), "unit_price": float(g.get("unit_price") or 0),
        "unit_weight": float(g.get("unit_weight") or 0),
        "unit_volume": float(g.get("unit_volume") or 0),
        "moq": int(g.get("moq") or 1), "hs_code": g.get("hs_code", ""),
        "tariff_rate": float(g.get("tariff_rate", -1) or -1),
        "source": g.get("source", "manual"), "note": g.get("note", ""),
    }
    if gid:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE goods SET {set_clause} WHERE id=?",
                     (*fields.values(), gid))
    else:
        cols = ", ".join(list(fields.keys()) + ["created_at"])
        phs = ", ".join(["?"] * (len(fields) + 1))
        conn.execute(f"INSERT INTO goods ({cols}) VALUES ({phs})",
                     (*fields.values(), now))
        gid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit(); conn.close()
    return gid


def ai_quote_predict(items, params, manual_result, usd_cny):
    """
    AI 报价预测：基于商品清单、费用参数、市场上下文，让大模型预测最优加价率/报价。
    与人工报价对比，返回预测结果 + 推理过程。
    未配置 AI Key 时降级为规则引擎（基于品类平均利润率推荐）。
    """
    # 构建市场上下文
    ctx = build_daily_context()
    manual_markup = float(params.get("markup", 0))
    manual_quote = manual_result.get("quote", 0)
    manual_profit = manual_result.get("profit", 0)
    total_cost = manual_result.get("total_cost", 0)

    # 商品品类信息
    item_summary = []
    for it in items:
        cat_key = it.get("category", "")
        cat_name = CATEGORY_MAP.get(cat_key, {}).get("name", "未分类")
        cet = CATEGORY_MAP.get(cat_key, {}).get("cet_duty", "—")
        item_summary.append(
            f"- {it.get('name','')}: 单价¥{it.get('unit_price',0)} × {it.get('quantity',0)}件 "
            f"(重{it.get('unit_weight',0)}kg/件, 品类={cat_name}, CET关税={cet}%)")

    context_block = {
        "商品清单": item_summary,
        "当前汇率": f"USD/CNY={usd_cny:.4f}, USD/GHS={ctx['latest_rate']['usd_ghs'] if ctx['latest_rate'] else 'N/A'}",
        "塞地30日变动": f"{ctx.get('ghs_change_pct','N/A')}%",
        "人工报价": f"加价率={manual_markup}%, 报价=¥{manual_quote:.2f} (${manual_quote/usd_cny:.2f}), "
                    f"毛利=¥{manual_profit:.2f}, 总成本=¥{total_cost:.2f}",
        "品类参考": "摩配/汽配CET=10%, 整车新品=20%, 电器/纺织=20-35%, 二手=0%",
    }

    if ai_enabled():
        try:
            prompt = (
                f"以下是当前贸易报价数据（JSON）：\n{json.dumps(context_block, ensure_ascii=False, indent=1)}\n\n"
                "请基于以上数据，预测对加纳买家的最优报价策略。输出JSON格式：\n"
                '{"predicted_markup": 数值, "reasoning": "Markdown推理过程(200字内)"}\n\n'
                "推理要点：\n"
                "1) 考虑塞地贬值风险对回款的影响\n"
                "2) 不同品类的加纳关税差异（摩配低、成品高）影响买方接受度\n"
                "3) 首单薄利跑量 vs 品牌溢价之间的平衡\n"
                "4) 加价率建议范围15-30%，不得低于总成本（ markup < 5%不现实）\n"
                "5) 严格守住四条经营红线\n\n"
                "只返回JSON，不要其他文字。"
            )
            raw = ai_chat([
                {"role": "system", "content": BUSINESS_BRIEF},
                {"role": "user", "content": prompt},
            ])
            # 尝试解析 JSON（AI可能包裹 markdown code block）
            clean = raw.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*", "", clean)
                clean = re.sub(r"\s*```$", "", clean).strip()
            pred = json.loads(clean)
            predicted_markup = float(pred.get("predicted_markup", manual_markup))
            reasoning = str(pred.get("reasoning", ""))
            engine = AI_LAST_ENGINE or load_ai_config()["model"]
        except Exception as e:
            # 降级规则引擎
            predicted_markup, reasoning = _rule_predict(
                items, manual_markup, total_cost, usd_cny, ctx)
            reasoning += f"\n\n> ⚠️ 大模型调用失败（{str(e)[:60]}），已降级为规则引擎预测。"
            engine = "rule(降级)"
    else:
        predicted_markup, reasoning = _rule_predict(
            items, manual_markup, total_cost, usd_cny, ctx)
        engine = "rule"

    # 基于预测加价率重算报价
    pred_params = dict(params)
    pred_params["markup"] = predicted_markup
    pred_result = calc_profit(pred_params, usd_cny)
    predicted_quote = pred_result["quote"]
    predicted_profit = pred_result["profit"]
    diff_pct = round((predicted_quote - manual_quote) / manual_quote * 100, 2) if manual_quote else 0

    # 存入数据库
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "INSERT INTO ai_predictions (quote_id, engine, predicted_markup, predicted_quote, "
        "predicted_profit, manual_markup, manual_quote, diff_pct, reasoning, "
        "items_json, context_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (None, engine, predicted_markup, predicted_quote, predicted_profit,
         manual_markup, manual_quote, diff_pct, reasoning,
         json.dumps(items, ensure_ascii=False),
         json.dumps(context_block, ensure_ascii=False), now))
    conn.commit(); conn.close()

    return {
        "engine": engine,
        "predicted_markup": round(predicted_markup, 2),
        "predicted_quote": round(predicted_quote, 2),
        "predicted_profit": round(predicted_profit, 2),
        "manual_markup": round(manual_markup, 2),
        "manual_quote": round(manual_quote, 2),
        "diff_pct": diff_pct,
        "reasoning": reasoning,
        "predicted_result": pred_result,
    }


def _rule_predict(items, manual_markup, total_cost, usd_cny, ctx):
    """
    规则引擎报价预测（无 AI Key 时降级）。
    基于品类关税水平、塞地变动率、利润率区间综合建议加价率。
    """
    # 基准加价率：低关税品类可以低加价，高关税品类需要高加价
    avg_cet = 0
    n = 0
    for it in items:
        cat = CATEGORY_MAP.get(it.get("category", ""))
        if cat:
            avg_cet += cat["cet_duty"]
            n += 1
    avg_cet = avg_cet / n if n else 10

    # 基准：CET 10% → markup 18%, CET 20% → markup 22%, CET 35% → markup 28%
    base_markup = 15 + avg_cet * 0.4

    # 塞地贬值偏快 → 适当提高加价率覆盖汇率风险
    ghs_chg = ctx.get("ghs_change_pct") or 0
    if ghs_chg > 2:
        base_markup += 2     # 贬值快+2%
    elif ghs_chg > 0:
        base_markup += 1

    # 约束范围 15-30%
    predicted = max(15, min(30, round(base_markup, 1)))

    reasoning = (
        f"## 规则引擎预测\n\n"
        f"- **品类分析**：平均CET关税 {avg_cet:.0f}%，"
        f"{'低关税品类(摩配/汽配)可薄利跑量' if avg_cet <= 10 else '高关税品类需较高加价覆盖买方税负'}\n"
        f"- **汇率影响**：近30天塞地{'贬值' if ghs_chg > 0 else '升值'} {abs(ghs_chg)}%，"
        f"{'需提高加价率覆盖回款汇率风险' if ghs_chg > 2 else '汇率风险可控'}\n"
        f"- **建议加价率**：{predicted}%（"
        f"{'高于' if predicted > manual_markup else '低于' if predicted < manual_markup else '持平'}"
        f"人工设定的{manual_markup}%）\n"
        f"- **约束**：加价率区间 15-30%，不得低于总成本"
    )
    return predicted, reasoning
# ==============================================================================
# 七、Web 看板
# ==============================================================================

# ==============================================================================
# 前端静态资源（web/ 目录）
# 前端技术栈：Vue3 + Element Plus + ECharts + Iconfont（全部本地化，离线可用）。
#   web/index.html              应用入口
#   web/css/app.css             全局样式（Apple 风浅色主题）
#   web/js/app.js               Vue3 应用（概览/汇率/政策/配件/AI日报/利润测算 六大视图）
#   web/vendor/                 vue.global.prod.js / element-plus / echarts.min.js
#                               / icons(ElementPlusIconsVue) / iconfont(字体图标)
# 页面通过 DashboardHandler 静态托管，数据仍走 /api/* 接口。
# ==============================================================================
WEB_DIR = os.path.join(_BASE_DIR, "web")
WEB_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
}



class DashboardHandler(BaseHTTPRequestHandler):
    """看板 HTTP 服务：/ 页面，/api/summary 数据总览，/api/calc 测算，/api/fetch 手动采集。"""

    def log_message(self, *args):
        pass  # 关闭默认访问日志，保持控制台整洁

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, rel):
        """安全托管 web/ 目录下的静态资源（防路径穿越）。"""
        rel = rel.lstrip("/")
        full = os.path.realpath(os.path.join(WEB_DIR, rel))
        root = os.path.realpath(WEB_DIR)
        if full != root and not full.startswith(root + os.sep):
            self._send(403, {"error": "forbidden"})
            return
        if not os.path.isfile(full):
            self._send(404, {"error": "not found"})
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            self._send(200, f.read(), WEB_MIME.get(ext, "application/octet-stream"))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        # ---- 访问门禁 ----
        # 1) 认证 API 永远公开
        if path.startswith("/api/auth/"):
            return self._auth_get(path)
        # 2) 登录页公开
        if path == "/login.html":
            return self._serve_static("login.html")
        # 3) 其余 API 必须登录（未登录返回 401，前端据此跳登录页）
        if path.startswith("/api/"):
            if not self.current_user():
                return self._send(401, {"error": "unauthorized", "login": True})
        # 4) 首页 / 其它 HTML 入口未登录 → 跳登录页
        if path in ("/", "/index.html") or path.endswith(".html"):
            if not self.current_user():
                return self.redirect("/login.html")
        # 5) 认证通过（或公开静态资源）继续原有路由
        if path == "/" or path.startswith("/index"):
            self._serve_static("index.html")   # Vue3 应用入口（web/index.html）
        elif self.path.startswith("/echarts.min.js"):
            # 本地静态文件提供 ECharts，避免依赖外网 CDN（部分网络环境下 CDN 不可达，
            # 曾导致 echarts.init 抛错、后续测算器初始化全部中断的故障）
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "echarts.min.js"), "rb") as f:
                    self._send(200, f.read(), "application/javascript; charset=utf-8")
            except OSError:
                self._send(404, {"error": "echarts.min.js not found"})
        elif self.path.startswith("/api/summary"):
            self._send(200, build_summary())
        elif self.path.startswith("/api/calc"):
            # GET 兼容旧版单商品测算（?unit_price=..&quantity=..）
            q = {k: float(v[0]) for k, v in
                 __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(
                     self.path.split("?", 1)[1]).items()}
            conn = get_db()
            row = conn.execute("SELECT usd_cny FROM rates ORDER BY date DESC LIMIT 1").fetchone()
            conn.close()
            self._send(200, calc_profit(q, usd_cny=row["usd_cny"] if row else 7.2))
        elif self.path.startswith("/api/goods"):
            # 本地商品库（供测算器导入；后续「本地数据录入」也写入此表）
            conn = get_db()
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM goods ORDER BY id DESC")]
            conn.close()
            self._send(200, {"goods": rows})
        elif self.path.startswith("/api/categories"):
            # 品类主数据（8大品类×关税率/认证要求，来自《项目落地执行手册》§6.1）
            self._send(200, {"categories": CATEGORIES})
        elif self.path.startswith("/api/vision/samples"):
            # 视觉样本库统计（迭代学习用）：?keyword=摩托车链条 可选筛选
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            kw = qs.get("keyword", [None])[0]
            stats = parts_vision.sample_stats(kw) if hasattr(parts_vision, "sample_stats") else {"error": "module outdated"}
            self._send(200, stats)
        elif self.path.startswith("/api/quotes"):
            # 报价单列表
            self._send(200, {"quotes": list_quotes()})
        elif path == "/api/contract/template":
            # 多类型双语模板：?type=sales|purchase|agency|freight|nda & ?lang=zh|en
            # 不带参数返回类型清单 + 默认销售合同双语（template 键向后兼容=英文版）
            # 注意用 path（已剥离查询串）匹配，否则带 ?lang= 的请求会落入下方 contract/{id} 兜底
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            ctype = (q.get("type") or ["sales"])[0]
            lang = (q.get("lang") or [""])[0]
            if ctype not in CONTRACT_TEMPLATE_SETS:
                ctype = "sales"
            tset = CONTRACT_TEMPLATE_SETS[ctype]
            types_list = [{"id": k, "zh": v["zh_label"], "en": v["en_label"]}
                          for k, v in CONTRACT_TEMPLATE_SETS.items()]
            payload = {"type": ctype,
                       "zh": tset["zh"], "en": tset["en"], "template": tset["en"],
                       "zh_label": tset["zh_label"], "en_label": tset["en_label"],
                       "types": types_list}
            if lang in ("zh", "en"):
                payload["lang"] = lang
                payload["template"] = tset[lang]
            self._send(200, payload)
        elif path == "/api/contract/history":
            # 合同审核历史
            self._send(200, {"contracts": list_contracts()})
        elif path.startswith("/api/contract/"):
            # 加载单条合同审核详情 /api/contract/{id}
            try:
                cid = int(path.split("/api/contract/")[1].split("/")[0])
            except (ValueError, IndexError):
                self._send(400, {"error": "invalid contract id"})
                return
            c = load_contract(cid)
            if c:
                self._send(200, c)
            else:
                self._send(404, {"error": "contract not found"})
        elif path.startswith("/api/quote/"):
            # 加载单个报价单详情 /api/quote/{id}
            try:
                qid = int(path.split("/api/quote/")[1].split("/")[0])
            except (ValueError, IndexError):
                self._send(400, {"error": "invalid quote id"})
                return
            q = load_quote(qid)
            if q:
                self._send(200, q)
            else:
                self._send(404, {"error": "quote not found"})
        elif self.path.startswith("/api/parts"):
            # 配件报价列表：?keyword=xxx 可筛选；?confirmed=1 仅返回 AI 图像确认过的商品
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            kw = qs.get("keyword", [None])[0]
            confirmed = qs.get("confirmed", [""])[0] in ("1", "true", "yes")
            conn = get_db()
            where, params = [], []
            if kw:
                where.append("keyword=?"); params.append(kw)
            if confirmed:
                where.append("img_ok=1")
            sql_where = ("WHERE " + " AND ".join(where)) if where else ""
            rows = conn.execute(
                f"SELECT * FROM parts_quotes {sql_where} ORDER BY keyword, price",
                params).fetchall()
            # 同标题去重：同一商品标题只保留最低价一条（多供应商重复铺货很常见）
            seen_titles, quotes = set(), []
            for r in rows:
                t = r["title"]
                if t in seen_titles:
                    continue
                seen_titles.add(t)
                quotes.append(dict(r))
            # 分关键词统计（与当前筛选口径一致）
            stats_sql = (
                f"SELECT keyword, COUNT(*) n, MIN(price) min_price, ROUND(AVG(price),2) avg_price, "
                f"MAX(first_seen) last_seen FROM parts_quotes {sql_where} GROUP BY keyword")
            stats = [dict(r) for r in conn.execute(stats_sql, params)]
            conn.close()
            self._send(200, {"quotes": quotes, "stats": stats, "confirmed": confirmed})
        else:
            # 其余路径按静态资源处理：/css/* /js/* /vendor/* 等（web/ 目录）
            self._serve_static(path)

    def do_POST(self):
        # ---- 访问门禁 ----
        if self.path.startswith("/api/auth/"):
            return self._auth_post(self.path)
        if not self.current_user():
            return self._send(401, {"error": "unauthorized", "login": True})
        if self.path == "/api/fetch":
            rate_ok, _ = fetch_rates()
            results = fetch_policies()
            parts_results = fetch_parts()
            self._send(200, {"rate_ok": rate_ok,
                             "policy_ok": sum(1 for _, ok, _ in results if ok),
                             "policy_total": len(results),
                             "parts_ok": sum(1 for _, ok, _ in parts_results if ok),
                             "parts_total": len(parts_results)})
        elif self.path == "/api/parts/fetch":
            # 抓取配件报价：请求体 {"keyword": "xxx"} 抓单个关键词，空则抓全部默认关键词
            body = self._read_json()
            kw = (body.get("keyword") or "").strip()
            results = fetch_parts([kw] if kw else None)
            kw_, ok, msg = results[0]
            self._send(200, {"ok": ok, "msg": msg, "keyword": kw_,
                             "source": parts_source()})
        elif self.path == "/api/analyze":
            # 生成 AI/规则引擎日报（大模型调用可能耗时较久）
            engine, content = generate_ai_report()
            self._send(200, {"engine": engine, "content": content,
                             "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
        elif self.path == "/api/calc":
            # 多商品合并测算：请求体 {"items":[...], "markup":21, ...}
            body = self._read_json()
            conn = get_db()
            row = conn.execute("SELECT usd_cny FROM rates ORDER BY date DESC LIMIT 1").fetchone()
            conn.close()
            self._send(200, calc_profit(body, usd_cny=row["usd_cny"] if row else 7.2))
        elif self.path == "/api/ask":
            # 基于政策库的智能问答，请求体 {"question": "..."}
            body = self._read_json()
            answer = ask_ai(body.get("question", ""))
            self._send(200, {"answer": answer})
        elif self.path == "/api/goods":
            # 保存商品到 goods 表：请求体 {name, sku, category, unit_price, ...}
            body = self._read_json()
            gid = save_goods(body)
            self._send(200, {"id": gid, "ok": True})
        elif self.path == "/api/quote/save":
            # 保存报价单快照：请求体 {name, items, params, result, note}
            body = self._read_json()
            qid = save_quote(
                body.get("name", f"报价单_{datetime.now().strftime('%m%d%H%M')}"),
                body.get("items", []), body.get("params", {}),
                body.get("result", {}), body.get("note", ""))
            self._send(200, {"id": qid, "ok": True})
        elif self.path == "/api/quote/ai-predict":
            # AI 报价预测对比：请求体 {items, params, result}
            body = self._read_json()
            items = body.get("items", [])
            params = body.get("params", {})
            manual_result = body.get("result", {})
            conn = get_db()
            row = conn.execute("SELECT usd_cny FROM rates ORDER BY date DESC LIMIT 1").fetchone()
            conn.close()
            usd_cny = row["usd_cny"] if row else 7.2
            prediction = ai_quote_predict(items, params, manual_result, usd_cny)
            self._send(200, prediction)
        elif self.path == "/api/vision/review":
            # 用户纠正配件图像校验：请求体 {parts_id, label}
            # label=1 表示"其实是该商品"（翻转为通过），label=0 表示"其实不是该商品"（翻转为否决）
            body = self._read_json()
            parts_id = body.get("parts_id")
            label = body.get("label")
            if parts_id is None or label not in (0, 1):
                self._send(400, {"error": "参数 parts_id 和 label(0/1) 必填"})
                return
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT id, keyword, img, img_ok FROM parts_quotes WHERE id=?",
                    (parts_id,)
                ).fetchone()
                if not row:
                    self._send(404, {"error": f"parts_id={parts_id} 不存在"})
                    return
                old_ok = int(row["img_ok"]) if row["img_ok"] is not None else 1
                new_ok = int(label)
                if old_ok == new_ok:
                    self._send(200, {"ok": True, "msg": "状态未变化，无需更新", "skipped": True})
                    return
                conn.execute("UPDATE parts_quotes SET img_ok=? WHERE id=?", (new_ok, parts_id))
                conn.commit()
            finally:
                conn.close()
            # 记录 user_correct 样本（供后续抓取的 k-NN 覆盖使用）
            msg = "状态已更新，但 CLIP 不可用，未记录样本"
            if parts_vision and hasattr(parts_vision, "record_correction"):
                ok, m = parts_vision.record_correction(parts_id, row["keyword"], row["img"] or "", new_ok)
                msg = m
            self._send(200, {"ok": True, "msg": msg, "old_img_ok": old_ok, "new_img_ok": new_ok})
        elif self.path == "/api/contract/review":
            # AI 审核合同：请求体 {"title": "...", "content": "..."}
            body = self._read_json()
            title = (body.get("title") or "").strip() or "未命名合同"
            content = (body.get("content") or "").strip()
            if not content:
                self._send(400, {"error": "合同内容不能为空"})
                return
            engine, result, risk_level, risk_score = ai_contract_review(content)
            cid = save_contract_review(title, content, result, engine, risk_level, risk_score)
            self._send(200, {"id": cid, "engine": engine, "review": result,
                             "risk_level": risk_level, "risk_score": risk_score,
                             "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
        elif self.path == "/api/contract/pdf":
            # 合同导出 PDF：请求体 {"title": "...", "content": "...", "lang": "zh|en"}
            body = self._read_json()
            title = (body.get("title") or "").strip() or ("销售合同" if body.get("lang") == "zh" else "SALES CONTRACT")
            content = (body.get("content") or "").strip()
            if not content:
                self._send(400, {"error": "合同内容不能为空"})
                return
            try:
                pdf_bytes = contract_to_pdf(title, content, body.get("lang") or "zh")
            except RuntimeError as e:
                self._send(503, {"error": str(e)})
                return
            except Exception as e:
                self._send(500, {"error": "PDF 生成失败: %s" % e})
                return
            # 文件名转 ASCII 安全形式（Content-Disposition 用 RFC 5987 编码中文）
            # 注意 re.ASCII：默认 \w 会放行中文字符，导致响应头 latin-1 编码崩溃
            safe_name = re.sub(r'[^\w\.\- ]', '_', title, flags=re.ASCII)[:60].strip() or "contract"
            self._send(200, pdf_bytes, "application/pdf", headers={
                "Content-Disposition":
                    "attachment; filename=\"%s.pdf\"; filename*=UTF-8''%s.pdf" % (
                        safe_name, urllib.parse.quote(title or "contract"))})
        else:
            self._send(404, {"error": "not found"})

    def _read_json(self):
        """读取 POST 请求的 JSON 请求体。"""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------------- 鉴权辅助 ----------------
    def current_user(self):
        """从 Cookie 解析会话，返回用户字典或 None。"""
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"tm_sid=([0-9a-f]+)", cookie)
        return _session_user(m.group(1) if m else None)

    def redirect(self, loc):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie_header(self, token, max_age=SESSION_TTL_DAYS * 86400):
        return "tm_sid=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (token, max_age)

    def _verify_captcha(self, token):
        """校验人机验证令牌（Cloudflare Turnstile）。

        未配置 site_key/secret 时视为 dev 模式，直接放行（本地联调无需注册）。
        配置后向 Turnstile 服务端校验站点密钥，成功才放行。
        返回 (ok: bool, reason: str)。
        """
        cfg = load_ai_config().get("captcha") or {}
        sk = (cfg.get("site_key") or "").strip()
        secret = (cfg.get("secret") or "").strip()
        if not sk or not secret:
            return True, "dev"
        if not token:
            return False, "missing token"
        try:
            data = urllib.parse.urlencode({
                "secret": secret,
                "response": token,
                "remoteip": self.client_address[0],
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST")
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            ok = bool(resp.get("success"))
            return ok, ("ok" if ok else "failed")
        except Exception as e:
            return False, "verify error: %s" % e

    def _auth_get(self, path):
        if path == "/api/auth/me":
            u = self.current_user()
            if not u:
                return self._send(401, {"error": "unauthorized", "login": True})
            return self._send(200, _user_public(u))
        if path == "/api/auth/captcha-config":
            cfg = load_ai_config().get("captcha") or {}
            return self._send(200, {"provider": cfg.get("provider", "turnstile"),
                                    "site_key": cfg.get("site_key", "")})
        return self._send(404, {"error": "not found"})

    def _auth_post(self, path):
        body = self._read_json()
        if path == "/api/auth/register":
            return self._auth_register(body)
        if path == "/api/auth/login":
            return self._auth_login(body)
        if path == "/api/auth/logout":
            cookie = self.headers.get("Cookie") or ""
            m = re.search(r"tm_sid=([0-9a-f]+)", cookie)
            if m:
                conn = get_db()
                conn.execute("DELETE FROM sessions WHERE token=?", (m.group(1),))
                conn.commit()
                conn.close()
            self._send(200, {"ok": True},
                       headers={"Set-Cookie": self._cookie_header("", 0)})
            return
        if path == "/api/auth/send-code":
            return self._auth_send_code(body)
        if path == "/api/auth/update":
            return self._auth_update(body)
        return self._send(404, {"error": "not found"})

    def _auth_send_code(self, body):
        # 人机验证（防短信/邮件轰炸）：dev 模式（未配置 Turnstile）自动放行
        ok, why = self._verify_captcha(body.get("captcha"))
        if not ok:
            return self._send(400, {"error": "请先完成人机验证", "captcha": True})
        channel = (body.get("channel") or "email").strip().lower()
        target = (body.get("target") or "").strip()
        purpose = (body.get("purpose") or "register").strip()
        if channel not in ("email", "sms"):
            return self._send(400, {"error": "channel 必须是 email 或 sms"})
        if channel == "email" and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", target):
            return self._send(400, {"error": "邮箱格式不正确"})
        if channel == "sms" and not re.match(r"^1\d{10}$", target):
            return self._send(400, {"error": "手机号格式不正确"})
        code = _gen_code()
        _store_code(target, code, purpose)
        ok = _send_code_email(target, code) if channel == "email" else _send_code_sms(target, code)
        # ok=False 表示未配置真实通道 → dev 回显模式，把 code 返回给前端便于联调（生产请删除）
        resp = {"ok": True, "dev": not ok, "channel": channel}
        if not ok:
            resp["code"] = code
        return self._send(200, resp)

    def _auth_register(self, body):
        # 人机验证（防机器人批量注册）：dev 模式自动放行
        ok, why = self._verify_captcha(body.get("captcha"))
        if not ok:
            return self._send(400, {"error": "请先完成人机验证", "captcha": True})
        username = (body.get("username") or "").strip()
        email = (body.get("email") or "").strip()
        phone = (body.get("phone") or "").strip()
        password = body.get("password") or ""
        code = (body.get("code") or "").strip()
        if not username:
            return self._send(400, {"error": "用户名不能为空"})
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return self._send(400, {"error": "邮箱格式不正确"})
        if not re.match(r"^1\d{10}$", phone):
            return self._send(400, {"error": "手机号格式不正确"})
        # 密码复杂度：≥8 位，且同时含字母与数字（密码「至少两级」复杂度要求）
        if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            return self._send(400, {"error": "密码至少 8 位，且同时包含字母和数字"})
        if not _verify_code(email, code, "register") and not _verify_code(phone, code, "register"):
            return self._send(400, {"error": "验证码错误或已过期"})
        account = generate_account()   # 注册成功自动生成 11 位易记账号（用户无需自设）
        conn = get_db()
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            conn.close()
            return self._send(409, {"error": "该邮箱已被注册"})
        if conn.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
            conn.close()
            return self._send(409, {"error": "该手机号已被注册"})
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        blob = pw_encrypt(password)
        cur = conn.execute(
            "INSERT INTO users(account,username,email,phone,pw_blob,pw_plain,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (account, username, email, phone, blob, password, now, now))
        uid = cur.lastrowid
        conn.commit()
        conn.close()
        _push_pw_history(uid, blob, password)   # 注册密码即历史第 1 条
        token = _create_session(uid)
        self._send(200, {"ok": True, "account": account, "user": _user_public({
            "id": uid, "account": account, "username": username, "email": email,
            "phone": phone, "realname_status": 0})},
            headers={"Set-Cookie": self._cookie_header(token)})

    def _auth_login(self, body):
        # 人机验证（防暴力破解）：dev 模式自动放行
        ok, why = self._verify_captcha(body.get("captcha"))
        if not ok:
            return self._send(400, {"error": "请先完成人机验证", "captcha": True})
        account = (body.get("account") or "").strip()
        password = body.get("password") or ""
        conn = get_db()
        u = conn.execute("SELECT * FROM users WHERE account=?", (account,)).fetchone()
        conn.close()
        if not u or not pw_check(u["pw_blob"], password):
            return self._send(401, {"error": "账号或密码错误"})
        token = _create_session(u["id"])
        self._send(200, {"ok": True, "user": _user_public(dict(u))},
                   headers={"Set-Cookie": self._cookie_header(token)})

    def _auth_update(self, body):
        """已登录用户自主更新资料/密码（账号设置）。"""
        u = self.current_user()
        if not u:
            return self._send(401, {"error": "unauthorized", "login": True})
        conn = get_db()
        fields = {}
        if body.get("username"):
            fields["username"] = (body["username"] or "").strip()
        if body.get("email"):
            em = (body["email"] or "").strip()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em):
                conn.close(); return self._send(400, {"error": "邮箱格式不正确"})
            if conn.execute("SELECT 1 FROM users WHERE email=? AND id!=?", (em, u["id"])).fetchone():
                conn.close(); return self._send(409, {"error": "该邮箱已被占用"})
            fields["email"] = em
        if body.get("phone"):
            ph = (body["phone"] or "").strip()
            if not re.match(r"^1\d{10}$", ph):
                conn.close(); return self._send(400, {"error": "手机号格式不正确"})
            if conn.execute("SELECT 1 FROM users WHERE phone=? AND id!=?", (ph, u["id"])).fetchone():
                conn.close(); return self._send(409, {"error": "该手机号已被占用"})
            fields["phone"] = ph
        if body.get("password"):
            pw = body["password"] or ""
            old = body.get("old_password") or ""
            if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
                conn.close(); return self._send(400, {"error": "密码至少 8 位，且同时包含字母和数字"})
            # 验证身份：修改密码必须提供当前密码（与加密密文比对，不使用原始密码校验）
            if not pw_check(u["pw_blob"], old):
                conn.close(); return self._send(401, {"error": "当前密码错误，身份验证失败"})
            # 防复用：新密码不能与『当前 + 最近 5 次』用过的密码重复
            if _pw_recently_used(u["id"], pw):
                conn.close(); return self._send(400, {"error": "新密码不能与前 5 次使用过的密码相同"})
            # 旧密码滚入历史（最近 5 条），随后更新当前密码
            old_plain = u.get("pw_plain") or pw_decrypt(u["pw_blob"]) or ""
            _push_pw_history(u["id"], u["pw_blob"], old_plain, conn)
            fields["pw_blob"] = pw_encrypt(pw)
            fields["pw_plain"] = pw          # 原始密码仅记录备查
        if not fields:
            conn.close(); return self._send(400, {"error": "没有需要更新的内容"})
        fields["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE users SET " + ", ".join("%s=?" % k for k in fields)
                     + " WHERE id=?", list(fields.values()) + [u["id"]])
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        conn.close()
        return self._send(200, {"ok": True, "user": _user_public(dict(row))})


def build_summary():
    """汇总看板所需的全部数据：最新汇率、历史走势、政策列表、测算默认参数。"""
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    rates = [dict(r) for r in conn.execute(
        "SELECT * FROM rates ORDER BY date DESC LIMIT 90")][::-1]
    latest = rates[-1] if rates else None
    # 塞地 30 日变动率：USD/GHS 上升即塞地贬值（历史未回填 GHS 时自动跳过）
    ghs_change = None
    ghs_vals = [r["usd_ghs"] for r in rates if r.get("usd_ghs") is not None]
    if len(ghs_vals) >= 2:
        ghs_change = round((ghs_vals[-1] - ghs_vals[0]) / ghs_vals[0] * 100, 2)
    # 政策列表：取最近 200 条 → 过滤噪音 → 关键词命中的排前面 → 截取 40 条
    all_rows = [dict(p) for p in conn.execute(
        "SELECT * FROM policies ORDER BY first_seen DESC, id DESC LIMIT 200")]
    policies = [dict(p, is_new=(p["first_seen"][:10] == today))
                for p in all_rows if not is_noise(p["title"])]
    # 排序：先按时间倒序，再用稳定排序把"关键词命中"的顶到前面
    policies.sort(key=lambda x: x["first_seen"], reverse=True)
    policies.sort(key=lambda x: x["keyword"] == "")
    policies = policies[:40]
    log = [dict(l) for l in conn.execute(
        "SELECT * FROM fetch_log WHERE ts LIKE ? ORDER BY id DESC", (today + "%",))]
    last_report = conn.execute(
        "SELECT ts, engine, content FROM ai_reports ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return {"latest_rate": latest, "rates": rates, "ghs_change_pct": ghs_change,
            "policies": policies, "fetch_log": log, "calc_defaults": CALC_DEFAULTS,
            "calc_default_item": CALC_DEFAULT_ITEM,
            "categories": CATEGORIES,
            "ai_enabled": ai_enabled(),
            "last_report": dict(last_report) if last_report else None}


def serve(host=None, port=None, open_browser=True):
    """启动 Web 看板：自动检测汇率数据上次停留时间，把缺口补齐到今天再启动。
    host/port 优先用参数，其次环境变量 TM_HOST/TM_PORT，最后 127.0.0.1:8765。
    open_browser=False 时不自动打开浏览器（服务器部署用）。
    """
    h = host or HOST
    p = port or PORT
    try:
        sync_rates_to_today()   # 检测最后数据日期 → 缺口一直补到本日（失败不阻塞启动）
    except Exception as e:
        print(f"[汇率同步] 启动同步失败（不影响看板启动）: {e}")
    print(f"看板已启动：http://{h}:{p}  （{'浏览器将自动打开，' if open_browser else ''}Ctrl+C 退出）")
    server = ThreadingHTTPServer((h, p), DashboardHandler)
    if open_browser:
        webbrowser.open(f"http://{h}:{p}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止。")


# ==============================================================================
# 七、命令行入口
# ==============================================================================

def main():
    # Windows 控制台中文输出兼容
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    if cmd == "fetch":
        run_fetch()
    elif cmd == "serve":
        # 启动看板：python trade_monitor.py serve [--host 0.0.0.0] [--port 8765] [--no-browser]
        # 服务器部署：TM_HOST=0.0.0.0 python trade_monitor.py serve --no-browser
        import argparse
        ap = argparse.ArgumentParser(description="启动 Web 看板")
        ap.add_argument("--host", default=None, help="监听地址，默认环境变量 TM_HOST 或 127.0.0.1")
        ap.add_argument("--port", type=int, default=None, help="监听端口，默认环境变量 TM_PORT 或 8765")
        ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器（服务器部署用）")
        ns = ap.parse_args(sys.argv[2:])
        serve(host=ns.host, port=ns.port, open_browser=not ns.no_browser)
    elif cmd == "calc":
        print("利润测算示例（多商品 + 按重量估算海运，汇率 7.2）：")
        p = dict(CALC_DEFAULTS)
        p["items"] = [dict(CALC_DEFAULT_ITEM),
                      {"name": "汽车雨刮器", "unit_price": 12.5, "quantity": 3000, "unit_weight": 0.4}]
        r = calc_profit(p)
        print("  商品明细:")
        for it in r["items"]:
            print(f"    {it['name']}: {it['quantity']:.0f}件 × ¥{it['unit_price']} "
                  f"= ¥{it['purchase']} ｜ 重量 {it['weight']} kg")
        print(f"  总重量: {r['total_weight']} kg ｜ 海运物流费(auto): ¥{r['freight']}")
        for k, v in [("采购成本", r["purchase"]), ("总成本", r["total_cost"]),
                     ("对加方报价(元)", r["quote"]), ("报价(美元)", r["quote_usd"]),
                     ("毛利", r["profit"]), ("毛利率%", r["margin_pct"]),
                     ("定金", r["deposit"]), ("加纳含税到岸(美元)", r["ghana_landed_usd"]),
                     ("分成51/49", f"{r['share_a']}/{r['share_b']}")]:
            print(f"  {k}: {v}")
    elif cmd == "analyze":
        engine, content = generate_ai_report()
        print(f"\n[分析引擎: {engine}]\n")
        print(content)
    elif cmd in ("history", "rates-history"):
        # 回填历史汇率：python trade_monitor.py history [天数]，默认 30 天
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        ok, msg = fetch_rates_history(days)
        print(f"[历史汇率回填 {days} 天] {'成功' if ok else '失败'}: {msg}")
        conn = get_db()
        rows = list(conn.execute("SELECT * FROM rates ORDER BY date DESC LIMIT 35"))
        print(f"  当前库内共 {conn.execute('SELECT COUNT(*) FROM rates').fetchone()[0]} 天汇率记录")
        for r in reversed(rows):
            ghs = f"{r['usd_ghs']:.4f}" if r["usd_ghs"] is not None else "  --  "
            print(f"  {r['date']}  USD/CNY={r['usd_cny']:.4f}  USD/GHS={ghs}")
        conn.close()
    elif cmd == "parts":
        # 配件报价采集：python trade_monitor.py parts [自定义关键词...]
        kws = sys.argv[2:] or None
        print("=" * 60)
        for kw, ok, msg in fetch_parts(kws):
            print(f"[{kw}] {'成功' if ok else '失败'}: {msg}")
        print("-" * 60)
        conn = get_db()
        for row in conn.execute(
                "SELECT keyword, title, price, moq, supplier, location FROM parts_quotes "
                "ORDER BY keyword, price"):
            print(f"  {row['keyword']} | ¥{row['price']:.2f} | {row['title'][:30]} | "
                  f"{row['supplier'][:16]} | {row['location']}")
        conn.close()
        print("=" * 60)
    elif cmd == "adduser":
        # 后台插入账号（不走注册）：python trade_monitor.py adduser <账号> [--password d12345678] [--email x] [--phone x] [--username x]
        import argparse
        ap = argparse.ArgumentParser(description="后台插入账号（默认密码 d12345678）")
        ap.add_argument("account")
        ap.add_argument("--password", default=DEFAULT_BACKEND_PW)
        ap.add_argument("--email", default="")
        ap.add_argument("--phone", default="")
        ap.add_argument("--username", default="")
        ns = ap.parse_args(sys.argv[2:])
        ok, msg = add_backend_user(ns.account, ns.password, ns.email, ns.phone, ns.username)
        print(("成功: " if ok else "失败: ") + msg)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
