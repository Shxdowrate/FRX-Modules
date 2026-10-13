"""
Currency — курсы и конвертер: любые валюты мира и криптовалюты (без ключей).

  .cur 100 usd rub        конвертация (можно по-русски: .cur 100 долларов в рубли)
  .cur 100 usd            в ваши любимые валюты сразу (настройка favorites)
  .cur btc                курс криптовалюты
  @бот cur 100 usd rub    в любом чате

Под ответом — кнопки: ⇄ поменять местами, ×10 / ÷10, другие валюты из избранного, обновить.
Курсы валют — open.er-api.com (ежедневно), крипта — Binance / Coinbase / CoinGecko (в реальном времени, без ключей).
"""

import html
import re
import time

import aiohttp

import bot
import decorators
import engine
import visual

NAME = "Currency"
MODULE_NAME = "currency"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Курсы и конвертер валют и крипты: .cur 100 usd rub, .cur btc, @бот cur …"

ALIASES = {
    'доллар': 'USD', 'доллара': 'USD', 'долларов': 'USD', 'доллары': 'USD', 'бакс': 'USD', 'баксов': 'USD', '$': 'USD',
    'евро': 'EUR', '€': 'EUR', 'рубль': 'RUB', 'рубля': 'RUB', 'рублей': 'RUB', 'рубли': 'RUB', 'руб': 'RUB', '₽': 'RUB',
    'юань': 'CNY', 'юаня': 'CNY', 'юаней': 'CNY', 'юани': 'CNY', 'фунт': 'GBP', 'фунта': 'GBP', 'фунтов': 'GBP', '£': 'GBP',
    'иена': 'JPY', 'иен': 'JPY', 'йена': 'JPY', '¥': 'JPY', 'гривна': 'UAH', 'гривны': 'UAH', 'гривен': 'UAH', '₴': 'UAH',
    'тенге': 'KZT', '₸': 'KZT', 'лира': 'TRY', 'лиры': 'TRY', 'лир': 'TRY', 'бел': 'BYN', 'белруб': 'BYN', 'сум': 'UZS', 'сумов': 'UZS',
    'лари': 'GEL', 'драм': 'AMD', 'драмов': 'AMD', 'франк': 'CHF', 'франков': 'CHF', 'вона': 'KRW', 'вон': 'KRW',
    'рупия': 'INR', 'рупий': 'INR', 'злотый': 'PLN', 'злотых': 'PLN', 'дирхам': 'AED', 'дирхамов': 'AED',
    'биткоин': 'BTC', 'биткоина': 'BTC', 'биткоинов': 'BTC', 'битков': 'BTC', 'эфир': 'ETH', 'эфира': 'ETH', 'тон': 'TON',
    'тонкоин': 'TON', 'usdt': 'USDT', 'тезер': 'USDT', 'солана': 'SOL', 'доги': 'DOGE', 'догикоин': 'DOGE',
}
SIGN = {'USD': '$', 'EUR': '€', 'RUB': '₽', 'GBP': '£', 'JPY': '¥', 'CNY': '¥', 'UAH': '₴', 'KZT': '₸', 'TRY': '₺', 'INR': '₹',
        'KRW': '₩', 'BTC': '₿', 'ETH': 'Ξ'}
_fiat = {'t': 0, 'rates': {}}


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='favorites', description='Любимые валюты — в них конвертируется, если цель не указана',
                               data_type='list', default=['RUB', 'USD', 'EUR', 'CNY', 'BTC', 'USDT'])


async def _get(url, params=None):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url, params=params, headers={'User-Agent': 'FRX-Currency'}) as r:
            r.raise_for_status()
            return await r.json(content_type=None)


async def fiat_rates():
    """Курсы фиата к USD (кэш на час)."""
    if time.time() - _fiat['t'] > 3600 or not _fiat['rates']:
        data = await _get('https://open.er-api.com/v6/latest/USD')
        if data.get('result') == 'success':
            _fiat.update(t=time.time(), rates=data['rates'])
    return _fiat['rates']


