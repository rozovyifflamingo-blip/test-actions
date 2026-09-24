"""
Эмулятор форума AntiO — ТОЛЬКО форум, ничего больше.

Идея: scrape_topic.py не меняется по сути (только BASE берётся из
переменной окружения FORUM_BASE_URL) и ходит СЮДА вместо антио.
Этот сервер отдаёт index.php?showtopic=...&st=... в ТОЧНО той разметке,
которую парсит parse_page() в scrape_topic.py — те же селекторы:
  div.post_block[id^="post_id_"]
  span[itemprop="creator name"]
  abbr.published (атрибут title = дата поста)
  div[itemprop="commentText"]

Вы пишете посты от разных ников через простую форму на "/".
На каждый новый пост сервер СРАЗУ (без ожидания 5 минут) запускает
настоящий scrape_topic.py подпроцессом, с cwd в test_run/ — чтобы
не трогать боевые state.json / actions.json / topic.html в корне репо.

Запуск:
    python3 forum_emulator.py [порт]   # по умолчанию порт 8000

Дальше:
  http://127.0.0.1:8000/            — форма постинга + лог последнего запуска скрипта
  http://127.0.0.1:8000/battle2.html — визуализация боя (берёт actions.json из test_run/)
  http://127.0.0.1:8000/index.php?showtopic=1&st=0 — как выглядит "тема форума" изнутри
"""
import html
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_RUN_DIR = os.path.join(ROOT_DIR, "test_run")
POSTS_FILE = os.path.join(TEST_RUN_DIR, "emulator_posts.json")
SCRAPE_SCRIPT = os.path.join(ROOT_DIR, "scrape_topic.py")

PAGE_SIZE = 20
TZ = timezone(timedelta(hours=3))
TOPIC_TITLE = "Тестовая тема (эмулятор)"

lock = threading.Lock()
LAST_LOG = "(скрипт ещё не запускался)"


# ── хранилище постов ──
def load_posts():
    try:
        with open(POSTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_posts(posts):
    os.makedirs(TEST_RUN_DIR, exist_ok=True)
    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)


def build_ts(date_str, time_str):
    """Собирает штамп поста "DD.MM.YYYY - HH:MM" из полей формы/JSON.
    Пустая дата → штамп "сейчас" (как раньше). Указанная дата обязана
    быть в формате ДД.ММ.ГГГГ (год из 4 цифр, чтобы не путать с форматом
    команд "+атака 21/09" внутри текста поста). Возвращает (ts, error)."""
    date_str = (date_str or "").strip()
    time_str = (time_str or "").strip()

    if not date_str:
        return datetime.now(TZ).strftime("%d.%m.%Y - %H:%M"), None

    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", date_str)
    if not m:
        return None, f'Дата поста "{date_str}" не в формате ДД.ММ.ГГГГ (например 21.09.2026)'
    day, month, year = (int(x) for x in m.groups())

    if not time_str:
        time_str = "12:00"
    tm = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if not tm:
        return None, f'Время поста "{time_str}" не в формате ЧЧ:ММ (например 14:30)'
    hour, minute = (int(x) for x in tm.groups())

    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=TZ)
    except ValueError:
        return None, f'Дата/время поста "{date_str} {time_str}" не существует в календаре'

    return dt.strftime("%d.%m.%Y - %H:%M"), None


def add_post(author, text, date_str="", time_str=""):
    ts, error = build_ts(date_str, time_str)
    if error:
        return load_posts(), error
    posts = load_posts()
    new_id = (posts[-1]["id"] + 1) if posts else 1
    posts.append({"id": new_id, "author": author.strip() or "Аноним", "text": text, "ts": ts})
    save_posts(posts)
    return posts, None


# ── запуск настоящего scrape_topic.py ──
def run_scrape(port):
    os.makedirs(TEST_RUN_DIR, exist_ok=True)
    env = dict(os.environ)
    env["FORUM_BASE_URL"] = f"http://127.0.0.1:{port}/"
    env["TOPIC_ID"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, SCRAPE_SCRIPT],
            cwd=TEST_RUN_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
    except Exception as e:
        out = f"Ошибка запуска scrape_topic.py: {e}"
    return out


# ── HTML: страница форума, как её видит parse_page() ──
def render_forum_page(posts_slice):
    parts = [
        f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        f'<title>{html.escape(TOPIC_TITLE)} - АнтиО</title></head><body>'
        f'<h1>{html.escape(TOPIC_TITLE)}</h1>'
    ]
    for p in posts_slice:
        body_html = html.escape(p["text"]).replace("\n", "<br>\n")
        parts.append(
            f'<div class="post_block" id="post_id_{p["id"]}">'
            f'<div class="postprofile"><span itemprop="creator name">{html.escape(p["author"])}</span></div>'
            f'<div class="postbody">'
            f'<abbr class="published" title="{html.escape(p["ts"])}">{html.escape(p["ts"])}</abbr>'
            f'<div itemprop="commentText">{body_html}</div>'
            f'</div></div>\n'
        )
    parts.append("</body></html>")
    return "".join(parts)


# ── HTML: страница управления эмулятором ──
def render_admin_page(error=None):
    posts = load_posts()
    rows = "".join(
        f'<div class="p"><b>{html.escape(p["author"])}</b> '
        f'<span class="t">#{p["id"]} · {html.escape(p["ts"])}</span>'
        f'<div class="txt">{html.escape(p["text"])}</div></div>'
        for p in reversed(posts)
    ) or '<p class="muted">постов пока нет</p>'

    admin_hint = f"Админ темы = автор поста №1 ({html.escape(posts[0]['author'])})" if posts else \
        "Админ темы = автор первого поста, который вы напишете"

    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Эмулятор форума</title>
