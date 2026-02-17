import os
import re
import math
import asyncio
import random
import requests
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.functions.messages import SendMediaRequest
from telethon.tl.types import (
    InputFileBig,
    InputMediaUploadedDocument,
    DocumentAttributeFilename,
)

# ── ENV ────────────────────────────────────────────────────────────────────
API_ID         = int(os.environ["TG_API_ID"])
API_HASH       = os.environ["TG_API_HASH"]
BOT_TOKEN      = os.environ["BOT_TOKEN"]
OWNER_ID       = int(os.environ["OWNER_ID"])
STRING_SESSION = os.environ.get("STRING_SESSION") # Aluth variable eka

# ── Config ─────────────────────────────────────────────────────────────────
PART_SIZE    = 1990 * 1024 * 1024   # 1990 MB per split part
UPLOAD_CHUNK = 256 * 1024           # 256 KB per chunk
PARALLEL     = 8                    # 8 parallel connections
UA           = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
WORK_DIR     = "/tmp/gofile_downloads"

os.makedirs(WORK_DIR, exist_ok=True)

# ── Clients ────────────────────────────────────────────────────────────────
# String Session eka use karala parallel clients hadanawa
user_clients = []
if STRING_SESSION:
    for i in range(PARALLEL):
        # Ekama string session eka use karala multiple connections hadanawa
        user_clients.append(TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH))
else:
    # Local run karanawa nam thama meka ona wenne
    user_clients.append(TelegramClient("user_session", API_ID, API_HASH))

bot_client = TelegramClient("bot_session", API_ID, API_HASH)

# ════════════════════════════════════════
# GoFile helpers (Oyaage original eka)
# ════════════════════════════════════════

def get_website_token() -> str:
    resp = requests.get("https://gofile.io/dist/js/config.js", headers={"User-Agent": UA}, timeout=15)
    resp.raise_for_status()
    js = resp.text
    if 'appdata.wt = "' in js:
        return js.split('appdata.wt = "')[1].split('"')[0]
    for pat in [r'websiteToken["\']?\s*[=:]\s*["\']([^"\']{4,})["\']', r'"wt"\s*:\s*"([^"]{4,})"']:
        m = re.search(pat, js)
        if m: return m.group(1)
    raise Exception("GoFile websiteToken config.js eke nemata.")

def resolve_gofile(page_url: str):
    content_id = page_url.rstrip("/").split("/d/")[-1]
    r = requests.post("https://api.gofile.io/accounts", headers={"User-Agent": UA}, timeout=15).json()
    if r.get("status") != "ok": raise Exception(f"Guest token fail: {r}")
    guest_token = r["data"]["token"]
    wt = get_website_token()
    hdrs = {"Authorization": f"Bearer {guest_token}", "X-Website-Token": wt, "User-Agent": UA}
    resp = requests.get(f"https://api.gofile.io/contents/{content_id}?cache=true", headers=hdrs, timeout=30).json()
    if resp.get("status") != "ok": raise Exception(f"GoFile API: {resp.get('status')}")
    data = resp["data"]
    children = data.get("children", {})
    item = next((v for v in children.values() if v.get("type") == "file"), None)
    if not item:
        if data.get("type") == "file": item = data
        else: raise Exception("File nemata.")
    return item["link"], item["name"], item.get("size", 0), hdrs

# ════════════════════════════════════════
# Progress, Download, Split & Upload (Oyaage original logically unchanged)
# ════════════════════════════════════════

def make_bar(pct: int, length: int = 12) -> str:
    filled = int(length * pct / 100)
    return "[" + "█" * filled + "░" * (length - filled) + "]"

async def download_file(url: str, filename: str, headers: dict, status_cb=None) -> str:
    path = os.path.join(WORK_DIR, filename)
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        last_pct = -10
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if total and status_cb:
                        pct = int(done / total * 100)
                        if pct - last_pct >= 5:
                            last_pct = pct
                            await status_cb(f"⬇️ **Downloading...**\n\n{make_bar(pct)} {pct}%\n📦 {done//(1024**2)} MB / {total//(1024**2)} MB")
    return path

def split_file(path: str) -> list:
    size = os.path.getsize(path)
    n = math.ceil(size / PART_SIZE)
    if n == 1: return [path]
    parts = []
    with open(path, "rb") as f:
        for i in range(n):
            pname = f"{path}.part{i+1}of{n}"
            with open(pname, "wb") as out:
                remaining = PART_SIZE
                while remaining > 0:
                    chunk = f.read(min(4 * 1024 * 1024, remaining))
                    if not chunk: break
                    out.write(chunk)
                    remaining -= len(chunk)
            if os.path.exists(pname) and os.path.getsize(pname) > 0:
                parts.append(pname)
    return parts

