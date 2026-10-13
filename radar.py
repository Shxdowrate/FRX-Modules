"""
Radar — следит за всеми чатами и сообщает вам, когда вас упомянули, ответили вам или написали ключевое слово.
Уведомление приходит от бота (со звуком) или в «Избранное» — с текстом, чатом, автором и кнопкой «перейти».

  .radar                      панель: что отслеживается, вкл/выкл, источники, тихие часы
  .radar add <слово, фраза>   ключевые слова (через запятую; регистр не важен; короткие — только целым словом)
  .radar re <регулярка>       ключ-регулярка, например  (?i)\bfrx\b
  .radar del <№|слово>        удалить ключ;  .radar list — список
  .radar ignore [@кто|id]     в чате — не следить за этим чатом (повтор — снова следить); с @кто — игнорировать человека
  .radar log                  последние 30 срабатываний
  .radar on | off             включить / выключить

Что ловит (переключается в панели): упоминания @вас, ответы на ваши сообщения, ключевые слова.
Где: личка / группы / каналы — настройка sources. Не дублирует: один чат — не чаще раза в cooldown секунд на один ключ,
и молчит про чат, где вы сами писали последние 2 минуты (вы и так там). Тихие часы — уведомления без звука.
"""

import html
import re
import time
from datetime import datetime

import db
import decorators
import engine
import visual

NAME = "Radar"
MODULE_NAME = "radar"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Упоминания, ответы и ключевые слова во всех чатах — уведомлением от бота с кнопкой «перейти»"

SOURCES = ['личные чаты', 'группы', 'каналы']
_LAST_OUT = {}      # chat_id -> когда вы последний раз писали
_COOL = {}          # (chat_id, ключ) -> время уведомления
_COMPILED = {}      # шаблон -> regex


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    c = engine.create_config
    await c(key='dest', description='Куда уведомлять: bot — от бота (со звуком), me — в «Избранное» (с кнопками действий)',
            data_type='choice', default='bot', options=['bot', 'me'])
    await c(key='sources', description='Где искать ключевые слова', data_type='choices', default=['группы', 'каналы'],
            options=SOURCES)
    await c(key='cooldown', description='Не чаще раза в N секунд на один чат и ключ', data_type='int', default=60,
            valid={'min_value': 0, 'max_value': 86400})
    await c(key='skip_active', description='Молчать про чат, где вы сами писали последние 2 минуты', data_type='bool', default=True)
    await c(key='quiet', description='Тихие часы (уведомления без звука), например 23-8; пусто — нет', data_type='str', default='',
            valid={'max_len': 11})


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


async def state():
    st = await db.get('state', None) or {}
    st.setdefault('on', True)
    st.setdefault('mentions', True)
    st.setdefault('replies', True)
    st.setdefault('words', [])
    st.setdefault('ignore_chats', [])
    st.setdefault('ignore_users', [])
    return st


async def save(st):
    await db.set('state', st)


def matcher(w):
    key = (w['p'], w['re'])
    if key not in _COMPILED:
        if w['re']:
            try:
                _COMPILED[key] = re.compile(w['p'])
            except re.error:
                _COMPILED[key] = None
        else:
            p = re.escape(w['p'])
            _COMPILED[key] = re.compile(rf'(?<!\w){p}(?!\w)' if len(w['p']) <= 4 else p, re.I)
    return _COMPILED[key]


def kind(event):
    if event.is_private:
        return 'личные чаты'
    if event.is_group:
        return 'группы'
    return 'каналы'


def in_quiet(spec):
    m = re.match(r'^\s*(\d{1,2})\s*[-–]\s*(\d{1,2})\s*$', spec or '')
    if not m:
        return False
    a, b, h = int(m.group(1)) % 24, int(m.group(2)) % 24, datetime.now().hour
    return a <= h < b if a < b else (h >= a or h < b)


def msg_link(chat_id, msg_id, username=None):
    if username:
        return f"https://t.me/{username}/{msg_id}"
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{msg_id}" if s.startswith('-100') else None


def name_of(ent):
    if ent is None:
        return '?'
    if getattr(ent, 'title', None):
        return ent.title
    return " ".join(x for x in (getattr(ent, 'first_name', '') or '', getattr(ent, 'last_name', '') or '') if x) or str(ent.id)


