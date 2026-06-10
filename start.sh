#!/bin/bash
# Entrypoint for the print-bridge container. The compose file installs
# cups/cups-ipp-utils/flask first, clones this repo to /app, then runs this.
set -e

# Start the CUPS scheduler (daemonizes itself) and wait until it answers.
/usr/sbin/cupsd
for i in $(seq 1 30); do
  if lpstat -r >/dev/null 2>&1; then break; fi
  sleep 1
done
lpstat -r || { echo "cupsd failed to start" >&2; exit 1; }

cd /app
exec python app.py
