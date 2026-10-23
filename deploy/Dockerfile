FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.5
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend ./backend
COPY scripts ./scripts
ENV PYTHONPATH=/app/backend PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
CMD ["uvicorn","cloudsre.demo_serve:app","--host","0.0.0.0","--port","8000","--no-access-log"]
