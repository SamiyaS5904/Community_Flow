"""
scripts/verify_telegram.py
--------------------------
Verifies the Telegram bot token and sends a real test message.

Run: python scripts/verify_telegram.py

BEFORE RUNNING — fill in .env:
    TELEGRAM_BOT_TOKEN  = token from @BotFather
    TELEGRAM_CHAT_ID    = your channel chat ID (see instructions below)

HOW TO GET YOUR CHANNEL CHAT ID:
    1. Add the bot to your channel as an admin
    2. Send any message in the channel
    3. Open this URL in your browser:
       https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
    4. Look for: "chat": {"id": -100xxxxxxxxxx}
    5. Copy that number into TELEGRAM_CHAT_ID in .env
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()


def verify():
    token    = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id  = os.getenv("TELEGRAM_CHAT_ID", "")
    admin_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

    print("=" * 55)
    print("  TELEGRAM API VERIFICATION")
    print("=" * 55)

    if not token:
        print("[FAIL] TELEGRAM_BOT_TOKEN is not set in .env")
        return False

    if not chat_id or "xxxxxxxxxx" in chat_id:
        print("[FAIL] TELEGRAM_CHAT_ID is not set in .env")
        print()
        print("[HINT] To find your chat ID:")
        print(f"       Open: https://api.telegram.org/bot{token[:30]}...../getUpdates")
        print('       Find: "chat": {"id": -100xxxxxxxxxx}')
        return False

    print(f"[INFO] Token    : {token[:15]}...")
    print(f"[INFO] Chat ID  : {chat_id}")
    print(f"[INFO] Admin ID : {admin_id or 'Not set'}")

    try:
        import httpx

        # Step 1: Verify bot token
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = r.json()
        if not data.get("ok"):
            print(f"\n[FAIL] Invalid bot token: {data.get('description')}")
            return False

        bot = data["result"]
        print(f"[INFO] Bot name : @{bot.get('username')} ({bot.get('first_name')})")

        # Step 2: Send test message
        print("[INFO] Sending test message...")
        r2 = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": (
                    "Carrot Owl Content Platform - Connection Test\n\n"
                    "Telegram verified successfully!\n"
                    "The content automation pipeline is ready."
                ),
            },
            timeout=10,
        )
        data2 = r2.json()

        if data2.get("ok"):
            msg_id = data2["result"]["message_id"]
            print()
            print(f"[SUCCESS] Message sent! message_id = {msg_id}")
            print("          Check your Telegram channel now.")
            return True
        else:
            print(f"\n[FAIL] {data2.get('description', 'Unknown error')}")
            print("[HINT] Make sure the bot is an admin with 'Post Messages' permission")
            return False

    except ImportError:
        print("[FAIL] httpx not installed. Run: pip install httpx")
        return False
    except Exception as e:
        print(f"\n[FAIL] {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    ok = verify()
    print()
    print("RESULT:", "PASS ✓" if ok else "FAIL ✗")
    print("=" * 55)
    sys.exit(0 if ok else 1)

