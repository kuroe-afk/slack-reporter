"""
エクスキーAIさま
Slackから報告を取得 → スプシ照合 → クライアント報告用--下書き--へ通知
"""

import os
import re
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

SLACK_BOT_TOKEN      = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID     = "C0BTZ2AJX0A"   # エクスキー--ai
SLACK_NOTIFY_CHANNEL = "C0BC7SYFP36"   # クライアント報告用--下書き--
SLACK_MENTION        = os.getenv("SLACK_MENTION", "")

SPREADSHEET_ID   = "1Qoeef0TvwiiGh8o47IJcqV2tE5NzY-105F-kcvCNrtk"
CREDENTIALS_FILE = "credentials.json"

KEYWORDS     = ["【アポ", "【見込み"]
FETCH_LIMIT  = 50
LAST_TS_FILE = "last_timestamp_exkey_ai.txt"
SOURCE_CHANNEL_NAME = "エクスキー--ai"

CHATWORK_HEADER = (
    "[To:2223905]内田 健人さん\n"
    "\n"
    "お世話になっております。\n"
)

TEMPLATE_APO = (
    CHATWORK_HEADER
    + "下記、アポイント獲得のご報告でございます。\n"
    "恐れ入りますが、ご対応のほどよろしくお願いいたします。\n"
    "[info]\n"
    "{body}\n"
    "[/info]"
)

TEMPLATE_MIKOMI = (
    CHATWORK_HEADER
    + "下記、アポイント見込みのご報告でございます。\n"
    "恐れ入りますが、ご対応のほどよろしくお願いいたします。\n"
    "\n"
    "[info]\n"
    "{body}\n"
    "[/info]"
)


# ── スプレッドシート ──

def open_spreadsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds  = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc     = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def load_sheet(ss, sheet_name, company_col, person_col):
    try:
        rows = ss.worksheet(sheet_name).get_all_values()
    except Exception:
        return []
    records = []
    for row in rows[1:]:
        records.append({
            "会社名":   row[company_col].strip() if len(row) > company_col else "",
            "担当者名": row[person_col].strip()  if len(row) > person_col  else "",
        })
    return records


def normalize(s):
    return re.sub(r'[\s　]', '', s).lower()


def clean_slack_text(text):
    text = re.sub(r'<[^>]+\|([^>]+)>', r'\1', text)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('*', '')
    return text


def extract_field(text, labels):
    for label in labels:
        m = re.search(rf'{label}[\s　]*[：:]\s*(.+)', text)
        if m:
            val = m.group(1).strip()
            val = re.sub(r'<[^>]+\|([^>]+)>', r'\1', val)
            val = re.sub(r'<[^>]+>', '', val)
            return val.strip()
    return ""


def check_in_sheet(records, text):
    slack_company = normalize(extract_field(text, ["会社名", "企業名", "社名"]))
    slack_person  = normalize(extract_field(text, ["担当者名", "氏名", "担当者"]))
    for rec in records:
        if not rec["会社名"]:
            continue
        if normalize(rec["会社名"]) in slack_company or slack_company in normalize(rec["会社名"]):
            if not rec["担当者名"] or normalize(rec["担当者名"])[:2] in slack_person or slack_person in normalize(rec["担当者名"]):
                return rec
    return None


# ── Slack ──

def load_last_timestamp():
    if os.path.exists(LAST_TS_FILE):
        with open(LAST_TS_FILE, "r") as f:
            return f.read().strip()
    return None


def save_last_timestamp(ts):
    with open(LAST_TS_FILE, "w") as f:
        f.write(str(ts))


def fetch_messages(client):
    last_ts  = load_last_timestamp()
    fetch_ts = datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).timestamp()
    if not last_ts:
        print("前回の記録が見つからないため、これ以降の投稿のみを対象にします")
        last_ts = str(fetch_ts)
    else:
        print("前回以降の新着を取得中...")
    res = client.conversations_history(channel=SLACK_CHANNEL_ID, limit=FETCH_LIMIT, oldest=last_ts)
    messages = res.get("messages", [])
    print(f"  → {len(messages)} 件取得")
    return messages, fetch_ts


