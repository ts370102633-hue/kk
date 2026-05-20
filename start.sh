#!/bin/bash
# StepAudio Voice Studio — 纯云端，无需本地模型
set -e
cd "$(dirname "$0")"

# 激活虚拟环境
source .venv/bin/activate 2>/dev/null || true

# StepAudio API 配置
export STEP_API_KEY="3TK66wvoMBlQUYt953nHgtKoNC8SAJyRCIUjzNSz3ZtatHm0fehfODRkhYbYcfsyb"
export STEP_API_BASE="https://api.stepfun.com/step_plan/v1"
export STEP_TTS_MODEL="stepaudio-2.5-tts"

echo ""
echo "  StepAudio Voice Studio 启动中..."
echo "  API + 前端: http://localhost:8808"
echo ""

cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8808
