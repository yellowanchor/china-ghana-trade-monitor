# 中加贸易分析看板 — 服务器部署指南

## 架构总览

```
用户浏览器 ──HTTPS──▶ Nginx (80/443) ──反代──▶ Python 服务 (127.0.0.1:8765)
                                                           │
                                            ┌──────────────┼──────────────┐
                                            ▼              ▼              ▼
                                     trade_monitor.db   web/ 静态    ai_config.json
                                     (SQLite 数据)    (Vue3 前端)    (密钥配置)
```

- **Python 服务**：`ThreadingHTTPServer`，仅监听 `127.0.0.1:8765`，不直接暴露公网
- **Nginx**：处理 HTTPS 终端、静态缓存、反代到 Python
- **systemd**：守护进程，崩溃自动重启、开机自启
- **SQLite**：单文件数据库，零运维，单机足够

---

## 前置条件

| 项目 | 要求 |
|------|------|
| 服务器 | Linux VPS（Ubuntu 20.04+ 推荐 / Debian / CentOS） |
| 内存 | 512 MB 起步（纯 Python + SQLite 很轻） |
| 端口 | 开放 80（HTTP）、443（HTTPS） |
| Python | 3.8+ |
| 域名 | 可选，有域名可用 Let's Encrypt 签发免费 HTTPS 证书 |

---

## 一、一键部署（推荐）

服务器上以 root 执行：

```bash
# 1. 克隆仓库
git clone https://github.com/yellowanchor/china-ghana-trade-monitor.git /opt/trade-monitor
cd /opt/trade-monitor

# 2. 运行部署脚本（自动安装 Python/Nginx/依赖/服务/反代）
sudo bash deploy/deploy.sh
```

脚本完成后，按提示完成 5 个手动步骤即可。

---

## 二、手动部署（逐步详解）

### 2.1 安装系统依赖

```bash
# Ubuntu / Debian
apt update && apt install -y python3 python3-venv python3-pip nginx git

# CentOS 7（需确保 Python 3.8+）
yum install -y python3 python3-devel nginx git
```

### 2.2 部署代码

```bash
git clone https://github.com/yellowanchor/china-ghana-trade-monitor.git /opt/trade-monitor
cd /opt/trade-monitor
```

> 如果服务器无法访问 GitHub，可在本地打包后上传：
> ```bash
> # 本地
> tar czf trade-monitor.tar.gz trade_monitor.py web/ deploy/ ai_config.example.json
> # 上传到服务器后解压到 /opt/trade-monitor
> ```

### 2.3 创建虚拟环境 + 安装依赖

```bash
cd /opt/trade-monitor
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r deploy/requirements.txt
```

> 核心服务**零 pip 依赖**（纯标准库），`requirements.txt` 里的 `reportlab` 仅用于合同 PDF 导出。

### 2.4 配置密钥

```bash
cp ai_config.example.json ai_config.json
nano ai_config.json
# 填入：AI API key、app_secret（密码加密密钥）、验证码配置等
chmod 600 ai_config.json   # 限制权限
```

> **关键**：`app_secret` 是密码两级加密的密钥，必须与本地一致或重新设定（改了密钥则旧密码全部失效需重置）。

### 2.5 初始化数据库 + 插入账号

```bash
# 首次运行自动建表
venv/bin/python3 -c "import trade_monitor; trade_monitor.get_db()"

# 插入后台账号（默认密码 d12345678）
venv/bin/python3 trade_monitor.py adduser 2782699247
venv/bin/python3 trade_monitor.py adduser 1241627990
```

