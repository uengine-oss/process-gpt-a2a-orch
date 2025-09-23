FROM python:3.11-slim AS base

ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Prevents Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install curl for downloading if needed

WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt ./

# Install dependencies globally (no virtualenv)
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src ./src

EXPOSE 8000

# Ensure Python can import packages from src when running scripts directly
ENV PYTHONPATH="/app/src"

CMD ["python", "src/a2a_agent_executor/server.py"]


