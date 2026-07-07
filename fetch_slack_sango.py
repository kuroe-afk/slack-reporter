"""
サンゴさま / アトムチェーン本部さま
Slackから報告を取得 → クライアント報告用チャンネルへ通知
"""

import os
import re
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

SLACK_BOT_TOKEN      = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID     = os.getenv("SANGO_SLACK_CHANNEL_ID")
SLACK_NOTIFY_CHANNEL = os.getenv("SLACK_NOTIFY_CHANNEL_ID")
SLACK_MENTION        = os.getenv("SLACK_MENTION", "")

KEYWORDS = ["【アポ", "【見込み", "【資料"]

FETCH_LIMIT  = 50
LAST_TS_FILE = "last_timestamp_sango.txt"

CHATWORK_HEADER = (
    "[To:8204288]明尾(メオ)正幸＜080-7603-6897＞さん\n"
    "[To:9104692]高橋　圭子さん\n"
    "[To:9819640]能美光太郎さん\n"
    "[To:9372436]中尾敏之さん\n"
    "\n"
    "お世話になっております。\n"
)

TEMPLATE_APO = (
    CHATWORK_HEADER
    + "下記、アポイント獲得のご報告でございます。\n"
    "恐れ入りますが、ご対応のほどよろしくお願いいたします。\n"
    "[info]{body}[/info]"
)

TEMPLATE_MIKOMI = (
    CHATWORK_HEADER
    + "下記、アポイント見込みのご報告でございます。\n"
    "恐れ入りますが、ご対応のほどよろしくお願いいたします。\n"
    "\n"
    "[info]{body}[/info]"
)

TEMPLATE_SHIRYO = (
    CHATWORK_HEADER
    + "下記、資料送付依頼でございます。\n"
    "恐れ入りますが、ご対応のほどよろしくお願いいたします。\n"
    "[info]{body}[/info]"
)


# ──────────────────────────────────────
# Slack 取得
# ──────────────────────────────────────

def fetch_user_name(client, user_id):
    try:
        res = client.users_info(user=user_id)
        profile = res["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or user_id
    except SlackApiError:
        return user_id


def load_last_timestamp():
    if os.path.exists(LAST_TS_FILE):
        with open(LAST_TS_FILE, "r") as f:
            return f.read().strip()
    return None


def save_last_timestamp(messages):
    if messages:
        latest_ts = messages[0].get("ts", "")
        with open(LAST_TS_FILE, "w") as f:
            f.write(latest_ts)


def fetch_messages(client):
    last_ts = load_last_timestamp()
    if last_ts:
        print("前回以降の新着を取得中...")
        response = client.conversations_history(
            channel=SLACK_CHANNEL_ID, limit=FETCH_LIMIT, oldest=last_ts
        )
    else:
        print(f"最新{FETCH_LIMIT}件を取得中（初回）...")
        response = client.conversations_history(
            channel=SLACK_CHANNEL_ID, limit=FETCH_LIMIT
        )
    messages = response.get("messages", [])
    save_last_timestamp(messages)
    print(f"  → {len(messages)} 件取得")
    return messages


def filter_messages(messages):
    filtered = []
    for msg in messages:
        text = msg.get("text", "")
        matched = [kw for kw in KEYWORDS if kw in text]
        if matched:
            filtered.append({
                "投稿日時": datetime.datetime.fromtimestamp(
                    float(msg.get("ts", 0)), tz=ZoneInfo("Asia/Tokyo")
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "投稿者ID": msg.get("user", "不明"),
                "本文": text,
                "マッチしたキーワード": "、".join(matched),
                "タイムスタンプ": msg.get("ts", ""),
            })
    print(f"  → キーワード一致: {len(filtered)} 件")
    return filtered


# ──────────────────────────────────────
# Slack 通知
# ──────────────────────────────────────

def extract_body(text, keyword):
    idx = text.find(keyword)
    if idx == -1:
        return text
    return text[idx:].strip()


def clean_slack_text(text):
    text = re.sub(r'<[^>]+\|([^>]+)>', r'\1', text)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    return text


def build_draft(msg):
    text = msg["本文"]
    if "【アポ" in text:
        body = clean_slack_text(extract_body(text, "【アポ"))
        return TEMPLATE_APO.format(body=body)
    elif "【資料" in text:
        body = clean_slack_text(extract_body(text, "【資料"))
        return TEMPLATE_SHIRYO.format(body=body)
    elif "【見込み" in text:
        body = clean_slack_text(extract_body(text, "【見込み"))
        return TEMPLATE_MIKOMI.format(body=body)
    return text


def add_reaction(client, timestamp, emoji):
    try:
        client.reactions_add(channel=SLACK_CHANNEL_ID, timestamp=timestamp, name=emoji)
    except SlackApiError as e:
        if e.response.get("error") != "already_reacted":
            print(f"  → リアクション失敗: {e.response.get('error')}")


def post_to_slack(client, msg, poster_name):
    draft = build_draft(msg)

    text = (
        f"{SLACK_MENTION}\n"
        f"*【元チャンネル】* #sangosama-アトムチェーン本部sama　"
        f"*【投稿日時】* {msg['投稿日時']}　"
        f"*【投稿者】* {poster_name}　"
        f"*【スプシ照合】* :warning: スプシ未確認\n"
        f"{'─' * 40}\n"
        f"{draft}"
    )
    client.chat_postMessage(channel=SLACK_NOTIFY_CHANNEL, text=text)


# ──────────────────────────────────────
# メイン
# ──────────────────────────────────────

def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return
    if not SLACK_CHANNEL_ID:
        print("エラー: SANGO_SLACK_CHANNEL_ID が未設定です")
        return
    if not SLACK_NOTIFY_CHANNEL:
        print("エラー: SLACK_NOTIFY_CHANNEL_ID が未設定です")
        return

    slack = WebClient(token=SLACK_BOT_TOKEN)

    try:
        messages = fetch_messages(slack)
        filtered = filter_messages(messages)

        if not filtered:
            print("キーワードに一致する投稿はありませんでした。")
            return

        print("\n通知チャンネルへ投稿中...")
        for msg in filtered:
            poster_name = fetch_user_name(slack, msg["投稿者ID"])
            try:
                post_to_slack(slack, msg, poster_name)
                add_reaction(slack, msg["タイムスタンプ"], "ballot_box_with_check")
            except SlackApiError as e:
                print(f"  → 投稿失敗: {e.response.get('error')}")

        print("完了！")

    except SlackApiError as e:
        code = e.response.get("error", "不明")
        print(f"Slack APIエラー: {code}")
        if code == "not_in_channel":
            print("→ ボットをチャンネルに招待してください")
        elif code == "invalid_auth":
            print("→ トークンを確認してください")
        else:
            print(f"→ 詳細: {e.response}")


if __name__ == "__main__":
    main()
