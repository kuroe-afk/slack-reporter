"""
営業リスト関連の日次まとめ
毎日18:00に前日18:00〜当日18:00の投稿を集計してSlackに投稿する
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
SLACK_NOTIFY_CHANNEL = os.getenv("DAILY_SUMMARY_CHANNEL_ID")

JST = ZoneInfo("Asia/Tokyo")

# リスト追加・作成依頼に関するキーワード（「リスト」と組み合わせで判定）
LIST_KEYWORDS = ["リスト追加", "リスト作成", "追加依頼", "作成依頼", "リストに追加",
                 "リストお願い", "リスト送", "リスト更新", "リスト修正", "リスト確認",
                 "追加リスト", "リストを作", "リストを追", "リストを更", "リストを送",
                 "タブ名", "CSVの整理", "CSV整理", "重複削除"]

# 完了を示すキーワード
DONE_KEYWORDS = ["完了", "追加しました", "作成しました", "送りました", "対応済",
                 "できました", "完成", "更新しました", "修正しました", "確認しました",
                 "架電OK", "OKです", "終わりました", "確認終わり"]

# 未完了・依頼を示すキーワード（定型の結びの挨拶は除外）
PENDING_KEYWORDS = ["依頼", "してほしい", "してください", "お願いします",
                    "確認お願い", "対応お願い", "作成お願い", "追加お願い",
                    "お願いできます", "可能ですか", "いただけます"]


def get_time_range():
    now = datetime.datetime.now(tz=JST)
    today_18 = now.replace(hour=18, minute=0, second=0, microsecond=0)
    yesterday_18 = today_18 - datetime.timedelta(days=1)
    return yesterday_18.timestamp(), today_18.timestamp()


def fetch_bot_user_id(client):
    try:
        res = client.auth_test()
        return res.get("user_id", "")
    except SlackApiError:
        return ""


def fetch_all_channels(client):
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
            print(f"  取得エラー ({channel_id}): {e.response.get('error')}")
        return []


def is_relevant(text):
    return any(kw in text for kw in LIST_KEYWORDS)


def classify(text):
    has_done    = any(kw in text for kw in DONE_KEYWORDS)
    has_pending = any(kw in text for kw in PENDING_KEYWORDS)

    if has_done and not has_pending:
        return "done"
    if has_pending and not has_done:
        return "pending"
    if has_done and has_pending:
        # 両方含む場合は完了キーワードを優先（完了報告に定型句が含まれるケースが多い）
        return "done"
    return "pending"


def clean_text(text):
    text = re.sub(r'<[^>]+\|([^>]+)>', r'\1', text)
    text = re.sub(r'<(https?://[^>]+)>', 'URL', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[ \t]+', ' ', text).strip()
    return text


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


def build_summary(done_items, pending_items, oldest_dt, latest_dt):
    header = (
        f":bar_chart: *営業リスト 日次まとめ*\n"
        f"対象：{oldest_dt.strftime('%m/%d %H:%M')} 〜 {latest_dt.strftime('%m/%d %H:%M')}\n"
        f"{'━' * 40}"
    )

    if not done_items and not pending_items:
        return header + "\n\n該当する投稿はありませんでした。"

    done_section = "\n\n✅ *対応完了*"
    if done_items:
        for item in done_items:
            done_section += (
                f"\n　• `#{item['channel']}` {item['time']} *{item['poster']}*\n"
                f"　　{item['text']}"
            )
    else:
        done_section += "\n　なし"

    pending_section = "\n\n⏳ *未完了・依頼中*"
    if pending_items:
        for item in pending_items:
            pending_section += (
                f"\n　• `#{item['channel']}` {item['time']} *{item['poster']}*\n"
                f"　　{item['text']}"
            )
    else:
        pending_section += "\n　なし"

    return header + done_section + pending_section


def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return
    if not SLACK_NOTIFY_CHANNEL:
        print("エラー: DAILY_SUMMARY_CHANNEL_ID が未設定です")
        return

    client = WebClient(token=SLACK_BOT_TOKEN)
    bot_user_id = fetch_bot_user_id(client)
    oldest_ts, latest_ts = get_time_range()
    oldest_dt = datetime.datetime.fromtimestamp(oldest_ts, tz=JST)
    latest_dt = datetime.datetime.fromtimestamp(latest_ts, tz=JST)

    print(f"対象期間: {oldest_dt} 〜 {latest_dt}")

    print("参加チャンネルを取得中...")
    channels = fetch_all_channels(client)
    print(f"  → {len(channels)} チャンネル")

    user_cache = {}
    done_items    = []
    pending_items = []

    for ch in channels:
        messages = fetch_messages_in_range(client, ch["id"], oldest_ts, latest_ts)
        for msg in messages:
            # ボット自身の投稿・システムメッセージを除外
            if msg.get("bot_id") or msg.get("subtype"):
                continue
            if msg.get("user") == bot_user_id:
                continue

            text = msg.get("text", "")
            if not text or not is_relevant(text):
                continue

            time_str = datetime.datetime.fromtimestamp(
                float(msg.get("ts", 0)), tz=JST
            ).strftime("%H:%M")
            poster   = fetch_user_name(client, msg.get("user", ""), user_cache)
            cleaned  = clean_text(text)
            status   = classify(text)

            item = {
                "channel": ch["name"],
                "time":    time_str,
                "poster":  poster,
                "text":    cleaned,
            }
            if status == "done":
                done_items.append(item)
            else:
                pending_items.append(item)

    print(f"完了: {len(done_items)} 件 / 未完了: {len(pending_items)} 件")

    summary_text = build_summary(done_items, pending_items, oldest_dt, latest_dt)

    try:
        client.chat_postMessage(channel=SLACK_NOTIFY_CHANNEL, text=summary_text)
        print("まとめを投稿しました。")
    except SlackApiError as e:
        print(f"投稿失敗: {e.response.get('error')}")


if __name__ == "__main__":
    main()
