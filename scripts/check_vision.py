"""Check the hosted vision model on one scanned page. Prints the HTTP status and error text (never the key).

    python scripts/check_vision.py                      # uses VISION_* from .env
    python scripts/check_vision.py --model Qwen/Qwen2.5-VL-7B-Instruct
    python scripts/check_vision.py --model "Qwen/Qwen2.5-VL-7B-Instruct:fastest"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdv.vision import VisionReader  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--env", default=".env")
ap.add_argument("--model", default="")
ap.add_argument("--pdf", default="../bundle/attachments/email_512_BL.pdf")
a = ap.parse_args()

v = VisionReader.from_env(a.env, "vlm")
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