def filter_messages(messages, bot_user_id):
    filtered = []
    for msg in messages:
        text    = msg.get("text", "")
        matched = [kw for kw in KEYWORDS if kw in text]
        if not matched:
            continue
        reactions = msg.get("reactions", [])
        if any(bot_user_id in r.get("users", []) for r in reactions):
            continue
        filtered.append({
            "投稿日時":           datetime.datetime.fromtimestamp(
                float(msg.get("ts", 0)), tz=ZoneInfo("Asia/Tokyo")
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "投稿者ID":           msg.get("user", "不明"),
            "本文":               text,
            "マッチしたキーワード": "、".join(matched),
            "タイムスタンプ":      msg.get("ts", ""),
        })
    print(f"  → キーワード一致: {len(filtered)} 件")
    return filtered


def fetch_user_name(client, user_id):
    try:
        res     = client.users_info(user=user_id)
        profile = res["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or user_id
    except SlackApiError:
        return user_id


def add_reaction(client, timestamp):
    try:
        client.reactions_add(channel=SLACK_CHANNEL_ID, timestamp=timestamp, name="ballot_box_with_check")
    except SlackApiError as e:
        if e.response.get("error") != "already_reacted":
            print(f"  → リアクション失敗: {e.response.get('error')}")


def post_thread_reply(client, timestamp, sheet_status):
    check_label = ":white_check_mark: スプシ登録済み" if sheet_status == "registered" else ":warning: スプシ未登録（要確認）"
    text = (
        f"{SLACK_MENTION}\n"
        f"【スプシ照合】 {check_label}"
    )
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text, thread_ts=timestamp)
    except SlackApiError as e:
        print(f"  → スレッド返信失敗: {e.response.get('error')}")


def post_to_notify(client, msg, sheet_status, poster_name, draft_text):
    check_label = "✅ スプシ登録済み" if sheet_status == "registered" else "⚠️ スプシ未登録（要確認）"
    text = (
        f"*【元チャンネル】* #{SOURCE_CHANNEL_NAME}　"
        f"*【投稿日時】* {msg['投稿日時']}　"
        f"*【投稿者】* {poster_name}　"
        f"*【スプシ照合】* {check_label}\n"
        f"{'─' * 40}\n"
        f"{draft_text}"
    )
    client.chat_postMessage(channel=SLACK_NOTIFY_CHANNEL, text=text)


# ── メイン ──

def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return

    slack       = WebClient(token=SLACK_BOT_TOKEN)
    bot_user_id = slack.auth_test()["user_id"]

    print("スプレッドシートを読み込み中...")
    try:
        ss             = open_spreadsheet()
        apo_records    = load_sheet(ss, "アポイント取得", company_col=4, person_col=5)
        mikomi_records = load_sheet(ss, "見込み企業",     company_col=4, person_col=5)
        print(f"  → アポ:{len(apo_records)}件 / 見込み:{len(mikomi_records)}件")
    except Exception as e:
        print(f"スプレッドシートエラー: {e}")
        return

    print(f"Slackチャンネル {SLACK_CHANNEL_ID} から取得中...")
    try:
        messages, fetch_ts = fetch_messages(slack)
        filtered           = filter_messages(messages, bot_user_id)

        if not filtered:
            print("新着の対象投稿はありませんでした。")
            save_last_timestamp(fetch_ts)
            return

        for msg in filtered:
            text        = msg["本文"]
            poster_name = fetch_user_name(slack, msg["投稿者ID"])

            try:
                if "【アポ" in text:
                    match  = check_in_sheet(apo_records, text)
                    status = "registered" if match else "unregistered"
                    body   = clean_slack_text(text[text.find("【アポ"):].strip())
                    draft_text = TEMPLATE_APO.format(body=body)
                    post_to_notify(slack, msg, status, poster_name, draft_text)
                    print(f"  → 【アポ】通知投稿完了")

                elif "【見込み" in text:
                    match  = check_in_sheet(mikomi_records, text)
                    status = "registered" if match else "unregistered"
                    body   = clean_slack_text(text[text.find("【見込み"):].strip())
                    draft_text = TEMPLATE_MIKOMI.format(body=body)
                    post_to_notify(slack, msg, status, poster_name, draft_text)
                    print(f"  → 【見込み】通知投稿完了")

                post_thread_reply(slack, msg["タイムスタンプ"], status)
                add_reaction(slack, msg["タイムスタンプ"])

            except Exception as e:
                print(f"  → 処理失敗: {e}")

        save_last_timestamp(fetch_ts)
        print("完了！")

    except SlackApiError as e:
        code = e.response.get("error", "不明")
        print(f"Slack APIエラー: {code}")
        if code == "not_in_channel":
            print("→ Botをチャンネルに招待してください（Slackで /invite @kuroe2）")


if __name__ == "__main__":
    main()
