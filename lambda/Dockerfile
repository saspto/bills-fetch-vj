FROM public.ecr.aws/lambda/python:3.12

# ── Layer 1: OS-level deps for Chromium ──────────────────────────────────────
RUN microdnf install -y \
    atk \
    cups-libs \
    libdrm \
    mesa-libgbm \
    nss \
    alsa-lib \
    pango \
    libXcomposite \
    libXdamage \
    libXrandr \
    libxkbcommon \
    at-spi2-atk \
    && microdnf clean all

# ── Layer 2: Python dependencies ─────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Layer 3: Download Chromium into the image (not /tmp) ─────────────────────
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN PLAYWRIGHT_BROWSERS_PATH=/ms-playwright playwright install chromium

# ── Layer 4: Function code (most volatile — always rebuilt last) ─────────────
COPY lambda_function.py ${LAMBDA_TASK_ROOT}/

CMD ["lambda_function.handler"]
