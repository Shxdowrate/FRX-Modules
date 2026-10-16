"""
Relay — пересылка новых постов из каналов и чатов в ваши чаты, с фильтрами. Для каждого правила:
откуда, куда, что пропускать и в каком виде.

  .relay              панель: список правил, добавить, настроить
  .relay add         мастер нового правила (источник → цель → фильтры)
  .relay <№>         настройки правила (фильтры, подпись, вкл/выкл, удалить)

Фильтры правила:
  • слова-пропуск — пересылать только посты с одним из этих слов (пусто — пропускать всё);
  • слова-стоп — не пересылать посты с этими словами (готовый список антирекламы «#реклама, partner…» в комплекте);
  • только с медиа — пропускать только фото/видео/файлы;
  • без рекламных ссылок — отбрасывать посты, где есть t.me/+…, bit.ly и т. п. (настраивается);
  • как пересылать — «переслано от…» или копией без подписи источника;
  • своя подпись — текст (или ссылка на источник) снизу поста.

Источник — канал/чат, где вы состоите (ссылка, @username или id). Цель — любой ваш чат, куда вы можете писать
(в т. ч. «Избранное»). Reply/комментарии, служебные сообщения и свои же посты в цель не пересылаются.
"""

import asyncio
import html
import re
import time

import db
import decorators
import engine
import visual

NAME = "Relay"
MODULE_NAME = "relay"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Пересылка постов из каналов с фильтрами: слова, только медиа, без рекламы, копией без «переслано от», своя подпись"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/relay.py"

DEFAULT_STOP = ['#реклама', '#ad', 'реклама', 'partner', 'спонсор', 'промокод']
AD_LINK_RE = re.compile(r'(t\.me/\+|t\.me/joinchat|bit\.ly/|goo\.gl/|cutt\.ly/|clck\.ru/|vk\.cc/|tinyurl\.com/)', re.I)
_src_index = {}        # chat_id источника -> [rule_id, ...] (кэш, чтобы watcher не читал всю базу)
_album_buf = {}        # grouped_id -> {'msgs': [], 'task': ...}


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config('album_wait', 'Сколько ждать остальные части альбома, секунд', 'float', 1.5,
                               valid={'min_value': 0.3, 'max_value': 5})
    await engine.create_config('ad_link_pattern', 'Регулярка «рекламных» ссылок (для фильтра «без рекламы»)', 'str',
                               AD_LINK_RE.pattern, valid={'min_len': 1, 'max_len': 500})
    await _reindex()


async def _rules():
    return await db.get('rules', {}) or {}


async def _save_rules(rules):
    await db.set('rules', rules)
    await _reindex()


async def _reindex():
    rules = await _rules()
    _src_index.clear()
    for rid, r in rules.items():
        if r.get('enabled', True):
            _src_index.setdefault(r['source'], []).append(rid)


def _new_rule(source, target, source_title='', target_title=''):
    return {'source': source, 'target': target, 'source_title': source_title, 'target_title': target_title,
            'enabled': True, 'pass_words': [], 'stop_words': list(DEFAULT_STOP), 'only_media': False,
            'no_ads': True, 'mode': 'copy', 'signature': '', 'count': 0, 'created': int(time.time())}


# --------------------------------------------------------------------------- фильтры

def _passes(rule, text, has_media, ad_re):
    low = text.lower()
    pw = rule.get('pass_words') or []
    if pw and not any(w.lower() in low for w in pw):
        return False
    if any(w.lower() in low for w in (rule.get('stop_words') or [])):
        return False
    if rule.get('only_media') and not has_media:
        return False
    if rule.get('no_ads') and text and ad_re and ad_re.search(text):
        return False
    return True


# --------------------------------------------------------------------------- пересылка

async def _ad_re():
    try:
        return re.compile(await engine.get_config('ad_link_pattern'), re.I)
    except Exception:
        return AD_LINK_RE


