import os
import re
import math
import asyncio
import random
import requests
from telethon import TelegramClient, events, Button
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.functions.messages import SendMediaRequest
from telethon.tl.types import (
    InputFileBig,
    InputMediaUploadedDocument,
    DocumentAttributeFilename,
)

# ── ENV variables ──────────────────────────────────────────────────────────
API_ID     = int(os.environ["TG_API_ID"])
API_HASH   = os.environ["TG_API_HASH"]
BOT_TOKEN  = os.environ["BOT_TOKEN"]
OWNER_ID   = int(os.environ["OWNER_ID"])
USER_PHONE = os.environ["TG_PHONE"]

# ── Config ─────────────────────────────────────────────────────────────────
PART_SIZE    = 1990 * 1024 * 1024   # 1990 MB per part
UPLOAD_CHUNK = 2048 * 1024          # 2 MB chunks (faster upload)
UA           = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
WORK_DIR     = "/tmp/gofile_downloads"

os.makedirs(WORK_DIR, exist_ok=True)

bot_client  = TelegramClient("bot_session",  API_ID, API_HASH)
user_client = TelegramClient("user_session", API_ID, API_HASH)


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
        if m:
            return m.group(1)
    raise Exception("GoFile websiteToken config.js eke nemata.")


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

    data     = resp["data"]
    children = data.get("children", {})
    item     = next((v for v in children.values() if v.get("type") == "file"), None)
    if not item:
        if data.get("type") == "file":
            item = data
        else:
            raise Exception("File nemata.")

    return item["link"], item["name"], item.get("size", 0), hdrs


# ════════════════════════════════════════
# Progress bar helper
# ════════════════════════════════════════

def make_progress_bar(pct: int, length: int = 12) -> str:
    filled = int(length * pct / 100)
    bar    = "█" * filled + "░" * (length - filled)
    return f"[{bar}]"


# ════════════════════════════════════════
# Download
# ════════════════════════════════════════

async def download_file(url: str, filename: str, headers: dict,
                        status_cb=None) -> str:
    path = os.path.join(WORK_DIR, filename)
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
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
                            bar = make_progress_bar(pct)
                            await status_cb(
                                f"⬇️ **Downloading...**\n\n"
                                f"{bar} {pct}%\n"
                                f"📦 {done//(1024**2)} MB / {total//(1024**2)} MB"
                            )
    return path


# ════════════════════════════════════════
# Split
# ════════════════════════════════════════

def split_file(path: str) -> list:
    size = os.path.getsize(path)
    n    = math.ceil(size / PART_SIZE)
    if n == 1:
        return [path]

    parts = []
    with open(path, "rb") as f:
        for i in range(n):
            pname = f"{path}.part{i+1}of{n}"
            with open(pname, "wb") as out:
                remaining = PART_SIZE
                while remaining > 0:
                    chunk = f.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            if os.path.getsize(pname) == 0:
                os.remove(pname)
                continue
            parts.append(pname)
    return parts


# ════════════════════════════════════════
# Upload — live MB progress
# ════════════════════════════════════════

async def upload_large_file(file_path: str, status_cb=None,
                             part_idx: int = 1, total_parts: int = 1) -> InputFileBig:
    file_size      = os.path.getsize(file_path)
    file_id        = random.randint(0, 2**63)
    total_chunks   = math.ceil(file_size / UPLOAD_CHUNK)
    uploaded_bytes = 0
    last_pct       = -10

    with open(file_path, "rb") as f:
        for idx in range(total_chunks):
            chunk = f.read(UPLOAD_CHUNK)
            if not chunk:
                break
            await user_client(SaveBigFilePartRequest(
                file_id=file_id,
                file_part=idx,
                file_total_parts=total_chunks,
                bytes=chunk,
            ))
            uploaded_bytes += len(chunk)

            if status_cb:
                pct = int(uploaded_bytes / file_size * 100)
                if pct - last_pct >= 5:
                    last_pct = pct
                    bar = make_progress_bar(pct)
                    await status_cb(
                        f"⬆️ **Uploading part {part_idx}/{total_parts}**\n\n"
                        f"{bar} {pct}%\n"
                        f"📤 {uploaded_bytes//(1024**2)} MB / {file_size//(1024**2)} MB"
                    )

    return InputFileBig(
        id=file_id,
        parts=total_chunks,
        name=os.path.basename(file_path),
    )


async def upload_parts(parts: list, original_name: str, status_cb=None):
    total = len(parts)
    for i, p in enumerate(parts, 1):
        fname   = os.path.basename(p)
        size_mb = os.path.getsize(p) // (1024 ** 2)
        caption = f"📦 {original_name}\n🗂 Part {i}/{total}"

        if status_cb:
            await status_cb(
                f"⬆️ **Uploading part {i}/{total}**\n\n"
                f"{make_progress_bar(0)} 0%\n"
                f"📤 0 MB / {size_mb} MB"
            )

        input_file = await upload_large_file(p, status_cb, i, total)

        await user_client(SendMediaRequest(
            peer="me",
            media=InputMediaUploadedDocument(
                file=input_file,
                mime_type="application/octet-stream",
                attributes=[DocumentAttributeFilename(fname)],
            ),
            message=caption,
            random_id=random.randint(0, 2**63),
        ))

        if status_cb:
            await status_cb(f"✅ **Part {i}/{total} uploaded!**\n📦 {fname}")


