"""
Scheduler — отложенные и повторяющиеся сообщения и команды юзербота.

  .sched <когда> <текст>          сообщение в этот чат
  .sched <когда> .команда …       выполнить команду FRX в этом чате (например, .sched каждый день 9:00 .w Москва)
  .sched <когда>  (ответом)       переслать копию сообщения, на которое ответили (с медиа и форматированием)
  .schedto <чат> <когда> <текст>  то же в другой чат: @username, id, ссылка или me (Избранное)
  .scheds [all]                   список: выполнить сейчас, пауза, удалить

Когда: 10m · 2h30m · через 15 минут · 18:30 · завтра 9:00 · 12.10 09:00 · 12.10.2026 9:00
Повтор: каждый день 9:00 · по будням 9:00 · по выходным 12:00 · каждый пн 10:00 · каждый пн,ср,пт 10:00 · every 2h · каждые 30m

Разовые сообщения ставятся в отложенные самого Telegram — они уйдут, даже если компьютер с FRX выключен
(их видно в чате в «Отложенных»). Команды и повторы выполняет FRX: если он был выключен, пропущенное выполнится
при включении, но только если опоздание не больше catch_up_min (по умолчанию 60 минут).
"""

import asyncio
import html
import re
import secrets
import time
from datetime import datetime, timedelta

import db
import decorators
import engine
import visual

NAME = "Scheduler"
MODULE_NAME = "scheduler"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Отложенные и повторяющиеся сообщения и команды: .sched 18:00 текст · .sched каждый день 9:00 .команда"

UNITS = {'s': 1, 'с': 1, 'сек': 1, 'секунд': 1, 'm': 60, 'м': 60, 'мин': 60, 'min': 60, 'минут': 60, 'минуты': 60, 'минуту': 60,
         'h': 3600, 'ч': 3600, 'час': 3600, 'часа': 3600, 'часов': 3600, 'd': 86400, 'д': 86400, 'дн': 86400, 'день': 86400,
         'дня': 86400, 'дней': 86400, 'w': 604800, 'нед': 604800}
WD = {'пн': 0, 'вт': 1, 'ср': 2, 'чт': 3, 'пт': 4, 'сб': 5, 'вс': 6, 'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4,
      'sat': 5, 'sun': 6, 'понедельник': 0, 'вторник': 1, 'среду': 2, 'среда': 2, 'четверг': 3, 'пятницу': 4, 'пятница': 4,
      'субботу': 5, 'суббота': 5, 'воскресенье': 6}
WD_NAMES = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
TASK = {'loop': None}


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='catch_up_min', description='Пропущенное (FRX был выключен) выполнять при включении, если '
                               'опоздание не больше стольких минут', data_type='int', default=60,
                               valid={'min_value': 0, 'max_value': 10080})
    await engine.create_config(key='use_native', description='Разовые сообщения ставить в отложенные Telegram (уйдут и без FRX)',
                               data_type='bool', default=True)
    TASK['loop'] = asyncio.ensure_future(loop())


@decorators.on_unload()
async def cleanup():
    if TASK['loop']:
        TASK['loop'].cancel()


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


def _owner_key():
    return f"{MODULE_NAME}:items:{getattr(engine, '_owner_id', None) or 0}"


async def items():
    return list(await db.gget(_owner_key(), []) or [])


async def save(lst):
    await db.gset(_owner_key(), sorted(lst, key=lambda x: x['at']))


# --------------------------------------------------------------------------- когда

def _dur(s):
    total = 0
    for num, unit in re.findall(r'(\d+)\s*([a-zа-я]+)', s):
        u = UNITS.get(unit) or next((v for k, v in UNITS.items() if unit.startswith(k) and len(k) > 1), None)
        if u is None:
            return 0
        total += int(num) * u
    return total


def next_daily(rep, after=None):
    """Ближайший момент для {'type': 'daily', 'h', 'm', 'days': [0..6]} строго после after."""
    after = after or datetime.now()
    t = after.replace(hour=rep['h'], minute=rep['m'], second=0, microsecond=0)
    for _ in range(9):
        if t > after and t.weekday() in rep['days']:
            return t.timestamp()
        t += timedelta(days=1)
    return (after + timedelta(days=1)).timestamp()


