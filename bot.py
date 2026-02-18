import os, re, math, asyncio, time, requests, random
from collections import deque
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.functions.messages import SendMediaRequest
from telethon.tl.types import InputFileBig, InputMediaUploadedDocument, DocumentAttributeFilename

API_ID         = int(os.environ.get("TG_API_ID", 0))
API_HASH       = os.environ.get("TG_API_HASH", "")
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
STRING_SESSION = os.environ.get("STRING_SESSION", "")
CHANNEL_ID     = -1003818449922

WORK_DIR   = "downloads"
CHUNK_SIZE = 512 * 1024
PART_SIZE  = 1900 * 1024 * 1024
UA         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
PROXIES    = {"http": "socks5h://127.0.0.1:40000", "https": "socks5h://127.0.0.1:40000"}

os.makedirs(WORK_DIR, exist_ok=True)

user_client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
bot_client  = TelegramClient("bot_session", API_ID, API_HASH)

job_queue      = deque()
worker_running = False

# ── GoFile ────────────────────────────────────────────────────────────────

def get_website_token():
    try:
        r = requests.get(
            "https://gofile.io/dist/js/config.js",
            headers={"User-Agent": UA}, proxies=PROXIES, timeout=15
        )
        if 'appdata.wt = "' in r.text:
            return r.text.split('appdata.wt = "')[1].split('"')[0]
        m = re.search(r'websiteToken["\']?\s*[=:]\s*["\']([^"\']{4,})["\']', r.text)
        return m.group(1) if m else "none"
    except:
        return "none"

def resolve_gofile(page_url):
    cid  = page_url.rstrip("/").split("/d/")[-1]
    hdrs = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://gofile.io/",
        "Origin": "https://gofile.io"
    }

    acc_token = None
    for _ in range(5):
        try:
            r = requests.post(
                "https://api.gofile.io/accounts",
                headers=hdrs, proxies=PROXIES, timeout=20
            ).json()
            if r.get("status") == "ok":
                acc_token = r["data"]["token"]
                break
        except:
            time.sleep(3)

    if not acc_token:
        raise Exception("GoFile guest account creation failed.")

    wt       = get_website_token()
    api_hdrs = {**hdrs, "Authorization": f"Bearer {acc_token}", "X-Website-Token": wt}

    resp = requests.get(
        f"https://api.gofile.io/contents/{cid}?cache=true",
        headers=api_hdrs, proxies=PROXIES, timeout=25
    ).json()

    if resp.get("status") != "ok":
        raise Exception(f"GoFile API Error: {resp.get('status')}")

    data     = resp.get("data", {})
    children = data.get("children", {})

    item = None
    if isinstance(children, dict):
        item = next((v for v in children.values() if v.get("type") == "file"), None)
    elif isinstance(children, list):
        item = next((v for v in children if v.get("type") == "file"), None)

    if not item:
        if data.get("type") == "file":
            item = data
        else:
            raise Exception("No downloadable file found.")

    return item["link"], item["name"], item.get("size", 0), api_hdrs

# ── Upload ────────────────────────────────────────────────────────────────

async def fast_upload(file_path, status_msg, fname):
    file_size   = os.path.getsize(file_path)
    total_parts = math.ceil(file_size / CHUNK_SIZE)
    file_id     = random.randint(0, 2**63)
    sem = asyncio.Semaphore(32)  # 16 → 32
    last_update = 0
    done_parts  = 0

    chunks = []
    with open(file_path, "rb") as f:
        for i in range(total_parts):
            chunks.append((i, f.read(CHUNK_SIZE)))

    async def upload_one(idx, data):
        nonlocal last_update, done_parts
        async with sem:
            for attempt in range(10):
                try:
                    await user_client(SaveBigFilePartRequest(
                        file_id=file_id, file_part=idx,
                        file_total_parts=total_parts, bytes=data
                    ))
                    done_parts += 1
                    break
                except Exception as e:
                    print(f"⚠️ Chunk {idx} attempt {attempt}: {e}")
                    await asyncio.sleep(2 ** min(attempt, 5))

            now = time.time()
            if now - last_update > 5:
                pct = int(done_parts / total_parts * 100)
                try:
                    await status_msg.edit(
                        f"⬆️ **Uploading:** `{fname}`\n\n"
                        f"[{'█'*int(pct/10)}{'░'*(10-int(pct/10))}] {pct}%\n"
                        f"📤 {min(done_parts * CHUNK_SIZE, file_size) // (1024**2)} MB / {file_size // (1024**2)} MB"
                    )
                except:
                    pass
                last_update = now

    await asyncio.gather(*[upload_one(i, d) for i, d in chunks])
    return InputFileBig(id=file_id, parts=total_parts, name=fname)

