"""
Bastion — полная защита и модерация групп: наказания, варны, фильтры, стоп-слова, капча, антифлуд, антирейд,
ночной режим, приветствия, права чата, репорты и журнал с кнопкой «отменить».

Наказания (ответом на сообщение, или @user / id первым аргументом; время и причина — по желанию):
  .ban [кто] [время] [причина]     бан (.dban — ещё и удалить сообщение, .sban — тихо: команда исчезает сразу)
  .unban [кто]                     разбан
  .kick [кто] [причина]            выгнать (сможет вернуться); .dkick — с удалением сообщения
  .mute [кто] [время] [причина]    мут — только чтение (.ro, .мут); .unmute — размут (.unro, .размут)
  время: 30s · 10m · 2h · 1d · 1w · 1h30m (по-русски тоже: 10м, 2ч, 1д, 1н); без времени — навсегда

Варны:
  .warn [кто] [причина]            предупреждение (.dwarn — с удалением сообщения). На лимите — наказание из настроек
  .unwarn [кто]                    снять последний варн;  .resetwarns [кто] — снять все
  .warns [кто]                     варны человека; без «кто» — у кого есть варны в этом чате

Фильтры и стоп-слова (сообщение удаляется, нарушителю — действие: del · warn · mute[:время] · kick · ban[:время]):
  .bfilter                         все фильтры и их действия;  .bfilter <фильтр> <действие|off>
                                   фильтры: links invites forwards mentions sendas inline stickers gifs photos videos
                                   rounds voices audio files polls contacts games stories premium emoji caps zalgo
                                   arabic cjk long newbie
  .bword                           стоп-слова;  .bword add слово, фраза, ещё [действие];  .bword re <регулярка> [действие]
  .bword del <№|слово>             удалить;  .bword clear — все

Защита чата:
  .captcha on|off · mode button|math|emoji · time <сек> · fail kick|ban|mute     проверка новичков
  .antiflood <сообщений> <секунд> [действие] | off                               флуд
  .antiraid <входов> <секунд> [минут] | off                                      наплыв ботов: новичков в мут, вам — тревога
  .night 23:00-07:00 | off                                                       ночью чат только для чтения
  .welcome <текст> | on | off | del <сек>     приветствие: {name} {mention} {first} {username} {id} {chat} {count}
  .goodbye <текст> | on | off                 прощание

Чат:
  .locks                           права участников кнопками;  .locks <что> on|off  (all media stickers gifs links polls
                                   photos videos voices rounds audios docs text inline games info invite pin)
  .slow <сек|off>                  медленный режим;  .pin [loud] · .unpin [all] — закрепы
  .promote [кто] [звание] · .demote [кто]    админка (.promote full — со всеми правами, кроме назначения админов)
  .staff — админы чата;  .bans — забаненные;  .trust / .untrust [кто] — не трогать фильтрами
  .gban / .ungban [кто] [причина]  бан во всех чатах под защитой Bastion

  .bastion (.bs)                   панель чата: всё выше — кнопками; .bastion on|off — включить защиту чата
  .bastion list                    все чаты под защитой

Участники пишут @admin, /report или !report (можно ответом на сообщение) — вам приходит карточка с кнопками
«удалить / варн / мут / бан». Журнал действий — в «Избранное» (настройка log_chat), у каждой записи — «↩ отменить».
Админы, вы и доверенные (.trust) фильтрами не проверяются. Чтобы Bastion работал, нужны права админа: удаление
сообщений и блокировка участников.
"""

import asyncio
import datetime as dt
import html
import random
import re
import time
from collections import deque

import db
import decorators
import engine
import visual

NAME = "Bastion"
MODULE_NAME = "bastion"
VERSION = "1.1"
AUTHOR = "FRX"
DESCRIPTION = "Модерация групп: бан/мут/кик, варны, фильтры и стоп-слова, капча, антифлуд, антирейд, ночь, приветствия, журнал"

# --------------------------------------------------------------------------- справочники

FILTERS = [
    ('links', 'ссылки'), ('invites', 'приглашения и t.me'), ('forwards', 'пересылки'), ('mentions', 'массовые упоминания'),
    ('sendas', 'от имени каналов'), ('inline', 'через ботов / с кнопками'), ('stickers', 'стикеры'), ('gifs', 'гифки'),
    ('photos', 'фото'), ('videos', 'видео'), ('rounds', 'кружки'), ('voices', 'голосовые'), ('audio', 'музыка'),
    ('files', 'файлы'), ('polls', 'опросы'), ('contacts', 'контакты и гео'), ('games', 'игры и кубики'),
    ('stories', 'истории'), ('premium', 'премиум-эмодзи'), ('emoji', 'эмодзи-спам (10+)'), ('caps', 'КАПС'),
    ('zalgo', 'залго-текст'), ('arabic', 'арабская вязь'), ('cjk', 'иероглифы'), ('long', 'простыни (2000+ символов)'),
    ('newbie', 'новички: ссылки, медиа, пересылки'),
]
FILTER_LABEL = dict(FILTERS)
ACT_CYCLE = [None, 'del', 'warn', 'mute:3600', 'mute:86400', 'mute', 'kick', 'ban:86400', 'ban']
ACT_WORDS = {'del': 'del', 'delete': 'del', 'удалить': 'del', 'удаление': 'del', 'warn': 'warn', 'варн': 'warn',
             'mute': 'mute', 'ro': 'mute', 'мут': 'mute', 'kick': 'kick', 'кик': 'kick', 'ban': 'ban', 'бан': 'ban',
             'off': None, 'выкл': None, 'нет': None}

LOCKS = [  # ключ, подпись, поля ChatBannedRights
    ('all', 'писать', ('send_messages',)), ('text', 'текст', ('send_plain',)), ('media', 'всё медиа', ('send_media',)),
    ('photos', 'фото', ('send_photos',)), ('videos', 'видео', ('send_videos',)), ('stickers', 'стикеры', ('send_stickers',)),
    ('gifs', 'гифки', ('send_gifs',)), ('voices', 'голосовые', ('send_voices',)), ('rounds', 'кружки', ('send_roundvideos',)),
    ('audios', 'музыка', ('send_audios',)), ('docs', 'файлы', ('send_docs',)), ('links', 'превью ссылок', ('embed_links',)),
    ('polls', 'опросы', ('send_polls',)), ('inline', 'инлайн-боты', ('send_inline',)), ('games', 'игры', ('send_games',)),
    ('info', 'менять инфо', ('change_info',)), ('invite', 'приглашать', ('invite_users',)), ('pin', 'закреплять', ('pin_messages',)),
]
RIGHT_FIELDS = ['view_messages', 'send_messages', 'send_media', 'send_stickers', 'send_gifs', 'send_games', 'send_inline',
                'embed_links', 'send_polls', 'change_info', 'invite_users', 'pin_messages', 'manage_topics', 'send_photos',
                'send_videos', 'send_roundvideos', 'send_audios', 'send_voices', 'send_docs', 'send_plain']
SLOW_STEPS = [0, 10, 30, 60, 300, 900, 3600]

DEF_WELCOME = "👋 {mention}, добро пожаловать в «{chat}»! Прочитайте закреп и будьте вежливы."
DEF_GOODBYE = "👋 {name} покинул(а) чат."

DEFAULTS = {
    'on': False, 'log': True, 'report': True, 'clean_service': False, 'notify': True,
    'warn_limit': 3, 'warn_action': 'mute:86400', 'warn_expire': 0,
    'flood': {'on': False, 'msgs': 6, 'secs': 5, 'action': 'mute:600'},
    'captcha': {'on': False, 'mode': 'emoji', 'timeout': 120, 'fail': 'kick'},
    'welcome': {'on': False, 'text': DEF_WELCOME, 'del': 0, 'clean': True},
    'goodbye': {'on': False, 'text': DEF_GOODBYE},
    'night': {'on': False, 'from': '23:00', 'to': '07:00'},
    'raid': {'on': False, 'joins': 10, 'secs': 60, 'minutes': 30},
    'newbie_hours': 24,
    'filters': {},
    'words': [],
    'trusted': [],
}

_CFG = {}            # chat_id -> настройки (кэш поверх db)
_ADMINS = {}         # chat_id -> (время, {id: звание})
_ME_RIGHTS = {}      # chat_id -> (время, dict прав)
_LINKED = {}         # chat_id -> (время, id привязанного канала)
_FLOOD = {}          # (chat, uid) -> deque времён
_JOINS = {}          # chat -> deque времён входов
_RAID_UNTIL = {}     # chat -> unix, до которого действует антирейд
_CAPTCHA = {}        # (chat, uid) -> {'task', 'form', 'answer', 'tries'}
_LAST_WELCOME = {}   # chat -> id последнего приветствия
_TASKS = set()


def _esc(x):
    return html.escape(str(x), quote=False)


# --------------------------------------------------------------------------- время и действия

_DUR_RE = re.compile(r'^(?:\d+(?:[.,]\d+)?\s*(?:s|sec|с|сек|m|min|м|мин|h|ч|час|d|д|дн|w|н|нед)[a-zа-я]*)+$', re.I)
_UNIT = {'s': 1, 'с': 1, 'm': 60, 'м': 60, 'h': 3600, 'ч': 3600, 'd': 86400, 'д': 86400, 'w': 604800, 'н': 604800}


def parse_dur(token):
    """'1h30m' → 5400; 'навсегда'/'forever' → 0; не время → None."""
    t = (token or '').strip().lower()
    if t in ('навсегда', 'forever', 'perm', '0'):
        return 0
    if not _DUR_RE.match(t):
        return None
    total = 0
    for num, unit in re.findall(r'(\d+(?:[.,]\d+)?)\s*([a-zа-я]+)', t):
        u = _UNIT.get(unit[0])
        if u is None:
            return None
        if unit.startswith(('мин', 'min')):
            u = 60
        total += float(num.replace(',', '.')) * u
    return int(total)


def fmt_dur(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return "навсегда"
    parts = []
    for size, name in ((604800, 'нед'), (86400, 'д'), (3600, 'ч'), (60, 'мин'), (1, 'с')):
        if sec >= size:
            parts.append(f"{sec // size} {name}")
            sec %= size
    return " ".join(parts[:2])


def parse_action(text):
    """'mute:1h' / 'мут 1ч' / 'ban' / 'off' → ('mute', 3600) | ('off', 0) | None."""
    t = (text or '').strip().lower().replace(' ', ':')
    if not t:
        return None
    name, _, dur = t.partition(':')
    if name not in ACT_WORDS:
        return None
    act = ACT_WORDS[name]
    if act is None:
        return ('off', 0)
    sec = 0
    if dur and act in ('mute', 'ban'):
        sec = parse_dur(dur)
        if sec is None:
            return None
    return (act, sec)


def act_str(act, sec=0):
    return f"{act}:{sec}" if sec and act in ('mute', 'ban') else act


def act_label(a):
    if not a:
        return "выкл"
    act, _, sec = a.partition(':')
    sec = int(sec or 0)
    base = {'del': 'удалить', 'warn': 'варн', 'mute': 'мут', 'kick': 'кик', 'ban': 'бан'}.get(act, act)
    return base + (f" {fmt_dur(sec)}" if sec else ("" if act in ('del', 'warn', 'kick') else " навсегда"))


def until(sec):
    """Telegram считает срок меньше 30 с или больше 366 дней «навсегда»."""
    if not sec:
        return None
    sec = max(31, min(int(sec), 365 * 86400))
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=sec)


# --------------------------------------------------------------------------- настройки чата

async def chat_cfg(chat_id):
    chat_id = int(chat_id)
    if chat_id not in _CFG:
        raw = await db.get(f"c{chat_id}", None) or {}
        cfg = {}
        for k, v in DEFAULTS.items():
            if isinstance(v, dict) and k != 'filters':
                cfg[k] = {**v, **(raw.get(k) or {})}
            else:
                cfg[k] = raw.get(k, v if not isinstance(v, (list, dict)) else type(v)(v))
        _CFG[chat_id] = cfg
    return _CFG[chat_id]


async def save_cfg(chat_id):
    await db.set(f"c{int(chat_id)}", _CFG[int(chat_id)])
    ids = set(await db.get('chats', []) or [])
    (ids.add if _CFG[int(chat_id)].get('on') else ids.discard)(int(chat_id))
    await db.set('chats', sorted(ids))


async def protected_chats():
    return [int(x) for x in (await db.get('chats', []) or [])]


async def gcfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


@decorators.on_start()
async def setup():
    c = engine.create_config
    await c(key='log_chat', description="Куда писать журнал: me — «Избранное», off — никуда, или id / @канал", data_type='str',
            default='me', valid={'max_len': 64})
    await c(key='notice_ttl', description='Через сколько секунд удалять уведомления о наказаниях в чате (0 — не удалять)',
            data_type='int', default=60, valid={'min_value': 0, 'max_value': 86400})
    await c(key='admin_cache', description='Как часто обновлять список админов чата, минут', data_type='int', default=10,
            valid={'min_value': 1, 'max_value': 1440})
    t = asyncio.ensure_future(_night_loop())
    _TASKS.add(t)


