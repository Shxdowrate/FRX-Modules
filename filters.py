"""
Filters — заметки-автоответы для чатов.

  .filter <ключ>        ответом на сообщение: сохранить его (текст, фото, видео, файлы — всё) как заметку этого чата.
                        Когда кто-то (и вы тоже) напишет сообщение с ключом — модуль ответит сохранённым сообщением.
  .filter <ключ> <текст> без ответа — заметка из текста.
  .filter -g <ключ>     глобальная заметка — работает во всех чатах.
  .fp <ключ>            настройки заметки инлайн-формой: совпадение, кто может вызвать, белый и чёрный списки,
                        ответом или нет, регистр, пауза от флуда.
  .filters              заметки этого чата (и глобальные) — список с настройками.
  .stop <ключ>          удалить заметку; .stopall — все заметки чата.

Сами сообщения хранятся в закрытом канале «FRX Filters» (создаётся сам при первой заметке) — так сохраняются файлы
любого размера, а в базе лежит только ссылка на сообщение и настройки.
"""

import asyncio
import html
import re
import time

import db
import decorators
import engine
import visual

NAME = "Filters"
MODULE_NAME = "filters"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Заметки-автоответы: .filter ключ ответом на сообщение — и модуль отвечает им, когда в чате пишут ключ"

MODES = [('in', 'содержит ключ'), ('exact', 'точно ключ'), ('start', 'начинается с ключа'), ('word', 'ключ отдельным словом')]
WHO = [('all', 'все'), ('me', 'только я'), ('white', 'белый список'), ('black', 'все, кроме чёрного списка')]
GLOBAL = 'global'

_sent = {}          # (chat_id, msg_id) отправленных модулем копий — чтобы не реагировать на самих себя
_last = {}          # (chat_id, key) -> время последнего срабатывания


def _esc(x):
    return html.escape(str(x), quote=False)


def _label(options, value):
    return dict(options).get(value, value)


def _next(options, value):
    keys = [k for k, _ in options]
    return keys[(keys.index(value) + 1) % len(keys)] if value in keys else keys[0]


# --------------------------------------------------------------------------- хранилище

async def _storage():
    """Закрытый канал «FRX Filters» для сообщений-заметок (создаётся один раз)."""
    from telethon.tl.functions.channels import CreateChannelRequest
    cid = await db.get('storage')
    if cid:
        try:
            return await engine.client.get_entity(cid)
        except Exception:
            pass
    r = await engine.client(CreateChannelRequest(title='FRX Filters', about='Хранилище заметок модуля Filters — не удаляйте', megagroup=False))
    ch = r.chats[0]
    await db.set('storage', ch.id if str(ch.id).startswith('-100') else int(f"-100{ch.id}"))
    try:                                   # убрать канал в архив, чтобы не мешал в списке чатов
        from telethon.tl.functions.folders import EditPeerFoldersRequest
        from telethon.tl.types import InputFolderPeer
        await engine.client(EditPeerFoldersRequest([InputFolderPeer(peer=await engine.client.get_input_entity(ch), folder_id=1)]))
    except Exception:
        pass
    return ch


async def _store_message(msg):
    """Кладёт сообщение в хранилище: пересылкой (если чат запрещает пересылку — скачивает и отправляет заново)."""
    st = await _storage()
    try:
        fwd = await engine.client.forward_messages(st, msg, drop_author=True)
        return fwd.id
    except Exception:
        pass
    try:
        copy = await engine.client.send_message(st, msg)
        return copy.id
    except Exception:
        pass
    file = await msg.download_media(file=bytes) if msg.media else None
    sent = await engine.client.send_file(st, file, caption=msg.text or '', formatting_entities=msg.entities) if file \
        else await engine.client.send_message(st, msg.text or '…', formatting_entities=msg.entities)
    return sent.id


async def _store_text(text):
    st = await _storage()
    return (await engine.client.send_message(st, text, parse_mode='html')).id


async def _load(scope):
    return dict(await db.get(f'f:{scope}', {}) or {})


async def _save(scope, data):
    if data:
        await db.set(f'f:{scope}', data)
    else:
        await db.delete(f'f:{scope}')


def _new_filter(key, msg_id):
    return {'key': key, 'msg': msg_id, 'mode': 'in', 'who': 'all', 'white': [], 'black': [], 'reply': True,
            'case': False, 'cooldown': 3, 'uses': 0, 'created': int(time.time())}


# --------------------------------------------------------------------------- команды

