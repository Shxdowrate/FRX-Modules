"""
Remind — напоминания. Приходят в ЛС от бота (со звуком), переживают перезапуск.

  .remind <когда> <текст>     когда: 10m · 2h30m · 1d · через 15 минут · 18:30 · завтра 9:00 · 12.10 09:00 · 12.10.2026 9:00
                              каждый день 9:00 · every 2h — повторяющееся
                              ответом на сообщение — в напоминании будет ссылка на него
  .reminds                    список с удалением

Если юзербот был выключен, когда напоминание должно было прийти, — оно придёт при включении с пометкой
«пропущено» и временем, когда должно было быть. Под напоминанием — кнопки «отложить на 10 мин / 1 ч / до завтра».
"""

import html
import re
import secrets
import time
from datetime import datetime, timedelta

import bot
import db
import decorators
import engine
import visual

NAME = "Remind"
MODULE_NAME = "remind"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Напоминания: .remind 2h текст / 18:30 текст / каждый день 9:00 текст — в ЛС от бота; пропущенные — при включении"

UNITS = {'s': 1, 'с': 1, 'сек': 1, 'm': 60, 'м': 60, 'мин': 60, 'min': 60, 'h': 3600, 'ч': 3600, 'час': 3600, 'часа': 3600,
         'часов': 3600, 'd': 86400, 'д': 86400, 'дн': 86400, 'день': 86400, 'дня': 86400, 'дней': 86400, 'w': 604800, 'нед': 604800,
         'минут': 60, 'минуты': 60, 'минуту': 60, 'секунд': 1}
LATE = 120          # опоздание больше этого — «пропущено»


def _esc(x):
    return html.escape(str(x), quote=False)


def _fmt(ts):
    d = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    day = 'сегодня' if d.date() == today else 'завтра' if d.date() == today + timedelta(days=1) else d.strftime('%d.%m.%Y')
    return f"{day} в {d:%H:%M}"


def parse_when(s):
    """'текст после «когда»' → (timestamp, repeat_seconds | 'daily' | None, остаток текста) или None."""
    s = s.strip()
    now = datetime.now()
    low = s.lower()
    m = re.match(r'^(?:каждый день|ежедневно|daily)\s+(\d{1,2})[:.](\d{2})\s*(.*)$', low, re.S)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t.timestamp(), 'daily', s[len(s) - len(m.group(3)):]
    m = re.match(r'^(?:every|каждые?)\s+((?:\d+\s*[a-zа-я]+\s*)+)\s*(.*)$', low, re.S)
    if m:
        sec = _dur(m.group(1))
        if sec and sec >= 60:
            return time.time() + sec, sec, s[len(s) - len(m.group(2)):]
    m = re.match(r'^(?:через\s+)?((?:\d+\s*(?:[a-zа-я]+)\s*)+)(.*)$', low, re.S)
    if m and _dur(m.group(1)):
        return time.time() + _dur(m.group(1)), None, s[len(s) - len(m.group(2)):]
    m = re.match(r'^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+(?:в\s+)?(\d{1,2})[:.](\d{2})\s*(.*)$', low, re.S)
    if m:
        y = int(m.group(3)) if m.group(3) else now.year
        y = y + 2000 if y < 100 else y
        try:
            t = datetime(y, int(m.group(2)), int(m.group(1)), int(m.group(4)), int(m.group(5)))
        except ValueError:
            return None
        if not m.group(3) and t <= now:
            t = t.replace(year=y + 1)
        return t.timestamp(), None, s[len(s) - len(m.group(6)):]
    m = re.match(r'^(завтра|послезавтра)?\s*(?:в\s+)?(\d{1,2})[:.](\d{2})(?!\.\d)\s*(.*)$', low, re.S)
    if m:
        t = now.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
        if m.group(1) == 'завтра':
            t += timedelta(days=1)
        elif m.group(1) == 'послезавтра':
            t += timedelta(days=2)
        elif t <= now:
            t += timedelta(days=1)
        return t.timestamp(), None, s[len(s) - len(m.group(4)):]
    return None


def _dur(s):
    total = 0
    for num, unit in re.findall(r'(\d+)\s*([a-zа-я]+)', s):
        u = UNITS.get(unit) or next((v for k, v in UNITS.items() if unit.startswith(k) and len(k) > 1), None)
        if u is None:
            return 0
        total += int(num) * u
    return total


async def _list():
    return list(await db.get('list', []) or [])


async def _save(items):
    await db.set('list', sorted(items, key=lambda r: r['at']))


@decorators.command('remind', 'rem', 'напомни')
async def remind_cmd(event):
    """напоминание: <когда> <текст> — 10m, 2h, 18:30, завтра 9:00, 12.10 9:00, каждый день 9:00, every 2h"""
    args = engine.get_args()
    parsed = parse_when(args)
    if not parsed:
        await engine.answer(event, visual.err("Не понял, когда напомнить") + "\n"
                            + visual.hint(".remind 30m позвонить · .remind 18:30 встреча · .remind завтра 9:00 · .remind каждый день 9:00 зарядка"))
        return
    at, repeat, text = parsed
    text = text.strip()
    link = None
    if event.is_reply:
        cid = str(event.chat_id)
        link = f"https://t.me/c/{cid[4:]}/{event.reply_to_msg_id}" if cid.startswith('-100') else None
        if not text:
            r = await event.get_reply_message()
            text = (r.raw_text or '')[:500] if r else ''
    if not text:
        text = 'напоминание'
    item = {'id': secrets.token_hex(3), 'at': at, 'text': text, 'repeat': repeat, 'link': link, 'created': time.time()}
    items = await _list()
    items.append(item)
    await _save(items)
    rep = " · каждый день" if repeat == 'daily' else (f" · каждые {_human(repeat)}" if repeat else '')
    await engine.answer(event, visual.ok("Напомню") + f" {_fmt(at)}{rep}\n" + visual.item(_esc(text)))


