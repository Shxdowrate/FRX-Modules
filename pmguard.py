"""
PM Guard — защита лички от незнакомцев (с инлайн-кнопками).

Незнакомец (не контакт, не бот, не одобренный) пишет вам в личку — модуль отвечает карточкой: «владелец ещё не
одобрил переписку» и кнопками причины: 💼 по делу · 💬 пообщаться · 🆘 срочно · 📢 реклама. Нажать их может только
сам незнакомец; выбранная причина сразу видна в вашей карточке, «срочно» — ещё и уведомлением от бота.
Продолжает писать — после N сообщений (настройка) блокировка. «Реклама» — блок сразу (спасибо за честность).

Вам в «Избранное» (от бота) приходит карточка: ✓ одобрить · ⛔ заблокировать · 🗑 заблокировать и удалить чат.

  .pmguard      панель: вкл/выкл, лимит сообщений, отчёты, текст, список одобренных
  .allow        в личке или ответом: одобрить человека (больше не трогаем)
  .deny         забрать одобрение;  .block — заблокировать сразу

Ваше первое сообщение человеку — тоже одобрение: с теми, кому вы пишете сами, модуль не спорит.
Без стационарного бота кнопок нет — модуль просто отвечает текстом предупреждения.
"""

import html

import db
import decorators
import engine
import visual

NAME = "PM Guard"
MODULE_NAME = "pmguard"
VERSION = "1.1"
AUTHOR = "FRX"
DESCRIPTION = "Защита лички: незнакомцу — карточка с кнопками причины, после N сообщений — блок; вам — карточка с решением"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/pmguard.py"

REASONS = {'work': '💼 по делу', 'chat': '💬 пообщаться', 'urgent': '🆘 срочно', 'ad': '📢 реклама'}
_warned = {}        # user_id -> форма/сообщение с предупреждением (прошлое удаляем, чтобы не копились)
_cards = {}         # user_id -> (форма-карточка у владельца, её текст)


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='limit', description='Сколько сообщений незнакомцу можно отправить до блокировки',
                               data_type='int', default=4, valid={'min_value': 1, 'max_value': 50})
    await engine.create_config(key='text', description='Текст предупреждения ({осталось} — сколько сообщений до блока)',
                               data_type='str', default='🛡 Привет! Я пока не одобрил переписку с вами — дождитесь ответа.\n'
                                                       'Не пишите много сообщений подряд: осталось {осталось}, потом — блокировка.')
    await engine.create_config(key='report', description='Присылать вам карточку о новом незнакомце', data_type='bool', default=True)
    await engine.create_config(key='buttons', description='Кнопки причины у незнакомца (по делу / пообщаться / срочно / реклама)',
                               data_type='bool', default=True)


async def _on():
    return bool(await db.get('on', True))


async def _approved():
    return set(await db.get('approved', []) or [])


async def _approve(uid, yes=True):
    a = await _approved()
    (a.add if yes else a.discard)(uid)
    await db.set('approved', sorted(a))
    counts = await db.get('counts', {}) or {}
    counts.pop(str(uid), None)
    await db.set('counts', counts)
    reasons = await db.get('reasons', {}) or {}
    if reasons.pop(str(uid), None) is not None:
        await db.set('reasons', reasons)
    await _drop_warning(uid)


async def _drop_warning(uid):
    prev = _warned.pop(uid, None)
    if prev is None:
        return
    try:
        if hasattr(prev, 'delete') and not isinstance(prev, int):
            await prev.delete()
        else:
            await engine.client.delete_messages(uid, [prev])
    except Exception:
        pass


async def _block(uid, wipe=False):
    from telethon.tl.functions.contacts import BlockRequest
    from telethon.tl.functions.messages import DeleteHistoryRequest
    await _approve(uid, False)
    await engine.client(BlockRequest(uid))
    if wipe:
        try:
            await engine.client(DeleteHistoryRequest(peer=uid, max_id=0, revoke=True))
        except Exception:
            pass


async def _target(event):
    if event.is_private and not event.is_reply:
        return event.chat_id
    if event.is_reply:
        r = await event.get_reply_message()
        return r.sender_id if r else None
    arg = engine.get_args().strip()
    if arg:
        try:
            return int(arg) if arg.lstrip('-').isdigit() else (await engine.client.get_entity(arg)).id
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- команды

