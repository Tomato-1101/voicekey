"""
Google Meet 議事録ボット（Windows 版）— Mac 版 `Caption/MeetBot/` の移植

会議 URL を渡すと、専用プロファイルの Chrome を裏で起動して会議に参加し、文字起こしを議事録として保存する。
Chrome は DevTools プロトコル（CDP）で動かす（Playwright・Node は同梱しない）。
**Meet の DOM 依存は `scripts.py` だけ**に閉じ込める（壊れたらそこだけ直す）。
"""
