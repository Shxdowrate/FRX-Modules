"""
WorldClock — время в любой точке мира: город, регион, страна (все её часовые пояса с регионами), UTC±N.

  .time                         ваши места: время, день/ночь, разница с вами
  .time <место>                 сколько сейчас там: «Токио», «Якутия», «Калифорния», «UTC+5»
  .time <страна>                все часовые пояса страны с регионами: «.time Россия», «.time США», «.time Бразилия»
  .time 15:30 <место>           это время там → у вас и в ваших местах;  .time 15:30 Москва > Токио — между двумя
  .time diff <место> <место>    разница во времени (несколько слов — через запятую: .time diff Нью-Йорк, Токио)
  .time add <место> · del <№|место> · list      ваши места
  .time home <место>            ваш часовой пояс (по умолчанию — часы компьютера с FRX)
  @бот time <место>             в любом чате инлайном

🟢 рабочее время (9–18, будни) · 🌙 ночь · ☀️ день. Города ищутся через Open-Meteo (без ключей), популярные — сразу.
"""

import datetime as dt
import html
import re
from zoneinfo import ZoneInfo, available_timezones

import aiohttp

import bot
import db
import decorators
import engine
import visual

NAME = "WorldClock"
MODULE_NAME = "worldclock"
VERSION = "1.1"
AUTHOR = "FRX"
DESCRIPTION = "Время в городах, регионах и странах (все пояса страны с регионами), перевод времени, разница, инлайн"
REQUIREMENTS = ["tzdata"]   # ставятся сами (core/requirements.py)

WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']

# Россия — по поясам, с регионами
RU_ZONES = [
    ('Europe/Kaliningrad', 'Калининградская область'),
    ('Europe/Moscow', 'Москва, Санкт-Петербург, почти вся европейская часть, Крым'),
    ('Europe/Samara', 'Самарская, Ульяновская, Саратовская, Астраханская области, Удмуртия'),
    ('Asia/Yekaterinburg', 'Свердловская, Челябинская, Тюменская, Курганская, Оренбургская области, Пермский край, '
                           'Башкортостан, ХМАО, ЯНАО'),
    ('Asia/Omsk', 'Омская область'),
    ('Asia/Novosibirsk', 'Новосибирская, Томская, Кемеровская области, Алтайский и Красноярский края, Алтай, Тыва, Хакасия'),
    ('Asia/Irkutsk', 'Иркутская область, Бурятия'),
    ('Asia/Chita', 'Забайкальский край, Амурская область, большая часть Якутии'),
    ('Asia/Vladivostok', 'Приморский и Хабаровский края, Еврейская АО, часть Якутии'),
    ('Asia/Magadan', 'Магаданская и Сахалинская области, север Якутии'),
    ('Asia/Kamchatka', 'Камчатский край, Чукотка'),
]

