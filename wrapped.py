"""
Wrapped — итоги твоего Telegram за период: красивые карточки, которые не стыдно переслать.

  .wrapped [период]          итоги по личкам и группам; период: 7d · 30d (по умолчанию) · 90d · 365d · year
                             (с 1 января) · 2w · 6m. Пример: .wrapped 90d
  .wrapped here [период]     итоги только этого чата (в личке — «вы двое», в группе — твои сообщения в ней)
  .wrapped stop              остановить подсчёт

Что внутри карточек:
  1. сколько ты написал и получил, дней в сети, слов, минут голосовых;
  2. твои люди: топ-5 собеседников, кто чаще пишет первым, как быстро ты отвечаешь;
  3. когда ты пишешь: часы и дни недели, «сова» ты или «жаворонок», самый бурный день;
  4. твой словарь: любимые слова и эмодзи;
  5. факты: кружки, стикеры, самое длинное сообщение, лучший стрик, самый ночной чат.

Как считается: модуль сам листает переписку от имени аккаунта (ничего никуда не отправляет, кроме итоговых
карточек в чат). Сколько диалогов и сообщений смотреть — настройки max_dialogs и max_per_chat (.cfg wrapped):
чем больше, тем точнее и дольше. В группах учитываются только твои сообщения. Боты, каналы и «Избранное»
не учитываются. Команды юзербота и ответы модулей в словарь не попадают.
Шрифт Montserrat скачивается один раз с GitHub (Google Fonts, лицензия OFL); без сети — системный шрифт.
"""

import asyncio
import datetime as dt
import html
import io
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import aiohttp

import decorators
import engine
import visual

NAME = "Wrapped"
MODULE_NAME = "wrapped"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Итоги твоего Telegram за период: карточки с людьми, временем, словами и фактами"
TG_URL = "https://t.me/mescr_m"
REQUIREMENTS = [("Pillow", "PIL")]

FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf"
W, H = 1080, 1350

# цвета FRX: тёмный фон, огненный акцент
BG = (13, 13, 16)
PANEL = (24, 24, 29)
PANEL2 = (33, 33, 40)
TEXT = (245, 245, 247)
MUTED = (150, 150, 162)
ACCENT = (255, 92, 31)
ACCENT2 = (255, 45, 85)
GOLD = (255, 186, 73)

STOP = set("""
это этот эта эти того тому там тут так как что чтобы если когда тоже также только уже есть было были будет будут
меня мне мной тебя тебе тобой него нему ним нее неё ней них нам нас вам вас ваш ваше ваша наш наша наше свой свою
своё свое себя себе очень можно нужно надо просто вообще всегда никогда кстати типа короче вроде ещё еще даже
потом сейчас сегодня завтра вчера здесь where with that this have from your what about there they them then than
just like will would could should been were when which into some because also only http https www com html
почему зачем какой какая какие какое который которая которые которое чего всех всем всего весь вся всё все
него ничего никто кого кому чем нибудь либо пока ладно тогда опять снова больше меньше много мало хорошо плохо
""".split())

EMOJI_RE = re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2})|"
    "(?:[\U0001F300-\U0001FAFF☀-➿⭐⭕❤⏰-⏺]"
    "[️\U0001F3FB-\U0001F3FF]?"
    "(?:‍[\U0001F300-\U0001FAFF☀-➿❤][️\U0001F3FB-\U0001F3FF]?)*)"
)
WORD_RE = re.compile(r"[a-zа-яё]{4,}", re.I)
LINK_RE = re.compile(r"https?://\S+|t\.me/\S+|@\w+")
BOT_MARKS = ("🟥", "🟧", "🔶", "⏳", "⚠️", "▸", "◆", "━")

_state = {"running": False, "cancel": False}


class _Stopped(Exception):
    pass


def _esc(x):
    return html.escape(str(x), quote=False)


