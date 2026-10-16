"""
FastNotes — быстрые заметки по #имени.

  #имя в вашем сообщении      — сообщение дополняется текстом заметки (сообщение из одного #имя целиком заменяется им)
  #имя от разрешённых людей   — модуль отвечает им текстом заметки
  @бот n [имя]                — заметки в инлайн-режиме: выбрали — текст ушёл в чат (без имени — все заметки)

  .notes                      менеджер: список, просмотр, правка, переименование, удаление, люди с доступом,
                              включение, бэкап — всё кнопками
  .nadd <имя> [текст]         добавить (без текста — ответом на сообщение)
  .ndel <имя>                 удалить
  .nlist                      список текстом
  .nuser                      ответом или <id/@user> — дать / забрать доступ к вашим заметкам
  .nbackup                    бэкап в «Избранное» файлом JSON;  .nrestore — ответом на файл бэкапа

Идея — модуль FastNotes автора @mescr_m для Hikka; здесь — с инлайн-менеджером.
"""

import asyncio
import html
import io
import json
import re
import time

import bot
import db
import decorators
import engine
import visual

NAME = "FastNotes"
MODULE_NAME = "fastnotes"
VERSION = "1.0"
AUTHOR = "FRX · идея @mescr_m"
DESCRIPTION = "Быстрые заметки по #имени: раскрываются в ваших сообщениях и отвечают разрешённым людям; менеджер .notes"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/fastnotes.py"

NAME_RE = re.compile(r'^[\w\-]{1,40}$')
TAG_RE = re.compile(r'(?<![\w#])#([\w\-]{1,40})')


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='sleep', description='Пауза между раскрытием нескольких заметок, сек',
                               data_type='float', default=0.6, valid={'min_value': 0, 'max_value': 10})
    await engine.create_config(key='replace_single', description='Сообщение из одного #имя заменять заметкой целиком',
                               data_type='bool', default=True)


async def _notes():
    return dict(await db.get('notes', {}) or {})


async def _set_notes(n):
    await db.set('notes', n)


async def _on():
    return bool(await db.get('on', True))


async def _users():
    return list(await db.get('users', []) or [])


def _block(name, text):
    return f"<b>#{_esc(name)}</b>\n{text}"


# --------------------------------------------------------------------------- раскрытие

@decorators.watcher()
async def notes_watcher(event):
    text = event.raw_text or ''
    if '#' not in text or not await _on():
        return
    if text.startswith(await engine.get_prefix()):
        return
    notes = await _notes()
    tags = [t.lower() for t in TAG_RE.findall(text)]
    hits = []
    for t in tags:
        key = next((k for k in notes if k.lower() == t), None)
        if key and key not in hits:
            hits.append(key)
    if not hits:
        return
    pause = await engine.get_config('sleep')
    if event.out:
        if len(hits) == 1 and text.strip().lower() == f"#{hits[0].lower()}" and await engine.get_config('replace_single'):
            await event.edit(notes[hits[0]], parse_mode='html')
            return
        body = _esc(text)
        for i, k in enumerate(hits):
            body += "\n\n" + _block(k, notes[k])
            await event.edit(body, parse_mode='html')
            if i < len(hits) - 1:
                await asyncio.sleep(pause)
        return
    if event.sender_id not in await _users():
        return
    body, msg = '', None
    for i, k in enumerate(hits):
        body += ("\n\n" if body else "") + _block(k, notes[k])
        msg = await event.reply(body, parse_mode='html') if msg is None else await msg.edit(body, parse_mode='html')
        if i < len(hits) - 1:
            await asyncio.sleep(pause)


# --------------------------------------------------------------------------- инлайн @бот n