# популярные места — без запросов в интернет (и русские названия к поясам)
PLACES = {
    'москва': 'Europe/Moscow', 'мск': 'Europe/Moscow', 'санкт-петербург': 'Europe/Moscow', 'питер': 'Europe/Moscow',
    'спб': 'Europe/Moscow', 'калининград': 'Europe/Kaliningrad', 'самара': 'Europe/Samara', 'ижевск': 'Europe/Samara',
    'саратов': 'Europe/Saratov', 'ульяновск': 'Europe/Ulyanovsk', 'астрахань': 'Europe/Astrakhan',
    'екатеринбург': 'Asia/Yekaterinburg', 'челябинск': 'Asia/Yekaterinburg', 'тюмень': 'Asia/Yekaterinburg',
    'пермь': 'Asia/Yekaterinburg', 'уфа': 'Asia/Yekaterinburg', 'оренбург': 'Asia/Yekaterinburg', 'омск': 'Asia/Omsk',
    'новосибирск': 'Asia/Novosibirsk', 'томск': 'Asia/Tomsk', 'барнаул': 'Asia/Barnaul', 'кемерово': 'Asia/Novokuznetsk',
    'новокузнецк': 'Asia/Novokuznetsk', 'красноярск': 'Asia/Krasnoyarsk', 'иркутск': 'Asia/Irkutsk', 'улан-удэ': 'Asia/Irkutsk',
    'чита': 'Asia/Chita', 'якутск': 'Asia/Yakutsk', 'благовещенск': 'Asia/Yakutsk', 'владивосток': 'Asia/Vladivostok',
    'хабаровск': 'Asia/Vladivostok', 'магадан': 'Asia/Magadan', 'южно-сахалинск': 'Asia/Sakhalin', 'сахалин': 'Asia/Sakhalin',
    'петропавловск-камчатский': 'Asia/Kamchatka', 'камчатка': 'Asia/Kamchatka', 'анадырь': 'Asia/Anadyr',
    'якутия': 'Asia/Yakutsk', 'забайкалье': 'Asia/Chita', 'приморье': 'Asia/Vladivostok', 'крым': 'Europe/Simferopol',
    'киев': 'Europe/Kyiv', 'минск': 'Europe/Minsk', 'алматы': 'Asia/Almaty', 'астана': 'Asia/Almaty', 'ташкент': 'Asia/Tashkent',
    'бишкек': 'Asia/Bishkek', 'душанбе': 'Asia/Dushanbe', 'ереван': 'Asia/Yerevan', 'тбилиси': 'Asia/Tbilisi', 'баку': 'Asia/Baku',
    'кишинёв': 'Europe/Chisinau', 'кишинев': 'Europe/Chisinau', 'рига': 'Europe/Riga', 'вильнюс': 'Europe/Vilnius',
    'таллин': 'Europe/Tallinn', 'варшава': 'Europe/Warsaw', 'прага': 'Europe/Prague', 'берлин': 'Europe/Berlin',
    'париж': 'Europe/Paris', 'лондон': 'Europe/London', 'рим': 'Europe/Rome', 'мадрид': 'Europe/Madrid', 'стамбул': 'Europe/Istanbul',
    'анталья': 'Europe/Istanbul', 'дубай': 'Asia/Dubai', 'тель-авив': 'Asia/Jerusalem', 'каир': 'Africa/Cairo',
    'дели': 'Asia/Kolkata', 'мумбаи': 'Asia/Kolkata', 'бангкок': 'Asia/Bangkok', 'пхукет': 'Asia/Bangkok', 'бали': 'Asia/Makassar',
    'пекин': 'Asia/Shanghai', 'шанхай': 'Asia/Shanghai', 'гонконг': 'Asia/Hong_Kong', 'сеул': 'Asia/Seoul', 'токио': 'Asia/Tokyo',
    'сингапур': 'Asia/Singapore', 'сидней': 'Australia/Sydney', 'нью-йорк': 'America/New_York', 'вашингтон': 'America/New_York',
    'чикаго': 'America/Chicago', 'денвер': 'America/Denver', 'лос-анджелес': 'America/Los_Angeles', 'сан-франциско': 'America/Los_Angeles',
    'калифорния': 'America/Los_Angeles', 'техас': 'America/Chicago', 'флорида': 'America/New_York', 'майами': 'America/New_York',
    'аляска': 'America/Anchorage', 'гавайи': 'Pacific/Honolulu', 'торонто': 'America/Toronto', 'ванкувер': 'America/Vancouver',
    'мехико': 'America/Mexico_City', 'сан-паулу': 'America/Sao_Paulo', 'рио-де-жанейро': 'America/Sao_Paulo',
    'буэнос-айрес': 'America/Argentina/Buenos_Aires',
}
COUNTRIES = {'россия': 'RU', 'рф': 'RU', 'сша': 'US', 'америка': 'US', 'канада': 'CA', 'бразилия': 'BR', 'австралия': 'AU',
             'мексика': 'MX', 'казахстан': 'KZ', 'индонезия': 'ID', 'китай': 'CN', 'украина': 'UA', 'аргентина': 'AR',
             'чили': 'CL', 'монголия': 'MN', 'гренландия': 'GL', 'испания': 'ES', 'португалия': 'PT', 'франция': 'FR',
             'германия': 'DE', 'великобритания': 'GB', 'англия': 'GB', 'турция': 'TR', 'индия': 'IN', 'япония': 'JP',
             'новая зеландия': 'NZ', 'конго': 'CD', 'узбекистан': 'UZ', 'беларусь': 'BY', 'белоруссия': 'BY', 'грузия': 'GE',
             'армения': 'AM', 'азербайджан': 'AZ', 'таиланд': 'TH', 'вьетнам': 'VN', 'египет': 'EG', 'оаэ': 'AE', 'израиль': 'IL'}
