#!/bin/sh
# Health-check a Backlogr service. Set BACKLOGR_URL (e.g. https://backlogr.example.ts.net/backlogr).
: "${BACKLOGR_URL:?set BACKLOGR_URL}"
curl -fsS -m 8 "$BACKLOGR_URL/healthz" >/dev/null && echo "backlogr healthy at $BACKLOGR_URL" || { echo "backlogr UNREACHABLE at $BACKLOGR_URL"; exit 1; }