@bot.inline_query('n', 'note', 'notes', access='self')
async def notes_inline(query):
    """заметки: [имя] — выбрал, и текст ушёл в чат"""
    notes = await _notes()
    q = query.text.strip().lstrip('#').lower()
    items = [(k, v) for k, v in sorted(notes.items()) if not q or q in k.lower()]
    if not items:
        return [engine.InlineResult("Заметок нет" if not notes else "Ничего не нашлось",
                                    text=visual.hint("Заметки FastNotes: .nadd имя текст"))]
    out = []
    for k, v in items[:50]:
        plain = re.sub(r'<[^>]+>', '', v)
        out.append(engine.InlineResult(f"#{k}", description=plain[:120], text=v))
    return out


# --------------------------------------------------------------------------- команды

@decorators.command('nadd', 'notesadd')
async def nadd_cmd(event):
    """добавить заметку: <имя> [текст] (без текста — ответом на сообщение)"""
    args = engine.get_args().strip()
    name, _, text = args.partition(' ')
    name = name.lstrip('#')
    if not NAME_RE.match(name or ''):
        await engine.answer(event, visual.err("Имя — одно слово: буквы, цифры, _ и -") + "\n" + visual.hint(".nadd имя текст"))
        return
    if not text.strip():
        reply = await event.get_reply_message() if event.is_reply else None
        if reply is None or not (reply.text or reply.raw_text):
            await engine.answer(event, visual.err("Нужен текст: после имени или ответом на сообщение с текстом"))
            return
        from telethon.extensions import html as tl_html
        text = tl_html.unparse(reply.raw_text or '', reply.entities or [])      # с оформлением, в HTML
    notes = await _notes()
    existed = any(k.lower() == name.lower() for k in notes)
    for k in [k for k in notes if k.lower() == name.lower()]:
        notes.pop(k)
    notes[name] = text
    await _set_notes(notes)
    await engine.answer(event, visual.ok("Заметка обновлена" if existed else "Заметка добавлена") + "\n"
                        + visual.field("вызов", f"#{_esc(name)}"))


@decorators.command('ndel', 'notesdel')
async def ndel_cmd(event):
    """удалить заметку: <имя>"""
    name = engine.get_args().strip().lstrip('#').lower()
    notes = await _notes()
    key = next((k for k in notes if k.lower() == name), None)
    if not key:
        await engine.answer(event, visual.err("Такой заметки нет"))
        return
    notes.pop(key)
    await _set_notes(notes)
    await engine.answer(event, visual.ok("Заметка удалена") + "\n" + visual.field("имя", f"#{_esc(key)}"))


@decorators.command('nlist', 'noteslist')
async def nlist_cmd(event):
    """список заметок; full — с текстом"""
    notes = await _notes()
    if not notes:
        await engine.answer(event, visual.hint("Заметок пока нет — .nadd имя текст"))
        return
    full = engine.get_args().strip().lower() == 'full'
    lines = [visual.title("Заметки", sub=f"{len(notes)} · {'включены' if await _on() else 'выключены'}")]
    for k, v in sorted(notes.items()):
        lines.append(visual.item(f"<code>#{_esc(k)}</code>") + (f"\n{v}" if full else ''))
    await engine.answer(event, "\n".join(lines))


@decorators.command('nuser', 'notesuser')
async def nuser_cmd(event):
    """дать / забрать доступ к заметкам: ответом или <id/@user>"""
    uid = await _target_id(event, engine.get_args().strip())
    if uid is None:
        await engine.answer(event, visual.err("Ответьте на сообщение человека или укажите id / @username"))
        return
    users = await _users()
    if uid in users:
        users.remove(uid)
        verb = "Доступ забран"
    else:
        users.append(uid)
        verb = "Доступ выдан"
    await db.set('users', users)
    await engine.answer(event, visual.ok(verb) + "\n" + visual.field("кто", await _name(uid), code=False))


async def _target_id(event, arg):
    if arg:
        if re.fullmatch(r'\d+', arg):
            return int(arg)
        try:
            return (await engine.client.get_entity(arg)).id
        except Exception:
            return None
    if event.is_reply:
        r = await event.get_reply_message()
        return r.sender_id if r else None
    return None


