#!/usr/bin/env python3
"""
NewsBrief — лендинг новостей с эмоциональной разметкой, избранным и авторизацией.
Готов к деплою на Railway / Render / любой Python-хостинг.

Запуск: python3 app.py
Переменные окружения:
  DEEPSEEK_API_KEY — ключ API DeepSeek (опционально)
  PORT             — порт (по умолчанию 8080)
  JWT_SECRET       — секрет для JWT (по умолчанию генерируется)
"""

import http.server
import urllib.request, urllib.error
import json, xml.etree.ElementTree as ET, os, sys, re, time, hashlib, hmac, base64, secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Union
from urllib.parse import urlparse, parse_qs

# ── Конфигурация ──────────────────────────────────────────────
RSS_URL = "https://www.vedomosti.ru/rss/news"
MAX_ITEMS = 10
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
PORT = int(os.environ.get("PORT", 8080))
JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))
ACCESS_TTL = 15 * 60          # 15 минут
REFRESH_TTL = 30 * 24 * 3600  # 30 дней

# Яндекс OAuth
YANDEX_CLIENT_ID = os.environ.get("YANDEX_CLIENT_ID", "")
YANDEX_CLIENT_SECRET = os.environ.get("YANDEX_CLIENT_SECRET", "")
YANDEX_REDIRECT_URI = os.environ.get("YANDEX_REDIRECT_URI", f"http://localhost:{PORT}/api/auth/yandex/callback")

CACHED_RESULT, CACHE_TIME, CACHE_TTL = None, 0, 300

# ── База данных (SQLite) ──────────────────────────────────────
import sqlite3, threading

SQLITE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favorites.db")
_local = threading.local()

def _db():
    if not hasattr(_local, 'conn') or _local.conn is None:
        _local.conn = sqlite3.connect(SQLITE_DB)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn

def db_execute(sql, params=None):
    conn = _db()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    if sql.strip().upper().startswith("SELECT"):
        return cur.fetchall()
    conn.commit()

