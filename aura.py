"""
Aura — живой профиль: имя, фамилия, био, аватарка и эмодзи-статус меняются сами по шаблонам.

  .aura                панель: что включено, шаблоны, предпросмотр, «применить сейчас»
  .aura on / off       включить / выключить (при выключении профиль возвращается как был)
  .aura vars           все переменные для шаблонов
  .aura base           ответом на фото — основа для аватарки-часов
  .aura addava         ответом на фото — добавить в карусель аватарок (.aura avas clear — очистить)
  .aura status         ответом на сообщение с премиум-эмодзи — они будут сменяться в статусе

Сейчас играет — Spotify, Яндекс Музыка и Last.fm (через него — почти любой плеер):
  .aura np             что играет сейчас: карточка с обложкой и прогрессом
  .aura spotify        подключить Spotify (пошагово: ключи приложения → ссылка → адрес из браузера)
  .aura yandex <токен> подключить Яндекс Музыку (OAuth-токен);   .aura lastfm <ник> [api_key] — Last.fm
  .aura np off <spotify|yandex|lastfm> — отключить источник
  Переменные: {np} «Исполнитель — Трек», {np_title}, {np_artist}, {np_time} 1:23/3:45, {np_bar} ▰▰▱▱, {np_source};
  {listening} и {doing} их тоже видят.

Шаблоны — обычный текст с переменными: «{clock} {time}», «{doing}», «до НГ {days:2027-01-01} дн.»
{doing} — что вы делаете: «слушает «трек»» (Spotify, Яндекс, Last.fm), «читает «чат»», «пишет в «чат»» — или «idle_text».
Какие чаты можно показывать в «читает» — настройка reading_sources (по умолчанию только публичные; личные — никогда,
пока вы сами их не включите). Частота смены — в настройках: слишком часто — Telegram может временно запретить
менять профиль (FloodWait), поэтому ниже рекомендуемого не ставьте.
"""

import asyncio
import base64
import datetime as dt
import io
import random
import re
import time
from pathlib import Path

import aiohttp

import db
import decorators
import engine
import files
import visual

NAME = "Aura"
MODULE_NAME = "aura"
VERSION = "1.2"
AUTHOR = "FRX"
DESCRIPTION = "Живой профиль: имя, био, аватарка-часы и эмодзи-статус по шаблонам, «читает/слушает …» (Spotify, Яндекс, Last.fm)"

SOURCES = ['публичные группы', 'непубличные группы', 'публичные каналы', 'непубличные каналы', 'боты', 'личные чаты']
AVATAR_MODES = ['off', 'clock', 'carousel']
CLOCKS = '🕛🕐🕑🕒🕓🕔🕕🕖🕗🕘🕙🕚'
HALF = '🕧🕜🕝🕞🕟🕠🕡🕢🕣🕤🕥🕦'
MOONS = '🌑🌒🌓🌔🌕🌖🌗🌘'
WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
WD_FULL = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']

VARS = [
    ('{time}', 'время, 14:05'), ('{date}', 'дата, 12.10'), ('{weekday}', 'день недели, пн'), ('{day}', 'понедельник'),
    ('{month}', '12 октября'), ('{year}', 'год'), ('{clock}', 'часы-эмодзи под время 🕑'), ('{moon}', 'фаза луны 🌔'),
    ('{doing}', 'слушает «…» / читает «…» / пишет в «…» — или idle_text'), ('{reading}', 'название чата, который вы читаете'),
    ('{listening}', 'что играет: Spotify, Яндекс Музыка, Last.fm'), ('{np}', 'Исполнитель — Трек'),
    ('{np_title} {np_artist}', 'трек и исполнитель'), ('{np_time}', '1:23/3:45'), ('{np_bar}', 'полоска ▰▰▱▱'),
    ('{np_source}', 'откуда играет'), ('{days:2027-01-01}', 'дней до даты'), ('{year_progress}', 'сколько прошло года, %'),
    ('{random:а|б|в}', 'случайный вариант'), ('{first} {last} {bio}', 'ваши исходные имя, фамилия, био'),
]

STATE = {'task': None, 'handler': None, 'client': None, 'last': {}, 'values': {}, 'block': {}, 'errors': {},
         'activity': None, 'premium': None, 'started': time.time(), 'np': None, 'np_at': 0, 'np_err': {}}


# --------------------------------------------------------------------------- настройки

@decorators.on_start()
async def setup():
    c = engine.create_config
    await c(key='enabled', description='Aura включена', data_type='bool', default=False)
    await c(key='name_on', description='Менять имя', data_type='bool', default=False)
    await c(key='last_on', description='Менять фамилию', data_type='bool', default=True)
    await c(key='bio_on', description='Менять био', data_type='bool', default=True)
    await c(key='status_on', description='Сменять эмодзи-статус (нужен Telegram Premium)', data_type='bool', default=False)
    await c(key='avatar_mode', description='Аватарка: off — не трогать, clock — фото с часами, carousel — по кругу из ваших фото',
            data_type='choice', default='off', options=AVATAR_MODES)
    await c(key='name_template', description='Шаблон имени', data_type='str', default='{first}', valid={'max_len': 200})
    await c(key='last_template', description='Шаблон фамилии', data_type='str', default='{clock} {time}', valid={'max_len': 200})
    await c(key='bio_template', description='Шаблон био', data_type='str', default='{doing}', valid={'max_len': 400})
    await c(key='idle_text', description='Что писать в {doing}, когда вы ничего не читаете и не слушаете',
            data_type='str', default='{moon} на связи · {weekday} {time}', valid={'max_len': 200})
    await c(key='reading_sources', description='Какие чаты можно показывать в «читает/пишет в» (личные — только если сами включите)',
            data_type='choices', default=['публичные группы', 'публичные каналы'], options=SOURCES)
    await c(key='reading_ttl', description='Сколько минут «читает …» держится после последнего действия', data_type='int',
            default=10, valid={'min_value': 1, 'max_value': 240})
    await c(key='reading_ignore', description='Чаты, которые никогда не показывать (id или @username)', data_type='list', default=[])
    await c(key='name_interval', description='Как часто можно менять имя/фамилию, сек. ⚠ Рекомендуется не меньше 60: '
            'чаще — Telegram может на время запретить менять профиль', data_type='int', default=60,
            valid={'min_value': 45, 'max_value': 86400})
    await c(key='bio_interval', description='Как часто можно менять био, сек. ⚠ Рекомендуется не меньше 60', data_type='int',
            default=60, valid={'min_value': 45, 'max_value': 86400})
    await c(key='avatar_interval', description='Как часто менять аватарку, мин. ⚠ Рекомендуется не меньше 15: каждая смена — '
            'новое фото в профиле и уведомление контактам в некоторых клиентах', data_type='int', default=15,
            valid={'min_value': 10, 'max_value': 10080})
    await c(key='status_interval', description='Как часто менять эмодзи-статус, мин. ⚠ Рекомендуется не меньше 5', data_type='int',
            default=5, valid={'min_value': 3, 'max_value': 10080})
    await c(key='status_ids', description='Премиум-эмодзи для статуса (id документов) — проще задать через .aura status',
            data_type='list', default=[])
    await c(key='np_interval', description='Как часто спрашивать Spotify / Яндекс / Last.fm, что играет, сек.', data_type='int',
            default=20, valid={'min_value': 10, 'max_value': 600})
    await c(key='np_paused', description='Показывать трек на паузе (иначе — как будто ничего не играет)', data_type='bool',
            default=False)
    hook()
    STATE['task'] = asyncio.ensure_future(loop())


