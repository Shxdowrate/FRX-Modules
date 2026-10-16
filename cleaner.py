"""
Cleaner — уборка аккаунта. Сначала показывает, что нашёл, и ничего не трогает без вашего «Да».

  .clean            проверить весь аккаунт и открыть панель уборки
  .cleanchat        в группе: найти удалённые аккаунты среди участников и исключить их (нужны права админа)

Что находит панель:
  • переписки с удалёнными аккаунтами          → удалить
  • чаты, откуда вас исключили / которые удалены → убрать из списка
  • каналы и группы без новых сообщений N дней  → покинуть или убрать в архив
  • каналы, которые вы не читаете (много непрочитанного) → покинуть или в архив
  • ботов, с которыми давно не общались          → удалить переписку
  • непрочитанное, упоминания и реакции          → отметить прочитанным
  • черновики                                    → очистить все
  • удалённые аккаунты в контактах               → убрать из контактов
  • кэш FRX (шрифты, эмодзи, превью — скачаются заново при нужде) → очистить
Ваши собственные сообщения модуль не удаляет.
"""

import asyncio
import html
import shutil
import time
from pathlib import Path

import decorators
import engine
import visual

NAME = "Cleaner"
MODULE_NAME = "cleaner"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Уборка аккаунта: мёртвые чаты, неактивные каналы, удалённые аккаунты, непрочитанное, черновики, кэш"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/cleaner.py"

CATS = {
    'deleted_pm': ('Переписки с удалёнными аккаунтами', 'delete'),
    'forbidden': ('Чаты, откуда вас исключили или удалённые', 'delete'),
    'inactive': ('Каналы и группы без новых сообщений', 'leave'),
    'unread_heavy': ('Каналы, которые вы не читаете', 'leave'),
    'bots_old': ('Давно забытые боты', 'delete'),
}


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='inactive_days', description='Чат «неактивный», если в нём нет новых сообщений столько дней',
                               data_type='int', default=180, valid={'min_value': 7, 'max_value': 3650})
    await engine.create_config(key='unread_limit', description='Канал «не читаете», если в нём столько непрочитанных или больше',
                               data_type='int', default=500, valid={'min_value': 50, 'max_value': 100000})
    await engine.create_config(key='keep_owned', description='Никогда не трогать чаты и каналы, где вы создатель или админ',
                               data_type='bool', default=True)


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


def _size(n):
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if n < 1024 or unit == 'ГБ':
            return f"{n:.0f} {unit}" if unit == 'Б' else f"{n:.1f} {unit}"
        n /= 1024


def cache_dir():
    try:
        from core.paths import DATA_DIR
        return Path(DATA_DIR) / 'cache'
    except Exception:
        return Path('data') / 'cache'


def dir_size(p):
    total = 0
    if p.is_dir():
        for f in p.rglob('*'):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    return total


def _title(ent):
    return getattr(ent, 'title', None) or ' '.join(x for x in (getattr(ent, 'first_name', '') or '',
                                                                getattr(ent, 'last_name', '') or '') if x) or 'Удалённый аккаунт'


# --------------------------------------------------------------------------- проверка

async def scan(progress=None):
    from telethon.tl.types import Channel, ChannelForbidden, Chat, ChatForbidden, User
    days = int(await cfg('inactive_days', 180))
    ulimit = int(await cfg('unread_limit', 500))
    keep_owned = await cfg('keep_owned', True)
    cut = time.time() - days * 86400
    found = {k: [] for k in CATS}
    unread, mentions, drafts, n = [], 0, 0, 0
    async for d in engine.client.iter_dialogs(ignore_migrated=True):
        n += 1
        if progress and n % 200 == 0:
            await progress(n)
        e = d.entity
        last = d.date.timestamp() if d.date else 0
        item = {'id': d.id, 'title': _title(e), 'archived': d.folder_id == 1}
        owned = bool(getattr(e, 'creator', False) or getattr(e, 'admin_rights', None))
        if d.unread_count or getattr(d, 'unread_mentions_count', 0) or getattr(d.dialog, 'unread_reactions_count', 0):
            unread.append(d.id)
            mentions += (getattr(d, 'unread_mentions_count', 0) or 0) + (getattr(d.dialog, 'unread_reactions_count', 0) or 0)
        if d.draft and getattr(d.draft, 'text', None):
            drafts += 1
        if isinstance(e, User):
            if e.deleted:
                found['deleted_pm'].append(item)
            elif e.bot and last and last < cut:
                found['bots_old'].append(item)
            continue
        if isinstance(e, (ChatForbidden, ChannelForbidden)) or getattr(e, 'deactivated', False) or (
                isinstance(e, Chat) and getattr(e, 'left', False)):
            found['forbidden'].append(item)
            continue
        if keep_owned and owned:
            continue
        if isinstance(e, (Channel, Chat)):
            if last and last < cut:
                found['inactive'].append(item)
            elif isinstance(e, Channel) and e.broadcast and d.unread_count >= ulimit:
                item['unread'] = d.unread_count
                found['unread_heavy'].append(item)
    return {'found': found, 'unread': unread, 'mentions': mentions, 'drafts': drafts, 'total': n,
            'cache': dir_size(cache_dir()), 'contacts': await deleted_contacts(), 'at': time.time()}


