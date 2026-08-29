"""
scripts/verify_serper.py
------------------------
Verifies the Serper.dev search API is configured and working.

Run: python scripts/verify_serper.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()


def verify():
    api_key  = os.getenv("SEARCH_API_KEY", "")
    provider = os.getenv("SEARCH_API_PROVIDER", "serper")

    print("=" * 55)
    print("  SERPER SEARCH API VERIFICATION")
    print("=" * 55)

    if not api_key:
        print("[FAIL] SEARCH_API_KEY is not set in .env")
        print("[HINT] Get a free API key at: https://serper.dev/")
        return False

    print(f"[INFO] Key      : {api_key[:8]}...{api_key[-4:]}")
    print(f"[INFO] Provider : {provider}")
    print("[INFO] Running test search: 'campus placement India 2025'")

    try:
        import httpx

        r = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": "campus placement India 2025 top companies", "num": 3},
            timeout=15,
        )

        if r.status_code != 200:
            print(f"\n[FAIL] HTTP {r.status_code}: {r.text[:200]}")
            return False

        data = r.json()
        results = data.get("organic", [])

        if not results:
            print("\n[WARN] No results returned (quota may be exhausted)")
            return False

        print()
        print(f"[SUCCESS] Got {len(results)} search results!")
        print()
        for i, res in enumerate(results, 1):
            print(f"  {i}. {res.get('title', 'N/A')}")
            print(f"     {res.get('link', '')}")
            snippet = res.get('snippet', '')
            if snippet:
                print(f"     {snippet[:100]}...")
            print()

        return True

    except ImportError:
        print("[FAIL] httpx not installed. Run: pip install httpx")
        return False
    except Exception as e:
        print(f"\n[FAIL] {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    ok = verify()
    print("RESULT:", "PASS ✓" if ok else "FAIL ✗")
    print("=" * 55)
    sys.exit(0 if ok else 1)

