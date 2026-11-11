"""
Google Meet 議事録ボット（Windows 版）

会議の URL を渡すと、ボット用の Chrome を裏で起動して会議に入り、Meet の字幕を読んで
議事録（Markdown）へ 1 行ずつ保存する。保存した議事録は Groq の無料モデルで要約できる。

画面から使う入口は `service.MeetBotService` と `transcript`（過去の議事録の一覧・読み出し）だけ。
残りのモジュールは部品:
- `scripts`     Meet のページ内で実行する JavaScript（Meet の DOM に依存するのはここだけ）
- `rules`       画面の文言・回数・時間から判断する純粋なロジックと、画面に出す文言
- `settle`      伸びる字幕から「確定した発言」を取り出す純粋なロジック
- `chrome`      Chrome の探索・起動と DevTools（CDP）の通信
- `transcript`  議事録の保存と読み戻し
- `summarizer`  議事録の要約（分割・429 の再試行・Groq への問い合わせ）
"""
