FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2-binary and other native libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source is mounted as a volume in dev — copy is a fallback for image-only builds
COPY . .

EXPOSE 8766

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8766", "--reload"]
