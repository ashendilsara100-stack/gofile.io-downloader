import os
import re
import math
import asyncio
import random
import requests
from collections import deque
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
STRING_SESSION = os.environ.get("STRING_SESSION")

# ── Config ─────────────────────────────────────────────────────────────────
PART_SIZE    = 1990 * 1024 * 1024
UPLOAD_CHUNK = 512 * 1024
PARALLEL     = 1          # Single connection — session conflict නෑ
UA           = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
WORK_DIR     = "/tmp/gofile_downloads"

os.makedirs(WORK_DIR, exist_ok=True)

# ── Clients ────────────────────────────────────────────────────────────────
if STRING_SESSION:
    user_client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
else:
    user_client = TelegramClient("user_session", API_ID, API_HASH)

bot_client = TelegramClient("bot_session", API_ID, API_HASH)

# ── Queue ──────────────────────────────────────────────────────────────────
# (url, status_message) tuples
job_queue    = deque()
queue_urls   = set()     # duplicate check
is_processing = False    # single worker flag


# ════════════════════════════════════════
# GoFile helpers
# ════════════════════════════════════════

def get_website_token() -> str:
    resp = requests.get(
        "https://gofile.io/dist/js/config.js",
        headers={"User-Agent": UA}, timeout=15
    )
    resp.raise_for_status()
    js = resp.text
    if 'appdata.wt = "' in js:
        return js.split('appdata.wt = "')[1].split('"')[0]
    for pat in [
        r'websiteToken["\']?\s*[=:]\s*["\']([^"\']{4,})["\']',
        r'"wt"\s*:\s*"([^"]{4,})"',
    ]:
        m = re.search(pat, js)
        if m: return m.group(1)
    raise Exception("GoFile websiteToken nemata.")


def resolve_gofile(page_url: str):
    content_id = page_url.rstrip("/").split("/d/")[-1]
    r = requests.post(
        "https://api.gofile.io/accounts",
        headers={"User-Agent": UA}, timeout=15
    ).json()
    if r.get("status") != "ok":
        raise Exception(f"Guest token fail: {r}")
    guest_token = r["data"]["token"]
    wt = get_website_token()
    hdrs = {
        "Authorization": f"Bearer {guest_token}",
        "X-Website-Token": wt,
        "User-Agent": UA,
    }
    resp = requests.get(
        f"https://api.gofile.io/contents/{content_id}?cache=true",
        headers=hdrs, timeout=30
    ).json()
    if resp.get("status") != "ok":
        raise Exception(f"GoFile API: {resp.get('status')}")
    data = resp["data"]
    children = data.get("children", {})
    item = next((v for v in children.values() if v.get("type") == "file"), None)
    if not item:
        if data.get("type") == "file": item = data
        else: raise Exception("File nemata.")
    return item["link"], item["name"], item.get("size", 0), hdrs


# ════════════════════════════════════════
# Progress bar
# ════════════════════════════════════════

def make_bar(pct: int, length: int = 12) -> str:
    filled = int(length * pct / 100)
    return "[" + "█" * filled + "░" * (length - filled) + "]"


# ════════════════════════════════════════
# Download
# ════════════════════════════════════════

