"""
岡・入井のやり取り要約
対象3チャンネルから岡・入井のスレッドを抽出し翌朝8時に報告
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
OKA_ID   = "U02KSNGJJ4C"
IRII_ID  = "U08MRMAD2MC"
TARGET_USERS = {OKA_ID, IRII_ID}


def get_time_range():
    """昨日00:00〜今日08:00"""
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
        res = client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            limit=100
        )
        return res.get("messages", [])
    except SlackApiError:
        return []


def clean_text(text):
    text = re.sub(r'<[^>]+\|([^>]+)>', r'\1', text)
    text = re.sub(r'<(https?://[^>]+)>', 'URL', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def build_thread_summary(thread_msgs, user_cache, client, oldest, latest):
    """スレッド内のやり取りを会話形式で整形"""
    lines = []
    for msg in thread_msgs:
        uid  = msg.get("user", "")
        ts   = float(msg.get("ts", 0))
        # 時間範囲外は除外
        if ts < oldest or ts > latest:
            continue
        if uid not in TARGET_USERS:
            continue
        time_str = datetime.datetime.fromtimestamp(ts, tz=JST).strftime("%H:%M")
        name     = fetch_user_name(client, uid, user_cache)
        text     = clean_text(msg.get("text", ""))
        if text:
            lines.append(f"　`{time_str}` *{name}*：{text}")
    return lines


def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return

    client = WebClient(token=SLACK_BOT_TOKEN)
    oldest_ts, latest_ts = get_time_range()
    oldest_dt = datetime.datetime.fromtimestamp(oldest_ts, tz=JST)
    latest_dt = datetime.datetime.fromtimestamp(latest_ts, tz=JST)

    print(f"対象期間: {oldest_dt} 〜 {latest_dt}")

    user_cache   = {}
    found_threads = []

    for ch_id in TARGET_CHANNELS:
        ch_name  = fetch_channel_name(client, ch_id)
        messages = fetch_messages_in_range(client, ch_id, oldest_ts, latest_ts)
        print(f"  #{ch_name}: {len(messages)} 件取得")

        # スレッド親メッセージを収集（岡か入井が投稿したもの）
        processed_threads = set()

        for msg in messages:
            uid      = msg.get("user", "")
            thread_ts = msg.get("thread_ts") or msg.get("ts")

            # 岡か入井が関与していないメッセージはスキップ
            if uid not in TARGET_USERS:
                continue
            if thread_ts in processed_threads:
                continue
            processed_threads.add(thread_ts)

            # スレッド全体を取得
            thread_msgs = fetch_thread_replies(client, ch_id, thread_ts)
            if not thread_msgs:
                thread_msgs = [msg]

            # スレッド内に岡・入井両方が含まれているか確認
            thread_users = {m.get("user") for m in thread_msgs}
            has_oka  = OKA_ID in thread_users
            has_irii = IRII_ID in thread_users

            # 片方しかいないスレッドも含める（動きがあれば報告）
            summary_lines = build_thread_summary(thread_msgs, user_cache, client, oldest_ts, latest_ts)
            if not summary_lines:
                continue

            parent_time = datetime.datetime.fromtimestamp(float(thread_ts), tz=JST).strftime("%H:%M")
            members = []
            if has_oka:
                members.append("岡")
            if has_irii:
                members.append("入井")

            found_threads.append({
                "channel": ch_name,
                "time":    parent_time,
                "members": "・".join(members),
                "lines":   summary_lines,
            })

    if not found_threads:
        print("該当するやり取りはありませんでした。活動のある日のみ投稿します。")
        return

    # 各スレッドを別メッセージで投稿
    date_str = oldest_dt.strftime("%m/%d")
    for t in found_threads:
        header = (
            f":speech_balloon: *岡・入井 やり取り要約* `{date_str}`\n"
            f"`#{t['channel']}` {t['time']} 【{t['members']}】\n"
            f"{'─' * 35}"
        )
        body   = "\n".join(t["lines"])
        text   = header + "\n" + body

        try:
            client.chat_postMessage(channel=SLACK_NOTIFY_CHANNEL, text=text)
            print(f"  投稿: #{t['channel']} {t['time']}")
        except SlackApiError as e:
            print(f"  投稿失敗: {e.response.get('error')}")

    print("完了！")


if __name__ == "__main__":
    main()