def _num(n):
    return f"{int(n):,}".replace(",", " ")


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _dur(seconds):
    s = int(seconds)
    if s < 60:
        return f"{s} с"
    if s < 3600:
        return f"{s // 60} мин"
    h, m = divmod(s // 60, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


# --------------------------------------------------------------------------- период

def parse_period(arg):
    """-> (start_utc, label) ; ValueError на непонятное."""
    now = dt.datetime.now(dt.timezone.utc)
    a = (arg or "30d").strip().lower()
    if a in ("year", "год", "y"):
        local = dt.datetime.now().astimezone()
        start = local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(dt.timezone.utc), f"{local.year} год"
    m = re.fullmatch(r"(\d{1,4})\s*([dwmдн]|д|н|м|day|days|week|weeks|month|months)?", a)
    if not m:
        raise ValueError("период: 7d, 30d, 90d, 365d, 2w, 6m или year")
    n = int(m.group(1))
    unit = (m.group(2) or "d")[0]
    days = n * {"d": 1, "д": 1, "w": 7, "н": 7, "m": 30, "м": 30}.get(unit, 1)
    days = max(1, min(days, 3650))
    label = f"{days} {_plural(days, 'день', 'дня', 'дней')}"
    if days == 7:
        label = "неделю"
    elif days in (30, 31):
        label = "месяц"
    elif days in (365, 366):
        label = "год"
    return now - dt.timedelta(days=days), label


# --------------------------------------------------------------------------- сбор статистики

class Stats:
    def __init__(self):
        self.sent = 0
        self.received = 0
        self.words = 0
        self.chars = 0
        self.hours = [0] * 24
        self.weekdays = [0] * 7
        self.per_day = Counter()
        self.word_counter = Counter()
        self.emoji_counter = Counter()
        self.voice_sec = 0
        self.voice_n = 0
        self.round_n = 0
        self.sticker_n = 0
        self.gif_n = 0
        self.photo_n = 0
        self.video_n = 0
        self.longest = 0
        self.night_by_chat = Counter()
        self.my_resp = []
        self.their_resp = []
        self.people = {}            # id -> dict(name, out, inc, me_first, they_first, resp=[])
        self.groups = Counter()     # name -> out
        self.chats_scanned = 0
        self.entities = {}          # id -> entity (для аватарок)


def _is_bot_output(text, prefix):
    t = text.lstrip()
    return (not t) or t.startswith(prefix) or t.startswith(BOT_MARKS)


def _name(ent):
    first = getattr(ent, "first_name", None) or getattr(ent, "title", None) or "Без имени"
    last = getattr(ent, "last_name", None)
    return f"{first} {last}".strip() if last else first


def _account_msg(S, m, chat_key, prefix):
    """Учитывает одно ИСХОДЯЩЕЕ сообщение."""
    local = m.date.astimezone()
    S.sent += 1
    S.hours[local.hour] += 1
    S.weekdays[local.weekday()] += 1
    S.per_day[local.date()] += 1
    if 0 <= local.hour < 5:
        S.night_by_chat[chat_key] += 1
    if m.voice:
        S.voice_n += 1
        S.voice_sec += int(getattr(m.file, "duration", 0) or 0)
    elif m.video_note:
        S.round_n += 1
    elif m.sticker:
        S.sticker_n += 1
    elif m.gif:
        S.gif_n += 1
    elif m.photo:
        S.photo_n += 1
    elif m.video:
        S.video_n += 1
    text = m.raw_text or ""
    if text and not m.via_bot_id and not _is_bot_output(text, prefix):
        S.chars += len(text)
        S.longest = max(S.longest, len(text))
        clean = LINK_RE.sub(" ", text)
        ws = WORD_RE.findall(clean)
        S.words += len(clean.split())
        for w in ws:
            w = w.lower().replace("ё", "е")
            if w not in STOP:
                S.word_counter[w] += 1
        for e in EMOJI_RE.findall(text):
            S.emoji_counter[e.replace("️", "")] += 1


async def collect(client, start, cfg, only_chat=None, progress=None):
    prefix = await engine.get_prefix()
    S = Stats()
    targets = []   # (entity, kind) kind: 'user' | 'group'
    if only_chat is not None:
        ent = await client.get_entity(only_chat)
        kind = "user" if getattr(ent, "first_name", None) is not None or ent.__class__.__name__ == "User" else "group"
        targets.append((ent, kind))
    else:
        async for d in client.iter_dialogs():
            if len(targets) >= cfg["max_dialogs"]:
                break
            if not d.pinned and d.date and d.date < start:
                break       # дальше только старые диалоги
            if d.date and d.date < start:
                continue
            ent = d.entity
            if d.is_user:
                if getattr(ent, "bot", False) or getattr(ent, "is_self", False) or getattr(ent, "deleted", False) \
                        or ent.id in (777000, 42777):
                    continue
                targets.append((ent, "user"))
            elif d.is_group and cfg["groups"]:
                targets.append((ent, "group"))

    total = len(targets)
    last_edit = 0.0
    for i, (ent, kind) in enumerate(targets, 1):
        if _state["cancel"]:
            raise _Stopped()
        if progress and time.monotonic() - last_edit > 3:
            last_edit = time.monotonic()
            await progress(i - 1, total, _name(ent))
        S.chats_scanned += 1
        key = _name(ent)
        if kind == "group":
            n = 0
            async for m in client.iter_messages(ent, from_user="me", limit=cfg["max_per_chat"]):
                if m.date < start:
                    break
                if m.action:
                    continue
                _account_msg(S, m, key, prefix)
                n += 1
            if n:
                S.groups[key] += n
            continue

        p = S.people.setdefault(ent.id, {"name": key, "out": 0, "inc": 0, "me_first": 0, "they_first": 0, "resp": []})
        S.entities[ent.id] = ent
        seq = []
        async for m in client.iter_messages(ent, limit=cfg["max_per_chat"]):
            if m.date < start:
                break
            if m.action:
                continue
            seq.append((m.date, bool(m.out)))
            if m.out:
                p["out"] += 1
                _account_msg(S, m, key, prefix)
            else:
                p["inc"] += 1
                S.received += 1
        seq.reverse()
        prev = None
        for date, out in seq:
            if prev is None or (date - prev[0]).total_seconds() > 6 * 3600:
                if out:
                    p["me_first"] += 1
                else:
                    p["they_first"] += 1
            elif prev[1] != out:
                gap = (date - prev[0]).total_seconds()
                if gap < 12 * 3600:
                    if out:
                        S.my_resp.append(gap)
                        p["resp"].append(gap)
                    else:
                        S.their_resp.append(gap)
            prev = (date, out)
    if progress:
        await progress(total, total, "")
    return S


def _median(xs):
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _streak(days):
    if not days:
        return 0, None
    ds = sorted(days)
    best, cur, best_end = 1, 1, ds[0]
    for a, b in zip(ds, ds[1:]):
        cur = cur + 1 if (b - a).days == 1 else 1
        if cur > best:
            best, best_end = cur, b
    return best, best_end


def summarize(S, label, me_name, total_days, hide_names=False):
    """Stats -> простой dict для отрисовки (без telethon-объектов)."""
    def nm(n):
        if not hide_names:
            return n
        parts = n.split()
        return " ".join(p[0] + "•" * min(4, max(1, len(p) - 1)) for p in parts if p)

    people = sorted(S.people.items(), key=lambda kv: kv[1]["out"] + kv[1]["inc"], reverse=True)
    people = [(pid, p) for pid, p in people if p["out"] + p["inc"] > 0]
    top = []
    for pid, p in people[:5]:
        first = "ты" if p["me_first"] >= p["they_first"] else "собеседник"
        top.append({
            "id": pid, "name": nm(p["name"]), "total": p["out"] + p["inc"], "out": p["out"], "inc": p["inc"],
            "first": first, "resp": _median(p["resp"]),
        })
    me_first = sum(p["me_first"] for _, p in people)
    they_first = sum(p["they_first"] for _, p in people)
    night_total = sum(S.hours[0:5])
    morning = sum(S.hours[5:11])
    day = sum(S.hours[11:17])
    evening = sum(S.hours[17:24])
    chrono = max((("Сова", night_total * 2.2 + evening * 0.3), ("Жаворонок", morning * 1.3),
                  ("Дневной режим", day), ("Вечерний человек", evening)), key=lambda x: x[1])[0]
    if S.sent == 0:
        chrono = "Тишина"
    streak, _ = _streak(set(S.per_day))
    busiest = S.per_day.most_common(1)
    night_chat = S.night_by_chat.most_common(1)
    fav_group = S.groups.most_common(1)
    return {
        "label": label, "me": me_name, "sent": S.sent, "received": S.received, "words": S.words,
        "chars": S.chars, "active_days": len(S.per_day), "total_days": total_days,
        "voice_min": round(S.voice_sec / 60), "voice_n": S.voice_n, "round_n": S.round_n,
        "sticker_n": S.sticker_n, "gif_n": S.gif_n, "photo_n": S.photo_n, "video_n": S.video_n,
        "longest": S.longest, "hours": S.hours, "weekdays": S.weekdays, "chrono": chrono,
        "peak_hour": max(range(24), key=lambda h: S.hours[h]) if S.sent else None,
        "busiest": (busiest[0][0].strftime("%d.%m.%Y"), busiest[0][1]) if busiest else None,
        "streak": streak, "words_top": S.word_counter.most_common(12), "emoji_top": S.emoji_counter.most_common(5),
        "people": top, "people_total": len(people), "me_first": me_first, "they_first": they_first,
        "my_resp": _median(S.my_resp), "their_resp": _median(S.their_resp),
        "night_share": round(100 * night_total / S.sent) if S.sent else 0,
        "night_chat": (nm(night_chat[0][0]), night_chat[0][1]) if night_chat else None,
        "fav_group": (fav_group[0][0], fav_group[0][1]) if fav_group else None,
        "chats": S.chats_scanned,
    }


# --------------------------------------------------------------------------- шрифты

def _cache_dir():
    try:
        from core.paths import DATA_DIR
        d = Path(DATA_DIR) / "cache" / "wrapped"
    except Exception:
        d = Path("data") / "cache" / "wrapped"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def ensure_font():
    p = _cache_dir() / "Montserrat-VF.ttf"
    if p.is_file() and p.stat().st_size > 200_000:
        return str(p)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
            async with s.get(FONT_URL) as r:
                if r.status == 200:
                    data = await r.read()
                    if len(data) > 200_000:
                        p.write_bytes(data)
                        return str(p)
    except Exception:
        pass
    return None


class Fonts:
    FALLBACK = {
        True: ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
        False: ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    }

    def __init__(self, vf_path):
        self.vf = vf_path
        self._cache = {}
        self._emoji = {}

    def get(self, size, weight=400):
        key = (size, weight)
        if key in self._cache:
            return self._cache[key]
        from PIL import ImageFont
        f = None
        if self.vf:
            try:
                f = ImageFont.truetype(self.vf, size)
                f.set_variation_by_axes([weight])
            except Exception:
                f = None
        if f is None:
            for c in self.FALLBACK[weight >= 600]:
                try:
                    f = ImageFont.truetype(c, size)
                    break
                except Exception:
                    continue
        if f is None:
            f = ImageFont.load_default()
        self._cache[key] = f
        return f

    def emoji(self, size):
        if size in self._emoji:
            return self._emoji[size]
        from PIL import ImageFont
        res = (None, 0)
        for c, native in (("seguiemj.ttf", None), ("Apple Color Emoji.ttc", 160), ("NotoColorEmoji.ttf", 109),
                          ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 109)):
            try:
                res = (ImageFont.truetype(c, native or size), native or size)
                break
            except Exception:
                continue
        self._emoji[size] = res
        return res


