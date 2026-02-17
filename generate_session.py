import os
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

# Details
API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE    = os.environ["TG_PHONE"]

async def main():
    # StringSession() kiyala dammama session eka text ekak widihata hadenawa
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        await client.start(phone=PHONE)
        print("\n" + "="*50)
        print("OYAAGE STRING SESSION EKA PAHATHTHA THIYENAWA (COPY ME):")
        print(f"\n{client.session.save()}\n")
        print("="*50)
        print("\n[!] Me code eka copy karala Railway/GitHub Secrets wala STRING_SESSION kiyala danna.")

if __name__ == "__main__":
    asyncio.run(main())