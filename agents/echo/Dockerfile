FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY agent.py ./

RUN useradd --create-home --uid 10001 agent
USER agent

ENV PATH="/app/.venv/bin:$PATH"

# worker health check server
EXPOSE 8081

CMD ["python", "agent.py", "start"]
