FROM python:3.11-slim

# Install system dependencies (ffmpeg is required for video notes processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create a non-privileged user and group
RUN groupadd -g 10001 botuser && \
    useradd -u 10001 -g botuser -m -s /bin/bash botuser

WORKDIR /app

# Copy requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Change ownership of the app directory to the botuser
RUN chown -R botuser:botuser /app

# Switch to the non-privileged user
USER botuser

# Start the bot
CMD ["python", "main.py"]
