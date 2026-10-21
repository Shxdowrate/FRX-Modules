"""
AutoAnon — автопоиск собеседников в анонимном чате (@AnonRuBot и похожие боты).

  .autoanon (.auan)            живая панель: статус, этап, таймер, статистика, журнал и кнопки
  .auan on | off               запустить / остановить автоматику
  .auan next                   сразу перейти к следующему собеседнику
  .auan stats                  статистика текстом
  .auan reset                  обнулить статистику

Как работает: модуль сам отправляет команду поиска, ждёт «Собеседник найден», здоровается, следит за
ответом и скипает молчунов (тайм-аут), собеседников с «стоп-фразами» (автоскип) и ушедших. На ключевое
слово отвечает сам, ставит диалог на паузу и присылает уведомление в личку от бота.
Приветствие можно через N секунд заменить другим текстом (edit_greeting_message_*).
Если собеседник ответил, а включено «pause_on_reply», автоматика замирает — диалог ваш, пока он не закончится.

Все тексты, команды и триггеры — в .cfg AutoAnon.
"""

import asyncio
import html
import random
import time
from collections import deque

import bot
import db
import decorators
import engine
import visual

NAME = "AutoAnon"
MODULE_NAME = "AutoAnon"
VERSION = "2.1"
AUTHOR = "FRX"
DESCRIPTION = "Автопоиск собеседников в анонимном чате @AnonRuBot: приветствие, автоскип, тайм-ауты, уведомления, живая панель"
TG_URL = "https://t.me/mescr_m"

# --------------------------------------------------------------------------- состояние (в памяти)

STAGES = {
    'off':    ('○', 'остановлен'),
    'search': ('🔎', 'ищем собеседника'),
    'greet':  ('👋', 'собеседник найден — ждём ответа'),
    'chat':   ('💬', 'идёт диалог'),
    'paused': ('⏸', 'диалог ваш — автоматика ждёт'),
}

S = {
    'running': False,
    'stage': 'off',
    'gen': 0,                 # растёт при каждом старте/стопе — отложенные действия старого запуска отменяются
    'started': 0.0,
    'activity': 0.0,          # последнее событие в чате (для тайм-аута)
    'stage_since': 0.0,
    'replied': False,         # собеседник уже ответил в этом диалоге
    'notified': False,
    'log': deque(maxlen=6),
    'forms': [],              # открытые панели
    'last_render': 0.0,
    'render_task': None,
    'peer_name': None,        # @username анонимного бота (для кнопки «открыть чат»)
    'dialog': 0,              # номер диалога: растёт на каждого найденного собеседника
    'edit_task': None,        # отложенная правка приветствия
}

DEFAULT_TRIGGER_WORDS = {'💕💕': 'Вижу-вижу, сейчас отвечу!'}
_LOCK = asyncio.Lock()

STAT_KEYS = ('found', 'replied', 'autoskip', 'timeout', 'left', 'keyword', 'manual')
STAT_LABELS = {
    'found': 'найдено собеседников',
    'replied': 'ответили',
    'autoskip': 'скип по стоп-фразе',
    'timeout': 'скип по тайм-ауту',
    'left': 'ушли сами',
    'keyword': 'ключевых слов',
    'manual': 'скипнуто вручную',
}


def _esc(x):
    return html.escape(str(x), quote=False)


def _now():
    return time.time()


def _hm(ts=None):
    return time.strftime('%H:%M', time.localtime(ts or _now()))


