FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jarvis_slack.py .

# Railway는 환경변수로 주입 — .env.jarvis 불필요
ENV PYTHONUNBUFFERED=1

CMD ["python", "jarvis_slack.py"]
