"""
Парсер темы форума AntiO.
Запускается по крону каждые 5 минут в GitHub Actions через прокси.

Команды в тексте поста:
  !старт          — открывает бой; до этой команды ничего из
                    перечисленного ниже не воспринимается вовсе
  +атака          — участник наносит удар
  !срыв           — на участника накладывается предмет «Урон −25 HP»
  !присоединяюсь  — автор поста добавляется в участники боя

!старт — разовый переключатель на всю тему (хранится в state.json,
не сбрасывается сам). Пока он не встретился ни разу, +атака/!срыв/
!присоединяюсь из ЛЮБЫХ постов, в т.ч. более ранних по позиции
внутри ТОГО ЖЕ поста, где !старт стоит позже по тексту, — просто
не создают событий: как будто игроки обсуждали механику, а не
играли. Это отсекает случайные вкрапления команд в обсуждении до
официального начала боя.

Дата команды берётся СПРАВА от слова (например «+атака 21/09»).
  - Валидная дата (год >= 2026) — событие записывается на неё.
  - Даты нет — событие пробует лечь на ВЧЕРА (если вчера у этого лица
    такой команды ещё не было). Если вчера уже было — событие не
    засчитывается (лог не допускает две одинаковые команды от одного
    лица в один день).
  - Если написанная дата уже занята той же командой у того же лица —
    событие тоже отбрасывается.
  - Дата не может быть РАНЬШЕ даты, когда это лицо (цель команды)
    присоединилось (!присоединяюсь) — раньше своего входа в бой
    действовать нельзя. Если лицо ещё не присоединялось вовсе —
    команда отбрасывается.
  - Дата не может быть ПОЗЖЕ даты того поста, в котором она написана —
    «сегодня» здесь берётся не с системных часов скрипта, а из
    собственного штампа времени поста на форуме (title у
    abbr.published), чтобы не зависеть от часов машины, на которой
    крутится крон.

Права на команду:
  - Обычный участник действует только от своего имени: даже если он
    напишет чужой ник, событие всё равно засчитывается на автора поста.
  - Админ — автор самого первого поста в теме. Он может отправлять
    команду за другого участника, написав его ник СЛЕВА от команды
    (ник должен совпадать с уже известным участником). Если ника нет —
    действие засчитывается на самого админа.

Пополняет actions.json (его читает battle2.html) и state.json
(внутреннее состояние: прогресс по страницам, известные участники,
админ). Плюс topic.html — читаемая выгрузка темы без картинок.
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

TOPIC_ID = os.environ.get("TOPIC_ID", "85035")
BASE = os.environ.get("FORUM_BASE_URL", "https://antio.ru/").rstrip("/") + "/"
IS_LOCAL_TEST = not BASE.startswith("https://antio.ru")
POSTS_PER_PAGE = 20
MAX_PAGES_PER_RUN = 50
DELAY_SEC = 2.0

STATE_FILE = "state.json"
ACTIONS_FILE = "actions.json"
TOPIC_FILE = "topic.html"

TZ = timezone(timedelta(hours=3))  # Москва
MIN_YEAR = 2026  # даты раньше этого года считаются некорректными
EXILE_INACTIVE_DAYS = 7  # неактивность 7+ дней подряд — изгнание

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.8",
}

PROXY_URL = os.environ.get("PROXY_URL", "").strip()
# запасной вариант — если вместо одной строки заведены старые 4 секрета
PROXY_HOST = os.environ.get("PROXY_HOST", "dc03.steelproxy.com")
PROXY_PORT = os.environ.get("PROXY_PORT", "3071")
PROXY_USER = os.environ.get("PROXY_USER", "5caDYcWX")
PROXY_PASS = os.environ.get("PROXY_PASS", "c8tV1Bt8")

# участники по умолчанию — пусто, известные имена собираются только
# из реальных команд с форума (!присоединяюсь / +атака / !срыв)
DEFAULT_PARTICIPANTS = []

# ── команды ──
COMMANDS = [
    ("attack", re.compile(r"\+\s*атак\w*", re.IGNORECASE)),
    ("sryv",   re.compile(r"!\s*срыв\w*", re.IGNORECASE)),
    ("join",   re.compile(r"!\s*присоедин\w*", re.IGNORECASE)),
    ("start",  re.compile(r"!\s*старт\w*", re.IGNORECASE)),
]
# игровые команды, которые вообще не воспринимаются до !старт —
# "start" в этот список не входит: это сам переключатель, а не то,
# что он включает/выключает
GATED_COMMANDS = {"attack", "sryv", "join"}
# удар и срыв — самостоятельные параметры: не могут стоять одновременно
# в один день у одного и того же лица (см. извлечение событий ниже)
OTHER_COMMAND = {"attack": "sryv", "sryv": "attack"}
# дата: 21/09, 21.09, 21-09, 21/09/25, 21.09.2025
DATE_RE = re.compile(r"(\d{1,2})[./\-](\d{1,2})(?:[./\-](\d{2,4}))?")

# пары кавычек, внутри которых команды не считаются: обычные, ёлочки,
# "типографские" открывающая/закрывающая. Ищем построчно (без \n внутри
# пары) — так одна забытая кавычка не "съест" остаток всего поста.
QUOTE_PAIRS = [
    re.compile(r'"[^"\n]*"'),
    re.compile(r"«[^»\n]*»"),
    re.compile(r"“[^”\n]*”"),
]


def quoted_spans(text):
    """Список (start, end) диапазонов текста внутри полных пар кавычек."""
    spans = []
    for rx in QUOTE_PAIRS:
        for m in rx.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def in_any_span(pos, spans):
    return any(s <= pos < e for s, e in spans)

DATE_LOOKAHEAD = 30   # символов справа от команды, где ищем дату
NAME_LOOKBEHIND = 40  # символов слева от команды, где ищем ник (для админа)
NAME_GAP_MAX = 12     # ник должен заканчиваться не дальше этого от команды


# ── json helpers ──
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── сеть ──
def build_proxies():
    if IS_LOCAL_TEST:
        # локальный форум-эмулятор — прокси не нужен и всё равно не
        # доведёт до localhost
        return None
    if PROXY_URL:
        # один секрет — можно как "http://user:pass@host:port",
        # так и просто "user:pass@host:port" (схему подставим сами)
        url = PROXY_URL
        if not re.match(r"^https?://", url):
            url = "http://" + url
        return {"http": url, "https": url}
    url = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
    return {"http": url, "https": url}


def fetch(session, url, proxies):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] GET {url}")
    resp = session.get(url, headers=HEADERS, proxies=proxies, timeout=30)
    print(f"    статус {resp.status_code}, {len(resp.text)} байт")
    if "Just a moment" in resp.text or "Один момент" in resp.text:
        print("    Cloudflare-проверка. Прерываю.")
        sys.exit(1)
    resp.raise_for_status()
    return resp.text


# ── разбор страницы ──
def parse_page(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    title_el = soup.select_one("h1, title")
    title = title_el.get_text(strip=True) if title_el else f"Тема {TOPIC_ID}"

    posts = []
    for block in soup.select('div.post_block[id^="post_id_"]'):
        post_id = block.get("id", "").replace("post_id_", "")

        # без логина имя лежит прямо во внешнем span
        author_el = block.select_one('span[itemprop="creator name"]')
        author = author_el.get_text(" ", strip=True) if author_el else "???"

        time_el = block.select_one("abbr.published")
        timestamp = time_el.get("title") if time_el else None
        time_text = time_el.get_text(strip=True) if time_el else ""

        text_el = block.select_one('div[itemprop="commentText"]')
        if text_el:
            for q in text_el.select("blockquote, .quote"):  # цитаты не считаем
                q.decompose()
            text = text_el.get_text("\n", strip=True)
        else:
            text = ""

        posts.append({
            "post_id": post_id,
            "author": author,
            "timestamp": timestamp,
            "time_text": time_text,
            "text": text,
        })
    return title, posts


def fetch_first_post_author(session, proxies):
    """Автор самого первого поста темы = админ."""
    url = f"{BASE}index.php?showtopic={TOPIC_ID}&st=0"
    page_html = fetch(session, url, proxies)
    _, posts = parse_page(page_html)
    return posts[0]["author"] if posts else None


# ── дата ──
def parse_post_datetime(post):
    """Дата/время САМОГО поста по его штампу с форума (title у
    abbr.published, формат "DD.MM.YYYY - HH:MM"), а не по системным
    часам скрипта. Возвращает None, если штамп отсутствует или не
    распознан — тогда вызывающий код обязан сам подставить безопасный
    fallback (см. main())."""
    raw = (post.get("timestamp") or post.get("time_text") or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d.%m.%Y - %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return None


def normalize_date(day, month, year, today):
    """YYYY-MM-DD или None, если дата некорректна / раньше MIN_YEAR."""
    try:
        day, month = int(day), int(month)
    except (TypeError, ValueError):
        return None
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None

    if year:
        year = int(year)
        if year < 100:
            year += 2000
    else:
        year = today.year

    try:
        d = datetime(year, month, day, tzinfo=TZ)
    except ValueError:
        return None

    # без года и дата ушла далеко в будущее — вероятно, имелся в виду
    # прошлый год (но не раньше MIN_YEAR)
    if d.date() > today.date() + timedelta(days=30):
        try:
            prev = datetime(year - 1, month, day, tzinfo=TZ)
        except ValueError:
            return None
        if prev.year >= MIN_YEAR:
            d = prev

    if d.year < MIN_YEAR:
        return None
    return d.strftime("%Y-%m-%d")


# ── поиск ника слева (только для админа) ──
def find_left_target(left_text, known_names):
    """Ищет ближайшее к концу строки известное имя, заканчивающееся не
    дальше NAME_GAP_MAX символов от места команды."""
    if not left_text or not known_names:
        return None
    best = None
    for name in sorted(known_names, key=len, reverse=True):
        for m in re.finditer(r"\b" + re.escape(name) + r"\b", left_text, re.IGNORECASE):
            gap = len(left_text) - m.end()
            if gap <= NAME_GAP_MAX and (best is None or m.end() > best[1]):
                best = (name, m.end())
    return best[0] if best else None


# ── разбор команд одного поста ──
def extract_events(post, today, admin_name, known_names, used_dates, join_dates, exiled, battle_state):
    """
    today: собственная дата ЭТОГО поста (см. parse_post_datetime) — не
    системные часы. Служит и дефолтным годом внутри normalize_date, и
    верхней границей для дат атак/срывов из этого поста.
    used_dates: dict {(target_lower, command): set(даты)} — обновляется
    на месте по мере обработки, чтобы дубликаты ловились и внутри
    одного прогона, не только между прогонами.
    join_dates: dict {name_lower: "YYYY-MM-DD"} — дата, начиная с
    которой лицо считается в бою (нижняя граница для атак/срывов).
    Обновляется на месте.
    exiled: set(name_lower) — кто изгнан на начало текущего прогона.
    Используется только для чтения (кроме сброса при повторном join —
    см. ниже), фактическое снятие изгнания делает unexile_active_names.
    battle_state: dict {"started": bool} — общий на весь прогон
    (и персистентный между прогонами через state.json) флаг "!старт
    уже был". Пока False, join/атака/срыв из ЛЮБОГО поста, включая
    более ранние по позиции внутри ЭТОГО ЖЕ поста, если !старт стоит
    после них в тексте, — не создают событий вовсе: чекер их просто
    не видит, как будто их не было. Обновляется на месте, как только
    внутри текущего поста встречается !старт.
    """
    events = []
    text = post["text"]
    author = post["author"]
    is_admin = bool(admin_name) and author.strip().lower() == admin_name.strip().lower()
    today_str = today.strftime("%Y-%m-%d")

    quoted = quoted_spans(text)
    hits_all = sorted(
        (m.start(), m.end(), command)
        for command, rx in COMMANDS for m in rx.finditer(text)
        if not in_any_span(m.start(), quoted)   # команда внутри "кавычек" — не считаем
    )

    for idx, (start, end, command) in enumerate(hits_all):
        prev_end = hits_all[idx - 1][1] if idx else 0
        next_start = hits_all[idx + 1][0] if idx + 1 < len(hits_all) else len(text)

        if command == "start":
            battle_state["started"] = True
            events.append({
                "id": f"{post['post_id']}:{command}:{start}",
                "post_id": post["post_id"],
                "pos": start,
                "author": author,
                "target": author,
                "command": "start",
                "date": today_str,
                "date_from_text": False,
                "is_admin_action": False,
                "context": text[max(0, start - 20):end + 20].replace("\n", " ").strip(),
                "post_time": post["timestamp"] or post["time_text"],
            })
            continue

        # ── до !старт бой считается не начатым: join/атака/срыв просто
        #    не воспринимаются, как будто их вообще не писали ──
        if command in GATED_COMMANDS and not battle_state["started"]:
            continue

        if command == "join":
            name_l = author.strip().lower()
            # первый join вообще, либо возвращение после изгнания —
            # в обоих случаях это НОВАЯ нижняя граница для атак
            if name_l not in join_dates or name_l in exiled:
                join_dates[name_l] = today_str
                exiled.discard(name_l)  # чтобы повторный join в этом же
                                         # прогоне не сдвигал границу снова
            events.append({
                "id": f"{post['post_id']}:{command}:{start}",
                "post_id": post["post_id"],
                "pos": start,
                "author": author,
                "target": author,
                "command": "join",
                "date": today_str,
                "date_from_text": False,
                "is_admin_action": False,
                "context": text[max(0, start - 20):end + 20].replace("\n", " ").strip(),
                "post_time": post["timestamp"] or post["time_text"],
            })
            known_names.add(author)
            continue

        # ── цель команды: обычно сам автор; админ может назвать другого ──
        target = author
        if is_admin:
            left_win = text[max(0, start - NAME_LOOKBEHIND, prev_end):start]
            if "\n" in left_win:
                left_win = left_win.rsplit("\n", 1)[-1]
            name = find_left_target(left_win, known_names)
            if name:
                target = name

        # ── дата: ищем СПРАВА от команды ──
        right_bound = min(end + DATE_LOOKAHEAD, next_start)
        right_win = text[end:right_bound]
        if "\n" in right_win:
            right_win = right_win.split("\n", 1)[0]
        date_hits = list(DATE_RE.finditer(right_win))
        written_date = normalize_date(*date_hits[0].groups(), today=today) if date_hits else None

        target_l = target.strip().lower()
        key = (target_l, command)
        used = used_dates.setdefault(key, set())

        # ── удар и срыв — независимые параметры: у одного лица не может
        #    быть одновременно и того, и другого в один день ──
        other_command = OTHER_COMMAND.get(command)
        other_used = used_dates.get((target_l, other_command), set()) if other_command else set()

        if written_date:
            candidate, date_from_text = written_date, True
        else:
            candidate = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            date_from_text = False

        # ── верхняя граница: нельзя действовать позже даты САМОГО
        #    поста (берём из штампа форума, не с часов скрипта) ──
        if candidate > today_str:
            continue

        # ── нижняя граница: нельзя действовать раньше даты, когда
        #    цель команды реально присоединилась к бою ──
        join_date = join_dates.get(target_l)
        if join_date is None or candidate < join_date:
            continue

        if candidate in used or candidate in other_used:
            continue  # эта дата у этого лица уже занята той же (или противоположной) командой
        final_date = candidate

        used.add(final_date)
        known_names.add(target)
        known_names.add(author)

        ctx = text[max(0, start - 30):min(len(text), end + 30)].replace("\n", " ").strip()
        events.append({
            "id": f"{post['post_id']}:{command}:{start}",
            "post_id": post["post_id"],
            "pos": start,
            "author": author,                      # кто физически написал пост
            "target": target,                       # на кого записано действие
            "command": command,
            "date": final_date,
            "date_from_text": date_from_text,
            "is_admin_action": is_admin and target.lower() != author.lower(),
            "context": ctx,
            "post_time": post["timestamp"] or post["time_text"],
        })
    return events


# ── вывод темы ──
def render_topic(title, posts):
    parts = ["""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title><style>
 body{{margin:0;padding:24px 16px;background:#f6f6f4;color:#1d1d1b;
      font:16px/1.6 -apple-system,Segoe UI,Roboto,Arial,sans-serif}}
 .wrap{{max-width:820px;margin:0 auto}}
 .post{{background:#fff;border:1px solid #e2e2dd;padding:14px 18px;margin-bottom:12px}}
 .head{{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;
        border-bottom:1px solid #eee;padding-bottom:6px;margin-bottom:10px}}
 .author{{font-weight:600}} .when{{color:#888;font-size:14px}}
 .body{{white-space:pre-wrap}}
</style></head><body><div class="wrap"><h1>{t}</h1>
<div style="color:#666;font-size:14px;margin-bottom:20px">Постов: {n} · обновлено {s}</div>
""".format(t=html.escape(title), n=len(posts),
           s=datetime.now(TZ).strftime("%d.%m.%Y %H:%M"))]
    for p in posts:
        parts.append(
            '<div class="post" id="post-{pid}"><div class="head">'
            '<span class="author">{a}</span><span class="when">{w}</span></div>'
            '<div class="body">{b}</div></div>\n'.format(
                pid=html.escape(p["post_id"]),
                a=html.escape(p["author"]),
                w=html.escape(p["timestamp"] or p["time_text"] or ""),
                b=html.escape(p["text"]) or "<i>(пусто)</i>",
            )
        )
    parts.append("</div></body></html>")
    return "".join(parts)


def build_used_dates(existing_events):
    """Восстанавливаем занятые даты по (цель, команда) из уже сохранённых
    событий — чтобы дедупликация работала и между запусками."""
    used = {}
    for e in existing_events:
        target = (e.get("target") or e.get("author") or "").strip().lower()
        key = (target, e.get("command"))
        used.setdefault(key, set()).add(e.get("date"))
    return used


def compute_exile_events(events, exiled_already, today, known_event_ids):
    """
    Смотрит по всем известным событиям (удар/срыв/присоединение), когда
    у каждого участника была последняя активность. Если участник не
    изгнан ранее и неактивен EXILE_INACTIVE_DAYS+ дней подряд —
    формирует для него событие "exile".

    exiled_already: set() имён (в нижнем регистре) — кто уже изгнан
    ранее (не переизгоняем повторно, пока не появится новая активность
    и функция un_exile_active_names не снимет изгнание).
    Возвращает (new_exile_events, updated_exiled_set).
    """
    last_activity = {}  # name_lower -> (date_str, display_name)
    for ev in events:
        if ev.get("command") not in ("attack", "sryv", "join"):
            continue
        name = (ev.get("target") or ev.get("author") or "").strip()
        d = ev.get("date")
        if not name or not d:
            continue
        key = name.lower()
        if key not in last_activity or d > last_activity[key][0]:
            last_activity[key] = (d, name)

    today_str = today.strftime("%Y-%m-%d")
    exiled = set(exiled_already)
    new_events = []

    for key, (last_date, display_name) in last_activity.items():
        if key in exiled:
            continue
        try:
            last_dt = datetime.strptime(last_date, "%Y-%m-%d").replace(tzinfo=TZ)
        except ValueError:
            continue
        inactive_days = (today.date() - last_dt.date()).days
        if inactive_days < EXILE_INACTIVE_DAYS:
            continue
        exile_id = f"{display_name}:exile:{today_str}"
        if exile_id in known_event_ids:
            exiled.add(key)
            continue
        known_event_ids.add(exile_id)
        exiled.add(key)
        new_events.append({
            "id": exile_id,
            "post_id": None,
            "pos": None,
            "author": None,
            "target": display_name,
            "command": "exile",
            "date": today_str,
            "date_from_text": False,
            "is_admin_action": False,
            "context": f"неактивен {inactive_days} дн. подряд",
            "post_time": None,
        })
    return new_events, exiled


def unexile_active_names(new_events, exiled):
    """Если изгнанный участник снова подал признак активности (новый
    удар/срыв/присоединение), снимаем с него изгнание — при повторном
    затишье 7+ дней он будет изгнан заново."""
    for ev in new_events:
        if ev.get("command") not in ("attack", "sryv", "join"):
            continue
        name = (ev.get("target") or ev.get("author") or "").strip()
        key = name.lower()
        if key in exiled:
            exiled.discard(key)
            print(f"  ↺ {name} снова активен — изгнание снято")


def main():
    # системные часы — только аварийный fallback на случай, если у
    # поста нет своего штампа времени (не должно происходить в норме);
    # вся игровая логика дат опирается на время форума, а не на них
    system_now = datetime.now(TZ)
    proxies = build_proxies()
    session = requests.Session()

    state = load_json(STATE_FILE, {})
    seen_ids = set(state.get("seen_post_ids", []))
    start_st = int(state.get("last_st", 0))
    all_posts = state.get("posts", [])
    title = state.get("title", f"Тема {TOPIC_ID}")
    known_names = set(state.get("known_names", DEFAULT_PARTICIPANTS))
    join_dates = dict(state.get("join_dates", {}))
    # изгнанные грузятся здесь же (не позже), чтобы join-обработка внутри
    # цикла ниже могла корректно определить «возврат после изгнания»
    exiled = set(state.get("exiled", []))
    # !старт — разовый переключатель на всю тему: пока False, join/атака/
    # срыв не воспринимаются вообще, откуда бы они ни шли
    battle_state = {"started": bool(state.get("battle_started", False))}

    admin_name = state.get("admin")
    if not admin_name:
        try:
            admin_name = fetch_first_post_author(session, proxies)
            print(f"Определён админ темы: {admin_name}")
        except Exception as e:
            print(f"    не удалось определить админа: {e}")
            admin_name = None

    actions = load_json(ACTIONS_FILE, {"topic_id": TOPIC_ID, "events": []})
    known_event_ids = {e["id"] for e in actions.get("events", [])}
    used_dates = build_used_dates(actions.get("events", []))

    new_posts, new_events = [], []
    st = start_st

    for _ in range(MAX_PAGES_PER_RUN):
        url = f"{BASE}index.php?showtopic={TOPIC_ID}&st={st}"
        try:
            page_html = fetch(session, url, proxies)
        except Exception as e:
            print(f"    ошибка запроса: {e} — останавливаюсь")
            break

        page_title, posts = parse_page(page_html)
        if page_title and st == 0:
            title = re.sub(r"\s*-\s*АнтиО\s*$", "", page_title)

        fresh = [p for p in posts if p["post_id"] not in seen_ids]
        print(f"    постов: {len(posts)}, новых: {len(fresh)}")

        for p in fresh:
            seen_ids.add(p["post_id"])
            new_posts.append(p)
            # «сегодня» для этого поста — его СОБСТВЕННЫЙ штамп времени
            # с форума, а не часы машины, где крутится скрипт
            post_today = parse_post_datetime(p) or system_now
            for ev in extract_events(p, post_today, admin_name, known_names,
                                      used_dates, join_dates, exiled, battle_state):
                if ev["id"] not in known_event_ids:
                    known_event_ids.add(ev["id"])
                    new_events.append(ev)

        if len(posts) < POSTS_PER_PAGE:
            break                     # это последняя страница
        st += POSTS_PER_PAGE          # страница заполнена — идём дальше
        state["last_st"] = st - POSTS_PER_PAGE
        time.sleep(DELAY_SEC)

    all_posts.extend(new_posts)

    for ev in new_events:
        if ev["command"] == "join":
            print(f"  + {ev['date']} | {ev['target']} присоединился")
            continue
        via = f" (за {ev['target']}, записал админ {ev['author']})" if ev["is_admin_action"] else ""
        mark = "дата из текста" if ev["date_from_text"] else "дата = вчера"
        print(f"  * {ev['date']} | {ev['target']} | {ev['command']} ({mark}){via}")

    # actions.json сохраняем всегда (даже с пустым events) — иначе на первом
    # запуске, пока в теме нет ни одной команды, файла не будет вообще,
    # и последующий git add по явному имени упадёт с "pathspec ... did not match"
    actions["topic_id"] = TOPIC_ID
    if new_events:
        actions["events"].extend(new_events)

    # ── изгнание за неактивность ──
    # «сегодня» для этой служебной проверки — дата САМОГО СВЕЖЕГО из
    # известных постов темы (по её собственному штампу), а не системные
    # часы: тема может быть неактивна, но это не значит, что часы
    # скрипта врут — а наоборот, системные часы могут ошибаться сами
    known_post_dates = [d for d in (parse_post_datetime(p) for p in all_posts) if d]
    service_today = max(known_post_dates) if known_post_dates else system_now

    unexile_active_names(new_events, exiled)  # свежая активность снимает изгнание
    exile_events, exiled = compute_exile_events(actions["events"], exiled, service_today, known_event_ids)
    if exile_events:
        actions["events"].extend(exile_events)
        for ev in exile_events:
            print(f"  ⛔ {ev['date']} | {ev['target']} | изгнан ({ev['context']})")
    if new_events or exile_events:
        actions["updated"] = datetime.now(TZ).isoformat(timespec="seconds")

    save_json(ACTIONS_FILE, actions)

    if new_posts:
        with open(TOPIC_FILE, "w", encoding="utf-8") as f:
            f.write(render_topic(title, all_posts))

    state.update({
        "title": title,
        "admin": admin_name,
        "last_st": max(0, st - POSTS_PER_PAGE) if st else 0,
        "seen_post_ids": sorted(seen_ids, key=lambda x: int(x) if x.isdigit() else 0),
        "known_names": sorted(known_names),
        "join_dates": join_dates,
        "battle_started": battle_state["started"],
        "exiled": sorted(exiled),
        "posts": all_posts,
        "checked_at": datetime.now(TZ).isoformat(timespec="seconds"),
    })
    save_json(STATE_FILE, state)

    print(f"Новых постов: {len(new_posts)}, новых событий: {len(new_events)}, "
          f"изгнано новых: {len(exile_events)}, всего постов: {len(all_posts)}, админ: {admin_name}")


if __name__ == "__main__":
    main()