@decorators.command('filter')
async def filter_cmd(event):
    """сохранить заметку: ответом на сообщение <ключ>, или <ключ> <текст>; -g — для всех чатов"""
    args = engine.get_args().strip()
    scope = event.chat_id
    if args.startswith('-g ') or args == '-g':
        scope, args = GLOBAL, args[2:].strip()
    reply = await event.get_reply_message() if event.is_reply else None
    if not args:
        await engine.answer(event, visual.err("Укажите ключ") + "\n" + visual.hint(".filter ключ — ответом на сообщение; .filter ключ текст — без ответа"))
        return
    if reply is None:
        key, _, text = args.partition(' ')
        if not text.strip():
            await engine.answer(event, visual.err("Ответьте на сообщение или напишите текст после ключа"))
            return
    else:
        key, text = args, None
    key = key.strip()
    if len(key) > 64:
        await engine.answer(event, visual.err("Слишком длинный ключ (до 64 символов)"))
        return
    await engine.answer(event, visual.wait("Сохраняю…"))
    try:
        msg_id = await _store_message(reply) if reply is not None else await _store_text(text)
    except Exception as e:
        await engine.answer(event, visual.err("Не удалось сохранить") + f"\n<code>{_esc(e)}</code>")
        return
    data = await _load(scope)
    old = data.get(key.lower())
    f = _new_filter(key, msg_id)
    if old:                                     # перезапись — настройки сохраняются
        f.update({k: old[k] for k in ('mode', 'who', 'white', 'black', 'reply', 'case', 'cooldown') if k in old})
    data[key.lower()] = f
    await _save(scope, data)
    where = "во всех чатах" if scope == GLOBAL else "в этом чате"
    await engine.answer(event, visual.ok("Заметка обновлена" if old else "Заметка сохранена") + "\n"
                        + visual.field("ключ", _esc(key)) + "\n" + visual.field("работает", where, code=False) + "\n"
                        + visual.hint(f"настройки — .fp {_esc(key)}"))


@decorators.command('stop', 'unfilter')
async def stop_cmd(event):
    """удалить заметку: <ключ> (сначала ищется в этом чате, потом среди глобальных)"""
    key = engine.get_args().strip().lower()
    for scope in (event.chat_id, GLOBAL):
        data = await _load(scope)
        if key in data:
            f = data.pop(key)
            await _save(scope, data)
            await _drop_stored(f)
            await engine.answer(event, visual.ok("Заметка удалена") + "\n" + visual.field("ключ", _esc(f['key'])))
            return
    await engine.answer(event, visual.err("Такой заметки нет") + "\n" + visual.hint(".filters — список"))


async def _drop_stored(f):
    try:
        await engine.client.delete_messages(await _storage(), [f['msg']])
    except Exception:
        pass


@decorators.command('stopall')
async def stopall_cmd(event):
    """удалить все заметки этого чата (с подтверждением)"""
    data = await _load(event.chat_id)
    if not data:
        await engine.answer(event, visual.hint("В этом чате заметок нет"))
        return
    try:
        form = await engine.create_inlineform(event.chat_id, visual.warn(f"Удалить все заметки этого чата ({len(data)})?"),
                                              buttons=[[("Удалить все", "sa:yes", "danger"), ("Отмена", "sa:no")]])
    except Exception:
        await engine.answer(event, visual.err("Нужен стационарный бот для подтверждения"))
        return
    chat_id = event.chat_id

    @form.on_click(data_startswith='sa:')
    async def _(click):
        if click.data == 'sa:yes':
            d = await _load(chat_id)
            for f in d.values():
                await _drop_stored(f)
            await _save(chat_id, {})
            await form.edit(visual.ok(f"Удалено заметок: {len(d)}"), buttons=None)
        else:
            await form.edit(visual.hint("Отменено"), buttons=None)
        await form.close()


