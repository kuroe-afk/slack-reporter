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

MENTION_OKA      = "<@U02KSNGJJ4C>"
SLACK_MENTION    = os.getenv("SLACK_MENTION", "")
REMINDER_DAYS    = 3
REMINDER_KEYWORD = "こちらの進捗確認をお願いします"

TAB_SANGO    = "SANGO様"
TAB_TASUKARU = "Tasukaru"

# 列番号（1-based）
COL_DONE     = {TAB_SANGO: 13, TAB_TASUKARU: 15}   # M列 / O列
COL_PROGRESS = {TAB_SANGO: 14, TAB_TASUKARU: 16}   # N列 / P列

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


def extract_field(text, labels, multiline=False):
    for label in labels:
        if multiline:
            # 次のフィールド（〇〇：）または末尾まで複数行取得
            m = re.search(rf'{label}[\s　]*[：:]\s*([\s\S]+?)(?=\n\S+[\s　]*[：:]|\Z)', text)
            if m:
                return m.group(1).strip()
        else:
            m = re.search(rf'{label}[\s　]*[：:]\s*(.+?)(?:\n|$)', text)
            if m:
                return m.group(1).strip()
    return ""


def make_management_id(ts):
    dt = datetime.datetime.fromtimestamp(float(ts), tz=ZoneInfo("Asia/Tokyo"))
    return f"LST-{dt.strftime('%Y%m%d-%H%M%S')}"


def make_thread_url(ts, thread_ts=None):
    ts_nodot = ts.replace('.', '')
    url = f"https://slack.com/archives/{SLACK_CHANNEL_ID}/p{ts_nodot}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}"
    return url


def ts_from_url(url):
    """SlackスレッドURLからts（とthread_ts）を復元"""
    m = re.search(r'/p(\d+)', url)
    if not m:
        return None, None
    raw = m.group(1)
    ts = raw[:10] + '.' + raw[10:]
    m2 = re.search(r'thread_ts=([\d.]+)', url)
    thread_ts = m2.group(1) if m2 else None
    return ts, thread_ts


# ── Slack ──

def load_last_timestamp():
    if os.path.exists(LAST_TS_FILE):
        with open(LAST_TS_FILE, "r") as f:
            return f.read().strip()
    return None


def save_last_timestamp(ts):
    with open(LAST_TS_FILE, "w") as f:
        f.write(str(ts))


SURNAME_MAP = {
    "黒江": "黒江",
    "岩井": "岩井",
    "高木": "高木",
    "入井": "入井",
    "播磨谷": "播磨谷",
    "岡": "岡",
}

def get_user_surname(client, user_id):
    try:
        res     = client.users_info(user=user_id)
        profile = res["user"]["profile"]
        name    = profile.get("display_name") or profile.get("real_name") or ""
        # 登録済み苗字と前方一致で判定
        for surname in SURNAME_MAP:
            if name.startswith(surname):
                return surname
        # 未登録の場合はスペース前の最初の単語
        return name.split()[0] if name else user_id
    except SlackApiError:
        return user_id


def get_reaction_names(client, ts, thread_ts=None):
    """指定tsの:woman-gesturing-ok:リアクション者の苗字リストを返す"""
    try:
        if thread_ts:
            # スレッド返信はconversations_repliesで取得
            res      = client.conversations_replies(channel=SLACK_CHANNEL_ID, ts=thread_ts)
            messages = [m for m in res.get("messages", []) if m.get("ts") == ts]
        else:
            res = client.conversations_history(
                channel=SLACK_CHANNEL_ID,
                latest=ts,
                oldest=str(float(ts) - 1),
                inclusive=True,
                limit=1
            )
            messages = res.get("messages", [])
        reactions = messages[0].get("reactions", []) if messages else []

        names = []
        for r in reactions:
            if r.get("name", "").startswith(REACTION_NAME):
                for uid in r.get("users", []):
                    names.append(get_user_surname(client, uid))
        return "、".join(names)
    except SlackApiError:
        return ""


def extract_progress_report(text):
    """【作業経過報告】の対応範囲〜メモ:の内容を抽出"""
    m = re.search(r'対応範囲[\s　]*[：:]\s*([\s\S]+?)(?=\n*$)', text)
    if not m:
        return ""
    return m.group(0).strip()


