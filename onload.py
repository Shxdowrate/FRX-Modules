"""
Onload — команды, которые FRX выполняет сам при каждом запуске (после перезапуска, обновления, включения компьютера).

  .onload                     список и панель: вкл/выкл, порядок, удалить, «выполнить сейчас»
  .onload add <команда>       добавить (префикс можно не писать): выполнится в ЭТОМ чате
  .onload add me <команда>    выполнится в «Избранном» (там же, где служебное)
  .onload delay <№> <сек>     пауза перед командой (например, дать сети подняться)
  .onload del <№>             удалить;  .onload run — выполнить все сейчас (проверка)

Примеры: «.onload add online on» — сразу «в сети»; «.onload add me sysinfo» — сводка о системе после старта;
«.onload add aura on». Команды выполняются по очереди, после того как подключится стационарный бот (или через 30 с).
Отчёт — от бота: что выполнено, что нет.
"""

import asyncio
import html
import time

import cache
import db
import decorators
import engine
import visual

NAME = "Onload"
MODULE_NAME = "onload"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Автозапуск команд при каждом старте FRX: в нужном чате, по порядку, с паузами и отчётом"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/onload.py"

_TASK = {'t': None}


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='report', description='Присылать от бота отчёт об автозапуске', data_type='bool', default=True)
    await engine.create_config(key='start_delay', description='Подождать после запуска, сек. (и ещё — пока подключится бот)',
                               data_type='int', default=5, valid={'min_value': 0, 'max_value': 600})
    if not await cache.get('booted', False):          # cache живёт до перезапуска FRX: .reloadmod не повторит автозапуск
        await cache.set('booted', True)
        _TASK['t'] = asyncio.ensure_future(_boot())


@decorators.on_unload()
async def cleanup():
    if _TASK['t'] and not _TASK['t'].done():
        _TASK['t'].cancel()


async def items():
    return await db.get('items', []) or []


async def _boot():
    try:
        await asyncio.sleep(int(await engine.get_config('start_delay') or 5))
        for _ in range(30):                              # ждём бота: формы и отчёты без него не работают
            if getattr(engine, '_aiogram_bot', None) is not None:
                break
            await asyncio.sleep(1)
        lst = [it for it in await items() if it.get('on', True)]
        if not lst:
            return
        res = await run_all(lst)
        if await engine.get_config('report'):
            await report(res, "после запуска")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        engine.tlog(f"[onload] {e!r}")


async def run_one(it):
    """Отправить команду в нужный чат и прогнать её через диспетчер FRX (как если бы вы её написали)."""
    from telethon import events
    prefix = await engine.get_prefix()
    cmd = it['cmd'] if it['cmd'].startswith(prefix) else prefix + it['cmd']
    chat = 'me' if it['chat'] in ('me', None) else it['chat']
    msg = await engine.client.send_message(chat, cmd)
    ev = events.NewMessage.Event(msg)
    ev._set_client(engine.client)
    await engine._on_new_message(ev)
    return cmd


async def run_all(lst):
    res = []
    for it in lst:
        if it.get('delay'):
            await asyncio.sleep(int(it['delay']))
        t0 = time.time()
        try:
            cmd = await run_one(it)
            res.append((True, cmd, time.time() - t0))
        except Exception as e:
            res.append((False, it['cmd'], str(e)[:120]))
        await asyncio.sleep(1)
    return res


async def report(res, when):
    lines = [visual.title("Onload", sub=f"{when}: {sum(1 for r in res if r[0])}/{len(res)}")]
    for ok, cmd, info in res:
        lines.append(("✓ " if ok else "✗ ") + f"<code>{_esc(cmd)}</code>" + ("" if ok else f" — {_esc(info)}"))
    text = "\n".join(lines)
    try:
        await engine.bot_send_to_owner(text)
    except Exception:
        engine.tlog("[onload] " + " · ".join(c for _o, c, _i in res))


async def chat_label(chat):
    if chat in ('me', None):
        return "Избранное"
    try:
        ent = await engine.client.get_entity(chat)
        return getattr(ent, 'title', None) or getattr(ent, 'first_name', None) or str(chat)
    except Exception:
        return str(chat)


async def list_text():
    lst = await items()
    lines = [visual.title("Onload", sub=f"команд при запуске: {len(lst)}")]
    for i, it in enumerate(lst, 1):
        lines.append(f"{'◉' if it.get('on', True) else '○'} {i}. <code>{_esc(it['cmd'])}</code> → {_esc(await chat_label(it['chat']))}"
                     + (f" · пауза {it['delay']} с" if it.get('delay') else ""))
    if not lst:
        lines.append(visual.hint("пусто — .onload add <команда>"))
    return "\n".join(lines)


