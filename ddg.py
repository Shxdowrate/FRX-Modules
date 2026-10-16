"""
DDG — поиск DuckDuckGo (без ключей и аккаунтов).

  .ddg <запрос> / .search <запрос>   результаты списком: заголовок, сайт, кусочек текста; кнопки — открыть сайт,
                                     карточка с превью страницы, скриншот сайта, следующая страница
  @бот ddg <запрос>                  в любом чате: выбрали сайт — в чат уходит карточка с превью; под ней кнопки
                                     «открыть» и «📸 скрин» (скриншот страницы встаёт прямо в карточку)

Ищет пакет ddgs (ставится сам; при сбое DuckDuckGo берёт другие движки), запасной — разбор HTML DDG.
Скриншоты — бесплатный сервис WordPress mShots (первый снимок сайта готовится несколько секунд).
"""

import asyncio
import html
import re
import secrets
import sys
import time
import urllib.parse

import aiohttp

import bot
import cache
import decorators
import engine
import visual

NAME = "DDG"
MODULE_NAME = "ddg"
VERSION = "1.1"
AUTHOR = "FRX"
DESCRIPTION = "Поиск DuckDuckGo: .ddg запрос и @бот ddg запрос — сайты с превью и скриншотом страницы"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/ddg.py"
REQUIREMENTS = ["ddgs"]   # ставятся сами (core/requirements.py)

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
      'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8'}
PER_PAGE = 5


def _esc(x):
    return html.escape(str(x), quote=False)


def _strip(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s or '')).strip()


def _domain(url):
    return (urllib.parse.urlparse(url).hostname or '').removeprefix('www.')


def _real_url(href):
    href = html.unescape(href)
    if href.startswith('//'):
        href = 'https:' + href
    q = urllib.parse.urlparse(href)
    if 'duckduckgo.com' in (q.hostname or '') and q.path.startswith('/l/'):
        target = urllib.parse.parse_qs(q.query).get('uddg', [''])[0]
        return target or href
    return href


_dep_lock = asyncio.Lock()


async def _ddgs():
    """Пакет ddgs (поиск DuckDuckGo с «браузерным» отпечатком — простые запросы DDG часто режет капчей).
    Нет — ставится pip'ом один раз."""
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        pass
    async with _dep_lock:
        try:
            from ddgs import DDGS
            return DDGS
        except ImportError:
            proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'pip', 'install', '-U', 'ddgs',
                                                        '--disable-pip-version-check', stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            await proc.communicate()
            import importlib
            importlib.invalidate_caches()
            try:
                from ddgs import DDGS
                return DDGS
            except ImportError:
                return None


async def search(query, region=None):
    """[{title, url, snippet, domain}]: ddgs (DuckDuckGo, при сбое — другие движки), запасной — разбор HTML DDG."""
    region = region or (await engine.get_config('region')) or 'ru-ru'
    errors = []
    DDGS = await _ddgs()
    if DDGS is not None:
        def run(backend):
            return DDGS(timeout=10).text(query, region=region, safesearch='moderate', max_results=25, backend=backend)
        for backend in ('duckduckgo', 'auto'):
            try:
                rows = await asyncio.get_running_loop().run_in_executor(None, run, backend)
            except Exception as e:
                errors.append(f"{backend}: {e}")
                continue
            out = [{'title': r.get('title') or '', 'url': r.get('href') or '', 'snippet': r.get('body') or '',
                    'domain': _domain(r.get('href') or '')} for r in rows or [] if (r.get('href') or '').startswith('http')]
            if out:
                return out
    try:
        out = await _search_html(query, region)
    except Exception as e:
        errors.append(f"html: {e}")
        out = []
    if not out and errors:
        raise RuntimeError("; ".join(errors)[:300])
    return out


