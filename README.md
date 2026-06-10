# print-bridge

Tiny LAN print server that lets Molcom Expenses print cheques and envelopes
with exact custom paper sizes, bypassing the Chrome/Windows print dialog
entirely (Windows won't let custom paper forms be added, so browser printing
can't do 6.26"×2.76" cheque stock).

Runs in a QNAP container next to the printers, exposed via Cloudflare Tunnel
at **print.vandreus.com**. The molcom-expenses worker proxies browser requests
to it (`/api/print/*`), adding the `X-Print-Secret` header server-side.

## Printers

Configured driverless (IPP Everywhere) — no vendor drivers in the container:

| id      | device                          | role     |
|---------|---------------------------------|----------|
| `canon` | Canon MF450 @ 10.69.7.107       | primary  |
| `epson` | Epson XP-15000 @ 10.69.7.235    | fallback |

`lpadmin -m everywhere` queries the live printer for capabilities, so a
printer that is off at container start is retried every 30 s by a background
loop and configured the moment it appears on the network.

## API (all requests need `X-Print-Secret`)

- `GET /health` — service + per-printer state (reachable / configured / idle / printing / stopped)
- `GET /printers/<id>/options` — raw `lpoptions -l` (use to discover tray / media-source names)
- `POST /print` — `{printer, pdf_base64, media, options?, copies?, title?}` → `{job_id}`
  - `media` examples: `Custom.159x70mm` (TD cheque), `COM10` (#10 envelope)
  - `print-scaling=none` is forced so exact field positions are never scaled
- `GET /jobs/<job_id>` — `{state: queued|printing|stopped|completed, message}`
- `POST /jobs/<job_id>/cancel`

## Deploy (Container Station)

Paste `docker-compose.yml` into Applications → Create, replacing
`REPLACE_WITH_SECRET`. Then add a Cloudflare Tunnel public hostname
`print.vandreus.com → http://<qnap-lan-ip>:8631` (same tunnel as
scan.vandreus.com). The hostname is additionally gated by a Cloudflare
Access Service Auth policy — the worker authenticates with a service token
(CF-Access-Client-Id/Secret) on top of X-Print-Secret.

To update: push to this repo, restart the container (it re-clones on boot).
