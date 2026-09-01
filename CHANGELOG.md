# Changelog

voicekeyの変更履歴を記録するファイルです。

## [Unreleased] - 2026-08-02

### Fixed
- **「変換中」のまま永久に固まる障害を恒久対策（2026-09-02・Mac）**。外部要因
  （coreaudiod の過負荷）で `AVAudioEngine` の HAL 呼び出しが無期限ブロックすると、
  `com.voicekey.audio-control` 直列キューごと詰まり、以後の録音・文字起こしが全部止まっていた。
  **ブロック自体はアプリ側では防げない**ので、「無限に待たず、数秒で正直にエラーを出して待機へ戻す」
  ようにした（`Core/StallPolicy.swift` を新設）。
  - **録音開始ウォッチドッグ（5 秒）**: 録音開始要求を出してから開始完了が返らなければ、
    HUD に「オーディオシステムが応答しません」を出し、録音セッションを畳んで待機へ戻す。
    タイマーは**詰まり得る audio-control キューの外**（メイン側の `Task.sleep`）で回す。
    見張りは `recordingSlot` ではなく**開始完了待ちの世代**で判定する（短いタップだと開始完了より
    先に離鍵が来るため、録音中かどうかで見ると詰まりを取り逃がす）。
  - **変換中ウォッチドッグ**: 録音停止から結果が返らなければ同様に打ち切る。上限は
    REST / ストリーミング **60 秒**、ローカル（Apple）認識 **20 秒**（実障害では localstt の
    確定が永久に来なかった＝「開始」だけで「確定」が出ない形で 2 回発生）。
  - **世代ガード**: 打ち切った録音世代を `AbandonedSessions` に記録し、あとからブロックが解けて
    遅れて届いた開始完了・音声・確定テキストは**すべて捨てる**（貼り付けない・UI を触らない・
    変換中カウントを二重に減らさない）。打ち切った世代がパイプラインの末尾なら鎖を切り、
    次の録音が同じハングに巻き込まれないようにする。
  - **詰まり中の追い打ち防止**: 開始タイムアウト後は新しい録音要求を即座に断る。復帰判定は
    制御キューへ積んだ ping ブロックが実行されたかどうかだけ（`AudioRecorder.ping`）。
    **エンジンの作り直し・デバイス再列挙はしない**（HAL をループで叩くと coreaudiod ごと
    巻き込んで Mac 全体のオーディオを殺すため。再構成は既存の構成変更ハンドラの担当）。
  - 正常経路には**待ち時間を一切足していない**（ウォッチドッグは並走タイマーで、成功時は
    完了コールバックがそれを取り消すだけ）。ダブルタップの録音を作り直さない恒久要件も不変。
  - 各分岐に行動ログを追加（`録音開始タイムアウト` / `文字起こしタイムアウト` /
    `オーディオキュー復帰` / `打ち切り済みセッションの…を破棄`）。os.log は `.notice` 以上。
  - 回帰テスト: `StallPolicyTests`（10 件・バックエンド別の上限値と世代台帳の振る舞い。
    実オーディオ・実タイマーの長待ちなし）。

### Added
- **行動ログを追加（2026-09-02）**。ユーザーの操作と内部の状態遷移をファイルへ常時記録し、
  **14 日より古いログは自動削除**するようにした。
  - **きっかけ**: Mac 版が「変換中」のまま固まる障害が起きたが、アプリのログには
    「録音開始要求を出したが完了しなかった」を示す行が無く、`sample(1)` を取るまで原因
    （`AVAudioEngine.inputFormat(forBus:)` が HAL キューへの `dispatch_sync` で無期限ブロックし、
    `com.voicekey.audio-control` 直列キューごと詰まっていた）を特定できなかった。
    **要求と完了が対で残っていれば、完了行の欠落だけで場所が分かる**。
  - **Mac**: `Core/ActionLog.swift` を追加。出力先は `~/Library/Logs/voicekey/voicekey-YYYY-MM-DD.log`、
    1 行 = `HH:mm:ss.SSS [category] message`。カテゴリは既存の os.log と同じ名前
    （`audio` / `hotkey` / `app` / `transcriber` / `formatter` / `paster` / `localstt` / `main` /
    `caption.*`）。**`write` は即 return**（整形もファイル I/O も専用の直列キュー・qos utility で行う）
    ＝ディクテーションのクリティカルパスに 1ms も足さない。1 行ずつ書き切るのでハングしても直前まで残る。
    起動時と日付切替時に 14 日より古いファイルを削除し、1 ファイルが 20MB を超えたら同日でも
    `voicekey-YYYY-MM-DD.2.log` へ送る。
  - **記録する行動**: ホットキーの押下／離鍵（スロット・hold/toggle・ダブルタップ判定の結果）、
    **録音開始要求（キューへ投げる直前）と録音開始完了**、タップ設置・エンジン開始（デバイス・サンプルレート）、
    録音停止（録音時間）、文字起こしの要求／応答／エラー、整形の開始／完了／エラー、貼り付けの実行／完了、
    HUD の表示遷移、オーディオ構成変更・デバイス切替・イベントタップの再有効化、設定変更の反映、
    アプリの起動／終了、ライブ字幕の開始／停止、Meet ボットの開始／終了、ローカル認識の開始／確定／終了。
  - **本文は残さない**（文字起こし・整形・貼り付けはいずれも**文字数だけ**）。
  - **Windows**: `utils/logger.py` のファイルハンドラーを `TimedRotatingFileHandler`
    （`when='midnight'` / `backupCount=14`）へ差し替えた。従来は `mode='w'` で**起動のたびに上書き**
    していたため、再起動を挟むと障害直前の行動が消えていた。あわせて Mac と同等の行動ログ行
    （ホットキー押下／離鍵、録音開始完了、文字起こし要求、整形開始、貼り付け実行／完了、UI 状態）を
    既存の logger へ追加した。
  - 回帰テスト: `ActionLogTests`（9 件・削除対象判定の純関数＋一時ディレクトリでの書き込み・
    連番ローテート・起動時削除）、`tests/test_logger.py::TestLogRotation`（4 件・日付ローテート設定と
    保持 14 日の削除判定、起動時に既存ログを切り詰めないこと）。

### Technical Details（行動ログ）
- **ActionLog.swift**: `ActionLog.shared.write(_ category:_ message:)` が唯一の入口。
  `filesToDelete(names:today:retentionDays:)` を純関数として切り出し、ファイル名の日付だけで
  削除対象を決める（テスト可能・自分のログ以外は絶対に消さない）。`flush()` はアプリ終了時とテスト専用。
- **AudioRecorder.swift**: 開始・停止とも**キューへ投げる前**に要求行を書く（キューが詰まると
  `queue.async` のブロックは一生実行されないため、中で書いても形跡が残らない）。
  完了行のためだけに HAL を叩かない（デバイスは名前ではなく既知の UID で残す）。
- **Google Meet 議事録ボットを追加（2026-08-28）**。会議 URL を渡すと**ボットが裏で会議に入り**、
  Meet の字幕を**話者名つき**で議事録へ保存する。入口はメニューバー「Meet 議事録ボット」→「会議に参加…」。
  - **方式**: 追加依存を増やさないため、**すでに入っている Chrome を DevTools プロトコル（CDP）で
    動かす**。Playwright / Node を同梱すると Chromium ごと数百 MB 抱えることになるため採用しなかった。
  - **文字起こしの取得元は 2 択**（メニューの「文字起こしの方法」・`CaptionSettings.meetTranscriptSource`）。
    経緯: 8/28 に「ミーティング系の文字起こしは全部ローカルの計算で」でローカル認識へ寄せ、
    8/31 に「Google Meet にもともとあるのか。じゃあそれにして。やっぱ」で**既定を Meet の字幕に戻した**。
    両方の経路を残してあるので、メニューでいつでも切り替えられる。
    - **Meet の字幕**（既定）: 話者名が取れる。認識は Google 側。伸びる字幕の確定判定は
      `CaptionSettleTracker` に切り出した（純ロジック・9 件の回帰テスト）。
      テストを書く過程で「言い終わった字幕が画面に残り続けると同じ行を再確定してしまう」穴を見つけ、
      話者ごとに直前の確定文を覚えて弾くよう修正した。
    - **端末内（Apple）**: 会議音声を **PID を固定したシステム音声タップ**で拾ってオンデバイス認識にかける。
      音声もテキストも外へ出ない。話者名は会議画面から読めたときだけ添える。
      この経路のときだけ、記録開始前にライブ字幕を止める（同じ音を 2 本のタップで拾わない）。
    - **ヘッドレスの Chrome でも音は出る**ことを実測（RMS 0.21 / 156,775 フレーム）。
      さらに「ブラウザの再生音声 → ローカル認識」を通しで確認し、原稿どおりの文字起こしを得た
      （`明日の午後 3時に渋谷駅で打ち合わせをしましょう。資料は事前に共有しておきます。`）。
  - ボットは**マイク・カメラを切って**参加する（`--use-fake-device-for-media-stream` と
    参加前のミュート操作の二重で担保）。
  - 専用プロファイル（`~/Library/Application Support/voicekey/meetbot-profile`）でヘッドレス起動するので、
    普段使いの Chrome のタブを巻き込まない。初回だけメニューの「ボット用ブラウザで Google にログイン…」で
    本人がログインする（ログイン操作は本人にしかできないため）。
  - **Meet の DOM 依存は `MeetBotScripts.swift` の 1 ファイルに閉じ込めた**。Meet の実装は予告なく
    変わるので、壊れたらここだけ直せばよい。クラス名に頼らず `jsname` / `aria-label` / 表示文字（日英）で探し、
    駄目なら次の手掛かりへ落ちる作りにしてある。
  - ハーネスを 3 つ追加（いずれも `[VERDICT] status=ok` で判定）:
    `--meetbot-test [URL]`（Chrome 起動 → CDP 接続 → JS 実行 → 画面の手掛かりを報告）、
    `--meetbot-audio-test [秒]`（ヘッドレス Chrome の音を拾えるか）、
    `--meetbot-stt-test <音声> --expect <語,…>`（ブラウザの音 → ローカル認識まで通しで文字が出るか）。
    疎通ハーネスの実測で Cookie 同意画面が挟まることが分かったため、参加前に同意を閉じる処理を入れてある。
  - 回帰テスト: `MeetBotURLTests`（5 件・Meet 以外の URL でブラウザを起動しない）。
- **ライブ字幕に「文字起こし」モードを追加（2026-08-28）**。従来のライブ翻訳（英→日）に加えて、
  **聞いた言葉を訳さずそのまま字幕にする**モードを選べるようにした。認識する言語は**日本語 / 英語**。
  - Apple の `SpeechTranscriber` は**ロケールを指定して起動する**仕様で自動言語判定を持たないため、
    言語はユーザーがメニュー / 設定タブで選ぶ（切り替えると認識セッションを張り直す）。
  - 翻訳モードの認識ロケールは従来どおり英語固定（`AppleTranslator` が en→ja の一方向のため）。
  - 文字起こしモードでは翻訳器を一切通さない（訳す時間も課金も発生しない）。
- **議事録（文字起こしのローカル自動保存）**。字幕が動いている間、確定した文字起こしを
  `~/Documents/voicekey/transcripts/YYYY-MM-DD_HHmm.md` へ時刻つきで追記する。
  - **保存するのは文字起こしだけ**（訳文は保存しない）。翻訳モードのときは原文（英語）が残る。
  - **5 分以上あいたら別ファイル**にする（午前の会議と午後の会議が 1 ファイルに混ざらないように）。
  - 追記型にしたのは、アプリが落ちてもスリープしても**書いた分は必ず残す**ため。
  - 話者名つきの行（`**話者**: 本文`）にも対応（Google Meet ボットで使う）。
  - 回帰テスト: `TranscriptRecorderTests`（7 件）。

### Changed
- **ライブ字幕の「起動時に自動開始」を既定 OFF に変更（2026-08-28 ユーザー指示）**。
  「最近ライブ翻訳が勝手に始まる」ため。開始はメニュー（⌥⌘S）か設定からの明示操作で行う。
- **文字起こしバックエンドの棚卸し（2026-08-27）**。難易度の高いベンチ（数字・日付・金額・人名を
  含む 44 秒の原稿＋その早口・雑音版）で選択肢を測り直し、**Gemini 3.5 Transcribe を選択肢から
  外した**（同日追加した `5787692` を revert）。
  - **理由**: 実測で既存の選択肢に**速度でも精度でも負けた**。CER はアプリと同じ数字正規化を
    通した値（`benchmark/rescore.py`）。

    | エンジン | CER（素/早口＋雑音） | 確定までの時間 | 費用 |
    |---|---|---|---|
    | ローカル（Apple） | 2.4% / 0.6% | 0.4s / 0.3s | 無料・オフライン |
    | Groq whisper-large-v3-turbo | 0.0% / 1.2% | 1.0s / 0.8s | 安い |
    | Gemini 3.5 Transcribe | 3.6% / 3.0% | 4.8s / 4.1s | 約 $0.005/分 |

  - 整形込みで返るのが売りだが、その利点は後段の Groq 整形で足りる。**1 分あたりの課金が
    増えるだけで得るものが無い**と判断した。ライブ字幕の翻訳エンジンとしての Gemini は従来どおり残す。
  - ベンチ資産（難易度の高い音声・アプリ基準の再採点・ストリーミング計測）は `d20c983` で残してある。
    再検討するときはそこから測り直すこと。

### Fixed
- **録音のたびに「モデルダウンロード中」通知が出る件の残りと、ピルに前回の入力が残る件を直した（Mac・2026-08-27）**。
  ユーザー報告「このバグまだ治ってない」「入力し終わった後にすぐまた入力を始めると、
  ピルに前回の入力の文字が残る」。前回（`af6b3a1`）の猶予 0.8 秒では**根が残っていた**。
  - **原因 1（前回の文字が残る）**: ライブ字幕（ストリーミングの暫定テキスト）を**次の録音の開始時**に
    消していた。ところが確定は**離鍵より後**に届く（実測でローカル認識は離鍵の 100ms 前後あと・
    パイプラインが詰まればさらに遅れる）。すぐ次の録音を始めると、その取りこぼしが新しい録音の
    ピルへ書き込まれる。ピルは `liveText` が空でないと波形バーを描かないので、
    「**前回の文字が出たまま波形が出ない**」状態になっていた（＝「録音表示が出ない」の正体）。
    - **修正**: 消すタイミングを「**録音が終わった時**」へ移し（`HudController.applyState` の
      transcribing / idle）、さらに `AppController` に**録音セッションの世代**を持たせて、
      前の録音のコールバックは main で捨てる（`recordingGeneration`）。
  - **原因 2（通知で録音表示が消える）**: 通知の自動消灯が「変換中なら戻す・それ以外は隠す」だったため、
    **録音中に通知が出ると 2 秒後に HUD ごと消えて戻らなかった**。モデル準備の進捗に限らず
    「音声が検出されませんでした」等でも同じことが起きる。
    - **修正**: `restoreAfterNotice` でその時点の実状態（録音中・変換中・待機）へ戻す。
  - **原因 3（アセット確認が毎回走る）**: `AssetInventory.status` が導入済みでも `.supported` を返すため、
    録音のたびにダウンロード要求の経路を通っていた（実測 3〜9ms）。猶予 0.8 秒は「たまたま遅かった 1 回」を
    防げない＝通知暴発の根が残る。
    - **修正**: 確認できたロケールを**プロセス内にキャッシュ**して 1 回だけにする
      （認識開始に失敗したら取り消して再確認へ戻す）。あわせて進捗は**通知ではなくピルの中**に出す
      （通知はピルを置き換えてしまうため）。文言も `モデル準備中 N%` に短縮してピル幅に収めた。
  - **回帰テスト**: `HudStateTests`（11 件）に通知からの復帰・ライブ字幕の持ち越し・
    auto_enter 昇格でのライブ字幕保持を固定。`swift test` 181 件 PASS /
    Windows `unittest` 522 件 PASS。

- **personal: ローカル（Apple）文字起こしで録音のたびに「ダウンロード中」通知が出て録音表示が消えるのを直した（Mac・2026-08-23）**。
  ユーザー報告「録音するたびに HUD にモデルロード中が出て、録音中の波形ピルが出ない」（認識自体は正常）。
  - **原因**: `AssetInventory.status` は**言語モデルが導入済みでも `.supported` を返すことがある**ため、
    `ensureAssetsInstalled` の `status != .installed` 分岐を録音のたびに通っていた。進捗タスクが即座に
    「ダウンロード中 0%」を `onAssetProgress` → HUD notice へ流し、その通知が録音中の表示を上書きしていた。
    実ダウンロードは起きておらず、実測で `downloadAndInstall()` は **5ms で完了**していた
    （ログの「ダウンロード中 0%」→「完了しました」が 5ms 差だったのが決め手）。
  - **修正**: 進捗は**実際に待たされているときだけ**出す。進捗タスクの先頭に 0.8 秒の猶予
    （`assetProgressGrace`）を置き、それを超えてまだ終わっていない場合の最初の tick から通知する。
    即完了時は通知を一切出さず、ログも「言語モデルは導入済み（即完了 Nms）」の 1 行に留める。
    「0%」を最初に出す実装はやめた。**初回の本物のダウンロードでは従来どおり進捗が出る**。
  - 字幕も同じ `ensureAssetsInstalled` を通るが、字幕は `onAssetProgress` を設定していないため挙動は不変。
  - **実測**: 猶予つき進捗タスクの意味論を最小再現で確認（即完了 5ms → **通知 0 回**・ログ「即完了 6ms」/
    1.6 秒かかる場合 → **通知 1 回**・ログ「ダウンロードが完了しました」）。
    `scripts/dev/local_stt_e2e.sh` のウォーム実行で `[ASSET]` 行が 1 件も出ず、認識は原文と完全一致・
    確定 278ms。`swift test` 162 件 PASS。

- **Windows: コードレビュー指摘の内部欠陥をまとめて修正（2026-08-23 に release から移植）**。
  ユーザーから見える機能・UI の変更はなし（挙動の劣化を防ぐ修正のみ）。personal の `src/` は
  2026-07-18 から未更新で、release の同修正（`ecb23d3`）が未反映だったため、単一ブランチ化に
  合わせてそのまま取り込んだ（対象 8 ファイル＋テスト 1 件は移植前の時点で release の親と
  バイト単位で同一だったので、差分は完全に一致する）。
  - **ストリーミング確定スレッドの 4 秒スタール**（`core/streaming_transcriber.py`）: 短命トークン取得を
    `except backend_client.BackendError` だけで守っていたため、応答に `token` が無い等の想定外例外で
    受信スレッドが `_resolve_finish()` を呼ばずに死に、`finish()` が猶予 1 秒＋タイムアウト 3 秒を
    フルに待ってから REST フォールバックしていた。`except Exception` に広げて必ず解決するようにした。
  - **プロキシの非 JSON 200 がユーザー向けエラー処理を素通り**（`core/backend_client.py`）: 200 応答を
    無条件に `resp.json()` していたため、プロキシが HTML/テキストを返すと `JSONDecodeError` が
    呼び出し側の `except BackendError` を抜けていた。`_json_body()` で `BackendError` へ正規化。
  - **即時入力（JWT 直叩き）が接続プールを使っていなかった**（`core/api_transcriber.py`）: 録音ごとに
    TCP+TLS を張り直していた。認証ヘッダーを固定しない共有クライアント（`_get_jwt_client`）を持たせ、
    `prewarm()` / `close()` もそれを扱うようにした。
  - **貼り付け失敗時にクリップボード原本が復元されない**（`core/input_handler.py`）: コピー以降を
    try/finally にして必ず復元を予約する。
  - **更新インストーラの TOCTOU**（`utils/updater.py`）: 検証済みバイト列を `tempfile.mkstemp` の
    排他生成ファイルへ書き出し、そちらを起動する。
  - **DEFAULT_CONFIG の共有参照汚染**（`config/config_manager.py`）: 土台を `copy.deepcopy(DEFAULT_CONFIG)` に統一。
  - **長文録音で VAD 推論が 2 回走る**（`core/vad.py`）: 同一配列に対するフレーム確率をキャッシュして 1 回に減らす。
  - **致命的エラーのログ書き出し先が CWD 依存**（`main.py`）: `startup_log.txt` と同じ OS 標準ログ
    ディレクトリに固定し、失敗時は標準エラー出力へフォールバックする。
  - **Technical Details**: `tests/test_api_transcriber.py` の `test_deepgram_uses_jwt_when_logged_in` を
    共有クライアント前提へ更新（`httpx.post` パッチ → `_get_jwt_client` パッチ）。
    検証: `py_compile` 全 8 ファイル OK / `QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests` **512 件 PASS**。

### Fixed
- **personal: 「翻訳して入力」で話す言語と出力言語が同じときに毎回エラー通知が出るのを直した（Mac・2026-08-23）**。
  実機ログで出力言語に「日本語」を選んだ状態（ja→ja）を検出。Apple の翻訳は同一言語ペアを受け付けないため
  `オンデバイス翻訳を利用できません` で失敗し、入力のたびに「翻訳できなかったため原文を入力しました」の
  通知が出ていた。同一ペアは**訳す必要が無い**ので、失敗扱いにせず原文をそのまま最終テキストとして返す
  （実測 0ms・通知なし）。回帰テストを 1 件追加し、検証ハーネス側も同一ペアを no-op として判定するようにした。

### Changed
- **単一ブランチ運用（personal 一本）へ移行し、ドキュメントを実態へ書き換え（2026-08-23）**。
  旧 `main`（自分用）/ `release`（製品版）/ `voice-agent` を `personal` へ統合してアーカイブした
  （GitHub からも削除。バックアップは `~/Project/_archive/voicekey-all-branches-2026-08-23.bundle`）。
  製品版の顧客配布・課金・アクティベーションキーの運用は終了し、販売まわりのリポジトリ
  （`voicekey-site` / `voicekey-releases`）は別リポジトリとして現状維持で切り離す。本リポジトリは今後も PRIVATE。
  - `README.md`: 商用版の「配布ステータス表・価格・ダウンロード導線・ポータル経由の入手手順」を撤去し、
    「開発者本人が毎日使う単一バージョン」の実態へ書き換え。ログイン前提だった記述（実績のアカウント連携・
    オンボーディングのログインステップ・API キーの入手案内）を personal の実態（中央 Keychain 直読み・
    ログイン画面なし）に合わせた。バージョン表（`APP_VERSION` との整合をテストが検査する）は維持。
  - `CLAUDE.md` / `OVERVIEW.md` / `AGENTS.md`: 「2 ブランチ運用」の節を「単一ブランチ運用」へ改訂し、
    本文中のブランチ名への言及を最小修正した（**恒久要件・教訓・両 OS 同時実装・README/OVERVIEW 更新ルール・
    ライブ字幕節・コメントルールなど他の節は内容を変更していない**）。
  - `HANDOFF.md`: **現役の恒久要件**（API キーを git にコミットしない／自動アップデート必須／ソース非公開／
    Apple Developer Program は販売時／Mac のビルド→kill→open 手順／UI スモークで keyring をモック）が
    含まれるため**残した**。ブランチ運用の 1 項目だけ実態へ最小修正。
  - 陳腐化した内部文書 4 件を削除（`FREE_LAUNCH_PLAN.md` / `KEY_ROTATION_runbook.md` /
    `TEST_v1.3.0_activation.md` / `competitor-research-2026-06-29.md`）。いずれも 2026-06 の製品ローンチ計画で、
    release では既に削除済み。git 履歴とバックアップ bundle に残る。

### Added
- **personal: 設定画面にモデル選択と整形の編集 UI を復元（Mac・2026-08-23 に main から移植）**。
  自分用ビルドは「何が動いているか隠さない」方針なので、製品版で固定していた項目を選べるようにした
  （保存先 `formatModel` / `autoFormatPrompt` / スロット別 `model` は personal に元からあり、UI だけ欠けていた）。
  - 録音キータブ: **モデル Picker**（先頭が「（推奨）」表記）。選択肢が 1 つだけのエンジン
    （ローカル（Apple））では出さない。
  - 一般タブ: **整形モデル Picker**（保存済みモデルがリスト外でも選択を保持）と
    **整形の指示（プロンプト）の編集欄＋「既定に戻す」**。
- **personal: 文字起こしバックエンドに「ローカル（Apple）」を追加（Mac・macOS 26+・2026-08-23）**。
  動機はユーザー要望「Talkify がすごく速い」＝**速度**。Apple のオンデバイス音声認識
  （SpeechAnalyzer / SpeechTranscriber）で **Mac の中だけ**で文字起こしする。
  - **通信も API キーも使わない**（`Keychain.apiKey(for: .appleLocal)` は Keychain を引かず必ず nil）。
    オフラインで動き、音声が外部へ出ない。
  - **Deepgram ストリーミングと同じ「離した瞬間に入力」型**。`LiveTranscribing` に適合させ、
    録音中の chunkHandler から並行して解析器へ投入し、キー離しで finalize → 即確定する。
    解析器の準備（数百 ms）が終わるまでのチャンクは退避してから流すので **1 サンプルも捨てない**。
  - 認識言語は既存の「言語」設定に従う（空なら OS の言語）。**初回だけ言語モデルのダウンロード**が走り、
    進捗を録音 HUD に出す（無言で固まらない）。
  - 認識器は字幕と同じ `Caption/Speech/SpeechRecognizer` を共用する（重複実装でドリフトさせないため）。
    **字幕とディクテーションで SpeechAnalyzer を 2 本同時に走らせられる**ことをハーネスで実測済み。
  - **恒久要件（他バックエンドのクリティカルパスに 1ms も足さない）を守る**: ローカルを選んでいない限り
    何も生成・初期化しない。`Transcriber` の HTTP 経路には到達しない（`transcribe()` の冒頭で分岐）。
  - **実測（`scripts/dev/local_stt_e2e.sh`・マイク不要）**: 日本語 4.55 秒の音声で認識テキストが
    **原文と完全一致**（`今日は良い天気ですね。音声入力のテストをしています。`）。確定までの所要は
    初回 3962ms（モデル DL 込み）→ **2 回目 155ms** → 字幕と並走させて **92ms**。
  - **Technical Details**: `Core/LocalSpeechTranscriber.swift`（新規）・`Config/AppConfig.swift`
    （`Backend.appleLocal`・`selectableCases` を macOS 26 でゲート）・`Core/Transcriber.swift`
    （`transcribeLocally`＝ストリーミングが空だったときの 1 発フォールバック）・`Core/Keychain.swift`
    （キーを引かない）・`AppController.swift`（`makeLiveTranscriber` / DL 進捗の HUD 配線）・
    `UI/SettingsView.swift`（モード説明）・`CLI/DictationTestMode.swift`＋`scripts/dev/local_stt_e2e.sh`（新規ハーネス）。
- **personal: 「翻訳して入力」を追加（Mac・macOS 26+・2026-08-23）**。
  話した内容を訳してから貼り付ける。本命は「**日本語で話す → 英語が入力される**」。
  - **全体で 1 つのトグル**（スロット単位ではない・ユーザー決定）＋**出力言語は選択式**
    （英語＝既定 / 中国語（簡体）/ 韓国語 / スペイン語 / 日本語）。設定は設定ウィンドウの
    「翻訳して入力」タブに集約（字幕設定と同じ精神）。
  - **翻訳エンジンは 2 択**: Apple のオンデバイス翻訳（既定・無料・オフライン）と Groq（LLM）。
    **Gemini は選択肢に出さない**（勝手に課金を発生させない恒久方針）。新しい有料 API も追加しない。
  - どのバックエンド（クラウド / ローカル）の結果にも効く。**貼り付け直前の最終テキストに 1 回だけ**
    適用する（部分訳はしない）。**トグル OFF のときは経路に一切触れない**。
  - 適用順は 整形 → 数字正規化 → ユーザー辞書 → **翻訳**。正規化と辞書は「原文の誤認識を直す」ための
    ものなので翻訳より前に効かせる。
  - 録音開始時に翻訳セッションを prepare して初回のモデルロードを隠す（ダウンロード承認 UI は出さない）。
    **翻訳に失敗したら必ず原文を貼る**（テキストを失わない）。無言にならないよう HUD で知らせる。
  - 整形と翻訳を両方 ON にすると LLM を 2 回呼ぶ。**整形を勝手に無効化はせず**、設定画面に注意書きを出して
    ユーザーが選べるようにした（Apple 翻訳はローカルなので実質クラウドは 1 回）。
  - **実測（`--translate-test`・実機）**: `今日は良い天気ですね。音声入力のテストをしています。` →
    `It's a nice day today. I'm testing the voice input.` を **243ms**（Apple オンデバイス・ja→en）。
  - **Technical Details**: `Core/DictationTranslator.swift`（新規・設定 `DictationTranslation` と
    Apple / Groq の翻訳器）・`Caption/Translation/GroqTranslator.swift`（`systemPromptProvider` を追加＝
    翻訳の向きを差し替え可能に。既定は従来どおり字幕の英→日）・`Config/AppConfig.swift`（@Published ミラー）・
    `AppController.swift`（`translateIfEnabled` を 2 つの貼り付け経路へ）・`UI/SettingsView.swift`
    （「翻訳して入力」タブ）。回帰テスト `Tests/VoicekeyTests/LocalSttAndTranslateTests.swift`（新規 10 件）。

### Fixed
- **personal: ライブ字幕を出しっぱなしにするとメモリが 1 日 100MB ずつ増え続けるのを直した（Mac・2026-08-23）**。
  - **原因**: Apple Speech フレームワーク内部（`SpeechRecognizerWorker.processAudio` の "PerfMeasurements"）が、
    SpeechAnalyzer のセッションが生きている間ずっと `[(Double, UInt64?)]`（要素 24B）へ **投入バッファ 1 個
    （約 11.6ms）につき 1 要素を追記し続ける**。上限も間引きも無く、アプリ側から解放する API も無い。
    字幕はシステム音声タップから**無音でもバッファが流れ続ける**ため、字幕 ON の間は 24 時間伸び続ける。
    実測: 常駐 4.7 日のプロセスで単一の配列が **512MB**（要素 1,950 万）、footprint の peak が 638MB。
  - **修正**: `CapturePipeline` に**認識セッションの定期リサイクル**を入れた。認識器（`SpeechRecognizer`）と
    その入力形式の変換器・結果の受け口を `Session` 1 値にまとめ、新セッションを裏で用意してから
    アトミックに差し替え、旧セッションは最後の確定を吐き切らせてから畳む。
    **`SystemAudioTap` は張り替えない**（TCC の許可と対象アプリのロックを維持するため）。
  - **発動条件**（既存の 1 秒レベルタイマーで判定）: 無音が 8 秒以上続いていて、かつセッション経過 30 分以上
    → 差し替え（字幕が出ていない静かな瞬間を狙うので体感の途切れが出ない）。音が鳴りっぱなしでも
    セッション経過 4 時間で強制的に差し替える（上限保証。30 分 ≒ 3.7MB / 4 時間 ≒ 30MB なので実害なし）。
  - 新しい解析器では音声タイムラインが 0 に戻るため、`analysisStartedAt` / `firstAudioAt` も
    セッション単位で入れ替える（`CaptionLatencyLog` が異常値を出さないように）。
  - **実測（`--caption-pipeline-test` + `VOICEKEY_CAPTION_RECYCLE_TEST_SECS=600` で 25 分・無音環境）**:
    `heap` の最大確保が直前 **2048KB[1]** → 直後 **64KB[2]**（2MB ブロックが消滅）、malloc 合計は
    8,316,869B → 6,313,413B（**-2,003,456B ＝ ちょうど 2MB**）。`vmmap` の footprint も 21.8MB → 19.2MB。
    差し替えの所要は **62ms**（ログ 17:17:32.658→.720）。`swift test` 151 件 PASS。
  - **Technical Details**: `macos/Sources/Voicekey/Caption/Pipeline/CapturePipeline.swift`
    （`Session` 構造体・`session` ロック・`makeSession()`・`checkRecycle(rms:)`・`recycle(reason:elapsed:)`、
    `handleCapturedBuffer` は差し替えと競合しないよう「変換 → 集計 → 投入」をロック内で完結）、
    `macos/Sources/Voicekey/Caption/CaptionSettings.swift`（`recycleTestSeconds`＝ハーネス用の閾値短縮 env）。
- **personal: ダブルタップ（auto_enter）が「たまに検出されない」のを直した（Mac・2026-08-15）**。
  ユーザー指摘「ダブルタップでの入力がたまに検出されない。もっと絶対に検出されるようにしてほしい」。
  - **原因**: 単一の窓 `kDoubleTapWindow = 0.4` が **2 か所**に効いていた。
    (1) `handleRelease` は 1 打目の**押しっぱなし時間**が 0.4 秒未満のときだけ 1 打目として記録し、
    (2) `handlePress` は**離鍵→2 打目の押下**が 0.4 秒未満のときだけ成立とした。
    人間の「タップ」は 400ms を普通に超えるので、どちらか一方で簡単に外れて黙って通常録音になっていた。
  - **追加のユーザー指摘（同日）**: 「1 打目では録音されず、2 打目から録り直される。やめてほしい。
    auto_enter でもそうでなくても、録音が始まるタイミングは同じにしてほしい。1 打目の音声も
    消さずにそのまま使えばいい」。旧実装は 1 打目の離鍵で録音を確定・破棄し、2 打目で
    **新しい録音を作り直して**いた（＝1 打目に入った声が消え、auto_enter のときだけ録音の
    開始点が 2 打目までずれる）。
  - **修正**: 判定を純粋型 `DoubleTapPolicy`（`Core/DoubleTapPolicy.swift`）へ切り出したうえで、
    **1 打目の離鍵で録音を止めない**設計にした。短いタップの離鍵では録音を続けたまま「保留
    （`Pending`）」に入り、2 打目が来たら **同じ録音を `autoEnter = true` に切り替えるだけ**。
    録音を作り直さないので、1 打目の音声は消えず、録音開始は常に最初の押下（auto_enter か
    どうかで変わらない）。2 打目が来なければ、待っていたぶんの末尾を切り落として普通に確定する。
  - **しきい値**: ホールド上限 `kTapHoldMax = 0.75`（＝これ以下なら保留に入る。長い口述の直後に
    素早く押し直しただけで Enter が自動送信される事故を防ぐガードで、**判定を 2 か所に散らさず
    ここ 1 か所に集約**したのが旧実装との本質的な差）。間隔窓は `NSEvent.doubleClickInterval` に
    追従（下限 0.5 秒・上限 1.2 秒でクランプ）。この窓はそのまま「2 打目が来ない単発タップの確定が
    どれだけ遅れるか」の上限にもなるので、無闇に広げない。
  - **測れるようにした**: 判定のたびに `.notice` で `slot / hold / gap / hold上限 / 窓 / 結果`
    を残す（不成立は `hold超過` `gap超過` `2打目なし` `別スロット` `1打目なし` と理由つき）。
    `log.info` は永続化されず `log show` で追えないため。
  - 実測: 旧しきい値なら落ちる 2 ケース（1 打目 hold 0.52 秒／離鍵→押下 0.45 秒）が新実装で成立する
    ことをテストで機械的に確認。`swift build -c release` 成功、`swift test` **148 件 PASS**
    （新規 16 件・`Tests/VoicekeyTests/DoubleTapPolicyTests.swift`）。録音を止めない状態遷移自体は
    `AppController` 側なので単体テスト範囲外＝実機のログ（`ダブルタップ判定` 行）で確認する。
- **personal: 字幕の裏に待機ピルが透けて「字幕とピルが別物に見える」のを直した（統合フェーズ 6・Mac・2026-08-12）**。
  ユーザー指摘「今なんかピルが統合されなくなったんだけど、なんで？」。
  - **原因**: 字幕パネルは待機ピルのカプセル下端（`visibleFrame.minY + 8`）に揃えて上へ伸びる
    ＝「ピルがそのまま字幕へ育つ」設計なのに、**字幕が出ている間もピルを消していなかった**。
    字幕ガラスは alpha 0.62 の半透明なので、裏に残ったピル（実測 63×12.5pt。
    `HudView` の `.idlePill` はコメントどおり「概ね 64×14」）が字幕テキストの裏へ
    濃いカプセルとして透け、2 つの物体が重なって見えていた。
    実装されていたのは「音声入力中は字幕を隠す」の一方向だけだった。
  - **修正**: `CaptionHUDController.onVisibilityChanged` → `CaptionService.onHUDVisibilityChanged` →
    `AppController` → `HudController.captionCovering` の一方向で結線し、字幕が出ている間は
    待機ピルを引っ込める。戻すのは**畳みアニメが終わってから**（縮んでいく 0.22 秒の途中で
    戻すと、まさに同じ透け方が再発するため）。干渉するのは `.idlePill` だけで、
    録音・変換中・通知には触らない（その間は字幕側が `isSuppressed` で自分から隠れる）。
  - **回帰ハーネスの穴も塞いだ**: `--caption-hud-test` は字幕の座標を**ピル位置を写した定数**と
    比べるだけで、実在するピルのウィンドウを一度も見ていなかった（だから status=ok のまま
    この不具合を素通しした）。実ウィンドウの可視状態を見る「ピル退避」「ピル復帰」フェーズを追加。
    結線を外すと `status=fail failures=字幕が出ているのに待機ピルが残っている` を実際に出すことを確認済み。
  - 実測: `--caption-hud-test` status=ok（ピル退避 pill=no / ピル復帰 pill=yes 96×22）、
    `--caption-mic-coexist-test` status=ok cycles=3 rebuilds=0、`swift test` 121 件 PASS。

### Changed
- **Mac: 録音開始に失敗したときの HUD 通知を原因別の文言にした（2026-08-16）**。
  一律「録音を開始できませんでした（マイクを確認）」だったため、ローカル LLM がメモリを
  食い尽くして coreaudiod が IO を開始できず `AVAudioEngine.start()` が OSStatus
  **2003329396（'what' = kAudioHardwareUnspecifiedError）** で失敗し続けた実事故で、
  マイクの故障と区別が付かなかった。`AudioRecorder.StartFailure` で失敗理由を分類し、
  このコードのときは「**メモリ不足**でマイクを開始できませんでした」、デバイス消失なら
  「マイクが見つかりません」、その他は OSStatus のコード付きで表示する。
  失敗時のログにも `code=` を `privacy: .public` で残す（`localizedDescription` は
  `<private>` に潰れて coreaudiod 側ログと突合できなかったため）。
  分類は失敗パスでのみ実行するので録音開始のクリティカルパスには影響しない。
  回帰テストは `Tests/VoicekeyTests/AudioStartFailureTests.swift`。
- **personal: ライブ字幕を「節ごとに刻んで」訳すようにし、ライブ行をハイブリッド化した（統合フェーズ 5・Mac・2026-08-10）**。
  ユーザー指摘「一文が長すぎて翻訳されるまで時間がかかりすぎる。もっと文を区切れ区切れにして。
  ある程度長くなったら一旦すぐ翻訳して」。
  - **区切りの強化（`TranslationCoordinator`）**: 上限 80 字 → **48 字**、無音での強制送出 0.7 秒 → **0.45 秒**、
    32 字を超えたらカンマ（`", "`）や接続詞（`" and " / " but " / " so " / " because " / " then "` 等）の
    **直後**で送出する節区切りを追加した（残りは次のバッファへ持ち越し）。
    判定順を **「長さ → 文末 → 節区切り」** にしたのが要点。音声認識は 40 語超の 1 文を
    まるごと 1 件の確定として渡してくる（実測 199 字）ため、文末判定を先に見ると
    その巨大な 1 文がそのまま 1 行として出てしまい、刻みがまったく効かなかった。
  - **ライブ行のハイブリッド化（`CaptionService`）**: クラウド（Gemini / Groq）選択時も、
    ライブ行の暫定訳だけ **Apple のオンデバイス翻訳**で出すようにした。無料・ローカルなので
    「クラウドへは確定文しか送らない」恒久要件に抵触しない。確定したらクラウドの訳で置き換わる。
    言語モデル未導入の環境では従来どおり原文だけのライブ行になる（ダウンロード承認 UI は出さない）。
  - 副次的な改善: クラウド選択時にも共有の `AppleTranslator` を準備するようになったため、
    429（レート制限）や通信断で `FallbackTranslator` が Apple へ落ちる経路が**実際に機能する**ようになった
    （これまではクラウド選択時に Apple が未準備で、落とし先まで失敗して字幕が出なかった）。
  - 実測（44 語・199 字の英文を `say` で再生、エンジン = Groq）:
    刻み数 **1 → 5**、確定から最初の日本語が出るまで **1110ms → 176ms**、
    発話終了から最初の訳表示 **1.70s → 0.87s**（全 5 行が 1.81s で出揃う）。
    ライブ行の暫定訳は before が **0 件**（クラウドでは無効だった）→ after は発話中ずっと更新（音声から 123ms）。
    `[COVERAGE] status=ok`（199 字中 199 字）を維持し、刻んでも 1 文字も落としていない。
  - 回帰テスト `TranslationBreakPolicyTests` を追加（長文が 4 分割以上・各行 48 字以下・
    連結すると原文と一致・短文は 1 行のまま）。
- **personal: ライブ字幕の設定を設定ウィンドウに集約した（統合フェーズ 4・Mac・2026-08-10）**。
  ユーザー指示「字幕の設定が voicekey の設定画面からできないので、できるようにして。設定の置き場所は
  設定 UI に集約（メニューバーは残してもいい）。『字幕の表示テスト』は要らないので消して」。
  - 設定ウィンドウのサイドバーに **「ライブ字幕」タブ（id=9）** を追加した。
    出るのは personal かつ macOS 26 以降のときだけ（`EmbeddedKeys.isPersonal` と `#available` で判定）。
  - 内容: 状態表示＋「字幕を開始/停止」ボタン、起動時の自動開始トグル、翻訳エンジン Picker
    （Apple / Gemini / Groq）と選択中エンジンのモデル ID 入力（空欄で既定に戻る・既定値をプレースホルダ表示）、
    「最前面のアプリだけを翻訳」「英語の原文も表示」「訳文を読み上げる」トグル、
    「字幕の大きさをリセット」ボタン、API キーの状態（設定済み〈取得元・末尾 4 桁〉/ 未設定）。
  - 値の正本は従来どおり `CaptionSettings`（UserDefaults の `caption*` キー）。設定 UI は他タブと同じく
    `ConfigStore` の `@Published` ミラー経由で読み書きする（`captionSpeak` / `captionShowSource` /
    `captionGeminiModel` / `captionGroqModel` を追加）。動作中の字幕へ即時反映が要る項目
    （エンジン・対象・読み上げ・原文表示）は `CaptionService` にも配る（メニューバーと同じ経路）。
  - **API キーの入力欄は置かない**（読み取り専用の方針は不変）。正本は共有 Keychain である旨を注記した。
  - キー状態の取得は `/usr/bin/security` を子プロセスで起動するため、body から毎回引かず
    タブを開いたときに一度だけ調べて保持する。
- **personal: 「字幕の表示テスト」を削除した**。メニュー項目と `CaptionService.previewCaption()` を撤去
  （待機中のお知らせ表示など他用途の表示経路はそのまま）。
- **personal: ライブ字幕を voicekey ピルと一体化した（統合フェーズ 2・Mac・2026-08-10）**。
  ユーザー指示「字幕の場所は voicekey ピルと同じ場所に固定して。ピルが字幕に大きくなっていく感じがいい」
  「統合してるんだから右下の subglass（ピル）はいらない。同じアプリにして」
  「音声入力しているときは字幕は表示されなくていい」。
  - **位置をピルに固定**: 画面下辺中央・`visibleFrame.minY + 8`（＝ピルのカプセル下端）に字幕の下辺を
    合わせ、行が増えたら**上へ伸びる**。ドラッグ移動と位置記憶（`captionHudAnchorX/Y`）は撤去し、
    移動グリップも外した（大きさのリサイズグリップは残す）。メニューは「字幕の位置をリセット」→
    「字幕の大きさをリセット」に変更。Dock 出没・解像度変更・外部ディスプレイ着脱には
    ピルと同じ `visibleFrame` 基準で追従する。
  - **ピルが字幕に育つ見た目**: 1 行のときは角丸半径＝高さの半分＝完全なカプセル（ピルと同じ形）、
    行が増えたら半径 22 で頭打ち。出現時はピル相当の小さなカプセル（96×22）から目標サイズへ
    0.3 秒で伸ばし、消えるときは 0.22 秒で same-anchor のまま畳む（ピルの
    `spring(response: 0.3, dampingFraction: 0.82)` に合わせたイージング）。
  - **透明度はモード別のまま**: 字幕は 0.62、ピルは 0.7。別パネルなので互いに影響しない。
  - **音声入力中は字幕を隠す**: HUD が録音・変換中・通知の間は字幕パネルを自動で隠し、待機に戻ったら
    また出す。**認識・翻訳は止めない**（表示だけ止める）。`HudModel.onModeChanged` →
    `AppController.applyCaptionVisibility` → `CaptionService.setDictationActive` の一方向配線で、
    字幕サービスを作っていなければ何もしない（遅延生成を壊さない）。
  - **回帰ハーネス `--caption-hud-test` を追加**: 待機ピルと字幕を同時に出し、
    (a) 下辺がピルのカプセル下端と一致 (b) 横中心が画面中心と一致 (c) 行が増えても下辺が動かず高さだけ伸びる
    (d) 音声入力中は画面から消える (e) 終わったらまた同じ位置に出る、を ±1pt で機械判定する。
    実測 `bottom=60.0 期待=60.0 / 中心X=756.0 期待=756.0 / 1行 90pt → 2行 172pt`。
- **personal: ライブ字幕の起動時自動開始を既定 ON に戻した**。共存バグ（下記 Fixed）を根治し、
  `--caption-mic-coexist-test` が `rebuilds=0` で通ることを実測したため解禁。
  初回起動時は自動開始しないガード（`captionEverStarted`）はそのまま維持する。
- **personal: 文字起こしの選択肢を特徴名でなく実プロバイダー名 + モデル名で表示するようにした（personal ブランチのみ・Mac）**。自分用ビルドは「何が動いているか」を隠す必要がないため（2026-08-02 ユーザー指示）、ピッカーの表示が **Deepgram nova-3 / OpenAI gpt-live-transcribe / Groq whisper-large-v3-turbo** になる（ラベルも「文字起こしモード」→「文字起こしエンジン」）。release（製品版）は従来の特徴名 2 択のまま。
  - **Technical Details**: `Backend.developerLabel`（`providerName` + `defaultModel`）を追加し、`UI/SettingsView.swift` が `EmbeddedKeys.isPersonal` のときだけこちらを使う。バックエンド切替時に `slot.model` は `defaultModel` へ揃うので、表示名と実際に使われるモデルは常に一致する（`BackendLabelTests` で固定・3 ケース）。

### Added
- **personal: 字幕とマイク録音の共存を実測する回帰ハーネスを追加（`--caption-mic-coexist-test`）**。
  ライブ字幕を一度動かすと同一プロセスの HAL クライアントが壊れ、以後ディクテーションが
  マイクの既定入力デバイスを解決できなくなる不具合（`HALC_ProxySystem::DestroyIOContext` エラー →
  `Could not find default device` → `入力デバイスが見つかりません`）の**再現と根治確認**に使う。
  「字幕パイプライン開始 → 3 秒 → 停止」を 3 サイクル回し、**字幕が動いている最中と停止後の両方**で
  (a) 既定入力デバイスが解決できる (b) AVAudioEngine の入力にフレームが届く (c) 入力フォーマットが生きている
  を機械判定する（`[MIC] phase=... status=ok|ng` と `[VERDICT]`）。
  さらに**タップの作り直し回数**を数え、無音なのに 1 回でも作り直したら FAIL にする
  （これが事故の直接原因だったため。`[CAPTION] ... タップ再構成回数=N` と `[VERDICT] ... rebuilds=N`）。
  - **注意**: CoreAudio（coreaudiod）が既に応答不能になっていると baseline の HAL 呼び出しで
    待ちに入って計測できない（`[VERDICT] status=inconclusive`）。その場合はまず
    `killall coreaudiod` で音声サブシステムを復旧させてから実行すること。

- **personal: ライブ字幕（システム音声 → 英語認識 → 日本語字幕）を統合した（personal ブランチのみ・Mac・macOS 26 以降）**。
  別アプリだった subglass を voicekey に取り込む統合フェーズ 1。再生中の動画・配信の音声をそのまま拾って
  日本語字幕をガラス HUD に出す。**ディクテーション（本来の voicekey）とは完全に独立**していて、
  字幕を開始するまで一切初期化されない（録音〜貼り付けのクリティカルパスに乗らない）。
  - **操作**: メニューバー →「ライブ字幕」（開始/停止・最前面のアプリだけを翻訳・起動時に自動開始・
    訳文を読み上げる・英語の原文も表示・翻訳エンジン・表示テスト・位置リセット）＋ グローバル **⌥⌘S**。
    ホットキーは Carbon `RegisterEventHotKey`（アクセシビリティ許可が要らない＝承認プロンプトを増やさない）で、
    既存の CGEventTap には相乗りしない。
  - **翻訳エンジン**: Apple 純正（オンデバイス・キー不要・既定）/ Gemini（`gemini-3.5-flash-lite`）/
    Groq（`llama-3.3-70b-versatile`・OpenAI 互換の SSE ストリーミング）。クラウドは**確定文のみ**送り、
    429・失敗時は Apple 翻訳へ自動フォールバックする。API キーは環境変数 → 中央 Keychain
    （service = 変数名 / account = `shared`・`/usr/bin/security` を子プロセスで読む）の順で探索し、
    **書き込みは一切しない**（voicekey 本体の Keychain 項目には触らない）。
  - **恒久要件（subglass から引き継ぎ）**: 自プロセス除外で読み上げ音声を拾い直さない／確定文は全部訳して
    ロール表示（確定 2 行＋ライブ行）で読める時間を保証／鮮度優先の破棄はライブ行だけ／
    部分訳は Apple のときだけ／既定は「最前面のアプリだけを翻訳」。
  - **自動開始**: 設定「起動時に字幕を自動開始」は既定 ON だが、**初回起動時だけは自動開始しない**
    （システムオーディオ収録の許可ダイアログを、voicekey が作り込んだ初回権限プロンプトの直列化に
    割り込ませないため。初回はメニューから明示的に開始する）。
  - **Dock 常時表示を personal では既定 ON に変更**（統合後の「翻訳のやつを Dock に」に対応・設定で OFF 可）。
  - **App Nap 対策**: 字幕を出している間は `ProcessInfo.beginActivity(.background)` を張る
    （常駐 nap 下で Timer / asyncAfter が沈黙すると、行の自主退場・無音フェードが動かなくなるため）。
  - **Technical Details**: `Sources/Voicekey/Caption/`（Audio / Speech / Translation / Pipeline / UI / CLI・27 ファイル）に隔離。
    全型を `@available(macOS 26.0, *)` でゲートし、Package の最低 OS（macOS 14）は上げていない。
    `AppController.caption` は遅延生成（`AnyObject` 保持）。設定は `CaptionSettings`（UserDefaults の
    `caption*` キー）が正本で、`ConfigStore` は UI 用の @Published ミラーを持つ。
    `Info.plist` に `NSAudioCaptureUsageDescription` / `NSSpeechRecognitionUsageDescription` を追加。
    検証ハーネス `scripts/dev/caption_e2e.sh` / `caption_tts_loop.sh` / `caption_scope.sh` / `caption_bench.sh`
    と、それを駆動する CLI モード（`--caption-pipeline-test` / `--caption-tts-loop-test` /
    `--caption-scope-test` / `--caption-bench` / `--caption-groq-models`）を追加。
    画素判定ハーネス向けに `VOICEKEY_CAPTION_DISABLE=1` で字幕を丸ごと無効化できる。

- **personal: 文字起こしに「OpenAI ライブ」（gpt-live-transcribe）を追加し、設定画面から選べるようにした（personal ブランチのみ・Mac）**。2026-07-28 に OpenAI が公開した新しいライブ文字起こしモデルで、Realtime WebSocket 専用。文字起こしモードのピッカーが **即時入力（Deepgram）/ OpenAI ライブ / スタンダード（Groq）** の 3 択になる。キーは Keychain の OpenAI 項目（`voicekey.OpenAI`）を REST と共用するので、設定は不要（既に入っていればそのまま動く）。
  - **実測（2026-07-31・`benchmark/delay_sweep.py gpt-live-transcribe`）**: `delay=minimal` が最速で **TTFB 449-524ms・確定 649-708ms・CER 2.7/3.1%**。前世代 gpt-realtime-whisper（TTFB 637-774ms）より喋り出しの表示が速い。確定は依然 Deepgram nova-3（69-75ms）の約 10 倍なので、**既定は Deepgram のまま**で選択肢として追加した。delay は実測が支持する `minimal` に固定（設定 UI には出さない）。
  - **REST フォールバック**: gpt-live-transcribe は Realtime 専用で REST は 404（実測）。ライブ接続が張れなかったときは同世代の一括用 `gpt-transcribe`（REST 実測 OK・CER 2.7%）へ自動で差し替える。
  - **Technical Details**: `Core/OpenAILiveTranscriber.swift` を新設（`LiveTranscribing` プロトコル＝Deepgram 版 `StreamingTranscriber` と共通の契約・session.update / input_audio_buffer.append(base64) / commit・16kHz→24kHz 線形補間リサンプル〔OpenAI は 24kHz 以上必須〕）。`Config/AppConfig.swift` に `Backend.openaiLive` を追加（`selectableCases` を 3 択に・整形は既定 OFF）、`Core/Keychain.swift` は OpenAI 項目を共用、`Core/Transcriber.swift` は REST 用モデル差し替え（`restModel`）、`AppController.swift` は `streamer` を `any LiveTranscribing` 化して生成を `makeLiveTranscriber` に集約。
  - **テスト**: `OpenAILiveResampleTests`（リサンプルの分割/一括一致＝チャンク境界に段差が出ないことを 1e-5 精度で保証・3 ケース）と `OpenAILiveE2ETests`（実音声 + 実 API の疎通ハーネス。既定 XCTSkip、`OPENAI_API_KEY=... VOICEKEY_LIVE_E2E=1 swift test --filter OpenAILiveE2ETests` で実行。実測 CER 0.0%）を追加。`benchmark/delay_sweep.py` は測定対象モデルを引数で指定できるようにした。
- **benchmark: gpt-realtime-whisper（OpenAI・2026-05 GA・Realtime WS 専用）の delay スイープ測定 `delay_sweep.py` を追加**。`stream_benchmark.py` の `run_openai` に delay 引数（minimal/low/medium/high/xhigh・`transcription` オブジェクト内で指定）を追加した。再実測（2026-07-20）の結論: delay をどう振っても確定レイテンシ 530〜1050ms で Deepgram nova-3（69〜75ms）に届かず、**ストリーミング既定は nova-3 のまま・アプリへのモデル追加は見送り**（ユーザー判断）。OpenAI 系では gpt-realtime-whisper が圧倒的最良（gpt-4o-transcribe 系は WS でも delta をほぼ返さず長文 TTFB 44s＝ライブ字幕不能）で、使うなら delay=minimal（TTFB 637-774ms・確定 631-776ms・CER 0/2.2%）。

- **数字変換 v2: 位取り・助数詞つき単独漢数字に対応し、保護リストと 2 トグルを追加した（両 OS・release）**。喋った数字を半角で出す精度を上げるため、貼付直前の後処理 `NumeralNormalizer`（Mac）/ `numeral_normalizer`（Windows）を決定的・純関数・マイクロ秒（LLM 不使用）・冪等のまま拡張した。Mac/Windows で変換規則を完全一致させ、両 OS を 1 コミットで実装。
  - **変換範囲を拡張**: ①**位取りを含む日本語数詞をパースして整数化**（十二→12・二十三→23・千二百三十四→1234・三万五千→35000）。②**助数詞つきの単独漢数字**を変換（三時→3時・十時→10時・百人→100人・千円→1000円）＝直後 1 文字が助数詞集合 COUNTER〔時分秒人名個円年月日回歳才度台冊枚杯匹頭件品番位階週泊章話〕のときだけ。地名で誤爆しやすい 本/反/条/丁/目/州/国 は COUNTER から除外（六本木・五反田・千葉はそのまま）。③従来の裸数字列（二〇二六→2026・〇九〇…→09012345678）は維持。
  - **保護リスト（誤変換を防ぐ・UI 編集可）**: 数字漢字で始まる語を**数字ランの先頭にアンカーして**照合し、一致したランは変換しない。先頭アンカーなので「十二人」の中の「二人」で「十二」を壊さない（十一人→11人）。既定シード＝一時的／一時停止／一人／二人／十分／一日中／一部始終／一石二鳥／一番／一度（ひとり・ふたり・じゅうぶん・いちばん・もう一度 等を守る）。ユーザーが設定 UI で追加・削除できる。
  - **2 トグル（永続・UI 編集可）**: 「数字を半角に変換」（マスター・OFF で完全パススルー）／「助数詞つきの漢数字も変換（三時→3時）」（長さ1＋助数詞のゲート。位取り≥2 はマスターのみで常時変換）。設定「一般 → 数字入力」に配置し、保護リスト編集（一覧＋追加＋削除）も同セクションに置いた。
  - **カタカナ「ゼロ」の数字文脈変換**: STT が読み上げ数字を算用数字にしても末尾等の「ゼロ」だけカタカナで残すことがある（実測「1234567ゼロ」）ため、数字（ASCII/全角/漢数字）に隣接する「ゼロ」だけを 0/〇 に寄せる前処理を追加（1234567ゼロ→12345670・ゼロ九〇→090・ゼロゼロ九→009）。単独の「ゼロから」等は数字に隣接しないので語として温存する。
  - **Technical Details**: Mac `Core/NumeralNormalizer.swift`（v2 アルゴリズム・`normalize(_:enabled:convertCounter:protectWords:)`）・`Config/AppConfig.swift`（`numeralNormalizeEnabled`／`numeralConvertCounter`／`numeralProtectWords`＋シード・UserDefaults 永続）・`AppController.swift`（貼付直前 2 経路で config 値を渡す）・`UI/SettingsView.swift`（数字入力セクション）。Windows `core/numeral_normalizer.py`（同アルゴリズム・`normalize(text, enabled=, convert_counter=, protect_words=)`）・`config/constants.py`（3 キーを DEFAULT_CONFIG へ・`_force_always_on` 対象外）・`app.py`（`_insert_and_enter` で config 値を渡す）・`ui/settings_window.py`（数字入力カード・load/save・保護リスト操作）。回帰テスト `NumeralNormalizerTests`（Swift・23 ケース）／`tests/test_numeral_normalizer.py`（Python・全面刷新）を両 OS で恒久化。

### Fixed
- **personal: ライブ字幕が無音のあいだタップを作り直し続け、Mac 全体のマイクを壊していたのを根治（Mac・2026-08-10）**。
  ユーザー報告「音声入力が全部使えなくなった」の根本原因。フレーム watchdog は
  「グローバルタップなら無音でも IO サイクルごとにフレームが届く」という前提でフレーム途絶を
  黙死と判定していたが、**実測ではその前提が誤りで、誰も鳴らしていない間はフレームが 0**
  （内蔵スピーカーでも Bluetooth でも同じ）。そのため字幕を動かしているだけで 4 秒おきに
  Process Tap と Aggregate Device を作り直し続けていた（実測: 無音 20 秒で Aggregate 生成 6 回・再構成 5 回）。
  この churn が既定デバイスの再評価を繰り返し起こして **coreaudiod を詰まらせ**、
  同一 Mac の全プロセスでオーディオが応答不能になり（無関係な `afplay` すら HAL 初期化でハング）、
  voicekey 本体のディクテーションが `Could not find default device` →「入力デバイスが見つかりません」で全滅した。
  復旧には `killall coreaudiod` が必要だった。
  - **作り直す前に「対象プロセスが実際に出力中か」を必ず確認する**。従来は対象限定モードだけで
    見ていた判定を全モードへ広げた（`isAnyTargetEmitting()`）。
  - **黙死判定での作り直しに上限（3 回）**を置き、直らなくても監視を止めて HAL を叩き続けない。
  - **タップ構築失敗の再試行を指数バックオフ（2→30 秒上限）＋連続 5 回で打ち切り**に変更。
    従来は 2 秒固定で無制限に再試行し、不調な coreaudiod をさらに叩き続けていた
    （事故時の実ログでは 1 回の CoreAudio 呼び出しが 30 秒ブロックし、33 秒周期で無限に継続していた）。
  - **既定出力デバイスの変更通知は、実際にデバイス ID が変わったときだけ**張り替える
    （HAL は中身が同じでも通知を投げてくる）。
  - **回帰**: `--caption-mic-coexist-test` を機械判定に拡張し、字幕が動いている「最中」のマイクも測り、
    タップ再構成回数が 1 回でもあれば FAIL にした。**修正前 `rebuilds=3` で fail → 修正後 `rebuilds=0` で ok** を実測。
  - **Technical Details**: `Caption/Audio/SystemAudioTap.swift`（`maxStallRebuilds` / `maxRetryInterval` /
    `maxRebuildAttempts` / `isAnyTargetEmitting()` / `builtOutputDeviceID` / `rebuildCount`）、
    `Caption/Pipeline/CapturePipeline.swift`（`tapRebuildCount`）、
    `Caption/CLI/CaptionMicCoexistTestRunner.swift`。同じ欠陥が subglass にもあったため同内容を移植済み。
- **即時入力（Deepgram ストリーミング）の遅延再発を根治（Mac＋Windows・release）**。監査で特定した実欠陥を直した。
  - **先読みトークンの周期コールド窓（両 OS）**: `fetchEphemeralToken()`（Mac）/ `fetch_ephemeral_token()`（Win）が「キャッシュ残 15 秒超なら取得せず即返す」短絡だったため、暖機（warm）ループ（240 秒間隔）が回っていても**満了直前まで一切更新されず**、満了〜次 tick に 0〜240 秒のコールド窓（録音がトークン往復＋WS 接続を待つ）が生じていた（本番 `token_grants` が毎時 1 回しか並ばない実測で確認）。fetch に `minRemaining`/`min_remaining` を足し、**先読み経路（warm ループ／録音後先読み）は残 TTL 360 秒未満で強制再取得**、録音開始のクリティカルパスは従来どおり残 15 秒で再利用するよう分離した。暖機間隔 240s では常に満了 120 秒以上前にトークンが差し替わり、周期コールド窓が消える。閾値は `warmMinRemaining` / `WARM_MIN_REMAINING`（＝240s + マージン120s）。Windows も Mac と同一構造・同一欠陥だったため同じ閾値を移植（両 OS 同期）。
  - **finish-before-connect の 3 秒スタール（Mac）**: `StreamingTranscriber.finish()` が WS 未接続のまま呼ばれると `CloseStream` 送信が no-op になり、来ない Metadata を上限 3 秒フルに待っていた（コールド窓中の短い発話が「たまに 3 秒固まる」体感の正体候補）。未接続時は close 要求フラグを立て、**接続確立→pending PCM フラッシュ後に `CloseStream` を送る**よう修正。さらに**音声を 1 バイトも送っておらず接続も無い場合は 3 秒待たず即座に空で解決**して REST フォールバックへ回す。既存の 3 秒上限は最終防衛として残す。
  - **診断ログ（Mac・.notice＝永続レベル）を追加**: ①warm tick の結果（取得/スキップ・キャッシュ残秒）②録音開始時の warm ヒット / cold（cold は取得所要 ms）を .notice へ昇格 ③finish の解決要因（metadata/disconnect/timeout/empty-noconnect/token-fetch-failed）。`log.info` は `log show` で追えないため .notice 以上にした。1 イベント 1 行・秘密情報なし。
  - 陳腐化コメントを実態へ修正（両 OS の「TTL(300s) > 暖機間隔(240s) なのでキャッシュは切れない」は短絡で誤り／Mac の「即時入力=Deepgram を選択肢から外したのでストリーミング経路は不使用」は Deepgram 復活で陳腐化）。
  - **Technical Details**: Mac `Core/BackendClient.swift`（`warmMinRemaining` / `fetchEphemeralToken(minRemaining:)` / `warmEphemeralToken` / `canReuseCachedToken` / `cachedTokenRemaining`）、`Core/StreamingTranscriber.swift`（`closeRequested` / `requestClose()` / `finish()` / `connect()` / `resolveFinish(reason:)`）、`AppController.swift`（warm tick ログ・録音後先読みの閾値・陳腐化コメント）。回帰テスト `EphemeralTokenReuseTests`・`StreamingFinishTests` を追加。Windows `core/backend_client.py`（`WARM_MIN_REMAINING` / `fetch_ephemeral_token(min_remaining=...)`）、`app.py`（`_warm_backends_now` / `_postwarm_ephemeral` の閾値・陳腐化コメント）。
- **変換中ピルの「変換中…」が横に揺れて見える回帰を根治（両 OS・release／main）**。前回修正（カプセル幅の凍結 `transcribingSizeLocked`）でも横揺れが残ると 2 度報告された件を、再現ハーネス（下記 `fullscreen_helper.swift` の単色フルスクリーン背景＋連続 screencapture のピクセル計測）で計測して根因を特定した。**文字のジオメトリ（カプセル幅・文字の左右端）は白/グレー背景とも 0px で完全に不動**で、動いていたのは opacity 明滅（1.0⇄0.35）だけだった。明滅の谷で末尾「…」の細いドットが先に視認閾値を割って消え、**可視インクの重心が横に振れる**（実測: 白背景 ±7pt／グレー背景 ±2.4pt・周期は明滅 1.6s と一致）ため「横に揺れる」と知覚されていた。対称要素を明滅させても「…」の閾値落ちは残るため、**変換中テキストを完全静止（明滅を撤去）**にして動きを断った。修正後は同ハーネスで可視インクの重心・可視ピクセル数とも全フレーム完全一定（0px）を確認。
  - Mac (`UI/Hud.swift`): `transcribingContent` から opacity 明滅（`pulseOn` トグル＋`repeatForever`）を撤去し静止表示に。未使用化した `pulseOn` state・`onAppear`/`onChange` の明滅駆動・`hudLog`／`import os.log` も除去。
  - Windows (`ui/hud.py`): `set_state("transcribing")` で `_blink.start()` を呼ばず静止表示に（`_transcribe_opacity` は 1.0 固定）。テスト `tests/test_hud.py` を「変換中は明滅を開始せず不透明度 1.0 で静止・目標サイズ不変」に更新。
- **Mac: フルスクリーン中に待機ピルが消えないバグを根治（release／main）**。前回修正（CGWindowList の全画面被覆検出＋`asyncAfter` 0.4/0.9s の再評価）でも「全然消えない」と 2 度報告された件。旧方式は常駐 App Nap 下で `asyncAfter` 再評価が沈黙し（遷移アニメ中の同期評価も全画面未確定で取りこぼす）待機ピルが出っぱなしになる脆弱性があった。**検出とタイマーをやめ、`collectionBehavior` で OS の Space 管理に委ねる方式へ変更**: 待機ピルは `.canJoinAllSpaces` を外して現在の Space のみに出す（＝OS が他アプリのフルスクリーン Space に重ねない）、録音/変換/通知は `.canJoinAllSpaces + .fullScreenAuxiliary` でフルスクリーン上にも出す。Space 切替時は通常デスクトップへ order し直して追従（同期通知で App Nap でも確実に走る）。実フルスクリーン Space を作る開発用ヘルパーと screencapture のピクセル判定で「フルスクリーン＝待機ピル非表示（non-white 0）・録音ピルは表示（width 319px）・通常デスクトップと解除後は待機ピル復帰」を確認。CGWindowList 検出（`isMainScreenFullScreen`/`reevaluateIdlePillForFullScreen`/`applyIdlePillFullScreenVisibility`）と `import CoreGraphics` は撤去。実測で `.fullScreenAuxiliary` を外すだけでは `.canJoinAllSpaces` が全 Space へ強制表示するため消えなかった知見もコード内に記録。

### Added
- **personal エディション（個人用最速版・Mac・`personal` ブランチ限定・非配布）を新設した**。開発者本人の日常利用専用に、サーバー/ログイン/課金の往復ゼロで最速に叩くビルド。`main` / `release` は一切変更していない（personal ブランチのみ）。
  - **エディションフラグ `EmbeddedKeys.isPersonal`**。`./scripts/generate_embedded_keys.sh --personal` で `isPersonal=true` / `isDist=false` を生成する（**キーは焼き込まない**＝バイナリにプロバイダーキーを埋め込まない方針を維持）。
  - **STT は Keychain 直読の直叩き**。`isPersonal` のとき Deepgram（WS ストリーミング）・Groq・ElevenLabs を開発者の Keychain（`voicekey.Deepgram` 等）から鍵を直読して直接プロバイダーを叩く（＝`isDist=false` の既存直叩き経路を流用）。自社サーバー（`BackendClient`）・短命 JWT・warm ループ・`confirmUsage`・ログイン/課金ゲートを**一切通らない**（サーバー往復ゼロ＝先読みコールド窓が原理的に発生しない）。`Transcriber.selectRoute()`（純関数）で経路を一元判定し、personal は他条件に関わらず必ず Keychain 直叩きになることをテストで保証する。
  - **ライト固定**。`isPersonal` のとき `NSApp.appearance = .aqua` をアプリ全体に固定（HUD・設定・オンボーディングがダークに追従しない）。
  - **認証/課金 UI 非表示**。ログイン/アカウント/API キータブを設定から隠し、オンボーディングのログインステップを飛ばす（埋め込み前提でなく Keychain 直読で常に利用可）。
  - **録音中のライブ字幕**。ストリーミングの暫定（interim）テキストを HUD の録音ピルに逐次表示する（幅固定・tail 表示で横揺れ防止）。ライブ字幕にも `NumeralNormalizer.normalize` をかけ、確定入力と一貫して半角アラビア数字で見せる（「途中は漢数字→確定で半角に変わる」チラつきを解消）。確定入力（貼付）は従来どおり。
  - **署名・バンドル ID は release と同一**（Apple Development・`com.voicekey.app`・TeamID `9KT598FS4A`）＝既存 Keychain 項目をそのまま読める。ad-hoc 署名・バンドル ID 変更はしない。
  - **Technical Details**: `macos/scripts/generate_embedded_keys.sh`（`--personal`）・`Config/EmbeddedKeys.generated.swift`（`isPersonal`・git 管理外）・`Core/Transcriber.swift`（`Route`/`selectRoute`）・`Core/StreamingTranscriber.swift`（personal 直結）・`AppController.swift`（interim→HUD 配線）・`UI/Hud.swift`（`liveText`/`setLiveText`）・`VoicekeyApp.swift`（ライト固定）・`UI/SettingsView.swift`（認証/APIキー UI 非表示）・`UI/OnboardingView.swift`（ログインステップ除外）。同期方式は `docs/PERSONAL_EDITION.md`。回帰テスト `Tests/VoicekeyTests/TranscribeRouteTests.swift`（新規・+3：personal は isDist/ログインに関わらず `.directKeychain`・サーバー経路/ログイン要求を通らない）。
- **Windows: ホーム画面（ダッシュボード）を追加した（Windows パリティ・release）**。Mac 版 HomeView のダッシュボードを Windows に同等化し、設定ウィンドウの**先頭「ホーム」ページ**として実装した（ウィンドウはホームを起点に開く。サイドノッチの「ホームを開く」からも開ける）。Mac の主役 3 カードを再現する:
  - **累計入力**（累計文字数＋回数・録音時間＋レベル/進捗バー＋「あと N 文字で Lv.N+1」）、**節約できた時間**（推定節約時間＋身近なものへの換算＝カップ麺/通勤/映画/睡眠/日 の Mac と同じしきい値）、**この期間**（今日⇄今週トグルで期間内の文字数・録音時間・回数）。加えて最近の履歴（直近 8 件・クリックでコピー）。データ源は既存の `StatsStore`／`HistoryStore`（Mac と計算式・しきい値一致）。**アプリ別使用状況は Windows の履歴がアプリ名メタデータを持たないため省略**。
  - **Technical Details**: Windows `ui/settings_window.py`（ナビ先頭に「ホーム」ページ・`_create_home_page`／`_build_cumulative_card`／`_build_saved_card`／`_build_period_card`／`_refresh_home`／`_saved_comparison`／`_grouped`／`select_home`・`home` ナビアイコン・page index 更新・showEvent で `_refresh_home`）・`app.py`（`_open_home` が `select_home`＋設定ウィンドウ前面化）。回帰テスト `tests/test_home_dashboard.py`（新規・+8：換算しきい値・3 桁区切り・カード反映・今日/今週切替・最近の履歴・`select_home`）。
- **Windows: サイドノッチ（画面左端の履歴スリット）を追加した（Windows パリティ・release）**。Mac 版 SideNotch と対で、画面左端・垂直中央にフォーカスを奪わない細い黒バーを常駐表示する。ホバーで少し太くなり、録音中はアクセント色のグローで点灯する。クリックすると左端から履歴パネル（検索フィールド＋一覧＋「ホームを開く」）が開く。行クリックでクリップボードへコピー、外側クリック（非アクティブ化）or スリット再クリック or Esc で閉じる。全消去はホーム側だけに置き、ここには置かない（Mac と同方針）。設定「一般 → 表示」に「サイドノッチを表示」トグルを追加（既定 ON）。
  - **背景は ⑤ の実ブラー**（対応 OS ではアクリル/ブラー、非対応は角丸の QSS 疑似ガラスへフォールバック）。**フォーカス制御**: スリットは `WindowDoesNotAcceptFocus + WA_ShowWithoutActivating` で貼り付け先（音声入力の入力先）を奪わない。履歴パネルは検索入力のためフォーカスを取るが、これはユーザーがスリットをクリックして履歴を見る操作なので許容する。スリット再クリックの開閉往復は 0.25 秒の再オープンガードで吸収する。
  - **Windows 固有の簡素化**: Windows の履歴はテキストのみ（アプリ名・アイコンのメタデータを持たない）ため、検索はテキスト一致のみ・行はテキスト 2 行＋相対時刻（Mac のアプリアイコン列は省略）。
  - **Technical Details**: Windows `ui/side_notch.py`（新規・`SideNotchSlit`／`SideNotchHistoryPanel`／`SideNotch` コントローラ・純粋関数 `relative_time`／`filter_items`）・`ui/__init__.py`（`SideNotch` 公開）・`config/constants.py`（`side_notch_enabled` 既定 True）・`app.py`（生成・`status_changed`→点灯・`side_notch_enabled` 反映・`open_home_requested`→`_open_home`）・`ui/settings_window.py`（表示カードのトグル・load/save）。回帰テスト `tests/test_side_notch.py`（新規・+18：相対時刻・フィルタ・スリット状態・パネルの検索/空状態・コントローラの開閉と中継）。DWM 呼び出しはモック境界の外（実機検証は `docs/BUILD_WINDOWS.md`）。
- **Windows: 実ブラー背景（DWM アクリル/ブラー）ヘルパを追加した（Windows パリティ・release）**。Mac の `NSVisualEffectView(.behindWindow)` に相当する「背後をライブに磨りガラス化する」実ブラーを Windows でも使えるようにした。`SetWindowCompositionAttribute` の `ACCENT_POLICY` を使い、**Windows 10 1803+（build 17134）はアクリル**（`ACCENT_ENABLE_ACRYLICBLURBEHIND`）／**Windows 10 1507+ は旧ブラー**（`ACCENT_ENABLE_BLURBEHIND`）／**それ未満・非対応・失敗時は現行の QSS 疑似ガラスへフォールバック**する。適用対象は矩形サーフェス（このあとのサイドノッチ履歴パネル・ホームダッシュボードで使用）。**HUD ピルは角丸・サイズがモーフし、矩形全体をブラーするこの方式ではピル外周に矩形フロストが出るため、現行の描画ベース疑似ガラス（`ui/hud.py`）を維持する**。
  - **Technical Details**: Windows `platform/windows/acrylic.py`（新規・`apply_blur(widget, dark=, ...) -> "acrylic"/"blur"/"none"`・`blur_supported()`・`clear_blur()`）。OS ビルド判定 `_windows_build` と accent state 選択 `_accent_state_for_build` を分離し、実 `SetWindowCompositionAttribute` は `_default_applier` に隔離。回帰テスト `tests/test_acrylic.py`（新規・+10：ビルド別 accent state・サポート判定・未対応時のフォールバック・applier 例外時の "none"）。実 DWM 呼び出しはモック境界の外（実機検証は `docs/BUILD_WINDOWS.md`）。
- **Windows: 操作音（録音開始/停止の効果音）を追加した（Windows パリティ・release）**。Mac は開始=2 音上昇・停止=2 音下降の短いブリップを正弦波でその場合成して鳴らす（`soundEffectsEnabled`・既定 ON）。Windows も **同じ定数（周波数 660→990 / 880→587・長さ 0.065s×2・振幅 0.1・前後 6ms フェード）** で合成し、既存依存の sounddevice でマイク録音とは独立に fire-and-forget 再生する（音源ファイルは同梱しない＝Mac と同方式）。設定「一般 → サウンド」に「操作音」トグルを追加（既定 ON）。失敗（オーディオ経路の一時不整合等）は握りつぶす（音が鳴らないだけ）。
  - **Technical Details**: Windows `core/sound_fx.py`（新規・波形合成 `make_buffer(kind)` は numpy のみの純粋関数／実再生 `_default_player` は差し替え可）・`config/constants.py`（`sound_effects_enabled` 既定 True）・`app.py`（`_begin_recording` で start・`_finish_recording` で stop・いずれも設定 ON 時）・`ui/settings_window.py`（サウンドカードのトグル・load/save）。回帰テスト `tests/test_sound_fx.py`（新規・+9：波形の形/振幅/フェード、start は上昇・stop は下降、再生本体への注入）。sounddevice の実出力はモック境界の外（実機検証は `docs/BUILD_WINDOWS.md`）。
- **Windows: 音声入力中はメディアの音量を下げるようにした（Windows パリティ・release）**。Mac は録音中に既定出力のマスター音量を 12% へ下げ、停止で元へ戻す（`duckMediaEnabled`・既定 ON）。Windows も Core Audio の `IAudioEndpointVolume`（マスター音量）を pycaw 経由で同じ挙動にした。設定「一般 → サウンド」に「音声入力中はメディアの音量を下げる」トグルを追加（Mac と同文言・既定 ON）。
  - **Mac と同じ安全策**: 現在音量が既に 12% 以下なら下げない（音量を引き上げない）／既にダッキング中なら二重に下げない／クラッシュ耐性（下げる前に元音量とフラグを状態ファイル `media_duck_state.json` へ保存し、録音中に落ちても次回起動時の `restore()` が残存フラグで元へ戻す）。pycaw が無い・音量制御を持たないデバイスでは無害にスキップ（録音は止めない）。COM 呼び出しは録音のクリティカルパスに乗せず専用シリアルワーカーで撃ちっぱなし。
  - **Technical Details**: Windows `core/media_ducker.py`（新規・`duck()`/`restore()`・`_duck_impl`/`_restore_impl` は provider/state_path 注入可）・`config/constants.py`（`duck_media_enabled` 既定 True）・`app.py`（`_begin_recording` で duck・`_finish_recording` で無条件 restore・起動時にクラッシュ復元 restore）・`ui/settings_window.py`（サウンドカードのトグル・load/save）・`requirements.txt`（`pycaw; sys_platform=="win32"`）・`voicekey.spec`（pycaw/comtypes を hiddenimports へ）。回帰テスト `tests/test_media_ducker.py`（新規・+9：閾値判定・二重防止・元音量復元・クラッシュ復元・コントローラ欠如時のスキップ）。COM 呼び出し自体はモック境界の外（実機検証は `docs/BUILD_WINDOWS.md`）。
- **Windows: フルスクリーンアプリ利用中は待機ピルを退避するようにした（Windows パリティ・release）**。Mac は `collectionBehavior` で OS の Space 管理へ委ね、他アプリのフルスクリーン Space に待機ピルを重ねない挙動になっている。Windows には相当の OS 機構が無いため、**前面ウィンドウがモニタ全面を覆っているか**を win32（`GetForegroundWindow`／`GetWindowRect`／`MonitorFromWindow`＋`GetMonitorInfo`）で判定し、**待機（常時表示）ピルだけ**を退避させる（録音中・変換中・通知はフルスクリーンでも従来どおり表示する＝Mac の `.fullScreenAuxiliary` と対）。フルスクリーン化/解除には約 0.7 秒間隔のポーリングで追従する（待機ピル表示中のみ稼働）。
  - **プラットフォーム分岐とテスト用シーム**: 実 win32 呼び出しは `core/fullscreen.py::_win32_foreground_fullscreen` に隔離し、`foreground_is_fullscreen(detector=...)` で判定関数を差し替え可能にした。macOS 等では既定判定が無く常に False（＝従来どおり待機ピルを表示）。判定不能・例外は安全側（フルスクリーンでない）へ倒す。
  - **Technical Details**: Windows `core/fullscreen.py`（新規）・`ui/hud.py`（`_fullscreen_probe` / `_fs_timer` / `_tick_fullscreen` / `_hide_keep_idle` / `_ensure_fs_timer`）。回帰テスト `tests/test_fullscreen.py`（新規・+12：detector 注入と例外処理、待機ピルの退避/復帰、録音/変換/通知が退避しないこと）。win32 API 呼び出し自体はモック境界の外（実機検証は `docs/BUILD_WINDOWS.md` のチェックリスト）。
- **数字を読み上げたら漢数字でなく半角アラビア数字で入力するようにした（両 OS・release）**。「数字は半角で出したい」との要望に対応し、3 層で半角化する。
  - **層1: Whisper（スタンダード=Groq / 開発の OpenAI）への style プロンプト**。文字起こしリクエストに「数字は半角で表記します。例: 2026年7月15日、3人で1200円、成功率98.5%。」を付与し、Whisper に半角表記を追従させる。main の Groq/OpenAI 直叩きはクライアント付与で効き、release は Groq をサイトのプロキシ経由で呼ぶため **voicekey-site 側でも `prompt` フィールドを Groq へ転送**するようにした（従来は未転送だった）。ElevenLabs（scribe）は prompt 非対応のため対象外。
  - **層2: 貼付直前の安全な後処理（`NumeralNormalizer` / `numeral_normalizer`）**。確定テキストにのみ適用（HUD の中間表示は対象外）: 全角数字・全角英字を半角化し、**位取りを含まない漢数字が 2 文字以上連続する並びだけ**アラビア数字化する（例: 三五八〇九一→358091）。単独の漢数字・位取り（十百千万）は変換しないため「一人」「二階」「十時」「一番」「五月雨」等の普通の語を壊さない。Deepgram は `numerals` が日本語未対応（公式ドキュメント確認）のため追加せず、既存の `smart_format=true` のまま。**（この層2 は下記「数字変換 v2」で位取り対応＋助数詞つき単独漢数字＋保護リストへ拡張した。）**
  - **層3: 整形プロンプトに半角ルールを追加**。voicekey-site のサーバー整形（`/api/v1/format`）の 3 プリセット（標準/すっきり/箇条書き）に「数字・英数字は半角のアラビア数字で表記する（漢数字にしない）」を追加。原文尊重が目的の「そのまま」プリセットには追加しない。main のローカル整形の既定プロンプトにも同ルールを追加（ユーザー編集済みのカスタムプロンプトは不変）。
  - **Technical Details**: Mac `Core/NumeralNormalizer.swift`（新規）・`Core/Transcriber.swift`（`numeralStyleHint` / `whisperPrompt(userPrompt:)`）・`Core/BackendClient.swift`（`transcribeGroq` / `groqMultipartBody` に `prompt`）・`Core/TextFormatter.swift`（既定プロンプト）・`AppController.swift`（貼付 2 経路で正規化）。回帰テスト `NumeralNormalizerTests`（+14）。Windows `core/numeral_normalizer.py`（新規）・`core/api_transcriber.py`（`_NUMERAL_STYLE_PROMPT` / `_whisper_prompt`）・`core/backend_client.py`（`transcribe_groq` に `prompt`）・`core/text_formatter.py`（既定プロンプト）・`app.py`（`_insert_and_enter` で正規化）。回帰テスト `tests/test_numeral_normalizer.py`（新規・+12）・`tests/test_api_transcriber.py`（Whisper prompt +4）。voicekey-site `lib/stt.ts`（`callGroqStt` に `prompt`）・`app/api/v1/transcribe/groq/route.ts`（`prompt` を転送）・`lib/format.ts`（3 プリセットに半角ルール）。
- **録音キー（2 スロット）を「未割り当て」にできるようにした（両 OS・release）**。「2 つ目のキーは使わないので割り当てない選択肢が欲しい」との要望に対応。`voice-agent` に Mac 先行実装していた UI を、Windows と同時に製品版へ移植した（両 OS 同一コミット）。
  - **未割り当ての内部表現＝空のホットキー**（Mac＝空トークン配列 `[]` / Windows＝空文字列 `""`・後方互換）。設定画面の各録音キーに**「割り当てを外す」ボタン**（割り当て済みのときだけ表示）と、キー捕捉中の **ESC で「割り当てなし」**確定を追加。空表示は「未割り当て」と明示し、「クリックしてキーを押すと割り当てます。ESC で割り当てなし（このホットキーを無効化）。」の補足文を添えた。
  - 未割り当てのスロットは**ホットキー照合の対象外**（Mac `AppController` は `!slot.hotkey.isEmpty` ガード済み・Windows `_slot_matches` は空 `required_keys` で False）のため誤発火せず、もう片方のキーは通常どおり動く。旧設定（割り当て済み）や保存・再読み込みは壊さない（未割り当ては既定キーに戻らず空のまま永続）。
  - **Technical Details**: Mac `Config/AppConfig.swift`（`SlotConfig.isAssigned`・`hotkeyLabel` の空表示「未設定」→「未割り当て」）、`UI/HotkeyRecorderView.swift`（`emptyLabel` 引数・ESC で `commit([])`）、`UI/SettingsView.swift`（`SlotSettingsTab` に「割り当てを外す」ボタン＋補足文）。回帰テスト `macos/Tests/VoicekeyTests/SlotConfigTests.swift`（+7）。Windows `ui/settings_window.py`（`HotkeyInput` の `empty_label` プレースホルダ・ESC で未割り当て・スロット行に「割り当てを外す」ボタン＋補足文）。回帰テスト `tests/test_handsfree_logic.py`（`_parse_hotkey("")`→空集合・未割り当てスロット非一致）・`tests/test_settings_window.py`（未割り当て保存が既定へ戻らない）。
- **開発用: HUD のフルスクリーン挙動・横揺れを実測する回帰ハーネス（`macos/scripts/dev/fullscreen_helper.swift`）**。自分のウィンドウを `toggleFullScreen(nil)` する数十行の単体 Swift（TCC 不要）。本物のフルスクリーン Space を作って待機ピルの非表示/録音ピルの表示を screencapture で検証でき、単色（既定=白）背景を敷けば変換中「変換中…」の文字位置を高コントラストで計測できる。あわせて `UI/Hud.swift` のデバッグ駆動に `VOICEKEY_HUD_DEBUG_STATE=cycle`（録音→変換中→待機を実タイマーで循環・App Nap 抑止付き）を追加し、遷移中の横揺れを実録音なしで再現できるようにした（いずれも env で明示指定時のみ・既定挙動は不変）。

### Changed
- **アプリアイコンをガラス質感へ刷新（両 OS）**。現行ブランドデザイン（インディゴのグラデーション squircle 地＋「波形→テキストカーソル」の 4 シンボル）はそのままに、上縁のハイライトリング・内周のリムライト（上=白/下=黒でガラスの厚み）・上部の控えめなスペキュラー・各シンボルの縦グラデの艶・シンボル背後の柔らかいドロップシャドウを追加し、「同じアイコンがガラスになった」仕上がりにした。色は実測 sRGB 値で現行と一致（`NSColor(srgbRed:)` 指定。calibratedRGB だとガンマ差で地が淡くなるため）。
  - `macos/scripts/dev/make_app_icon.swift`: 旧 5 本バーデザイン（2026-07-03 に廃止済み＝ユーザーが「不採用」と明言した昔のアイコン）を描いたまま陳腐化していた生成スクリプトを、現行デザイン＋ガラス質感を描く実装へ全面書き換え（生成元と実アイコンの不一致を解消）。
  - `macos/scripts/assets/app_icon_1024.png`（マスター）・`macos/Resources/AppIcon.icns`（Mac）・`icon.ico`（Windows・16〜256 の 6 サイズ）を同マスターから再生成。Windows の ico はこれまで別素材だったが Mac と同一デザインに統一。
- **UI から紫・ネオン発光を排し、信頼感のあるニュートラルなガラス基調へ（両 OS・release）**。「後ろの紫・ボタンのネオンをやめて、頼りになる・信頼できる色に」との指摘を受け、彩度の高い色付き影・発光・紫みを全排除し、無彩色（黒・グレー・白）＋システムアクセントの控えめな使用に整えた。ガラスの文法（すりガラス・上縁ハイライト・極細リム・ソフトな無彩色影）は維持。**HUD ピルの状態色（auto_enter の紫・ハンズフリーのティール）は意味のあるステータス色として維持**する。
  - Mac (`UI/Glass.swift`): ウィンドウ全面ウォッシュから紫（`Color.purple`）を撤去し、黒〜ダークグレー（ダーク）／白（ライト）主体＋アクセント 0.04 の気配のみへ。prominent ボタンのアクセント色グロー影（`accentColor.opacity(0.5)`, radius 10）をソフトな無彩色影（`black.opacity(0.18)`, radius 6）に変更（塗りのアクセントグラデは維持）。
  - Mac (`UI/SettingsView.swift`): サイドバー選択ピルのアクセント色グロー影を同じ無彩色影へ。アバター円はグロー影が元々無いため据え置き（円形はアバターとして許容）。
  - Mac (`UI/OnboardingView.swift`): ステップのヒーローアイコンを「アクセントのグラデ円＋白シンボル＋グロー影」（「円の上に円でダサい」と指摘）から、角丸スクエアの無色ガラスタイル（`glassSurface`）＋アクセント色の素の SF シンボルへ置換。円形コンテナとグロー影を廃止。
  - Windows (`ui/styles.py`): backdrop（`BG_WINDOW`）の対角グラデから青紫みを抜き、ダーク＝無彩色チャコール（#2A2A2D→#1F1F22→#151517）／ライト＝無彩色ライトグレー（#F1F1F3→#F6F6F8→#EAEAED）に。QSS には発光影が無いため primary ボタンはアクセント塗り＋白トップリムのまま（沈静化済み）。
- **セットアップガイドを「起動時にだけ」表示するよう変更（両 OS・release）**。「今のセットアップガイドは、起動時にだけ表示するようにして」との指摘を受け、メニューバー（Mac）／タスクトレイ（Win）に常設していた「セットアップガイド…」の再表示項目とその配線を撤去した。表示経路は**起動時の自動判定だけ**になる（初回起動の自動表示ロジック・完了フラグ・デバッグ用 `VOICEKEY_OPEN_ONBOARDING`〔Mac〕は現状維持）。既存ユーザーには従来どおり表示されない。
  - Mac: `VoicekeyApp.swift`（メニュー項目とセレクタ `openOnboarding` を削除。自動判定・デバッグが使う `showOnboarding(fromStep:)` は維持）。
  - Windows: `ui/system_tray.py`（トレイ項目と `open_onboarding` シグナルを削除）、`app.py`（トレイ配線を削除。`_show_onboarding` は初回自動表示から継続利用）。
  - ドキュメント: `README.md`・`OVERVIEW.md` のセットアップガイド再表示に関する記述を削除・修正。
- **録音 HUD「ピル」の挙動を刷新（両 OS・release・フィードバック第6弾）**。ピル全体を一回り小型化し、状態で大きさが変わる**三段階サイズ（待機=最小 < 変換中=小 < 録音中=大）**にして、切り替わりをアニメで滑らかにモーフさせた。あわせて **Dock（Win: タスクバー）の出没・解像度変更に合わせてピルの縦位置を追従**させ、**ハンズフリー録音は文字ラベル（「ハンズフリー」「もう一度押すと停止」）を廃止し、状態ドットと波形バーのティール着色だけで区別**する方式へ変更（情報最小限・幅も詰まる）。変換中の表現も**回転スピナー（Win）をやめ、Mac と同じ「変換中…」テキストの明滅に統一**した。
  - Mac (`UI/Hud.swift`): 通常パディング 16/8→13/6・変換中 12/5、波形バー高さ 3〜22→3〜18、状態ドット 8→7pt、「変換中…」12→11pt。`NSApplication.didChangeScreenParametersNotification` を監視し表示中は `NSAnimationContext`（0.22s・easeOut）で位置追従。**フルスクリーン Space では待機ピルだけ自動で隠す**（visibleFrame≒frame をヒューリスティック判定・録音/変換/通知は表示継続）。変換中のサイズ凍結を廃止して三段階化。ハンズフリーのラベル/停止ヒントを削除しドット＋波形を `handsFreeAccent`（ティール）で着色。
  - Windows (`ui/hud.py`): `PILL_HEIGHT` 40→36・波形/フォントを Mac に合わせて縮小。`QVariantAnimation`（200ms・OutCubic）で幅/高さをモーフ、変換中は明滅（opacity 1.0⇄0.35・0.8s 往復）に統一しスピナーを撤去。`QScreen.availableGeometryChanged`／`primaryScreenChanged` で表示中は再配置（フルスクリーン判定は確実・軽量な手段が乏しいため今回は見送り・コードにコメント）。`app.py` から新シグナル `hands_free_changed` を配線しドット＋波形を Mac と同じティール（`#00C7B8`）で着色。
- **ユーザー向け用語「リアルタイム」→「即時入力」へ全面刷新（両 OS・release）**。「話しながら文字がその場に現れていく」ライブ字幕機能は撤去済みで、実挙動は「キーを離した瞬間に全文が一括入力される」ため、モード名「リアルタイム」が実態と食い違っていた。設定画面の文字起こしモード 2 択ラベル・モード説明キャプション・ログイン説明文・オンボーディングの表示名・通知に出る文字起こしモード名（`display_name`）など、**ユーザーの画面に出る全文言を「即時入力」に統一**し、あわせて「話しながら文字が表示されます」等のライブ表示を前提とした説明を「しゃべり終わった瞬間、全文がまとめて入力されます（最速・実測 0.1 秒）」へ書き換えた。保存値（`deepgram`）・enum/関数/クラス名・API パラメータ・ストリーミング技術を説明する内部コメントは不変（技術的には引き続きリアルタイムストリーミングで受信している）。
  - Mac: `Config/AppConfig.swift`（`Backend.label`）、`UI/SettingsView.swift`（モード説明・ログイン文）、`UI/OnboardingView.swift`。
  - Windows: `ui/settings_window.py`（2 択ラベル・モード説明・ログイン文）、`ui/onboarding_window.py`（表示名マップ）、`core/api_transcriber.py`（Deepgram の `display_name`）、`config/constants.py`・`config/config_manager.py`（コメント）。
  - ドキュメント: `README.md`・`OVERVIEW.md` の現行モード説明を更新（過去バージョンのリリースノートは履歴として原文保持）。
- **Mac: 履歴の手動「消去」ボタンを撤去（ホーム画面「最近の履歴」）**。履歴はもともと 200 件を超えると古い順に自動で消えるローテーション式で、統計（累計・アプリ別使用状況・期間別）は履歴と独立した累計カウンタ（stats.json）のため履歴の増減で減ることはない。手動の全削除 UI は誤操作リスクに対して得るものがなく撤去した（設定「履歴」の保存オン/オフは従来どおり）。`HistoryStore.clear()` も呼び出し元が無くなったため削除。

### Added
- **Windows: 待機ピル（常時表示）を新設し Mac 同等に（release・フィードバック第6弾）**。設定「一般」ページの「表示」に Mac と同文言の**「ピルを常に表示」トグル**（`config.hud_always_visible`・既定 OFF・`_force_always_on` の対象外＝ユーザーが自由に ON/OFF）を追加。ON にすると待機中も画面下部中央に極小ピル（Mac の 64×14 相当・中身なし）を表示し、録音開始で大サイズへモーフする。`config/constants.py` に既定値を追加（既存 settings.yaml は deep-merge で False 補完）、`ui/settings_window.py` にロード/保存を配線、`app.py` の HUD 生成と設定ホットリロードで `always_visible` を反映。テスト `tests/test_hud.py` を新設（三段階サイズ順序・待機ピルの可視条件・ハンズフリー配線と全状態描画・既定値と `_force_always_on` 非対象）。開発用 `scripts/dev/preview_ui.py` を待機ピル/ハンズフリー録音の新状態に対応。
- **セットアップガイドを「読むだけ」から「実際に使って体験する」形に刷新（両 OS・release）**。従来は権限・ログイン・使い方の説明で終わっていたオンボーディングに、**実際に録音キーを押して例文を声で入力する体験ステップ**を追加した。既存の権限（Mac）・ログインステップの構成は保持し、その後ろに以下を足す: ①**即時入力の練習**（例文「明日の打ち合わせ、15時に変更でお願いします。」を録音キーを押しながら読む→練習欄に文字が入ると成功メッセージ「よくできました！ 声がそのまま文字になりました」）②**ハンズフリーの練習**（トグル録音を同じ練習欄で実践）③**整形の体験**（整形を一時的に強制 ON にし、フィラー混じりの例文を読ませてフィラーや言い直しが自動で消える様子を体験・整え方 4 プリセットの紹介付き）④**機能ツアー**（2 つ目の録音キー／履歴／統計／HUD をカードで紹介）。成功判定は練習欄のテキスト変化を監視（Mac=SwiftUI `onChange`／Win=`QTextEdit.textChanged`）。促し文の録音キー名は実設定から動的に表示する。体験は強制せず各ステップに「スキップ」を常設。完了後もメニューバー（Mac）／タスクトレイ（Win）の「セットアップガイド…」から再表示できる（Windows はトレイに項目を新設）。
  - Mac: `UI/OnboardingView.swift`（`OnboardingStep` に体験 3＋ツアーの 4 ステップ追加・`OnboardingModel` に体験成功状態とエンジン起動/整形オーバーライドの配線・`PracticeCard` 新設・成功判定の純ロジック `OnboardingPractice`）、`VoicekeyApp.swift`（エンジン起動と整形オーバーライドのクロージャ配線）、`AppController.swift`（整形体験用の一時オーバーライド `practiceFormatOverride`）。
  - Windows: `ui/onboarding_window.py`（練習/ツアーページ・成功演出・自動フォーカス・スキップ・成功判定 `_has_practice_input`）、`app.py`（整形オーバーライド `_practice_format_override` を `_snapshot_context` に反映・トレイからの再表示配線）、`ui/system_tray.py`（トレイに「セットアップガイド…」を追加）。
  - テスト: Mac `OnboardingPracticeTests`（成功判定・エンジン起動 1 回・整形オーバーライドの遷移）＋`OnboardingDeciderTests`（ステップ数/順序）、Windows `test_onboarding_window.py`（7 ステップ遷移・スキップ・成功演出・整形オーバーライド・純ロジック）＋`test_task_context_snapshot.py`（オーバーライドで整形強制 ON・保存値は不変）。
- **テキスト整形を「削らない整形＋プリセット選択」に刷新（Mac＋サーバー・release）**。従来の単一プロンプトは「フィラー除去・言い直し整理」が発言内容の削除まで起こすことがあった。全プリセット共通で「適切に句読点を入れる」「発言の削除・要約・言い換え禁止」を明記した 4 プリセットに再設計し、設定の各録音キータブ（「文章を自動で整える」トグルの下）で選択できるようにした（自由入力プロンプト・モデル選択は出さない）: **標準**＝言いよどみだけ除去（既定）/ **そのまま**＝一切削らず句読点だけ / **すっきり**＝言い直し・どもりを整理 / **箇条書き**＝列挙をリスト化。選択はサーバーへ `preset_id` として送られ、STT+整形統合（`format=1`）と整形単体 API の両方で効く。旧クライアント（preset_id なし）は「標準」に解決され、削らない方向への変更なので退行なし。
- **Mac: サイドノッチ（画面左端の履歴スリット）を新設（大型機能追加 Phase C・release）**（`UI/SideNotch.swift`・新規）。画面左端・垂直中央に常駐する細いスリット（黒基調＋うっすら外枠。ホバーで少し太くなり、録音中はアクセント色のグローで点灯）。クリックで履歴パネル（浮遊ガラス島・約 320×440）が左端から開く。パネル上部の検索フィールドでテキスト・アプリ名の部分一致（大文字小文字無視）絞り込みができ、各行はアプリアイコン＋テキスト 2 行省略＋相対時刻でクリックするとコピー、フッターの「ホームを開く」でホーム画面へ移動する。設定「表示」の「サイドノッチを表示」で ON/OFF（既定 ON）。非アクティベーティングのまま検索入力を受けられるよう `canBecomeKey=true` の NSPanel を使い（`.nonactivatingPanel` 維持）、音声入力の入力先アプリは前面のまま奪わない。
- **Mac: ホームと設定を 1 つのメインウィンドウに統合（大型機能追加 v3.1・release）**（`UI/MainWindowView.swift`）。ホームに浮遊ガラス島のサイドバー（ブランド行＋「ダッシュボード」／左下に「設定」ナビとアカウント行＝アバター＋メールアドレス、未ログインは「ログイン」表示）を追加し、「設定」を押すと**同じウィンドウのまま設定画面へ切り替わる**（別ウィンドウを開かない）。上部「ダッシュボード」でホームへ戻る。メニュー「設定…」(⌘,)・`VOICEKEY_OPEN_SETTINGS`・Dock/Finder からの再オープンはこのメインウィンドウを開く（オンボーディング/フィードバックは従来どおり別ウィンドウ）。
- **Mac: ホーム画面を新設（大型機能追加 Phase B・release）**（`UI/HomeView.swift`・新規）。起動 / Dock・Finder からの再オープン / メニューの「ホーム」で開くメインダッシュボード。設定と同じ frosted chrome・真の中央・約 760×560。レイアウトは v2.1（島で全面を包まず、frosted backdrop の上に控えめなカードをフラットに敷く）。構成: ヘッダ（更新ピル or アプリアイコン＋名称／右に「設定を開く」）、統計カード（今日/今週/累計の入力文字数・レベル/XP・推定節約時間）、アプリ別使用状況（累計文字数上位 5 アプリをアイコン＋横バーで表示・空状態あり）、最近の履歴（直近 8 件・アプリアイコン＋相対時刻・クリックでコピー・「消去」）。**実績・履歴タブは設定から撤去してホームへ移設**。
- **Mac: アップデート導線をホームの「更新ピル」に刷新（Phase B・release）**。新バージョンをサイレントに検知（Sparkle の `checkForUpdateInformation()` を起動 5 分後＋以後 6 時間ごと・UI を一切出さない）し、検知時だけホーム左上に落ち着いた青系の小さな横長ピル「⬆ v{version} に更新する」を表示。クリックで更新フロー（DL→インストール）を開始する。バックグラウンド検知でダイアログを勝手に出さない。手動チェックは設定「バージョン情報」タブのボタンに集約し、メニューバーの「アップデートを確認…」項目は削除。
- **Mac: 大型機能追加 Phase A（基盤＋設定項目・release）**。競合パリティのための新機能群を追加（ノッチ・ホーム画面は後続フェーズ）。
  - **操作音**（`Core/SoundFX.swift`・新規）: 録音開始＝2 音上昇 / 停止＝下降の短いブリップをコードで合成再生（音源ファイル不要）。録音用エンジンとは独立した再生専用 `AVAudioEngine`＋撃ちっぱなし再生で、音声入力パイプラインに待ちを足さない。設定「サウンド」の「操作音」で ON/OFF（既定 ON）。
  - **メディア音量ダッキング**（`Core/MediaDucker.swift`・新規）: 録音中だけ既定出力デバイスの音量を **12%** へ下げ、停止で元へ戻す。現在音量が既に 12% 以下なら何もしない（音量を引き上げる逆転を防止）。異常終了に備え「元音量・実行中フラグ」を UserDefaults へ退避し、次回起動で残存していれば巻き戻す。設定「サウンド」の「音声入力中はメディアの音量を下げる」で ON/OFF（既定 ON）。
  - **再貼り付けショートカット**: 最後に入力したテキストをもう一度貼り付けるグローバルキー（既定 ⌃⌘V・クリアで無効化可）。設定「一般」に「最後の文字起こしを貼り付け」行を追加。
  - **前面アプリ認識**（`Core/FrontAppTracker.swift`・新規）: 貼り付け先アプリ（bundleID・名前・アイコン）を追跡し、HUD へアイコン表示・アプリ別実績集計に使う（自アプリが前面のときは直前の他アプリを保持）。
  - **HUD 拡張**: 録音中/変換中のピル左端に貼り付け先アプリのアイコン（18pt）を表示。設定「表示」の「ピルを常に表示」ON で待機中も極小ピル（中身なし・mic アイコンは出さない）を常時表示（既定 OFF）。
  - **Dock 常時表示トグル**: 設定「表示」の「Dock に表示」ON で起動時から Dock にアイコンを出し、ウィンドウを閉じても引っ込めない（既定 OFF＝従来の動的表示）。
  - **アプリ別使用実績の集計**: 貼り付け先アプリごとに回数・文字数・録音秒数を累計（ホーム画面の「アプリ別使用率」用の基盤・Phase B で表示）。
  - **サイドノッチ表示トグル**（既定 ON）: 設定「表示」に項目を追加（実体は Phase C。今回はフィールドとトグルのみ先行）。
  - **履歴保存トグル**（既定 ON）: 設定「履歴」の「履歴を保存」OFF で履歴に残さない。
- **Mac: 設定/セットアップ/フィードバック画面を開いている間だけ Dock にアイコンを表示**（activation policy の `.regular` ⇄ `.accessory` 動的切替）。開いている間は ⌘Tab で普通のアプリのように切り替えられ、全部閉じるとメニューバー常駐だけに戻る。**Finder/Launchpad からアプリを開き直すと設定ウィンドウが前面に開く**（`applicationShouldHandleReopen`）。
- **Mac: `UI/Glass.swift` を新設**。macOS 26 では本物の Liquid Glass API（`glassEffect` / `Glass.clear.interactive()`）、macOS 14–15 ではマテリアル近似へ自動フォールバックする共通スタイル部品（呼び出し側は `#available` 不要）。`VOICEKEY_GLASS_FALLBACK=1` で近似描画を強制でき、macOS 26 上でも旧 OS の見た目を QA できる。

### Changed
- **Mac: セットアップガイドを「本体ウィンドウ内の全画面 2 ペイン」へ全面再デザイン（release）**。従来の独立ウィンドウ＋中央ガラス島・同心円ヒーロー・7 ステップ（ようこそ/権限/ログイン/体験3/機能ツアー/完了）を刷新し、参照デザイン（Typeless 系）に倣った**左＝説明（見出し・本文・黒基調のピル primary・ゴースト secondary）／右＝淡いパステルのモックアップ**の 2 ペインを**メインウィンドウの中に**開くようにした（別ウィンドウとガラス島を廃止・完了でホーム画面へ着地・上部に細いパンくず「ようこそ › 準備 › 体験」＋進捗下線）。ようこそ・動作確認・まとめは全画面インタースティシャル。ステップは ようこそ → 権限（マイク → アクセシビリティ → 入力監視・**1 個ずつ**・各ステップに信頼カード）→ ログイン → 動作確認 → **マイクテスト（新設・録音せずライブでレベルメーターが動く・入力デバイス選択付き）** → **録音キーテスト（新設・押すとキーが光る・録音しない＝無料枠を消費しない）** → 体験3（メモ風モックウィンドウを右ペインに置き実際に入力）→ まとめ「話して、タイプしないで。」。**機能ツアーは廃止**（マイク／録音キーの実テストに置換）。体験は各ステップの「スキップ」で任意。
  - Mac: `UI/OnboardingView.swift`（12 ステップの `OnboardingStep`・2 ペイン/インタースティシャルの描画・`MicMeterView`/`GiantKeyView`/`PracticeStepView`/信頼カード・黒/白ピルの `BlackPillButtonStyle`）、`AppController.swift`（マイクモニタ `startMicMonitor`・録音キーテスト `hotkeyTestActive`＋`onHotkeyHeldChanged`・練習エンジン `startEngineForPractice`）、`Core/AudioRecorder.swift`（録音せずレベルだけ流すモニタリングモード `startMonitoring`/`stopMonitoring`）、`VoicekeyApp.swift`（オンボーディングをメインウィンドウへ統合・900×600↔760×600 リサイズ）、`UI/SettingsView.swift`（メインウィンドウにオンボーディング面を差し込む）。
- **Windows: セットアップガイドを Mac 同等の体験型 2 ペインへ再デザイン（release）**。従来の単一ガラス島・7 ステップ（ようこそ/ログイン/体験3/機能ツアー/完了）を刷新し、**左＝説明／右＝画面イメージ**の 2 ペイン＋上部パンくず（ようこそ › 準備 › 体験）に。Windows は OS 権限ゲートが無いのでステップは **ようこそ（インタースティシャル）→ ログイン → マイクテスト（新設）→ 録音キーテスト（新設）→ 体験①②③ → まとめ（インタースティシャル「話して、タイプしないで。」）** の 8 ステップ。**機能ツアーは廃止**。マイクテストは録音キーを押さずに入力レベルをバーで確認（無料枠を消費しない）、録音キーテストは押すと右の巨大キーが光る（`app.set_hotkey_test` で録音を開始しない）、体験は右のメモ風モックウィンドウに実際に入力する。
  - Windows: `ui/onboarding_window.py`（2 ペイン・8 ステップ・`_LevelMeter`/`_GiantKey`/`_Breadcrumb`・メモ風モック）、`core/mic_monitor.py`（新規・録音経路に触れず独自 InputStream で RMS だけ測る `MicLevelMonitor`）、`app.py`（`_on_press`/`_on_release` に録音キーテストのゲート・`set_hotkey_test`・設定からの `show_onboarding` 公開）。
- **ホーム/設定に「マイクテスト」「セットアップガイド（再表示）」を新設（両 OS・release）**。Mac はホームのダッシュボードに、録音キーを押さずにマイクの入力レベルを確かめられる**マイクテスト**（テスト/停止・ミニレベルメーター・入力デバイス選択）と、セットアップガイドを**もう一度見る**カードを追加（`UI/HomeView.swift`・`HomeMicTestModel`。ホーム以外のメニュー/トレイへは戻さない）。**Windows はホーム画面が無いため設定「一般」タブに同じ 2 つ**（マイクテストのレベルバー・「セットアップガイドを開く」ボタン）を新設（`ui/settings_window.py`・`open_onboarding_requested` シグナル → `app.show_onboarding`）。
- **Mac: ホーム画面の統計カードを 3 カード構成に再設計（release）**。旧「今日/今週/累計」1 枚カードを廃止し、①**累計入力**（主役=累計文字数・脇に累計回数と累計録音時間・下部にレベル/進捗バー）②**節約できた時間**（主役=推定節約時間・下に段階制の身近な換算 1 行〔カップ麺→通勤→映画→睡眠→日数の 6 段階・SF Symbols アイコン付き〕）③**この期間**（右上の「今日⇄今週」セグメントで切替・選択は保存されて次回も維持・主役=期間内文字数・脇に録音時間と回数・切替時に数値がロールする）の 3 枚へ。主役数字は 28-30pt の等幅太字、重要度の低い指標は小さく下に配置。
- **Mac: HUD のピル⇄録音インジケーターの変形を「単一カプセルの連続変形」に作り直し（release）**。従来はモード切替のたびにカプセルごと削除＋挿入されて「消して出し直す」感が残っていた。カプセル背景を単一ビューに固定してサイズ（幅・高さ）だけを spring で補間し、中身（波形・変換中表示）は opacity クロスフェードで出入りする構造へ変更（出現/消滅の transition は HUD 自体の表示・非表示のときだけ）。所要時間は従来と同じ（速度優先の禁則は不変）。カプセル背景は**背後のアプリが透ける磨りガラス**に刷新: SwiftUI の glassEffect は同一ウィンドウ内の背後しかサンプルできず、他アプリの上に重なる HUD では灰色の不透明な塊になる（構造問題）。ウィンドウ越しにサンプルできる AppKit の `NSGlassEffectView`（macOS 26・屈折あり）を一度採用したが、映り込みが**実質スナップショット**（再レイアウトまで固定）で背後の画面変化にリアルタイム追従しないことが実機の目視で判明したため、Dock やメニューバーと同じ合成経路で**毎フレーム背後をライブに映す `NSVisualEffectView`（`.behindWindow`）を既定に変更**（屈折の歪みは出ないが、背景色の変化へ常に追従する）。`NSGlassEffectView` は `VOICEKEY_HUD_LIQUID=1` の実験用に残置。さらにガラスの透け感を強化: ダークモードの material は tint が強く背後の文字が「平均色の塊」に塗り潰れるため（8 素材を実測比較）、VEV を `alphaValue=0.7` の線形混合で薄めて**背後の文字がぼんやり透ける「軽いガラス」**にし、リム（0.12→0.09）・上縁ハイライト（0.22→0.14）・影（0.15/0.25→0.10/0.16）も弱めてピルの存在感を下げた。Space／フルスクリーン切替後の固まりへの防御として、Space 切替・アプリ切替の通知で再描画（再レイアウト＋frame nudge）を強制する（タイマーなし・イベント駆動＝アイドル時の消費を足さない）。表示位置は**カプセル下端が常に画面下端（Dock 上端）+約 8pt に揃う下端アンカー**に変更（従来はパネル内センターで極小ピルほど高く浮いて見えた。モーフは下端から上に育つ）。
- **Mac: 録音中のライブ字幕（ストリーミング途中経過の HUD 表示）を撤去（release）**。リアルタイムモードの文字起こしは従来どおり録音と並行して進み、キーを離した瞬間の一括入力と速度は不変（撤去したのは録音中の HUD への逐次テキスト表示だけ）。録音中の HUD は波形バーのみのシンプルな表示になった。
- **Mac: 「変換中…」のスピナー（くるくる）を廃止し、「変換中…」テキスト自体のゆっくりした明滅に変更（release）**。waveform マークの明滅も撤去し、貼り付け先アプリのアイコン＋「変換中…」テキスト（opacity 1.0⇄0.35・easeInOut 0.8s 往復・サイズは揺れない）のみの構成に。「変換中」表示中は**カプセルのサイズを凍結**（録音インジケーターの大きさのまま維持）し、切替時の縮小アニメーション自体を無くした — 「変換中…」の文字が左右に動いて見える根因はカプセルの縮小だったため、フェードのタイミング調整ではなくサイズ変化そのものを根絶した。transcribing を離れるとアニメーションは確実に停止する（裏で回り続けない）。
- **新規ユーザーの既定ホットキーを変更（release）**: 録音キー 1（メイン）＝右⌘・押している間・**リアルタイム**（従来はスタンダード）、録音キー 2（サブ）＝右⌥・トグル（ハンズフリー）・スタンダード（従来どおり）。保存済み設定を持つ既存ユーザーには影響しない（初期値のみの変更。リアルタイム既定に伴い新規は整形既定 OFF＝モード別既定に整合）。
- **Mac: 長文並列分割を「2〜3 個への均等分割」に調整（release）**。従来は無音境界の数だけ無制限に分割され（サーバー消費が分割数ぶん増える）、逆に 0.7 秒の完全な息継ぎが無い長話では 1 本も分割されず遅いままだった。分割数を総再生長に応じて 2 個（24 秒未満）/ 3 個（24 秒以上）へ隣接結合で均等化し（順序は不変）、0.7 秒で境界が見つからないときは 0.35 秒の短いポーズで一度だけ再探索するようにした。
- **Mac: サイドノッチのスリットを黒基調＋うっすら外枠に変更し、クリック透過を完全に防止（v3.2・release）**。以前のアクセント色スリットを黒ベース（録音中だけアクセント色のグローで点灯）に変更。macOS が「透明ピクセルのクリックを下のアプリへ通す」挙動で誤クリックが起きていたため、スリット・履歴パネルとも可視要素の外側までパネル全域に極薄の実体塗りを敷いて透明ピクセルを無くし、`ignoresMouseEvents=false` と合わせて下のアプリへクリックが抜けないようにした。あわせて履歴パネルに検索フィールドを追加し、**全消去ボタンは撤去**（履歴の全削除はホーム画面の「最近の履歴」からのみ・サイドノッチには検索と「ホームを開く」だけを残す）。
- **Mac: 待機ピル→録音インジケーターのモーフィングを追加（v3.1・release）**。「ピルを常に表示」ON のとき、録音開始で待機中の小さな横長ピル（中身なしの極小横長）がそのまま大きく育って録音インジケーターへ連続変形し、終了で逆に縮む（同一 HUD パネルを使い回し・hide→show の作り直しをしない）。spring の減衰を高め（dampingFraction 0.82）にして「行き過ぎて戻る」バウンスを消し、中身は非対称フェード（旧中身を先に速く消し、カプセルが育ち始めてから新中身を出す）で「ピルがそのままスムーズに大きくなる」見え方に調整。
- **Mac: 設定・セットアップガイド・フィードバックの全ウィンドウを「浮遊するガラス島」デザインに全面刷新**。ウィンドウ全面は `NSVisualEffectView`（`.hudWindow` / behindWindow）＋アクセント色のグラデーションウォッシュで**デスクトップが強く透ける下地**になり、その上に**設定はサイドバーだけ**／セットアップ・フィードバックは中央カードが**影と上縁リムライト付きの角丸ガラス島として浮かぶ**（設定の右コンテンツは島にせず、ウィンドウ背景面としてフラットに敷く＝Claude デスクトップのサイドバー＋本文と同じ関係。エッジ to エッジの 2 分割と Divider は廃止・サイドバー島の四周に下地が見える）。ボタンは透けるガラスカプセル（macOS 26 は `Glass.clear.interactive()`）、主要アクションとサイドバー選択ピルは**アクセント色のグラデーション＋白リム＋グロー影**。設定はブランド行（アプリアイコン）とページ見出しを新設し、ナビにホバーハイライトを追加。`Form` の行フィルも半透明化して層に見えるようにした。
- **Mac: ウィンドウを画面の「真の中央」に表示**。`NSWindow.center()` は Apple 仕様で中央より上に置くため、`visibleFrame` から原点を手計算に変更。あわせて `NSHostingController` の初回レイアウト前は frame がサイズ 0 で中央計算が右上にずれる問題を `fittingSize` による事前サイズ確定で修正。
- **Windows: 設定・セットアップガイドを「浮遊するガラス島」風の配色に刷新**。対角の深いグラデーション（アクセント色を帯びた下地）の上に、サイドバー・コンテンツ・セットアップ本体が**角丸 16px の半透明パネル（上縁リムライト＋下縁の沈み込み）として浮かぶ**構成（最外レイアウトに 12px マージン・島間 10px で下地を覗かせる）。ボタンはカプセル寄りの半透明ガラス、プライマリとサイドバー選択は鮮やかなアクセントグラデ＋白リム上縁（ウィンドウ透過・DWM API は不使用＝環境非依存）。セットアップガイドは `MacTheme` のスタイルシートを適用し、設定画面とテーマ系統を統一。
- **Mac: 履歴の保持件数を 10 → 200 件に拡張し、各エントリにメタデータ（貼り付け先アプリ・文字数・日時）を保持**（Phase A）。旧フォーマット（文字列配列・メタデータ無しの旧 `HistoryItem`）は読み込み時に自動移行する。実績にも**アプリ別の集計**（回数・文字数・録音秒数）を追加（旧 `stats.json` は後方互換で読み込み）。

### Fixed
- **Mac: 初回起動でマイクとアクセシビリティ/入力監視の権限ポップアップが同時に複数出る不具合を修正（release・実機報告）**。セットアップガイドの目的は「初回起動時に権限ポップアップが一気に出ないようにする」ことなのに、初回起動で 2 つのダイアログがほぼ同時に出ていた。原因は `startup()` がマイク許可要求（`AudioRecorder.requestPermission()`）とホットキー監視の開始（`hotkeys.start()`＝入力監視）を**まとめて**呼んでいたこと。`startup()` を**サブシステム単位（マイク／入力監視／バックエンド暖機）の冪等メソッドに分解**し、それぞれ「その権限が確定済みのステップ以降でのみ」呼ぶ構造へ変更した。初回起動（オンボーディング表示中）は `startup()` 自体を呼ばず（`OnboardingDecider` が `.show` を返す間は従来どおり `startNow=false`）、権限プロンプトは**オンボーディングの各権限ステップの「許可する」ボタン押下時にだけ・1 つずつ**発火する（`requestMic`／`requestAccessibility`／`requestInputMonitoring`。ステップ進入時は状態を読むだけの `refreshCurrentPermission` のみ）。**既存ユーザー（権限付与済み・オンボーディング完了済み）の起動経路は不変**。常駐アプリでは遅延 Task/Timer が確実には発火しないため、ゲートはボタン押下＝同期・イベント駆動で行う。
  - Mac: `AppController.swift`（`startup` を `startMicSubsystem`/`startHotkeySubsystem`/`startBackendWarmupIfLoggedIn` に分解・冪等フラグ `micSubsystemStarted`/`hotkeySubsystemStarted`・各サブシステム起動を `.notice` で記録）、`VoicekeyApp.swift`（初回表示 `.show` 時は `startup()` を呼ばない従来分岐を維持）。
- **Mac: フルスクリーン中に待機ピルが隠れなかった不具合を修正（release）**。546ec43 で入れた「フルスクリーン Space では待機ピルだけ自動で隠す」機能が実機で全く効いていなかった（実機報告）。root-cause-debug の規律で自前フルスクリーン窓の再現ハーネスと os.log/NSLog 計測により、原因を 2 段で特定して直した。
  - **一次原因**: Space 切替を受ける `NSWorkspace.activeSpaceDidChangeNotification` の監視クロージャが `Task { @MainActor in self?.refreshBackdrop() }` で再評価を回していたが、常駐（背景・App Nap）状態のこのアプリではその Task がそもそも実行されず、待機ピルを隠す判定ロジックが丸ごと走っていなかった（Timer.scheduledTimer も同様に発火しないことを確認）。→ 同期実行の `MainActor.assumeIsolated { refreshBackdrop() }` に変更してコールバック内で確実に評価する。
  - **二次原因**: 判定に使っていた `NSScreen.main.visibleFrame≒frame` ヒューリスティックは、背景アプリから見ると自分が居る通常デスクトップ Space の値のまま（Dock/メニューバー分縮んだ値）で、他アプリのフルスクリーンを構造的に反映しない（計測でフルスクリーン中も終始 `vf=1512x897@y52`）。→ **CGWindowList で「画面全体を覆う通常レイヤー(0)のオンスクリーンウィンドウ」の有無で判定**する方式に置換（bounds と layer だけ参照＝ウィンドウ名も画像も取らないので Screen Recording 権限は不要・同期呼び出しで通知内評価が可能）。最大化ウィンドウは visibleFrame 高さ止まりなので誤検出しない。
  - あわせて念のため `DispatchQueue.main.asyncAfter`(0.4s/0.9s) の遅延再評価（アクティブ時のみ確実に走る保険・べき等）と、隠す/戻す遷移の `os.log` .notice 記録を追加。Windows はフルスクリーン判定の確実・軽量な手段が乏しいため対象外のまま（既存コメント維持）。再現ハーネスでフルスクリーン enter→待機ピル消失、exit→復帰の両方向を screencapture で確認。
  - Mac (`UI/Hud.swift`): `isMainScreenFullScreen()` を CGWindowList 判定に置換、Space 監視を同期実行化、`reevaluateIdlePillForFullScreen()`/`applyIdlePillFullScreenVisibility()` を追加。
- **HUD ピルの「変換中」表示が横に揺れる回帰を修正（両 OS・release）**。三段階サイズ化（546ec43）の際に、旧実装にあった「変換中はカプセルサイズを凍結する」ガード（`onPreferenceChange` の `if !isTranscribing`）を撤廃したのが原因。変換中は「変換中…」がゆっくり明滅（opacity 往復）するが、明滅の間に中身が再測定されて実測幅が 1px 未満で揺れ、カプセル幅が spring で追従し直す→中央寄せの「変換中…」が横へ流れて見えていた（実機報告）。三段階サイズ（待機<変換中<録音）は維持したまま、**変換中に入った直後の一度だけ**実測サイズを採用して以降は凍結する方式に変更（録音→変換中の縮小モーフは初回採用で 1 回だけ発火し、明滅では再発火しない）。あわせて **HUD 検証用のデバッグ環境変数 `VOICEKEY_HUD_DEBUG_STATE`**（`transcribing`/`recording`/`idle` を起動時に固定表示）を追加。
  - Mac (`UI/Hud.swift`): `HudView` に凍結ロック `transcribingSizeLocked` を追加し、`onPreferenceChange` は変換中の初回のみ `contentSize` を採用。`onChange(of: isTranscribing)` の離脱で凍結を解除。`HudController` にデバッグ固定表示フック `applyDebugStateIfNeeded()`（`AppController` init から呼ぶ・指定時は `update(for:)` を無視）。
  - Windows (`ui/hud.py`): 元々 `_disp_w/_disp_h` を遷移時に一度だけ確定し、明滅（`_on_blink_tick`）は不透明度だけを変える設計で横揺れしない（`_target_size` は paint 毎に呼ばず凍結値で描画）。回帰ガードとして `tests/test_hud.py` に「変換中に入った後、明滅を進めても目標/表示サイズが不変（不透明度だけ変化）」のテストを追加。
- **Mac: 全ウィンドウで ⌘V/⌘C/⌘X/⌘A が効かずビープ音になる問題を修正**。メニューバー常駐（`.accessory`）アプリは `NSApp.mainMenu` を持たず、標準編集キーがレスポンダチェーンの `cut:`/`copy:`/`paste:`/`selectAll:` へ届かないため、セットアップガイドの入力欄・内蔵ターミナル等で貼り付け不能だった（2026-07-05 実機報告）。`applicationDidFinishLaunching` 冒頭でアプリ＋編集メニューをコードで構築して `NSApp.mainMenu` に設定する `installMainMenu()` を追加（メニューは画面に出ないがキーイベントのフォールバック先として機能する）。Windows は AppKit 固有のため無関係。
- **リアルタイム（Deepgram）で日本語発話が韓国語等に文字起こしされることがある問題を修正（両OS）**。nova-3 導入当時は ja 単言語指定が未対応だったため多言語自動判定（`language=multi`）で接続しており、multi の言語判定が日本語を韓国語等に誤ることがあった。現在の nova-3 は ja 単言語をサポート済み（2026-07 Deepgram ドキュメント確認）のため、ストリーミング（WebSocket）と REST の全 Deepgram 経路（Mac / Windows とも）で設定言語（既定 ja）をそのまま送るように変更。
- **Mac: ホットキーを離した瞬間の HUD 反応が最大 0.4 秒遅れていた問題を修正**。短いタップの離鍵後、ダブルタップ 2 打目を「録音を止めずに」`kDoubleTapWindow`(0.4 秒) 待ってから確定していたため、無音でキーを離してもインジケーターが 0.4 秒消えず、発話ありでも「変換中…」への切替が遅れて見えていた。離鍵で**即座に確定**する方式へ変更（UI 状態更新はパイプライン処理を待たず離鍵直後に走る）。ダブルタップ（auto_enter）は 2 打目の押下時に直近の離鍵記録から検出し、新しい録音を auto_enter で開始する（連続録音の仕組みを流用）。
- **Mac: セットアップガイドが「初回起動時だけ」を確実に守るように修正**。アプリが強制終了（SIGKILL・ビルド検証の kill 等）されるとウィンドウクローズ処理が走らず完了フラグが保存されないため、次回起動でも再表示される穴があった。自動表示を決めた時点で完了フラグを即保存する方式に変更（メニューの「セットアップガイド…」からの手動再表示・入力監視の権限反映再起動からの再開は従来どおり）。

### Technical Details
- **Phase C / SideNotch.swift（新規）**: `SideNotchController`（常駐スリット＋履歴パネルの生成・配置・録音状態連動・画面構成変更での再配置）／`SideNotchSlitView`（黒基調バー＋薄グレー外枠の CALayer・`NSTrackingArea(.activeAlways)` でホバー拡大・録音中はアクセント色グロー）／`SideNotchHistoryView`（検索フィールド＋履歴リスト・`filteredItems` でテキスト/アプリ名の大文字小文字無視部分一致）。v3.2: 検索入力用に `canBecomeKey=true` の `SideNotchHistoryPanel` を導入し `openHistory` で `makeKey`（`.nonactivatingPanel` 維持）、開くたび検索欄をリセット＆再フォーカスするためパネルは閉じるときに破棄。クリック透過防止はスリット NSView の `layer.backgroundColor` とパネル外周の `ZStack` に極薄塗り（alpha ~0.02）＋両パネルに `ignoresMouseEvents=false`
- **v3.1 / MainWindowView.swift**: ホーム/設定を同一ウィンドウで切り替えるサイドバー統合。`VoicekeyApp` の設定導線（メニュー「設定…」・⌘,・`VOICEKEY_OPEN_SETTINGS`・再オープン）をメインウィンドウへ集約。デバッグ用 `VOICEKEY_OPEN_NOTCH`（env/defaults・読み取り後削除）でサイドノッチ履歴パネルを起動時に自動表示
- **Phase B / VoicekeyApp.swift**: `homeWindow` と `showHome()` を追加（設定と同じ生成・キャッシュ再利用・真の中央）。`handleReopen` の遷移先を設定→ホームへ変更、メニューに「ホーム」項目を追加し「アップデートを確認…」を削除。`restoreAccessoryPolicyIfNoUserWindows` の判定に `homeWindow` を追加。デバッグ用 `VOICEKEY_OPEN_HOME`（env/defaults・読み取り後削除）で起動時に自動表示。`SettingsView` の生成引数から `history` / `stats` を削除
- **Phase B / HomeView.swift（新規）**: `HomeView(config:history:stats:updater:onOpenSettings:)`。統計は `StatsStore` の既存 API（`charactersInLast` / `totalCharacters` / `level` / `levelProgress` / `savedSeconds` 等）、アプリ別は `stats.data.appUsage` の上位 5 件、履歴は `history.items.prefix(8)`。アイコンは `NSWorkspace.urlForApplication(withBundleIdentifier:)`→`icon(forFile:)`（未解決は `app.dashed`）。カードは `glassFormRows` と同じ半透明フィルで島化しない
- **Phase B / UpdaterController.swift**: `availableVersion`（@Published・単一情報源）を導入し `updateAvailable` / `availableVersionString`（AboutTab 互換の別名）を導出。Sparkle の自動バックグラウンドチェックを `automaticallyChecksForUpdates = false` で停止し、`Timer` で 5 分後＋6 時間ごとに `checkForUpdateInformation()`（UI なし）を実行。検知は `SPUUpdaterDelegate` の `didFindValidUpdate` / `updaterDidNotFindUpdate` で反映
- **Phase B / SettingsView.swift**: `navItems` から実績（tag 3）・履歴（tag 4）を削除、`content` の該当 case と `StatsTab` / `HistoryTab` / `StatsPeriod` / `AnimatedNumber` / `HistoryRow` の各 View 実装、および未使用になった `import Charts` を削除（実装はホームへ移設）
- **VoicekeyApp.swift**: `centerOnScreen` / `restoreAccessoryPolicyIfNoUserWindows` / `handleReopen` を追加。設定/フィードバックにも `NSWindowDelegate` を配線し、`windowWillClose` で可視ウィンドウ数を見て `.accessory` へ復帰
- **Glass.swift（新規）**: `glassSurface` / `glassCapsule` / `glassIsland`（島＝リムライト＋影）/ `glassFormRows`（行フィル半透明化）/ `glassButtons` / `glassProminentButton` / `LiquidButtonStyle`（全 OS 共通・非 prominent は透けるガラス、prominent はアクセントグラデ）/ `VisualEffectBackdrop`（`.hudWindow`＋ウォッシュ）/ `GlassWindow.applyFrostedChrome` / `GlassGroup`
- **SettingsView**: サイドバーのみガラス島（`sidebar` に `glassIsland`＋四周マージン）・右コンテンツは島にしないフラットな `contentPane`（`HStack(spacing:12)`・Divider 廃止・700×600）・ブランド行・ページ見出し・ナビのグラデ選択ピルとホバー・全タブに `glassFormRows`。Phase A で「一般」に再貼り付け行、「表示 / サウンド / 履歴」セクションを追加
- **Phase A 新規 Core**: `SoundFX.swift`（`AVAudioEngine` で開始/停止ブリップをコード合成・撃ちっぱなし）/ `MediaDucker.swift`（CoreAudio `VirtualMainVolume` を 12% へ・逆転ガード・クラッシュ耐性フラグ）/ `FrontAppTracker.swift`（`NSWorkspace.didActivateApplicationNotification` で貼り付け先アプリを追跡）
- **HistoryStore / StatsStore**: `HistoryItem`（`appBundleID` / `appName` / `characters` を `decodeIfPresent` で旧 JSON 互換）・maxItems 10→200・`AppStat` とアプリ別 `appUsage` 集計を追加。両ストアに `init(directory:)` を足してテスト隔離
- **AppController**: `pendingTapFinish`（0.4 秒の遅延停止タスク）を廃止し離鍵で即 `finishRecording`。`beginRecording`/`finishRecording` に操作音・ダッキング・貼り付け先アイコン取得を配線。再貼り付けショートカット（`repasteKeyPressed` / `repasteLast`）を追加
- **ConfigStore**: `soundEffectsEnabled` / `duckMediaEnabled` / `repasteKey` / `hudAlwaysVisible` / `dockIconAlwaysVisible` / `sideNotchEnabled` / `historyEnabled` を追加（`repasteKey` は「未設定＝⌃⌘V」と「空＝明示的に無効」を区別）
- **OnboardingView / FeedbackView**: 中央 1 島化（`glassIsland`）・ステップアイコンをアクセントグラデ円にヒーロー化（660×588 / 452）
- **styles.py**: 両パレット刷新＋新属性 `BG_WINDOW`（対角グラデ）/ `CONTENT_BG` / `POPUP_BG` / `GLASS_EDGE`（上縁リム）/ `EDGE_BOTTOM`（下縁の沈み）/ `ACCENT_GRAD` 系（ポップアップは別トップレベルウィンドウで rgba が事故るため不透明 `POPUP_BG` に分離）
- **settings_window.py**: ルートレイアウトに margins 12px / spacing 10px・右ペインを `QFrame#contentPane` 化（島スタイル適用のため）
- **onboarding_window.py**: `get_stylesheet` を構築時適用・コンテンツ全体を `QFrame#onboardingIsland` で 1 島化・インラインスタイルをグローバル QSS（`QFrame#card` / `#hairline` / `class="primary"`）へ整理

## [1.8.0] - 2026-07-03

### Added
- **アプリアイコンを新ブランドロゴへ刷新（両 OS）**。VoiceKey の新ロゴ（インディゴのグラデーション地に「波形→テキストカーソル」のシンボル）を Mac（`AppIcon.icns`）・Windows（`icon.ico`）両方へ展開。サイト（favicon・LP）と同一デザインでブランドを統一。
- **初回起動オンボーディング（初回セットアップガイド）を追加（両 OS・release）**。初回起動でアプリが黙って常駐して「何これ？」となるのを解消し、権限取得を場当たりでなく順に案内する。
  - **Mac（6 ステップ）**: ようこそ → マイク → アクセシビリティ → 入力監視 → ログイン → 完了。各権限ステップは 1 秒ポーリングで許可されたら✓を出して次へ進める。マイクは拒否済みならシステム設定を開くボタンへ切替。入力監視は許可後に `CGEventTap` の作成を試し、失敗したら「アプリを再起動」ボタンを出す（反映にプロセス再起動が要る実測ケース）。再起動時は現在ステップを保存して再開する。ログインは既存の `LoginCoordinator` を購読し「あとで」でスキップ可。完了ページは実際の既定設定（録音キー・文字起こしモード・録音のしかた）を `ConfigStore` から読んで表示する。
  - **Windows（3 ステップ）**: ようこそ → ログイン（`login_coordinator` を流用・「あとで」可）→ 使い方（既定ホットキーを表示）。Windows は OS 権限ゲートが無いため権限ステップは持たない。
  - **起動シーケンス**: 初回未完了なら本体の `startup()` を呼ばずにセットアップを表示し、完了/クローズ時に開始する（説明前にいきなりマイクダイアログが出る事故を防ぐ）。**既存ユーザー**（Mac=`didSetupLaunchAtLogin` が既に true／Windows=起動時に `settings.yaml` が既に存在）には出さず、完了フラグを補完する。ウィンドウを閉じてもスキップ扱いで完了フラグを立て、毎回は出さない。完了直後の権限チェック NSAlert は二重に出さないよう抑止する。
  - **デバッグ再表示**: Mac=環境変数/UserDefaults `VOICEKEY_OPEN_ONBOARDING` で強制表示＋メニューバーに「セットアップガイド…」を追加していつでも開ける。
  - 文言は Phase 4 の新語彙（録音キー／文字起こしモード／リアルタイム／スタンダード／ハンズフリーキー／文章を自動で整える）に統一（「バックエンド」「トグル」「LLM」等の旧語彙は不使用）。製品名表記は「VoiceKey」。
  - フラグ: Mac=`UserDefaults`（`didCompleteOnboarding` / 中断再開用 `onboardingStep`）／Windows=`settings.yaml` の `did_complete_onboarding`（既存キーの deep merge で消えない）。保存キー・既存設定の互換は不変。

### Changed
- **設定 UI の文言を一般ユーザー向けに刷新（両 OS・release・表示文言のみ）**。専門用語（バックエンド / LLM / トグル 等）を日常語へ言い換えた。保存キー・enum の rawValue・settings.yaml のキー名は一切変更していない（decode/マイグレーション互換を維持）。
  - 設定行ラベル: 「バックエンド」→「文字起こしモード」、「動作」→「録音のしかた」（選択肢「押している間」→「押している間だけ」/「トグル」→「押すたびに開始・停止」）、「テキスト整形（LLM）」→「文章を自動で整える」、「自動 Enter の遅延」→「ダブルタップ送信の待ち時間」、「ハンズフリー切替キー」→「ハンズフリーキー」、「入力デバイス」→「マイク」。
  - ページ/タブ名: 「ホットキー 1」→「録音キー 1（メイン）」、「ホットキー 2」→「録音キー 2（サブ）」。
  - 説明キャプションを追加/差し替え: 「文章を自動で整える」の下に「『えー』『あの』などの言いよどみを除去し、句読点や改行を整えます。」、「ダブルタップ送信の待ち時間」に「録音キーを素早く2回押したとき、貼り付け後に Enter を自動で押すまでの待ち時間です。」、「ハンズフリーキー」に「このキーを押しながら録音キーを押すと、押しっぱなしにしなくても録音が続きます（もう一度録音キーを押すと停止）。」（実挙動＝録音キー併用の修飾キーに合わせた説明）。
  - Mac=`AppConfig.HotkeyMode.label` / `SettingsView`、Windows=`settings_window._MODE_LABELS` / 各行ラベル・ページ名。README の該当する設定項目名も新文言へ更新。
- **テキスト整形をモード別の既定に整理し、整形プロンプト入力欄を撤去（両 OS・release）**。
  - **モード別の整形既定**: リアルタイム(Deepgram)=**既定 OFF**（速度全振り）、スタンダード(Groq)=**既定 ON**（録音後にきれいに整形）。設定でモード（バックエンド）を切り替えると整形トグルがそのモードの既定へ追従する（ユーザーはその後トグルで自由に上書き可）。
  - **一回限りのマイグレーション**: 既存ユーザーは整形が明示 ON で保存されているため、初回だけ「リアルタイム(Deepgram)スロットの整形を既定 OFF」へ矯正する。以後は二度と触らない＝その後ユーザーが Deepgram で整形 ON にしたら尊重する（Mac=`UserDefaults` キー `didMigrateModeDefaultsV18` / Windows=`settings.yaml` の `migrated_format_defaults` マーカー）。
  - **「プロンプト（任意）」入力欄を両 OS で撤去**（release のプロキシ経路では STT プロンプトを送っておらず機能喪失ゼロ）。保存フィールド（`SlotConfig.prompt` / `api_prompt`）自体は互換のため残し、値は既存保存値をそのまま維持する。
- **スタンダード(Groq)の文字起こし＋整形を 1 リクエストに統合（両 OS・release）**。
  - スタンダード × 整形 ON × ログイン済み × ハンズフリー EL 差し替えでない、かつ**単発送信**のときは、Groq プロキシへ `format=1` を付けて送り、サーバー内で STT→整形まで実行した整形済みテキストを受け取る（録音後のクライアント整形の往復を省く）。**分割送信（2 セグメント以上）は従来どおり**各セグメント整形なしで送り、結合後にクライアント整形する。
  - サーバーが整形失敗（`formatted:false`）でも `text` に STT 原文が入って返るため、**クライアント側での再整形はしない**（同じ Groq 障害で二度失敗＋遅延増になるため）。
  - 計測ログの整形段は統合時に「整形 サーバー統合」（Mac）／「整形: サーバー統合」（Windows）と表記する。
- **製品版の文字起こしモードを 2 択に整理し、スタンダードのハンズフリー録音時は内部で高精度エンジンへ自動切替（両 OS・release）**。
  ユーザーが選ぶモードを従来の 3 択から **「リアルタイム」（=Deepgram nova-3・話しながら表示）/「スタンダード」（=Groq whisper-large-v3-turbo・録音後にきれいに整形・既定）** の 2 択に絞った。
  - **「スタンダード」をハンズフリー録音（toggle 実効＝トグルモード or ハンズフリー切替キー併用）で使うときは、録音の処理エンジンだけ内部で ElevenLabs（scribe_v1）へ自動切替する**（長時間録音の精度対策）。保存値・UI 表示は「スタンダード（Groq）」のまま変わらず、切替はアプリが内部で行う（設定不要）。
  - UI: バックエンド選択の直下に選択中モードの説明を追加（リアルタイム=「話しながら文字が表示されます（最速）」／スタンダード=「録音後にきれいな文章にして入力します（おすすめ）」＋「ハンズフリー録音のときは、長い録音に強いエンジンへ自動で切り替わります。」）。
  - **既定スロットを両ホットキーとも「スタンダード」（Groq）に統一**（従来はハンズフリー側が高精度=ElevenLabs 固定）。ハンズフリーの精度は上記の内部自動切替で担保する。
- **設計方針**: バックエンドの enum・保存値は 4 値（deepgram/groq/elevenlabs/openai）のまま維持し、「ユーザーが選べる集合」だけを 2 値に絞った（保存データの decode 互換・ElevenLabs の内部利用継続・main とのコード形状差の最小化のため）。

### Fixed
- **旧保存値のマイグレーション**: 既存ユーザーが**ハンズフリー側などに「高精度」（ElevenLabs）を明示選択していた場合は「スタンダード」（Groq）へ移行される**（openai も同様に移行）。deepgram（リアルタイム）・groq（スタンダード）はそのまま維持。移行時はモデル指定を空にして各バックエンドの推奨モデルへフォールバックさせる（Mac=`SlotConfig` decode / Windows=`_constrain_release_backends`）。
- **Windows 設定画面のバックエンド表示の化けを予防**: 保存値が選択肢に無い（旧 elevenlabs 等）場合の判定を「combo に存在するか（`findData >= 0`）」に修正し、無ければ「スタンダード」（groq）へフォールバックするようにした（従来は `max(0, -1)` で先頭＝リアルタイムに化ける芽があった）。
- **Mac の「高精度」/「OpenAI」ラベル重複バグを修正**: 内部利用の elevenlabs=「スタンダード（ハンズフリー）」、選択肢外の openai=「OpenAI（開発用）」に整理（計測ログ・エラーメッセージ用）。

### Technical Details
- **Mac（`macos/Sources/Voicekey/`）**: `Backend.selectableCases` を `[.deepgram, .groq]` に縮小。`AppController` に `handsfreeTranscriber`（EL scribe_v1・`rebuildTranscribers` で言語追随）を常設し、`beginRecording` で `effectiveMode == .toggle && slot.backend == .groq` のとき `RecordContext.transcriber` を差し替え＋prewarm。`warmBackendsNow` の EL 暖機条件を「groq スロットが toggle か、ハンズフリー切替キー設定あり」に変更。`SettingsView.SlotSettingsTab` にモード説明キャプションを追加。
- **Windows（`src/`）**: `RELEASE_TRANSCRIBE_BACKENDS = ("deepgram", "groq")`。`app.py` に `_build_handsfree_transcriber` / `_maybe_handsfree_slot`（`self._handsfree_transcriber`）を追加し、`_begin_recording` のスナップショットへ EL スロットを渡す（ログ「ハンズフリー: 内部エンジン切替 (groq→elevenlabs)」）。`_warm_backends_now` の EL 暖機条件を Mac と同一化。`settings_window.py` に `_backend_caption_text` とキャプション更新（`_update_backend_caption`）を追加。DEFAULT_CONFIG の hotkey2 backend を groq に。
- **テスト**: Mac `SlotConfigMigrationTests`（旧保存値 JSON の decode 移行を検証）を新規追加。Windows `test_handsfree_logic`（`_maybe_handsfree_slot` の toggle/hold/deepgram 分岐）・`test_config_manager`（elevenlabs→groq 移行）を更新。
- **オンボーディング（Phase 5）**: Mac=`UI/OnboardingView.swift`（`OnboardingStep` / `OnboardingModel` / `OnboardingDecider` / `OnboardingView`）新規、`VoicekeyApp.swift`（起動分岐・`showOnboarding`・`NSWindowDelegate`・再起動イディオム・メニュー項目）改修、`AppController.startup(showPermissionAlert:)` と `HotkeyMonitor.canCreateEventTap()` を追加。Windows=`ui/onboarding_window.py`（`OnboardingWindow`）新規、`app.py`（`_maybe_show_onboarding` / `_on_onboarding_finished`・`QTimer.singleShot`）、`config_manager.config_file_existed`・`constants.DEFAULT_CONFIG["did_complete_onboarding"]` を追加。テスト=Mac `OnboardingDeciderTests`（起動時分岐の純関数）、Windows `test_onboarding_window`（offscreen スモーク）・`test_config_manager` の初回起動フラグ。
- **製品版の「高速リアルタイム」(Deepgram ストリーミング) が main と違い「話した瞬間に出ない／ローディングが入る」遅延を根治（Mac・release）**。
  原因は文字起こし処理でも通信でもなく、**WebSocket を開くタイミング**だった。main（自分用・未ログイン）は
  親キーで **`connect()` を同期実行**＝ホットキーを押した瞬間に WS ハンドシェイクを始めるのに対し、
  製品版（ログイン）は毎回 `Task { await 短命トークン取得 }` の**非同期完了を待ってから** WS を開いていた。
  暖機ループ（`warmBackendsNow`）が起動時・4 分間隔・スリープ復帰時に**有効な再利用トークンを先読みキャッシュ済み**
  （設計意図は「録音開始ゼロ待ち」）なのに、`start()` がキャッシュを使わず毎回非同期経路を通っていたため、
  この「ゼロ待ち」が非同期ホップぶん潰され、短い発話ではライブ字幕が追いつかず「ローディング」に見えていた。
  - 修正: `BackendClient.cachedEphemeralTokenIfValid()`（同期・往復ゼロ）を追加し、`StreamingTranscriber.start()` は
    **温めたトークンがあれば main と同じく同期で即 connect**（cold 時＝起床直後・暖機漏れ・TTL 超過のみ従来の非同期取得へ）。
    warm ヒット時の WS 開始時刻が main と一致し、話した瞬間からライブ表示・貼り付けが始まる。消費計上（jti/meter）は非同期経路と同一。
  - **Windows は変更不要**（スレッド設計で `fetch_ephemeral_token()`＝キャッシュヒット時ほぼ 0ms→即 `connect()` と既に同期。
    Mac の `Task{await}` 非同期デファーが無い＝この遅延は Mac 固有の実装差）。

## [1.7.0] - 2026-07-01

### Fixed
- **「ログインの有効期限が切れました」が頻発する誤表示を根治（段階0・両 OS・release）**。
  サーバーの `refresh` は「他経路（別タスク／別プロセス）が既に refresh_token を回転済み＝
  セッションは有効」を **409（`refresh_conflict`）** で正しく返していたが、クライアントが 409 を
  処理しておらず、**まだ有効なのに期限切れ扱い**して再ログインを要求していた。
  - **Mac**（Swift）: `AuthClient.send` が 409 を `default → .server(409)` で投げ、`performRefresh` が
    そのまま伝播 → `BackendClient` の 401 再試行が不成立 → 元の 401 が `.unauthorized`
    （「ログインの有効期限が切れました」）として表面化していた。
  - **Windows**（Python）: `_perform_refresh` が 409 を `if e.status == 401` に当てられず素通りで再送出 →
    `backend_client._send` の 401 再試行が不成立で同じく誤表示。
  - 修正は両 OS とも refresh 処理に **409 吸収を 1 箇所追加**するだけ（破棄せず最新セッションを返す）。
    401 再試行はそのまま連動し、サーバーは変更なし。再現テスト（409 でセッションを破棄しないこと）を追加。

### Changed
- **製品版の文字起こしを 3 モード「高速リアルタイム(Deepgram) / 正確性(Groq) / 高精度(ElevenLabs)」に整理（両 OS・release＋サーバ）**。
  速度改善の一環。**普通入力の既定を Groq(whisper-large-v3-turbo)＝「正確性」に置き換え**、あなたが遅いと感じていた
  **「録音開始時のトークン取得」ステップを普通入力から消した**（Groq は Deepgram のような短命トークンが不要）。
  **Deepgram は「高速リアルタイム」（ストリーミング）として選択肢に残す**（話しながらライブ表示したいとき用）。
  ラベルを整理: Deepgram=「高速リアルタイム」/ Groq=「正確性」/ ElevenLabs=「高精度」。
  - **サーバ**（voicekey-site）: `POST /api/v1/transcribe/groq` を新設。**Edge Runtime＋東京(hnd1)** で Node の cold start を排除
    （普通入力は短尺なので Edge で全量バッファ→Groq へ中継）。`consume:true`（1 プロキシ呼び出し=無料枠 1 消費）。
    `GROQ_API_KEY` は整形用の既設のものを流用。暖機 GET 付き。EL「高精度」プロキシは長文 passthrough のため Node 据え置き。
  - **クライアント**（Mac=Swift / Windows=Python）: 文字起こし選択肢を `Deepgram/Groq/ElevenLabs` の 3 モードに。
    Groq もサーバープロキシ経由（`transcribeGroq`/`transcribe_groq`）。既定スロットは **1=普通入力(正確性=Groq・押している間) /
    2=ハンズフリー(高精度=ElevenLabs・トグル)**。保存済み deepgram はそのまま維持（範囲外の openai のみ groq へ移行）。
    暖機に Groq プロキシを追加。
- **製品版のアップロードを FLAC 化して STT の通信時間を約半分に（Mac・release＋サーバ）**。
  従来サーバープロキシ経路（Groq「正確性」/ ElevenLabs「高精度」）だけ生 WAV を送っていたため、
  main（自分用・直叩き）の約 2 倍のバイト数を日本↔東京↔米国へアップロードしていた。可逆圧縮の
  **FLAC**（main の直叩きが両プロバイダーへ FLAC で実証済み・16bit 量子化は WAV と同一で精度への影響ゼロ）に
  そろえ、**アップロード量を約 43%（実測音源で 234KB→100KB）に削減**＝「1 ホップ」の体感差をさらに縮めた。
  - **サーバ**（voicekey-site）: Groq プロキシがクライアントの filename をそのまま Groq へ引き継ぐよう修正
    （`audio.flac` なら FLAC として復号される）。後方互換（filename 未指定/`.wav` は従来どおり）。EL は passthrough で
    生ボディを EL へ流すためサーバー変更不要。
  - **クライアント**（Mac=Swift）: Groq/EL プロキシ送信を `WavEncoder`→`encodeAudio`（FLAC 優先・失敗時 WAV）に。
  - **Windows は当面 WAV 据え置き**（Python に無料の FLAC エンコーダが無く、native lib 同梱は onnxruntime 同梱バグと同種の
    リスクを実機テスト不可の Windows ビルドに負うため。出力テキストは完全同一・退行なし＝VAD が Mac=RMS/Win=ONNX と
    実装分岐しているのと同じ「OS 固有内部実装」の範疇）。
- **放置後の初回録音の遅さを根治（両 OS・release）**。長時間放置後の初回で「ログインセッション期限切れ→更新往復」を
  録音のクリティカルパスで踏んでいたのを、暖機のたびに**先回りでセッションを有効化**（`warm_session`/`ensureValidSession`）して解消。
  Mac は加えて **App Nap を抑止**（`beginActivity(.background)`）してアイドル中も暖機ループを間引かせない＝放置後もセッション/トークンが手元に温存される。
- **段階別タイミングログを追加（Mac・release）**。録音停止→貼付を「VAD / 文字起こし / 整形 / 貼付」に分解して 1 行で出力し、
  main との速度差の在り処を実測できるようにした（`[計測] … 総計Xms（VAD … / 文字起こし … / 整形 … / 貼付 …）`）。
- **無料も有料と同じ「再利用トークン方式」に統一＝無料の録音開始のサーバー往復をゼロに（段階3・両 OS・release＋サーバ）**。
  無料が有料より遅い主因は「無料枠の回数計測を録音開始のクリティカルパスでサーバーにやらせていた」こと。
  無料は録音ごとに使い捨てトークンを取り直す設計だったため、回数カウントのためだけに毎録音サーバーを叩いて
  いた（cold start 込み）。これを撤廃し、有料と同一の機構にする。
  - **サーバ**（voicekey-site）: `x-vk-confirm:"2"` の新クライアントには、無料でも**発行時に消費しない
    再利用可トークン**（`cacheable:true`・`meter:true`）を返す。発行では残枠チェックのみ。消費は録音成立後に
    `/api/v1/usage/confirm`（`jti` なし）で非同期に +1 する＝**録音開始のサーバー往復をゼロ**にできる。
    `usage/confirm` は `jti` 有無で段階1（`confirm_free_quota`）/段階3（`increment_free_used`）を受け分ける。
    `getEntitlementSnapshot` で paid 判定＋残量を 1 クエリ化。DB に `increment_free_used`（原子的 +1・paid は no-op）を追加。
    後方互換: `"1"`=hold（段階1）/ヘッダ無し=即 consume（段階0）は維持＝**既存配布版は無改修で動く**。
  - **クライアント**（Mac=Swift / Windows=Python）: 起動時・録音後・スリープ復帰の**先読みを無料でも実行**
    （先読みは消費しない）。`EphemeralToken` に `meter` を追加し、録音成立時に `jti` があれば `confirmUsage(jti)`、
    無ければ `meter` で `confirmUsageCount()`（jti なし）を非同期送信。ストリーミングが空文字で REST へ
    フォールバックした 1 録音も REST 経路で確定する。**親キーはクライアントに埋め込まない（サーバー認証を維持）**。
  - 残枠超過の窓（使い切った瞬間の TTL 内で数回だけ余分に録音できる）は許容し、次の発行で 402 が閉じる
    ＝厳密な「1録音=1消費」より**無料体験の速さ（第一印象）**を優先する判断。TTL は 300 秒に据え置き。
- **録音開始のサーバー認証を理論最速化＝放置後の初回録音の遅延を根治（段階2・両 OS・release＋サーバ）**。
  「しばらく放置した後の初回録音が遅い」原因を本番リージョン(hnd1)から実測で特定: ①Vercel 関数の
  cold start（約1.0-1.2秒）、②`entitlements`→`rateLimit`→`device`→`Deepgram grant` を直列で踏む往復
  （warm でも約300ms）、③短命トークン TTL が60秒しかなく60秒超の中断ごとにフル発行が走ること。
  - **サーバ**（voicekey-site）: 認証を Supabase 公開鍵による**ローカル JWT 署名検証**にして GoTrue 往復を
    撤去（失敗時のみ従来検証に保険フォールバック）。`/auth/ephemeral` の独立3往復を **Promise.all で並列化**し
    hold と Deepgram grant を重ねて実行（直列和≈300ms → grant 支配≈160ms）。`rateLimit` 通過後に grant を
    発行する順序は維持＝親キー濫用ゲートを残す。**TTL 60s → 300s**（クライアントはこの値ぶんキャッシュ再利用
    するため、数分の中断ならフル発行を踏まず往復ゼロ）。**サーバーのみの変更＝既存配布版にも即効**。
  - **クライアント**（Mac=Swift / Windows=Python）: 暖機ループを「有料はトークンを**先読み**してキャッシュ」
    （アイドル中も常に有効トークンが手元にあり録音開始ゼロ待ち。無料は枠を守る GET 暖機のまま）に変更。
    さらに**スリープ復帰を検知して即・暖機＋先読み**（Mac=`NSWorkspace.didWake`、Windows=ウォールクロック
    跳躍検知）。復帰直後の話し始めの待ちを消す。親キーはクライアントに埋め込まない（サーバー認証を維持）。
- **無料体験の遅延解消＝保留/確定方式（段階1・両 OS・release）**。録音開始のたびに走っていた
  同期 `consume_free_quota` 往復（400-800ms）を録音開始のクリティカルパスから外した。「1録音=1消費」は
  維持したまま、開始遅延を解消する。
  - トークン発行時は消費せず**保留(hold)**のみ。録音が成功（文字起こし非空）したら
    `/api/v1/usage/confirm` に保留 `jti` を送って確定する（`free_used` を +1）。空文字（無音/接続失敗で
    REST フォールバック）のときは確定しない＝保留は TTL で自動的に戻る（refund）。
  - `jti` 付きトークンは取得時に **1 回限り pop**（キャッシュから除去）。旧クライアント（`x-vk-confirm`
    ヘッダ無し）は従来どおり**即時 consume にフォールバック**（後方互換）。
  - **Mac**: `BackendClient` に `jti` / `x-vk-confirm` / `confirmUsage`、`StreamingTranscriber` が start で
    保留 jti を控え finish の非空時に確定。**Windows**: `backend_client.confirm_usage`、
    `streaming_transcriber` が threading で確定送信。`constants` に `API_USAGE_CONFIRM_PATH`。
  - **サーバ**（voicekey-site）: `/api/v1/usage/confirm`、`auth/ephemeral` の hold 化、
    DB マイグレーション `0017`（hold/confirm/refund/sweep RPC）。
- **「高速リアルタイム」（Deepgram ストリーミング）の話し始めの遅延を軽減（段階A・両 OS・release）**。
  リアルタイム入力は録音開始の瞬間に「短命トークンの取得（サーバー往復）」＋「Deepgram への
  WebSocket 接続確立」を直列で踏むため、最初の文字が出るまで体感の遅れがあった。第1段として
  **録音終了直後に次回分の短命トークンを先読み／暖機**し、次の録音開始を warm path に乗せる。
  - 有料アカウント（active）= 次回トークンを**先読み取得**（録音直後にキャッシュへ）。
  - 無料／未確定 = サーバーの**暖機 GET**（消費なし）でプロキシ関数を温存。
  - これにより「録音 → 終了 → すぐ次の録音」を繰り返す通常運用で、2 回目以降の開始遅延を縮める。
- **開始遅延の内訳を実測するログを追加（段階A・両 OS）**。録音開始からの
  「短命トークン取得 ms」「WS 接続 ms」「最初の文字まで ms」をログ出力し、
  「始まりが遅い」の主因（サーバー往復か WS ハンドシェイクか）を実使用で裏取りできるようにした
  （段階B＝WS 事前接続の設計判断に使う）。

### Technical Details
- **Windows**（Python）`app.py`: `_prewarm_backend` で取得済みの `active` を `_account_active` に保持。
  新メソッド `_postwarm_ephemeral`（ログイン＋Deepgram ストリーミング使用時のみ、active=先読み取得／
  それ以外=暖機 GET）を `_finish_recording` の録音停止直後に別スレッドで起動。
  `streaming_transcriber.py`: 生成時刻からの `トークン取得 / WS接続 / 最初の文字まで` の経過を `logger.info` 出力。
- **Mac**（Swift）`AppController.swift`: `accountActive` を `startup()` で保持し、`taskFinished()` で
  上記と同条件の先読み／暖機を `Task` で起動。`StreamingTranscriber.swift`: トークン取得 ms と
  録音開始からの最初の文字 ms を `os.log` 出力。

## [1.6.3] - 2026-06-29

### Fixed
- **録音後の処理中に「サーバーに接続できませんでした」が出るバグを修正（両 OS・release）**。
  「正確性」（ElevenLabs）など**サーバー経由**の文字起こし／整形は、録音直前のトークン取得用に
  **短く設定したタイムアウト（15 秒）をそのまま流用**していた。長文や cold start を踏むと
  応答前にこの 15 秒で切れ、**音声入力が終わった後の処理段階で「通信に失敗しました
  （＝サーバーに接続できませんでした）」**になっていた（＝せっかくの入力が無駄になる）。
  さらにサーバー側の serverless 関数も実行上限が短いと長文処理の途中で**関数が強制終了**し、
  失敗記録すら残らないまま落ちていた（暖機ではこの上限超過は防げない）。
  - **クライアント（Mac/Windows）**: 文字起こし・整形リクエストだけ、**リクエスト単位で
    タイムアウトを延長**（ElevenLabs 文字起こし=90 秒・整形=60 秒）。録音直前のトークン取得は
    速さ優先で従来の 15 秒のまま（用途ごとにタイムアウトを分離）。
  - **サーバー**: ElevenLabs プロキシ関数と整形プロキシ関数の両方に **`maxDuration=60`** を明示し、
    長文処理の途中終了を防止（どちらも明示が無く既定の短い実行上限のままだった）。

### Technical Details
- **Mac**（Swift）`BackendClient.swift`: `transcribeElevenLabs` に `req.timeoutInterval = 90`、
  `formatText` に `req.timeoutInterval = 60` を設定（短い接続用セッション既定 15s をリクエスト単位で上書き）。
- **Windows**（Python）`backend_client.py`: `transcribe_elevenlabs` に
  `timeout=httpx.Timeout(90.0, connect=5.0)`、`format_text` に `timeout=httpx.Timeout(60.0, connect=5.0)` を
  個別指定（共有クライアント既定 15s を呼び出し単位で上書き）。
- **voicekey-site** `app/api/v1/transcribe/elevenlabs/route.ts` および `app/api/v1/format/route.ts`:
  `export const maxDuration = 60` を追加。
- **テスト**: `tests/test_backend_client.py` の ElevenLabs multipart 検証を実フィールド名
  `language_code` に追従（v1.6.2 のストリーミング透過で `language` → `language_code` に変わって以降、
  当時 unittest 未実行のため取り残されていた stale assert を修正）。

## [1.6.2] - 2026-06-29

### Fixed
- **「正確性」（ElevenLabs）の話し始め〜文字起こしの遅延を解消（両 OS・release）**。
  「正確性」モードは ElevenLabs のバッチ文字起こし（scribe_v1）を使うが、これは
  クライアント直叩き用の短命キーが無いため**サーバープロキシ経由**で中継している。
  この初回利用時に Vercel serverless 関数の **cold start（最大数秒）** を踏むのが
  遅延の主因だった。「高速リアルタイム」と同じ対策を「正確性」プロキシにも入れた:
  - サーバーに**消費なし・認証なしの `GET` 暖機ハンドラ**を追加（EL も DB も叩かず即 200・
    無料枠もコストも消費しない）。アプリが**起動時＋約 4 分間隔**でこの GET を叩いて
    プロキシ関数（lambda）を温存し、録音後の文字起こし POST を warm path に乗せる。
  - **中継の二度手間を削減**：新クライアントは ElevenLabs がそのまま受け取れる multipart
    （`file` + `model_id=scribe_v1` + `language_code`）を `x-vk-passthrough: 1` 付きで送り、
    サーバーは**ボディをバッファせず EL へストリーム透過**する（従来の「全量受信 →
    再構築 → 送信」をやめる）。旧クライアント（ヘッダ無し）は従来どおりサーバー側で
    組み直すため**後方互換**（精度・モデルは scribe_v1 のまま不変）。

### Technical Details
- **voicekey-site** `app/api/v1/transcribe/elevenlabs/route.ts`: 暖機用 `GET`（`{ warm: true }`）を
  追加。`POST` に `x-vk-passthrough` ヘッダ判定を追加し、ヘッダありかつ multipart なら
  `request.body` を `duplex:"half"` で EL へストリーム透過。ヘッダ無しは従来の
  `request.formData()` 再構築にフォールバック（後方互換）。
- **Mac**（Swift）: `BackendClient.warmElevenLabs()`（消費なし GET）／`transcribeElevenLabs` に
  `x-vk-passthrough: 1` ヘッダと EL 形式 multipart（`model_id`/`language_code`）を追加／
  `AppController` の起動時暖機・`startEphemeralWarmLoop`（240s）に ElevenLabs スロット判定を追加。
- **Windows**（Python）: `backend_client.warm_elevenlabs()`（消費なし GET）／
  `transcribe_elevenlabs` に `x-vk-passthrough` ヘッダと `model_id`/`language_code` を追加／
  `app.py` の `_prewarm_backend`・`_ephemeral_warm_loop` に ElevenLabs スロット判定を追加。

## [1.6.1] - 2026-06-29

### Fixed
- **無料体験中の「高速リアルタイム」の話し始めの遅延を解消（両 OS・release）**。
  無料体験ユーザーは録音直前に短命トークン発行 `POST /api/v1/auth/ephemeral` を毎回叩いて
  無料枠を 1 消費する（有料はトークンをキャッシュ再利用するため毎回は叩かない）。この POST が
  Vercel serverless 関数の **cold start（最大数秒）** を踏むと、そのまま「話し始めの待ち時間」
  になっていた＝リアルタイム文字起こしの遅延の主因。サーバーに**消費なし・認証なしの `GET`
  暖機ハンドラ**を追加し（DB も Deepgram も叩かず即 200・無料枠もコストも消費しない）、
  アプリが**起動時＋約 4 分間隔**でこの GET を叩いて発行関数（lambda）を温存するようにした。
  これで録音時の POST が warm path に乗り、遅延なくトークンを取得できる。ユーザーが選んだ
  「より低リスクな根本対応」（消費 API を分離せず＝突破耐性と「1 録音 = 1 消費」を維持したまま
  cold start だけを潰す）に沿った実装。
- **使うたびに設定画面の「残り回数」が即減って見えるよう修正（両 OS・release）**。
  消費自体は従来からサーバーが原子的に行っていたが、アプリ側の残量表示は起動／ログイン時に
  一度取得したきりで録音後に更新されず、「使っても残り回数が減らない」ように見えていた
  （表示だけが古い状態）。**録音 1 回ごとに利用権の残量を静かに取り直す**ようにした
  （UI を「確認中…」に落とさない quiet 更新・クリティカルパス外・有料/未ログインは no-op）。

### Technical Details
- **voicekey-site** `app/api/v1/auth/ephemeral/route.ts`: 暖機用 `GET`（消費なし・認証なし・
  外部 API 非依存で `{ warm: true }` を返すだけ）を追加。`POST`（消費あり・トークン発行）と
  同一ファイル＝同一関数なので、GET で温めると POST 経路ごと warm path に乗る。
- **Mac**（Swift）: `BackendClient.warmEphemeral()`（消費なし GET）／`AppController` に
  `startEphemeralWarmLoop()`（240s 間隔・ログイン中かつ Deepgram ストリーミング時のみ）と
  起動時暖機の無料/有料分岐／`taskFinished()` から `LoginCoordinator.refreshEntitlementQuiet()`。
  `LoginCoordinator` に `refreshEntitlementQuiet()` ＋ 共通 `applyStatus()` を追加。
- **Windows**（Python）: `backend_client.warm_ephemeral()`（消費なし GET）／`app.py` の
  `_prewarm_backend` を無料ユーザーも暖機するよう拡張・`_ephemeral_warm_loop`（240s 常駐）・
  `_refresh_entitlement_async`（録音完了ごと・`account_refreshed` シグナルで UI 更新）。
  `login_coordinator.refresh_entitlement(quiet=...)` ＋ 共通 `_apply_status()` を追加。

## [1.6.0] - 2026-06-29

### Added
- **CI で Python / Swift のユニットテストを自動実行（`.github/workflows/tests.yml`・両ブランチ）**。
  これまで CI は Windows 配布ビルド（`windows-build.yml`）と Release（`release.yml`）のみで、
  テストは手元実行頼みだった。`main` / `release` への push・PR・手動実行で Python（`unittest discover -s tests`・
  `QT_QPA_PLATFORM=offscreen`）と Swift（`swift test --package-path macos` ＋ `swift build`）を
  macOS ランナーで回す。テストはすべてモック済み（実モデル DL・実 API キー・ネットワーク・実 Keychain に
  触れない）ため、プロバイダーキー等の GitHub Secrets は CI に一切渡さない（漏洩面の縮小）。

### Changed
- **ドキュメントを現状の実装に合わせて修正（両ブランチ・実装乖離の解消）**。
  README / OVERVIEW.md / AGENTS.md / CLAUDE.md に残っていた旧 WhisperWin 時代の記述を一掃した。
  具体的には (1) 「SuperWhisper / faster-whisper / CUDA 必須 / NVIDIA GPU / VRAM」→ 文字起こしは
  クラウド API・VAD はローカル CPU（Silero ONNX + onnxruntime。GPU 不要。`torch`/`torchaudio` は
  `silero-vad` の依存として入るだけで実行時不使用）、(2) 「Dynamic Island 風オーバーレイ / `src/ui/overlay.py`」→
  コンパクトな録音 HUD（`src/ui/hud.py`）、(3) 実在しない `src/core/groq_transcriber.py` /
  `openai_transcriber.py` / `transcriber.py` → `api_transcriber.py`（REST）/ `streaming_transcriber.py`、
  (4) 「文字起こしは OpenAI/Groq だけ」→ 現在のプロバイダー（main は 4 種実名、release は高速リアルタイム=
  Deepgram / 正確性=ElevenLabs の 2 択＋整形に Groq）、(5) Windows 単一コードベース前提 → Mac(Swift)/
  Windows(Python) 二本立て、(6) 「自動テストは無い」→ Python(`unittest discover -s tests`・offscreen)・
  Mac(`swift test`)のテストが存在。CLAUDE.md の旧アーキ節は OVERVIEW.md に委譲する簡潔版へ差し替え。
  OVERVIEW.md / AGENTS.md / CLAUDE.md は両ブランチ同一、README はブランチ別（main=実プロバイダー名 /
  release=2 択名）に整合させた。
- **未使用 import / ローカル変数を整理（lint クリーンアップ・両ブランチ）**。
  `pyflakes` で検出した未使用を除去：`src/ui/settings_window.py`（`importlib.util` / `os` / `QSpinBox`、
  `_on_key_press` の未使用ローカル `modifiers`）、`src/ui/system_tray.py`（`QtCore.Qt`）、
  `tests/test_secrets_auth.py`（`json`）。意図的な参照は残す：`src/core/streaming_transcriber.py` の
  `websockets.sync.client.connect`（同期クライアント有無の存在確認プローブ・`# noqa` 済み）。

### Fixed
- **`onnxruntime` を明示依存に追加（VAD が無効化される潜在バグ・両ブランチ）**。
  `vad.py` は Silero VAD の ONNX を `onnxruntime` で実行するが、`onnxruntime` は `silero-vad` の
  optional extra（`onnx-cpu`）でしか入らず、`requirements.txt` に宣言が無かった。クリーンな
  `pip install -r requirements.txt`（CI・Windows 配布ビルドを含む）では `onnxruntime` が入らず、
  `vad.py` の遅延 `import onnxruntime` が失敗 → `analyze()` が安全側の `(True, None)` を返し、
  **無音圧縮・長文分割が一切効かない**状態になっていた（録音はそのまま全文送信される＝文字起こし自体は
  動くため気付きにくい）。`requirements.txt` に `onnxruntime>=1.16.1`（silero-vad の extra と同じ下限）を
  明示追加。これで CI の VAD テストが通り、次回以降の配布ビルドでは VAD が実際に機能する。
  **注**: 既存の配布済み Windows 版（CI ビルド）は `onnxruntime` 未同梱のため VAD が無効のまま＝
  この修正を反映するには Windows 版の再ビルド・再配布が必要（PyInstaller は `vad.py` の
  `import onnxruntime` を検出して contrib フックで同梱する想定だが、再ビルド時に同梱を実機確認すること）。
- **CI の Swift ジョブでクリーンチェックアウト時に `EmbeddedKeys.generated.swift` を生成（両ブランチ）**。
  同ファイルは `.gitignore` 済みの生成物で checkout に含まれず、`swift test`/`swift build` が
  「cannot find 'EmbeddedKeys' in scope」で失敗していた。`tests.yml` の Swift ジョブに
  引数なし（キーなし・`isDist=false`・Keychain/Secrets 不要）のスタブ生成
  （`bash macos/scripts/generate_embedded_keys.sh`）をビルド前に挟んで解消。
- **未ログイン時のゲート文言を無料体験仕様に修正（release・Mac/Windows）**。
  配布版で未ログインのまま文字起こしすると「利用するにはログインとアクティベーションキーが必要です」と
  表示していたが、実際はログインすればまず無料体験枠で使え、枠を使い切ってからアクティベーションキーが
  必要になる（v1.5.0〜）。未ログイン時は「ログインすると無料体験で使えます（設定 → アカウント）」に統一。
  無料枠終了（402）／利用権なし（403）はサーバーが返す既存メッセージ（「無料体験を使い切りました…」
  ／「利用するにはアクティベーションキーの登録が必要です…」）で区別済み。Mac（`Transcriber.swift`・
  `StreamingTranscriber.swift` のコメント／ログ）と Windows（`api_transcriber.py` の `_dist_guard`）の
  文言・docstring を揃えた。`main`（自分用）はこのゲート自体が存在しないため対象外。
- **製品版のバックエンド表示名をエラー・通知でも 2 択名に統一（release・Windows）**。
  設定ドロップダウン（Win）と Mac `AppConfig.backendDisplayName` は既に「高速リアルタイム／正確性」だったが、
  `api_transcriber.py` の `display_name`（API キー未設定・レート制限・HTTP エラー・タイムアウト等のメッセージや
  通知に露出）だけが ElevenLabs=「多言語」/ Deepgram=「リアルタイム」のままで、製品版の 2 択名と食い違っていた。
  ElevenLabs を「正確性」、Deepgram を「高速リアルタイム」に統一（`src/core/api_transcriber.py`）。
  `main`（自分用）は実プロバイダー名表示のため対象外（既に OpenAI/Groq/ElevenLabs/Deepgram で一致）。
- **アプリのバージョン定義を `constants.APP_VERSION` の単一ソースへ統一（両ブランチ）**。
  `src/__init__.py` の `__version__` が `"2.0.0"` とハードコードされ、実際の配布版（`constants.APP_VERSION`・
  Info.plist・README）と食い違っていた。`from .config.constants import APP_VERSION as __version__` に変更し、
  Windows ビルドスクリプト／`updater.py` が既に参照している `APP_VERSION` を唯一の真実とした。
  整合回帰テストを追加（`tests/test_version_consistency.py`: `__init__` がハードコードせず単一ソースを参照する／
  macOS Info.plist の表示バージョン／README 配布ステータス表の macOS・Windows 行が `APP_VERSION` と一致する。
  値固定でなく相互一致を検証するのでブランチ非依存）。
- **VAD テストがクリーンな clone / git archive でも通るよう、音声 fixture をリポジトリ同梱に変更（両ブランチ）**。
  `tests/test_vad.py` が `.gitignore` 除外（`benchmark/audio/*.wav`）の `benchmark/audio/short_ja.wav` に
  依存しており、開発者ローカルにしか無いためクリーンな checkout では VAD テスト 13 件が setup error に
  なっていた。同じ音声（`say` 合成の日本語・秘密情報なし）を `tests/fixtures/short_ja.wav` として
  コミットし、テストの参照先をそちらへ変更。クリーンな `git archive` 展開でも 20 件すべて通ることを確認した。
- **VAD の孤立ノイズ判定を「総数」でなく「連続 run の長さ」に修正（両 OS・両ブランチ）**。
  Python `_speech_regions` はしきい値超えフレームの総数が 2 以上なら発話としていたため、離れた
  単発クリックノイズ 2 個を 2 発話として採用していた。Swift `speechRegions` も run 長 1 の単発
  フレームから区間を作っていた。各連続 run の長さが `_MIN_SPEECH_FRAMES`（=2 フレーム・≒64ms）
  以上の run だけを発話とみなすようにし、両 OS の閾値・挙動を揃えた（`src/core/vad.py`・
  `macos/.../Core/VoiceActivity.swift`）。テスト追加（離れた単発 2 個=ノイズ／連続 2 フレーム=発話／
  単発＋run 混在=run だけ採用）。
- **短い後続 VAD セグメントの結合判定を「現在セグメント長」も見るよう修正（両 OS・両ブランチ）**。
  分割並列送信のセグメント結合が「直前セグメントが短いとき」だけ結合していたため、長区間の後ろに
  続く `_MIN_SEGMENT_SEC`（2 秒）未満の短区間が独立したまま残り、無駄な細切れ API 送信になっていた。
  現在セグメント長と直前セグメント長の両方で判定し、先頭・中間・末尾いずれの短区間も直前へ結合する
  ようにした。Swift は結合ロジックを純粋関数 `mergeShortSegments` に抽出して決定的にテスト可能にした
  （`src/core/vad.py`・`macos/.../Core/VoiceActivity.swift`）。テスト追加（先頭/中間/末尾短区間/全長区間）。
- **CJK 隣接スペースの除去を全バックエンド・streaming/REST・Mac/Windows で統一（両ブランチ）**。
  これまで CJK 単語間スペースの除去は一部経路のみ（Mac は Deepgram ストリーミングだけ、Windows は
  Deepgram REST だけ）で、ElevenLabs・OpenAI・Groq の REST 経路やサーバープロキシ経由では
  「今日 は 晴れ」のようにスペースが残っていた。Mac は共通正規化 `TextNormalize.stripCJKSpaces` を
  新設して streaming/REST（直叩き・ElevenLabs プロキシ・Deepgram 短命 JWT）全 return で適用、
  Windows は既存 `text_utils.strip_cjk_spaces` を基底 `ApiTranscriber.transcribe`（OpenAI/Groq）と
  `ElevenLabsTranscriber.transcribe`（プロキシ/直叩き両方）にも適用した。判定範囲（ひらがな・
  カタカナ・CJK 拡張A・統合漢字・互換漢字・全角半角形）は両 OS で一致。冪等なので Deepgram の
  二重適用も安全（`macos/.../Core/TextNormalize.swift`・`macos/.../Core/Transcriber.swift`・
  `macos/.../Core/StreamingTranscriber.swift`・`src/core/api_transcriber.py`）。テスト追加
  （`macos/Tests/VoicekeyTests/TextNormalizeTests.swift`・`tests/test_text_utils.py` に中国語/全角/
  冪等の境界ケース：日本語・中国語・英数字の境界を網羅）。
- **録音中のマイク切断・構成変更で復帰できないとき、それまでの録音音声を捨てないようにした（Mac・両ブランチ）**。
  デバイス切断で `handleConfigurationChange` が復帰失敗すると `recording=false` にし、その後の
  `stop()` がガードで空配列を返していたため、切断直前までに録音できていた音声が失われていた。
  録音 buffer の「未取得」状態を `BufferAvailability` で管理し、録音確定済み（`recording=false`）でも
  未取得 buffer があれば一度だけ取り出して文字起こしへ回すようにした（二重取得は防止）。切断時も
  喋った分が無駄にならない（`macos/.../AudioRecorder.swift`）。回帰テスト追加
  （`macos/Tests/VoicekeyTests/BufferAvailabilityTests.swift`: 通常停止は一度だけ/切断後も一度だけ
  取り出す/対象なしは空/セッションをまたいで再利用）。
- **長押し録音の直後に始めた録音へ誤って自動 Enter が付く不具合を修正（Windows・両ブランチ）**。
  ダブルタップ（auto_enter）判定用のリリース情報（時刻・スロット）を、長押しの離鍵でも
  記録していたため、長い口述の直後に素早く次の録音を始めると `_DOUBLE_TAP_SEC`（0.4 秒）内なら
  ダブルタップと誤認し Enter が自動送信されていた。リリース情報を「短いタップとして成立した
  ときだけ」記録するようにし、Mac 版（既に短タップのみ記録）と挙動を揃えた（`src/app._on_release`）。
  回帰テスト追加（`tests/test_double_tap_release.py`: 短タップは記録/長押しは非記録/長押し直後の
  再押下は非 auto_enter/短タップ直後の再押下は auto_enter 維持）。
- **ログ出力先を作業ディレクトリ依存にせず OS 標準ログディレクトリへ固定（Windows・両ブランチ）**。
  起動時ログ（`startup_log.txt`）を相対パスで開いていたため、ログイン時自動起動やショートカット
  起動では cwd が `C:\Windows\System32` 等になり、ログが書けない／想定外の場所に散らばっていた。
  相対名は OS 標準のユーザー書き込み可能ディレクトリ（Windows=`%LOCALAPPDATA%\voicekey\logs`、
  macOS=`~/Library/Logs/voicekey`、その他=`$XDG_STATE_HOME/voicekey/logs`）配下へ解決するようにし、
  絶対パスはそのまま使用。ディレクトリ/ファイル作成に失敗してもアプリは止めずコンソール出力で継続。
  Mac ネイティブ版は Apple 統合ログ（`os.Logger`）を使うため対象外。回帰テスト追加
  （`tests/test_logger.py`: 相対名は log dir 配下/絶対パスはそのまま/作成失敗時はコンソール継続/
  None で無効化/プラットフォーム別 dir）。
- **Keychain 書き込み（delete→add）の add 失敗で旧資格情報を失わないようにした（製品版・Mac）**。
  `Keychain.write` は所有権確保のため delete→add 方式を採る（`SecItemUpdate` は他アプリ/旧署名所有の
  項目が ACL ごと残り承認ダイアログが再発するため・2026-06-12 実測）。ただし delete 後に add が失敗すると
  旧値まで消えるため、書き込み前に旧値を控え、add 失敗時は旧値の復元を試みるようにした（所有権と
  資格情報の保全を両立）。SecItem 操作を `Keychain.Ops` として注入可能にし、実 Keychain に触れない
  単体テストを追加（`macos/Tests/VoicekeyTests/KeychainWriteTests.swift`: 成功時の格納/失敗時の旧値復元/
  新規保存失敗時は復元しない）。あわせて Swift テストターゲット（`voicekeyTests`）を新設。
  注: レビュー提案の `SecItemUpdate` は上記の承認ダイアログ回帰を招くため採用せず、目的（資格情報の
  消失防止）のみを満たす実装にした。
- **フィードバック送信中のダイアログ破棄でワーカー QThread を巻き込むクラッシュを防止（製品版・Windows）**。
  送信ワーカー（QThread）をダイアログに parent していたうえ、送信中もキャンセル可能だったため、
  送信中に閉じる/キャンセルすると `dialog.exec()` が戻ってダイアログが破棄され、実行中の QThread が
  「running 中に破棄」されてクラッシュし得た。送信中はキャンセルを無効化し `reject()` も無視、
  ワーカーはダイアログに parent せず寿命を分離（`finished`→`deleteLater`＋参照解除）、アプリ終了など
  強制的に閉じる経路では `closeEvent` でワーカー完了を待ってから破棄するようにした
  （`src/ui/feedback_dialog.py`）。回帰テスト追加（`tests/test_feedback_dialog.py`: 成功/失敗/
  送信中の reject 無視/強制 close 時の wait）。
- **設定画面のキャンセルを本当に「破棄」にし、自動起動の反映を保存成功と同期（Windows・両ブランチ）**。
  設定ウィンドウは破棄されず使い回されるため、(a) 開き直しても永続 config を読み直さず前回の
  未保存編集・テーマプレビューが残って後で保存され得た、(b) 自動起動（レジストリ）を設定保存の
  *前* に反映していたため保存失敗時に「自動起動だけ変わって設定は元のまま」という不整合が起きた。
  `showEvent` で毎回 `_load_current_settings()` を呼んで永続 config から UI を再ロード（テーマ
  プレビューも保存値へ戻す）し、キャンセル＝次に開いた時点で確実に破棄されるようにした。自動起動の
  反映は `ConfigManager.save` 成功後の同じ境界へ移動し、反映失敗時はユーザーに通知（保存失敗時は
  自動起動に一切触れない）。回帰テスト追加（`tests/test_settings_window.py`: 再表示での値・テーマ
  復元、保存失敗時に自動起動へ触れない、保存成功時のみ反映。オフスクリーン Qt＋keyring/デバイス/
  レジストリはモック）。
- **ログイン時起動の方式を一元化し、二重起動・OFF 無視を解消（Windows・両ブランチ）**。
  インストーラー（Inno Setup）が Startup フォルダにショートカットを常設する一方、設定 UI は
  レジストリ（`HKCU\...\Run`）を操作していたため、UI で OFF にしても必ず起動し、ON にすると
  ショートカットと Run キーで二重起動し得た。インストーラーから Startup ショートカット作成を撤去し、
  ログイン時起動を設定 UI（Run キー）に一元化。アップグレード時は `[InstallDelete]` で旧版が作った
  ショートカットを除去する（`installer/windows/voicekey.iss`）。新規インストール直後はログイン時
  起動 OFF（設定 UI で任意に有効化）。検証マトリクス（新規/ON/OFF/アップグレード/再インストール）を
  同ファイルにコメントで明文化。
- **貼り付け後のクリップボード復元がユーザーの内容を壊さないように修正（両OS・両ブランチ）**。
  文字起こし結果はクリップボード経由で貼り付け、0.3 秒後にユーザーの元のクリップボードへ復元する。
  - #13（Windows）: 復元は無条件に元の内容を書き戻していたため、貼り付け〜復元の 0.3 秒の間に
    ユーザーが別の内容をコピーすると、そのコピーが古い内容で上書きされて失われていた。復元前に
    クリップボードが「自分が挿入したテキストのままか」を確認し、ユーザーが新しくコピーしていたら
    復元しないようにした（Mac は既に `changeCount` で同等の保護があったため Windows を同水準に揃えた）。
  - #24（両OS）: 連続して貼り付けると、2 回目の退避でクリップボード上の「自分の 1 回目の挿入
    テキスト」を原本と誤認し、最後の復元でユーザーの真のオリジナルではなく自分の挿入テキストを
    書き戻していた。貼り付けごとに世代番号を採番し、(a) 古い世代の復元を無効化、(b) 連続貼り付け
    時はユーザーの真のオリジナルを世代をまたいで引き継ぐ方式に変更（Python `InputHandler`、
    Mac `Paster`。Mac の `paste` は復元状態を直列化するため MainActor 隔離に変更）。
  - 回帰テスト追加（`tests/test_input_handler.py::TestClipboardRestoreGuard`: ユーザー上書き回避・
    連続貼り付けの原本復元・空クリップボードの非破壊）。
- **settings.yaml の保存をアトミックにし、書き込み中断での設定全消失を防止（Windows・両ブランチ）**。
  保存は `open("w")` でファイルを truncate してから書いていたため、書き込み中にクラッシュや
  ディスク不足が起きると settings.yaml が空・破損で残り、ユーザーの全設定が失われ得た。
  同一ディレクトリの一時ファイルへ全量書き込み＋`fsync` してから `os.replace` で原子的に置換する
  方式に変更（`ConfigManager.save`）。`os.replace` は Windows でも既存ファイルを上書きするため
  両 OS で安全。回帰テスト追加（`tests/test_config_manager.py::TestAtomicSave`）。
- **使用実績のアカウント同期を順序逆転・過少計上・アカウント漏洩から守る（製品版のみ・両OS＋サーバー）**。
  ログイン中の実績は端末ごとの「当日の絶対値」をサーバーへ送り、取得時に端末横断で合算して表示する。
  この経路に 3 つの不具合があった。
  - 順序逆転: 録音のたびに新スレッドで送信していたため複数送信が並走し、到着順が前後すると
    古い（小さい）絶対値が新しい値を上書きして過少計上し得た。アプリ側は送信を単一フライトで
    直列化＋coalesce し（走行中の再要求は最新値 1 回に畳む。Python `StatsStore._sync_worker`、
    Mac `StatsStore.syncWorker`）、サーバー側は単調増加 upsert（`apply_usage_stats` RPC が
    `GREATEST` を取る）にして到着順に依存しないようにした。
  - 過少計上: 表示が「端末横断合算 と ローカル の field-wise max」だったため、別端末の寄与がある日に
    この端末の未送信分が埋もれて取りこぼしていた。サーバー応答にこの端末ぶんの日次
    （`self_daily`＝server baseline）を追加し、表示を `合算 + max(0, ローカル − baseline)` に変更。
    端末横断を保ったまま未同期分だけを足す＝二重計上も過少計上もしない（再インストールで
    ローカル 0 でも合算を下回らない）。`self_daily` を返さない旧サーバーは従来の max 合成へ安全に
    フォールバック（Python `stats._effective_daily_locked`／`snapshot`、Mac `StatsStore.effectiveDaily`／
    累計プロパティ）。
  - アカウント漏洩: ログアウト／アカウント切替が実績取得の往復に割り込むと、後から完了した取得が
    前アカウントの実績を復活表示し得た。取得開始時の認証世代（#9 の generation）を控え、保存直前に
    世代が変わっていたら破棄する（Python `StatsStore.refresh_account`、Mac `StatsStore.refreshAccount`）。
  - サーバー（voicekey-site）: `GET /api/v1/stats` が `self_daily` を返す／`POST /api/v1/stats/sync` が
    `apply_usage_stats` RPC（単調増加 upsert）を使う／マイグレーション `0016_usage_stats_monotonic.sql`。
    無料枠の課金判定はサーバーの本物の消費（`consume_free_quota`）で行い、本表示用集計は使わない。
  - 回帰テスト追加（Python `tests/test_stats.py`）: 未同期分を足す／同期済みは二重計上しない／
    再インストールの下限維持／旧サーバーの max フォールバック／取得中のアカウント切替で破棄／
    5 連打が送信 2 回に畳まれる。
- **無料枠の「1録音＝1消費」を保証（短命トークンのキャッシュをサーバーの cacheable に従わせる・製品版のみ・両OS＋サーバー）**。
  `fetch_ephemeral_token` は短命 JWT を約 60 秒キャッシュし録音をまたいで再利用していたが、
  サーバーの無料枠消費は token 発行時に 1 回のため、同じ JWT を複数録音に使うと無料体験の
  消費が 1 回しか進まなかった（＝1録音=1消費が崩れる）。
  - サーバー（voicekey-site `/api/v1/auth/ephemeral`）が応答に `cacheable` を追加。利用権あり
    （paid＝発行で消費しない）は `true`、無料体験（free＝発行ごとに 1 消費）は `false`。
  - アプリは `cacheable === true` のときだけ録音間でキャッシュ再利用する。無料体験・`cacheable`
    不明（旧サーバー）はキャッシュせず録音ごとに取り直す＝正確性優先（Python `fetch_ephemeral_token`、
    Mac `BackendClient`／`EphemeralToken.cacheable`）。クライアント側の推測でなくサーバー応答に従う。
  - 起動時暖機は無料枠を消費しない（`/me` で接続を温め、短命トークンの先取りは有料ユーザー
    のみ＝消費ゼロ）。`a824811` を維持。
  - 回帰テスト追加（Python `tests/test_backend_client.py::TestEphemeralCacheConsume`）: 無料体験は
    60 秒以内の 2 録音で発行が 2 回（＝2 消費）・利用権ありは 2 録音目がキャッシュ再利用で発行 1 回・
    `cacheable` 欠落の旧サーバーは安全側（毎録音取得）。
- **ログアウト後のセッション復活を防止（認証世代の導入・in-flight 取得の無効化・製品版のみ・両OS）**。
  ログアウトやアカウント切替が、進行中のトークンリフレッシュ／短命トークン取得の往復に割り込んだとき、
  後から完了したそれらの処理が破棄済みのセッションや旧アカウントのトークンを保存・採用し直し得た
  （＝ログアウトしたのに復活する／別アカウントのトークンが残る）。
  - 「認証世代（generation）」を導入。リフレッシュ／短命トークン取得は開始時の世代を控え、
    保存・キャッシュの直前に世代を再確認する。ログアウト／別アカウントのログインで世代を +1 し、
    世代が変わっていたら結果を保存・採用しない（Python `auth_client._save_session_if_current` ＋
    `backend_client.fetch_ephemeral_token` の世代チェック、Mac `AuthClient.saveIfCurrent` ＋
    `BackendClient` 取得 Task の世代チェック）。世代の +1 とセッション保存／破棄は専用ロックで
    不可分化し、リフレッシュのネットワーク I/O はロック外で行うのでログアウトは往復完了を待たない。
  - ログアウト時に進行中のリフレッシュ／短命トークン取得を cancel する（Mac `AuthClient.bumpGeneration`
    が `inFlightRefresh` を、`BackendClient.clearTokenCache` が `inFlightToken` を cancel）。
  - 認証情報の保存失敗を握りつぶさない。ログイン（コード交換）で Keychain/keyring 保存に失敗したら
    例外にして「ログイン済み」を見せない（Python は `BackendError`、Mac は `AuthError.saveFailed`）。
  - device_id の初回生成を直列化（Python `secrets._device_id_lock` / Mac `Keychain.deviceIdLock`）。
    同時呼び出しで別々の ID を生成してサーバーの同時利用台数上限に誤って当たるのを防ぐ。
  - 回帰テスト追加（Python `tests/test_auth_generation.py`）: ログアウトがリフレッシュ往復に割り込んでも
    復活しない・別アカウントのログインが短命トークン取得に割り込んでも旧トークンをキャッシュしない・
    device_id の同時生成が 1 本に直列化される。
- **処理キューへ可変設定を持ち込まない（録音開始時に処理コンテキストを不変スナップショット化・両ブランチ・両OS）**。
  録音終了後にキューへ積んだタスクが、処理開始時点の「現在の」slot / model / transcriber を
  参照していた。設定の hot-reload やスロット変更が録音終了〜処理開始の間に挟まると、録音開始時とは
  別プロバイダーへ音声を送り得た（Python はワーカースレッドが `self._slots` を引き直していた）。
  - Python: 録音開始時に slot / language / VAD / 分割 / 正規化 / 整形設定を不変の `TaskContext` へ
    snapshot し、`TranscriptionTask.context` で持ち回る。`_process_task` / `_maybe_format` は
    ライブ設定（`self._slots` / `self._config`）を一切読まず snapshot だけを使う。
  - Mac: 録音開始時に transcriber 参照（＝プロバイダー）と処理フラグを `RecordContext` へ snapshot し、
    `processAudio` へ引き継ぐ。backend 変更時は `rebuildTranscribers` が新インスタンスを作るため、
    参照固定で録音開始時のプロバイダーが確実に保持される（捕捉点を録音終了→録音開始へ前倒し）。
  - 回帰テスト追加（Python `tests/test_task_context_snapshot.py`）: 録音終了と処理開始の間にスロットを
    別プロバイダーへ差し替えても、音声が録音開始時のプロバイダーへ届き差し替え後へは送られない・
    スロットが消えても snapshot で完遂する。
- **連続録音間の音声混入を防止（chunk_callback / chunkHandler を録音世代に束縛・両ブランチ・両OS）**。
  前回録音の stop が完了する前に次の録音を開始すると、共有の送出フック（Python
  `chunk_callback` / Mac `chunkHandler`）が次録音の streamer へ差し替わり、前回録音末尾の
  チャンクが次録音のストリームへ送られ得た（別バックエンドの録音に前回 Deepgram の音声が混入）。
  既存の `_session_id` ガードはストリーム世代（recover 時のみ進む）の判定で、同一ストリーム内の
  録音と録音の区別はできていなかった。
  - 送出フックを録音世代に束縛（`(gen, callback)` を原子的に差し替え）。物理録音が開始
    （`_do_start` / `start`）した時点の世代を「受理世代」として確定し、audio callback は
    世代不一致のチャンクを送出しない。受理世代は録音開始でのみ進むため、旧録音の stop
    ドレイン中（差し替えは起こり得る）に次録音の streamer へ旧音声が渡らない。
  - start/stop は元々 AudioControl スレッド（Python）/ engine queue（Mac）で直列化されており、
    物理 start は前回 stop 完了後にしか走らない。残る非直列点（listener/main スレッドでの
    フック差し替え）を世代束縛で塞いだ（要件「stop 完了まで次 start を待機」は直列化で既達）。
  - 回帰テスト追加（Python `tests/test_audio_recorder.py::TestCrossRecordingChunkBinding`）:
    旧録音末尾が次 streamer へ届かない・解除後は誰にも届かない。
- **音声コールバック内の同期 WebSocket 送信を撤去（ロック中 I/O 禁止・固定上限キュー＋専用 sender スレッド・両ブランチ）**。
  これまで `StreamingTranscriber.send()` は audio コールバックスレッドから `self._lock` を
  保持したまま同期 `ws.send(pcm)` を呼んでいた。回線が遅い/詰まると送信がブロックし、
  PortAudio の音声コールバックがその間ずっと塞がれて録音が途切れ得た（さらに `_lock` 保持中の
  I/O は他スレッドの finish/cancel も巻き込む）。
  - `send()` は `_float32_to_pcm16le` の後、固定上限キュー（`_send_q`, maxsize=512）へ
    `put_nowait` するだけのノンブロッキング処理にした（ロックもネットワーク I/O も持たない）。
  - 実送信は専用 sender スレッド（`_sender_loop`）が担い、`_lock` を持たずに `ws.send` する。
    接続前に積まれた PCM も sender が FIFO で送るので順序は保たれる（旧 `_pending` 退避を置換）。
  - キュー溢れ・送信失敗は音声を黙って捨てず streaming 失敗として記録（`_mark_failed`）し、
    `finish()` が空文字を返して**保持済み全音声**（`task.audio_data`）での REST フォールバックへ移す。
  - `finish()` の CloseStream も送信キュー経由（退避済み音声の後に送出）にして順序を保証。
  - 回帰テスト追加（`tests/test_streaming_transcriber.py`）: 遅い WebSocket でも `send()` が即 return、
    キュー溢れ→失敗確定→`finish()` 空文字、送信失敗→失敗確定、CloseStream マーカーで sender 終了。
- **録音開始失敗時に古い Deepgram ストリーミング接続を確実に破棄（両ブランチ）**。
  `_on_record_started(False)` が録音スロットだけを消し、`_active_streamer` と
  `chunk_callback`（= 古い `streamer.send`）を残していた。その結果、次回に別バックエンドで
  録音しても音声が前回の Deepgram WebSocket へ送られ得た（別バックエンドの録音に Deepgram の
  結果が混ざる）。開始失敗時に streamer を `cancel()`（受信ループ停止・WebSocket close・短命
  トークン破棄）し、`chunk_callback` を解除、状態をクリアする。ハング復帰（`_monitor_loop`）や
  Mac 版 `AppController` の既存後始末と同じ不変条件に揃えた。回帰テスト追加
  （`tests/test_record_start_failure.py`：次回録音の音声が前回接続へ 1 チャンクも送られない）。
- **録音停止のハング復帰（recover）でコールバックが二重発火する不具合を修正（両ブランチ・両OS相当ロジック）**。
  `stream.stop()` がハングして watchdog の `recover()` が完了コールバックを代行発火した後、
  古い停止スレッドが復帰すると同じ録音で完了コールバックがもう一度呼ばれ（再現で callback_count==2）、
  さらに古いスレッドが新世代のストリーム・録音状態・音声バッファを破壊し得た。
  - 録音世代ごとに音声バッファ（キュー）を分離し、`recover()` は新世代へ別キューを割り当てる。
  - 完了コールバックを `_StopCompletion`（ワンショット）に閉じ込め、通常停止と recover 代行が
    競合しても各録音で **exactly once** だけドレイン＆発火する。
  - 古い世代の停止スレッドは、世代が進んでいたら新世代の `_recording` / `_stream` に触れない。
  - App 側の未完了タスク数（`_outstanding`）はデクリメントを 0 で下限クランプし、万一の二重発火でも
    負数化（HUD が「変換中」に張り付く）を防ぐ。
  - 回帰テスト追加: ハング→recover→新規録音→旧stop復帰でコールバック 1 回・新世代バッファ不可侵
    （`tests/test_audio_recorder.py`）、未完了数の非負（`tests/test_outstanding_count.py`）。

### Security
- **Windows の自動更新を固定公開鍵の Ed25519 署名で検証（SHA256 だけを信頼しない・release）**。
  これまで Windows 版は version.json の SHA256 だけでインストーラを検証していたが、SHA256 は
  フィード（version.json / exe のホスティング）を改竄・MITM されると攻撃者が exe と SHA256 を
  整合させられるため改竄に無力だった。配布物を開発者だけが持つ Ed25519 秘密鍵で署名し、
  アプリに埋め込んだ固定公開鍵で検証するようにした（Mac 版 Sparkle EdDSA と同方針）。
  秘密鍵を持たない第三者は正規署名を作れないため、不正な更新を実行できない。
  - 検証ユーティリティ `src/utils/update_signing.py`（sign / verify / 公開鍵導出）。検証は
    どんな失敗でも例外を投げず False を返し「信頼しない」に倒す。
  - `src/utils/updater.py` の `_install` を署名検証 → SHA256 の二段検証に変更。公開鍵が
    設定されていれば署名必須（無ければ更新拒否）。未設定ビルド（移行期の保険）は警告のみで
    SHA256 のみにフォールバック。
  - 公開鍵を `src/config/constants.py` の `UPDATE_PUBLIC_KEY_ED25519` に埋め込み。
  - 鍵運用は Sparkle に倣う: 秘密鍵は `~/.voicekey/voicekey_update_ed25519` に置き git に
    入れない。署名は CI ではなく秘密鍵が手元にある Mac でリリース relay 時に行う
    （`scripts/build/generate_update_key.py` で鍵生成・`scripts/build/sign_update.py` で署名）。
  - 依存に `cryptography>=42.0` を追加し、`voicekey.spec` で配布版へ同梱。
- **配布バイナリへの長期プロバイダーキー埋め込みを撤去（release・両OS）**。
  これまで配布版は開発者の OpenAI / Groq / ElevenLabs / Deepgram のキーを XOR 難読化して
  バイナリに焼き込んでいたが、XOR は暗号化ではなく（マスクも同じバイナリに同梱され復元可能）
  漏洩源だった。製品版は文字起こし・整形をすべて自社サーバー経由（Deepgram=短命 JWT 直叩き /
  ElevenLabs・Groq=サーバープロキシ）で行うため、アプリ側にプロバイダーキーは不要。
  配布物に長期キーを **1 バイトも埋め込まない**ようにした。
  - 生成スクリプトをキーレス化: `scripts/build/generate_embedded_keys.py` と
    `macos/scripts/generate_embedded_keys.sh` は IS_DIST フラグだけの「DIST マーカー」を出力し、
    .env.dist / Keychain / 環境変数からキーを読まない（Mac の `--export-env` も撤去）。
  - キー解決の埋め込みフォールバックを撤去: `src/utils/secrets.get_api_key`（Python）と
    Mac `Keychain.apiKey`（Swift）はキーを keyring/Keychain からのみ取得する。
  - テキスト整形の直 Groq フォールバックに DIST ガードを追加（`text_formatter` / `TextFormatter.swift`）。
    配布ビルドは未ログイン時も直プロバイダーを叩かず、整形せず原文を返す（整形はサーバープロキシ専用）。
  - ビルドパイプラインがプロバイダーキーを扱わない: `.github/workflows/windows-build.yml` から
    API キーの Secrets 受け渡しを削除。ビルド時に `scripts/build/verify_no_embedded_keys.py` で
    生成物にキーの痕跡が無いことを検査してから先へ進む（漏洩回帰の防止・Win/Mac 両パイプライン）。
  - **要対応（コード変更とは別）**: v1.2.0 以前の配布物に焼き込まれた旧キーは**漏洩済み**として
    扱い、各プロバイダー側で**ローテーション（無効化・再発行）が必要**。キー値そのものはここでは扱わない。
- **配布版で接続先・認証情報の汚染を防止（`.env` / `VOICEKEY_SERVER_URL` の無効化・両OS）**。
  配布（DIST）ビルドでは `.env` を読み込まず、環境変数 `VOICEKEY_SERVER_URL` による
  サーバー接続先の上書きも無視するようにした。これまでは作業ディレクトリの `.env` や
  環境変数を汚染されると、Bearer トークン・音声・フィードバックを任意のサーバーへ
  向けられる余地があった。override は開発ビルドの preview 検証用に限定する。
  - 実装: Windows=`src/main.py`（`load_dotenv` を dist でスキップ）/ `src/utils/secrets.py`
    （`get_server_base_url` の override を dist で破棄）、Mac=`ServerConfig.resolveBaseURL`
    （dist では override 分岐に入らず本番固定・テスト可能な純関数へ分離）。
  - テスト追加（dist で override が無視される回帰／`tests/test_secrets_auth.py`）。

## [1.5.1] - 2026-06-28

### Changed
- **製品版の初回録音を高速化（起動時にサーバー接続を暖機／両OS・release）**。
  ログイン後の最初の文字起こしは、サーバー（`voicekey.vercel.app`）への往復に加えて serverless 関数の
  cold start（最大数秒）を初回に払っていた。**アプリ起動時にサーバー接続を 1 回だけ暖機**して
  TLS・接続・認証経路を温めることで、初回録音のサーバー往復を体感から消した。背景での定期取得
  （API の無駄打ち）は行わず、起動時 1 回限り。
  - **無料枠を消費しない設計**: 暖機は `GET /api/v1/me`（無料体験枠を消費しない read）で行う。
    短命トークンの先取り（`/api/v1/auth/ephemeral`）は**有料ユーザーのときだけ**実行する
    （有料は consume 前に return＝消費ゼロ。トークンキャッシュも温まる）。**無料体験ユーザーは
    トークンを取得しない**ため、起動のたびに無料枠を 1 消費してしまう事故が起きない。
  - 記事 https://zenn.dev/catnose99/articles/nani-translate の「起動時プリフライトで cold start を消す」を応用。
  - **実装**: Mac=`AppController.startup()`、Windows=`app.py VoicekeyApp._prewarm_backend`。
    ガード＝ログイン済み（製品版）。トークン先取りの追加条件＝`active`（有料）かつ既定が
    Deepgram ストリーミング。既存の `fetchAccountStatus()`／`fetchEphemeralToken()`
    （60 秒 TTL キャッシュ＋同時取得集約）をそのまま再利用。
  - `main`（自分用）は各プロバイダを直叩きしサーバー往復が無く既に最速のため変更なし。
- **テキスト整形の往復を短縮（暖機先を実際の経路に修正／両OS・release）**。
  録音開始時の `prewarm()` は整形用 TLS を温めていたが、**温める先が `api.groq.com`（直叩き）固定**で、
  製品版（ログイン済み）の整形が通る **サーバープロキシ `/api/v1/format` を温めていなかった**。ログイン時は
  プロキシ側を温めるよう修正し、録音後の整形の往復（特に最初の cold start）を短縮した。
  - 暖機は空テキストの POST で行う（サーバーは空を **400 で短絡**＝Groq を呼ばず lambda・TLS・認証だけ
    温まる。`/format` は `consume:false` なので**無料枠も消費しない**）。
  - **実装**: Mac=`BackendClient.warmFormatProxy()`＋`TextFormatter.prewarm()`、
    Windows=`backend_client.warm_format_proxy()`＋`text_formatter.prewarm()`。未ログイン（`main`/自分用）は
    従来どおり直 Groq を温める。テスト追加（暖機の分岐・空 POST・未ログイン no-op）。
  - 注: 整形の主たる所要時間は Groq の推論（≒355ms・US）で、これは暖機では縮まない。本変更が消すのは
    接続確立・サーバー関数の cold start 分。

### Technical Details
- アプリのバージョンを **1.5.1** に更新（Win=`config/constants.py`、Mac=`Resources/Info.plist`：
  `CFBundleShortVersionString=1.5.1` / `CFBundleVersion=13`）。性能改善（バグ修正レベル）のため PATCH を更新。

## [1.5.0] - 2026-06-27

### Added
- **アカウントごとに無料体験枠を付与（使い切るとアクティベーションキーが必要／両OS・release ＋ サーバー）**。
  これまでは「ログイン＋有効なアクティベーションキー」が無いと一切文字起こしできなかったが、
  **ログインすれば各アカウントにつき無料で 200 回まで文字起こしを試せる**ようにした。無料体験を使い切ると、
  従来どおりアクティベーションキーの登録が必要になる（課金は未実装のため、当面はキー登録のみが解放手段）。
  - **数え方**: 文字起こし 1 回（＝短命トークン発行 or ElevenLabs プロキシ呼び出し 1 回）につき 1 消費。
    **累計で一度きり**（月次リセットなし）。テキスト整形（後処理）は消費しない。
    サーバーだけが見える本物の呼び出し回数で数えるため、クライアント申告の使用量では突破できない。
  - **整合性**: サーバー側で原子的にカウント（`consume_free_quota` RPC＝`free_used < free_quota` のときだけ
    +1 する条件付き UPDATE）。残枠ゼロの呼び出しは **402** を返し、アプリは「無料体験を使い切りました。
    続けて使うにはアクティベーションキーの登録が必要です（設定 → アカウント）」と表示する。
  - **UI（設定 → アカウント）**: 無料体験中は「無料体験中（残り N / 200 回）」、使い切ると
    「無料体験を使い切りました。…キーを入力してください」を表示。無料体験中でも先にキーを登録できる。
  - **サーバー**: `entitlements` に `free_quota`（既定 200）・`free_used` を追加、`consume_free_quota(uuid)` RPC、
    `authorizeUsable(request,{consume})` を追加。`GET /api/v1/me` は `free_quota / free_used / free_remaining` を返す。
    短命トークン発行（Deepgram）と ElevenLabs プロキシは消費あり、テキスト整形（Groq）は消費なし。
  - **実装**: サーバー=`voicekey-site`（`lib/apiAuth.ts`・`app/api/v1/{auth/ephemeral,transcribe/elevenlabs,format,me}`・
    DB マイグレーション `free_quota_count_based`）、Mac=`Core/BackendClient.swift`・`Core/LoginCoordinator.swift`・
    `UI/SettingsView.swift`、Windows=`core/backend_client.py`・`core/login_coordinator.py`・`ui/settings_window.py`。

### Technical Details
- アプリのバージョンを **1.5.0** に更新（Win=`config/constants.py`、Mac=`Resources/Info.plist`：
  `CFBundleShortVersionString=1.5.0` / `CFBundleVersion=12`）。後方互換の機能追加のため MINOR を更新。
- 後方互換: 旧サーバー応答（`free_*` 無し）でも 0 扱いで従来挙動（未契約＝キー要求）になる。

## [1.4.0] - 2026-06-27

### Added
- **使用実績をアカウントに紐付け（端末横断・再インストール後も引き継ぎ／両OS・release）**。
  ログイン中は「実績」タブの累計文字数・回数・レベル/経験値・推定節約時間・連続利用日数、
  および入力量チャート（週/月/年）を、**この端末だけでなくアカウント全体（複数端末の合算）**で表示する。
  別の PC で使った分も合算され、アプリを入れ直しても実績が消えなくなった。未ログイン時は従来どおりローカル集計。
  - **動作**: 音声入力のたびに当日分（この端末の絶対値）をサーバーへ送信（撃ちっぱなし・冪等 upsert なので
    二重計上なし）。ログイン直後・起動時に直近 60 日分を押し上げてから、端末横断の合算を取り込む。
    集計・送信はすべて貼り付け確定「後」のバックグラウンド処理で、**音声→テキストの遅延には一切影響しない**。
  - **下限保証**: 端末横断の値がローカルより小さい場合でもローカルを下限に保ち、実績が下がって見えないようにした。
  - **サーバー**: `usage_stats` テーブル（`user_id, device_id, day` で日次の絶対値を upsert）と
    `POST /api/v1/stats/sync`・`GET /api/v1/stats`（端末横断で合算して日次系列＋累計を返す）。RLS で本人のみ閲覧可。
  - **実装**: Mac=`Core/StatsStore.swift`・`Core/BackendClient.swift`・`Core/LoginCoordinator.swift`、
    Windows=`core/stats.py`・`core/backend_client.py`・`core/login_coordinator.py`・`app.py`。

## [1.3.1] - 2026-06-27

### Fixed
- **音声入力後に「ログインが必要です」と誤表示される不具合を修正（両OS・release）**。
  ログイン＋アクティベーションキー登録済みでも、音声入力のたびにセッションが破棄され再ログインを
  求められることがあった。原因は **同じ refresh_token を同時に複数回使うとサーバー（Supabase GoTrue）の
  トークンローテーションで `refresh_token_already_used` となり、再利用検知で全セッションが revoke** される点。
  - **アプリ側**: トークン更新（refresh）を直列化し、同時に来た更新要求を進行中の 1 本に集約して
    refresh_token を二重使用しないようにした。Mac=`Core/AuthClient.swift`（進行中 Task の共有）、
    Windows=`core/auth_client.py`（`RLock` ＋ `ensure_valid_session` のロック下再確認）。
  - **サーバー側**: 一時的な競合（`refresh_token_already_used`）は 401 ではなく **409（復帰可能）** で返すように変更。
    アプリはこれを「現在のセッションは有効」と解釈し、セッションを破棄しない。`voicekey-site`
    `app/api/v1/auth/refresh/route.ts`。**この修正はデプロイだけで既存版（v1.3.0）のユーザーにも有効**。

### Changed
- **音声入力のレイテンシ（毎回の遅延）を大幅短縮（両OS・release ＋ サーバー）**。
  - **サーバーのリージョンを東京（`hnd1`）に固定**（`voicekey-site/vercel.json`）。従来は Vercel 関数が
    米国東部（`iad1`）で動き、東京の Supabase DB へ**毎回 6 往復の太平洋横断**（各 ~150ms ≒ 約 1 秒）＋
    コールドスタートが発生していた。同一リージョン化で DB 往復が数 ms に短縮される。
  - **短命トークン発行 API の監査書き込みを `after()` でレスポンス後に回す**（`token_grants` 記録・利用ログ・
    デバイス最終アクセス更新）。トークン発行のクリティカルパスから DB 書き込みを外した。`app/api/v1/auth/ephemeral/route.ts`。
  - **アプリ側に短命トークンのキャッシュを追加**。TTL（60 秒）内は録音をまたいで同じトークンを再利用し、
    2 回目以降の録音はトークン取得のネットワーク往復ゼロで開始する。同時取得は 1 本に集約。
    Mac=`Core/BackendClient.swift`、Windows=`core/backend_client.py`。ログアウト時はキャッシュを破棄。

### Fixed (サーバー堅牢性・`voicekey-site`)
- **Stripe Webhook の書き込み失敗を握り潰していた不具合を修正**。`subscriptions` upsert・`profiles` の customer
  紐付け・`entitlements` の再集計（取得/upsert/`recompute_entitlement`）でエラーを throw するようにし、
  失敗時は冪等記録を消して Stripe に再送させる。従来は「課金されたのに利用権が付かない」事故が無言で起こり得た。
  `app/api/v1/webhooks/stripe/route.ts`、`lib/entitlements.ts`。
- **短命トークン API の `device_id` 長さ上限（200 文字）を追加**（巨大値で DB を汚されないように）。
- **コード交換（exchange）でトークン欠落時に空のセッションを返さないよう null チェックを追加**（アプリが
  「ログイン成功」と誤認するのを防ぐ）。`app/api/v1/auth/exchange/route.ts`。
- **利用ログ（`logUsage`）の DB エラーを `console.error` で観測可能に**（`insert` は例外を投げず `{error}` を
  返すため従来は完全に握り潰されていた）。`lib/apiAuth.ts`。
- **Checkout のプロフィール取得を `.single()`→`.maybeSingle()` に変更**（行が無いとき不要なエラーを出さない）。
  `app/api/v1/billing/checkout/route.ts`。

### Technical Details
- アプリのバージョンを **1.3.1** に更新（Win=`config/constants.py`、Mac=`Resources/Info.plist`：
  `CFBundleShortVersionString` 1.3.1 / `CFBundleVersion` 10）。
- 回帰テスト: `tests/test_backend_client.py` にトークンキャッシュのテスト分離（`clear_token_cache`）を追加。
  並行リフレッシュ直列化に伴い先回りリフレッシュのテストを `_perform_refresh` パッチに更新。
  Windows 全 263 テスト通過・Mac ビルド（0 警告）通過。

## [1.3.0] - 2026-06-26

### Added
- **アクティベーションキーの登録 UI ＋「無料配布はアクティベーション必須」ゲートを追加（両OS・release）**。
  Stripe 課金は一旦無しで、**ログイン＋アクティベーションキーだけで誰でも無料で使える**配布形態にするための
  アプリ側実装。設定 → アカウントでログイン後、アクティベーションキーを入力・登録すると、利用権が
  **アカウントに紐付く**（`redeem_activation_key` がサーバー側で `redeemed_by` に記録）。一度登録すれば
  別の端末でログインしても同じライセンスで使える（利用権はアカウント単位）。配布（DIST）ビルドでは
  埋め込みキー直叩きのフォールバックを止め、**ログイン＋有効な利用権が無いと文字起こしできない**ようにした
  （開発ビルドは従来どおり埋め込み/設定キーの並存を維持）。
  - **サーバー契約**: ログイン中アカウントの状態を返す `GET /api/v1/me`（`{email, active, active_until}`・未契約でも
    200 で `active:false`）を `voicekey-site` に追加。キー登録は既存の `POST /api/v1/activation/redeem`
    （`{ok, active_until}` / 失敗は 400 ＋日本語 `{error}`）に配線。
  - **Technical Details**:
    - 接続定数: `API_ME_PATH` / `API_REDEEM_PATH` を追加（Win=`config/constants.py`、Mac=`Config/ServerConfig.swift`）。
    - バックエンドクライアント: `fetch_account_status()`（GET /me）と `redeem_activation_key()`（POST /redeem・
      サーバーの日本語エラー本文を優先表示）を追加。403 の案内文を「サブスクリプションが有効ではありません」→
      「利用するにはアクティベーションキーの登録が必要です（設定 → アカウント）」に変更。
      Win=`core/backend_client.py`（GET 対応・エラー本文を握れる `_send` に拡張）、Mac=`Core/BackendClient.swift`
      （`AccountStatus` / `.message(String)` エラー）。
    - ログイン司令塔に利用権の状態を追加（`unknown/checking/active/none/error` ＋ メール）。ログイン直後に自動確認、
      `redeem()` 成功で `active` に更新。Win=`core/login_coordinator.py`、Mac=`Core/LoginCoordinator.swift`。
    - 設定 UI のアカウント画面に「ライセンス（アクティベーションキー）」セクション（利用権の状態表示・キー入力欄・
      登録ボタン・期限表示）を追加。Win=`ui/settings_window.py`（登録はワーカースレッド＋Signal）、Mac=`UI/SettingsView.swift`。
    - 無料ゲート: 配布版で未ログインなら文字起こしを `_dist_guard`（Win）/ DIST 分岐（Mac）でブロック。Deepgram の
      ストリーミングは配布版で埋め込みキーにフォールバックせず、未ログイン時は REST 経由でゲートのメッセージを出す。
      Win=`core/api_transcriber.py` / `core/streaming_transcriber.py`、Mac=`Core/Transcriber.swift` / `Core/StreamingTranscriber.swift`。
    - 回帰テスト: `tests/test_backend_client.py`（/me・redeem の成功/エラー/本文表示）、`tests/test_login_coordinator.py`
      （利用権確認・キー登録・ログアウトでのクリア）、`tests/test_api_transcriber.py`（DIST ゲートの4ケース）を追加。
      Windows 全 263 テスト・Mac ビルド（`swift build`）ともに通過。
- **実績タブにデザイン重視の使用統計チャートを追加（両OS両ブランチ）**。
  「今日 / 今週 / 累計」の入力量を 0 からカウントアップ表示し、**期間（週・月・年）を切り替えられる棒グラフ**で
  日ごと・月ごとの入力量を可視化する。開いた瞬間に数字がカウントアップし、棒が下から伸びるアニメーションで
  「使うほど貯まる」達成感を出す。集計はすべて貼り付け確定後のローカル処理のみで、音声入力に遅延を足さない。
  - **Technical Details**:
    - データ層（両OS共通の系列メソッド）: 日付ごとの入力量バケット `daily`（`{characters, recording_seconds, sessions}` を
      `yyyy-MM-dd` キーで保持・直近 800 日に剪定）を追加。`daily_series(n)` / `monthly_series(n)` / 直近 n 日合計の
      ヘルパーを実装。Windows=`src/core/stats.py`、Mac=`Core/StatsStore.swift`（`DayStat` / `UsagePoint`）。回帰テスト
      `tests/test_stats.py` に日次・月次系列の 4 ケースを追加。
    - Mac: `UI/SettingsView.swift` の `StatsTab` を Swift Charts（`BarMark`）で再構成。`StatsPeriod`（週/月/年）の
      セグメント Picker、`AnimatedNumber`（`Animatable`）でカウントアップ。依存追加なし（macOS 標準の Charts）。
    - Windows: `ui/settings_window.py` に自前描画ウィジェット `UsageBarChart`（`QPainter`・`QPropertyAnimation` で棒が
      伸びる）を追加。サマリーは `QVariantAnimation` でカウントアップ、期間切替は `QButtonGroup` のセグメント風ボタン。
      QtCharts 等の依存は追加していない。
- **設定 UI を「開閉できる左サイドバー」に変更（両OS両ブランチ）**。
  項目が増えても画面外に溢れないよう、設定タブを縦のサイドバーにまとめ、開閉トグルで「アイコン＋ラベル ⇄ アイコンのみの
  レール」を切り替えられるようにした。ナビは縦スクロール可能で、選択中の項目はアクセント色で塗る。
  - **Technical Details**:
    - Mac: 横並び `TabView`（画面外へ溢れていた）を `HStack { sidebar; Divider(); content }` に置換。`sidebarCollapsed`
      状態で幅 200⇄62 をアニメーション。ナビは `ScrollView` ＋ `NavItem`（SF Symbols アイコン）。
    - Windows: 既存の `QListWidget` サイドバーに開閉トグル（☰）とアイコンを追加。折りたたみ時は幅 184⇄60 を
      `QPropertyAnimation` で変化させ、項目ラベルを消してアイコンのみにする（ホバーのツールチップで名称表示）。
      アイコンは依存追加なしの自前 SVG → `QIcon`（通常色＋選択時白の 2 状態）。`styles.py` にセグメントボタン／
      トグルの QSS を追加。

## [1.2.0] - 2026-06-20

### Added
- **ユーザー辞書（確定置換）機能を追加（両OS両ブランチ）**。
  設定の「ユーザー辞書」タブで「変換元 → 変換先」の置換ルールを追加・編集・削除でき、
  文字起こし・整形が終わった文章を貼り付ける直前にローカルで機械置換する（部分一致・登録順・
  API を通さないので音声入力に遅延を足さない）。行ごとの有効/無効トグル付き。
  - **Technical Details**:
    - Windows: `config/constants.py` の `DEFAULT_CONFIG` に `replacements`（`{from,to,enabled}` のリスト）を追加。
      `app.py` に `_apply_replacements()` を新設し `_insert_and_enter()` の貼り付け直前で適用（履歴にも置換後を記録）。
      `ui/settings_window.py` に「ユーザー辞書」ページ（動的な行の追加/削除・`_collect_replacements()` で保存）を追加。
      回帰テスト `tests/test_replacements.py`（10 ケース）。
    - Mac: `Config/AppConfig.swift` に `ReplacementRule`（Codable/Identifiable）と `ConfigStore.replacements`＋永続化、
      `applyReplacements(_:)` を追加。`AppController.swift` が整形後・貼り付け前に適用。
      `UI/SettingsView.swift` に「ユーザー辞書」タブ（`DictionaryTab`）を追加。
- **設定に「バージョン情報」タブを追加（自動アップデートの可視化・両OS両ブランチ）**。
  現在のアプリバージョンを表示し、「アップデートを確認」で最新版を手動チェック、新しいバージョンが
  見つかったときだけ「今すぐ更新する」ボタンを出す。チェック頻度（起動時＋1 日ごと）・フィード URL・
  署名/インストール処理は変更していない（既存の Sparkle / updater をそのまま利用）。
  - **Technical Details**:
    - Windows: `utils/updater.py` に `up_to_date` シグナルを追加し、`check_now(manual=)`/`_check(manual=)` で
      手動チェック時のみ「最新です」「確認に失敗」を UI へ通知（定期チェックは従来どおりログのみ）。
      `ui/settings_window.py` に「バージョン情報」ページ（`_create_version_page` ＋確認/更新ハンドラ）を新設し、
      `SettingsWindow(updater=)` で updater を受け取って配線。`app.py` は updater を設定ウィンドウより先に生成して渡す。
    - Mac: `Core/UpdaterController.swift` を `ObservableObject` + `SPUUpdaterDelegate` 化し、検知結果を
      `availableVersionString` に publish（既存の `isAvailable`/`checkForUpdates` は維持）。
      `UI/SettingsView.swift` に「バージョン情報」タブ（`AboutTab`）を追加。Sparkle 既定の更新ダイアログ・
      メニュー「アップデートを確認…」導線はそのまま残す。
- **製品版バックエンドへの配線（段階3・並存ガード）を追加**（Mac / Windows 両方・release ブランチ・休眠中）。
  既存の文字起こし／整形プリミティブが「ログイン済みならサーバー経路、未ログインなら従来の
  埋め込み／設定キー直叩き」を自分で切り替えるようにした。ログイン UI（段階4）が入るまでは
  常に未ログイン扱い（`isLoggedIn` が false）のため、**ユーザーから見える挙動は一切変わらない**。
  - **高速リアルタイム（Deepgram）**: ログイン時は録音時にサーバーから短命 JWT を取得し、
    WebSocket ストリーミング・REST フォールバックとも `Authorization: Bearer <jwt>` で**直叩き**
    （低レイテンシ核心を維持）。未ログイン時は従来どおり `Token <キー>`。
  - **正確性（ElevenLabs）**: ログイン時はサーバープロキシ（multipart）経由（バッチは短命キー非対応）。
    プロキシ失敗は `TranscriptionError` に写す。
  - **テキスト整形（Groq）**: ログイン時はサーバープロキシ経由（モデル/プロンプトはサーバー固定）。
    失敗時は従来どおり原文フォールバック（発話を絶対に失わない）。
  - Mac の `StreamingTranscriber` に接続前チャンクの退避バッファ（`pending`）と `cancelled` フラグを
    追加（短命 JWT 取得が非同期なため、接続確立前に届く PCM を取りこぼさない）。
  - **既知のフォローアップ（段階4/5 で対応）**: 現状は文字起こし呼び出しごとに短命 JWT を取得する。
    長文分割（並列）では分割数だけ取得しうるため、録音プリウォーム位置での先取得＋キャッシュは段階4/5 で行う。
  - **Technical Details**:
    - Windows: `core/backend_client.py` に `is_logged_in()` を追加。`core/streaming_transcriber.py`
      （`_run(key, logged_in)` で Bearer/Token 分岐）、`core/api_transcriber.py`（ElevenLabs プロキシ／
      Deepgram `_transcribe_via_jwt`）、`core/text_formatter.py`（整形プロキシ）に配線。
      回帰テスト 13 件追加（`test_backend_client` / `test_streaming_transcriber` / `test_api_transcriber` /
      `test_text_formatter`）。直叩きテストは `is_logged_in` を False 固定し実 keyring に触れない。
    - Mac: `Core/BackendClient.swift` に `isLoggedIn` を追加。`Core/StreamingTranscriber.swift`
      （`connect(auth:)` 抽出＋ JWT 経路＋退避バッファ）、`Core/Transcriber.swift`
      （`transcribeElevenLabsViaProxy` / `transcribeDeepgramViaJWT`／`send`・`encodeAudio` 抽出）、
      `Core/TextFormatter.swift`（整形プロキシ）に配線。
- **アプリ内フィードバック送信フォームを追加**（Mac / Windows 両方・release ブランチ）。
  メニュー／トレイの「フィードバックを送る…」を、これまでの `mailto:`（既定メーラー起動）から
  **アプリ内の入力フォーム → 自社サーバー送信**へ変更した。ログイン済みならアカウントに紐づき、
  未ログイン（無料ベータ・匿名）でも `device_id` + アプリバージョンで送れる（サブスク有効性は問わない）。
  送信成功／失敗を画面に明示する（誤送信防止より「送れた確証」を優先）。受信は管理画面のみ
  （外部通知は付けない）＝サーバーは Supabase の `feedback` テーブルに保存し、`/admin/feedback` で一覧する。
  - サーバー側は別リポ `voicekey-site`（`feedback` テーブル migration 0013 ／ `POST /api/v1/feedback`
    ／ 管理一覧 `/admin/feedback`）。**サーバーのデプロイと Supabase への migration 適用が済むまでは
    実送信は成立しない**（アプリ側のフォーム表示・入力は動作する）。
  - **Technical Details**:
    - Windows: `core/backend_client.py` に `submit_feedback()`（認証は任意）。`ui/feedback_dialog.py`
      （`QDialog` ＋ 送信ワーカー `QThread`）を新規追加し、`ui/system_tray.py` の `_send_feedback`
      を mailto からダイアログ起動へ変更（不要になった `QUrl` / `QDesktopServices` / `APP_VERSION`
      の import を削除）。`constants.API_FEEDBACK_PATH` を追加。`test_backend_client` に送信テスト 3 件追加。
    - Mac: `Core/BackendClient.swift` に `submitFeedback(_:)`（認証は任意）。`UI/FeedbackView.swift`
      （SwiftUI フォーム）を新規追加し、`VoicekeyApp.swift` の `sendFeedback` を mailto から
      フィードバックウィンドウ表示へ変更。`Config/ServerConfig.swift` に `feedbackPath` を追加。
- **ブラウザ経由ログインの認証クライアントを追加（段階4・増分1／Mac・Windows 両方・release・休眠中）**。
  ログイン UI 配線前の土台として、(1) CSRF 用 state 生成、(2) ブラウザで開くログイン URL
  （`/auth/app?state=&device_id=&platform=`）の構築、(3) ワンタイムコード→トークン交換
  （`POST /api/v1/auth/exchange`）、(4) `refresh_token`→トークン更新（`POST /api/v1/auth/refresh`）と
  失効 60 秒前の自動リフレッシュを実装。トークンは URL に乗せず、ワンタイムコードの交換でのみ取得し、
  既存の認証セッション保存（Keychain / Credential Manager）に書き込む。`refresh` も失効（401）したら
  セッションを破棄して再ログインへ誘導する。**URL スキーム登録・deep link 受信・ログイン UI は後続増分**。
  - サーバー側は別リポ `voicekey-site`：トークン更新エンドポイント `POST /api/v1/auth/refresh` を新規追加
    （GoTrue 直叩きをサーバーに閉じ込め、アプリに anon キーを埋め込まない）。`exchange`/`refresh` とも
    `expires_at` を **UNIX 秒(number)** で返す統一契約に変更（アプリは Double/float で保存・parse 不要）。
  - **Technical Details**:
    - Windows: `core/auth_client.py` を新規追加（`make_state` / `make_login_url` / `exchange_code` /
      `refresh` / `ensure_valid_session` / `logout`）。エラー型・HTTP クライアントは `backend_client` と共有。
      `constants` に `AUTH_APP_PATH` / `API_AUTH_EXCHANGE_PATH` / `API_AUTH_REFRESH_PATH` を追加。
      `tests/test_auth_client.py` に 11 件追加（実 keyring・実通信に触れない）。
    - Mac: `Core/AuthClient.swift` を新規追加（同名の API・`AuthError` 列挙）。`Config/ServerConfig.swift`
      に `authAppPath` / `exchangePath` / `refreshPath` を追加。
- **ブラウザ経由ログインの司令塔（deep link 解析・state 照合・交換）を追加（段階4・増分2／両OS・release・休眠中）**。
  ログイン開始（state 生成 → ログイン URL）と deep link 受信（`voicekey://auth?code=&state=` の解析 →
  CSRF 用 state 照合 → コード交換）を 1 か所に束ねた。保留 state はログイン開始〜受信の間だけ保持し、
  戻ってきた state が一致しなければ交換しない／一度使った state は消費して再利用を防ぐ。
  **URL スキーム登録（Info.plist / レジストリ）と OS からの URL 受信配線・ログイン UI は増分3**。
  - **Technical Details**:
    - Windows: `core/login_coordinator.py` を新規追加（`LoginCoordinator`: `begin_login` / `complete_login` /
      `logout` / `parse_auth_url`）。Qt 非依存（ネットワークを伴う `complete_login` は UI 側でワーカー実行する想定）。
      `tests/test_login_coordinator.py` に 14 件追加（解析・state 不一致/消費・交換失敗）。
    - Mac: `Core/LoginCoordinator.swift` を新規追加（`ObservableObject`・`Status` 列挙・`parseAuthURL` 純粋関数）。
- **ログイン UI と URL スキーム受信を追加（段階4・増分3／両OS・release）**。設定に「アカウント」ページを新設し、
  ログイン状態（未ログイン／ブラウザで完了待ち／処理中／ログイン済み／失敗）の表示と
  ログイン・ログアウトボタンを追加。ログインボタンは既定ブラウザでログインページを開き、
  ブラウザ側で完了すると `voicekey://auth?code=&state=` の deep link でアプリに戻ってトークン交換まで自動で進む。
  **ログインの成立にはサーバー（voicekey-site）のデプロイが必要**なため、それまでは UI 表示のみ動作する（休眠）。
  - **Technical Details**:
    - Mac: `Resources/Info.plist` に `CFBundleURLTypes`（`voicekey` スキーム）を追加し、`VoicekeyApp.swift` で
      `NSAppleEventManager`（`kAEGetURL`）を受けて `LoginCoordinator.shared.handleDeepLink` へ渡す。
      `UI/SettingsView.swift` に「アカウント」タブを追加（`LoginCoordinator` を購読）。
      ビルド済み .app で `voicekey:` スキームが LaunchServices に登録され、deep link がアプリへ届くことを実機確認。
    - Windows: `core/deep_link.py` を新規追加（`voicekey://` のレジストリ登録〔win32/凍結ビルドのみ〕＋
      `QLocalServer`/`QLocalSocket` による単一インスタンス化と URL 転送）。`main.py` で 2 つ目以降の起動を
      稼働中インスタンスへ転送、`app.py` の `VoicekeyApp.handle_deep_link`（ワーカースレッドでコード交換）へ配線。
      `ui/settings_window.py` に「アカウント」ページを追加（状態ポーリング＋ログイン/ログアウト）。
      `login_coordinator.shared()`（遅延生成シングルトン）を追加。`tests/test_deep_link.py` を新規追加。
      単一インスタンス判定は失敗しても通常起動するよう安全側に倒している（deep link 転送だけ諦める）。
- **トークン自動更新をバックエンド呼び出しに配線（段階4・増分4／両OS・release・休眠中）**。
  認証付きのサーバー呼び出し（短命 JWT 取得・ElevenLabs プロキシ・整形プロキシ）で、(1) 送信前に
  失効間際なら `ensure_valid_session` で先回りリフレッシュ、(2) それでも `401` が返ったら一度だけ
  `refresh_token` で更新して**同一リクエストを再試行**するようにした。更新も失敗（401）したら
  再ログインを促すエラーに写す。再試行は一度だけ（無限ループ防止）。認証ヘッダの無い呼び出し
  （exchange / refresh / 匿名フィードバック）はリフレッシュ対象外＝再帰しない。これにより
  ログイン済みユーザーは access_token の失効を意識せず使い続けられる（UI からは見えない裏側の改善）。
  - **Technical Details**:
    - Windows: `core/backend_client.py` の `_auth_headers()` が送信前に `auth_client.ensure_valid_session()`
      を呼び、`_post()` に `_allow_refresh` 引数を追加して 401 時に一度だけ `auth_client.refresh()`→再試行。
      `tests/test_backend_client.py` に 401 リフレッシュ再試行・失敗時非再試行・一度きり・先回りリフレッシュの
      4 件を追加（フィクスチャの `expires_at` を未来時刻にして既存テストを no-op 化）。
    - Mac: `Core/BackendClient.swift` の各認証付きメソッドが先頭で `AuthClient.ensureValidSession()` を呼び、
      `send(_:allowRefresh:)` が 401 時に `AuthClient.refresh()`→新 Bearer で一度だけ再試行する。

### Fixed
- **（Windows）2 回目以降の録音でマイク音声が拾えなくなるバグを修正**。永続ストリームの
  audio callback は「ストリームを開いた時点」の `session_id` に固定されるのに、`_do_start` が
  録音のたびに `session_id` を +1 していたため、1 回目は偶然一致して録音できても 2 回目以降は
  session 不一致で callback が受信音声を全部破棄し、無音になっていた。`session_id` は
  「ストリーム世代」を表すよう、ストリームを開くとき（`_open_stream`）にだけ採番する方式へ変更。
  録音の start/stop を繰り返しても callback は有効なまま。leak したゾンビ callback は `recover()`
  時の世代繰り上げで従来どおり弾かれる。`tests/test_audio_recorder.py` に 2 回目録音の回帰テストを追加。
- **（Mac・体感は両 OS）録音開始直後に「マイクの構成が変わったため録音を停止しました」と
  誤って止まるバグを修正**。`AVAudioEngineConfigurationChange` は `engine.start()` 直後や
  フォーマット確定時にもデバイス未変更で頻繁に誤発火するのに、録音中はこれを一律「デバイス切断」
  とみなして即停止していた。エンジンがまだ動いていれば中断せず、本当に停止していたら同じデバイスで
  タップ・変換器を作り直して**録音をシームレスに継続**し、復帰不能（実際の切断等）のときだけ
  停止通知するよう変更（短時間の再起動回数を数えてループも防止）。`start()` の起動処理を
  `installTapAndStart()` に抽出し再開経路と共通化。

## [Mac 1.2.0 / Windows 1.2.0] - 2026-06-18

### Added
- **製品版バックエンド接続の基盤を追加**（Mac / Windows 両方・release ブランチ・まだ未配線）。
  製品版が長期 API キーをアプリに同梱せず、自社サーバー経由で短命トークン／プロキシを使う
  構成（Phase 5 アプリ統合）の土台。現時点では呼び出し経路が未接続のため、ユーザーから
  見える挙動は変わらない（段階的コミットの最初の 1 段）。
  - サーバー接続先の定数（配布 = https://voicekey.vercel.app / 開発 = http://localhost:3000、
    環境変数 `VOICEKEY_SERVER_URL` で上書き可）と API パス（短命 JWT 発行 / ElevenLabs
    プロキシ / Groq 整形プロキシ）。
  - 端末固有 ID（device_id。識別子であって認証子ではない。サーバー側の同時台数上限・
    悪用検知に使う）を Keychain / Credential Manager に生成・保存。
  - Supabase 認証セッション（access_token / refresh_token / expires_at）の保存・取得・削除。
  - **Technical Details**:
    - Mac: `Config/ServerConfig.swift`（新規）、`Core/Keychain.swift` に `deviceId()` /
      `AuthSession` / `saveAuthSession` / `authSession` / `clearAuthSession` を追加。
    - Windows: `config/constants.py` に接続先・パス定数、`utils/secrets.py` に
      `get_server_base_url()` / `get_device_id()` / `get_auth_session()` /
      `save_auth_session()` / `clear_auth_session()` を追加。`tests/test_secrets_auth.py`（9 ケース）追加。
- **製品版バックエンドクライアントを追加**（Mac / Windows 両方・release ブランチ・段階2・まだ未配線）。
  Phase 4 で実装済みの自社サーバー API を叩くクライアント。`Authorization: Bearer
  <Supabase access_token>` ＋ `x-device-id` で認証し、(1) Deepgram 短命 JWT 取得、
  (2) ElevenLabs 文字起こしプロキシ（multipart）、(3) Groq 整形プロキシ（text のみ・
  モデル/プロンプトはサーバー固定）を提供する。非 200（401/403/409/429/503）は
  ユーザー向け日本語メッセージのエラーに写す。既存の文字起こし経路への配線は段階3 で行う。
  - **Technical Details**:
    - Mac: `Core/BackendClient.swift`（新規。`fetchEphemeralToken` / `transcribeElevenLabs` /
      `formatText` ＋ `BackendError`）。
    - Windows: `core/backend_client.py`（新規。`fetch_ephemeral_token` / `transcribe_elevenlabs` /
      `format_text` ＋ `BackendError`）。`tests/test_backend_client.py`（9 ケース・httpx.MockTransport）追加。
- **使用実績（統計＋ゲーミフィケーション）機能を追加**（Mac / Windows 両方・両ブランチ共通）。
  音声入力を使うほど育つ「実績」を設定ウィンドウの新タブ「実績」に表示する。
  - **表示項目**: レベルと経験値（累計文字数 = XP）＋次レベルまでの進捗バー、推定節約時間
    （同じ文章をキーボードで打つ場合との差。タイピング 4.0 字/秒を控えめに仮定し、過大表示を避ける。
    短い入力でマイナスになる分は 0 に丸める）、累計文字数、音声入力した回数、連続利用日数（最長記録付き）。
  - **実績はリセット不可**（ユーザーが消せないよう、リセット操作は提供しない）。
  - **遅延ゼロ設計**: 集計は貼り付け確定「後」のローカル処理のみで、音声 → テキストの経路には
    一切待ちを足さない（ユーザー指示の最重要制約を順守）。
  - **永続化**: アプリ再起動後も残るよう小さな JSON（`stats.json`）に保存。
    Mac は `~/Library/Application Support/voicekey/stats.json`、Windows は `settings.yaml` と同じディレクトリ。
  - レベル/しきい値/連続日数/節約時間の計算式は Mac（Swift）と Windows（Python）で一致させた。
  - **Technical Details**:
    - Mac: `Core/StatsStore.swift`（新規・`StatsData` Codable ＋レベル計算）、`AppController.swift` の
      確定出力 2 箇所（ストリーミング / REST）で `stats.recordSession(...)` を呼ぶ、
      `UI/SettingsView.swift` に `StatsTab`（tag 3）を追加、`VoicekeyApp.swift` で `stats` を受け渡し。
    - Windows: `core/stats.py`（新規・`StatsStore` ＋ `threshold()` / `level_for_xp()`）、`app.py` の
      `_record_stats()` を文字起こし 2 経路で呼ぶ、`ui/settings_window.py` に「実績」ページ
      （サイドバー index 3）を追加。`tests/test_stats.py` を新規追加（計算式・永続化・復帰の 14 ケース）。

## [Mac 1.1.0 / Windows 1.1.0] - 2026-06-18

### Changed
- **（release＝製品版ブランチ）文字起こしを 2 モードに簡素化＋モデル/整形設定を非公開化**（Mac / Windows 両方）。
  顧客が迷わず使えるよう、文字起こしバックエンドを **「高速リアルタイム」（Deepgram nova-3）/
  「正確性」（ElevenLabs scribe_v1）の 2 択のみ**に絞り、モデル選択 UI を撤去（推奨モデルに固定）。
  OpenAI は文字起こしから除外、Groq は裏のテキスト整形専用に。テキスト整形は Groq 固定モデル
  （llama-3.1-8b-instant）＋固定プロンプトで**裏で自動実行**し、モデル/プロンプト選択 UI は撤去・
  **オンオフトグルのみ残置（既定オン）**。
  - Mac: `Backend.label` を製品名（高速リアルタイム/正確性）に、`Backend.selectableCases` を追加して
    バックエンド Picker を 2 択化。スロットのモデル Picker・一般タブの整形モデル/指示 UI を撤去。
    既定スロットを deepgram/elevenlabs に、`SlotConfig` の decode で旧 openai/groq を deepgram へ移行
    （モデルも推奨へ揃える）。整形既定 `formatEnabled` を `true` に。API キータブは deepgram/elevenlabs/groq のみ。
  - Windows: `_TRANSCRIBE_BACKEND_LABELS`（2 択）・`_API_KEY_BACKENDS`（3 種）を追加。バックエンド Combo を
    2 択化、モデル Combo・整形モデル/指示 UI を撤去（不要化した `_BACKEND_LABELS`/`_BACKEND_MODELS`/
    `_model_label` 等を削除）。`constants.py` の既定を deepgram/elevenlabs＋`format_enabled: True` に。
    `config_manager._constrain_release_backends` を追加し、保存済み openai/groq を deepgram へ移行
    （`api_model` を空にして `default_api_models` へフォールバック）。テスト 2 件追加。
- **VAD・長文分割・リアルタイムストリーミング・録音 HUD（＋ Windows の音量正規化）のオンオフを
  設定 UI から撤去し、常時 ON に固定**（Mac / Windows 両方）。これらは「使い分けが難しく常に ON が
  最適」なため、ユーザーが OFF にする手段を持たせない方針に変更。動作（消費側）は従来どおり常に有効。
  - Mac: `ConfigStore` の `vadEnabled / splitParallelEnabled / streamingEnabled / hudEnabled` を
    init で `true` 固定にし、`SettingsView` GeneralSettingsTab から該当 Toggle を撤去。
  - Windows: `settings_window.py` 一般ページから該当ウィジェットと読込/保存バインド・
    ストリーミング診断（`_refresh_streaming_status`）を撤去。`config_manager._force_always_on` を
    追加し、読込（`_load_config`）と保存（`save`）の双方で `vad_filter / split_parallel_enabled /
    streaming_enabled / hud_enabled / audio_preprocess.volume_normalize` を `True` に矯正
    （保存済み settings.yaml に古い `false` が残っていても無視して常時 ON を保証）。
- **2 ブランチ運用に分離**（`main`=自分用 / `release`=製品版・絶対に混ぜない）。分岐ポリシーを
  `CLAUDE.md`・`AGENTS.md`・`HANDOFF.md` に明記（どのブランチに入れるかは毎回指定、未指定なら確認）。
- **バックエンドの表示名を特徴ベース名に変更**（Mac / Windows 両方）。設定 UI の
  バックエンド選択・ユーザー向けエラーメッセージで提供元名（OpenAI / Groq /
  ElevenLabs / Deepgram）を出さず、**「高精度 / 高速 / 多言語 / リアルタイム」**と表示する。
  保存値（`Backend.rawValue` / `settings.yaml` の `backend`）は従来どおり不変なので
  既存設定はそのまま読める。API キー入力欄だけは、どのキーを入れる欄か分かるよう提供元名を
  表示（配布版では API キータブ自体が非表示なので提供元名は開発時のみ露出）。
  - Mac: `Backend.label` を特徴名に変更し、提供元名は `Backend.providerName` に分離。
    `Transcriber` のエラー文（ElevenLabs / Deepgram の応答解析失敗）も `backend.label` に統一
  - Windows: `_BACKEND_LABELS` を特徴名に変更し、提供元名は `_BACKEND_PROVIDER_NAMES` に分離。
    `api_transcriber.py` の各 `display_name` も特徴名に変更（ユーザー向けエラー文に出るため）
- **Windows 設定 UI の補足説明文を Mac と同等に削減**。プロバイダー名・モデル名
  （Groq / Deepgram / llama-... 等）を晒す説明や冗長な解説を撤去し、コントロール名だけでは
  意味が通じない 2 項目（自動 Enter の遅延 / ハンズフリー切替キー）のみ短文を残した。
  ストリーミングのトグル名・状態メッセージからも「Deepgram」表記を除去（「リアルタイム」へ）。
  プライバシー上の保存先注記（履歴・API キーの保存場所）は残置

## [Mac 1.0.2 / Windows 1.0.1] - 2026-06-16

### Added
- **Windows インストーラにデスクトップショートカット作成オプションを追加**。Inno Setup の `[Tasks]`/`[Icons]` に `desktopicon` を追加し、インストール時に「デスクトップにショートカットを作成する」を選べる（既定チェック済み）。従来はスタートメニューとログイン時自動起動のみだった
- **Windows 版 v1.0.0 を初公開配布**。GitHub Actions（`windows-build.yml`）でキー埋め込みインストーラを生成し、公開バイナリ専用リポ `voicekey-releases`（**ソース非公開**）の GitHub Releases でホスト。配布サイト（Vercel）の「Windows 版をダウンロード」を有効化。容量が大きい（約 268MB）ためサイト本体（Vercel）ではなく GitHub Releases から配る構成にした
  - `scripts/build/build_windows_dist.ps1`: version.json の `url` を GitHub Releases のアセット URL に変更（自動アップデータのダウンロード元）

### Fixed
- **Windows でマイク自動検出が OS をクラッシュ（再起動）させる重大バグを修正（WDF エラー）**。`src/core/mic_auto_detect.py` が全入力デバイスに `sd.InputStream` を一斉に同時オープンしていたため、同じ物理マイクが host API ごと（特にカーネルストリーミングの WDM-KS）に重複列挙される Windows では、それらを同時に開いた瞬間に音声ドライバが WDF レベルでクラッシュし OS が再起動していた。次の 3 点で根治した:
  - **同時オープンを廃止し、1 台ずつ順次プローブする**（同時に開く `InputStream` は常に最大 1）。回帰テスト `test_devices_probed_sequentially` で「同時オープンが起きない・プローブ後にストリームが残らない」ことを保証
  - **WDM-KS / ASIO（カーネル直叩き・排他系）と同名重複デバイスを自動検出の対象から除外**（Windows のみ）。カーネルを直接叩く host API を自動検出では触らない
  - 開く前に `sd.check_input_settings` で構成を検証し、非対応・占有中デバイスは実際に開かずスキップ
  - 順次化に伴い、設定 UI の文言を「マイクに向かって喋り続けてください」に変更。`AudioRecorder.list_input_devices` の戻りに host API 名（`hostapi`）を追加
- **Windows 版 GitHub Actions ビルドの文字化け／エンコーディング不具合を修正**（Mac から PC を使わずに Windows 配布物をビルドできるようにする一連の対応）
  - `scripts/build/build_windows_dist.ps1` に UTF-8 BOM を付与。Windows PowerShell 5.1（powershell.exe）が BOM なし UTF-8 を cp1252 と誤読し、日本語を含む行でパースエラー（`The string is missing the terminator`）になっていた
  - `scripts/build/generate_embedded_keys.py` で stdout/stderr を UTF-8 に固定。Windows ランナーの既定 stdout（cp1252）で日本語の進捗 print が `UnicodeEncodeError` になり、鍵生成は成功しているのにスクリプトが落ちていた
  - `.github/workflows/windows-build.yml` のビルドステップに `PYTHONUTF8: "1"` を追加（PyInstaller を含むステップ全体の Python 出力を UTF-8 化）
  - **ソース平文混入チェックを `src/` 配下のみに限定**。従来は dist 内の `.py` を 1 つでも検出すると配布中止していたが、PyInstaller onedir は torch/torchaudio 等 OSS ライブラリの `.py`（公開コードで IP 漏洩に当たらない）を必ず同梱するため 2301 件を誤検知していた。自分の proprietary な `src/` は voicekey.spec で `datas=[]`＋PYZ バイトコード格納なので平文混入しない設計を維持しつつ、チェック対象を `\src\` パスのみに絞った
  - **自動アップデータの version.json パースを BOM 耐性化**。`src/utils/updater.py` を `utf-8-sig` デコードに変更し、ビルドスクリプトは version.json を BOM 無し UTF-8 で書き出すようにした（PS5.1 の `Set-Content -Encoding UTF8` が付ける BOM で `json.loads` が落ち、自動アップデートが静かに効かなくなるのを防ぐ）
- **コードレビューで検出した追加バグを修正**（2026-06-14）
  - **自動アップデータのダウンロードが回線 stall で永久ブロックするのを修正**。`urllib.request.urlretrieve`（タイムアウト不可）から `timeout=30` 付き `urlopen` + ストリームコピーに変更し、回線が固まっても確実に打ち切れるようにした。あわせて version.json に `url` / `sha256` が無い場合は DL せず明示エラーにし、検証不能なインストーラを実行しないようにした（`src/utils/updater.py`）
  - **設定保存（`ConfigManager.save`）が手書きのネストキーを消すのを修正**。浅い `dict.update` を `_deep_merge` に変更し、`default_api_models` などネストした辞書内のカスタムキーが保存時に丸ごと失われないようにした（`src/config/config_manager.py`）
  - **ダーク／ライトのテーマ切替トグルが回転アニメーションしないのを修正**。`ThemeToggleButton.angle` を Python 組み込み `property` から Qt の `Property(float, ...)` に変更し、`QPropertyAnimation` が回転角度を駆動できるようにした（`src/ui/settings_window.py`）
- **ダブルタップ Enter 自動送信と録音中 UI の消去が約 0.5 秒遅れる問題を修正（Mac / Windows 両方）**（2026-06-16）。固定待ちが 2 か所あり、いずれも設定値ではないため「ミリ秒に設定しても効かない」状態だった:
  - **ダブルタップ確定後の離鍵で再び 0.4 秒待っていたのを撤廃**。2 打目の押下で確定済み（auto_enter）なのに、最後の離鍵が録音開始から 0.4 秒以内だと「2 打目待ち」の判定窓に巻き込まれ、短い録音ほど必ず 0.4 秒遅れて停止していた。確定後の離鍵は即座に録音停止するようにした（Mac: `AppController.handleRelease` / Windows: `app._on_release`）
  - **貼り付け後のクリップボード復元待ち（0.3 秒）を呼び出し元から切り離した**。貼り付け先が読み終えるまで 0.3 秒待ってから復元する処理が、Enter 送信と録音中 HUD の非表示を直列にブロックしていた。復元はバックグラウンドに逃がし、貼り付け直後に Enter 送信・UI 非表示へ進むようにした（Mac: `Paster.paste` を別タスク化 / Windows: `InputHandler.insert_text` を `threading.Timer` 化）。Windows の貼り付け前待機も Mac と揃えて 0.1→0.05 秒に短縮
  - 体感の Enter までの待ちは「貼り付け前待機 + Auto Enter Delay（設定値）」だけになり、0.4〜0.5 秒の固定遅延が消えた。回帰テスト `test_double_tap_latency` / `test_input_handler` で検証
- **コードレビューで検出した残りのバグを修正**（2026-06-16）
  - **録音末尾チャンクの取りこぼしを修正**。`AudioRecorder._do_stop` が `stream.stop()` の「前」に録音中フラグを倒していたため、stop() が内部バッファを流し終える間に届く最後の音声が callback で捨てられていた。フラグを stop() の後で倒すようにした（回帰テスト `test_audio_recorder`）
  - **`ConfigManager` をスレッドセーフ化**。config を listener / 設定監視 / UI の各スレッドが同時に read/write しても壊れないよう `RLock` で保護（`get` / `reload_if_changed` / `save`）
  - **設定ウィンドウが `ConfigManager` を二重生成していたのを解消**。本体と同一インスタンスを共有し、保存時にシグナル（`settings_saved`）で設定変更を即時適用するようにした（従来は最大 1 ポーリング周期ぶん反映が遅れていた）（`src/ui/settings_window.py` / `src/app.py`）
  - **自動アップデータのバージョン解析を堅牢化**。`v1.2.3` や `1.2.3-beta` / `+build` 形式でも `ValueError` で更新確認が無言で止まらないようにした（`parse_version`）
  - **ホットキー入力欄の不整合を修正**。クリックしてキーを押さずにフォーカスを外すと「内部状態は空・表示は旧値」になる問題を解消（`HotkeyInput.focusInEvent`）

### Changed
- **設定画面の補足説明文を削減**（Mac）。プロバイダー名・モデル名（Groq / Deepgram /
  llama-... 等）を晒す説明や冗長な解説を削除し、コントロール名だけでは意味が通じない
  2 項目（自動 Enter の遅延 / ハンズフリー切替キー）のみ短い説明に書き直して残した。
  ストリーミングのトグル名からも「（Deepgram）」表記を除去
- **HUD のハンズフリー停止ヒントの文言を「もう一度押すと停止」に変更**（Mac）
- **ハンズフリー録音中の HUD 表示を新設**（Mac）。トグル実効モードの録音中は
  状態ドットとメニューバーアイコンをティール色にし、ピル内に「ハンズフリー」ラベルと
  「もう一度で停止」ヒントバッジを表示。通常録音（赤）・自動送信（紫）と一目で区別できる
  - `AppState.recording` に `handsFree` を追加し、`recordingEffectiveMode == .toggle` を発信
  - `HudView` に `handsFreeAccent`（ティール）と `stopHint` バッジを追加
- **VAD の発話区間計算を共通化**（Windows `_speech_regions` / Mac `speechRegions` 抽出）。
  無音圧縮（analyze/condense）と分割（segment）が、同じ 1 回の推論結果を 2 通りの
  ギャップ閾値（0.5s 保持 / 0.7s 分割）でマージする方式に整理
- **Windows 版 設定 UI の全面再デザイン**（「ダサい・安っぽい」指摘対応。2026-06-13）
  - 上部タブ → **左サイドバーナビゲーション**（macOS System Settings 風。
    ブランド表示 + ページタイトルヘッダー、ウィンドウは 560x640 → 720x600）
  - 設定項目を**カード型セクション**にグルーピング（角丸の面 + 行間ヘアライン区切り。
    一般ページは 基本/音声処理/表示と動作/テキスト整形/起動 の 5 カード構成）
  - チェックボックスを **iOS 風トグルスイッチ**に置換（ToggleSwitch クラス新規。
    QCheckBox 互換 API・スライドアニメーション付き・自前描画）
  - styles.py の**デザイン体系を刷新**: グラデーション全廃のフラットモダン、
    3 層の面構成（サイドバー/コンテンツ/カード）、タイポグラフィ体系
    （タイトル 19px / 本文 13px / 補足 12px）、ライトの成功・警告色を白カード上で
    読めるコントラストに調整
  - **コンボボックス矢印のグリフ崩れを修正**: CSS ボーダートリック → SVG ファイル参照
    （Qt の QSS は data URI 非サポートのため一時ディレクトリへ書き出して url() 参照。
    スピンボックスの上下ボタンも同方式で統一）
  - 補足説明ラベルは objectName("caption") + QSS 化（テーマ切替に自動追従）

### Added
- **ハンズフリー切替キー**（グローバル設定 `handsfree_key`）
  - 切替キー＋既存ホットキーを同時押しすると、そのプロバイダーがトグル録音
    （1 回押して開始・もう一度で停止）になる。既存の長押し（hold）はそのまま維持
  - スロットは 2 個のまま。両プロバイダーをハンズフリー化できる。既定は空＝無効
  - Mac/Windows 両対応。録音中の「実効モード」を新設し release/toggle 停止判定が参照
- **長文の分割並列送信**（`split_parallel_enabled`・**既定オン**）
  - 12 秒超の録音を 0.7 秒超の無音区間で分割し、REST batch API（OpenAI/Groq/
    ElevenLabs）へ並列送信して index 順に結合。待ち時間を最遅セグメント分まで短縮
  - 区切りは無音の中だけなので語の途中では切れず精度への実害なし。一部失敗時は
    全体 1 本送信へフォールバック。VAD 推論は録音 1 回のまま（region 計算を共通化）
  - Deepgram ストリーミングは対象外（既にリアルタイム）
- **JIS（日本語）キーボード配列対応**
  - 記号キー（- = [ ] \ ; ' , . / `）と日本語専用キー（¥ / かな / 英数 / 無変換 /
    変換 / 半角全角）をホットキーに使用可能化（Mac: 物理キーコード、Windows: VK ベース）
  - 環境・IME により一部の日本語キーは pynput に届かず使えない場合がある
- **開発版／ベータ版の起動を 1 コマンド化**（「2 バージョンをはっきり区別したい」要望対応）
  - `macos/scripts/run_dev.sh` 新規: スタブ鍵で再ビルド → `/Applications/voicekey.app` へ
    インストール → 再起動。普段使いは常にこれ（API キータブが表示されるのが開発版の目印）
  - `macos/scripts/run_beta.sh` 新規: dist/ の最新配布 DMG をマウントしてベータ版を一時起動
    （テスターと同一物の動作確認用。終わったら run_dev.sh で戻る）
  - 背景: dist/voicekey.app はビルドのたびに dev/dist で上書きされ、どちらが動いているか
    分からなくなっていた（実際に配布ビルド後、ベータ版を常用し続ける状態が発生していた）
- **Windows 版ビルドの GitHub Actions 化**（PC またぎ・.env.dist 手運びの廃止）
  - `.github/workflows/windows-build.yml` 新規: windows-latest で
    `gh workflow run windows-build.yml -f version=X.Y.Z` → setup.exe + version.json を
    artifact で取得。キーは GitHub Secrets → 環境変数で注入（ランナーにファイルを置かない）
  - `docs/BUILD_WINDOWS.md` を GitHub Actions 標準・実機ビルドはフォールバックに再構成

### Fixed
- **未設定（空ホットキー）スロットが全キーで誤発火しうる潜在バグ**を修正（Windows `_slot_matches`）。
  `required_keys` が空のとき `all([])` が True になり、未設定スロットがどのキー押下でも
  一致していた。空のときは一致しないようガードを追加（ハンズフリー切替キー実装で
  `_slot_matches` を `required_keys` ベースに整理した際に表面化）
- **録音開始遅延の根本修正**（「キーを押してもマイクがすぐオンにならない」報告。
  実測: 押下→マイク実起動が初回 1511ms、2 回目以降 41〜69ms）
  - 原因①: 毎押下で全入力デバイスの HAL 列挙 + AUHAL 再構成をやり直していた
    （既存の prewarm は設定デバイスを見ておらず、初回押下のデバイス切替で
    温めた状態が捨てられる＝構造的に無効化されていた）
  - 原因②: 実 IO（AudioOutputUnitStart）の初回起動コスト（1 秒超）を録音時に支払っていた
  - 原因③: メインスレッドの Keychain 読み（API プリウォーム類）が recorder.start より
    先に走っていた
  - 修正: デバイス適用を「設定が変わったときだけ」に（AudioRecorder に適用済み UID を
    キャッシュ）、起動時 prewarm で設定デバイスを適用しダミータップで IO を一度
    起動・停止して前払い（**起動直後にマイクインジケータが一瞬点灯するのはこの
    ウォームアップ**。音声は記録しない）、beginRecording の順序を recorder.start 最優先に入替
- 入力デバイスを一度指定すると「システム既定」に戻せなかった（エンジンに前回のデバイスが
  固定されたまま残る）→ 既定デバイス ID の明示設定で復帰、既定デバイスの変更にも追従
- 録音中のマイク切断・構成変更でエンジンが静かに止まり、喋り続けても何も入らなかった
  → AVAudioEngineConfigurationChange を監視し、録音を確定（途中までの音声は変換）+ HUD 通知
- 録音開始失敗時に Deepgram ストリーミングセッションと chunkHandler が残留し、
  次の録音（別バックエンドでも）に Deepgram の結果が混ざり得た → 失敗時に後始末
- 長い口述（0.4 秒超）の直後 0.4 秒以内に次の録音を始めるとダブルタップ誤判定で
  auto_enter（Enter 自動送信）になっていた → 短いタップの離鍵だけを 1 打目として記録
- クリップボード復元が、貼り付け待ちの 0.3 秒間にユーザーがコピーした新しい内容を
  上書き破壊していた → changeCount を確認し、書き換わっていたら復元しない
- 連続録音で 1 件目がエラー通知を出すと、2 件目の変換が進行中でも HUD が消えていた
  → 通知の消灯時に変換中なら「変換中…」表示へ戻す
- マイク自動検出: 締め切り後に起動が完了した遅いデバイス（Bluetooth 等）が停止されず
  マイクを掴み続けた → 締め切り後の起動完了は即停止
- ダブルタップ確定（auto_enter 昇格）時に HUD の波形・ライブ字幕が一瞬消えていた
- スレッド競合の修正: AudioRecorder の chunkHandler / recording（audio スレッド vs
  メイン）、Transcriber の model/language/prompt（設定変更 vs 文字起こしタスク）を
  lock で同期
- URLSession の漸増リーク修正: StreamingTranscriber（録音ごとに生成）と
  バックエンド変更で捨てられる旧 Transcriber のセッションを明示破棄

### Technical Details
- **AudioRecorder.swift**: appliedDeviceUID キャッシュ + applyInputDevice() 抽出、
  prewarm の IO 前払い、AVAudioEngineConfigurationChange 監視（deviceChangedHandler）、
  stateLock による同期
- **AudioDevices.swift**: defaultInputDeviceID() 追加（プロパティ 1 回取得・軽量）
- **AppController.swift**: beginRecording の順序入替（デバイス反映 → WS → start →
  プリウォーム）、start 失敗時の streamer 後始末、handleRelease の lastRelease 記録条件変更
- **Paster.swift / Hud.swift / MicAutoDetector.swift / StreamingTranscriber.swift /
  Transcriber.swift**: 上記の各修正

## [1.0.1] - 2026-06-12

Mac 版 v1.0.1 を公開（アプリアイコン追加・DMG レイアウト改善）。
既存テスターには Sparkle の自動アップデート（build 3 → 4、差分配信付き）で届く。

### Added
- **Mac 版アプリアイコンを新規作成**（ユーザー要望。これまではジェネリックアイコン）
  - ダーク角丸スクエア + ブランドの青い波形バー（配布サイトの HUD と同モチーフ）
  - `scripts/dev/make_app_icon.swift` 新規: 1024px マスター PNG のジェネレータ
    （icns 変換コマンドはファイル冒頭コメントに記載）
  - `Resources/AppIcon.icns` をコミットし、`Info.plist` に CFBundleIconFile、
    `build_app.sh` に Resources へのコピーを追加。NSWorkspace 経由で解決確認済み

### Changed
- **Mac 版 DMG のレイアウトを一般的なアプリの形式に改善**（ユーザー要望）
  - 背景画像（660x400pt・2x）にドラッグ&ドロップ誘導の矢印と「右クリック→開く」の
    1 行ガイドを描画。左にアプリ・右に Applications・右上に手順書テキストを配置
  - `scripts/package_dmg.sh` 新規: UDRW で作成 → Finder (AppleScript) でアイコン位置・
    背景を .DS_Store に焼き込み → UDZO 変換 → 署名。`build_dmg.sh` の DMG 作成部を置換
  - `scripts/dev/make_dmg_background.swift` 新規: 背景画像ジェネレータ
    （生成物 `scripts/assets/dmg_background.png` はコミット対象）
  - 配布中の v1.0.0 DMG を同一バイナリ（build 3・CDHash 一致確認済み）のまま
    新レイアウトで再パッケージし、サイトへ再デプロイ（downloads.json のサイズも更新）

### Fixed
- **インストール手順を macOS 15 (Sequoia) の実挙動に合わせて修正**（実テスターからの報告）。
  Sequoia では DMG を開く段階で「マルウェアが含まれていないことを検証できませんでした」が
  出る（アプリ初回起動時だけではない）。同梱手順書と配布サイトに
  「完了で閉じる（ゴミ箱に入れない）→ プライバシーとセキュリティ →このまま開く」
  「警告は DMG とアプリ初回起動の最大 2 回」を明記
- `generate_embedded_keys.sh`: コピペで `--` が – / —（en/em ダッシュ）に化けた引数も
  受け付けるようにした（`–export-env` で引数なし扱い→スタブ生成になる事故が実際に 2 回発生）。
  あわせて不明な引数は黙ってスタブを生成せずエラー終了に変更（「できたつもり」事故防止）

## [1.0.0] - 2026-06-12 (無料テスト版・ベータ初回リリース)

Mac 版 v1.0.0 を公開。配布物（DMG・更新用 zip・appcast・version.json）はすべて配布ページ
https://voicekey.vercel.app（Vercel）から配信し、GitHub はテスターから一切見えない構成。
Windows 版は実機ビルド待ち。

### Changed（同日追記: 配布を Vercel に一本化）
- 当初 GitHub Releases + raw URL で公開したが、「ダウンロード URL からリポジトリの存在が
  見えるのを防ぎたい」というユーザー要望で、配布物をすべて Vercel サイト内
  （/downloads/・/mac/・/windows/）へ移設。サイトの最新版表示も GitHub API から
  サイト内 downloads.json 参照に変更
- `Info.plist` SUFeedURL / `updater.py` VERSION_URL / `build_dmg.sh` の appcast URL /
  `build_windows_dist.ps1` の version.json URL を voicekey.vercel.app 配下へ変更し、
  Mac 版を再ビルド（1.0.0 build 3。strings 平文キー 0 件・全配布 URL 200 を確認）
- voicekey-releases リポジトリは private 化（バイナリ置き場としての役目を終了）

### Added
- ベータ配布計画の開始（HANDOFF.md にゴール・フェーズ・恒久要件を記録）
- `.gitignore` に配布ビルド用キー生成物を追加（`.env.dist` / `src/config/embedded_keys.py` /
  `macos/Sources/Voicekey/Config/EmbeddedKeys.generated.swift`）。API キーは git に絶対コミットしない
- 配布用 `beta` ブランチを新設（開発は main、リリース時に main → beta マージで dist ビルド）
- **Mac 版 API キー埋め込み機構（テスター向け配布ビルド用）**
  - `macos/scripts/generate_embedded_keys.sh`: `--dist` で Keychain の現行キー4件を抽出し、
    ランダム 32 バイトマスクで XOR 難読化した `EmbeddedKeys.generated.swift`（git 管理外）を生成。
    引数なしはスタブ生成（isDist=false・キーなし、通常開発用）
  - `Keychain.swift`: キー解決を「Keychain → 環境変数 → 埋め込みキー」の3段フォールバックに拡張
  - `SettingsView.swift`: DIST ビルドでは API キータブを非表示（テスターの混乱防止）
  - `build_app.sh`: 生成ファイル未存在時のスタブ自動生成 + 署名 identity の環境変数
    `VOICEKEY_SIGN_IDENTITY` 対応（Developer ID への後日切替用）
  - 検証済み: XOR 復号ラウンドトリップ（swiftc 単体テスト）/ DIST バイナリの strings に平文キーなし /
    スタブビルドで通常起動
- **Mac 版 Sparkle 2 自動アップデート + 配布パイプライン（ベータ配布 Phase 2）**
  - `Package.swift` に Sparkle 2.9.3 を追加。`build_app.sh` が Sparkle.framework を
    Contents/Frameworks へ同梱し rpath を追加（SPM 手組みバンドルのため手動埋め込み）
  - `UpdaterController.swift`: DIST ビルド かつ .app バンドル実行時のみ Sparkle を起動
    （開発ビルドに更新ダイアログが出るのを防止）
  - メニューに「アップデートを確認…」（DIST のみ）と「フィードバックを送る…」（mailto）を追加
  - `Info.plist`: SUFeedURL（voicekey-releases の appcast）/ SUPublicEDKey / 自動チェック 24h
  - `build_dmg.sh` 新規: バージョン更新 → キー埋め込み（終了時に必ずスタブへ復元）→ ビルド →
    開発機 rpath 除去 → hardened runtime で inside-out 署名（ad-hoc はエラー終了）→
    DMG（/Applications リンク + 手順書同梱）→ Sparkle 用 zip → appcast 生成。
    `--identity` / `--notarize` は Developer ID 加入後にそのまま使えるパラメータ化済み
  - `voicekey.entitlements` 新規（hardened runtime 下のマイク使用に必須）
  - `dmg_readme.txt` 新規: Gatekeeper 回避手順（macOS 14/15 別）+ 音声が外部 STT API に
    送信される旨のプライバシー注意
  - 検証済み: 偽キーで v0.0.1 → v0.0.2 の DMG/zip/appcast を実ビルドし、ローカル HTTP サーバの
    appcast 経由で旧版が新版を検知 → バイナリ差分 DL → 終了時に自動インストールされ
    バンドルが 0.0.2 に置き換わる E2E を確認。codesign --verify --deep --strict 通過
- **Windows 版 配布一式（キー埋め込み・自動アップデート・インストーラ、ベータ配布 Phase 4）**
  - `scripts/build/generate_embedded_keys.py` 新規: `.env.dist` → 環境変数の順でキーを取得し、
    XOR 難読化した `src/config/embedded_keys.py`（git 管理外）を生成。keyring は意図的に不使用
    （macOS 検証時の実 Keychain ダイアログ事故防止）
  - `src/utils/secrets.py`: キー解決を「keyring → 埋め込みキー」フォールバックに拡張 +
    `is_dist_build()` 追加（未登録キャッシュ・keyring 例外・keyring 不在の全経路で埋め込みへ落ちる）
  - `src/utils/updater.py` 新規: 自前自動アップデータ。voicekey-releases の version.json を
    起動 60 秒後 + 24 時間ごとにチェック（DIST のみ）→ トレイ通知 + メニュー項目（モーダル禁止）→
    SHA256 検証付き DL → Inno `/VERYSILENT /CLOSEAPPLICATIONS` でサイレント更新 → 新版自動再起動
  - `src/ui/system_tray.py`: 更新通知（showMessage + 動的メニュー項目）と
    「フィードバックを送る…」（mailto・バージョン入り件名）を追加
  - `src/app.py`: Updater を SystemTray と配線（検知 → 通知、インストール → 終了要求 → _quit_app）
  - `src/ui/settings_window.py`: DIST ビルドで API キータブ非表示
  - `installer/windows/voicekey.iss` 新規: Inno Setup 6。AppId GUID 固定 /
    `PrivilegesRequired=lowest` + `{localappdata}\Programs\voicekey`（UAC なしでサイレント更新可能）/
    日本語 UI / スタートアップ登録 / 更新後の自動再起動
  - `scripts/build/build_windows_dist.ps1` 新規: APP_VERSION 更新 → キー埋め込み → PyInstaller →
    dist への .py 混入検査 → ISCC → SHA256 → version.json 出力 → embedded_keys.py 自動削除
  - `macos/scripts/generate_embedded_keys.sh` に `--export-env` 追加（Keychain から `.env.dist` を
    書き出し、Windows ビルド機へ手動コピーする受け渡し用。chmod 600）
  - `docs/BUILD_WINDOWS.md` 新規: Windows 実機ビルド手順 + 配布前チェックリスト
    （.py 非混入 / キー入力なしで動く / 自動更新 E2E / リリース順序: バイナリ添付 → version.json）
  - `tests/test_updater.py`・`tests/test_secrets_embedded.py` 新規（22 テスト。
    バージョン比較・SHA256 不一致時の非実行・キー解決チェーン・生成スクリプトのラウンドトリップ）

- **マイク自動検出（Mac 版・Windows 版、ベータ配布 Phase 5）**
  - 設定の入力デバイス行に「自動検出」ボタンを追加。押した後に喋ると、全入力デバイスを
    約 4 秒間同時監視し、声が入っているマイクを自動選択する（どのマイクか分からない人向け）
  - 判定: 30ms フレームの RMS を集め `score = p90 − p10`。喋り声は変動大・定常ノイズや
    ループバック系は変動小のため、単純な最大 RMS より誤選択が少ない（しきい値 0.005）
  - Mac: `MicAutoDetector.swift` 新規（デバイスごとに独立 AVAudioEngine を同時起動・使い捨て。
    録音用エンジンには触れない。開けない/ハングするデバイスはスキップ）+
    `SettingsView.swift` にボタンと「自動検出中… マイクに向かって喋ってください」進捗表示
  - Windows: `src/core/mic_auto_detect.py` 新規（使い捨てスレッドで全デバイスに sd.InputStream を
    同時オープン。AudioRecorder の永続ストリームには触れない）+ `settings_window.py` に
    ボタン・進捗ラベル（完了は Qt Signal でメインスレッドへ）
  - 検証: スコアロジックの単体テスト（Swift 5 ケース / Python 12 テスト・偽 sounddevice での
    同時監視統合テスト含む）、オフスクリーン UI スモーク、Mac ビルド+再起動。
    実マイクでの最終確認は要ユーザー操作（設定 → 自動検出 → 一言喋る）

- **Windows 版 UI 再デザイン（Mac 版の質感へ統一、ベータ配布 Phase 6）**
  - `src/ui/styles.py` をデザインシステム化: フォント定数（タイトル 18 / 本文 14 / キャプション 12px）、
    角丸定数（コントロール 8px / パネル 12px）、ボタン・選択タブの qlineargradient、
    macOS システムカラー追加（SUCCESS / WARNING / アクセントグラデ）、入力欄 hover、
    QSlider スタイル新規（4px groove + 18px 白グラデハンドル）、QCheckBox hover、
    ラベル用ヘルパー 5 種（title/caption/status_ok/status_warn/status_muted）
  - `src/ui/settings_window.py`: インラインスタイル 7 箇所を styles.py ヘルパーへ集約
  - `src/ui/system_tray.py`: トレイアイコンを原色ベタ塗り → macOS システムカラー
    （待機 #8E8E93 / 録音 #FF453A / 変換中 #FF9F0A / 自動 Enter #BF5AF2）+
    放射グラデーションのハイライト + 細い縁取りで立体感を付与
  - `src/ui/hud.py`: ピル背景を縦グラデーション化（.ultraThinMaterial 風）+ 白 10% ボーダー +
    paintEvent 内 3 層自前影（QGraphicsDropShadowEffect は透過窓で効かないため）。
    ドット色を Mac 版と統一（録音 #FF453A / 自動 Enter #BF5AF2）。表示タイミングは不変更（速度最優先）
  - `scripts/dev/preview_ui.py` 新規: オフスクリーンで設定全タブ（ダーク/ライト）・HUD 4 状態・
    トレイ 4 状態を PNG 出力する目視確認ツール（secrets/keyring はモック）
  - 検証: preview_ui.py のスクショ目視（全タブ × 2 テーマ + HUD + トレイ）、py_compile、
    unittest 111 件全通過

### Changed
- `voicekey.spec`: **`datas=[('src','src')]` を削除し `noarchive=False` 化**
  （配布物に Python ソース平文が同梱されていた問題の根治）。代わりに
  `collect_submodules('src')` で全モジュールを PYZ アーカイブへ収集
- **Apple Developer Program 不加入を恒久決定（2026-06-12 ユーザー確定）**。
  配布は Apple Development 署名 + 右クリック→開く手順書同梱が恒久形となり、
  Phase 7（Developer ID + 公証切替）を中止。`dmg_readme.txt` の
  「ベータ版のため公証が未対応（正式版で対応予定）」という文言を
  「この警告は配布方式によるもので、アプリ自体の安全性に問題はない。初回に
  一度開けば以後はダブルクリックで起動できる」に変更（HANDOFF.md も同時更新）

### Fixed
- 設定画面が DIST ビルドで起動時クラッシュするバグ（API キータブを作らないのに
  `_load_current_settings` が `_api_key_status` を参照していた。オフスクリーンスモークで発見し、
  タブ生成前の空辞書初期化で修正）
- 設定画面の一般タブに横スクロールバーが常駐するバグ（自動検出ボタン追加でデバイス行が
  窓幅 560px を超過。コンボを最小 200px + 伸縮に変更し、タブのスクロールを縦専用化）
- リアルタイムストリーミングが「表示されない」とき理由が分からない問題（Windows 版）。
  動かない構成（websockets 未導入 / Deepgram キー未設定 / どちらのホットキーも
  バックエンドが Deepgram でない）では警告ログだけ残して REST へ無言フォールバック
  していたため、設定画面のチェックボックス直下に阻害要因を診断表示するようにした。
  バックエンド変更・キー保存/削除・チェック切替で即時更新される。
  検証: チェーン全段の再現スクリプト（偽 WebSocket → 受信スレッド → シグナル →
  HUD 字幕描画）でコード経路の健全性を確認 + 診断ラベル 4 シナリオのオフスクリーン
  テスト + unittest 111 件全通過。
  注: 既定構成はホットキー 1=OpenAI / 2=Groq のため、チェックを入れても Deepgram
  バックエンドに変えない限りストリーミングは発動しない（これが診断表示で分かるようになった）
- マイク自動検出が「検出中…」のまま固まりうる問題（Windows 版・バグレビューで発見）。
  `mic_auto_detect.py` の検出後処理がストリーム破棄 → スコア集計の順だったため、
  WASAPI 排他デバイス等で `stop()`/`close()` がハングすると結果通知（on_done → UI 復帰）
  まで道連れになっていた。スコア確定と best 決定を先に行い、ストリーム破棄は
  別デーモンスレッドへ分離（ハングしても結果には影響しない）。unittest 111 件全通過

## [Unreleased] - 2026-06-10 (voicekey for Mac)

### Added
- **macOS ネイティブ版 `macos/` を新規作成（Swift / SwiftUI）**
  - 方針転換: 「voicekey for Mac（Swift ネイティブ）」と「voicekey for Windows（既存 Python 版がベース）」をそれぞれ最適な技術で開発する。本リポジトリはモノレポ構成とする
  - メニューバー常駐（`MenuBarExtra`、Dock 非表示）。アイコン色で状態表示（待機=テンプレート / 録音=赤 / 自動送信録音=紫 / 変換中=オレンジ）
  - **HotkeyMonitor**: CGEventTap（listen-only）によるグローバルキー監視。修飾キーの左右をデバイス依存ビットで厳密判定し、`tapDisabledByTimeout` 受信時は即時再有効化（pynput で起きていた「ホットキー永久無反応」を OS レベルで根治）
  - **AudioRecorder**: AVAudioEngine 録音 + 16kHz モノラル変換。エンジン操作は専用シリアルキューで直列化
  - **VoiceActivity**: 音量正規化（ゲイン上限 +20dB）+ Apple SoundAnalysis のオンデバイス ML 分類器による発話判定（フォールバックはエネルギーベース）+ 無音トリミング
  - **Transcriber**: OpenAI / Groq REST（WAV multipart、URLSession）。録音開始時の TLS プリウォーム付き
  - **Keychain**: API キー保存。サービス名は Python 版（keyring）と互換で、保存済みキーをそのまま読める
  - **Paster**: クリップボード貼り付け + 元内容の自動復元 + ダブルタップ自動 Enter
  - **HUD**: 録音中のみ画面下部中央に音声レベル連動の波形ピル（NSPanel、クリック透過、フルスクリーン上にも表示）。変換中スピナー / エラー・無音通知（2 秒）
  - **設定ウィンドウ**: 一般（言語・VAD・HUD・自動 Enter 遅延・ログイン時起動）/ ホットキー 1・2（キャプチャ式レコーダー・モード・バックエンド・モデル・プロンプト）/ API キー
  - **AppController**: 文字起こしパイプラインの直列チェーン化（録音順の挿入保証）、録音 300 秒上限の保険、起動時の権限チェック（マイク・入力監視・アクセシビリティ）とシステム設定への誘導
  - ビルド: SwiftPM + `scripts/build_app.sh`（.app バンドル組み立て + ad-hoc 署名）。`swift build` 警告ゼロ、起動スモークテスト済み

### Added (2026-06-10 追記)
- **ElevenLabs / Deepgram バックエンドを追加（Mac 版）**
  - ElevenLabs Scribe（`scribe_v1` / `scribe_v1_experimental`）: multipart + `xi-api-key` 認証。音声イベントタグ（笑い声など）は音声入力に不要なため無効化
  - Deepgram（`nova-2` 既定 / `nova-3`）: WAV 生バイト POST + `Token` 認証。`smart_format` 有効。言語未指定時は自動判定、nova-3 + 非英語は多言語モードに自動切替
  - `Transcriber` をバックエンド別のリクエスト構築/応答解析に再構成（`MultipartForm` ヘルパー新設）。Keychain サービス名は Python 版と互換（`voicekey.ElevenLabs` / `voicekey.Deepgram`）。設定 UI の API キータブ・バックエンド/モデル選択は全 4 バックエンド対応
- **ログイン時自動起動（Mac 版）**
  - 初回起動時に `SMAppService.mainApp.register()` を自動実行（Mac を開けば voicekey が必ず起動する）。設定画面の「ログイン時に起動」トグルでオフにすれば再登録しない
- **文字起こしベンチマーク基盤 `benchmark/` を新規追加**
  - 同一の日本語音声（短文 6.8s / 長文 41.7s、`say` で合成）を 4 バックエンドの各モデルに送り、レイテンシと CER（文字誤り率）を測定する `run_benchmark.py`
  - API キーは環境変数 / `.env` → Keychain（アプリと共用）の順で取得（中身は非表示）。`make_audio.sh` で音声生成、原稿 `.txt` が CER 採点の正解を兼ねる
  - 初回計測（OpenAI / Groq / ElevenLabs、Deepgram はキー待ち）: ElevenLabs scribe_v1 が最高精度（CER 0.0%/0.4%）だが長文 3.5s と低速、Groq whisper-large-v3-turbo が最速（385ms/742ms）で精度も良好（2.7%/5.4%）、OpenAI gpt-4o-transcribe は中速・長文 CER 7.6%

### Added (2026-06-10 追記 3)
- **各社の実在モデルを API から取得する `benchmark/list_models.py` を追加**
  - 推測でなく `/models` エンドポイントから最新モデルを確認。OpenAI は `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` / `gpt-4o-transcribe-diarize` / `gpt-realtime-whisper` / `whisper-1`、Deepgram は `nova-2` / `nova-3` を確認。ElevenLabs STT は `scribe_v1` / `scribe_v1_experimental` のみ（`/v1/models` は TTS 用で STT v2 は存在しない）
- **バッチ比較を全 4 社・全モデルに拡充（`run_benchmark.py`）。Deepgram 込みの確定計測**
  - レイテンシ（最速値）/ CER（短文・長文）:
    - Groq turbo: 330ms / 695ms、CER 2.7% / 5.4%（REST 最速）
    - Deepgram nova-3: 1084ms / 1475ms、CER 0.0% / 4.0%（長文でもレイテンシが伸びない・精度安定）
    - ElevenLabs scribe_v1: 1106ms / 3682ms、CER 0.0% / 0.4%（短文・長文とも最高精度だが長文は低速）
    - OpenAI gpt-4o-mini-transcribe: 861ms / 1937ms、CER 2.7% / 8.0%
  - 注意点: Deepgram は日本語出力に単語間スペースを挿入する（CER は空白除去後のため低いが、貼り付け時はスペース除去が必要）。`gpt-4o-transcribe-diarize` は長文で `chunking_strategy` 必須エラー（話者分離モデルはディクテーション用途では不適）
- **リアルタイムストリーミング速度測定 `benchmark/stream_benchmark.py` を追加（WebSocket）**
  - 同一音声を 1 倍速で WS 送信し、TTFB（喋り出して最初の字が出るまで）と確定レイテンシ（送信完了＝ホットキーを離した相当から最終確定まで）を測定
  - OpenAI Realtime は GA 仕様に対応（`session.update` で `session.type="transcription"`、`OpenAI-Beta` ヘッダ廃止）。**入力 PCM は 24kHz 以上必須**のため 16kHz テスト音声を `audioop.ratecv` で 24kHz にアップサンプルして送信
  - 結果（TTFB / 確定 / CER、短文・長文）:
    - Deepgram nova-3: 1049ms / **61ms** / 0.0%（短）、1089ms / **125ms** / 1.3%（長）。真のストリーミング（発話中に逐次確定）で離した瞬間ほぼ確定。最良
    - OpenAI gpt-realtime-whisper: 1051ms / 951ms / 0.0%（短）、1192ms / 668ms / 2.7%（長）。真のストリーミングだが確定は Deepgram より遅い
    - OpenAI gpt-4o-transcribe / mini: TTFB が音声長と同じ（発話中は字が出ず commit 後に一括）。確定は離してから 0.4〜2.8s。ライブ字幕は不可
  - デバッグ用 `benchmark/debug_openai_rt.py`（OpenAI Realtime の生イベント確認）も追加
  - 結論: HUD にライブ表示するなら Deepgram nova-3 が最適（確定 60〜170ms・精度安定・多言語）。OpenAI で揃えるなら gpt-realtime-whisper。ホールド入力で離してから一括でよいなら REST の Groq turbo が最速

### Added (2026-06-10 追記 4)
- **アプリ本体にリアルタイムストリーミング + ライブ字幕を実装（Mac 版）**
  - 新規 `StreamingTranscriber.swift`: Deepgram WebSocket（`URLSessionWebSocketTask`、外部依存なし）。録音中の 16kHz PCM を逐次送信し、暫定（interim）/確定（is_final）を受信
  - `AudioRecorder`: 逐次チャンク通知 `chunkHandler` を追加。全バッファ蓄積とは独立して送るため、ストリーミングが失敗しても同じ録音バッファで REST にフォールバックできる
  - `AppController`: backend=Deepgram かつストリーミング有効のとき WS 経路。ホットキーを離した瞬間に確定テキストを貼り付け、空・失敗時は REST フォールバック。録音破棄時は `cancel()`
  - HUD: 録音中に**ライブ字幕**を表示（発話しながら文字が伸び、最新の語尾が見えるよう頭を省略表示）。HUD 幅を 460pt に拡張（ピル自体は内容に追従）
  - 日本語スペース除去: Deepgram は日本語に単語間スペースを挿入するため、「前後どちらかが CJK 文字」のスペースのみ除去（英単語間スペースは維持）
  - 設定の一般タブに「リアルタイムストリーミング（Deepgram）」トグルを追加（既定 ON）
  - 既定モデル更新: Deepgram を **nova-3** に（ストリーミング最良）、ElevenLabs に **scribe_v2** を追加
- **ベンチに最新モデルを追加（検索＋API 実測で見落としを洗い出し）**
  - ElevenLabs **Scribe v2**（バッチ）: 短文 CER 0.0%（1255ms）だが**長文は 5.8% と scribe_v1 の 0.4% より精度後退**。利用可能 STT は scribe_v1 / scribe_v1_experimental / scribe_v2 の 3 つ（`probe_stt_models.py` で確定）
  - Deepgram **Flux**（`flux-general-multi`、会話向け新モデル、`/v2/listen`）: TTFB 885〜1107ms、確定 407〜546ms、CER 0.0%/2.7%。ターン検出ぶん確定が遅くホールド入力には nova-3 が上
  - ElevenLabs **Scribe v2 Realtime**（`/v1/speech-to-text/realtime` WebSocket、`scribe_v2_realtime`）: 短文 確定 266ms/CER 0.0% だが TTFB ~2.2s と高め、長文で確定取得に失敗（要調整）。公称 150ms は本計測では未再現
  - `debug_realtime_new.py` で Flux / ElevenLabs Realtime の生プロトコル（TurnInfo/event、partial/committed_transcript）を確定してから実装
  - **ストリーミング最終結論**: 離して→確定が最速・最精度なのは **Deepgram nova-3（確定 94〜109ms、CER 0.0%/1.3%）**。ライブ字幕の既定として最適。OpenAI 統一なら gpt-realtime-whisper（確定 ~800ms）。Flux は会話エージェント向け、ElevenLabs Realtime は現状 nova-3 に及ばず
  - 新規キーが要る有力候補（未計測・要サインアップ）: Soniox（日本語 WER 8.7% 主張・$0.12/h）、Speechmatics（月 40h 無料）、Mistral Voxtral Realtime（$0.006/min）

### Added (2026-06-10 追記 5) — Windows 版を Mac 版と同等まで刷新
- **ElevenLabs / Deepgram バックエンドを追加（Windows/Python 版、全 4 バックエンド対応）**
  - `TranscriptionBackend` 列挙型に `ELEVENLABS` / `DEEPGRAM` を追加（従来は groq/openai のみ）
  - `ElevenLabsTranscriber`（REST、`scribe_v1` 既定）: `xi-api-key` 認証 + multipart（`model_id` / `language_code`）。応答 JSON の `text` を採用。実 API で短文 CER 0.0% を確認
  - `DeepgramTranscriber`（REST、`nova-3` 既定）: `Token` 認証 + WAV 生バイト POST、`punctuate` / `smart_format` 有効。nova-3 は日本語単言語非対応のため `language=multi` に自動切替。日本語の単語間スペースを除去。実 API で短文 CER 0.0% を確認
  - `ApiTranscriber` 基底に `_auth_headers()` / `_raise_for_status()` / `_post()` を抽出し、4 バックエンドで共通化（重複削減）。Keychain サービス名は Mac 版と互換（`voicekey.Deepgram` 追加）
- **Deepgram リアルタイムストリーミング + HUD ライブ字幕を実装（Mac 版と同挙動）**
  - 新規 `src/core/streaming_transcriber.py`: Deepgram WebSocket（`websockets` 同期クライアント、受信は専用スレッド。アプリのスレッドベース設計に合わせ asyncio 不使用）。Mac 版 `StreamingTranscriber.swift` の移植
  - `AudioRecorder` に `chunk_callback` を追加。REST 用バッファ（`audio_q`）とは独立に録音中の生 PCM を逐次送出。ストリーミング失敗時は同じ録音バッファで REST に自動フォールバック
  - `app.py`: backend=Deepgram かつストリーミング有効のとき WS 経路。離した瞬間に確定テキストを貼り付け、空/失敗時は REST フォールバック。設定ホットリロード・ハング復旧・終了時に接続を確実に破棄
  - 接続確立前に `finish()` が呼ばれた短い発話向けに接続猶予（最大 1 秒）を実装し、取りこぼしを防止
  - 実 API 結合テスト: 短文はリアルタイム送信で完全一致（離して→確定 1.4s）、長文も全文取得を確認
- **録音中 HUD を新規実装（`src/ui/hud.py`、UI 刷新）**
  - 画面下部中央の小型ピル。録音中は音声レベル連動の波形バー、ストリーミング時はライブ字幕（頭省略で最新の語尾を表示）、変換中は「変換中…」、通知は 2 秒表示
  - **フォーカスを絶対に奪わない**設計（`WindowDoesNotAcceptFocus` + `WA_ShowWithoutActivating` + 入力透過）。貼り付け先ウィンドウのフォーカスを維持。タスクバー非登録
  - これまで表示先のなかった `notice` シグナル（エラー・無音検出）も HUD で可視化
- **設定 UI を全 4 バックエンド対応に拡張（`settings_window.py`）**
  - バックエンド選択に elevenlabs / deepgram を追加。モデル一覧をバックエンド別マップ（`_BACKEND_MODELS`）で差し替え、保存済みモデルが非対応なら既定（先頭）へ
  - Advanced タブに「リアルタイムストリーミング（Deepgram）」「録音中の HUD を表示」トグルを追加（保存・ロード対応）
- **既定値 / 依存 / パッケージング**
  - `constants.py`: `streaming_enabled` / `hud_enabled`（既定 ON）、`default_api_models` を全 4 バックエンド（deepgram=nova-3 / elevenlabs=scribe_v1）に拡張。`config_manager.py` の `API_BACKENDS` を 4 種へ
  - `requirements.txt` に `websockets>=13.0`、`voicekey.spec` に `collect_submodules('websockets')`（遅延 import のため明示収集）
- **ユニットテスト基盤を新規追加（`tests/`、これまで皆無）**
  - text_utils（CJK スペース除去）/ streaming_transcriber（メッセージ解析・PCM 変換・接続前 finish）/ api_transcriber（認証ヘッダー・言語解決・ステータス検査）/ config_manager（マイグレーション・正規化・既定）/ audio_utils（WAV ヘッダ・クリップ）の 38 テスト。ネットワーク/実機非依存で全パス

### Technical Details (追記 5)
- **types.py**: `TranscriptionBackend` に ELEVENLABS/DEEPGRAM、`TranscriptionTask` に `streamer` フィールド
- **app.py**: `_BACKEND_CLASSES` マップ、`interim_text` シグナル、`_active_streamer` 状態、`_insert_and_enter()` ヘルパー、ストリーミング優先＋REST フォールバックの `_process_task`
- **検証**: 全変更ファイル `py_compile` OK、オフスクリーン結合スモーク OK、実 Deepgram/ElevenLabs API で REST・ストリーミングとも一致、ユニットテスト 38 件パス。Windows GUI/ホットキー/トレイ/マイクの実機確認はユーザー側で実施（開発は macOS のため）

### Added (2026-06-11 追記 6) — 入力デバイス選択（Mac）とログイン時起動（Windows）
両プラットフォームに「相手側が既に持っていた機能」を追加し、parity を揃えた。

- **Mac 版に入力デバイス選択を追加**（Windows 版は既存）
  - Core Audio (HAL) で入力チャンネルを持つマイクを列挙する `AudioDevices`（新規）を追加
  - 設定の「一般」タブに入力デバイスのピッカーと更新ボタンを追加（「システム既定」+ 接続中のマイク。未接続の保存済みデバイスも選択を保持して表示）
  - 選択は安定した UID で永続化し（`AudioDeviceID` は再接続で変わるため）、録音開始のたびに `AVAudioEngine` の inputNode へ `setDeviceID` で適用
- **Windows 版にログイン時自動起動を追加**（Mac 版は既存 = SMAppService）
  - レジストリ Run キー（`HKCU\...\CurrentVersion\Run`）で管理する `autostart`（新規）を追加。`sys.platform` ガードで非 Windows では安全に no-op
  - 設定の「Advanced」に「ログイン時に起動」チェックボックスを追加（非 Windows では無効化＋注記）。状態はレジストリが真実なので settings.yaml には保存しない

### Technical Details (追記 6)
- **macos/Core/AudioDevices.swift**（新規）: `inputDevices()` / `deviceID(forUID:)`。`kAudioHardwarePropertyDevices` 列挙 + `kAudioDevicePropertyStreamConfiguration`（入力スコープ）でチャンネル判定
- **macos/Core/AudioRecorder.swift**: `inputDeviceUID` を追加し `start()` 内で `input.auAudioUnit.setDeviceID()` を実行
- **macos/Config/AppConfig.swift**: `@Published inputDeviceUID` を UserDefaults に永続化
- **macos/UI/SettingsView.swift** / **AppController.swift**: ピッカー追加、録音開始前に `recorder.inputDeviceUID = config.inputDeviceUID`
- **src/utils/autostart.py**（新規）: `is_supported()` / `is_enabled()` / `set_enabled()` / `_launch_command()`（凍結 exe か pythonw+run.py を引用符付きで組み立て）
- **src/ui/settings_window.py**: `_autostart_check` 追加（load=レジストリ実状態、save=`set_enabled`）
- **検証**: Mac `swift build` 成功、Windows `py_compile` OK・ユニットテスト 41 件パス（autostart 3 件追加）・オフスクリーンで設定ウィンドウ構築と非対応時の無効化を確認。Windows 実機でのレジストリ登録・自動起動はユーザー側で確認（開発は macOS のため）

### Added (2026-06-11 追記 7) — LLM テキスト自動整形（Mac / Windows）
文字起こし確定テキストを貼り付け直前に Groq の高速 LLM（既定 `llama-3.1-8b-instant`、Chat Completions）で 1 回整形する機能。主流ディクテーションアプリ（Wispr Flow / Superwhisper 等）と同等の後処理。

- **ホットキーごとにオン/オフ**（既定オフ）。片方は raw 高速・もう片方は整形済み、という使い分けができる
- **整形モード 6 種**（識別子・プロンプト文言は Mac / Windows で完全一致）: 自動クリーン（フィラー除去・句読点整形）/ 箇条書き / 丁寧（敬語）/ カジュアル / メール調 / カスタム（自由プロンプト。空なら自動クリーンにフォールバック）
- **発話を絶対に失わない**: 空入力は API 非呼出、キー未設定・タイムアウト（10 秒上限）・HTTP 非 200・応答不正・空応答・あらゆる例外は警告ログ + 原文をそのまま貼り付け。整形失敗で例外が貼り付け経路へ漏れることはない
- 整形モデルは設定（一般 / Advanced）で変更可能。ストリーミング確定・REST の両経路に適用

### Added (2026-06-11 追記 8) — 整形モード「おまかせ（自動判断）」と UI 改善
- **「おまかせ（自動判断）」モードを追加し既定に**（Mac / Windows、識別子 `auto`）
  - ユーザーが毎回モードを選ばなくても、LLM がテキストの内容から整形方法を自動判断する（フィラー除去は常時。列挙・手順なら箇条書き、それ以外は自然な文章。文体は元の発言を維持）
  - 自動判断の指示（システムプロンプト）は設定で自由に編集可能（Mac: 一般タブ「おまかせ整形の指示」+ 既定に戻すボタン / Windows: Advanced「Auto Format Prompt」）。空欄なら既定の指示を使用
- **整形モデルを自由入力からリスト選択に変更**（Mac / Windows 共通リスト）: llama-3.1-8b-instant（既定・最速）/ llama-3.3-70b-versatile / openai/gpt-oss-20b / openai/gpt-oss-120b。保存済みのリスト外モデルも選択を保持
- Technical: Mac `FormatMode.auto` + `defaultAutoPromptBody` + `TextFormatter.knownModels`、`ConfigStore.autoFormatPrompt`（UserDefaults 永続化）。Windows `DEFAULT_AUTO_PROMPT` / `KNOWN_FORMAT_MODELS`、`format_auto_prompt` 設定（空 = 既定で保存し既定文の将来更新に追従）。`build_system_prompt` / `format_text` に `auto_prompt` 引数追加。テスト 5 件追加（全 58 件パス）。Mac は実機でモード選択 UI・一般タブの編集欄・既存スロット設定の温存を確認

### Added (2026-06-11 追記 9) — モデル選択リストに「（推奨）」表記
- **音声（文字起こし）・文字（整形）両方のモデル選択リストで、推奨モデルの表示名に「（推奨）」を付けた**（Mac / Windows）
  - 表示ラベルだけの変更で、保存値・API へ送る値はモデル識別子のまま（Mac: Picker tag / Windows: QComboBox userData）
  - 推奨 = ベンチ実測 2026-06-10 に基づく各バックエンドの既定: OpenAI `gpt-4o-mini-transcribe` / Groq `whisper-large-v3-turbo` / ElevenLabs `scribe_v1`（日本語最高精度。v2 は長文後退）/ Deepgram `nova-3` / 整形 `llama-3.1-8b-instant`（速度テスト実行待ちの暫定）
  - Mac の knownModels の並びを「先頭＝既定＝推奨」に統一（OpenAI を mini 先頭、ElevenLabs を scribe_v1 先頭へ。Windows と同順序に）。保存済みの選択はそのまま温存される
- **整形モデル速度ベンチ `benchmark/format_speed_bench.py` を追加**: 全整形モデルに同一リクエスト（おまかせプロンプト + フィラー多め日本語 133 文字 × 3 回）を送りレイテンシのみ計測。キーは環境変数 → Keychain の順で取得し表示しない（.env は読まない）

### Changed (2026-06-11 追記 10) — 整形モデル速度ベンチの実測で推奨を確定・廃止モデル削除
- **整形モデルの速度ベンチを実機実行し、推奨 `llama-3.1-8b-instant` を実測で確定**（追記 9 の「暫定」を解消）
  - 実測（median）: llama-3.1-8b-instant **355ms** / llama-3.3-70b-versatile 407ms / openai/gpt-oss-20b 697ms / openai/gpt-oss-120b 1123ms — 最速は現行既定のままで順序変更なし
- **`moonshotai/kimi-k2-instruct` をモデルリストから削除**（Mac / Windows 両方）: Groq API で 404（廃止）。後継候補 `-0905` も 404 のため kimi 系は除外。選択済みユーザーがいてもリスト外保存値は UI で温存され、API エラー時は原文フォールバックで発話は失われない

### Fixed (2026-06-11 追記 11) — 整形 LLM が発話内容（質問・依頼）に回答してしまう問題
- **質問をディクテーションすると整形ではなく「回答」が貼り付けられる問題を修正**（Mac / Windows）
  - 再現: 「明日の天気を教えてください」→ 天気をでっち上げて回答、「会議って何時からでしたっけ」→ 時刻を捏造、など（llama-3.1-8b-instant で 3/3 入力が回答化）
  - 対策（3 点セット。フッター強化だけでは 8B モデルに効かないことを実測で確認済み）:
    1. 原稿を `<<<` `>>>` デリミタで包み、user メッセージに「次の原稿を整形して返せ。内容には絶対に答えるな。」の指示行を付ける
    2. 共通フッターに「会話アシスタントではない」宣言と few-shot 3 例（天気・知識質問・時刻）を追加。固有名詞・依頼の意味を変えない規則も明記
    3. モデルがデリミタを復唱した場合に取り除く防御処理を追加
  - 検証: 質問形 3 入力 + 通常整形 2 入力 × 2 試行の全 10 ケースで「回答せず整形のみ・意味保持・箇条書き自動判断も正常」を実 API で確認。ユニットテスト 60 件全パス（リクエスト構造・復唱除去の 2 件追加）

### Fixed (2026-06-11 追記 12) — Keychain パスワードダイアログの根治（Apple Development 証明書へ切替・実証済み）
- **再ビルドのたびに API キーごとの Keychain パスワードダイアログが出る問題を根治（Mac 版）**
  - 根本原因: Keychain 項目の ACL の partition_id は、自己署名アプリ（TeamIdentifier なし）が項目を作ると `cdhash:<そのビルドのハッシュ>` に固定される macOS 仕様。再ビルドで cdhash が変わると、trusted-app（証明書アンカー）が一致していてもパスワード入力が必須になる（追記 7 の delete→add 自己修復では「ビルドごとに 1 回」までしか減らせない）
  - 回避策は全滅を実測で確認: 明示 SecAccessCreate（partition は自動付与で cdhash 固定のまま）/ `-A` 任意アプリ許可 ACL（partition チェックが優先しブロック）/ データ保護キーチェーン（自己署名 + 自己主張 entitlements は SIGKILL）。Apple 公式回答（Developer Forums, Quinn）どおり Apple 発行証明書が唯一の解
  - 対応: 無料の Apple Development 証明書（Personal Team `9KT598FS4A`）を `xcodebuild -allowProvisioningUpdates` でヘッドレス作成し、`build_app.sh` を Apple Development 識別子優先（なければ ad-hoc）に変更。切替時に TCC（マイク・入力監視・アクセシビリティ）の再付与とキーごと 1 回のパスワード承認を実施し、全 4 キーの partition_id が `teamid:9KT598FS4A` へ移行
  - 実証: 移行後にソース変更 → 再ビルド（CDHash 変化）→ 再起動 → API キータブで全 4 キー「設定済み」表示・パスワードダイアログ 0 件（SecurityAgent ウィンドウ 0）を確認。partition は teamid 基準のため、今後は再ビルドでも年 1 回の証明書更新でもダイアログは出ない

### Fixed (2026-06-11 追記 13) — 整形で「やることは…」のような導入文が出力から消える問題
- **列挙を話すと箇条書きだけが貼り付き、導入文が捨てられる問題を修正**（Mac / Windows）
  - 再現: 「今日やることは二つあります。一つ目はご飯を食べる。二つ目は顔を洗う」→「- ご飯を食べる / - 顔を洗う」だけが出力され、何のリストか分からない
  - 対策: auto / bullets のプロンプトに「導入・前置きの文は削除せず、箇条書きの前の行にそのまま残す」規則と具体例（「持ち物は三つです。えーと、財布と、鍵と、あと定期」→ 導入文 + 箇条書き）を追加。共通フッターにも「フィラー語以外の情報（導入・前置きの文を含む）を省略しない」を明記
  - **Mac 版の「既定プロンプト改善が反映されない」隠れバグも修正**: 既定の auto プロンプトが UserDefaults にそのまま永続化されており、アプリ更新で既定文を改善しても古い文が使われ続けていた。Windows 版と同じ「既定文と同じ内容は保存しない」方式に変更し、未編集ユーザーには常に最新の既定文が使われるようにした（保存済みの旧既定文は削除済み）
  - 検証: 実 API（llama-3.1-8b-instant）で「導入文 + 列挙（auto / bullets）」「緩い導入（買い物リストなんだけど）」「質問に答えない」「通常文を箇条書きにしない」の 5 ケース × 2 試行すべて期待どおりを確認。ユニットテスト 60 件全パス

### Changed (2026-06-11 追記 14) — Windows 版 UI を Mac 版と同等に刷新
- **設定ウィンドウを Mac 版と同じ 4 タブ構成（一般 / ホットキー 1 / ホットキー 2 / API キー）に再構成**（`settings_window.py`）
  - サイドバー 2 ページ（General / Advanced）構成を廃止し、QTabWidget でタブ化。全ラベル・補足説明文を Mac 版 SettingsView.swift と同一の日本語文言に統一（「押している間 / トグル」「無音を自動スキップ（VAD）」「専門用語や固有名詞のヒントを入力」等）
  - **API キータブを新設**: 4 バックエンド（OpenAI / Groq / ElevenLabs / Deepgram）のキーを 1 か所で保存・削除。「設定済み / 未設定」を色付きで表示し、従来ホットキー設定の中に埋もれていたスロット別キー入力を廃止
  - 言語は自由入力から「日本語 / 英語 / 自動判定」の選択式に変更（空文字 = API 側の自動判定。全バックエンド対応済みの既存挙動）
  - 各タブは縦スクロール対応。保存値がリスト外でも選択を保持する既存ポリシーは言語・整形モデルにも適用
  - Windows 版固有設定（VAD 最小無音時間・音量正規化・ダークモード切替・自動 Enter スライダー）は一般タブに統合して維持
- **HUD を Mac 版 Hud.swift と同寸・同表現に刷新**（`hud.py`）
  - 460×56 / 波形バー 24 本（旧 360×48 / 20 本）。ピルが Mac 版カプセル同様に内容の幅へ縮む
  - 変換中の回転スピナー（約 30fps、表示中のみタイマー駆動）と、自動 Enter 録音時の紫 ⏎ バッジを追加
- **システムトレイの表記を voicekey に統一**（`system_tray.py`）: ツールチップの旧称 SuperWhisper を「voicekey - 待機中 / 録音中 / 録音中（自動 Enter）/ 変換中」へ、メニューを「設定… / 強制リセット（フリーズ復帰）/ 終了」の日本語表記（Mac 版メニューバーと同文言）へ変更
- **Technical Details**: `styles.py` に QTabWidget/QTabBar・QPlainTextEdit・QScrollArea スタイルを追加（不要になったサイドバー用 QListWidget スタイルは削除）。設定値の保存形式（settings.yaml のキー・値）は不変のため後方互換。検証: ユニットテスト全パス + offscreen スモーク（4 タブ構築・バックエンド切替でモデル候補差し替え・保存 dict 整合・HUD 全状態の描画）+ スクリーンショット目視確認

### Changed (2026-06-11 追記 15) — 整形モード撤去・プロンプト全面刷新（すべておまかせに一本化）
- **整形モード選択（自動クリーン / 箇条書き / 丁寧 / カジュアル / メール調 / カスタム / おまかせ）を廃止**（Mac / Windows）
  - 整形は常に「LLM の自動判断」一本に。スロット設定はオン/オフのトグルだけになり、モード Picker とスロット別カスタムプロンプト欄を削除（整形指示の編集は一般タブ「整形の指示」に集約、既定に戻すボタンは維持）
  - 設定の後方互換: 保存キー（Windows `format_auto_prompt` / Mac `autoFormatPrompt`・`formatEnabled`）は維持。旧 `format_mode` / `format_custom_prompt`（Mac: `formatMode` / `formatCustomPrompt`）は読み捨てされるだけで設定リセットは起きない
- **既定プロンプトを市販音声入力アプリの調査に基づき全面書き直し**（約 870 → 約 490 文字に半減。プロンプト長は prefill 時間に直結するため速度改善）
  - 調査対象: Wispr Flow / superwhisper / VoiceInk / Aqua Voice / Dragon 等の整形機能（フィラー除去・自己訂正の解決・句読点と段落・リスト自動整形・数字や日付の表記正規化・文体維持・質問に答えないガード）
  - 新プロンプトの構成: フィラーと無意味な繰り返しの削除 / 言い直しは最終発言のみ残す（例付き）/ 句読点・改行・段落と数字・日付・時刻の表記 / 列挙・手順はリスト化してよい（緩い指定）+ 導入文は残す / 文体維持・要約禁止
  - 共通フッター（質問に回答しない・`<<< >>>` デリミタ・出力形式）は文言を圧縮しつつ全ガードを維持（追記 11・13 の回帰なし）
- **Technical Details**: `text_formatter.py` / `TextFormatter.swift` から `_MODE_PROMPTS` / `FormatMode` を削除し `DEFAULT_FORMAT_PROMPT`（Mac: `TextFormatter.defaultPrompt`）に統一。`build_system_prompt(prompt)` / `format_text(text, model, prompt)` にシグネチャ変更。`HotkeySlot` / `HotkeySlotConfig` / `SlotConfig` から mode/custom フィールド撤去。テスト 16 件を新仕様に書き直し全 57 件パス。実 API で「列挙→導入文+リスト」「質問に回答しない」を確認

### Fixed (2026-06-11 追記 16) — 録音開始の高速化とダブルタップ・短音声の取りこぼし修正
- **ダブルタップ（自動 Enter）の 1 打目から録音が途切れないように**（Mac / Windows）
  - 旧挙動: 1 打目の離鍵で録音停止（短すぎて破棄）→ 2 打目で録音を再開、のためタップと同時に話し始めた文頭が欠けていた
  - 新挙動: hold モードで押下から 0.4 秒未満の離鍵では録音を止めず、2 打目を待つ（2 打目が来たらそのまま録音継続 + auto_enter 化、来なければ通常確定）。1 打目のタップ中・タップ間の音声もすべて録音される
  - 通常のホールド入力（0.4 秒以上）は従来どおり離した瞬間に確定（待ち時間の追加なし）
  - 誤タップ（無音の短いタップ）は「音声が検出されませんでした」を出さず静かに破棄
- **短い発話が「音声が検出されませんでした」になる問題を修正**（Mac 版）
  - 原因: SoundAnalysis 分類器の解析窓が 1 秒のため、1 秒未満の音声では分類結果が一度も出ず、その場合に「声なし」と誤判定していた（エネルギー判定へのフォールバックが効いていなかった）
  - 修正: 約 1.2 秒未満の音声は最初からエネルギー判定を使用 + 分類結果ゼロ件は「判定不能」としてエネルギー判定にフォールバック
- **録音開始そのものを高速化**（Mac 版）: 起動時と録音停止直後に CoreAudio 入力ユニットの初期化と `engine.prepare()` を済ませておき、ホットキー押下から実際に音が録れ始めるまでの遅延を最小化（`AudioRecorder.prewarm()` 新設。マイク自体は起動しないため常時録音やインジケータ点灯はない）
- **Technical Details**: AppController に `pendingTapFinish` / `recordingStartedAt`、`finishRecording(quietIfNoSpeech:)`。app.py に `_pending_tap_timer`（threading.Timer、_state_lock 保護）、`TranscriptionTask.quiet_if_no_speech`。`VoiceActivity.SpeechObserver` に `resultCount`。Windows 版 VAD（Silero、32ms フレーム）は短音声に強いため変更なし

### Changed (2026-06-11 追記 17) — 文字起こしまでの処理時間を総合削減（精度は不変）
- **発話間の長い無音を圧縮してから送信**（Mac / Windows、REST 経路のみ）
  - 従来は前後の無音しか切っておらず、話の途中で考え込んだ無音はすべて API に送られていた。各発話区間の前後 250ms の余白を残し、区間間の無音を最大 0.5 秒まで保持して圧縮する（元の無音が約 1 秒以下なら切らない）。長考した分だけアップロードと API 処理が丸ごと縮む
  - ポーズは句読点・文区切りの推定材料になるため 0.5 秒残す設計（完全には消さない）。語頭・語尾は余白で保護。**リアルタイム（Deepgram ストリーミング）経路は対象外**（録音中に逐次送信済みのため圧縮しても速くならない）
- **FLAC ロスレス圧縮でアップロードを約 4 割削減**（Mac 版、全 4 バックエンド）
  - WAV の代わりに FLAC（16bit、量子化は WAV と同一）で送信。実測で WAV 比 61%。可逆圧縮のため精度への影響はゼロ（ラウンドトリップ検証で maxDiff = 量子化誤差 4.6e-5）。エンコード失敗時は WAV に自動フォールバック
  - 実 API 検証: 無音圧縮 + FLAC の音声を 4 社に送信し、Groq CER 2.6% / OpenAI 2.6% / ElevenLabs 2.6% / Deepgram 0.0%（WAV ベースラインと同等、発話 2 区間とも完全保持）
  - Windows 版は標準ライブラリで FLAC を生成できないため見送り（依存追加が必要。実機検証とセットで別途）
- **VAD 自体の処理時間を削減**（精度・判定結果は同一）
  - Mac: SoundAnalysis 分類器に 0.5 秒ずつ流し、speech 検出時点で打ち切る早期終了を追加。判定は「どこかに speech があるか」の OR なので結果は全量解析と同一。実測 6.8s+25s 無音の音声で 33ms（発話が冒頭にあるほど・録音が長いほど効く）
  - Windows: has_speech と speech_bounds が同じフレーム推論を 2 回実行していたのを `analyze()` 1 回に統合（VAD 時間が半減）。無音圧縮も同じ推論結果を共用
- **整形 LLM の接続を使い回し + 録音中に事前確立**（Mac / Windows）
  - Windows: 整形のたびに httpx.post が TCP+TLS を張り直していた（毎回 100〜300ms 上乗せ）のを keep-alive 付き共有クライアントに変更
  - 両 OS: 録音開始時（整形が有効なスロットのみ）に整形 API への接続も温める（文字起こし API の prewarm と同パターン）。停止後の整形リクエストはハンドシェイク済みの接続で即送信される
- **Technical Details**: `VoiceActivity.condense()`（speechBounds を置換・吸収）/ `FlacEncoder.swift`（新規、AVAudioFile + CoreAudio 内蔵エンコーダ）/ `Transcriber.EncodedAudio`（FLAC/WAV の filename・contentType を保持）/ `TextFormatter.prewarm()`。Windows は `SileroVad.analyze()`（has_speech / speech_bounds を置換）/ `text_formatter._get_client()` + `prewarm()`。検証: ユニットテスト 68 件全パス（VAD は実 Silero ONNX で新規 7 件）、Swift 実装は実コードをリンクした検証ハーネスで 11 項目全パス、実 API 4 社で CER 劣化なしを確認、Mac ビルド成功・新ビルド起動確認済み

### Added (2026-06-12 追記 18) — 音声入力履歴（直近 10 件をクリップボードへ再コピー）
- **Mac 版: 音声入力の履歴を最大 10 件保存し、設定画面から再コピーできる「履歴」タブを追加**
  - 文字起こし（整形後）のテキストを貼り付けのたびに自動で履歴へ記録（貼り付け失敗時の救出にもなるよう貼り付け前に記録）。ストリーミング・REST 両経路に対応
  - 設定ウィンドウに「履歴」タブを新設（一般 / ホットキー 1・2 / 履歴 / API キー の 5 タブ構成）。行をクリックでクリップボードにコピーし「コピーしました」を 1.5 秒表示。各行に日時、下部に「履歴を消去」ボタン
  - 履歴は `~/Library/Application Support/voicekey/history.json` に保存（アプリ再起動後も残る・この Mac の外には出ない）。11 件目以降は古いものから自動削除
- **Technical Details (Mac)**: `Core/HistoryStore.swift`（新規、`@MainActor ObservableObject`・iso8601 JSON 永続化・atomic write）、`AppController.history` + 両貼り付け経路で `history.add(output)`、`SettingsView` に `HistoryTab`/`HistoryRow`（行全体クリック領域・lineLimit 2）。検証: swift build / build_app.sh 成功、新ビルド起動、空状態と 3 件表示をスクリーンショットで確認
- **Windows 版: 同等の「履歴」タブを追加（5 タブ構成に）**
  - 貼り付けの単一地点 `_insert_and_enter` で履歴に自動記録（ストリーミング・REST 両経路をカバー）。履歴は settings.yaml と同じディレクトリの `history.json` に保存
  - 履歴タブ: 行クリックで全文をクリップボードにコピーし「コピーしました（n 文字）」を 1.5 秒表示。80 文字超は省略プレビュー（全文はツールチップ）、各行に日時、「履歴を消去」ボタン、空時はプレースホルダー。タブ切替・ウィンドウ表示のたびに最新化
- **Technical Details (Windows)**: `src/core/history.py`（新規、`HistoryStore`・threading.Lock・一時ファイル経由の atomic 置換・壊れた JSON は空で復帰）、`app.py` に `self._history` + `SettingsWindow(history=...)`、`settings_window.py` に `_create_history_tab` / `_refresh_history` / `_copy_history_item` / `_clear_history`、`styles.py` に QListWidget テーマ。検証: ユニットテスト 77 件全パス（履歴 9 件新規）、offscreen スモークでタブ構成・クリック→クリップボード一致・消去・ストアなし時の安全動作を確認

### Fixed (2026-06-12 追記 19) — API キーのパスワード承認ダイアログ再発を根治（自己修復 write を撤去）
- **再発原因**: 読み取り時の「自己修復移行 write」（追記 7）が残っていたこと。起動のたびに鍵項目を delete→add で作り直すため、(a) 他プロセス（テスト用 python の keyring 等）に与えた承認が毎回消えてダイアログが再発、(b) ad-hoc 署名の実行（debug ビルド・検証ハーネス等）が一度でも鍵を読むと項目所有が cdhash 固定に退行し、次の正規ビルドでパスワード要求が再発する退行ベクトルになっていた。Apple Development 証明書移行（追記 12）の実行装置としては役目を終えていた
- **修正**: `Keychain.apiKey()` の自己修復 write を撤去（保存経路 `setApiKey` の delete→add は維持）。承認ダイアログが再発した場合は設定画面からキーを 1 回再保存すれば現アプリ所有で作り直される
- **検証**: 4 項目の partition_id が `[teamid:9KT598FS4A, cdhash:...]` であることを ACL メタデータの per-item 診断で実測（秘密値は読まない）→ 修正版を再ビルド（CDHash 変化）→ 再起動 → API キータブで 4 キーとも「設定済み」表示・ダイアログ 0 件・鍵項目の更新日時が不変（起動毎の作り直しが停止）

### Fixed (2026-06-11 追記 7)
- **API キー使用のたびに Keychain の承認ダイアログが出る問題（Mac 版）**
  - 原因: Python 版 keyring や旧 ad-hoc 署名ビルドが作成した Keychain 項目は ACL 上の所有者が「別アプリ」のため、現在の署名アプリの読み取りで毎回承認を求められていた
  - 修正 1: 保存処理を `SecItemUpdate` から **`SecItemDelete` → `SecItemAdd`** に変更し、項目を常に現アプリが新規作成して所有権を取る
  - 修正 2: 読み取り成功直後に同じ値で書き直す**自己修復移行**を追加（環境変数フォールバック経路では行わない）。既存ユーザーは各キーにつき**次回の読み取りで 1 回だけ承認すれば以後ダイアログが出なくなる**（キーの再入力は不要）

### Technical Details (追記 7)
- **macos/Core/TextFormatter.swift**（新規）: `FormatMode` enum（clean/bullets/polite/casual/email/custom + 日本語ラベル + システムプロンプト）と `TextFormatter`（ephemeral URLSession、リクエスト 10 秒、temperature 0.2、失敗時は原文返しで throws しない）
- **macos/Core/Keychain.swift**: `write()` を delete→add 化、`apiKey(for:)` に自己修復移行を追加
- **macos/Config/AppConfig.swift**: `SlotConfig` に `formatEnabled` / `formatMode` / `formatCustomPrompt`（既定値付き）。手書き `init(from decoder:)` を extension に実装し、既存ユーザーの保存スロットを decodeIfPresent + 既定値で後方互換読み込み（設定リセット防止）。`ConfigStore` に `@Published formatModel`
- **macos/UI/SettingsView.swift**: スロットタブに整形トグル + モード Picker +（custom 時）プロンプト欄、一般タブに整形モデル欄
- **macos/AppController.swift**: ストリーミング / REST 両経路の `Paster.paste` 直前で `formatter.format()` を適用
- **src/core/text_formatter.py**（新規）: `build_system_prompt` / `format_text`（httpx、Keychain → `GROQ_API_KEY` 環境変数の順でキー解決、失敗時原文）
- **src/config/constants.py / types.py**: `format_model`（グローバル）と hotkey1/2 の `format_enabled` / `format_mode` / `format_custom_prompt` を追加
- **src/app.py**: `_maybe_format` ヘルパーを追加し、ストリーミング確定・REST 完了の両経路で `_insert_and_enter` 直前に適用
- **src/ui/settings_window.py**: 各ホットキーに整形チェックボックス + モード Combo +（カスタム時のみ表示の）プロンプト欄、Advanced に Format Model 欄
- **検証**: Mac `swift build` エラー/警告 0 → `build_app.sh` → 実機再起動し、スロットタブの整形 UI（トグル → モード Picker → カスタム欄の段階表示）と既存設定の後方互換読み込み（ホットキー・バックエンドがリセットされないこと）をスクリーンショットで確認。Windows `py_compile` OK・ユニットテスト 53 件（新規 13 件含む）全パス・オフスクリーンで設定 UI 構築を確認。Groq への実呼び出しとKeychain 承認ダイアログ消滅は実ディクテーションでの確認待ち

### Fixed (2026-06-10 追記 2)
- **ホットキーがほとんど反応しない問題（Mac 版）**
  - 原因 1: ウィンドウを 1 つも持たないメニューバーアプリは App Nap の対象になり、イベントタップのコールバックが遅延 → OS にタイムアウト無効化されてホットキーが死ぬ。`beginActivity` と Info.plist `NSAppSleepDisabled` で App Nap を無効化し、タップ監視スレッドの QoS を `.userInteractive` に引き上げ
  - 原因 2: ad-hoc 署名はビルドごとに署名が変わり、入力監視の TCC 許可と不一致になる（タップは作成できるが OS が無効化し続ける）。ビルドスクリプトを自己署名証明書 `voicekey-codesign` があればそれで署名するよう変更（証明書の信頼登録はユーザー操作が必要）
  - 防御策: タップスレッドに 5 秒間隔のウォッチドッグを追加し、無効化通知の取りこぼしでも自動復旧。無効化理由（timeout/userInput）のログも追加
  - **根本原因（確定）**: ad-hoc 署名はビルドごとに署名 ID が変わり、入力監視の TCC 許可と不一致になるため OS がタップを無効化し続けていた（5 秒ごとに発生）。自己署名証明書 `voicekey-codesign` で署名を固定し、`tccutil reset` 後に 1 回だけ許可し直すことで完全解決。再ビルド→再起動でもタップ無効化 0 回・権限再要求なしを実機で確認
  - 署名固定の手順は `macos/README.md` の「署名について」に記載
- **設定画面の API キー・プロンプト入力欄が見えない問題（Mac 版）**
  - グループ化フォーム内の SecureField / TextField は枠が描画されず、プレースホルダがただのテキストに見えて入力欄と認識できなかった。`.textFieldStyle(.roundedBorder)` で明示的に枠付きに変更（API キー 4 欄・プロンプト・自動 Enter 遅延）
  - `fixedSize()` による高さ潰れの可能性も排除し、設定ウィンドウを明示サイズ（480×520）に変更
  - デバッグ用の設定ウィンドウ自動表示フラグ（`VOICEKEY_OPEN_SETTINGS`、一回限り）を追加し、スクリーンショットでの実機検証を可能にした

### Fixed
- **メニューバーアイコンが表示されない問題（Mac 版）**
  - 原因 1: SwiftUI `MenuBarExtra` がラベルの `NSImage` を正しく描画できない（青い円になる）
  - 原因 2: アイテム位置の永続化値（`NSStatusItem Preferred Position`）が不可視領域（ノッチ下・Hidden Bar の隠し領域）を指すと二度と表示されない。ユーザー環境では Hidden Bar が新規アイテムを隠し領域に配置していた
  - 対策: `MenuBarExtra` を廃止し AppKit `NSStatusItem` 直接管理へ書き換え（`VoicekeyApp.swift`）。エントリポイントを SwiftUI App から `NSApplicationDelegate` ベースに変更し、`startup()` をラベルの `.task` から `applicationDidFinishLaunching` へ移動。ドラッグでの取り外しを禁止（`behavior = []`）、設定ウィンドウは `NSHostingController` で自前管理。位置記録を可視領域にリセットして復旧

## [Unreleased] - 2026-06-10

### Changed
- **コア層の全面刷新（大規模バグ調査の結果に基づく Phase 1）**
  - `src/core/audio_recorder.py` 全面書き換え: 永続ストリーム方式へ移行
    - PortAudio の open/close を録音のたびに行わず、ストリームを開いたまま start/stop だけで録音を切り替える（close は 30 秒アイドル時・デバイス変更時・終了時のみ）
    - 専用の AudioControl スレッドが PortAudio 呼び出しをすべて直列実行。公開 API（`start_async` / `stop_async`）は完全ノンブロッキングで、pynput リスナースレッドを一切ブロックしない（macOS の CGEventTap タイムアウトでホットキーが死ぬ問題の根治）
    - `health()` / `recover()` による世代管理ウォッチドッグ復旧を実装。ハングした制御スレッドを見捨てて新世代に切り替え、取得済み音声は救出する
    - 停止時はストリームを閉じずに同期 stop するため、発話末尾の取りこぼしが発生しない
    - HUD 用に約 33ms 間隔の音声レベルコールバックを追加
  - `src/core/vad.py` 全面書き換え: torch + MPS 版 VadFilter を廃止し、onnxruntime (CPU) + numpy のみの `SileroVad` に置換
    - torch のトップレベル import（起動遅延の主因の一つ）を排除。VAD ロード約 88ms・推論 4ms/2 秒音声
    - アプリ全体で 1 インスタンスを共有（旧実装はスロットごとに二重ロード）
    - `speech_bounds()` を新設し、録音前後の無音・ノイズ区間をトリミングして API へ送る量と幻覚を削減
  - `src/core/api_transcriber.py` 新設: OpenAI / Groq トランスクライバを統合
    - openai / groq SDK を廃止し httpx の multipart POST に統一（OpenAI 互換 REST）。SDK の import 時間を排除
    - `prewarm()` で録音開始時に TLS 接続を事前確立し、初回 API 呼び出しの往復を短縮
    - 失敗は "Error:" 文字列ではなく `TranscriptionError` 例外で伝達
    - 旧 `openai_transcriber.py` / `groq_transcriber.py` は削除
  - `src/core/audio_utils.py`: MP3 変換（ffmpeg サブプロセス）を廃止し WAV 専用に簡素化
    - import 時に `ffmpeg -version` を実行していた起動ブロック（最大 5 秒）を排除
    - int16 変換前に `np.clip` を追加し、範囲外サンプルのラップアラウンド（轟音ノイズ化）を防止

### Fixed
- **「ほぼ無音＋ノイズ」録音で全く違う内容が出力される問題（幻覚）への対策**
  - `src/core/audio_preprocess.py:normalize_volume`: ゲイン上限 +20dB を新設。従来は上限がなく、ノイズフロアだけの録音がフルボリュームまで増幅されて API が架空テキストを生成する主要因だった
- **API キーの Keychain 読み出しを毎回行っていた問題**
  - `src/utils/secrets.py`: プロセス内キャッシュを追加（set/delete で無効化）。録音のたびに数十 ms の Keychain アクセスが走らなくなった
  - ElevenLabs 用サービス識別子 `SERVICE_ELEVENLABS` を追加

### Technical Details
- **書き換え**: `src/core/audio_recorder.py`, `src/core/vad.py`, `src/core/audio_utils.py`
- **新規**: `src/core/api_transcriber.py`（`ApiTranscriber` 基底 + `OpenAITranscriber` / `GroqTranscriber`）
- **削除**: `src/core/openai_transcriber.py`, `src/core/groq_transcriber.py`
- **編集**: `src/core/audio_preprocess.py`（`MAX_GAIN_DB`）、`src/utils/secrets.py`（キャッシュ）、`src/core/__init__.py`（エクスポート更新）
- スモークテスト: コア層 import 272ms（torch 削除前は約 1.4 秒）、WAV クリップ・ゲイン上限・VAD 無音/ノイズ判定を確認済み

### Changed (続き: アプリ層)
- **`src/app.py` 全面書き換え（`SuperWhisperApp` → `VoicekeyApp`）**
  - リスナーハンドラを完全ノンブロッキング化（キー集合更新とコマンド投函のみ）。macOS CGEventTap タイムアウトによるホットキー死亡を構造的に防止
  - UI 状態は `_emit_state()` の単一発信点に集約（「アイコンが録音中のまま」の根治）
  - 常駐ワーカー 1 本が 正規化 → VAD ゲート/トリム → API → テキスト挿入 を直列処理
  - 左修飾キーのマッチング修正: macOS の pynput は左修飾キーを汎用名（cmd 等）で報告するため、`<cmd_l>` 設定が一致しなかった問題を `_acceptable_names()` で解消
  - toggle モードも低レベル Listener に統一（GlobalHotKeys 廃止。右修飾キー対応＋エッジ検出でキーリピート誤発火を防止）
  - ウォッチドッグ: PortAudio ハング自動復旧（recover）、録音 300 秒上限、リスナースレッド死活監視
  - 起動時に macOS 権限（アクセシビリティ・入力監視）をチェックし、不足時はダイアログでシステム設定へ誘導
  - ホットリロードでリスナー再起動が不要な設計に変更（ハンドラがイベント時にスロット設定を参照）。退役トランスクライバは 30 秒後に遅延 close
  - dev_mode のタイミングファイル出力・引用符ラップを削除（ログ出力に一本化）
- **`src/core/input_handler.py`**: 貼り付け後にユーザーの元クリップボード内容を復元（テキストのみ）

### Technical Details (続き)
- **書き換え**: `src/app.py`
- **編集**: `src/core/input_handler.py`（クリップボード復元）、`src/platform/base.py` / `src/platform/macos/adapter.py`（`check_input_permissions` / `open_permission_settings` 追加）、`src/main.py` / `src/__init__.py`（クラス名変更追従）

## [Unreleased] - 2026-06-01

### Fixed
- **macOS フリーズ後に「毎回 2 秒待たされる」現象を根絶し、録音停止を完全ノンブロッキング化**
  - `src/core/audio_recorder.py:_cleanup_stream`: PortAudio の `stream.stop()/close()` を daemon スレッドへ投げっぱなしにし、呼び出し元は **一切待たない（join しない）** よう変更。従来は `join(timeout=2.0)` で待っており、一度 close がハングするとその後の録音停止が毎回最大 2 秒ブロックしていた
  - 未使用化した `_CLEANUP_TIMEOUT_SEC` 定数を削除
  - トレードオフ: close がハングしたストリームは OS のマイクを掴んだまま残る（マイクインジケーターが消えない）が、録音・文字入力動作は一切遅延しない。残ったインジケーターはアプリ再起動で解消する
- **ダブルタップ連打時に録音を取りこぼす（samples=0）レースを解消**
  - `src/app.py:stop_and_transcribe`: `_recorder.stop()` を `_finalize_recording_async`（別スレッド）から呼んでいたため、停止完了前に次の録音 start が割り込み、状態がズレて空録音になることがあった。close を待たない設計になったため `stop()` を `_recording_lock` 内で同期実行して音声をその場で確定し、`_active_slot` も即クリアするよう変更
  - `src/app.py:_finalize_recording_async`: シグネチャを変更し確定済み `audio_data` を引数で受け取る（内部での `_recorder.stop()` 呼び出しを削除）。担当は音量正規化とキュー投入のみ
  - `src/app.py:start_recording`: `self._recorder.start()` の戻り値を確認し、開始に成功してから `_is_recording` を立てるよう変更。app 側と recorder 側の状態がズレて「録音中のつもりだが録れていない」ゾンビ状態になるのを防止

### Technical Details
- **編集**: `src/core/audio_recorder.py`（`_cleanup_stream` を fire-and-forget 化、`_CLEANUP_TIMEOUT_SEC` 削除）
- **編集**: `src/app.py`（`start_recording` の start 戻り値チェック、`stop_and_transcribe` の同期 stop 化と `_active_slot` 即クリア、`_finalize_recording_async` の引数化）

## [Unreleased] - 2026-05-28

### Changed
- **README.md を AI エージェント向けセットアップに最適化**
  - 目次に「AI エージェント向けセットアップ手順」を追加
  - 前提条件チェックリスト（OS / Python / git / ffmpeg / ネットワーク）と確認コマンドを表形式で明示
  - macOS / Windows それぞれ「クローン → ffmpeg → venv → 依存関係 → 設定ファイル → 起動」を 1 ブロックで完結するコピペ可能なコマンド列に再整理
  - API キーは設定ウィンドウから入力すると OS シークレットストアに保存される旨を明記し、旧来の `.env` 直書き手順から更新
  - 「ポータル経由で配布物をダウンロードする場合」セクションを追加し、`tag v*` push が GitHub Actions を経由して Releases に直リンクされるリリースフローを記載
  - トラブルシューティングに「macOS で録音中・停止後にアプリがフリーズする」項目を追加（PR #9 で根治済み・Force Reset の使い方）
  - よくあるつまずきポイント（権限再起動・`python3` 必須・`pip install` 遅延・GPU 不要・Windows 管理者権限）を AI が事前案内できる形で明文化

## [Unreleased] - 2026-05-05

### Fixed
- **macOS PortAudio 由来のフリーズ問題に対処**
  - `_recorder.stop()` が `_recording_lock` を握ったまま PortAudio (CoreAudio) の `stream.stop()` / `close()` を呼ぶと、CoreAudio がハングした際にロックを巻き込んでアプリ全体が停止する問題があった
  - `src/core/audio_recorder.py:_cleanup_stream`: `stream.stop()` / `close()` を別スレッドへ逃がし、最大 2 秒のタイムアウトで諦めて呼出元へ復帰。`self._stream` は即 `None` に切替えるため後続の start/stop は新ストリーム前提で進める。タイムアウトしても `_collect_audio_data()` でキューから音声を回収するので発話内容はロストしない（ユーザーは 2 秒余分に待つだけで結果が得られる）
  - `src/app.py:stop_and_transcribe`: `_recording_lock` を解放してから `_recorder.stop()` を呼ぶよう修正。lock を巻き込まないためアプリ全体のフリーズを防止
- **PortAudio ゾンビ callback による録音バッファ汚染を解消**
  - 古い stream は `_cleanup_stream` で `_stream = None` にしても、PortAudio の I/O スレッドが close 完了まで callback を呼び続け、共通の `self._queue` に古い音声を流し込み続けるため 2 回目以降の録音が無音判定 (`has_speech=False`) になっていた
  - `src/core/audio_recorder.py`: 録音セッション識別子 `_session_id` を導入。`start()` のたびにインクリメントし、`_make_audio_callback(session_id)` でセッション ID を埋め込んだクロージャを各 `InputStream` に渡す。callback は `if self._session_id != my_session: return` で旧 stream のゾンビ呼出を即弾く
  - これにより `stream.close()` がハング中でも、新セッションの queue は旧 stream の音声で汚染されない
- **ダブルタップ Auto-Enter 検出が PortAudio ハング時に失われる問題**
  - `stop_and_transcribe` が keyboard listener スレッド内で `_recorder.stop()`（最大 2 秒ブロック）まで実行していたため、キーを離した直後の次の press イベントが listener で待たされ、ダブルタップ判定ウィンドウ (400ms) を超えてしまっていた
  - `src/app.py`: `stop_and_transcribe` はフラグ更新のみ同期で行い、`_finalize_recording_async` を別 daemon スレッドで起動して `_recorder.stop()` 以降を実行。listener スレッドは即時に次のキーイベントを処理可能に

### Added
- **Force Reset (Unfreeze) メニューを再導入**
  - 過去に削除されたが、PortAudio ハング時の最終手段として復活。ただし用途が変わり、内部状態リセットではなく **プロセスごとの再起動** で OS のマイクハンドル / 「マイク使用中」オレンジドット / メニューバーアイコンを完全にリセットする
  - `src/app.py:force_reset_recording`: `subprocess.Popen([sys.executable] + sys.argv, start_new_session=True)` で同じコマンドラインの新プロセスを独立起動し、自分は `os._exit(0)` で即時終了。execv 方式だと macOS で NSStatusItem が再登録されない事象があったため subprocess + 新セッション方式に統一
  - `src/ui/system_tray.py`: メニューに `Force Reset (Unfreeze)` 項目と `force_reset` Signal を追加
- **フリーズ再現用デバッグスクリプト** (`scripts/simulate_freeze.py`)
  - `sounddevice.InputStream.stop` / `close` を「指定秒数だけ眠るだけ」のメソッドに monkeypatch してから voicekey 本体を起動。`FREEZE_SEC` 環境変数で待ち時間を制御（既定 30 秒）
  - 上記フリーズ系修正の動作検証を確実に再現できるようにするため
- **録音状態のデバッグログ拡充**
  - `src/core/audio_recorder.py`: `start()` でストリーム ID、`_audio_callback` で各セッション初回の callback 受信、`stop()` で取得した `queue_items` / `samples` / `duration` / `callback_received` をログ出力。フリーズや録音欠損の切り分けに使用

### Technical Details
- **編集**: `src/app.py`（import に `os` / `subprocess` / `sys` を追加、`stop_and_transcribe` の非同期化、`_finalize_recording_async` 新設、`force_reset_recording` を再起動方式へ書き換え、`_tray.force_reset` の signal 接続）
- **編集**: `src/core/audio_recorder.py`（`_session_id` / `_callback_received` 追加、`_make_audio_callback` クロージャ生成、`_cleanup_stream` のタイムアウト化、`stop()` のログ拡充）
- **編集**: `src/ui/system_tray.py`（`force_reset` Signal、Force Reset メニュー項目）
- **新規**: `scripts/simulate_freeze.py`

## [Unreleased] - 2026-05-01

### Added
- **API キーの OS シークレットストア保管 (macOS Keychain / Windows Credential Manager)**
  - 新規モジュール `src/utils/secrets.py`: `keyring` ライブラリを通じて `get_api_key` / `set_api_key` / `delete_api_key` を提供（サービス識別子 `voicekey.Groq` / `voicekey.OpenAI`）
  - 設定ウィンドウの各 Hotkey の API 設定エリアに「API Key」入力欄（パスワードマスク）と Save / Clear ボタンを追加。同じ backend を選んだ Hotkey 間で同じエントリを共有
  - 取得は **Keychain → 環境変数** の優先順。既存の `.env` / `GROQ_API_KEY` / `OPENAI_API_KEY` 利用は維持され、後方互換を保ったまま Keychain に移行可能（自動マイグレーションは行わない）
  - `settings.yaml` には API キーを書き込まない（ConfigManager 側は変更なし）
- **macOS でのメニューバー常駐動作**
  - `python run.py` 起動時に `NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)` を呼んで Dock / Cmd+Tab から非表示化
  - PyInstaller ビルド版は `.app` バンドル化し、`Info.plist` に `LSUIElement: True` / `NSPrincipalClass: NSApplication` / `NSMicrophoneUsageDescription` を含める（`voicekey.spec`）
  - 設定ウィンドウを開く処理を `raise_()` → `activateWindow()` → `NSApp.activateIgnoringOtherApps_(True)` の順で前面化するよう修正（メニューバー → Settings で確実に最前面に出る）

### Changed
- `requirements.txt` に `keyring>=24.0` と `pyobjc-framework-Cocoa>=10.0; sys_platform == "darwin"` を追加
- `PlatformAdapter` に `configure_app_visibility(hide_from_dock)` と `bring_to_front(window)` を追加（既定 no-op）。macOS アダプタでのみ AppKit 経由で実装し、Windows は no-op
- `GroqTranscriber._get_client` / `OpenAITranscriber._get_client` の API キー取得経路を `_resolve_api_key()` ヘルパー経由に統一（Keychain → 環境変数）。エラーメッセージも「設定ウィンドウから保存するか、環境変数を設定してください」に更新
- **メニューバーアイコンの左クリック挙動を変更**: 以前は左クリック / ダブルクリックで設定ウィンドウが直接開いていたが、コンテキストメニューを表示するだけに変更。ユーザーがメニューから「Settings」を選んだ時にのみ設定ウィンドウを開く（`src/ui/system_tray.py` の `_setup_click_handler` / `_on_activated` を削除、`setContextMenu` のみで動作）

### Removed
- **Force Reset 機能を完全削除**
  - トレイメニューの「Force Reset」項目（`src/ui/system_tray.py` の `force_reset` Signal とアクション）を削除
  - `src/app.py` から `force_reset()` メソッド本体、シグナル接続、`_reset_generation` 世代カウンタ、`_queue_processor` / `_process_transcription_task` 内の世代比較による結果破棄ロジックを全て削除
  - 通常運用で連打フリーズ等の根治対応（自動復旧ループ・録音解除等）が既に入っているため、ユーザー手動のリセットボタンは不要と判断
- **Dynamic Island 風オーバーレイ UI を完全廃止** (`src/ui/overlay.py` 削除)
  - 録音 / 文字起こし状態は **トレイアイコンの色だけ** で判別する設計に統一（IDLE 青 / RECORDING 赤 / RECORDING_AUTO_ENTER 紫 / TRANSCRIBING オレンジ）
  - `src/app.py` から overlay 関連の初期化（`_setup_ui_components` 内）、状態反映 (`_update_ui_status`)、音声レベル反映 (`_on_audio_level`)、波形コールバック登録 (`set_level_callback`) をすべて削除
  - `_show_backend_warning` を「オーバーレイにメッセージ表示」から「ログに warning 出力」に変更（API キー未設定時など）
  - `src/ui/__init__.py` から `DynamicIslandOverlay` の export を削除
  - `src/config/constants.py` から UI セクションの `OVERLAY_BASE_WIDTH` / `OVERLAY_BASE_HEIGHT` / `OVERLAY_EXPANDED_WIDTH` / `OVERLAY_EXPANDED_HEIGHT` / `OVERLAY_TOP_MARGIN` / `ANIMATION_DURATION_MS` を削除
  - 副次効果: 起動時にオーバーレイ用の QMainWindow を作らないため、初期化が軽量化

### Security
- **`settings.yaml` を git 追跡対象から除外**
  - `.gitignore:81` に `settings.yaml` が記載されていたが、過去に誤って追跡されていたため `git rm --cached settings.yaml` で解除（ローカルファイルは残存）
  - 新規 `settings.example.yaml` をコミット対象に追加。新規ユーザー / Clone 時はこのファイルをコピーして使う
  - 将来 `settings.yaml` に万一機密情報を書き込んでも誤コミットされないよう予防
- **依存パッケージの完全 lock**
  - 新規 `requirements.lock`: `pip freeze --exclude-editable` で venv 内の全パッケージとバージョンを固定（再現性 / サプライチェーン耐性向上）
  - 既存の `requirements.txt` は人間が読みやすい下限指定の形を維持。lock ファイルは並列に追加するだけで既存インストール手順への影響なし

### Technical Details
- **新規ファイル**: `src/utils/secrets.py` / `settings.example.yaml` / `requirements.lock`
- **編集**: `src/main.py`（QApplication 作成後に `configure_app_visibility(True)` を呼び出し）
- **編集**: `src/app.py`（`_open_settings` を最前面化シーケンスに変更）
- **編集**: `src/platform/base.py` / `src/platform/macos/adapter.py`（可視性制御メソッドを追加）
- **編集**: `src/core/groq_transcriber.py` / `src/core/openai_transcriber.py`（`_resolve_api_key` 追加）
- **編集**: `src/ui/settings_window.py`（API キー入力欄、`_save_api_key` / `_clear_api_key` / `_refresh_api_key_status` 追加、backend 切替時に Keychain ステータス再描画）
- **編集**: `src/ui/system_tray.py`（左クリック直接起動を廃止、メニュー経由のみ）
- **編集**: `voicekey.spec`（macOS 用 BUNDLE と Info.plist）

## [Unreleased] - 2026-04-30

### Added
- **音声前処理パイプライン（音量正規化）**
  - 新規モジュール `src/core/audio_preprocess.py` を追加
  - Peak+RMS ハイブリッド音量正規化：目標 RMS = -20 dBFS、ピーク上限 = -3 dBFS（音割れ防止）
  - 録音直後・API 送信前に適用、numpy のみで <1ms の低レイテンシ
  - 小さい声を底上げして API 文字起こしの精度を向上、大音量はクリッピング防止のため抑え込み
  - ノイズ対策は API モデル側に任せる方針（noisereduce 等は採用せず）
- **Auto Enter Delay スライダーを設定 UI に追加**
  - ダブルタップ Auto-Enter 機能で、テキスト挿入後から Enter 押下までの待機時間を 0〜500ms で調整可能（`src/ui/settings_window.py`）
  - 既定値 50ms。一部アプリが即時 Enter に反応しない問題に対するユーザー調整手段（`src/config/constants.py`）

### Changed
- `DEFAULT_CONFIG` に `audio_preprocess.volume_normalize` キーを追加（既定 True）
- 設定 UI の Advanced タブに音声前処理セクションを追加
- `stop_and_transcribe()` で `recorder.stop()` 直後に `preprocess_audio()` を呼ぶよう変更（`src/app.py`）

### Fixed
- **録音状態の Race Condition 解消（Phase 3）**
  - `_recording_lock` (RLock) を導入し、`start_recording` / `stop_and_transcribe` / `force_reset` の check-then-set を直列化（`src/app.py`）
  - 並列スレッドから start/stop が同時に呼ばれた場合に `_is_recording` と `_active_slot` の整合性が崩れる問題を解消
  - `start_recording` で transcriber 取得失敗時に `_active_slot` をリセットするよう修正（リーク防止）
  - 6 並列スレッドで 600 回の start/stop を実行しても整合性が保たれることを確認
  - ロック順序: `_recording_lock` → `_queue_worker_lock` → `recorder._lock`（逆順は禁止、デッドロック防止）

- **プラットフォーム整合性の向上（Phase 2）**
  - `InputHandler.insert_text` の貼り付けキー操作を `with pressed(...)` から明示的な `try/finally` に変更。`'v'` の release で例外が発生しても修飾キー（Cmd/Ctrl）が確実に解放されるよう改善（`src/core/input_handler.py`）
  - `OpenAITranscriber` / `GroqTranscriber` に `close()` メソッドを追加し、`unload_model()` から呼び出すよう変更。httpx 接続プールを明示的に閉じてリークを防ぐ（`src/core/openai_transcriber.py`, `src/core/groq_transcriber.py`）
  - `_setup_hotkey_slots()` の冒頭で旧 `api_transcriber.close()` を呼び、Hot reload 時に旧クライアントの HTTP 接続が leak する問題を解消（`src/app.py`）
  - `_apply_config_changes()` で slots 変更検出時に `self._listener.stop()` を呼び、自動再起動ループに新設定でリスナーを再立ち上げさせる（`src/app.py`）

- **連打フリーズ問題の根治（マイク占有/キー押下誤認識/Force Reset 効かず）**
  - `force_reset()` で `_pressed_keys` / `_last_hotkey_release_time` / `_last_hotkey_release_slot` をクリアするよう修正。リセット後も「キーが押されたまま」と誤認識される問題を解消（`src/app.py`）
  - キーボードリスナー (`_start_keyboard_listener`) を自動復旧ループ化。例外で死んでも黙って永久停止せず、押下キー状態をクリアして再起動する（`src/app.py`）
  - `_quit_app()` で `listener.stop()` と `recorder.stop()` を明示的に呼ぶよう修正。終了時にマイクが OS にロックされ続ける問題を解消（`src/app.py`）
  - `AudioRecorder` の `start` / `stop` / `_cleanup_stream` を `threading.RLock` で直列化。`stop` 中に `start` が割り込んで旧ストリームが OS 占有のまま捨てられる競合を解消（`src/core/audio_recorder.py`）
  - `_cleanup_stream` で `stream.stop()` と `stream.close()` を独立 try/except で囲み、片方が例外を出しても他方を必ず実行するよう修正（`src/core/audio_recorder.py`）
  - `_queue_processor` の各タスク処理を try/except/finally で囲み、個別タスクの例外でワーカー全体が死なないようにした。`task_done()` も常に呼ぶ（`src/app.py`）
  - `_queue_worker_running` の check-and-set を `_queue_worker_lock` で排他化し、二重ワーカー起動を防止（`src/app.py`）
  - `_handle_key_press` / `_handle_key_release` で `_normalize_key` 失敗時の挙動を改善。debug ログ出力＋永久録音を防ぐ保険として「押下キー無し＋録音中」検出時に自動停止（`src/app.py`）

### Technical Details
- **src/app.py**
  - `_setup_state` に `_queue_worker_lock` (Lock) と `_listener` 参照保持を追加
  - `_start_queue_worker` を `_start_queue_worker_locked` にリネーム（呼び出し側がロック取得済み前提）
  - キーボードリスナー再起動ループにより Hot reload 時のリスナー入れ替えも将来対応可能
- **src/core/audio_recorder.py**
  - `__init__` に `threading.RLock` を追加し、ライフサイクル全パスを保護
  - `start()` 冒頭で残骸ストリームのクリーンアップを実施

---

## [Unreleased] - 2026-04-18

### Added
- **Auto Enter 遅延調整スライダー**
  - ダブルタップ時のテキスト挿入後〜Enter押下までの待機時間をUIから調整可能に
  - Settings の Advanced ページにスライダー（0〜500ms、既定50ms）と現在値ラベルを追加
  - 即時Enterに反応しないアプリ（Slack、一部Webフォーム等）向けに遅延を伸ばせる
  - 新規設定キー `auto_enter_delay_ms` を追加（settings.yaml・ホットリロード対応）

### Technical Details
- **constants.py**: `DEFAULT_CONFIG` に `auto_enter_delay_ms: 50` を追加
- **settings_window.py**: `QSlider` + `QLabel` を Advanced ページに追加、load/save に反映
- **app.py**: `_handle_transcription_result()` のハードコード `time.sleep(0.05)` を `self._config.get("auto_enter_delay_ms", 50)` 参照に置換

---

## [Unreleased] - 2026-04-08

### Added
- **強制リセット機能**
  - トレイアイコンの右クリックメニューに「Force Reset」を追加
  - 録音中・文字起こし中の全処理を強制停止してidle状態に復帰
  - 世代カウンタにより実行中のAPI呼び出し結果も安全に破棄

- **ダブルタップ + ホールドで自動Enterキー送信**
  - ホールドモードでホットキーをダブルタップ（2回目を長押し）すると、文字起こし結果入力後にEnterキーを自動送信
  - チャットアプリでの音声入力→送信をワンアクションで完結
  - ダブルタップ判定ウィンドウ: 400ms

### Technical Details
- **types.py**: `TranscriptionTask` に `auto_enter` フィールドを追加
- **input_handler.py**: `press_enter()` メソッドを追加（pynput Key.enter使用）
- **system_tray.py**: `force_reset` シグナルとメニュー項目を追加
- **app.py**: `force_reset()` メソッド、世代カウンタ `_reset_generation`、ダブルタップ検出ロジック、`text_ready` シグナルを `Signal(str, bool)` に拡張

---

## [Unreleased] - 2026-02-27

### Added
- **Cross-platform 抽象レイヤーを追加**
  - `src/platform/` を新設し、OS差分を `core` から分離
  - `PlatformAdapter` インターフェースと `get_platform_adapter()` ファクトリを追加
  - `windows` / `macos` 向けアダプタ実装を追加

- **入力デバイス選択機能を追加**
  - Settings の Advanced ページでマイク入力デバイスを選択可能
  - `audio_input_device` 設定キーを追加（`default` / デバイスID）
  - 録音開始時に指定デバイスを使用し、失敗時は自動でデフォルトへフォールバック

- **運用ドキュメントの追加**
  - `docs/CROSS_PLATFORM_UNIFICATION_PLAN.md`（統合計画）
  - `docs/CROSS_PLATFORM_TEST_CHECKLIST.md`（検証チェックリスト）
  - `run.sh`（macOS/Linux向け起動スクリプト）

### Changed
- **入力処理を platform 注入方式へ移行**
  - `src/core/input_handler.py` の `sys.platform` 分岐を削除
  - 貼り付け修飾キー（Cmd/Ctrl）を platform アダプタで制御

- **録音設定の動的反映を強化**
  - `settings.yaml` の変更監視で入力デバイス設定の更新を即時適用

- **UI のOS依存ロジックを分離**
  - `src/ui/settings_window.py` のホットキー変換を platform 経由に変更
  - `src/ui/system_tray.py` のアクティベーション判定を platform ポリシー化
  - `src/ui/styles.py` のフォント指定を OS別フォールバック対応に変更

- **アプリ初期化の依存注入を整理**
  - `src/app.py` で platform アダプタを初期化し、
    InputHandler / SettingsWindow / SystemTray / キー正規化に注入

### Technical Details
- **新規追加**
  - `src/platform/base.py`
  - `src/platform/factory.py`
  - `src/platform/common/keymap.py`
  - `src/platform/windows/adapter.py`
  - `src/platform/macos/adapter.py`

- **更新**
  - `src/app.py`
  - `src/core/input_handler.py`
  - `src/ui/settings_window.py`
  - `src/ui/system_tray.py`
  - `src/ui/styles.py`
  - `README.md`

## [Unreleased] - 2026-02-03

### Added
- **起動時プリロード機能の実装**
  - 起動時にVADモデルをバックグラウンドでプリロードし、最初の文字起こしを高速化
  - `preload_on_startup` 設定オプションを追加（デフォルト: true）
  - `app.py` に `_preload_models_async()` を追加

### Fixed
- **VADプリロードのタイミング改善**
  - ホットキースロット初期化後にプリロードを実行するよう調整
  - 起動順序を `setup_state -> start_background_threads -> preload` に整理

### Technical Details
- **src/app.py**
  - `_preload_models_async()` を追加し、設定に応じて非同期プリロードを実行
  - `_preload_vad_model()` を実行ロジック専用に整理
- **src/config/constants.py**
  - `DEFAULT_CONFIG` に `preload_on_startup: true` を追加

## [Unreleased] - 2026-01-30

### Added
- **文字起こしキューイング機能の実装**
  - 文字起こし処理中に新しい録音を開始しても、前タスクを破棄せずキューに追加
  - すべての録音結果を順番に処理して入力
  - `queue.Queue` を使用したスレッドセーフなタスク管理
  - `TranscriptionTask` データクラスを追加

### Changed
- **app.py の文字起こし処理ロジックをキュー方式へ変更**
  - `start_recording()` からキャンセル方式を削除
  - `stop_and_transcribe()` でキュー投入
  - `_start_queue_worker()`, `_queue_processor()`, `_process_transcription_task()` を追加
  - `_handle_transcription_result()` は結果処理専用にし、idle遷移はワーカー管理へ移行

### Technical Details
- **src/config/types.py**
  - `TranscriptionTask` データクラスを追加（audio_data, slot_id, timestamp）
- **src/app.py**
  - `_transcription_queue` / `_queue_worker_running` を追加
  - キュー処理完了時に `idle` へ復帰する制御を追加

## [Unreleased] - 2026-01-15

### Added
- **CONTRIBUTING.md ドキュメント作成**
  - 詳細なバージョニングルール（X=大きな変更、Y=ユーザーが気づく変更、Z=小さな修正）
  - コミットメッセージ規約（type: description形式）
  - 変更記録（CHANGELOG）の運用ルール
  - ブランチ戦略とリリースプロセス
  - プルリクエストのガイドライン
  - プッシュのタイミングとチェックリスト

- **デュアルホットキー機能の実装**
- **2つの独立したホットキー設定**: 固定で2つのホットキースロットを追加
  - 各ホットキーに対して異なるショートカット、モード（hold/toggle）、バックエンド（local/groq/openai）を設定可能
  - APIバックエンド（Groq/OpenAI）の場合、各ホットキーで異なるモデルとプロンプトを指定可能
  - ローカルバックエンドは両方のホットキーで共通の設定を使用（VRAM節約）

- **新しい設定構造**: `settings.yaml` の階層化
  - `hotkey1` / `hotkey2`: 各ホットキーの個別設定
  - `local_backend`: ローカルGPU設定（共通）
  - `language`, `vad_filter` などのグローバル設定

- **自動マイグレーション機能**
  - 旧設定フォーマット（単一ホットキー）を検出し、新形式に自動変換
  - 既存ユーザーの設定を保持しながらアップグレード可能
  - マイグレーション時のログ出力

- **設定UIの刷新**
  - Generalページ: 2つのホットキーを横並びで設定
  - 各ホットキーグループに: ショートカット入力、モード選択、バックエンド選択、API設定
  - Modelページ: ローカル共通設定のみに簡略化
  - API設定の動的表示（バックエンド選択に応じて表示/非表示）

### Changed
- **CLAUDE.md に自動コミットルール追加**
  - AI開発者向けに、機能実装完了時の自動コミットルールを明記
  - コミットのタイミング、必須チェック項目、例外ケースを定義
  - プッシュは手動実行（自動プッシュしない）
  - ユーザーへの報告フォーマットを標準化

- **README.md コントリビューションセクション更新**
  - CONTRIBUTING.md へのリンク追加
  - 開発ガイドラインへのナビゲーション改善

- **app.py の大幅なリファクタリング**
  - `HotkeySlot` データクラスを追加（各スロットの状態管理）
  - `_hotkey_slots` 辞書で複数ホットキーを管理
  - `_local_transcriber` を共有インスタンスとして分離
  - `_active_slot` で現在アクティブなスロットを追跡
  - キーボードリスナーが両方のホットキーを同時監視
  - `start_recording()` にスロットID引数を追加

- **config_manager.py の強化**
  - `_deep_merge()` 関数でネストされた辞書のマージをサポート
  - `_migrate_legacy_config()` メソッドで旧設定を自動変換
  - 深いマージによりデフォルト設定との統合を改善

- **ホットリロード機能の維持**
  - `_apply_config_changes()` が新構造に対応
  - ホットキー設定変更時の自動更新
  - バックエンド変更時のAPI Transcriber再作成
  - ローカル設定変更時のモデルアンロード

### Technical Details
- **types.py**
  - `HotkeySlotConfig` データクラスを追加

- **constants.py**
  - `DEFAULT_CONFIG` を新構造（hotkey1/hotkey2/local_backend）に変更
  - `default_api_models` でバックエンド別のデフォルトモデルを定義

- **settings_window.py**
  - `_create_hotkey_group()` で各スロットのUIを生成
  - `_create_api_settings_widget()` でAPI設定ウィジェットを動的生成
  - `_on_slot_backend_changed()` でバックエンド変更を処理
  - `_load_current_settings()` / `_save_settings()` を新構造に対応

### Fixed
- ホットキー競合時の優先順位（最初に検出されたスロットが優先）
- Hold/Toggle混在時のキーボードリスナー処理

---

## [v2.0.0] - 2026-01-15

### Added
- デュアルホットキースロットとMP3音声サポート
- OpenAIバックエンドとGroqバックエンドのモジュール化
- macOSスタイルの設定UI

### Changed
- プロジェクト構造のリファクタリング
- バックエンドの分離（local/groq/openai）

---

## [Previous Releases]

### [2026-01-05] - LLMプロンプト処理の改善
- LLMプロンプト処理のリファクタリング
- 設定UIの改善

### [2025-12-09] - UI改善とドキュメント更新
- オーバーレイUIの改善
- ホットキー処理の改善
- AI用コメントルールの追加
- README更新

### [2025-12-09] - v2.0.0リリース
- 日本語コメントの追加
- LLM処理ログ表示
- コード整理

### [2025-12-08] - LLM後処理機能
- LLM後処理機能の追加
- GUI設定の追加
- macOSスタイルのUIテーマ
- オーバーレイの改善

### [2025-12-08] - Groqバックエンド統合
- Groq API対応
- VADフィルター統合
- PyInstallerビルド設定更新

### [2025-12-08] - プロジェクト整形
- 全体的なコード整形
- 安定版リリース

### [2025-12-01] - 初期リリース
- プロジェクト名変更（SuperWhisperLike → voicekey）
- GNU GPL v3ライセンス追加
- 無音検出のUIフィードバック
- エラーハンドリング改善
- ビルドアーティファクトのクリーンアップ

---

## Notes

### 変更記録のガイドライン
- すべての機能追加、変更、修正を記録する
- 各エントリには簡潔な説明と影響範囲を含める
- 技術的な詳細は "Technical Details" セクションに記載
- ユーザー影響のある変更は目立つように記載

### バージョニング
- メジャーバージョン: 破壊的変更または大規模な機能追加
- マイナーバージョン: 後方互換性のある機能追加
- パッチバージョン: バグ修正とマイナーな改善
