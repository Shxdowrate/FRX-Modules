"""
Whisper — шёпот в любом чате через инлайн-режим бота.

  @бот wh [@user] текст              шёпот одному
  @бот wh [@user1, 123456, @user2] текст   нескольким (через запятую или пробел)

В чат уходит сообщение «🔒 Шёпот для …» с кнопкой «посмотреть»: прочитать могут только адресаты (и вы).
Короткий текст показывается всплывающим окном, длинный (больше 200 символов) — в личке с ботом по кнопке.
Когда адресат прочитал, под сообщением появляется отметка «прочитано».
"""

import html
import re
import secrets
import time

import bot
import db
import decorators
import engine
import visual

NAME = "Whisper"
MODULE_NAME = "whisper"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Шёпот через инлайн: @бот wh [@user, 123] текст — прочитать могут только адресаты"

ALERT_MAX = 190
KEEP_DAYS = 30


def _esc(x):
    return html.escape(str(x), quote=False)


def _parse(text):
    """'[@a, 123] привет' -> ([{'u': 'a'}, {'id': 123}], 'привет') | (None, None)."""
    m = re.match(r'^\s*\[([^\]]*)\]\s*(.*)$', text or '', re.S)
    if not m:
        return None, None
    targets = []
    for part in re.split(r'[\s,;]+', m.group(1)):
        part = part.strip()
        if not part:
            continue
        if re.fullmatch(r'\d{3,15}', part):
            targets.append({'id': int(part)})
        elif re.fullmatch(r'@?[A-Za-z][\w]{3,31}', part):
            targets.append({'u': part.lstrip('@').lower()})
    return targets, m.group(2).strip()


def _who(targets):
    return ", ".join(f"@{t['u']}" if 'u' in t else str(t['id']) for t in targets)


def _allowed(w, user_id, username):
    if user_id == w.get('from'):
        return True
    username = (username or '').lower()
    return any((t.get('id') == user_id) or (username and t.get('u') == username) for t in w.get('to', []))


async def _bot_username():
    name = getattr(engine, '_bot_username', None)
    if not name and getattr(engine, '_aiogram_bot', None) is not None:
        name = (await engine._aiogram_bot.get_me()).username
        engine._bot_username = name
    return name


@bot.inline_query('wh', 'whisper', 'шепот', access='self')
async def whisper_query(query):
    """шёпот: [@user, 123] текст — прочитают только адресаты"""
    targets, text = _parse(query.text)
    if not targets or not text:
        return [engine.InlineResult(
            "Шёпот: wh [@user] текст", description="адресаты в квадратных скобках, через запятую",
            text=visual.hint("Шёпот пишется так: <code>@бот wh [@user, 123456] текст</code>"))]
    wid = secrets.token_urlsafe(6)
    record = {'from': query.user_id, 'from_name': query.first_name, 'to': targets, 'text': text[:4000],
              'ts': int(time.time()), 'seen': []}

    async def save(_inline_message_id):                # запрос приходит на каждую букву — сохраняем только отправленный
        await db.set(f'w:{wid}', record)
    who = _who(targets)
    preview = (text[:60] + '…') if len(text) > 60 else text
    return [engine.InlineResult(
        f"🔒 Шёпот для {who}", description=f"«{preview}» — увидят только они",
        text=f"🔒 <b>Шёпот</b> для {_esc(who)}",
        buttons=[[("👁 Посмотреть", f"wh:{wid}", "primary")]], on_chosen=save)]


@bot.inline_click(data_startswith='wh:')
async def whisper_click(click):
    wid = click.data[3:]
    w = await db.get(f'w:{wid}')
    if w is None:
        await click.answer("Шёпот устарел или удалён", show_alert=True)
        return
    if not _allowed(w, click.user_id, click.username):
        await click.answer("Этот шёпот не для вас 🤫", show_alert=True)
        return
    if len(w['text']) <= ALERT_MAX:
        await click.answer(w['text'], show_alert=True)
    else:
        name = await _bot_username()
        await click.answer(url=f"https://t.me/{name}?start=wh_{wid}")
    if click.user_id != w['from'] and click.user_id not in w['seen']:
        w['seen'].append(click.user_id)
        await db.set(f'w:{wid}', w)
        names = w.setdefault('seen_names', [])
        names.append(click.first_name or click.username or str(click.user_id))
        await db.set(f'w:{wid}', w)
        try:
            await click.edit(f"🔒 <b>Шёпот</b> для {_esc(_who(w['to']))}\n<i>👁 прочитал(а): {_esc(', '.join(names))}</i>",
                             buttons=[[("👁 Посмотреть", f"wh:{wid}", "primary")]])
        except Exception:
            pass


@bot.start_link('wh_')
async def whisper_long(event, wid):
    w = await db.get(f'w:{wid}')
    if w is None:
        await event.reply("Шёпот устарел или удалён")
        return
    sender = getattr(getattr(event.raw, 'from_user', None), 'username', '') or ''
    if not _allowed(w, event.sender_id, sender):
        await event.reply("Этот шёпот не для вас 🤫")
        return
    await event.reply(f"🔒 <b>Шёпот от {_esc(w.get('from_name') or 'владельца')}</b>\n\n{_esc(w['text'])}")


@decorators.cycle(interval='6h')
async def whisper_cleanup():
    """старые шёпоты (больше KEEP_DAYS дней) удаляются"""
    border = time.time() - KEEP_DAYS * 86400
    for key, w in (await db.list()).items():
        if key.startswith('w:') and isinstance(w, dict) and w.get('ts', 0) < border:
            await db.delete(key)
