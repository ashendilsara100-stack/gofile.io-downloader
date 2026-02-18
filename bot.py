import os
import re
import asyncio
import time
import requests
import random
from collections import deque
from telethon import TelegramClient, events, helpers
from telethon.sessions import StringSession
from telethon.tl.functions.upload import SaveBigFilePartRequest

# --- CONFIGURATION ---
API_ID         = int(os.environ["TG_API_ID"])
API_HASH       = os.environ["TG_API_HASH"]
BOT_TOKEN      = os.environ["BOT_TOKEN"]
STRING_SESSION = os.environ.get("STRING_SESSION")
CHANNEL_ID     = -1003818449922 

WORK_DIR = "downloads"
os.makedirs(WORK_DIR, exist_ok=True)

# Clients
user_client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
bot_client = TelegramClient("bot_session", API_ID, API_HASH)

job_queue = deque()
worker_running = False

# --- SPEED OPTIMIZED UPLOADER ---
async def fast_upload(client, file_path, status_msg, fname):
    file_size = os.path.getsize(file_path)
    # Chunks size (512KB for big files is standard)
    chunk_size = 512 * 1024
    total_parts = math.ceil(file_size / chunk_size)
    file_id = random.randint(0, 2**63)
    
    # Concurrent connections (Max 8 is safe for Oracle)
    semaphore = asyncio.Semaphore(8)
    last_update = 0

    async def upload_part(part_index, part_data):
        nonlocal last_update
        async with semaphore:
            for attempt in range(5):
                try:
                    await client(SaveBigFilePartRequest(
                        file_id=file_id,
                        file_part=part_index,
                        file_total_parts=total_parts,
                        bytes=part_data
                    ))
                    break
                except Exception:
                    await asyncio.sleep(2)
            
            # Update Progress every 5 seconds
            now = time.time()
            if now - last_update > 5:
                pct = (part_index / total_parts) * 100
                await status_msg.edit(f"⬆️ **Fast Uploading:** `{fname}`\n\n[{'■'*int(pct/10)}{'□'*(10-int(pct/10))}] {pct:.1f}%")
                last_update = now

    tasks = []
    with open(file_path, "rb") as f:
        for i in range(total_parts):
            part_data = f.read(chunk_size)
            tasks.append(upload_part(i, part_data))
    
    await asyncio.gather(*tasks)
    return helpers.create_input_file(file_id, total_parts, fname, file_size)

# --- GOFILE LOGIC ---
def get_website_token():
    try:
        r = requests.get("https://gofile.io/dist/js/config.js", timeout=15)
        m = re.search(r'websiteToken["\']?\s*[=:]\s*["\']([^"\']{4,})["\']', r.text)
        return m.group(1) if m else "none"
    except: return "none"

def resolve_gofile(page_url):
    cid = page_url.rstrip("/").split("/d/")[-1]
    r = requests.post("https://api.gofile.io/accounts", timeout=15).json()
    token = r['data']['token']
    wt = get_website_token()
    headers = {"Authorization": f"Bearer {token}", "X-Website-Token": wt}
    resp = requests.get(f"https://api.gofile.io/contents/{cid}?cache=true", headers=headers, timeout=20).json()
    if resp.get("status") != "ok": raise Exception("Link is invalid.")
    item = next((v for v in resp["data"].get("children", {}).values() if v.get("type") == "file"), resp["data"])
    return item["link"], item["name"], item.get("size", 0), headers

# --- WORKER ---
async def process_job(url, status_msg):
    path = ""
    try:
        await status_msg.edit("🔍 Analysing Link...")
        dl_url, fname, size, hdrs = resolve_gofile(url)
        path = os.path.join(WORK_DIR, fname)

        # Download with larger buffer (5MB)
        await status_msg.edit(f"📥 **Downloading:** `{fname}`")
        with requests.get(dl_url, headers=hdrs, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=5*1024*1024):
                    if chunk: f.write(chunk)

        # Parallel Upload
        input_file = await fast_upload(user_client, path, status_msg, fname)
        
        await user_client.send_file(
            CHANNEL_ID,
            file=input_file,
            caption=f"✅ **File:** `{fname}`\n📦 **Size:** {size//(1024**2)} MB",
            force_document=True
        )
        await status_msg.edit(f"🚀 **Success!** Uploaded to Channel.")

    except Exception as e:
        await status_msg.edit(f"❌ **Error:** {str(e)}")
    finally:
        if path and os.path.exists(path): os.remove(path)

async def worker():
    global worker_running
    while job_queue:
        worker_running = True
        url, msg = job_queue.popleft()
        await process_job(url, msg)
    worker_running = False

# --- COMMANDS ---
@bot_client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.respond("⚡ **High-Speed GoFile Bot Active!**\nSend link to start.")

@bot_client.on(events.NewMessage(pattern='/queue'))
async def queue_status(event):
    await event.respond(f"📋 **Queue Count:** {len(job_queue)}")

@bot_client.on(events.NewMessage(pattern=r"https://gofile\.io/d/\S+"))
async def link_handler(event):
    global worker_running
    url = event.pattern_match.group(0).strip()
    msg = await event.respond("⏳ Adding to Fast Queue...")
    job_queue.append((url, msg))
    if not worker_running: asyncio.create_task(worker())

async def main():
    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)
    await asyncio.gather(user_client.run_until_disconnected(), bot_client.run_until_disconnected())

if __name__ == "__main__":
    import math
    asyncio.run(main())