@decorators.on_unload()
async def cleanup():
    for t in list(_TASKS):
        t.cancel()
    for st in list(_CAPTCHA.values()):
        if st.get('task'):
            st['task'].cancel()
    _TASKS.clear()
    _CAPTCHA.clear()


@decorators.on_account_switch()
async def switched(*_a):
    for d in (_CFG, _ADMINS, _ME_RIGHTS, _LINKED, _FLOOD, _JOINS, _RAID_UNTIL):
        d.clear()


# --------------------------------------------------------------------------- люди и права

def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{_esc(name or uid)}</a>'


def name_of(ent):
    if ent is None:
        return '?'
    t = getattr(ent, 'title', None)
    if t:
        return t
    n = " ".join(x for x in (getattr(ent, 'first_name', '') or '', getattr(ent, 'last_name', '') or '') if x)
    return n or (('@' + ent.username) if getattr(ent, 'username', None) else str(ent.id))


async def entity(uid):
    try:
        return await engine.client.get_entity(uid)
    except Exception:
        return None


async def who(uid):
    ent = await entity(uid)
    return mention(uid, name_of(ent)) if ent is not None else f"<code>{uid}</code>"


async def admins(chat_id):
    """{id: звание} админов чата, с кэшем."""
    ttl = int(await gcfg('admin_cache', 10)) * 60
    hit = _ADMINS.get(chat_id)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    from telethon.tl.types import ChannelParticipantsAdmins
    res = {}
    try:
        async for u in engine.client.iter_participants(chat_id, filter=ChannelParticipantsAdmins):
            p = getattr(u, 'participant', None)
            res[u.id] = getattr(p, 'rank', None) or ('владелец' if type(p).__name__.endswith('Creator') else 'админ')
    except Exception:
        pass
    _ADMINS[chat_id] = (time.time(), res)
    return res


async def my_rights(chat_id):
    """Права аккаунта в чате: {'admin', 'creator', 'delete', 'ban', 'pin', 'promote', 'info', 'invite'}."""
    hit = _ME_RIGHTS.get(chat_id)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    r = {'admin': False}
    try:
        ent = await engine.client.get_entity(chat_id)
        ar = getattr(ent, 'admin_rights', None)
        creator = bool(getattr(ent, 'creator', False))
        r = {'admin': creator or ar is not None, 'creator': creator,
             'delete': creator or bool(ar and ar.delete_messages), 'ban': creator or bool(ar and ar.ban_users),
             'pin': creator or bool(ar and ar.pin_messages), 'promote': creator or bool(ar and ar.add_admins),
             'info': creator or bool(ar and ar.change_info), 'invite': creator or bool(ar and ar.invite_users),
             'super': type(ent).__name__ == 'Channel' or bool(getattr(ent, 'megagroup', False))}
    except Exception:
        pass
    _ME_RIGHTS[chat_id] = (time.time(), r)
    return r


async def linked_channel(chat_id):
    hit = _LINKED.get(chat_id)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    lid = None
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        full = await engine.client(GetFullChannelRequest(chat_id))
        lc = getattr(full.full_chat, 'linked_chat_id', None)
        lid = int(f"-100{lc}") if lc else None
    except Exception:
        pass
    _LINKED[chat_id] = (time.time(), lid)
    return lid


async def is_immune(chat_id, uid, cfg):
    if uid in (getattr(engine, '_owner_id', None), chat_id, 777000, 1087968824, 136817688):
        return True
    if uid in (cfg.get('trusted') or []):
        return True
    if uid == await linked_channel(chat_id):
        return True
    return uid in await admins(chat_id)


# --------------------------------------------------------------------------- цель команды

async def target(event, args=None):
    """(id, остаток аргументов, сообщение-ответ | None). id=None — не нашли.
    Кто: упоминание по имени (text mention), @username или id первым словом, иначе — автор сообщения-ответа."""
    args = (engine.get_args() if args is None else args).strip()
    reply = await event.get_reply_message() if event.is_reply else None
    from telethon.tl.types import MessageEntityMentionName
    raw = event.message.message or ''
    for e in (event.message.entities or []):
        if isinstance(e, MessageEntityMentionName):
            return e.user_id, raw[e.offset + e.length:].strip(), reply
    first, _, rest = args.partition(' ')
    if first and (first.startswith('@') or re.fullmatch(r'-?\d{5,}', first)):
        try:
            ent = await engine.client.get_entity(int(first) if first.lstrip('-').isdigit() else first)
            return _peer_id(ent), rest.strip(), reply
        except Exception:
            return None, args, reply
    if reply is not None:
        return reply.sender_id, args, reply
    return None, args, reply


def B(text, data, style=None):
    """Кнопка формы: цвет — только если задан."""
    return (text, data, style) if style else (text, data)


def _peer_id(ent):
    from telethon.utils import get_peer_id
    try:
        return get_peer_id(ent)
    except Exception:
        return getattr(ent, 'id', None)


def split_dur_reason(rest):
    """'1h спам в чате' → (3600, 'спам в чате'); 'спам' → (0, 'спам')."""
    first, _, tail = (rest or '').strip().partition(' ')
    sec = parse_dur(first) if first else None
    if sec is None:
        return 0, (rest or '').strip()
    return sec, tail.strip()


# --------------------------------------------------------------------------- исполнение наказаний

def _rights(**kw):
    from telethon.tl.types import ChatBannedRights
    return ChatBannedRights(until_date=kw.pop('until_date', None), **kw)


async def _edit_banned(chat_id, uid, rights):
    from telethon.tl.functions.channels import EditBannedRequest
    await engine.client(EditBannedRequest(await engine.client.get_input_entity(chat_id),
                                          await engine.client.get_input_entity(uid), rights))


async def do_ban(chat_id, uid, sec=0):
    r = await my_rights(chat_id)
    if not r.get('super'):
        await engine.client.kick_participant(chat_id, uid)
        return
    await _edit_banned(chat_id, uid, _rights(until_date=until(sec), view_messages=True))


async def do_unban(chat_id, uid):
    await _edit_banned(chat_id, uid, _rights())


async def do_mute(chat_id, uid, sec=0):
    r = await my_rights(chat_id)
    if not r.get('super'):
        raise RuntimeError("в обычной группе Telegram не умеет мут — сделайте её супергруппой (например, включите историю для новых)")
    await _edit_banned(chat_id, uid, _rights(until_date=until(sec), send_messages=True, send_media=True, send_stickers=True,
                                             send_gifs=True, send_games=True, send_inline=True, embed_links=True,
                                             send_polls=True, send_plain=True))


async def do_unmute(chat_id, uid):
    await _edit_banned(chat_id, uid, _rights())


async def do_kick(chat_id, uid):
    await engine.client.kick_participant(chat_id, uid)


async def apply_action(chat_id, uid, action, reason='', by='вы', msg=None, silent=False, delete_msg=True):
    """Главная точка: удалить сообщение (если есть) и применить действие. Возвращает текст результата."""
    act, _, sec = (action or 'del').partition(':')
    sec = int(sec or 0)
    if msg is not None and delete_msg:
        try:
            await msg.delete()
        except Exception:
            pass
    if act == 'del':
        return "сообщение удалено"
    if act == 'warn':
        return await add_warn(chat_id, uid, reason, by=by, silent=silent)
    if act == 'mute':
        await do_mute(chat_id, uid, sec)
        res = f"мут {fmt_dur(sec)}"
    elif act == 'ban':
        await do_ban(chat_id, uid, sec)
        res = f"бан {fmt_dur(sec)}"
    elif act == 'kick':
        await do_kick(chat_id, uid)
        res = "кик"
    else:
        return "?"
    if not silent:
        await announce(chat_id, uid, res, reason)
    await log_action(chat_id, uid, res, reason, by, undo=act if act in ('mute', 'ban') else None)
    return res


async def announce(chat_id, uid, res, reason):
    cfg = await chat_cfg(chat_id)
    if not cfg.get('notify', True):
        return
    text = f"🛡 {await who(uid)} — <b>{_esc(res)}</b>" + (f"\n▹ {_esc(reason)}" if reason else "")
    await temp_message(chat_id, text)


async def temp_message(chat_id, text, ttl=None):
    try:
        m = await engine.client.send_message(chat_id, engine.safe_html(text), parse_mode='html', link_preview=False)
    except Exception:
        return None
    ttl = int(await gcfg('notice_ttl', 60)) if ttl is None else ttl
    if ttl:
        async def later():
            await asyncio.sleep(ttl)
            try:
                await m.delete()
            except Exception:
                pass
        t = asyncio.ensure_future(later())
        _TASKS.add(t)
        t.add_done_callback(_TASKS.discard)
    return m


# --------------------------------------------------------------------------- журнал

async def chat_title(chat_id):
    ent = await entity(chat_id)
    return name_of(ent) if ent is not None else str(chat_id)


def msg_link(chat_id, msg_id, username=None):
    if username:
        return f"https://t.me/{username}/{msg_id}"
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{msg_id}" if s.startswith('-100') else None


async def log_action(chat_id, uid, res, reason, by, undo=None, extra=''):
    cfg = await chat_cfg(chat_id)
    dest = str(await gcfg('log_chat', 'me')).strip()
    if not cfg.get('log', True) or dest.lower() in ('off', 'нет', '', '0'):
        return
    text = "\n".join([visual.title("Bastion", sub=_esc(res)),
                      visual.field("чат", _esc(await chat_title(chat_id)), code=False),
                      visual.field("кто", await who(uid) + f" <code>{uid}</code>", code=False),
                      visual.field("причина", _esc(reason or '—'), code=False),
                      visual.field("кем", _esc(by), code=False),
                      visual.field("когда", dt.datetime.now().strftime('%d.%m %H:%M'))] + ([extra] if extra else []))
    to_me = dest.lower() in ('me', 'избранное')
    target_chat = 'me' if to_me else (int(dest) if dest.lstrip('-').isdigit() else dest)
    buttons = []
    if undo == 'mute':
        buttons.append(("↩ Размутить", "u"))
    elif undo == 'ban':
        buttons.append(("↩ Разбанить", "u"))
    elif undo == 'warn':
        buttons.append(("↩ Снять варн", "w"))
    if undo in ('mute', 'warn'):
        buttons.append(("🔨 Бан", "b", "danger"))
    buttons.append(("✓", "x"))
    try:
        chat_for_form = getattr(engine, '_owner_id') if to_me else _peer_id(await engine.client.get_entity(target_chat))
        form = await engine.create_inlineform(chat_for_form, text, buttons=[buttons], reply_to=False, timeout=7 * 86400)
    except Exception:
        try:
            await engine.client.send_message(target_chat, engine.safe_html(text), parse_mode='html', link_preview=False)
        except Exception as e:
            engine.tlog(f"[bastion] журнал: {e}")
        return

    @form.on_click()
    async def _(click):
        try:
            if click.data == 'x':
                await form.edit(text, buttons=None)
                await form.close()
                return
            if click.data == 'u':
                await (do_unmute if undo == 'mute' else do_unban)(chat_id, uid)
                done = "отменено"
            elif click.data == 'w':
                await pop_warn(chat_id, uid)
                done = "варн снят"
            elif click.data == 'b':
                await do_ban(chat_id, uid)
                done = "забанен"
            else:
                return
            await form.edit(text + "\n\n" + visual.ok(done), buttons=None)
            await form.close()
        except Exception as e:
            await click.answer(f"Не вышло: {e}"[:190], show_alert=True)


# --------------------------------------------------------------------------- варны

async def warns_of(chat_id, uid):
    all_w = await db.get('warns', {}) or {}
    lst = (all_w.get(str(chat_id)) or {}).get(str(uid)) or []
    cfg = await chat_cfg(chat_id)
    days = int(cfg.get('warn_expire') or 0)
    if days:
        lst = [w for w in lst if time.time() - w.get('t', 0) < days * 86400]
    return lst


async def _set_warns(chat_id, uid, lst):
    all_w = await db.get('warns', {}) or {}
    ch = all_w.setdefault(str(chat_id), {})
    if lst:
        ch[str(uid)] = lst
    else:
        ch.pop(str(uid), None)
    if not ch:
        all_w.pop(str(chat_id), None)
    await db.set('warns', all_w)


