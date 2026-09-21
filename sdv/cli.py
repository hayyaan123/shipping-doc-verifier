"""Command line:  python -m sdv run --data <folder-or-url> --out out --jev off|auto|all"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .inbox import Inbox
from .jev import JevClient
from .pipeline import process_all
from .report import text_report, write_outputs
from .submission import write_submission


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sdv")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="process the inbox and write results")
    run.add_argument("--data", required=True, help="folder with inbox/ + attachments/, or http://localhost:8080")
    run.add_argument("--out", default="out", help="output folder (results.json, report.txt, report.html, submission.json)")
    run.add_argument("--jev", choices=["off", "auto", "all"], default="auto",
                     help="off: rules only | auto: Jev only where rules are unsure | all: Jev on every email")
    run.add_argument("--env", default=".env", help="path to the git-ignored env file holding the Jev key")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--submit", action="store_true", help="POST the submission to /submit (needs an http:// --data)")
    args = ap.parse_args(argv)

    inbox = Inbox(args.data)
    jev = None
    if args.jev != "off":
        from .jev import find_api_key

        jev = JevClient(api_key=find_api_key(args.env))
        if not jev.api_key:
            print("[jev] no key found; using cached answers if any, otherwise the rule tier.", file=sys.stderr)

    t0 = time.time()
    results = process_all(inbox, jev, args.jev, args.limit)
    write_outputs(results, args.out)
    write_submission(results, f"{args.out}/submission.json")
    print(text_report(results).split("\n\n")[0])
    if jev:
        print(f"[jev] live calls={jev.calls} cache hits={jev.cache_hits} failures={jev.failures}"
              + (f" | disabled: {jev.disabled_reason}" if jev.disabled_reason else ""))
    failed = [r for r in results if r.error]
    if failed:
        print(f"[warn] {len(failed)} emails failed processing (retryable): {[r.email_id for r in failed][:5]}")
    print(f"Done in {time.time() - t0:.1f}s -> {args.out}/")
    if args.submit:
        sub = json.loads(open(f"{args.out}/submission.json", encoding="utf-8").read())
        print(json.dumps(inbox.submit(sub), indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
