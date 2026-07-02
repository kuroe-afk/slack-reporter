"""
at-acty-proチャンネルから報告を取得 → スプシ照合 → Gmail下書き作成 → Slack通知
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

SLACK_BOT_TOKEN      = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID     = os.getenv("ACTY_SLACK_CHANNEL_ID")
SLACK_NOTIFY_CHANNEL = os.getenv("SLACK_NOTIFY_CHANNEL_ID")
SLACK_MENTION        = os.getenv("SLACK_MENTION", "")

SPREADSHEET_ID   = os.getenv("ACTY_SPREADSHEET_ID")
CREDENTIALS_FILE = "credentials.json"

GMAIL_CLIENT_ID     = os.getenv("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET")
REFRESH_TOKEN_ACTY  = os.getenv("GMAIL_REFRESH_TOKEN_ACTY")

KEYWORDS     = ["【アポ", "【資料", "【見込み"]
FETCH_LIMIT  = 50
LAST_TS_FILE = "last_timestamp_acty.txt"

MAIL_ACTY_FROM = "ap.sales@smartshare.jp"
MAIL_ACTY_TO_APO_CC  = "t.matsuda@smartshare.jp, y.tomita@smartshare.jp, Sales@smartshare.jp"
MAIL_ACTY_BCC        = "funai@actyproism.com"

SUBJECT_APO    = "【御礼/お打合せURLのご案内】スマートシェア株式会社"
SUBJECT_SHIRYO = "【御礼/[サービス資料]のご案内】スマートシェア株式会社"

SLACK_MENTION_ACTY = "<@U09J3BU9RNU> <@U09VCJQ1FR9>"

BODY_APO_CUSTOMER = """\
{company}
{person}

平素は大変お世話になっております。
スマートシェア株式会社の{poster_lastname}でございます。

本日は弊社よりご連絡させていただきました件で、
ご丁寧にご対応いただきまして、誠に有難うございました。

またご多用の中、お打合せのお時間をいただけますこと、
重ねて御礼申し上げます。

早速ではございますが、お打合せ当日の会議URLを
以下にご案内させていただきますので、
ご確認の程宜しくお願い申し上げます。
＝＝＝＝＝＝＝
■お打合せ概要
日時：{datetime}
会議URL：{meeting_url}
＝＝＝＝＝＝＝

当日は、貴社のSNS活動のお取組み状況などをお聞かせいただきながら、
弊社サービスについてご紹介させていただければと存じます。

この度は、貴重なお時間をいただけますこと、重ねて御礼申し上げます。
それでは、当日はどうぞ宜しくお願い申し上げます。
--
「「「「「「「「「「「「「「「「「「「「
スマートシェア株式会社 Smart Share Inc.
〒150-0011
東京都渋谷区東2丁目22-14ロゼ氷川3階
Mail:　ap.sales@smartshare.jp
HP:　https://www.smartshare.jp/
「「「「「「「「「「「「「「「「「「「「"""

BODY_SHIRYO_CUSTOMER = """\
{company}
{person}

平素は大変お世話になっております。
スマートシェア株式会社の{poster_lastname}でございます。

本日はご多用の中、お電話致しました際、
ご丁寧にご対応いただきまして、誠に有難うございました。

お電話にてご紹介致しました、
弊社サービスの概要資料を添付にて送付いたします。
■SNSキャンペーンツール
https://www.ownly.jp/?a0v5la7bquf89=0e0369e2b250957f20dpch00mag9d18z&uy3ubftvh0u6o8=1db997de8e8463f1cf6d0307b6d50bf6&xnfrr0ncac=30836&cc4d76fdaf5=758

■＃を利用したインフルエンサーマーケティングツール
https://www.ownly.jp/lp/hashtag_seo?a0v5la7bquf89=0e0369e2b250957f20dpch00mag9d18z&uy3ubftvh0u6o8=1db997de8e8463f1cf6d0307b6d50bf6&xnfrr0ncac=30836&cc4d76fdaf5=758

ご査収くださいませ。

今後、SNSマーケティングに関する情報収集を始められる際には、ぜひこちらにご連絡をいただきまして、
改めてお打合せのご調整をさせていただけますと幸いでございます。

引き続き、どうぞ宜しくお願い申し上げます。

--
「「「「「「「「「「「「「「「「「「「「
スマートシェア株式会社 Smart Share Inc.
〒150-0011
東京都渋谷区東2丁目22-14ロゼ氷川3階
Mail:　ap.sales@smartshare.jp
HP:　https://www.smartshare.jp/
「「「「「「「「「「「「「「「「「「「「"""


BODY_MIKOMI_CUSTOMER = """\
{company}
{person}

平素は大変お世話になっております。
スマートシェア株式会社の{poster_lastname}でございます。

本日はご多用の中、お電話致しました際、
ご丁寧にご対応いただきまして、誠に有難うございました。

お電話にてご紹介致しました、
弊社サービスの概要資料を添付にて送付いたします。
■SNSキャンペーンツール
https://www.ownly.jp/?a0v5la7bquf89=0e0369e2b250957f20dpch00mag9d18z&uy3ubftvh0u6o8=1db997de8e8463f1cf6d0307b6d50bf6&xnfrr0ncac=30836&cc4d76fdaf5=758

