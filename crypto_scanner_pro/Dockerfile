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
    # Chromium + Selenium deps (Alpine-native, no musl issues)
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
    schedule==1.2.1

# Copy root filesystem
COPY rootfs /

# Make scripts executable
RUN chmod a+x /run.sh
RUN chmod a+x /usr/local/bin/* || true

# Expose ports
EXPOSE 8099 5000

# Labels
LABEL \
    io.hass.name="Crypto Scanner" \
    io.hass.description="Dashboard completa per scanner crypto" \
    io.hass.type="addon" \
    io.hass.version="1.1.0"

# Run
CMD [ "/run.sh" ]