def parse_when(s):
    """→ (timestamp, repeat | None, остаток) или None. repeat: {'type': 'every', 'sec'} | {'type': 'daily', 'h', 'm', 'days'}."""
    s = s.strip()
    low = s.lower()
    now = datetime.now()

    def rest(m, g):
        return s[len(s) - len(m.group(g)):]

    m = re.match(r'^(?:каждый день|ежедневно|daily|every day)\s+(?:в\s+)?(\d{1,2})[:.](\d{2})\s*(.*)$', low, re.S)
    if m:
        rep = {'type': 'daily', 'h': int(m.group(1)), 'm': int(m.group(2)), 'days': list(range(7))}
        return next_daily(rep), rep, rest(m, 3)
    m = re.match(r'^(?:по будням|в будни|weekdays)\s+(?:в\s+)?(\d{1,2})[:.](\d{2})\s*(.*)$', low, re.S)
    if m:
        rep = {'type': 'daily', 'h': int(m.group(1)), 'm': int(m.group(2)), 'days': [0, 1, 2, 3, 4]}
        return next_daily(rep), rep, rest(m, 3)
    m = re.match(r'^(?:по выходным|в выходные|weekends)\s+(?:в\s+)?(\d{1,2})[:.](\d{2})\s*(.*)$', low, re.S)
    if m:
        rep = {'type': 'daily', 'h': int(m.group(1)), 'm': int(m.group(2)), 'days': [5, 6]}
        return next_daily(rep), rep, rest(m, 3)
    m = re.match(r'^(?:кажд(?:ый|ую|ое|ые)|по|every)\s+([a-zа-я]+(?:\s*,\s*[a-zа-я]+)*)\s+(?:в\s+)?(\d{1,2})[:.](\d{2})\s*(.*)$', low, re.S)
    if m:
        days = [WD.get(x.strip()) for x in m.group(1).split(',')]
        if days and None not in days:
            rep = {'type': 'daily', 'h': int(m.group(2)), 'm': int(m.group(3)), 'days': sorted(set(days))}
            return next_daily(rep), rep, rest(m, 4)
    m = re.match(r'^(?:every|кажд(?:ые|ый|ую))\s+((?:\d+\s*[a-zа-я]+\s*)+)(.*)$', low, re.S)
    if m:
        sec = _dur(m.group(1))
        if sec >= 60:
            return time.time() + sec, {'type': 'every', 'sec': sec}, rest(m, 2)
        return None
    m = re.match(r'^(?:через\s+)?((?:\d+\s*(?:[a-zа-я]+)\s*)+)(.*)$', low, re.S)
    if m and _dur(m.group(1)):
        return time.time() + _dur(m.group(1)), None, rest(m, 2)
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
        return t.timestamp(), None, rest(m, 6)
    m = re.match(r'^(завтра|послезавтра|сегодня)?\s*(?:в\s+)?(\d{1,2})[:.](\d{2})(?!\.\d)\s*(.*)$', low, re.S)
    if m:
        t = now.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
        if m.group(1) == 'завтра':
            t += timedelta(days=1)
        elif m.group(1) == 'послезавтра':
            t += timedelta(days=2)
        elif t <= now:
            t += timedelta(days=1)
        return t.timestamp(), None, rest(m, 4)
    return None


def fmt_at(ts):
    d = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    day = 'сегодня' if d.date() == today else 'завтра' if d.date() == today + timedelta(days=1) else d.strftime('%d.%m.%Y')
    return f"{day} в {d:%H:%M}"


def fmt_rep(rep):
    if not rep:
        return ''
    if rep['type'] == 'every':
        sec = rep['sec']
        for u, n in ((86400, 'д'), (3600, 'ч'), (60, 'мин')):
            if sec % u == 0:
                return f"каждые {sec // u} {n}"
        return f"каждые {sec // 60} мин"
    days = rep['days']
    when = f"{rep['h']:02}:{rep['m']:02}"
    if len(days) == 7:
        return f"каждый день в {when}"
    if days == [0, 1, 2, 3, 4]:
        return f"по будням в {when}"
    if days == [5, 6]:
        return f"по выходным в {when}"
    return ", ".join(WD_NAMES[d] for d in days) + f" в {when}"


# --------------------------------------------------------------------------- выполнение

async def run_item(it, late=0):
    client = engine.client
    chat = it['chat']
    if it['kind'] == 'copy':
        src = await client.get_messages(it['src'][0], ids=it['src'][1])
        if src is None:
            raise RuntimeError("исходное сообщение удалено")
        await client.send_message(chat, src)
        return
    if it['kind'] == 'cmd':
        msg = await client.send_message(chat, it['text'])
        from telethon import events
        ev = events.NewMessage.Event(msg)
        ev._set_client(client)
        await engine._on_new_message(ev)
        return
    await client.send_message(chat, it['text'], link_preview=True)


