"""
GMS（Global Mobility Service）さま
Slackから報告を取得 → スプシ照合 → 専用チャンネルへ通知
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
SLACK_CHANNEL_ID     = os.getenv("GMS_SLACK_CHANNEL_ID")
SLACK_NOTIFY_CHANNEL = os.getenv("GMS_NOTIFY_CHANNEL_ID")
SLACK_MENTION        = os.getenv("SLACK_MENTION", "")

SPREADSHEET_ID   = os.getenv("GMS_SPREADSHEET_ID")
CREDENTIALS_FILE = "credentials.json"

SOURCE_CHANNEL_NAME = "sangosama-global-mobility-service"

KEYWORDS     = ["【アポ", "【資料"]
FETCH_LIMIT  = 50
LAST_TS_FILE = "last_timestamp_gms.txt"

RECIPIENT_HEADER = "<@U08KSKKSL5Q>"

TEMPLATE_APO = (
    f"{RECIPIENT_HEADER}\n\n"
    "お世話になっております。\n"
    "下記、アポイント獲得の報告でございます。\n"
    "恐れ入りますが、ご対応のほどよろしくお願いいたします。\n\n"
    "```{body}```"
)

TEMPLATE_SHIRYO = (
    f"{RECIPIENT_HEADER}\n\n"
    "お世話になっております。\n"
    "下記、資料送付依頼でございます。\n"
    "恐れ入りますが、ご対応のほどよろしくお願いいたします。\n\n"
    "```{body}```"
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
            "担当者名": row[person_col].strip() if len(row) > person_col else "",
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
        match = re.search(rf'{label}[\s　]*[：:]\s*(.+)', text)
        if match:
            val = match.group(1).strip()
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


def save_last_timestamp(ts):
    """今回チェックした時刻を保存する（投稿が0件でも必ず保存する）"""
    with open(LAST_TS_FILE, "w") as f:
        f.write(str(ts))


def fetch_messages(client):
    last_ts = load_last_timestamp()
    fetch_ts = datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).timestamp()
    if not last_ts:
        # 初回は「今この瞬間」以降のみを対象にする（過去の投稿を一切処理しないための安全策）
        last_ts = str(fetch_ts)
    response = client.conversations_history(channel=SLACK_CHANNEL_ID, limit=FETCH_LIMIT, oldest=last_ts)
    messages = response.get("messages", [])
    print(f"  → {len(messages)} 件取得")
    return messages, fetch_ts


def filter_messages(messages, bot_user_id):
    filtered = []
    for msg in messages:
        text = msg.get("text", "")
        matched = [kw for kw in KEYWORDS if kw in text]
        if not matched:
            continue
        reactions = msg.get("reactions", [])
        if any(bot_user_id in r.get("users", []) for r in reactions):
            continue  # 既に処理済み（ボット自身のリアクションが付いている）
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


def extract_body(text, keyword):
    idx = text.find(keyword)
    if idx == -1:
        return text
    return text[idx:].strip()


def build_draft(msg):
    text = msg["本文"]
    if "【アポ" in text:
        body = clean_slack_text(extract_body(text, "【アポ"))
        return TEMPLATE_APO.format(body=body)
    elif "【資料" in text:
        body = clean_slack_text(extract_body(text, "【資料"))
        return TEMPLATE_SHIRYO.format(body=body)
    return text


def add_reaction(client, timestamp):
    try:
        client.reactions_add(channel=SLACK_CHANNEL_ID, timestamp=timestamp, name="ballot_box_with_check")
    except SlackApiError as e:
        if e.response.get("error") != "already_reacted":
            print(f"  → リアクション失敗: {e.response.get('error')}")


def post_to_slack(client, msg, sheet_status, poster_name):
    draft = build_draft(msg)
    check_label = "✅ スプシ登録済み" if sheet_status == "registered" else "⚠️ スプシ未登録（要確認）"

    text = (
        f"{SLACK_MENTION}\n"
        f"*【元チャンネル】* #{SOURCE_CHANNEL_NAME}　"
        f"*【投稿日時】* {msg['投稿日時']}　"
        f"*【投稿者】* {poster_name}　"
        f"*【スプシ照合】* {check_label}\n"
        f"{'─' * 40}\n"
        f"{draft}"
    )
    client.chat_postMessage(channel=SLACK_NOTIFY_CHANNEL, text=text)


# ── メイン ──

def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return
    if not SLACK_CHANNEL_ID:
        print("エラー: GMS_SLACK_CHANNEL_ID が未設定です")
        return
    if not SLACK_NOTIFY_CHANNEL:
        print("エラー: GMS_NOTIFY_CHANNEL_ID が未設定です")
        return
    if not SPREADSHEET_ID:
        print("エラー: GMS_SPREADSHEET_ID が未設定です")
        return

    slack = WebClient(token=SLACK_BOT_TOKEN)
    bot_user_id = slack.auth_test()["user_id"]

    print("スプレッドシートを読み込み中...")
    try:
        ss = open_spreadsheet()
        apo_records    = load_sheet(ss, "アポイント",     company_col=6, person_col=7)
        shiryo_records = load_sheet(ss, "資料送付依頼",   company_col=7, person_col=8)
        print(f"  → アポ:{len(apo_records)}件 / 資料:{len(shiryo_records)}件")
    except Exception as e:
        print(f"スプレッドシート読み込みエラー: {e}")
        return

    print(f"Slackチャンネル {SLACK_CHANNEL_ID} から取得中...")
    try:
        messages, fetch_ts = fetch_messages(slack)
        filtered = filter_messages(messages, bot_user_id)

        if not filtered:
            print("キーワードに一致する投稿はありませんでした。")
            save_last_timestamp(fetch_ts)
            return

        print("\n通知チャンネルへ投稿中...")
        for msg in filtered:
            text = msg["本文"]

            if "【アポ" in text:
                match = check_in_sheet(apo_records, text)
            elif "【資料" in text:
                match = check_in_sheet(shiryo_records, text)
            else:
                match = None

            status = "registered" if match else "unregistered"
            poster_name = fetch_user_name(slack, msg["投稿者ID"])

            try:
                post_to_slack(slack, msg, status, poster_name)
                add_reaction(slack, msg["タイムスタンプ"])
            except SlackApiError as e:
                print(f"  → 投稿失敗: {e.response.get('error')}")

        save_last_timestamp(fetch_ts)
        print("完了！")

    except SlackApiError as e:
        code = e.response.get("error", "不明")
        print(f"Slack APIエラー: {code}")
        if code == "not_in_channel":
            print("→ Botをチャンネルに招待してください（Slackで /invite @kuroe）")
        elif code == "missing_scope":
            print(f"→ 権限不足: {e.response.get('needed')}")


if __name__ == "__main__":
    main()
