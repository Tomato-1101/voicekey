"""
設定管理モジュール

YAMLファイルからの設定読み込み、保存、ホットリロードを提供する。
設定ファイルが変更されると自動的に再読み込みされる。
"""

import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from ..utils.logger import get_logger
from .constants import DEFAULT_CONFIG, SETTINGS_FILE_NAME, default_format_enabled

logger = get_logger(__name__)
# 対応バックエンド（REST + ストリーミング）。これ以外は openai にフォールバック
API_BACKENDS = {"groq", "openai", "elevenlabs", "deepgram"}
# 製品版（release ブランチ）でユーザーが文字起こしに選べるバックエンド（2 択）。
# リアルタイム = deepgram（nova-3・短命トークン直叩き＋ストリーミング。話しながらライブ表示）/
# スタンダード = groq（既定・whisper-large-v3-turbo・プロキシ経由。録音後にきれいに整形）。
# elevenlabs（scribe_v1）は選択肢から外し、スタンダードのハンズフリー録音時に内部でのみ使う
# （長時間録音の精度対策・app._maybe_handsfree_slot）。openai は開発用のみ。
# ＝範囲外（旧 elevenlabs / openai 等）の保存値だけ groq へ自動移行する（下の _constrain_release_backends）。
# deepgram は選択肢に残るため維持。バックエンドの enum・保存値は 4 値のまま（「選べる集合」だけを絞る）。
RELEASE_TRANSCRIBE_BACKENDS = ("deepgram", "groq")
# 製品版で範囲外（旧 elevenlabs / openai 等）の保存値を移行する先（スタンダード＝既定）
RELEASE_DEFAULT_BACKEND = "groq"


def _deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """
    2つの辞書を深くマージする。

    updatesの値がbaseの値を上書きする。
    ネストされた辞書も再帰的にマージされる。

    Args:
        base: ベースとなる辞書
        updates: 更新値を含む辞書

    Returns:
        マージされた辞書
    """
    result = base.copy()
    for key, value in updates.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # 両方が辞書の場合は再帰的にマージ
            result[key] = _deep_merge(result[key], value)
        else:
            # それ以外は上書き
            result[key] = value
    return result


