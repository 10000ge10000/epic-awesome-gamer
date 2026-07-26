FROM python:3.12-slim

WORKDIR /app

# 预创建挂载点目录（解决 Docker 存储驱动挂载权限问题）
RUN mkdir -p /app/data

# 版本全部钉死。此前这里是无版本号的 pip install，每次 build 都会拉当天最新版，
# 意味着 `git pull && docker compose up -d --build` 随时可能构建出一个跑不起来的镜像，
# 而旧镜像因为共用 epic-kiosk-web:local 这个 tag 已被覆盖，回滚困难。
# 下面的版本取自 2026-07-27 线上实际运行且验证通过的容器。
RUN pip install --no-cache-dir \
    fastapi==0.139.2 \
    uvicorn==0.51.0 \
    redis==8.0.1 \
    apscheduler==3.11.3 \
    python-multipart==0.0.32 \
    jinja2==3.1.6 \
    httpx==0.28.1 \
    cryptography==49.0.0 \
    starlette==1.3.1 \
    pydantic==2.13.4

RUN groupadd --gid 1002 app && useradd --uid 1002 --gid 1002 --create-home app

COPY --chown=1002:1002 . .

USER app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
