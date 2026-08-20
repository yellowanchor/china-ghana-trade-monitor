# 中加贸易分析工具 · 项目交接文档

> 本文件供**新会话 / 新开发者**快速接手本项目。阅读本文 + 运行一次 `fetch` 与 `serve` 即可全面了解系统。
> 最后更新：2026-08-20

---

## 1. 项目概述

面向"中国 ⇄ 加纳（Ghana）"外贸业务的一体化分析工具，单机 Web 应用：

- **汇率监控**：USD/CNY、USD/GHS 每日采集，历史回填，30 天趋势 + 变化率研判
- **政策监控**：6 个官方源抓取，按外贸关键词自动过滤，标红今日新增
- **配件报价**：百度爱采购批量抓取（四层过滤），可选 CLIP 视觉模型验证商品图
- **AI 日报**：多通道大模型生成每日分析（智谱主通道 + DeepSeek 备用，限流自动容灾）
- **利润测算**：报价 = 总成本 × (1 + 加价率)，支持加纳税负、股比等参数

技术栈：**纯 Python 3.8+ 标准库**（后端）+ **Vue3 / ECharts / Element Plus / iconfont**（前端，全部本地化离线可用）。

---

## 2. 目录结构

```
G:\外贸\贸易分析工具\
├── trade_monitor.py        # 核心单文件应用（后端 + 静态托管，约 2000 行）
├── parts_vision.py         # 配件图片 CLIP 视觉验证（可选，ONNX 量化模型）
├── ai_config.json          # AI 密钥配置（含 rate_api_key，⚠️ 不入库不外传）
├── trade_monitor.db        # SQLite 数据库（首次运行自动创建）
├── models/clip/            # CLIP ONNX 模型文件（.gitignore 排除）
├── web/                    # 前端静态资源（Vue3 全局版，无构建工具）
│   ├── index.html
│   ├── css/app.css
│   ├── js/app.js           # 6 个视图组件 + 根组件（约 900 行）
│   └── vendor/             # 本地化依赖：vue / echarts / element-plus / icons / iconfont
└── PROJECT_HANDOVER.md     # 本文档
```

GitHub 私有仓库（yellowanchor/china-ghana-trade-monitor）仅含源代码：
`trade_monitor.py / parts_vision.py / echarts.min.js / web/ / README.md / ai_config.example.json / requirements-ml.txt`
**`.gitignore` 排除**：`*.db / *.log / models/ / ai_config.json / 测试脚本 / 样本图`。

---

## 3. 快速启动

```bash
cd G:\外贸\贸易分析工具

python trade_monitor.py fetch      # 采集一次（汇率 + 政策 + 配件报价）
python trade_monitor.py serve      # 启动看板 → http://127.0.0.1:8765
python trade_monitor.py calc       # 命令行利润测算示例
python trade_monitor.py analyze    # 生成 AI 分析日报
python trade_monitor.py parts [关键词...]   # 采集配件报价（默认关键词）
python trade_monitor.py history 30 # 回填最近 30 天历史汇率（默认 30，可自定义天数）
```

看板页面：总览 / 汇率趋势 / 政策监控 / 配件报价 / 利润测算 / AI 日报。

---

## 4. 数据库设计（SQLite）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `rates` | date(PK), usd_cny, usd_ghs, ghs_cny | ghs_cny = usd_cny/usd_ghs 交叉汇率；**字段可空**（ECB 兜底时无 GHS） |
| `policies` | title, source, url, keyword, is_new, fetched_at | is_new=1 表示当日新增 |
| `parts` | keyword, title, price, unit, url, img_url, img_ok | img_ok=1 表示通过 CLIP 视觉验证 |
| `fetch_log` | ts, kind, ok, msg | 采集日志（失败排查用） |

---

## 5. 数据源与采集策略

### 汇率（多源降级）
| 优先级 | 数据源 | 说明 |
|---|---|---|
| 每日实时 | open.er-api.com | 免费无 key，全货币，供 `fetch` 用 |
| 历史回填(有key) | **allratestoday.com** | Bearer 认证（key 存 ai_config.json `rate_api_key`），免费 300 次/月、历史免费、160+ 货币含 GHS；`/api/historical-rates?source=USD&target=CNY&period=30d`，单 target 需分两次调用 |
| 历史回填(无key) | frankfurter.app（ECB） | 免费无 key，仅 USD/CNY 工作日，不覆盖已有记录 |

### 政策（6 源）
商务部 / 中国政府网政策库 / 外汇管理局 / 重庆人社局 / 深圳人社局 / 中非合作论坛。
（海关总署官网 JS 反爬，用中国政府网替代。）

### 配件报价
百度爱采购 b2b.baidu.com（1688/慧聪/马可波罗均不可用）；四层过滤：
关键词相关性 → 价格区间 → 中位数带宽 → 同标题去重。
⚠️ 爱采购 403 频率封锁：需退避重试。

### AI 日报（多通道容灾）
主：智谱 glm-4.7-flash（永久免费，200K 上下文，`extra_body` 关闭思考模式）；
备：DeepSeek deepseek-chat。
限流（智谱 1302/1305）通道内退避重试，切备用通道；实际使用引擎记入日报署名。

---

