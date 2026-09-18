FROM python:3.12-slim-bookworm

ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# playwright 的浏览器 CDN 在墙内很慢，默认走 npmmirror；
# 需要官方源时 --build-arg PLAYWRIGHT_DOWNLOAD_HOST= 置空即可。
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST} \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_BUNDLED_BROWSER=1

WORKDIR /app

RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|http://deb.debian.org/debian|https://${APT_MIRROR}/debian|g; s|http://deb.debian.org/debian-security|https://${APT_MIRROR}/debian-security|g" /etc/apt/sources.list.d/debian.sources; \
    else \
      sed -i "s|http://deb.debian.org/debian|https://${APT_MIRROR}/debian|g; s|http://deb.debian.org/debian-security|https://${APT_MIRROR}/debian-security|g" /etc/apt/sources.list; \
    fi \
 && printf 'Acquire::Retries "5";\nAcquire::https::Timeout "30";\nAcquire::http::Timeout "30";\n' >/etc/apt/apt.conf.d/99codex-speed \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

# 只影响 pip 的设置放在 apt 层之后，避免改动它们导致 apt 层缓存失效。
# 默认 15s 超时对 playwright 那个 48MB 的 wheel 不够，镜像抖动时会直接失败。
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

COPY requirements.txt constraints.txt /app/
# requirements.txt 给范围，constraints.txt 钉死实测通过的那一组，
# 否则同一份 requirements 在不同时间重建会装出不同版本。
# playwright install 装的是与已安装 playwright 版本严格配套的 chromium，
# 因此 driver 与浏览器不可能错配；--with-deps 顺带补齐所需系统库。
RUN pip install -r /app/requirements.txt -c /app/constraints.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8000/health', timeout=5).raise_for_status()" || exit 1

CMD ["python", "main.py"]
