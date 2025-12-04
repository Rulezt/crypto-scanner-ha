FROM python:3.11-alpine

WORKDIR /app

# Install system dependencies
RUN apk add --no-cache \
    gcc \
    musl-dev \
    linux-headers \
    bash \
    jq

# Install Python packages
RUN pip3 install --no-cache-dir \
    flask==3.0.0 \
    flask-cors==4.0.0 \
    requests==2.31.0 \
    schedule==1.2.0 \
    matplotlib==3.8.2 \
    pandas==2.1.4 \
    numpy==1.26.2

# Copy application
COPY rootfs /

# Make executable
RUN chmod +x /run.sh

# Expose port
EXPOSE 8080

CMD ["/run.sh"]