async def deleted_contacts():
    from telethon.tl.functions.contacts import GetContactsRequest
    try:
        res = await engine.client(GetContactsRequest(hash=0))
        return [u.id for u in getattr(res, 'users', []) if getattr(u, 'deleted', False)]
    except Exception:
        return []


# --------------------------------------------------------------------------- действия

async def act_dialogs(items, how, status=None):
    """how: delete | leave | archive. → (готово, ошибок)"""
    ok = bad = 0
    for i, it in enumerate(items):
        try:
            if how == 'archive':
                await engine.client.edit_folder(it['id'], 1)
            else:
                await engine.client.delete_dialog(it['id'], revoke=False)
            ok += 1
        except Exception as e:
            sec = getattr(e, 'seconds', None)
            if sec and sec < 120:
                await asyncio.sleep(sec + 1)
                try:
                    await (engine.client.edit_folder(it['id'], 1) if how == 'archive' else engine.client.delete_dialog(it['id']))
                    ok += 1
                    continue
                except Exception:
                    pass
            bad += 1
            engine.tlog(f"[cleaner] {how} {it['title']}: {e}")
        await asyncio.sleep(0.6)
        if status and i % 10 == 9:
            await status(i + 1, len(items))
    return ok, bad


async def read_all(ids, status=None):
    ok = 0
    for i, cid in enumerate(ids):
        try:
            await engine.client.send_read_acknowledge(cid, clear_mentions=True, clear_reactions=True)
            ok += 1
        except Exception as e:
            sec = getattr(e, 'seconds', None)
            if sec and sec < 120:
                await asyncio.sleep(sec + 1)
        await asyncio.sleep(0.35)
        if status and i % 20 == 19:
            await status(i + 1, len(ids))
    return ok


async def clear_drafts():
    from telethon.tl.functions.messages import ClearAllDraftsRequest
    await engine.client(ClearAllDraftsRequest())


async def drop_contacts(ids):
    from telethon.tl.functions.contacts import DeleteContactsRequest
    if ids:
        await engine.client(DeleteContactsRequest(id=ids))


def clear_cache():
    d = cache_dir()
    freed = dir_size(d)
    if d.is_dir():
        for p in d.iterdir():
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError:
                pass
    return freed - dir_size(d)


# --------------------------------------------------------------------------- панель