def _human(sec):
    sec = int(sec)
    for u, n in ((86400, 'д'), (3600, 'ч'), (60, 'мин')):
        if sec >= u and sec % u == 0:
            return f"{sec // u} {n}"
    return f"{sec // 60} мин"


@decorators.command('reminds', 'reminders')
async def reminds_cmd(event):
    """список напоминаний с удалением"""
    items = await _list()
    if not items:
        await engine.answer(event, visual.hint("Напоминаний нет — .remind 30m текст"))
        return

    def text():
        lines = [visual.title("Напоминания", sub=str(len(items)))]
        for i, r in enumerate(items, 1):
            rep = " 🔁" if r.get('repeat') else ''
            lines.append(f"<b>{i}.</b> {_fmt(r['at'])}{rep} — {_esc(r['text'][:80])}")
        return "\n".join(lines)

    def buttons():
        rows = [[(f"✕ {i}. {r['text'][:24]}", f"rm:d:{r['id']}")] for i, r in enumerate(items, 1)][:20]
        rows.append([(visual.btn('Закрыть', 'close'), "rm:close")])
        return rows
    try:
        form = await engine.create_inlineform(event.chat_id, text(), buttons=buttons())
    except Exception:
        await engine.answer(event, text())
        return

    @form.on_click(data_startswith='rm:')
    async def _(click):
        nonlocal items
        if click.data == 'rm:close':
            await form.delete()
            return
        rid = click.data[5:]
        items = [r for r in await _list() if r['id'] != rid]
        await _save(items)
        await click.answer("удалено")
        if items:
            await form.edit(text(), buttons=buttons())
        else:
            await form.edit(visual.hint("Напоминаний больше нет"), buttons=None)
            await form.close()


async def _deliver(r, late):
    head = "⏰ <b>Напоминание</b>"
    if late > LATE:
        head = f"⏰ <b>Пропущенное напоминание</b>\n<i>должно было прийти {_fmt(r['at'])} — юзербот был выключен</i>"
    body = f"{head}\n\n{_esc(r['text'])}" + (f"\n\n<a href=\"{_esc(r['link'])}\">к сообщению</a>" if r.get('link') else '')
    markup = None
    name = getattr(engine, '_bot_username', None)
    try:
        if not name and getattr(engine, '_aiogram_bot', None) is not None:
            name = (await engine._aiogram_bot.get_me()).username
        if name and not r.get('repeat'):
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            u = f"https://t.me/{name}?start=rs_{r['id']}_"
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="+10 мин", url=u + "600"),
                                                            InlineKeyboardButton(text="+1 ч", url=u + "3600"),
                                                            InlineKeyboardButton(text="завтра", url=u + "86400")]])
    except Exception:
        markup = None
    try:
        await engine.bot_send_to_owner(body, buttons=markup)
    except Exception:
        await engine.client.send_message('me', body, parse_mode='html')


@decorators.cycle(interval='15s')
async def remind_tick():
    items = await _list()
    now = time.time()
    due = [r for r in items if r['at'] <= now]
    if not due:
        return
    keep = [r for r in items if r['at'] > now]
    snoozable = await db.get('done', {}) or {}
    for r in due:
        await _deliver(r, now - r['at'])
        if r.get('repeat') == 'daily':
            nxt = r['at']
            while nxt <= now:
                nxt += 86400
            keep.append({**r, 'at': nxt})
        elif r.get('repeat'):
            nxt = r['at']
            while nxt <= now:
                nxt += r['repeat']
            keep.append({**r, 'at': nxt})
        else:
            snoozable[r['id']] = {**r, 'done': now}
    for k in [k for k, v in snoozable.items() if now - v.get('done', now) > 2 * 86400]:
        snoozable.pop(k)
    await db.set('done', snoozable)
    await _save(keep)


@decorators.on_start()
async def remind_start():
    await remind_tick()                 # пропущенные, пока юзербот был выключен, — сразу


@bot.start_link('rs_')
async def snooze(event, payload):
    """кнопка «отложить» под напоминанием"""
    if event.sender_id != getattr(engine, '_owner_id', None):
        return
    rid, _, sec = payload.rpartition('_')
    done = await db.get('done', {}) or {}
    r = done.pop(rid, None)
    if r is None:
        await event.reply("Это напоминание уже отложено или устарело")
        return
    await db.set('done', done)
    r = {k: v for k, v in r.items() if k != 'done'}
    r['at'] = time.time() + int(sec or 600)
    items = await _list()
    items.append(r)
    await _save(items)
    await event.reply(f"⏰ Отложено — напомню {_fmt(r['at'])}")
