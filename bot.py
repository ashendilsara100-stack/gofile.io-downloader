import os
import re
import math
import asyncio
import random
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
CONCURRENT = 6
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
worker_task   = None   # single worker reference


# ════════════════════════════════════════
# GoFile
# ════════════════════════════════════════

def get_website_token() -> str:
    r = requests.get(
        "https://gofile.io/dist/js/config.js",
        headers={"User-Agent": UA}, timeout=15
    )
    r.raise_for_status()
    js = r.text
    if 'appdata.wt = "' in js:
        return js.split('appdata.wt = "')[1].split('"')[0]
    for pat in [
        r'websiteToken["\']?\s*[=:]\s*["\']([^"\']{4,})["\']',
        r'"wt"\s*:\s*"([^"]{4,})"',
    ]:
        m = re.search(pat, js)
        if m:
            return m.group(1)
    raise Exception("GoFile websiteToken nemata.")


def resolve_gofile(page_url: str):
    cid = page_url.rstrip("/").split("/d/")[-1]
    r   = requests.post(
        "https://api.gofile.io/accounts",
        headers={"User-Agent": UA}, timeout=15
    ).json()
    if r.get("status") != "ok":
        raise Exception(f"Guest token fail: {r}")

    wt   = get_website_token()
    hdrs = {
        "Authorization":   f"Bearer {r['data']['token']}",
        "X-Website-Token": wt,
        "User-Agent":      UA,
    }
    resp = requests.get(
        f"https://api.gofile.io/contents/{cid}?cache=true",
        headers=hdrs, timeout=30
    ).json()
    if resp.get("status") != "ok":
        raise Exception(f"GoFile API: {resp.get('status')}")

    data = resp["data"]
    children = data.get("children", {})
    item = next((v for v in children.values() if v.get("type") == "file"), None)
    if not item:
        if data.get("type") == "file":
            item = data
        else:
            raise Exception("File nemata.")

    return item["link"], item["name"], item.get("size", 0), hdrs


# ════════════════════════════════════════
# Helpers
# ════════════════════════════════════════

def make_bar(pct: int, length: int = 12) -> str:
    f = int(length * pct / 100)
    return "[" + "█" * f + "░" * (length - f) + "]"


