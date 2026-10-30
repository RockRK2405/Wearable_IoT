FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt && pip install "uvicorn[standard]" twilio

COPY . .

ENV MODE=cloud HOST=0.0.0.0 PORT=8000
EXPOSE 8000

# PORT is provided by most cloud hosts (Render/Railway/Fly); default 8000 locally.
CMD ["sh", "-c", "uvicorn backend.app.main:app_factory --factory --host 0.0.0.0 --port ${PORT:-8000}"]
