"""
qwerty — исправить текст, набранный не в той раскладке: «ghbdtn» → «привет», «руддщ» → «hello».

  .k                  без всего — исправить ваше последнее сообщение в этом чате (правкой)
  .k                  ответом — исправить то сообщение: ваше — правкой, чужое — ответом с исправленным текстом
  .k <текст>          перевести этот текст (команда заменится результатом)
  .k ru / .k en       то же, но направление указать явно (по умолчанию — само определяет)
  .k auto on|off      сам исправлять ваши сообщения, если похоже, что раскладка не та (ссылки, код и команды не трогает)

Алиасы: .qwerty, .раскладка (в русской раскладке «.л» — тоже работает).
"""

import html
import re

import decorators
import engine
import visual

NAME = "qwerty"
MODULE_NAME = "qwerty"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Исправляет текст, набранный не в той раскладке (ghbdtn → привет); .k — одна буква, есть авторежим"

EN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./~QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?@#$^&"
RU = "ёйцукенгшщзхъфывапролджэячсмитьбю.ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,\"№;:?"
EN2RU = dict(zip(EN, RU))
RU2EN = dict(zip(RU, EN))
AMBIG_EN = set(",.;'[]`<>:\"{}~/?@#$^&")       # знаки, которые на месте букв — буквы, а отдельно — знаки

# самые частые биграммы (доля на тысячу) — для автоопределения «похоже на язык»
RU_BI = ('ст но то на ен ов ни ра во ко ал по ре пр ер ос ет ли ла ва ро ан го ор ка ть ол ве ит ле ел ак ом ин де ти ис '
         'ло та ог он ри ат ет ск ми мо не ед ен ам ой ал ит ую ая ые ых ем им ое ия ет ут ют ем бы да ди ду хо че чт эт '
         'то ты вы мы же ке ше ще жи ши ча ща ду те ту зд ри ки ги пи ви ез ес').split()
EN_BI = ('th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha as ou io le ve co me de hi ri ro ic '
         'ne ea ra ce li ch ll be ma si om ur wh wi ow ld lo yo ay ee oo ly ck ss ey ok ga go we us so do no up il ad').split()


def _esc(x):
    return html.escape(str(x), quote=False)


def is_cyr(c):
    return 'а' <= c.lower() <= 'я' or c in 'ёЁ'


def is_lat(c):
    return 'a' <= c.lower() <= 'z'


def convert(text, direction):
    """direction: 'ru' — латиница → кириллица, 'en' — наоборот."""
    out = []
    n = len(text)
    for i, ch in enumerate(text):
        if direction == 'ru':
            if ch in AMBIG_EN:
                prev_l = i > 0 and (is_lat(text[i - 1]) or text[i - 1] in "[];',.`")
                next_l = i + 1 < n and (is_lat(text[i + 1]) or text[i + 1] in "[];',.`")
                # «,» и «.» перед буквой — это «б»/«ю»; в конце слова — обычные знаки
                if ch in ',.' and not next_l:
                    out.append(ch)
                    continue
                if ch in '?' and not prev_l:
                    out.append(ch)
                    continue
                if ch in '/' and not (prev_l and next_l):
                    out.append('.' if prev_l else ch)
                    continue
                if ch == '&' and prev_l:
                    out.append('?')
                    continue
                if ch in '@#$^&' and not prev_l:
                    out.append(ch)
                    continue
            out.append(EN2RU.get(ch, ch))
        else:
            out.append(RU2EN.get(ch, ch) if is_cyr(ch) else ch)
    return ''.join(out)


def detect(text):
    """Куда переводить: 'ru' если текст латинский, 'en' если кириллица; None — непонятно."""
    lat = sum(is_lat(c) for c in text)
    cyr = sum(is_cyr(c) for c in text)
    if lat > cyr:
        return 'ru'
    if cyr > lat:
        return 'en'
    return None


def score(text, table):
    """Сколько биграмм текста — частые биграммы языка (0…1)."""
    words = re.findall(r'[^\W\d_]+', text.lower())
    pairs = [w[i:i + 2] for w in words for i in range(len(w) - 1)]
    if not pairs:
        return 0.0
    s = set(table)
    return sum(p in s for p in pairs) / len(pairs)


def looks_wrong(text):
    """Авторежим: (направление, исправленный текст) или None, если текст и так в порядке."""
    t = text.strip()
    if len(t) < 5 or re.search(r'https?://|www\.|@\w|`|\d{3,}|[{}<>]{2}|\.\w{2,4}\b/', t):
        return None
    d = detect(t)
    if d is None:
        return None
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 5:
        return None
    if d == 'ru' and any(is_cyr(c) for c in t) or d == 'en' and any(is_lat(c) for c in t):
        return None                                    # смешанный текст — не трогаем
    fixed = convert(t, d)
    src_tab, dst_tab = (EN_BI, RU_BI) if d == 'ru' else (RU_BI, EN_BI)
    a, b = score(t, src_tab), score(fixed, dst_tab)
    if b >= 0.42 and b > a * 1.8 + 0.05:
        return d, fixed
    return None


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


@decorators.on_start()
async def setup():
    await engine.create_config(key='auto', description='Сам исправлять ваши сообщения в неверной раскладке', data_type='bool',
                               default=False)


@decorators.command('k', 'qwerty', 'л', 'раскладка')
async def k_cmd(event):
    """исправить раскладку: ответом, <текст> или своё прошлое сообщение; ru|en — направление; auto on|off"""
    args = engine.get_args()
    low = args.strip().lower()
    if low.startswith('auto'):
        v = low.split()[1] if len(low.split()) > 1 else ''
        if v in ('on', 'off', 'вкл', 'выкл'):
            await engine.edit_config('auto', 'edit', v in ('on', 'вкл'))
        await engine.answer(event, visual.ok("Авторежим раскладки: " + ("вкл" if await cfg('auto') else "выкл"))
                            + "\n" + visual.hint("исправляю только явные ошибки: «ghbdtn rfr ltkf» → «привет как дела»"))
        return
    force = None
    if low in ('ru', 'en', 'ру', 'ен'):
        force, args = ('ru' if low in ('ru', 'ру') else 'en'), ''
    elif re.match(r'^(ru|en)\s', low):
        force, args = low[:2], args.strip()[3:]
    if args.strip():
        d = force or detect(args) or 'ru'
        await engine.answer(event, _esc(convert(args.strip(), d)))
        return
    target = await event.get_reply_message() if event.is_reply else None
    if target is None:
        async for m in engine.client.iter_messages(event.chat_id, from_user='me', limit=5):
            if m.id != event.id and (m.raw_text or '').strip() and not m.raw_text.startswith(await engine.get_prefix()):
                target = m
                break
    if target is None or not (target.raw_text or '').strip():
        await engine.answer(event, visual.err("Нечего исправлять — ответьте на сообщение или напишите .k <текст>"))
        return
    d = force or detect(target.raw_text) or 'ru'
    fixed = convert(target.raw_text, d)
    if target.out:
        try:
            await target.edit(fixed)
            await event.delete()
            return
        except Exception:
            pass
    await engine.answer(event, _esc(fixed))


@decorators.watcher()
async def auto_fix(event):
    if not event.out or getattr(event.message, 'via_bot_id', None) or getattr(event.message, 'fwd_from', None):
        return
    text = event.raw_text or ''
    if not text or event.message.entities or text.startswith(await engine.get_prefix()) or text.startswith('/'):
        return
    if not await cfg('auto', False):
        return
    res = looks_wrong(text)
    if res is None:
        return
    try:
        await event.edit(res[1])
    except Exception:
        pass