async def add_warn(chat_id, uid, reason='', by='вы', silent=False):
    cfg = await chat_cfg(chat_id)
    lst = await warns_of(chat_id, uid)
    lst.append({'r': reason or '', 't': int(time.time()), 'by': by})
    limit = int(cfg.get('warn_limit') or 3)
    if len(lst) >= limit:
        await _set_warns(chat_id, uid, [])
        act = cfg.get('warn_action') or 'mute:86400'
        why = f"{limit}/{limit} варнов" + (f": {reason}" if reason else "")
        res = await apply_action(chat_id, uid, act, why, by=by, silent=silent)
        return f"варн {limit}/{limit} → {res}"
    await _set_warns(chat_id, uid, lst)
    if not silent:
        await warn_notice(chat_id, uid, len(lst), limit, reason)
    await log_action(chat_id, uid, f"варн {len(lst)}/{limit}", reason, by, undo='warn')
    return f"варн {len(lst)}/{limit}"


async def pop_warn(chat_id, uid):
    lst = await warns_of(chat_id, uid)
    if lst:
        lst.pop()
    await _set_warns(chat_id, uid, lst)
    return len(lst)


async def warn_notice(chat_id, uid, n, limit, reason):
    cfg = await chat_cfg(chat_id)
    if not cfg.get('notify', True):
        return
    text = (f"⚠️ {await who(uid)} — варн <b>{n}/{limit}</b>" + (f"\n▹ {_esc(reason)}" if reason else "")
            + f"\n▹ на {limit}-м: {_esc(act_label(cfg.get('warn_action')))}")
    try:
        form = await engine.create_inlineform(chat_id, text, buttons=[[("Снять варн", "unw")]], reply_to=False,
                                              timeout=int(await gcfg('notice_ttl', 60)) or 3600)
    except Exception:
        await temp_message(chat_id, text)
        return

    @form.on_click(data='unw')
    async def _(click):
        left = await pop_warn(chat_id, uid)
        await form.edit(f"✓ Варн снят — у {await who(uid)} осталось {left}/{limit}", buttons=None)
        await form.close()

    ttl = int(await gcfg('notice_ttl', 60))
    if ttl:
        async def later():
            await asyncio.sleep(ttl)
            try:
                await form.delete()
            except Exception:
                pass
        t = asyncio.ensure_future(later())
        _TASKS.add(t)
        t.add_done_callback(_TASKS.discard)


# --------------------------------------------------------------------------- команды наказаний

async def _need_group(event):
    if not event.is_group:
        await engine.answer(event, visual.err("Это команда для групп"))
        return False
    return True


async def _punish_cmd(event, act, with_time=True, delete_reply=False, silent=False):
    if not await _need_group(event):
        return
    uid, rest, reply = await target(event)
    if not uid:
        await engine.answer(event, visual.err("Кого? Ответом на сообщение, @username или id"))
        return
    if uid in (await admins(event.chat_id)) and act != 'unban':
        await engine.answer(event, visual.err("Это админ чата — сначала .demote"))
        return
    sec, reason = split_dur_reason(rest) if with_time else (0, rest)
    if silent:
        try:
            await event.delete()
        except Exception:
            pass
    try:
        if delete_reply and reply is not None:
            try:
                await reply.delete()
            except Exception:
                pass
        res = await apply_action(event.chat_id, uid, act_str(act, sec), reason, by='вы', silent=True, delete_msg=False)
    except Exception as e:
        if not silent:
            await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    if silent:
        return
    await engine.answer(event, "\n".join([f"🛡 {await who(uid)} — <b>{_esc(res)}</b>"]
                                         + ([f"▹ {_esc(reason)}"] if reason else [])))


@decorators.command('ban', 'бан')
async def ban_cmd(event):
    """забанить: [кто] [время] [причина] — ответом, @user или id; без времени — навсегда"""
    await _punish_cmd(event, 'ban')


@decorators.command('dban')
async def dban_cmd(event):
    """забанить и удалить его сообщение (ответом): [время] [причина]"""
    await _punish_cmd(event, 'ban', delete_reply=True)


@decorators.command('sban')
async def sban_cmd(event):
    """тихий бан: команда и сообщение исчезают, в чате — ни слова: [кто] [время] [причина]"""
    await _punish_cmd(event, 'ban', delete_reply=True, silent=True)


@decorators.command('unban', 'разбан')
async def unban_cmd(event):
    """разбанить: [кто]"""
    if not await _need_group(event):
        return
    uid, _rest, _r = await target(event)
    if not uid:
        await engine.answer(event, visual.err("Кого? Ответом, @username или id"))
        return
    try:
        await do_unban(event.chat_id, uid)
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    await log_action(event.chat_id, uid, "разбан", '', 'вы')
    await engine.answer(event, visual.ok(f"{await who(uid)} разбанен(а)"))


@decorators.command('kick', 'кик')
async def kick_cmd(event):
    """выгнать (сможет вернуться по ссылке): [кто] [причина]"""
    await _punish_cmd(event, 'kick', with_time=False)


@decorators.command('dkick')
async def dkick_cmd(event):
    """выгнать и удалить его сообщение (ответом): [причина]"""
    await _punish_cmd(event, 'kick', with_time=False, delete_reply=True)


@decorators.command('mute', 'ro', 'мут')
async def ro_cmd(event):
    """мут — только чтение: [кто] [время] [причина]; без времени — навсегда"""
    await _punish_cmd(event, 'mute')


@decorators.command('unmute', 'unro', 'размут')
async def unro_cmd(event):
    """снять мут: [кто]"""
    if not await _need_group(event):
        return
    uid, _rest, _r = await target(event)
    if not uid:
        await engine.answer(event, visual.err("Кого? Ответом, @username или id"))
        return
    try:
        await do_unmute(event.chat_id, uid)
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    await log_action(event.chat_id, uid, "размут", '', 'вы')
    await engine.answer(event, visual.ok(f"{await who(uid)} снова может писать"))


@decorators.command('warn', 'варн')
async def warn_cmd(event):
    """предупреждение: [кто] [причина]; на лимите — наказание из настроек"""
    await _warn_cmd(event, False)


@decorators.command('dwarn')
async def dwarn_cmd(event):
    """варн и удалить сообщение (ответом): [причина]"""
    await _warn_cmd(event, True)


async def _warn_cmd(event, delete_reply):
    if not await _need_group(event):
        return
    uid, rest, reply = await target(event)
    if not uid:
        await engine.answer(event, visual.err("Кого? Ответом, @username или id"))
        return
    if uid in await admins(event.chat_id):
        await engine.answer(event, visual.err("Админам варны не выдаются"))
        return
    if delete_reply and reply is not None:
        try:
            await reply.delete()
        except Exception:
            pass
    try:
        res = await add_warn(event.chat_id, uid, rest, by='вы', silent=True)
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    cfg = await chat_cfg(event.chat_id)
    await engine.answer(event, f"⚠️ {await who(uid)} — <b>{_esc(res)}</b>" + (f"\n▹ {_esc(rest)}" if rest else "")
                        + (f"\n▹ на {cfg['warn_limit']}-м: {_esc(act_label(cfg['warn_action']))}" if '→' not in res else ""))


@decorators.command('unwarn', 'снятьварн')
async def unwarn_cmd(event):
    """снять последний варн: [кто]"""
    if not await _need_group(event):
        return
    uid, _rest, _r = await target(event)
    if not uid:
        await engine.answer(event, visual.err("У кого? Ответом, @username или id"))
        return
    left = await pop_warn(event.chat_id, uid)
    cfg = await chat_cfg(event.chat_id)
    await engine.answer(event, visual.ok(f"Варн снят — у {await who(uid)} {left}/{cfg['warn_limit']}"))


@decorators.command('resetwarns')
async def resetwarns_cmd(event):
    """снять все варны: [кто]"""
    if not await _need_group(event):
        return
    uid, _rest, _r = await target(event)
    if not uid:
        await engine.answer(event, visual.err("У кого? Ответом, @username или id"))
        return
    await _set_warns(event.chat_id, uid, [])
    await engine.answer(event, visual.ok(f"Все варны {await who(uid)} сняты"))


@decorators.command('warns', 'варны')
async def warns_cmd(event):
    """варны человека [кто]; без аргумента и ответа — все с варнами в этом чате"""
    if not await _need_group(event):
        return
    cfg = await chat_cfg(event.chat_id)
    uid, _rest, _r = await target(event)
    if uid:
        lst = await warns_of(event.chat_id, uid)
        lines = [visual.title("Варны", sub=f"{len(lst)}/{cfg['warn_limit']}"), visual.field("кто", await who(uid), code=False)]
        for i, w in enumerate(lst, 1):
            when = dt.datetime.fromtimestamp(w.get('t', 0)).strftime('%d.%m %H:%M')
            lines.append(visual.item(f"{i}. {when} — {_esc(w.get('r') or 'без причины')} ({_esc(w.get('by', ''))})"))
        if not lst:
            lines.append(visual.hint("чисто"))
        await engine.answer(event, "\n".join(lines))
        return
    all_w = ((await db.get('warns', {}) or {}).get(str(event.chat_id)) or {})
    lines = [visual.title("Варны в чате", sub=f"лимит {cfg['warn_limit']} → {act_label(cfg['warn_action'])}")]
    for u, lst in sorted(all_w.items(), key=lambda kv: -len(kv[1]))[:40]:
        live = await warns_of(event.chat_id, int(u))
        if live:
            lines.append(visual.item(f"{await who(int(u))} — {len(live)}/{cfg['warn_limit']}"))
    if len(lines) == 1:
        lines.append(visual.hint("ни у кого нет варнов"))
    await engine.answer(event, "\n".join(lines))


@decorators.command('gban')
async def gban_cmd(event):
    """глобальный бан во всех чатах под защитой Bastion: [кто] [причина]"""
    uid, rest, _r = await target(event)
    if not uid:
        await engine.answer(event, visual.err("Кого? Ответом, @username или id"))
        return
    gb = await db.get('gbans', {}) or {}
    gb[str(uid)] = {'r': rest, 't': int(time.time())}
    await db.set('gbans', gb)
    await engine.answer(event, visual.wait("Баню во всех чатах…"))
    ok, fail = 0, 0
    for cid in await protected_chats():
        try:
            if (await my_rights(cid)).get('ban'):
                await do_ban(cid, uid)
                ok += 1
        except Exception:
            fail += 1
    await engine.answer(event, visual.ok(f"{await who(uid)} — глобальный бан") + "\n"
                        + visual.field("чатов", ok) + (("\n" + visual.field("не вышло", fail)) if fail else "")
                        + "\n" + visual.hint("зайдёт или напишет в любом защищённом чате — будет забанен там тоже"))


@decorators.command('ungban')
async def ungban_cmd(event):
    """снять глобальный бан: [кто]"""
    uid, _rest, _r = await target(event)
    if not uid:
        await engine.answer(event, visual.err("Кого? Ответом, @username или id"))
        return
    gb = await db.get('gbans', {}) or {}
    gb.pop(str(uid), None)
    await db.set('gbans', gb)
    n = 0
    for cid in await protected_chats():
        try:
            await do_unban(cid, uid)
            n += 1
        except Exception:
            pass
    await engine.answer(event, visual.ok(f"Глобальный бан снят, разбанен в {n} чатах"))


@decorators.command('trust')
async def trust_cmd(event):
    """доверять: фильтры и антифлуд не трогают этого человека в этом чате: [кто]"""
    await _trust(event, True)


@decorators.command('untrust')
async def untrust_cmd(event):
    """перестать доверять: [кто]"""
    await _trust(event, False)


async def _trust(event, yes):
    if not await _need_group(event):
        return
    uid, _rest, _r = await target(event)
    if not uid:
        await engine.answer(event, visual.err("Кого? Ответом, @username или id"))
        return
    cfg = await chat_cfg(event.chat_id)
    s = set(cfg.get('trusted') or [])
    (s.add if yes else s.discard)(uid)
    cfg['trusted'] = sorted(s)
    await save_cfg(event.chat_id)
    await engine.answer(event, visual.ok(f"{await who(uid)} — " + ("доверенный" if yes else "больше не доверенный")))


# --------------------------------------------------------------------------- чат: админка, закрепы, права

@decorators.command('promote')
async def promote_cmd(event):
    """сделать админом: [кто] [звание]; .promote full [кто] — со всеми правами (кроме назначения админов)"""
    if not await _need_group(event):
        return
    args = engine.get_args().strip()
    full = bool(re.match(r'(?i)^full\b', args))
    uid, rest, _r = await target(event, re.sub(r'(?i)^full\s*', '', args) if full else args)
    if not uid:
        await engine.answer(event, visual.err("Кого? Ответом, @username или id"))
        return
    from telethon.tl.functions.channels import EditAdminRequest
    from telethon.tl.types import ChatAdminRights
    rights = ChatAdminRights(change_info=full, delete_messages=True, ban_users=True, invite_users=True,
                             pin_messages=True, manage_call=True, other=True, manage_topics=full,
                             post_stories=full, edit_stories=full, delete_stories=full)
    try:
        await engine.client(EditAdminRequest(await engine.client.get_input_entity(event.chat_id),
                                             await engine.client.get_input_entity(uid), rights, rank=(rest or '')[:16]))
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    _ADMINS.pop(event.chat_id, None)
    await engine.answer(event, visual.ok(f"{await who(uid)} — админ" + (f" «{_esc(rest[:16])}»" if rest else ""))
                        + ("\n" + visual.hint("полные права") if full else ""))