@decorators.command('allow', 'approve')
async def allow_cmd(event):
    """одобрить переписку: в личке, ответом или <id/@user>"""
    uid = await _target(event)
    if not uid:
        await engine.answer(event, visual.err("Кого одобрить? В личке, ответом или id"))
        return
    await _approve(uid, True)
    await _card_done(uid, visual.ok("одобрен"))
    await engine.answer(event, visual.ok("Переписка одобрена") + "  " + visual.hint("PM Guard больше не тронет этого человека"))


@decorators.command('deny', 'disapprove')
async def deny_cmd(event):
    """забрать одобрение: в личке, ответом или <id/@user>"""
    uid = await _target(event)
    if not uid:
        await engine.answer(event, visual.err("У кого забрать? В личке, ответом или id"))
        return
    await _approve(uid, False)
    await engine.answer(event, visual.ok("Одобрение снято"))


@decorators.command('block')
async def block_cmd(event):
    """заблокировать: в личке, ответом или <id/@user>"""
    uid = await _target(event)
    if not uid:
        await engine.answer(event, visual.err("Кого? В личке, ответом или id"))
        return
    await _block(uid)
    await _card_done(uid, visual.ok("заблокирован"))
    await engine.answer(event, visual.ok("Заблокирован"))


async def _panel_text():
    on = await _on()
    return "\n".join([
        visual.title("PM Guard", sub="включён" if on else "выключен"),
        visual.field("лимит сообщений", await engine.get_config('limit')),
        visual.field("одобрено", len(await _approved())),
        visual.field("ждут решения", len(await db.get('counts', {}) or {})),
        visual.field("карточки вам", "да" if await engine.get_config('report') else "нет", code=False),
        visual.field("кнопки причины", "да" if await engine.get_config('buttons') else "нет", code=False),
        "", visual.section("Предупреждение"), _esc((await engine.get_config('text'))[:600])])


async def _panel_buttons():
    on = await _on()
    return [
        [("⏸ Выключить" if on else "▶ Включить", "pp:on", "danger" if on else "success")],
        [("−", "pp:l:-1"), (f"Лимит: {await engine.get_config('limit')}", "pp:nop"), ("+", "pp:l:1")],
        [("Карточки: " + ("вкл" if await engine.get_config('report') else "выкл"), "pp:rep"),
         ("Кнопки причины: " + ("вкл" if await engine.get_config('buttons') else "выкл"), "pp:btn")],
        [engine.InputButton("✎ Текст предупреждения", "text", hint="новый текст ({осталось} — сколько осталось)")],
        [("👥 Одобренные", "pp:list"), ("🧹 Сбросить счётчики", "pp:reset")],
        [(visual.btn('Закрыть', 'close'), "pp:close")]]


