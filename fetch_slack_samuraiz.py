"""
サムライズさまチャンネルから報告を取得 → スプシ照合 → Gmail下書き作成 → Slack通知
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

# Slack
SLACK_BOT_TOKEN          = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID         = os.getenv("SAMURAIZ_SLACK_CHANNEL_ID")   # C07959RG11V
SLACK_NOTIFY_CHANNEL     = os.getenv("SLACK_NOTIFY_CHANNEL_ID")     # C0B74VD3YKH
SLACK_MENTION            = os.getenv("SLACK_MENTION", "")

# Google
SPREADSHEET_ID           = os.getenv("SAMURAIZ_SPREADSHEET_ID")
CREDENTIALS_FILE         = "credentials.json"

# Gmail OAuth
GMAIL_CLIENT_ID          = os.getenv("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET      = os.getenv("GMAIL_CLIENT_SECRET")
REFRESH_TOKEN_JIMUCENTER = os.getenv("GMAIL_REFRESH_TOKEN_JIMUCENTER")
REFRESH_TOKEN_SAMURAIZ   = os.getenv("GMAIL_REFRESH_TOKEN_SAMURAIZ")

KEYWORDS   = ["【アポ", "【見込み", "【資料依頼"]
FETCH_LIMIT = 50
LAST_TS_FILE = "last_timestamp_samuraiz.txt"

# ── メールアドレス定数 ──
MAIL_APO_MIKOMI_FROM = "tasukaru.jimucenter@gmail.com"
MAIL_SHIRYO_FROM     = "samuraiz@tasukaru39.com"

MAIL_APO_MIKOMI_TO   = "Mio Souji <m_souji@samuraiz.co.jp>"
MAIL_APO_MIKOMI_CC   = (
    "okanaho@tasukaru39.com, "
    "s.iwai@tasukaru39.com, "
    "s.takaki@tasukaru39.com, "
    "n.harimaya@tasukaru39.com, "
    "kuroe@tasukaru39.com"
)
MAIL_SHIRYO_CC       = "Mio Souji <m_souji@samuraiz.co.jp>"

SUBJECT_APO    = "アポイント取得致しました【株式会社Tasukaruでございます】"
SUBJECT_MIKOMI = "アポイント見込みのご報告【株式会社Tasukaru黒江でございます】"
SUBJECT_SHIRYO = "【株式会社サムライズ】資料送付のご案内"

BODY_APO = """\
株式会社サムライズ
荘司　様

お世話になっております。
下記、アポイントの報告でございます。
恐れ入りますが、ご対応のほどよろしくお願いいたします。

株式会社Tasukaru　事務センター
------------------------------------------------------------------------
{body}"""

BODY_MIKOMI = """\
株式会社サムライズ

荘司　様

お世話になっております。
下記、アポイント見込みの報告でございます。
恐れ入りますが、ご対応のほどよろしくお願いいたします。

株式会社Tasukaru　事務センター
------------------------------------------------------------------------
{body}"""

BODY_SHIRYO_INSTANA = """\
{company}
{person}

お世話になっております。
株式会社サムライズの荘司でございます。

この度は弊社からお電話させていただきありがとうございます。
ご案内させていただきましたInstanaの製品資料をお送りいたします。

【Instana製品資料】
https://x.gd/YfnB3

ご不明な点などございましたら、本メールの返信にて荘司までお問い合わせください。
ご確認のほど、よろしくお願いいたします。

--------------------------------------------------------------------------------
株式会社サムライズ
 データ・マネジメント・ソリューション事業部
 荘司　澪 / SOUJI MIO
 〒141-0032 品川区大崎1-6-4新大崎勧業ビル5F
 TEL: 03-5436-2042 FAX: 03-5436-2041
 Email: m_souji@samuraiz.co.jp
--------------------------------------------------------------------------------"""

BODY_SHIRYO_FINOPS = """\
{company}
{person}

お世話になっております。
株式会社サムライズの荘司でございます。

