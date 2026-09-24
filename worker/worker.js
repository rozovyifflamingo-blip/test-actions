// Cloudflare Worker: постоянные URL для трёх скриншотов. Без карты и R2.
//
//   GET  /battle.png   — скрин боя
//   GET  /calendar.png — скрин календаря
//   GET  /log.png      — журнал боя
//   PUT  /<имя>.png    — заменить картинку (нужен заголовок
//                        Authorization: Bearer <UPLOAD_TOKEN>)
//
// Хранилище — Durable Object (SQLite): работает на бесплатном плане без
// привязки карты и строго согласовано — после PUT следующий GET сразу
// отдаёт новую картинку (у KV до минуты живёт кеш на краю сети).
// Все ответы отдаются с полным запретом кеширования.

import { DurableObject } from "cloudflare:workers";

const NAMES = new Set(["battle", "calendar", "log"]);
const MAX_BYTES = 10 * 1024 * 1024;
const CHUNK = 1024 * 1024;   // в SQLite-DO строка ≤ 2 МБ — режем на куски по 1 МБ
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

const NO_CACHE = {
  "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
  "Pragma": "no-cache",
  "Expires": "0",
  "CDN-Cache-Control": "no-store",
  "Cloudflare-CDN-Cache-Control": "no-store",
};

function reply(body, status = 200, extra = {}) {
  return new Response(body, {
    status,
    headers: { ...NO_CACHE, "X-Content-Type-Options": "nosniff", ...extra },
  });
}

// сравнение токенов без утечки по времени
async function safeEqual(a, b) {
  const enc = new TextEncoder();
  const [ha, hb] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(ha, hb);
}

// ── Хранилище картинок ─────────────────────────────────────────────────
export class ImageStore extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`CREATE TABLE IF NOT EXISTS chunks (
      name TEXT NOT NULL, idx INTEGER NOT NULL, data BLOB NOT NULL,
      PRIMARY KEY (name, idx))`);
  }

  // замена атомарная: читатель видит либо старую картинку целиком, либо новую
  put(name, buf) {
    this.ctx.storage.transactionSync(() => {
      this.sql.exec("DELETE FROM chunks WHERE name = ?", name);
      for (let i = 0, idx = 0; i < buf.byteLength; i += CHUNK, idx++) {
        this.sql.exec(
          "INSERT INTO chunks (name, idx, data) VALUES (?, ?, ?)",
          name, idx, buf.slice(i, i + CHUNK)
        );
      }
    });
  }

  get(name) {
    const rows = this.sql
      .exec("SELECT data FROM chunks WHERE name = ? ORDER BY idx", name)
      .toArray();
    if (!rows.length) return null;
    const total = rows.reduce((n, r) => n + r.data.byteLength, 0);
    const out = new Uint8Array(total);
    let off = 0;
    for (const r of rows) {
      out.set(new Uint8Array(r.data), off);
      off += r.data.byteLength;
    }
    return out.buffer;
  }
}

// ── HTTP ───────────────────────────────────────────────────────────────
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/([a-z]+)\.png$/);
    if (!m || !NAMES.has(m[1])) return reply("Not found", 404);
    const name = m[1];
    const store = env.IMAGES.get(env.IMAGES.idFromName("main"));

    if (request.method === "PUT") {
      const auth = request.headers.get("Authorization") || "";
      if (!env.UPLOAD_TOKEN || !(await safeEqual(auth, `Bearer ${env.UPLOAD_TOKEN}`))) {
        return reply("Unauthorized", 401);
      }
      const buf = await request.arrayBuffer();
      if (buf.byteLength === 0 || buf.byteLength > MAX_BYTES) {
        return reply("Bad size", 413);
      }
      const head = new Uint8Array(buf, 0, Math.min(8, buf.byteLength));
      if (!PNG_SIGNATURE.every((b, i) => head[i] === b)) {
        return reply("Not a PNG", 415);
      }
      await store.put(name, buf);
      return reply(
        JSON.stringify({ ok: true, url: `${url.origin}/${name}.png`, bytes: buf.byteLength }),
        200,
        { "Content-Type": "application/json" }
      );
    }

    if (request.method === "GET" || request.method === "HEAD") {
      const buf = await store.get(name);
      if (!buf) return reply("Not uploaded yet", 404);
      return reply(request.method === "HEAD" ? null : buf, 200, {
        "Content-Type": "image/png",
        "Content-Length": String(buf.byteLength),
        "Access-Control-Allow-Origin": "*",
      });
    }

    return reply("Method not allowed", 405, { Allow: "GET, HEAD, PUT" });
  },
};
