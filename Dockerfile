FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV TERN_STATE=/var/data/state.json \
    TERN_EVENTLOG=/var/data/eventlog.db \
    TERN_AUTOSTART=1 \
    TERN_MODE=PAPER

RUN mkdir -p /var/data

EXPOSE 8000
CMD ["sh", "-c", "uvicorn service.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
