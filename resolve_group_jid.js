#!/usr/bin/env node
/**
 * One-time utility: resolve a WhatsApp group JID from an invite link code.
 *
 * Usage:
 *   NODE_PATH=$HOME/.npm-global/lib/node_modules/openclaw/node_modules \
 *     node resolve_group_jid.js AbCdEfGhIjKlMnOpQrStUv
 *
 * Invite code = path segment from https://chat.whatsapp.com/<CODE> (ignore ?query).
 * Auth session is read from OpenClaw's on-disk WhatsApp creds (survives reboot).
 */

const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
} = require("@whiskeysockets/baileys");

const os = require("os");
const path = require("path");

const AUTH_DIR =
  process.env.OPENCLAW_WHATSAPP_AUTH_DIR ||
  path.join(os.homedir(), ".openclaw/credentials/whatsapp/default");

const inviteCode = process.argv[2] || process.env.WHATSAPP_INVITE_CODE;
if (!inviteCode) {
  console.error("Usage: node resolve_group_jid.js <INVITE_CODE>");
  console.error("  e.g. node resolve_group_jid.js AbCdEfGhIjKlMnOpQrStUv");
  process.exit(1);
}

async function run() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  const sock = makeWASocket({
    auth: state,
    version,
    printQRInTerminal: false,
    syncFullHistory: false,
    logger: require("pino")({ level: "silent" }),
  });
  sock.ev.on("creds.update", saveCreds);

  const finish = (err, info) => {
    try {
      sock.ws?.close();
    } catch {}
    if (err) {
      console.error("[ERROR]", err.message || err);
      process.exit(1);
    }
    console.log("\n========================================");
    console.log("GROUP JID:", info.id);
    if (info.subject) console.log("GROUP NAME:", info.subject);
    console.log("========================================\n");
    console.log("Persist to:");
    console.log("  ~/.openclaw/openclaw.json  -> channels.whatsapp.groups");
    console.log("  .env                       -> HOUSEHOLD_GROUP_JID");
    process.exit(0);
  };

  sock.ev.on("connection.update", async (update) => {
    if (update.connection === "open") {
      try {
        finish(null, await sock.groupGetInviteInfo(inviteCode));
      } catch (e) {
        finish(e);
      }
    }
    if (update.connection === "close") {
      const code = update.lastDisconnect?.error?.output?.statusCode;
      if (code !== undefined) finish(new Error(`connection closed (${code})`));
    }
  });
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