@decorators.command('clean', 'cleaner', 'уборка')
async def clean_cmd(event):
    """уборка аккаунта: найти мёртвые чаты, неактивные каналы, непрочитанное, черновики — и убрать по кнопке"""
    await engine.answer(event, visual.wait("Проверяю аккаунт…"))

    async def prog(n):
        await engine.answer(event, visual.wait(f"Проверяю аккаунт… {n} чатов"))
    try:
        state = {'r': await scan(prog), 'confirm': None, 'log': [], 'busy': False}
    except Exception as e:
        await engine.answer(event, visual.err("Проверка не удалась") + f"\n<code>{_esc(str(e)[:300])}</code>")
        return
    days = int(await cfg('inactive_days', 180))

    def text():
        r = state['r']
        t = [visual.title("Уборка", sub=f"{r['total']} чатов")]
        if state['confirm']:
            key, how = state['confirm']
            items = r['found'][key]
            verb = {'delete': 'Удалить переписки', 'leave': 'Покинуть', 'archive': 'Убрать в архив'}[how]
            t += ["", visual.warn(f"{verb}: {len(items)}?")]
            t += [visual.item(_esc(x['title'][:50]) + (f" · {x['unread']} непрочит." if x.get('unread') else ''))
                  for x in items[:25]]
            if len(items) > 25:
                t.append(visual.hint(f"и ещё {len(items) - 25}"))
            return "\n".join(t)
        for key, (label, _how) in CATS.items():
            n = len(r['found'][key])
            extra = f" ({days}+ дн.)" if key in ('inactive', 'bots_old') else ''
            t.append(f"{'•' if n else '·'} {label}{extra}: <b>{n}</b>")
        t.append(f"{'•' if r['unread'] else '·'} Чатов с непрочитанным: <b>{len(r['unread'])}</b>"
                 + (f" · упоминаний и реакций {r['mentions']}" if r['mentions'] else ''))
        t.append(f"{'•' if r['drafts'] else '·'} Черновиков: <b>{r['drafts']}</b>")
        t.append(f"{'•' if r['contacts'] else '·'} Удалённых аккаунтов в контактах: <b>{len(r['contacts'])}</b>")
        t.append(f"· Кэш FRX: <b>{_size(r['cache'])}</b>")
        if state['log']:
            t.append("")
            t += state['log'][-6:]
        t.append("")
        t.append(visual.hint("ничего не удаляется без подтверждения · настройки: .cfg cleaner"))
        return "\n".join(t)

    def buttons():
        r = state['r']
        if state['busy']:
            return [[("⏳ Работаю…", "cl:noop")]]
        if state['confirm']:
            key, how = state['confirm']
            return [[("Да", "cl:yes", 'danger'), ("Нет", "cl:no")]]
        rows = []
        f = r['found']
        if f['deleted_pm']:
            rows.append([(f"🗑 Удалённые аккаунты ({len(f['deleted_pm'])})", "cl:ask:deleted_pm:delete", 'danger')])
        if f['forbidden']:
            rows.append([(f"🗑 Мёртвые чаты ({len(f['forbidden'])})", "cl:ask:forbidden:delete", 'danger')])
        for key in ('inactive', 'unread_heavy'):
            if f[key]:
                n = len(f[key])
                rows.append([(f"🚪 {'Неактивные' if key == 'inactive' else 'Не читаю'} ({n})", f"cl:ask:{key}:leave", 'danger'),
                             (f"🗄 В архив ({sum(1 for x in f[key] if not x['archived'])})", f"cl:ask:{key}:archive")])
        if f['bots_old']:
            rows.append([(f"🤖 Старые боты ({len(f['bots_old'])})", "cl:ask:bots_old:delete", 'danger')])
        line = []
        if r['unread']:
            line.append((f"✓ Прочитать всё ({len(r['unread'])})", "cl:read", 'primary'))
        if r['drafts']:
            line.append((f"✎ Черновики ({r['drafts']})", "cl:drafts"))
        if line:
            rows.append(line)
        line = []
        if r['contacts']:
            line.append((f"👤 Контакты ({len(r['contacts'])})", "cl:contacts"))
        if r['cache'] > 1024 * 1024:
            line.append((f"🧹 Кэш ({_size(r['cache'])})", "cl:cache"))
        if line:
            rows.append(line)
        rows.append([("⟳ Проверить заново", "cl:rescan"), (visual.btn('Закрыть', 'close'), "cl:close")])
        return rows

    form = await engine.create_inlineform(event.chat_id, text(), buttons=buttons())
    await event.delete()

    async def redraw():
        try:
            await form.edit(text(), buttons=buttons())
        except Exception:
            pass

    async def status(done, total):
        state['log'] = state['log'][:-1] + [visual.wait(f"{done}/{total}…")] if state['log'] and '…' in state['log'][-1] \
            else state['log'] + [visual.wait(f"{done}/{total}…")]
        await redraw()

    def drop_progress():
        state['log'] = [x for x in state['log'] if not x.endswith('…')]

    @form.on_click(data_startswith='cl:')
    async def _(click):
        parts = click.data.split(':')
        op = parts[1]
        r = state['r']
        if op == 'noop':
            await click.answer("Подождите, ещё работаю")
            return
        if state['busy']:
            await click.answer("Подождите, ещё работаю")
            return
        if op == 'close':
            await form.delete()
            return
        if op == 'ask':
            state['confirm'] = (parts[2], parts[3])
            if parts[3] == 'archive':
                r['found'][parts[2]] = [x for x in r['found'][parts[2]] if not x['archived']] + \
                    [x for x in r['found'][parts[2]] if x['archived']]
            await redraw()
            return
        if op == 'no':
            state['confirm'] = None
            await redraw()
            return
        state['busy'] = True
        await redraw()
        try:
            if op == 'yes' and state['confirm']:
                key, how = state['confirm']
                state['confirm'] = None
                items = r['found'][key]
                if how == 'archive':
                    items = [x for x in items if not x['archived']]
                ok, bad = await act_dialogs(items, how, status)
                drop_progress()
                verb = {'delete': 'удалено', 'leave': 'покинуто', 'archive': 'в архиве'}[how]
                state['log'].append(visual.ok(f"{CATS[key][0]}: {verb} {ok}") + (f", не вышло {bad}" if bad else ''))
                if how == 'archive':
                    for x in items:
                        x['archived'] = True
                else:
                    r['found'][key] = [x for x in r['found'][key] if x not in items]
            elif op == 'read':
                ok = await read_all(r['unread'], status)
                drop_progress()
                state['log'].append(visual.ok(f"Прочитано чатов: {ok}"))
                r['unread'], r['mentions'] = [], 0
            elif op == 'drafts':
                await clear_drafts()
                state['log'].append(visual.ok(f"Черновики очищены: {r['drafts']}"))
                r['drafts'] = 0
            elif op == 'contacts':
                await drop_contacts(r['contacts'])
                state['log'].append(visual.ok(f"Убрано из контактов: {len(r['contacts'])}"))
                r['contacts'] = []
            elif op == 'cache':
                freed = await asyncio.to_thread(clear_cache)
                state['log'].append(visual.ok(f"Кэш очищен: {_size(freed)}"))
                r['cache'] = dir_size(cache_dir())
            elif op == 'rescan':
                state['r'] = await scan()
                state['log'].append(visual.ok("Проверено заново"))
        except Exception as e:
            state['log'].append(visual.err(_esc(str(e)[:200])))
        finally:
            state['busy'] = False
        await redraw()