async def _name(uid):
    try:
        u = await engine.client.get_entity(uid)
        return _esc(" ".join(filter(None, [getattr(u, 'first_name', None), getattr(u, 'last_name', None)])) or getattr(u, 'title', '') or uid)
    except Exception:
        return str(uid)


@decorators.command('nbackup', 'notesbackup')
async def nbackup_cmd(event):
    """бэкап заметок файлом в «Избранное»"""
    data = {'type': 'frx-fastnotes', 'version': 1, 'created': int(time.time()), 'notes': await _notes(), 'users': await _users()}
    f = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=1).encode('utf-8'))
    f.name = f"fastnotes_{time.strftime('%Y%m%d_%H%M')}.json"
    await engine.client.send_file('me', f, caption=f"🗒 Бэкап FastNotes — {len(data['notes'])} заметок\nВосстановить: ответом .nrestore")
    await engine.answer(event, visual.ok("Бэкап отправлен в «Избранное»"))


@decorators.command('nrestore', 'notesrestore')
async def nrestore_cmd(event):
    """восстановить заметки: ответом на файл бэкапа (заметки с теми же именами перезапишутся)"""
    r = await event.get_reply_message() if event.is_reply else None
    if r is None or not r.file:
        await engine.answer(event, visual.err("Ответьте на файл бэкапа .json"))
        return
    try:
        raw = await r.download_media(file=bytes)
        data = json.loads(raw.decode('utf-8'))
        if data.get('type') != 'frx-fastnotes' or not isinstance(data.get('notes'), dict):
            raise ValueError("это не бэкап FastNotes")
    except Exception as e:
        await engine.answer(event, visual.err("Не удалось прочитать бэкап") + f"\n<code>{_esc(e)}</code>")
        return
    notes = await _notes()
    notes.update({str(k): str(v) for k, v in data['notes'].items() if NAME_RE.match(str(k))})
    await _set_notes(notes)
    users = sorted(set(await _users()) | {int(u) for u in data.get('users', []) if str(u).isdigit()})
    await db.set('users', users)
    await engine.answer(event, visual.ok("Заметки восстановлены") + "\n" + visual.field("всего", len(notes)))


# --------------------------------------------------------------------------- менеджер

