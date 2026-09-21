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
    run.add_argument("--db", default=None, help="also save every case into this SQLite case store (e.g. out/cases.db)")

    aud = sub.add_parser("audit", help="ask Jev about every email and compare with the rule tier (validation evidence)")
    aud.add_argument("--data", required=True)
    aud.add_argument("--out", default="out")
    aud.add_argument("--env", default=".env")
    aud.add_argument("--limit", type=int, default=None)

    srv = sub.add_parser("serve", help="run the review console over a case store")
    srv.add_argument("--db", default="out/cases.db")
    srv.add_argument("--port", type=int, default=8000)
    srv.add_argument("--data", default=None, help="optional: enables the console's Retry-failed button")
    srv.add_argument("--jev", choices=["off", "auto", "all"], default="off")
    srv.add_argument("--env", default=".env")

    exp = sub.add_parser("export", help="write a read-only static console (index.html) for free hosting")
    exp.add_argument("--db", default="out/cases.db")
    exp.add_argument("--out", default="site")

    syn = sub.add_parser("sync", help="mirror the case store to / from Cloud Firestore")
    syn.add_argument("direction", choices=["push", "pull"])
    syn.add_argument("--db", default="out/cases.db")
    syn.add_argument("--env", default=".env")
    syn.add_argument("--project", default=None)

    args = ap.parse_args(argv)

    if args.cmd == "audit":
        return _audit(args)
    if args.cmd == "serve":
        return _serve(args)
    if args.cmd == "export":
        from .console import export_static
        from .store import CaseStore

        print("wrote", export_static(CaseStore(args.db), args.out))
        return 0
    if args.cmd == "sync":
        return _sync(args)

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
    if args.db:
        from .store import CaseStore

        store = CaseStore(args.db)
        store.upsert_many([r.to_dict() for r in results])
        print(f"[store] {len(results)} cases saved to {args.db}")
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


def _jev(env: str):
    from .jev import find_api_key

    j = JevClient(api_key=find_api_key(env))
    if not j.api_key:
        print("[jev] no key found in", env, file=sys.stderr)
    return j


def _audit(args) -> int:
    from .audit import audit

    jev = _jev(args.env)
    s = audit(Inbox(args.data), jev, args.out, args.limit)
    print(f"Jev answered {s['jev_answered']}/{s['emails']} emails; agreed with the rule tier on {s['agree']} "
          f"(rate {s['agreement_rate']}).")
    print(f"live calls={s['jev_calls']} cache hits={s['cache_hits']} failures={s['failures']}"
          + (f" | disabled: {s['disabled_reason']}" if s["disabled_reason"] else ""))
    for d in s["disagreements"][:25]:
        print(f"  {d['email_id']}: rules={d['rules']} ({d['rules_conf']}) jev={d['jev']} ({d['jev_conf']})  {d['subject'][:60]}")
    print(f"Full detail: {args.out}/jev_audit.json")
    return 0


def _serve(args) -> int:
    from .console import serve
    from .store import CaseStore

    inbox = Inbox(args.data) if args.data else None
    jev = _jev(args.env) if args.jev != "off" else None
    serve(CaseStore(args.db), port=args.port, inbox=inbox, jev=jev, jev_mode=args.jev)
    return 0


def _sync(args) -> int:
    from .jev import load_env
    from .store import CaseStore
    from . import cloud

    import os

    for k, v in load_env(args.env).items():
        os.environ.setdefault(k, v)
    store = CaseStore(args.db)
    n = cloud.push_cases(store, project=args.project) if args.direction == "push" else cloud.pull_decisions(store, project=args.project)
    print(f"{args.direction}: {n} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