@decorators.command('cleanchat', 'чисткачата')
async def cleanchat_cmd(event):
    """в группе: найти удалённые аккаунты среди участников и исключить (нужно право банить)"""
    if event.is_private:
        await engine.answer(event, visual.err("Это для групп и каналов"))
        return
    await engine.answer(event, visual.wait("Ищу удалённые аккаунты…"))
    try:
        dead = [u async for u in engine.client.iter_participants(event.chat_id) if getattr(u, 'deleted', False)]
    except Exception as e:
        await engine.answer(event, visual.err("Список участников недоступен") + f"\n<code>{_esc(str(e)[:200])}</code>")
        return
    if not dead:
        await engine.answer(event, visual.ok("Удалённых аккаунтов нет"))
        return
    form = await engine.create_inlineform(event.chat_id, visual.warn(f"Удалённых аккаунтов: {len(dead)}. Исключить их?"),
                                          buttons=[[("Исключить", "cc:yes", 'danger'), ("Нет", "cc:no")]])
    await event.delete()

    @form.on_click(data_startswith='cc:')
    async def _(click):
        if click.data == 'cc:no':
            await form.delete()
            return
        await form.edit(visual.wait(f"Исключаю {len(dead)}…"), buttons=[])
        ok = bad = 0
        for u in dead:
            try:
                await engine.client.kick_participant(event.chat_id, u)
                ok += 1
            except Exception as e:
                sec = getattr(e, 'seconds', None)
                if sec and sec < 120:
                    await asyncio.sleep(sec + 1)
                bad += 1
            await asyncio.sleep(0.5)
        await form.edit(visual.ok(f"Исключено: {ok}") + ("\n" + visual.warn(f"не вышло: {bad} — нужны права админа") if bad else ''),
                        buttons=[])
