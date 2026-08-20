# 本地启动指南（Windows）

> 中加贸易分析工具 · 本机运行看板的完整步骤。适用于日常开发与本地使用，与服务器部署（见 `DEPLOY.md`）互不冲突。

---

## 一、前置条件

| 项目 | 要求 | 说明 |
|------|------|------|
| Python | venv 环境（含 reportlab） | `C:\Users\djc\.workbuddy\binaries\python\envs\default\Scripts\python.exe` |
| 工作目录 | `G:\外贸\贸易分析工具` | **必须先 cd 到该目录**（数据库、配置、web 均按脚本所在目录定位） |
| 数据库 | `trade_monitor.db` 已存在 | 首次运行会自动建表，无需手工操作 |
| 配置 | `ai_config.json` 已存在 | 含 AI 密钥；缺失时 AI 日报/合同审核自动降级，看板仍可用 |

核心代码为 Python 3.8+ **纯标准库**，无 reportlab 时合同 PDF 导出自动降级，其余功能不受影响。

---

## 二、启动看板（标准方式）

打开终端（PowerShell 或 Git Bash），执行：

```powershell
cd G:\外贸\贸易分析工具
C:\Users\djc\.workbuddy\binaries\python\envs\default\Scripts\python.exe trade_monitor.py serve
```

行为说明：

1. 若**当日尚无汇率数据**，启动前会自动联网采集一次（需几秒钟）。
2. 启动后自动打开浏览器访问 `http://127.0.0.1:8765`。
3. 看到 `看板已启动：http://127.0.0.1:8765` 即为成功。
4. 停止服务：在终端按 `Ctrl+C`，或直接关闭终端窗口。

## 三、登录

看板已启用登录门禁：

| 账号 | 密码 | 说明 |
|------|------|------|
| `2782699247` | `d12345678` | 预置账号 1 |
| `1241627990` | `d12345678` | 预置账号 2 |

- 注册账号为自动生成的 11 位数字账号（注册成功页会展示并自动复制）。
- 修改密码：登录后右上角用户中心 →「账号设置」，需输入当前密码验证身份，新密码不可与最近 5 次重复。
- 未登录访问任何页面会跳转登录页。

---

## 四、可选启动参数

```powershell
# 指定监听地址与端口
... trade_monitor.py serve --host 127.0.0.1 --port 9000

# 不自动打开浏览器
... trade_monitor.py serve --no-browser

# 用环境变量代替参数
$env:TM_HOST="127.0.0.1"; $env:TM_PORT="9000"
... trade_monitor.py serve
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `TM_HOST` 环境变量，缺省 `127.0.0.1` | 仅本机访问；`0.0.0.0` 表示对外监听（仅服务器部署用） |
| `--port` | `TM_PORT` 环境变量，缺省 `8765` | 端口被占用时换一个 |
| `--no-browser` | 关 | 不自动打开浏览器 |

---

## 五、其他常用命令

```powershell
cd G:\外贸\贸易分析工具
$py = "C:\Users\djc\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

# 手动采集数据（汇率 + 政策 + 配件报价）
& $py trade_monitor.py fetch

# 生成 AI 日报
& $py trade_monitor.py analyze

# 配件报价查询
& $py trade_monitor.py parts 刹车片

# 后台建号（默认密码 d12345678）
& $py trade_monitor.py adduser 12345678901 --username 张三 --email a@b.com
```

---

## 六、常见问题排查

**1. 端口被占用 / 重复进程**

```powershell
# 查谁占用 8765
Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object OwningProcess -Unique

# 结束旧进程（记下 PID 后）
Stop-Process -Id <PID> -Force
```

改过代码重启服务前，务必先确认 8765 只有一个进程在监听，否则会出现「改了代码不生效」的假象。

**2. 页面样式/功能没更新**：前端是静态文件，改 `web/` 下文件**无需重启**，浏览器 `Ctrl+F5` 强制刷新即可；改 `trade_monitor.py` **必须重启**。

**3. 启动时卡在"自动采集一次"**：外网汇率源偶发超时会自动跳过，看板照常启动；数据可稍后用 `fetch` 命令补采。

**4. 找不到数据库 / 配置**：确认是先 `cd G:\外贸\贸易分析工具` 再启动——所有路径按脚本所在目录定位，与终端当前目录无关，但保持一致可避免困惑。

---

## 七、快速启动清单（每天照做）

```powershell
cd G:\外贸\贸易分析工具
C:\Users\djc\.workbuddy\binaries\python\envs\default\Scripts\python.exe trade_monitor.py serve
```

登录 → 用完 `Ctrl+C` 停止。
