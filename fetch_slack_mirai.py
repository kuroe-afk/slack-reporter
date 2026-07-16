"""
株式会社MIRAIさま
Slackから報告を取得 → スプシ照合 → Gmail下書き作成 → スレッド返信で通知
"""

import os
import re
import base64
import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import gspread
from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

SLACK_BOT_TOKEN  = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("MIRAI_SLACK_CHANNEL_ID")
SLACK_MENTION    = os.getenv("SLACK_MENTION", "")

SPREADSHEET_ID   = os.getenv("MIRAI_SPREADSHEET_ID")
CREDENTIALS_FILE = "credentials.json"

GMAIL_CLIENT_ID          = os.getenv("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET      = os.getenv("GMAIL_CLIENT_SECRET")
REFRESH_TOKEN_JIMUCENTER = os.getenv("GMAIL_REFRESH_TOKEN_JIMUCENTER")
REFRESH_TOKEN_MIRAI      = os.getenv("GMAIL_REFRESH_TOKEN_MIRAI")

KEYWORDS     = ["【アポ", "【見込み", "【資料"]
FETCH_LIMIT  = 50
LAST_TS_FILE = "last_timestamp_mirai.txt"

# ── アポ・見込み用（jimucenterアカウントから送信、宛先固定）──
MAIL_FROM_APO_MIKOMI = "tasukaru.jimucenter@gmail.com"
MAIL_TO_APO_MIKOMI   = "hara@mirai-tofuture.com"
MAIL_CC_APO_MIKOMI   = (
    "okanaho@tasukaru39.com, s.iwai@tasukaru39.com, s.takaki@tasukaru39.com, "
    "n.harimaya@tasukaru39.com, kuroe@tasukaru39.com"
)

SUBJECT_APO    = "アポイント取得致しました【株式会社Tasukaruでございます】"
SUBJECT_MIKOMI = "アポイント見込みのご報告【株式会社Tasukaruでございます】"

BODY_APO = """\
株式会社MIRAI
原 様

お世話になっております。
下記、 アポイント獲得のご報告でございます。
恐れ入りますが、ご対応のほどよろしくお願いいたします。

株式会社Tasukaru　事務センター
------------------------------------------------------------------------
{body}"""

BODY_MIKOMI = """\
株式会社MIRAI
原 様

お世話になっております。
下記、 アポイント見込みの報告でございます。
恐れ入りますが、ご対応のほどよろしくお願いいたします。

株式会社Tasukaru　事務センター
------------------------------------------------------------------------
{body}"""

# ── 資料用（mirai@tasukaru39.comから送信、宛先はSlack本文から抽出）──
MAIL_FROM_SHIRYO = "mirai@tasukaru39.com"
MAIL_CC_SHIRYO    = "hara@mirai-tofuture.com"
SUBJECT_SHIRYO    = "【株式会社MIRAI】資料送付のご案内"

BODY_SHIRYO = """\
{company}
{person}

お世話になっております。
株式会社MIRAIの原でございます。

この度は弊社からお電話させていただきありがとうございます。
ご案内させていただきました資料をお送りいたします。

【資料Ⅰ】  https://x.gd/LacRz

【資料Ⅱ】  https://x.gd/a2nhw

【資料Ⅲ】　https://x.gd/mIleU

ご不明な点などございましたら、本メールの返信にて原までお問い合わせください。
ご確認のほど、よろしくお願いいたします。

--------------------------------------------------------------------------------
株式会社MIRAI
代表取締役　原 憲二
埼玉県さいたま市南区別所3-38-29 和田ビル3階
TEL:  048-767-6470
Email:  hara@mirai-tofuture.com
--------------------------------------------------------------------------------"""


# ── Gmail接続 ──

def get_gmail_service(refresh_token):
    creds = OAuthCredentials(
        token=None,
        refresh_token=refresh_token,
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def create_draft(service, sender, to, cc, subject, body):
    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = to
    msg["Cc"]      = cc
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()


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


def extract_name(raw):
    """「徳田様_トクダ様」「大谷　諒様(オオタニ様)」→「徳田 様」「大谷　諒 様」"""
    name = re.sub(r'[（(][^）)]*[）)]', '', raw)
    name = name.split('_')[0].strip()
    name = re.sub(r'\s*様\s*$', '', name).strip()
    return name + ' 様'


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


def save_last_timestamp(messages):
    if messages:
        with open(LAST_TS_FILE, "w") as f:
            f.write(messages[0].get("ts", ""))


def fetch_messages(client):
    last_ts = load_last_timestamp()
    if not last_ts:
        # 初回は「今この瞬間」以降のみを対象にする（過去の投稿を一切処理しないための安全策）
        last_ts = str(datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).timestamp())
    res = client.conversations_history(channel=SLACK_CHANNEL_ID, limit=FETCH_LIMIT, oldest=last_ts)
    messages = res.get("messages", [])
    save_last_timestamp(messages)
    print(f"  → {len(messages)} 件取得しました")
    return messages


def filter_messages(messages):
    filtered = []
    for msg in messages:
        text    = msg.get("text", "")
        matched = [kw for kw in KEYWORDS if kw in text]
        if matched:
            filtered.append({
                "投稿日時":           datetime.datetime.fromtimestamp(float(msg.get("ts", 0)), tz=ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S"),
                "投稿者ID":           msg.get("user", "不明"),
                "本文":               text,
                "マッチしたキーワード": "、".join(matched),
                "タイムスタンプ":       msg.get("ts", ""),
            })
    print(f"  → キーワード一致: {len(filtered)} 件")
    return filtered


def add_reaction(client, timestamp):
    try:
        client.reactions_add(channel=SLACK_CHANNEL_ID, timestamp=timestamp, name="ballot_box_with_check")
    except SlackApiError as e:
        if e.response.get("error") != "already_reacted":
            print(f"  → リアクション失敗: {e.response.get('error')}")


def post_thread_reply(client, timestamp, sheet_status, draft_status):
    check_label = ":white_check_mark: スプシ登録済み" if sheet_status == "registered" else ":warning: スプシ未登録（要確認）"
    draft_label = ":e-mail: Gmail下書き作成済み" if draft_status == "ok" else ":x: 下書き作成失敗"
    text = (
        f"{SLACK_MENTION}\n"
        f"【スプシ照合】 {check_label}　{draft_label}"
    )
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text, thread_ts=timestamp)
    except SlackApiError as e:
        print(f"  → スレッド返信失敗: {e.response.get('error')}")


# ── メイン ──

def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return
    if not SLACK_CHANNEL_ID:
        print("エラー: MIRAI_SLACK_CHANNEL_ID が未設定です")
        return
    if not SPREADSHEET_ID:
        print("エラー: MIRAI_SPREADSHEET_ID が未設定です")
        return

    slack = WebClient(token=SLACK_BOT_TOKEN)

    print("スプレッドシートを読み込み中...")
    try:
        ss = open_spreadsheet()
        apo_records    = load_sheet(ss, "アポイント取得", company_col=4, person_col=5)
        shiryo_records = load_sheet(ss, "資料送付",     company_col=4, person_col=5)
        mikomi_records = load_sheet(ss, "見込み企業",   company_col=4, person_col=5)
        print(f"  → アポ:{len(apo_records)}件 / 資料:{len(shiryo_records)}件 / 見込み:{len(mikomi_records)}件")
    except Exception as e:
        print(f"スプレッドシートエラー: {e}")
        return

    print("Gmailに接続中...")
    try:
        gmail_jimucenter = get_gmail_service(REFRESH_TOKEN_JIMUCENTER)
        gmail_mirai       = get_gmail_service(REFRESH_TOKEN_MIRAI)
        print("  → 接続成功")
    except Exception as e:
        print(f"Gmailエラー: {e}")
        return

    print(f"Slackチャンネル {SLACK_CHANNEL_ID} から取得中...")
    try:
        messages = fetch_messages(slack)
        filtered = filter_messages(messages)

        if not filtered:
            print("新着の対象投稿はありませんでした。")
            return

        for msg in filtered:
            text    = msg["本文"]
            keyword = msg["マッチしたキーワード"]

            draft_status = "ok"
            sheet_rec    = None

            try:
                if "【アポ" in text:
                    sheet_rec = check_in_sheet(apo_records, text)
                    body_text = clean_slack_text(text[text.find("【アポ"):].strip())
                    create_draft(
                        gmail_jimucenter, MAIL_FROM_APO_MIKOMI, MAIL_TO_APO_MIKOMI, MAIL_CC_APO_MIKOMI,
                        SUBJECT_APO, BODY_APO.format(body=body_text)
                    )
                elif "【資料" in text:
                    sheet_rec  = check_in_sheet(shiryo_records, text)
                    company    = extract_field(text, ["企業名", "会社名", "社名"]) or "（会社名）"
                    raw_person = extract_field(text, ["氏名", "担当者名", "担当者"])
                    person     = extract_name(raw_person) if raw_person else "（担当者名）"
                    to_email   = extract_field(text, ["e-mail", "mail", "メール"]) or ""
                    create_draft(
                        gmail_mirai, MAIL_FROM_SHIRYO, to_email, MAIL_CC_SHIRYO,
                        SUBJECT_SHIRYO, BODY_SHIRYO.format(company=company, person=person)
                    )
                elif "【見込み" in text:
                    sheet_rec = check_in_sheet(mikomi_records, text)
                    body_text = clean_slack_text(text[text.find("【見込み"):].strip())
                    create_draft(
                        gmail_jimucenter, MAIL_FROM_APO_MIKOMI, MAIL_TO_APO_MIKOMI, MAIL_CC_APO_MIKOMI,
                        SUBJECT_MIKOMI, BODY_MIKOMI.format(body=body_text)
                    )
            except Exception as e:
                print(f"  → 下書き作成失敗: {e}")
                draft_status = "error"

            sheet_status = "registered" if sheet_rec else "unregistered"

            try:
                post_thread_reply(slack, msg["タイムスタンプ"], sheet_status, draft_status)
                add_reaction(slack, msg["タイムスタンプ"])
                print(f"  → 処理完了: {keyword} / 下書き:{draft_status} / スプシ:{sheet_status}")
            except SlackApiError as e:
                print(f"  → スレッド返信失敗: {e.response.get('error')}")

        print("\n完了！")

    except SlackApiError as e:
        code = e.response.get("error", "不明")
        print(f"Slack APIエラー: {code}")
        if code == "not_in_channel":
            print("→ Botをチャンネルに招待してください（Slackで /invite @kuroe）")
        elif code == "missing_scope":
            print(f"→ 権限不足: {e.response.get('needed')}")


if __name__ == "__main__":
    main()
