"""
Slackから報告を取得 → スプシ照合 → 専用チャンネルへ通知するスクリプト
"""

import os
import re
import csv
import json
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

SLACK_BOT_TOKEN           = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID          = os.getenv("SLACK_CHANNEL_ID")
SLACK_NOTIFY_CHANNEL      = os.getenv("SLACK_NOTIFY_CHANNEL_ID")
SLACK_MENTION             = os.getenv("SLACK_MENTION", "")
SLACK_SOURCE_CHANNEL_NAME = os.getenv("SLACK_SOURCE_CHANNEL_NAME", "")

# スプレッドシートID（URLの /d/ と /edit の間の文字列）
SPREADSHEET_ID = "1rFRQXEOjpeu7Q_BP8v5TFwTXQr5FluKSCQpId4Arv_s"

# サービスアカウントキーのパス
CREDENTIALS_FILE = "credentials.json"

# 抽出キーワード
KEYWORDS = ["【アポ", "【見込み"]

FETCH_LIMIT = 50

# 最後に処理したタイムスタンプを保存するファイル
LAST_TS_FILE = "last_timestamp.txt"

# Chatwork転送用テンプレート
CHATWORK_HEADER = (
    "[To:2223905]内田 健人さん\n"
    "[To:11098951]増田 将馬 ※070-2470-6645さん\n"
    "[To:11399515]小林 恭也さん\n"
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


# ──────────────────────────────────────
# Google スプレッドシート
# ──────────────────────────────────────

def open_spreadsheet():
    """スプレッドシートを開く"""
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def load_sheet_records(sheet):
    """シートの全行を取得（A列=日付, E列=会社名, F列=担当者名）"""
    rows = sheet.get_all_values()
    records = []
    for row in rows[1:]:  # 1行目はヘッダーなのでスキップ
        date_val    = row[0].strip() if len(row) > 0 else ""  # A列：日付
        company     = row[4].strip() if len(row) > 4 else ""  # E列：会社名
        person      = row[5].strip() if len(row) > 5 else ""  # F列：担当者名
        if company:
            records.append({"日付": date_val, "会社名": company, "担当者名": person})
    return records


def extract_company_from_slack(text):
    """Slackの本文から「会社名　：〇〇」の部分を抽出する"""
    # 「会社名」の後ろに続く値を取り出す（全角・半角スペース・コロン混在に対応）
    match = re.search(r'会社名[\s　]*[：:]\s*(.+)', text)
    if match:
        # Slackのリンク記法 <mailto:...|表示名> や <tel:...|番号> を除去
        company = match.group(1).strip()
        company = re.sub(r'<[^>]+\|([^>]+)>', r'\1', company)
        company = re.sub(r'<[^>]+>', '', company)
        return company.strip()
    return ""


def extract_person_from_slack(text):
    """Slackの本文から「担当者名　：〇〇」の部分を抽出する"""
    match = re.search(r'担当者[\s　名]*[：:]\s*(.+)', text)
    if match:
        person = match.group(1).strip()
        person = re.sub(r'<[^>]+\|([^>]+)>', r'\1', person)
        person = re.sub(r'<[^>]+>', '', person)
        # 「さん」や空白を除いた姓だけでも一致させるため末尾を整理
        return person.strip()
    return ""


def normalize(s):
    """照合用に空白・全角スペースを除去して小文字化する"""
    return re.sub(r'[\s　]', '', s).lower()


def check_in_sheet(records, text, post_date):
    """
    Slackの本文から会社名・担当者名を抽出し、スプシと照合する。
    一致した場合は該当レコードを返す。見つからなければ None を返す。
    """
    slack_company = normalize(extract_company_from_slack(text))
    slack_person  = normalize(extract_person_from_slack(text))

    for rec in records:
        sheet_company = normalize(rec["会社名"])
        sheet_person  = normalize(rec["担当者名"])

        if not sheet_company:
            continue

        # 会社名が一致（スプシの値がSlack抽出値に含まれる、またはその逆）
        company_match = (
            sheet_company in slack_company or
            slack_company in sheet_company
        )
        if not company_match:
            continue

        # 担当者名が空ならば会社名だけで一致とみなす
        if not sheet_person:
            return rec

        # 担当者名も照合（姓だけでも一致とみなす）
        sheet_last = sheet_person[:2]  # 姓2文字
        person_match = (
            sheet_person in slack_person or
            slack_person in sheet_person or
            (sheet_last and sheet_last in slack_person)
        )
        if person_match:
            return rec

    return None


# ──────────────────────────────────────
# Slack 取得・フィルタ
# ──────────────────────────────────────

def fetch_channel_name(client, channel_id):
    try:
        res = client.conversations_info(channel=channel_id)
        return res["channel"].get("name", channel_id)
    except SlackApiError:
        return channel_id


def fetch_user_name(client, user_id):
    """ユーザーIDから表示名を取得する"""
    try:
        res = client.users_info(user=user_id)
        profile = res["user"]["profile"]
        # 表示名 → 本名 の順で取得
        return profile.get("display_name") or profile.get("real_name") or user_id
    except SlackApiError:
        return user_id


def load_last_timestamp():
    """前回処理した最後のタイムスタンプを読み込む"""
    if os.path.exists(LAST_TS_FILE):
        with open(LAST_TS_FILE, "r") as f:
            return f.read().strip()
    return None


def save_last_timestamp(messages):
    """今回処理した中で最新のタイムスタンプを保存する"""
    if messages:
        latest_ts = messages[0].get("ts", "")
        with open(LAST_TS_FILE, "w") as f:
            f.write(latest_ts)


def fetch_messages(client, channel_id, limit):
    last_ts = load_last_timestamp()
    if last_ts:
        print(f"チャンネル {channel_id} から前回以降の新着を取得中...")
        response = client.conversations_history(channel=channel_id, limit=limit, oldest=last_ts)
    else:
        print(f"チャンネル {channel_id} から最新{limit}件を取得中（初回）...")
        response = client.conversations_history(channel=channel_id, limit=limit)
    messages = response.get("messages", [])
    print(f"  → {len(messages)} 件取得しました")
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
    print(f"  → キーワードに一致した投稿: {len(filtered)} 件")
    return filtered


# ──────────────────────────────────────
# 保存
# ──────────────────────────────────────

def save_json(data, filepath):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  → JSONを保存しました: {filepath}")


def save_csv(data, filepath):
    if not data:
        print("  → 保存するデータがありません（CSVスキップ）")
        return
    fieldnames = list(data[0].keys())
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)
    print(f"  → CSVを保存しました: {filepath}")