@decorators.command('demote')
async def demote_cmd(event):
    """снять админку: [кто]"""
    if not await _need_group(event):
        return
    uid, _rest, _r = await target(event)
    if not uid:
        await engine.answer(event, visual.err("Кого? Ответом, @username или id"))
        return
    from telethon.tl.functions.channels import EditAdminRequest
    from telethon.tl.types import ChatAdminRights
    try:
        await engine.client(EditAdminRequest(await engine.client.get_input_entity(event.chat_id),
                                             await engine.client.get_input_entity(uid), ChatAdminRights(), rank=''))
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    _ADMINS.pop(event.chat_id, None)
    await engine.answer(event, visual.ok(f"{await who(uid)} больше не админ"))


@decorators.command('pin')
async def pin_cmd(event):
    """закрепить сообщение (ответом); [loud] — с уведомлением всем"""
    if not event.is_reply:
        await engine.answer(event, visual.err("Ответьте на сообщение, которое закрепить"))
        return
    r = await event.get_reply_message()
    loud = engine.get_args().strip().lower() in ('loud', 'громко', 'notify')
    try:
        await engine.client.pin_message(event.chat_id, r, notify=loud)
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    await event.delete()


@decorators.command('unpin')
async def unpin_cmd(event):
    """открепить (ответом — это сообщение, без ответа — последнее); [all] — все"""
    allp = engine.get_args().strip().lower() in ('all', 'все', 'всё')
    try:
        if allp:
            await engine.client.unpin_message(event.chat_id)
        else:
            r = await event.get_reply_message() if event.is_reply else None
            if r is None:
                from telethon.tl.types import InputMessagesFilterPinned
                pinned = await engine.client.get_messages(event.chat_id, limit=1, filter=InputMessagesFilterPinned)
                r = pinned[0] if pinned else None
            if r is None:
                await engine.answer(event, visual.err("Закрепов нет"))
                return
            await engine.client.unpin_message(event.chat_id, r)
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    await engine.answer(event, visual.ok("Откреплено" + (" всё" if allp else "")))


@decorators.command('slow')
async def slow_cmd(event):
    """медленный режим: <секунды|off> (0, 10, 30, 60, 300, 900, 3600)"""
    if not await _need_group(event):
        return
    a = engine.get_args().strip().lower()
    sec = 0 if a in ('off', 'выкл', '0', '') else parse_dur(a if not a.isdigit() else a + 's')
    if sec is None:
        await engine.answer(event, visual.err("Формат: .slow 30 · .slow 5m · .slow off"))
        return
    sec = min(SLOW_STEPS, key=lambda s: abs(s - sec))
    from telethon.tl.functions.channels import ToggleSlowModeRequest
    try:
        await engine.client(ToggleSlowModeRequest(await engine.client.get_input_entity(event.chat_id), sec))
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    await engine.answer(event, visual.ok("Медленный режим выключен" if not sec else f"Медленный режим: раз в {fmt_dur(sec)}"))


@decorators.command('staff')
async def staff_cmd(event):
    """админы чата со званиями"""
    if not await _need_group(event):
        return
    _ADMINS.pop(event.chat_id, None)
    adm = await admins(event.chat_id)
    lines = [visual.title("Админы", sub=str(len(adm)))]
    for uid, rank in adm.items():
        ent = await entity(uid)
        if getattr(ent, 'bot', False):
            continue
        lines.append(visual.item(f"{mention(uid, name_of(ent))} — {_esc(rank)}"))
    bots = [uid for uid in adm if getattr(await entity(uid), 'bot', False)]
    if bots:
        lines.append(visual.section("Боты"))
        lines += [visual.item(f"{mention(u, name_of(await entity(u)))}") for u in bots]
    await engine.answer(event, "\n".join(lines))


@decorators.command('bans')
async def bans_cmd(event):
    """забаненные в чате (до 50)"""
    if not await _need_group(event):
        return
    from telethon.tl.types import ChannelParticipantsKicked
    lines = [visual.title("Забаненные")]
    n = 0
    try:
        async for u in engine.client.iter_participants(event.chat_id, filter=ChannelParticipantsKicked, limit=50):
            n += 1
            p = getattr(u, 'participant', None)
            ud = getattr(getattr(p, 'banned_rights', None), 'until_date', None)
            till = "навсегда" if not ud or ud.year > 2100 else ud.astimezone().strftime('до %d.%m %H:%M')
            lines.append(visual.item(f"{mention(u.id, name_of(u))} <code>{u.id}</code> — {till}"))
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    if not n:
        lines.append(visual.hint("никого"))
    await engine.answer(event, "\n".join(lines))


async def current_rights(chat_id):
    ent = await engine.client.get_entity(chat_id)
    r = getattr(ent, 'default_banned_rights', None)
    return {f: bool(getattr(r, f, False)) for f in RIGHT_FIELDS} if r else {f: False for f in RIGHT_FIELDS}


async def set_rights(chat_id, banned: dict):
    from telethon.tl.functions.messages import EditChatDefaultBannedRightsRequest
    from telethon.tl.types import ChatBannedRights
    kw = {f: bool(banned.get(f)) for f in RIGHT_FIELDS if f != 'view_messages'}
    await engine.client(EditChatDefaultBannedRightsRequest(await engine.client.get_input_entity(chat_id),
                                                           ChatBannedRights(until_date=None, **kw)))


def locks_text(cur):
    lines = [visual.title("Права участников", sub="🔒 — запрещено всем, кроме админов")]
    for key, label, fields in LOCKS:
        locked = any(cur.get(f) for f in fields)
        lines.append(f"{'🔒' if locked else '🔓'} {label} <code>{key}</code>")
    return "\n".join(lines)


def locks_buttons(cur, prefix='lk:', last=None):
    rows, row = [], []
    for key, label, fields in LOCKS:
        locked = any(cur.get(f) for f in fields)
        row.append(B(("🔒 " if locked else "🔓 ") + label, f"{prefix}{key}", 'danger' if locked else None))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("🔓 Открыть всё", f"{prefix}!open", 'success'),
                 last or (visual.btn('Закрыть', 'close'), f"{prefix}!close")])
    return rows


async def toggle_lock(chat_id, key):
    c = await current_rights(chat_id)
    if key == '!open':
        c = {f: False for f in RIGHT_FIELDS}
    else:
        spec = next((x for x in LOCKS if x[0] == key), None)
        if spec is None:
            return
        new = not any(c.get(f) for f in spec[2])
        for f in spec[2]:
            c[f] = new
    await set_rights(chat_id, c)


@decorators.command('locks', 'права')
async def locks_cmd(event):
    """права участников: панель кнопками или <что> on|off (all media stickers gifs links polls …)"""
    if not await _need_group(event):
        return
    args = engine.get_args().strip().lower().split()
    try:
        cur = await current_rights(event.chat_id)
    except Exception as e:
        await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
        return
    if args:
        key = args[0]
        spec = next((x for x in LOCKS if x[0] == key), None)
        if spec is None:
            await engine.answer(event, visual.err("Что закрыть? " + " ".join(k for k, _, _ in LOCKS)))
            return
        on = (args[1] if len(args) > 1 else 'on') in ('on', 'вкл', 'lock', 'закрыть', '1')
        for f in spec[2]:
            cur[f] = on
        try:
            await set_rights(event.chat_id, cur)
        except Exception as e:
            await engine.answer(event, visual.err(f"Не вышло: {_esc(e)}"))
            return
        await engine.answer(event, visual.ok(f"{spec[1]}: " + ("закрыто 🔒" if on else "открыто 🔓")))
        return
    chat_id = event.chat_id
    try:
        form = await engine.create_inlineform(chat_id, locks_text(cur), buttons=locks_buttons(cur), timeout=600)
    except Exception:
        await engine.answer(event, locks_text(cur) + "\n\n" + visual.hint(".locks <что> on|off"))
        return
    await event.delete()

    @form.on_click(data_startswith='lk:')
    async def _(click):
        k = click.data[3:]
        if k == '!close':
            await form.delete()
            return
        try:
            await toggle_lock(chat_id, k)
        except Exception as e:
            await click.answer(f"Не вышло: {e}"[:190], show_alert=True)
            return
        c = await current_rights(chat_id)
        await form.edit(locks_text(c), buttons=locks_buttons(c))


# --------------------------------------------------------------------------- команды настроек защиты

async def _cfg_for(event):
    if not event.is_group:
        await engine.answer(event, visual.err("Команда настраивает защиту группы — пишите её в группе"))
        return None
    return await chat_cfg(event.chat_id)


@decorators.command('bfilter', 'bfilters')
async def bfilter_cmd(event):
    """фильтры чата: список или <фильтр> <del|warn|mute[:время]|kick|ban[:время]|off>"""
    cfg = await _cfg_for(event)
    if cfg is None:
        return
    args = engine.get_args().strip().split(maxsplit=1)
    if not args:
        lines = [visual.title("Фильтры", sub="защита " + ("включена" if cfg['on'] else "выключена — .bastion on"))]
        for key, label in FILTERS:
            a = cfg['filters'].get(key)
            lines.append(f"{'◉' if a else '○'} {label} <code>{key}</code> — {_esc(act_label(a))}")
        lines.append("")
        lines.append(visual.hint(".bfilter links mute:1h · .bfilter stickers del · .bfilter links off"))
        await engine.answer(event, "\n".join(lines))
        return
    key = args[0].lower()
    if key not in FILTER_LABEL:
        await engine.answer(event, visual.err("Нет такого фильтра. Есть: " + " ".join(k for k, _ in FILTERS)))
        return
    pa = parse_action(args[1] if len(args) > 1 else 'del')
    if pa is None:
        await engine.answer(event, visual.err("Действие: del · warn · mute[:время] · kick · ban[:время] · off"))
        return
    cfg['filters'][key] = None if pa[0] == 'off' else act_str(*pa)
    if key == 'newbie' and cfg['filters'][key] is None:
        cfg['filters'].pop(key, None)
    await save_cfg(event.chat_id)
    await engine.answer(event, visual.ok(f"{FILTER_LABEL[key]}: {act_label(cfg['filters'].get(key))}")
                        + ("" if cfg['on'] else "\n" + visual.warn("защита чата выключена — .bastion on")))


def _split_word_action(text):
    """'казино, ставки mute:1h' → (['казино', 'ставки'], 'mute:3600')."""
    parts = text.rsplit(maxsplit=1)
    action = 'del'
    if len(parts) == 2:
        pa = parse_action(parts[1])
        if pa and pa[0] != 'off':
            action, text = act_str(*pa), parts[0]
    return [w.strip() for w in text.split(',') if w.strip()], action


@decorators.command('bword', 'bwords', 'стопслово')
async def bword_cmd(event):
    """стоп-слова: список · add <слово, фраза> [действие] · re <регулярка> [действие] · del <№|слово> · clear"""
    cfg = await _cfg_for(event)
    if cfg is None:
        return
    args = engine.get_args().strip()
    sub, _, rest = args.partition(' ')
    sub = sub.lower()
    words = cfg['words']
    if sub in ('add', '+', 'добавить'):
        lst, action = _split_word_action(rest)
        if not lst:
            await engine.answer(event, visual.err("Формат: .bword add слово, фраза [mute:1h]"))
            return
        for w in lst:
            words.append({'p': w.lower(), 're': False, 'a': action})
        await save_cfg(event.chat_id)
        await engine.answer(event, visual.ok(f"Добавлено: {len(lst)} → {act_label(action)}"))
        return
    if sub in ('re', 'regex'):
        pattern, action = rest, 'del'
        m = re.match(r'^(.*\S)\s+(\S+)$', rest)
        if m and parse_action(m.group(2)) and parse_action(m.group(2))[0] != 'off':
            pattern, action = m.group(1), act_str(*parse_action(m.group(2)))
        try:
            re.compile(pattern)
        except re.error as e:
            await engine.answer(event, visual.err(f"Регулярка с ошибкой: {_esc(e)}"))
            return
        words.append({'p': pattern, 're': True, 'a': action})
        await save_cfg(event.chat_id)
        await engine.answer(event, visual.ok(f"Регулярка добавлена → {act_label(action)}"))
        return
    if sub in ('del', 'rm', '-', 'удалить'):
        r = rest.strip().lower()
        if r.isdigit() and 1 <= int(r) <= len(words):
            words.pop(int(r) - 1)
        else:
            before = len(words)
            words[:] = [w for w in words if w['p'] != r]
            if len(words) == before:
                await engine.answer(event, visual.err("Не нашёл — номер из .bword или само слово"))
                return
        await save_cfg(event.chat_id)
        await engine.answer(event, visual.ok("Удалено"))
        return
    if sub in ('clear', 'очистить'):
        words.clear()
        await save_cfg(event.chat_id)
        await engine.answer(event, visual.ok("Стоп-слова очищены"))
        return
    lines = [visual.title("Стоп-слова", sub=str(len(words)))]
    for i, w in enumerate(words, 1):
        lines.append(f"{i}. {'<i>re</i> ' if w['re'] else ''}<code>{_esc(w['p'])}</code> — {_esc(act_label(w['a']))}")
    if not words:
        lines.append(visual.hint("пусто"))
    lines.append("")
    lines.append(visual.hint(".bword add казино, ставки mute:1d · .bword re (?i)крипт[ао] ban · .bword del 2"))
    await engine.answer(event, "\n".join(lines))


