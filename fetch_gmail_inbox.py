"""
Gmail受信トレイ監視スクリプト
対象アカウントに新着メールが届いたらSlackに通知する
✅ラベルが付いているメールはスキップ
"""

import os
import base64
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

SLACK_BOT_TOKEN      = os.getenv("SLACK_BOT_TOKEN")
SLACK_NOTIFY_CHANNEL = "C0BC7SYFP36"   # クライアント報告用--下書き--

GMAIL_CLIENT_ID     = os.getenv("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET")

LAST_TS_FILE = "last_timestamp_gmail.txt"
SKIP_LABEL   = "✅"   # このラベルが付いているメールはスキップ

# 監視するアカウント一覧
ACCOUNTS = [
    {
        "name":          "datarein@tasukaru39.com",
        "refresh_token": os.getenv("GMAIL_REFRESH_TOKEN_DATAREIN"),
    },
    {
        "name":          "mirai@tasukaru39.com",
        "refresh_token": os.getenv("GMAIL_REFRESH_TOKEN_MIRAI"),
    },
    {
        "name":          "exkey@tasukaru39.com",
        "refresh_token": os.getenv("GMAIL_REFRESH_TOKEN_EXKEY"),
    },
    {
        "name":          "okanaho.sango@gmail.com",
        "refresh_token": os.getenv("GMAIL_REFRESH_TOKEN_SANGO_OKA"),
    },
]


# ── タイムスタンプ管理 ──

def load_last_timestamp():
    if os.path.exists(LAST_TS_FILE):
        with open(LAST_TS_FILE, "r") as f:
            return f.read().strip()
    return None


def save_last_timestamp(ts):
    with open(LAST_TS_FILE, "w") as f:
        f.write(str(ts))


# ── Gmail ──

def get_gmail_service(refresh_token):
    creds = OAuthCredentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def get_label_ids(service, label_names):
    """ラベル名からIDを取得する"""
    res    = service.users().labels().list(userId="me").execute()
    labels = res.get("labels", [])
    return {
        lbl["name"]: lbl["id"]
        for lbl in labels
        if lbl["name"] in label_names
    }


def get_header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def get_body_text(payload, max_chars=500):
    """メール本文（テキスト）を取得する"""
    def decode_part(data):
        try:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        except Exception:
            return ""

    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return decode_part(data)[:max_chars]

    for part in payload.get("parts", []):
        result = get_body_text(part, max_chars)
        if result:
            return result
    return ""


def fetch_new_emails(service, after_epoch):
    """✅ラベルなし かつ after_epoch以降のメールを取得する"""
    label_map     = get_label_ids(service, [SKIP_LABEL])
    skip_label_id = label_map.get(SKIP_LABEL)

    # after_epoch以降のメインタブのメールのみ取得（プロモーション・ソーシャル・新着を除外）
    query = f"in:inbox -category:promotions -category:social -category:updates after:{int(after_epoch)}"
    res      = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
    messages = res.get("messages", [])

    new_emails = []
    for m in messages:
        detail           = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
        label_ids_on_msg = detail.get("labelIds", [])

        # ✅ラベルが付いていたらスキップ
        if skip_label_id and skip_label_id in label_ids_on_msg:
            continue

        headers  = detail.get("payload", {}).get("headers", [])
        subject  = get_header(headers, "Subject") or "（件名なし）"
        from_    = get_header(headers, "From")    or "（不明）"
        date_str = get_header(headers, "Date")    or ""
        body     = get_body_text(detail.get("payload", {}))

        new_emails.append({
            "subject": subject,
            "from":    from_,
            "date":    date_str,
            "body":    body,
        })

    return new_emails


# ── Slack通知 ──

def post_to_slack(slack, account_name, email):
    body_preview = email["body"].replace("\r\n", "\n").replace("\r", "\n").strip()
    # 長すぎる場合は切り詰める
    if len(body_preview) > 400:
        body_preview = body_preview[:400] + "…"

    slack_mention = os.getenv("SLACK_MENTION", "")
    text = (
        f"{slack_mention}\n"
        f":email: *新着メール通知*\n"
        f"*【受信アカウント】* {account_name}\n"
        f"*【送信者】* {email['from']}\n"
        f"*【件名】* {email['subject']}\n"
        f"*【受信日時】* {email['date']}\n"
        f"{'─' * 40}\n"
        f"{body_preview}"
    )
    try:
        slack.chat_postMessage(channel=SLACK_NOTIFY_CHANNEL, text=text)
        print(f"  → Slack通知: {email['subject']}")
    except SlackApiError as e:
        print(f"  → Slack通知失敗: {e.response.get('error')}")


# ── メイン ──

def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return

    slack    = WebClient(token=SLACK_BOT_TOKEN)
    now_ts   = datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).timestamp()
    last_ts  = load_last_timestamp()

    if not last_ts:
        print("前回の記録が見つからないため、これ以降の投稿のみを対象にします")
        save_last_timestamp(now_ts)
        return

    after_epoch = float(last_ts)

    for account in ACCOUNTS:
        name          = account["name"]
        refresh_token = account["refresh_token"]

        if not refresh_token:
            print(f"[{name}] リフレッシュトークン未設定 → スキップ")
            continue

        print(f"[{name}] チェック中...")
        try:
            service    = get_gmail_service(refresh_token)
            new_emails = fetch_new_emails(service, after_epoch)

            if not new_emails:
                print(f"  → 新着なし")
            else:
                print(f"  → 新着 {len(new_emails)} 件")
                for email in new_emails:
                    post_to_slack(slack, name, email)

        except Exception as e:
            print(f"  → エラー: {e}")

    save_last_timestamp(now_ts)
    print("完了！")


if __name__ == "__main__":
    main()
