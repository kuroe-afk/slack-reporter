"""
架電開始チェック → スプレッドシート〇入力
平日 10:30 / 17:30 JST に実行
各チャンネルで今日の【架電開始】投稿を確認し、B案件名シートに〇を入力する
"""

import os
import re
import datetime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

SLACK_BOT_TOKEN  = os.getenv("SLACK_BOT_TOKEN")
SPREADSHEET_ID   = "1xbvLkaaTE2X9NSSprjRJIAUflhJYvO6t0IdR9abHKKU"
SHEET_GID        = 762448710
CREDENTIALS_FILE = "credentials.json"
KAIDEN_KEYWORD   = "【架電開始】"

JST = ZoneInfo("Asia/Tokyo")

# チャンネルID → 案件名（スプレッドシート上の行ラベル）
CHANNEL_ANKEN = {
    "C0ATN807RNG": "MIRAI",        # miraisama
    "C08SM1MTW4W": "ixrea",        # ixrea
    "C07959RG11V": "サムライズ",    # サムライズさま
    "C07KPLGTDS4": "tasukaru_shinki",  # tasukaru-shinnki
    "C0BJSJA4AN7": "採用代行",          # 採用代行
}


def open_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(SPREADSHEET_ID)
    for ws in ss.worksheets():
        if ws.id == SHEET_GID:
            return ws
    raise Exception(f"シートが見つかりません (GID: {SHEET_GID})")


def normalize_date(cell_value):
    """セルの日付表記を 'M/D' 形式に正規化"""
    v = str(cell_value).strip()
    m = re.match(r'(\d{1,2}/\d{1,2})', v)
    if m:
        return m.group(1)
    m2 = re.match(r'\d{4}/0?(\d+)/0?(\d+)', v)
    if m2:
        return f"{int(m2.group(1))}/{int(m2.group(2))}"
    return v


def build_maps(all_values):
    """日付列インデックスと案件行インデックスのマップを作成"""
    header_row = all_values[0]
    date_col_map = {}
    for col_idx, cell in enumerate(header_row):
        nd = normalize_date(cell)
        if nd and nd not in date_col_map:
            date_col_map[nd] = col_idx

    anken_row_map = {}
    all_anken = set(CHANNEL_ANKEN.values())
    for row_idx, row in enumerate(all_values):
        for cell in row:
            stripped = cell.strip()
            if stripped in all_anken and stripped not in anken_row_map:
                anken_row_map[stripped] = row_idx
                break

    return date_col_map, anken_row_map


def check_kaiden_today(client, channel_id):
    """今日の【架電開始】投稿があればTrueを返す"""
    now = datetime.datetime.now(tz=JST)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        res = client.conversations_history(
            channel=channel_id,
            oldest=str(today_start.timestamp()),
            limit=200,
        )
        for msg in res.get("messages", []):
            if KAIDEN_KEYWORD in msg.get("text", ""):
                return True
    except SlackApiError as e:
        print(f"  取得エラー ({channel_id}): {e.response.get('error')}")
    return False


def main():
    if not SLACK_BOT_TOKEN:
        print("Error: SLACK_BOT_TOKEN not set")
        return

    slack = WebClient(token=SLACK_BOT_TOKEN)
    today = datetime.datetime.now(tz=JST)
    today_str = f"{today.month}/{today.day}"
    weekday = today.weekday()  # 0=月, 6=日

    if weekday >= 5:
        print(f"今日は週末（{today_str}）のためスキップします")
        return

    print(f"架電開始チェック: {today_str}")
    print(f"スプレッドシートを読み込み中...")

    ws = open_sheet()
    all_values = ws.get_all_values()
    date_col_map, anken_row_map = build_maps(all_values)

    if today_str not in date_col_map:
        print(f"スプレッドシートに {today_str} の列が存在しません。スキップします。")
        return

    col_idx = date_col_map[today_str]
    print(f"  {today_str} → 列 {col_idx + 1}")

    updates = []
    for ch_id, anken in CHANNEL_ANKEN.items():
        if anken not in anken_row_map:
            print(f"  [!] 案件行が見つかりません: {anken}")
            continue

        row_idx = anken_row_map[anken]
        current = all_values[row_idx][col_idx] if col_idx < len(all_values[row_idx]) else ""

        if current == "〇":
            print(f"  {anken}: 既に〇 (スキップ)")
            continue

        found = check_kaiden_today(slack, ch_id)
        if found:
            r = row_idx + 1
            c = col_idx + 1
            updates.append({
                "range": gspread.utils.rowcol_to_a1(r, c),
                "values": [["〇"]],
            })
            print(f"  {anken}: 【架電開始】確認 -> 〇 を入力 (行{r}列{c})")
        else:
            print(f"  {anken}: 【架電開始】なし")

    if updates:
        print(f"\n{len(updates)} セルを更新中...")
        ws.batch_update(updates)
        print("完了！")
    else:
        print("\n更新対象なし。")


if __name__ == "__main__":
    main()