@decorators.command('captcha', 'капча')
async def captcha_cmd(event):
    """капча новичкам: on|off · mode button|math|emoji · time <сек> · fail kick|ban|mute"""
    cfg = await _cfg_for(event)
    if cfg is None:
        return
    a = engine.get_args().strip().lower().split()
    c = cfg['captcha']
    if a and a[0] in ('on', 'off', 'вкл', 'выкл'):
        c['on'] = a[0] in ('on', 'вкл')
    elif len(a) >= 2 and a[0] == 'mode' and a[1] in ('button', 'math', 'emoji'):
        c['mode'] = a[1]
    elif len(a) >= 2 and a[0] in ('time', 'timeout'):
        sec = parse_dur(a[1] if not a[1].isdigit() else a[1] + 's')
        if not sec:
            await engine.answer(event, visual.err("Формат: .captcha time 120"))
            return
        c['timeout'] = max(30, min(sec, 3600))
    elif len(a) >= 2 and a[0] == 'fail' and a[1] in ('kick', 'ban', 'mute'):
        c['fail'] = a[1]
    elif a:
        await engine.answer(event, visual.err("on|off · mode button|math|emoji · time <сек> · fail kick|ban|mute"))
        return
    await save_cfg(event.chat_id)
    await engine.answer(event, "\n".join([
        visual.title("Капча", sub="включена" if c['on'] else "выключена"),
        visual.field("вид", {'button': 'кнопка «я человек»', 'math': 'пример', 'emoji': 'найти эмодзи'}[c['mode']], code=False),
        visual.field("время", fmt_dur(c['timeout']), code=False),
        visual.field("не прошёл", {'kick': 'кик', 'ban': 'бан', 'mute': 'остаётся в муте'}[c['fail']], code=False)]
        + ([] if cfg['on'] else ["", visual.warn("защита чата выключена — .bastion on")])))


@decorators.command('antiflood', 'флуд')
async def antiflood_cmd(event):
    """антифлуд: <сообщений> <секунд> [действие] | off — например .antiflood 6 5 mute:10m"""
    cfg = await _cfg_for(event)
    if cfg is None:
        return
    a = engine.get_args().strip().lower().split()
    f = cfg['flood']
    if a and a[0] in ('off', 'выкл'):
        f['on'] = False
    elif a and a[0] in ('on', 'вкл'):
        f['on'] = True
    elif len(a) >= 2 and a[0].isdigit() and a[1].isdigit():
        f.update(on=True, msgs=max(2, min(int(a[0]), 50)), secs=max(1, min(int(a[1]), 120)))
        if len(a) >= 3:
            pa = parse_action(a[2])
            if pa and pa[0] != 'off':
                f['action'] = act_str(*pa)
    elif a:
        await engine.answer(event, visual.err("Формат: .antiflood 6 5 mute:10m · .antiflood off"))
        return
    await save_cfg(event.chat_id)
    await engine.answer(event, visual.title("Антифлуд", sub="включён" if f['on'] else "выключен") + "\n"
                        + visual.field("порог", f"{f['msgs']} сообщений за {f['secs']} с", code=False) + "\n"
                        + visual.field("действие", act_label(f['action']), code=False))


@decorators.command('antiraid', 'рейд')
async def antiraid_cmd(event):
    """антирейд: <входов> <секунд> [минут режима] | off — наплыв: новичков в мут, вам — тревога"""
    cfg = await _cfg_for(event)
    if cfg is None:
        return
    a = engine.get_args().strip().lower().split()
    r = cfg['raid']
    if a and a[0] in ('off', 'выкл'):
        r['on'] = False
        _RAID_UNTIL.pop(event.chat_id, None)
    elif a and a[0] in ('on', 'вкл'):
        r['on'] = True
    elif len(a) >= 2 and a[0].isdigit() and a[1].isdigit():
        r.update(on=True, joins=max(3, int(a[0])), secs=max(5, int(a[1])))
        if len(a) >= 3 and a[2].isdigit():
            r['minutes'] = max(1, min(int(a[2]), 1440))
    elif a:
        await engine.answer(event, visual.err("Формат: .antiraid 10 60 30 · .antiraid off"))
        return
    await save_cfg(event.chat_id)
    await engine.answer(event, visual.title("Антирейд", sub="включён" if r['on'] else "выключен") + "\n"
                        + visual.field("порог", f"{r['joins']} входов за {r['secs']} с", code=False) + "\n"
                        + visual.field("режим", f"{r['minutes']} мин: новички сразу в мут", code=False))


@decorators.command('night', 'ночь')
async def night_cmd(event):
    """ночной режим: 23:00-07:00 | off — в это время писать могут только админы"""
    cfg = await _cfg_for(event)
    if cfg is None:
        return
    a = engine.get_args().strip().lower()
    n = cfg['night']
    m = re.match(r'^(\d{1,2})[:.](\d{2})\s*[-–— ]\s*(\d{1,2})[:.](\d{2})$', a)
    if a in ('off', 'выкл'):
        n['on'] = False
    elif a in ('on', 'вкл'):
        n['on'] = True
    elif m:
        n.update(on=True, **{'from': f"{int(m.group(1)):02}:{m.group(2)}", 'to': f"{int(m.group(3)):02}:{m.group(4)}"})
    elif a:
        await engine.answer(event, visual.err("Формат: .night 23:00-07:00 · .night off"))
        return
    await save_cfg(event.chat_id)
    await engine.answer(event, visual.ok(f"Ночной режим: {n['from']}–{n['to']}" if n['on'] else "Ночной режим выключен")
                        + "\n" + visual.hint("время — часы компьютера, где запущен FRX"))


async def _greet_cmd(event, key):
    cfg = await _cfg_for(event)
    if cfg is None:
        return
    raw = engine.get_args().strip()
    a = raw.lower()
    g = cfg[key]
    if a in ('on', 'вкл'):
        g['on'] = True
    elif a in ('off', 'выкл'):
        g['on'] = False
    elif key == 'welcome' and a.startswith('del'):
        sec = parse_dur(a[3:].strip() + ('s' if a[3:].strip().isdigit() else '')) if a[3:].strip() else 0
        g['del'] = int(sec or 0)
    elif a in ('reset', 'сброс'):
        g['text'] = DEF_WELCOME if key == 'welcome' else DEF_GOODBYE
    elif raw:
        g.update(on=True, text=raw[:3000])
    await save_cfg(event.chat_id)
    preview_text = await render_greet(g['text'], event.chat_id, await engine.client.get_me())
    await engine.answer(event, "\n".join([
        visual.title("Приветствие" if key == 'welcome' else "Прощание", sub="включено" if g['on'] else "выключено"),
        preview_text, "",
        visual.hint("{name} {mention} {first} {username} {id} {chat} {count}"
                    + (" · .welcome del 60 — удалять через минуту" if key == 'welcome' else ""))]))


@decorators.command('welcome', 'приветствие')
async def welcome_cmd(event):
    """приветствие новичков: <текст> | on | off | del <сек> | reset"""
    await _greet_cmd(event, 'welcome')


@decorators.command('goodbye', 'прощание')
async def goodbye_cmd(event):
    """прощание: <текст> | on | off | reset"""
    await _greet_cmd(event, 'goodbye')


async def render_greet(tpl, chat_id, user):
    title = await chat_title(chat_id)
    count = ''
    if '{count}' in tpl:
        try:
            count = str((await engine.client.get_participants(chat_id, limit=0)).total)
        except Exception:
            count = '?'
    name = name_of(user)
    rep = {'{name}': _esc(name), '{mention}': mention(user.id, name), '{first}': _esc(getattr(user, 'first_name', '') or name),
           '{last}': _esc(getattr(user, 'last_name', '') or ''),
           '{username}': ('@' + user.username) if getattr(user, 'username', None) else _esc(name),
           '{id}': str(user.id), '{chat}': _esc(title), '{count}': count}
    out = tpl
    for k, v in rep.items():
        out = out.replace(k, v)
    return out


# --------------------------------------------------------------------------- проверка сообщений

_URL_RE = re.compile(r'(?i)\b(?:https?://|www\.)\S+|\b[a-z0-9-]{2,}\.(?:com|ru|net|org|io|me|xyz|top|site|online|shop|info|biz|'
                     r'link|club|pro|app|dev|cc|co|gg|ly|su|рф|tk|ml|ga|cf|ua|by|kz|uz)\b(?:/\S*)?')
_TME_RE = re.compile(r'(?i)(?:https?://)?(?:t(?:elegram)?\.(?:me|dog)|tg\.dev)/(\+|joinchat/)?([\w-]+)')
_ARABIC = re.compile(r'[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]')
_CJK = re.compile(r'[぀-ヿ㐀-䶿一-鿿가-힯]')
_ZALGO = re.compile(r'[̀-ͯ҃-҉᪰-᫿᷀-᷿⃐-⃿︠-︯]')
_EMOJI = re.compile('[\U0001F000-\U0001FAFF☀-➿⬀-⯿\U0001F1E6-\U0001F1FF]')


def media_kind(m):
    """Какому медиа-фильтру подпадает сообщение."""
    if getattr(m, 'sticker', None):
        return 'stickers'
    if getattr(m, 'gif', None):
        return 'gifs'
    if getattr(m, 'video_note', None):
        return 'rounds'
    if getattr(m, 'voice', None):
        return 'voices'
    if getattr(m, 'audio', None):
        return 'audio'
    if getattr(m, 'photo', None):
        return 'photos'
    if getattr(m, 'video', None):
        return 'videos'
    if getattr(m, 'poll', None):
        return 'polls'
    if getattr(m, 'contact', None) or getattr(m, 'geo', None) or getattr(m, 'venue', None):
        return 'contacts'
    if getattr(m, 'game', None) or getattr(m, 'dice', None):
        return 'games'
    if type(getattr(m, 'media', None)).__name__ == 'MessageMediaStory':
        return 'stories'
    if getattr(m, 'document', None):
        return 'files'
    return None


async def violations(event, cfg, chat_username=None):
    """[(фильтр, действие, причина)] в порядке проверки; первая — главная."""
    m = event.message
    f = cfg['filters']
    text = m.message or ''
    ents = m.entities or []
    out = []

    def hit(key, why):
        a = f.get(key)
        if a:
            out.append((key, a, why))

    names = {type(e).__name__ for e in ents}
    has_link = bool({'MessageEntityUrl', 'MessageEntityTextUrl'} & names) or bool(_URL_RE.search(text))
    tme = [x for x in _TME_RE.findall(text + ' ' + ' '.join(getattr(e, 'url', '') or '' for e in ents))]
    if any(p or (n.lower() not in ('c', 's', 'addstickers', 'addemoji', 'share', 'iv') and
                 (not chat_username or n.lower() != chat_username.lower())) for p, n in tme):
        hit('invites', 'ссылка-приглашение')
    if has_link:
        hit('links', 'ссылка')
    if m.fwd_from:
        hit('forwards', 'пересылка')
    if sum(1 for e in ents if type(e).__name__ in ('MessageEntityMention', 'MessageEntityMentionName')) >= 5:
        hit('mentions', 'массовые упоминания')
    if getattr(m, 'via_bot_id', None) or getattr(m, 'reply_markup', None):
        hit('inline', 'через бота')
    mk = media_kind(m)
    if mk:
        hit(mk, FILTER_LABEL.get(mk, mk))
    if 'MessageEntityCustomEmoji' in names:
        hit('premium', 'премиум-эмодзи')
    if len(_EMOJI.findall(text)) + sum(1 for e in ents if type(e).__name__ == 'MessageEntityCustomEmoji') >= 10:
        hit('emoji', 'эмодзи-спам')
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        hit('caps', 'капс')
    if len(_ZALGO.findall(text)) >= 12:
        hit('zalgo', 'залго')
    if _ARABIC.search(text):
        hit('arabic', 'арабская вязь')
    if _CJK.search(text):
        hit('cjk', 'иероглифы')
    if len(text) > 2000:
        hit('long', 'простыня')
    sid = event.sender_id or 0
    if sid < 0 and sid != event.chat_id:
        hit('sendas', 'от имени канала')
    if f.get('newbie') and (has_link or m.fwd_from or mk):
        joined = ((await db.get('joined', {}) or {}).get(str(event.chat_id)) or {}).get(str(sid))
        if joined and time.time() - joined < int(cfg.get('newbie_hours') or 24) * 3600:
            hit('newbie', 'новичок: ссылки и медиа пока нельзя')
    low = text.lower()
    for w in cfg['words']:
        try:
            ok = re.search(w['p'], text) if w['re'] else (re.search(r'(?<!\w)' + re.escape(w['p']) + r'(?!\w)', low)
                                                         if ' ' not in w['p'] and len(w['p']) <= 3 else w['p'] in low)
        except re.error:
            ok = False
        if ok:
            out.append(('word', w['a'], f"стоп-слово «{w['p'][:30]}»"))
            break
    return out