# --------------------------------------------------------------------------- отрисовка

class Card:
    def __init__(self, fonts, page, pages, label):
        from PIL import Image, ImageDraw, ImageFilter
        self.F = fonts
        img = Image.new("RGB", (W, H), BG)
        glow = Image.new("RGB", (W // 4, H // 4), BG)
        gd = ImageDraw.Draw(glow)
        # огненное свечение: правый верх и левый низ, сдвиг зависит от страницы
        ox = (page * 37) % 60
        gd.ellipse((150 + ox, -90, 360 + ox, 110), fill=(120, 38, 12))
        gd.ellipse((-80, 250 - ox, 120, 420 - ox), fill=(95, 16, 38))
        glow = glow.filter(ImageFilter.GaussianBlur(38)).resize((W, H))
        self.img = Image.blend(img, glow, 0.9)
        self.d = ImageDraw.Draw(self.img)
        self.page, self.pages, self.label = page, pages, label
        self._chrome()

    def font(self, size, weight=400):
        return self.F.get(size, weight)

    def text(self, xy, s, size, weight=400, fill=TEXT, anchor="la"):
        self.d.text(xy, s, font=self.font(size, weight), fill=fill, anchor=anchor)

    def width(self, s, size, weight=400):
        return self.d.textlength(s, font=self.font(size, weight))

    def fit(self, s, size, weight, max_w, min_size=22):
        while size > min_size and self.width(s, size, weight) > max_w:
            size -= 2
        if self.width(s, size, weight) > max_w:
            while s and self.width(s + "…", size, weight) > max_w:
                s = s[:-1]
            s += "…"
        return s, size

    def panel(self, box, r=36, fill=PANEL):
        self.d.rounded_rectangle(box, radius=r, fill=fill)

    def gradient_bar(self, box, r=None):
        from PIL import Image, ImageDraw
        x0, y0, x1, y1 = [int(v) for v in box]
        w, h = max(1, x1 - x0), max(1, y1 - y0)
        grad = Image.new("RGB", (w, 1))
        for x in range(w):
            t = x / max(1, w - 1)
            grad.putpixel((x, 0), tuple(int(ACCENT[i] + (ACCENT2[i] - ACCENT[i]) * t) for i in range(3)))
        grad = grad.resize((w, h))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=r if r is not None else h // 2, fill=255)
        self.img.paste(grad, (x0, y0), mask)

    def emoji(self, xy, ch, size):
        """Рисует эмодзи цветным шрифтом; False — шрифта нет."""
        from PIL import Image, ImageDraw
        font, native = self.F.emoji(size)
        if font is None:
            return False
        try:
            tile = Image.new("RGBA", (int(native * 1.4), int(native * 1.4)), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((native * 0.7, native * 0.7), ch, font=font, embedded_color=True, anchor="mm")
            bbox = tile.getbbox()
            if not bbox:
                return False
            tile = tile.crop(bbox)
            k = size / max(tile.size)
            tile = tile.resize((max(1, int(tile.width * k)), max(1, int(tile.height * k))))
            self.img.paste(tile, (int(xy[0] - tile.width / 2), int(xy[1] - tile.height / 2)), tile)
            return True
        except Exception:
            return False

    def _chrome(self):
        self.text((72, 78), "TELEGRAM WRAPPED", 30, 800, ACCENT)
        self.text((W - 72, 78), self.label.upper(), 26, 600, MUTED, anchor="ra")
        self.d.line((72, 128, W - 72, 128), fill=(48, 48, 56), width=2)
        self.text((72, H - 70), "made with FRX", 26, 700, MUTED, anchor="ls")
        self.text((W - 72, H - 70), f"{self.page}/{self.pages}", 26, 600, MUTED, anchor="rs")

    def title(self, s, sub=None, y=170):
        self.text((72, y), s, 76, 800, TEXT)
        if sub:
            self.text((74, y + 98), sub, 32, 500, MUTED)

    def save(self, path):
        self.img.save(path, "PNG", optimize=True)
        return path


def _avatar(card, xy, size, data, name):
    from PIL import Image, ImageDraw
    x, y = xy
    mask = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 2 - 1, size * 2 - 1), fill=255)
    mask = mask.resize((size, size))
    im = None
    if data:
        try:
            im = Image.open(io.BytesIO(data)).convert("RGB")
            s = min(im.size)
            im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2))
            im = im.resize((size, size))
        except Exception:
            im = None
    if im is None:
        im = Image.new("RGB", (size, size), PANEL2)
        d = ImageDraw.Draw(im)
        ch = (name or "?").strip()[:1].upper() or "?"
        d.text((size / 2, size / 2), ch, font=card.font(int(size * 0.45), 800), fill=ACCENT, anchor="mm")
    card.img.paste(im, (x, y), mask)


