"""
Railway deploy කරන්න කලින් LOCAL වලින් 1 වතාවක් run කරන්න.
user_session.session file ekak hadewi — eka repo ekata add karanna.
"""
import os
import asyncio
from telethon import TelegramClient
from dotenv import load_dotenv

load_dotenv()

API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE    = os.environ["TG_PHONE"]

async def main():
    client = TelegramClient("user_session", API_ID, API_HASH)
    await client.start(phone=PHONE)
    me = await client.get_me()
    print(f"[✓] Logged in as: {me.first_name} (@{me.username})")
    print("[✓] user_session.session file created!")
    print("[→] Eka Railway ekata upload karanna (Variables -> Files or repo ekata add karanna)")
    await client.disconnect()

asyncio.run(main())