async def tick():
    now = time.time()
    catch = int(await cfg('catch_up_min', 60)) * 60
    updates = {}                                   # id → новая версия записи | None (удалить)
    for it in await items():
        if it.get('native'):
            if it['at'] < now - 60:                # Telegram уже отправил сам
                updates[it['id']] = None
            continue
        if it.get('paused') or it['at'] > now:
            continue
        late = now - it['at']
        if late <= max(catch, 90):
            try:
                await run_item(it, late)
                it['runs'] = it.get('runs', 0) + 1
                it.pop('error', None)
            except Exception as e:
                it['error'] = str(e)[:150]
                engine.tlog(f"[scheduler] {it['id']}: {e}")
        else:
            engine.tlog(f"[scheduler] {it['id']}: пропущено (опоздание {int(late // 60)} мин)")
        rep = it.get('repeat')
        if rep and rep['type'] == 'every':
            nxt = it['at'] + rep['sec']
            if nxt <= now:
                nxt = now + rep['sec'] - ((now - it['at']) % rep['sec'])
            it['at'] = nxt
        elif rep:
            it['at'] = next_daily(rep)
        updates[it['id']] = it if rep else None
    if updates:
        # пока шли команды, список могли поменять (.sched, кнопки) — применяем только свои изменения
        out = []
        for x in await items():
            if x['id'] not in updates:
                out.append(x)
            elif updates[x['id']] is not None:
                out.append({**x, **{k: updates[x['id']][k] for k in ('at', 'runs', 'error') if k in updates[x['id']]},
                            **({} if 'error' in updates[x['id']] else {'error': None})})
        await save([{k: v for k, v in x.items() if v is not None or k != 'error'} for x in out])


async def loop():
    await asyncio.sleep(3)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            engine.tlog(f"[scheduler] {e!r}")
        await asyncio.sleep(5)


# --------------------------------------------------------------------------- команды

async def _chat_title(chat):
    try:
        ent = await engine.client.get_entity(chat)
        if getattr(ent, 'is_self', False):
            return 'Избранное'
        return getattr(ent, 'title', None) or ' '.join(x for x in (getattr(ent, 'first_name', '') or '',
                                                                     getattr(ent, 'last_name', '') or '') if x) or str(chat)
    except Exception:
        return str(chat)


async def schedule(event, chat, args):
    parsed = parse_when(args)
    if not parsed:
        await engine.answer(event, visual.err("Не понял, когда") + "\n" + visual.hint(
            ".sched 18:30 текст · .sched завтра 9:00 текст · .sched каждый день 9:00 .команда · .sched по будням 10:00 текст"))
        return
    at, rep, text = parsed
    text = text.strip()
    prefix = await engine.get_prefix()
    reply = await event.get_reply_message() if event.is_reply else None
    if text:
        kind = 'cmd' if text.startswith(prefix) and len(text) > len(prefix) else 'text'
    elif reply is not None:
        kind = 'copy'
    else:
        await engine.answer(event, visual.err("Что отправить? Напишите текст после времени или ответьте на сообщение"))
        return
    if at - time.time() > 365 * 86400:
        await engine.answer(event, visual.err("Дальше чем на год вперёд — нельзя"))
        return
    it = {'id': secrets.token_hex(3), 'chat': chat, 'chat_title': await _chat_title(chat), 'kind': kind, 'text': text[:4000],
          'at': at, 'repeat': rep, 'created': time.time()}
    if kind == 'copy':
        it['src'] = (event.chat_id, reply.id)
        it['text'] = (reply.raw_text or ('медиа' if reply.media else ''))[:200]
    native = kind in ('text', 'copy') and not rep and await cfg('use_native', True) and at - time.time() > 15
    if native:
        try:
            content = reply if kind == 'copy' else text
            msg = await engine.client.send_message(chat, content, schedule=datetime.fromtimestamp(at))
            it['native'] = msg.id if msg is not None else True
        except Exception as e:
            engine.tlog(f"[scheduler] native schedule: {e}")
    lst = await items()
    lst.append(it)
    await save(lst)
    icon = {'cmd': '⚙', 'copy': '📎', 'text': '💬'}[kind]
    where = f" → {_esc(it['chat_title'])}" if chat != event.chat_id else ''
    lines = [visual.ok(("Повтор: " + fmt_rep(rep)) if rep else f"Отправлю {fmt_at(at)}") + where,
             f"{icon} {_esc(it['text'][:200])}"]
    if rep:
        lines.append(visual.hint(f"первый раз — {fmt_at(at)}"))
    if it.get('native'):
        lines.append(visual.hint("стоит в «Отложенных» Telegram — уйдёт и без FRX"))
    lines.append(visual.hint(".scheds — список"))
    await engine.answer(event, "\n".join(lines))


@decorators.command('sched', 'schedule', 'отложить')
async def sched_cmd(event):
    """отложить в этот чат: <когда> <текст | .команда>; ответом — копию сообщения. Повтор: каждый день 9:00, по будням, every 2h"""
    await schedule(event, event.chat_id, engine.get_args())


