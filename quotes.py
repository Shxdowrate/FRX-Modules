"""
Quotes — красивые цитаты из сообщений: картинкой или стикером, как QuotLy, только без внешних ботов.

  .q                ответом на сообщение — цитата-стикер
  .q 3              три сообщения подряд, начиная с того, на которое ответили (до 20)
  .qs [3]           то же — и сразу в ваш стикерпак «Цитаты» (пак создаётся сам, отправляется стикер из пака)
  .qp [3]           картинкой (PNG, крупнее) вместо стикера
  .qset             настройки: фон, аватарки, ответы, пак

Рисуется по-телеграмовски: аватарка, имя в цвете пользователя, пузыри, «в ответ на…», форматирование
(жирный, курсив, код, ссылки, спойлер), эмодзи (Twemoji), премиум-эмодзи, фото и стикеры в сообщениях.
Подряд идущие сообщения одного человека склеиваются в одну группу, как в Telegram.
"""

import html
import io
import re
from pathlib import Path

import aiohttp

import decorators
import engine
import media
import visual

NAME = "Quotes"
MODULE_NAME = "quotes"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Цитаты из сообщений картинкой или стикером: .q ответом, .qs — сразу в ваш стикерпак"

S = 2                     # рисуем в 2× — потом сжимаем до 512 (стикер) — так текст чёткий
MAX_W = 512 * S
NAME_COLORS = ['#FF7A75', '#FFAC5C', '#B48BF2', '#6FD08A', '#5FC9E8', '#6AB3F3', '#FF77B4']
COLOR_ORDER = [0, 6, 3, 1, 5, 2, 4]           # как Telegram раскладывает цвета по id
BG = {'dark': ((30, 39, 50), (245, 245, 245), (106, 179, 243), (140, 155, 170)),
      'light': ((255, 255, 255), (20, 20, 20), (36, 129, 204), (120, 130, 140)),
      'black': ((22, 22, 24), (240, 240, 240), (120, 170, 255), (130, 130, 135)),
      'frx': ((34, 24, 22), (250, 236, 228), (255, 138, 80), (170, 140, 128))}


def _esc(x):
    return html.escape(str(x), quote=False)


def _hex(c):
    c = c.lstrip('#')
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


@decorators.on_start()
async def setup():
    await engine.create_config(key='theme', description='Цвет пузырей', data_type='choice', default='dark',
                               options=list(BG))
    await engine.create_config(key='avatars', description='Показывать аватарки', data_type='bool', default=True)
    await engine.create_config(key='replies', description='Показывать, на что человек отвечал', data_type='bool', default=True)
    await engine.create_config(key='media', description='Показывать фото и стикеры из сообщений', data_type='bool', default=True)
    await engine.create_config(key='pack_title', description='Название вашего пака цитат', data_type='str', default='Цитаты')
    await engine.create_config(key='pack_emoji', description='Эмодзи, под которым стикер-цитата лежит в паке',
                               data_type='str', default='💬')


async def cfg(key):
    try:
        return await engine.get_config(key, module_name=MODULE_NAME)
    except KeyError:
        return None


# --------------------------------------------------------------------------- шрифты и эмодзи

def _cache():
    try:
        from core.paths import DATA_DIR
        d = Path(DATA_DIR) / 'cache' / 'quotes'
    except Exception:
        d = Path('data') / 'cache' / 'quotes'
    d.mkdir(parents=True, exist_ok=True)
    return d


FONT_URL = 'https://raw.githubusercontent.com/google/fonts/main/ofl/notosans/NotoSans%5Bwdth,wght%5D.ttf'
MONO_URL = 'https://raw.githubusercontent.com/JetBrains/JetBrainsMono/master/fonts/ttf/JetBrainsMono-Regular.ttf'
EMOJI_URLS = ['https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72/{}.png',
              'https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{}.png']
_fonts = {}