def buttons(lst):
    rows = []
    for i, it in enumerate(lst[:15]):
        rows.append([(f"{'◉' if it.get('on', True) else '○'} {i + 1}. {it['cmd'][:28]}", f"ol:t:{i}"),
                     ("▲", f"ol:u:{i}"), ("✕", f"ol:d:{i}")])
    rows.append([engine.InputButton("＋ Команда (в Избранном)", "add", hint="online on")])
    rows.append([("▶ Выполнить сейчас", "ol:run", "primary"), (visual.btn('Закрыть', 'close'), "ol:x")])
    return rows


@decorators.command('onload', 'автозапуск')
async def onload_cmd(event):
    """команды при каждом запуске: список/панель; add [me] <команда> · del <№> · delay <№> <сек> · run"""
    raw = engine.get_args().strip()
    sub, _, rest = raw.partition(' ')
    sub = sub.lower()
    lst = await items()
    if sub in ('add', '+'):
        target = event.chat_id
        if rest.lower().startswith('me '):
            target, rest = 'me', rest[3:]
        rest = rest.strip()
        if not rest:
            await engine.answer(event, visual.err("Какую команду? .onload add <команда>"))
            return
        name = rest.lstrip(await engine.get_prefix()).split()[0].lower()
        if name in ('restart', 'rs', 'update', 'upd', 'exit', 'onload'):
            await engine.answer(event, visual.err("Эту команду при запуске выполнять нельзя — получится цикл"))
            return
        try:
            known = name in engine.command_map or bool(engine.aliases.get(name))
        except Exception:
            known = True
        if not known:
            await engine.answer(event, visual.warn(f"Команды «{_esc(name)}» сейчас нет — добавил, но проверьте"))
        lst.append({'cmd': rest, 'chat': 'me' if target == 'me' or target == getattr(engine, '_owner_id', None) else target,
                    'on': True, 'delay': 0})
        await db.set('items', lst)
        await engine.answer(event, visual.ok(f"Добавлено: {_esc(rest)}") + "\n" + visual.hint(f"чат: {_esc(await chat_label(lst[-1]['chat']))}"))
        return
    if sub in ('del', 'rm', '-'):
        if not rest.isdigit() or not 1 <= int(rest) <= len(lst):
            await engine.answer(event, visual.err("Номер из .onload"))
            return
        it = lst.pop(int(rest) - 1)
        await db.set('items', lst)
        await engine.answer(event, visual.ok(f"Удалено: {_esc(it['cmd'])}"))
        return
    if sub == 'delay':
        p = rest.split()
        if len(p) != 2 or not all(x.isdigit() for x in p) or not 1 <= int(p[0]) <= len(lst):
            await engine.answer(event, visual.err("Формат: .onload delay <№> <секунд>"))
            return
        lst[int(p[0]) - 1]['delay'] = min(int(p[1]), 3600)
        await db.set('items', lst)
        await engine.answer(event, await list_text())
        return
    if sub in ('run', 'test'):
        await engine.answer(event, visual.wait("Выполняю…"))
        res = await run_all([it for it in lst if it.get('on', True)])
        await report(res, "вручную")
        await engine.answer(event, visual.ok(f"Выполнено {sum(1 for r in res if r[0])}/{len(res)} — отчёт от бота"))
        return
    try:
        form = await engine.create_inlineform(event.chat_id, await list_text(), buttons=buttons(lst), timeout=600)
    except Exception:
        await engine.answer(event, await list_text())
        return
    await event.delete()

    @form.on_click(data_startswith='ol:')
    async def _(click):
        d = click.data[3:]
        cur = await items()
        if d == 'x':
            await form.delete()
            return
        if d == 'run':
            await click.answer("Выполняю…")
            res = await run_all([it for it in cur if it.get('on', True)])
            await report(res, "вручную")
        elif d[:2] in ('t:', 'u:', 'd:'):
            i = int(d[2:])
            if i < len(cur):
                if d[0] == 't':
                    cur[i]['on'] = not cur[i].get('on', True)
                elif d[0] == 'u' and i > 0:
                    cur[i - 1], cur[i] = cur[i], cur[i - 1]
                elif d[0] == 'd':
                    cur.pop(i)
                await db.set('items', cur)
        await form.edit(await list_text(), buttons=buttons(await items()))

    @form.on_input(data='add')
    async def _(inp):
        v = (inp.value or '').strip()
        if v:
            cur = await items()
            cur.append({'cmd': v, 'chat': 'me', 'on': True, 'delay': 0})
            await db.set('items', cur)
        await form.edit(await list_text(), buttons=buttons(await items()))