# --------------------------------------------------------------------------- слежка

@decorators.watcher()
async def watch(event):
    if event.out:
        if not getattr(event.message, 'via_bot_id', None):
            _LAST_OUT[event.chat_id] = time.time()
        return
    st = await state()
    if not st['on']:
        return
    chat_id, sid = event.chat_id, event.sender_id
    if chat_id in st['ignore_chats'] or sid in st['ignore_users'] or sid == getattr(engine, '_owner_id', None):
        return
    try:
        if getattr(await event.get_sender(), 'bot', False) and event.is_private:
            return                                      # личка с ботами — не наш случай
    except Exception:
        pass
    text = event.raw_text or ''
    why = None
    if st['mentions'] and getattr(event.message, 'mentioned', False) and not event.is_private:
        reply = await event.get_reply_message() if event.is_reply else None
        if reply is not None and reply.out:
            if st['replies']:
                why = ('reply', 'ответ на ваше сообщение')
        else:
            why = ('mention', 'вас упомянули')
    if why is None and st['words'] and kind(event) in (await cfg('sources', []) or []):
        for w in st['words']:
            rx = matcher(w)
            if rx is not None and rx.search(text):
                why = ('w:' + w['p'], f"ключ «{w['p'][:40]}»")
                break
    if why is None:
        return
    if await cfg('skip_active', True) and time.time() - _LAST_OUT.get(chat_id, 0) < 120:
        return
    cd = int(await cfg('cooldown', 60))
    key = (chat_id, why[0])
    if cd and time.time() - _COOL.get(key, 0) < cd:
        return
    _COOL[key] = time.time()
    await notify(event, why[1], text)


async def notify(event, why, text):
    chat = await event.get_chat()
    sender = await event.get_sender()
    sid = event.sender_id
    link = msg_link(event.chat_id, event.id, getattr(chat, 'username', None))
    who = f'<a href="tg://user?id={sid}">{_esc(name_of(sender))}</a>' if sid and sid > 0 else _esc(name_of(sender))
    body = text[:700] + ('…' if len(text) > 700 else '') if text else '[медиа]'
    card = "\n".join([f"📡 <b>{_esc(why)}</b>",
                      visual.field("чат", _esc(name_of(chat)), code=False),
                      visual.field("кто", who, code=False), "",
                      f"<blockquote>{_esc(body)}</blockquote>"])
    log = await db.get('log', []) or []
    log.append({'t': int(time.time()), 'why': why, 'chat': name_of(chat), 'cid': event.chat_id, 'who': name_of(sender),
                'text': (text or '[медиа]')[:200], 'link': link})
    await db.set('log', log[-30:])
    dest = await cfg('dest', 'bot')
    silent = in_quiet(await cfg('quiet', ''))
    if dest == 'bot' and getattr(engine, '_aiogram_bot', None) is not None:
        try:
            buttons = [[("↗ Перейти", f"url:{link}")]] if link else None
            await engine.bot.raw.send_message(getattr(engine, '_owner_id'), engine.safe_html(card), parse_mode='HTML',
                                              disable_notification=silent,
                                              reply_markup=engine.build_inline_markup(buttons) if buttons else None,
                                              link_preview_options=_no_preview())
            return
        except Exception as e:
            engine.tlog(f"[radar] бот: {e}")
    chat_id = event.chat_id
    rows = ([[("↗ Перейти", f"url:{link}")]] if link else []) + [
        [("🔕 Не следить за чатом", "ic"), ("🙈 Игнорировать автора", "iu")], [("✓", "x")]]
    try:
        form = await engine.create_inlineform(getattr(engine, '_owner_id'), card, buttons=rows, reply_to=False, timeout=86400)
    except Exception:
        await engine.client.send_message('me', engine.safe_html(card), parse_mode='html', silent=silent)
        return

    @form.on_click()
    async def _(click):
        st = await state()
        if click.data == 'ic' and chat_id not in st['ignore_chats']:
            st['ignore_chats'].append(chat_id)
            note = "чат больше не отслеживается"
        elif click.data == 'iu' and sid not in st['ignore_users']:
            st['ignore_users'].append(sid)
            note = "автор в игноре"
        else:
            note = ""
        await save(st)
        await form.edit(card + ("\n\n" + visual.ok(note) if note else ""), buttons=None)
        await form.close()


