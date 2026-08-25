/**
 * Q&M AI Enquiry & Enrollment System — WhatsApp Gateway (Frontend)
 * ------------------------------------------------------------------
 * Isolated WhatsApp server. Sole responsibility: inbound + outbound messages.
 *
 *   INBOUND   WhatsApp message  ->  POST {BACKEND_WEBHOOK_URL}
 *   OUTBOUND  POST /send-reply  ->  WhatsApp message
 *
 * This is the "Shared Entry Point — WhatsApp Intent Router" channel from the
 * proposal (Section 5.1). It does NO routing or business logic itself; every
 * inbound message (text, or image-with-caption such as the /pay command) is
 * normalised and forwarded to the Python CrewAI backend, which owns the Intent
 * Router and Modules A/B/C. Replies come back through /send-reply.
 *
 * Based on the project's reference gateway (open-wa/runn8nv2.js) using
 * whatsapp-web.js. Swap the WhatsApp Web transport for the Meta WhatsApp
 * Business Cloud API later with no change to the backend contract.
 *
 *   npm install
 *   npm start
 */

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');
const express = require('express');
require('dotenv').config();

// ── Config ──────────────────────────────────────────────────────────────────
const BACKEND_WEBHOOK_URL =
  process.env.BACKEND_WEBHOOK_URL || 'http://localhost:8000/webhook/whatsapp';
const REPLY_SERVER_PORT = parseInt(process.env.REPLY_SERVER_PORT || '3000', 10);
const CHROME_PATH =
  process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const HEADLESS = String(process.env.HEADLESS || 'false') === 'true';
const FORWARD_TIMEOUT_MS = parseInt(process.env.FORWARD_TIMEOUT_MS || '120000', 10);

// Maps a bare phone number -> the exact WhatsApp chat id we last saw from them
// (e.g. "6591234567" -> "123456789@lid"). Needed because WhatsApp Web replies
// must target the original chat id, not a reconstructed one.
const contactCache = {};

// ── Express reply server (OUTBOUND) ──────────────────────────────────────────
const app = express();
app.use(express.json({ limit: '25mb' }));
app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  next();
});

/**
 * POST /send-reply  { to, message }
 * The backend calls this to deliver an outbound WhatsApp message. `to` may be a
 * bare phone number or any WhatsApp id form; we resolve to the cached chat id.
 */
app.post('/send-reply', async (req, res) => {
  const { to, message } = req.body || {};

  console.log(`\n📤 POST /send-reply  to=${to}  len=${(message || '').length}`);

  if (!to || !message) {
    return res
      .status(400)
      .json({ success: false, error: 'Missing required fields: "to" and "message"' });
  }
  if (!whatsappClient) {
    return res.status(503).json({ success: false, error: 'WhatsApp client not ready' });
  }

  try {
    const number = String(to).trim().replace('@lid', '').replace('@c.us', '').replace('+', '');
    let sendTo = contactCache[number];
    if (!sendTo) {
      // Fall back to the standard contact id form if we have never seen them.
      sendTo = `${number}@c.us`;
      console.log(`   not in cache, trying ${sendTo}`);
    }
    await whatsappClient.sendMessage(sendTo, message);
    console.log(`✅ sent to ${sendTo}`);
    res.json({ success: true, to: sendTo });
  } catch (err) {
    console.error(`❌ reply failed: ${err.message}`);
    res.status(500).json({ success: false, error: err.message });
  }
});

app.get('/status', (req, res) => {
  res.json({
    whatsappReady: !!whatsappClient,
    cachedContacts: Object.keys(contactCache).length,
    backendWebhook: BACKEND_WEBHOOK_URL,
    timestamp: new Date().toISOString(),
  });
});

app.listen(REPLY_SERVER_PORT, () => {
  console.log(`🌐 Reply server: http://localhost:${REPLY_SERVER_PORT}`);
  console.log(`   POST /send-reply  → send a WhatsApp message`);
  console.log(`   GET  /status      → connection status`);
});

