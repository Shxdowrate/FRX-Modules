"""
Buttons — посты с кнопками-ссылками. Через инлайн-режим (в любом чате) или от имени стационарного бота — тогда
сообщение чистое, без «via @бот»: так удобно постить в свой канал.

  .btn <текст + кнопки>              пост с кнопками в этот чат (через инлайн-режим, «via @бот»)
  .bpost [куда] <текст + кнопки>     пост ОТ БОТА: куда — @канал, id или ничего (этот чат). Бот должен быть в чате
                                     (в канале — админом с правом публикации)
  ответом на сообщение               .btn / .bpost берут текст, форматирование и фото/видео/файл из него, а кнопки — из команды
  .bedit <ссылка на пост> <кнопки>   заменить кнопки у поста в канале (бот — админ с правом редактирования); кнопок нет — убрать
  .bpreview <текст + кнопки>         посмотреть, как разберутся кнопки

Кнопки — после текста, каждая строка = ряд, кнопки в ряду через « | »:
  Сайт - https://example.com | Канал - @mychannel
  [Написать мне](https://t.me/username)
  Промокод - copy:FRX2026          ← кнопка «скопировать текст» (только .bpost; в .btn — всплывашка с текстом)
  Подсказка - popup:Текст всплывашки   ← только .btn

Форматирование текста (жирный, курсив, ссылки, спойлеры, цитаты) сохраняется как в вашем сообщении.
"""

import html
import re
from pathlib import Path

import decorators
import engine
import files
import visual

NAME = "Buttons"
MODULE_NAME = "buttons"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Посты с кнопками-ссылками: через инлайн или от стационарного бота без «via» (в каналы); правка кнопок постов"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/buttons.py"

BTN_RE = re.compile(r'^\s*(?:\[(?P<t1>[^\]]+)\]\((?P<u1>[^)]+)\)|(?P<t2>.+?)\s+[-—–]\s+(?P<u2>\S+))\s*$')


def _esc(x):
    return html.escape(str(x), quote=False)


def norm_url(u):
    u = u.strip()
    if u.startswith('@'):
        return 'https://t.me/' + u[1:]
    if re.match(r'^(t\.me|telegram\.me)/', u, re.I):
        return 'https://' + u
    if re.match(r'^(https?|tg)://', u, re.I):
        return u
    if re.match(r'^[\w-]+(\.[\w-]+)+(/\S*)?$', u):
        return 'https://' + u
    return None


def parse_button(chunk):
    m = BTN_RE.match(chunk)
    if not m:
        return None
    text = (m.group('t1') or m.group('t2')).strip()
    target = (m.group('u1') or m.group('u2')).strip()
    low = target.lower()
    if low.startswith('copy:'):
        return {'text': text, 'kind': 'copy', 'value': target[5:][:256]}
    if low.startswith('popup:'):
        return {'text': text, 'kind': 'popup', 'value': target[6:][:200]}
    url = norm_url(target)
    if url is None:
        return None
    return {'text': text, 'kind': 'url', 'value': url}


def parse_row(line):
    parts = [p for p in re.split(r'\s+\|\s+|\s*\|\s*(?=\[)', line) if p.strip()]
    row = [parse_button(p) for p in parts]
    return row if row and all(row) else None


def split_text_buttons(raw):
    """Строки с кнопками — в конце. → (сколько символов текста, [[кнопка, …], …])."""
    lines = raw.split('\n')
    rows = []
    cut = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            if rows:
                cut = i
                continue
            cut = i
            continue
        row = parse_row(lines[i])
        if row is None:
            break
        rows.insert(0, row)
        cut = i
    text = '\n'.join(lines[:cut]).rstrip()
    return len(text), rows


def html_of(message, start=0, end=None):
    """HTML куска сообщения (по символам Python) с сохранением форматирования."""
    from telethon.extensions import html as thtml
    from telethon.helpers import add_surrogate, del_surrogate
    raw = message.message or ''
    end = len(raw) if end is None else end
    s16 = len(add_surrogate(raw[:start]))
    e16 = len(add_surrogate(raw[:end]))
    piece = del_surrogate(add_surrogate(raw)[s16:e16])
    ents = []
    for e in (message.entities or []):
        a, b = e.offset, e.offset + e.length
        if b <= s16 or a >= e16:
            continue
        if type(e).__name__ in ('MessageEntityUrl', 'MessageEntityMention', 'MessageEntityHashtag', 'MessageEntityBotCommand'):
            continue
        import copy
        e2 = copy.copy(e)
        e2.offset = max(a, s16) - s16
        e2.length = min(b, e16) - max(a, s16)
        ents.append(e2)
    return thtml.unparse(piece, ents).strip()