def init_db():
    db_execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            link TEXT NOT NULL,
            title TEXT NOT NULL,
            descr TEXT,
            category TEXT,
            pub_date TEXT,
            enclosure_url TEXT,
            emotion_label TEXT,
            emotion_strength INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, link)
        )
    """)
    db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            password_hash TEXT,
            yandex_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Миграции
    try:
        db_execute("ALTER TABLE users ADD COLUMN yandex_id TEXT UNIQUE")
    except sqlite3.OperationalError:
        pass
    # Миграция: добавляем user_id если таблица уже существовала без него
    try:
        db_execute("ALTER TABLE favorites ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # колонка уже есть

# ── JWT helpers (stdlib only) ───────────────────────────────────

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def _b64url_decode(s: str) -> bytes:
    s += '=' * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)

def encode_jwt(payload: dict, secret: str = JWT_SECRET) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url_encode(json.dumps(header, separators=(',', ':')).encode())
    p = _b64url_encode(json.dumps(payload, separators=(',', ':')).encode())
    sig_input = f"{h}.{p}".encode()
    sig = _b64url_encode(hmac.new(secret.encode(), sig_input, hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"

def decode_jwt(token: str, secret: str = JWT_SECRET) -> Optional[dict]:
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        h, p, sig = parts
        sig_input = f"{h}.{p}".encode()
        expected = _b64url_encode(hmac.new(secret.encode(), sig_input, hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64url_decode(p))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None

# ── Password hashing (pbkdf2) ───────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 200_000)
    return f"pbkdf2:sha256:200000:{salt}:{h.hex()}"

def verify_password(password: str, hashed: str) -> bool:
    try:
        _, algo, iterations, salt, stored = hashed.split(':')
        h = hashlib.pbkdf2_hmac(algo, password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(h.hex(), stored)
    except (ValueError, AttributeError):
        return False

# ── Auth helpers ────────────────────────────────────────────────

def create_tokens(user_id: int) -> Tuple[str, str]:
    now = int(time.time())
    access = encode_jwt({"sub": user_id, "exp": now + ACCESS_TTL, "type": "access"})
    refresh = encode_jwt({"sub": user_id, "exp": now + REFRESH_TTL, "type": "refresh"})
    return access, refresh

def set_auth_cookies(handler, access: str, refresh: str):
    handler.send_header("Set-Cookie",
        f"access_token={access}; HttpOnly; Path=/; Max-Age={ACCESS_TTL}; SameSite=Lax")
    handler.send_header("Set-Cookie",
        f"refresh_token={refresh}; HttpOnly; Path=/api/auth; Max-Age={REFRESH_TTL}; SameSite=Lax")

def clear_auth_cookies(handler):
    handler.send_header("Set-Cookie", "access_token=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax")
    handler.send_header("Set-Cookie", "refresh_token=; HttpOnly; Path=/api/auth; Max-Age=0; SameSite=Lax")

def get_user_from_request(handler) -> Optional[int]:
    """Извлекает user_id из HttpOnly cookie. Возвращает None если нет/невалиден."""
    cookie = handler.headers.get("Cookie", "")
    match = re.search(r'access_token=([^;]+)', cookie)
    if not match:
        return None
    payload = decode_jwt(match.group(1))
    if payload is None or payload.get("type") != "access":
        return None
    return payload.get("sub")

# ── User DB ─────────────────────────────────────────────────────

def create_user(email: str, password: str) -> Optional[dict]:
    email = email.strip().lower()
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return None
    if len(password) < 4:
        return None
    try:
        db_execute("INSERT INTO users (email, password_hash) VALUES (?, ?)",
                   (email, hash_password(password)))
        user = db_execute("SELECT id, email FROM users WHERE email = ?", (email,))
        return dict(user[0]) if user else None
    except sqlite3.IntegrityError:
        return None  # email already exists

def authenticate_user(email: str, password: str) -> Optional[dict]:
    email = email.strip().lower()
    user = db_execute("SELECT id, email, password_hash FROM users WHERE email = ?", (email,))
    if not user:
        return None
    u = dict(user[0])
    if verify_password(password, u["password_hash"]):
        return {"id": u["id"], "email": u["email"]}
    return None

# ── Яндекс OAuth ─────────────────────────────────────────────────

def get_yandex_token(code: str) -> Optional[dict]:
    """Обменивает authorization_code на access_token."""
    if not YANDEX_CLIENT_ID or not YANDEX_CLIENT_SECRET:
        return None
    try:
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": YANDEX_CLIENT_ID,
            "client_secret": YANDEX_CLIENT_SECRET,
        }).encode()
        req = urllib.request.Request("https://oauth.yandex.ru/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[app] Yandex token error: {e}", file=sys.stderr)
        return None

def get_yandex_user_info(access_token: str) -> Optional[dict]:
    """Получает id, login, email пользователя Яндекса."""
    try:
        req = urllib.request.Request("https://login.yandex.ru/info",
            headers={"Authorization": f"OAuth {access_token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[app] Yandex user info error: {e}", file=sys.stderr)
        return None

def find_or_create_yandex_user(yandex_info: dict) -> Optional[dict]:
    """Находит или создаёт пользователя по данным Яндекса."""
    yandex_id = str(yandex_info.get("id", ""))
    email = (yandex_info.get("default_email") or yandex_info.get("login") + "@yandex.ru").strip().lower()

    # Ищем по yandex_id
    user = db_execute("SELECT id, email FROM users WHERE yandex_id = ?", (yandex_id,))
    if user:
        return dict(user[0])

    # Ищем по email (связываем аккаунты)
    user = db_execute("SELECT id, email FROM users WHERE email = ?", (email,))
    if user:
        db_execute("UPDATE users SET yandex_id = ? WHERE id = ?", (yandex_id, user[0]["id"]))
        return dict(user[0])

    # Создаём нового
    try:
        db_execute("INSERT INTO users (email, yandex_id) VALUES (?, ?)", (email, yandex_id))
        user = db_execute("SELECT id, email FROM users WHERE yandex_id = ?", (yandex_id,))
        return dict(user[0]) if user else None
    except sqlite3.IntegrityError:
        return None

# ── Favorites (user-scoped) ─────────────────────────────────────

def get_favorite_links(user_id: Optional[int] = None):
    if user_id is None:
        rows = db_execute("SELECT link FROM favorites")
    else:
        rows = db_execute("SELECT link FROM favorites WHERE user_id = ?", (user_id,))
    return {r[0] for r in rows} if rows else set()

def add_favorite(link, title, descr, category, pub_date, enclosure_url, emotion_label, emotion_strength, user_id=0):
    db_execute("""
        INSERT OR IGNORE INTO favorites (user_id, link, title, descr, category, pub_date, enclosure_url, emotion_label, emotion_strength)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, link, title, descr, category, pub_date, enclosure_url, emotion_label, emotion_strength))

def remove_favorite(link, user_id=0):
    db_execute("DELETE FROM favorites WHERE link = ? AND user_id = ?", (link, user_id))

