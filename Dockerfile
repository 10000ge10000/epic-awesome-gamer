FROM python:3.12-slim

WORKDIR /app

# 预创建挂载点目录（解决 Docker 存储驱动挂载权限问题）
RUN mkdir -p /app/data

RUN pip install --no-cache-dir \
    fastapi uvicorn redis apscheduler aiofiles python-multipart jinja2 httpx cryptography

RUN groupadd --gid 1002 app && useradd --uid 1002 --gid 1002 --create-home app

COPY --chown=1002:1002 . .

USER app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