# ════════════════════════════════════════
# Cleanup
# ════════════════════════════════════════

def cleanup(original: str, parts: list):
    targets = set(parts)
    if original not in parts:
        targets.add(original)
    for f in targets:
        try:
            os.remove(f)
        except:
            pass


# ════════════════════════════════════════
# Bot handlers
# ════════════════════════════════════════

active_jobs = {}


@bot_client.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    buttons = [
        [
            Button.inline("📖 How to Use", b"howto"),
            Button.url("👨‍💻 Developer", "https://t.me/ashencode"),
        ],
        [
            Button.inline("ℹ️ About", b"about"),
        ],
    ]
    await event.respond(
        "╔═══════════════════════╗\n"
        "║  📦 **GoFile Downloader**  ║\n"
        "╚═══════════════════════╝\n\n"
        "🚀 GoFile links auto download කරලා\n"
        "    ඔබේ **Saved Messages** ෙ upload කරනවා!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📎 Link send කරන්න:\n"
        "`https://gofile.io/d/XXXXXX`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━",
        buttons=buttons,
    )


@bot_client.on(events.CallbackQuery(data=b"howto"))
async def howto_handler(event):
    await event.answer()
    await event.respond(
        "📖 **How to Use**\n\n"
        "**1️⃣** GoFile share link copy කරන්න\n"
        "       `https://gofile.io/d/XXXXXX`\n\n"
        "**2️⃣** Bot ෙකට paste කරලා send කරන්න\n\n"
        "**3️⃣** Bot auto:\n"
        "       ⬇️ Download\n"
        "       ✂️ 2GB parts walata split\n"
        "       ⬆️ Saved Messages upload\n\n"
        "**4️⃣** Done! ✅\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 Progress live ලා පෙනෙනවා!",
        buttons=[[Button.inline("🔙 Back", b"back")]],
    )


@bot_client.on(events.CallbackQuery(data=b"about"))
async def about_handler(event):
    await event.answer()
    await event.respond(
        "ℹ️ **About**\n\n"
        "🤖 **GoFile → Telegram Bot**\n\n"
        "✅ GoFile auto download\n"
        "✅ 2GB parts support\n"
        "✅ Live progress bar\n"
        "✅ Fast upload\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👨‍💻 Dev: @ashencode",
        buttons=[[Button.inline("🔙 Back", b"back")]],
    )


@bot_client.on(events.CallbackQuery(data=b"back"))
async def back_handler(event):
    await event.answer()
    await start_handler(event)


@bot_client.on(events.NewMessage(pattern=r"https://gofile\.io/d/\S+"))
async def gofile_handler(event):
    chat_id = event.chat_id

    if active_jobs.get(chat_id):
        return await event.respond("⏳ Job ekak already running. Wait karanna.")

    active_jobs[chat_id] = True
    url        = event.pattern_match.group(0).strip()
    status_msg = await event.respond(f"🔍 Processing: `{url}`")

    async def update_status(text: str):
        try:
            await status_msg.edit(text)
        except:
            pass

    original_file = None
    parts = []
    try:
        await update_status("🔍 GoFile link resolve karanawa...")
        dl_url, fname, size, hdrs = resolve_gofile(url)

        await update_status(
            f"📄 **{fname}**\n"
            f"💾 Size: {size//(1024**2)} MB\n\n"
            f"{make_progress_bar(0)} 0%\n"
            f"⬇️ Starting download..."
        )

        original_file = await download_file(dl_url, fname, hdrs, update_status)

        await update_status("✂️ Splitting into 2GB parts...")
        parts = split_file(original_file)

        await upload_parts(parts, fname, update_status)

        total_size = sum(os.path.getsize(p) for p in parts if os.path.exists(p))
        await update_status(
            f"🎉 **Done!**\n\n"
            f"📦 `{fname}`\n"
            f"🗂 Parts: {len(parts)}\n"
            f"💾 Total: {total_size//(1024**2)} MB\n\n"
            f"✅ Saved Messages ෙ check කරන්න!"
        )

    except Exception as e:
        await update_status(f"❌ **Error:**\n`{e}`")

    finally:
        cleanup(original_file or "", parts)
        active_jobs.pop(chat_id, None)


# ════════════════════════════════════════
# Main
# ════════════════════════════════════════

async def main():
    await user_client.start(phone=USER_PHONE)
    print("[✓] User client connected.")

    await bot_client.start(bot_token=BOT_TOKEN)
    me = await bot_client.get_me()
    print(f"[✓] Bot started: @{me.username}")
    print("[*] Waiting for GoFile links...")

    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())