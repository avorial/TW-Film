FROM python:3.12-slim

WORKDIR /app

# System dependencies (Playwright's Chromium + git for auto-update)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpangocairo-1.0-0 libpango-1.0-0 \
    libcairo2 libx11-xcb1 libxcb-dri3-0 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Clone repo — code is pulled fresh on every container start via entrypoint.sh
RUN git clone https://github.com/avorial/TW-Film.git .

# Python deps (installed once at build time; only rebuild if requirements change)
RUN pip install --no-cache-dir -r requirements_web.txt

# Install Playwright's Chromium browser binary
RUN playwright install chromium

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DOCKER_ENV=1
ENV PORT=8763

EXPOSE 8763
CMD ["/entrypoint.sh"]