<style>
body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#14120f;color:#e8dcc0;margin:0;padding:24px;}}
.wrap{{max-width:760px;margin:0 auto}}
h1{{font-size:18px;color:#e5c158}}
.hint{{color:#9a8f78;font-size:13px;margin-bottom:16px}}
label{{display:block;font-size:12px;color:#9a8f78;margin:10px 0 3px;text-transform:uppercase}}
input[type=text],textarea{{width:100%;box-sizing:border-box;background:#0f0d0a;color:#e8dcc0;border:1px solid #4a3f2a;border-radius:4px;padding:8px 10px;font:inherit}}
textarea{{min-height:90px;resize:vertical}}
button{{margin-top:12px;padding:10px 16px;border:none;border-radius:4px;background:#e5c158;color:#1c1912;font-weight:600;cursor:pointer}}
.log{{background:#0f0d0a;border:1px solid #3a3122;border-radius:4px;padding:10px;white-space:pre-wrap;font:12px/1.5 monospace;color:#cfc3a5;max-height:220px;overflow:auto}}
.p{{background:#0f0d0a;border:1px solid #3a3122;border-radius:4px;padding:8px 10px;margin-bottom:6px}}
.p .t{{color:#9a8f78;font-size:11px}}
.p .txt{{white-space:pre-wrap;margin-top:4px;color:#cfc3a5}}
.muted{{color:#5a5142}}
a{{color:#7fb3e0}}
.links{{margin-top:16px;font-size:13px}}
form{{border-bottom:1px solid #4a3f2a;padding-bottom:16px;margin-bottom:16px}}
.row{{display:flex;gap:10px}}
.row > div{{flex:1}}
.err{{background:#3a1414;border:1px solid #7a2c2c;color:#f0b8b8;border-radius:4px;padding:8px 10px;margin-bottom:14px;font-size:13px}}
</style></head><body><div class="wrap">
<h1>Эмулятор форума АнтиО</h1>
<div class="hint">{admin_hint}. Каждый новый пост сразу прогоняется через настоящий scrape_topic.py (cwd = test_run/, боевые файлы в корне репо не трогаются).</div>
{f'<div class="err">⚠ {html.escape(error)}</div>' if error else ''}

<form method="post" action="/post">
  <label>Автор поста</label>
  <input type="text" name="author" placeholder="Например: Ева" required>
  <div class="row">
    <div>
      <label>Дата поста (необязательно)</label>
      <input type="text" name="date" placeholder="ДД.ММ.ГГГГ, например 21.09.2026">
    </div>
    <div>
      <label>Время поста (необязательно)</label>
      <input type="text" name="time" placeholder="ЧЧ:ММ, например 14:30">
    </div>
  </div>
  <div class="hint" style="margin:6px 0 0">Пусто = штамп поста ставится "сейчас". Дата задаёт время самого поста на форуме (то, что читает scrape_topic.py), а НЕ дату команды внутри текста (её по-прежнему пишете справа от команды, например "+атака 21/09").</div>
  <label>Текст поста</label>
  <textarea name="text" placeholder="+атака 21/09&#10;или !срыв&#10;или !присоединяюсь"></textarea>
  <button type="submit">Опубликовать пост</button>
</form>

<h3>Лог последнего запуска scrape_topic.py</h3>
<div class="log">{html.escape(LAST_LOG)}</div>

<div class="links">
  <a href="/battle2.html" target="_blank">Открыть battle2.html (визуализация боя)</a> ·
  <a href="/index.php?showtopic=1&st=0" target="_blank">Как видит тему сам скрипт</a> ·
  <a href="/reset" onclick="return confirm('Стереть все тестовые посты и результаты?')">Сбросить всё</a>
</div>

<h3>Посты в теме ({len(posts)})</h3>
{rows}
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # тише, не спамить консоль стандартными access-логами

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, rel_path):
        for base in (TEST_RUN_DIR, ROOT_DIR):
            fp = os.path.join(base, rel_path)
            if os.path.isfile(fp):
                ctype = "text/html; charset=utf-8"
                if fp.endswith(".json"):
                    ctype = "application/json"
                elif fp.endswith(".png"):
                    ctype = "image/png"
                elif fp.endswith(".otf"):
                    ctype = "font/otf"
                with open(fp, "rb") as f:
                    self._send(200, f.read(), ctype)
                return True
        return False

    def do_GET(self):
        global LAST_LOG
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send(200, render_admin_page())
            return

        if path == "/index.php":
            qs = parse_qs(parsed.query)
            st = int(qs.get("st", ["0"])[0] or 0)
            posts = load_posts()
            self._send(200, render_forum_page(posts[st:st + PAGE_SIZE]))
            return

        if path == "/reset":
            with lock:
                save_posts([])
                for fn in ("state.json", "actions.json", "topic.html"):
                    fp = os.path.join(TEST_RUN_DIR, fn)
                    if os.path.exists(fp):
                        os.remove(fp)
                LAST_LOG = "(сброшено)"
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if self._serve_static(path.lstrip("/")):
            return

        self._send(404, "not found")

    def do_POST(self):
        global LAST_LOG
        if self.path != "/post":
            self._send(404, "not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)
        author = (form.get("author", [""])[0]).strip()
        text = (form.get("text", [""])[0])
        date_str = (form.get("date", [""])[0])
        time_str = (form.get("time", [""])[0])

        with lock:
            _, error = add_post(author, text, date_str, time_str)
            if error:
                self._send(400, render_admin_page(error=error))
                return
            port = self.server.server_address[1]
            LAST_LOG = run_scrape(port)

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    os.makedirs(TEST_RUN_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Эмулятор форума: http://127.0.0.1:{port}/")
    print(f"Боевые файлы в корне репо не трогаются, всё пишется в {TEST_RUN_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