class ConfigManager:
    """
    アプリケーション設定の管理クラス。
    
    YAMLファイルからの設定読み込み、保存を行い、
    ファイル変更時には自動的に再読み込みを行う。
    
    Attributes:
        config_path: 設定ファイルのパス
        config: 現在の設定辞書
    """
    
    def __init__(self, config_path: Optional[str] = None) -> None:
        """
        ConfigManagerを初期化する。
        
        Args:
            config_path: 設定ファイルのパス。Noneの場合はプロジェクトルートから自動検索
        """
        self.config_path = self._resolve_config_path(config_path)
        self.last_mtime: Optional[float] = None  # ファイル更新時刻
        # 起動時（＝この初回ロード時）に settings.yaml が既に存在していたか。
        # 「完全な初回起動（新規インストール）」か「既存ユーザー」かの判定に使う
        # （初回オンボーディング表示要否・Phase 5）。以後の save/reload では変えない。
        self.config_file_existed: bool = os.path.exists(self.config_path)
        # config は listener / 設定監視 / UI の各スレッドから read/write されるため保護する
        self._lock = threading.RLock()
        self.config: Dict[str, Any] = self._load_config()

    def _resolve_config_path(self, config_path: Optional[str]) -> str:
        """設定ファイルのパスを解決する。"""
        if config_path:
            return config_path

        if getattr(sys, 'frozen', False):
            # PyInstallerでビルドされた実行ファイルの場合
            base_dir = Path(sys.executable).parent
        else:
            # スクリプトとして実行：プロジェクトルート（srcの親）を使用
            base_dir = Path(__file__).parent.parent.parent

        return str(base_dir / SETTINGS_FILE_NAME)

    def _migrate_legacy_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        旧形式の設定を新形式にマイグレーションする。

        旧形式（単一hotkey）を検出した場合、hotkey1に変換し、
        hotkey2はデフォルト値を設定する。

        Args:
            config: 読み込まれた設定辞書

        Returns:
            マイグレーション済みの設定辞書
        """
        if "hotkey" in config and "hotkey1" not in config:
            # 旧形式を検出
            logger.info("旧設定フォーマットを検出。新形式にマイグレーション中...")

            backend = self._normalize_backend(config.pop("transcription_backend", "openai"))
            model_key = "groq_model" if backend == "groq" else "openai_model"
            prompt_key = "groq_prompt" if backend == "groq" else "openai_prompt"

            # 旧ホットキー設定をhotkey1に移行
            config["hotkey1"] = {
                "hotkey": config.pop("hotkey", "<f2>"),
                "hotkey_mode": config.pop("hotkey_mode", "toggle"),
                "backend": backend,
                "api_model": config.get(model_key, ""),
                "api_prompt": config.get(prompt_key, ""),
            }

            # hotkey2にデフォルト値
            config["hotkey2"] = DEFAULT_CONFIG["hotkey2"].copy()

            # 旧キーを削除
            for key in [
                "groq_model",
                "openai_model",
                "groq_prompt",
                "openai_prompt",
                "transcription_backend",
                "model_size",
                "compute_type",
                "release_memory_delay",
                "condition_on_previous_text",
                "no_speech_threshold",
                "log_prob_threshold",
                "no_speech_prob_cutoff",
                "beam_size",
                "model_cache_dir",
                "local_backend",
            ]:
                config.pop(key, None)

            logger.info("設定マイグレーション完了")

        # スロット設定が存在する場合は、API専用バックエンドに正規化
        defaults = DEFAULT_CONFIG.get("default_api_models", {})
        for slot_id in [1, 2]:
            slot_key = f"hotkey{slot_id}"
            slot = config.get(slot_key)
            if not isinstance(slot, dict):
                continue

            backend = self._normalize_backend(slot.get("backend", "openai"))
            slot["backend"] = backend

            api_model = str(slot.get("api_model", "") or "").strip()
            if not api_model:
                slot["api_model"] = defaults.get(backend, "")

        # 廃止したローカル推論設定キーを削除
        for key in [
            "local_backend",
            "model_size",
            "compute_type",
            "release_memory_delay",
            "condition_on_previous_text",
            "no_speech_threshold",
            "log_prob_threshold",
            "no_speech_prob_cutoff",
            "beam_size",
            "model_cache_dir",
        ]:
            config.pop(key, None)

        return config

    @staticmethod
    def _normalize_backend(backend: Any) -> str:
        """
        バックエンド値をAPI専用の有効値に正規化する。

        `local` や不正値は `openai` にフォールバックする。
        """
        backend_str = str(backend).lower()
        if backend_str in API_BACKENDS:
            return backend_str
        return "openai"

    def _load_config(self) -> Dict[str, Any]:
        """
        ファイルから設定を読み込み、デフォルト値とマージする。

        Returns:
            すべてのキーが保証された設定辞書
        """
        if not os.path.exists(self.config_path):
            logger.warning(f"設定ファイルが見つかりません: {self.config_path}。デフォルト値を使用します。")
            return DEFAULT_CONFIG.copy()

        try:
            self.last_mtime = os.path.getmtime(self.config_path)

            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded_config = yaml.safe_load(f) or {}

            # 旧形式の設定をマイグレーション
            loaded_config = self._migrate_legacy_config(loaded_config)

            # デフォルト設定と深くマージ（ネストされた辞書も保証）
            config = _deep_merge(DEFAULT_CONFIG, loaded_config)
            # 常時 ON 固定の項目を矯正（UI から撤去したため保存済みの古い false を上書き）
            self._force_always_on(config)
            # 製品版は文字起こしバックエンドを 3 択に制限（範囲外の openai 等は groq へ移行・deepgram は維持）
            self._constrain_release_backends(config)
            # モード別整形既定の一回限りマイグレーション（deepgram スロットの整形を初回だけ既定 OFF へ）
            self._migrate_format_defaults(config)
            return config

        except Exception as e:
            logger.error(f"設定読み込みエラー: {e}")
            return DEFAULT_CONFIG.copy()

    @staticmethod
    def _force_always_on(config: Dict[str, Any]) -> None:
        """
        UI から撤去し常時 ON に固定する項目を矯正する（破壊的更新）。

        VAD・長文分割・ストリーミング・録音 HUD・音量正規化は設定 UI から撤去したため、
        保存済み settings.yaml に古い false が残っていても無視して True に固定する。
        ユーザーが OFF にする手段を持たないことを保証する。

        Args:
            config: 矯正対象の設定辞書（その場で書き換える）
        """
        config["vad_filter"] = True
        config["split_parallel_enabled"] = True
        config["streaming_enabled"] = True
        config["hud_enabled"] = True
        # 音量正規化は Windows 固有の前処理（audio_preprocess セクション）
        if not isinstance(config.get("audio_preprocess"), dict):
            config["audio_preprocess"] = {}
        config["audio_preprocess"]["volume_normalize"] = True

    @staticmethod
    def _constrain_release_backends(config: Dict[str, Any]) -> None:
        """
        製品版（release）の文字起こしバックエンドを 2 択（deepgram/groq）に制限する。

        保存済み settings.yaml に範囲外バックエンド（旧 elevenlabs / openai 等）が残っていても、
        groq（スタンダード＝既定）へ移行する。移行時は api_model を空にして
        default_api_models（groq→whisper-large-v3-turbo）にフォールバックさせる。
        deepgram（リアルタイム）は選択肢に残したので、保存済み deepgram はそのまま維持される。
        elevenlabs は enum としては残す（スタンダードのハンズフリー録音時に内部でのみ使う）が、
        ユーザーが直接選ぶ保存値としては許可しない（「選べる集合」だけを絞る）。

        Args:
            config: 矯正対象の設定辞書（その場で書き換える）
        """
        for slot_key in ("hotkey1", "hotkey2"):
            slot = config.get(slot_key)
            if not isinstance(slot, dict):
                continue
            backend = str(slot.get("backend", "")).lower()
            if backend not in RELEASE_TRANSCRIBE_BACKENDS:
                slot["backend"] = RELEASE_DEFAULT_BACKEND
                # 移行先バックエンドのモデルへフォールバックさせる
                slot["api_model"] = ""

    @staticmethod
    def _migrate_format_defaults(config: Dict[str, Any]) -> None:
        """モード別整形既定を既存ユーザーへ一回限りで適用する（破壊的更新・Mac 版と対）。

        既存ユーザーは format_enabled=true が明示保存されているため、リアルタイム(deepgram)
        スロットの整形を初回だけ既定 OFF（速度全振り）へ矯正する。矯正済みマーカー
        （migrated_format_defaults）を立て、以後は二度と触らない＝その後ユーザーが deepgram で
        整形 ON にしたらそれを尊重する。マーカーは ConfigManager.save の deep merge で保持される
        （save は self.config を土台にマージするので、_load_config で立てたマーカーが次回保存で
        settings.yaml へ書き戻る）。deepgram 以外（スタンダード=groq 等）は既定 ON のままなので触らない。

        Args:
            config: 矯正対象の設定辞書（その場で書き換える）
        """
        if config.get("migrated_format_defaults"):
            return  # 一度矯正したら二度と触らない（ユーザーの選択を尊重）
        for slot_key in ("hotkey1", "hotkey2"):
            slot = config.get(slot_key)
            if not isinstance(slot, dict):
                continue
            backend = str(slot.get("backend", "")).lower()
            # deepgram のみ既定 OFF へ矯正（他バックエンドの既定 ON は明示保存済みなので触らない）
            if not default_format_enabled(backend):
                slot["format_enabled"] = False
        config["migrated_format_defaults"] = True

    def reload_if_changed(self) -> bool:
        """
        ファイルが変更されていれば設定を再読み込みする。
        
        Returns:
            再読み込みした場合True、しなかった場合False
        """
        if not os.path.exists(self.config_path):
            return False
            
        try:
            current_mtime = os.path.getmtime(self.config_path)
            if self.last_mtime is None or current_mtime > self.last_mtime:
                logger.info("設定ファイルが変更されました。再読み込み中...")
                new_config = self._load_config()
                with self._lock:
                    self.config = new_config
                return True
        except Exception as e:
            logger.error(f"設定確認エラー: {e}")
            
        return False

    def get(self, key: str, default: Any = None) -> Any:
        """
        設定値を取得する。
        
        Args:
            key: 設定キー
            default: キーが存在しない場合のデフォルト値
            
        Returns:
            設定値
        """
        with self._lock:
            return self.config.get(key, default)

    def save(self, new_config: Dict[str, Any]) -> bool:
        """
        設定をファイルに保存する。
        
        Args:
            new_config: 新しい設定値を含む辞書
            
        Returns:
            成功した場合True、失敗した場合False
        """
        try:
            with self._lock:
                # 内部設定を更新（ネストした辞書のカスタムキーを失わないよう deep merge する。
                # 浅い update だと default_api_models 等の手書き追加キーが丸ごと消える）
                self.config = _deep_merge(self.config, new_config)
                # 常時 ON 固定の項目はファイルにも True で書き戻す（UI から撤去済み）
                self._force_always_on(self.config)

                # アトミック書き込み: 一時ファイルへ全量書いてから os.replace で置換する。
                # open("w") の直接書きは truncate 後にクラッシュ/ディスク不足が起きると
                # settings.yaml が空・破損で残り、ユーザーの全設定が消える。同一ディレクトリの
                # 一時ファイル経由なら、置換は同一ファイルシステム上の原子的 rename になる
                # （os.replace は Windows でも既存ファイルを上書きする）。
                tmp_path = f"{self.config_path}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True)
                    f.flush()
                    os.fsync(f.fileno())  # 置換前に中身をディスクへ確実に落とす
                os.replace(tmp_path, self.config_path)

                # 再読み込みループを防ぐために更新時刻を記録
                self.last_mtime = os.path.getmtime(self.config_path)
            logger.info("設定を保存しました。")
            return True
            
        except Exception as e:
            logger.error(f"設定保存エラー: {e}")
            return False

    # 後方互換性のためのエイリアス
    def save_config(self, new_config: Dict[str, Any]) -> bool:
        """save()のエイリアス。"""
        return self.save(new_config)
