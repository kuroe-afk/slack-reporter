"""
Slackの監視対象チャンネルを確認し、
「メンションされたのに返信がない」「質問形なのに反応がない」投稿を
対応漏れとしてレポート用チャンネルに通知するスクリプト
"""

import os
import re
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

SLACK_BOT_TOKEN      = os.getenv("MONITOR_SLACK_BOT_TOKEN")
REPORT_CHANNEL_ID    = os.getenv("MONITOR_REPORT_CHANNEL_ID")
TARGET_CHANNEL_IDS   = [c.strip() for c in os.getenv("MONITOR_TARGET_CHANNEL_IDS", "").split(",") if c.strip()]

# 何時間返信がなければ「対応漏れ」とみなすか
THRESHOLD_HOURS = 3

# 何時間さかのぼってチェックするか(古すぎる投稿は毎回re-flagしない)
LOOKBACK_HOURS = 72

MENTION_PATTERN = re.compile(r"<@[UW][A-Z0-9]+>")

QUESTION_SUFFIX_PATTERNS = [
    r"\?", r"？",
    r"ですか", r"ますか", r"でしょうか", r"でしたか", r"んですか",
    r"かな", r"かね",
]
QUESTION_PATTERN = re.compile(
    r"(" + "|".join(QUESTION_SUFFIX_PATTERNS) + r")[。.!! ]*$"
)


def is_flag_candidate(text):
    """メンション付き、または文末が質問形かどうか"""
    if MENTION_PATTERN.search(text):
        return True
    last_line = text.strip().splitlines()[-1] if text.strip() else ""
    if QUESTION_PATTERN.search(last_line.strip()):
        return True
    return False


def is_responded(msg):
    """スレッド返信 or リアクションが付いていれば対応済みとみなす"""
    if msg.get("reply_count", 0) > 0:
        return True
    if msg.get("reactions"):
        return True
    return False


def fetch_channel_name(client, channel_id):
    try:
        res = client.conversations_info(channel=channel_id)
        return res["channel"].get("name", channel_id)
    except SlackApiError:
        return channel_id


def fetch_user_name(client, user_id):
    if not user_id:
        return "不明"
    try:
        res = client.users_info(user=user_id)
        profile = res["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or user_id
    except SlackApiError:
        return user_id


def fetch_recent_messages(client, channel_id, oldest_ts):
    messages = []
    cursor = None
    while True:
        kwargs = {"channel": channel_id, "oldest": oldest_ts, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        res = client.conversations_history(**kwargs)
        messages.extend(res.get("messages", []))
        cursor = res.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


def get_permalink(client, channel_id, ts):
    try:
        res = client.conversations_getPermalink(channel=channel_id, message_ts=ts)
        return res.get("permalink", "")
    except SlackApiError:
        return ""


def format_elapsed(ts):
    posted = datetime.datetime.fromtimestamp(float(ts), tz=ZoneInfo("Asia/Tokyo"))
    now = datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo"))
    delta = now - posted
    hours = int(delta.total_seconds() // 3600)
    return posted.strftime("%Y-%m-%d %H:%M"), hours


def check_channel(client, channel_id, now_ts):
    oldest_ts = str(now_ts - LOOKBACK_HOURS * 3600)
    channel_name = fetch_channel_name(client, channel_id)

    try:
        messages = fetch_recent_messages(client, channel_id, oldest_ts)
    except SlackApiError as e:
        code = e.response.get("error", "不明")
        print(f"  → チャンネル {channel_id} 取得失敗: {code}")
        return channel_name, [], code

    flagged = []
    for msg in messages:
        if msg.get("subtype"):
            continue
        text = msg.get("text", "")
        if not text:
            continue
        if not is_flag_candidate(text):
            continue
        if is_responded(msg):
            continue

        posted_str, hours = format_elapsed(msg["ts"])
        if hours < THRESHOLD_HOURS:
            continue

        poster = fetch_user_name(client, msg.get("user"))
        permalink = get_permalink(client, channel_id, msg["ts"])
        flagged.append({
            "channel_name": channel_name,
            "poster": poster,
            "posted_str": posted_str,
            "hours": hours,
            "text": text,
            "permalink": permalink,
        })

    return channel_name, flagged, None


def build_report(all_flagged, errors):
    if not all_flagged and not errors:
        today = datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
        return f"*【対応漏れチェック】{today}*\n本日確認した範囲では、対応漏れは見つかりませんでした。"

    lines = [f"*【対応漏れチェック】{datetime.datetime.now(tz=ZoneInfo('Asia/Tokyo')).strftime('%Y-%m-%d')}*"]
    if all_flagged:
        lines.append(f"対応が必要と思われる投稿が {len(all_flagged)} 件あります。\n")
        for item in all_flagged:
            lines.append(
                f"・#{item['channel_name']}　{item['posted_str']}(経過 {item['hours']}時間)　投稿者: {item['poster']}\n"
                f"  > {item['text'][:100]}\n"
                f"  {item['permalink']}"
            )
    else:
        lines.append("未対応の投稿は見つかりませんでした。")

    if errors:
        lines.append("\n⚠️ 一部チャンネルの確認に失敗しました:")
        for ch, code in errors:
            lines.append(f"  ・{ch}: {code}")

    return "\n".join(lines)


def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: .env の MONITOR_SLACK_BOT_TOKEN を設定してください")
        return
    if not REPORT_CHANNEL_ID:
        print("エラー: .env の MONITOR_REPORT_CHANNEL_ID を設定してください")
        return
    if not TARGET_CHANNEL_IDS:
        print("エラー: .env の MONITOR_TARGET_CHANNEL_IDS を設定してください")
        return

    client = WebClient(token=SLACK_BOT_TOKEN)
    now_ts = datetime.datetime.now().timestamp()

    all_flagged = []
    errors = []

    for channel_id in TARGET_CHANNEL_IDS:
        print(f"チャンネル {channel_id} を確認中...")
        channel_name, flagged, error = check_channel(client, channel_id, now_ts)
        if error:
            errors.append((channel_name, error))
            continue
        print(f"  → 対応漏れ候補: {len(flagged)} 件")
        all_flagged.extend(flagged)

    report_text = build_report(all_flagged, errors)

    try:
        client.chat_postMessage(channel=REPORT_CHANNEL_ID, text=report_text)
        print("\nレポートを投稿しました。")
    except SlackApiError as e:
        code = e.response.get("error", "不明")
        print(f"\nレポート投稿失敗: {code}")
        if code == "not_in_channel":
            print("→ Botをレポート投稿先チャンネルに招待してください（Slackで /invite @gyomu_monitor_bot）")


if __name__ == "__main__":
    main()