def _no_preview():
    try:
        from aiogram.types import LinkPreviewOptions
        return LinkPreviewOptions(is_disabled=True)
    except Exception:
        return None


# --------------------------------------------------------------------------- команды

async def panel_text():
    st = await state()
    lines = [visual.title("Radar", sub="включён" if st['on'] else "выключен"),
             visual.field("упоминания", "да" if st['mentions'] else "нет", code=False),
             visual.field("ответы вам", "да" if st['replies'] else "нет", code=False),
             visual.field("ключи ищутся в", ", ".join(await cfg('sources', []) or []) or "нигде", code=False),
             visual.field("куда", "от бота" if await cfg('dest', 'bot') == 'bot' else "в «Избранное»", code=False),
             visual.field("тихие часы", await cfg('quiet', '') or "нет", code=False),
             visual.field("игнор", f"{len(st['ignore_chats'])} чатов · {len(st['ignore_users'])} людей", code=False),
             visual.section(f"Ключи · {len(st['words'])}")]
    for i, w in enumerate(st['words'], 1):
        lines.append(f"{i}. {'<i>re</i> ' if w['re'] else ''}<code>{_esc(w['p'])}</code>")
    if not st['words']:
        lines.append(visual.hint("нет — .radar add слово, фраза"))
    return "\n".join(lines)


async def panel_buttons():
    st = await state()
    src = await cfg('sources', []) or []
    return [
        [("● Включён" if st['on'] else "○ Выключен", "rd:on", 'success' if st['on'] else 'danger')],
        [(("✓ " if st['mentions'] else "") + "Упоминания", "rd:t:mentions"), (("✓ " if st['replies'] else "") + "Ответы", "rd:t:replies")],
        [(("✓ " if s in src else "") + s.split()[0].capitalize(), f"rd:s:{i}") for i, s in enumerate(SOURCES)],
        [("Куда: " + ("бот" if await cfg('dest', 'bot') == 'bot' else "Избранное"), "rd:dest")],
        [engine.InputButton("＋ Ключи через запятую", "add", hint="frx, юзербот, мой ник")],
        [("✕ Удалить ключ", "rd:del"), ("🧹 Сбросить игнор", "rd:unign")],
        [("🗒 Журнал", "rd:log"), (visual.btn('Закрыть', 'close'), "rd:close")],
    ]


async def log_text():
    log = list(reversed(await db.get('log', []) or []))
    lines = [visual.title("Radar", sub=f"последние {len(log)}")]
    for e in log:
        when = datetime.fromtimestamp(e['t']).strftime('%d.%m %H:%M')
        title = f'<a href="{_esc(e["link"])}">{_esc(e["chat"][:30])}</a>' if e.get('link') else _esc(e['chat'][:30])
        lines.append(f"▸ {when} · {title} · {_esc(e['who'][:20])} — {_esc(e['why'])}\n   <i>{_esc(e['text'][:80])}</i>")
    if not log:
        lines.append(visual.hint("пока ничего"))
    return "\n".join(lines)