async def _download(url, path):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.read()
                    if data:
                        path.write_bytes(data)
                        return True
    except Exception:
        pass
    return False


async def _ensure_fonts():
    d = _cache()
    for fn, url in (('NotoSans.ttf', FONT_URL), ('Mono.ttf', MONO_URL)):
        p = d / fn
        if not p.is_file() or p.stat().st_size < 50_000:
            await _download(url, p)


def font(size, weight='Regular', mono=False):
    key = (size, weight, mono)
    if key in _fonts:
        return _fonts[key]
    from PIL import ImageFont
    d = _cache()
    f = None
    if mono:
        for c in (d / 'Mono.ttf', 'consola.ttf', 'DejaVuSansMono.ttf'):
            try:
                f = ImageFont.truetype(str(c), size)
                break
            except Exception:
                continue
    else:
        try:
            f = ImageFont.truetype(str(d / 'NotoSans.ttf'), size)
            f.set_variation_by_name(weight)
        except Exception:
            f = None
            fallback = {'Bold': ('segoeuib.ttf', 'arialbd.ttf', 'DejaVuSans-Bold.ttf'),
                        'Italic': ('segoeuii.ttf', 'ariali.ttf', 'DejaVuSans-Oblique.ttf')}.get(weight, ())
            for c in fallback + ('segoeui.ttf', 'arial.ttf', 'DejaVuSans.ttf'):
                try:
                    f = ImageFont.truetype(c, size)
                    break
                except Exception:
                    continue
    f = f or ImageFont.load_default()
    _fonts[key] = f
    return f


def _is_emoji_cp(cp):
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF or 0x2300 <= cp <= 0x23FF
            or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299)
            or 0x2190 <= cp <= 0x21FF and cp in (0x2194, 0x2195, 0x2196, 0x2197, 0x2198, 0x2199, 0x21A9, 0x21AA))


def split_emoji(text):
    """Текст → [(кусок, is_emoji)]: эмодзи — целыми последовательностями (ZWJ, тон кожи, флаги, клавиши)."""
    out, buf, i, n = [], '', 0, len(text)
    while i < n:
        ch = text[i]
        cp = ord(ch)
        is_key = ch in '0123456789#*' and i + 1 < n and text[i + 1] in '️⃣' and (text[i + 1] == '⃣' or (i + 2 < n and text[i + 2] == '⃣'))
        if _is_emoji_cp(cp) or 0x1F1E6 <= cp <= 0x1F1FF or is_key:
            if buf:
                out.append((buf, False))
                buf = ''
            j = i + 1
            if 0x1F1E6 <= cp <= 0x1F1FF and j < n and 0x1F1E6 <= ord(text[j]) <= 0x1F1FF:
                j += 1                                     # флаг — пара региональных букв
            while j < n:
                c2 = ord(text[j])
                if c2 in (0xFE0F, 0x20E3) or 0x1F3FB <= c2 <= 0x1F3FF or 0xE0020 <= c2 <= 0xE007F:
                    j += 1
                elif c2 == 0x200D and j + 1 < n:
                    j += 2
                else:
                    break
            out.append((text[i:j], True))
            i = j
        else:
            buf += ch
            i += 1
    if buf:
        out.append((buf, False))
    return out


_emoji_mem = {}


async def emoji_image(seq):
    """PNG эмодзи Twemoji (кэш на диске) → PIL.Image | None."""
    from PIL import Image
    cps = [ord(c) for c in seq]
    if 0x200D not in cps:
        cps = [c for c in cps if c != 0xFE0F]
    code = '-'.join(f"{c:x}" for c in cps)
    if code in _emoji_mem:
        return _emoji_mem[code]
    p = _cache() / 'emoji' / f"{code}.png"
    p.parent.mkdir(exist_ok=True)
    if not p.is_file():
        for u in EMOJI_URLS:
            if await _download(u.format(code), p):
                break
    img = None
    if p.is_file():
        try:
            img = Image.open(p).convert('RGBA')
        except Exception:
            img = None
    _emoji_mem[code] = img
    return img