_crypto = {}       # SYM -> (время, цена в USD)
STABLE = {'USDT', 'USDC', 'DAI', 'FDUSD', 'TUSD', 'BUSD', 'USDE'}


async def _binance(sym):
    data = await _get('https://api.binance.com/api/v3/ticker/price', {'symbol': f'{sym}USDT'})
    return float(data['price'])


async def _coinbase(sym):
    data = await _get('https://api.coinbase.com/v2/exchange-rates', {'currency': sym})
    return float(data['data']['rates']['USD'])


async def _coingecko(sym):
    found = await _get('https://api.coingecko.com/api/v3/search', {'query': sym})
    coin = next((c for c in found.get('coins') or [] if (c.get('symbol') or '').upper() == sym), None)
    if not coin:
        raise ValueError(sym)
    data = await _get('https://api.coingecko.com/api/v3/simple/price', {'ids': coin['id'], 'vs_currencies': 'usd'})
    return float(data[coin['id']]['usd'])


async def crypto_usd(sym):
    """Цена криптовалюты в USD (кэш на минуту): Binance → Coinbase → CoinGecko, все без ключей."""
    hit = _crypto.get(sym)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    sources = (_coinbase, _coingecko) if sym in STABLE else (_binance, _coinbase, _coingecko)
    for src in sources:
        try:
            price = await src(sym)
            if price > 0:
                _crypto[sym] = (time.time(), price)
                return price
        except Exception:
            continue
    raise ValueError(f"не знаю валюту {sym}")


async def usd_value(cur):
    """Сколько долларов стоит 1 единица валюты."""
    rates = await fiat_rates()
    if cur in rates and rates[cur]:
        return 1 / rates[cur]
    return await crypto_usd(cur)


async def convert(amount, src, dst):
    if src == dst:
        return amount
    return amount * await usd_value(src) / await usd_value(dst)


def code(word):
    w = (word or '').strip().lower().strip('.,')
    if w in ALIASES:
        return ALIASES[w]
    if re.fullmatch(r'[a-z0-9]{2,10}', w):
        return w.upper()
    return None


def parse(text):
    """'100 usd rub' / '100 долларов в рубли' / 'btc' → (amount, src, [dst…] | None)."""
    t = text.lower().replace(',', '.')
    t = re.sub(r'\b(в|to|in|на)\b', ' ', t)
    t = re.sub(r'(\d)\s*(k|к|тыс|m|млн)(?![a-zа-я])', r'\1\2 ', t)                 # 5к, 2m, 3 тыс
    t = re.sub(r'(\d)(?!(?:k|к|тыс|m|млн)\b)([^\d\s.kкmм])', r'\1 \2', t)
    parts = t.split()
    amount = 1.0
    if parts and re.fullmatch(r'\d+(?:\.\d+)?(?:[kкm]|тыс|млн)?', parts[0]):
        num = parts.pop(0)
        mult = 1000 if num.endswith(('k', 'к', 'тыс')) else 1_000_000 if num.endswith(('m', 'млн')) else 1
        amount = float(re.sub(r'[^\d.]', '', num)) * mult
    codes = [c for c in (code(p) for p in parts) if c]
    if not codes:
        return None
    return amount, codes[0], codes[1:] or None


def fmt(v, cur):
    if v >= 1:
        s = f"{v:,.2f}".replace(',', ' ')
        s = s[:-3] if s.endswith('.00') else s
    elif v == 0:
        s = "0"
    else:
        s = f"{v:.8f}".rstrip('0').rstrip('.')
    sign = SIGN.get(cur) if cur in ('USD', 'EUR', 'RUB', 'GBP', 'UAH', 'KZT', 'TRY', 'INR', 'KRW', 'JPY') else None
    return f"{s} {sign}" if sign else f"{s} {cur}"


