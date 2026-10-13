"""
Weather — погода (Open-Meteo, без ключей).

  .w [город]          сейчас + кнопки «по часам», «на неделю», «обновить» (без города — город из настроек)
  .wset <город>       город по умолчанию
  @бот w [город]      погода в любом чате инлайном
"""

import html
import time
from datetime import datetime

import aiohttp

import bot
import db
import decorators
import engine
import visual

NAME = "Weather"
MODULE_NAME = "weather"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Погода сейчас, по часам и на неделю (Open-Meteo, без ключей): .w город, @бот w город"

WMO = {0: ('☀️', 'ясно'), 1: ('🌤', 'в основном ясно'), 2: ('⛅', 'переменная облачность'), 3: ('☁️', 'пасмурно'),
       45: ('🌫', 'туман'), 48: ('🌫', 'изморозь'), 51: ('🌦', 'слабая морось'), 53: ('🌦', 'морось'), 55: ('🌧', 'сильная морось'),
       56: ('🌧', 'ледяная морось'), 57: ('🌧', 'ледяная морось'), 61: ('🌦', 'небольшой дождь'), 63: ('🌧', 'дождь'),
       65: ('🌧', 'ливень'), 66: ('🌧', 'ледяной дождь'), 67: ('🌧', 'ледяной дождь'), 71: ('🌨', 'небольшой снег'),
       73: ('🌨', 'снег'), 75: ('❄️', 'сильный снег'), 77: ('🌨', 'снежная крупа'), 80: ('🌦', 'ливень'), 81: ('🌧', 'ливни'),
       82: ('⛈', 'сильный ливень'), 85: ('🌨', 'снегопад'), 86: ('❄️', 'сильный снегопад'), 95: ('⛈', 'гроза'),
       96: ('⛈', 'гроза с градом'), 99: ('⛈', 'сильная гроза с градом')}
NIGHT = {0: '🌙', 1: '🌙'}
DIRS = ['С', 'СВ', 'В', 'ЮВ', 'Ю', 'ЮЗ', 'З', 'СЗ']
WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']


def _esc(x):
    return html.escape(str(x), quote=False)


async def _get(url, params):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url, params=params) as r:
            r.raise_for_status()
            return await r.json(content_type=None)


async def geocode(name):
    data = await _get('https://geocoding-api.open-meteo.com/v1/search', {'name': name, 'count': 1, 'language': 'ru', 'format': 'json'})
    res = (data.get('results') or [None])[0]
    if not res:
        return None
    parts = []
    for x in (res.get('name'), res.get('admin1'), res.get('country')):
        if x and x not in parts:
            parts.append(x)
    place = ", ".join(parts)
    return {'name': res.get('name'), 'place': place, 'lat': res['latitude'], 'lon': res['longitude']}


async def forecast(loc):
    return await _get('https://api.open-meteo.com/v1/forecast', {
        'latitude': loc['lat'], 'longitude': loc['lon'], 'timezone': 'auto', 'forecast_days': 7, 'wind_speed_unit': 'ms',
        'current': 'temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m,'
                   'wind_gusts_10m,pressure_msl,precipitation,is_day,cloud_cover',
        'hourly': 'temperature_2m,precipitation_probability,weather_code,is_day',
        'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset,'
                 'wind_speed_10m_max,uv_index_max'})


def _icon(code, day=1):
    ic, desc = WMO.get(int(code or 0), ('🌡', '—'))
    return (NIGHT.get(int(code or 0), ic) if not day else ic), desc


def _t(v):
    v = round(float(v))
    return f"+{v}°" if v > 0 else f"{v}°"