@decorators.command('notes')
async def notes_cmd(event):
    """менеджер заметок: список, правка, доступ, бэкап — кнопками"""
    try:
        form = await engine.create_inlineform(event.chat_id, visual.wait("…"), buttons=[[("…", "nm:noop")]])
    except Exception:
        await nlist_cmd(event)
        return
    state = {'page': 0, 'view': 'list', 'cur': None}

    async def show_list(note=''):
        notes = await _notes()
        on = await _on()
        state.update(view='list', cur=None)
        lines = [visual.title("FastNotes", sub=f"{len(notes)} заметок · {'включены' if on else 'выключены'}")]
        if not notes:
            lines.append(visual.hint("пока пусто — «➕ Добавить» или .nadd имя текст"))
        if note:
            lines += ["", visual.ok(_esc(note))]
        items = [(f"#{k}", f"nm:o:{k}"[:64]) for k in sorted(notes)]
        rows, state['page'], _ = engine.picker_page(items, state['page'], 8, 'nm:p:')
        rows.append([engine.InputButton("➕ Добавить", "add", hint="имя текст заметки")])
        rows.append([(visual.btn('Включены', 'on' if on else 'off'), "nm:toggle"), ("👥 Доступ", "nm:users")])
        rows.append([("💾 Бэкап в Избранное", "nm:backup"), (visual.btn('Закрыть', 'close'), "nm:close")])
        await form.edit("\n".join(lines), buttons=rows)

    async def show_note(key, note=''):
        notes = await _notes()
        if key not in notes:
            await show_list("заметки уже нет")
            return
        state.update(view='note', cur=key)
        text = notes[key]
        lines = [visual.title(f"#{_esc(key)}"), text if len(text) < 3500 else text[:3500] + "…"]
        if note:
            lines += ["", visual.ok(_esc(note))]
        rows = [[engine.InputButton("✎ Изменить текст", "edit", hint="новый текст"),
                 engine.InputButton("✎ Переименовать", "rename", hint="новое имя")],
                [("Удалить", "nm:del", "danger")],
                [(visual.btn('К списку', 'back'), "nm:list")]]
        await form.edit("\n".join(lines), buttons=rows)

    async def show_users(note=''):
        users = await _users()
        state.update(view='users')
        lines = [visual.title("Доступ к заметкам", sub="эти люди получают ответ на #имя"), ""]
        if not users:
            lines.append(visual.hint("никого — добавьте кнопкой ниже или .nuser ответом"))
        names = {u: await _name(u) for u in users}
        for u in users:
            lines.append(visual.item(f"{names[u]} · <code>{u}</code>"))
        if note:
            lines += ["", visual.ok(_esc(note))]
        rows = [[(f"✕ {names[u][:30]}", f"nm:ud:{u}")] for u in users]
        rows.append([engine.InputButton("➕ Добавить", "user", hint="@username или id")])
        rows.append([(visual.btn('К списку', 'back'), "nm:list")])
        await form.edit("\n".join(lines), buttons=rows)

    await show_list()

    @form.on_click(data_startswith='nm:')
    async def _(click):
        d = click.data[3:]
        if d.startswith('p:'):
            state['page'] = int(d[2:])
            await show_list()
        elif d.startswith('o:'):
            await show_note(d[2:])
        elif d == 'list':
            await show_list()
        elif d == 'toggle':
            await db.set('on', not await _on())
            await show_list()
        elif d == 'users':
            await show_users()
        elif d.startswith('ud:'):
            users = await _users()
            uid = int(d[3:])
            if uid in users:
                users.remove(uid)
                await db.set('users', users)
            await show_users("доступ забран")
        elif d == 'del' and state['cur']:
            notes = await _notes()
            notes.pop(state['cur'], None)
            await _set_notes(notes)
            await show_list(f"#{state['cur']} удалена")
        elif d == 'backup':
            data = {'type': 'frx-fastnotes', 'version': 1, 'created': int(time.time()), 'notes': await _notes(), 'users': await _users()}
            f = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=1).encode('utf-8'))
            f.name = f"fastnotes_{time.strftime('%Y%m%d_%H%M')}.json"
            await engine.client.send_file('me', f, caption="🗒 Бэкап FastNotes — восстановить: ответом .nrestore")
            await click.answer("бэкап в «Избранном»")
        elif d == 'close':
            await form.delete()

    @form.on_input()
    async def _(inp):
        v = (inp.value or '').strip()
        notes = await _notes()
        if inp.data == 'add':
            name, _, text = v.lstrip('#').partition(' ')
            if not NAME_RE.match(name) or not text.strip():
                await show_list("нужно: имя (одно слово) и текст через пробел")
                return
            notes[name] = _esc(text.strip())
            await _set_notes(notes)
            await show_note(name, "добавлена")
        elif inp.data == 'edit' and state['cur']:
            notes[state['cur']] = _esc(v) if v else notes.get(state['cur'], '')
            await _set_notes(notes)
            await show_note(state['cur'], "текст изменён")
        elif inp.data == 'rename' and state['cur']:
            new = v.lstrip('#')
            if not NAME_RE.match(new) or any(k.lower() == new.lower() for k in notes if k != state['cur']):
                await show_note(state['cur'], "имя занято или неверное")
                return
            notes[new] = notes.pop(state['cur'])
            await _set_notes(notes)
            await show_note(new, "переименовано")
        elif inp.data == 'user':
            try:
                uid = int(v) if v.isdigit() else (await engine.client.get_entity(v)).id
            except Exception:
                await show_users("не нашёл такого пользователя")
                return
            users = await _users()
            if uid not in users:
                users.append(uid)
                await db.set('users', users)
            await show_users("доступ выдан")
