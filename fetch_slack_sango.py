"""
サンゴさま / アトムチェーン本部さま
Slackから報告を取得 → スプシ照合 → クライアント報告用チャンネルへ通知
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
SLACK_CHANNEL_ID     = os.getenv("SANGO_SLACK_CHANNEL_ID")
SLACK_NOTIFY_CHANNEL = os.getenv("SLACK_NOTIFY_CHANNEL_ID")
SLACK_MENTION        = os.getenv("SLACK_MENTION", "")

SPREADSHEET_ID   = os.getenv("SANGO_SPREADSHEET_ID")
CREDENTIALS_FILE = "credentials.json"

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
# スプレッドシート
# ──────────────────────────────────────

def open_spreadsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def load_apo_records(sheet):
    """アポイント取得企業: E=企業名(4)"""
    rows = sheet.get_all_values()
    records = []
    for row in rows[1:]:
        company = row[4].strip() if len(row) > 4 else ""
        if company:
            records.append({"企業名": company})
    return records


def load_mikomi_records(sheet):
    """見込み企業: F=社名(5)"""
    rows = sheet.get_all_values()
    records = []
    for row in rows[1:]:
        company = row[5].strip() if len(row) > 5 else ""
        if company:
            records.append({"企業名": company})
    return records


def load_shiryo_records(sheet):
    """資料送付: E=企業名(4)"""
    rows = sheet.get_all_values()
    records = []
    for row in rows[1:]:
        company = row[4].strip() if len(row) > 4 else ""
        if company:
            records.append({"企業名": company})
    return records


def normalize(s):
    return re.sub(r'[\s　]', '', s).lower()


def extract_company_from_slack(text):
    for label in ["会社名", "企業名", "社名"]:
        match = re.search(rf'{label}[\s　]*[：:]\s*(.+)', text)
        if match:
            val = match.group(1).strip()
            val = re.sub(r'<[^>]+\|([^>]+)>', r'\1', val)
            val = re.sub(r'<[^>]+>', '', val)
            return val.strip()
    return ""


def check_in_sheet(records, text):
    slack_company = normalize(extract_company_from_slack(text))
    if not slack_company:
        return None
    for rec in records:
        sheet_company = normalize(rec["企業名"])
        if not sheet_company:
            continue
        if sheet_company in slack_company or slack_company in sheet_company:
            return rec
    return None


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
    if not last_ts:
        # キャッシュが無い場合は「今この瞬間」以降のみを対象にする（過去の投稿を大量処理しないための安全策）
        print("前回の記録が見つからないため、これ以降の投稿のみを対象にします")
        last_ts = str(datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).timestamp())
    else:
        print("前回以降の新着を取得中...")
    response = client.conversations_history(
        channel=SLACK_CHANNEL_ID, limit=FETCH_LIMIT, oldest=last_ts
    )
    messages = response.get("messages", [])
    print(f"  → {len(messages)} 件取得")
    return messages


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


def post_to_slack(client, msg, sheet_status, poster_name):
    draft = build_draft(msg)
    check_label = "✅ スプシ登録済み" if sheet_status == "registered" else "⚠️ スプシ未登録（要確認）"

    text = (
        f"{SLACK_MENTION}\n"
        f"*【元チャンネル】* #sangosama-アトムチェーン本部sama　"
        f"*【投稿日時】* {msg['投稿日時']}　"
        f"*【投稿者】* {poster_name}　"
        f"*【スプシ照合】* {check_label}\n"
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
    if not SPREADSHEET_ID:
        print("エラー: SANGO_SPREADSHEET_ID が未設定です")
        return

    slack = WebClient(token=SLACK_BOT_TOKEN)
    bot_user_id = slack.auth_test()["user_id"]

    # スプシ読み込み
    print("スプレッドシートを読み込み中...")
    try:
        ss = open_spreadsheet()
        apo_records    = load_apo_records(ss.worksheet("アポイント取得企業"))
        mikomi_records = load_mikomi_records(ss.worksheet("見込み企業"))
        shiryo_records = load_shiryo_records(ss.worksheet("資料送付"))
        print(f"  → アポイント取得企業: {len(apo_records)} 件")
        print(f"  → 見込み企業: {len(mikomi_records)} 件")
        print(f"  → 資料送付: {len(shiryo_records)} 件")
    except Exception as e:
        print(f"スプレッドシート読み込みエラー: {e}")
        return

    # Slack取得・通知
    try:
        messages = fetch_messages(slack)
        filtered = filter_messages(messages, bot_user_id)

        if not filtered:
            print("キーワードに一致する投稿はありませんでした。")
            save_last_timestamp(messages)
            return

        print("\n通知チャンネルへ投稿中...")
        for msg in filtered:
            text = msg["本文"]

            if "【アポ" in text:
                match = check_in_sheet(apo_records, text)
            elif "【資料" in text:
                match = check_in_sheet(shiryo_records, text)
            elif "【見込み" in text:
                match = check_in_sheet(mikomi_records, text)
            else:
                match = None

            status = "registered" if match else "unregistered"
            poster_name = fetch_user_name(slack, msg["投稿者ID"])

            try:
                post_to_slack(slack, msg, status, poster_name)
                add_reaction(slack, msg["タイムスタンプ"], "ballot_box_with_check")
            except SlackApiError as e:
                print(f"  → 投稿失敗: {e.response.get('error')}")

        save_last_timestamp(messages)
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
