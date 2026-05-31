FROM ubuntu:22.04

RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip3 install -r requirements.txt

COPY backend/ ./backend/

ENV APP_ENV="production"
ENV DATABASE_URL="sqlite:////data/stepaudio.db"
ENV LOCAL_STORAGE_DIR="/data/files"
ENV STEP_API_BASE="https://api.stepfun.com/step_plan/v1"
ENV STEP_FILE_API_BASE="https://api.stepfun.com/v1"
ENV STEP_ASR_MODEL="stepaudio-2.5-asr"
ENV STEP_TTS_MODEL="stepaudio-2.5-tts"

VOLUME ["/data"]
EXPOSE 7860
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "7860"]
