#!/usr/bin/env bash
# ==============================================================================
# 一键部署脚本 — 中加贸易分析看板
# 在全新的 Linux 服务器上执行（Ubuntu 20.04+ / Debian 11+ / CentOS 7+）
#
# 用法：
#   chmod +x deploy/deploy.sh
#   sudo ./deploy/deploy.sh
#
# 完成后手动步骤：
#   1. 编辑 ai_config.json 填入密钥（参考 ai_config.example.json）
#   2. 修改 nginx-trade-monitor.conf 中 your-domain.com → 你的域名
#   3. certbot --nginx -d your-domain.com  签发 HTTPS
#   4. python3 trade_monitor.py adduser 你的账号  插入后台账号
# ==============================================================================
set -euo pipefail

INSTALL_DIR="/opt/trade-monitor"
SERVICE_NAME="trade-monitor"
PYTHON_REQ="3.8"

echo "================================================"
echo "  中加贸易分析看板 — 服务器部署脚本"
echo "  安装目录: $INSTALL_DIR"
echo "================================================"

# ---------- 1. 检查 root ----------
if [ "$(id -u)" -ne 0 ]; then
    echo "✗ 请用 root 或 sudo 执行此脚本"
    exit 1
fi

# ---------- 2. 安装系统依赖 ----------
echo "[1/7] 检测系统并安装依赖..."
if command -v apt-get &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip nginx git
elif command -v yum &>/dev/null; then
    yum install -y python3 python3-devel nginx git
    # CentOS 7 的 Python3 可能偏旧，检查版本
    PY_VER=$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    if [ "$(python3 -c 'import sys;print(1 if sys.version_info >= (3,8) else 0)')" = "0" ]; then
        echo "✗ 当前 Python $PY_VER < 3.8，CentOS 7 需自行安装 Python 3.8+"
        echo "  建议: yum install -y rh-python38 && scl enable rh-python38 bash"
        exit 1
    fi
else
    echo "✗ 不支持的发行版，请手动安装 python3 nginx git"
    exit 1
fi

# ---------- 3. 创建运行用户 ----------
echo "[2/7] 创建运行用户 www-data..."
if ! id -u www-data &>/dev/null; then
    if command -v apt-get &>/dev/null; then
        useradd --system --no-create-home --shell /usr/sbin/nologin www-data
    else
        useradd --system --no-create-home --shell /sbin/nologin www-data
    fi
fi

# ---------- 4. 克隆 / 部署代码 ----------
echo "[3/7] 部署代码到 $INSTALL_DIR..."
if [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR"
    git pull origin main
else
    # 方式一：从 GitHub 克隆（需服务器能访问 GitHub）
    git clone https://github.com/yellowanchor/china-ghana-trade-monitor.git "$INSTALL_DIR"
    # 方式二：本地打包上传（取消注释并修改路径）
    # mkdir -p "$INSTALL_DIR"
    # cp -r /path/to/trade_monitor.py /path/to/web /path/to/deploy "$INSTALL_DIR/"
fi

# ---------- 5. 创建虚拟环境 + 安装依赖 ----------
echo "[4/7] 创建 Python 虚拟环境..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/deploy/requirements.txt" -q 2>/dev/null || \
    echo "  (可选依赖安装跳过 — 核心功能不受影响)"

# ---------- 6. 初始化配置 ----------
echo "[5/7] 初始化配置文件..."
cd "$INSTALL_DIR"

# ai_config.json（从模板创建，需手动填入密钥）
if [ ! -f ai_config.json ]; then
    if [ -f ai_config.example.json ]; then
        cp ai_config.example.json ai_config.json
        echo "  → 已从模板创建 ai_config.json，请编辑填入密钥"
    else
        echo "  ⚠ 未找到 ai_config.example.json，请手动创建 ai_config.json"
    fi
fi

# 初始化数据库（首次启动会自动建表，这里预触发）
"$INSTALL_DIR/venv/bin/python3" -c "import sys; sys.path.insert(0,'.'); import trade_monitor; trade_monitor.get_db(); print('  → 数据库初始化完成')"

# 权限
chown -R www-data:www-data "$INSTALL_DIR"
chmod 600 "$INSTALL_DIR/ai_config.json" 2>/dev/null || true

# ---------- 7. 安装 systemd 服务 ----------
echo "[6/7] 安装 systemd 服务..."
cp "$INSTALL_DIR/deploy/trade-monitor.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "  ✓ $SERVICE_NAME 已启动并设为开机自启"
else
    echo "  ✗ $SERVICE_NAME 启动失败，请检查: journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi

# ---------- 8. Nginx 反向代理 ----------
echo "[7/7] 配置 Nginx 反向代理..."
if command -v apt-get &>/dev/null; then
    NGINX_CONF_DIR="/etc/nginx/sites-available"
    NGINX_EN_DIR="/etc/nginx/sites-enabled"
else
    NGINX_CONF_DIR="/etc/nginx/conf.d"
    NGINX_EN_DIR=""
fi

cp "$INSTALL_DIR/deploy/nginx-trade-monitor.conf" "$NGINX_CONF_DIR/"
if [ -n "$NGINX_EN_DIR" ]; then
    ln -sf "$NGINX_CONF_DIR/nginx-trade-monitor.conf" "$NGINX_EN_DIR/"
fi

if nginx -t 2>/dev/null; then
    systemctl reload nginx
    echo "  ✓ Nginx 配置已加载"
else
    echo "  ⚠ Nginx 配置语法检查失败，请手动编辑 $NGINX_CONF_DIR/nginx-trade-monitor.conf"
    echo "    将 your-domain.com 改为你的域名或 IP，然后: nginx -t && systemctl reload nginx"
fi

# ---------- 完成 ----------
echo ""
echo "================================================"
echo "  部署完成！"
echo "================================================"
echo ""
echo "  ▸ 服务状态:   systemctl status $SERVICE_NAME"
echo "  ▸ 实时日志:   journalctl -u $SERVICE_NAME -f"
echo "  ▸ 服务启停:   systemctl start/stop/restart $SERVICE_NAME"
echo ""
echo "  待完成的手动步骤："
echo "  1. 编辑配置:   nano $INSTALL_DIR/ai_config.json"
echo "  2. 改域名:     nano $NGINX_CONF_DIR/nginx-trade-monitor.conf"
echo "  3. HTTPS 签发: certbot --nginx -d your-domain.com"
echo "  4. 插入账号:   cd $INSTALL_DIR && venv/bin/python3 trade_monitor.py adduser 你的账号"
echo "  5. 防火墙:     ufw allow 80,443/tcp && ufw enable"
echo ""
