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
import fetch_slack_ixrea as reporter_ixrea
import fetch_slack_mirai as reporter_mirai
import fetch_slack_reron as reporter_reron
import fetch_slack_ap_planning as reporter_ap_planning
import fetch_slack_gms as reporter_gms
import fetch_slack_nippongas as reporter_nippongas
import fetch_slack_bridgeplus as reporter_bridgeplus
import fetch_slack_saiyo as reporter_saiyo
import fetch_slack_datarein as reporter_datarein
import fetch_slack_list_request as reporter_list_request
import fetch_slack_exkey_ai as reporter_exkey_ai

CLIENTS = [
    ("汎用", reporter_generic),
    ("acty", reporter_acty),
    ("samuraiz", reporter_samuraiz),
    ("sango", reporter_sango),
    ("ixrea", reporter_ixrea),
    ("mirai", reporter_mirai),
    ("reron", reporter_reron),
    ("ap_planning", reporter_ap_planning),
    ("gms", reporter_gms),
    ("nippongas", reporter_nippongas),
    ("bridgeplus", reporter_bridgeplus),
    ("saiyo", reporter_saiyo),
    ("datarein", reporter_datarein),
    ("list_request", reporter_list_request),
    ("exkey_ai", reporter_exkey_ai),
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
