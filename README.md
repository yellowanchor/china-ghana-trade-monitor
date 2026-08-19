# 中加纳出口贸易智能分析工具（China-Ghana Trade Monitor）

面向对加纳出口贸易创业场景的单文件智能分析工具：汇率监控、政策监控、配件批发报价采集（含 CLIP 图像 AI 验证）、首单利润测算、Web 看板、AI 日报，一站式覆盖。

## 功能总览

| 模块 | 说明 |
|---|---|
| 汇率监控 | 自动抓取 USD/CNY、USD/GHS（open.er-api.com，免费无密钥），计算塞地兑人民币交叉汇率与贬值幅度 |
| 政策监控 | 监控商务部/中国政府网/外汇局/重庆人社局/深圳人社局/中非合作论坛 6 个官网，按出口/外汇/创业/关税关键词过滤，自动识别新增条目 |
| 配件报价监控 | 爬取百度爱采购（b2b.baidu.com）摩配/汽配批发报价，四层过滤（关键词相关性 + 价格区间 + 中位数带宽 + 同标题去重）+ CLIP 零样本图像分类验证，重庆货源高亮 |
| 利润测算 | 按创业方案书财务模型：总成本×(1+加价率)=报价，含加纳端综合税负、51/49 股比分成测算 |
| Web 看板 | 零依赖浏览器看板：汇率折线图 + 政策列表 + 配件报价分页表格（缩略图/超链接/升降序）+ 交互式利润计算器 |
| AI 日报 | 任意 OpenAI 兼容接口（DeepSeek/通义/GLM）生成"汇率风险/政策要点/首单建议"三段式日报 + 政策库问答；未配置密钥自动降级规则引擎 |

## 快速开始

主程序零依赖，Python 3.8+ 直接运行：

```bash
python trade_monitor.py fetch    # 采集一次数据（汇率 + 政策 + 配件报价）
python trade_monitor.py serve    # 启动 Web 看板（http://127.0.0.1:8765）
python trade_monitor.py calc     # 命令行利润测算示例
python trade_monitor.py analyze  # 生成 AI 分析日报
python trade_monitor.py parts 摩托车链条 汽车刹车片   # 自定义关键词采集配件报价
```

数据存储在运行目录的 `trade_monitor.db`（SQLite，首次运行自动创建）。

## 可选：配件图像 AI 验证（CLIP）

配件抓取默认使用规则过滤；安装 ML 依赖并下载模型后，自动启用 CLIP 零样本图像分类——下载商品展示图，与"目标商品 vs 齿轮/链轮/锁/油瓶/图纸等混淆项"提示词做图文匹配，仅模型确认的商品入库：

```bash
pip install -r requirements-ml.txt

# 下载量化版 CLIP 模型（~150MB）到 models/clip/
mkdir -p models/clip && cd models/clip
curl -LO https://hf-mirror.com/Xenova/clip-vit-base-patch32/resolve/main/tokenizer.json
curl -LO https://hf-mirror.com/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_quantized.onnx
curl -LO https://hf-mirror.com/Xenova/clip-vit-base-patch32/resolve/main/onnx/text_model_quantized.onnx
```

依赖或模型缺失时自动降级为纯规则过滤，不影响主程序运行。

## AI 日报配置

复制 `ai_config.example.json` 为 `ai_config.json`，填入任意 OpenAI 兼容服务的 `base_url` / `api_key` / `model` 即可。

## 项目结构

```
trade_monitor.py        # 主程序（采集 + 存储 + HTTP 服务 + 看板前端）
parts_vision.py         # CLIP 图像验证模块（可选）
echarts.min.js          # 本地化 ECharts（看板图表，避免 CDN 依赖）
ai_config.example.json  # AI 接口配置模板
requirements-ml.txt     # 可选 ML 依赖
```

## 技术说明

- **零依赖架构**：主程序仅 Python 标准库（urllib/sqlite3/http.server/html.parser），任何机器开箱即用；
- **爱采购解析**：SSR 页面 `window.data` JS 变量提取（括号配平 + 字符串感知）+ 递归遍历商品对象；403 限流自动退避重试；
- **四层报价清洗**：关键词核心词必命中 + 辅件后缀/噪音词黑名单 → 1~10000 元价格区间 → 中位数带宽 [P50/5, P50×5] → 同标题去重保留最低价；
- **CLIP 图像验证**：ONNX 量化模型 CPU 推理，正样本与混淆项提示词竞争判别 + 置信度阈值，过滤齿轮/链轮/锁具等图片串扰。

## 免责声明

仅供学习与研究使用。数据源（汇率 API、政府官网、百度爱采购）均为公开页面，请遵守目标站点服务条款与 robots 协议，控制请求频率。
