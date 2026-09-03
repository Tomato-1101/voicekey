// voicekey 履歴同期 Worker
// Mac と Windows のクライアント間で口述履歴を共有するための最小限の同期API。
// 利用者は本人のみを想定しているため、認証は共有Bearerトークン1本で十分と判断した。

const MAX_ITEMS = 200;
const MAX_TEXT_LEN = 20000;
const MAX_ID_LEN = 64;
const MAX_DATE_LEN = 40;
const MAX_DEVICE_LEN = 32;
const MAX_APP_NAME_LEN = 200;
const DEFAULT_LIMIT = 200;
const MAX_LIMIT = 500;

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

// トークン漏洩の入口になり得るため、比較時間からの推測を避けて定数時間で比較する。
// 長さが異なる場合は timingSafeEqual に渡す前に弾く（渡すと例外になるため）。
function timingSafeEqual(a, b) {
  const encoder = new TextEncoder();
  const aBytes = encoder.encode(a);
  const bBytes = encoder.encode(b);
  if (aBytes.byteLength !== bBytes.byteLength) return false;
  return crypto.subtle.timingSafeEqual(aBytes, bBytes);
}

// 戻り値: null=トークン未設定(デプロイ側の設定ミス), false=不一致, true=認証成功
// デプロイミスとクライアントの不正トークンを呼び出し元で区別できるようにするため3値にしている。
function checkAuth(request, env) {
  if (!env.SYNC_TOKEN) return null;
  const header = request.headers.get("Authorization") || "";
  const match = header.match(/^Bearer (.+)$/);
  if (!match) return false;
  return timingSafeEqual(match[1], env.SYNC_TOKEN);
}

function validateItem(item) {
  if (typeof item !== "object" || item === null) return "item must be an object";
  if (typeof item.id !== "string" || item.id.length === 0 || item.id.length > MAX_ID_LEN)
    return "invalid id";
  if (typeof item.text !== "string" || item.text.length === 0 || item.text.length > MAX_TEXT_LEN)
    return "invalid text";
  if (typeof item.date !== "string" || item.date.length > MAX_DATE_LEN)
    return "invalid date";
  if (typeof item.device !== "string" || item.device.length === 0 || item.device.length > MAX_DEVICE_LEN)
    return "invalid device";
  if (item.app_name !== undefined && item.app_name !== null) {
    if (typeof item.app_name !== "string" || item.app_name.length > MAX_APP_NAME_LEN)
      return "invalid app_name";
  }
  if (item.characters !== undefined && (!Number.isInteger(item.characters) || item.characters < 0))
    return "invalid characters";
  return null;
}

async function handleHistoryPost(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json(400, { error: "bad_request", detail: "invalid json" });
  }
  if (!Array.isArray(body.items) || body.items.length === 0 || body.items.length > MAX_ITEMS) {
    return json(400, { error: "bad_request", detail: "items must be an array of 1..200 elements" });
  }
  for (const item of body.items) {
    const err = validateItem(item);
    if (err) return json(400, { error: "bad_request", detail: err });
  }

  // バッチ全体で同一のreceived_atを使う。1件ずつ計測すると同時投稿の順序が
  // 見た目上ばらけてしまい、差分同期のカーソルとして扱いづらくなるため。
  const receivedAt = new Date().toISOString();
  const insert = env.DB.prepare(
    "INSERT OR IGNORE INTO history (id,text,date,device,app_name,characters,received_at) VALUES (?1,?2,?3,?4,?5,?6,?7)"
  );
  const results = await env.DB.batch(
    body.items.map((item) =>
      insert.bind(
        item.id,
        item.text,
        item.date,
        item.device,
        item.app_name ?? null,
        Number.isInteger(item.characters) ? item.characters : item.text.length,
        receivedAt
      )
    )
  );
  // 同一idはINSERT OR IGNOREで無視されるため、ネットワーク障害後に同じバッチを
  // 再送しても安全（べき等）。acceptedは実際に新規挿入された件数のみを返す。
  const accepted = results.reduce((sum, r) => sum + (r.meta?.changes ?? 0), 0);
  return json(200, { accepted, received_at: receivedAt });
}

async function handleHistoryGet(request, env) {
  const url = new URL(request.url);
  const since = url.searchParams.get("since");
  if (since && since.length > MAX_DATE_LEN) {
    return json(400, { error: "bad_request", detail: "invalid since" });
  }
  let limit = parseInt(url.searchParams.get("limit") ?? "", 10);
  if (!Number.isInteger(limit)) limit = DEFAULT_LIMIT;
  limit = Math.min(Math.max(limit, 1), MAX_LIMIT);

  const columns = "id,text,date,device,app_name,characters,received_at";
  const stmt = since
    ? env.DB.prepare(`SELECT ${columns} FROM history WHERE received_at > ?1 ORDER BY received_at DESC LIMIT ?2`).bind(since, limit)
    : env.DB.prepare(`SELECT ${columns} FROM history ORDER BY received_at DESC LIMIT ?1`).bind(limit);

  const { results } = await stmt.all();
  return json(200, { items: results });
}

export default {
  async fetch(request, env) {
    try {
      const { pathname } = new URL(request.url);

      // 認証不要にして、トークン設定ミスとネットワーク到達性の問題を切り分けられるようにする。
      if (pathname === "/health" && request.method === "GET") {
        return json(200, { ok: true });
      }

      if (pathname === "/history") {
        const auth = checkAuth(request, env);
        if (auth === null) return json(503, { error: "token_not_configured" });
        if (auth === false) return json(401, { error: "unauthorized" });

        if (request.method === "POST") return await handleHistoryPost(request, env);
        if (request.method === "GET") return await handleHistoryGet(request, env);
        return json(405, { error: "method_not_allowed" });
      }

      return json(404, { error: "not_found" });
    } catch (err) {
      // トークンなど機微な値が混ざらないよう、メッセージのみログに残す。
      console.error(err instanceof Error ? err.message : String(err));
      return json(500, { error: "internal" });
    }
  },
};
