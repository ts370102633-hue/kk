#!/bin/bash
# StepAudio Voice Studio 一键部署脚本
# 适用于阿里云轻量应用服务器 Ubuntu 22.04/24.04
set -e

echo "=========================================="
echo "  StepAudio Voice Studio 一键部署"
echo "=========================================="

# 更新系统
echo "[1/7] 更新系统..."
apt update && apt upgrade -y

# 安装 Python 3.12
echo "[2/7] 安装 Python 3.12..."
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.12 python3.12-venv python3.12-dev

# 安装 Git
echo "[3/7] 安装 Git..."
apt install -y git

# 创建持久化目录和配置目录
echo "[4/7] 准备持久化目录..."
mkdir -p /etc/stepaudio /var/lib/stepaudio/files
chmod 700 /etc/stepaudio /var/lib/stepaudio

if [ ! -f /etc/stepaudio/stepaudio.env ]; then
  cat > /etc/stepaudio/stepaudio.env << 'EOF'
APP_ENV=production
DATABASE_URL=sqlite:////var/lib/stepaudio/stepaudio.db
LOCAL_STORAGE_DIR=/var/lib/stepaudio/files
STEP_API_KEY=请替换成你的StepAudio API Key
STEP_API_BASE=https://api.stepfun.com/step_plan/v1
STEP_FILE_API_BASE=https://api.stepfun.com/v1
STEP_ASR_MODEL=stepaudio-2.5-asr
STEP_TTS_MODEL=stepaudio-2.5-tts
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请替换成强密码
VIDEO_HD_MIN_SHORT_SIDE=1080
VIDEO_YTDLP_FALLBACK_ENABLED=true
VIDEO_DOUYIN_SHORTCUT_FALLBACK_ENABLED=false
VIDEO_SHORTCUT_API_ENABLED=true
VIDEO_SHORTCUT_ORIGINAL_FIRST_ENABLED=false
VIDEO_REQUIRE_ORIGINAL_API=false
VIDEO_SHORTCUT_DAILY_LIMIT=20
VIDEO_SHORTCUT_RATE_LIMIT_COOLDOWN_SECONDS=300
VIDEO_SHORTCUT_CONFIG_URL=https://qsy.jiejing.fun/qsy.json
VIDEO_SHORTCUT_API_BASE=https://a.jiejing.fun
VIDEO_SHORTCUT_AUTH_CODE=
TIKHUB_ENABLED=false
TIKHUB_ORIGINAL_FIRST_ENABLED=false
TIKHUB_API_BASE=https://api.tikhub.dev
TIKHUB_API_KEY=
TIKHUB_DOUYIN_REGION=CN
TIKHUB_TIMEOUT_SECONDS=45
EOF
  chmod 600 /etc/stepaudio/stepaudio.env
  echo "已创建 /etc/stepaudio/stepaudio.env。请先填入真实 STEP_API_KEY 和 ADMIN_PASSWORD 后重新运行本脚本。"
  exit 1
fi

if grep -q "请替换" /etc/stepaudio/stepaudio.env; then
  echo "/etc/stepaudio/stepaudio.env 里还有占位内容，请先填入真实 STEP_API_KEY 和 ADMIN_PASSWORD。"
  exit 1
fi

# 克隆代码
echo "[5/7] 克隆代码..."
cd /opt
rm -rf kk-studio
git clone https://gitee.com/shen-dekunkk/kk-studio.git
cd kk-studio

# 创建虚拟环境并安装依赖
echo "[6/7] 安装依赖..."
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
playwright install-deps

# 创建 systemd 服务
echo "[7/7] 配置系统服务..."
cat > /etc/systemd/system/stepaudio.service << EOF
[Unit]
Description=StepAudio Voice Studio
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/kk-studio
EnvironmentFile=/etc/stepaudio/stepaudio.env
Environment=PATH=/opt/kk-studio/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/opt/kk-studio/.venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 启动服务
systemctl daemon-reload
systemctl enable stepaudio
systemctl start stepaudio

# 获取服务器 IP
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

echo ""
echo "=========================================="
echo "  部署完成！"
echo "=========================================="
echo ""
echo "  访问地址: http://${SERVER_IP}:8000"
echo ""
echo "  数据库: /var/lib/stepaudio/stepaudio.db"
echo "  文件目录: /var/lib/stepaudio/files"
echo "  配置文件: /etc/stepaudio/stepaudio.env"
echo ""
echo "  常用命令:"
echo "  查看状态: systemctl status stepaudio"
echo "  查看日志: journalctl -u stepaudio -f"
echo "  重启服务: systemctl restart stepaudio"
echo "  停止服务: systemctl stop stepaudio"
echo ""
echo "  防火墙配置:"
echo "  请在阿里云控制台放行 8000 端口"
echo "=========================================="