async def gather(event, args_offset_text):
    """(html текста, ряды кнопок, сообщение-источник медиа | None)."""
    msg = event.message
    raw = msg.message or ''
    # где начинаются аргументы команды в исходном тексте
    m = re.match(r'^\S+\s?', raw)
    start = m.end() if m else 0
    if args_offset_text is not None:
        start = raw.find(args_offset_text, start) if args_offset_text else len(raw)
        start = max(start, 0)
    body = raw[start:]
    tlen, rows = split_text_buttons(body)
    reply = await event.get_reply_message() if event.is_reply else None
    if reply is not None:
        text_html = html_of(reply) if reply.message else ''
        if tlen and body[:tlen].strip():                 # в команде есть и свой текст — он важнее
            text_html = html_of(msg, start, start + tlen)
        return text_html, rows, reply if reply.media else None
    return html_of(msg, start, start + tlen), rows, None


def rows_for_form(rows, popups):
    out = []
    for r in rows:
        row = []
        for b in r:
            if b['kind'] == 'url':
                row.append((b['text'], f"url:{b['value']}"))
            else:
                popups.append(b['value'])
                row.append((b['text'], f"bp:{len(popups) - 1}"))
        out.append(row)
    return out


def markup_for_bot(rows):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = []
    for r in rows:
        row = []
        for b in r:
            if b['kind'] == 'url':
                row.append(InlineKeyboardButton(text=b['text'], url=b['value']))
            elif b['kind'] == 'copy':
                try:
                    from aiogram.types import CopyTextButton
                    row.append(InlineKeyboardButton(text=b['text'], copy_text=CopyTextButton(text=b['value'])))
                except Exception:
                    raise ValueError("кнопки copy: нужен aiogram поновее (3.14+)")
            else:
                raise ValueError("popup-кнопки работают только в .btn (у постов бота их некому обрабатывать)")
        kb.append(row)
    return InlineKeyboardMarkup(inline_keyboard=kb) if kb else None


SYNTAX = ("Кнопки — после текста, строка = ряд, в ряду через « | »:\n"
          "<code>Сайт - https://example.com | Канал - @channel</code>\n"
          "<code>[Написать](https://t.me/username)</code>\n"
          "<code>Промокод - copy:FRX2026</code>")


async def media_file(msg):
    """Скачать медиа сообщения → (путь, вид)."""
    kind = ('photo' if msg.photo else 'animation' if msg.gif else 'video' if msg.video and not msg.video_note
            else 'audio' if msg.audio else 'voice' if msg.voice else 'sticker' if msg.sticker else 'document')
    data = await engine.client.download_media(msg, file=bytes)
    ext = (msg.file.ext if msg.file else '') or {'photo': '.jpg', 'animation': '.mp4', 'video': '.mp4'}.get(kind, '.bin')
    name = (msg.file.name if msg.file and msg.file.name else f"post{ext}")
    obj = await files.create_file(getattr(msg.file, 'mime_type', None) or 'application/octet-stream', name, data)
    return obj['path'], kind


# --------------------------------------------------------------------------- команды

@decorators.command('btn', 'кнопки')
async def btn_cmd(event):
    """пост с кнопками в этот чат через инлайн-режим: <текст + строки кнопок> (или ответом на сообщение)"""
    text_html, rows, src = await gather(event, None)
    if not rows:
        await engine.answer(event, visual.err("Не нашёл кнопок") + "\n" + SYNTAX)
        return
    if not text_html and src is None:
        text_html = "⁠"
    popups = []
    media = None
    if src is not None and (src.photo or src.gif):
        media, _ = await media_file(src)
    try:
        extra = {'media': media} if media else {}
        form = await engine.create_inlineform(event.chat_id, text_html or "⁠", buttons=rows_for_form(rows, popups),
                                              reply_to=False, **extra)
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    await event.delete()
    if popups:
        @form.on_click(data_startswith='bp:')
        async def _(click):
            await click.answer(popups[int(click.data[3:])], show_alert=True)
    else:
        await form.close(remove_buttons=False)


async def resolve_dest(token, here):
    if not token:
        return here
    if token.lower() in ('here', 'тут', 'сюда'):
        return here
    if re.fullmatch(r'-?\d+', token):
        return int(token)
    if token.startswith('@') or re.fullmatch(r'https?://t\.me/\w+', token):
        name = token.rsplit('/', 1)[-1].lstrip('@')
        try:
            ent = await engine.client.get_entity(name)
            from telethon.utils import get_peer_id
            return get_peer_id(ent)
        except Exception:
            return '@' + name
    return None


