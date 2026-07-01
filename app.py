#!/usr/bin/env python3
"""
NewsBrief — лендинг новостей с эмоциональной разметкой и избранным.
Готов к деплою на Railway / Render / любой Python-хостинг.

Запуск: python3 app.py
Переменные окружения:
  DEEPSEEK_API_KEY — ключ API DeepSeek (опционально, без него эмоции не размечаются)
  PORT             — порт (по умолчанию 8080)
"""

import http.server
import urllib.request, urllib.error
import json, xml.etree.ElementTree as ET, os, sys, re, time
from datetime import datetime
from urllib.parse import urlparse

# ── Конфигурация ──────────────────────────────────────────────
RSS_URL = "https://www.vedomosti.ru/rss/news"
MAX_ITEMS = 10
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
PORT = int(os.environ.get("PORT", 8080))

CACHED_RESULT, CACHE_TIME, CACHE_TTL = None, 0, 300

# ── База данных (SQLite) ──────────────────────────────────────
import sqlite3, threading

SQLITE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favorites.db")
_local = threading.local()

def _db():
    if not hasattr(_local, 'conn') or _local.conn is None:
        _local.conn = sqlite3.connect(SQLITE_DB)
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
            link TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            descr TEXT,
            category TEXT,
            pub_date TEXT,
            enclosure_url TEXT,
            emotion_label TEXT,
            emotion_strength INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

def get_favorite_links():
    rows = db_execute("SELECT link FROM favorites")
    return {r[0] for r in rows} if rows else set()

def add_favorite(link, title, descr, category, pub_date, enclosure_url, emotion_label, emotion_strength):
    db_execute("""
        INSERT OR IGNORE INTO favorites (link, title, descr, category, pub_date, enclosure_url, emotion_label, emotion_strength)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (link, title, descr, category, pub_date, enclosure_url, emotion_label, emotion_strength))

def remove_favorite(link):
    db_execute("DELETE FROM favorites WHERE link = ?", (link,))

def get_all_favorites():
    rows = db_execute("""
        SELECT link, title, descr, category, pub_date, enclosure_url, emotion_label, emotion_strength, added_at
        FROM favorites ORDER BY added_at DESC
    """)
    return [{"link": r[0], "title": r[1], "desc": r[2], "category": r[3],
             "pubDate": r[4], "enclosureUrl": r[5], "emotion_label": r[6],
             "emotion_strength": r[7], "added_at": r[8] if r[8] else None,
             "is_favorited": True} for r in (rows or [])]

# ── RSS + эмоции ──────────────────────────────────────────────

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

def get_articles():
    global CACHED_RESULT, CACHE_TIME
    now = time.time()
    if CACHED_RESULT and (now - CACHE_TIME) < CACHE_TTL:
        return CACHED_RESULT
    articles = analyze_emotions(fetch_rss())
    fav = get_favorite_links()
    for a in articles:
        a["is_favorited"] = a["link"] in fav
    CACHED_RESULT, CACHE_TIME = articles, now
    return articles

# ── HTTP сервер ───────────────────────────────────────────────

INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

class AppHandler(http.server.BaseHTTPRequestHandler):
    def _cors(self, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if status != 204: self.end_headers()

    def _json(self, data, status=200):
        self._cors(status)
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _static(self, path):
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(INDEX_HTML, "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self._cors(204)
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/news":
            try:
                self._json(get_articles())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif path == "/api/favorites":
            try:
                self._json(get_all_favorites())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._static(path)

    def _clear_cache(self):
        global CACHED_RESULT
        CACHED_RESULT = None

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length)) if length > 0 else {}
        if path == "/api/favorite":
            try:
                link = data.get("link", "")
                article = next((a for a in (CACHED_RESULT or []) if a["link"] == link), None)
                if not article:
                    return self._json({"error": "Статья не найдена"}, 404)
                add_favorite(article["link"], article["title"], article["desc"],
                             article["category"], article["pubDate"], article["enclosureUrl"],
                             article.get("emotion_label", "—"), article.get("emotion_strength", 0))
                self._clear_cache()
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length)) if length > 0 else {}
        if path == "/api/favorite":
            try:
                remove_favorite(data.get("link", ""))
                self._clear_cache()
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        pass

if __name__ == "__main__":
    init_db()
    if not DEEPSEEK_API_KEY:
        print("[app] DEEPSEEK_API_KEY не задан — эмоции не размечаются", file=sys.stderr)
    print(f"[app] NewsBrief запущен на http://0.0.0.0:{PORT}", file=sys.stderr)
    http.server.HTTPServer(("0.0.0.0", PORT), AppHandler).serve_forever()