@decorators.watcher()
async def guard(event):
    if not event.is_group:
        return
    chat_id = event.chat_id
    if int(chat_id) not in _CFG and int(chat_id) not in await protected_chats():
        await _maybe_report(event, None)
        return
    cfg = await chat_cfg(chat_id)
    if not cfg.get('on'):
        await _maybe_report(event, cfg)
        return
    if await _maybe_report(event, cfg):
        return
    if event.out:
        return
    sid = event.sender_id
    if not sid:
        return
    gb = await db.get('gbans', {}) or {}
    if str(sid) in gb and (await my_rights(chat_id)).get('ban'):
        try:
            await apply_action(chat_id, sid, 'ban', 'глобальный бан: ' + (gb[str(sid)].get('r') or ''), by='gban', msg=event.message)
        except Exception as e:
            engine.tlog(f"[bastion] gban: {e}")
        return
    st = _CAPTCHA.get((chat_id, sid))
    if st is not None:                                   # ещё не прошёл капчу
        if st.get('form') is None:
            await captcha_text_answer(event, st)
        else:
            try:
                await event.delete()
            except Exception:
                pass
        return
    if await is_immune(chat_id, sid, cfg):
        return
    rights = await my_rights(chat_id)
    if not rights.get('delete'):
        return
    fl = cfg['flood']
    if fl.get('on'):
        q = _FLOOD.setdefault((chat_id, sid), deque(maxlen=60))
        now = time.time()
        q.append(now)
        while q and now - q[0] > fl['secs']:
            q.popleft()
        if len(q) >= fl['msgs']:
            q.clear()
            try:
                await apply_action(chat_id, sid, fl['action'], f"флуд: {fl['msgs']} сообщ. за {fl['secs']} с",
                                   by='антифлуд', msg=event.message)
            except Exception as e:
                engine.tlog(f"[bastion] антифлуд: {e}")
            return
    ent = await entity(chat_id)
    found = await violations(event, cfg, getattr(ent, 'username', None))
    if not found:
        return
    key, action, why = found[0]
    if action != 'del' and not rights.get('ban'):
        action = 'del'
    try:
        await apply_action(chat_id, sid, action, why, by='фильтр' if key != 'word' else 'стоп-слово', msg=event.message)
    except Exception as e:
        engine.tlog(f"[bastion] фильтр {key}: {e}")


_REPORT_RE = re.compile(r'^\s*(?:@admins?|[/!.]report|@админ\w*)\b', re.I)


async def _maybe_report(event, cfg):
    """Участник позвал админов: @admin, /report, !report. True — это был репорт."""
    if event.out or not _REPORT_RE.match(event.raw_text or ''):
        return False
    if cfg is None or not cfg.get('on') or not cfg.get('report', True):
        return False
    chat_id = event.chat_id
    reporter = event.sender_id
    reply = await event.get_reply_message() if event.is_reply else None
    culprit = reply.sender_id if reply else None
    ent = await entity(chat_id)
    link = msg_link(chat_id, (reply or event.message).id, getattr(ent, 'username', None))
    lines = [visual.title("Bastion", sub="репорт"),
             visual.field("чат", _esc(name_of(ent)), code=False),
             visual.field("пожаловался", await who(reporter), code=False)]
    if culprit:
        lines += [visual.field("на кого", await who(culprit) + f" <code>{culprit}</code>", code=False),
                  visual.field("сообщение", _esc((reply.raw_text or '[медиа]')[:500]), code=False)]
    extra = (event.raw_text or '').split(maxsplit=1)
    if len(extra) > 1:
        lines.append(visual.field("пишет", _esc(extra[1][:300]), code=False))
    text = "\n".join(lines)
    buttons = []
    if link:
        buttons.append([("↗ К сообщению", f"url:{link}")])
    if culprit:
        buttons.append([("🗑 Удалить", "d"), ("⚠ Варн", "w"), ("🔇 Мут 1ч", "m")])
        buttons.append([("🔨 Бан", "b", "danger"), ("✓ Всё ок", "x")])
    else:
        buttons.append([("✓ Ок", "x")])
    try:
        form = await engine.create_inlineform(getattr(engine, '_owner_id'), text, buttons=buttons, reply_to=False,
                                              timeout=3 * 86400)
    except Exception:
        try:
            await engine.bot_send_to_owner(text)
        except Exception:
            await engine.client.send_message('me', engine.safe_html(text), parse_mode='html')
        return True

    @form.on_click()
    async def _(click):
        d = click.data
        try:
            if d == 'x':
                await form.edit(text + "\n\n" + visual.ok("закрыто"), buttons=None)
                await form.close()
                return
            res = {'d': 'del', 'w': 'warn', 'm': 'mute:3600', 'b': 'ban'}.get(d)
            if not res:
                return
            r = await apply_action(chat_id, culprit, res, 'по репорту', by='вы (репорт)', msg=reply)
            await form.edit(text + "\n\n" + visual.ok(r), buttons=None)
            await form.close()
        except Exception as e:
            await click.answer(f"Не вышло: {e}"[:190], show_alert=True)
    try:
        await event.reply("✓ Админы получили жалобу")
    except Exception:
        pass
    return True


# --------------------------------------------------------------------------- входы и выходы: капча, приветствие, рейд

@decorators.telegram_event()
async def joins(event):
    chat_id = getattr(event, 'chat_id', None)
    if not chat_id or int(chat_id) not in await protected_chats():
        return
    cfg = await chat_cfg(chat_id)
    if not cfg.get('on'):
        return
    joined = getattr(event, 'user_joined', False) or getattr(event, 'user_added', False)
    left = getattr(event, 'user_left', False) or getattr(event, 'user_kicked', False)
    if not joined and not left:
        return
    if cfg.get('clean_service') and getattr(event, 'action_message', None) is not None:
        try:
            await event.action_message.delete()
        except Exception:
            pass
    ids = list(getattr(event, 'user_ids', None) or [])
    if left:
        if cfg['goodbye'].get('on') and getattr(event, 'user_left', False):
            for uid in ids:
                u = await entity(uid)
                if u is not None and not getattr(u, 'bot', False):
                    await temp_message(chat_id, await render_greet(cfg['goodbye']['text'], chat_id, u), ttl=0)
        return
    added_by = None
    if getattr(event, 'user_added', False):
        try:
            added_by = await event.get_added_by()
        except Exception:
            added_by = getattr(event, 'added_by', None)
    by_admin = bool(added_by is not None and (getattr(added_by, 'id', None) in await admins(chat_id)
                                              or getattr(added_by, 'is_self', False)))
    now = time.time()
    jd = await db.get('joined', {}) or {}
    ch = jd.setdefault(str(chat_id), {})
    for uid in ids:
        ch[str(uid)] = int(now)
    if len(ch) > 3000:
        for k in sorted(ch, key=ch.get)[:len(ch) - 3000]:
            ch.pop(k, None)
    await db.set('joined', jd)
    raid = cfg['raid']
    raid_now = False
    if raid.get('on'):
        q = _JOINS.setdefault(chat_id, deque(maxlen=500))
        for _ in ids:
            q.append(now)
        while q and now - q[0] > raid['secs']:
            q.popleft()
        if len(q) >= raid['joins'] and _RAID_UNTIL.get(chat_id, 0) < now:
            _RAID_UNTIL[chat_id] = now + raid['minutes'] * 60
            await _raid_alarm(chat_id, len(q), raid)
        raid_now = _RAID_UNTIL.get(chat_id, 0) > now
    gb = await db.get('gbans', {}) or {}
    for uid in ids:
        u = await entity(uid)
        if u is None or getattr(u, 'is_self', False) or (getattr(u, 'bot', False) and by_admin):
            continue
        if str(uid) in gb:
            try:
                await apply_action(chat_id, uid, 'ban', 'глобальный бан', by='gban')
            except Exception:
                pass
            continue
        if raid_now:
            try:
                await do_mute(chat_id, uid, raid['minutes'] * 60)
            except Exception:
                pass
            continue
        if cfg['captcha'].get('on') and not by_admin and not getattr(u, 'bot', False):
            await start_captcha(chat_id, u, cfg)
            continue
        if cfg['welcome'].get('on'):
            await send_welcome(chat_id, u, cfg)


async def _raid_alarm(chat_id, n, raid):
    text = "\n".join([visual.title("Bastion", sub="🚨 рейд"), visual.field("чат", _esc(await chat_title(chat_id)), code=False),
                      visual.field("входов", f"{n} за {raid['secs']} с", code=False),
                      visual.field("режим", f"{raid['minutes']} мин: все новички сразу в мут", code=False)])
    try:
        form = await engine.create_inlineform(getattr(engine, '_owner_id'), text, reply_to=False, timeout=86400,
                                              buttons=[[("🔒 Закрыть чат", "lock", "danger"), ("Снять режим", "stop")],
                                                       [("✓ Ок", "x")]])
    except Exception:
        try:
            await engine.bot_send_to_owner(text)
        except Exception:
            pass
        return

    @form.on_click()
    async def _(click):
        if click.data == 'lock':
            try:
                cur = await current_rights(chat_id)
                cur['send_messages'] = True
                await set_rights(chat_id, cur)
                await click.answer("Чат закрыт — откройте .locks")
            except Exception as e:
                await click.answer(f"Не вышло: {e}"[:190], show_alert=True)
                return
        elif click.data == 'stop':
            _RAID_UNTIL.pop(chat_id, None)
            await click.answer("Режим рейда снят")
        await form.edit(text + "\n\n" + visual.ok({'lock': 'чат закрыт', 'stop': 'режим снят', 'x': 'ок'}.get(click.data, '')),
                        buttons=None)
        await form.close()
    await temp_message(chat_id, "🚨 Слишком много входов — новички пока в муте. Админы предупреждены.", ttl=300)


async def send_welcome(chat_id, user, cfg):
    w = cfg['welcome']
    if w.get('clean') and _LAST_WELCOME.get(chat_id):
        try:
            await engine.client.delete_messages(chat_id, [_LAST_WELCOME[chat_id]])
        except Exception:
            pass
    m = await temp_message(chat_id, await render_greet(w['text'], chat_id, user), ttl=int(w.get('del') or 0))
    if m is not None:
        _LAST_WELCOME[chat_id] = m.id


EMOJI_SET = ['🍎', '🚗', '🐱', '⚽', '🎸', '🌵', '🍕', '🚀', '🐟', '🎈', '🌙', '🔑']
EMOJI_NAME = {'🍎': 'яблоко', '🚗': 'машину', '🐱': 'кота', '⚽': 'мяч', '🎸': 'гитару', '🌵': 'кактус', '🍕': 'пиццу',
              '🚀': 'ракету', '🐟': 'рыбу', '🎈': 'шарик', '🌙': 'луну', '🔑': 'ключ'}