@decorators.command('bpost', 'botpost')
async def bpost_cmd(event):
    """пост ОТ стационарного бота (без «via»): [@канал | id] <текст + кнопки>; ответом — с медиа из сообщения"""
    if getattr(engine, '_aiogram_bot', None) is None:
        await engine.answer(event, visual.err("Стационарный бот не запущен"))
        return
    args = engine.get_args().strip()
    first = args.split(maxsplit=1)[0] if args else ''
    dest = await resolve_dest(first, event.chat_id) if first and (first.startswith('@') or re.fullmatch(r'-?\d{5,}', first)
                                                                  or first.lower() in ('here', 'тут', 'сюда')
                                                                  or first.startswith('https://t.me/')) else event.chat_id
    rest = args[len(first):].lstrip() if dest != event.chat_id or first.lower() in ('here', 'тут', 'сюда') else args
    text_html, rows, src = await gather(event, rest if rest != args else None)
    try:
        markup = markup_for_bot(rows)
    except ValueError as e:
        await engine.answer(event, visual.err(str(e)))
        return
    if not text_html and src is None:
        await engine.answer(event, visual.err("Нужен текст поста (или ответом на сообщение)") + "\n" + SYNTAX)
        return
    raw = engine.bot.raw
    try:
        if src is not None:
            from aiogram.types import FSInputFile
            path, kind = await media_file(src)
            f = FSInputFile(path)
            cap = dict(caption=text_html or None, parse_mode='HTML', reply_markup=markup)
            if kind == 'sticker':
                sent = await raw.send_sticker(dest, f, reply_markup=markup)
            elif kind == 'voice':
                sent = await raw.send_voice(dest, f, **cap)
            else:
                method = {'photo': raw.send_photo, 'animation': raw.send_animation, 'video': raw.send_video,
                          'audio': raw.send_audio}.get(kind, raw.send_document)
                sent = await method(dest, f, request_timeout=600, **cap)
                Path(path).unlink(missing_ok=True)
        else:
            from aiogram.types import LinkPreviewOptions
            sent = await raw.send_message(dest, text_html, parse_mode='HTML', reply_markup=markup,
                                          link_preview_options=LinkPreviewOptions(is_disabled=False))
    except Exception as e:
        hint = "добавьте бота в канал админом с правом публиковать" if 'chat not found' in str(e).lower() or 'rights' in str(e).lower() \
            or 'forbidden' in str(e).lower() else ""
        await engine.answer(event, visual.err(f"Бот не смог отправить: {_esc(e)}") + (("\n" + visual.hint(hint)) if hint else ""))
        return
    if dest == event.chat_id:
        await event.delete()
        return
    uname = getattr(sent.chat, 'username', None)
    link = f"https://t.me/{uname}/{sent.message_id}" if uname else (
        f"https://t.me/c/{str(sent.chat.id)[4:]}/{sent.message_id}" if str(sent.chat.id).startswith('-100') else None)
    await engine.answer(event, visual.ok("Опубликовано") + (f'\n<a href="{link}">открыть пост</a>' if link else ""))


@decorators.command('bedit')
async def bedit_cmd(event):
    """заменить кнопки у поста: <ссылка на пост> [строки кнопок]; без кнопок — убрать"""
    if getattr(engine, '_aiogram_bot', None) is None:
        await engine.answer(event, visual.err("Стационарный бот не запущен"))
        return
    raw_args = engine.get_args().strip()
    m = re.match(r'(?:https?://)?t\.me/(?:c/(\d+)|(\w+))/(\d+)\S*', raw_args)
    if not m:
        await engine.answer(event, visual.err("Первым — ссылка на пост: https://t.me/канал/123"))
        return
    chat = int(f"-100{m.group(1)}") if m.group(1) else '@' + m.group(2)
    mid = int(m.group(3))
    _tl, rows = split_text_buttons(raw_args[m.end():].lstrip('\n '))
    try:
        await engine.bot.raw.edit_message_reply_markup(chat_id=chat, message_id=mid, reply_markup=markup_for_bot(rows))
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}") + "\n"
                            + visual.hint("бот должен быть админом канала с правом редактировать сообщения"))
        return
    await engine.answer(event, visual.ok("Кнопки обновлены" if rows else "Кнопки убраны"))


@decorators.command('bpreview')
async def bpreview_cmd(event):
    """как разберутся кнопки: <текст + кнопки>"""
    text_html, rows, _src = await gather(event, None)
    lines = [visual.title("Buttons", sub="разбор"), visual.section("Текст"), text_html or visual.hint("(пусто)"),
             visual.section(f"Кнопки · рядов {len(rows)}")]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. " + "  |  ".join(f"[{_esc(b['text'])}] → {b['kind']}: <code>{_esc(b['value'][:60])}</code>" for b in r))
    if not rows:
        lines.append(visual.hint("кнопок не нашлось") + "\n" + SYNTAX)
    await engine.answer(event, "\n".join(lines))