## 6. 配置说明（ai_config.json）

```json
{
  "base_url": "https://open.bigmodel.cn/api/paas/v4",
  "api_key": "智谱key",
  "model": "glm-4.7-flash",
  "timeout": 60,
  "extra_body": { "thinking": { "type": "disabled" } },
  "rate_api_key": "allratestoday.com 的 key",   // 历史汇率回填用
  "fallbacks": [{ "base_url": "...deepseek...", "api_key": "...", "model": "deepseek-chat" }]
}
```

⚠️ **该文件含真实密钥，禁止入库 / 外传 / 截图**。GitHub 只放 `ai_config.example.json`（占位符）。

---

## 7. 前端说明（重点：无构建工具）

- Vue3 以 `vue.global.prod.js` 全局版引入（IIFE），**不经过 npm/webpack/vite 构建**
- 所有依赖本地化到 `web/vendor/`（离线可用）：vue 146KB / echarts 1MB / element-plus 1MB / icons 206KB / iconfont 306KB
- **修改前端 = 直接改 `web/js/app.js` 后刷新页面**（服务端每次请求读盘，无需重启）
- 结构：`index.html` + `css/app.css`（Apple 风浅色，`--accent #0071e3`）+ `js/app.js`
- 踩坑：
  - 模板里访问模块级函数必须显式挂到 `methods`（如 `fmt`/`fmtMoney` → CalculatorView，`md2html` → AiReportView）
  - ECharts 实例必须在 `mounted` 中 `echarts.init`，`beforeUnmount` 时 `dispose`
  - 汇率图 tooltip 为自定义 formatter：显示日期 + USD/CNY + USD/GHS + 交叉汇率，缺失显示 `--`
  - jsdom 无真实 canvas，ECharts init 报 "dpr" 错误是测试环境局限，非代码 bug

---

## 8. 关键约定与踩坑（务必遵守）

1. **所有文件路径必须基于 `_BASE_DIR = os.path.dirname(os.path.abspath(__file__))`**，禁止相对路径——曾因工作目录漂移导致读错库、政策消失、AI 报未配置
2. **Windows 端口清理**：8765 上若残留多个 LISTENING 进程（旧代码在服务），必须显式 `taskkill /F /PID <pid>`（沙箱下需提权）杀掉全部再启动，否则访问到旧代码
3. **CDN 不可达**：前端依赖全部本地化，不要改回 CDN 引用
4. **密钥安全**：ai_config.json 永不入库
5. **历史回填口径**：有 key 时 allratestoday 覆盖为 interbank 中间价（含周末）；无 key 时 ECB 只补缺失日期不覆盖

---

## 9. 已实现功能清单

- [x] 每日汇率/政策/配件自动采集（`fetch`）
- [x] 历史汇率回填 `history`（30 天，CNY+GHS 全量；2026-08-20 已回填 30 天）
- [x] 前端 Vue3 迁移（6 视图 + 图表 tooltip 增强）
- [x] AI 日报多通道容灾（智谱 + DeepSeek）
- [x] 配件图片 CLIP 视觉验证（`parts_vision.py`，模型缺失自动降级纯规则）
- [x] 利润测算（多商品批量）
- [x] 路径漂移三问题修复（汇率不显示/政策消失/AI 未配置）

## 10. 待办事项（用户明确指定）

1. **计算器多元化**：多 SKU 并行测算 + 汇总合计报价/成本/利润
2. **本地数据录入（AI）**：AI 文档读取 + 照片识别，把人工采集数据/采购单录入数据库（对齐主数据表：SKU/价格/重量/体积）
3. **后台联动**：录入数据后台正确读取使用，打通人工 ↔ 自动数据闭环

### 远期候选
- 1688 商品比价爬虫（需 Playwright，反爬强）
- 政策 RAG 问答（复用毕设 A 股 Agent 的 RAG 管道）
- 每日定时自动化采集 + 报告推送
- 云服务器部署（7×24 在线版；数据库选型结论：继续 SQLite，加 Web 客户端时升级 PostgreSQL 免费托管档）

---

## 11. 运维手册

```bash
# 备份数据库（单文件，拷贝即备份）
copy trade_monitor.db trade_monitor_backup_YYYYMMDD.db

# 查看采集日志（排查失败）
sqlite3 trade_monitor.db "SELECT * FROM fetch_log ORDER BY ts DESC LIMIT 20"

# 回填历史汇率
python trade_monitor.py history 90

# 重启看板（先杀旧进程再启动）
netstat -ano | grep 8765        # 找到 PID
taskkill /F /PID <pid>          # 杀干净
python trade_monitor.py serve   # 后台启动
```

## 12. GitHub 同步流程

```bash
# 本地两处副本：G:\外贸\贸易分析工具\（开发）→ G:\外贸\china-ghana-trade-monitor\（仓库副本）
cp trade_monitor.py ../china-ghana-trade-monitor/
cp web/js/app.js  ../china-ghana-trade-monitor/web/js/
cd ../china-ghana-trade-monitor
git add -A && git commit -m "描述" && git push origin main   # 私有仓库，gh 已登录
```

**改动代码后必须同步两处并 push**（用户习惯）。所有文件变更历史见 `git log`。
