#!/bin/sh
# MODE=api   (mặc định): chạy API.
# MODE=setup : mở trình duyệt thật qua noVNC (/vnc.html) để bạn đăng nhập ChatGPT lần đầu.
set -e
mkdir -p /data/profile
rm -f /tmp/.X99-lock /data/profile/SingletonLock /data/profile/SingletonCookie /data/profile/SingletonSocket

Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
sleep 1

if [ "$MODE" = "setup" ]; then
  if [ -z "$VNC_PASSWORD" ]; then
    echo "MODE=setup cần biến VNC_PASSWORD" >&2
    exit 1
  fi
  x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass
  x11vnc -display :99 -rfbauth /tmp/vncpass -forever -shared -rfbport 5900 -localhost -quiet &
  CHROME=$(python -c "from playwright.sync_api import sync_playwright as s; p=s().start(); print(p.chromium.executable_path); p.stop()")
  "$CHROME" --no-sandbox --disable-dev-shm-usage --user-data-dir=/data/profile \
      --window-position=0,0 --window-size=1270,880 https://chatgpt.com/ &
  echo "Setup: mở /vnc.html để đăng nhập"
  exec websockify --web /usr/share/novnc "${PORT:-8000}" localhost:5900
fi

exec python -m uvicorn api:app --host 0.0.0.0 --port "${PORT:-8000}"