# ──────────────────────────────────────
# Slack 通知
# ──────────────────────────────────────

def extract_body(text, keyword):
    idx = text.find(keyword)
    if idx == -1:
        return text
    return text[idx:].strip()


def build_chatwork_draft(msg):
    text = msg["本文"]
    if "【アポ" in text:
        body = extract_body(text, "【アポ")
        return TEMPLATE_APO.format(body=body)
    elif "【見込み" in text:
        body = extract_body(text, "【見込み")
        return TEMPLATE_MIKOMI.format(body=body)
    return text


def add_reaction(client, channel_id, timestamp, emoji):
    """元投稿にリアクションを付ける"""
    try:
        client.reactions_add(channel=channel_id, timestamp=timestamp, name=emoji)
    except SlackApiError as e:
        # すでに同じリアクションがある場合はスキップ
        if e.response.get("error") != "already_reacted":
            print(f"  → リアクション失敗: {e.response.get('error')}")


def post_to_slack(client, notify_channel, source_channel_name, msg, sheet_status, poster_name):
    """1件分を通知チャンネルへ投稿する"""
    display_name = SLACK_SOURCE_CHANNEL_NAME if SLACK_SOURCE_CHANNEL_NAME else source_channel_name
    draft = build_chatwork_draft(msg)

    if sheet_status == "registered":
        check_label = "✅ スプシ登録済み"
    else:
        check_label = "⚠️ スプシ未登録（要確認）"

    text = (
        f"{SLACK_MENTION}\n"
        f"*【元チャンネル】* #{display_name}　"
        f"*【投稿日時】* {msg['投稿日時']}　"
        f"*【投稿者】* {poster_name}　"
        f"*【スプシ照合】* {check_label}\n"
        f"{'─' * 40}\n"
        f"{draft}"
    )
    client.chat_postMessage(channel=notify_channel, text=text)


