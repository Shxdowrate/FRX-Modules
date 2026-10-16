"""
AlwaysOnline — аккаунт всё время «в сети», даже когда все приложения Telegram закрыты.
Сам по себе статус «в сети» ничего не читает: у собеседников не появляются «прочитано».

По желанию — автопрочтение: модуль отмечает прочитанными новые сообщения в выбранных видах чатов
(личка, группы, каналы, боты), кроме чатов из чёрного списка.

  .online                 панель: включить / выключить, что читать, чёрный список
  .online on | off        включить / выключить «всегда в сети»
  .online bl              добавить этот чат в чёрный список автопрочтения (повторно — убрать)
"""

import asyncio
import html
import time

import decorators
import engine
import visual

NAME = "AlwaysOnline"
MODULE_NAME = "alwaysonline"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Всегда в сети без чтения сообщений; по желанию — автопрочтение личек, групп, каналов, ботов с чёрным списком"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/alwaysonline.py"

KINDS = [('read_private', '👤 Личка'), ('read_groups', '👥 Группы'), ('read_channels', '📢 Каналы'), ('read_bots', '🤖 Боты')]
_state = {'last_ping': 0.0, 'last_read': {}, 'loop': None}


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config('enabled', 'Всегда в сети', 'bool', False)
    await engine.create_config('interval', 'Как часто напоминать Telegram, что вы в сети, секунд', 'int', 50,
                               valid={'min_value': 20, 'max_value': 240})
    await engine.create_config('read_private', 'Автопрочтение: личные сообщения людей', 'bool', False)
    await engine.create_config('read_groups', 'Автопрочтение: группы', 'bool', False)
    await engine.create_config('read_channels', 'Автопрочтение: каналы', 'bool', False)
    await engine.create_config('read_bots', 'Автопрочтение: чаты с ботами', 'bool', False)
    await engine.create_config('read_only_when_online', 'Автопрочтение только когда «всегда в сети» включён', 'bool', True)
    await engine.create_config('blacklist', 'Чаты без автопрочтения (id)', 'list', [])
    _start_loop()


@decorators.on_unload()
async def cleanup():
    t = _state.get('loop')
    if t is not None and not t.done():
        t.cancel()
    _state['loop'] = None


def _start_loop():
    t = _state.get('loop')
    if t is not None and not t.done():
        return
    _state['loop'] = asyncio.ensure_future(_loop())


async def _set_status(online):
    from telethon.tl.functions.account import UpdateStatusRequest
    await engine.client(UpdateStatusRequest(offline=not online))


async def _loop():
    """Своя петля вместо @cycle — интервал берётся из настройки и меняется на лету."""
    while True:
        try:
            if await engine.get_config('enabled'):
                await _set_status(True)
                _state['last_ping'] = time.time()
            interval = await engine.get_config('interval') or 50
        except asyncio.CancelledError:
            raise
        except Exception as e:
            engine.tlog(f"[AlwaysOnline] статус: {e}")
            interval = 60
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- автопрочтение

async def _kind(event):
    if event.is_private:
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        return 'read_bots' if getattr(sender, 'bot', False) else 'read_private'
    if event.is_group:
        return 'read_groups'
    if event.is_channel:
        return 'read_channels'
    return None


@decorators.watcher()
async def autoread(event):
    if event.out:
        return
    try:
        cfg = {k: await engine.get_config(k) for k, _ in KINDS}
    except Exception:
        return
    if not any(cfg.values()):
        return
    if await engine.get_config('read_only_when_online') and not await engine.get_config('enabled'):
        return
    kind = await _kind(event)
    if not kind or not cfg.get(kind):
        return
    if event.chat_id in [int(x) for x in (await engine.get_config('blacklist') or []) if str(x).lstrip('-').isdigit()]:
        return
    cid = event.chat_id
    top = _state.setdefault('max_id', {})
    top[cid] = max(top.get(cid, 0), event.id)
    now = time.monotonic()
    last = _state['last_read'].get(cid, 0)
    if now - last < 3:                 # не чаще раза в 3 с на чат: пачка сообщений читается одним запросом
        await asyncio.sleep(3 - (now - last))
        if _state['last_read'].get(cid, 0) > last:
            return                     # уже прочитал кто-то из пачки — его max_id включает и это сообщение
    _state['last_read'][cid] = time.monotonic()
    try:                               # в группах заодно гасим счётчик упоминаний «@»
        await engine.client.send_read_acknowledge(cid, max_id=top[cid],
                                                  clear_mentions=bool(event.is_group and event.mentioned))
    except Exception as e:
        engine.tlog(f"[AlwaysOnline] прочтение {event.chat_id}: {e}")


# --------------------------------------------------------------------------- панель и команда

async def _panel_text(note=''):
    on = await engine.get_config('enabled')
    bl = await engine.get_config('blacklist') or []
    lines = [visual.title("AlwaysOnline", sub="в сети" if on else "выключен")]
    lines.append(visual.field("всегда в сети", "◉ да" if on else "○ нет", code=False))
    reads = [label for key, label in KINDS if await engine.get_config(key)]
    lines.append(visual.field("автопрочтение", ", ".join(reads) if reads else "выключено", code=False))
    if reads and await engine.get_config('read_only_when_online'):
        lines.append(visual.hint("читает только пока включён «всегда в сети»"))
    lines.append(visual.field("чёрный список", f"{len(bl)} чат(ов)", code=False))
    if _state['last_ping'] and on:
        lines.append(visual.hint(f"последний сигнал «в сети» — {int(time.time() - _state['last_ping'])} с назад"))
    if note:
        lines += ["", note]
    return "\n".join(lines)