# --------------------------------------------------------------------------- разбор сообщений

class Run:
    __slots__ = ('text', 'bold', 'italic', 'mono', 'strike', 'under', 'link', 'spoiler', 'emoji', 'custom')

    def __init__(self, text, **kw):
        self.text = text
        for k in self.__slots__[1:]:
            setattr(self, k, kw.get(k, False))


def styled_runs(text, entities):
    """Текст + entities Telegram (смещения в UTF-16) → [Run]."""
    from telethon.helpers import add_surrogate, del_surrogate
    from telethon.tl import types as T
    st = add_surrogate(text or '')
    n = len(st)
    flags = [dict() for _ in range(n)]
    for e in entities or []:
        key = {T.MessageEntityBold: 'bold', T.MessageEntityItalic: 'italic', T.MessageEntityCode: 'mono',
               T.MessageEntityPre: 'mono', T.MessageEntityStrike: 'strike', T.MessageEntityUnderline: 'under',
               T.MessageEntitySpoiler: 'spoiler', T.MessageEntityUrl: 'link', T.MessageEntityTextUrl: 'link',
               T.MessageEntityMention: 'link', T.MessageEntityMentionName: 'link', T.MessageEntityHashtag: 'link',
               T.MessageEntityEmail: 'link', T.MessageEntityBotCommand: 'link', T.MessageEntityCashtag: 'link',
               T.MessageEntityPhone: 'link', T.MessageEntityCustomEmoji: 'custom'}.get(type(e))
        if not key:
            continue
        for k in range(e.offset, min(n, e.offset + e.length)):
            flags[k][key] = e.document_id if key == 'custom' else True
    runs, i = [], 0
    while i < n:
        j = i + 1
        while j < n and flags[j] == flags[i]:
            j += 1
        while j < n and '\udc00' <= st[j] <= '\udfff':     # не рвём суррогатную пару посередине
            j += 1
        piece = del_surrogate(st[i:j])
        f = flags[i]
        if f.get('custom'):
            runs.append(Run(piece, custom=f['custom'], emoji=True))
        else:
            for part, is_e in split_emoji(piece):
                runs.append(Run(part, emoji=is_e, **{k: v for k, v in f.items() if k != 'custom'}))
        i = j
    return runs


def _name(ent):
    if ent is None:
        return 'Удалённый аккаунт'
    if getattr(ent, 'title', None):
        return ent.title
    return ' '.join(x for x in (getattr(ent, 'first_name', '') or '', getattr(ent, 'last_name', '') or '') if x) or 'Без имени'


def _color(ent):
    pc = getattr(ent, 'color', None)
    idx = getattr(pc, 'color', None) if pc is not None else None
    if isinstance(idx, int) and 0 <= idx < 7:
        return _hex(NAME_COLORS[COLOR_ORDER[idx]])
    eid = abs(getattr(ent, 'id', 0) or 0)
    return _hex(NAME_COLORS[COLOR_ORDER[eid % 7]])


