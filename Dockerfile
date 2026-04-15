FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN groupadd --system mariana && useradd --system --gid mariana --create-home --home-dir /home/mariana mariana

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libffi-dev \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    libglib2.0-0 \
    shared-mime-info \
    fonts-noto-cjk \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the Python package and essential files
COPY mariana/ ./mariana/
COPY README.md .

RUN mkdir -p /data/mariana && chown -R mariana:mariana /app /data/mariana
USER mariana

CMD ["python", "-m", "mariana.main", "--mode", "daemon"]
