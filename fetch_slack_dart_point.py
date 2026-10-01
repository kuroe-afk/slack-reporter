"""
ダーツポイントシステム
月予算: 10,000円 / 賞品: Amazonギフト券1,000円 / 月最大10名当選
先月スピンしてハズレた人の当選確率を優先的に上げる

スプシ構成:
  [ポイント管理] A:ユーザーID B:現在ポイント C:合計獲得 D:名前 E:最終スピン月 F:最終当選月
  [履歴]        A:日時 B:種別 C:付与者ID D:受取者ID E:変動ポイント F:メモ
  [月予算]       A:年月 B:当選済み人数 C:総スピン数
  [処理済みリアクション] A:キー（channel_ts_fromuser）
"""

import os
import re
import random
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

SLACK_BOT_TOKEN         = os.getenv("SLACK_BOT_TOKEN")
NAHOOKA_BOT_TOKEN       = os.getenv("NAHOOKA_BOT_TOKEN")
OKA_USER_ID      = "U02KSNGJJ4C"    # 岡さん（公式ポイント付与者）
GENERAL_CHANNEL  = "C02KQG6HGCA"    # general
DART_EMOJI       = "dart"
ROULETTE_KEYWORD = "【ルーレット】"

POINTS_TO_SPIN  = 10
MONTHLY_BUDGET  = 10000
PRIZE_AMOUNT    = 1000
MAX_WINNERS     = MONTHLY_BUDGET // PRIZE_AMOUNT  # 10
EXPECTED_SPINS  = 20   # 推定月間スピン数（スタッフ約20名）
DART_TEXT       = "🎯"  # 岡さんがコメントに書くダーツ絵文字

DART_PAGE_URL = "https://claude.ai/artifact/ShjoaytZJzigWQPdj4oRZq"

# 【アポ】自動🎯返信：2ポイントチャンネル
APO_DOUBLE_CHANNELS = {
    "C0BDP8Y3N1K",  # sangosama-ai経営技研
    "C0BLMAYP7KR",  # sangosama-sky-代理店
    "C06BH8LBUKG",  # sangosama-摩須-謙太郎さま
    "C0B3SNTRA8J",  # sangosama-cooperative
    "C08LFP5F7UK",  # sangosama-ターミニックス
    "C0C1G1YS0ET",  # sango-代理店開拓
}
PRIZE_NAME    = f"Amazonギフト券{PRIZE_AMOUNT:,}円"

# 他スタッフのリアクションを監視するチャンネル（必要に応じて追加）
MONITOR_CHANNELS = [GENERAL_CHANNEL]

SPREADSHEET_ID   = os.getenv("DART_POINT_SPREADSHEET_ID",
                              "15XHLNuRfobSTKay_qlNqS1rdG6e83-bWCt26Gjx5zD0")
CREDENTIALS_FILE = "credentials.json"
LAST_TS_FILE     = "last_timestamp_dart_point.txt"

SHEET_POINTS    = "ポイント管理"
SHEET_HISTORY   = "履歴"
SHEET_BUDGET    = "月予算"
SHEET_PROCESSED = "処理済みリアクション"

# ポイント管理 列インデックス（0始まり）
C_UID, C_PTS, C_TOT, C_NAME, C_LSPIN, C_LWIN = 0, 1, 2, 3, 4, 5


# ── 日付ユーティリティ ──────────────────────────

def current_month():
    return datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).strftime("%Y-%m")

def last_month():
    now = datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo"))
    first = now.replace(day=1)
    prev  = first - datetime.timedelta(days=1)
    return prev.strftime("%Y-%m")

def now_ts():
    return datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).timestamp()

