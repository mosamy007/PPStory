FROM python:3.9-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    imagemagick \
    ghostscript \
    && rm -rf /var/lib/apt/lists/*

# Fix ImageMagick policy to allow text rendering
# This is a common fix for MoviePy on Linux containers
RUN sed -i 's/<policy domain="path" rights="none" pattern="@\*"\/>/<!-- <policy domain="path" rights="none" pattern="@*" \/> -->/g' /etc/ImageMagick-6/policy.xml

WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Create necessary directories that might not exist or be empty
RUN mkdir -p uploads outputs music fonts

# Set environment variables
ENV PORT=5000

# Expose the port
EXPOSE 5000

# Start command using Gunicorn
# Adjust workers/timeout based on available resources (Render free tier is limited)
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 app:app
