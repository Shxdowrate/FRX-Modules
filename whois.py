"""
Whois — всё о пользователе, чате или канале.

  .whois [@user | id]    без аргумента — ответом на сообщение (или вы сами в личке — собеседник)
  .id                    коротко: id чата, ваш, того, на кого ответили, и id сообщения

Для людей: имя, username(ы), id, примерная дата регистрации (по id), дата-центр, Premium, бот/скам/фейк,
статус, «о себе», общие чаты, число фото профиля. Телефон (если виден) показывается целиком только в Избранном
(и в личке с самим этим человеком) — в группах и чужих личках он скрыт, чтобы случайно не слить номер. Для групп и каналов: тип, участники, ссылка, описание.
"""

import html
from datetime import datetime

import decorators
import engine
import visual

NAME = "Whois"
MODULE_NAME = "whois"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Информация о пользователе, группе или канале: .whois ответом / @user / id; .id — быстрые id"

# опорные точки «id → дата регистрации» (примерно; Telegram раздаёт id почти по порядку)
ID_DATES = [(1000000, 2013, 8), (10000000, 2014, 1), (50000000, 2014, 9), (100000000, 2015, 2), (200000000, 2016, 3),
            (300000000, 2016, 11), (400000000, 2017, 6), (500000000, 2018, 1), (700000000, 2018, 10), (900000000, 2019, 5),
            (1100000000, 2020, 2), (1400000000, 2020, 9), (1700000000, 2021, 3), (2000000000, 2021, 9), (5000000000, 2021, 12),
            (5500000000, 2022, 6), (6000000000, 2023, 1), (6500000000, 2023, 9), (7000000000, 2024, 3), (7500000000, 2024, 10),
            (8000000000, 2025, 4), (8500000000, 2025, 11)]
MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']


def _esc(x):
    return html.escape(str(x), quote=False)


def reg_date(uid):
    prev = (0, 2013, 8)
    for point in ID_DATES:
        if uid < point[0]:
            span = point[0] - prev[0]
            k = (uid - prev[0]) / span if span else 0
            m0 = prev[1] * 12 + prev[2] - 1
            m1 = point[1] * 12 + point[2] - 1
            m = round(m0 + (m1 - m0) * k)
            return f"≈ {MONTHS[m % 12]} {m // 12}"
        prev = point
    return f"после {MONTHS[prev[2] - 1]} {prev[1]}"


def _status(st):
    if st is None:
        return 'скрыт'
    name = type(st).__name__
    if name == 'UserStatusOnline':
        return 'в сети'
    if name == 'UserStatusOffline':
        return 'был(а) ' + st.was_online.astimezone().strftime('%d.%m.%Y %H:%M')
    return {'UserStatusRecently': 'был(а) недавно', 'UserStatusLastWeek': 'был(а) на этой неделе',
            'UserStatusLastMonth': 'был(а) в этом месяце'}.get(name, 'давно')


async def _target(event):
    arg = engine.get_args().strip()
    if arg:
        return int(arg) if arg.lstrip('-').isdigit() else arg
    if event.is_reply:
        r = await event.get_reply_message()
        if r:
            if r.fwd_from and r.fwd_from.from_id:
                return r.fwd_from.from_id
            return r.sender_id
    return event.chat_id if event.is_private else 'me'


