"""ローカルサーバーでOAuthコードを自動キャプチャしてリフレッシュトークンを取得する"""
import urllib.request
import urllib.parse
import json
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler

CLIENT_ID     = "940503495629-1de3g2an3h39k279rs4g5t29pulf8h6s.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REDIRECT_URI  = "http://localhost:8080"
SCOPE         = "https://www.googleapis.com/auth/gmail.modify"

auth_code = None

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write("認証完了！このタブを閉じてターミナルに戻ってください。".encode("utf-8"))

    def log_message(self, *args):
        pass  # ログ抑制

auth_url = (
    f"https://accounts.google.com/o/oauth2/auth"
    f"?client_id={CLIENT_ID}"
    f"&redirect_uri={REDIRECT_URI}"
    f"&response_type=code"
    f"&scope={SCOPE}"
    f"&access_type=offline"
    f"&prompt=consent"
)

print("ブラウザを開きます。okanaho.sango@gmail.com でログインして許可してください...")
webbrowser.open(auth_url)

server = HTTPServer(("localhost", 8080), Handler)
server.handle_request()

if not auth_code:
    print("コード取得失敗")
    exit(1)

print("コード取得完了。トークンを発行中...")

data = urllib.parse.urlencode({
    "code":          auth_code,
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "redirect_uri":  REDIRECT_URI,
    "grant_type":    "authorization_code",
}).encode()

req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
try:
    res   = urllib.request.urlopen(req)
    token = json.loads(res.read().decode())
    print("\n" + "="*60)
    print("✅ リフレッシュトークン取得成功！")
    print("="*60)
    print(f"GMAIL_REFRESH_TOKEN_SANGO_OKA={token['refresh_token']}")
    print("="*60)
    print("\nこの値をGitHub Secretsに追加してください。")
except Exception as e:
    err = e.read().decode() if hasattr(e, "read") else str(e)
    print(f"エラー: {err}")