■＃を利用したインフルエンサーマーケティングツール
https://www.ownly.jp/lp/hashtag_seo?a0v5la7bquf89=0e0369e2b250957f20dpch00mag9d18z&uy3ubftvh0u6o8=1db997de8e8463f1cf6d0307b6d50bf6&xnfrr0ncac=30836&cc4d76fdaf5=758

ご査収くださいませ。

今後、SNSマーケティングに関する情報収集を始められる際には、
以下からお打合せのご調整をさせていただけますと幸いでございます。

■スマートシェアお打ち合わせ（佐藤）
https://timerex.net/s/ss_so/4a15c0ca

引き続き、どうぞ宜しくお願い申し上げます。

--
「「「「「「「「「「「「「「「「「「「「
スマートシェア株式会社 Smart Share Inc.
〒150-0011
東京都渋谷区東2丁目22-14ロゼ氷川3階
Mail:　ap.sales@smartshare.jp
HP:　https://www.smartshare.jp/
「「「「「「「「「「「「「「「「「「「「"""


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


def create_draft(service, sender, to, cc, bcc, subject, body):
    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = to
    msg["Cc"]      = cc
    msg["Bcc"]     = bcc
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
    try:
        rows = ss.worksheet(sheet_name).get_all_values()
    except Exception:
        return []
    records = []
    for row in rows[1:]:
        records.append({
            "会社名":      row[5].strip() if len(row) > 5 else "",
            "アポ取得者":  row[2].strip() if len(row) > 2 else "",
        })
    return records


def normalize(s):
    return re.sub(r'[\s　]', '', s).lower()


def extract_field(text, label):
    match = re.search(rf'{label}[\s　]*[：:]\s*(.+)', text)
    if not match:
        return ""
    val = match.group(1).strip()
    # <https://url|表示名> → 表示名
    val = re.sub(r'<[^>]+\|([^>]+)>', r'\1', val)
    # <https://url> → https://url（URLはそのまま残す）
    val = re.sub(r'<(https?://[^>]+)>', r'\1', val)
    # その他のSlackタグを除去
    val = re.sub(r'<[^>]+>', '', val)
    return val.strip()


def extract_name(raw):
    """「徳田様_トクダ様_女性」「大谷　諒様(オオタニ様／30代)」→「徳田 様」「大谷　諒 様」"""
    # カッコ内を除去
    name = re.sub(r'[（(][^）)]*[）)]', '', raw)
    # アンダースコア以降を除去
    name = name.split('_')[0].strip()
    # 末尾の様を除去してから「 様」を付ける
    name = re.sub(r'\s*様\s*$', '', name).strip()
    return name + ' 様'


def extract_lastname(full_name):
    """「黒江さとみ」→「黒江」（スペースがあれば最初の単語、なければ最初の2文字）"""
    parts = full_name.split()
    if len(parts) > 1:
        return parts[0]
    return full_name[:2] if len(full_name) >= 2 else full_name


def check_in_sheet(records, company, poster):
    norm_company = normalize(company)
    norm_poster  = normalize(poster)
    for rec in records:
        if not rec["会社名"]:
            continue
        if normalize(rec["会社名"]) in norm_company or norm_company in normalize(rec["会社名"]):
            if not rec["アポ取得者"] or normalize(rec["アポ取得者"])[:2] in norm_poster or norm_poster[:2] in normalize(rec["アポ取得者"]):
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
    if last_ts:
        res = client.conversations_history(channel=SLACK_CHANNEL_ID, limit=FETCH_LIMIT, oldest=last_ts)
    else:
        res = client.conversations_history(channel=SLACK_CHANNEL_ID, limit=FETCH_LIMIT)
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


def fetch_user_name(client, user_id):
    try:
        res     = client.users_info(user=user_id)
        profile = res["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or user_id
    except SlackApiError:
        return user_id


def post_slack_notify_apo(client, msg, poster_name, sheet_status, draft_status, body_text):
    check_label  = "✅ スプシ登録済み" if sheet_status == "registered" else "⚠️ スプシ未登録"
    draft_label  = ":e-mail: Gmail下書き作成済み" if draft_status == "ok" else ":x: 下書き作成失敗"
    text = (
        f"{SLACK_MENTION}\n"
        f"【元チャンネル】 #at-acty-pro　"
        f"【投稿日時】 {msg['投稿日時']}　"
        f"【投稿者】 {poster_name}　"
        f"【スプシ照合】 {check_label}　{draft_label}\n"
        f":warning:ログ未格納　ログURL未記入\n\n"
        f"{SLACK_MENTION_ACTY}\n"
        f"お世話になっております。\n"
        f"下記、アポイント獲得のご報告でございます。\n"
        f"恐れ入りますが、ご対応のほどよろしくお願いいたします。\n"
        f"```{body_text}```"
    )
    client.chat_postMessage(channel=SLACK_NOTIFY_CHANNEL, text=text)


def post_thread_reply_shiryo(client, msg, poster_name, draft_status):
    draft_label = ":e-mail: Gmail下書き作成済み" if draft_status == "ok" else ":x: 下書き作成失敗"
    text = (
        f"{SLACK_MENTION}\n"
        f"【元チャンネル】 #at-acty-pro　"
        f"【投稿日時】 {msg['投稿日時']}　"
        f"【投稿者】 {poster_name}\n"
        f"【スプシ照合】 対象外　{draft_label}"
    )
    client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text, thread_ts=msg["タイムスタンプ"])


def add_reaction(client, timestamp):
    try:
        client.reactions_add(channel=SLACK_CHANNEL_ID, timestamp=timestamp, name="ballot_box_with_check")
    except SlackApiError as e:
        if e.response.get("error") != "already_reacted":
            print(f"  → リアクション失敗: {e.response.get('error')}")


# ── メイン ──

def main():
    slack = WebClient(token=SLACK_BOT_TOKEN)

    print("スプレッドシートを読み込み中...")
    try:
        ss          = open_spreadsheet()
        apo_records = load_sheet(ss, "商談案件管理")
        print(f"  → アポ:{len(apo_records)}件")
    except Exception as e:
        print(f"スプレッドシートエラー: {e}")
        return

    print("Gmailに接続中...")
    try:
        gmail_acty = get_gmail_service(REFRESH_TOKEN_ACTY)
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
            poster  = fetch_user_name(slack, msg["投稿者ID"])
            poster_lastname = extract_lastname(poster)

            draft_status = "ok"
            sheet_status = "unregistered"
            body_text    = ""

            if "【アポ" in text:
                company     = extract_field(text, "会社名")
                raw_person  = extract_field(text, "担当者名")
                person      = extract_name(raw_person)
                to_email    = extract_field(text, "e-mail")
                appt_dt     = extract_field(text, "商談日時")
                meeting_url = extract_field(text, "会議URL")
                sheet_rec   = check_in_sheet(apo_records, company, poster)
                sheet_status = "registered" if sheet_rec else "unregistered"
                body_text   = text[text.find("【アポ"):].strip()

                try:
                    body_customer = BODY_APO_CUSTOMER.format(
                        company=company,
                        person=person,
                        poster_lastname=poster_lastname,
                        datetime=appt_dt,
                        meeting_url=meeting_url,
                    )
                    create_draft(
                        gmail_acty,
                        MAIL_ACTY_FROM,
                        to_email,
                        MAIL_ACTY_TO_APO_CC,
                        MAIL_ACTY_BCC,
                        SUBJECT_APO,
                        body_customer,
                    )
                except Exception as e:
                    print(f"  → Gmail下書き失敗: {e}")
                    draft_status = "error"

                post_slack_notify_apo(slack, msg, poster, sheet_status, draft_status, body_text)

            elif "【見込み" in text:
                company    = extract_field(text, "企業名")
                raw_person = extract_field(text, "氏名")
                person     = extract_name(raw_person)
                to_email   = extract_field(text, "e-mail")

                try:
                    body_customer = BODY_MIKOMI_CUSTOMER.format(
                        company=company,
                        person=person,
                        poster_lastname=poster_lastname,
                    )
                    create_draft(
                        gmail_acty,
                        MAIL_ACTY_FROM,
                        to_email,
                        MAIL_ACTY_TO_APO_CC,
                        MAIL_ACTY_BCC,
                        SUBJECT_SHIRYO,
                        body_customer,
                    )
                except Exception as e:
                    print(f"  → Gmail下書き失敗: {e}")
                    draft_status = "error"

                post_thread_reply_shiryo(slack, msg, poster, draft_status)

            elif "【資料" in text:
                company    = extract_field(text, "企業名")
                raw_person = extract_field(text, "氏名")
                person     = extract_name(raw_person)
                to_email   = extract_field(text, "e-mail")

                try:
                    body_customer = BODY_SHIRYO_CUSTOMER.format(
                        company=company,
                        person=person,
                        poster_lastname=poster_lastname,
                    )
                    create_draft(
                        gmail_acty,
                        MAIL_ACTY_FROM,
                        to_email,
                        MAIL_ACTY_TO_APO_CC,
                        MAIL_ACTY_BCC,
                        SUBJECT_SHIRYO,
                        body_customer,
                    )
                except Exception as e:
                    print(f"  → Gmail下書き失敗: {e}")
                    draft_status = "error"

                post_thread_reply_shiryo(slack, msg, poster, draft_status)

            # リアクション
            try:
                add_reaction(slack, msg["タイムスタンプ"])
                print(f"  → 処理完了: {keyword} / 下書き:{draft_status}")
            except SlackApiError as e:
                print(f"  → リアクション失敗: {e.response.get('error')}")

        print("\n完了！")

    except SlackApiError as e:
        code = e.response.get("error", "不明")
        print(f"Slack APIエラー: {code}")
        if code == "missing_scope":
            print(f"→ 権限不足: {e.response.get('needed')}")


if __name__ == "__main__":
    main()
