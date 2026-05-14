ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM $BUILD_FROM

# Set shell
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install requirements using APK
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-flask \
    py3-yaml \
    py3-requests \
    nginx \
    bash \
    curl \
    jq \
    chromium \
    chromium-chromedriver \
    xvfb \
    dbus \
    ttf-freefont \
    font-noto

# Install Python packages
RUN pip3 install --break-system-packages --no-cache-dir \
    flask-cors==4.0.0 \
    selenium==4.18.1 \
    schedule==1.2.1 \
    matplotlib==3.8.4 \
    pandas==2.2.1 \
    numpy==1.26.4 \
    websocket-client==1.7.0

# Copy root filesystem
COPY rootfs /

# Make scripts executable
RUN chmod a+x /run.sh
RUN chmod a+x /usr/local/bin/* || true

# Expose port
EXPOSE 8080

# Labels
LABEL \
    io.hass.name="Crypto Scanner Pro" \
    io.hass.description="Scanner crypto avanzato con Telegram" \
    io.hass.type="addon" \
    io.hass.version="2.3.2"

# Run
CMD [ "/run.sh" ]
