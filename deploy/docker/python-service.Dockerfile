FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY apps ./apps
COPY packages ./packages
COPY scripts ./scripts

ARG INSTALL_TBANK_EXTRA=false
ARG TBANK_PIP_EXTRA_INDEX_URL=https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple

RUN python -m pip install --upgrade pip && \
    if [ "$INSTALL_TBANK_EXTRA" = "true" ]; then \
      python -m pip install -e ".[tbank]" --extra-index-url "$TBANK_PIP_EXTRA_INDEX_URL"; \
    else \
      python -m pip install .; \
    fi

CMD ["python", "-m", "trade_core.service"]
