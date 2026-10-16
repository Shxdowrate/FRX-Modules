"""
QR & Links — QR-коды и ссылки.

  .qr <текст или ссылка>   сделать QR-код (или ответом на сообщение — из его текста)
  .qr                      ответом на фото с QR-кодом — прочитать его
  .short <ссылка>          сократить ссылку (is.gd, запасной — clck.ru)
  .unshort <ссылка>        куда на самом деле ведёт короткая ссылка (вся цепочка переходов)
"""

import html
import io
import re
import urllib.parse

import aiohttp

import decorators
import engine
import visual

NAME = "QR & Links"
MODULE_NAME = "qrlinks"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "QR-коды (создать и прочитать) и ссылки (сократить и раскрыть): .qr, .short, .unshort"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/qrlinks.py"

URL_RE = re.compile(r'https?://\S+')
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36'}


def _esc(x):
    return html.escape(str(x), quote=False)


async def _text_arg(event):
    t = engine.get_args().strip()
    if not t and event.is_reply:
        r = await event.get_reply_message()
        t = (r.raw_text or '').strip() if r else ''
    return t


@decorators.command('qr')
async def qr_cmd(event):
    """QR-код: <текст> — сделать; ответом на фото с QR — прочитать"""
    r = await event.get_reply_message() if event.is_reply else None
    if r is not None and r.photo and not engine.get_args().strip():
        await engine.answer(event, visual.wait("Читаю QR…"))
        img = await r.download_media(file=bytes)
        form = aiohttp.FormData()
        form.add_field('file', img, filename='qr.jpg', content_type='image/jpeg')
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.post('https://api.qrserver.com/v1/read-qr-code/', data=form) as resp:
                    data = await resp.json(content_type=None)
            sym = (data[0].get('symbol') or [{}])[0]
            if sym.get('error') or not sym.get('data'):
                raise RuntimeError(sym.get('error') or 'QR-код не найден')
            await engine.answer(event, visual.ok("В QR-коде:") + f"\n<code>{_esc(sym['data'])}</code>")
        except Exception as e:
            await engine.answer(event, visual.err("Не прочитал") + f"\n<code>{_esc(e)}</code>")
        return
    text = await _text_arg(event)
    if not text:
        await engine.answer(event, visual.err("Что закодировать?") + "\n" + visual.hint(".qr https://… · ответом на фото — прочитать"))
        return
    url = 'https://api.qrserver.com/v1/create-qr-code/?' + urllib.parse.urlencode(
        {'data': text[:1500], 'size': '640x640', 'margin': 24, 'ecc': 'M', 'format': 'png'})
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.get(url) as resp:
            png = await resp.read()
    f = io.BytesIO(png)
    f.name = 'qr.png'
    await engine.client.send_file(event.chat_id, f, caption=f"<code>{_esc(text[:900])}</code>", parse_mode='html',
                                  reply_to=event.reply_to_msg_id)
    await event.delete()


@decorators.command('short', 'shorten')
async def short_cmd(event):
    """сократить ссылку: <ссылка>"""
    m = URL_RE.search(await _text_arg(event))
    if not m:
        await engine.answer(event, visual.err("Нужна ссылка"))
        return
    url = m.group(0)
    short = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        for api in ('https://is.gd/create.php?format=simple&url=', 'https://clck.ru/--?url='):
            try:
                async with s.get(api + urllib.parse.quote(url, safe='')) as resp:
                    t = (await resp.text()).strip()
                    if resp.status == 200 and t.startswith('http'):
                        short = t
                        break
            except Exception:
                continue
    if not short:
        await engine.answer(event, visual.err("Сервисы сокращения недоступны"))
        return
    await engine.answer(event, visual.ok("Короткая ссылка") + f"\n{_esc(short)}\n" + visual.hint(_esc(url[:200])))


@decorators.command('unshort', 'expand')
async def unshort_cmd(event):
    """куда ведёт ссылка: <короткая ссылка>"""
    m = URL_RE.search(await _text_arg(event))
    if not m:
        await engine.answer(event, visual.err("Нужна ссылка"))
        return
    chain, url = [m.group(0)], m.group(0)
    async with aiohttp.ClientSession(headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as s:
        for _ in range(10):
            try:
                async with s.head(url, allow_redirects=False) as resp:
                    loc = resp.headers.get('Location')
                    if resp.status in (405, 403) and not loc:
                        async with s.get(url, allow_redirects=False) as r2:
                            loc = r2.headers.get('Location')
            except Exception:
                break
            if not loc:
                break
            url = urllib.parse.urljoin(url, loc)
            chain.append(url)
    lines = [visual.title("Цепочка переходов", sub=f"{len(chain) - 1} переход(ов)")]
    for i, u in enumerate(chain):
        lines.append(f"{'🏁' if i == len(chain) - 1 else '↳'} <code>{_esc(u[:300])}</code>")
    await engine.answer(event, "\n".join(lines))