@decorators.command('whois', 'u')
async def whois_cmd(event):
    """информация о пользователе / чате: [@user | id], или ответом на сообщение"""
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import GetFullChatRequest
    from telethon.tl.functions.users import GetFullUserRequest
    from telethon.tl.types import Channel, Chat, User
    await engine.answer(event, visual.wait("Смотрю…"))
    try:
        ent = await engine.client.get_entity(await _target(event))
    except Exception as e:
        await engine.answer(event, visual.err("Не нашёл") + f"\n<code>{_esc(e)}</code>")
        return
    lines = []
    if isinstance(ent, User):
        full = (await engine.client(GetFullUserRequest(ent))).full_user
        name = " ".join(filter(None, [ent.first_name, ent.last_name])) or 'Удалённый аккаунт'
        unames = [ent.username] if ent.username else []
        unames += [u.username for u in (getattr(ent, 'usernames', None) or []) if u.username not in unames]
        flags = [f for f, on in (('бот', ent.bot), ('Premium', getattr(ent, 'premium', False)), ('верифицирован', ent.verified),
                                 ('⚠ SCAM', ent.scam), ('⚠ FAKE', ent.fake), ('контакт', ent.contact), ('взаимный контакт', ent.mutual_contact),
                                 ('вы', ent.is_self)) if on]
        dc = getattr(getattr(ent, 'photo', None), 'dc_id', None)
        lines = [visual.title(_esc(name), sub=" · ".join(flags) if flags else None),
                 visual.field("id", ent.id),
                 visual.field("username", ", ".join("@" + _esc(u) for u in unames) if unames else "нет", code=False),
                 visual.field("ссылка", f'<a href="tg://user?id={ent.id}">открыть профиль</a>', code=False),
                 visual.field("регистрация", reg_date(ent.id), code=False),
                 visual.field("дата-центр", f"DC{dc}" if dc else "не видно (нет фото)", code=False)]
        if not ent.bot:
            lines.append(visual.field("статус", _status(ent.status), code=False))
        if ent.phone:
            me = await engine.client.get_me()
            in_saved = event.chat_id == me.id
            with_owner = event.is_private and event.chat_id == ent.id and not ent.is_self   # личка с ним самим
            if in_saved or with_owner:
                lines.append(visual.field("телефон", "+" + ent.phone))
            else:
                lines.append(visual.field("телефон", "+" + ent.phone[:1] + " ••• скрыт", code=False) + " "
                             + visual.hint("полностью — только в Избранном"))
        if full.about:
            lines.append(visual.field("о себе", _esc(full.about), code=False))
        lines.append(visual.field("общих чатов", full.common_chats_count))
        if getattr(full, 'birthday', None):
            b = full.birthday
            lines.append(visual.field("день рождения", f"{b.day} {MONTHS[b.month - 1]}" + (f" {b.year}" if b.year else ''), code=False))
        try:
            photos = await engine.client.get_profile_photos(ent, limit=0)
            lines.append(visual.field("фото профиля", photos.total))
        except Exception:
            pass
        if getattr(full, 'personal_channel_id', None):
            lines.append(visual.field("канал в профиле", full.personal_channel_id))
    elif isinstance(ent, Channel):
        full = (await engine.client(GetFullChannelRequest(ent))).full_chat
        kind = 'канал' if ent.broadcast else ('супергруппа' + (' (форум)' if getattr(ent, 'forum', False) else ''))
        lines = [visual.title(_esc(ent.title), sub=kind),
                 visual.field("id", f"-100{ent.id}"),
                 visual.field("username", "@" + _esc(ent.username) if ent.username else "закрытый", code=False),
                 visual.field("участников", full.participants_count or '—'),
                 visual.field("создан", ent.date.astimezone().strftime('%d.%m.%Y') if ent.date else '—', code=False)]
        if getattr(full, 'admins_count', None):
            lines.append(visual.field("админов", full.admins_count))
        if getattr(full, 'online_count', None):
            lines.append(visual.field("в сети", full.online_count))
        if full.about:
            lines.append(visual.field("описание", _esc(full.about[:500]), code=False))
        if getattr(full, 'linked_chat_id', None):
            lines.append(visual.field("связанный чат", f"-100{full.linked_chat_id}"))
        flags = [f for f, on in (('верифицирован', ent.verified), ('⚠ SCAM', ent.scam), ('⚠ FAKE', ent.fake),
                                 ('запрет пересылки', getattr(ent, 'noforwards', False))) if on]
        if flags:
            lines.append(visual.field("отметки", ", ".join(flags), code=False))
    elif isinstance(ent, Chat):
        full = (await engine.client(GetFullChatRequest(ent.id))).full_chat
        lines = [visual.title(_esc(ent.title), sub="группа"), visual.field("id", f"-{ent.id}"),
                 visual.field("участников", ent.participants_count)]
        if full.about:
            lines.append(visual.field("описание", _esc(full.about[:500]), code=False))
    await engine.answer(event, "\n".join(lines))


@decorators.command('id')
async def id_cmd(event):
    """быстрые id: чат, вы, тот, на кого ответили, сообщение"""
    lines = [visual.field("чат", event.chat_id), visual.field("вы", event.sender_id)]
    if event.is_reply:
        r = await event.get_reply_message()
        if r:
            lines.append(visual.field("отправитель", r.sender_id))
            lines.append(visual.field("сообщение", r.id))
            if r.fwd_from and r.fwd_from.from_id:
                fid = getattr(r.fwd_from.from_id, 'user_id', None) or getattr(r.fwd_from.from_id, 'channel_id', None)
                lines.append(visual.field("переслано от", fid))
            if r.file:
                lines.append(visual.field("файл", f"{r.file.mime_type} · {r.file.size // 1024} КБ"))
    await engine.answer(event, "\n".join(lines))
