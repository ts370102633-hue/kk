#!/bin/bash
# StepAudio Voice Studio — 纯云端，无需本地模型
set -e
cd "$(dirname "$0")"

# 激活虚拟环境
source .venv/bin/activate 2>/dev/null || true

# StepAudio API 配置：本地可放 .env，云服务器建议配置为系统环境变量
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
export STEP_API_BASE="${STEP_API_BASE:-https://api.stepfun.com/step_plan/v1}"
export STEP_TTS_MODEL="${STEP_TTS_MODEL:-stepaudio-2.5-tts}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///./data/stepaudio.db}"
export LOCAL_STORAGE_DIR="${LOCAL_STORAGE_DIR:-./data/files}"

if [ -z "${STEP_API_KEY:-}" ]; then
  echo "  警告: 未配置 STEP_API_KEY，登录和页面可用，但语音克隆/TTS/转写会失败。"
fi

echo ""
echo "  StepAudio Voice Studio 启动中..."
echo "  API + 前端: http://localhost:8808"
echo ""

cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8808
