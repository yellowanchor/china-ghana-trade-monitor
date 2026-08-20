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
  python trade_monitor.py analyze   # 生成一份 AI（或规则引擎）日报并打印

数据存储：本目录下 trade_monitor.db（SQLite，首次运行自动创建）
依赖：仅 Python 3.8+ 标准库，无需 pip 安装任何包
================================================================================
"""

import gzip
import json
import os
import re
import ssl
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

# 可选的 CLIP 图像校验模块：依赖缺失时自动降级为纯规则过滤
try:
    from parts_vision import check_image
except Exception as _e:
    check_image = lambda img_url, kw: (None, None)

# ==============================================================================
# 一、全局配置
# ==============================================================================

# 脚本自身所在目录（固定基准，防止进程工作目录漂移导致读写错库/找不到配置）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 数据库文件路径（与脚本同目录，绝对路径）
DB_PATH = os.path.join(_BASE_DIR, "trade_monitor.db")

# 看板服务地址与端口
HOST, PORT = "127.0.0.1", 8765

# 免费汇率 API（无需密钥）：返回各国货币兑 1 USD 的汇率
RATE_API = "https://open.er-api.com/v6/latest/USD"

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
}

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
    # 备用通道（可选）：主通道失败时按顺序自动切换
    "fallbacks": [
        {"base_url": "https://api.deepseek.com/v1",
         "api_key": "",     # ← 填入备用 Key 后启用
         "model": "deepseek-chat"},
    ],
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
# 二、数据库初始化
# ==============================================================================

def get_db():
    """获取数据库连接，并确保所有表已创建（幂等操作，可重复调用）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 让查询结果可以按列名取值
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
            category    TEXT DEFAULT '',            -- 分类（摩配/汽配/日用品…）
            unit_price  REAL DEFAULT 0,             -- 采购单价（元/件）
            unit_weight REAL DEFAULT 0,             -- 单件重量（kg/件）
            moq         INTEGER DEFAULT 1,          -- 最小起订量（件）
            source      TEXT DEFAULT 'manual',      -- 来源：manual人工 / ai文档识别 / photo照片识别 / auto抓取
            note        TEXT DEFAULT '',            -- 备注
            created_at  TEXT                        -- 录入时间
        );
    """)
    # 字段迁移：商品展示图、AI 图像校验结果（兼容旧库）
    for col_sql in (
        "ALTER TABLE parts_quotes ADD COLUMN img TEXT",
        "ALTER TABLE parts_quotes ADD COLUMN img_ok INTEGER DEFAULT 1",
        "ALTER TABLE parts_quotes ADD COLUMN img_conf REAL",
    ):
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
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


def fetch_parts(keywords=None, save_top=30):
    """
    抓取百度爱采购（b2b.baidu.com）批发报价，写入 parts_quotes 表。
    数据源说明：爱采购聚合了 1688/各地供应商的公开批发报价，无需登录、无强反爬；
    重庆本地供应商会出现在结果中（发货地字段可筛选）。1688 官网是 JS 反爬，
    故采用此聚合源；如需 1688 明细，可后续用 Playwright 增强。
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
            # 两轮搜索：全网比价 + "重庆"前缀定向（爱采购 URL 不支持地区参数，用关键词实现）
            # 两次搜索间留 5 秒间隔，避免触发百度限流（403）
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
                img_ok, img_conf = 1, None
                if img:
                    ok, conf = check_image(img, kw)
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
    利润测算（多商品合并版）：输入商品清单与费用参数，输出报价/毛利/税负全套结果。

    参数结构（兼容两种格式）：
      多商品（推荐）:
        {"items": [{"name","unit_price","quantity","unit_weight"}, ...],
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
    """
    # —— 商品清单：多商品 items > 旧格式单商品 > 默认示例 ——
    items = p.get("items") if isinstance(p.get("items"), list) and p.get("items") else None
    if items is None:
        if p.get("unit_price") is not None and p.get("quantity") is not None:
            items = [{"name": "商品1", "unit_price": p.get("unit_price", 0),
                      "quantity": p.get("quantity", 0), "unit_weight": p.get("unit_weight", 0) or 0}]
        else:
            items = [dict(CALC_DEFAULT_ITEM)]

    detail = []
    total_weight = 0.0
    purchase = 0.0
    for it in items:
        up = float(it.get("unit_price") or 0)
        qty = float(it.get("quantity") or 0)
        uw = float(it.get("unit_weight") or 0)
        buy = up * qty                       # 该商品采购成本
        w = uw * qty                         # 该商品总重量（kg）
        total_weight += w
        purchase += buy
        detail.append({
            "name": str(it.get("name") or f"商品{len(detail)+1}"),
            "unit_price": round(up, 2),
            "quantity": qty,
            "unit_weight": round(uw, 3),
            "purchase": round(buy, 2),
            "weight": round(w, 2),
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
    ghana_landed = quote_usd * (1 + float(p.get("ghana_tax") or 0) / 100)  # 加纳端含税到岸成本（美元）
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
        "ghana_landed_usd": round(ghana_landed, 2),
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
    if len(rates) >= 2:  # 期间塞地兑美元变动（正数=塞地贬值）
        ghs_change = round((rates[-1]["usd_ghs"] - rates[0]["usd_ghs"])
                           / rates[0]["usd_ghs"] * 100, 2)
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

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
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
            self._send(200, {"ok": ok, "msg": msg, "keyword": kw_})
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


def build_summary():
    """汇总看板所需的全部数据：最新汇率、历史走势、政策列表、测算默认参数。"""
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    rates = [dict(r) for r in conn.execute(
        "SELECT * FROM rates ORDER BY date DESC LIMIT 90")][::-1]
    latest = rates[-1] if rates else None
    # 塞地 30 日变动率：USD/GHS 上升即塞地贬值
    ghs_change = None
    if len(rates) >= 2:
        old = rates[0]["usd_ghs"]; new = rates[-1]["usd_ghs"]
        ghs_change = round((new - old) / old * 100, 2)
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
            "ai_enabled": ai_enabled(),
            "last_report": dict(last_report) if last_report else None}


def serve():
    """启动 Web 看板：若当日还没有汇率数据，先自动补采一次。"""
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    has_today = conn.execute("SELECT 1 FROM rates WHERE date=?", (today,)).fetchone()
    conn.close()
    if not has_today:
        print("当日尚无汇率数据，先自动采集一次 ...")
        fetch_rates()
    print(f"看板已启动：http://{HOST}:{PORT}  （浏览器将自动打开，Ctrl+C 退出）")
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    webbrowser.open(f"http://{HOST}:{PORT}")
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
        serve()
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
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