この度は弊社からお電話させていただきありがとうございます。
ご案内させていただきました FinOps(Cloudability） の製品資料をお送りいたします。

【資料Ⅰ】
https://x.gd/0z8kI

【資料Ⅱ】
https://x.gd/uN7OZ

ご不明な点などございましたら、本メールの返信にて荘司までお問い合わせください。
ご確認のほど、よろしくお願いいたします。

--------------------------------------------------------------------------------
株式会社サムライズ
 データ・マネジメント・ソリューション事業部
 荘司　澪 / SOUJI MIO
 〒141-0032 品川区大崎1-6-4新大崎勧業ビル5F
 TEL: 03-5436-2042 FAX: 03-5436-2041
 Email: m_souji@samuraiz.co.jp
--------------------------------------------------------------------------------"""

BODY_SHIRYO_TURBONOMIC = """\
{company}
{person}

お世話になっております。
株式会社サムライズの荘司でございます。

この度は弊社からお電話させていただきありがとうございます。
ご案内させていただきましたTurbonomicの製品資料をお送りいたします。

【Turbonomic製品資料】
https://x.gd/Vfbvd

ご不明な点などございましたら、本メールの返信にて荘司までお問い合わせください。
ご確認のほど、よろしくお願いいたします。

--------------------------------------------------------------------------------
株式会社サムライズ
 データ・マネジメント・ソリューション事業部
 荘司　澪 / SOUJI MIO
 〒141-0032 品川区大崎1-6-4新大崎勧業ビル5F
 TEL: 03-5436-2042 FAX: 03-5436-2041
 Email: m_souji@samuraiz.co.jp
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
    """Gmail下書きを作成する"""
    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = to
    msg["Cc"]      = cc
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()


# ── スプレッドシート ──

def open_spreadsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds  = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc     = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def load_sheet(ss, sheet_name):
    rows    = ss.worksheet(sheet_name).get_all_values()
    records = []
    for row in rows[1:]:
        records.append({
            "架電日":   row[1].strip() if len(row) > 1 else "",
            "リスト":   row[2].strip() if len(row) > 2 else "",
            "架電者":   row[3].strip() if len(row) > 3 else "",
            "企業名":   row[4].strip() if len(row) > 4 else "",
            "氏名":     row[5].strip() if len(row) > 5 else "",
            "部署役職": row[6].strip() if len(row) > 6 else "",
            "電話":     row[7].strip() if len(row) > 7 else "",
            "email":    row[8].strip() if len(row) > 8 else "",
        })
    return records


def normalize(s):
    return re.sub(r'[\s　]', '', s).lower()


def clean_slack_text(text):
    """Slackの<url|表示>や<url>などをシンプルなテキストに変換"""
    text = re.sub(r'<[^>]+\|([^>]+)>', r'\1', text)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('*', '')
    return text


def extract_field(text, label):
    """本文から「ラベル：値」を抽出する（全角スペース・複数スペース対応）"""
    match = re.search(rf'{label}[\s　]*[：:]\s*(.+)', text)
    if not match:
        return ""
    val = match.group(1).strip()
    # Slackのリンク記法 <mailto:...|表示名> を表示名だけに変換
    val = re.sub(r'<[^>]+\|([^>]+)>', r'\1', val)
    val = re.sub(r'<[^>]+>', '', val)
    # 「（よみ）」などカッコ内の読み仮名を除去（「様」は残す）
    val = re.sub(r'[　\s]*[（(].+?[）)]', '', val)
    # 末尾の「男性」「女性」などの性別表記を除去
    val = re.sub(r'[　\s]*[男女]性\s*$', '', val)
    return val.strip()


def check_in_sheet(records, text):
    slack_company = normalize(extract_field(text, "企業名"))
    slack_person  = normalize(extract_field(text, "氏名"))
    for rec in records:
        if not rec["企業名"]:
            continue
        if normalize(rec["企業名"]) in slack_company or slack_company in normalize(rec["企業名"]):
            if not rec["氏名"] or normalize(rec["氏名"])[:2] in slack_person or slack_person in normalize(rec["氏名"]):
                return rec
    return None


# ── Slack ──

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
        # 前回の記録が無い場合は「今この瞬間」以降のみを対象にする（過去の投稿を大量処理しないための安全策）
        last_ts = str(fetch_ts)
    res = client.conversations_history(channel=SLACK_CHANNEL_ID, limit=FETCH_LIMIT, oldest=last_ts)
    messages = res.get("messages", [])
    print(f"  → {len(messages)} 件取得しました")
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
            continue  # 既に処理済み（ボット自身のリアクションが付いている）
        filtered.append({
            "投稿日時":         datetime.datetime.fromtimestamp(float(msg.get("ts", 0))).strftime("%Y-%m-%d %H:%M:%S"),
            "投稿者ID":         msg.get("user", "不明"),
            "本文":             text,
            "マッチしたキーワード": "、".join(matched),
            "タイムスタンプ":     msg.get("ts", ""),
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


def post_slack_notify(client, msg, poster_name, sheet_status, draft_status):
    check_label = "✅ スプシ登録済み" if sheet_status == "registered" else "⚠️ スプシ未登録（要確認）"
    draft_label = "📧 Gmail下書き作成済み" if draft_status == "ok" else "❌ 下書き作成失敗"
    text = (
        f"{SLACK_MENTION}\n"
        f"*【チャンネル】* #サムライズさま　"
        f"*【投稿日時】* {msg['投稿日時']}　"
        f"*【投稿者】* {poster_name}\n"
        f"*【スプシ照合】* {check_label}　{draft_label}\n"
        f"{'─' * 40}\n"
        f"{msg['本文']}"
    )
    client.chat_postMessage(channel=SLACK_NOTIFY_CHANNEL, text=text)


def add_reaction(client, timestamp):
    try:
        client.reactions_add(channel=SLACK_CHANNEL_ID, timestamp=timestamp, name="ballot_box_with_check")
    except SlackApiError as e:
        if e.response.get("error") != "already_reacted":
            print(f"  → リアクション失敗: {e.response.get('error')}")


SHIRYO_SHEET_LINK = "https://docs.google.com/spreadsheets/d/1qk7_gipzGXz3cveIWIXkYfg2-g6jqGl8NwHx5IF3K4E/edit?gid=1890993103#gid=1890993103"


def post_thread_reply(client, timestamp, sheet_status, draft_status, is_shiryo=False):
    check_label = ":white_check_mark: スプシ登録済み" if sheet_status == "registered" else ":warning: スプシ未登録（要確認）"
    draft_label = ":e-mail: Gmail下書き作成済み" if draft_status == "ok" else ":x: 下書き作成失敗"
    text = (
        f"{SLACK_MENTION}\n"
        f"【スプシ照合】 {check_label}　{draft_label}"
    )
    if is_shiryo:
        text += f"\n※:g:資料送付日：{SHIRYO_SHEET_LINK}"
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text, thread_ts=timestamp)
    except SlackApiError as e:
        print(f"  → スレッド返信失敗: {e.response.get('error')}")


# ── メイン ──

def main():
    slack = WebClient(token=SLACK_BOT_TOKEN)
    bot_user_id = slack.auth_test()["user_id"]

    # スプシ読み込み
    print("スプレッドシートを読み込み中...")
    try:
        ss             = open_spreadsheet()
        apo_records    = load_sheet(ss, "アポイント取得")
        mikomi_records = load_sheet(ss, "見込み企業")
        shiryo_records = load_sheet(ss, "資料送付")
        print(f"  → アポ:{len(apo_records)}件 / 見込み:{len(mikomi_records)}件 / 資料:{len(shiryo_records)}件")
    except Exception as e:
        print(f"スプレッドシートエラー: {e}")
        return

    # Gmail接続
    print("Gmailに接続中...")
    try:
        gmail_jimucenter = get_gmail_service(REFRESH_TOKEN_JIMUCENTER)
        gmail_samuraiz   = get_gmail_service(REFRESH_TOKEN_SAMURAIZ)
        print("  → 接続成功")
    except Exception as e:
        print(f"Gmailエラー: {e}")
        return

    # Slack取得
    print(f"Slackチャンネル {SLACK_CHANNEL_ID} から取得中...")
    try:
        messages, fetch_ts = fetch_messages(slack)
        filtered = filter_messages(messages, bot_user_id)

        if not filtered:
            print("新着の対象投稿はありませんでした。")
            save_last_timestamp(fetch_ts)
            return

        for msg in filtered:
            text    = msg["本文"]
            keyword = msg["マッチしたキーワード"]
            poster  = fetch_user_name(slack, msg["投稿者ID"])

            # スプシ照合
            if "【アポ" in text:
                sheet_rec = check_in_sheet(apo_records, text)
            elif "【見込み" in text:
                sheet_rec = check_in_sheet(mikomi_records, text)
            elif "【資料依頼" in text:
                sheet_rec = check_in_sheet(shiryo_records, text)
            else:
                sheet_rec = None

            sheet_status = "registered" if sheet_rec else "unregistered"

            # Gmail下書き作成
            draft_status = "ok"
            try:
                if "【アポ" in text:
                    body_text = clean_slack_text(text[text.find("【アポ"):].strip())
                    create_draft(
                        gmail_jimucenter,
                        MAIL_APO_MIKOMI_FROM,
                        MAIL_APO_MIKOMI_TO,
                        MAIL_APO_MIKOMI_CC,
                        SUBJECT_APO,
                        BODY_APO.format(body=body_text)
                    )
                elif "【見込み" in text:
                    body_text = clean_slack_text(text[text.find("【見込み"):].strip())
                    create_draft(
                        gmail_jimucenter,
                        MAIL_APO_MIKOMI_FROM,
                        MAIL_APO_MIKOMI_TO,
                        MAIL_APO_MIKOMI_CC,
                        SUBJECT_MIKOMI,
                        BODY_MIKOMI.format(body=body_text)
                    )
                elif "【資料依頼" in text:
                    company   = extract_field(text, "企業名") or "（会社名）"
                    person    = extract_field(text, "氏名") or "（担当者名）"
                    to_email  = extract_field(text, "e-mail") or ""
                    list_type = sheet_rec["リスト"] if sheet_rec else ""

                    # スプシC列またはSlack投稿でテンプレートを判定
                    if "FinOps" in text or "FinOps" in list_type:
                        body_text = BODY_SHIRYO_FINOPS.format(company=company, person=person)
                    elif "Turbonomic" in text or "Turbonomic" in list_type:
                        body_text = BODY_SHIRYO_TURBONOMIC.format(company=company, person=person)
                    else:
                        body_text = BODY_SHIRYO_INSTANA.format(company=company, person=person)

                    create_draft(
                        gmail_samuraiz,
                        MAIL_SHIRYO_FROM,
                        to_email,
                        MAIL_SHIRYO_CC,
                        SUBJECT_SHIRYO,
                        body_text
                    )
            except Exception as e:
                print(f"  → 下書き作成失敗: {e}")
                draft_status = "error"

            # スレッド返信とリアクション
            is_shiryo = "【資料依頼" in text
            try:
                post_thread_reply(slack, msg["タイムスタンプ"], sheet_status, draft_status, is_shiryo)
                add_reaction(slack, msg["タイムスタンプ"])
                print(f"  → 処理完了: {keyword} / 下書き:{draft_status} / スプシ:{sheet_status}")
            except SlackApiError as e:
                print(f"  → スレッド返信失敗: {e.response.get('error')}")

        save_last_timestamp(fetch_ts)
        print("\n完了！")

    except SlackApiError as e:
        code = e.response.get("error", "不明")
        print(f"Slack APIエラー: {code}")
        if code == "missing_scope":
            print(f"→ 権限不足: {e.response.get('needed')}")


if __name__ == "__main__":
    main()