async def start_captcha(chat_id, user, cfg):
    """С ботом — мут и кнопки (жмёт только новичок). Без бота — без мута: пока новичок не напишет ответ,
    его сообщения удаляются (замученный не смог бы ответить текстом)."""
    uid = user.id
    c = cfg['captcha']
    with_bot = getattr(engine, '_aiogram_bot', None) is not None
    if with_bot:
        try:
            await do_mute(chat_id, uid, 0)
        except Exception as e:
            engine.tlog(f"[bastion] капча: не смог замутить {uid}: {e}")
            return
    mode = c.get('mode', 'emoji')
    if mode == 'math':
        a, b = random.randint(2, 9), random.randint(2, 9)
        answer = str(a + b)
        opts = list({answer, str(a + b + 1), str(a + b - 1), str(a * b if a * b != a + b else a + b + 2)})
        while len(opts) < 4:
            x = str(random.randint(4, 18))
            if x not in opts:
                opts.append(x)
        question = f"сколько будет <b>{a} + {b}</b>?"
    elif mode == 'emoji':
        opts = random.sample(EMOJI_SET, 6)
        answer = random.choice(opts)
        question = f"нажмите на <b>{EMOJI_NAME[answer]}</b>"
    else:
        opts, answer = ['✅ Я человек'], '✅ Я человек'
        question = "нажмите кнопку ниже"
    random.shuffle(opts)
    text = (f"👋 {mention(uid, name_of(user))}, чтобы писать в чате, {question}\n"
            f"▹ на это есть {fmt_dur(c['timeout'])}")
    st = {'answer': answer, 'tries': 0, 'form': None, 'task': None, 'text': None, 'user': user, 'opts': opts}
    _CAPTCHA[(chat_id, uid)] = st
    rows = [[(o, f"cp:{i}") for i, o in enumerate(opts[k:k + 3], start=k)] for k in range(0, len(opts), 3)]
    form = None
    if with_bot:
        try:
            form = await engine.create_inlineform(chat_id, text, buttons=rows, for_user=[uid, getattr(engine, '_owner_id')],
                                                  reply_to=False)
            st['form'] = form
        except Exception:
            form = None
            try:
                await do_unmute(chat_id, uid)          # форма не встала — переходим на текстовый ответ
            except Exception:
                pass
    if form is not None:
        @form.on_click(data_startswith='cp:')
        async def _(click):
            clicker = getattr(click.from_user, 'id', None)
            if clicker != uid and clicker != getattr(engine, '_owner_id', None):
                await click.answer("Это проверка не для вас", show_alert=True)
                return
            choice = opts[int(click.data[3:])]
            if choice == answer or clicker == getattr(engine, '_owner_id', None):
                await captcha_pass(chat_id, user, cfg)
            else:
                st['tries'] += 1
                if st['tries'] >= 2:
                    await captcha_fail(chat_id, uid, cfg, "ошибся дважды")
                else:
                    await click.answer("Неверно — осталась одна попытка", show_alert=True)
    else:
        if mode == 'button':
            st['answer'] = 'я человек'
            q = text.replace('нажмите кнопку ниже', 'напишите «я человек»')
        elif mode == 'emoji':
            q = text.replace('нажмите на', 'отправьте эмодзи —') + "\n▹ варианты: " + " ".join(opts)
        else:
            q = text.replace('сколько будет', 'напишите числом, сколько будет')
        st['text'] = await temp_message(chat_id, q + "\n▹ до ответа ваши сообщения удаляются", ttl=0)
    st['task'] = asyncio.ensure_future(_captcha_timer(chat_id, uid, cfg, c['timeout']))


async def captcha_text_answer(event, st):
    """Без бота: новичок пишет ответ текстом."""
    chat_id, uid = event.chat_id, event.sender_id
    given = (event.raw_text or '').strip().lower().strip('«»"!. ')
    try:
        await event.delete()
    except Exception:
        pass
    if given == str(st['answer']).lower():
        await captcha_pass(chat_id, st['user'], await chat_cfg(chat_id))
        return
    st['tries'] += 1
    if st['tries'] >= 3:
        await captcha_fail(chat_id, uid, await chat_cfg(chat_id), "не ответил на капчу")


async def _captcha_timer(chat_id, uid, cfg, timeout):
    try:
        await asyncio.sleep(timeout)
    except asyncio.CancelledError:
        return
    if (chat_id, uid) in _CAPTCHA:
        await captcha_fail(chat_id, uid, cfg, "не прошёл капчу вовремя")


async def captcha_pass(chat_id, user, cfg):
    st = _CAPTCHA.pop((chat_id, user.id), None)
    if st is None:
        return
    if st.get('task'):
        st['task'].cancel()
    try:
        await do_unmute(chat_id, user.id)
    except Exception:
        pass
    for key in ('form', 'text'):
        if st.get(key) is not None:
            try:
                await st[key].delete()
            except Exception:
                pass
    if cfg['welcome'].get('on'):
        await send_welcome(chat_id, user, cfg)


async def captcha_fail(chat_id, uid, cfg, why):
    st = _CAPTCHA.pop((chat_id, uid), None)
    if st is None:
        return
    if st.get('task') and st['task'] is not asyncio.current_task():
        st['task'].cancel()
    for key in ('form', 'text'):
        if st.get(key) is not None:
            try:
                await st[key].delete()
            except Exception:
                pass
    fail = cfg['captcha'].get('fail', 'kick')
    try:
        if fail == 'kick':
            await do_kick(chat_id, uid)
        elif fail == 'ban':
            await do_ban(chat_id, uid)
        elif st.get('form') is None:                   # текстовая капча шла без мута — мутим сейчас
            await do_mute(chat_id, uid, 0)
        await log_action(chat_id, uid, f"капча: {'кик' if fail == 'kick' else 'бан' if fail == 'ban' else 'остался в муте'}",
                         why, 'капча', undo='ban' if fail == 'ban' else ('mute' if fail == 'mute' else None))
    except Exception as e:
        engine.tlog(f"[bastion] капча: {e}")


# --------------------------------------------------------------------------- ночной режим

def _in_window(now_hm, a, b):
    return a <= now_hm < b if a < b else (now_hm >= a or now_hm < b)


async def _night_loop():
    await asyncio.sleep(20)
    while True:
        try:
            now_hm = dt.datetime.now().strftime('%H:%M')
            states = await db.get('night_state', {}) or {}
            changed = False
            for cid in await protected_chats():
                cfg = await chat_cfg(cid)
                n = cfg['night']
                closed = str(cid) in states
                want = bool(cfg.get('on') and n.get('on') and _in_window(now_hm, n['from'], n['to']))
                if want == closed:
                    continue
                try:
                    cur = await current_rights(cid)
                    if want:
                        states[str(cid)] = bool(cur.get('send_messages'))
                        cur['send_messages'] = True
                        await set_rights(cid, cur)
                        await temp_message(cid, f"🌙 Ночной режим: чат закрыт до {n['to']}. Спокойной ночи!", ttl=0)
                    else:
                        was = states.pop(str(cid))
                        cur['send_messages'] = bool(was)
                        await set_rights(cid, cur)
                        await temp_message(cid, "☀️ Доброе утро! Чат снова открыт.", ttl=0)
                    changed = True
                except Exception as e:
                    engine.tlog(f"[bastion] ночь {cid}: {e}")
            if changed:
                await db.set('night_state', states)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            engine.tlog(f"[bastion] ночь: {e!r}")
        await asyncio.sleep(30)


# --------------------------------------------------------------------------- панель

def _onoff(v):
    return "вкл" if v else "выкл"


async def panel_main(chat_id):
    cfg = await chat_cfg(chat_id)
    r = await my_rights(chat_id)
    nf = sum(1 for k, _ in FILTERS if cfg['filters'].get(k))
    lines = [visual.title("Bastion", sub=_esc(await chat_title(chat_id))),
             visual.field("защита", "включена" if cfg['on'] else "выключена", code=False),
             visual.field("ваши права", ("владелец" if r.get('creator') else "админ" if r.get('admin') else "нет — нужна админка")
                          + ("" if not r.get('admin') or r.get('ban') else " · без бана") + ("" if not r.get('admin') or r.get('delete')
                                                                                            else " · без удаления"), code=False),
             visual.field("варны", f"{cfg['warn_limit']} → {act_label(cfg['warn_action'])}", code=False),
             visual.field("фильтры", f"{nf} из {len(FILTERS)} · стоп-слов {len(cfg['words'])}", code=False),
             visual.field("антифлуд", f"{cfg['flood']['msgs']}/{cfg['flood']['secs']}с → {act_label(cfg['flood']['action'])}"
                          if cfg['flood']['on'] else "выкл", code=False),
             visual.field("капча", cfg['captcha']['mode'] if cfg['captcha']['on'] else "выкл", code=False),
             visual.field("приветствие", _onoff(cfg['welcome']['on']), code=False),
             visual.field("ночь", f"{cfg['night']['from']}–{cfg['night']['to']}" if cfg['night']['on'] else "выкл", code=False),
             visual.field("антирейд", f"{cfg['raid']['joins']}/{cfg['raid']['secs']}с" if cfg['raid']['on'] else "выкл", code=False)]
    if _RAID_UNTIL.get(chat_id, 0) > time.time():
        lines.append(visual.warn(f"идёт рейд-режим ещё {fmt_dur(_RAID_UNTIL[chat_id] - time.time())}"))
    return "\n".join(lines)


async def panel_main_buttons(chat_id):
    cfg = await chat_cfg(chat_id)
    on = cfg['on']
    return [
        [("● Защита включена" if on else "○ Защита выключена", "bs:on", 'success' if on else 'danger')],
        [("⚠ Варны", "bs:p:warn"), ("🧹 Фильтры", "bs:p:filt"), ("🚫 Стоп-слова", "bs:p:words")],
        [("🌊 Антифлуд", "bs:p:flood"), ("🤖 Капча", "bs:p:cap"), ("👋 Приветствие", "bs:p:greet")],
        [("🌙 Ночь", "bs:p:night"), ("🚨 Антирейд", "bs:p:raid"), ("🔒 Права чата", "bs:p:locks")],
        [("📣 Репорты: " + _onoff(cfg['report']), "bs:t:report"), ("🗒 Журнал: " + _onoff(cfg['log']), "bs:t:log")],
        [("🧽 Служебные: " + ("удалять" if cfg['clean_service'] else "оставить"), "bs:t:clean_service"),
         ("🔔 В чат: " + _onoff(cfg['notify']), "bs:t:notify")],
        [(visual.btn('Закрыть', 'close'), "bs:close")],
    ]


def _cyc(lst, cur, step=1):
    i = lst.index(cur) if cur in lst else 0
    return lst[(i + step) % len(lst)]


WARN_ACTS = ['mute:3600', 'mute:86400', 'mute:604800', 'mute', 'kick', 'ban:86400', 'ban:604800', 'ban']
FLOOD_ACTS = ['del', 'warn', 'mute:600', 'mute:3600', 'mute', 'kick', 'ban']


