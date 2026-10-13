"""
TagAll — упомянуть всех в группе. Аккуратно: пачками, с паузами, без ботов, удалённых и давно не заходивших,
с прогрессом и остановкой.

  .tagall [текст]        всех участников (по умолчанию — без ботов, удалённых и тех, кто не заходил больше месяца)
  .tagall -a [текст]     вообще всех (и неактивных)
  .tagadmins [текст]     только админов
  .tagonline [текст]     только тех, кто сейчас в сети или был недавно
  .tagstop               остановить

Ответом на сообщение — упоминания придут ответом на него. По 5 человек в сообщении (уведомление получают все),
пауза между сообщениями — настройка delay; FloodWait Telegram модуль переживает сам. В больших группах (больше
limit участников) спросит подтверждение.
"""

import asyncio
import html
from datetime import datetime, timedelta, timezone

import decorators
import engine
import visual

NAME = "TagAll"
MODULE_NAME = "tagall"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Упомянуть всех: пачками по 5, без ботов и неактивных, с прогрессом и .tagstop; .tagadmins, .tagonline"

_running = {}       # chat_id -> asyncio.Event (стоп)
HIDDEN = 'ㅤ'       # «пустой» символ: упоминание без видимого имени


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='per_message', description='Упоминаний в одном сообщении', data_type='int', default=5,
                               valid={'min_value': 1, 'max_value': 10})
    await engine.create_config(key='delay', description='Пауза между сообщениями, сек', data_type='float', default=2.0,
                               valid={'min_value': 0.5, 'max_value': 30})
    await engine.create_config(key='style', description='Как выглядят упоминания: names — именами, hidden — невидимо',
                               data_type='choice', default='names', options=['names', 'hidden'])
    await engine.create_config(key='limit', description='Больше стольких человек — спросить подтверждение', data_type='int',
                               default=150, valid={'min_value': 10})
    await engine.create_config(key='delete_after', description='Удалять сообщения с упоминаниями через N секунд (0 — не удалять)',
                               data_type='int', default=0, valid={'min_value': 0, 'max_value': 86400})


def _active(u, border):
    st = getattr(u, 'status', None)
    name = type(st).__name__ if st else ''
    if name in ('UserStatusOnline', 'UserStatusRecently', 'UserStatusLastWeek'):
        return True
    if name == 'UserStatusOffline':
        return st.was_online >= border
    return False


def _online(u):
    st = getattr(u, 'status', None)
    name = type(st).__name__ if st else ''
    if name in ('UserStatusOnline', 'UserStatusRecently'):
        return True
    return name == 'UserStatusOffline' and st.was_online >= datetime.now(timezone.utc) - timedelta(hours=1)


async def _collect(chat_id, kind, everyone):
    from telethon.tl.types import ChannelParticipantsAdmins
    me = getattr(engine, '_owner_id', None)
    border = datetime.now(timezone.utc) - timedelta(days=30)
    flt = ChannelParticipantsAdmins() if kind == 'admins' else None
    out = []
    async for u in engine.client.iter_participants(chat_id, filter=flt, aggressive=kind != 'admins'):
        if u.bot or u.deleted or u.id == me:
            continue
        if kind == 'online' and not _online(u):
            continue
        if kind == 'all' and not everyone and not _active(u, border):
            continue
        out.append(u)
    return out


async def _run(event, kind):
    from telethon.errors import FloodWaitError
    if event.is_private:
        await engine.answer(event, visual.err("Это для групп"))
        return
    chat_id = event.chat_id
    if chat_id in _running:
        await engine.answer(event, visual.warn("Уже тегаю здесь — .tagstop, чтобы остановить"))
        return
    args = engine.get_args().strip()
    everyone = args.startswith('-a')
    text = args[2:].strip() if everyone else args
    reply_to = event.reply_to_msg_id
    await engine.answer(event, visual.wait("Собираю участников…"))
    try:
        users = await _collect(chat_id, kind, everyone)
    except Exception as e:
        await engine.answer(event, visual.err("Не получил список участников") + f"\n<code>{_esc(e)}</code>")
        return
    if not users:
        await engine.answer(event, visual.hint("Некого упоминать"))
        return
    limit = await engine.get_config('limit')
    if len(users) > limit and not args.endswith('!'):
        await engine.answer(event, visual.warn(f"Участников: <b>{len(users)}</b> — это много.") + "\n"
                            + visual.hint(f"повторите с ! в конце, чтобы всё равно: <code>.tag{kind} {_esc(args)} !</code>"))
        return
    text = text.rstrip('!').strip()
    stop = asyncio.Event()
    _running[chat_id] = stop
    per = await engine.get_config('per_message')
    delay = await engine.get_config('delay')
    style = await engine.get_config('style')
    ttl = await engine.get_config('delete_after')
    sent_ids, done = [], 0
    try:
        for i in range(0, len(users), per):
            if stop.is_set():
                break
            chunk = users[i:i + per]
            if style == 'hidden':
                marks = "".join(f'<a href="tg://user?id={u.id}">{HIDDEN}</a>' for u in chunk)
            else:
                marks = ", ".join(f'<a href="tg://user?id={u.id}">{_esc((u.first_name or "участник")[:24])}</a>' for u in chunk)
            body = (f"{_esc(text)}\n" if text else '') + marks
            while True:
                try:
                    m = await engine.client.send_message(chat_id, body, parse_mode='html', reply_to=reply_to, link_preview=False)
                    sent_ids.append(m.id)
                    break
                except FloodWaitError as e:
                    await engine.answer(event, visual.wait(f"Telegram просит подождать {e.seconds} с…"))
                    await asyncio.sleep(e.seconds + 1)
            done += len(chunk)
            if i // per % 3 == 0:
                try:
                    await engine.answer(event, visual.wait(f"Тегаю… <code>{engine.progress_bar(done / len(users) * 100, 10, 'bar')}</code> {done}/{len(users)}")
                                        + "\n" + visual.hint(".tagstop — остановить"))
                except Exception:
                    pass
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
    finally:
        _running.pop(chat_id, None)
    await engine.answer(event, (visual.warn("Остановлено") if stop.is_set() else visual.ok("Готово"))
                        + f"  {visual.hint(f'упомянуто {done} из {len(users)}')}")
    if ttl and sent_ids:
        await asyncio.sleep(ttl)
        try:
            await engine.client.delete_messages(chat_id, sent_ids)
        except Exception:
            pass


@decorators.command('tagall', 'all')
async def tagall_cmd(event):
    """упомянуть всех: [-a — и неактивных] [текст] [! — без подтверждения]"""
    await _run(event, 'all')


@decorators.command('tagadmins', 'admins')
async def tagadmins_cmd(event):
    """упомянуть админов: [текст]"""
    await _run(event, 'admins')


@decorators.command('tagonline')
async def tagonline_cmd(event):
    """упомянуть тех, кто в сети или был недавно: [текст]"""
    await _run(event, 'online')


@decorators.command('tagstop')
async def tagstop_cmd(event):
    """остановить упоминания в этом чате"""
    stop = _running.get(event.chat_id)
    if stop is None:
        await engine.answer(event, visual.hint("Здесь ничего не тегается"))
        return
    stop.set()
    await engine.answer(event, visual.ok("Останавливаю…"))


@decorators.on_unload()
async def _unload():
    for stop in _running.values():
        stop.set()
