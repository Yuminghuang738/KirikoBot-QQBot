FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libxml2-dev libxslt-dev gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# 依赖源可以换。默认走 PyPI；在国内网络下换成本地镜像会快很多：
#   docker compose build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=

# 重试一次。这里**不是**为了对付源站故障，而是对付**下载损坏**：
# 实测遇到过一次 `THESE PACKAGES DO NOT MATCH THE HASHES` —— scipy 那个 35MB 的
# 轮子下下来和索引给的 sha256 对不上（而且索引给的哈希本身也和 PyPI 的真实值
# 不一致），重跑就正常了。大文件 + 代理网络偶发损坏，重试一次最省事。
RUN set -eux; \
    args=""; \
    if [ -n "$PIP_TRUSTED_HOST" ]; then args="--trusted-host $PIP_TRUSTED_HOST"; fi; \
    pip install --no-cache-dir -i "$PIP_INDEX_URL" $args -r requirements.txt \
    || (echo "首次安装失败，5 秒后重试一次（怀疑是下载损坏）" >&2; sleep 5; \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" $args -r requirements.txt)

COPY . .

RUN useradd --create-home --user-group appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "main:app"]
