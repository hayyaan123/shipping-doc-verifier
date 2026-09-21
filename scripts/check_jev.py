"""Run this ONCE on your own machine to confirm your Jev key works and see what a call costs.

    python scripts/check_jev.py

It reads the key from .env (Jev_api_key or TYPESAFE_API_KEY) or the environment. The key is never printed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdv.jev import API_URL, JevClient, find_api_key, triage_questions, triage_state  # noqa: E402


def main() -> int:
    key = find_api_key()
    if not key:
        print("No key found. Put Jev_api_key=... in .env (which is git-ignored).")
        return 1
    print(f"Key found ({len(key)} characters). Calling {API_URL} ...")
    client = JevClient(api_key=key, cache_dir=".cache/jev_check")
    email = {
        "subject": "REQUEST BL DRAFT _ PO 26067",
        "body": "Attached are the SI and draft BL for OC 5ALT-01226. Please check the details and confirm.",
        "attachments": ["attachments/x_SI.txt", "attachments/x_BL.txt"],
    }
    answers = client.ask(triage_state(email), triage_questions())
    if answers is None:
        print("FAILED:", client.disabled_reason or "no answer (network blocked, bad request, or out of credits)")
        return 2
    print(json.dumps(answers, indent=2)[:1200])
    print("\nOK. Expected category BL_COMPARISON; got:", client.choice(answers, "category"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
