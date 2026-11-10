"""API HTTP bọc quanh trình duyệt đã đăng nhập chatgpt.com (dùng cá nhân).

Biến môi trường:
  API_KEY       một khóa duy nhất, hoặc
  API_KEYS      nhiều khóa: "duan1:khoa1,duan2:khoa2"
  HEADLESS      0 = có cửa sổ (mặc định) | new = Chromium headless mới, không cần màn hình
                | 1 = headless-shell cũ (dễ bị Cloudflare chặn)
  IDLE_TAB      giây không dùng thì đóng tab của cuộc hội thoại (mặc định 300)
  IDLE_BROWSER  giây không còn tab nào thì tắt hẳn trình duyệt (mặc định 600)
  MAX_TABS      số tab/cuộc hội thoại mở cùng lúc tối đa (mặc định 5)
  REPLY_TIMEOUT giây chờ phản hồi tối đa (mặc định 180)

Endpoint (trừ /health đều cần  Authorization: Bearer <khóa>):
  GET    /health
  POST   /chat   {"message": "...", "conversation_id": "<tùy chọn>"}
                 -> {"reply": "...", "conversation_id": "..."}
                 Không gửi conversation_id = mở cuộc hội thoại mới.
  POST   /v1/chat/completions   kiểu OpenAI (không stream), thêm trường
                 "conversation_id" trong body; id trả về ở trường "conversation_id".
  GET    /conversations          các cuộc hội thoại của khóa này đang mở tab
  DELETE /conversations/{id}     đóng tab ngay (cuộc trò chuyện vẫn còn trên ChatGPT)
"""
import asyncio
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from playwright.async_api import async_playwright
from pydantic import BaseModel

BASE = Path(__file__).parent
URL = "https://chatgpt.com/"
PROFILE = Path(os.environ.get("PROFILE_DIR", BASE / "profile"))
OWNERS_FILE = Path(os.environ.get("OWNERS_FILE", BASE / "conversations.json"))
HEADLESS_MODE = os.environ.get("HEADLESS", "0").lower()  # 0 | 1 | new
IDLE_TAB = int(os.environ.get("IDLE_TAB", "300"))
IDLE_BROWSER = int(os.environ.get("IDLE_BROWSER", "600"))
MAX_TABS = int(os.environ.get("MAX_TABS", "5"))
REPLY_TIMEOUT = int(os.environ.get("REPLY_TIMEOUT", "180"))

CONV_RE = re.compile(r"/c/([0-9a-fA-F-]{20,})")
UUID_RE = re.compile(r"^[0-9a-fA-F-]{20,64}$")

SEL_INPUTS = [
    "#prompt-textarea",
    '[data-testid="prompt-textarea"]',
    'div.ProseMirror[contenteditable="true"]',
    'div[contenteditable="true"]',
]  # không dùng "textarea": đó là ô tạm (pending-home-input) bị thay khi trang tải xong
SEL_ASSISTANT = '[data-markdown-text-style="assistant-message"], [data-message-author-role="assistant"]'
SEL_STOP = '[data-testid="stop-button"], button[aria-label*="Stop"]'


# ---------- khóa API ----------
def load_keys():
    keys = {}
    for item in os.environ.get("API_KEYS", "").split(","):
        if ":" in item:
            name, key = item.split(":", 1)
            if name.strip() and key.strip():
                keys[key.strip()] = name.strip()
    if os.environ.get("API_KEY"):
        keys[os.environ["API_KEY"]] = "default"
    return keys


KEYS = load_keys()
bearer = HTTPBearer(auto_error=False)


def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    if not KEYS:
        raise HTTPException(500, "Server chưa đặt API_KEY hoặc API_KEYS.")
    if cred:
        for key, name in KEYS.items():
            if secrets.compare_digest(cred.credentials, key):
                return name
    raise HTTPException(401, "Sai API key.")