@decorators.on_unload()
async def cleanup():
    if STATE['task']:
        STATE['task'].cancel()
    unhook()


@decorators.on_account_switch()
async def switched(*_a):
    unhook()
    hook()
    STATE['activity'] = None
    STATE['premium'] = None


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


async def setc(key, value):
    await engine.edit_config(key, 'edit', value, module_name=MODULE_NAME)


def _k(key):
    # исходный профиль, фото Aura и счётчики — свои у каждого аккаунта (FRX умеет переключать аккаунты)
    return f"{MODULE_NAME}:{key}:{getattr(engine, '_owner_id', None) or 0}"


async def dget(key, default=None):
    return await db.gget(_k(key), default)


async def dset(key, value):
    return await db.gset(_k(key), value)


def data_dir():
    try:
        from core.paths import DATA_DIR
        d = Path(DATA_DIR) / 'aura'
    except Exception:
        d = Path('data') / 'aura'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _esc(x):
    return str(x).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


# --------------------------------------------------------------------------- «читает / пишет в»

def hook():
    from telethon import events
    from telethon.tl.types import UpdateReadChannelInbox, UpdateReadHistoryInbox
    client = engine.client
    if client is None or STATE['handler'] is not None:
        return

    async def on_read(update):
        peer = getattr(update, 'peer', None)
        if isinstance(update, UpdateReadChannelInbox):
            from telethon.tl.types import PeerChannel
            peer = PeerChannel(update.channel_id)
        if peer is not None:
            await note(peer, 'reading')

    async def on_out(event):
        await note(event.chat_id, 'writing')

    client.add_event_handler(on_read, events.Raw(types=[UpdateReadHistoryInbox, UpdateReadChannelInbox]))
    client.add_event_handler(on_out, events.NewMessage(outgoing=True))
    STATE['handler'], STATE['client'] = (on_read, on_out), client


def unhook():
    h, client = STATE['handler'], STATE['client']
    if h and client:
        for fn in h:
            try:
                client.remove_event_handler(fn)
            except Exception:
                pass
    STATE['handler'] = STATE['client'] = None


def kind_of(ent):
    from telethon.tl.types import Channel, Chat, User
    if isinstance(ent, User):
        return 'боты' if ent.bot else 'личные чаты'
    if isinstance(ent, Chat):
        return 'непубличные группы'
    if isinstance(ent, Channel):
        public = bool(ent.username or getattr(ent, 'usernames', None))
        if ent.broadcast:
            return 'публичные каналы' if public else 'непубличные каналы'
        return 'публичные группы' if public else 'непубличные группы'
    return None


async def note(peer, what):
    try:
        if not await cfg('enabled'):
            return
        ent = await engine.client.get_entity(peer)
        if getattr(ent, 'is_self', False):
            return
        if kind_of(ent) not in (await cfg('reading_sources', []) or []):
            return
        ignore = {str(x).lower().lstrip('@') for x in (await cfg('reading_ignore', []) or [])}
        from telethon.utils import get_peer_id
        if str(get_peer_id(ent)) in ignore or str(ent.id) in ignore or (getattr(ent, 'username', '') or '').lower() in ignore:
            return
        title = getattr(ent, 'title', None) or ' '.join(x for x in (getattr(ent, 'first_name', '') or '',
                                                                      getattr(ent, 'last_name', '') or '') if x)
        STATE['activity'] = {'what': what, 'title': title or '…', 'at': time.time()}
    except Exception:
        pass


def listening():
    np = STATE.get('np')
    return f"{np['artist']} — {np['title']}" if np else ''


# --------------------------------------------------------------------------- сейчас играет: Spotify / Яндекс / Last.fm

SP_REDIRECT = 'http://127.0.0.1:8888/callback'
SP_SCOPE = 'user-read-currently-playing user-read-playback-state'


async def npget(key, default=None):
    return await db.gget(f"{MODULE_NAME}:np:{key}", default)


async def npset(key, value):
    return await db.gset(f"{MODULE_NAME}:np:{key}", value)


async def _http_json(method, url, **kw):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.request(method, url, **kw) as r:
            if r.status == 204:
                return 204, None
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = None
            return r.status, data


async def sp_token():
    sp = await npget('spotify') or {}
    if not sp.get('refresh'):
        return None
    if sp.get('access') and sp.get('exp', 0) > time.time() + 30:
        return sp['access']
    auth = base64.b64encode(f"{sp['id']}:{sp['secret']}".encode()).decode()
    st, data = await _http_json('POST', 'https://accounts.spotify.com/api/token',
                                data={'grant_type': 'refresh_token', 'refresh_token': sp['refresh']},
                                headers={'Authorization': f'Basic {auth}'})
    if st != 200 or not data or 'access_token' not in data:
        raise RuntimeError(f"Spotify: не обновился токен ({st}) — подключите заново: .aura spotify")
    sp['access'], sp['exp'] = data['access_token'], time.time() + int(data.get('expires_in', 3600))
    if data.get('refresh_token'):
        sp['refresh'] = data['refresh_token']
    await npset('spotify', sp)
    return sp['access']


