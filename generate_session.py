import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

# API ID eka 38963550 widiyata fix kala
API_ID = 38963550 
API_HASH = "1e7e73506dd3e91f2c513240e701945d"

print("--- Telegram Session Generator ---")
try:
    with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        session_string = client.session.save()
        print("\n✅ OYAGE STRING SESSION EKA PAHATHA DAKWE:")
        print("-" * 60)
        print(session_string)
        print("-" * 60)
        print("\nUda thiyena code eka copy karala GitHub Secrets wala STRING_SESSION widiyata danna.")
except Exception as e:
    print(f"\n❌ Error ekak awa: {e}")