#!/bin/sh
# Retired local server (moved to server-host 2026-10-24). Now just checks the remote service is healthy.
curl -fsS -m 8 https://backlogr.example.invalid/task-manager/healthz >/dev/null && echo "task server (server-host) healthy" || { echo "task server on server-host UNREACHABLE"; exit 1; }
