"""
設定管理モジュール

YAMLファイルからの設定読み込み、保存、ホットリロードを提供する。
設定ファイルが変更されると自動的に再読み込みされる。
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from ..utils.logger import get_logger
from .constants import DEFAULT_CONFIG, SETTINGS_FILE_NAME

logger = get_logger(__name__)
# 対応バックエンド（REST + ストリーミング）。これ以外は openai にフォールバック
API_BACKENDS = {"groq", "openai", "elevenlabs", "deepgram"}


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
            return config

        except Exception as e:
            logger.error(f"設定読み込みエラー: {e}")
            return DEFAULT_CONFIG.copy()

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
                self.config = self._load_config()
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
            # 内部設定を更新
            self.config.update(new_config)
            
            # ファイルに書き込み
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True)
            
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