@decorators.command('pmguard', 'pmg')
async def pmguard_cmd(event):
    """панель PM Guard: вкл/выкл, лимит, отчёты, текст, одобренные"""
    arg = engine.get_args().strip().lower()
    if arg in ('on', 'off', 'вкл', 'выкл'):
        await db.set('on', arg in ('on', 'вкл'))
        await engine.answer(event, await _panel_text())
        return
    try:
        form = await engine.create_inlineform(event.chat_id, await _panel_text(), buttons=await _panel_buttons())
    except Exception:
        await db.set('on', not await _on())          # без бота — просто переключатель, как раньше
        await engine.answer(event, await _panel_text() + "\n\n" + visual.hint(".pmguard on|off — включить/выключить"))
        return
    await event.delete()
    st = {'page': 0}

    async def list_view():
        ids = sorted(await _approved())
        per = 8
        pages = max(1, -(-len(ids) // per))
        st['page'] = max(0, min(st['page'], pages - 1))
        chunk = ids[st['page'] * per:(st['page'] + 1) * per]
        lines = [visual.title("Одобренные", sub=f"{len(ids)} · стр. {st['page'] + 1}/{pages}")]
        rows = []
        for uid in chunk:
            try:
                ent = await engine.client.get_entity(uid)
                name = " ".join(filter(None, [getattr(ent, 'first_name', ''), getattr(ent, 'last_name', '')])) or str(uid)
            except Exception:
                name = str(uid)
            lines.append(visual.item(f'<a href="tg://user?id={uid}">{_esc(name)}</a> <code>{uid}</code>'))
            rows.append([(f"✕ {name[:24]}", f"pp:rm:{uid}")])
        if not ids:
            lines.append(visual.hint("пока никого — одобряйте .allow или кнопкой в карточке"))
        nav = ([("◂", "pp:pg:-1")] if st['page'] > 0 else []) + ([("▸", "pp:pg:1")] if st['page'] < pages - 1 else [])
        if nav:
            rows.append(nav)
        rows.append([(visual.btn('Назад', 'back'), "pp:back")])
        return "\n".join(lines), rows

    @form.on_click(data_startswith='pp:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
            return
        if d == 'nop':
            await click.answer()
            return
        if d == 'on':
            await db.set('on', not await _on())
        elif d.startswith('l:'):
            v = max(1, min(50, await engine.get_config('limit') + int(d[2:])))
            await engine.edit_config('limit', 'edit', v)
        elif d == 'rep':
            await engine.edit_config('report', 'edit', not await engine.get_config('report'))
        elif d == 'btn':
            await engine.edit_config('buttons', 'edit', not await engine.get_config('buttons'))
        elif d == 'reset':
            await db.set('counts', {})
            await click.answer("счётчики сброшены")
        elif d in ('list', 'back') or d.startswith(('pg:', 'rm:')):
            if d.startswith('pg:'):
                st['page'] += int(d[3:])
            elif d.startswith('rm:'):
                await _approve(int(d[3:]), False)
                await click.answer("одобрение снято")
            if d != 'back':
                text, rows = await list_view()
                await form.edit(text, buttons=rows)
                return
        await form.edit(await _panel_text(), buttons=await _panel_buttons())

    @form.on_input(data='text')
    async def _(inp):
        value = (inp.value or '').strip()
        if value:
            await engine.edit_config('text', 'edit', value)
        await form.edit(await _panel_text(), buttons=await _panel_buttons())


# --------------------------------------------------------------------------- незнакомцы

@decorators.watcher()
async def pm_watcher(event):
    if not event.is_private or not await _on():
        return
    uid = event.chat_id
    if event.out:                                       # вы пишете первым — значит, человеку можно
        if getattr(event.message, 'via_bot_id', None):  # это наша же карточка через бота, не ваше сообщение
            return
        if uid not in await _approved() and not (event.raw_text or '').startswith(await engine.get_prefix()) \
                and uid not in _warned and str(uid) not in (await db.get('counts', {}) or {}):
            await _approve(uid, True)
        return
    if uid in await _approved() or uid == getattr(engine, '_owner_id', None) or uid == 777000:
        return
    sender = await event.get_sender()
    if sender is None or getattr(sender, 'bot', False) or getattr(sender, 'contact', False) or getattr(sender, 'verified', False) \
            or getattr(sender, 'support', False):
        return
    counts = await db.get('counts', {}) or {}
    n = counts.get(str(uid), 0) + 1
    counts[str(uid)] = n
    await db.set('counts', counts)
    limit = await engine.get_config('limit')
    name = " ".join(filter(None, [sender.first_name, sender.last_name])) or str(uid)
    if n > limit:
        await _drop_warning(uid)
        try:
            await event.reply("🛡 Слишком много сообщений — вы заблокированы. Владелец увидит переписку и решит сам.")
            await _block(uid)
        except Exception:
            pass
        await _report(uid, name, sender, event, blocked=True)
        return
    await _drop_warning(uid)
    text = (await engine.get_config('text')).replace('{осталось}', str(limit - n + 1))
    reason = (await db.get('reasons', {}) or {}).get(str(uid))
    _warned[uid] = await _warn(uid, text, reason, event)
    if n == 1:
        await _report(uid, name, sender, event, blocked=False)


async def _warn(uid, text, reason, event):
    """Предупреждение незнакомцу: карточка с кнопками причины (жмёт только он) или просто текст."""
    if getattr(engine, '_aiogram_bot', None) is not None and await engine.get_config('buttons'):
        body = _esc(text) + ("\n\n" + visual.hint(f"Причина: {REASONS.get(reason, reason)} — владелец уже знает") if reason
                             else "\n\n" + visual.hint("Зачем вы пишете? Нажмите — владелец сразу увидит:"))
        buttons = None if reason else [[("💼 По делу", "pm:work"), ("💬 Пообщаться", "pm:chat")],
                                       [("🆘 Срочно", "pm:urgent", "danger"), ("📢 Реклама", "pm:ad")]]
        try:
            form = await engine.create_inlineform(uid, body, buttons=buttons, for_user=[uid], reply_to=event.id)
        except Exception:
            form = None
        if form is not None:
            @form.on_click(data_startswith='pm:')
            async def _(click):
                who = getattr(click.from_user, 'id', None)
                if who != uid:
                    await click.answer("Эти кнопки — для собеседника", show_alert=True)
                    return
                r = click.data[3:]
                reasons = await db.get('reasons', {}) or {}
                reasons[str(uid)] = r
                await db.set('reasons', reasons)
                if r == 'ad':
                    await form.edit("🛡 Спасибо за честность — реклама здесь не нужна. Вы заблокированы.", buttons=None)
                    await form.close()
                    _warned.pop(uid, None)
                    try:
                        await _block(uid)
                    except Exception:
                        pass
                    await _card_reason(uid, r, final=visual.ok("заблокирован: реклама"))
                    return
                await form.edit(_esc(text) + "\n\n" + visual.ok(f"Причина: {REASONS[r]}") + "\n"
                                + visual.hint("владелец получил уведомление — дождитесь ответа"), buttons=None)
                await _card_reason(uid, r)
            return form
    sent = await event.reply(text)
    return sent.id


async def _card_reason(uid, reason, final=None):
    """Причина от незнакомца → в карточку владельца (и пуш, если срочно)."""
    card = _cards.get(uid)
    line = visual.field("причина", REASONS.get(reason, reason), code=False)
    if card is not None:
        form, base = card
        base = base + "\n" + line
        _cards[uid] = (form, base)
        try:
            if final:
                await form.edit(base + "\n\n" + final, buttons=None)
                await form.close()
                _cards.pop(uid, None)
            else:
                await form.edit(base, buttons=_card_buttons(uid, False))
        except Exception:
            pass
    if reason == 'urgent':
        try:
            await engine.bot_send_to_owner("🆘 " + visual.title("PM Guard", sub="срочно") + "\n"
                                           + f'<a href="tg://user?id={uid}">незнакомец</a> пишет, что дело срочное')
        except Exception:
            pass


async def _card_done(uid, final):
    card = _cards.pop(uid, None)
    if card is None:
        return
    form, base = card
    try:
        await form.edit(base + "\n\n" + final, buttons=None)
        await form.close()
    except Exception:
        pass


def _card_buttons(uid, blocked):
    if blocked:
        return [[("🔓 Разблокировать и одобрить", f"pg:u:{uid}", "success")],
                [("🗑 Удалить чат", f"pg:w:{uid}", "danger"), ("Скрыть", f"pg:x:{uid}")]]
    return [[("✓ Одобрить", f"pg:a:{uid}", "success"), ("⛔ Заблокировать", f"pg:b:{uid}", "danger")],
            [("🗑 Блок + удалить чат", f"pg:w:{uid}", "danger"), ("Скрыть", f"pg:x:{uid}")]]


async def _report(uid, name, sender, event, blocked):
    if not await engine.get_config('report'):
        return
    uname = f" @{sender.username}" if getattr(sender, 'username', None) else ''
    reason = (await db.get('reasons', {}) or {}).get(str(uid))
    text = "\n".join([visual.title("PM Guard", sub="заблокирован" if blocked else "новый незнакомец"),
                      visual.field("кто", f'<a href="tg://user?id={uid}">{_esc(name)}</a>{_esc(uname)}', code=False),
                      visual.field("id", uid),
                      visual.field("пишет", _esc((event.raw_text or '[медиа]')[:300]), code=False)]
                     + ([visual.field("причина", REASONS.get(reason, reason), code=False)] if reason else []))
    old = _cards.pop(uid, None)
    if old is not None:
        try:
            await old[0].delete()
        except Exception:
            pass
    if getattr(engine, '_aiogram_bot', None) is None:
        await engine.client.send_message('me', text, parse_mode='html')
        return
    try:
        form = await engine.create_inlineform(getattr(engine, '_owner_id'), text, reply_to=False,
                                              buttons=_card_buttons(uid, blocked))
    except Exception:
        try:
            await engine.bot_send_to_owner(text)
        except Exception:
            pass
        return
    _cards[uid] = (form, text)

    @form.on_click(data_startswith='pg:')
    async def _(click):
        from telethon.tl.functions.contacts import UnblockRequest
        act, u = click.data[3], int(click.data[5:])
        base = _cards.get(u, (form, text))[1]
        if act == 'x':
            _cards.pop(u, None)
            await form.delete()
            return
        if act == 'a':
            await _approve(u, True)
            res = visual.ok("одобрен")
        elif act == 'b':
            await _block(u)
            res = visual.ok("заблокирован")
        elif act == 'w':
            await _block(u, wipe=True)
            res = visual.ok("заблокирован, переписка удалена")
        else:
            await engine.client(UnblockRequest(u))
            await _approve(u, True)
            res = visual.ok("разблокирован и одобрен")
        _cards.pop(u, None)
        await form.edit(base + "\n\n" + res, buttons=None)
        await form.close()