// ── Helpers ──────────────────────────────────────────────────────────────────
function timestamp() {
  return new Date().toLocaleString('en-SG', { timeZone: 'Asia/Singapore' });
}

/**
 * Resolve a WhatsApp chat ID to a real E.164 phone number (digits only, no +).
 *
 * @c.us accounts: id.user IS the phone number — always reliable.
 * @lid  accounts: WhatsApp Linked Identity — the LID is NOT the phone number.
 *                 Without the browser (Puppeteer), there is no API to map LID
 *                 to phone. We return null so the backend can prompt the user.
 */
async function getPhoneNumber(client, chatId) {
  // @c.us — id.user is directly the E.164 number (no country prefix +)
  if (chatId.endsWith('@c.us')) {
    return chatId.split('@')[0];
  }

  // @lid — try the wwebjs contact API (works if wwebjs has already cached
  // the mapping; fails silently otherwise — backend will handle it)
  if (chatId.endsWith('@lid')) {
    try {
      const contact = await client.getContactById(chatId);
      // c.us server means wwebjs resolved it to a real contact
      if (contact.id?.server === 'c.us') return contact.id.user;
      // contact.number for LID in wwebjs 1.34.x returns the LID — not useful
    } catch { /* not cached yet */ }
    return null; // signal to backend: phone unknown, ask the user
  }

  // Unknown format — strip suffix and return as-is
  return chatId.split('@')[0];
}

async function forwardToBackend(payload) {
  try {
    const r = await axios.post(BACKEND_WEBHOOK_URL, payload, {
      headers: { 'Content-Type': 'application/json' },
      timeout: FORWARD_TIMEOUT_MS,
    });
    // The backend may return a reply to send synchronously, but the normal path
    // is asynchronous delivery via /send-reply, so we just log the ack here.
    console.log(`✅ forwarded to backend — status ${r.status}`);
    return r.data;
  } catch (err) {
    console.error(`❌ forward to backend failed: ${err.message}`);
    return null;
  }
}

// ── WhatsApp client (INBOUND) ────────────────────────────────────────────────
let whatsappClient = null;
let isRestarting = false;