async def _search_html(query, region):
    """[{title, url, snippet, domain}] — HTML-версия DuckDuckGo, запасная — lite."""
    async with aiohttp.ClientSession(headers=UA, timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post('https://html.duckduckgo.com/html/', data={'q': query, 'kl': region}) as r:
            page = await r.text()
        out = []
        blocks = re.split(r'<div[^>]+class="[^"]*\bresult\b[^"]*"', page)[1:]
        for b in blocks:
            if 'result--ad' in b[:200]:
                continue
            a = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not a:
                continue
            sn = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|div|td)>', b, re.S)
            url = _real_url(a.group(1))
            if not url.startswith('http'):
                continue
            out.append({'title': _strip(a.group(2)), 'url': url, 'snippet': _strip(sn.group(1)) if sn else '', 'domain': _domain(url)})
        if out:
            return out
        async with s.post('https://lite.duckduckgo.com/lite/', data={'q': query, 'kl': region}) as r:
            page = await r.text()
        links = re.findall(r'<a[^>]+rel="nofollow"[^>]+href="([^"]+)"[^>]*class=.result-link.[^>]*>(.*?)</a>', page, re.S) or \
            re.findall(r'<a[^>]+href="([^"]+)"[^>]*class=.result-link.[^>]*>(.*?)</a>', page, re.S)
        snippets = re.findall(r'<td[^>]+class=.result-snippet.[^>]*>(.*?)</td>', page, re.S)
        for i, (href, title) in enumerate(links):
            url = _real_url(href)
            out.append({'title': _strip(title), 'url': url, 'snippet': _strip(snippets[i]) if i < len(snippets) else '', 'domain': _domain(url)})
        return out


def shot_url(url, w=1280, h=800):
    return f"https://s.wordpress.com/mshots/v1/{urllib.parse.quote(url, safe='')}?w={w}&h={h}"


async def warm_shot(url, wait=12.0):
    """mShots готовит снимок несколько секунд (до этого отдаёт заглушку) — опрашиваем, пока не будет готов."""
    deadline = time.monotonic() + wait
    async with aiohttp.ClientSession(headers=UA, timeout=aiohttp.ClientTimeout(total=10)) as s:
        while time.monotonic() < deadline:
            try:
                async with s.get(shot_url(url), allow_redirects=False) as r:
                    if r.status == 200 and 'image/jpeg' in r.headers.get('Content-Type', ''):
                        return True
            except Exception:
                pass
            await asyncio.sleep(1.5)
    return False


def card_text(r, shot=False):
    head = f'<a href="{_esc(shot_url(r["url"]))}">&#8203;</a>' if shot else ''
    return (f'{head}<b>{_esc(r["title"])}</b>\n{_esc(r["snippet"][:600])}\n\n'
            f'🔗 <a href="{_esc(r["url"])}">{_esc(r["domain"])}</a>')


def favicon(domain):
    return f"https://www.google.com/s2/favicons?domain={urllib.parse.quote(domain)}&sz=64"


@decorators.on_start()
async def setup():
    await engine.create_config(key='region', description='Регион поиска (ru-ru, us-en, wt-wt — без региона…)',
                               data_type='str', default='ru-ru')


# --------------------------------------------------------------------------- инлайн

@bot.inline_query('ddg', 'search', 'g', access='self')
async def ddg_inline(query):
    """поиск DuckDuckGo: <запрос>"""
    q = query.text.strip()
    if not q:
        return [engine.InlineResult("DuckDuckGo: напишите запрос", text=visual.hint("<code>@бот ddg запрос</code>"))]
    try:
        results = await search(q)
    except Exception as e:
        return [engine.InlineResult("Поиск не удался", description=str(e)[:100], text=visual.err("Поиск не удался"))]
    out = []
    for r in results[:20]:
        key = secrets.token_urlsafe(5)

        async def keep(_imid, key=key, r=r):         # кнопке «скрин» нужен адрес — запоминаем, когда отправили
            await cache.set(f's:{key}', r)
            asyncio.ensure_future(warm_shot(r['url']))
        out.append(engine.InlineResult(
            r['title'] or r['domain'], description=f"{r['domain']} — {r['snippet'][:150]}", thumb=favicon(r['domain']),
            text=card_text(r), link_preview=r['url'],
            buttons=[[("🔗 Открыть", "url:" + r['url']), ("📸 Скрин", f"ddg:s:{key}")]], on_chosen=keep))
    return out or [engine.InlineResult("Ничего не нашлось", text=visual.hint(f"По запросу «{_esc(q)}» ничего нет"))]


