"""
本日架電案件サマリー
平日9時に各チャンネルの【本日こちら架電お願いします】を収集し、事務専用チャンネルへ通知
"""

import os
import re
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
NOTIFY_CHANNEL  = "C07DE0GFZPV"   # 事務専用チャンネル
OKA_USER_ID     = "U02KSNGJJ4C"   # 岡菜保
KEYWORD         = "【本日こちら架電お願いします】"


def get_today_oldest():
    """今日0時（JST）のUNIXタイムスタンプを返す"""
    jst = ZoneInfo("Asia/Tokyo")
    now = datetime.datetime.now(tz=jst)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return str(today_midnight.timestamp())


def get_channel_name(client, channel_id):
    try:
        res = client.conversations_info(channel=channel_id)
        return res["channel"].get("name", channel_id)
    except SlackApiError:
        return channel_id


def get_display_name(client, user_id):
    try:
        res = client.users_info(user=user_id)
        profile = res["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or user_id
    except SlackApiError:
        return user_id


def extract_assignees(text):
    """
    メッセージから架電担当者のユーザーIDを抽出する
    - KEYWORDより前にあるメンション（先頭）は除外
    - 「CC」が同行にあるメンション（CC扱い）は除外
    """
    keyword_pos = text.find(KEYWORD)
    if keyword_pos == -1:
        return []

    # KEYWORD以降のテキストのみ対象
    after_keyword = text[keyword_pos:]

    assignees = []
    for line in after_keyword.splitlines():
        # CC行はスキップ
        if re.search(r'[Ｃc][Ｃc]|CC|cc', line, re.IGNORECASE):
            continue
        for uid in re.findall(r'<@([A-Z0-9]+)>', line):
            if uid not in assignees:
                assignees.append(uid)

    return assignees


def scan_channels(client):
    """botが参加しているチャンネルを全スキャンして架電案件を収集"""
    oldest = get_today_oldest()
    results = []

    try:
        res = client.users_conversations(
            types="public_channel,private_channel", limit=200, exclude_archived=True
        )
        channels = res.get("channels", [])
    except SlackApiError as e:
        print(f"チャンネル一覧取得失敗: {e.response.get('error')}")
        return results

    print(f"  → {len(channels)} チャンネルをスキャン")

    for ch in channels:
        ch_id   = ch["id"]
        ch_name = ch.get("name", ch_id)
        try:
            msgs = client.conversations_history(
                channel=ch_id, oldest=oldest, limit=50
            ).get("messages", [])
        except SlackApiError:
            continue

        for msg in msgs:
            text = msg.get("text", "")
            if KEYWORD not in text:
                continue
            assignees = extract_assignees(text)
            if assignees:
                results.append({
                    "channel_name": ch_name,
                    "assignees":    assignees,
                })
                print(f"  → {ch_name}: {assignees}")

    return results


def build_summary(client, items):
    """通知テキストを組み立てる"""
    total = len(items)
    lines = [
        f"<@{OKA_USER_ID}>",
        "本日の架電案件一覧です。",
        f"架電案件合計：{total}案件",
        "▼各案件詳細",
    ]
    for item in items:
        names = "　".join(
            f"@{get_display_name(client, uid)}" for uid in item["assignees"]
        )
        lines.append(f"・{item['channel_name']}　{names}")
    return "\n".join(lines)


def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return

    client = WebClient(token=SLACK_BOT_TOKEN)

    print("架電案件を収集中...")
    items = scan_channels(client)

    if not items:
        print("本日の架電案件は見つかりませんでした。")
        return

    summary = build_summary(client, items)
    print("\n--- 通知内容 ---")
    print(summary)
    print("----------------")

    try:
        client.chat_postMessage(channel=NOTIFY_CHANNEL, text=summary)
        print("通知完了！")
    except SlackApiError as e:
        print(f"通知失敗: {e.response.get('error')}")


if __name__ == "__main__":
    main()
