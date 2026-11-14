FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

RUN useradd --system --uid 10001 app && mkdir -p /data && chown app /data
USER app
ENV DB_PATH=/data/bot.db

CMD ["python", "-m", "bot.main"]