def now_str():
    return datetime.datetime.now(tz=ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S")


# ── スプレッドシート ─────────────────────────────

def open_spreadsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def _int(val):
    try:
        return int(val) if val else 0
    except (ValueError, TypeError):
        return 0


def load_users(ws):
    """全ユーザーデータを {user_id: (row_idx, row_data)} で返す"""
    rows = ws.get_all_values()
    result = {}
    for i, row in enumerate(rows[1:], start=2):
        if row and row[C_UID]:
            result[row[C_UID]] = (i, row)
    return result


def get_user(users, user_id):
    """ユーザーデータを返す。存在しない場合は (None, []) を返す"""
    return users.get(user_id, (None, []))


def upsert_user(ws, users, user_id, points, total, name, last_spin, last_win):
    """ユーザーをupdateまたはappend"""
    row = [user_id, points, total, name, last_spin, last_win]
    row_idx, _ = get_user(users, user_id)
    if row_idx:
        ws.update(f"A{row_idx}:F{row_idx}", [row], value_input_option="USER_ENTERED")
    else:
        ws.append_row(row, value_input_option="USER_ENTERED")


def add_history(ws, kind, from_id, to_id, delta, memo=""):
    ws.append_row([now_str(), kind, from_id, to_id, delta, memo],
                  value_input_option="USER_ENTERED")


def get_month_data(ws):
    """今月の (row_idx, winners, spins) を返す。なければ行を追加"""
    ym   = current_month()
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], start=2):
        if row and row[0] == ym:
            return i, _int(row[1] if len(row) > 1 else 0), _int(row[2] if len(row) > 2 else 0)
    ws.append_row([ym, 0, 0], value_input_option="USER_ENTERED")
    return len(rows) + 1, 0, 0


def update_month_data(ws, row_idx, winners, spins):
    ws.update(f"B{row_idx}:C{row_idx}", [[winners, spins]],
              value_input_option="USER_ENTERED")


def load_processed_keys(ws):
    rows = ws.get_all_values()
    return set(row[0] for row in rows if row and row[0])


def add_processed_keys(ws, keys):
    if keys:
        ws.append_rows([[k] for k in keys], value_input_option="USER_ENTERED")


# ── 確率計算 ────────────────────────────────────

def calc_win_prob(remaining_winners, total_spins, last_spin_month, last_win_month):
    """
    先月ハズレ優先の当選確率を計算する
    - 先月スピンしてハズレた人 → x1.45（確率UP）
    - 先月当選した人         → x0.55（確率DOWN）
    - それ以外              → x1.0（通常）
    """
    if remaining_winners <= 0:
        return 0.0

    remaining_spins = max(EXPECTED_SPINS - total_spins, 1)
    base_prob       = remaining_winners / remaining_spins

    lm = last_month()
    if last_win_month == lm:
        multiplier = 0.55   # 先月当選
    elif last_spin_month == lm and last_win_month != lm:
        multiplier = 1.45   # 先月ハズレ
    else:
        multiplier = 1.0    # 通常

    return min(max(base_prob * multiplier, 0.05), 0.92)


# ── Slack ────────────────────────────────────────

def get_user_name(client, user_id):
    try:
        res = client.users_info(user=user_id)
        p   = res["user"]["profile"]
        return p.get("display_name") or p.get("real_name") or user_id
    except SlackApiError:
        return user_id


def send_dm(client, user_id, text):
    try:
        client.chat_postMessage(channel=user_id, text=text)
    except SlackApiError as e:
        print(f"  → DM失敗({user_id}): {e.response.get('error')}")


def notify_general_10pt(client, user_id):
    """10ポイント達成をgeneralに通知"""
    text = (
        f"🎯✨ おめでとうございます！<@{user_id}> さん 🎉\n"
        f"ついに *10ポイント* 達成です！🏆🎰\n"
        f"このメッセージのスレッドに *【ルーレット】* と返信してください 🍀\n"
        f"Good luck! 🌟"
    )
    try:
        res = client.chat_postMessage(channel=GENERAL_CHANNEL, text=text)
        return res["ts"]
    except SlackApiError as e:
        print(f"  → general通知失敗: {e.response.get('error')}")
        return None


# ── ポイント処理 ─────────────────────────────────