# ──────────────────────────────────────
# メイン
# ──────────────────────────────────────

def main():
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN.startswith("xoxb-ここに"):
        print("エラー: .env の SLACK_BOT_TOKEN を設定してください")
        return
    if not SLACK_CHANNEL_ID or SLACK_CHANNEL_ID.startswith("ここに"):
        print("エラー: .env の SLACK_CHANNEL_ID を設定してください")
        return
    if not SLACK_NOTIFY_CHANNEL or SLACK_NOTIFY_CHANNEL.startswith("ここに"):
        print("エラー: .env の SLACK_NOTIFY_CHANNEL_ID を設定してください")
        return
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"エラー: {CREDENTIALS_FILE} が見つかりません。slack_reporterフォルダに置いてください")
        return

    slack = WebClient(token=SLACK_BOT_TOKEN)

    # ── スプシ読み込み ──
    print("スプレッドシートを読み込み中...")
    try:
        ss = open_spreadsheet()
        apo_records   = load_sheet_records(ss.worksheet("アポイント取得"))
        mikomi_records = load_sheet_records(ss.worksheet("見込み企業"))
        print(f"  → アポイント取得シート: {len(apo_records)} 件")
        print(f"  → 見込み企業シート: {len(mikomi_records)} 件")
    except Exception as e:
        print(f"スプレッドシート読み込みエラー: {e}")
        return

    # ── Slack取得・フィルタ ──
    try:
        source_name = fetch_channel_name(slack, SLACK_CHANNEL_ID)
        messages    = fetch_messages(slack, SLACK_CHANNEL_ID, FETCH_LIMIT)
        filtered    = filter_messages(messages)

        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        save_json(filtered, f"output_{now}.json")
        save_csv(filtered, f"output_{now}.csv")

        if not filtered:
            print("\nキーワードに一致する投稿は見つかりませんでした。")
            save_last_timestamp(messages)
            return

        # ── スプシ照合 → Slack通知 ──
        print(f"\n通知チャンネル {SLACK_NOTIFY_CHANNEL} へ投稿中...")
        registered = 0
        unregistered = 0

        for msg in filtered:
            text = msg["本文"]

            if "【アポ" in text:
                match = check_in_sheet(apo_records, text, msg["投稿日時"])
            elif "【見込み" in text:
                match = check_in_sheet(mikomi_records, text, msg["投稿日時"])
            else:
                match = None

            status = "registered" if match else "unregistered"
            if match:
                registered += 1
            else:
                unregistered += 1

            poster_name = fetch_user_name(slack, msg["投稿者ID"])
            try:
                post_to_slack(slack, SLACK_NOTIFY_CHANNEL, source_name, msg, status, poster_name)
                # 元投稿にリアクションを付ける
                add_reaction(slack, SLACK_CHANNEL_ID, msg["タイムスタンプ"], "ballot_box_with_check")
            except SlackApiError as e:
                print(f"  → 投稿失敗: {e.response.get('error')}")

        print(f"  → 完了（スプシ登録済み: {registered} 件 ／ 未登録: {unregistered} 件）")
        save_last_timestamp(messages)
        print("\n完了！")

    except SlackApiError as e:
        code = e.response.get("error", "不明")
        print(f"\nSlack APIエラー: {code}")
        if code == "not_in_channel":
            print("→ ボットをそのチャンネルに招待してください（Slackで /invite @ボット名）")
        elif code == "missing_scope":
            needed = e.response.get("needed", "")
            print(f"→ 権限が足りません。Slack APIで '{needed}' を追加して再インストールしてください。")
        elif code == "invalid_auth":
            print("→ .env のトークンが間違っています。確認してください。")
        else:
            print(f"→ 詳細: {e.response}")


if __name__ == "__main__":
    main()