async def np_spotify():
    tok = await sp_token()
    if not tok:
        return None
    st, d = await _http_json('GET', 'https://api.spotify.com/v1/me/player/currently-playing?additional_types=episode',
                             headers={'Authorization': f'Bearer {tok}'})
    if st == 204 or not d or not d.get('item'):
        return None
    if st != 200:
        raise RuntimeError(f"Spotify: ответ {st}")
    it = d['item']
    artists = ', '.join(a['name'] for a in it.get('artists', [])) or (it.get('show') or {}).get('name', '')
    imgs = (it.get('album') or {}).get('images') or it.get('images') or []
    return {'title': it.get('name', ''), 'artist': artists, 'progress': (d.get('progress_ms') or 0) / 1000,
            'duration': (it.get('duration_ms') or 0) / 1000, 'paused': not d.get('is_playing'),
            'cover': imgs[0]['url'] if imgs else None, 'link': (it.get('external_urls') or {}).get('spotify'),
            'source': 'Spotify'}


async def np_yandex():
    tok = await npget('yandex')
    if not tok:
        return None
    import json
    import uuid
    dev = await npget('ya_device') or uuid.uuid4().hex[:16]
    await npset('ya_device', dev)
    proto = {'Ynison-Device-Id': dev, 'Ynison-Device-Info': json.dumps({'app_name': 'Chrome', 'type': 1})}
    hdr = lambda p: {'Sec-WebSocket-Protocol': f"Bearer, v2, {json.dumps(p)}", 'Origin': 'http://music.yandex.ru',  # noqa: E731
                     'Authorization': f'OAuth {tok}'}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.ws_connect('wss://ynison.music.yandex.ru/redirector.YnisonRedirectService/GetRedirectToYnison',
                                headers=hdr(proto)) as ws:
            redirect = json.loads((await ws.receive()).data)
        proto['Ynison-Redirect-Ticket'] = redirect['redirect_ticket']
        ver = {'device_id': dev, 'version': 0, 'timestamp_ms': 0}
        payload = {'update_full_state': {
            'player_state': {'player_queue': {'current_playable_index': -1, 'entity_id': '', 'entity_type': 'VARIOUS',
                                              'playable_list': [], 'options': {'repeat_mode': 'NONE'},
                                              'entity_context': 'BASED_ON_ENTITY_BY_DEFAULT', 'version': ver,
                                              'from_optional': ''},
                             'status': {'duration_ms': 0, 'paused': True, 'playback_speed': 1, 'progress_ms': 0, 'version': ver}},
            'device': {'capabilities': {'can_be_player': False, 'can_be_remote_controller': False, 'volume_granularity': 16},
                       'info': {'device_id': dev, 'type': 'WEB', 'title': 'FRX Aura', 'app_name': 'Chrome'},
                       'volume_info': {'volume': 0}, 'is_shadow': True},
            'is_currently_active': False},
            'rid': str(uuid.uuid4()), 'player_action_timestamp_ms': 0, 'activity_interception_type': 'DO_NOT_INTERCEPT_BY_DEFAULT'}
        async with s.ws_connect(f"wss://{redirect['host']}/ynison_state.YnisonStateService/PutYnisonState",
                                headers=hdr(proto)) as ws:
            await ws.send_str(json.dumps(payload))
            state = json.loads((await ws.receive()).data)
        ps = state.get('player_state') or {}
        q = ps.get('player_queue') or {}
        lst = q.get('playable_list') or []
        idx = q.get('current_playable_index', -1)
        if not lst or not (0 <= idx < len(lst)) or lst[idx].get('playable_type') == 'LOCAL_TRACK':
            return None
        status = ps.get('status') or {}
        tid = lst[idx].get('playable_id')
        async with s.get(f'https://api.music.yandex.net/tracks/{tid}', headers={'Authorization': f'OAuth {tok}'}) as r:
            tr = ((await r.json(content_type=None)) or {}).get('result') or [{}]
    t = tr[0] if tr else {}
    cover = t.get('coverUri')
    album = (t.get('albums') or [{}])[0].get('id')
    return {'title': t.get('title') or lst[idx].get('title') or '?', 'artist': ', '.join(a.get('name', '') for a in t.get('artists', [])),
            'progress': int(status.get('progress_ms') or 0) / 1000, 'duration': int(status.get('duration_ms') or t.get('durationMs') or 0) / 1000,
            'paused': bool(status.get('paused')), 'cover': f"https://{cover.replace('%%', '400x400')}" if cover else None,
            'link': f"https://music.yandex.ru/album/{album}/track/{tid}" if album else None, 'source': 'Яндекс Музыка'}


async def np_lastfm():
    lf = await npget('lastfm') or {}
    if not lf.get('user') or not lf.get('key'):
        return None
    st, d = await _http_json('GET', 'https://ws.audioscrobbler.com/2.0/', params={
        'method': 'user.getrecenttracks', 'user': lf['user'], 'api_key': lf['key'], 'format': 'json', 'limit': 1})
    tracks = ((d or {}).get('recenttracks') or {}).get('track') or []
    if not tracks:
        return None
    t = tracks[0]
    if (t.get('@attr') or {}).get('nowplaying') != 'true':
        return None
    imgs = [i.get('#text') for i in t.get('image', []) if i.get('#text')]
    return {'title': t.get('name', ''), 'artist': (t.get('artist') or {}).get('#text', ''), 'progress': 0, 'duration': 0,
            'paused': False, 'cover': imgs[-1] if imgs else None, 'link': t.get('url'), 'source': 'Last.fm'}


NP_SOURCES = [('spotify', np_spotify), ('yandex', np_yandex), ('lastfm', np_lastfm)]