def process_point(client, nahooka_client, ss, from_user_id, to_user_id, is_official):
    """
    🎯リアクション1件を処理する
    is_official=True  → 岡さんからの公式付与
    is_official=False → 他スタッフからの贈与（送り主のポイントを消費）
    戻り値: 付与後の受取者ポイント数（失敗時はNone）
    """
    ws_pts  = ss.worksheet(SHEET_POINTS)
    ws_hist = ss.worksheet(SHEET_HISTORY)
    ws_bud  = ss.worksheet(SHEET_BUDGET)

    to_name   = get_user_name(client, to_user_id)
    from_name = get_user_name(client, from_user_id)
    users     = load_users(ws_pts)

    # ── 贈与の場合: 送り主のポイントを確認・消費 ──
    if not is_official:
        _, from_row = get_user(users, from_user_id)
        from_pts = _int(from_row[C_PTS] if len(from_row) > C_PTS else 0)
        if from_pts <= 0:
            print(f"  → 贈与失敗: {from_name} のポイント不足({from_pts}pt)")
            send_dm(nahooka_client, from_user_id,
                    f"😢 ポイントが不足しているため贈与できませんでした。\n"
                    f"現在のポイント：★ *{from_pts}pt*")
            return None

        from_total = _int(from_row[C_TOT] if len(from_row) > C_TOT else 0)
        from_lspin = from_row[C_LSPIN] if len(from_row) > C_LSPIN else ""
        from_lwin  = from_row[C_LWIN]  if len(from_row) > C_LWIN  else ""
        upsert_user(ws_pts, users, from_user_id,
                    from_pts - 1, from_total, from_name, from_lspin, from_lwin)
        add_history(ws_hist, "贈与(送)", from_user_id, to_user_id, -1,
                    f"{from_name}→{to_name}")

    # ── 月予算チェック（公式のみ） ──
    if is_official:
        bud_row, winners, spins = get_month_data(ws_bud)
        if winners >= MAX_WINNERS:
            print(f"  → 月予算上限到達（{winners}/{MAX_WINNERS}名）")
            return None
        # 付与はするがルーレット当選枠は別管理なので予算チェックのみここで行う

    # ── 受け取り側に+1 ──
    _, to_row = get_user(users, to_user_id)
    to_pts   = _int(to_row[C_PTS] if len(to_row) > C_PTS else 0)
    to_total = _int(to_row[C_TOT] if len(to_row) > C_TOT else 0)
    to_lspin = to_row[C_LSPIN] if len(to_row) > C_LSPIN else ""
    to_lwin  = to_row[C_LWIN]  if len(to_row) > C_LWIN  else ""

    new_pts = to_pts + 1
    upsert_user(ws_pts, users, to_user_id,
                new_pts, to_total + 1, to_name, to_lspin, to_lwin)

    kind = "公式付与" if is_official else "贈与(受)"
    add_history(ws_hist, kind, from_user_id, to_user_id, 1,
                f"{from_name}→{to_name}")
    print(f"  → ポイント付与: {to_name} {to_pts}→{new_pts}pt")

    # ── DM通知 ──
    giver  = "岡さん" if is_official else from_name
    remain = POINTS_TO_SPIN - new_pts
    dm = (
        f"🎯 *1ポイントGetしました！*\n"
        f"付与者：{giver}\n"
        f"ただいまの合計：★ *{new_pts}ポイント* 🌟\n\n"
    )
    if new_pts >= POINTS_TO_SPIN:
        dm += f"🎰 *{POINTS_TO_SPIN}ポイント達成！* generalチャンネルをご確認ください 🎉"
    else:
        dm += f"ルーレットまであと *{remain}ポイント* です 💪"
    send_dm(nahooka_client, to_user_id, dm)

    # ── 10ポイント達成通知 ──
    if new_pts >= POINTS_TO_SPIN and to_pts < POINTS_TO_SPIN:
        notify_general_10pt(nahooka_client, to_user_id)

    return new_pts


# ── ルーレット処理 ────────────────────────────────

