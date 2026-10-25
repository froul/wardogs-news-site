"""Посты канала -> сайт-лента, как t.me/s/newswardogs, но в нашем стиле.

Запуск: python build_site.py   (на GitHub — Actions раз в 30 минут)

Откуда берёт:
* посты, фото, видео и просмотры — с публичной страницы канала https://t.me/s/newswardogs
  (листает всю историю, новые посты подтягиваются сами);
* картинку премиум-эмодзи по номеру — один раз у Telegram (токен бота из переменной
  TELEGRAM_BOT_TOKEN, только чтение), затем по картинке находится наш PNG из emoji-lib/;
  результат запоминается в site/emoji/ и больше не запрашивается.

Пишет в site/: index.html (лента), sitemap.xml, robots.txt, llms.txt и llms-full.txt
(для ИИ-поисковиков), скачивает фото/видео в site/media/. Домен берётся из файла site/CNAME.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

CHANNEL = "newswardogs"
TG = f"https://t.me/{CHANNEL}"
MSK = timezone(timedelta(hours=3))  # время постов показываем по Москве

HERE = Path(__file__).resolve().parent
SITE = HERE / "site"
EMOJI_LIB = HERE / "emoji-lib"
ENV = HERE / ".env"  # необязательно: TELEGRAM_BOT_TOKEN=… для локальной сборки (в git не попадает)
OUT_EMOJI = SITE / "emoji"
OUT_MEDIA = SITE / "media"
TEMPLATE = HERE / "template.html"

MATCH_LIMIT = 35.0  # средняя разница пикселей 32×32; совпадения ~3–25, чужие картинки 50+
VOID = {"br", "img", "meta", "link", "input", "hr", "source"}
INLINE = {"strong": "b", "b": "b", "em": "i", "i": "i", "u": "u", "s": "s", "strike": "s",
          "del": "s", "code": "code", "pre": "pre", "blockquote": "blockquote"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WardogsSite/1.0"}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

EYE = ('<svg class="eye" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 5C6.5 5 '
       '2.7 9.4 1.5 12c1.2 2.6 5 7 10.5 7s9.3-4.4 10.5-7C21.3 9.4 17.5 5 12 5zm0 11.2A4.2 4.2 0 1 1 12 '
       '7.8a4.2 4.2 0 0 1 0 8.4zm0-6.4a2.2 2.2 0 1 0 0 4.4 2.2 2.2 0 0 0 0-4.4z"/></svg>')
PLAY = ('<svg class="play" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" '
        'd="M8 5.5v13l11-6.5z"/></svg>')


def log(*a) -> None:
    print(*a, flush=True)


def fetch(url: str, data: bytes | None = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, data=data, headers=UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def esc(s: str) -> str:
    return html.escape(s, quote=True)


# ---------------------------------------------------------------- маленькое DOM-дерево


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    kids: list[Node | str] = field(default_factory=list)

    @property
    def cls(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def find_all(self, pred, skip=None) -> list[Node]:
        out = []
        for k in self.kids:
            if isinstance(k, Node):
                if skip and skip(k):
                    continue
                if pred(k):
                    out.append(k)
                out += k.find_all(pred, skip)
        return out

    def first(self, pred, skip=None) -> Node | None:
        found = self.find_all(pred, skip)
        return found[0] if found else None

    def text(self) -> str:
        return "".join(k if isinstance(k, str) else k.text() for k in self.kids)


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].kids.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def parse(text: str) -> Node:
    t = _Tree()
    t.feed(text)
    return t.root


def has(c: str):
    return lambda n: c in n.cls


def bg_url(node: Node) -> str:
    m = re.search(r"background-image:url\('([^']+)'\)", node.attrs.get("style", ""))
    if not m:
        return ""
    u = m.group(1)
    return "https:" + u if u.startswith("//") else u


# ---------------------------------------------------------------- премиум-эмодзи


def _norm(im: Image.Image) -> Image.Image:
    im = im.convert("RGBA")
    box = im.getchannel("A").point(lambda a: 255 if a > 40 else 0).getbbox()
    if box:
        im = im.crop(box)
    w, h = im.size
    s = max(w, h, 1)
    sq = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sq.paste(im, ((s - w) // 2, (s - h) // 2))
    bg = Image.new("RGBA", sq.size, (0, 0, 0, 255))
    return Image.alpha_composite(bg, sq).convert("RGB").resize((32, 32), Image.LANCZOS)


def _bot_token() -> str:
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        return os.environ["TELEGRAM_BOT_TOKEN"].strip()
    try:
        for line in ENV.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


class Emoji:
    """Номер премиум-эмодзи -> файл в site/emoji/ (наш PNG, если нашёлся похожий)."""

    def __init__(self) -> None:
        OUT_EMOJI.mkdir(parents=True, exist_ok=True)
        self.map_path = OUT_EMOJI / "map.json"
        try:
            self.map: dict[str, str] = json.loads(self.map_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.map = {}
        self._lib: dict[Path, Image.Image] | None = None
        self.token = _bot_token()
        self.new = 0

    def lib(self) -> dict[Path, Image.Image]:
        if self._lib is None:
            self._lib = {}
            for p in sorted(EMOJI_LIB.rglob("*.png")) if EMOJI_LIB.exists() else []:
                with Image.open(p) as im:
                    self._lib[p] = _norm(im)
        return self._lib

    def src(self, eid: str) -> str:
        name = self.map.get(eid, "")
        return f"emoji/{urllib.parse.quote(name)}" if name and (OUT_EMOJI / name).exists() else ""

    def resolve(self, ids: set[str]) -> None:
        """Скачивает картинки ещё не известных эмодзи (по 100 за запрос)."""
        todo = sorted(i for i in ids if not self.src(i))
        if not todo:
            return
        if not self.token:
            log(f"! {len(todo)} эмодзи без картинки: нет TELEGRAM_BOT_TOKEN — покажем обычные эмодзи")
            return
        api = f"https://api.telegram.org/bot{self.token}"
        for k in range(0, len(todo), 100):
            chunk = todo[k:k + 100]
            try:
                body = json.dumps({"custom_emoji_ids": chunk}).encode()
                res = json.loads(fetch(f"{api}/getCustomEmojiStickers", body))["result"]
            except Exception as e:  # noqa: BLE001 - сеть/Telegram: попробуем в следующий раз
                log(f"! Telegram не отдал эмодзи: {type(e).__name__}")
                return
            for st in res:
                self._save(api, st)
        self.map_path.write_text(json.dumps(self.map, indent=1, sort_keys=True), encoding="utf-8")

    def _save(self, api: str, st: dict) -> None:
        eid = st["custom_emoji_id"]
        # анимированные (tgs/webm) браузер как картинку не покажет — берём их превью
        fid = st["file_id"] if not (st.get("is_animated") or st.get("is_video")) else \
            (st.get("thumbnail") or {}).get("file_id", "")
        if not fid:
            return
        try:
            path = json.loads(fetch(f"{api}/getFile?file_id={urllib.parse.quote(fid)}"))["result"]["file_path"]
            raw = fetch(f"{api.replace('/bot', '/file/bot')}/{path}")
            im = Image.open(io.BytesIO(raw))
            im.load()
        except Exception:  # noqa: BLE001
            return
        a = _norm(im)
        best, score = None, 1e9
        for p, b in self.lib().items():
            s = sum(ImageStat.Stat(ImageChops.difference(a, b)).mean)
            if s < score:
                best, score = p, s
        if best is not None and score <= MATCH_LIMIT:
            name = "_".join(best.relative_to(EMOJI_LIB).with_suffix("").parts) + ".png"
            (OUT_EMOJI / name).write_bytes(best.read_bytes())
        else:
            name = f"tg_{eid}.png"
            im.convert("RGBA").save(OUT_EMOJI / name)
        self.map[eid] = name
        self.new += 1


# ---------------------------------------------------------------- текст поста


def safe_href(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    return url if re.match(r"(?i)^(https?:|tg:|mailto:)", url) else ""


class Text:
    """div.tgme_widget_message_text -> безопасный HTML (только наши теги)."""

    def __init__(self, emoji: Emoji) -> None:
        self.emoji = emoji

    def render(self, node: Node) -> str:
        out: list[str] = []
        self._kids(node, out)
        s = "".join(out)
        s = re.sub(r"(</blockquote>|</pre>)\s*<br>", r"\1", s)  # цитата и так заканчивает строку
        return re.sub(r"^(\s|<br>)+|(\s|<br>)+$", "", s)

    def _kids(self, node: Node, out: list[str]) -> None:
        for k in node.kids:
            if isinstance(k, str):
                out.append(html.escape(re.sub(r"\s*\n\s*", " ", k), quote=False))
            else:
                self._node(k, out)

    def _node(self, n: Node, out: list[str]) -> None:
        if n.tag == "br":
            out.append("<br>")
        elif n.tag == "tg-emoji":
            src = self.emoji.src(n.attrs.get("emoji-id", ""))
            alt = esc(n.text().strip())
            out.append(f'<img class="e" src="{src}" alt="{alt}">' if src else alt)
        elif n.tag == "i" and "emoji" in n.cls:  # обычный эмодзи картинкой Telegram: берём символ
            out.append(html.escape(n.text().strip()))
        elif n.tag == "a":
            href = safe_href(n.attrs.get("href", ""))
            out.append(f'<a href="{esc(href)}" target="_blank" rel="noopener">' if href else "<span>")
            self._kids(n, out)
            out.append("</a>" if href else "</span>")
        elif n.tag in INLINE:
            t = INLINE[n.tag]
            out.append(f"<{t}>")
            self._kids(n, out)
            out.append(f"</{t}>")
        elif n.tag in ("tg-spoiler",) or "tg-spoiler" in n.cls:
            out.append('<span class="spoiler">')
            self._kids(n, out)
            out.append("</span>")
        elif n.tag in ("script", "style", "svg"):
            return
        else:
            self._kids(n, out)


def emoji_ids(node: Node) -> set[str]:
    return {n.attrs.get("emoji-id", "") for n in node.find_all(lambda n: n.tag == "tg-emoji")} - {""}


def plain(s: str, limit: int) -> str:
    """HTML поста -> короткий текст для описания страницы."""
    t = re.sub(r"<br>", " ", s)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[:limit - 1].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------- посты со страницы канала


def download(url: str, name: str, keep: set[str]) -> str:
    """CDN Telegram -> site/media/<name>. Возвращает путь для страницы или '' при ошибке."""
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower() or ".jpg"
    fname = name + ext
    keep.add(fname)
    target = OUT_MEDIA / fname
    if not target.exists():
        try:
            data = fetch(url, timeout=120)
        except Exception:  # noqa: BLE001
            keep.discard(fname)
            return ""
        target.write_bytes(data)
    return "media/" + urllib.parse.quote(fname)


def img_size(src: str) -> tuple[int, int]:
    try:
        with Image.open(OUT_MEDIA / urllib.parse.unquote(src.split("/", 1)[1])) as im:
            return im.size
    except (OSError, IndexError):
        return (0, 0)


def scrape() -> tuple[list[Node], Node | None]:
    """Все посты со страницы t.me/s/<канал> (листает назад по 20) + блок описания канала."""
    msgs: dict[int, Node] = {}
    info = None
    before = None
    for _ in range(500):
        url = f"https://t.me/s/{CHANNEL}" + (f"?before={before}" if before else "")
        page = parse(fetch(url).decode("utf-8", "replace"))
        if info is None:
            info = page.first(has("tgme_channel_info_description"))
        found = page.find_all(lambda n: n.tag == "div" and "tgme_widget_message" in n.cls and "data-post" in n.attrs)
        if not found:
            break
        for m in found:
            msgs[int(m.attrs["data-post"].rsplit("/", 1)[1])] = m
        more = page.first(has("tme_messages_more"))
        before = more.attrs.get("data-before") if more else None
        if not before:
            break
    return [msgs[k] for k in sorted(msgs)], info


def collect() -> tuple[list[dict], str, Emoji]:
    OUT_MEDIA.mkdir(parents=True, exist_ok=True)
    emoji = Emoji()
    text = Text(emoji)
    nodes, info = scrape()
    not_reply = lambda n: "tgme_widget_message_reply" in n.cls  # noqa: E731 - цитата ответа
    ids: set[str] = set()
    for m in nodes:
        t = m.first(has("tgme_widget_message_text"), skip=not_reply)
        if t:
            ids |= emoji_ids(t)
    emoji.resolve(ids)

    keep: set[str] = set()
    posts = []
    for m in nodes:
        if "service_message" in m.cls:
            continue
        pid = m.attrs["data-post"].rsplit("/", 1)[1]
        tnode = m.first(has("tgme_widget_message_text"), skip=not_reply)
        body = text.render(tnode) if tnode else ""
        media = []
        for k, a in enumerate(m.find_all(lambda n: n.cls & {"tgme_widget_message_photo_wrap",
                                                            "tgme_widget_message_video_player"},
                                         skip=not_reply)):
            name = f"{pid}_{k + 1}"
            if "tgme_widget_message_photo_wrap" in a.cls:
                src = download(bg_url(a), name, keep) if bg_url(a) else ""
                if src:
                    w, h = img_size(src)
                    media.append({"type": "photo", "src": src, "w": w, "h": h})
                continue
            thumb = a.first(has("tgme_widget_message_video_thumb"))
            poster = download(bg_url(thumb), name + "_thumb", keep) if thumb and bg_url(thumb) else ""
            w, h = img_size(poster) if poster else (0, 0)
            video = a.first(lambda n: n.tag == "video")
            gif = bool(a.first(has("message_video_gif")))
            src = download(video.attrs["src"], name, keep) if video and video.attrs.get("src") else ""
            if src:
                media.append({"type": "gif" if gif else "video", "src": src, "poster": poster, "w": w, "h": h})
            else:  # «Media is too big»: превью, а смотреть — в канале
                media.append({"type": "external", "src": poster, "w": w, "h": h})
        if not body and not media:
            continue
        tm = m.first(lambda n: n.tag == "time" and "datetime" in n.attrs)
        views = m.first(has("tgme_widget_message_views"))
        posts.append({
            "id": pid,
            "date": tm.attrs["datetime"] if tm else "",
            "html": body,
            "media": media,
            "views": views.text().strip() if views else "",
        })
    for f in OUT_MEDIA.iterdir():  # файлы постов, которых больше нет в канале
        if f.is_file() and f.name not in keep:
            f.unlink()
    about = text.render(info) if info else ""
    return posts, about, emoji


# ---------------------------------------------------------------- разметка страницы


FOOTER = re.compile(r"WARDOGS NEWS\s*\|\s*ПОДПИСАТЬСЯ", re.I)  # подпись в конце каждого поста


def body_text(p: dict) -> str:
    """Пост -> чистый текст по строкам, без подписи канала (для описаний и llms.txt)."""
    t = re.sub(r"<br>|</blockquote>|</pre>", "\n", p["html"])
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = FOOTER.sub("", t)
    rows = [re.sub(r"[ \t]+", " ", r).strip() for r in t.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(rows)).strip()


def _rows(p: dict) -> list[str]:
    """Строки поста без эмодзи и маркеров в начале."""
    rows = (re.sub(r"^[^\wА-Яа-яЁё«\"]+", "", r).strip() for r in body_text(p).split("\n"))
    return [r for r in rows if r]


def headline(p: dict) -> str:
    """Первая строка поста — заголовок новости."""
    rows = _rows(p)
    return plain(esc(rows[0]), 110) if rows else f"Новость №{p['id']}"


def summary(p: dict, limit: int) -> str:
    """Текст после заголовка одной строкой, с точками между пунктами."""
    rows = [r if re.search(r"[.!?…:;»)]$", r) else r + "." for r in _rows(p)[1:]]
    return plain(esc(" ".join(rows)), limit) or headline(p)


def media_html(p: dict, eager: bool = False) -> str:
    items = p["media"]
    alt = esc(headline(p))
    load = "eager" if eager else "lazy"
    if not items:
        return ""
    link = f"{TG}/{p['id']}"
    out = []
    for m in items:
        style = ""
        if len(items) == 1 and m["w"] and m["h"]:
            r = min(1.9, max(0.8, m["w"] / m["h"]))  # как Telegram: не выше 0.8 и не шире 1.9
            style = f' style="aspect-ratio:{r:.3f}"'
        size = f' width="{m["w"]}" height="{m["h"]}"' if m["w"] else ""
        if m["type"] == "photo":
            out.append(f'<a class="m" href="{m["src"]}" data-zoom{style}>'
                       f'<img src="{m["src"]}"{size} alt="{alt}" loading="{load}" decoding="async"></a>')
        elif m["type"] == "external":
            img = f'<img src="{m["src"]}"{size} alt="{alt}" loading="{load}" decoding="async">' if m["src"] else ""
            out.append(f'<a class="m out" href="{link}" target="_blank" rel="noopener"{style}>{img}'
                       f'<span class="watch">{PLAY}Смотреть в Telegram</span></a>')
        else:
            poster = f' poster="{m["poster"]}"' if m.get("poster") else ""
            if m["type"] == "gif":
                out.append(f'<div class="m"{style}><video data-src="{m["src"]}"{poster} muted loop playsinline '
                           f'preload="none"></video><span class="tag">GIF</span></div>')
            else:
                out.append(f'<div class="m"{style}><video src="{m["src"]}"{poster} controls playsinline '
                           f'preload="none"></video></div>')
    n = len(items)
    cls = f"n{min(n, 4)}" + (" odd" if n > 4 and n % 2 else "")
    return f'<div class="media {cls}">{"".join(out)}</div>'


def post_html(p: dict, eager: bool = False) -> str:
    d = datetime.fromisoformat(p["date"]).astimezone(MSK) if p["date"] else None
    when = f'{d:%d.%m}<span class="y">.{d:%Y}</span> · {d:%H:%M}' if d else ""
    views = f'<span class="views">{EYE}{esc(p["views"])}</span>' if p["views"] else ""
    text = f'<div class="text">{p["html"]}</div>' if p["html"] else ""
    return (f'<article class="post" id="{p["id"]}"><div class="clip">{media_html(p, eager)}{text}'
            f'<div class="foot"><a class="open" href="{TG}/{p["id"]}" target="_blank" rel="noopener">'
            f'Открыть в Telegram</a><span class="meta">{views}'
            f'<a class="time" href="#{p["id"]}"><time datetime="{esc(p["date"])}">{when}</time></a></span></div>'
            f'<div class="scan"></div></div></article>')


def feed_html(posts: list[dict]) -> str:
    out, last = [], None
    now_year = datetime.now(MSK).year
    for i, p in enumerate(posts):
        if p["date"]:
            d = datetime.fromisoformat(p["date"]).astimezone(MSK)
            if d.date() != last:
                last = d.date()
                label = f"{d.day} {MONTHS[d.month - 1]}" + ("" if d.year == now_year else f" {d.year}")
                out.append(f'<div class="day"><span>{label}</span></div>')
        out.append(post_html(p, eager=i >= len(posts) - 3))  # последние видны сразу — грузим без очереди
    return "\n".join(out)


# ---------------------------------------------------------------- для поисковиков и ИИ

ABOUT_ORG = ("Неофициальный фанатский новостной проект об онлайн-игре WARDOGS: обновления, патчи, события "
             "и заявления разработчиков. Не связан с разработчиками и издателем игры.")
SOCIALS = [TG, "https://youtube.com/@WARDOGS_NEWS", "https://tiktok.com/@news_wardogs"]
AI_BOTS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot", "Claude-User",
           "PerplexityBot", "Perplexity-User", "Google-Extended", "Applebot-Extended", "YandexBot",
           "YandexAdditional", "Bingbot", "DuckAssistBot", "meta-externalagent", "MistralAI-User"]


def ru_date(iso: str) -> str:
    d = datetime.fromisoformat(iso).astimezone(MSK)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def json_ld(posts: list[dict], url: str, desc: str) -> str:
    """Разметка schema.org: кто мы, что за сайт и список последних новостей."""
    org = {"@type": "Organization", "@id": url + "#org", "name": "Wardogs News", "url": url,
           "logo": url + "assets/avatar.jpg", "description": ABOUT_ORG, "sameAs": SOCIALS}
    game = {"@type": "VideoGame", "name": "WARDOGS"}
    news = []
    for p in reversed(posts[-30:]):
        item = {"@type": "NewsArticle", "headline": headline(p), "url": f"{url}#{p['id']}",
                "datePublished": p["date"], "inLanguage": "ru", "about": game,
                "author": {"@id": url + "#org"}, "publisher": {"@id": url + "#org"},
                "description": summary(p, 300), "sameAs": f"{TG}/{p['id']}"}
        img = [url + m["src"] for m in p["media"] if m["type"] == "photo"]
        if img:
            item["image"] = img
        news.append(item)
    graph = [
        org,
        {"@type": "WebSite", "@id": url + "#site", "url": url, "name": "Wardogs News", "inLanguage": "ru",
         "description": desc, "publisher": {"@id": url + "#org"}, "about": game},
        {"@type": "CollectionPage", "url": url, "name": "Wardogs News — новости игры WARDOGS",
         "isPartOf": {"@id": url + "#site"}, "about": game, "inLanguage": "ru",
         "dateModified": posts[-1]["date"] if posts else None,
         "mainEntity": {"@type": "ItemList", "itemListOrder": "https://schema.org/ItemListOrderDescending",
                        "itemListElement": [{"@type": "ListItem", "position": i + 1, "item": n}
                                            for i, n in enumerate(news)]}},
    ]
    raw = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)
    raw = raw.replace("</", "<\\/")  # текст поста не должен закрыть тег script
    return f'<script type="application/ld+json">{raw}</script>'


def llms(posts: list[dict], url: str, desc: str) -> tuple[str, str]:
    """llms.txt — краткая справка о сайте для ИИ-поисковиков; llms-full.txt — все новости текстом."""
    head = (f"# Wardogs News\n\n> {ABOUT_ORG} Все новости первыми выходят в Telegram-канале {TG}\n\n"
            f"{desc}\n\n## Где следить\n\n"
            f"- [Telegram-канал]({TG}): основной источник, новости выходят здесь первыми\n"
            f"- [YouTube]({SOCIALS[1]}): видеоверсии новостей\n"
            f"- [TikTok]({SOCIALS[2]}): короткие ролики и карусели\n")
    short = [head, "\n## Последние новости\n\n"]
    full = [head, "\n## Все новости (сначала новые)\n"]
    for p in reversed(posts):
        when = ru_date(p["date"]) if p["date"] else ""
        short.append(f"- [{html.unescape(headline(p))}]({url}#{p['id']}): {when}. "
                     f"{summary(p, 220)}\n")
        full.append(f"\n### {html.unescape(headline(p))}\n\nДата: {when}. Источник: {TG}/{p['id']}\n\n"
                    f"{body_text(p)}\n")
    short.append(f"\n## Optional\n\n- [Полный текст всех новостей]({url}llms-full.txt)\n")
    return "".join(short), "".join(full)


def robots(url: str) -> str:
    out = ["User-agent: *", "Allow: /", ""]
    out += ["# AI search engines and assistants are welcome to read and cite the news"]
    for bot in AI_BOTS:
        out.append(f"User-agent: {bot}")
    out += ["Allow: /", ""]
    if url:
        out.append(f"Sitemap: {url}sitemap.xml")
    return "\n".join(out) + "\n"


def build() -> None:
    posts, about, emoji = collect()
    domain = (SITE / "CNAME").read_text(encoding="utf-8").strip() if (SITE / "CNAME").exists() else ""
    url = f"https://{domain}/" if domain else ""
    last = posts[-1] if posts else None
    desc = ("Новости игры WARDOGS: обновления, патчи, события и заявления разработчиков. "
            "Все новости первыми выходят в Telegram-канале Wardogs News.")
    page = TEMPLATE.read_text(encoding="utf-8")
    image = url + "assets/og.jpg"  # постоянное превью 1200×630 для соцсетей и мессенджеров
    fill = {
        "FEED": feed_html(posts),
        "ABOUT": about,
        "DESCRIPTION": esc(desc),
        "URL": esc(url),
        "CANONICAL": f'<link rel="canonical" href="{esc(url)}">' if url else "",
        "OG_IMAGE": (f'<meta property="og:image" content="{esc(image)}">\n'
                     '<meta property="og:image:type" content="image/jpeg">\n'
                     '<meta property="og:image:width" content="1200">\n'
                     '<meta property="og:image:height" content="630">\n'
                     '<meta property="og:image:alt" content="Wardogs News — новости игры WARDOGS">\n'
                     f'<meta name="twitter:image" content="{esc(image)}">') if url else "",
        "UPDATED": f'<meta property="og:updated_time" content="{esc(last["date"])}">' if last else "",
        "JSONLD": json_ld(posts, url, desc),
        "TG": TG,
        # меняется вместе с CSS/JS — браузеры не держат старую версию из кэша
        "V": hashlib.sha1(b"".join((SITE / "assets" / f).read_bytes()
                                   for f in ("style.css", "app.js"))).hexdigest()[:8],
        "BUILT": datetime.now(MSK).isoformat(timespec="seconds"),
        "YEAR": str(datetime.now(MSK).year),
    }
    page = re.sub(r"\{\{(\w+)\}\}", lambda m: fill[m.group(1)], page)
    (SITE / "index.html").write_text(page, encoding="utf-8")
    short, full = llms(posts, url, desc)
    (SITE / "llms.txt").write_text(short, encoding="utf-8")
    (SITE / "llms-full.txt").write_text(full, encoding="utf-8")
    (SITE / "robots.txt").write_text(robots(url), encoding="utf-8")
    if url:
        imgs = "".join(f"<image:image><image:loc>{esc(url + m['src'])}</image:loc></image:image>"
                       for p in posts for m in p["media"] if m["type"] == "photo")
        (SITE / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n'
            f'<url><loc>{esc(url)}</loc><lastmod>{last["date"] if last else ""}</lastmod>'
            f'<changefreq>hourly</changefreq>{imgs}</url>\n'
            '</urlset>\n', encoding="utf-8")
    else:
        log("! нет файла site/CNAME — ссылки для поисковиков (canonical, sitemap, картинки) не подписаны доменом")
    n_media = sum(len(p["media"]) for p in posts)
    log(f"постов: {len(posts)} (последний №{last['id'] if last else '-'}), фото/видео: {n_media}, "
        f"новых эмодзи: {emoji.new} — site/index.html готов")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    build()
