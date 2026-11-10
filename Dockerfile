FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Xvfb = màn hình ảo; x11vnc + noVNC chỉ dùng ở MODE=setup để đăng nhập lần đầu
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify procps fonts-noto-color-emoji libatomic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY api.py start.sh ./
RUN sed -i 's/\r$//' start.sh && chmod +x start.sh

# Phiên đăng nhập + danh sách cuộc hội thoại nằm trong /data (phải khai là thư mục bền)
ENV PROFILE_DIR=/data/profile \
    OWNERS_FILE=/data/conversations.json \
    HEADLESS=0 \
    DISPLAY=:99 \
    MAX_TABS=2 \
    PORT=8000

EXPOSE 8000
CMD ["./start.sh"]
