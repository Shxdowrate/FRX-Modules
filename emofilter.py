"""
EmoFilter — автоматически ставит выбранную реакцию на сообщения, где встречается ключевое слово.
Удобно отмечать свои триггеры: имя, тему, бренд — реакция вместо ручного «лайка».

  .emo <ключ> <эмодзи>     правило для этого чата: на сообщение с «ключ» ставить «эмодзи»
  .emo -g <ключ> <эмодзи>  глобальное правило — во всех чатах
  .emo                     список правил (этого чата и глобальных) с настройками
  .unemo <ключ>            удалить правило этого чата (или глобальное с таким ключом)
  .emop <ключ>             настройки правила кнопками: совпадение, чьи сообщения, свои тоже, «большая» реакция, пауза

Эмодзи — обычный (❤️, 🔥, 👍) или премиум-эмодзи (ответом на него, если у вас Telegram Premium). Чат должен
разрешать такую реакцию — иначе Telegram её отклонит (правило при этом сохранится).
"""

import asyncio
import html
import re
import time

import db
import decorators
import engine
import visual

NAME = "EmoFilter"
MODULE_NAME = "emofilter"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Автоматически ставит выбранную реакцию на сообщения с ключевым словом; свой ключ для каждого чата или глобально"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/emofilter.py"

MODES = [('in', 'содержит ключ'), ('exact', 'точно ключ'), ('start', 'начинается с ключа'), ('word', 'ключ отдельным словом')]
WHO = [('all', 'все'), ('others', 'все, кроме меня'), ('me', 'только я')]
GLOBAL = 'global'
_last = {}             # (chat_id, key) -> время последней реакции (антифлуд)


def _esc(x):
    return html.escape(str(x), quote=False)


def _next(options, value):
    keys = [k for k, _ in options]
    return keys[(keys.index(value) + 1) % len(keys)] if value in keys else keys[0]


def _label(options, value):
    return dict(options).get(value, value)


@decorators.on_start()
async def setup():
    await engine.create_config('default_mode', 'Совпадение по умолчанию для новых правил', 'choice', 'in',
                               options=[k for k, _ in MODES])
    await engine.create_config('cooldown', 'Пауза между реакциями по одному правилу в чате, секунд', 'int', 2,
                               valid={'min_value': 0, 'max_value': 3600})


# --------------------------------------------------------------------------- реакция

async def _set_reaction(chat_id, msg_id, rule):
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji, ReactionCustomEmoji
    if rule.get('custom_id'):
        reaction = [ReactionCustomEmoji(document_id=int(rule['custom_id']))]
    else:
        reaction = [ReactionEmoji(emoticon=rule['emoji'])]
    peer = await engine.client.get_input_entity(chat_id)
    await engine.client(SendReactionRequest(peer=peer, msg_id=msg_id, reaction=reaction, big=bool(rule.get('big'))))


# --------------------------------------------------------------------------- хранилище правил

async def _rules(scope):
    return await db.get(f'r:{scope}', {}) or {}


async def _save(scope, data):
    if data:
        await db.set(f'r:{scope}', data)
    else:
        await db.delete(f'r:{scope}')


def _matches(rule, text):
    key, low = rule['key'], text.lower()
    k = key.lower() if not rule.get('case') else key
    t = text if rule.get('case') else low
    mode = rule.get('mode', 'in')
    if mode == 'exact':
        return t.strip() == k
    if mode == 'start':
        return t.lstrip().startswith(k)
    if mode == 'word':
        return re.search(r'(?<!\w)' + re.escape(k) + r'(?!\w)', t) is not None
    return k in t


def _allowed(rule, event):
    who = rule.get('who', 'all')
    if who == 'me':
        return event.out
    if who == 'others':
        return not event.out
    return True