def get_thread_progress_and_done(client, parent_ts):
    """スレッド内の最新【作業経過報告】と【作業終了】を返す"""
    progress_text = ""
    is_done       = False
    try:
        res      = client.conversations_replies(channel=SLACK_CHANNEL_ID, ts=parent_ts)
        messages = res.get("messages", [])
        for msg in messages[1:]:
            text = msg.get("text", "")
            if "【作業経過報告】" in text:
                extracted = extract_progress_report(text)
                if extracted:
                    progress_text = extracted  # 上書き（最新を使う）
            if "【作業終了】" in text:
                is_done = True
    except SlackApiError:
        pass
    return progress_text, is_done


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


def needs_reminder(tab, row):
    """リマインドが必要かどうかを判定"""
    def is_empty(val):
        return not val or val.strip() in ("", "FALSE", "False")

    if tab == TAB_SANGO:
        g = row[6] if len(row) > 6 else ""
        h = row[7] if len(row) > 7 else ""
        return is_empty(g) or is_empty(h)

    if tab == TAB_TASUKARU:
        g = row[6] if len(row) > 6 else ""
        j = row[9] if len(row) > 9 else ""
        h = row[7] if len(row) > 7 else ""
        i = row[8] if len(row) > 8 else ""
        return (is_empty(g) or is_empty(j)) or (is_empty(h) and is_empty(i))

    return False


def already_reminded(client, ts):
    """スレッドにリマインド済みかどうか確認"""
    try:
        res = client.conversations_replies(channel=SLACK_CHANNEL_ID, ts=ts)
        for msg in res.get("messages", [])[1:]:
            if REMINDER_KEYWORD in msg.get("text", ""):
                return True
    except SlackApiError:
        pass
    return False


def send_reminder(client, ts):
    """スレッドにリマインドを投稿"""
    text = (
        f"{MENTION_OKA} {SLACK_MENTION}\n"
        f":warning:こちらの進捗確認をお願いします。"
    )
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text, thread_ts=ts)
        print(f"  → リマインド送信: ts={ts}")
    except SlackApiError as e:
        print(f"  → リマインド失敗: {e.response.get('error')}")


def add_reaction(client, ts):
    try:
        client.reactions_add(channel=SLACK_CHANNEL_ID, timestamp=ts, name="g")
    except SlackApiError as e:
        if e.response.get("error") != "already_reacted":
            print(f"  → リアクション失敗: {e.response.get('error')}")


# ── メイン処理 ──

def register_message(client, ss, text, ts, thread_ts=None):
    """1件のメッセージをスプシに登録する。thread_ts は返信先スレッドのts。"""
    tab = detect_tab(text)
    if not tab:
        print(f"  → 企業名不明のためスキップ: {text[:40]}")
        return

    ws   = ss.worksheet(tab)
    urls = ws.col_values(5)
    url  = make_thread_url(ts, thread_ts)
    if url in urls:
        return  # 重複スキップ

    dt_str  = datetime.datetime.fromtimestamp(float(ts), tz=ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S")
    mgmt_id = make_management_id(ts)
    anken   = extract_field(text, ["案件名"])
    content = re.sub(r'[\r\n]+', ' ', extract_field(text, ["内容"], multiline=True)).strip()
    f_names = get_reaction_names(client, ts, thread_ts)
    g_check = has_csv_reply(client, thread_ts or ts)

    a_col    = ws.col_values(1)
    next_row = next((i + 1 for i, v in enumerate(a_col) if i > 0 and not v.strip()), len(a_col) + 1)
    ws.update(f"A{next_row}:G{next_row}",
              [[mgmt_id, dt_str, anken, content, url, f_names, g_check]],
              value_input_option="USER_ENTERED")

    reply_ts = thread_ts or ts
    try:
        client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=f":white_check_mark: 管理番号 {mgmt_id} で登録しました",
            thread_ts=reply_ts
        )
    except SlackApiError as e:
        print(f"  → 返信失敗: {e.response.get('error')}")

    add_reaction(client, ts)
    print(f"  → スプシ追記: [{tab}] {mgmt_id} / {anken}")


