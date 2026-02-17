import os
import re
import math
import asyncio
import random
import time
import requests
from collections import deque
from telethon import TelegramClient, events
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
STRING_SESSION = os.environ.get("STRING_SESSION")

# ── Config ─────────────────────────────────────────────────────────────────
PART_SIZE  = 1990 * 1024 * 1024
CHUNK_SIZE = 512 * 1024
CONCURRENT = 8  # Safe speed for parallel chunks
UA         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
WORK_DIR   = "/tmp/gofile_dl"

os.makedirs(WORK_DIR, exist_ok=True)

# ── Clients ────────────────────────────────────────────────────────────────
if STRING_SESSION:
    user_client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
else:
    user_client = TelegramClient("user_session", API_ID, API_HASH)

bot_client = TelegramClient("bot_session", API_ID, API_HASH)

# ── Queue ──────────────────────────────────────────────────────────────────
job_queue     = deque()
queue_urls    = set()
worker_task   = None 

# ════════════════════════════════════════
# GoFile & Utility
# ════════════════════════════════════════

def get_website_token() -> str:
    r = requests.get("https://gofile.io/dist/js/config.js", headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    js = r.text
    if 'appdata.wt = "' in js:
        return js.split('appdata.wt = "')[1].split('"')[0]
    for pat in [r'websiteToken["\']?\s*[=:]\s*["\']([^"\']{4,})["\']', r'"wt"\s*:\s*"([^"]{4,})"']:
        m = re.search(pat, js)
        if m: return m.group(1)
    raise Exception("GoFile websiteToken nemata.")

def resolve_gofile(page_url: str):
    cid = page_url.rstrip("/").split("/d/")[-1]
    r = requests.post("https://api.gofile.io/accounts", headers={"User-Agent": UA}, timeout=15).json()
    if r.get("status") != "ok": raise Exception(f"Guest token fail: {r}")
    wt = get_website_token()
    hdrs = {"Authorization": f"Bearer {r['data']['token']}", "X-Website-Token": wt, "User-Agent": UA}
    resp = requests.get(f"https://api.gofile.io/contents/{cid}?cache=true", headers=hdrs, timeout=30).json()
    if resp.get("status") != "ok": raise Exception(f"GoFile API Error")
    data = resp["data"]
    children = data.get("children", {})
    item = next((v for v in children.values() if v.get("type") == "file"), None)
    if not item:
        if data.get("type") == "file": item = data
        else: raise Exception("File nemata.")
    return item["link"], item["name"], item.get("size", 0), hdrs

def make_bar(pct: int, length: int = 12) -> str:
    f = int(length * pct / 100)
    return "[" + "█" * f + "░" * (length - f) + "]"

def safe_remove(path: str):
    try:
        if path and os.path.exists(path): os.remove(path)
    except: pass

# ════════════════════════════════════════
# Core Functions
# ════════════════════════════════════════

async def download_file(url: str, fname: str, headers: dict, cb) -> str:
    path = os.path.join(WORK_DIR, fname)
    last_edit = 0
    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if total and (now - last_edit > 5): # Thappara 5kata sarayak edit
                        pct = int(done / total * 100)
                        await cb(f"⬇️ **Downloading...**\n\n{make_bar(pct)} {pct}%\n📦 {done//(1024**2)} MB / {total//(1024**2)} MB")
                        last_edit = now
    return path

async def upload_part(file_path: str, cb, part_num: int, total_parts: int) -> InputFileBig:
    file_size = os.path.getsize(file_path)
    file_id = random.randint(0, 2**63)
    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    done_chunks = 0
    last_edit = 0
    sem = asyncio.Semaphore(CONCURRENT)

    async def upload_one(idx: int, data: bytes):
        nonlocal done_chunks, last_edit
        async with sem:
            for attempt in range(10): # Retries wadi kala
                try:
                    await user_client(SaveBigFilePartRequest(
                        file_id=file_id, file_part=idx, file_total_parts=total_chunks, bytes=data
                    ))
                    done_chunks += 1
                    now = time.time()
                    if (now - last_edit > 5): # FloodWait nawaththanna limit kala
                        pct = int(done_chunks / total_chunks * 100)
                        await cb(f"⬆️ **Uploading part {part_num}/{total_parts}**\n\n{make_bar(pct)} {pct}%\n📤 {min(done_chunks * CHUNK_SIZE, file_size)//(1024**2)} MB / {file_size//(1024**2)} MB")
                        last_edit = now
                    return
                except Exception:
                    await asyncio.sleep(2)
    
    tasks = []
    with open(file_path, "rb") as f:
        for i in range(total_chunks):
            tasks.append(upload_one(i, f.read(CHUNK_SIZE)))
    await asyncio.gather(*tasks)
    return InputFileBig(id=file_id, parts=total_chunks, name=os.path.basename(file_path))

# ════════════════════════════════════════
# Queue Worker (Piliwelata link handle kirima)
# ════════════════════════════════════════

async def queue_worker():
    global worker_task
    while job_queue:
        url, status_msg = job_queue.popleft()
        
        async def cb(text: str):
            try: await status_msg.edit(text)
            except: pass

        try:
            await cb("🔍 Starting current job...")
            dl_url, fname, size, hdrs = resolve_gofile(url)
            path = await download_file(dl_url, fname, hdrs, cb)
            
            # Splitting
            await cb("✂️ Splitting file...")
            size_raw = os.path.getsize(path)
            n_parts = math.ceil(size_raw / PART_SIZE)
            parts = []
            if n_parts > 1:
                with open(path, "rb") as f:
                    for i in range(n_parts):
                        pname = f"{path}.part{i+1}"
                        with open(pname, "wb") as out:
                            out.write(f.read(PART_SIZE))
                        parts.append(pname)
                safe_remove(path)
            else:
                parts = [path]

            # Uploading
            for i, p in enumerate(parts, 1):
                input_file = await upload_part(p, cb, i, len(parts))
                await user_client(SendMediaRequest(
                    peer="me",
                    media=InputMediaUploadedDocument(
                        file=input_file, mime_type="application/octet-stream",
                        attributes=[DocumentAttributeFilename(os.path.basename(p))]
                    ),
                    message=f"📦 {fname}\n🗂 Part {i}/{len(parts)}",
                    random_id=random.randint(0, 2**63),
                ))
                safe_remove(p)
            
            await cb(f"✅ **Done!**\n`{fname}` saved to Saved Messages.")
        except Exception as e:
            await cb(f"❌ **Error:**\n`{e}`")
        finally:
            queue_urls.discard(url)
            await asyncio.sleep(2) # Break ekak ganna links athara

    worker_task = None

@bot_client.on(events.NewMessage(pattern=r"https://gofile\.io/d/\S+"))
async def gofile_handler(event):
    global worker_task
    url = event.pattern_match.group(0).strip()
    if url in queue_urls: return await event.respond("⚠️ Link eka queue eke thiyenne.")
    
    queue_urls.add(url)
    pos = len(job_queue) + (1 if worker_task else 0)
    status_msg = await event.respond(f"📋 Queued at position: {pos + 1}")
    job_queue.append((url, status_msg))
    
    if worker_task is None or worker_task.done():
        worker_task = asyncio.create_task(queue_worker())

async def main():
    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)
    print("Bot is running...")
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