@decorators.watcher()
async def emo_watcher(event):
    text = event.raw_text or ''
    if not text:
        return
    prefix = await engine.get_prefix()
    if event.out and text.startswith(prefix):      # свои команды не трогаем
        return
    found = []
    for scope in (event.chat_id, GLOBAL):
        for k, rule in (await _rules(scope)).items():
            if _matches(rule, text) and _allowed(rule, event):
                found.append((scope, rule))
    if not found:
        return
    scope, rule = max(found, key=lambda x: len(x[1]['key']))     # самый длинный ключ — точнее
    cooldown = rule.get('cooldown')
    if cooldown is None:
        cooldown = await engine.get_config('cooldown')
    now = time.time()
    tag = (event.chat_id, rule['key'])
    if now - _last.get(tag, 0) < cooldown:
        return
    _last[tag] = now
    try:
        await _set_reaction(event.chat_id, event.id, rule)
    except Exception as e:
        engine.tlog(f"[EmoFilter] реакция «{rule['key']}» ({rule.get('emoji')}): {e}")


# --------------------------------------------------------------------------- разбор эмодзи

async def _emoji_from(event, text):
    """Вернёт (emoji, custom_id|None) из текста или из ответа на премиум-эмодзи."""
    custom_id, emoji = None, ''
    if event.is_reply:
        r = await event.get_reply_message()
        for ent, token in (r.get_entities_text() if r else []):
            from telethon.tl.types import MessageEntityCustomEmoji
            if isinstance(ent, MessageEntityCustomEmoji):
                custom_id = ent.document_id
                emoji = token
                break
    m = re.search(r'\s(\S+)\s*$', ' ' + text) if text else None
    if m and not custom_id:
        emoji = m.group(1)
    return emoji, custom_id


def _strip_emoji(text, emoji):
    return re.sub(r'\s*' + re.escape(emoji) + r'\s*$', '', text).strip() if emoji else text.strip()


# --------------------------------------------------------------------------- команды

@decorators.command('emo', 'реакт')
async def emo_cmd(event):
    """правило реакции: <ключ> <эмодзи> (или -g для всех чатов); без аргументов — список"""
    args = engine.get_args().strip()
    if not args:
        await _list(event)
        return
    is_global = False
    if args.split(maxsplit=1)[0] in ('-g', '--global'):
        is_global = True
        args = args.split(maxsplit=1)[1] if ' ' in args else ''
    emoji, custom_id = await _emoji_from(event, args)
    key = _strip_emoji(args, emoji if not custom_id else '')
    if custom_id:                                  # премиум-эмодзи пришёл ответом — весь текст это ключ
        key = args.strip()
    if not key or (not emoji and not custom_id):
        await engine.answer(event, visual.err("Формат: .emo <ключ> <эмодзи>") + "\n"
                            + visual.hint("или ответом на премиум-эмодзи: .emo <ключ> · -g — во всех чатах"))
        return
    scope = GLOBAL if is_global else event.chat_id
    rules = await _rules(scope)
    rules[key.lower()] = {'key': key, 'emoji': emoji or '⭐', 'custom_id': custom_id,
                          'mode': await engine.get_config('default_mode'), 'who': 'all', 'case': False,
                          'big': False, 'cooldown': None}
    await _save(scope, rules)
    where = "во всех чатах" if is_global else "в этом чате"
    await engine.answer(event, visual.ok(f"Реакция {emoji or 'премиум'} на «{_esc(key)}» {where}") + "\n"
                        + visual.hint("настроить: .emop " + key))


@decorators.command('unemo', 'снятьреакт')
async def unemo_cmd(event):
    """удалить правило реакции: <ключ>"""
    key = engine.get_args().strip().lower()
    if not key:
        await engine.answer(event, visual.err("Укажите ключ"))
        return
    for scope in (event.chat_id, GLOBAL):
        rules = await _rules(scope)
        if key in rules:
            del rules[key]
            await _save(scope, rules)
            await engine.answer(event, visual.ok(f"Правило «{_esc(key)}» удалено"
                                                 + (" (глобальное)" if scope == GLOBAL else '')))
            return
    await engine.answer(event, visual.hint("Такого правила нет"))