def process_new_messages(client, ss):
    """新着メッセージ（親投稿＋スレッド返信）をスプシに追記"""
    last_ts  = load_last_timestamp()
    fetch_ts = datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).timestamp()

    kwargs = {"channel": SLACK_CHANNEL_ID, "limit": FETCH_LIMIT}
    if last_ts:
        kwargs["oldest"] = last_ts

    res      = client.conversations_history(**kwargs)
    messages = res.get("messages", [])
    print(f"  → {len(messages)} 件取得（親投稿）")

    # 親投稿を処理しつつ、返信があるものはスレッドも確認
    parent_ts_list = []
    for msg in reversed(messages):
        text = msg.get("text", "")
        ts   = msg.get("ts", "")
        if TRIGGER_KEYWORD in text:
            register_message(client, ss, text, ts)
        if int(msg.get("reply_count", 0)) > 0:
            parent_ts_list.append(ts)

    # スレッド返信を確認（last_ts以降の新着返信のみ）
    for parent_ts in parent_ts_list:
        try:
            rep_kwargs = {"channel": SLACK_CHANNEL_ID, "ts": parent_ts}
            if last_ts:
                rep_kwargs["oldest"] = last_ts
            res2    = client.conversations_replies(**rep_kwargs)
            replies = res2.get("messages", [])
            for reply in replies:
                if reply.get("ts") == parent_ts:
                    continue  # 親投稿自身はスキップ
                if TRIGGER_KEYWORD in reply.get("text", ""):
                    register_message(client, ss, reply["text"], reply["ts"], thread_ts=parent_ts)
        except SlackApiError as e:
            print(f"  → スレッド取得失敗: {e.response.get('error')}")

    # 親投稿がlast_ts以前でもスレッドに新着返信がある可能性を拾う
    # → チャンネル全体の最新100件の親投稿のスレッドも確認
    res3     = client.conversations_history(channel=SLACK_CHANNEL_ID, limit=FETCH_LIMIT)
    all_msgs = res3.get("messages", [])
    for msg in all_msgs:
        parent_ts = msg.get("ts", "")
        if int(msg.get("reply_count", 0)) == 0:
            continue
        if parent_ts in parent_ts_list:
            continue  # 上で処理済み
        try:
            rep_kwargs = {"channel": SLACK_CHANNEL_ID, "ts": parent_ts}
            if last_ts:
                rep_kwargs["oldest"] = last_ts
            res4    = client.conversations_replies(**rep_kwargs)
            replies = res4.get("messages", [])
            for reply in replies:
                if reply.get("ts") == parent_ts:
                    continue
                if TRIGGER_KEYWORD in reply.get("text", ""):
                    register_message(client, ss, reply["text"], reply["ts"], thread_ts=parent_ts)
        except SlackApiError as e:
            print(f"  → スレッド取得失敗: {e.response.get('error')}")

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

            ts, thread_ts = ts_from_url(url)
            if not ts:
                continue

            # F列: リアクション（空または更新が必要な場合）
            current_f = row[5] if len(row) > 5 else ""
            new_f     = get_reaction_names(client, ts, thread_ts)
            if new_f and new_f != current_f:
                ws.update_cell(i, 6, new_f)
                print(f"  → F列更新: {tab} 行{i} → {new_f}")

            # G列: CSV確認依頼（未チェックの場合のみ確認）
            current_g = row[6] if len(row) > 6 else ""
            parent_ts = thread_ts or ts
            if current_g not in ("TRUE", "True", True):
                if has_csv_reply(client, parent_ts):
                    ws.update_cell(i, 7, True)
                    print(f"  → G列チェック: {tab} 行{i}")

            # 作業経過報告・作業終了チェック
            progress, is_done = get_thread_progress_and_done(client, parent_ts)
            if progress:
                current_progress = row[COL_PROGRESS[tab] - 1] if len(row) >= COL_PROGRESS[tab] else ""
                if progress != current_progress:
                    ws.update_cell(i, COL_PROGRESS[tab], progress)
                    print(f"  → 作業経過報告更新: {tab} 行{i}")
            if is_done:
                current_done = row[COL_DONE[tab] - 1] if len(row) >= COL_DONE[tab] else ""
                if current_done not in ("TRUE", "True", True):
                    ws.update_cell(i, COL_DONE[tab], True)
                    print(f"  → 作業終了チェック: {tab} 行{i}")
                # 作業メモを空白に戻す
                ws.update_cell(i, COL_PROGRESS[tab], "")
                print(f"  → 作業メモクリア: {tab} 行{i}")

            # リマインド: 投稿から3日経過 かつ 条件未達成 かつ 未送信
            post_dt = datetime.datetime.fromtimestamp(float(ts), tz=ZoneInfo("Asia/Tokyo"))
            age     = datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")) - post_dt
            if age.days >= REMINDER_DAYS and needs_reminder(tab, row) and not already_reminded(client, parent_ts):
                send_reminder(client, parent_ts)


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
