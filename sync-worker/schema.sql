-- date        : クライアント側での口述時刻（送信された ISO 8601 文字列をそのまま保存）
-- received_at : サーバ側で記録した受信時刻（ISO 8601 UTC）。
--               クライアント間で時計がずれていても差分同期の基準にできるよう、
--               increment sync のカーソルには received_at を使う。
CREATE TABLE IF NOT EXISTS history (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  date TEXT NOT NULL,
  device TEXT NOT NULL,
  app_name TEXT,
  characters INTEGER NOT NULL DEFAULT 0,
  received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_received ON history(received_at);