async def render(amount, src, dsts):
    lines = [f"💱 <b>{fmt(amount, src)}</b>", ""]
    for d in dsts:
        if d == src:
            continue
        try:
            v = await convert(amount, src, d)
            lines.append(visual.item(f"<code>{fmt(v, d)}</code>"))
        except Exception as e:
            lines.append(visual.item(f"{d}: <i>{_esc(e)}</i>"))
    if len(dsts) == 1 and dsts[0] != src:
        try:
            one = await convert(1, src, dsts[0])
            back = await convert(1, dsts[0], src)
            lines += ["", visual.hint(f"1 {src} = {fmt(one, dsts[0])} · 1 {dsts[0]} = {fmt(back, src)}")]
        except Exception:
            pass
    lines.append(visual.hint(time.strftime('курс на %d.%m %H:%M')))
    return "\n".join(lines)


@decorators.command('cur', 'currency', 'курс')
async def cur_cmd(event):
    """курс / конвертация: [сумма] <валюта> [в валюту] — 100 usd rub, btc, 5к рублей в тенге"""
    p = parse(engine.get_args())
    if not p:
        await engine.answer(event, visual.err("Не понял") + "\n" + visual.hint(".cur 100 usd rub · .cur btc · .cur 100 долларов в рубли"))
        return
    st = {'amount': p[0], 'src': p[1], 'dst': p[2]}
    favs = [str(x).upper() for x in await engine.get_config('favorites')]

    async def text():
        return await render(st['amount'], st['src'], st['dst'] or [f for f in favs if f != st['src']])

    def buttons():
        rows = []
        if st['dst'] and len(st['dst']) == 1:
            rows.append([("⇄", "cu:swap"), ("×10", "cu:x10"), ("÷10", "cu:d10")])
        quick = [f for f in favs if f != st['src'] and not (st['dst'] and f in st['dst'])][:4]
        if quick:
            rows.append([(f"→ {f}", f"cu:to:{f}") for f in quick])
        rows.append([("🔄 Обновить", "cu:refresh"), (visual.btn('Закрыть', 'close'), "cu:close")])
        return rows
    try:
        body = await text()
    except Exception as e:
        await engine.answer(event, visual.err("Курсы недоступны") + f"\n<code>{_esc(e)}</code>")
        return
    try:
        form = await engine.create_inlineform(event.chat_id, body, buttons=buttons())
    except Exception:
        await engine.answer(event, body)
        return
    await engine.answer(event, visual.ok("Курс") + "  " + visual.hint("ниже"))

    @form.on_click(data_startswith='cu:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
            return
        if d == 'swap':
            st['src'], st['dst'] = st['dst'][0], [st['src']]
        elif d == 'x10':
            st['amount'] *= 10
        elif d == 'd10':
            st['amount'] /= 10
        elif d.startswith('to:'):
            st['dst'] = [d[3:]]
        elif d == 'refresh':
            _fiat['t'] = 0
            await click.answer("обновлено")
        await form.edit(await text(), buttons=buttons())


@bot.inline_query('cur', 'курс', 'currency', access='public')
async def cur_inline(query):
    """курс: [сумма] валюта [в валюту]"""
    p = parse(query.text)
    if not p:
        return [engine.InlineResult("Курс: cur 100 usd rub", text=visual.hint("<code>@бот cur 100 usd rub</code>"))]
    amount, src, dst = p
    favs = [str(x).upper() for x in await engine.get_config('favorites')]
    try:
        body = await render(amount, src, dst or [f for f in favs if f != src])
        first = dst[0] if dst else next(f for f in favs if f != src)
        v = await convert(amount, src, first)
    except Exception as e:
        return [engine.InlineResult("Курс недоступен", description=str(e)[:100], text=visual.err("Курс недоступен"))]
    return [engine.InlineResult(f"{fmt(amount, src)} = {fmt(v, first)}", description="нажмите, чтобы отправить", text=body)]
