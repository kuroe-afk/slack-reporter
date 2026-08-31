"""
リスト作成channel の【リスト作成依頼】投稿を監視し、スプレッドシートへ自動入力する。

A: 管理番号
B: 投稿日時
C: 案件名
D: 内容
E: SlackスレッドURL
F: :woman-gesturing-ok: リアクションした人の苗字（複数可）
G: 【CSV確認依頼】がスレッド返信にあればTrue
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

SLACK_BOT_TOKEN  = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = "C0ADL8GR4NT"   # リスト作成channel

SPREADSHEET_ID   = "1AJP-zT6c_k_6zWy2Dkg5Jxo13yruYO131IFequL8Bk8"
CREDENTIALS_FILE = "credentials.json"
LAST_TS_FILE     = "last_timestamp_list_request.txt"

TRIGGER_KEYWORD  = "【リスト作成依頼】"
CSV_KEYWORD      = "【CSV確認依頼】"
REACTION_NAME    = "woman-gesturing-ok"

TAB_SANGO    = "SANGO様"
TAB_TASUKARU = "Tasukaru"

FETCH_LIMIT = 100


# ── スプレッドシート ──

def open_spreadsheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc    = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def detect_tab(text):
    """企業名フィールドからタブ名を判定"""
    m = re.search(r'企業名[\s　]*[：:]\s*(.+)', text)
    if not m:
        return None
    company = m.group(1).strip()
    # 全角→半角、大文字統一
    company_norm = company.replace('　', '').replace(' ', '').lower()
    company_norm = company_norm.translate(str.maketrans(
        'ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ'
        'ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ',
        'abcdefghijklmnopqrstuvwxyz' * 2
    ))
    if 'sango' in company_norm or 'さんご' in company_norm:
        return TAB_SANGO
    if 'tasukaru' in company_norm or 'たすかる' in company_norm:
        return TAB_TASUKARU
    return None


def extract_field(text, labels):
    for label in labels:
        m = re.search(rf'{label}[\s　]*[：:]\s*(.+?)(?:\n|$)', text)
        if m:
            return m.group(1).strip()
    return ""


def make_management_id(ts):
    dt = datetime.datetime.fromtimestamp(float(ts), tz=ZoneInfo("Asia/Tokyo"))
    return f"LST-{dt.strftime('%Y%m%d-%H%M%S')}"


def make_thread_url(ts):
    ts_nodot = ts.replace('.', '')
    return f"https://slack.com/archives/{SLACK_CHANNEL_ID}/p{ts_nodot}"


def ts_from_url(url):
    """SlackスレッドURLからtsを復元"""
    m = re.search(r'/p(\d+)$', url)
    if not m:
        return None
    raw = m.group(1)
    return raw[:10] + '.' + raw[10:]


# ── Slack ──

def load_last_timestamp():
    if os.path.exists(LAST_TS_FILE):
        with open(LAST_TS_FILE, "r") as f:
            return f.read().strip()
    return None


def save_last_timestamp(ts):
    with open(LAST_TS_FILE, "w") as f:
        f.write(str(ts))


def get_user_surname(client, user_id):
    try:
        res     = client.users_info(user=user_id)
        profile = res["user"]["profile"]
        name    = profile.get("display_name") or profile.get("real_name") or ""
        # 苗字のみ（スペース区切りの最初、または全体）
        return name.split()[0] if name else user_id
    except SlackApiError:
        return user_id


def get_reaction_names(client, ts):
    """指定tsの:woman-gesturing-ok:リアクション者の苗字リストを返す"""
    try:
        res       = client.reactions_get(channel=SLACK_CHANNEL_ID, timestamp=ts)
        reactions = res.get("message", {}).get("reactions", [])
        names = []
        for r in reactions:
            if r.get("name") == REACTION_NAME:
                for uid in r.get("users", []):
                    names.append(get_user_surname(client, uid))
        return "、".join(names)
    except SlackApiError:
        return ""


def has_csv_reply(client, ts):
    """スレッド返信に【CSV確認依頼】があればTrue"""
    try:
        res      = client.conversations_replies(channel=SLACK_CHANNEL_ID, ts=ts)
        messages = res.get("messages", [])
        for msg in messages[1:]:   # 最初は親投稿なのでスキップ
            if CSV_KEYWORD in msg.get("text", ""):
                return True
    except SlackApiError:
        pass
    return False


def add_reaction(client, ts):
    try:
        client.reactions_add(channel=SLACK_CHANNEL_ID, timestamp=ts, name="g")
    except SlackApiError as e:
        if e.response.get("error") != "already_reacted":
            print(f"  → リアクション失敗: {e.response.get('error')}")


# ── メイン処理 ──

def process_new_messages(client, ss):
    """新着メッセージをスプシに追記"""
    last_ts  = load_last_timestamp()
    fetch_ts = datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).timestamp()

    kwargs = {"channel": SLACK_CHANNEL_ID, "limit": FETCH_LIMIT}
    if last_ts:
        kwargs["oldest"] = last_ts

    res      = client.conversations_history(**kwargs)
    messages = res.get("messages", [])
    print(f"  → {len(messages)} 件取得")

    for msg in reversed(messages):
        text = msg.get("text", "")
        if TRIGGER_KEYWORD not in text:
            continue

        ts  = msg.get("ts", "")
        tab = detect_tab(text)
        if not tab:
            print(f"  → 企業名不明のためスキップ: {text[:40]}")
            continue

        # 既存行チェック（重複防止）
        ws   = ss.worksheet(tab)
        urls = ws.col_values(5)   # E列
        url  = make_thread_url(ts)
        if url in urls:
            continue

        dt_str  = datetime.datetime.fromtimestamp(float(ts), tz=ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S")
        mgmt_id = make_management_id(ts)
        anken   = extract_field(text, ["案件名"])
        content = extract_field(text, ["内容"])
        f_names = get_reaction_names(client, ts)
        g_check = has_csv_reply(client, ts)

        ws.append_row(
            [mgmt_id, dt_str, anken, content, url, f_names, g_check],
            value_input_option="USER_ENTERED"
        )
        add_reaction(client, ts)
        print(f"  → スプシ追記: [{tab}] {mgmt_id} / {anken}")

    save_last_timestamp(fetch_ts)


def update_existing_rows(client, ss):
    """既存行のF列（リアクション）とG列（CSV確認）を更新"""
    for tab in [TAB_SANGO, TAB_TASUKARU]:
        try:
            ws   = ss.worksheet(tab)
            rows = ws.get_all_values()
        except Exception as e:
            print(f"  → {tab} 読み込み失敗: {e}")
            continue

        for i, row in enumerate(rows, start=1):
            if len(row) < 5 or not row[4]:
                continue
            url = row[4]
            if not url.startswith("https://slack.com/archives/"):
                continue

            ts = ts_from_url(url)
            if not ts:
                continue

            # F列: リアクション（空または更新が必要な場合）
            current_f = row[5] if len(row) > 5 else ""
            new_f     = get_reaction_names(client, ts)
            if new_f and new_f != current_f:
                ws.update_cell(i, 6, new_f)
                print(f"  → F列更新: {tab} 行{i} → {new_f}")

            # G列: CSV確認依頼（未チェックの場合のみ確認）
            current_g = row[6] if len(row) > 6 else ""
            if current_g not in ("TRUE", "True", True):
                if has_csv_reply(client, ts):
                    ws.update_cell(i, 7, True)
                    print(f"  → G列チェック: {tab} 行{i}")


def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return

    slack = WebClient(token=SLACK_BOT_TOKEN)

    print("スプレッドシートを開いています...")
    try:
        ss = open_spreadsheet()
    except Exception as e:
        print(f"スプレッドシートエラー: {e}")
        return

    print("新着メッセージを処理中...")
    try:
        process_new_messages(slack, ss)
    except SlackApiError as e:
        print(f"Slack APIエラー: {e.response.get('error')}")
        return

    print("既存行のF・G列を更新中...")
    update_existing_rows(slack, ss)

    print("完了！")


if __name__ == "__main__":
    main()