@decorators.command('filters')
async def filters_cmd(event):
    """заметки этого чата и глобальные — список с настройками"""
    chat_id = event.chat_id
    items = []
    for scope in (chat_id, GLOBAL):
        for k, f in sorted((await _load(scope)).items()):
            items.append((scope, k, f))
    if not items:
        await engine.answer(event, visual.hint("Заметок нет") + "\n" + visual.hint(".filter ключ — ответом на сообщение"))
        return
    lines = [visual.title("Заметки", sub=f"в этом чате и глобальные — {len(items)}")]
    for scope, k, f in items:
        lines.append(visual.item(f"<code>{_esc(f['key'])}</code>" + ("  🌐" if scope == GLOBAL else '')
                                 + f"  · {_label(MODES, f['mode'])} · {_label(WHO, f['who'])}"))
    try:
        btns = [(("🌐 " if s == GLOBAL else "") + f['key'][:40], f"fl:{'g' if s == GLOBAL else 'c'}:{k}"[:64]) for s, k, f in items]
        rows, page, total = engine.picker_page(btns, 0, 8, 'flp:')
        rows.append([(visual.btn('Закрыть', 'close'), 'fl:close')])
        form = await engine.create_inlineform(chat_id, "\n".join(lines), buttons=rows)
    except Exception:
        await engine.answer(event, "\n".join(lines))
        return

    @form.on_click(data_startswith='flp:')
    async def _(click):
        rows, _, _ = engine.picker_page(btns, int(click.data[4:]), 8, 'flp:')
        rows.append([(visual.btn('Закрыть', 'close'), 'fl:close')])
        await form.edit("\n".join(lines), buttons=rows)

    @form.on_click(data_startswith='fl:')
    async def _(click):
        if click.data == 'fl:close':
            await form.delete()
            return
        _, sc, key = click.data.split(':', 2)
        await form.close(remove_buttons=False)
        await _settings_form(chat_id, GLOBAL if sc == 'g' else chat_id, key, form=None, reply_to=False)


@decorators.command('fp', 'filterplus')
async def fp_cmd(event):
    """настройки заметки: <ключ>"""
    key = engine.get_args().strip().lower()
    for scope in (event.chat_id, GLOBAL):
        if key and key in await _load(scope):
            await _settings_form(event.chat_id, scope, key)
            return
    await engine.answer(event, visual.err("Такой заметки нет") + "\n" + visual.hint(".filters — список"))


# --------------------------------------------------------------------------- форма настроек

def _settings_text(f, scope):
    lines = [visual.title("Заметка", sub=_esc(f['key'])),
             visual.field("где", "во всех чатах" if scope == GLOBAL else "в этом чате", code=False),
             visual.field("совпадение", _label(MODES, f['mode']), code=False),
             visual.field("кто может вызвать", _label(WHO, f['who']), code=False)]
    if f['who'] == 'white':
        lines.append(visual.field("белый список", ", ".join(map(str, f['white'])) or 'пуст'))
    if f['who'] == 'black':
        lines.append(visual.field("чёрный список", ", ".join(map(str, f['black'])) or 'пуст'))
    lines += [visual.field("отвечать реплаем", "да" if f['reply'] else "нет", code=False),
              visual.field("регистр букв", "важен" if f['case'] else "не важен", code=False),
              visual.field("пауза", f"{f['cooldown']} с"),
              visual.field("срабатываний", f.get('uses', 0))]
    return "\n".join(lines)


def _settings_buttons(f):
    rows = [[(f"Совпадение: {_label(MODES, f['mode'])}", "fs:mode")],
            [(f"Кто: {_label(WHO, f['who'])}", "fs:who")]]
    if f['who'] == 'white':
        rows.append([engine.InputButton("✎ Белый список", "white", hint="@user, 123456 (пусто — очистить)")])
    if f['who'] == 'black':
        rows.append([engine.InputButton("✎ Чёрный список", "black", hint="@user, 123456 (пусто — очистить)")])
    rows += [[(visual.btn('Реплаем', 'on' if f['reply'] else 'off'), "fs:reply"), (visual.btn('Регистр', 'on' if f['case'] else 'off'), "fs:case")],
             [engine.InputButton(f"Пауза: {f['cooldown']} с", "cooldown", hint="секунд, например 5")],
             [("Показать заметку", "fs:show"), ("Удалить", "fs:del", "danger")],
             [(visual.btn('Закрыть', 'close'), "fs:close")]]
    return rows