# ---------- ai sở hữu cuộc hội thoại nào ----------
def load_owners():
    try:
        return json.loads(OWNERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


owners: dict[str, str] = load_owners()


def save_owners():
    OWNERS_FILE.write_text(json.dumps(owners, indent=1), encoding="utf-8")


# ---------- trình duyệt (bật khi cần, tắt khi rảnh) ----------
class Tab:
    def __init__(self, page, conv_id, owner):
        self.page, self.conv_id, self.owner = page, conv_id, owner
        self.lock = asyncio.Lock()
        self.last_used = time.time()


pw = None
ctx = None
tabs: dict[str, Tab] = {}
browser_lock = asyncio.Lock()
last_activity = time.time()


async def ensure_browser():
    global ctx
    async with browser_lock:
        if ctx is not None:
            try:
                _ = ctx.pages  # còn sống?
                return ctx
            except Exception:
                ctx = None
        opts = {}
        if HEADLESS_MODE == "new":
            # Chromium đầy đủ ở chế độ headless mới: không cần màn hình ảo, ít bị Cloudflare nhận ra
            opts = {"headless": True, "channel": "chromium"}
        else:
            opts = {"headless": HEADLESS_MODE == "1"}
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE),
            viewport={"width": 1100, "height": 800},
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
            **opts,
        )
        ctx.on("close", lambda *_: tabs.clear())
        return ctx


async def close_browser():
    global ctx
    async with browser_lock:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
            ctx = None
        tabs.clear()


async def find_input(page):
    for sel in SEL_INPUTS:
        loc = page.locator(sel)
        for i in range(await loc.count()):
            item = loc.nth(i)
            if await item.is_visible():
                return item
    return None


