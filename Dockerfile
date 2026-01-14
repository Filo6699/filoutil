FROM python:3.13-slim

WORKDIR /app

# Install system deps (fonts for charts)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Run bot
CMD ["python", "-m", "filoutil.app"]
