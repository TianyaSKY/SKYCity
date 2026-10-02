# syntax=docker/dockerfile:1

FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/backend/.venv/bin:$PATH"

WORKDIR /app/backend
RUN pip install --no-cache-dir uv
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

COPY backend/ ./
COPY world_data/ /app/world_data/
COPY --from=frontend-build /app/frontend/dist/ /app/frontend/dist/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