async def wait_input(page, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        box = await find_input(page)
        if box:
            return box
        await asyncio.sleep(0.5)
    raise HTTPException(503, "Không thấy ô chat. Phiên đăng nhập có thể đã hết hạn.")


async def stable_count(page, quiet=1.5, timeout=15):
    """Số tin trả lời hiện có, chờ tới khi lịch sử tải xong (không đổi)."""
    last, since, end = -1, time.time(), time.time() + timeout
    while time.time() < end:
        n = await page.locator(SEL_ASSISTANT).count()
        if n != last:
            last, since = n, time.time()
        elif time.time() - since >= quiet:
            break
        await asyncio.sleep(0.3)
    return last


async def ask(page, text):
    await page.bring_to_front()  # tab nền không vẽ khung hình -> click bị treo
    before = await stable_count(page, quiet=0.4, timeout=3)
    for attempt in range(4):
        box = await wait_input(page, 30)
        try:
            await box.click(timeout=5000)
            break
        except Exception:
            if attempt == 3:
                raise HTTPException(503, "Không bấm được vào ô chat.")
            await asyncio.sleep(1)
    await page.keyboard.insert_text(text)
    await page.keyboard.press("Enter")

    deadline = time.time() + REPLY_TIMEOUT
    while await page.locator(SEL_ASSISTANT).count() <= before:
        if time.time() > deadline:
            raise HTTPException(504, "Không nhận được phản hồi.")
        await asyncio.sleep(0.3)

    last = page.locator(SEL_ASSISTANT).last
    prev, stable_since = "", time.time()
    while True:
        cur = await last.inner_text()
        if cur != prev:
            prev, stable_since = cur, time.time()
        generating = await page.locator(SEL_STOP).count() > 0
        if not generating and prev and time.time() - stable_since > 2.5:
            return prev
        if time.time() > deadline:
            return prev
        await asyncio.sleep(0.3)


async def make_room():
    """Đảm bảo không vượt MAX_TABS: đóng tab rảnh lâu nhất."""
    while len(tabs) >= MAX_TABS:
        idle = [t for t in tabs.values() if not t.lock.locked()]
        if not idle:
            raise HTTPException(429, f"Đang có {MAX_TABS} cuộc hội thoại chạy, thử lại sau.")
        await close_tab(min(idle, key=lambda t: t.last_used))


async def close_tab(tab: Tab):
    tabs.pop(tab.conv_id, None)
    try:
        await tab.page.close()
    except Exception:
        pass


async def open_existing(conv_id, owner) -> Tab:
    if owners.get(conv_id) != owner:
        raise HTTPException(404, "Không tìm thấy cuộc hội thoại này (hoặc không thuộc về khóa của bạn).")
    t = tabs.get(conv_id)
    if t:
        return t
    await make_room()
    context = await ensure_browser()
    page = await context.new_page()
    await page.goto(f"{URL}c/{conv_id}")
    try:
        await wait_input(page, 60)
    except HTTPException:
        await page.close()
        raise
    if conv_id not in page.url:
        await page.close()
        raise HTTPException(404, "ChatGPT không mở được cuộc hội thoại này (đã bị xóa?).")
    await stable_count(page)
    t = Tab(page, conv_id, owner)
    tabs[conv_id] = t
    return t


async def converse(owner, text, conv_id):
    global last_activity
    last_activity = time.time()
    if conv_id:
        if not UUID_RE.match(conv_id):
            raise HTTPException(400, "conversation_id không hợp lệ.")
        tab = await open_existing(conv_id, owner)
        async with tab.lock:
            reply = await ask(tab.page, text)
            tab.last_used = last_activity = time.time()
        return reply, conv_id

    # cuộc hội thoại mới
    await make_room()
    context = await ensure_browser()
    page = await context.new_page()
    try:
        await page.goto(URL)
        await wait_input(page, 60)
        reply = await ask(page, text)
        end = time.time() + 20
        new_id = None
        while time.time() < end:
            m = CONV_RE.search(page.url)
            if m:
                new_id = m.group(1)
                break
            await asyncio.sleep(0.5)
        if not new_id:
            raise HTTPException(502, "Không lấy được conversation_id từ ChatGPT.")
    except BaseException:
        await page.close()
        raise
    owners[new_id] = owner
    save_owners()
    tab = Tab(page, new_id, owner)
    tabs[new_id] = tab
    last_activity = time.time()
    return reply, new_id


async def reaper():
    while True:
        await asyncio.sleep(20)
        now = time.time()
        for t in list(tabs.values()):
            if not t.lock.locked() and now - t.last_used > IDLE_TAB:
                await close_tab(t)
        if ctx is not None and not tabs and now - last_activity > IDLE_BROWSER:
            await close_browser()


@asynccontextmanager
async def lifespan(app):
    global pw
    pw = await async_playwright().start()
    task = asyncio.create_task(reaper())
    yield
    task.cancel()
    await close_browser()
    await pw.stop()


app = FastAPI(title="ChatGPT web bridge", lifespan=lifespan)


# ---------- endpoint ----------
class ChatIn(BaseModel):
    message: str
    conversation_id: str | None = None


@app.get("/health")
async def health():
    return {"ok": True, "browser_running": ctx is not None, "open_tabs": len(tabs)}


@app.post("/chat")
async def chat(body: ChatIn, owner: str = Depends(auth)):
    reply, cid = await converse(owner, body.message, body.conversation_id)
    return {"reply": reply, "conversation_id": cid}


@app.get("/conversations")
async def conversations(owner: str = Depends(auth)):
    return [{"conversation_id": t.conv_id, "idle_seconds": int(time.time() - t.last_used)}
            for t in tabs.values() if t.owner == owner]


@app.delete("/conversations/{conv_id}")
async def close_conv(conv_id: str, owner: str = Depends(auth)):
    t = tabs.get(conv_id)
    if t and t.owner == owner and not t.lock.locked():
        await close_tab(t)
    return {"ok": True}


class OAIMessage(BaseModel):
    role: str
    content: str | list


class OAIIn(BaseModel):
    model: str = "chatgpt-web"
    messages: list[OAIMessage]
    conversation_id: str | None = None


@app.post("/v1/chat/completions")
async def completions(body: OAIIn, owner: str = Depends(auth)):
    users = [m for m in body.messages if m.role == "user"]
    if not users:
        raise HTTPException(400, "Cần ít nhất một tin nhắn user.")
    content = users[-1].content
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    reply, cid = await converse(owner, content, body.conversation_id)
    return {
        "id": "chatcmpl-" + secrets.token_hex(8),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "conversation_id": cid,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": reply}}],
    }
