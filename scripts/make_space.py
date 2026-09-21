"""Assemble a folder that can be pushed to a Hugging Face Space (Docker SDK).

    python scripts/make_space.py space
    cd space && git init -b main && git add . && git commit -m "Deploy" \
        && git remote add space https://huggingface.co/spaces/<user>/<name> && git push space main

The Space builds the Dockerfile, processes the bundled sample inbox into a case database, and serves the console.
"""
import shutil
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1] if len(sys.argv) > 1 else "space")
if out.exists():
    shutil.rmtree(out)
out.mkdir(parents=True)
ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
for name in ("sdv", "demo"):
    shutil.copytree(root / name, out / name, ignore=ignore)
(out / "deploy").mkdir()
shutil.copy(root / "deploy" / "start.sh", out / "deploy" / "start.sh")
for name in ("Dockerfile", "requirements-app.txt", ".dockerignore"):
    shutil.copy(root / name, out / name)
(out / "README.md").write_text(
    """---
title: Shipping Document Verifier
emoji: 🚢
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

Shipping-operations inbox triage and SI vs draft-BL verification (Averis x Monash Hackathon 2026).
Source: https://github.com/hayyaan123/shipping-doc-verifier
""",
    encoding="utf-8",
)
print("Space folder ready:", out.resolve())