@decorators.command('schedto', 'отложитьв')
async def schedto_cmd(event):
    """отложить в другой чат: <@чат | id | me> <когда> <текст | .команда>"""
    args = engine.get_args().strip()
    if not args:
        await engine.answer(event, visual.err("Куда и когда?") + "\n" + visual.hint(".schedto @channel 18:00 текст · .schedto me каждый день 9:00 .w"))
        return
    target, _, rest = args.partition(' ')
    try:
        if target.lower() in ('me', 'self', 'избранное'):
            chat = (await engine.client.get_me()).id
        else:
            from telethon.utils import get_peer_id
            ent = await engine.client.get_entity(int(target) if target.lstrip('-').isdigit() else target)
            chat = get_peer_id(ent)
    except Exception as e:
        await engine.answer(event, visual.err("Чат не найден") + f"\n<code>{_esc(str(e)[:200])}</code>")
        return
    await schedule(event, chat, rest)


@decorators.command('scheds', 'schedlist', 'отложенные')
async def scheds_cmd(event):
    """список отложенного в этом чате; all — во всех чатах"""
    show_all = engine.get_args().strip().lower() in ('all', 'все', 'всё')
    chat = event.chat_id

    async def current():
        return [x for x in await items() if show_all or x['chat'] == chat]

    async def text():
        lst = await current()
        if not lst:
            return visual.hint("Ничего не запланировано — .sched 18:00 текст")
        out = [visual.title("Отложенное", sub=("все чаты" if show_all else "этот чат") + f" · {len(lst)}")]
        for i, x in enumerate(lst[:15], 1):
            icon = {'cmd': '⚙', 'copy': '📎', 'text': '💬'}[x['kind']]
            when = (fmt_rep(x['repeat']) + f", дальше {fmt_at(x['at'])}") if x.get('repeat') else fmt_at(x['at'])
            flags = (" ⏸" if x.get('paused') else '') + (" ☁" if x.get('native') else '')
            where = f" → {_esc(x.get('chat_title', ''))}" if show_all else ''
            out.append(f"<b>{i}.</b> {icon} {when}{flags}{where}\n    {_esc(x['text'][:90])}")
            if x.get('error'):
                out.append(visual.warn(f"   ошибка: {_esc(x['error'])}"))
        if len(lst) > 15:
            out.append(visual.hint(f"и ещё {len(lst) - 15}"))
        out.append(visual.hint("☁ — в «Отложенных» Telegram · ⏸ — на паузе"))
        return "\n".join(out)

    async def buttons():
        rows = []
        for i, x in enumerate((await current())[:15], 1):
            row = []
            if not x.get('native'):
                row.append((f"▶ {i}", f"sc:run:{x['id']}", 'primary'))
            if x.get('repeat'):
                row.append((("▶ " if x.get('paused') else "⏸ ") + str(i), f"sc:pause:{x['id']}"))
            row.append((f"✕ {i}", f"sc:del:{x['id']}", 'danger'))
            rows.append(row)
        rows.append([(visual.btn('Закрыть', 'close'), "sc:close")])
        return rows

    form = await engine.create_inlineform(event.chat_id, await text(), buttons=await buttons())
    await event.delete()

    @form.on_click(data_startswith='sc:')
    async def _(click):
        _p, op, *rest = click.data.split(':')
        if op == 'close':
            await form.delete()
            return
        rid = rest[0] if rest else ''
        lst = await items()
        it = next((x for x in lst if x['id'] == rid), None)
        if it is None:
            await click.answer("Уже нет")
        elif op == 'del':
            if it.get('native') and isinstance(it['native'], int):
                try:
                    from telethon.tl.functions.messages import DeleteScheduledMessagesRequest
                    await engine.client(DeleteScheduledMessagesRequest(peer=await engine.client.get_input_entity(it['chat']),
                                                                       id=[it['native']]))
                except Exception as e:
                    engine.tlog(f"[scheduler] delete native: {e}")
            await save([x for x in lst if x['id'] != rid])
            await click.answer("Удалено")
        elif op == 'pause':
            it['paused'] = not it.get('paused')
            if not it['paused'] and it['at'] < time.time():
                it['at'] = next_daily(it['repeat']) if it['repeat']['type'] == 'daily' else time.time() + it['repeat']['sec']
            await save(lst)
            await click.answer("На паузе" if it['paused'] else "Продолжаем")
        elif op == 'run':
            try:
                await run_item(it)
                await click.answer("Выполнено")
            except Exception as e:
                await click.answer(f"Ошибка: {str(e)[:150]}", show_alert=True)
        await form.edit(await text(), buttons=await buttons())