async def _list(event):
    local = await _rules(event.chat_id)
    glob = await _rules(GLOBAL)
    if not local and not glob:
        await engine.answer(event, visual.hint("Правил реакций нет") + "\n" + visual.hint(".emo <ключ> <эмодзи>"))
        return
    lines = [visual.title("EmoFilter", sub="правила реакций")]

    def block(title, rules):
        if not rules:
            return
        lines.append(visual.section(title))
        for rule in rules.values():
            e = rule['emoji'] if not rule.get('custom_id') else f"{rule['emoji']}(prem)"
            lines.append(f"{e} «{_esc(rule['key'])}»  <i>{_label(MODES, rule.get('mode', 'in'))} · {_label(WHO, rule.get('who', 'all'))}"
                         + (" · big" if rule.get('big') else '') + "</i>")
    block("В этом чате", local)
    block("Глобальные", glob)
    lines.append("")
    lines.append(visual.hint("настроить правило: .emop <ключ>"))
    await engine.answer(event, "\n".join(lines))


@decorators.command('emop', 'реактнастр')
async def emop_cmd(event):
    """настройки правила реакции кнопками: <ключ>"""
    key = engine.get_args().strip().lower()
    if not key:
        await engine.answer(event, visual.err("Укажите ключ: .emop слово"))
        return
    scope = event.chat_id if key in await _rules(event.chat_id) else GLOBAL
    rules = await _rules(scope)
    if key not in rules:
        await engine.answer(event, visual.hint("Такого правила нет"))
        return

    def text():
        r = rules[key]
        return "\n".join([
            visual.title("Правило", sub=f"{r['emoji']} «{_esc(r['key'])}»" + (" · глобальное" if scope == GLOBAL else '')),
            visual.field("совпадение", _label(MODES, r.get('mode', 'in')), code=False),
            visual.field("чьи сообщения", _label(WHO, r.get('who', 'all')), code=False),
            visual.field("учитывать регистр", "да" if r.get('case') else "нет", code=False),
            visual.field("большая реакция", "да" if r.get('big') else "нет", code=False),
            visual.field("пауза", f"{r['cooldown']} с" if r.get('cooldown') is not None else "по умолчанию", code=False),
        ])

    def buttons():
        r = rules[key]
        return [
            [(f"Совпадение: {_label(MODES, r.get('mode', 'in'))}", "ep:mode")],
            [(f"Чьи: {_label(WHO, r.get('who', 'all'))}", "ep:who")],
            [(("◉" if r.get('case') else "○") + " регистр", "ep:case"),
             (("◉" if r.get('big') else "○") + " big", "ep:big")],
            [engine.InputButton("⏱ Пауза", "cooldown", hint="секунды между реакциями (пусто — по умолчанию)")],
            [("🗑 Удалить правило", "ep:del", "danger"), (visual.btn('Закрыть', 'close'), "ep:close")],
        ]

    try:
        form = await engine.create_inlineform(event.chat_id, text(), buttons=buttons())
    except Exception:
        await engine.answer(event, text() + "\n" + visual.hint("кнопки — со стационарным ботом"))
        return
    try:
        await event.delete()
    except Exception:
        pass

    @form.on_click(data_startswith='ep:')
    async def _(click):
        d = click.data[3:]
        fresh = await _rules(scope)
        if key not in fresh:
            await form.edit(visual.hint("Правило уже удалено"), buttons=None)
            return
        rules[key] = fresh[key]
        r = rules[key]
        if d == 'close':
            await form.delete()
            return
        if d == 'del':
            del fresh[key]
            await _save(scope, fresh)
            await form.edit(visual.ok(f"Правило «{_esc(key)}» удалено"), buttons=None)
            return
        if d == 'mode':
            r['mode'] = _next(MODES, r.get('mode', 'in'))
        elif d == 'who':
            r['who'] = _next(WHO, r.get('who', 'all'))
        elif d == 'case':
            r['case'] = not r.get('case')
        elif d == 'big':
            r['big'] = not r.get('big')
        fresh[key] = r
        await _save(scope, fresh)
        await click.answer()
        await form.edit(text(), buttons=buttons())

    @form.on_input(data='cooldown')
    async def _(inp):
        fresh = await _rules(scope)
        if key not in fresh:
            return
        v = (inp.value or '').strip()
        fresh[key]['cooldown'] = int(v) if v.isdigit() else None
        await _save(scope, fresh)
        rules[key] = fresh[key]
        await form.edit(text(), buttons=buttons())
