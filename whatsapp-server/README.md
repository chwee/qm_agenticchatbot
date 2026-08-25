# WhatsApp Gateway (Frontend)

Isolated WhatsApp server for the Q&M system. Its **only** job is inbound and
outbound messages — it contains no business logic. This is the channel side of
the proposal's "Shared Entry Point — WhatsApp Intent Router" (§5.1).

- **Inbound:** every 1:1 WhatsApp message (text, or image-with-caption such as
  `/pay`) is normalised and `POST`ed to the backend webhook. Image media is
  downloaded and forwarded as base64 in the same payload (stateless `/pay`
  design, proposal §10.4).
- **Outbound:** exposes `POST /send-reply { to, message }` which the backend
  calls to deliver replies (the agents' "Send WhatsApp Reply" action).

Built on `whatsapp-web.js` (per the project's reference `open-wa/runn8nv2.js`).
Uses your **system Chrome** via `CHROME_PATH`, so no Chromium download is needed.

## Setup

```bash
set PUPPETEER_SKIP_DOWNLOAD=true && npm install
copy .env.example .env          # defaults already point at the backend
npm start
```

On first run a QR code prints in the terminal — scan it from WhatsApp
(Linked devices). The session is saved under `.wwebjs_auth/` so subsequent
starts skip the QR.

## Config (`.env`)

| Var | Default | Meaning |
|-----|---------|---------|
| `BACKEND_WEBHOOK_URL` | `http://localhost:8000/webhook/whatsapp` | where inbound messages are forwarded |
| `REPLY_SERVER_PORT` | `3000` | port for `/send-reply` |
| `CHROME_PATH` | Windows Chrome path | Chrome/Chromium executable |
| `HEADLESS` | `false` | run browser headless (set true after first QR scan) |
| `FORWARD_TIMEOUT_MS` | `120000` | inbound forward timeout |

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/send-reply` | `{ to, message }` → send a WhatsApp message |
| GET  | `/status` | gateway + connection status |

## Inbound payload shape (to backend)

```json
{
  "event": "incoming_message",
  "from": { "lid": "...@c.us", "phone": "6591234567", "name": "Tan Wei Ling" },
  "message": { "id": "...", "type": "chat|image", "body": "/pay", "has_media": true },
  "media": { "mimetype": "image/jpeg", "data": "<base64>", "filename": null }
}
```

## Migrating to Meta WhatsApp Business Cloud API

Per the proposal, this `whatsapp-web.js` transport is for development/POC. To go
to production, replace the inbound `client.on('message')` handler and the
`/send-reply` sender with Meta Graph API calls — the backend contract (the
payload above + `/send-reply`) stays identical.