def process_roulette(client, nahooka_client, ss, user_id, thread_ts):
    """ルーレットを実行する"""
    ws_pts  = ss.worksheet(SHEET_POINTS)
    ws_hist = ss.worksheet(SHEET_HISTORY)
    ws_bud  = ss.worksheet(SHEET_BUDGET)

    user_name = get_user_name(client, user_id)
    users     = load_users(ws_pts)
    _, row    = get_user(users, user_id)
    cur_pts   = _int(row[C_PTS] if len(row) > C_PTS else 0)
    lspin     = row[C_LSPIN] if len(row) > C_LSPIN else ""
    lwin      = row[C_LWIN]  if len(row) > C_LWIN  else ""

    # ── ポイント確認 ──
    if cur_pts < POINTS_TO_SPIN:
        try:
            nahooka_client.chat_postMessage(
                channel=GENERAL_CHANNEL,
                text=(f"😢 <@{user_id}> さん、ポイントが不足しています。\n"
                      f"現在：*{cur_pts}pt* / 必要：*{POINTS_TO_SPIN}pt*"),
                thread_ts=thread_ts,
            )
        except SlackApiError:
            pass
        return

    # ── 今月すでにスピン済みか確認 ──
    cm = current_month()
    if lspin == cm:
        try:
            nahooka_client.chat_postMessage(
                channel=GENERAL_CHANNEL,
                text=f"😊 <@{user_id}> さん、今月はすでにルーレットを使用済みです。\n来月またチャレンジしてください 🌸",
                thread_ts=thread_ts,
            )
        except SlackApiError:
            pass
        return

    # ── 月データ取得 ──
    bud_row, winners, spins = get_month_data(ws_bud)

    # ── 当選確率を計算 ──
    remaining = MAX_WINNERS - winners
    win_prob  = calc_win_prob(remaining, spins, lspin, lwin)
    won       = random.random() < win_prob

    # ── ポイント消費・月データ更新 ──
    new_pts  = cur_pts - POINTS_TO_SPIN
    new_lwin = cm if won else lwin
    to_total = _int(row[C_TOT] if len(row) > C_TOT else 0)
    upsert_user(ws_pts, users, user_id,
                new_pts, to_total, user_name, cm, new_lwin)

    new_winners = winners + (1 if won else 0)
    update_month_data(ws_bud, bud_row, new_winners, spins + 1)

    kind_str = f"ルーレット{'当選' if won else 'ハズレ'}"
    add_history(ws_hist, kind_str, user_id, user_id,
                -POINTS_TO_SPIN, f"{user_name} / 確率:{win_prob:.0%}")

    print(f"  → ルーレット: {user_name} / 確率:{win_prob:.0%} / 結果:{'当選' if won else 'ハズレ'}")

    # ── ダーツページURLを生成 ──
    import urllib.parse
    result_param = "win" if won else "lose"
    name_enc     = urllib.parse.quote(user_name)
    prize_enc    = urllib.parse.quote(PRIZE_NAME)
    dart_url     = f"{DART_PAGE_URL}?result={result_param}&name={name_enc}&prize={prize_enc}"

    # ── Slackに投稿 ──
    if won:
        msg = (
            f"🎯 <@{user_id}> さん、準備ができました！\n"
            f"下のリンクからダーツを投げてみよう 🎰✨\n"
            f"{dart_url}\n\n"
            f"残りポイント：★ *{new_pts}pt*"
        )
    else:
        msg = (
            f"🎯 <@{user_id}> さん、準備ができました！\n"
            f"下のリンクからダーツを投げてみよう 🍀\n"
            f"{dart_url}\n\n"
            f"残りポイント：★ *{new_pts}pt*"
        )

    try:
        nahooka_client.chat_postMessage(
            channel=GENERAL_CHANNEL,
            text=msg,
            thread_ts=thread_ts,
        )
    except SlackApiError as e:
        print(f"  → ルーレット結果投稿失敗: {e.response.get('error')}")

    # 当選の場合はDMにも通知
    if won:
        send_dm(nahooka_client, user_id,
                f"🎊 *おめでとうございます！*\n"
                f"{PRIZE_NAME} に当選しました！\n"
                f"岡さんへご連絡ください 🙌")
        # 岡さんにも当選者を通知
        send_dm(nahooka_client, OKA_USER_ID,
                f"🎯 *ルーレット当選のお知らせ*\n"
                f"{user_name} さんが {PRIZE_NAME} に当選しました！\n"
                f"Amazonギフト券の送付をお願いします 🎁")


# ── リアクション取得 ──────────────────────────────