async def _avatar(ent, size):
    from PIL import Image, ImageDraw
    img = None
    if ent is not None:
        try:
            data = await engine.client.download_profile_photo(ent, file=bytes, download_big=False)
            if data:
                img = Image.open(io.BytesIO(data)).convert('RGB').resize((size, size), Image.LANCZOS)
        except Exception:
            img = None
    if img is None:                                     # нет фото — кружок с инициалами, как в Telegram
        col = _color(ent)
        img = Image.new('RGB', (size, size), col)
        top = tuple(min(255, c + 40) for c in col)
        grad = Image.linear_gradient('L').resize((size, size))
        img = Image.composite(Image.new('RGB', (size, size), top), img, grad.transpose(Image.FLIP_TOP_BOTTOM))
        d = ImageDraw.Draw(img)
        initials = ''.join(w[:1] for w in _name(ent).split()[:2]).upper() or '?'
        f = font(int(size * 0.4), 'SemiBold')
        tw = d.textlength(initials, font=f)
        d.text(((size - tw) / 2, size * 0.27), initials, font=f, fill=(255, 255, 255))
    mask = Image.new('L', (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size * 4, size * 4], fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


async def _custom_emoji(doc_ids):
    """Премиум-эмодзи → {document_id: PIL.Image} (статичная картинка или первый кадр / превью)."""
    from PIL import Image
    from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
    out = {}
    ids = list({i for i in doc_ids if i})
    if not ids:
        return out
    try:
        docs = await engine.client(GetCustomEmojiDocumentsRequest(document_id=ids))
    except Exception:
        return out
    for doc in docs:
        p = _cache() / 'custom' / f"{doc.id}.png"
        p.parent.mkdir(exist_ok=True)
        if not p.is_file():
            try:
                if doc.mime_type == 'image/webp':
                    data = await engine.client.download_media(doc, file=bytes)
                else:                                   # tgs / webm — берём превью
                    data = await engine.client.download_media(doc, file=bytes, thumb=-1)
                if data:
                    Image.open(io.BytesIO(data)).convert('RGBA').save(p)
            except Exception:
                continue
        try:
            out[doc.id] = Image.open(p).convert('RGBA')
        except Exception:
            pass
    return out


async def _media_thumb(msg, max_w):
    """Фото / стикер / видео / gif в сообщении → картинка-превью."""
    from PIL import Image
    if not (msg.photo or msg.sticker or msg.video or msg.gif or (msg.document and (msg.file.mime_type or '').startswith('image/'))):
        return None
    try:
        if msg.photo or (msg.document and (msg.file.mime_type or '').startswith('image/')) and not msg.sticker:
            data = await engine.client.download_media(msg, file=bytes, thumb=-1 if msg.photo else None)
        elif msg.sticker and (msg.file.mime_type or '') == 'image/webp':
            data = await engine.client.download_media(msg, file=bytes)
        else:
            data = await engine.client.download_media(msg, file=bytes, thumb=-1)
        if not data:
            return None
        im = Image.open(io.BytesIO(data)).convert('RGBA')
    except Exception:
        return None
    limit = max_w if not msg.sticker else int(max_w * 0.6)
    k = min(1.0, limit / im.width, (limit * 1.2) / im.height)
    k = max(k, min(1.0, 260 * S / max(im.width, im.height)))
    return im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)


def _media_label(msg):
    if msg.voice:
        return f"🎤 Голосовое сообщение {int(getattr(msg.file, 'duration', 0) or 0) // 60}:{int(getattr(msg.file, 'duration', 0) or 0) % 60:02}"
    if msg.video_note:
        return "📹 Видеосообщение"
    if msg.audio:
        f = msg.file
        return f"🎵 {getattr(f, 'performer', '') or ''} — {getattr(f, 'title', '') or f.name or 'Аудио'}".replace(' —  ', ' ')
    if msg.poll:
        return f"📊 {msg.poll.poll.question.text if hasattr(msg.poll.poll.question, 'text') else msg.poll.poll.question}"
    if msg.geo:
        return "📍 Геопозиция"
    if msg.contact:
        return "👤 Контакт"
    if msg.document and not (msg.sticker or msg.gif or msg.video):
        return f"📎 {msg.file.name or 'Файл'}"
    return None


# --------------------------------------------------------------------------- вёрстка

