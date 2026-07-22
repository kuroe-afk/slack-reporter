"""
岡・入井のやり取り要約
対象3チャンネルから岡・入井のスレッドを抽出し翌朝8時に1メッセージで報告
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
SLACK_NOTIFY_CHANNEL = "C0BG7NQ5Y3W"

JST = ZoneInfo("Asia/Tokyo")

TARGET_CHANNELS = ["C07DE0GFZPV", "C0ADL8GR4NT", "C04ERALDWR4"]
OKA_ID  = "U02KSNGJJ4C"
IRII_ID = "U08MRMAD2MC"
TARGET_USERS = {OKA_ID, IRII_ID}

# 挨拶・定型文（除去対象）
GREETING_PATTERNS = [
    r'^お疲れ様です[。！!]*\s*', r'^おはようございます[。！!]*\s*',
    r'^こんにちは[。！!]*\s*', r'^よろしくお願いいたします[。！!]*\s*',
    r'^よろしくお願いします[。！!]*\s*', r'^お世話になっております[。！!]*\s*',
]


def get_time_range():
    now = datetime.datetime.now(tz=JST)
    today_8 = now.replace(hour=8, minute=0, second=0, microsecond=0)
    yesterday_0 = (today_8 - datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return yesterday_0.timestamp(), today_8.timestamp()


def fetch_channel_name(client, channel_id):
    try:
        res = client.conversations_info(channel=channel_id)
        return res["channel"].get("name", channel_id)
    except SlackApiError:
        return channel_id


def fetch_user_name(client, user_id, cache):
    if user_id in cache:
        return cache[user_id]
    try:
        res = client.users_info(user=user_id)
        profile = res["user"]["profile"]
        name = profile.get("display_name") or profile.get("real_name") or user_id
    except SlackApiError:
        name = user_id
    cache[user_id] = name
    return name


def fetch_messages_in_range(client, channel_id, oldest, latest):
    try:
        res = client.conversations_history(
            channel=channel_id,
            oldest=str(oldest),
            latest=str(latest),
            limit=200
        )
        return res.get("messages", [])
    except SlackApiError as e:
        print(f"  取得エラー ({channel_id}): {e.response.get('error')}")
        return []


def fetch_thread_replies(client, channel_id, thread_ts):
    try:
        res = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=100)
        return res.get("messages", [])
    except SlackApiError:
        return []


def clean_text(text):
    text = re.sub(r'<[^>]+\|([^>]+)>', r'\1', text)
    text = re.sub(r'<(https?://[^>]+)>', 'URL', text)
    text = re.sub(r'<[^>]+>', '', text)
    # 挨拶を除去
    for pat in GREETING_PATTERNS:
        text = re.sub(pat, '', text, flags=re.MULTILINE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return

    client = WebClient(token=SLACK_BOT_TOKEN)
    oldest_ts, latest_ts = get_time_range()
    oldest_dt = datetime.datetime.fromtimestamp(oldest_ts, tz=JST)

    print(f"対象期間: {oldest_dt.strftime('%m/%d')} 00:00 〜 08:00")

    user_cache = {}
    sections   = []

    for ch_id in TARGET_CHANNELS:
        ch_name  = fetch_channel_name(client, ch_id)
        messages = fetch_messages_in_range(client, ch_id, oldest_ts, latest_ts)
        print(f"  #{ch_name}: {len(messages)} 件取得")

        processed_threads = set()
        ch_lines = []

        for msg in messages:
            uid       = msg.get("user", "")
            thread_ts = msg.get("thread_ts") or msg.get("ts")

            if uid not in TARGET_USERS:
                continue
            if thread_ts in processed_threads:
                continue
            processed_threads.add(thread_ts)

            thread_msgs = fetch_thread_replies(client, ch_id, thread_ts)
            if not thread_msgs:
                thread_msgs = [msg]

            for m in thread_msgs:
                m_uid = m.get("user", "")
                m_ts  = float(m.get("ts", 0))
                if m_ts < oldest_ts or m_ts > latest_ts:
                    continue
                if m_uid not in TARGET_USERS:
                    continue
                text = clean_text(m.get("text", ""))
                if not text:
                    continue
                time_str = datetime.datetime.fromtimestamp(m_ts, tz=JST).strftime("%H:%M")
                name     = fetch_user_name(client, m_uid, user_cache)
                ch_lines.append(f"　`{time_str}` {name}：{text}")

        if ch_lines:
            sections.append(f"*#{ch_name}*\n" + "\n".join(ch_lines))

    if not sections:
        print("該当するやり取りはありませんでした。")
        return

    date_str = oldest_dt.strftime("%m/%d")
    text = (
        f":speech_balloon: *岡・入井 やり取り要約* `{date_str}`\n"
        f"{'━' * 40}\n"
        + "\n\n".join(sections)
    )

    try:
        client.chat_postMessage(channel=SLACK_NOTIFY_CHANNEL, text=text)
        print("投稿完了！")
    except SlackApiError as e:
        print(f"投稿失敗: {e.response.get('error')}")


if __name__ == "__main__":
    main()
