FROM python:3.11

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ ./backend/

ENV STEP_API_KEY="3TK66wvoMBlQUYt953nHgtKoNC8SAJyRCIUjzNSz3ZtatHm0fehfODRkhYbYcfsyb"
ENV STEP_API_BASE="https://api.stepfun.com/step_plan/v1"
ENV STEP_TTS_MODEL="stepaudio-2.5-tts"

EXPOSE 7860
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "7860"]
