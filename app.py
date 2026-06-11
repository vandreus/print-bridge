"""print-bridge — LAN print server for Molcom Expenses cheque/envelope printing.

Runs in a QNAP container next to the printers and exposes a tiny authenticated
HTTP API in front of CUPS. Printers are configured driverless (IPP Everywhere)
so no vendor drivers are needed. Exposed publicly via Cloudflare Tunnel at
print.vandreus.com; every request must carry the X-Print-Secret header.

Printers:
  canon — Canon imageCLASS MF450 series (laser, primary)
  epson — Epson XP-15000 (inkjet, fallback)
"""

import base64
import os
import re
import socket
import subprocess
import tempfile
import threading
import time

from flask import Flask, jsonify, request

app = Flask(__name__)

PRINT_SECRET = os.environ.get("PRINT_SECRET", "")

PRINTERS = {
    "canon": {
        "label": "Canon MF450 (laser)",
        "uri": os.environ.get("CANON_URI", "ipp://10.69.7.107/ipp/print"),
    },
    "epson": {
        "label": "Epson XP-15000 (inkjet)",
        "uri": os.environ.get("EPSON_URI", "ipp://10.69.7.235:631/ipp/print"),
    },
}

# Per-printer driverless setup state, maintained by the background setup loop.
setup_state = {
    pid: {"configured": False, "error": None, "last_attempt": None}
    for pid in PRINTERS
}

# PPD InputSlot name per printer for each logical feed a job can request.
# Discovered via GET /printers/<id>/options:
#   Canon MF450:   InputSlot: Auto ByPassTray *Tray1
#   Epson XP-15000: InputSlot: Auto *Main Rear Disc
#   "manual"   = bypass/rear hand-feed (envelopes)
#   "cassette" = front paper drawer (TD cheque stock loaded as a stack)
# Unsupported values are dropped by ppd_choices(); the cassette names are each
# printer's default tray, so a drop simply falls back to the same drawer.
SOURCE_SLOT = {
    "manual":   {"canon": "ByPassTray", "epson": "Rear"},
    "cassette": {"canon": "Tray1",      "epson": "Main"},
}


def run(cmd, timeout=60):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )


def uri_host(uri):
    m = re.match(r"ipps?://([^:/]+)", uri)
    return m.group(1) if m else None


def tcp_reachable(host, port=631, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def configure_printer(pid):
    """lpadmin -m everywhere queries the printer for its capabilities, so the
    printer must be on and reachable — retried by the setup loop until it is."""
    p = PRINTERS[pid]
    setup_state[pid]["last_attempt"] = time.time()
    r = run(["lpadmin", "-p", pid, "-E", "-v", p["uri"], "-m", "everywhere"])
    if r.returncode == 0:
        run(["cupsenable", pid])
        run(["cupsaccept", pid])
        setup_state[pid]["configured"] = True
        setup_state[pid]["error"] = None
        app.logger.info("printer %s configured (%s)", pid, p["uri"])
        return True
    setup_state[pid]["error"] = (r.stderr or r.stdout).strip()
    app.logger.warning("printer %s setup failed: %s", pid, setup_state[pid]["error"])
    return False


def setup_loop():
    while True:
        pending = [pid for pid in PRINTERS if not setup_state[pid]["configured"]]
        if not pending:
            return
        for pid in pending:
            host = uri_host(PRINTERS[pid]["uri"])
            if host and tcp_reachable(host):
                configure_printer(pid)
        time.sleep(30)


def ppd_choices(pid):
    """Parse `lpoptions -l` into {option: {valid choices}} (asterisk = default
    is stripped). Used to drop option values a given printer doesn't support."""
    r = run(["lpoptions", "-p", pid, "-l"])
    choices = {}
    for line in (r.stdout or "").splitlines():
        if ":" not in line:
            continue
        head, vals = line.split(":", 1)
        key = head.split("/", 1)[0].strip()
        choices[key] = {v.lstrip("*") for v in vals.split()}
    return choices


def printer_status(pid):
    """Summarize CUPS + network state for one printer."""
    host = uri_host(PRINTERS[pid]["uri"])
    reachable = tcp_reachable(host) if host else False
    state = "unconfigured"
    message = ""
    if setup_state[pid]["configured"]:
        r = run(["lpstat", "-p", pid, "-l"])
        out = r.stdout or ""
        if "is idle" in out:
            state = "idle"
        elif "now printing" in out:
            state = "printing"
        elif "disabled" in out or "stopped" in out:
            state = "stopped"
        else:
            state = "unknown"
        # Surface the most recent status/reason line CUPS reports
        # (e.g. "Paused", "The printer is not responding.").
        for line in out.splitlines():
            line = line.strip()
            if line.startswith(("Status:", "Alerts:", "reason")):
                message = f"{message} {line}".strip()
    return {
        "label": PRINTERS[pid]["label"],
        "uri": PRINTERS[pid]["uri"],
        "reachable": reachable,
        "configured": setup_state[pid]["configured"],
        "setup_error": setup_state[pid]["error"],
        "state": state,
        "message": message,
    }


@app.before_request
def check_secret():
    if not PRINT_SECRET:
        return jsonify({"error": "PRINT_SECRET not set on server"}), 500
    if request.headers.get("X-Print-Secret") != PRINT_SECRET:
        return jsonify({"error": "unauthorized"}), 401


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "printers": {pid: printer_status(pid) for pid in PRINTERS},
    })