async def _panel_buttons():
    on = await engine.get_config('enabled')
    rows = [[("◉ Всегда в сети" if on else "○ Всегда в сети", "ao:toggle", "success" if on else None)]]
    row = []
    for key, label in KINDS:
        v = await engine.get_config(key)
        row.append((("◉ " if v else "○ ") + label, f"ao:k:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    rid = await engine.get_config('read_only_when_online')
    rows.append([(("◉ " if rid else "○ ") + "Читать только когда в сети", "ao:rid")])
    rows.append([("🚫 Чёрный список", "ao:bl"), (visual.btn('Закрыть', 'close'), "ao:close")])
    return [[b if b[2:] and b[2] else b[:2] for b in r] for r in rows]


async def _chat_title(cid):
    try:
        ent = await engine.client.get_entity(int(cid))
        return getattr(ent, 'title', None) or " ".join(x for x in (getattr(ent, 'first_name', ''), getattr(ent, 'last_name', '')) if x) or str(cid)
    except Exception:
        return str(cid)


@decorators.command('online', 'aon')
async def online_cmd(event):
    """всегда в сети: [on | off | bl] — без аргументов панель; bl — этот чат в чёрный список автопрочтения"""
    a = engine.get_args().strip().lower()
    if a in ('on', 'вкл', '1'):
        await engine.edit_config('enabled', 'edit', True)
        await _set_status(True)
        _state['last_ping'] = time.time()
        _start_loop()
        await engine.answer(event, visual.ok("Всегда в сети — включено"))
        return
    if a in ('off', 'выкл', '0'):
        await engine.edit_config('enabled', 'edit', False)
        try:
            await _set_status(False)
        except Exception:
            pass
        await engine.answer(event, visual.ok("Всегда в сети — выключено"))
        return
    if a in ('bl', 'black', 'чс'):
        bl = [int(x) for x in (await engine.get_config('blacklist') or []) if str(x).lstrip('-').isdigit()]
        if event.chat_id in bl:
            await engine.edit_config('blacklist', 'del', event.chat_id)
            await engine.answer(event, visual.ok("Чат убран из чёрного списка автопрочтения"))
        else:
            await engine.edit_config('blacklist', 'add', event.chat_id)
            await engine.answer(event, visual.ok("Чат в чёрном списке — здесь автопрочтения не будет"))
        return
    if a:
        await engine.answer(event, visual.err("Формат: .online · .online on · .online off · .online bl"))
        return
    try:
        form = await engine.create_inlineform(event.chat_id, await _panel_text(), buttons=await _panel_buttons())
    except Exception:
        await engine.answer(event, await _panel_text(visual.hint("кнопки — со стационарным ботом; настройки — .cfg alwaysonline")))
        return
    try:
        await event.delete()
    except Exception:
        pass
    chat_id = event.chat_id

    async def show_bl(note=''):
        bl = await engine.get_config('blacklist') or []
        lines = [visual.title("Чёрный список", sub="здесь автопрочтения нет")]
        rows = []
        for cid in bl[:20]:
            t = await _chat_title(cid)
            lines.append(visual.item(f"{_esc(t)}  <code>{cid}</code>"))
            rows.append([(f"✕ {t[:30]}", f"ao:del:{cid}")])
        if not bl:
            lines.append(visual.hint("пусто"))
        if note:
            lines += ["", note]
        in_bl = chat_id in [int(x) for x in bl if str(x).lstrip('-').isdigit()]
        rows.append([("➕ Этот чат" if not in_bl else "✕ Этот чат", "ao:here"),
                     engine.InputButton("➕ По id / @", "bladd", hint="id чата, @username или ссылка")])
        rows.append([(visual.btn('Назад', 'back'), "ao:main")])
        await form.edit("\n".join(lines), buttons=rows)

    @form.on_click(data_startswith='ao:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
            return
        if d == 'toggle':
            on = not await engine.get_config('enabled')
            await engine.edit_config('enabled', 'edit', on)
            try:
                await _set_status(on)
                _state['last_ping'] = time.time() if on else 0
            except Exception:
                pass
            _start_loop()
            await click.answer("в сети" if on else "выключено")
        elif d.startswith('k:'):
            key = d[2:]
            await engine.edit_config(key, 'edit', not await engine.get_config(key))
            await click.answer()
        elif d == 'rid':
            await engine.edit_config('read_only_when_online', 'edit', not await engine.get_config('read_only_when_online'))
            await click.answer()
        elif d == 'bl':
            await click.answer()
            await show_bl()
            return
        elif d == 'here':
            bl = [int(x) for x in (await engine.get_config('blacklist') or []) if str(x).lstrip('-').isdigit()]
            await engine.edit_config('blacklist', 'del' if chat_id in bl else 'add', chat_id)
            await show_bl()
            return
        elif d.startswith('del:'):
            try:
                await engine.edit_config('blacklist', 'del', int(d[4:]))
            except Exception:
                pass
            await show_bl()
            return
        elif d == 'main':
            await click.answer()
        await form.edit(await _panel_text(), buttons=await _panel_buttons())

    @form.on_input(data='bladd')
    async def _(inp):
        v = (inp.value or '').strip()
        try:
            cid = int(v) if v.lstrip('-').isdigit() else (await engine.client.get_peer_id(v))
            await engine.edit_config('blacklist', 'add', cid)
            await show_bl(visual.ok(f"Добавлен {_esc(await _chat_title(cid))}"))
        except Exception as e:
            await show_bl(visual.err("Не нашёл чат") + f" <code>{_esc(str(e)[:120])}</code>")