@decorators.command('radar', 'радар')
async def radar_cmd(event):
    """слежка за упоминаниями и ключами: панель; add · re · del · list · ignore · log · on/off"""
    raw = engine.get_args().strip()
    sub, _, rest = raw.partition(' ')
    sub = sub.lower()
    st = await state()
    if sub in ('on', 'off', 'вкл', 'выкл'):
        st['on'] = sub in ('on', 'вкл')
        await save(st)
        await engine.answer(event, visual.ok("Radar включён" if st['on'] else "Radar выключен"))
        return
    if sub in ('add', '+'):
        new = [w.strip() for w in rest.split(',') if w.strip()]
        if not new:
            await engine.answer(event, visual.err("Формат: .radar add слово, фраза"))
            return
        for w in new:
            if not any(x['p'] == w.lower() and not x['re'] for x in st['words']):
                st['words'].append({'p': w.lower(), 're': False})
        await save(st)
        await engine.answer(event, visual.ok(f"Ключей: {len(st['words'])}") + "\n" + visual.hint(", ".join(_esc(w) for w in new)))
        return
    if sub in ('re', 'regex'):
        try:
            re.compile(rest)
        except re.error as e:
            await engine.answer(event, visual.err(f"Ошибка в регулярке: {_esc(e)}"))
            return
        st['words'].append({'p': rest, 're': True})
        await save(st)
        await engine.answer(event, visual.ok("Регулярка добавлена"))
        return
    if sub in ('del', 'rm', '-'):
        r = rest.strip().lower()
        if r.isdigit() and 1 <= int(r) <= len(st['words']):
            st['words'].pop(int(r) - 1)
        else:
            before = len(st['words'])
            st['words'] = [w for w in st['words'] if w['p'] != r]
            if len(st['words']) == before:
                await engine.answer(event, visual.err("Нет такого ключа — номер из .radar list или само слово"))
                return
        await save(st)
        await engine.answer(event, visual.ok("Удалено"))
        return
    if sub in ('ignore', 'игнор'):
        target = None
        if rest:
            try:
                target = (await engine.client.get_entity(int(rest) if rest.lstrip('-').isdigit() else rest)).id
            except Exception:
                await engine.answer(event, visual.err("Не нашёл такого"))
                return
        elif event.is_reply:
            target = (await event.get_reply_message()).sender_id
        if target is not None:
            lst = st['ignore_users']
            (lst.remove if target in lst else lst.append)(target)
            await save(st)
            await engine.answer(event, visual.ok("Человек в игноре" if target in lst else "Человек больше не в игноре"))
            return
        lst = st['ignore_chats']
        (lst.remove if event.chat_id in lst else lst.append)(event.chat_id)
        await save(st)
        await engine.answer(event, visual.ok("Этот чат больше не отслеживается" if event.chat_id in lst else "Чат снова отслеживается"))
        return
    if sub in ('log', 'журнал'):
        await engine.answer(event, await log_text())
        return
    if sub in ('list', 'список'):
        await engine.answer(event, await panel_text())
        return
    try:
        form = await engine.create_inlineform(event.chat_id, await panel_text(), buttons=await panel_buttons(), timeout=600)
    except Exception:
        await engine.answer(event, await panel_text())
        return
    await event.delete()
    view = {'del': False}

    async def redraw():
        if view['del']:
            st2 = await state()
            rows = [[(f"✕ {w['p'][:30]}", f"rd:x:{i}")] for i, w in enumerate(st2['words'][:30])]
            rows.append([(visual.btn('Назад', 'back'), "rd:back")])
            await form.edit(visual.title("Radar", sub="какой ключ удалить?"), buttons=rows)
        else:
            await form.edit(await panel_text(), buttons=await panel_buttons())

    @form.on_click(data_startswith='rd:')
    async def _(click):
        d = click.data[3:]
        s2 = await state()
        if d == 'close':
            await form.delete()
            return
        if d == 'log':
            await form.edit(await log_text(), buttons=[[(visual.btn('Назад', 'back'), "rd:back")]])
            return
        if d == 'on':
            s2['on'] = not s2['on']
        elif d.startswith('t:'):
            s2[d[2:]] = not s2[d[2:]]
        elif d.startswith('s:'):
            src = SOURCES[int(d[2:])]
            cur = await cfg('sources', []) or []
            if src in cur and len(cur) == 1:
                await click.answer("Хотя бы один источник должен остаться", show_alert=True)
                return
            await engine.edit_config('sources', 'del' if src in cur else 'add', src)
        elif d == 'dest':
            await engine.edit_config('dest', 'edit', 'me' if await cfg('dest', 'bot') == 'bot' else 'bot')
        elif d == 'unign':
            s2['ignore_chats'], s2['ignore_users'] = [], []
            await click.answer("Игнор сброшен")
        elif d == 'del':
            view['del'] = True
        elif d == 'back':
            view['del'] = False
        elif d.startswith('x:'):
            i = int(d[2:])
            if i < len(s2['words']):
                s2['words'].pop(i)
        await save(s2)
        await redraw()

    @form.on_input(data='add')
    async def _(inp):
        s2 = await state()
        for w in (inp.value or '').split(','):
            w = w.strip().lower()
            if w and not any(x['p'] == w and not x['re'] for x in s2['words']):
                s2['words'].append({'p': w, 're': False})
        await save(s2)
        await redraw()