async def page(chat_id, name, st):
    cfg = await chat_cfg(chat_id)
    back = [(visual.btn('Назад', 'back'), "bs:back")]
    if name == 'warn':
        exp = cfg['warn_expire']
        text = "\n".join([visual.title("Варны"), visual.field("лимит", cfg['warn_limit']),
                          visual.field("на лимите", act_label(cfg['warn_action']), code=False),
                          visual.field("сгорают", f"через {exp} дн" if exp else "никогда", code=False), "",
                          visual.hint(".warn · .unwarn · .warns · .resetwarns")])
        rows = [[("−", "bs:wl:-1"), (f"Лимит: {cfg['warn_limit']}", "bs:nop"), ("+", "bs:wl:1")],
                [("На лимите: " + act_label(cfg['warn_action']), "bs:wa")],
                [("Сгорают: " + (f"{exp} дн" if exp else "никогда"), "bs:we")], back]
        return text, rows
    if name == 'filt':
        per = 9
        pages = -(-len(FILTERS) // per)
        st['fp'] = max(0, min(st.get('fp', 0), pages - 1))
        chunk = FILTERS[st['fp'] * per:(st['fp'] + 1) * per]
        text = "\n".join([visual.title("Фильтры", sub=f"стр. {st['fp'] + 1}/{pages}"),
                          visual.hint("нажатие — следующее действие: выкл → удалить → варн → мут 1ч → мут 1д → мут → кик → бан 1д → бан"),
                          visual.hint(f"новички — первые {cfg['newbie_hours']} ч после входа")])
        rows = [[(f"{'◉' if cfg['filters'].get(k) else '○'} {lbl}: {act_label(cfg['filters'].get(k))}", f"bs:f:{k}")]
                for k, lbl in chunk]
        nav = ([("◂", "bs:fp:-1")] if st['fp'] > 0 else []) + ([("▸", "bs:fp:1")] if st['fp'] < pages - 1 else [])
        if nav:
            rows.append(nav)
        rows.append([("Все выкл", "bs:f!off"), ("Анти-спам набор", "bs:f!spam")])
        rows.append(back)
        return text, rows
    if name == 'words':
        lines = [visual.title("Стоп-слова", sub=str(len(cfg['words'])))]
        rows = []
        for i, w in enumerate(cfg['words'][:20]):
            lines.append(f"{i + 1}. {'<i>re</i> ' if w['re'] else ''}<code>{_esc(w['p'][:40])}</code> — {_esc(act_label(w['a']))}")
            rows.append([(f"✕ {w['p'][:20]}", f"bs:wd:{i}"), ("→ " + act_label(w['a']), f"bs:wx:{i}")])
        if not cfg['words']:
            lines.append(visual.hint("пусто — добавьте кнопкой или .bword add слово, фраза [действие]"))
        rows.append([engine.InputButton("＋ Слова через запятую", "words_add", hint="казино, ставки, крипта")])
        rows.append([engine.InputButton("＋ Регулярка", "words_re", hint="(?i)крипт[ао]")])
        rows.append(back)
        return "\n".join(lines), rows
    if name == 'flood':
        f = cfg['flood']
        text = "\n".join([visual.title("Антифлуд", sub=_onoff(f['on'])),
                          visual.field("порог", f"{f['msgs']} сообщений за {f['secs']} с", code=False),
                          visual.field("действие", act_label(f['action']), code=False)])
        rows = [[B("● Включён" if f['on'] else "○ Выключен", "bs:fl:on", 'success' if f['on'] else None)],
                [("−", "bs:fl:m:-1"), (f"Сообщений: {f['msgs']}", "bs:nop"), ("+", "bs:fl:m:1")],
                [("−", "bs:fl:s:-1"), (f"Секунд: {f['secs']}", "bs:nop"), ("+", "bs:fl:s:1")],
                [("Действие: " + act_label(f['action']), "bs:fl:a")], back]
        return text, rows
    if name == 'locks':
        try:
            cur = await current_rights(chat_id)
        except Exception as e:
            return visual.err(f"Права не прочитать: {_esc(e)}"), [back]
        return locks_text(cur), locks_buttons(cur, prefix='bs:lk:', last=back[0])
    if name == 'cap':
        c = cfg['captcha']
        text = "\n".join([visual.title("Капча", sub=_onoff(c['on'])),
                          visual.field("вид", {'button': 'кнопка', 'math': 'пример', 'emoji': 'найти эмодзи'}[c['mode']], code=False),
                          visual.field("время", fmt_dur(c['timeout']), code=False),
                          visual.field("не прошёл", {'kick': 'кик', 'ban': 'бан', 'mute': 'остаётся в муте'}[c['fail']], code=False),
                          "", visual.hint("новичок сразу в муте; прошёл — мут снимается и приходит приветствие")])
        rows = [[("● Включена" if c['on'] else "○ Выключена", "bs:cp:on")],
                [("Вид: " + {'button': 'кнопка', 'math': 'пример', 'emoji': 'эмодзи'}[c['mode']], "bs:cp:mode")],
                [("−30с", "bs:cp:t:-30"), (f"Время: {fmt_dur(c['timeout'])}", "bs:nop"), ("+30с", "bs:cp:t:30")],
                [("Не прошёл: " + {'kick': 'кик', 'ban': 'бан', 'mute': 'мут'}[c['fail']], "bs:cp:fail")], back]
        return text, rows
    if name == 'greet':
        w, g = cfg['welcome'], cfg['goodbye']
        me = await engine.client.get_me()
        text = "\n".join([visual.title("Приветствие и прощание"),
                          visual.section("Приветствие · " + _onoff(w['on'])), await render_greet(w['text'], chat_id, me),
                          visual.field("удалять через", fmt_dur(w['del']) if w['del'] else "не удалять", code=False),
                          visual.field("только последнее", "да" if w.get('clean') else "нет", code=False),
                          visual.section("Прощание · " + _onoff(g['on'])), await render_greet(g['text'], chat_id, me), "",
                          visual.hint("{name} {mention} {first} {username} {id} {chat} {count}")])
        rows = [[("Приветствие: " + _onoff(w['on']), "bs:g:w"), ("Прощание: " + _onoff(g['on']), "bs:g:g")],
                [engine.InputButton("✎ Текст приветствия", "greet_w", hint="Привет, {mention}! Добро пожаловать в {chat}")],
                [engine.InputButton("✎ Текст прощания", "greet_g", hint="{name} ушёл")],
                [("Удалять: " + (fmt_dur(w['del']) if w['del'] else "нет"), "bs:g:del"),
                 ("Только последнее: " + ("да" if w.get('clean') else "нет"), "bs:g:clean")], back]
        return text, rows
    if name == 'night':
        n = cfg['night']
        text = "\n".join([visual.title("Ночной режим", sub=_onoff(n['on'])),
                          visual.field("с", n['from']), visual.field("до", n['to']), "",
                          visual.hint("в это время писать могут только админы; время — часы компьютера с FRX")])
        rows = [[("● Включён" if n['on'] else "○ Выключен", "bs:n:on")],
                [("◂", "bs:n:f:-1"), (f"С {n['from']}", "bs:nop"), ("▸", "bs:n:f:1")],
                [("◂", "bs:n:t:-1"), (f"До {n['to']}", "bs:nop"), ("▸", "bs:n:t:1")], back]
        return text, rows
    if name == 'raid':
        r = cfg['raid']
        text = "\n".join([visual.title("Антирейд", sub=_onoff(r['on'])),
                          visual.field("порог", f"{r['joins']} входов за {r['secs']} с", code=False),
                          visual.field("режим длится", f"{r['minutes']} мин", code=False), "",
                          visual.hint("в режиме рейда все новички сразу в мут, вам — тревога с кнопкой «закрыть чат»")])
        rows = [[("● Включён" if r['on'] else "○ Выключен", "bs:r:on")],
                [("−", "bs:r:j:-1"), (f"Входов: {r['joins']}", "bs:nop"), ("+", "bs:r:j:1")],
                [("−", "bs:r:s:-10"), (f"Секунд: {r['secs']}", "bs:nop"), ("+", "bs:r:s:10")],
                [("−", "bs:r:m:-10"), (f"Минут: {r['minutes']}", "bs:nop"), ("+", "bs:r:m:10")], back]
        return text, rows
    return await panel_main(chat_id), await panel_main_buttons(chat_id)


SPAM_SET = {'invites': 'ban', 'sendas': 'ban', 'mentions': 'mute:86400', 'zalgo': 'del', 'arabic': 'del', 'emoji': 'del',
            'newbie': 'del', 'inline': 'del'}


@decorators.command('bastion', 'bs', 'бастион')
async def bastion_cmd(event):
    """панель защиты чата; on|off — включить защиту; list — все защищённые чаты"""
    a = engine.get_args().strip().lower()
    if a in ('list', 'список') or not event.is_group:
        ids = await protected_chats()
        lines = [visual.title("Bastion", sub=f"под защитой: {len(ids)}")]
        for cid in ids:
            lines.append(visual.item(f"{_esc(await chat_title(cid))} <code>{cid}</code>"))
        if not ids:
            lines.append(visual.hint("в группе напишите .bastion on"))
        await engine.answer(event, "\n".join(lines))
        return
    chat_id = event.chat_id
    cfg = await chat_cfg(chat_id)
    if a in ('on', 'off', 'вкл', 'выкл'):
        cfg['on'] = a in ('on', 'вкл')
        await save_cfg(chat_id)
        r = await my_rights(chat_id)
        await engine.answer(event, visual.ok("Bastion охраняет чат" if cfg['on'] else "Защита чата выключена")
                            + ("" if r.get('admin') or not cfg['on'] else "\n" + visual.warn("у вас нет админки — фильтровать не получится"))
                            + ("\n" + visual.hint(".bs — панель настроек") if cfg['on'] else ""))
        return
    st = {'page': 'main'}
    try:
        form = await engine.create_inlineform(chat_id, await panel_main(chat_id), buttons=await panel_main_buttons(chat_id),
                                              timeout=900)
    except Exception:
        await engine.answer(event, await panel_main(chat_id) + "\n\n" + visual.hint("без бота — настраивайте командами: .help bastion"))
        return
    await event.delete()

    async def redraw():
        text, rows = await page(chat_id, st['page'], st)
        await form.edit(text, buttons=rows)

    @form.on_click(data_startswith='bs:')
    async def _(click):
        d = click.data[3:]
        c = await chat_cfg(chat_id)
        if d == 'close':
            await form.delete()
            return
        if d == 'nop':
            await click.answer()
            return
        if d == 'back':
            st['page'] = 'main'
        elif d.startswith('p:'):
            st['page'] = d[2:]
        elif d.startswith('lk:'):
            try:
                await toggle_lock(chat_id, d[3:])
            except Exception as e:
                await click.answer(f"Не вышло: {e}"[:190], show_alert=True)
                return
        elif d == 'on':
            c['on'] = not c['on']
        elif d.startswith('t:'):
            c[d[2:]] = not c.get(d[2:])
        elif d.startswith('wl:'):
            c['warn_limit'] = max(1, min(20, c['warn_limit'] + int(d[3:])))
        elif d == 'wa':
            c['warn_action'] = _cyc(WARN_ACTS, c['warn_action'])
        elif d == 'we':
            c['warn_expire'] = _cyc([0, 1, 3, 7, 14, 30, 90], c['warn_expire'])
        elif d.startswith('fp:'):
            st['fp'] = st.get('fp', 0) + int(d[3:])
        elif d.startswith('f:'):
            k = d[2:]
            nxt = _cyc(ACT_CYCLE, c['filters'].get(k))
            if nxt:
                c['filters'][k] = nxt
            else:
                c['filters'].pop(k, None)
        elif d == 'f!off':
            c['filters'].clear()
        elif d == 'f!spam':
            c['filters'].update(SPAM_SET)
            await click.answer("Включено: инвайты и «от каналов» — бан, масс-упоминания — мут, залго/вязь/эмодзи-спам и "
                               "медиа новичков — удаление", show_alert=True)
        elif d.startswith('wd:'):
            i = int(d[3:])
            if i < len(c['words']):
                c['words'].pop(i)
        elif d.startswith('wx:'):
            i = int(d[3:])
            if i < len(c['words']):
                c['words'][i]['a'] = _cyc(ACT_CYCLE[1:], c['words'][i]['a'])
        elif d.startswith('fl:'):
            f = c['flood']
            p = d[3:]
            if p == 'on':
                f['on'] = not f['on']
            elif p == 'a':
                f['action'] = _cyc(FLOOD_ACTS, f['action'])
            elif p.startswith('m:'):
                f['msgs'] = max(2, min(50, f['msgs'] + int(p[2:])))
            elif p.startswith('s:'):
                f['secs'] = max(1, min(120, f['secs'] + int(p[2:])))
        elif d.startswith('cp:'):
            cp = c['captcha']
            p = d[3:]
            if p == 'on':
                cp['on'] = not cp['on']
            elif p == 'mode':
                cp['mode'] = _cyc(['emoji', 'math', 'button'], cp['mode'])
            elif p == 'fail':
                cp['fail'] = _cyc(['kick', 'ban', 'mute'], cp['fail'])
            elif p.startswith('t:'):
                cp['timeout'] = max(30, min(3600, cp['timeout'] + int(p[2:])))
        elif d.startswith('g:'):
            p = d[2:]
            if p == 'w':
                c['welcome']['on'] = not c['welcome']['on']
            elif p == 'g':
                c['goodbye']['on'] = not c['goodbye']['on']
            elif p == 'del':
                c['welcome']['del'] = _cyc([0, 30, 60, 300, 900, 3600], c['welcome']['del'])
            elif p == 'clean':
                c['welcome']['clean'] = not c['welcome'].get('clean')
        elif d.startswith('n:'):
            n = c['night']
            p = d[2:]
            hours = [f"{h:02}:{m:02}" for h in range(24) for m in (0, 30)]
            if p == 'on':
                n['on'] = not n['on']
            elif p.startswith('f:'):
                n['from'] = _cyc(hours, n['from'], int(p[2:]))
            elif p.startswith('t:'):
                n['to'] = _cyc(hours, n['to'], int(p[2:]))
        elif d.startswith('r:'):
            r = c['raid']
            p = d[2:]
            if p == 'on':
                r['on'] = not r['on']
            elif p.startswith('j:'):
                r['joins'] = max(3, min(200, r['joins'] + int(p[2:])))
            elif p.startswith('s:'):
                r['secs'] = max(10, min(3600, r['secs'] + int(p[2:])))
            elif p.startswith('m:'):
                r['minutes'] = max(5, min(1440, r['minutes'] + int(p[2:])))
        await save_cfg(chat_id)
        await redraw()

    @form.on_input(data='words_add')
    async def _(inp):
        lst, action = _split_word_action((inp.value or '').strip())
        c = await chat_cfg(chat_id)
        for w in lst:
            c['words'].append({'p': w.lower(), 're': False, 'a': action})
        await save_cfg(chat_id)
        await redraw()

    @form.on_input(data='words_re')
    async def _(inp):
        v = (inp.value or '').strip()
        try:
            re.compile(v)
        except re.error:
            await redraw()
            return
        c = await chat_cfg(chat_id)
        if v:
            c['words'].append({'p': v, 're': True, 'a': 'del'})
            await save_cfg(chat_id)
        await redraw()

    for key, part in (('greet_w', 'welcome'), ('greet_g', 'goodbye')):
        def make(key, part):
            @form.on_input(data=key)
            async def _(inp):
                v = (inp.value or '').strip()
                c = await chat_cfg(chat_id)
                if v:
                    c[part].update(on=True, text=v[:3000])
                    await save_cfg(chat_id)
                await redraw()
        make(key, part)


@decorators.on_error()
async def _errors(error_text, context):
    if context.get('module') == MODULE_NAME:
        engine.botlog(error_text, level='error', function=context.get('function'))