def layout(runs, max_w, size, emoji_map):
    """Перенос строк по словам → [[(Run-кусок, x, ширина)]] и высота строки."""
    from PIL import ImageDraw, Image
    d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    lines, cur, x = [], [], 0
    lh = int(size * 1.36)

    def fnt(r):
        return font(size, 'Bold' if r.bold else ('Italic' if r.italic else 'Regular'), mono=r.mono)

    def width(r, t):
        if r.emoji:
            return int(size * 1.18)
        return d.textlength(t, font=fnt(r))

    for r in runs:
        if r.emoji:
            w = width(r, r.text)
            if x + w > max_w and cur:
                lines.append(cur)
                cur, x = [], 0
            cur.append((r, r.text, x, w))
            x += w
            continue
        parts = re.split(r'(\n| )', r.text)
        for p in parts:
            if p == '':
                continue
            if p == '\n':
                lines.append(cur)
                cur, x = [], 0
                continue
            w = width(r, p)
            if p == ' ' and not cur:
                continue
            if x + w > max_w and cur:
                lines.append(cur)
                cur, x = [], 0
                if p == ' ':
                    continue
            while w > max_w:                              # очень длинное слово — режем
                k = max(1, int(len(p) * max_w / w) - 1)
                cur.append((r, p[:k], x, width(r, p[:k])))
                lines.append(cur)
                cur, x = [], 0
                p = p[k:]
                w = width(r, p)
            cur.append((r, p, x, w))
            x += w
    if cur:
        lines.append(cur)
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines, lh, fnt


