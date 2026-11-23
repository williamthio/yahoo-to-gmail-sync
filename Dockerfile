FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install locked runtime dependencies first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Install the application package (non-editable) into /app/.venv.
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable


FROM python:3.12-slim AS runtime

RUN useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER appuser

ENTRYPOINT ["yahoo-to-gmail-sync"]