### 2.6 设置目录权限

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin www-data 2>/dev/null || true
chown -R www-data:www-data /opt/trade-monitor
```

### 2.7 安装 systemd 服务

```bash
# 复制服务文件
cp deploy/trade-monitor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable trade-monitor    # 开机自启
systemctl start trade-monitor     # 立即启动
systemctl status trade-monitor    # 确认运行
```

验证：
```bash
curl http://127.0.0.1:8765/login.html   # 应返回登录页 HTML
```

### 2.8 配置 Nginx 反向代理

```bash
cp deploy/nginx-trade-monitor.conf /etc/nginx/sites-available/
ln -sf /etc/nginx/sites-available/nginx-trade-monitor.conf /etc/nginx/sites-enabled/
# 编辑配置，把 your-domain.com 改成你的域名或服务器 IP
nano /etc/nginx/sites-available/nginx-trade-monitor.conf
nginx -t && systemctl reload nginx
```

### 2.9 HTTPS 证书（有域名时）

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d your-domain.com
# Certbot 自动改 Nginx 配置加 443 监听 + 自动续期定时任务
```

无域名时，直接用 HTTP（端口 80）也可用，但**密码会明文传输**，仅限内网/测试。

### 2.10 防火墙

```bash
# Ubuntu UFW
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable

# CentOS firewalld
firewall-cmd --permanent --add-service=http
firewall-cmd --permanent --add-service=https
firewall-cmd --permanent --add-service=ssh
firewall-cmd --reload
```

**不要开放 8765 端口**，它只在本机被 Nginx 访问。

---

## 三、日常运维

### 服务管理
```bash
systemctl start trade-monitor      # 启动
systemctl stop trade-monitor       # 停止
systemctl restart trade-monitor    # 重启
systemctl status trade-monitor     # 状态
journalctl -u trade-monitor -f     # 实时日志
journalctl -u trade-monitor -n 100 # 最近 100 行
```

### 更新代码
```bash
cd /opt/trade-monitor
git pull origin main
systemctl restart trade-monitor
```

### 备份
```bash
# 数据库 + 配置
tar czf backup-$(date +%Y%m%d).tar.gz \
    /opt/trade-monitor/trade_monitor.db \
    /opt/trade-monitor/ai_config.json

# 定时备份（crontab -e）
# 0 3 * * * tar czf /opt/backup/tm-$(date +\%Y\%m\%d).tar.gz /opt/trade-monitor/trade_monitor.db /opt/trade-monitor/ai_config.json
```

### 修改密钥后重置密码
```bash
cd /opt/trade-monitor
venv/bin/python3 -c "
import trade_monitor as tm, sqlite3
c = sqlite3.connect('trade_monitor.db')
c.execute('UPDATE users SET pw_blob=?, pw_plain=? WHERE account=?',
          (tm.pw_encrypt('d12345678'), 'd12345678', '2782699247'))
c.execute('DELETE FROM password_history WHERE user_id=(SELECT id FROM users WHERE account=?)',
          ('2782699247',))
c.commit(); print('reset done')
"
```

---

## 四、安全注意事项

| 风险点 | 说明 | 建议 |
|--------|------|------|
| HTTP 明文 | 无 HTTPS 时密码/Cookie 明文传输 | 必须用 Nginx + Let's Encrypt |
| SQLite 并发 | 高并发写有锁竞争 | 本项目量级足够；如需扩展可换 PostgreSQL |
| ai_config.json | 含 API 密钥 + 加密 secret | `chmod 600`，不入 git，定期备份 |
| 8765 端口 | Python 服务无 TLS | 仅 127.0.0.1 监听，防火墙不开放 |
| 明文密码存储 | `pw_plain` 字段存原始密码 | 用户明确要求；生产环境建议删除该字段只存加密密文 |

---

## 五、快速验证清单

部署完成后逐项验证：

```bash
# 1. 服务存活
systemctl is-active trade-monitor       # → active

# 2. 本机可访问
curl -s http://127.0.0.1:8765/login.html | head -5

# 3. 公网/Nginx 可访问
curl -s https://your-domain.com/login.html | head -5

# 4. 登录 API
curl -s -H 'Content-Type: application/json' \
  -d '{"account":"2782699247","password":"d12345678"}' \
  https://your-domain.com/api/auth/login

# 5. HTTPS 证书有效
curl -vI https://your-domain.com 2>&1 | grep -i 'expire\|subject'

# 6. 自动续期（如有域名）
certbot renew --dry-run
```
