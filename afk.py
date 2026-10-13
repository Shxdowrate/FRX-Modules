"""
AFK — «меня нет на месте».

  .afk [причина]    включить: в личке и при упоминании / ответе вам в группах модуль сам ответит,
                    что вас нет, с причиной и временем. Каждому чату — не чаще раза в N минут (настройка).
  .unafk            выключить вручную. Любое ваше сообщение (не команда) тоже выключает AFK —
                    и присылает сводку: кто писал, пока вас не было.
"""

import html
import time

import db
import decorators
import engine
import visual

NAME = "AFK"
MODULE_NAME = "afk"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Автоответ «меня нет»: .afk причина — в личке и на упоминания; сводка пропущенного по возвращении"

_answered = {}       # chat_id -> время последнего автоответа
_incoming = {}       # chat_id -> время последнего входящего: исходящее сразу после него — скорее автоответ модуля, а не вы
_mine = set()        # id наших автоответов


def _esc(x):
    return html.escape(str(x), quote=False)


def _ago(sec):
    sec = int(sec)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    parts = ([f"{d} д"] if d else []) + ([f"{h} ч"] if h else []) + ([f"{m} мин"] if m and not d else [])
    return " ".join(parts) or "меньше минуты"


@decorators.on_start()
async def setup():
    await engine.create_config(key='cooldown', description='Отвечать в один чат не чаще, чем раз в столько минут',
                               data_type='int', default=10, valid={'min_value': 0, 'max_value': 1440})
    await engine.create_config(key='in_groups', description='Отвечать на упоминания и ответы в группах',
                               data_type='bool', default=True)
    await engine.create_config(key='text', description='Текст автоответа ({причина} и {время} подставятся)',
                               data_type='str', default='💤 Меня сейчас нет на месте ({время}).\nПричина: {причина}')


@decorators.command('afk')
async def afk_cmd(event):
    """включить AFK: [причина]"""
    reason = engine.get_args().strip() or 'не указана'
    await db.set('state', {'since': time.time(), 'reason': reason, 'missed': []})
    _answered.clear()
    await engine.answer(event, visual.ok("AFK включён") + "\n" + visual.field("причина", _esc(reason), code=False)
                        + "\n" + visual.hint("выключится, когда вы напишете любое сообщение, или .unafk"))


@decorators.command('unafk')
async def unafk_cmd(event):
    """выключить AFK"""
    if not await db.get('state'):
        await engine.answer(event, visual.hint("AFK и так выключен"))
        return
    await engine.answer(event, await _off())


async def _off():
    st = await db.get('state') or {}
    await db.delete('state')
    missed = st.get('missed', [])
    lines = [visual.ok("С возвращением!") + "  " + visual.hint(f"вас не было {_ago(time.time() - st.get('since', time.time()))}")]
    if missed:
        lines.append(visual.section(f"Писали ({len(missed)})"))
        for m in missed[-15:]:
            link = f"https://t.me/c/{str(m['chat'])[4:]}/{m['id']}" if str(m['chat']).startswith('-100') else None
            who = _esc(m['who'])
            lines.append(visual.item((f'<a href="{link}">{who}</a>' if link else who) + f": {_esc(m['text'][:80])}"))
    return "\n".join(lines)


@decorators.watcher()
async def afk_watcher(event):
    st = await db.get('state')
    if not st:
        return
    if event.out:
        text = event.raw_text or ''
        if (event.chat_id, event.id) in _mine or text.startswith(await engine.get_prefix()) or time.time() - st['since'] < 5:
            return
        if time.time() - _incoming.get(event.chat_id, 0) < 2.5:      # чей-то автоответ (автоответчик, бот)
            return
        try:
            await engine.bot_send_to_owner(await _off())
        except Exception:
            await engine.client.send_message('me', await _off(), parse_mode='html')
        return
    _incoming[event.chat_id] = time.time()
    if event.is_private:
        sender = await event.get_sender()
        if getattr(sender, 'bot', False):
            return
    elif not (await engine.get_config('in_groups') and (event.mentioned or await _is_reply_to_me(event))):
        return
    sender = await event.get_sender()
    who = " ".join(filter(None, [getattr(sender, 'first_name', None), getattr(sender, 'last_name', None)])) or getattr(sender, 'title', '') or str(event.sender_id)
    st.setdefault('missed', []).append({'chat': event.chat_id, 'id': event.id, 'who': who, 'text': event.raw_text or '[медиа]'})
    st['missed'] = st['missed'][-50:]
    await db.set('state', st)
    cd = await engine.get_config('cooldown') * 60
    if time.time() - _answered.get(event.chat_id, 0) < cd:
        return
    _answered[event.chat_id] = time.time()
    tpl = await engine.get_config('text')
    text = tpl.replace('{причина}', _esc(st['reason'])).replace('{время}', 'уже ' + _ago(time.time() - st['since']))
    sent = await event.reply(text, parse_mode='html')
    _mine.add((event.chat_id, sent.id))


async def _is_reply_to_me(event):
    if not event.is_reply:
        return False
    try:
        r = await event.get_reply_message()
        return bool(r and r.out)
    except Exception:
        return False