def _dur(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- настройки

@decorators.on_load()
async def _create_config(module_info):
    if module_info.module_name != MODULE_NAME:
        return
    cfg = engine.create_config
    await cfg('chat_id', 'ID анонимного бота (по умолчанию — @AnonRuBot)', 'int', 660309226)
    await cfg('search_cmd', 'Сообщение для поиска собеседника', 'str', '🙋‍♀️ Поиск Ж')
    await cfg('next_cmd', 'Команда «следующий собеседник»', 'str', '/next')
    await cfg('stop_cmd', 'Команда «завершить диалог»', 'str', '/stop')
    await cfg('greeting_text', 'Первое сообщение собеседнику — случайное из списка (пусто — не здороваться)', 'list',
              ['Привет! МД?'])
    await cfg('edit_greeting_message_status', 'Через edit_greeting_message_sleep секунд заменить приветствие '
              'текстом edit_greeting_message (если диалог ещё идёт)', 'bool', False)
    await cfg('edit_greeting_message', 'На что заменить приветствие', 'str', '')
    await cfg('edit_greeting_message_sleep', 'Через сколько секунд заменить приветствие', 'int', 10,
              valid={'min_value': 1, 'max_value': 3600})
    await cfg('trigger_search_dialog', 'Тексты бота: идёт поиск (любой из списка, часть сообщения)', 'list',
              ['Ищем девушку...'])
    await cfg('trigger_start_dialog', 'Тексты бота: собеседник найден (любой из списка)', 'list', ['Собеседник найден'])
    await cfg('trigger_dialog_dropped_by_user', 'Тексты бота: диалог завершили вы (любой из списка)', 'list',
              ['Вы закончили связь с вашим собеседником'])
    await cfg('trigger_dialog_dropped_by_peer', 'Тексты бота: диалог завершил собеседник (любой из списка)', 'list',
              ['Собеседник закончил с вами связь'])
    await cfg('autoskip_trigger', 'Стоп-фразы собеседника → сразу скип. Целое сообщение без учёта регистра; '
              '*слово* в звёздочках — если встречается где угодно', 'list', [])
    # ключевые слова → ответ; раньше было одно слово (trigger_word) и один ответ (trigger_word_answer) — переносим
    try:
        await engine.get_config('trigger_words')
        fresh = False
    except KeyError:
        fresh = True
    await cfg('trigger_words', 'Ключевые слова собеседника → ответ (пустой ответ — не отвечать). Слово встретилось — '
              'ответить, поставить диалог на паузу и уведомить вас', 'dict', DEFAULT_TRIGGER_WORDS)
    if fresh:
        try:
            old_word = (await engine.get_config('trigger_word') or '').strip()
        except KeyError:
            old_word = ''
        if old_word:
            try:
                old_answer = await engine.get_config('trigger_word_answer') or ''
            except KeyError:
                old_answer = ''
            await engine.edit_config('trigger_words', 'edit', {old_word: old_answer})
    await cfg('random_wait', 'Максимальная пауза перед действием, сек (имитация человека)', 'int', 5,
              valid={'min_value': 1, 'max_value': 60})
    await cfg('timeout', 'Сколько секунд тишины терпеть, прежде чем скипнуть', 'int', 180,
              valid={'min_value': 15, 'max_value': 3600})
    await cfg('pause_on_reply', 'Собеседник ответил → не скипать по тайм-ауту (диалог ваш)', 'bool', False)
    await cfg('notify_reply', 'Уведомлять в личку, когда собеседник ответил', 'bool', True)


async def _c(key):
    return await engine.get_config(key)


async def _cl(key):
    """Настройка-список (строку тоже понимает — на случай старого значения)."""
    v = await _c(key)
    if isinstance(v, str):
        v = [v]
    return [str(x) for x in (v or []) if str(x).strip()]


def _found(txt, variants):
    """Встречается ли в тексте хоть один вариант (без учёта регистра)."""
    low = (txt or '').casefold()
    return any(v.casefold() in low for v in variants)


# --------------------------------------------------------------------------- статистика и журнал

async def _stats():
    st = await db.get('stats') or {}
    return {k: int(st.get(k, 0)) for k in STAT_KEYS}


async def _bump(key, n=1):
    st = await _stats()
    st[key] = st.get(key, 0) + n
    await db.set('stats', st)


def _log(icon, text):
    S['log'].appendleft((_hm(), icon, text))


def _set_stage(stage):
    S['stage'] = stage
    S['stage_since'] = _now()


def _touch():
    S['activity'] = _now()


# --------------------------------------------------------------------------- панель

async def _panel_text():
    st = await _stats()
    icon, label = STAGES.get(S['stage'], ('·', S['stage']))
    lines = [visual.title("AutoAnon", sub="автопоиск в анонимном чате")]
    if S['running']:
        lines.append(f"{visual.visual('status_on')} <b>Работает</b> · {_dur(_now() - S['started'])}")
        lines.append(f"{icon} {label}")
        if S['stage'] in ('greet', 'chat', 'search'):
            limit = await _c('timeout') * (2 if S['stage'] == 'search' else 1)
            left = limit - (_now() - S['activity'])
            if left > 0:
                bar = engine.progress_bar(100 * left / limit, 10, 'bar')
                what = 'повтор поиска' if S['stage'] == 'search' else 'автоскип'
                lines.append(f"{visual.visual('time')} {what} через <code>{_dur(left)}</code>  {bar}")
    else:
        lines.append(f"{visual.visual('status_off')} <b>Остановлен</b>")
        lines.append(visual.hint("нажмите «Запустить» — модуль сам начнёт поиск"))

    lines += ["", visual.section("Статистика")]
    found = st['found']
    rate = f"  <i>({round(100 * st['replied'] / found)}%)</i>" if found else ''
    lines.append(visual.field('найдено', found) + "   " + visual.field('ответили', st['replied']) + rate)
    skipped = st['autoskip'] + st['timeout'] + st['manual']
    lines.append(visual.field('скипнуто', skipped) + "   " + visual.field('ушли сами', st['left']))
    if st['keyword']:
        lines.append(visual.field('ключевых слов', st['keyword']))

    if S['log']:
        lines += ["", visual.section("Журнал")]
        for hm, ic, tx in list(S['log'])[:5]:
            lines.append(f"<code>{hm}</code> {ic} {tx}")
    return "\n".join(lines)


def _panel_buttons():
    if S['running']:
        rows = [[("⏸ Остановить", "aa:off", "danger")]]
        if S['stage'] == 'paused':
            rows.append([("▶️ Продолжить поиск", "aa:next", "success")])
        else:
            rows.append([("⏭ Следующий", "aa:next", "primary"), ("⏹ Выйти и стоп", "aa:stop")])
    else:
        rows = [[("▶️ Запустить", "aa:on", "success")]]
    rows.append([("🔄 Обновить", "aa:refresh"), ("📊 Подробно", "aa:stats")])
    rows.append([("⚙️ Настройки", "aa:cfg"), (visual.btn('Закрыть', 'close'), "aa:close")])
    return rows


async def _stats_text():
    st = await _stats()
    lines = [visual.title("AutoAnon", sub="статистика за всё время")]
    for k in STAT_KEYS:
        lines.append(visual.field(STAT_LABELS[k], st[k]))
    if st['found']:
        lines += ["", visual.hint(f"отвечает {round(100 * st['replied'] / st['found'])}% собеседников")]
    return "\n".join(lines)


async def _render_now():
    S['last_render'] = time.monotonic()
    if not S['forms']:
        return
    text, buttons = await _panel_text(), _panel_buttons()
    for form in list(S['forms']):
        if getattr(form, '_closed', False):
            S['forms'].remove(form)
            continue
        try:
            await form.edit(text, buttons=buttons)
        except RuntimeError:
            if form in S['forms']:
                S['forms'].remove(form)
        except Exception as e:
            if 'not modified' not in str(e).lower():
                engine.tlog(f"[AutoAnon] панель: {type(e).__name__}: {e}")


async def _render_later():
    try:
        wait = 1.5 - (time.monotonic() - S['last_render'])
        if wait > 0:
            await asyncio.sleep(wait)
        await _render_now()
    except asyncio.CancelledError:
        pass


def _refresh():
    """Перерисовать панели не чаще раза в 1,5 с (события в чате идут пачками)."""
    t = S.get('render_task')
    if t is not None and not t.done():
        return
    S['render_task'] = asyncio.ensure_future(_render_later())


# --------------------------------------------------------------------------- действия

async def _send(text):
    if not text:
        return None
    chat_id = await _c('chat_id')
    _touch()
    return await engine.send_message(chat_id, text)


def _cancel_greeting_edit():
    t = S.get('edit_task')
    if t is not None and not t.done():
        t.cancel()
    S['edit_task'] = None


async def _edit_greeting_later(msg, text, delay, gen, dialog):
    """Через delay секунд заменить приветствие — если это всё ещё тот же диалог и автоматика работает."""
    try:
        await asyncio.sleep(delay)
        if not (S['running'] and S['gen'] == gen and S['dialog'] == dialog and S['stage'] in ('greet', 'chat', 'paused')):
            return
        await msg.edit(text)
        _log('✏️', 'приветствие изменено')
        _refresh()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        engine.tlog(f"[AutoAnon] не удалось изменить приветствие: {type(e).__name__}: {e}")


async def _greet(gen):
    greetings = await _cl('greeting_text')
    if not greetings or not await _human_pause(gen):
        return
    sent = await _send(random.choice(greetings))
    if sent is None or not await _c('edit_greeting_message_status'):
        return
    new_text = (await _c('edit_greeting_message') or '').strip()
    if not new_text:
        return
    _cancel_greeting_edit()
    S['edit_task'] = asyncio.ensure_future(_edit_greeting_later(
        sent, new_text, max(1, int(await _c('edit_greeting_message_sleep'))), gen, S['dialog']))


async def _human_pause(gen):
    """Случайная пауза; False — за это время автоматику остановили/перезапустили."""
    top = max(1, int(await _c('random_wait')))
    await asyncio.sleep(random.uniform(min(1.0, top), top))
    return S['running'] and S['gen'] == gen


async def _peer_link_row():
    name = S.get('peer_name')
    if name is None:
        try:
            ent = await engine.client.get_entity(await _c('chat_id'))
            name = getattr(ent, 'username', None) or ''
        except Exception:
            name = ''
        S['peer_name'] = name
    return [[("↗ Открыть чат", f"url:https://t.me/{name}")]] if name else None


async def _notify(text):
    try:
        await engine.bot_send_to_owner(text, buttons=await _peer_link_row())
    except Exception as e:
        engine.tlog(f"[AutoAnon] уведомление не доставлено: {e}")


async def start():
    if S['running']:
        return
    S['running'] = True
    S['gen'] += 1
    S['started'] = _now()
    S['replied'] = S['notified'] = False
    _touch()
    _set_stage('search')
    _log('▶️', 'запуск — ищем собеседника')
    await db.set('enabled', True)
    _refresh()
    try:
        await _send(await _c('search_cmd'))
    except Exception as e:
        _log('🟥', f"не удалось написать боту: {_esc(str(e)[:80])}")
        await stop(silent=True)
        raise


async def stop(silent=False):
    was = S['running']
    S['running'] = False
    S['gen'] += 1
    _cancel_greeting_edit()
    _set_stage('off')
    if was and not silent:
        _log('⏸', 'остановлено')
    await db.set('enabled', False)
    _refresh()


async def skip_now(reason='manual'):
    """Следующий собеседник прямо сейчас (кнопка / .auan next / продолжить после паузы)."""
    if not S['running']:
        return
    if reason == 'manual' and S['stage'] in ('greet', 'chat'):
        await _bump('manual')
    S['replied'] = S['notified'] = False
    _cancel_greeting_edit()
    _set_stage('search')
    _log('⏭', 'следующий собеседник')
    _refresh()
    await _send(await _c('next_cmd'))


# --------------------------------------------------------------------------- разбор сообщений анонимного бота

def _autoskip_hit(text, triggers):
    low = (text or '').strip().lower()
    if not low:
        return None
    for t in triggers or []:
        t = str(t).strip()
        if not t:
            continue
        if len(t) > 2 and t.startswith('*') and t.endswith('*'):
            if t[1:-1].lower() in low:
                return t
        elif low == t.lower():
            return t
    return None


@decorators.watcher()
async def _watch(event):
    if not S['running']:
        return
    chat_id = await _c('chat_id')
    if event.chat_id != chat_id:
        return
    if event.out:                          # вы пишете сами (или модуль) — это тоже активность
        _touch()
        return
    if event.sender_id != chat_id:
        return
    _touch()
    async with _LOCK:
        await _handle_bot_message(event)
    _refresh()


async def _handle_bot_message(event):
    gen = S['gen']
    txt = event.raw_text or ''
    has_media = bool(getattr(event, 'media', None))

    t_search = await _cl('trigger_search_dialog')
    t_start = await _cl('trigger_start_dialog')
    t_drop_me = await _cl('trigger_dialog_dropped_by_user')
    t_drop_peer = await _cl('trigger_dialog_dropped_by_peer')

    if _found(txt, t_search):
        _set_stage('search')
        return

    if _found(txt, t_start):
        await _bump('found')
        S['replied'] = S['notified'] = False
        S['dialog'] += 1
        _cancel_greeting_edit()
        _set_stage('greet')
        _log('👋', 'собеседник найден')
        _refresh()
        await _greet(gen)
        return

    by_peer = _found(txt, t_drop_peer)
    if by_peer or _found(txt, t_drop_me):
        _cancel_greeting_edit()
        if by_peer:
            await _bump('left')
            _log('🚪', 'собеседник ушёл')
        S['replied'] = S['notified'] = False
        _set_stage('search')
        _refresh()
        if await _human_pause(gen):
            await _send(await _c('search_cmd'))
        return

    # --- всё остальное — сообщения собеседника, которые бот пересылает от своего имени
    if S['stage'] not in ('greet', 'chat', 'paused'):
        return

    hit = _autoskip_hit(txt, await _c('autoskip_trigger'))
    if hit:
        await _bump('autoskip')
        _log('⛔', f"стоп-фраза «{_esc(hit.strip('*')[:30])}» — скип")
        _set_stage('search')
        _refresh()
        if await _human_pause(gen):
            await _send(await _c('next_cmd'))
        return

    preview = _esc((txt or ('[медиа]' if has_media else ''))[:300])
    first = not S['replied']
    if first:
        S['replied'] = True
        await _bump('replied')
        _log('💬', 'собеседник ответил')

    words = await _c('trigger_words')
    if not isinstance(words, dict):
        words = {str(words): ''} if words else {}
    low = txt.casefold()
    word = next((w for w in words if str(w).strip() and str(w).casefold() in low), None)
    if word is not None:
        await _bump('keyword')
        _set_stage('paused')
        S['notified'] = True
        _log('💕', f"ключевое слово «{_esc(str(word)[:20])}» — пауза")
        _refresh()
        await _notify("\n".join([visual.title("AutoAnon", sub="ключевое слово!"),
                                 f"<blockquote>{preview}</blockquote>",
                                 visual.hint("диалог поставлен на паузу и ждёт вас")]))
        answer = str(words.get(word) or '').strip()
        if answer and await _human_pause(gen):
            await _send(answer)
        return

    if first:
        pause = await _c('pause_on_reply')
        _set_stage('paused' if pause else 'chat')
        if await _c('notify_reply') and not S['notified']:
            S['notified'] = True
            await _notify("\n".join([visual.title("AutoAnon", sub="собеседник ответил"),
                                     f"<blockquote>{preview or '…'}</blockquote>",
                                     visual.hint("автоматика на паузе — диалог ваш" if pause
                                                 else "если замолчит — скипну по тайм-ауту")]))


# --------------------------------------------------------------------------- тайм-ауты

@decorators.cycle(interval='5s')
async def _watchdog():
    if not S['running'] or S['stage'] in ('paused', 'off'):
        return
    limit = await _c('timeout')
    if S['stage'] == 'search':
        limit *= 2                          # поиск бывает долгим — не дёргаем бота слишком часто
    if _now() - S['activity'] < limit:
        return
    async with _LOCK:
        if not S['running'] or _now() - S['activity'] < limit:
            return
        if S['stage'] in ('greet', 'chat'):
            await _bump('timeout')
            _log('⌛', 'тишина — скип')
        else:
            _log('🔁', 'поиск затянулся — пробуем снова')
        S['replied'] = S['notified'] = False
        _set_stage('search')
        await _send(await _c('next_cmd'))
    _refresh()


@decorators.on_start()
async def _resume():
    if await db.get('enabled'):
        _log('▶️', 'возобновлено после перезапуска')
        S['running'] = False
        try:
            await start()
        except Exception as e:
            engine.tlog(f"[AutoAnon] не удалось возобновить: {e}")


@decorators.on_unload()
async def _unload():
    S['running'] = False
    S['gen'] += 1
    _cancel_greeting_edit()
    t = S.get('render_task')
    if t is not None and not t.done():
        t.cancel()
    S['forms'].clear()


# --------------------------------------------------------------------------- команда и панель

@decorators.command('autoanon', 'auan')
async def autoanon_cmd(event):
    """панель автопоиска: [on|off|next|stats|reset]"""
    arg = engine.get_args().strip().lower()
    if arg in ('on', 'start', 'вкл', 'старт'):
        try:
            await start()
            await engine.answer(event, visual.ok("AutoAnon запущен") + "\n" + visual.hint("ищем собеседника…"))
        except Exception as e:
            await engine.answer(event, visual.err("Не удалось начать") + f"\n<code>{_esc(str(e)[:200])}</code>")
        return
    if arg in ('off', 'stop', 'выкл', 'стоп'):
        await stop()
        await engine.answer(event, visual.ok("AutoAnon остановлен"))
        return
    if arg in ('next', 'skip', 'скип', 'дальше'):
        if not S['running']:
            await engine.answer(event, visual.warn("AutoAnon не запущен") + "\n" + visual.hint(".auan on"))
            return
        await skip_now()
        await engine.answer(event, visual.ok("Следующий собеседник"))
        return
    if arg in ('stats', 'стат', 'статистика'):
        await engine.answer(event, await _stats_text())
        return
    if arg in ('reset', 'сброс'):
        await db.set('stats', {})
        _refresh()
        await engine.answer(event, visual.ok("Статистика обнулена"))
        return
    await _open_panel(event)


async def _open_panel(event):
    try:
        form = await engine.create_inlineform(event.chat_id, await _panel_text(), buttons=_panel_buttons())
    except Exception as e:
        await engine.answer(event, await _panel_text() + "\n\n" + visual.warn("кнопки недоступны: нужен стационарный бот")
                            + f"\n<code>{_esc(str(e)[:150])}</code>\n" + visual.hint(".auan on | off | next | stats"))
        return
    S['forms'].append(form)
    try:
        await event.delete()
    except Exception:
        pass

    @form.on_click(data_startswith='aa:')
    async def _(click):
        act = click.data[3:]
        if act == 'close':
            if form in S['forms']:
                S['forms'].remove(form)
            await form.delete()
            return
        if act == 'stats':
            await click.answer()
            await form.edit(await _stats_text(), buttons=[
                [("🧹 Обнулить", "aa:reset", "danger")],
                [(visual.btn('Назад', 'back'), "aa:refresh")]])
            return
        if act == 'reset':
            await db.set('stats', {})
            await click.answer("Статистика обнулена")
            await form.edit(await _stats_text(), buttons=[[(visual.btn('Назад', 'back'), "aa:refresh")]])
            return
        if act == 'cfg':
            await click.answer()
            try:
                await engine.exec_modcmd(event, 'config', 'config', MODULE_NAME)
            except Exception:
                prefix = await engine.get_prefix()
                await click.answer(f"Откройте {prefix}cfg {MODULE_NAME}", show_alert=True)
            return
        if act == 'on':
            try:
                await start()
                await click.answer("Запущено — ищем собеседника")
            except Exception as e:
                await click.answer(f"Не удалось: {str(e)[:150]}", show_alert=True)
        elif act == 'off':
            await stop()
            await click.answer("Остановлено")
        elif act == 'next':
            if S['running']:
                await skip_now()
                await click.answer("Ищем следующего")
        elif act == 'stop':
            if S['running'] and S['stage'] in ('greet', 'chat'):
                await _bump('manual')
            await stop(silent=True)
            _log('⏹', 'диалог завершён, автоматика остановлена')
            await _send(await _c('stop_cmd'))
            await click.answer("Диалог завершён, AutoAnon остановлен")
        else:
            await click.answer()
        await _render_now()