async def render(groups, theme='dark', avatars=True, replies=True, show_media=True):
    """groups: [{'ent', 'msgs': [Message…]}] → PIL.Image (RGBA, прозрачный фон)."""
    from PIL import Image, ImageDraw, ImageFilter
    await _ensure_fonts()
    bg, fg, link_c, muted = BG.get(theme, BG['dark'])
    size, name_size = 17 * S, 15 * S
    pad, gap, av = 11 * S, 5 * S, 44 * S
    left = (av + 8 * S) if avatars else 0
    bubble_max = MAX_W - left - 4 * S
    text_max = bubble_max - pad * 2
    custom_ids = []
    for g in groups:
        for m in g['msgs']:
            custom_ids += [e.document_id for e in (m.entities or []) if hasattr(e, 'document_id')]
    custom = await _custom_emoji(custom_ids)
    blocks = []                                          # что рисовать: по пузырю на сообщение
    for g in groups:
        col = _color(g['ent'])
        for i, m in enumerate(g['msgs']):
            b = {'msg': m, 'first': i == 0, 'last': i == len(g['msgs']) - 1, 'ent': g['ent'], 'color': col}
            runs = styled_runs(m.raw_text or '', m.entities)
            label = _media_label(m)
            if label and not runs:
                runs = styled_runs(label, [])
                for r in runs:
                    r.italic = not r.emoji
            b['lines'], b['lh'], b['fnt'] = layout(runs, text_max, size, custom)
            b['thumb'] = await _media_thumb(m, text_max) if show_media else None
            b['reply'] = None
            if replies and i == 0 and m.is_reply:
                try:
                    r = await m.get_reply_message()
                    if r is not None:
                        rs = await r.get_sender()
                        snippet = (r.raw_text or _media_label(r) or ('Фото' if r.photo else 'Стикер' if r.sticker else 'Сообщение'))
                        b['reply'] = (_name(rs), _color(rs), ' '.join(snippet.split())[:70])
                except Exception:
                    pass
            name_w = ImageDraw.Draw(Image.new('RGB', (1, 1))).textlength(_name(g['ent']), font=font(name_size, 'Bold')) if b['first'] else 0
            text_w = max([ln[-1][2] + ln[-1][3] for ln in b['lines'] if ln] + [0])
            reply_w = 0
            if b['reply']:
                fdraw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
                reply_w = min(text_max, max(fdraw.textlength(b['reply'][0], font=font(14 * S, 'Bold')),
                                             fdraw.textlength(b['reply'][2], font=font(14 * S)))) + 14 * S
            thumb_w = b['thumb'].width if b['thumb'] is not None else 0
            inner_w = max(text_w, name_w, reply_w, thumb_w, 40 * S)
            b['w'] = min(bubble_max, int(inner_w + pad * 2))
            h = pad
            if b['first']:
                h += int(name_size * 1.4)
            if b['reply']:
                h += int(14 * S * 2.9) + 4 * S
            if b['thumb'] is not None:
                h += b['thumb'].height + (6 * S if b['lines'] else 0)
            h += len(b['lines']) * b['lh']
            h += pad - (4 * S if b['lines'] else 0)
            b['h'] = int(h)
            blocks.append(b)
    total_w = max(left + b['w'] for b in blocks) + 4 * S
    total_h = sum(b['h'] for b in blocks) + gap * (len(blocks) - 1) + 4 * S
    img = Image.new('RGBA', (int(total_w), int(total_h)), (0, 0, 0, 0))
    # тень пузырей
    shadow = Image.new('RGBA', img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    y = 0
    for b in blocks:
        sd.rounded_rectangle([left, y + 2 * S, left + b['w'], y + b['h'] + 2 * S], 16 * S, fill=(0, 0, 0, 70))
        y += b['h'] + gap
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(3 * S)))
    d = ImageDraw.Draw(img)
    y = 0
    for b in blocks:
        x0 = left
        r = 16 * S
        d.rounded_rectangle([x0, y, x0 + b['w'], y + b['h']], r, fill=bg)
        if b['last']:                                    # «хвостик» пузыря у последнего сообщения группы
            d.polygon([(x0 - 7 * S, y + b['h']), (x0 + 12 * S, y + b['h'] - 14 * S), (x0 + 12 * S, y + b['h'])], fill=bg)
        cy = y + pad
        if b['first']:
            d.text((x0 + pad, cy - 3 * S), _name(b['ent']), font=font(name_size, 'Bold'), fill=b['color'])
            cy += int(name_size * 1.4)
        if b['reply']:
            rn, rc, rt = b['reply']
            bar_h = int(14 * S * 2.6)
            d.rounded_rectangle([x0 + pad, cy, x0 + b['w'] - pad, cy + bar_h], 5 * S, fill=tuple(int(c * 0.18 + bg[i] * 0.82) for i, c in enumerate(rc)))
            d.rectangle([x0 + pad, cy, x0 + pad + 3 * S, cy + bar_h], fill=rc)
            d.text((x0 + pad + 10 * S, cy + 3 * S), rn[:40], font=font(14 * S, 'Bold'), fill=rc)
            rt_f = font(14 * S)
            while rt and d.textlength(rt, font=rt_f) > b['w'] - pad * 2 - 16 * S:
                rt = rt[:-2]
            d.text((x0 + pad + 10 * S, cy + 3 * S + int(14 * S * 1.25)), rt, font=rt_f, fill=fg)
            cy += bar_h + 4 * S + int(14 * S * 0.3)
        if b['thumb'] is not None:
            th = b['thumb']
            m = Image.new('L', th.size, 0)
            ImageDraw.Draw(m).rounded_rectangle([0, 0, th.width, th.height], 10 * S, fill=255)
            if th.mode == 'RGBA':
                m = Image.composite(th.getchannel('A'), Image.new('L', th.size, 0), m)
            img.paste(th, (x0 + pad, cy), m)
            cy += th.height + (6 * S if b['lines'] else 0)
        for ln in b['lines']:
            for run, t, lx, w in ln:
                px, py = x0 + pad + lx, cy
                if run.emoji:
                    em = custom.get(run.custom) if run.custom else await emoji_image(t)
                    if em is not None:
                        es = int(size * 1.12)
                        img.alpha_composite(em.resize((es, es), Image.LANCZOS), (int(px + size * 0.03), int(py + size * 0.12)))
                    continue
                f = b['fnt'](run)
                col = link_c if run.link else fg
                if run.mono:
                    col = (230, 160, 120) if theme in ('dark', 'black', 'frx') else (190, 70, 40)
                if run.spoiler:
                    d.rounded_rectangle([px, py + 4 * S, px + w, py + b['lh'] - 2 * S], 4 * S, fill=tuple(min(255, c + 45) for c in bg))
                    continue
                d.text((px, py), t, font=f, fill=col)
                if run.strike:
                    d.line([px, py + b['lh'] * 0.52, px + w, py + b['lh'] * 0.52], fill=col, width=S)
                if run.under:
                    d.line([px, py + b['lh'] * 0.86, px + w, py + b['lh'] * 0.86], fill=col, width=S)
            cy += b['lh']
        if b['last'] and avatars:
            avi = await _avatar(b['ent'], av)
            img.alpha_composite(avi, (0, int(y + b['h'] - av)))
        y += b['h'] + gap
    return img


