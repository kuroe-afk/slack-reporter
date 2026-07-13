"""
各クライアントのSlack報告取得スクリプトを、1つのプロセス内でまとめて実行する
オーケストレータースクリプト。

各クライアントのロジック(fetch_slack_*.py)には一切手を加えず、
GitHub Actionsの実行環境構築(checkout・Python準備・ライブラリインストール)を
1回にまとめることでコストを削減する。
"""

import traceback

import fetch_slack as reporter_generic
import fetch_slack_acty as reporter_acty
import fetch_slack_samuraiz as reporter_samuraiz
import fetch_slack_sango as reporter_sango

CLIENTS = [
    ("汎用", reporter_generic),
    ("acty", reporter_acty),
    ("samuraiz", reporter_samuraiz),
    ("sango", reporter_sango),
]


def main():
    for name, module in CLIENTS:
        print(f"\n{'=' * 20} {name} {'=' * 20}")
        try:
            module.main()
        except Exception:
            print(f"[{name}] 実行中にエラーが発生しました:")
            traceback.print_exc()


if __name__ == "__main__":
    main()