async def upload_large_file_parallel(file_path: str, status_cb=None, part_num: int = 1, total_parts: int = 1) -> InputFileBig:
    file_size = os.path.getsize(file_path)
    file_id = random.randint(0, 2**63)
    total_chunks = math.ceil(file_size / UPLOAD_CHUNK)
    uploaded_chunks = [0]
    lock = asyncio.Lock()

    async def upload_chunk(chunk_idx: int, data: bytes, client: TelegramClient):
        for attempt in range(3):
            try:
                await client(SaveBigFilePartRequest(file_id=file_id, file_part=chunk_idx, file_total_parts=total_chunks, bytes=data))
                async with lock:
                    uploaded_chunks[0] += 1
                    done = uploaded_chunks[0]
                    if status_cb and done % max(1, total_chunks // 20) == 0:
                        pct = int(done / total_chunks * 100)
                        await status_cb(f"⬆️ **Uploading part {part_num}/{total_parts}**\n\n{make_bar(pct)} {pct}%\n📤 {min(done * UPLOAD_CHUNK, file_size)//(1024**2)} MB / {file_size//(1024**2)} MB\n⚡ Parallel: {PARALLEL}")
                return
            except Exception as e:
                if attempt == 2: raise e
                await asyncio.sleep(1)

    chunks = []
    with open(file_path, "rb") as f:
        for i in range(total_chunks):
            data = f.read(UPLOAD_CHUNK)
            if not data: break
            chunks.append((i, data))

    semaphore = asyncio.Semaphore(PARALLEL)
    async def bounded_upload(chunk_idx: int, data: bytes, client: TelegramClient):
        async with semaphore: await upload_chunk(chunk_idx, data, client)

    tasks = [bounded_upload(c_idx, c_data, user_clients[i % len(user_clients)]) for i, (c_idx, c_data) in enumerate(chunks)]
    await asyncio.gather(*tasks)
    return InputFileBig(id=file_id, parts=total_chunks, name=os.path.basename(file_path))

async def upload_parts(parts: list, original_name: str, status_cb=None):
    total = len(parts)
    for i, p in enumerate(parts, 1):
        input_file = await upload_large_file_parallel(p, status_cb, i, total)
        await user_clients[0](SendMediaRequest(
            peer="me",
            media=InputMediaUploadedDocument(
                file=input_file, mime_type="application/octet-stream",
                attributes=[DocumentAttributeFilename(os.path.basename(p))]
            ),
            message=f"📦 {original_name}\n🗂 Part {i}/{total}",
            random_id=random.randint(0, 2**63),
        ))

def cleanup(original: str, parts: list):
    targets = set(parts)
    if original: targets.add(original)
    for f in targets:
        try: os.remove(f)
        except: pass

# ════════════════════════════════════════
# Bot Handlers & Main
# ════════════════════════════════════════

active_jobs = {}

@bot_client.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    await event.respond("📦 **GoFile Downloader**\n\nLink yawanna:\n`https://gofile.io/d/XXXXXX`", 
                        buttons=[[Button.url("Dev", "https://t.me/ashencode")]])

@bot_client.on(events.NewMessage(pattern=r"https://gofile\.io/d/\S+"))
async def gofile_handler(event):
    chat_id = event.chat_id
    if active_jobs.get(chat_id): return await event.respond("⏳ Job running...")
    active_jobs[chat_id] = True
    url = event.pattern_match.group(0).strip()
    status_msg = await event.respond(f"🔍 Processing...")

    async def update_status(text: str):
        try: await status_msg.edit(text)
        except: pass

    original_file = None
    parts = []
    try:
        dl_url, fname, size, hdrs = resolve_gofile(url)
        original_file = await download_file(dl_url, fname, hdrs, update_status)
        parts = split_file(original_file)
        await upload_parts(parts, fname, update_status)
        await update_status(f"✅ **Done!**\n📦 `{fname}` upload kala.")
    except Exception as e:
        await update_status(f"❌ **Error:**\n`{e}`")
    finally:
        cleanup(original_file, parts)
        active_jobs.pop(chat_id, None)

async def main():
    # User clients connect using String Session
    print("[*] Connecting User Clients...")
    for i, client in enumerate(user_clients):
        await client.start()
        print(f"[✓] User Client {i} connected.")

    await bot_client.start(bot_token=BOT_TOKEN)
    me = await bot_client.get_me()
    print(f"[✓] Bot started: @{me.username}")
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())