# ── Worker ────────────────────────────────────────────────────────────────

async def process_job(url, status_msg):
    parts = []
    try:
        await status_msg.edit("🔍 **Analysing link...**")
        dl_url, fname, size, hdrs = resolve_gofile(url)

        # ✅ Stream download + split directly (disk space save, no kill)
        downloaded = 0
        total      = size
        part_num   = 1
        buf        = b""
        last_edit  = 0

        with requests.get(dl_url, headers=hdrs, stream=True, timeout=300) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", total))

            for chunk in r.iter_content(chunk_size=4*1024*1024):
                if chunk:
                    buf        += chunk
                    downloaded += len(chunk)

                    now = time.time()
                    if now - last_edit > 5:
                        pct = int(downloaded / total * 100)
                        spd = downloaded / (now - last_edit + 0.001) / (1024*1024)
                        try:
                            await status_msg.edit(
                                f"⬇️ **Downloading:** `{fname}`\n\n"
                                f"[{'█'*int(pct/10)}{'░'*(10-int(pct/10))}] {pct}%\n"
                                f"📦 {downloaded//(1024**2)} MB / {total//(1024**2)} MB\n"
                                f"⚡ {spd:.1f} MB/s"
                            )
                        except:
                            pass
                        last_edit = now

                    # ✅ Part size hit වුණාම disk write කරලා buf clear කරන්න
                    while len(buf) >= PART_SIZE:
                        pname = os.path.join(WORK_DIR, f"{fname}.part{part_num}")
                        with open(pname, "wb") as f:
                            f.write(buf[:PART_SIZE])
                        parts.append(pname)
                        buf = buf[PART_SIZE:]
                        part_num += 1

            # Remaining data
            if buf:
                pname = os.path.join(WORK_DIR, f"{fname}.part{part_num}")
                with open(pname, "wb") as f:
                    f.write(buf)
                parts.append(pname)

        # Upload parts
        total_parts_count = len(parts)
        for i, p in enumerate(parts, 1):
            pname_display = os.path.basename(p)
            print(f"⬆️ Uploading part {i}/{total_parts_count}: {pname_display}")
            input_file = await fast_upload(p, status_msg, pname_display)
            await user_client(SendMediaRequest(
                peer=CHANNEL_ID,
                media=InputMediaUploadedDocument(
                    file=input_file,
                    mime_type="application/octet-stream",
                    attributes=[DocumentAttributeFilename(pname_display)]
                ),
                message=f"✅ **{fname}**\n🗂 Part {i}/{total_parts_count}\n📦 {size//(1024**2)} MB",
                random_id=random.randint(0, 2**63),
            ))
            if os.path.exists(p):
                os.remove(p)
            print(f"✅ Part {i}/{total_parts_count} uploaded.")

        await status_msg.edit(f"✅ **Done!** `{fname}` uploaded! ({total_parts_count} part{'s' if total_parts_count > 1 else ''})")

    except Exception as e:
        print(f"❌ Error: {e}")
        await status_msg.edit(f"❌ **Error:** {e}")
    finally:
        for p in parts:
            if os.path.exists(p):
                try: os.remove(p)
                except: pass

async def worker():
    global worker_running
    while job_queue:
        worker_running = True
        url, msg = job_queue.popleft()
        await process_job(url, msg)
        await asyncio.sleep(2)
    worker_running = False

# ── Handlers ──────────────────────────────────────────────────────────────

@bot_client.on(events.NewMessage(pattern="/start"))
async def start(event):
    await event.respond("⚡ **GoFile Bot Active!**\nSend a gofile.io link to start.")

@bot_client.on(events.NewMessage(pattern=r"https://gofile\.io/d/\S+"))
async def link_handler(event):
    global worker_running
    url = event.pattern_match.group(0).strip()
    msg = await event.respond("⏳ Added to queue...")
    job_queue.append((url, msg))
    if not worker_running:
        asyncio.create_task(worker())

# ── Main ──────────────────────────────────────────────────────────────────

async def main():
    print("🚀 Starting bot...")
    await user_client.start()
    me = await user_client.get_me()
    print(f"✅ User: {me.first_name} (ID: {me.id})")
    await bot_client.start(bot_token=BOT_TOKEN)
    me = await bot_client.get_me()
    print(f"✅ Bot: @{me.username}")
    print("✅ Bot is running...")
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == "__main__":
    asyncio.run(main())