def render(s, fonts, out_dir, avatars=None):
    avatars = avatars or {}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    pages = 5
    paths = []

    # 1 — обложка
    c = Card(fonts, 1, pages, s["label"])
    hello, hs = c.fit(f"{s['me']},", 44, 600, W - 144)
    c.text((72, 190), hello, hs, 600, MUTED)
    c.text((72, 250), "вот твой Telegram", 64, 800)
    c.text((72, 330), f"за {s['label']}", 64, 800, ACCENT)
    c.panel((72, 450, W - 72, 760), 44)
    big = _num(s["sent"])
    bs = 170
    while c.width(big, bs, 900) > W - 230 and bs > 80:
        bs -= 6
    c.text((116, 470), big, bs, 900)
    c.text((120, 690), _plural(s["sent"], "сообщение ты отправил", "сообщения ты отправил", "сообщений ты отправил"),
           36, 600, MUTED)
    tiles = [
        (_num(s["received"]), "получено"),
        (f"{s['active_days']}/{s['total_days']}", "дней в сети"),
        (_num(s["words"]), _plural(s["words"], "слово", "слова", "слов")),
        (_num(s["voice_min"]), "минут голосовых"),
    ]
    tw = (W - 144 - 30) // 2
    for i, (v, lbl) in enumerate(tiles):
        x = 72 + (i % 2) * (tw + 30)
        y = 790 + (i // 2) * 210
        c.panel((x, y, x + tw, y + 180), 36)
        v2, vs = c.fit(v, 72, 800, tw - 70)
        c.text((x + 36, y + 30), v2, vs, 800, GOLD if i == 1 else TEXT)
        c.text((x + 38, y + 122), lbl, 30, 500, MUTED)
    paths.append(c.save(out / f"wrapped_{stamp}_1.png"))

    # 2 — люди
    c = Card(fonts, 2, pages, s["label"])
    c.title("Твои люди", f"{s['people_total']} {_plural(s['people_total'], 'собеседник', 'собеседника', 'собеседников')} в личке")
    y = 330
    people = s["people"]
    mx = max([p["total"] for p in people] or [1])
    if not people:
        c.text((72, y + 40), "В личке за этот период тихо.", 40, 600, MUTED)
    for rank, p in enumerate(people, 1):
        c.panel((72, y, W - 72, y + 150), 34, PANEL if rank > 1 else PANEL2)
        _avatar(c, (100, y + 25), 100, avatars.get(p["id"]), p["name"])
        nm, ns = c.fit(p["name"], 40, 700, W - 72 - 230 - 230)
        c.text((228, y + 26), nm, ns, 700)
        c.text((W - 104, y + 26), _num(p["total"]), 40, 800, ACCENT if rank == 1 else TEXT, anchor="ra")
        bar_w = int((W - 72 - 228 - 32) * p["total"] / mx)
        c.d.rounded_rectangle((228, y + 84, W - 104, y + 96), radius=6, fill=(46, 46, 54))
        c.gradient_bar((228, y + 84, 228 + max(bar_w, 14), y + 96))
        sub = f"первым пишет {p['first']}"
        if p["resp"] is not None:
            sub += f" · ты отвечаешь за ~{_dur(p['resp'])}"
        sub, ss = c.fit(sub, 26, 500, W - 104 - 228)
        c.text((228, y + 108), sub, ss, 500, MUTED)
        y += 170
    if s["me_first"] + s["they_first"]:
        share = round(100 * s["me_first"] / (s["me_first"] + s["they_first"]))
        c.text((72, H - 150), f"Разговор начинаешь ты в {share}% случаев", 34, 700, GOLD)
    paths.append(c.save(out / f"wrapped_{stamp}_2.png"))

    # 3 — время
    c = Card(fonts, 3, pages, s["label"])
    c.title("Когда ты пишешь", None)
    c.text((72, 280), s["chrono"], 60, 900, ACCENT)
    if s["peak_hour"] is not None:
        c.text((72, 362), f"пик — {s['peak_hour']:02d}:00–{(s['peak_hour'] + 1) % 24:02d}:00", 34, 600, MUTED)
    # часы
    c.panel((72, 430, W - 72, 820), 40)
    hx0, hx1, hy0, hy1 = 110, W - 110, 470, 740
    mh = max(s["hours"]) or 1
    bw = (hx1 - hx0) / 24
    for h, v in enumerate(s["hours"]):
        bh = max(6, int((hy1 - hy0) * v / mh))
        x0 = int(hx0 + h * bw + 4)
        x1 = int(hx0 + (h + 1) * bw - 4)
        if h == s["peak_hour"]:
            c.gradient_bar((x0, hy1 - bh, x1, hy1), r=8)
        else:
            c.d.rounded_rectangle((x0, hy1 - bh, x1, hy1), radius=8, fill=(70, 70, 82) if v else (45, 45, 52))
        if h % 3 == 0:
            c.text(((x0 + x1) / 2, hy1 + 22), f"{h:02d}", 24, 600, MUTED, anchor="ma")
    # дни недели
    c.panel((72, 850, W - 72, 1160), 40)
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    mw = max(s["weekdays"]) or 1
    top_wd = max(range(7), key=lambda i: s["weekdays"][i])
    for i, v in enumerate(s["weekdays"]):
        cx = 72 + 70 + i * ((W - 144 - 140) / 6)
        bh = max(8, int(170 * v / mw))
        if i == top_wd and v:
            c.gradient_bar((cx - 32, 1080 - bh, cx + 32, 1080), r=14)
        else:
            c.d.rounded_rectangle((cx - 32, 1080 - bh, cx + 32, 1080), radius=14, fill=(70, 70, 82))
        c.text((cx, 1100), names[i], 26, 700, TEXT if i == top_wd else MUTED, anchor="ma")
    if s["busiest"]:
        c.text((72, 1200), f"Самый бурный день — {s['busiest'][0]}: {_num(s['busiest'][1])} сообщ.", 32, 700, GOLD)
    paths.append(c.save(out / f"wrapped_{stamp}_3.png"))

    # 4 — словарь
    c = Card(fonts, 4, pages, s["label"])
    c.title("Твой словарь", f"{_num(s['words'])} {_plural(s['words'], 'слово', 'слова', 'слов')} · {_num(s['chars'])} символов")
    x, y = 72, 340
    words = s["words_top"]
    if not words:
        c.text((72, y), "Слов почти не было — ты человек стикеров.", 38, 600, MUTED)
    for i, (w, n) in enumerate(words):
        size = 54 if i == 0 else 44 if i < 3 else 36
        weight = 800 if i < 3 else 600
        label = f"{w}"
        cnt = f" {_num(n)}"
        tw_ = c.width(label, size, weight) + c.width(cnt, 26, 600) + 64
        if x + tw_ > W - 72:
            x = 72
            y += size + 52
        if y > 820:
            break
        fill = PANEL2 if i else None
        if i == 0:
            c.gradient_bar((x, y, x + tw_, y + size + 34), r=(size + 34) // 2)
        else:
            c.d.rounded_rectangle((x, y, x + tw_, y + size + 34), radius=(size + 34) // 2, fill=fill)
        c.text((x + 32, y + 17), label, size, weight, TEXT)
        c.text((x + 32 + c.width(label, size, weight), y + 17 + size - 26), cnt, 26, 600,
               TEXT if i == 0 else MUTED)
        x += tw_ + 18
    ey = max(y + 140, 880)
    c.text((72, ey), "Любимые эмодзи", 40, 800)
    em = s["emoji_top"]
    if em and fonts.emoji(96)[0] is None:
        # нет цветного шрифта эмодзи (не Windows/macOS) — без «квадратиков»
        c.text((72, ey + 70), f"Поставлено эмодзи: {_num(sum(n for _, n in em))}+", 34, 600, MUTED)
        em = []
    elif not em:
        c.text((72, ey + 70), "Эмодзи ты почти не ставишь. Строго.", 34, 600, MUTED)
    else:
        slot = (W - 144) / 5
        for i, (e, n) in enumerate(em):
            cx = 72 + slot * i + slot / 2
            c.panel((cx - slot / 2 + 10, ey + 70, cx + slot / 2 - 10, ey + 300), 34)
            if not c.emoji((cx, ey + 160), e, 96):
                c.text((cx, ey + 160), e, 60, 600, anchor="mm")
            c.text((cx, ey + 240), _num(n), 32, 800, ACCENT if i == 0 else TEXT, anchor="ma")
    paths.append(c.save(out / f"wrapped_{stamp}_4.png"))

    # 5 — факты
    c = Card(fonts, 5, pages, s["label"])
    c.title("Факты", f"просмотрено чатов: {s['chats']}")
    facts = [
        (_num(s["round_n"]), "кружков записал"),
        (_num(s["sticker_n"]), "стикеров отправил"),
        (_num(s["photo_n"] + s["video_n"]), "фото и видео"),
        (f"{s['streak']} {_plural(s['streak'], 'день', 'дня', 'дней')}", "лучший стрик подряд"),
        (_num(s["longest"]), "символов в самом длинном"),
        (_dur(s["my_resp"]) if s["my_resp"] is not None else "—", "твой обычный ответ"),
        (f"{s['night_share']}%", "сообщений с 00 до 05"),
        (_dur(s["their_resp"]) if s["their_resp"] is not None else "—", "отвечают тебе"),
    ]
    tw = (W - 144 - 30) // 2
    for i, (v, lbl) in enumerate(facts):
        x = 72 + (i % 2) * (tw + 30)
        y = 320 + (i // 2) * 190
        c.panel((x, y, x + tw, y + 165), 34)
        v2, vs = c.fit(v, 60, 800, tw - 64)
        c.text((x + 32, y + 26), v2, vs, 800, ACCENT if i in (3, 6) else TEXT)
        l2, ls = c.fit(lbl, 28, 500, tw - 64)
        c.text((x + 34, y + 112), l2, ls, 500, MUTED)
    yy = 320 + 4 * 190 + 10
    lines = []
    if s["night_chat"]:
        lines.append(f"Самый ночной чат — {s['night_chat'][0]}")
    if s["fav_group"]:
        lines.append(f"Любимая группа — {s['fav_group'][0]} ({_num(s['fav_group'][1])})")
    for ln in lines[:2]:
        t, ts = c.fit(ln, 32, 700, W - 144)
        c.text((72, yy), t, ts, 700, GOLD)
        yy += 56
    paths.append(c.save(out / f"wrapped_{stamp}_5.png"))
    return [str(p) for p in paths]


# --------------------------------------------------------------------------- команда

@decorators.on_load()
async def _setup(module_info):
    if module_info.module_name != MODULE_NAME:
        return
    await engine.create_config("max_dialogs", "Сколько последних диалогов просматривать", "int", 60,
                               valid={"min_value": 5, "max_value": 400})
    await engine.create_config("max_per_chat", "Максимум сообщений на один чат", "int", 3000,
                               valid={"min_value": 100, "max_value": 50000})
    await engine.create_config("groups", "Учитывать твои сообщения в группах", "bool", True)
    await engine.create_config("hide_names", "Прятать имена собеседников на карточках (А•••)", "bool", False)
    await engine.create_config("send_to", "Куда присылать карточки", "choice", "этот чат",
                               options=["этот чат", "Избранное"])


@decorators.on_unload()
async def _unload():
    _state["cancel"] = True


@decorators.command("wrapped", "итоги")
async def wrapped_cmd(event):
    """итоги Telegram карточками: [период] | here [период] | stop"""
    args = engine.get_args().split()
    if args and args[0].lower() in ("stop", "стоп"):
        if _state["running"]:
            _state["cancel"] = True
            await engine.answer(event, visual.wait("Останавливаю подсчёт…"))
        else:
            await engine.answer(event, visual.hint("подсчёт не запущен"))
        return
    if _state["running"]:
        await engine.answer(event, visual.warn("Wrapped уже считается — дождитесь или .wrapped stop"))
        return

    only_chat = None
    if args and args[0].lower() in ("here", "тут", "чат"):
        only_chat = event.chat_id
        args = args[1:]
    try:
        start, label = parse_period(args[0] if args else None)
    except ValueError as e:
        await engine.answer(event, visual.err(str(e)))
        return

    cfg = {
        "max_dialogs": int(await engine.get_config("max_dialogs") or 60),
        "max_per_chat": int(await engine.get_config("max_per_chat") or 3000),
        "groups": bool(await engine.get_config("groups")),
    }
    hide = bool(await engine.get_config("hide_names"))
    send_to = await engine.get_config("send_to") or "этот чат"

    _state.update(running=True, cancel=False)
    t0 = time.monotonic()
    client = engine.client
    try:
        await engine.answer(event, visual.wait(f"Собираю итоги за {label}… это может занять пару минут"))

        async def progress(done, total, name):
            bar = engine.progress_bar(100 * done / max(1, total), 12, "bar", show_percent=True)
            line = f"\n{visual.hint('сейчас:')} {_esc(name)}" if name else ""
            try:
                await engine.answer(event, "\n".join([
                    visual.title("Wrapped", sub=f"за {label}"),
                    f"{bar}  <code>{done}/{total}</code>{line}",
                    visual.hint("остановить — .wrapped stop"),
                ]))
            except Exception:
                pass

        S = await collect(client, start, cfg, only_chat=only_chat, progress=progress)
        if S.sent == 0 and S.received == 0:
            await engine.answer(event, visual.warn(f"За {label} сообщений не нашлось"))
            return

        me = await client.get_me()
        total_days = max(1, (dt.datetime.now(dt.timezone.utc) - start).days)
        summary = summarize(S, label, _name(me), total_days, hide_names=hide)

        await engine.answer(event, visual.wait("Рисую карточки…"))
        avatars = {}
        if not hide:
            for p in summary["people"]:
                ent = S.entities.get(p["id"])
                if ent is None:
                    continue
                try:
                    data = await client.download_profile_photo(ent, file=bytes, download_big=False)
                    if data:
                        avatars[p["id"]] = data
                except Exception:
                    pass
        fonts = Fonts(await ensure_font())
        out_dir = _cache_dir() / "cards"
        paths = await asyncio.to_thread(render, summary, fonts, out_dir, avatars)

        caption = (f"<b>Мой Telegram Wrapped</b> · за {_esc(label)}\n"
                   f"{_num(summary['sent'])} отправлено · {_num(summary['received'])} получено · "
                   f"{summary['active_days']} {_plural(summary['active_days'], 'день', 'дня', 'дней')} в сети\n"
                   f"<i>made with FRX</i>")
        target = "me" if send_to == "Избранное" else event.chat_id
        await client.send_file(target, paths, caption=[caption] + [""] * (len(paths) - 1), parse_mode="html")
        took = int(time.monotonic() - t0)
        if send_to == "Избранное":
            await engine.answer(event, visual.ok(f"Wrapped готов — в «Избранном» ({_dur(took)})"))
        else:
            try:
                await event.delete()
            except Exception:
                await engine.answer(event, visual.ok(f"Wrapped готов ({_dur(took)})"))
        for p in paths:
            try:
                Path(p).unlink()
            except Exception:
                pass
    except _Stopped:
        await engine.answer(event, visual.warn("Подсчёт остановлен"))
    finally:
        _state.update(running=False, cancel=False)