ZONE_RU = {v: k for k, v in reversed(list(PLACES.items())) if '-' not in k or k in ('нью-йорк', 'лос-анджелес', 'сан-паулу', 'тель-авив')}


def _esc(x):
    return html.escape(str(x), quote=False)


# --------------------------------------------------------------------------- данные поясов

_ZONETAB = {}


def zonetab():
    """{страна: [(пояс, комментарий)]} из zone.tab (tzdata)."""
    if _ZONETAB:
        return _ZONETAB
    text = ''
    try:
        import importlib.resources as res
        text = res.files('tzdata').joinpath('zoneinfo', 'zone.tab').read_text(encoding='utf-8')
    except Exception:
        for p in ('/usr/share/zoneinfo/zone.tab', '/usr/share/zoneinfo/zone1970.tab'):
            try:
                text = open(p, encoding='utf-8').read()
                break
            except OSError:
                continue
    for line in text.splitlines():
        if line.startswith('#') or not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) >= 3:
            for cc in parts[0].split(','):
                _ZONETAB.setdefault(cc, []).append((parts[2], parts[3] if len(parts) > 3 else ''))
    return _ZONETAB


class Fixed(dt.tzinfo):
    def __init__(self, minutes):
        self.m = minutes

    def utcoffset(self, d):
        return dt.timedelta(minutes=self.m)

    def dst(self, d):
        return dt.timedelta(0)

    def tzname(self, d):
        return fmt_off(self.m * 60)


def fmt_off(sec):
    sign = '+' if sec >= 0 else '−'
    sec = abs(int(sec))
    h, m = sec // 3600, sec % 3600 // 60
    return f"UTC{sign}{h}" + (f":{m:02}" if m else "")


def tz_of(spec):
    """Строка пояса ('Europe/Moscow' | 'UTC+5:30' | 'local') → tzinfo."""
    if spec == 'local':
        return dt.datetime.now().astimezone().tzinfo
    m = re.fullmatch(r'(?:UTC|GMT)?\s*([+-−])\s*(\d{1,2})(?::?(\d{2}))?', spec.replace('−', '-'), re.I)
    if m:
        mins = int(m.group(2)) * 60 + int(m.group(3) or 0)
        return Fixed(-mins if m.group(1) in '-−' else mins)
    if spec.upper() in ('UTC', 'GMT'):
        return dt.timezone.utc
    return ZoneInfo(spec)


def city_of(zone):
    if '/' not in zone:
        return zone
    name = ZONE_RU.get(zone) or zone.rsplit('/', 1)[-1].replace('_', ' ')
    return name[:1].upper() + name[1:]