@bot.inline_click(data_startswith='ddg:s:')
async def ddg_shot(click):
    r = await cache.get(f's:{click.data[6:]}')
    if r is None:
        await click.answer("Карточка устарела — найдите заново", show_alert=True)
        return
    await click.answer("делаю скриншот…")
    await warm_shot(r['url'])
    await click.edit(card_text(r, shot=True), buttons=[[("🔗 Открыть", "url:" + r['url'])]], link_preview=shot_url(r['url']))


# --------------------------------------------------------------------------- команда

@decorators.command('ddg', 'search')
async def ddg_cmd(event):
    """поиск DuckDuckGo: <запрос>"""
    q = engine.get_args().strip()
    if not q and event.is_reply:
        r = await event.get_reply_message()
        q = (r.raw_text or '').strip() if r else ''
    if not q:
        await engine.answer(event, visual.err("Что ищем?") + "\n" + visual.hint(".ddg запрос"))
        return
    await engine.answer(event, visual.wait(f"Ищу «{_esc(q)}»…"))
    try:
        results = await search(q)
    except Exception as e:
        await engine.answer(event, visual.err("Поиск не удался") + f"\n<code>{_esc(e)}</code>")
        return
    if not results:
        await engine.answer(event, visual.hint(f"По запросу «{_esc(q)}» ничего не нашлось"))
        return
    pages = max(1, -(-len(results) // PER_PAGE))
    st = {'page': 0}

    def page_text():
        p = st['page']
        lines = [visual.title("DuckDuckGo", sub=f"«{_esc(q)}» · стр. {p + 1}/{pages}")]
        for i, r in enumerate(results[p * PER_PAGE:(p + 1) * PER_PAGE], p * PER_PAGE + 1):
            lines.append(f'<b>{i}.</b> <a href="{_esc(r["url"])}">{_esc(r["title"])}</a>\n    <i>{_esc(r["domain"])}</i> — {_esc(r["snippet"][:160])}')
        return "\n\n".join(lines)

    def page_buttons():
        p = st['page']
        rng = range(p * PER_PAGE, min(len(results), (p + 1) * PER_PAGE))
        rows = [[(str(i + 1), f"dd:o:{i}") for i in rng]]
        nav = []
        if p > 0:
            nav.append(("◂", "dd:p:-1"))
        if p < pages - 1:
            nav.append(("▸ ещё", "dd:p:1"))
        if nav:
            rows.append(nav)
        rows.append([(visual.btn('Закрыть', 'close'), "dd:close")])
        return rows

    try:
        form = await engine.create_inlineform(event.chat_id, page_text(), buttons=page_buttons())
    except Exception:
        await engine.answer(event, page_text())
        return
    await engine.answer(event, visual.ok("DuckDuckGo") + "  " + visual.hint("результаты ниже"))

    @form.on_click(data_startswith='dd:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
        elif d.startswith('p:'):
            st['page'] = max(0, min(pages - 1, st['page'] + int(d[2:])))
            await form.edit(page_text(), buttons=page_buttons())
        elif d == 'list':
            await form.edit(page_text(), buttons=page_buttons())
        elif d.startswith('o:') or d.startswith('s:'):
            i = int(d[2:])
            r = results[i]
            shot = d.startswith('s:')
            if shot:
                await click.answer("делаю скриншот…")
                await warm_shot(r['url'])
            else:
                asyncio.ensure_future(warm_shot(r['url'], 20))
            await form.edit(card_text(r, shot=shot), buttons=[
                [("🔗 Открыть", "url:" + r['url'])] + ([] if shot else [("📸 Скрин", f"dd:s:{i}")]),
                [(visual.btn('К результатам', 'back'), "dd:list")]])
