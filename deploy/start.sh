#!/bin/sh
# build: process the bundled sample into the workspace (run once, at image build)
# serve: start the backend API + frontend (default)
set -e
SAMPLE=demo/data
WORK=${SDV_WORKDIR:-workspace}
if [ -n "$(ls demo/jev_cache/*.json 2>/dev/null | head -1)" ]; then
  export SDV_JEV_CACHE=demo/jev_cache   # pre-computed Jev answers: used without an API key
  : "${SDV_JEV:=all}"
fi
if [ "$1" = "build" ]; then
  exec python -m sdv bootstrap --workdir "$WORK" --sample "$SAMPLE" --jev "${SDV_JEV:-off}"
fi
# Vision stays OFF on the public copy unless the operator sets it (and provides the key as a secret).
# SDV_CORS_ORIGIN: set to the frontend's URL if the frontend is hosted separately; "*" is fine for a public demo.
exec python -m sdv serve --workdir "$WORK" --sample "$SAMPLE" --frontend frontend \
  --host 0.0.0.0 --port "${PORT:-7860}" --cors-origin "${SDV_CORS_ORIGIN:-*}" \
  --jev "${SDV_JEV:-off}" --vision "${SDV_VISION:-off}" ${SDV_ALLOW_PRIVATE_URLS:+--allow-private-urls}