def to_sticker(img):
    """→ webp 512 по большей стороне (правила стикеров Telegram)."""
    from PIL import Image
    k = 512 / max(img.width, img.height)
    im = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'WEBP', quality=92, method=6)
    return buf.getvalue(), im.size


def to_png(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- команды

async def collect(event, count):
    """Сообщения для цитаты: то, на которое ответили, и следующие за ним (count штук), без команд юзербота."""
    r = await event.get_reply_message() if event.is_reply else None
    if r is None:
        return None
    msgs = [r]
    if count > 1:
        async for m in engine.client.iter_messages(event.chat_id, min_id=r.id, reverse=True, limit=count * 3):
            if m.id == event.id:
                break
            if m.action is not None:
                continue
            msgs.append(m)
            if len(msgs) >= count:
                break
    groups = []
    for m in msgs:
        sender = await m.get_sender()
        if m.fwd_from and m.fwd_from.from_id:                    # пересланное — цитируем автора оригинала
            try:
                sender = await engine.client.get_entity(m.fwd_from.from_id)
            except Exception:
                pass
        sid = getattr(sender, 'id', None)
        if groups and groups[-1]['id'] == sid:
            groups[-1]['msgs'].append(m)
        else:
            groups.append({'id': sid, 'ent': sender, 'msgs': [m]})
    return groups


def _count(arg):
    m = re.search(r'\b(\d{1,2})\b', arg or '')
    return max(1, min(20, int(m.group(1)))) if m else 1


async def make(event, mode):
    args = engine.get_args()
    if not event.is_reply:
        await engine.answer(event, visual.err("Ответьте на сообщение, которое цитировать") + "\n"
                            + visual.hint(".q · .q 3 — три сообщения подряд · .qs — сразу в стикерпак"))
        return
    await engine.answer(event, visual.wait("Рисую цитату…"))
    try:
        groups = await collect(event, _count(args))
        if not groups:
            raise RuntimeError('нет сообщений')
        theme = next((t for t in BG if t in args.lower().split()), None) or await cfg('theme') or 'dark'
        img = await render(groups, theme, (await cfg('avatars')) is not False and 'noav' not in args.lower(),
                           (await cfg('replies')) is not False, (await cfg('media')) is not False)
    except Exception as e:
        await engine.answer(event, visual.err("Не получилось нарисовать") + f"\n<code>{_esc(str(e)[:300])}</code>")
        return
    reply_to = event.reply_to_msg_id
    if mode == 'png':
        f = io.BytesIO(to_png(img))
        f.name = 'quote.png'
        await engine.client.send_file(event.chat_id, f, reply_to=reply_to, force_document=False)
        await event.delete()
        return
    webp, (w, h) = to_sticker(img)
    if mode == 'pack':
        bot = getattr(engine, '_aiogram_bot', None)
        owner = getattr(engine, '_owner_id', None)
        if bot is None or owner is None:
            await engine.answer(event, visual.err("Для пака нужен стационарный бот FRX"))
            return
        try:
            res = await media.pack_add(bot, owner, f"quotes_{owner}", await cfg('pack_title') or 'Цитаты', webp, 'static',
                                       [await cfg('pack_emoji') or '💬'])
            doc = await media.pack_last_document(engine.client, res['name'])
        except Exception as e:
            await engine.answer(event, visual.err("Пак не обновился") + f"\n<code>{_esc(str(e)[:300])}</code>")
            return
        if doc is not None:
            await engine.client.send_file(event.chat_id, doc, reply_to=reply_to)
            await event.delete()
            if res['created']:
                await engine.client.send_message('me', visual.ok("Создан ваш пак цитат") + f"\n{res['link']}", parse_mode='html')
            return
    from telethon.tl.types import DocumentAttributeImageSize, DocumentAttributeSticker, InputStickerSetEmpty
    f = io.BytesIO(webp)
    f.name = 'quote.webp'
    await engine.client.send_file(event.chat_id, f, reply_to=reply_to, force_document=False, mime_type='image/webp',
                                  attributes=[DocumentAttributeSticker(alt='💬', stickerset=InputStickerSetEmpty()),
                                              DocumentAttributeImageSize(w=w, h=h)])
    await event.delete()


@decorators.command('q', 'quote', 'цитата')
async def q_cmd(event):
    """цитата-стикер: ответом на сообщение; [число] — несколько сообщений подряд; [dark|light|black|frx] — фон"""
    await make(event, 'sticker')


@decorators.command('qs', 'qpack')
async def qs_cmd(event):
    """цитата — и сразу в ваш стикерпак «Цитаты»: ответом, [число]"""
    await make(event, 'pack')


@decorators.command('qp', 'qpng')
async def qp_cmd(event):
    """цитата картинкой (PNG): ответом, [число]"""
    await make(event, 'png')


@decorators.command('qset')
async def qset_cmd(event):
    """настройки цитат: фон, аватарки, ответы, фото"""
    async def text():
        return "\n".join([visual.title("Quotes", sub="настройки"),
                          visual.field("фон", await cfg('theme')),
                          visual.field("аватарки", "да" if await cfg('avatars') else "нет", code=False),
                          visual.field("«в ответ на»", "да" if await cfg('replies') else "нет", code=False),
                          visual.field("фото и стикеры", "да" if await cfg('media') else "нет", code=False),
                          visual.field("пак", _esc(await cfg('pack_title') or 'Цитаты'), code=False),
                          "", visual.hint(".q · .q 3 · .qs — в пак · .qp — картинкой")])

    async def buttons():
        th = await cfg('theme')
        return [[(("• " if t == th else "") + t, f"qs:t:{t}") for t in BG],
                [("Аватарки: " + ("вкл" if await cfg('avatars') else "выкл"), "qs:b:avatars"),
                 ("Ответы: " + ("вкл" if await cfg('replies') else "выкл"), "qs:b:replies")],
                [("Фото: " + ("вкл" if await cfg('media') else "выкл"), "qs:b:media"),
                 engine.InputButton("✎ Название пака", "title", hint="как назвать ваш пак цитат")],
                [(visual.btn('Закрыть', 'close'), "qs:close")]]
    form = await engine.create_inlineform(event.chat_id, await text(), buttons=await buttons())
    await event.delete()

    @form.on_click(data_startswith='qs:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
            return
        if d.startswith('t:'):
            await engine.edit_config('theme', 'edit', d[2:], module_name=MODULE_NAME)
        elif d.startswith('b:'):
            k = d[2:]
            await engine.edit_config(k, 'edit', not await cfg(k), module_name=MODULE_NAME)
        await form.edit(await text(), buttons=await buttons())

    @form.on_input(data='title')
    async def _(inp):
        v = (inp.value or '').strip()[:60]
        if v:
            await engine.edit_config('pack_title', 'edit', v, module_name=MODULE_NAME)
        await form.edit(await text(), buttons=await buttons())
