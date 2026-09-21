#!/bin/sh
# build: process the bundled sample inbox into a case database (run once, at image build)
# serve: start the review console (default)
set -e
DATA=demo/data
DB=out/cases.db
if [ "$1" = "build" ]; then
  JEV=off
  if [ -n "$(ls demo/jev_cache/*.json 2>/dev/null | head -1)" ]; then
    export SDV_JEV_CACHE=demo/jev_cache   # pre-computed Jev answers: used without an API key
    JEV=all
  fi
  python -m sdv run --data "$DATA" --out out --db "$DB" --jev "$JEV" --vision off
  exit 0
fi
# Shipped Jev answers are used without a key (a cache miss falls back to the rules; no live call is possible without a key).
# Vision stays OFF on the public copy unless the operator sets it (and provides the key as a secret).
if [ -n "$(ls demo/jev_cache/*.json 2>/dev/null | head -1)" ]; then export SDV_JEV_CACHE=demo/jev_cache; : "${SDV_JEV:=all}"; fi
exec python -m sdv serve --db "$DB" --data "$DATA" --host 0.0.0.0 --port "${PORT:-7860}" --jev "${SDV_JEV:-off}" --vision "${SDV_VISION:-off}"
