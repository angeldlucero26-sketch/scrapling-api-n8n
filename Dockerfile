FROM python:3.12-slim

WORKDIR /app

# Install system deps for Scrapling's browser features
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers and dependencies
RUN playwright install chromium
RUN playwright install-deps chromium

# Install Scrapling's browser dependencies (Camoufox for StealthyFetcher)
RUN python -m scrapling install || true

COPY main.py .

EXPOSE 8899

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8899"]
