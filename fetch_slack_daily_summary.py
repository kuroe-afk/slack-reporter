"""
営業リスト関連の日次まとめ
毎日18:00に前日18:00〜当日18:00の投稿を集計してSlackに投稿する
"""

import os
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

SLACK_BOT_TOKEN      = os.getenv("SLACK_BOT_TOKEN")
SLACK_NOTIFY_CHANNEL = os.getenv("DAILY_SUMMARY_CHANNEL_ID")

JST = ZoneInfo("Asia/Tokyo")

# 営業リスト関連のキーワード
KEYWORDS = [
    "リスト", "追加", "タスク", "進捗", "漏れ", "営業", "スプシ", "スプレッドシート",
    "架電", "アポ", "見込み", "資料", "更新", "確認", "対応"
]


def get_time_range():
    """前日18:00〜当日18:00のUnixタイムスタンプを返す"""
    now = datetime.datetime.now(tz=JST)
    today_18 = now.replace(hour=18, minute=0, second=0, microsecond=0)
    yesterday_18 = today_18 - datetime.timedelta(days=1)
    return yesterday_18.timestamp(), today_18.timestamp()


def fetch_all_channels(client):
    """Botが参加している全チャンネルを取得"""
    channels = []
    cursor = None
    while True:
        res = client.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=200,
            cursor=cursor
        )
        for ch in res.get("channels", []):
            if ch.get("is_member"):
                channels.append({"id": ch["id"], "name": ch.get("name", ch["id"])})
        cursor = res.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return channels


def fetch_messages_in_range(client, channel_id, oldest, latest):
    """指定時間範囲のメッセージを取得"""
    try:
        res = client.conversations_history(
            channel=channel_id,
            oldest=str(oldest),
            latest=str(latest),
            limit=200
        )
        return res.get("messages", [])
    except SlackApiError as e:
        if e.response.get("error") not in ("not_in_channel", "channel_not_found"):
            print(f"  チャンネル取得エラー ({channel_id}): {e.response.get('error')}")
        return []


def is_relevant(text):
    """営業リスト関連の投稿か判定"""
    return any(kw in text for kw in KEYWORDS)


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


def build_summary(channel_summaries, oldest_dt, latest_dt):
    date_str = oldest_dt.strftime("%Y/%m/%d")
    header = (
        f":bar_chart: *営業リスト関連 日次まとめ*\n"
        f"対象期間：{oldest_dt.strftime('%m/%d %H:%M')} 〜 {latest_dt.strftime('%m/%d %H:%M')}\n"
        f"{'━' * 40}\n"
    )

    if not channel_summaries:
        return header + "\n該当する投稿はありませんでした。"

    body = ""
    for ch_name, messages in channel_summaries.items():
        body += f"\n*#{ch_name}* （{len(messages)}件）\n"
        for m in messages:
            time_str = m["time"]
            poster   = m["poster"]
            text     = m["text"][:100].replace("\n", " ")
            if len(m["text"]) > 100:
                text += "…"
            body += f"　`{time_str}` {poster}：{text}\n"

    return header + body


def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return
    if not SLACK_NOTIFY_CHANNEL:
        print("エラー: DAILY_SUMMARY_CHANNEL_ID が未設定です")
        return

    client = WebClient(token=SLACK_BOT_TOKEN)
    oldest_ts, latest_ts = get_time_range()
    oldest_dt = datetime.datetime.fromtimestamp(oldest_ts, tz=JST)
    latest_dt = datetime.datetime.fromtimestamp(latest_ts, tz=JST)

    print(f"対象期間: {oldest_dt} 〜 {latest_dt}")

    print("参加チャンネルを取得中...")
    channels = fetch_all_channels(client)
    print(f"  → {len(channels)} チャンネル")

    user_cache = {}
    channel_summaries = {}

    for ch in channels:
        messages = fetch_messages_in_range(client, ch["id"], oldest_ts, latest_ts)
        relevant = []
        for msg in messages:
            text = msg.get("text", "")
            if not text or msg.get("subtype"):
                continue
            if not is_relevant(text):
                continue
            time_str = datetime.datetime.fromtimestamp(
                float(msg.get("ts", 0)), tz=JST
            ).strftime("%H:%M")
            poster = fetch_user_name(client, msg.get("user", ""), user_cache)
            relevant.append({"time": time_str, "poster": poster, "text": text})

        if relevant:
            channel_summaries[ch["name"]] = relevant
            print(f"  #{ch['name']}: {len(relevant)} 件")

    summary_text = build_summary(channel_summaries, oldest_dt, latest_dt)

    try:
        client.chat_postMessage(channel=SLACK_NOTIFY_CHANNEL, text=summary_text)
        print("まとめを投稿しました。")
    except SlackApiError as e:
        print(f"投稿失敗: {e.response.get('error')}")


if __name__ == "__main__":
    main()
