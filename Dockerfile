FROM python:3.12.11-slim-bookworm AS build
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.8.22
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --python /usr/local/bin/python \
    && .venv/bin/python -m gateway.crypto.build_native

FROM python:3.12.11-slim-bookworm
WORKDIR /app
RUN groupadd --gid 10001 gateway && useradd --uid 10001 --gid gateway gateway \
    && mkdir /data && chown gateway:gateway /data
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" LD_LIBRARY_PATH="/app/.tools/gmssl/lib" PYTHONUNBUFFERED=1
USER gateway
HEALTHCHECK --interval=30s --timeout=20s --start-period=60s --retries=3 \
    CMD python -c "import os; from gateway.webui.launcher import api; assert all(api(p,'state',public_origin=os.environ.get('WEBUI_'+r+'_ORIGIN',''))['config_loaded'] for r,p in [('CLIENT',18080),('GATEWAY',18081),('ADAPTER',18082)])"
CMD ["guomi-gateway", "webui-all", "--directory", "/data/identities", "--no-browser"]