def get_bot_channels(client):
    """botが参加している全チャンネルのIDリストを返す"""
    channels = []
    cursor   = None
    while True:
        kwargs = {"exclude_archived": True, "types": "public_channel,private_channel", "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            res = client.conversations_list(**kwargs)
        except SlackApiError as e:
            print(f"  → チャンネル一覧取得失敗: {e.response.get('error')}")
            break
        for ch in res.get("channels", []):
            if ch.get("is_member"):
                channels.append(ch["id"])
        cursor = res.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return channels


def fetch_oka_dart_replies(client, processed_keys, after_ts):
    """
    岡さんがスレッド返信に🎯テキストを書いた投稿を検知する
    → 親メッセージの投稿者にポイントを付与
    """
    results  = []
    channels = get_bot_channels(client)
    print(f"  → {len(channels)} チャンネルをスキャン")

    for ch in channels:
        try:
            res  = client.conversations_history(channel=ch, limit=200, oldest=after_ts)
            msgs = res.get("messages", [])
        except SlackApiError:
            continue

        for msg in msgs:
            parent_ts   = msg.get("ts", "")
            parent_user = msg.get("user", "")
            if not parent_user or parent_user == OKA_USER_ID:
                continue
            if int(msg.get("reply_count", 0)) == 0:
                continue

            try:
                rep = client.conversations_replies(
                    channel=ch, ts=parent_ts, oldest=after_ts, limit=100)
                replies = rep.get("messages", [])
            except SlackApiError:
                continue

            for reply in replies:
                if reply.get("ts") == parent_ts:
                    continue
                if reply.get("user") != OKA_USER_ID:
                    continue
                dart_count = reply.get("text", "").count(DART_TEXT)
                if dart_count == 0:
                    continue
                for i in range(dart_count):
                    key = f"oka_{ch}_{reply['ts']}_{i}"
                    if key in processed_keys:
                        continue
                    results.append({
                        "to_user":     parent_user,
                        "from_user":   OKA_USER_ID,
                        "is_official": True,
                        "key":         key,
                    })

    return results


def fetch_staff_dart_mentions(client, processed_keys, after_ts):
    """
    スタッフが「🎯 @相手」と投稿してポイントを贈与する
    例: 「🎯 <@U123> ありがとう！」→ 送り主-1pt / @相手+1pt
    チャンネル通常投稿・スレッド返信どちらも対象
    """
    import re
    results  = []
    channels = get_bot_channels(client)

    def _check_message(msg, ch):
        sender = msg.get("user", "")
        if not sender or sender == OKA_USER_ID:
            return
        text = msg.get("text", "")
        dart_count = text.count(DART_TEXT)
        if dart_count == 0:
            return
        ts = msg.get("ts", "")
        for mentioned in re.findall(r'<@([A-Z0-9]+)>', text):
            if mentioned == sender:
                continue
            for i in range(dart_count):
                key = f"gift_{ch}_{ts}_{mentioned}_{i}"
                if key in processed_keys:
                    continue
                results.append({
                    "to_user":     mentioned,
                    "from_user":   sender,
                    "is_official": False,
                    "key":         key,
                })

    for ch in channels:
        try:
            res  = client.conversations_history(channel=ch, limit=200, oldest=after_ts)
            msgs = res.get("messages", [])
        except SlackApiError:
            continue

        for msg in msgs:
            _check_message(msg, ch)
            # スレッド返信もチェック
            if int(msg.get("reply_count", 0)) > 0:
                try:
                    rep = client.conversations_replies(
                        channel=ch, ts=msg["ts"], oldest=after_ts, limit=100)
                    for reply in rep.get("messages", []):
                        if reply.get("ts") == msg["ts"]:
                            continue
                        _check_message(reply, ch)
                except SlackApiError:
                    pass

    return results


def fetch_roulette_requests(client, after_ts):
    """generalチャンネルのスレッドから【ルーレット】を検知"""
    requests = []
    try:
        res  = client.conversations_history(channel=GENERAL_CHANNEL, limit=50, oldest=after_ts)
        msgs = res.get("messages", [])
        for msg in msgs:
            parent_ts = msg.get("ts", "")
            if int(msg.get("reply_count", 0)) == 0:
                continue
            rep = client.conversations_replies(
                channel=GENERAL_CHANNEL, ts=parent_ts, oldest=after_ts)
            for reply in rep.get("messages", []):
                if reply.get("ts") == parent_ts:
                    continue
                if ROULETTE_KEYWORD in reply.get("text", ""):
                    requests.append({
                        "user_id":   reply.get("user", ""),
                        "thread_ts": parent_ts,
                    })
    except SlackApiError as e:
        print(f"  → ルーレット検知失敗: {e.response.get('error')}")
    return requests


# ── 【アポ】自動🎯返信 ───────────────────────────

def fetch_and_reply_apo(client, nahooka_client, processed_keys, after_ts):
    """【アポ で始まる投稿を検知してnahookaが🎯返信する
    戻り値: [{"key": ..., "sender": ..., "dart_count": ...}, ...]
    """
    channels = get_bot_channels(client)
    results  = []
    for ch in channels:
        try:
            res  = client.conversations_history(channel=ch, limit=200, oldest=after_ts)
            msgs = res.get("messages", [])
        except SlackApiError as e:
            print(f"  → 履歴取得失敗({ch}): {e.response.get('error')}")
            continue
        for msg in msgs:
            text   = msg.get("text", "")
            sender = msg.get("user", "")
            ts     = msg.get("ts", "")
            # 先頭のSlackメンション（<@U...> <!subteam^...> など）を取り除いてからチェック
            stripped = re.sub(r'^(<[@!][^>]+>\s*)+', '', text).strip()
            if not stripped.startswith("【アポ"):
                continue
            if not sender or sender == OKA_USER_ID:
                continue
            key = f"apo_{ch}_{ts}"
            if key in processed_keys:
                continue
            dart_count = 2 if ch in APO_DOUBLE_CHANNELS else 1
            darts      = DART_TEXT * dart_count
            reply_text = f"<@{sender}> ありがとうございます{darts}"
            try:
                nahooka_client.chat_postMessage(
                    channel=ch,
                    text=reply_text,
                    thread_ts=ts,
                )
                print(f"  → 【アポ】返信: {ch} / {dart_count}pt / {sender}")
                results.append({"key": key, "sender": sender, "dart_count": dart_count})
            except SlackApiError as e:
                print(f"  → 【アポ】返信失敗({ch}): {e.response.get('error')}")
    return results


# ── タイムスタンプ管理 ────────────────────────────

def load_last_ts():
    if os.path.exists(LAST_TS_FILE):
        with open(LAST_TS_FILE, "r") as f:
            return f.read().strip()
    return None

def save_last_ts(ts):
    with open(LAST_TS_FILE, "w") as f:
        f.write(str(ts))


# ── メイン ──────────────────────────────────────

def main():
    if not SLACK_BOT_TOKEN:
        print("エラー: SLACK_BOT_TOKEN が未設定です")
        return
    if not NAHOOKA_BOT_TOKEN:
        print("エラー: NAHOOKA_BOT_TOKEN が未設定です")
        return
    if not SPREADSHEET_ID:
        print("エラー: DART_POINT_SPREADSHEET_ID が未設定です")
        return

    client         = WebClient(token=SLACK_BOT_TOKEN)   # 既存bot（チャンネル監視用）
    nahooka_client = WebClient(token=NAHOOKA_BOT_TOKEN) # nahooka bot（通知・DM送信用）

    print("スプレッドシートを開いています...")
    try:
        ss = open_spreadsheet()
    except Exception as e:
        print(f"スプレッドシートエラー: {e}")
        return

    fetch_ts = now_ts()
    last_ts  = load_last_ts()
    if not last_ts:
        print("前回の記録が見つからないため、これ以降のみを対象にします")
        save_last_ts(fetch_ts)
        return

    # ── 処理済みキーを読み込む ──
    try:
        ws_proc = ss.worksheet(SHEET_PROCESSED)
    except Exception as e:
        print(f"処理済みシートエラー: {e}")
        return
    processed_keys = load_processed_keys(ws_proc)

    new_keys = []

    # ── 岡さんの🎯返信コメント処理 ──
    print("岡さんの🎯返信コメントを確認中...")
    oka_replies = fetch_oka_dart_replies(client, processed_keys, last_ts)
    print(f"  → {len(oka_replies)} 件（未処理）")
    for r in oka_replies:
        process_point(client, nahooka_client, ss, r["from_user"], r["to_user"], is_official=True)
        new_keys.append(r["key"])

    # ── スタッフ間🎯メンション贈与処理 ──
    print("スタッフ間の🎯メンション贈与を確認中...")
    staff_gifts = fetch_staff_dart_mentions(client, processed_keys, last_ts)
    print(f"  → {len(staff_gifts)} 件（未処理）")
    for r in staff_gifts:
        process_point(client, nahooka_client, ss, r["from_user"], r["to_user"], is_official=False)
        new_keys.append(r["key"])

    # ── 【アポ】自動🎯返信 + ポイント付与 ──
    print("【アポ】投稿を確認中...")
    apo_results = fetch_and_reply_apo(client, nahooka_client, processed_keys, last_ts)
    for r in apo_results:
        for _ in range(r["dart_count"]):
            process_point(client, nahooka_client, ss, OKA_USER_ID, r["sender"], is_official=True)
        new_keys.append(r["key"])

    # ── 処理済みキーを保存 ──
    if new_keys:
        add_processed_keys(ws_proc, new_keys)

    # ── ルーレット処理 ──
    print("【ルーレット】依頼を確認中...")
    roulette_reqs = fetch_roulette_requests(client, last_ts)
    print(f"  → {len(roulette_reqs)} 件")
    for req in roulette_reqs:
        if req["user_id"]:
            process_roulette(client, nahooka_client, ss, req["user_id"], req["thread_ts"])

    save_last_ts(fetch_ts)
    print("完了！")


if __name__ == "__main__":
    main()