async def geocode(q):
    """Open-Meteo: [{name, region, country, cc, tz, kind}] — kind: country | region | place."""
    key = q.lower().strip()
    cache = await db.get('geo', {}) or {}
    if key in cache:
        return cache[key]
    url = 'https://geocoding-api.open-meteo.com/v1/search'
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as s:
            async with s.get(url, params={'name': q, 'count': 5, 'language': 'ru', 'format': 'json'}) as r:
                data = await r.json(content_type=None)
    except Exception:
        return []
    out = []
    for x in (data or {}).get('results') or []:
        fc = x.get('feature_code') or ''
        out.append({'name': x.get('name'), 'region': x.get('admin1') or '', 'country': x.get('country') or '',
                    'cc': x.get('country_code') or '', 'tz': x.get('timezone'),
                    'kind': 'country' if fc.startswith('PCL') else 'region' if fc.startswith('ADM1') else 'place'})
    if out:
        cache[key] = out
        if len(cache) > 300:
            cache = dict(list(cache.items())[-300:])
        await db.set('geo', cache)
    return out


async def resolve(q):
    """→ ('country', cc, имя) | ('zone', пояс, подпись) | None."""
    q = q.strip()
    low = q.lower().replace('ё', 'е')
    if re.fullmatch(r'(?i)(utc|gmt)\s*[+-−]?\s*\d{0,2}(:?\d{2})?', q) or re.fullmatch(r'[+-]\d{1,2}(:\d{2})?', q):
        spec = q.upper().replace('GMT', 'UTC')
        spec = 'UTC' if spec in ('UTC',) else spec if spec.startswith('UTC') else 'UTC' + spec
        try:
            tz_of(spec)
            return ('zone', spec, spec)
        except Exception:
            return None
    if low in COUNTRIES:
        return ('country', COUNTRIES[low], q.capitalize())
    if low in PLACES or low.replace('е', 'ё') in PLACES:
        z = PLACES.get(low) or PLACES[low.replace('е', 'ё')]
        return ('zone', z, q[:1].upper() + q[1:])
    if q in available_timezones():
        return ('zone', q, city_of(q))
    found = await geocode(q)
    if not found:
        return None
    best = found[0]
    if best['kind'] == 'country':
        return ('country', best['cc'], best['name'])
    if not best['tz']:
        return None
    label = best['name'] + (f", {best['region']}" if best['region'] and best['region'] != best['name'] else '') \
        + (f" ({best['country']})" if best['country'] else '')
    return ('zone', best['tz'], label)


# --------------------------------------------------------------------------- вывод

def mark(now):
    if now.weekday() < 5 and 9 <= now.hour < 18:
        return '🟢'
    return '🌙' if now.hour >= 22 or now.hour < 7 else '☀️'


async def home():
    return await db.get('home', None) or 'local'


def line(label, tzspec, base_now=None, ref_off=None, show_diff=True):
    now = dt.datetime.now(tz_of(tzspec))
    off = now.utcoffset().total_seconds()
    day = ''
    if base_now is not None:
        dd = (now.date() - base_now.date()).days
        day = ' · завтра' if dd == 1 else ' · вчера' if dd == -1 else ''
    diff = ''
    if show_diff and ref_off is not None:
        d = (off - ref_off) / 3600
        diff = ' · как у вас' if d == 0 else f" · {'+' if d > 0 else '−'}{abs(d):g} ч"
    return f"{mark(now)} <b>{now:%H:%M}</b> {WD[now.weekday()]} — {_esc(label)} <i>{fmt_off(off)}{diff}{day}</i>"