def safe_remove(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except:
        pass


# ════════════════════════════════════════
# Download
# ════════════════════════════════════════

async def download_file(url: str, fname: str, headers: dict, cb) -> str:
    path = os.path.join(WORK_DIR, fname)
    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        total    = int(r.headers.get("content-length", 0))
        done     = 0
        last_pct = -10
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = int(done / total * 100)
                        if pct - last_pct >= 5:
                            last_pct = pct
                            await cb(
                                f"⬇️ **Downloading...**\n\n"
                                f"{make_bar(pct)} {pct}%\n"
                                f"📦 {done//(1024**2)} MB / {total//(1024**2)} MB"
                            )
    return path


# ════════════════════════════════════════
# Split
# ════════════════════════════════════════

def split_file(path: str) -> list:
    size = os.path.getsize(path)
    n    = math.ceil(size / PART_SIZE)
    if n <= 1:
        return [path]

    parts = []
    with open(path, "rb") as f:
        for i in range(n):
            pname = f"{path}.part{i+1}of{n}"
            with open(pname, "wb") as out:
                rem = PART_SIZE
                while rem > 0:
                    chunk = f.read(min(4 * 1024 * 1024, rem))
                    if not chunk:
                        break
                    out.write(chunk)
                    rem -= len(chunk)
            if os.path.exists(pname) and os.path.getsize(pname) > 0:
                parts.append(pname)

    safe_remove(path)
    return parts


# ════════════════════════════════════════
# Upload
# ════════════════════════════════════════

async def upload_part(file_path: str, cb, part_num: int, total_parts: int) -> InputFileBig:
    file_size    = os.path.getsize(file_path)
    file_id      = random.randint(0, 2**63)
    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    done         = [0]
    sem          = asyncio.Semaphore(CONCURRENT)

    async def upload_one(idx: int, data: bytes):
        async with sem:
            for attempt in range(3):
                try:
                    await user_client(SaveBigFilePartRequest(
                        file_id=file_id,
                        file_part=idx,
                        file_total_parts=total_chunks,
                        bytes=data,
                    ))
                    done[0] += 1
                    if done[0] % max(1, total_chunks // 20) == 0:
                        pct     = int(done[0] / total_chunks * 100)
                        done_mb = min(done[0] * CHUNK_SIZE, file_size) // (1024**2)
                        tot_mb  = file_size // (1024**2)
                        await cb(
                            f"⬆️ **Uploading part {part_num}/{total_parts}**\n\n"
                            f"{make_bar(pct)} {pct}%\n"
                            f"📤 {done_mb} MB / {tot_mb} MB\n"
                            f"⚡ {CONCURRENT} concurrent chunks"
                        )
                    return
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2 ** attempt)

    tasks = []
    with open(file_path, "rb") as f:
        for i in range(total_chunks):
            data = f.read(CHUNK_SIZE)
            if not data:
                break
            tasks.append(upload_one(i, data))

    await asyncio.gather(*tasks)
    return InputFileBig(id=file_id, parts=total_chunks, name=os.path.basename(file_path))


async def upload_all_parts(parts: list, original_name: str, cb):
    total = len(parts)
    for i, p in enumerate(parts, 1):
        size_mb = os.path.getsize(p) // (1024**2)
        await cb(
            f"⬆️ **Uploading part {i}/{total}**\n\n"
            f"{make_bar(0)} 0%\n"
            f"📤 0 MB / {size_mb} MB\n"
            f"⚡ {CONCURRENT} concurrent chunks"
        )
        input_file = await upload_part(p, cb, i, total)
        await user_client(SendMediaRequest(
            peer="me",
            media=InputMediaUploadedDocument(
                file=input_file,
                mime_type="application/octet-stream",
                attributes=[DocumentAttributeFilename(os.path.basename(p))],
            ),
            message=f"📦 {original_name}\n🗂 Part {i}/{total}",
            random_id=random.randint(0, 2**63),
        ))
        safe_remove(p)
        await cb(f"✅ **Part {i}/{total} done!**")


# ════════════════════════════════════════
# Single job processor
# ════════════════════════════════════════

async def process_one(url: str, status_msg):
    """
    Process single job.
    cb closure — status_msg passed as argument (no loop capture bug).
    """
    parts = []

    async def cb(text: str):
        try:
            await status_msg.edit(text)
        except:
            pass

    try:
        q_info = f"\n📋 {len(job_queue)} in queue" if job_queue else ""
        await cb(f"🔍 Resolving...{q_info}")

        dl_url, fname, size, hdrs = resolve_gofile(url)
        await cb(
            f"📄 **{fname}**\n"
            f"💾 {size//(1024**2)} MB\n\n"
            f"{make_bar(0)} 0%\n"
            f"⬇️ Downloading...{q_info}"
        )

        path  = await download_file(dl_url, fname, hdrs, cb)
        parts = split_file(path)
        await upload_all_parts(parts, fname, cb)

        next_info = f"\n\n▶️ Next: `{job_queue[0][0]}`" if job_queue else ""
        await cb(
            f"🎉 **Done!**\n\n"
            f"📦 `{fname}`\n"
            f"🗂 {len(parts)} part(s) → Saved Messages"
            f"{next_info}"
        )

    except Exception as e:
        await cb(f"❌ **Error:**\n`{e}`")

    finally:
        for p in parts:
            safe_remove(p)
        queue_urls.discard(url)


# ════════════════════════════════════════
# Queue worker — guaranteed single instance
# ════════════════════════════════════════

async def queue_worker():
    """
    Single worker loop.
    job_queue ෙ items one by one process කරනවා.
    Worker done වුනාම global worker_task = None.
    """
    global worker_task

    while job_queue:
        url, status_msg = job_queue.popleft()

        # Notify next if queued
        if job_queue:
            try:
                await job_queue[0][1].edit(
                    f"📋 **Queued #{len(job_queue)}**\n\n"
                    f"`{job_queue[0][0]}`\n\n"
                    f"⏳ Current job ඉවර වුනාම start..."
                )
            except:
                pass

        await process_one(url, status_msg)

        # Notify next job starting
        if job_queue:
            try:
                await job_queue[0][1].edit(
                    f"▶️ **Starting now!**\n`{job_queue[0][0]}`"
                )
            except:
                pass

        await asyncio.sleep(0.5)

    worker_task = None


# ════════════════════════════════════════
# Bot handlers
# ════════════════════════════════════════

@bot_client.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    await event.respond(
        "╔═══════════════════════╗\n"
        "║  📦 **GoFile Downloader**  ║\n"
        "╚═══════════════════════╝\n\n"
        "🚀 GoFile → Telegram Saved Messages\n"
        f"⚡ {CONCURRENT}x concurrent upload\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📎 `https://gofile.io/d/XXXXXX`\n\n"
        "📋 Links කිහිපයක් → auto queue\n"
        "📊 /queue — status\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )


@bot_client.on(events.NewMessage(pattern="/queue"))
async def queue_handler(event):
    if not job_queue and worker_task is None:
        return await event.respond("✅ Queue empty — bot ready!")
    lines = [f"📋 **Queue** ({len(job_queue)} waiting)\n"]
    if worker_task is not None:
        lines.insert(1, "🔄 1 job processing now\n")
    for i, (url, _) in enumerate(job_queue, 1):
        lines.append(f"{i}. `{url}`")
    await event.respond("\n".join(lines))


@bot_client.on(events.NewMessage(pattern=r"https://gofile\.io/d/\S+"))
async def gofile_handler(event):
    global worker_task

    url = event.pattern_match.group(0).strip()

    # Duplicate check
    if url in queue_urls:
        return await event.respond(f"⚠️ Already queued!\n`{url}`")

    queue_urls.add(url)

    pos = len(job_queue) + (1 if worker_task is not None else 0)

    if pos == 0:
        status_msg = await event.respond("🔍 Starting...")
    else:
        status_msg = await event.respond(
            f"📋 **Queued #{pos + 1}**\n\n"
            f"`{url}`\n\n"
            f"⏳ {pos} job(s) ඉවර වුනාම start!"
        )

    job_queue.append((url, status_msg))

    # Single worker guarantee — already running නම් spawn නොකරයි
    if worker_task is None or worker_task.done():
        worker_task = asyncio.create_task(queue_worker())


# ════════════════════════════════════════
# Main
# ════════════════════════════════════════

async def main():
    await user_client.start()
    print("[✓] User client connected.")

    await bot_client.start(bot_token=BOT_TOKEN)
    me = await bot_client.get_me()
    print(f"[✓] Bot @{me.username} running!")
    print(f"[*] Concurrent chunks : {CONCURRENT}")
    print(f"[*] Part size         : {PART_SIZE//(1024**2)} MB")
    print("[*] Waiting for links...")

    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())