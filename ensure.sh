#!/bin/sh
# Health-check a Backlogr service. Set BACKLOGR_URL (e.g. https://backlogr.example.invalid/backlogr).
: "${BACKLOGR_URL:?set BACKLOGR_URL}"
if curl -fsS -m 8 "$BACKLOGR_URL/healthz" >/dev/null; then echo "backlogr healthy at $BACKLOGR_URL"; else echo "backlogr UNREACHABLE at $BACKLOGR_URL"; exit 1; fi