def get_all_favorites(user_id=0):
    rows = db_execute("""
        SELECT link, title, descr, category, pub_date, enclosure_url, emotion_label, emotion_strength, added_at
        FROM favorites WHERE user_id = ? ORDER BY added_at DESC
    """, (user_id,))
    return [{"link": r[0], "title": r[1], "desc": r[2], "category": r[3],
             "pubDate": r[4], "enclosureUrl": r[5], "emotion_label": r[6],
             "emotion_strength": r[7], "added_at": r[8] if r[8] else None,
             "is_favorited": True} for r in (rows or [])]

# ── RSS + эмоции (без изменений) ────────────────────────────────

def fetch_rss():
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        root = ET.fromstring(resp.read().decode("utf-8"))
    articles = []
    for item in root.findall(".//item")[:MAX_ITEMS]:
        enc = item.find("enclosure")
        articles.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "desc": (item.findtext("description") or "").strip(),
            "pubDate": (item.findtext("pubDate") or "").strip(),
            "category": (item.findtext("category") or "Рынки").strip(),
            "enclosureUrl": enc.get("url", "") if enc is not None else "",
        })
    return articles

def analyze_emotions(articles):
    if not DEEPSEEK_API_KEY:
        for a in articles: a["emotion_label"], a["emotion_strength"] = "—", 0
        return articles

    titles = "\n".join(f"{i+1}. {a['title']}" for i, a in enumerate(articles))
    prompt = f"""Проанализируй эмоциональную окраску каждого заголовка. Верни ТОЛЬКО JSON-массив:
[{{"emotion_label": "негатив", "emotion_strength": 7}}, ...]
 emotion_label: позитив, негатив, тревога, надежда, нейтрально, гнев, гордость, страх, удивление, интерес
 emotion_strength: 0–10

{titles}"""

    try:
        body = json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}],
                           "temperature": 0.3, "max_tokens": 500}).encode()
        req = urllib.request.Request(DEEPSEEK_API_URL, data=body, headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = json.loads(resp.read().decode())["choices"][0]["message"]["content"].strip()
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
        emotions = json.loads(content)
        for i, a in enumerate(articles):
            a["emotion_label"] = emotions[i].get("emotion_label", "нейтрально") if i < len(emotions) else "—"
            a["emotion_strength"] = int(emotions[i].get("emotion_strength", 0)) if i < len(emotions) else 0
    except Exception as e:
        print(f"[app] DeepSeek error: {e}", file=sys.stderr)
        for a in articles: a["emotion_label"], a["emotion_strength"] = "—", 0
    return articles

def get_articles(user_id: Optional[int] = None):
    global CACHED_RESULT, CACHE_TIME
    now = time.time()
    if CACHED_RESULT and (now - CACHE_TIME) < CACHE_TTL:
        articles = CACHED_RESULT
    else:
        articles = analyze_emotions(fetch_rss())
        CACHED_RESULT, CACHE_TIME = articles, now

    fav = get_favorite_links(user_id)
    result = []
    for a in articles:
        item = dict(a)
        item["is_favorited"] = item["link"] in fav
        result.append(item)
    return result

# ── HTTP сервер ───────────────────────────────────────────────

INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

class AppHandler(http.server.BaseHTTPRequestHandler):
    def _cors(self, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "http://localhost:8080")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if status != 204: self.end_headers()

    def _json(self, data, status=200):
        self._cors(status)
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _static(self, path):
        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(INDEX_HTML, "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length > 0 else {}

    def do_OPTIONS(self):
        self._cors(204)
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/news":
            try:
                user_id = get_user_from_request(self)
                self._json(get_articles(user_id))
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif path == "/api/favorites":
            try:
                user_id = get_user_from_request(self)
                if user_id is None:
                    self._json({"error": "Требуется авторизация"}, 401)
                else:
                    self._json(get_all_favorites(user_id))
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif path == "/api/auth/me":
            user_id = get_user_from_request(self)
            if user_id is None:
                self._json({"authenticated": False})
            else:
                user = db_execute("SELECT id, email, created_at FROM users WHERE id = ?", (user_id,))
                if user:
                    u = dict(user[0])
                    self._json({"authenticated": True, "user": {"id": u["id"], "email": u["email"]}})
                else:
                    self._json({"authenticated": False})
        elif path == "/api/auth/yandex/login":
            if not YANDEX_CLIENT_ID:
                self._json({"error": "Яндекс ID не настроен"}, 501)
            else:
                auth_url = (
                    "https://oauth.yandex.ru/authorize"
                    f"?response_type=code"
                    f"&client_id={YANDEX_CLIENT_ID}"
                    f"&redirect_uri={urllib.parse.quote(YANDEX_REDIRECT_URI, safe='')}"
                )
                self._json({"url": auth_url})
        elif path == "/api/auth/yandex/callback":
            query = parse_qs(urlparse(self.path).query)
            code = query.get("code", [None])[0]
            if not code:
                self.send_response(302)
                self.send_header("Location", "/?error=yandex_no_code")
                self.end_headers()
                return
            token_data = get_yandex_token(code)
            if not token_data or "access_token" not in token_data:
                self.send_response(302)
                self.send_header("Location", "/?error=yandex_token")
                self.end_headers()
                return
            user_info = get_yandex_user_info(token_data["access_token"])
            if not user_info or "id" not in user_info:
                self.send_response(302)
                self.send_header("Location", "/?error=yandex_userinfo")
                self.end_headers()
                return
            user = find_or_create_yandex_user(user_info)
            if user is None:
                self.send_response(302)
                self.send_header("Location", "/?error=yandex_create")
                self.end_headers()
                return
            access, refresh = create_tokens(user["id"])
            self.send_response(302)
            set_auth_cookies(self, access, refresh)
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self._static(path)

    # ── POST ───────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_body()

        # ── Auth: register ──────────────────────────────────
        if path == "/api/auth/register":
            email = data.get("email", "").strip()
            password = data.get("password", "")
            user = create_user(email, password)
            if user is None:
                self._json({"error": "Email уже занят или неверный формат"}, 400)
                return
            access, refresh = create_tokens(user["id"])
            self._cors(200)
            set_auth_cookies(self, access, refresh)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "user": user}, ensure_ascii=False).encode())

        # ── Auth: login ─────────────────────────────────────
        elif path == "/api/auth/login":
            email = data.get("email", "")
            password = data.get("password", "")
            user = authenticate_user(email, password)
            if user is None:
                self._json({"error": "Неверный email или пароль"}, 401)
                return
            access, refresh = create_tokens(user["id"])
            self._cors(200)
            set_auth_cookies(self, access, refresh)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "user": user}, ensure_ascii=False).encode())

        # ── Auth: logout ────────────────────────────────────
        elif path == "/api/auth/logout":
            self._cors(200)
            clear_auth_cookies(self)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode())

        # ── Auth: refresh ───────────────────────────────────
        elif path == "/api/auth/refresh":
            cookie = self.headers.get("Cookie", "")
            match = re.search(r'refresh_token=([^;]+)', cookie)
            if not match:
                self._json({"error": "Нет refresh токена"}, 401)
                return
            payload = decode_jwt(match.group(1))
            if payload is None or payload.get("type") != "refresh":
                self._json({"error": "Невалидный refresh токен"}, 401)
                return
            user_id = payload.get("sub")
            access, refresh = create_tokens(user_id)
            self._cors(200)
            set_auth_cookies(self, access, refresh)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode())

        # ── Favorite: add ──────────────────────────────────
        elif path == "/api/favorite":
            user_id = get_user_from_request(self)
            if user_id is None:
                self._json({"error": "Требуется авторизация"}, 401)
                return
            try:
                link = data.get("link", "")
                article = next((a for a in (CACHED_RESULT or []) if a["link"] == link), None)
                if not article:
                    self._json({"error": "Статья не найдена"}, 404)
                    return
                add_favorite(article["link"], article["title"], article["desc"],
                             article["category"], article["pubDate"], article["enclosureUrl"],
                             article.get("emotion_label", "—"), article.get("emotion_strength", 0),
                             user_id)
                self._clear_cache()
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        else:
            self._json({"error": "not found"}, 404)

    # ── DELETE ──────────────────────────────────────────────────
    def do_DELETE(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length)) if length > 0 else {}
        if path == "/api/favorite":
            user_id = get_user_from_request(self)
            if user_id is None:
                self._json({"error": "Требуется авторизация"}, 401)
                return
            try:
                remove_favorite(data.get("link", ""), user_id)
                self._clear_cache()
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def _clear_cache(self):
        global CACHED_RESULT
        CACHED_RESULT = None

    def log_message(self, fmt, *args):
        pass  # тихий режим

if __name__ == "__main__":
    init_db()
    if not DEEPSEEK_API_KEY:
        print("[app] DEEPSEEK_API_KEY не задан — эмоции не размечаются", file=sys.stderr)
    print(f"[app] NewsBrief запущен на http://0.0.0.0:{PORT}", file=sys.stderr)
    http.server.HTTPServer(("0.0.0.0", PORT), AppHandler).serve_forever()
