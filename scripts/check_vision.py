"""Check the hosted vision model on one scanned page. Prints the HTTP status and error text (never the key).

    python scripts/check_vision.py                      # uses VISION_* from .env
    python scripts/check_vision.py --list               # which image-capable models your account can call
    python scripts/check_vision.py --model Qwen/Qwen3-VL-30B-A3B-Instruct
    python scripts/check_vision.py --model "Qwen/Qwen3-VL-30B-A3B-Instruct:fastest"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdv.vision import VisionReader  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--env", default=".env")
ap.add_argument("--model", default="")
ap.add_argument("--list", action="store_true", help="list image-capable models served through the router")
ap.add_argument("--pdf", default="../bundle/attachments/email_512_BL.pdf")
a = ap.parse_args()

import json
import urllib.request

v = VisionReader.from_env(a.env, "vlm")
if a.list:
    req = urllib.request.Request(v.base_url + "/models", headers={"Authorization": f"Bearer {v.api_key}"} if v.api_key else {})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=60).read()).get("data", [])
    except Exception as e:
        sys.exit(f"could not list models: {type(e).__name__}: {e}")
    shown = 0
    for m in data:
        mods = (m.get("architecture") or {}).get("input_modalities") or []
        if "image" in mods:
            provs = ", ".join(f"{p.get('provider')}" + (f" (${p['pricing'].get('input')}/{p['pricing'].get('output')})" if isinstance(p.get("pricing"), dict) and p["pricing"].get("input") is not None else "")
                              for p in (m.get("providers") or []))
            print(f"{m.get('id')}   <- {provs}")
            shown += 1
    print(f"\n{shown} image-capable models of {len(data)} listed. Pick one and run: python scripts/check_vision.py --model <id>")
    sys.exit(0)
if a.model:
    v.model = a.model
v.cache = None
print(f"key present: {bool(v.api_key)} | endpoint: {v.base_url} | model: {v.model}")
if not v.api_key:
    sys.exit("No VISION_API_KEY in .env")
lines = v.read(Path(a.pdf).read_bytes())
if lines:
    print("OK. The model returned:\n" + "\n".join(lines[:15]))
else:
    print("FAILED:", v.last_error or "empty answer")
