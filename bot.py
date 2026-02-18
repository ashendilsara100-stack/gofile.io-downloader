import os, re, math, asyncio, time, requests, random
from collections import deque
from telethon import TelegramClient, events, helpers
from telethon.sessions import StringSession
from telethon.tl.functions.upload import SaveBigFilePartRequest

# --- CONFIGURATION ---
API_ID         = int(os.environ.get("TG_API_ID", 0)) 
API_HASH       = os.environ.get("TG_API_HASH", "")
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
STRING_SESSION = os.environ.get("STRING_SESSION", "")
CHANNEL_ID     = -1003818449922 

# Warp Proxy Settings (Cloudflare Warp needs to be running)
PROXIES = {
    "http": "socks5h://127.0.0.1:40000",
    "https": "socks5h://127.0.0.1:40000"
}

WORK_DIR = "downloads"
os.makedirs(WORK_DIR, exist_ok=True)

user_client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
bot_client = TelegramClient("bot_session", API_ID, API_HASH)

job_queue = deque()
worker_running = False

# --- SPEED OPTIMIZED UPLOADER ---
async def fast_upload(client, file_path, status_msg, fname):
    file_size = os.path.getsize(file_path)
    chunk_size = 512 * 1024
    total_parts = math.ceil(file_size / chunk_size)
    file_id = random.randint(0, 2**63)
    
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
            
            now = time.time()
            if now - last_update > 5:
                pct = (part_index / total_parts) * 100
                try: await status_msg.edit(f"⬆️ **Fast Uploading:** `{fname}`\n\n[{'■'*int(pct/10)}{'□'*(10-int(pct/10))}] {pct:.1f}%")
                except: pass
                last_update = now

    tasks = []
    with open(file_path, "rb") as f:
        for i in range(total_parts):
            part_data = f.read(chunk_size)
            tasks.append(upload_part(i, part_data))
    
    await asyncio.gather(*tasks)
    return helpers.create_input_file(file_id, total_parts, fname, file_size)

# --- GOFILE LOGIC (WARP ENABLED & IMPROVED) ---
def get_website_token():
    try:
        # User-agent එකක් නැතිව සමහරවිට config file එක දෙන්නේ නැහැ
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}
        r = requests.get("https://gofile.io/dist/js/config.js", proxies=PROXIES, headers=headers, timeout=15)
        m = re.search(r'websiteToken["\']?\s*[=:]\s*["\']([^"\']{4,})["\']', r.text)
        return m.group(1) if m else "none"
    except: return "none"

def resolve_gofile(page_url):
    try:
        cid = page_url.rstrip("/").split("/d/")[-1]
        sess = requests.Session()
        sess.proxies.update(PROXIES) 
        
        # Browser එකක් ලෙස හැසිරීමට Headers එකතු කිරීම
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })

        # 1. Guest Account එකක් සාදා ගැනීම
        acc_resp = sess.post("https://api.gofile.io/accounts", timeout=20).json()
        if acc_resp.get("status") != "ok":
            raise Exception("API blocked account creation.")
        token = acc_resp['data']['token']
        
        # 2. Website Token ලබා ගැනීම
        wt = get_website_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Website-Token": wt,
            "Origin": "https://gofile.io",
            "Referer": f"https://gofile.io/d/{cid}"
        }
        
        # 3. Content විස්තර ලබා ගැනීම
        resp = sess.get(f"https://api.gofile.io/contents/{cid}?cache=true", headers=headers, timeout=25).json()
        
        if resp.get("status") != "ok":
            status_info = resp.get("status", "Unknown Error")
            raise Exception(f"Gofile API Error: {status_info}")
            
        data = resp.get("data", {})
        children = data.get("children", {})
        
        # Children හසුරුවන ආකාරය (Dict or List)
        item = None
        if isinstance(children, dict):
            item = next((v for v in children.values() if v.get("type") == "file"), None)
        elif isinstance(children, list):
            item = next((v for v in children if v.get("type") == "file"), None)
        
        # Folder එකක් නම් ඒක ඇතුළේ ඇති පළමු file එක
        if not item and data.get("type") == "file":
            item = data
            
        if not item:
            raise Exception("No file found. Link might be empty or password protected.")
            
        return item["link"], item["name"], item.get("size", 0), headers
    except Exception as e:
        raise Exception(f"Resolve Error: {str(e)}")

# --- WORKER ---
async def process_job(url, status_msg):
    path = ""
    try:
        await status_msg.edit("🔍 **Analysing Link (via Warp)...**")
        dl_url, fname, size, hdrs = resolve_gofile(url)
        path = os.path.join(WORK_DIR, fname)

        # Download via Warp Proxy
        await status_msg.edit(f"📥 **Downloading:** `{fname}`")
        with requests.get(dl_url, headers=hdrs, stream=True, timeout=60, proxies=PROXIES) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=5*1024*1024): # 5MB buffer
                    if chunk: f.write(chunk)

        # Fast Upload to Telegram
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
        if path and os.path.exists(path): 
            try: os.remove(path)
            except: pass

async def worker():
    global worker_running
    while job_queue:
        worker_running = True
        url, msg = job_queue.popleft()
        await process_job(url, msg)
        await asyncio.sleep(2)
    worker_running = False

# --- COMMANDS ---
@bot_client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.respond("⚡ **Warp-Powered GoFile Bot Active!**\nSend link to start.")

@bot_client.on(events.NewMessage(pattern=r"https://gofile\.io/d/\S+"))
async def link_handler(event):
    global worker_running
    url = event.pattern_match.group(0).strip()
    msg = await event.respond("⏳ Adding to Warp Queue...")
    job_queue.append((url, msg))
    if not worker_running: asyncio.create_task(worker())

async def main():
    print("🚀 Connecting Clients...")
    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)
    print("💎 Bot Online with Warp Proxy!")
    await asyncio.gather(user_client.run_until_disconnected(), bot_client.run_until_disconnected())

if __name__ == "__main__":
    asyncio.run(main())