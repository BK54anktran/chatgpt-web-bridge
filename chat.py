"""Điều khiển trình duyệt thật để chat trên chatgpt.com bằng tài khoản của bạn.

Lần đầu: cửa sổ trình duyệt mở ra, bạn tự đăng nhập. Phiên đăng nhập được lưu
trong thư mục ./profile nên các lần sau không phải đăng nhập lại.

Lệnh trong lúc chat:
  /new   tạo cuộc trò chuyện mới
  /quit  thoát
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL = "https://chatgpt.com/"
PROFILE = Path(__file__).parent / "profile"

# Nếu ChatGPT đổi giao diện, chỉ cần sửa các selector này.
SEL_INPUTS = [
    "#prompt-textarea",
    '[data-testid="prompt-textarea"]',
    'div.ProseMirror[contenteditable="true"]',
    'div[contenteditable="true"]',
    "textarea",
]
SEL_ASSISTANT = '[data-markdown-text-style="assistant-message"], [data-message-author-role="assistant"]'
SEL_STOP = '[data-testid="stop-button"], button[aria-label*="Stop"]'


def find_input(page):
    for sel in SEL_INPUTS:
        loc = page.locator(sel)
        for i in range(loc.count()):
            item = loc.nth(i)
            if item.is_visible():
                return item
    return None


def wait_ready(page, timeout_s=600):
    """Chờ tới khi ô nhập hiện ra (tức là đã đăng nhập)."""
    notified = False
    end = time.time() + timeout_s
    while time.time() < end:
        if find_input(page):
            return
        if not notified:
            print("Chưa thấy ô chat. Hãy đăng nhập trong cửa sổ trình duyệt...")
            notified = True
        time.sleep(1)
    sys.exit("Hết thời gian chờ đăng nhập.")


def new_chat(page):
    page.goto(URL)
    wait_ready(page)


def ask(page, text):
    before = page.locator(SEL_ASSISTANT).count()
    box = find_input(page)
    if box is None:
        raise PWTimeout("không thấy ô nhập")
    box.click()
    # Dán qua clipboard-free insert để giữ xuống dòng; Enter để gửi.
    page.keyboard.insert_text(text)
    page.keyboard.press("Enter")

    # Chờ câu trả lời mới xuất hiện.
    page.wait_for_function(
        "([sel, n]) => document.querySelectorAll(sel).length > n",
        arg=[SEL_ASSISTANT, before],
        timeout=60000,
    )
    last = page.locator(SEL_ASSISTANT).last
    printed = ""
    stable_since = time.time()
    while True:
        cur = last.inner_text()
        if cur != printed:
            sys.stdout.write(cur[len(printed):] if cur.startswith(printed) else "\n" + cur)
            sys.stdout.flush()
            printed = cur
            stable_since = time.time()
        generating = page.locator(SEL_STOP).count() > 0
        if not generating and time.time() - stable_since > 2.5:
            break
        time.sleep(0.2)
    print()


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,  # headless thường bị Cloudflare chặn
            viewport={"width": 1100, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        new_chat(page)
        print("Sẵn sàng. Gõ tin nhắn, /new để cuộc trò chuyện mới, /quit để thoát.\n")
        try:
            while True:
                try:
                    msg = input("Bạn> ").strip()
                except EOFError:
                    break
                if not msg:
                    continue
                if msg == "/quit":
                    break
                if msg == "/new":
                    new_chat(page)
                    print("(đã tạo cuộc trò chuyện mới)")
                    continue
                print("GPT> ", end="", flush=True)
                try:
                    ask(page, msg)
                except PWTimeout:
                    print("\n[Không nhận được phản hồi. Có thể đang bị giới hạn hoặc giao diện đã đổi.]")
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
