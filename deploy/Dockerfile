FROM python:3.12.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

RUN addgroup --system app && adduser --system --ingroup app --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app

EXPOSE 8000

# 启动前跑一次 migration，SQLite 文件是新建的空库也没事（alembic upgrade head
# 在空库上就是从头建表）。docs/design.md 5 节：这是应用唯一需要的持久化表。
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