async def np_refresh(force=False):
    """Спросить источники по очереди; первый, где что-то играет, — в STATE['np']."""
    if not force and time.time() - STATE['np_at'] < int(await cfg('np_interval', 20)):
        return STATE['np']
    STATE['np_at'] = time.time()
    STATE['np_conn'] = {'spotify': bool((await npget('spotify') or {}).get('refresh')), 'yandex': bool(await npget('yandex')),
                        'lastfm': bool((await npget('lastfm') or {}).get('key'))}
    show_paused = await cfg('np_paused', False)
    found = None
    for name, fn in NP_SOURCES:
        try:
            r = await fn()
            STATE['np_err'].pop(name, None)
        except Exception as e:
            STATE['np_err'][name] = str(e)[:150]
            r = None
        if r and (show_paused or not r.get('paused')):
            r['at'] = time.time()
            found = r
            break
    STATE['np'] = found
    return found


def np_now():
    """Текущий трек с прогрессом «на сейчас» (между опросами прогресс досчитывается сам)."""
    np = STATE.get('np')
    if not np:
        return None
    np = dict(np)
    if not np['paused'] and np['duration']:
        np['progress'] = min(np['duration'], np['progress'] + time.time() - np['at'])
    return np


async def np_command(event, sub, rest):
    """.aura np / spotify / yandex / lastfm"""
    from urllib.parse import parse_qs, urlencode, urlparse
    sub = {'nowplaying': 'np', 'ya': 'yandex', 'lfm': 'lastfm'}.get(sub, sub)
    if sub == 'np' and len(rest) >= 2 and rest[0] in ('off', 'выкл'):
        src = {'ya': 'yandex', 'lfm': 'lastfm'}.get(rest[1], rest[1])
        if src not in ('spotify', 'yandex', 'lastfm'):
            await engine.answer(event, visual.err("Источник: spotify · yandex · lastfm"))
            return
        await npset(src, None)
        await np_refresh(force=True)
        await engine.answer(event, visual.ok(f"{src} отключён"))
        return
    if sub == 'spotify':
        if rest and rest[0] in ('off', 'выкл'):
            await npset('spotify', None)
            await engine.answer(event, visual.ok("Spotify отключён"))
            return
        if len(rest) >= 2:
            await npset('spotify_pending', {'id': rest[0], 'secret': rest[1]})
            url = 'https://accounts.spotify.com/authorize?' + urlencode({
                'client_id': rest[0], 'response_type': 'code', 'redirect_uri': SP_REDIRECT, 'scope': SP_SCOPE})
            try:
                await event.delete()                     # в команде был secret — не оставляем его в чате
            except Exception:
                pass
            await engine.send_message('me', "\n".join([
                visual.title("Aura", sub="Spotify · шаг 2"),
                f'1. Откройте <a href="{_esc(url)}">ссылку входа Spotify</a> и разрешите доступ.',
                "2. Браузер откроет страницу, которая не загрузится, — это нормально.",
                "3. Скопируйте адрес из адресной строки (начинается с http://127.0.0.1:8888/callback?code=…) и отправьте:",
                "<code>.aura spotify &lt;адрес&gt;</code>"]))
            return
        if len(rest) == 1 and ('code=' in rest[0] or len(rest[0]) > 60):
            pend = await npget('spotify_pending') or {}
            if not pend:
                await engine.answer(event, visual.err("Сначала: .aura spotify <client_id> <client_secret>"))
                return
            code = parse_qs(urlparse(rest[0]).query).get('code', [rest[0]])[0]
            auth = base64.b64encode(f"{pend['id']}:{pend['secret']}".encode()).decode()
            st, d = await _http_json('POST', 'https://accounts.spotify.com/api/token', headers={'Authorization': f'Basic {auth}'},
                                     data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': SP_REDIRECT})
            if st != 200 or not d or not d.get('refresh_token'):
                await engine.answer(event, visual.err(f"Spotify не принял код ({st}): {_esc((d or {}).get('error_description') or d)}")
                                    + "\n" + visual.hint("код одноразовый — получите новый по ссылке"))
                return
            await npset('spotify', {'id': pend['id'], 'secret': pend['secret'], 'refresh': d['refresh_token'],
                                    'access': d['access_token'], 'exp': time.time() + int(d.get('expires_in', 3600))})
            await npset('spotify_pending', None)
            await event.delete()
            await engine.send_message(event.chat_id, visual.ok("Spotify подключён") + "\n" + visual.hint(".aura np — что играет"))
            return
        await engine.answer(event, "\n".join([
            visual.title("Aura", sub="Spotify · шаг 1"),
            '1. Зайдите на <a href="https://developer.spotify.com/dashboard">developer.spotify.com/dashboard</a> → Create app.',
            f"2. Redirect URI — ровно <code>{SP_REDIRECT}</code>, API — Web API. Сохраните.",
            "3. В настройках приложения возьмите Client ID и Client Secret и отправьте:",
            "<code>.aura spotify &lt;client_id&gt; &lt;client_secret&gt;</code>",
            visual.hint("команда с ключами сразу удалится, ссылка придёт в «Избранное»")]))
        return
    if sub == 'yandex':
        if not rest:
            await engine.answer(event, visual.title("Aura", sub="Яндекс Музыка") + "\n"
                                + "Отправьте OAuth-токен Яндекс Музыки: <code>.aura yandex &lt;токен&gt;</code>\n"
                                + visual.hint("команда с токеном сразу удалится · .aura yandex off — отключить"))
            return
        try:
            await event.delete()
        except Exception:
            pass
        if rest[0] in ('off', 'выкл'):
            await npset('yandex', None)
            await engine.send_message(event.chat_id, visual.ok("Яндекс Музыка отключена"))
            return
        await npset('yandex', rest[0])
        try:
            np = await np_yandex()
            note = f"сейчас: {_esc(np['artist'])} — {_esc(np['title'])}" if np else "подключено, сейчас ничего не играет"
            await engine.send_message(event.chat_id, visual.ok("Яндекс Музыка подключена") + "\n" + visual.hint(note))
        except Exception as e:
            await engine.send_message(event.chat_id, visual.warn(f"Токен сохранён, но проверить не вышло: {_esc(e)}"))
        return
    if sub == 'lastfm':
        if not rest or rest[0] in ('off', 'выкл'):
            if rest:
                await npset('lastfm', None)
                await engine.answer(event, visual.ok("Last.fm отключён"))
                return
            await engine.answer(event, "\n".join([
                visual.title("Aura", sub="Last.fm"),
                "Last.fm видит, что играет в любом плеере со скробблингом (Spotify, Apple Music, YouTube Music через расширение, foobar, AIMP…).",
                '1. Ключ API — бесплатно: <a href="https://www.last.fm/api/account/create">last.fm/api/account/create</a>',
                "2. <code>.aura lastfm &lt;ваш ник&gt; &lt;api_key&gt;</code>"]))
            return
        old = await npget('lastfm') or {}
        key = rest[1] if len(rest) > 1 else old.get('key')
        if not key:
            await engine.answer(event, visual.err("Нужен ключ API: .aura lastfm <ник> <api_key>"))
            return
        await npset('lastfm', {'user': rest[0], 'key': key})
        try:
            await event.delete()
        except Exception:
            pass
        await engine.send_message(event.chat_id, visual.ok(f"Last.fm подключён: {_esc(rest[0])}"))
        return
    # .aura np — карточка
    await engine.answer(event, visual.wait("Смотрю, что играет…"))
    await np_refresh(force=True)
    np = np_now()
    if not np:
        errs = STATE['np_err']
        conn = [k for k, v in (STATE.get('np_conn') or {}).items() if v]
        await engine.answer(event, visual.hint("Сейчас ничего не играет") + "\n"
                            + (visual.hint("подключено: " + ", ".join(conn)) if conn else
                               visual.hint("подключите: .aura spotify · .aura yandex · .aura lastfm"))
                            + "".join("\n" + visual.warn(f"{k}: {_esc(v)}") for k, v in errs.items()))
        return
    lines = [f"🎧 <b>{_esc(np['title'])}</b>", _esc(np['artist'])]
    if np['duration']:
        lines.append(f"<code>{mmss(np['progress'])}</code> {np_bar(np, 12)} <code>{mmss(np['duration'])}</code>")
    lines.append(visual.hint(np['source'] + (" · на паузе" if np['paused'] else "")
                             + (f' · <a href="{_esc(np["link"])}">открыть</a>' if np.get('link') else "")))
    text = "\n".join(lines)
    if np.get('cover'):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
                async with sess.get(np['cover']) as r:
                    data = await r.read()
            obj = await files.create_file('image/jpeg', 'aura_np.jpg', data)
            await engine.answer(event, text, file=obj['path'])
            return
        except Exception:
            pass
    await engine.answer(event, text)


def mmss(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02}"


def np_bar(np, width=8):
    if not np or not np.get('duration'):
        return ''
    k = int(round(width * np['progress'] / np['duration']))
    return '▰' * k + '▱' * (width - k)


async def activity():
    a = STATE['activity']
    if a and time.time() - a['at'] <= int(await cfg('reading_ttl', 10)) * 60:
        return a
    return None


# --------------------------------------------------------------------------- шаблоны

def moon(now):
    ref = dt.datetime(2000, 1, 6, 18, 14)
    age = ((now - ref).total_seconds() / 86400) % 29.530588
    return MOONS[int((age / 29.530588) * 8 + 0.5) % 8]


async def original():
    return await dget('original', {}) or {}


async def render(template, limit=None, nested=True):
    now = dt.datetime.now()
    orig = await original()
    act = await activity()
    track = listening()
    cut = [60]

    def short(s):
        s = ' '.join(str(s).split())
        return s if len(s) <= cut[0] else s[:max(1, cut[0] - 1)].rstrip() + '…'

    idle = await cfg('idle_text', '') if nested else ''

    async def var(name, arg):
        if name.startswith('np'):
            np = np_now()
            if not np:
                return ''
            return {'np': short(f"{np['artist']} — {np['title']}" if np['artist'] else np['title']),
                     'np_title': short(np['title']), 'np_artist': short(np['artist']),
                     'np_time': f"{mmss(np['progress'])}/{mmss(np['duration'])}" if np['duration'] else '',
                     'np_bar': np_bar(np), 'np_source': np['source']}.get(name)
        if name == 'time':
            return now.strftime('%H:%M')
        if name == 'date':
            return now.strftime('%d.%m')
        if name == 'weekday':
            return WD[now.weekday()]
        if name == 'day':
            return WD_FULL[now.weekday()]
        if name == 'month':
            return f"{now.day} {MONTHS[now.month - 1]}"
        if name == 'year':
            return str(now.year)
        if name == 'clock':
            return (HALF if now.minute >= 30 else CLOCKS)[now.hour % 12]
        if name == 'moon':
            return moon(now)
        if name == 'reading':
            return short(act['title']) if act else ''
        if name == 'listening':
            return short(track)
        if name == 'doing':
            if track:
                return f"слушает «{short(track)}»"
            if act:
                return f"{'читает' if act['what'] == 'reading' else 'пишет в'} «{short(act['title'])}»"
            return await render(idle, nested=False) if idle else ''
        if name == 'days' and arg:
            try:
                return str(max(0, (dt.date.fromisoformat(arg.strip()) - now.date()).days))
            except ValueError:
                return '?'
        if name == 'year_progress':
            start = dt.datetime(now.year, 1, 1)
            return str(int((now - start).total_seconds() * 100 / ((dt.datetime(now.year + 1, 1, 1) - start).total_seconds())))
        if name == 'random' and arg:
            return random.choice(arg.split('|'))
        if name in ('first', 'last', 'bio'):
            return orig.get(name) or ''
        return None

    async def once():
        out, pos = [], 0
        for m in re.finditer(r'\{(\w+)(?::([^{}]*))?\}', template or ''):
            out.append(template[pos:m.start()])
            v = await var(m.group(1), m.group(2))
            out.append(m.group(0) if v is None else v)
            pos = m.end()
        out.append((template or '')[pos:])
        return re.sub(r'[ \t]{2,}', ' ', ''.join(out)).strip()

    text = await once()
    while limit and len(text) > limit and cut[0] > 8:
        cut[0] -= 6
        text = await once()
    if limit and len(text) > limit:
        text = text[:limit - 1] + '…'
    return text


# --------------------------------------------------------------------------- профиль

async def is_premium():
    if STATE['premium'] is None:
        STATE['premium'] = bool(getattr(await engine.client.get_me(), 'premium', False))
    return STATE['premium']


async def save_original():
    if await dget('original'):
        return
    from telethon.tl.functions.users import GetFullUserRequest
    me = await engine.client.get_me()
    full = await engine.client(GetFullUserRequest(me))
    st = getattr(me, 'emoji_status', None)
    await dset('original', {'first': me.first_name or '', 'last': me.last_name or '', 'bio': full.full_user.about or '',
                            'status': getattr(st, 'document_id', None)})


async def restore():
    from telethon.tl.functions.account import UpdateEmojiStatusRequest, UpdateProfileRequest
    from telethon.tl.types import EmojiStatus, EmojiStatusEmpty
    o = await original()
    if not o:
        return
    try:
        await engine.client(UpdateProfileRequest(first_name=o.get('first') or 'FRX', last_name=o.get('last') or '',
                                                 about=o.get('bio') or ''))
    except Exception as e:
        engine.tlog(f"[aura] restore profile: {e}")
    if await cfg('status_on'):
        try:
            await engine.client(UpdateEmojiStatusRequest(EmojiStatus(document_id=o['status']) if o.get('status') else EmojiStatusEmpty()))
        except Exception:
            pass
    await drop_auto_avatar()
    await dset('original', None)
    STATE['values'].clear()


async def restore_part(flag):
    """Часть выключили в панели — вернуть её как было, остальное Aura продолжает менять."""
    from telethon.tl.functions.account import UpdateEmojiStatusRequest, UpdateProfileRequest
    from telethon.tl.types import EmojiStatus, EmojiStatusEmpty
    o = await original()
    if not o:
        return
    try:
        if flag == 'name_on':
            await engine.client(UpdateProfileRequest(first_name=o.get('first') or 'FRX'))
        elif flag == 'last_on':
            await engine.client(UpdateProfileRequest(last_name=o.get('last') or ''))
        elif flag == 'bio_on':
            await engine.client(UpdateProfileRequest(about=o.get('bio') or ''))
        elif flag == 'status_on':
            await engine.client(UpdateEmojiStatusRequest(EmojiStatus(document_id=o['status']) if o.get('status') else EmojiStatusEmpty()))
    except Exception as e:
        engine.tlog(f"[aura] restore {flag}: {e}")


def due(part, seconds):
    now = time.time()
    if STATE['block'].get(part, 0) > now:
        return False
    return now - STATE['last'].get(part, 0) >= seconds


def flood(part, e):
    sec = getattr(e, 'seconds', None)
    if sec:
        STATE['block'][part] = time.time() + sec + 5
        STATE['errors'][part] = f"Telegram попросил подождать {sec} с — увеличьте интервал"
    else:
        STATE['errors'][part] = str(e)[:150]
    engine.tlog(f"[aura] {part}: {e}")


async def apply(force=False):
    """Один проход: посчитать шаблоны и поменять то, что изменилось и чему пора."""
    from telethon.tl.functions.account import UpdateProfileRequest
    if not await cfg('enabled'):
        return
    await save_original()
    vals = STATE['values']
    kw, parts = {}, []
    if force or due('name', int(await cfg('name_interval', 60))):
        if await cfg('name_on'):
            v = await render(await cfg('name_template', ''), 64)
            if v and v != vals.get('first'):
                kw['first_name'] = v
        if await cfg('last_on'):
            v = await render(await cfg('last_template', ''), 64)
            if v != vals.get('last'):
                kw['last_name'] = v
        if kw:
            parts.append('name')
    if await cfg('bio_on') and (force or due('bio', int(await cfg('bio_interval', 60)))):
        v = await render(await cfg('bio_template', ''), 140 if await is_premium() else 70)
        if v != vals.get('bio'):
            kw['about'] = v
            parts.append('bio')
    if kw:
        try:
            await engine.client(UpdateProfileRequest(**kw))
            vals.update({'first': kw.get('first_name', vals.get('first')), 'last': kw.get('last_name', vals.get('last')),
                         'bio': kw.get('about', vals.get('bio'))})
            for p in parts:
                STATE['last'][p] = time.time()
                STATE['errors'].pop(p, None)
        except Exception as e:
            for p in parts:
                flood(p, e)
    mode = await cfg('avatar_mode', 'off')
    if mode != 'off' and (force or due('avatar', int(await cfg('avatar_interval', 15)) * 60)):
        try:
            if await set_avatar(mode):
                STATE['last']['avatar'] = time.time()
                STATE['errors'].pop('avatar', None)
        except Exception as e:
            flood('avatar', e)
    if await cfg('status_on') and (force or due('status', int(await cfg('status_interval', 5)) * 60)):
        try:
            await set_status()
            STATE['last']['status'] = time.time()
            STATE['errors'].pop('status', None)
        except Exception as e:
            flood('status', e)


async def loop():
    await asyncio.sleep(5)
    while True:
        try:
            if await cfg('enabled'):
                await np_refresh()
            await apply()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            engine.tlog(f"[aura] {e!r}")
        await asyncio.sleep(10)


# ---- аватарка

FONT_URL = 'https://raw.githubusercontent.com/google/fonts/main/ofl/notosans/NotoSans%5Bwdth,wght%5D.ttf'


async def _font(size):
    from PIL import ImageFont
    cands = [data_dir() / 'NotoSans.ttf']
    if not any(p.is_file() for p in cands):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
                async with s.get(FONT_URL) as r:
                    if r.status == 200:
                        cands[1].write_bytes(await r.read())
        except Exception:
            pass
    for p in cands:
        if p.is_file():
            try:
                f = ImageFont.truetype(str(p), size)
                f.set_variation_by_name('Bold')
                return f
            except Exception:
                continue
    for name in ('segoeuib.ttf', 'arialbd.ttf', 'DejaVuSans-Bold.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


async def clock_avatar():
    from PIL import Image, ImageDraw, ImageFilter
    base_p = data_dir() / 'base.jpg'
    if not base_p.is_file():
        data = await engine.client.download_profile_photo('me', file=bytes, download_big=True)
        if not data:
            raise RuntimeError("нет фото для часов — пришлите .aura base ответом на фото")
        base_p.write_bytes(data)
    im = Image.open(base_p).convert('RGB')
    side = min(im.size)
    im = im.crop(((im.width - side) // 2, (im.height - side) // 2, (im.width + side) // 2, (im.height + side) // 2)).resize((640, 640))
    now = dt.datetime.now()
    shade = Image.new('L', (640, 640), 0)
    ImageDraw.Draw(shade).rectangle([0, 400, 640, 640], fill=150)
    shade = shade.filter(ImageFilter.GaussianBlur(60))
    im = Image.composite(Image.new('RGB', im.size, (0, 0, 0)), im, shade)
    d = ImageDraw.Draw(im)
    f, f2 = await _font(150), await _font(40)
    t = now.strftime('%H:%M')
    w = d.textlength(t, font=f)
    d.text(((640 - w) / 2 + 3, 405 + 4), t, font=f, fill=(0, 0, 0))
    d.text(((640 - w) / 2, 405), t, font=f, fill=(255, 255, 255))
    sub = f"{WD_FULL[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}"
    w2 = d.textlength(sub, font=f2)
    d.text(((640 - w2) / 2, 365), sub, font=f2, fill=(235, 235, 235))
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=92)
    return buf.getvalue()


def carousel_files():
    d = data_dir() / 'avatars'
    d.mkdir(exist_ok=True)
    return sorted(p for p in d.iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'))


async def set_avatar(mode):
    from telethon.tl.functions.photos import UploadProfilePhotoRequest
    if mode == 'clock':
        data = await clock_avatar()
    else:
        files = carousel_files()
        if not files:
            raise RuntimeError("карусель пуста — добавьте фото: .aura addava ответом на фото")
        i = (int(await dget('carousel_i', -1)) + 1) % len(files)
        await dset('carousel_i', i)
        data = files[i].read_bytes()
    up = await engine.client.upload_file(io.BytesIO(data), file_name='aura.jpg')
    res = await engine.client(UploadProfilePhotoRequest(file=up))
    await drop_auto_avatar()
    ph = res.photo
    await dset('auto_photo', {'id': ph.id, 'hash': ph.access_hash, 'ref': base64.b64encode(ph.file_reference).decode()})
    return True


async def drop_auto_avatar():
    """Удалить прошлое фото, которое ставила Aura (чтобы в профиле не копились сотни аватарок)."""
    from telethon.tl.functions.photos import DeletePhotosRequest
    from telethon.tl.types import InputPhoto
    prev = await dget('auto_photo')
    if not prev:
        return
    try:
        await engine.client(DeletePhotosRequest([InputPhoto(id=prev['id'], access_hash=prev['hash'],
                                                            file_reference=base64.b64decode(prev['ref']))]))
    except Exception as e:
        engine.tlog(f"[aura] delete photo: {e}")
    await dset('auto_photo', None)


# ---- эмодзи-статус

async def set_status():
    from telethon.tl.functions.account import UpdateEmojiStatusRequest
    from telethon.tl.types import EmojiStatus
    ids = [int(x) for x in (await cfg('status_ids', []) or []) if str(x).lstrip('-').isdigit()]
    if not ids:
        raise RuntimeError("нет эмодзи — .aura status ответом на сообщение с премиум-эмодзи")
    if not await is_premium():
        raise RuntimeError("эмодзи-статус есть только с Telegram Premium")
    i = (int(await dget('status_i', -1)) + 1) % len(ids)
    await dset('status_i', i)
    await engine.client(UpdateEmojiStatusRequest(EmojiStatus(document_id=ids[i])))


# --------------------------------------------------------------------------- панель и команды

def _ago(t):
    if not t:
        return "ещё не менялось"
    s = int(time.time() - t)
    return f"{s} с назад" if s < 90 else f"{s // 60} мин назад"


async def panel_text(show_vars=False):
    on = await cfg('enabled')
    t = [visual.title("Aura", sub="включена" if on else "выключена")]
    rows = [('name_on', 'Имя', 'name_template', 64), ('last_on', 'Фамилия', 'last_template', 64),
            ('bio_on', 'Био', 'bio_template', 140 if await is_premium() else 70)]
    for flag, label, tkey, lim in rows:
        tpl = await cfg(tkey, '')
        if await cfg(flag):
            t.append(f"✓ <b>{label}</b>: <code>{_esc(tpl)}</code>\n   → {_esc(await render(tpl, lim)) or '<i>пусто</i>'}")
        else:
            t.append(f"· {label}: <i>не трогаем</i>")
    mode = await cfg('avatar_mode', 'off')
    t.append(f"{'✓' if mode != 'off' else '·'} Аватарка: " + {'off': '<i>не трогаем</i>', 'clock': 'фото с часами',
                                                               'carousel': f'по кругу ({len(carousel_files())} фото)'}[mode]
             + (f" · каждые {await cfg('avatar_interval', 15)} мин" if mode != 'off' else ''))
    n = len(await cfg('status_ids', []) or [])
    t.append(f"{'✓' if await cfg('status_on') else '·'} Статус: {n} эмодзи" + (f" · каждые {await cfg('status_interval', 5)} мин"
                                                                              if await cfg('status_on') else ''))
    a = await activity()
    track = listening()
    np = np_now()
    t.append("")
    srcs = [n for n, key in (('Spotify', 'spotify'), ('Яндекс', 'yandex'), ('Last.fm', 'lastfm')) if (STATE.get('np_conn') or {}).get(key)]
    t.append(visual.field("музыка", (f"{np['source']}: {_esc(np['artist'] + ' — ' if np['artist'] else '')}{_esc(np['title'])}"
                                     if np else "ничего не играет") + (f" · подключено: {', '.join(srcs)}" if srcs else
                                                                        " · подключить: .aura spotify / yandex / lastfm"),
                          code=False))
    t.append(visual.field("сейчас", f"слушает «{_esc(track)}»" if track else (
        f"{'читает' if a['what'] == 'reading' else 'пишет в'} «{_esc(a['title'])}»" if a else "ничего (idle_text)"), code=False))
    t.append(visual.field("профиль", _ago(STATE['last'].get('name') or STATE['last'].get('bio')), code=False))
    for part, err in STATE['errors'].items():
        t.append(visual.warn(f"{part}: {_esc(err)}"))
    if show_vars:
        t.append("")
        t.append(visual.section("Переменные"))
        t += [f"<code>{k}</code> — {_esc(v)}" for k, v in VARS]
    t.append("")
    t.append(visual.hint("частота смены и «читает» — .cfg aura"))
    return "\n".join(t)


async def panel_buttons():
    on = await cfg('enabled')

    def mark(flag, label):
        return ("✓ " if flag else "") + label
    mode = await cfg('avatar_mode', 'off')
    return [
        [(("● Включена" if on else "○ Выключена"), "au:power", 'success' if on else 'danger'), ("⟳ Применить", "au:apply", 'primary')],
        [(mark(await cfg('name_on'), "Имя"), "au:t:name_on"), (mark(await cfg('last_on'), "Фамилия"), "au:t:last_on"),
         (mark(await cfg('bio_on'), "Био"), "au:t:bio_on")],
        [("Аватарка: " + {'off': 'нет', 'clock': 'часы', 'carousel': 'карусель'}[mode], "au:ava"),
         (mark(await cfg('status_on'), "Статус"), "au:t:status_on")],
        [engine.InputButton("✎ Имя", "tpl_name", hint="Шаблон имени, напр. {first}"),
         engine.InputButton("✎ Фамилия", "tpl_last", hint="Шаблон фамилии, напр. {clock} {time}"),
         engine.InputButton("✎ Био", "tpl_bio", hint="Шаблон био, напр. {doing}")],
        [engine.InputButton("✎ Когда ничего не делаю", "tpl_idle", hint="Текст {doing}, когда вы ничего не читаете")],
        [("Переменные", "au:vars"), (visual.btn('Закрыть', 'close'), "au:close")],
    ]


async def power(on):
    if on:
        await save_original()
        await setc('enabled', True)
        STATE['values'].clear()
        await apply(force=True)
    else:
        await setc('enabled', False)
        await restore()


@decorators.command('aura', 'аура')
async def aura_cmd(event):
    """живой профиль: панель; on/off · vars · base · addava · avas clear · status · np · spotify · yandex · lastfm"""
    args = engine.get_args().strip().lower().split()
    sub = args[0] if args else ''
    r = await event.get_reply_message() if event.is_reply else None
    if sub in ('on', 'вкл'):
        await power(True)
        await engine.answer(event, visual.ok("Aura включена") + "\n" + visual.hint(".aura — панель · .aura off — вернуть профиль"))
        return
    if sub in ('off', 'выкл'):
        await power(False)
        await engine.answer(event, visual.ok("Aura выключена — профиль вернулся как был"))
        return
    if sub == 'vars':
        await engine.answer(event, visual.title("Aura", sub="переменные") + "\n" + "\n".join(f"<code>{k}</code> — {_esc(v)}" for k, v in VARS))
        return
    if sub in ('base', 'addava'):
        if r is None or not (r.photo or (r.document and (r.file.mime_type or '').startswith('image/'))):
            await engine.answer(event, visual.err("Ответьте на фото"))
            return
        data = await engine.client.download_media(r, file=bytes)
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert('RGB')
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=93)
        if sub == 'base':
            (data_dir() / 'base.jpg').write_bytes(buf.getvalue())
            await engine.answer(event, visual.ok("Основа для аватарки-часов сохранена") + "\n" + visual.hint("включить: .aura → Аватарка: часы"))
        else:
            p = data_dir() / 'avatars' / f"{int(time.time())}.jpg"
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(buf.getvalue())
            await engine.answer(event, visual.ok(f"В карусели {len(carousel_files())} фото"))
        return
    if sub == 'avas' and len(args) > 1 and args[1] in ('clear', 'очистить'):
        for p in carousel_files():
            p.unlink(missing_ok=True)
        await engine.answer(event, visual.ok("Карусель очищена"))
        return
    if sub in ('np', 'nowplaying', 'spotify', 'yandex', 'ya', 'lastfm', 'lfm'):
        await np_command(event, sub, engine.get_args().strip().split()[1:])
        return
    if sub == 'status':
        from telethon.tl.types import MessageEntityCustomEmoji
        ids = [str(e.document_id) for e in ((r.entities if r else None) or []) if isinstance(e, MessageEntityCustomEmoji)]
        if not ids:
            await engine.answer(event, visual.err("Ответьте на сообщение с премиум-эмодзи — они станут статусами по очереди"))
            return
        ids = list(dict.fromkeys(ids))[:50]
        await engine.edit_config('status_ids', 'to_default', module_name=MODULE_NAME)
        for i in ids:
            await engine.edit_config('status_ids', 'add', i, module_name=MODULE_NAME)
        await engine.answer(event, visual.ok(f"Статусов: {len(ids)}") + "\n" + visual.hint("включить: .aura → Статус"))
        return

    view = {'vars': False}
    form = await engine.create_inlineform(event.chat_id, await panel_text(), buttons=await panel_buttons())
    await event.delete()

    async def redraw():
        await form.edit(await panel_text(view['vars']), buttons=await panel_buttons())

    @form.on_click(data_startswith='au:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
            return
        if d == 'power':
            await power(not await cfg('enabled'))
            await click.answer("Включена" if await cfg('enabled') else "Выключена, профиль возвращён")
        elif d == 'apply':
            if not await cfg('enabled'):
                await click.answer("Сначала включите Aura", show_alert=True)
                return
            await apply(force=True)
            await click.answer("Применено")
        elif d.startswith('t:'):
            k = d[2:]
            new = not await cfg(k)
            await setc(k, new)
            if not new and await cfg('enabled'):
                await restore_part(k)
            STATE['values'].clear()
        elif d == 'ava':
            cur = await cfg('avatar_mode', 'off')
            nxt = AVATAR_MODES[(AVATAR_MODES.index(cur) + 1) % len(AVATAR_MODES)]
            await setc('avatar_mode', nxt)
            if nxt == 'off':
                await drop_auto_avatar()
            STATE['last'].pop('avatar', None)
        elif d == 'vars':
            view['vars'] = not view['vars']
        await redraw()

    for key, ckey in (('tpl_name', 'name_template'), ('tpl_last', 'last_template'), ('tpl_bio', 'bio_template'),
                      ('tpl_idle', 'idle_text')):
        def make(key, ckey):
            @form.on_input(data=key)
            async def _(inp):
                v = (inp.value or '').strip()
                if v:
                    await setc(ckey, '' if v == '-' else v)
                    STATE['values'].clear()
                await redraw()
        make(key, ckey)