@app.get("/printers/<pid>/options")
def printer_options(pid):
    """Raw `lpoptions -l` output — used to discover media-source / InputSlot
    names (multipurpose tray etc.) once the real printer is connected."""
    if pid not in PRINTERS:
        return jsonify({"error": "unknown printer"}), 404
    if not setup_state[pid]["configured"]:
        return jsonify({"error": "printer not configured yet",
                        "setup_error": setup_state[pid]["error"]}), 409
    r = run(["lpoptions", "-p", pid, "-l"])
    return jsonify({"options": (r.stdout or "").splitlines()})


@app.post("/print")
def submit_print():
    body = request.get_json(force=True, silent=True) or {}
    pid = body.get("printer", "canon")
    if pid not in PRINTERS:
        return jsonify({"error": "unknown printer"}), 400
    if not setup_state[pid]["configured"]:
        # One immediate retry: the printer may have just been switched on.
        host = uri_host(PRINTERS[pid]["uri"])
        if not (host and tcp_reachable(host) and configure_printer(pid)):
            return jsonify({
                "error": f"printer '{pid}' is not available",
                "detail": setup_state[pid]["error"] or "unreachable",
            }), 503

    pdf_b64 = body.get("pdf_base64")
    if not pdf_b64:
        return jsonify({"error": "pdf_base64 is required"}), 400
    try:
        pdf = base64.b64decode(pdf_b64)
    except Exception:
        return jsonify({"error": "invalid base64"}), 400
    if not pdf.startswith(b"%PDF"):
        return jsonify({"error": "payload is not a PDF"}), 400

    media = body.get("media")  # e.g. "Custom.159x70mm" or "Env10"
    copies = int(body.get("copies", 1))
    title = body.get("title", "print-bridge job")
    options = dict(body.get("options") or {})

    # Map the logical feed ("manual" bypass / "cassette" front drawer) to this
    # printer's InputSlot. Envelopes are hand-fed; cheques stack in the drawer.
    src = body.get("source")
    if src in SOURCE_SLOT and pid in SOURCE_SLOT[src]:
        options.setdefault("InputSlot", SOURCE_SLOT[src][pid])

    # Drop any PPD option value this printer doesn't support, so a mapping
    # meant for one printer can never fail the job on the other.
    ppd = ppd_choices(pid)
    for k in list(options):
        if k in ppd and str(options[k]) not in ppd[k]:
            app.logger.warning("dropping unsupported option %s=%s for %s",
                               k, options[k], pid)
            del options[k]

    cmd = ["lp", "-d", pid, "-n", str(copies), "-t", title]
    if media:
        cmd += ["-o", f"media={media}"]
    # Exact-position documents must never be scaled or centered by the filter.
    options.setdefault("print-scaling", "none")
    for k, v in options.items():
        cmd += ["-o", f"{k}={v}"]

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf)
        path = f.name
    try:
        r = run(cmd + [path])
    finally:
        # lp copies the file into the spool, safe to delete immediately.
        try:
            os.unlink(path)
        except OSError:
            pass

    if r.returncode != 0:
        return jsonify({"error": "lp failed",
                        "detail": (r.stderr or r.stdout).strip()}), 502
    m = re.search(r"request id is (\S+)", r.stdout or "")
    if not m:
        return jsonify({"error": "could not parse job id",
                        "detail": r.stdout}), 502
    return jsonify({"job_id": m.group(1)})


@app.get("/jobs/<job_id>")
def job_status(job_id):
    pid = job_id.rsplit("-", 1)[0]
    if pid not in PRINTERS:
        return jsonify({"error": "unknown job id"}), 404

    active = run(["lpstat", "-W", "not-completed", "-o", pid]).stdout or ""
    pstat = run(["lpstat", "-p", pid, "-l"]).stdout or ""
    message = " ".join(
        line.strip() for line in pstat.splitlines()
        if line.strip().startswith(("Status:", "Alerts:"))
    )

    if re.search(rf"^{re.escape(job_id)}\s", active, re.M):
        printing = f"now printing {job_id}" in pstat
        state = "printing" if printing else "queued"
        if "disabled" in pstat or "Paused" in pstat:
            state = "stopped"
        return jsonify({"job_id": job_id, "state": state, "message": message})

    completed = run(["lpstat", "-W", "completed", "-o", pid]).stdout or ""
    if re.search(rf"^{re.escape(job_id)}\s", completed, re.M):
        return jsonify({"job_id": job_id, "state": "completed", "message": message})

    # Not in either list — CUPS may have purged it; treat as completed so the
    # wizard never hangs on a job that already left the queue.
    return jsonify({"job_id": job_id, "state": "completed",
                    "message": message, "note": "job no longer in queue"})


@app.post("/jobs/<job_id>/cancel")
def job_cancel(job_id):
    r = run(["cancel", job_id])
    if r.returncode != 0:
        return jsonify({"error": (r.stderr or r.stdout).strip()}), 502
    return jsonify({"job_id": job_id, "state": "canceled"})


if __name__ == "__main__":
    threading.Thread(target=setup_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