async def country_text(cc, title):
    my = dt.datetime.now(tz_of(await home()))
    ref = my.utcoffset().total_seconds()
    if cc == 'RU':
        zones = RU_ZONES
    else:
        zones = zonetab().get(cc, [])
    if not zones:
        return visual.err(f"Не знаю поясов страны «{_esc(title)}»")
    groups = {}
    for z, comment in zones:
        try:
            off = dt.datetime.now(ZoneInfo(z)).utcoffset().total_seconds()
        except Exception:
            continue
        g = groups.setdefault(off, {'zones': [], 'notes': []})
        g['zones'].append(z)
        if comment:
            g['notes'].append(comment)
    lines = [visual.title(title, sub=f"часовых поясов: {len(groups)}")]
    for off in sorted(groups):
        g = groups[off]
        now = dt.datetime.now(ZoneInfo(g['zones'][0]))
        d = (off - ref) / 3600
        diff = 'как у вас' if d == 0 else f"{'+' if d > 0 else '−'}{abs(d):g} ч от вас"
        lines.append(f"{mark(now)} <b>{now:%H:%M}</b> {WD[now.weekday()]} · <i>{fmt_off(off)}, {diff}</i>")
        if cc == 'RU':
            notes = g['notes']
        else:
            cities = sorted({city_of(z) for z in g['zones']})
            notes = [", ".join(cities[:8]) + (f" и ещё {len(cities) - 8}" if len(cities) > 8 else "")]
            extra = sorted({n for n in g['notes'] if n})[:3]
            if extra:
                notes.append("; ".join(extra))
        for n in notes:
            lines.append(f"   ▹ {_esc(n)}")
    return "\n".join(lines)


async def mine_text():
    hz = await home()
    my = dt.datetime.now(tz_of(hz))
    ref = my.utcoffset().total_seconds()
    places = await db.get('places', []) or []
    lines = [visual.title("Время", sub=f"у вас {my:%H:%M}, {WD[my.weekday()]} {my:%d.%m}"),
             line("у вас" + ("" if hz == 'local' else f" ({city_of(hz)})"), hz, my, ref, show_diff=False)]
    for p in places:
        try:
            lines.append(line(p['label'], p['tz'], my, ref))
        except Exception:
            lines.append(f"? — {_esc(p['label'])}")
    if not places:
        lines.append(visual.hint("добавьте места: .time add Токио · .time add США не получится — только место"))
    return "\n".join(lines)


def parse_hm(s):
    m = re.fullmatch(r'(\d{1,2})[:.](\d{2})', s)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