async def _deliver(rule, messages):
    """messages — один пост или части альбома (по возрастанию id)."""
    target = rule['target']
    first = messages[0]
    text = first.message or ''
    sig = rule.get('signature') or ''
    try:
        if rule.get('mode') == 'forward':
            await engine.client.forward_messages(target, messages, from_peer=first.chat_id)
            if sig:
                await engine.client.send_message(target, sig, parse_mode='html', link_preview=False)
        else:
            # копия без «переслано от» — drop_author
            sent = await engine.client.forward_messages(target, messages, from_peer=first.chat_id, drop_author=True)
            if sig:
                try:
                    tail = (text + "\n\n" + sig) if text else sig
                    m0 = sent[0] if isinstance(sent, (list, tuple)) else sent
                    await engine.client.edit_message(m0, tail, parse_mode='html', link_preview=False)
                except Exception:
                    await engine.client.send_message(target, sig, parse_mode='html', link_preview=False)
    except Exception as e:
        engine.tlog(f"[Relay] пересылка в {target}: {e}")
        try:
            await engine.bot_send_to_owner(visual.err("Relay: не переслать пост") + f"\n<code>{_esc(str(e)[:200])}</code>")
        except Exception:
            pass
        return
    rules = await _rules()
    for rid, r in rules.items():
        if r['source'] == rule['source'] and r['target'] == rule['target']:
            r['count'] = r.get('count', 0) + 1
    await db.set('rules', rules)


async def _handle(rule, messages, ad_re):
    text = ' '.join(m.message or '' for m in messages)
    has_media = any(m.media is not None for m in messages)
    if _passes(rule, text, has_media, ad_re):
        await _deliver(rule, messages)


@decorators.watcher()
async def relay_watcher(event):
    rids = _src_index.get(event.chat_id)
    if not rids:
        return
    msg = event.message
    if msg is None or event.action_message is not None:
        return
    if getattr(msg, 'reply_to', None) and not getattr(msg, 'grouped_id', None):
        return                              # комментарии/ответы (но альбом сам по себе не считается ответом)
    rules = await _rules()
    active = [rules[r] for r in rids if r in rules and rules[r].get('enabled', True)]
    if not active:
        return
    ad_re = await _ad_re()

    gid = getattr(msg, 'grouped_id', None)
    if gid:
        buf = _album_buf.get(gid)
        if buf is None:
            buf = _album_buf[gid] = {'msgs': [], 'task': None}
        buf['msgs'].append(msg)
        if buf['task'] is not None:
            buf['task'].cancel()

        async def flush():
            try:
                await asyncio.sleep(await engine.get_config('album_wait'))
            except asyncio.CancelledError:
                return
            group = sorted(_album_buf.pop(gid, {'msgs': []})['msgs'], key=lambda m: m.id)
            for rule in active:
                await _handle(rule, group, ad_re)
        buf['task'] = asyncio.ensure_future(flush())
        return

    for rule in active:
        await _handle(rule, [msg], ad_re)


# --------------------------------------------------------------------------- вспомогательное

async def _resolve_chat(query):
    q = query.strip()
    if q in ('me', 'избранное', 'saved'):
        me = await engine.client.get_me()
        return me.id, 'Избранное'
    ent = await engine.client.get_entity(int(q) if q.lstrip('-').isdigit() else q)
    title = getattr(ent, 'title', None) or " ".join(x for x in (getattr(ent, 'first_name', ''), getattr(ent, 'last_name', '')) if x) or str(getattr(ent, 'id', q))
    return engine.client.get_peer_id(ent) if hasattr(engine.client, 'get_peer_id') else ent.id, title


# --------------------------------------------------------------------------- команда

@decorators.command('relay', 'репост')
async def relay_cmd(event):
    """пересылка постов: add — новое правило, <№> — настройки, без аргументов — список"""
    a = engine.get_args().strip().lower()
    if a in ('add', 'new', 'добавить', '+'):
        await _wizard(event)
        return
    if a.isdigit():
        await _rule_panel(event, a)
        return
    await _list_panel(event)


async def _list_text():
    rules = await _rules()
    lines = [visual.title("Relay", sub="пересылка постов")]
    if not rules:
        lines.append(visual.hint("правил нет — .relay add"))
        return "\n".join(lines), rules
    for i, (rid, r) in enumerate(sorted(rules.items(), key=lambda x: x[1].get('created', 0)), 1):
        dot = "🟢" if r.get('enabled', True) else "⚪"
        lines.append(f"{dot} <b>{i}.</b> {_esc(r.get('source_title') or r['source'])} → {_esc(r.get('target_title') or r['target'])}"
                     f"  <i>{r.get('count', 0)} шт.</i>")
    lines.append("")
    lines.append(visual.hint("настроить: .relay <№>"))
    return "\n".join(lines), rules