def now_text(loc, f):
    c = f['current']
    ic, desc = _icon(c['weather_code'], c.get('is_day', 1))
    d = f['daily']
    wind_dir = DIRS[int((c.get('wind_direction_10m', 0) + 22.5) // 45) % 8]
    lines = [f"{ic} <b>{_esc(loc['name'])}</b> — {_t(c['temperature_2m'])}, {desc}",
             visual.hint(_esc(loc['place'])), "",
             visual.field("ощущается", _t(c['apparent_temperature'])),
             visual.field("ветер", f"{c['wind_speed_10m']:.0f} м/с, {wind_dir}" + (f" (порывы до {c['wind_gusts_10m']:.0f})" if c.get('wind_gusts_10m') else '')),
             visual.field("влажность", f"{c['relative_humidity_2m']}%"),
             visual.field("давление", f"{c['pressure_msl'] * 0.750062:.0f} мм рт. ст."),
             visual.field("облачность", f"{c.get('cloud_cover', 0)}%"),
             visual.field("сегодня", f"{_t(d['temperature_2m_min'][0])} … {_t(d['temperature_2m_max'][0])}, осадки {d['precipitation_probability_max'][0] or 0}%"),
             visual.field("восход / закат", f"{d['sunrise'][0][-5:]} / {d['sunset'][0][-5:]}")]
    if d.get('uv_index_max') and d['uv_index_max'][0] is not None:
        lines.append(visual.field("УФ-индекс", f"{d['uv_index_max'][0]:.0f}"))
    return "\n".join(lines)


def hourly_text(loc, f):
    h = f['hourly']
    now = f['current']['time'][:13]
    start = next((i for i, t in enumerate(h['time']) if t[:13] >= now), 0)
    lines = [f"🕒 <b>{_esc(loc['name'])}</b> — по часам", ""]
    for i in range(start, min(start + 24, len(h['time'])), 2):
        ic, _ = _icon(h['weather_code'][i], h['is_day'][i])
        p = h['precipitation_probability'][i]
        lines.append(f"<code>{h['time'][i][11:16]}</code>  {ic} {_t(h['temperature_2m'][i]):>5}" + (f"  💧{p}%" if p else ''))
    return "\n".join(lines)


def week_text(loc, f):
    d = f['daily']
    lines = [f"📅 <b>{_esc(loc['name'])}</b> — на неделю", ""]
    for i, day in enumerate(d['time']):
        dt = datetime.strptime(day, '%Y-%m-%d')
        ic, desc = _icon(d['weather_code'][i])
        lines.append(f"<code>{WD[dt.weekday()]} {dt.day:02}.{dt.month:02}</code>  {ic} {_t(d['temperature_2m_min'][i])} … "
                     f"{_t(d['temperature_2m_max'][i])}  <i>{desc}</i>" + (f" 💧{d['precipitation_probability_max'][i]}%" if d['precipitation_probability_max'][i] else ''))
    return "\n".join(lines)


async def _resolve(name):
    name = (name or '').strip() or (await db.get('city')) or 'Москва'
    loc = await geocode(name)
    if loc is None:
        raise ValueError(f"город «{name}» не найден")
    return loc


@decorators.command('wset')
async def wset_cmd(event):
    """город по умолчанию для .w: <город>"""
    name = engine.get_args().strip()
    try:
        loc = await _resolve(name) if name else None
    except Exception as e:
        await engine.answer(event, visual.err(_esc(e)))
        return
    if loc is None:
        await engine.answer(event, visual.err("Укажите город") + "\n" + visual.hint(".wset Москва"))
        return
    await db.set('city', loc['name'])
    await engine.answer(event, visual.ok("Город по умолчанию") + "\n" + visual.field("город", _esc(loc['place']), code=False))


@decorators.command('w', 'weather', 'погода')
async def w_cmd(event):
    """погода: [город] — сейчас, по часам и на неделю кнопками"""
    try:
        loc = await _resolve(engine.get_args())
        f = await forecast(loc)
    except Exception as e:
        await engine.answer(event, visual.err("Погода недоступна") + f"\n<code>{_esc(e)}</code>")
        return
    st = {'view': 'now', 'f': f, 't': time.time()}
    views = {'now': now_text, 'hours': hourly_text, 'week': week_text}

    def buttons():
        lab = {'now': 'Сейчас', 'hours': 'По часам', 'week': 'Неделя'}
        return [[(("● " if st['view'] == k else '') + v, f"wx:{k}") for k, v in lab.items()],
                [("🔄 Обновить", "wx:refresh"), (visual.btn('Закрыть', 'close'), "wx:close")]]
    try:
        form = await engine.create_inlineform(event.chat_id, now_text(loc, f), buttons=buttons())
    except Exception:
        await engine.answer(event, now_text(loc, f))
        return
    await engine.answer(event, visual.ok("Погода") + "  " + visual.hint(_esc(loc['name'])))

    @form.on_click(data_startswith='wx:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
            return
        if d == 'refresh' or time.time() - st['t'] > 900:
            st['f'], st['t'] = await forecast(loc), time.time()
            if d == 'refresh':
                await click.answer("обновлено")
        if d in views:
            st['view'] = d
        await form.edit(views[st['view']](loc, st['f']), buttons=buttons())


@bot.inline_query('w', 'weather', 'погода', access='public')
async def w_inline(query):
    """погода: [город]"""
    try:
        loc = await _resolve(query.text)
        f = await forecast(loc)
    except Exception as e:
        return [engine.InlineResult("Погода недоступна", description=str(e)[:100], text=visual.err(_esc(e)))]
    c = f['current']
    ic, desc = _icon(c['weather_code'], c.get('is_day', 1))
    return [engine.InlineResult(f"{ic} {loc['name']}: {_t(c['temperature_2m'])}, {desc}", description=loc['place'],
                                text=now_text(loc, f), link_preview=False),
            engine.InlineResult(f"📅 {loc['name']}: на неделю", description="минимум и максимум по дням",
                                text=week_text(loc, f), link_preview=False)]