async def convert_text(hm, src_q, dst_q):
    src = await resolve(src_q)
    if src is None or src[0] != 'zone':
        return visual.err(f"Не нашёл место «{_esc(src_q)}» (нужен город или пояс, не страна)")
    tz = tz_of(src[1])
    today = dt.datetime.now(tz)
    moment = today.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    lines = [visual.title(f"{hm[0]:02}:{hm[1]:02}", sub=f"по времени: {src[2]}")]
    targets = []
    if dst_q:
        for q in [x.strip() for x in dst_q.split(',') if x.strip()]:
            r = await resolve(q)
            if r and r[0] == 'zone':
                targets.append((r[2], r[1]))
    else:
        hz = await home()
        targets.append(("у вас", hz))
        targets += [(p['label'], p['tz']) for p in await db.get('places', []) or []]
    for label, spec in targets:
        t = moment.astimezone(tz_of(spec))
        dd = (t.date() - moment.date()).days
        day = ' (завтра)' if dd == 1 else ' (вчера)' if dd == -1 else ''
        lines.append(f"{mark(t)} <b>{t:%H:%M}</b>{day} — {_esc(label)} <i>{fmt_off(t.utcoffset().total_seconds())}</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- команды

@decorators.command('time', 'tz', 'время')
async def time_cmd(event):
    """время: [место | страна | UTC±N]; 15:30 <место> [> куда]; diff A, B; add · del · list · home"""
    raw = engine.get_args().strip()
    sub, _, rest = raw.partition(' ')
    low = sub.lower()
    if not raw or low in ('list', 'список'):
        await engine.answer(event, await mine_text())
        return
    if low in ('add', '+'):
        r = await resolve(rest)
        if r is None or r[0] != 'zone':
            await engine.answer(event, visual.err("Нужен город, регион или UTC±N (страну целиком — .time <страна>)"))
            return
        places = await db.get('places', []) or []
        places.append({'label': r[2], 'tz': r[1]})
        await db.set('places', places[:25])
        await engine.answer(event, await mine_text())
        return
    if low in ('del', 'rm', '-'):
        places = await db.get('places', []) or []
        if rest.isdigit() and 1 <= int(rest) <= len(places):
            places.pop(int(rest) - 1)
        else:
            places = [p for p in places if p['label'].lower() != rest.lower()]
        await db.set('places', places)
        await engine.answer(event, await mine_text())
        return
    if low in ('home', 'дом', 'me'):
        if rest.lower() in ('', 'local', 'компьютер'):
            await db.set('home', None)
        else:
            r = await resolve(rest)
            if r is None or r[0] != 'zone':
                await engine.answer(event, visual.err("Не нашёл такое место"))
                return
            await db.set('home', r[1])
        await engine.answer(event, await mine_text())
        return
    if low in ('diff', 'разница'):
        parts = [x.strip() for x in re.split(r',|\s+и\s+|>', rest) if x.strip()]
        if len(parts) == 1 and ' ' in parts[0]:
            parts = parts[0].split()
        if len(parts) < 2:
            await engine.answer(event, visual.err("Формат: .time diff Москва, Токио"))
            return
        rs = [await resolve(p) for p in parts[:2]]
        if any(r is None or r[0] != 'zone' for r in rs):
            await engine.answer(event, visual.err("Нужны два места (города или пояса)"))
            return
        a, b = (dt.datetime.now(tz_of(r[1])) for r in rs)
        d = (b.utcoffset() - a.utcoffset()).total_seconds() / 3600
        await engine.answer(event, "\n".join([
            visual.title("Разница", sub=f"{abs(d):g} ч"), line(rs[0][2], rs[0][1], show_diff=False),
            line(rs[1][2], rs[1][1], show_diff=False), "",
            visual.hint(f"в «{rs[1][2]}» на {abs(d):g} ч {'больше' if d > 0 else 'меньше'}" if d else "время одинаковое")]))
        return
    hm = parse_hm(sub)
    if hm:
        src_q, _, dst_q = rest.partition('>')
        if not src_q.strip():
            await engine.answer(event, visual.err("Формат: .time 15:30 Москва [> Токио, Лондон]"))
            return
        await engine.answer(event, await convert_text(hm, src_q.strip(), dst_q.strip()))
        return
    r = await resolve(raw)
    if r is None:
        await engine.answer(event, visual.err(f"Не нашёл «{_esc(raw)}»"))
        return
    if r[0] == 'country':
        await engine.answer(event, await country_text(r[1], r[2]))
        return
    my = dt.datetime.now(tz_of(await home()))
    await engine.answer(event, "\n".join([visual.title("Время", sub=_esc(r[2])),
                                          line(r[2], r[1], my, my.utcoffset().total_seconds())]))


@bot.inline_query('time', 'tz', access='public')
async def time_inline(query):
    """время в городе, регионе или стране: <место>"""
    q = (query.text or '').strip()
    if not q:
        return [engine.InlineResult("Напишите город или страну", description="например: Токио, Якутия, США",
                                    text="🕐 Время в любом городе: @бот time <место>")]
    r = await resolve(q)
    if r is None:
        return [engine.InlineResult("Не нашёл", description=q, text=f"🕐 Не нашёл «{_esc(q)}»")]
    if r[0] == 'country':
        text = await country_text(r[1], r[2])
        return [engine.InlineResult(f"{r[2]}: все часовые пояса", description="отправить список", text=text)]
    now = dt.datetime.now(tz_of(r[1]))
    return [engine.InlineResult(f"{now:%H:%M} — {r[2]}", description=f"{WD[now.weekday()]}, {now:%d.%m} · "
                                f"{fmt_off(now.utcoffset().total_seconds())}",
                                text=f"🕐 В {_esc(r[2])} сейчас <b>{now:%H:%M}</b> ({WD[now.weekday()]}, {now:%d.%m}, "
                                     f"{fmt_off(now.utcoffset().total_seconds())})")]