async def _settings_form(chat_id, scope, key, form=None, reply_to=None):
    data = await _load(scope)
    f = data.get(key)
    if f is None:
        return
    form = await engine.create_inlineform(chat_id, _settings_text(f, scope), buttons=_settings_buttons(f), reply_to=reply_to)

    async def update(mutate):
        d = await _load(scope)
        cur = d.get(key)
        if cur is None:
            await form.edit(visual.warn("Заметку уже удалили"), buttons=None)
            return None
        mutate(cur)
        d[key] = cur
        await _save(scope, d)
        await form.edit(_settings_text(cur, scope), buttons=_settings_buttons(cur))
        return cur

    @form.on_click(data_startswith='fs:')
    async def _(click):
        act = click.data[3:]
        if act == 'mode':
            await update(lambda c: c.__setitem__('mode', _next(MODES, c['mode'])))
        elif act == 'who':
            await update(lambda c: c.__setitem__('who', _next(WHO, c['who'])))
        elif act in ('reply', 'case'):
            await update(lambda c: c.__setitem__(act, not c[act]))
        elif act == 'show':
            cur = (await _load(scope)).get(key)
            if cur:
                await _send(chat_id, cur, None)
            await click.answer("отправлено")
        elif act == 'del':
            d = await _load(scope)
            cur = d.pop(key, None)
            await _save(scope, d)
            if cur:
                await _drop_stored(cur)
            await form.edit(visual.ok("Заметка удалена") + "\n" + visual.field("ключ", _esc(key)), buttons=None)
            await form.close()
        elif act == 'close':
            await form.delete()

    @form.on_input()
    async def _(inp):
        if inp.data == 'cooldown':
            try:
                v = max(0, min(3600, int(float(inp.value.strip().replace(',', '.')))))
            except ValueError:
                v = None
            if v is not None:
                await update(lambda c: c.__setitem__('cooldown', v))
            else:
                cur = (await _load(scope)).get(key)
                await form.edit(_settings_text(cur, scope) + "\n\n" + visual.warn("нужно число секунд"), buttons=_settings_buttons(cur))
        elif inp.data in ('white', 'black'):
            ids = await _resolve_ids(inp.value)
            await update(lambda c: c.__setitem__(inp.data, ids))


async def _resolve_ids(text):
    out = []
    for part in re.split(r'[\s,;]+', text or ''):
        part = part.strip()
        if not part or part == '-':
            continue
        if re.fullmatch(r'-?\d+', part):
            out.append(int(part))
            continue
        try:
            ent = await engine.client.get_entity(part)
            out.append(ent.id)
        except Exception:
            pass
    return sorted(set(out))


# --------------------------------------------------------------------------- срабатывание

def _matches(f, text):
    key = f['key'] if f['case'] else f['key'].lower()
    t = text if f['case'] else text.lower()
    mode = f['mode']
    if mode == 'exact':
        return t.strip() == key
    if mode == 'start':
        return t.lstrip().startswith(key)
    if mode == 'word':
        return re.search(r'(?<!\w)' + re.escape(key) + r'(?!\w)', t) is not None
    return key in t


def _allowed(f, sender_id, out):
    who = f['who']
    if who == 'me':
        return out
    if who == 'white':
        return out or sender_id in f.get('white', [])
    if who == 'black':
        return sender_id not in f.get('black', [])
    return True


async def _send(chat_id, f, reply_to):
    st = await _storage()
    src = await engine.client.get_messages(st, ids=f['msg'])
    if src is None:
        return None
    sent = await engine.client.send_message(chat_id, src, reply_to=reply_to)
    _sent[(chat_id, sent.id)] = time.time()
    if len(_sent) > 500:
        for k in sorted(_sent, key=_sent.get)[:250]:
            _sent.pop(k, None)
    return sent


@decorators.watcher()
async def filters_watcher(event):
    text = event.raw_text or ''
    if not text or (event.chat_id, event.id) in _sent:
        return
    prefix = await engine.get_prefix()
    if text.startswith(prefix):
        return
    found = []
    for scope in (event.chat_id, GLOBAL):
        data = await db.get(f'f:{scope}', {}) or {}
        for k, f in data.items():
            if _matches(f, text) and _allowed(f, event.sender_id, event.out):
                found.append((scope, k, f))
    if not found:
        return
    scope, k, f = max(found, key=lambda x: len(x[2]['key']))           # самый длинный ключ точнее
    now = time.time()
    if now - _last.get((event.chat_id, k), 0) < f.get('cooldown', 3):
        return
    _last[(event.chat_id, k)] = now
    try:
        sent = await _send(event.chat_id, f, event.id if f.get('reply', True) else None)
    except Exception as e:
        engine.tlog(f"[Filters] не отправить «{f['key']}»: {e}")
        return
    if sent is None:
        engine.tlog(f"[Filters] сообщение заметки «{f['key']}» пропало из хранилища")
        return
    data = await db.get(f'f:{scope}', {}) or {}
    if k in data:
        data[k]['uses'] = data[k].get('uses', 0) + 1
        await db.set(f'f:{scope}', data)
