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

# --- CONFIGURATION ---
API_ID         = int(os.environ["TG_API_ID"])
API_HASH       = os.environ["TG_API_HASH"]
BOT_TOKEN      = os.environ["BOT_TOKEN"]
STRING_SESSION = os.environ.get("STRING_SESSION")
# ඔයාගේ Channel ID එක මෙතනට දාන්න (E.g: -1001234567890)
CHANNEL_ID     = -1003818449922 

WORK_DIR = "downloads"
os.makedirs(WORK_DIR, exist_ok=True)

# Clients
user_client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
bot_client = TelegramClient("bot_session", API_ID, API_HASH)

job_queue = deque()
worker_running = False

# --- PROGRESS UI ---
def make_progress_bar(current, total):
    pct = (current / total) * 100
    completed = int(pct / 10)
    return f"[{'■' * completed}{'□' * (10 - completed)}] {pct:.1f}%"

async def progress_callback(current, total, event, text):
    # විනාඩියකට සැරයක් වගේ update කරන්න (Flood wait මගහැරීමට)
    if not hasattr(progress_callback, "last_edit"):
        progress_callback.last_edit = 0
    
    now = time.time()
    if now - progress_callback.last_edit < 4:
        return
        
    bar = make_progress_bar(current, total)
    await event.edit(f"{text}\n\n{bar}\n📦 {current//(1024**2)}MB / {total//(1024**2)}MB")
    progress_callback.last_edit = now

# --- GOFILE LOGIC ---
def get_website_token():
    try:
        r = requests.get("https://gofile.io/dist/js/config.js", timeout=10)
        return re.search(r'websiteToken["\']?\s*[=:]\s*["\']([^"\']{4,})["\']', r.text).group(1)
    except: return "none"

def resolve_gofile(page_url):
    cid = page_url.rstrip("/").split("/d/")[-1]
    r = requests.post("https://api.gofile.io/accounts", timeout=10).json()
    token = r['data']['token']
    wt = get_website_token()
    headers = {"Authorization": f"Bearer {token}", "X-Website-Token": wt}
    resp = requests.get(f"https://api.gofile.io/contents/{cid}?cache=true", headers=headers).json()
    if resp.get("status") != "ok": raise Exception("GoFile API error.")
    children = resp["data"].get("children", {})
    item = next((v for v in children.values() if v.get("type") == "file"), None)
    if not item: item = resp["data"] if resp["data"].get("type") == "file" else None
    return item["link"], item["name"], item.get("size", 0), headers

# --- CORE WORKER ---
async def process_job(url, status_msg):
    try:
        await status_msg.edit("🔍 Analysing GoFile Link...")
        dl_url, fname, size, hdrs = resolve_gofile(url)
        path = os.path.join(WORK_DIR, fname)

        # Download with Progress
        await status_msg.edit(f"📥 **Downloading:** `{fname}`")
        with requests.get(dl_url, headers=hdrs, stream=True) as r:
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            downloaded = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=2*1024*1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    await progress_callback(downloaded, total_size, status_msg, f"📥 **Downloading:** `{fname}`")

        # Upload with Progress
        await status_msg.edit(f"⬆️ **Uploading:** `{fname}`")
        file_handle = await user_client.upload_file(
            path, 
            progress_callback=lambda c, t: progress_callback(c, t, status_msg, f"⬆️ **Uploading:** `{fname}`")
        )

        await user_client.send_file(
            CHANNEL_ID, 
            file_handle, 
            caption=f"✅ **File:** `{fname}`\n📦 **Size:** {size//(1024**2)} MB",
            force_document=True
        )

        await status_msg.edit(f"🚀 **Success!**\n`{fname}` uploaded to Channel.")
        if os.path.exists(path): os.remove(path)

    except Exception as e:
        await status_msg.edit(f"❌ **Error:** {str(e)}")

async def worker():
    global worker_running
    worker_running = True
    while job_queue:
        url, msg = job_queue.popleft()
        await process_job(url, msg)
    worker_running = False

# --- HANDLERS ---
@bot_client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.respond("👋 **GoFile Downloader Bot Active!**\nSend me a link to upload to channel.\n\n/queue - Check status")

@bot_client.on(events.NewMessage(pattern='/queue'))
async def queue_status(event):
    if not job_queue: return await event.respond("✅ Queue is empty.")
    msg = "📋 **Current Queue:**\n"
    for i, (url, _) in enumerate(job_queue, 1):
        msg += f"{i}. `{url.split('/')[-1]}`\n"
    await event.respond(msg)

@bot_client.on(events.NewMessage(pattern=r"https://gofile\.io/d/\S+"))
async def link_handler(event):
    global worker_running
    url = event.pattern_match.group(0)
    msg = await event.respond("⏳ Adding to queue...")
    job_queue.append((url, msg))
    if not worker_running:
        asyncio.create_task(worker())

async def main():
    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)
    print("💎 Bot Online with Progress Bar & Channel Support!")
    await bot_client.run_until_disconnected()

asyncio.run(main())