function createClient() {
  const client = new Client({
    authStrategy: new LocalAuth({ dataPath: '.wwebjs_auth' }),
    restartOnAuthFail: true,
    puppeteer: {
      executablePath: CHROME_PATH,
      headless: HEADLESS,
      handleSIGINT: false,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--no-first-run',
        '--no-zygote',
        '--disable-extensions',
      ],
    },
  });

  async function safeRestart(reason) {
    if (isRestarting) return;
    isRestarting = true;
    whatsappClient = null;
    console.log(`🔄 restarting in 8s (${reason})`);
    try {
      await client.destroy();
    } catch (e) {}
    setTimeout(() => {
      isRestarting = false;
      createClient();
    }, 8000);
  }

  client.on('qr', (qr) => {
    console.clear();
    console.log('╔══════════════════════════════════════╗');
    console.log('║   Q&M WhatsApp Gateway — Scan QR      ║');
    console.log('╚══════════════════════════════════════╝');
    qrcode.generate(qr, { small: true });
  });

  client.on('loading_screen', (p, m) => process.stdout.write(`\r⏳ Loading: ${p}% ${m}   `));
  client.on('authenticated', () => console.log('\n🔐 Authenticated'));

  client.on('ready', () => {
    whatsappClient = client;
    console.clear();
    console.log('╔════════════════════════════════════════════╗');
    console.log('║   ✅ Q&M WhatsApp Gateway Active            ║');
    console.log('╚════════════════════════════════════════════╝');
    console.log(`Started   : ${timestamp()}`);
    console.log(`Backend   : ${BACKEND_WEBHOOK_URL}`);
    console.log(`Reply API : http://localhost:${REPLY_SERVER_PORT}/send-reply`);
    console.log('Waiting for messages...\n');
  });

  client.on('auth_failure', async (msg) => {
    console.error('❌ auth failure:', msg);
    await safeRestart('auth_failure');
  });

  client.on('disconnected', async (reason) => {
    whatsappClient = null;
    console.log(`\n❌ disconnected: ${reason}`);
    await safeRestart('disconnected');
  });

  client.on('message', async (msg) => {
    try {
      // Ignore groups and status broadcasts — the gateway is 1:1 only.
      if (msg.from.includes('@g.us') || msg.from === 'status@broadcast') return;

      const resolvedPhone = await getPhoneNumber(client, msg.from);
      const isLid = msg.from.endsWith('@lid');

      // Cache msg.from (the original WhatsApp JID) as the send target.
      // For @c.us accounts: msg.from = "6591234567@c.us"       → sendMessage uses @c.us  ✓
      // For @lid accounts:  msg.from = "153811586920512@lid"   → sendMessage uses @lid   ✓
      // Using phone@c.us for @lid accounts does NOT work — the account lives on the
      // @lid server, not @c.us, so the message is silently dropped by WhatsApp.
      //
      // Three cache entries so /send-reply matches any form the backend sends:
      //   by resolved phone  "6597607916"          → correct JID
      //   by bare JID digits "153811586920512"      → correct JID  ← /send-reply strips suffix then looks here
      //   by full JID        "153811586920512@lid"  → correct JID
      const cacheKey = resolvedPhone || msg.from.split('@')[0];
      contactCache[cacheKey]               = msg.from;
      contactCache[msg.from.split('@')[0]] = msg.from;
      contactCache[msg.from]               = msg.from;

      if (isLid && !resolvedPhone) {
        console.warn(`⚠️  LID account — phone unknown: ${msg.from}  (backend will prompt user)`);
      } else if (isLid) {
        console.log(`🔑 LID resolved: ${msg.from} → phone=${resolvedPhone}`);
      }

      // Download media for image messages (e.g. /pay PayNow screenshot). The
      // caption arrives as msg.body in the SAME webhook payload (stateless
      // /pay design, proposal Section 10.4).
      let media = null;
      if (msg.hasMedia) {
        try {
          const m = await msg.downloadMedia();
          if (m && m.data) {
            media = { mimetype: m.mimetype, data: m.data, filename: m.filename || null };
          }
        } catch (e) {
          console.error('⚠️  media download failed:', e.message);
        }
      }

      const payload = {
        event: 'incoming_message',
        timestamp: new Date().toISOString(),
        timestamp_sg: timestamp(),
        from: {
          chat_id: msg.from,                        // stable routing key (always set)
          phone: resolvedPhone || null,              // real E.164 number, or null if LID unresolved
          name: msg._data?.notifyName || null,
        },
        message: {
          id: msg.id._serialized,
          type: msg.type,
          body: msg.body || '',
          is_group: false,
          has_media: !!msg.hasMedia,
        },
        media, // { mimetype, data(base64), filename } or null
      };

      console.log('─'.repeat(60));
      console.log(`📨 IN  ${resolvedPhone ? '+' + resolvedPhone : msg.from} (${msg._data?.notifyName || '?'}) [${msg.type}]`);
      console.log(`   ${media ? '[media] ' : ''}${(msg.body || '').slice(0, 120)}`);

      const data = await forwardToBackend(payload);
      // Optional synchronous reply support: if the backend chooses to answer in
      // the HTTP response instead of via /send-reply, honour it.
      if (data && data.reply && whatsappClient) {
        await whatsappClient.sendMessage(msg.from, data.reply);
        console.log(`✅ sync reply sent`);
      }
    } catch (err) {
      console.error('❌ inbound error:', err.message);
    }
  });

  client.initialize().catch(async (err) => {
    console.error('❌ initialize error:', err.message);
    await safeRestart('initialize_error');
  });

  return client;
}

console.log('🚀 Starting Q&M WhatsApp Gateway...');
createClient();
