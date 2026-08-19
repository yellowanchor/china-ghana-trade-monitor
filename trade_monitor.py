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

# 数据库文件路径（与脚本同目录）
DB_PATH = "trade_monitor.db"

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
    "unit_price": 29.0,      # 采购单价（元/件）
    "quantity": 2000,        # 数量（件）
    "freight": 9500,         # 国内物流 + LCL 拼柜海运费（元）
    "agent_fee": 1500,       # 报关/外贸代理/单证费（元）
    "misc_fee": 2000,        # 寄样 + 验货 + 杂费（元）
    "markup": 21,            # 加价率（%，按总成本加成）
    "deposit_ratio": 30,     # 加纳方定金比例（%，必须先收定金再采购）
    "ghana_tax": 35,         # 加纳端综合税负（%，ECOWAS CET + VAT15% + NHIL/GETFund 各2.5%）
    "equity_a": 51,          # 重庆合伙人股比（%）
}

# ---------------------------- AI 智能分析配置 ----------------------------------
# 配置文件 ai_config.json（与脚本同目录，首次运行自动生成模板）。
# 兼容所有 OpenAI 风格 /chat/completions 接口：
#   DeepSeek  : base_url=https://api.deepseek.com/v1      model=deepseek-chat
#   通义千问   : base_url=https://dashscope.aliyuncs.com/compatible-mode/v1  model=qwen-plus
#   智谱 GLM  : base_url=https://open.bigmodel.cn/api/paas/v4   model=glm-4-flash
# api_key 留空时，AI 功能自动降级为内置规则引擎（无需任何费用即可使用）。
AI_CONFIG_FILE = "ai_config.json"
AI_CONFIG_TEMPLATE = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",          # ← 填入你的 API Key 后即启用真正的大模型分析
    "model": "deepseek-chat",
    "timeout": 60,
}


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
    利润测算：输入采购与费用参数，输出报价/毛利/税负全套结果。
    模型：总成本 = 采购成本 + 海运物流 + 代理费 + 杂费
          报价   = 总成本 × (1 + 加价率)
          毛利   = 报价 − 总成本
    """
    purchase = p["unit_price"] * p["quantity"]          # 采购成本
    total_cost = purchase + p["freight"] + p["agent_fee"] + p["misc_fee"]
    quote = total_cost * (1 + p["markup"] / 100)        # 对加纳方报价（元）
    profit = quote - total_cost                         # 毛利（元）
    quote_usd = quote / usd_cny                         # 报价折美元
    deposit = quote * p["deposit_ratio"] / 100          # 定金（先收再采购）
    ghana_landed = quote_usd * (1 + p["ghana_tax"] / 100)  # 加纳端含税到岸成本（美元）
    return {
        "purchase": round(purchase, 2),
        "total_cost": round(total_cost, 2),
        "quote": round(quote, 2),
        "quote_usd": round(quote_usd, 2),
        "profit": round(profit, 2),
        "margin_pct": round(profit / quote * 100, 2) if quote else 0,
        "deposit": round(deposit, 2),
        "ghana_landed_usd": round(ghana_landed, 2),
        "share_a": round(profit * p["equity_a"] / 100, 2),        # 重庆合伙人分成
        "share_b": round(profit * (100 - p["equity_a"]) / 100, 2) # 深圳合伙人分成
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


def ai_chat(messages):
    """
    调用 OpenAI 兼容的 /chat/completions 接口。
    messages: [{"role": "system"/"user"/"assistant", "content": "..."}, ...]
    返回模型回复文本；失败抛异常。
    """
    cfg = load_ai_config()
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = json.dumps({"model": cfg["model"], "messages": messages,
                          "temperature": 0.3}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # 兼容部分国内网关证书
    with urllib.request.urlopen(req, timeout=cfg.get("timeout", 60), context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


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
            engine = load_ai_config()["model"]
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

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>中加贸易智能分析看板</title>
<script src="/echarts.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text",
                 "PingFang SC", "Helvetica Neue", "Microsoft YaHei", sans-serif;
    background: #f5f5f7; color: #1d1d1f; font-size: 15px;
    -webkit-font-smoothing: antialiased;
  }
  /* —— 吸顶导航（毛玻璃，仿 apple.com）—— */
  nav {
    position: sticky; top: 0; z-index: 100;
    background: rgba(251, 251, 253, 0.82);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    backdrop-filter: saturate(180%) blur(20px);
    border-bottom: 0.5px solid rgba(0, 0, 0, 0.1);
  }
  nav .nav-inner {
    max-width: 1024px; margin: 0 auto; padding: 0 22px;
    height: 48px; display: flex; align-items: center; justify-content: space-between;
  }
  nav .brand { font-weight: 600; font-size: 15px; letter-spacing: -0.01em; }
  nav .links { display: flex; gap: 26px; }
  nav a { color: #424245; text-decoration: none; font-size: 12px; }
  nav a:hover { color: #000; }
  /* —— 首屏大标题 —— */
  .hero { text-align: center; padding: 72px 22px 12px; }
  .hero .eyebrow { color: #86868b; font-size: 19px; font-weight: 600; margin-bottom: 6px; }
  .hero h1 {
    font-size: 48px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.1;
    background: linear-gradient(94deg, #1d1d1f 30%, #6e6e73);
    -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
  }
  .hero .sub { color: #6e6e73; font-size: 17px; margin-top: 12px; }
  /* —— 居中限宽容器（不占满整屏）—— */
  .container { max-width: 1024px; margin: 0 auto; padding: 12px 22px 40px; }
  /* —— 章节标题（仿苹果分区标题）—— */
  .section-head { padding: 44px 4px 18px; }
  .section-head .kicker { color: #86868b; font-size: 12px; font-weight: 600;
                          text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }
  .section-head h2 { font-size: 30px; font-weight: 700; letter-spacing: -0.015em; }
  .section-head .desc { color: #6e6e73; font-size: 14px; margin-top: 4px; }
  /* —— 卡片 —— */
  .cards { display: flex; gap: 14px; flex-wrap: wrap; }
  .card {
    background: #fff; border-radius: 18px; padding: 22px 24px; min-width: 176px; flex: 1;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04); border: 0.5px solid rgba(0, 0, 0, 0.05);
    transition: transform 0.25s ease, box-shadow 0.25s ease;
  }
  .card:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0, 0, 0, 0.07); }
  .card .label { color: #86868b; font-size: 12px; font-weight: 600; margin-bottom: 8px; }
  .card .value { font-size: 28px; font-weight: 700; letter-spacing: -0.02em;
                 font-variant-numeric: tabular-nums; }
  .card .note { font-size: 12px; color: #86868b; margin-top: 6px; }
  /* —— 面板 —— */
  .panel {
    background: #fff; border-radius: 18px; padding: 28px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04); border: 0.5px solid rgba(0, 0, 0, 0.05);
    margin-bottom: 14px;
  }
  #rateChart { height: 300px; }
  /* —— 表格 —— */
  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  th, td { text-align: left; padding: 11px 12px; border-bottom: 0.5px solid #e8e8ed; }
  th { color: #86868b; font-weight: 600; font-size: 12px; }
  tbody tr { transition: background 0.15s; }
  tbody tr:hover { background: #f5f5f7; }
  /* —— 标签 —— */
  .tag { display: inline-block; background: rgba(0, 113, 227, 0.1); color: #0066cc;
         border-radius: 980px; padding: 2px 10px; font-size: 11px; font-weight: 600; margin-left: 6px; }
  .new { background: rgba(255, 59, 48, 0.1); color: #d70015; }
  .fail { color: #d70015; }
  /* —— 表单 —— */
  .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
  .grid label { display: block; font-size: 12px; color: #86868b; font-weight: 600; margin-bottom: 6px; }
  .grid input {
    width: 100%; padding: 10px 12px; border: 0.5px solid #d2d2d7; border-radius: 10px;
    font-size: 15px; background: #fff; outline: none;
    transition: border 0.2s, box-shadow 0.2s;
  }
  .grid input:focus { border-color: #0071e3; box-shadow: 0 0 0 4px rgba(0, 113, 227, 0.12); }
  .result { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 20px; }
  .result .card { min-width: 150px; }
  .result .value { font-size: 22px; }
  .result .value.green { color: #248a3d; }
  /* —— 胶囊按钮（Apple Blue）—— */
  button {
    background: #0071e3; color: #fff; border: 0; border-radius: 980px;
    padding: 9px 20px; cursor: pointer; font-size: 13px; font-weight: 500;
    transition: background 0.2s, transform 0.1s;
  }
  button:hover { background: #0077ed; }
  button:active { transform: scale(0.97); }
  .btn-sm { padding: 7px 16px; font-size: 12.5px; }
  /* —— AI 输出区 —— */
  .md { background: #f5f5f7; border-radius: 14px; padding: 20px 24px; margin-top: 16px;
        line-height: 1.85; font-size: 14px; }
  .md h3 { font-size: 16px; font-weight: 700; color: #1d1d1f; margin: 14px 0 8px; }
  .md h3:first-child { margin-top: 0; }
  .md li { margin-left: 20px; }
  .md blockquote { border-left: 3px solid #f0b429; padding-left: 12px; color: #6e6e73; margin: 6px 0; }
  .askrow { display: flex; gap: 10px; margin-top: 16px; }
  .askrow input {
    flex: 1; padding: 10px 16px; border: 0.5px solid #d2d2d7; border-radius: 980px;
    font-size: 14px; outline: none; transition: border 0.2s, box-shadow 0.2s;
  }
  .askrow input:focus { border-color: #0071e3; box-shadow: 0 0 0 4px rgba(0, 113, 227, 0.12); }
  .engine-tag { font-size: 12px; color: #86868b; margin-left: 10px; font-weight: 400; }
  /* —— 页脚 —— */
  footer { text-align: center; color: #86868b; font-size: 12px; padding: 28px 22px 40px;
           border-top: 0.5px solid #e8e8ed; margin-top: 28px; }
  footer a { color: #0066cc; text-decoration: none; }
  /* —— 窄屏适配 —— */
  @media (max-width: 768px) {
    .hero { padding: 48px 22px 8px; }
    .hero h1 { font-size: 34px; }
    nav .links { gap: 14px; }
    .grid { grid-template-columns: repeat(2, 1fr); }
    .section-head h2 { font-size: 24px; }
  }
</style>
</head>
<body>
<nav>
  <div class="nav-inner">
    <span class="brand">中加贸易分析</span>
    <div class="links">
      <a href="#overview">概览</a><a href="#rates">汇率</a><a href="#policies">政策</a><a href="#parts">配件报价</a>
      <a href="#ai">AI 分析</a><a href="#calculator">测算器</a>
    </div>
  </div>
</nav>

<header class="hero">
  <div class="eyebrow">重庆 × 深圳 · 对加纳出口贸易</div>
  <h1>贸易智能分析</h1>
  <div class="sub">汇率、政策与利润，尽在一屏之间。</div>
</header>

<div class="container">

<div class="section-head" id="overview">
  <div class="kicker">Overview</div>
  <h2>今日概览</h2>
  <div class="desc">汇率自动采集自 open.er-api.com，政策监控覆盖商务部等 6 个官网。</div>
</div>
<div class="cards" id="rateCards"></div>

<div class="section-head" id="rates">
  <div class="kicker">Exchange Rates</div>
  <h2>汇率走势</h2>
  <div class="desc">USD 兑 CNY / GHS。USD/GHS 上行即塞地贬值，是本模式最大风险变量。</div>
</div>
<div class="panel">
  <div id="rateChart"></div>
</div>

<div class="section-head" id="policies">
  <div class="kicker">Policy Monitor</div>
  <h2>政策监控</h2>
  <div class="desc">重点关键词标蓝，今日新增标红。</div>
</div>
<div class="panel">
  <button class="btn-sm" onclick="runFetch()">立即采集一次</button>
  <span id="fetchMsg" style="margin-left:10px;color:#7a8699"></span>
  <table style="margin-top:12px">
    <thead><tr><th style="width:90px">来源</th><th>标题</th><th style="width:130px">首次发现</th></tr></thead>
    <tbody id="policyBody"></tbody>
  </table>
</div>

<div class="section-head" id="parts">
  <div class="kicker">Parts Quotes</div>
  <h2>配件报价监控</h2>
  <div class="desc">摩配/汽配批发报价（百度爱采购聚合源），已按关键词相关性过滤辅助配件，并通过 CLIP 视觉模型对商品展示图做 AI 校验，重庆发货地高亮，商品标题可点击跳转。</div>
</div>
<div class="panel">
  <div class="askrow" style="margin-top:0">
    <input id="partKeyword" placeholder="输入配件关键词抓取报价，如：摩托车离合器片 / 汽车雨刮器"
           onkeydown="if(event.key==='Enter')fetchParts()">
    <button class="btn-sm" onclick="fetchParts()">抓取该关键词</button>
    <button class="btn-sm" onclick="fetchPartsAll()">抓取全部默认词</button>
  </div>
  <span id="partsMsg" style="color:#7a8699;font-size:13px"></span>
  <div id="partsStats" class="cards" style="margin-top:14px"></div>
  <label style="margin-top:10px;display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#555">
    <input type="checkbox" id="partsConfirmedOnly" checked onchange="loadParts()">
    仅展示 AI 图像确认的商品（CLIP 零样本校验）
  </label>
  <div style="margin-top:10px" id="kwFilter"></div>
  <table style="margin-top:8px">
    <thead><tr><th style="width:64px">图片</th><th style="width:110px">关键词</th><th>商品</th>
      <th style="width:100px">单价
        <a href="javascript:void(0)" id="sortAscBtn" onclick="sortParts('asc')"
           title="按单价升序" style="margin-left:4px;font-size:11px;text-decoration:none">▲</a><a
           href="javascript:void(0)" id="sortDescBtn" onclick="sortParts('desc')"
           title="按单价降序" style="margin-left:2px;font-size:11px;text-decoration:none">▼</a>
      </th>
      <th style="width:80px">起订量</th><th>供应商</th><th style="width:90px">发货地</th></tr></thead>
    <tbody id="partsBody"></tbody>
  </table>
  <div id="partsPager" style="display:flex;align-items:center;gap:12px;margin-top:14px;font-size:13px;color:#86868b">
    <button class="btn-sm" onclick="partsGo(-1)">← 上一页</button>
    <span id="partsPageInfo">—</span>
    <button class="btn-sm" onclick="partsGo(1)">下一页 →</button>
    <span style="margin-left:auto">每页 10 条</span>
  </div>
</div>

<div class="section-head" id="ai">
  <div class="kicker">AI Insights</div>
  <h2>AI 智能分析</h2>
  <div class="desc">基于当日汇率与政策自动生成研判日报，也可直接提问。</div>
</div>
<div class="panel">
  <h2 style="font-size:18px;font-weight:600;margin-bottom:14px">今日分析日报<span id="aiEngine" class="engine-tag"></span></h2>
  <button class="btn-sm" onclick="analyzeNow()">生成今日分析日报</button>
  <span id="analyzeMsg" style="margin-left:10px;color:#7a8699"></span>
  <div class="md" id="aiReport">点击按钮生成：汇率风险研判 + 政策要点 + 首单建议</div>
  <div class="askrow">
    <input id="question" placeholder="向 AI 提问，例如：渝创渝新的申报条件是什么？"
           onkeydown="if(event.key==='Enter')askAI()">
    <button class="btn-sm" onclick="askAI()">提问</button>
  </div>
  <div class="md" id="aiAnswer" style="display:none"></div>
</div>

<div class="section-head" id="calculator">
  <div class="kicker">Profit Calculator</div>
  <h2>首单利润测算器</h2>
  <div class="desc">财务模型来自方案书表 6，修改任意参数即时重算。</div>
</div>
<div class="panel">
  <div class="grid">
    <div><label>采购单价（元/件）</label><input id="unit_price" type="number"></div>
    <div><label>数量（件）</label><input id="quantity" type="number"></div>
    <div><label>海运物流费（元）</label><input id="freight" type="number"></div>
    <div><label>报关代理费（元）</label><input id="agent_fee" type="number"></div>
    <div><label>寄样验货杂费（元）</label><input id="misc_fee" type="number"></div>
    <div><label>加价率（%）</label><input id="markup" type="number"></div>
    <div><label>定金比例（%）</label><input id="deposit_ratio" type="number"></div>
    <div><label>加纳综合税负（%）</label><input id="ghana_tax" type="number"></div>
    <div><label>重庆合伙人股比（%）</label><input id="equity_a" type="number"></div>
  </div>
  <div class="result" id="calcResult">
    <div style="width:100%;padding:18px 0;color:#86868b;text-align:center">正在计算…</div>
  </div>
</div>

</div>

<footer>
  数据来源：open.er-api.com ｜ 各部委 / 人社局官网 ｜ 模型依据《中加纳出口贸易创业方案书》<br>
  经营红线：美元计价 · 尾款付清放货 · 对公收款 · 禁买单出口
</footer>

<script>
let CFG = null;   // 服务端下发的默认参数 + 最新汇率

async function loadAll() {
  const res = await fetch('/api/summary');
  const d = await res.json();
  CFG = d;
  // —— 汇率卡片 ——
  const r = d.latest_rate;
  const cards = [
    ['USD / CNY', r ? r.usd_cny.toFixed(4) : '--', '美元兑人民币'],
    ['USD / GHS', r ? r.usd_ghs.toFixed(4) : '--', '美元兑加纳塞地'],
    ['GHS / CNY', r ? r.ghs_cny.toFixed(4) : '--', '塞地兑人民币（交叉）'],
    ['塞地30日变动', d.ghs_change_pct !== null ? d.ghs_change_pct + '%' : '待积累',
     'USD/GHS 升 = 塞地贬值']
  ];
  document.getElementById('rateCards').innerHTML = cards.map(c =>
    `<div class="card"><div class="label">${c[0]}</div>
     <div class="value">${c[1]}</div><div class="note">${c[2]}</div></div>`).join('');

  // —— 汇率折线图（容错：图表失败不阻断后续测算器等模块初始化）——
  const dates = d.rates.map(x => x.date);
  try {
    const chart = echarts.init(document.getElementById('rateChart'));
    chart.setOption({
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(29,29,31,0.85)', borderWidth: 0,
               textStyle: { color: '#fff', fontSize: 12 } },
    legend: { data: ['USD/CNY', 'USD/GHS'], textStyle: { color: '#86868b' }, top: 4 },
    grid: { left: 50, right: 50, top: 44, bottom: 28 },
    xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: '#d2d2d7' } },
              axisLabel: { color: '#86868b' } },
    yAxis: [
      { type: 'value', name: '兑CNY', nameTextStyle: { color: '#86868b' },
        axisLabel: { color: '#86868b' }, splitLine: { lineStyle: { color: '#e8e8ed' } } },
      { type: 'value', name: '兑GHS', nameTextStyle: { color: '#86868b' },
        axisLabel: { color: '#86868b' }, splitLine: { show: false } }
    ],
    series: [
      { name: 'USD/CNY', type: 'line', smooth: true, data: d.rates.map(x => x.usd_cny),
        symbolSize: 6, itemStyle: { color: '#ff3b30' },
        lineStyle: { width: 2.5 } },
      { name: 'USD/GHS', type: 'line', smooth: true, yAxisIndex: 1,
        symbolSize: 6, data: d.rates.map(x => x.usd_ghs), itemStyle: { color: '#0071e3' },
        lineStyle: { width: 2.5 } }
    ]
  });
  } catch (e) {
    // ECharts 加载失败时给出提示，其余模块（政策/AI/测算器）继续工作
    document.getElementById('rateChart').innerHTML =
      '<div style="padding:40px;color:#86868b;text-align:center">图表组件加载失败，不影响其他功能</div>';
  }

  // —— 政策列表 ——
  document.getElementById('policyBody').innerHTML = d.policies.map(p => `
    <tr><td>${p.source}</td>
    <td><a href="${p.url}" target="_blank" style="text-decoration:none;color:#1d1d1f">${p.title}</a>
      ${p.keyword ? `<span class="tag">${p.keyword.split(',')[0]}</span>` : ''}
      ${p.is_new ? '<span class="tag new">今日新增</span>' : ''}</td>
    <td>${p.first_seen.slice(0, 10)}</td></tr>`).join('')
    || '<tr><td colspan="3">暂无数据，请点击"立即采集一次"</td></tr>';

  // —— 加载最近一次 AI 日报（如有）——
  if (d.last_report) {
    document.getElementById('aiReport').innerHTML = md2html(d.last_report.content);
    document.getElementById('aiEngine').textContent =
      '引擎: ' + d.last_report.engine + ' ｜ ' + d.last_report.ts +
      (d.ai_enabled ? '' : '（未配置API Key，规则引擎模式）');
  } else if (!d.ai_enabled) {
    document.getElementById('aiEngine').textContent = '未配置API Key，当前为规则引擎模式';
  }

  // —— 测算器初始化（独立函数 initCalc，另在启动时单独调用，防被前序渲染错误阻断）——
  initCalc(d);
}

function initCalc(d) {
  for (const k of Object.keys(d.calc_defaults)) {
    const el = document.getElementById(k);
    if (el) { el.value = d.calc_defaults[k]; el.oninput = doCalc; }
  }
  doCalc();
}

function doCalc() {
  const p = {}; let valid = true;
  for (const k of Object.keys(CFG.calc_defaults)) {
    const v = parseFloat(document.getElementById(k).value);
    if (isNaN(v)) valid = false;
    p[k] = v;
  }
  if (!valid) return;
  fetch('/api/calc?' + new URLSearchParams(p)).then(r => r.json()).then(c => {
    const items = [
      ['采购成本', '¥' + c.purchase.toLocaleString()],
      ['总成本', '¥' + c.total_cost.toLocaleString()],
      ['对加方报价', '¥' + c.quote.toLocaleString() + '（$' + c.quote_usd.toLocaleString() + '）'],
      ['毛利', '¥' + c.profit.toLocaleString(), true],
      ['毛利率', c.margin_pct + '%', true],
      ['定金（先收再采购）', '¥' + c.deposit.toLocaleString()],
      ['加纳含税到岸成本', '$' + c.ghana_landed_usd.toLocaleString()],
      ['两人分成 ' + p.equity_a + '/' + (100 - p.equity_a), '¥' + c.share_a.toLocaleString() + ' / ¥' + c.share_b.toLocaleString()]
    ];
    document.getElementById('calcResult').innerHTML = items.map(i =>
      `<div class="card"><div class="label">${i[0]}</div>
       <div class="value ${i[2] ? 'green' : ''}">${i[1]}</div></div>`).join('');
  });
}

async function runFetch() {
  document.getElementById('fetchMsg').textContent = '采集中，请稍候…';
  const res = await fetch('/api/fetch', { method: 'POST' });
  const d = await res.json();
  document.getElementById('fetchMsg').textContent =
    '汇率: ' + (d.rate_ok ? '成功' : '失败') + ' ｜ 政策源成功 ' + d.policy_ok + '/' + d.policy_total
    + ' ｜ 配件词成功 ' + (d.parts_ok !== undefined ? d.parts_ok + '/' + d.parts_total : '-');
  loadAll();
  loadParts();
}

// —— 配件报价监控 ——
let partsKwFilter = null;   // null = 全部
let partsSortDir = 'asc';   // 单价排序方向
let partsData = null;       // 缓存最近一次接口数据，排序时免重新请求
let partsPage = 1;          // 当前页
const PARTS_PAGE_SIZE = 10; // 每页展示条数
function escAttr(s) { return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
async function loadParts() {
  try {
    let params = new URLSearchParams();
    if (partsKwFilter) params.set('keyword', partsKwFilter);
    const confirmed = document.getElementById('partsConfirmedOnly')?.checked ?? true;
    if (confirmed) params.set('confirmed', '1');
    const url = '/api/parts' + (params.toString() ? '?' + params.toString() : '');
    partsData = await (await fetch(url)).json();
    partsPage = 1;          // 数据刷新后回到第一页
    renderParts();
  } catch (e) { console.error('loadParts:', e); }
}
function sortParts(dir) {
  partsSortDir = dir;
  partsPage = 1;
  renderParts();
}
function renderParts() {
  if (!partsData) return;
  const d = partsData;
  // 关键词统计卡片
  document.getElementById('partsStats').innerHTML = d.stats.map(s => `
    <div class="card" style="cursor:pointer" onclick="filterParts(${s.keyword ? '\'' + s.keyword + '\'' : 'null'})">
      <div class="label">${s.keyword}</div>
      <div class="value">¥${Number(s.min_price).toFixed(2)}<span style="font-size:12px;color:#86868b"> 起</span></div>
      <div class="note">均价 ¥${s.avg_price} ｜ ${s.n} 条</div>
    </div>`).join('') || '<div style="color:#86868b;padding:8px 0">暂无报价数据，点击上方按钮抓取</div>';
  // 关键词筛选标签
  document.getElementById('kwFilter').innerHTML = '<span style="font-size:12px;color:#86868b;margin-right:8px">筛选：</span>'
    + '<a href="javascript:void(0)" onclick="filterParts(null)" style="margin-right:8px;'
    + (partsKwFilter ? '' : 'font-weight:700') + '">全部</a>'
    + d.stats.map(s => `<a href="javascript:void(0)" onclick="filterParts('${s.keyword}')" style="margin-right:8px;`
      + (partsKwFilter === s.keyword ? 'font-weight:700' : '') + `">${s.keyword}</a>`).join('');
  // 排序按钮激活态
  const asc = document.getElementById('sortAscBtn'), desc = document.getElementById('sortDescBtn');
  if (asc) asc.style.color = partsSortDir === 'asc' ? '#0071e3' : '#86868b';
  if (asc) asc.style.fontWeight = partsSortDir === 'asc' ? '700' : '400';
  if (desc) desc.style.color = partsSortDir === 'desc' ? '#0071e3' : '#86868b';
  if (desc) desc.style.fontWeight = partsSortDir === 'desc' ? '700' : '400';
  // 报价明细表：按单价升/降序，标题为超链接跳转商品页，重庆发货地高亮，附带商品展示图
  const all = [...(d.quotes || [])].sort((a, b) =>
    partsSortDir === 'asc' ? a.price - b.price : b.price - a.price);
  const total = all.length;
  const pages = Math.max(1, Math.ceil(total / PARTS_PAGE_SIZE));
  if (partsPage > pages) partsPage = pages;
  const start = (partsPage - 1) * PARTS_PAGE_SIZE;
  const pageItems = all.slice(start, start + PARTS_PAGE_SIZE);
  document.getElementById('partsBody').innerHTML = pageItems.map(q => {
    const isCQ = (q.location || '').indexOf('重庆') >= 0;
    const imgHtml = q.img
      ? `<a href="${escAttr(q.url || q.img)}" target="_blank" rel="noopener" title="点击查看商品大图/页面">
           <img src="${escAttr(q.img)}" alt="商品图" loading="lazy"
                style="width:48px;height:48px;object-fit:cover;border-radius:8px;border:0.5px solid #d2d2d7"></a>`
      : '<span style="color:#ccc">—</span>';
    const titleHtml = q.url
      ? `<a href="${escAttr(q.url)}" target="_blank" rel="noopener"
             style="color:#0071e3;text-decoration:none;border-bottom:1px dotted #0071e3"
             title="点击打开商品页面：${escAttr(q.title)}">${q.title}</a>`
      : `<span title="${escAttr(q.title)}">${q.title}</span>`;
    const aiBadge = q.img_ok === 1
      ? `<span class="tag" style="background:#e8f5e9;color:#2e7d32;margin-left:6px" title="AI 图像校验通过，置信度 ${(Number(q.img_conf || 0) * 100).toFixed(1)}%">AI ✓</span>`
      : (q.img_ok === 0
          ? `<span class="tag" style="background:#ffebee;color:#c62828;margin-left:6px" title="AI 图像校验未通过">AI ✗</span>`
          : `<span class="tag" style="background:#f5f5f5;color:#757575;margin-left:6px" title="未进行 AI 图像校验">AI -</span>`);
    return `<tr>
      <td>${imgHtml}</td>
      <td>${q.keyword}</td>
      <td style="max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${titleHtml}${aiBadge}</td>
      <td style="color:${isCQ ? '#0071e3' : '#1d1d1f'};font-weight:600">¥${Number(q.price).toFixed(2)}</td>
      <td>${q.moq || '-'}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escAttr(q.supplier)}">${q.supplier}</td>
      <td>${isCQ ? '<span class="tag">📍 ' + q.location + '</span>' : (q.location || '-')}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="7" style="color:#86868b">暂无数据</td></tr>';
  // 分页信息
  const info = document.getElementById('partsPageInfo');
  if (info) info.textContent = total ? `第 ${partsPage}/${pages} 页 ｜ 共 ${total} 条` : '—';
}
function partsGo(delta) {
  partsPage += delta;
  if (partsPage < 1) partsPage = 1;
  renderParts();
}
function filterParts(kw) { partsKwFilter = kw; loadParts(); }
async function fetchParts() {
  const kw = document.getElementById('partKeyword').value.trim();
  if (!kw) { document.getElementById('partsMsg').textContent = '请先输入关键词'; return; }
  document.getElementById('partsMsg').textContent = '抓取中，约 5–15 秒…';
  try {
    const d = await (await fetch('/api/parts/fetch', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({keyword: kw})
    })).json();
    document.getElementById('partsMsg').textContent = d.ok ? ('✔ ' + d.msg) : ('✘ ' + d.msg);
    partsKwFilter = d.keyword; loadParts();
  } catch (e) { document.getElementById('partsMsg').textContent = '抓取失败: ' + e; }
}
async function fetchPartsAll() {
  document.getElementById('partsMsg').textContent = '抓取全部默认关键词中，约 30–60 秒…';
  try {
    const d = await (await fetch('/api/parts/fetch', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    })).json();
    document.getElementById('partsMsg').textContent = '✔ ' + d.msg;
    loadParts();
  } catch (e) { document.getElementById('partsMsg').textContent = '抓取失败: ' + e; }
}

// —— 简易 Markdown 渲染（标题/列表/引用/加粗）——
function md2html(s) {
  const esc = s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const out = [];
  let inUl = false;
  for (let line of esc.split('\n')) {
    line = line.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
    if (line.startsWith('### ') || line.startsWith('## ')) {
      if (inUl) { out.push('</ul>'); inUl = false; }
      out.push('<h3>' + line.replace(/^#+ /, '') + '</h3>');
    } else if (line.startsWith('- ') || line.startsWith('  - ')) {
      if (!inUl) { out.push('<ul>'); inUl = true; }
      out.push('<li>' + line.replace(/^\s*- /, '') + '</li>');
    } else if (line.startsWith('> ')) {
      if (inUl) { out.push('</ul>'); inUl = false; }
      out.push('<blockquote>' + line.slice(2) + '</blockquote>');
    } else if (/^\d+\.\s/.test(line)) {
      if (inUl) { out.push('</ul>'); inUl = false; }
      out.push('<p>' + line + '</p>');
    } else if (line.trim() === '') {
      if (inUl) { out.push('</ul>'); inUl = false; }
    } else {
      if (inUl) { out.push('</ul>'); inUl = false; }
      out.push('<p>' + line + '</p>');
    }
  }
  if (inUl) out.push('</ul>');
  return out.join('');
}

async function analyzeNow() {
  document.getElementById('analyzeMsg').textContent = '分析生成中，约需数秒…';
  try {
    const res = await fetch('/api/analyze', { method: 'POST' });
    const d = await res.json();
    document.getElementById('aiReport').innerHTML = md2html(d.content);
    document.getElementById('aiEngine').textContent = '引擎: ' + d.engine + ' ｜ ' + d.ts;
    document.getElementById('analyzeMsg').textContent = '';
  } catch (e) {
    document.getElementById('analyzeMsg').textContent = '生成失败: ' + e;
  }
}

async function askAI() {
  const q = document.getElementById('question').value.trim();
  if (!q) return;
  const box = document.getElementById('aiAnswer');
  box.style.display = 'block';
  box.innerHTML = 'AI 思考中…';
  try {
    const res = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q })
    });
    const d = await res.json();
    box.innerHTML = md2html(d.answer);
  } catch (e) {
    box.innerHTML = '提问失败: ' + e;
  }
}

// 主流程：loadAll 内部任何模块出错都不影响测算器（initCalc 独立调用 + 兜底重试）
loadAll().catch(e => console.error('loadAll 部分失败:', e));
loadParts();   // 配件报价独立加载，不受汇率/政策渲染影响
// 等 summary 返回后由 loadAll 调 initCalc；若 loadAll 意外中断，2 秒后兜底用默认参数初始化测算器
setTimeout(() => {
  if (!document.getElementById('calcResult').innerHTML) {
    fetch('/api/summary').then(r => r.json()).then(d => initCalc(d))
      .catch(console.error);
  }
}, 2000);
</script>
</body>
</html>"""


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

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
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
            q = {k: float(v[0]) for k, v in
                 __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(
                     self.path.split("?", 1)[1]).items()}
            conn = get_db()
            row = conn.execute("SELECT usd_cny FROM rates ORDER BY date DESC LIMIT 1").fetchone()
            conn.close()
            self._send(200, calc_profit(q, usd_cny=row["usd_cny"] if row else 7.2))
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
            self._send(404, {"error": "not found"})

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
        print("利润测算示例（默认参数，汇率 7.2）：")
        for k, v in calc_profit().items():
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