async def _list_panel(event):
    text, rules = await _list_text()
    ordered = [rid for rid, _ in sorted(rules.items(), key=lambda x: x[1].get('created', 0))]
    rows = [[(f"{i}. {rules[rid].get('source_title') or rules[rid]['source']}"[:30], f"rl:open:{rid}")]
            for i, rid in enumerate(ordered, 1)]
    rows.append([("➕ Новое правило", "rl:add", "success")])
    rows.append([(visual.btn('Закрыть', 'close'), "rl:close")])
    try:
        form = await engine.create_inlineform(event.chat_id, text, buttons=rows)
    except Exception:
        await engine.answer(event, text + "\n" + visual.hint("кнопки — со стационарным ботом"))
        return
    try:
        await event.delete()
    except Exception:
        pass

    @form.on_click(data_startswith='rl:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
        elif d == 'add':
            await form.delete()
            await _wizard_in_form(click.chat_id)
        elif d.startswith('open:'):
            await form.delete()
            await _open_rule_form(click.chat_id, d[5:])


async def _wizard(event):
    await engine.answer(event, visual.wait("Новое правило Relay"))
    await _wizard_in_form(event.chat_id)


async def _wizard_in_form(chat_id):
    import waiters
    form = await engine.create_inlineform(chat_id, visual.title("Новое правило", sub="шаг 1 из 2")
                                          + "\n" + visual.field("источник", "пришлите ссылку / @username / id канала", code=False),
                                          buttons=[[engine.InputButton("📥 Источник", "src", hint="ссылка, @username или id канала")],
                                                   [(visual.btn('Отмена', 'close'), "wz:cancel")]])
    state = {}

    @form.on_click(data='wz:cancel')
    async def _(click):
        await form.delete()

    @form.on_input(data='src')
    async def _(inp):
        try:
            cid, title = await _resolve_chat(inp.value)
        except Exception as e:
            await form.edit(visual.err("Не нашёл канал") + f" <code>{_esc(str(e)[:120])}</code>",
                            buttons=[[engine.InputButton("📥 Источник", "src", hint="ссылка, @username или id")],
                                     [(visual.btn('Отмена', 'close'), "wz:cancel")]])
            return
        state['source'] = cid
        state['source_title'] = title
        await form.edit(visual.title("Новое правило", sub="шаг 2 из 2")
                        + "\n" + visual.field("источник", _esc(title), code=False)
                        + "\n" + visual.field("цель", "куда пересылать — ссылка / @ / id, или «Избранное»", code=False),
                        buttons=[[engine.InputButton("📤 Цель", "tgt", hint="чат-получатель: @username, id или «me»")],
                                 [("📁 В Избранное", "wz:saved")],
                                 [(visual.btn('Отмена', 'close'), "wz:cancel")]])

    @form.on_click(data='wz:saved')
    async def _(click):
        me = await engine.client.get_me()
        await _finish(me.id, 'Избранное')

    @form.on_input(data='tgt')
    async def _(inp):
        try:
            cid, title = await _resolve_chat(inp.value)
        except Exception as e:
            await form.edit(visual.err("Не нашёл чат") + f" <code>{_esc(str(e)[:120])}</code>",
                            buttons=[[engine.InputButton("📤 Цель", "tgt", hint="чат-получатель")],
                                     [("📁 В Избранное", "wz:saved")], [(visual.btn('Отмена', 'close'), "wz:cancel")]])
            return
        await _finish(cid, title)

    async def _finish(target, target_title):
        import secrets
        rules = await _rules()
        rid = secrets.token_hex(4)
        rules[rid] = _new_rule(state['source'], target, state.get('source_title', ''), target_title)
        await _save_rules(rules)
        await form.edit(visual.ok("Правило создано") + "\n"
                        + visual.field("", f"{_esc(state.get('source_title'))} → {_esc(target_title)}", code=False)
                        + "\n" + visual.hint(".relay — список и настройки · по умолчанию: копией, стоп-слова рекламы, без рекламных ссылок"),
                        buttons=[[("⚙ Настроить", f"wzf:open:{rid}")], [(visual.btn('Готово', 'close'), "wzf:done")]])

    @form.on_click(data_startswith='wzf:')
    async def _(click):
        d = click.data[4:]
        if d == 'done':
            await form.delete()
        elif d.startswith('open:'):
            await form.delete()
            await _open_rule_form(click.chat_id, d[5:])


async def _rule_panel(event, num):
    rules = await _rules()
    ordered = [rid for rid, _ in sorted(rules.items(), key=lambda x: x[1].get('created', 0))]
    idx = int(num) - 1
    if not 0 <= idx < len(ordered):
        await engine.answer(event, visual.err("Нет правила с таким номером"))
        return
    await engine.answer(event, visual.wait("Открываю правило…"))
    await _open_rule_form(event.chat_id, ordered[idx])


def _rule_text(r):
    return "\n".join([
        visual.title("Правило Relay", sub=("🟢 включено" if r.get('enabled', True) else "⚪ выключено")),
        visual.field("источник", _esc(r.get('source_title') or r['source']), code=False),
        visual.field("цель", _esc(r.get('target_title') or r['target']), code=False),
        visual.field("слова-пропуск", ", ".join(r.get('pass_words') or []) or "любые посты", code=False),
        visual.field("стоп-слова", ", ".join(r.get('stop_words') or []) or "нет", code=False),
        visual.field("только с медиа", "да" if r.get('only_media') else "нет", code=False),
        visual.field("без рекламных ссылок", "да" if r.get('no_ads') else "нет", code=False),
        visual.field("как пересылать", "копией (без «переслано от»)" if r.get('mode') == 'copy' else "с «переслано от»", code=False),
        visual.field("подпись", _esc(r['signature']) if r.get('signature') else "нет", code=False),
        visual.field("переслано", f"{r.get('count', 0)} шт.", code=False),
    ])


def _rule_buttons(r):
    return [
        [("🟢 Вкл" if r.get('enabled', True) else "⚪ Выкл", "rr:toggle")],
        [engine.InputButton("✅ Слова-пропуск", "passw", hint="слова через запятую (пусто — любые посты)"),
         engine.InputButton("⛔ Стоп-слова", "stopw", hint="слова через запятую")],
        [(("◉ " if r.get('only_media') else "○ ") + "Только медиа", "rr:media"),
         (("◉ " if r.get('no_ads') else "○ ") + "Без рекламы", "rr:ads")],
        [(f"Режим: {'копия' if r.get('mode') == 'copy' else 'переслать'}", "rr:mode"),
         engine.InputButton("✎ Подпись", "sig", hint="текст подписи под постом (пусто — убрать)")],
        [("🗑 Удалить", "rr:del", "danger"), (visual.btn('Закрыть', 'close'), "rr:close")],
    ]


async def _open_rule_form(chat_id, rid):
    rules = await _rules()
    if rid not in rules:
        await engine.send_message(chat_id, visual.err("Правило не найдено"))
        return
    form = await engine.create_inlineform(chat_id, _rule_text(rules[rid]), buttons=_rule_buttons(rules[rid]))

    async def refresh():
        rr = await _rules()
        if rid not in rr:
            await form.edit(visual.hint("Правило удалено"), buttons=None)
            return
        await form.edit(_rule_text(rr[rid]), buttons=_rule_buttons(rr[rid]))

    @form.on_click(data_startswith='rr:')
    async def _(click):
        d = click.data[3:]
        rr = await _rules()
        if d == 'close':
            await form.delete()
            return
        if rid not in rr:
            await form.edit(visual.hint("Правило удалено"), buttons=None)
            return
        r = rr[rid]
        if d == 'del':
            del rr[rid]
            await _save_rules(rr)
            await form.edit(visual.ok("Правило удалено"), buttons=None)
            return
        if d == 'toggle':
            r['enabled'] = not r.get('enabled', True)
        elif d == 'media':
            r['only_media'] = not r.get('only_media')
        elif d == 'ads':
            r['no_ads'] = not r.get('no_ads')
        elif d == 'mode':
            r['mode'] = 'forward' if r.get('mode') == 'copy' else 'copy'
        await _save_rules(rr)
        await click.answer()
        await refresh()

    async def _set_words(key, value):
        rr = await _rules()
        if rid in rr:
            rr[rid][key] = [w.strip() for w in re.split(r'[,\n]', value) if w.strip()]
            await _save_rules(rr)
        await refresh()

    @form.on_input(data='passw')
    async def _(inp):
        await _set_words('pass_words', inp.value or '')

    @form.on_input(data='stopw')
    async def _(inp):
        await _set_words('stop_words', inp.value or '')

    @form.on_input(data='sig')
    async def _(inp):
        rr = await _rules()
        if rid in rr:
            rr[rid]['signature'] = (inp.value or '').strip()
            await _save_rules(rr)
        await refresh()