async def download_file(url: str, filename: str, headers: dict,
                        status_cb=None) -> str:
    path = os.path.join(WORK_DIR, filename)
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
                    if total and status_cb:
                        pct = int(done / total * 100)
                        if pct - last_pct >= 5:
                            last_pct = pct
                            await status_cb(
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
    if n <= 1: return [path]
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


# ════════════════════════════════════════
# Upload — part upload karala immediately delete
# ════════════════════════════════════════

async def upload_large_file(file_path: str, status_cb=None,
                             part_num: int = 1, total_parts: int = 1) -> InputFileBig:
    file_size    = os.path.getsize(file_path)
    file_id      = random.randint(0, 2**63)
    total_chunks = (file_size + UPLOAD_CHUNK - 1) // UPLOAD_CHUNK
    done_chunks  = [0]

    with open(file_path, "rb") as f:
        for idx in range(total_chunks):
            data = f.read(UPLOAD_CHUNK)
            if not data: break

            # Retry 3x
            for attempt in range(3):
                try:
                    await user_client(SaveBigFilePartRequest(
                        file_id=file_id,
                        file_part=idx,
                        file_total_parts=total_chunks,
                        bytes=data,
                    ))
                    break
                except Exception as e:
                    if attempt == 2: raise e
                    await asyncio.sleep(2 ** attempt)

            done_chunks[0] += 1
            if status_cb and done_chunks[0] % max(1, total_chunks // 20) == 0:
                pct = int(done_chunks[0] / total_chunks * 100)
                await status_cb(
                    f"⬆️ **Uploading part {part_num}/{total_parts}**\n\n"
                    f"{make_bar(pct)} {pct}%\n"
                    f"📤 {min(done_chunks[0] * UPLOAD_CHUNK, file_size)//(1024**2)} MB"
                    f" / {file_size//(1024**2)} MB"
                )

    return InputFileBig(
        id=file_id,
        parts=total_chunks,
        name=os.path.basename(file_path),
    )


async def upload_parts(parts: list, original_name: str,
                        original_file: str, status_cb=None):
    """
    Part upload කරලා immediately delete — disk space free කරනවා.
    Original file ත් split ඉවර වුනාම delete.
    """
    total = len(parts)

    # Original file delete (parts already created)
    if len(parts) > 1 and os.path.exists(original_file):
        os.remove(original_file)

    for i, p in enumerate(parts, 1):
        size_mb = os.path.getsize(p) // (1024**2)
        if status_cb:
            await status_cb(
                f"⬆️ **Uploading part {i}/{total}**\n\n"
                f"{make_bar(0)} 0%\n"
                f"📤 0 MB / {size_mb} MB"
            )

        input_file = await upload_large_file(p, status_cb, i, total)

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

        # Part upload ඉවර — immediately delete ✅
        try:
            os.remove(p)
        except:
            pass

        if status_cb:
            await status_cb(f"✅ **Part {i}/{total} done!**\n📦 {os.path.basename(p)}")


# ════════════════════════════════════════
# Queue worker — one job at a time
# ════════════════════════════════════════

async def queue_worker():
    """
    Background worker — queue ෙ jobs one by one process කරනවා.
    Job ඉවර වුනාම next auto start.
    """
    global is_processing

    while True:
        if not job_queue:
            is_processing = False
            return  # Queue empty — worker stop

        url, status_msg, chat_id = job_queue.popleft()
        original_file = None
        parts = []

        async def update_status(text: str):
            try:
                await status_msg.edit(text)
            except:
                pass

        try:
            # Queue remaining count show
            remaining = len(job_queue)
            queue_info = f"\n\n📋 Queue: {remaining} waiting" if remaining > 0 else ""

            await update_status(f"🔍 Resolving...{queue_info}")
            dl_url, fname, size, hdrs = resolve_gofile(url)

            await update_status(
                f"📄 **{fname}**\n"
                f"💾 {size//(1024**2)} MB\n\n"
                f"{make_bar(0)} 0%\n"
                f"⬇️ Downloading...{queue_info}"
            )

            original_file = await download_file(dl_url, fname, hdrs, update_status)
            parts = split_file(original_file)

            await upload_parts(parts, fname, original_file, update_status)

            # Done — next queue item info
            next_info = ""
            if job_queue:
                next_info = f"\n\n▶️ Next: `{job_queue[0][0]}`"

            await update_status(
                f"✅ **Done!**\n\n"
                f"📦 `{fname}`\n"
                f"🗂 {len(parts)} part(s) → Saved Messages"
                f"{next_info}"
            )

        except Exception as e:
            await update_status(f"❌ **Error:**\n`{e}`")

        finally:
            # Cleanup any remaining files
            for f in ([original_file] if original_file else []) + parts:
                if f and os.path.exists(f):
                    try: os.remove(f)
                    except: pass

            queue_urls.discard(url)

            # Next job ෙ queue notify
            if job_queue:
                next_url, next_msg, _ = job_queue[0]
                try:
                    await next_msg.edit(
                        f"▶️ **Starting now!**\n`{next_url}`"
                    )
                except:
                    pass

            await asyncio.sleep(1)  # Brief pause between jobs


# ════════════════════════════════════════
# Bot handlers
# ════════════════════════════════════════

@bot_client.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    await event.respond(
        "╔═══════════════════════╗\n"
        "║  📦 **GoFile Downloader**  ║\n"
        "╚═══════════════════════╝\n\n"
        "🚀 GoFile links auto download → Saved Messages!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📎 Link send කරන්න:\n"
        "`https://gofile.io/d/XXXXXX`\n\n"
        "📋 Links කිහිපයක් දෙනකොට\n"
        "    auto queue ලා one by one process!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 /queue — queue status බලන්න"
    )


@bot_client.on(events.NewMessage(pattern="/queue"))
async def queue_handler(event):
    if not job_queue and not is_processing:
        return await event.respond("✅ Queue empty — bot ready!")

    lines = [f"📋 **Queue Status** ({len(job_queue)} waiting)\n"]
    for i, (url, _, _) in enumerate(job_queue, 1):
        lines.append(f"{i}. `{url}`")

    if is_processing:
        lines.insert(1, "🔄 Currently processing 1 job\n")

    await event.respond("\n".join(lines))


@bot_client.on(events.NewMessage(pattern=r"https://gofile\.io/d/\S+"))
async def gofile_handler(event):
    global is_processing

    url     = event.pattern_match.group(0).strip()
    chat_id = event.chat_id

    # Duplicate check
    if url in queue_urls:
        return await event.respond(
            f"⚠️ **Already in queue!**\n`{url}`"
        )

    queue_urls.add(url)
    pos = len(job_queue) + (1 if is_processing else 0)

    if pos == 0:
        # Directly start
        status_msg = await event.respond(f"🔍 Starting: `{url}`")
    else:
        # Queue ෙ add
        status_msg = await event.respond(
            f"📋 **Queued! Position #{pos + 1}**\n\n"
            f"`{url}`\n\n"
            f"⏳ {pos} job(s) ඉවර වුනාම start වෙයි..."
        )

    job_queue.append((url, status_msg, chat_id))

    # Worker start (already running නම් skip)
    if not is_processing:
        is_processing = True
        asyncio.create_task(queue_worker())


# ════════════════════════════════════════
# Main
# ════════════════════════════════════════

async def main():
    await user_client.start()
    print("[✓] User client connected.")

    await bot_client.start(bot_token=BOT_TOKEN)
    me = await bot_client.get_me()
    print(f"[✓] Bot @{me.username} running!")
    print("[*] Queue mode: one job at a time")
    print("[*] Waiting for GoFile links...")

    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())