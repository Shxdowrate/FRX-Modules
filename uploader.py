"""
Uploader — выложить файл на файлообменник и получить прямую ссылку. Ответьте на любой файл (фото, видео,
документ, голосовое…) командой — модуль загрузит его на выбранный сервис и пришлёт прямую ссылку, по которой
файл открывается/скачивается напрямую (её можно вставлять в HTML, Markdown, на форум).

  .upload (.ul)        ответом на файл: загрузить на сервис по умолчанию
  .upload <сервис>     ответом на файл: на конкретный сервис (catbox, 0x0, uguu, litterbox, tmpfiles)
  .upload here <текст> выложить текст как файл (или ответом на текстовое сообщение)
  .uploaders           список сервисов: что умеют, срок хранения, лимит

Сервисы (бесплатные, без регистрации):
  • catbox   — постоянно, до 200 МБ, прямая ссылка;
  • litterbox — временно (1 ч … 3 дня), до 1 ГБ;
  • 0x0      — долго (зависит от размера), до 512 МБ;
  • uguu     — 48 часов, до 128 МБ;
  • tmpfiles — 1 час, до 100 МБ.
Доступность сервисов и лимиты сервисы меняют на своей стороне; если один не ответил — попробуйте другой.
Выкладывайте только то, что имеете право распространять.
"""

import asyncio
import html
import io
import os
import time

import aiohttp

import decorators
import engine
import visual

NAME = "Uploader"
MODULE_NAME = "uploader"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Файл ответом → на файлообменник, в ответ прямая ссылка (catbox, 0x0, uguu, litterbox, tmpfiles)"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/uploader.py"

UA = {'User-Agent': 'Mozilla/5.0 (FRX Uploader)'}

# name -> (заголовок, срок, лимит МБ, функция загрузки)
SERVICES_META = {
    'catbox':    ('Catbox', 'постоянно', 200),
    'litterbox': ('Litterbox', '1 ч – 3 дня', 1024),
    '0x0':       ('0x0.st', 'от месяца до года (меньше файл — дольше)', 512),
    'uguu':      ('uguu.se', '48 часов', 128),
    'tmpfiles':  ('tmpfiles.org', '1 час', 100),
}


def _esc(x):
    return html.escape(str(x), quote=False)


def _mb(n):
    return f"{n / 1048576:.1f} МБ"


@decorators.on_start()
async def setup():
    await engine.create_config('service', 'Сервис по умолчанию', 'choice', 'catbox',
                               options=list(SERVICES_META))
    await engine.create_config('litterbox_time', 'Срок хранения на Litterbox', 'choice', '24h',
                               options=['1h', '12h', '24h', '72h'])
    await engine.create_config('max_mb', 'Не загружать файлы крупнее, МБ (0 — без ограничения)', 'int', 200,
                               valid={'min_value': 0, 'max_value': 2048})


# --------------------------------------------------------------------------- загрузчики

async def _post(session, url, data, parse_text=True):
    async with session.post(url, data=data, headers=UA, timeout=aiohttp.ClientTimeout(total=600)) as resp:
        text = (await resp.text()).strip()
        if resp.status != 200:
            raise RuntimeError(f"{url}: HTTP {resp.status} {text[:150]}")
        return text


async def up_catbox(session, content, filename):
    form = aiohttp.FormData()
    form.add_field('reqtype', 'fileupload')
    form.add_field('fileToUpload', content, filename=filename)
    link = await _post(session, 'https://catbox.moe/user/api.php', form)
    if not link.startswith('http'):
        raise RuntimeError(f"catbox: {link[:150]}")
    return link


async def up_litterbox(session, content, filename):
    form = aiohttp.FormData()
    form.add_field('reqtype', 'fileupload')
    form.add_field('time', await engine.get_config('litterbox_time'))
    form.add_field('fileToUpload', content, filename=filename)
    link = await _post(session, 'https://litterbox.catbox.moe/resources/internals/api.php', form)
    if not link.startswith('http'):
        raise RuntimeError(f"litterbox: {link[:150]}")
    return link


async def up_0x0(session, content, filename):
    form = aiohttp.FormData()
    form.add_field('file', content, filename=filename)
    link = await _post(session, 'https://0x0.st', form)
    if not link.startswith('http'):
        raise RuntimeError(f"0x0: {link[:150]}")
    return link


async def up_uguu(session, content, filename):
    form = aiohttp.FormData()
    form.add_field('files[]', content, filename=filename)
    text = await _post(session, 'https://uguu.se/upload?output=text', form)
    link = text.split()[0] if text else ''
    if not link.startswith('http'):
        import json
        try:
            data = json.loads(text)
            link = data['files'][0]['url']
        except Exception:
            raise RuntimeError(f"uguu: {text[:150]}")
    return link


async def up_tmpfiles(session, content, filename):
    import json
    form = aiohttp.FormData()
    form.add_field('file', content, filename=filename)
    text = await _post(session, 'https://tmpfiles.org/api/v1/upload', form)
    try:
        url = json.loads(text)['data']['url']
    except Exception:
        raise RuntimeError(f"tmpfiles: {text[:150]}")
    return url.replace('tmpfiles.org/', 'tmpfiles.org/dl/', 1)      # прямая ссылка на файл


UPLOADERS = {'catbox': up_catbox, 'litterbox': up_litterbox, '0x0': up_0x0, 'uguu': up_uguu, 'tmpfiles': up_tmpfiles}


async def upload(service, content, filename):
    async with aiohttp.ClientSession() as session:
        return await UPLOADERS[service](session, content, filename)


# --------------------------------------------------------------------------- команды

def _service_arg(a):
    a = a.lower()
    aliases = {'cb': 'catbox', 'cat': 'catbox', 'lb': 'litterbox', 'litter': 'litterbox',
               '0x0.st': '0x0', 'ugu': 'uguu', 'tmp': 'tmpfiles', 'tmpfile': 'tmpfiles'}
    a = aliases.get(a, a)
    return a if a in UPLOADERS else None


@decorators.command('upload', 'ul', 'залить')
async def upload_cmd(event):
    """выложить файл на файлообменник и получить прямую ссылку: ответом на файл [сервис]; here <текст> — текст файлом"""
    arg = engine.get_args().strip()
    service = await engine.get_config('service')

    # .upload here <текст> — выложить текст
    parts = arg.split(maxsplit=1)
    if parts and parts[0].lower() in ('here', 'text', 'txt', 'текст'):
        body = parts[1] if len(parts) > 1 else ''
        if not body and event.is_reply:
            r = await event.get_reply_message()
            body = (r.raw_text or '') if r else ''
        if not body:
            await engine.answer(event, visual.err("Что выкладывать? .upload here <текст> или ответом на сообщение"))
            return
        await _do_upload(event, service, body.encode('utf-8'), f'text_{int(time.time())}.txt', len(body.encode('utf-8')))
        return

    if parts and _service_arg(parts[0]):
        service = _service_arg(parts[0])

    if not event.is_reply:
        await engine.answer(event, visual.err("Ответьте этой командой на файл") + "\n"
                            + visual.hint(".upload — на сервис по умолчанию · .upload catbox · .upload here <текст>"))
        return
    reply = await event.get_reply_message()
    if reply is None or reply.file is None:
        if reply is not None and reply.raw_text:
            await _do_upload(event, service, reply.raw_text.encode('utf-8'), f'text_{reply.id}.txt',
                             len(reply.raw_text.encode('utf-8')))
            return
        await engine.answer(event, visual.err("В ответе нет файла"))
        return

    size = reply.file.size or 0
    limit_mb = await engine.get_config('max_mb')
    if limit_mb and size > limit_mb * 1048576:
        await engine.answer(event, visual.err(f"Файл {_mb(size)} больше лимита {limit_mb} МБ") + "\n"
                            + visual.hint("поднимите .cfg uploader max_mb"))
        return
    svc_limit = SERVICES_META[service][2]
    if size > svc_limit * 1048576:
        await engine.answer(event, visual.err(f"{SERVICES_META[service][0]}: файл {_mb(size)} больше лимита {svc_limit} МБ")
                            + "\n" + visual.hint("выберите другой сервис: .upload catbox · litterbox · 0x0"))
        return

    name = reply.file.name or (f"file_{reply.id}" + (reply.file.ext or ''))
    await engine.answer(event, visual.wait(f"Скачиваю из Telegram · {_mb(size)}…"))
    data = io.BytesIO()
    try:
        await engine.client.download_media(reply, file=data)
    except Exception as e:
        await engine.answer(event, visual.err("Не скачать файл") + f"\n<code>{_esc(str(e)[:200])}</code>")
        return
    await _do_upload(event, service, data.getvalue(), name, size or data.tell())


async def _do_upload(event, service, content, filename, size):
    title, term, _lim = SERVICES_META[service]
    await engine.answer(event, visual.wait(f"Загружаю на {title} · {_mb(size)}…"))
    t0 = time.monotonic()
    try:
        link = await upload(service, content, filename)
    except Exception as e:
        await engine.answer(event, visual.err(f"{title} не принял файл") + f"\n<code>{_esc(str(e)[:250])}</code>"
                            + "\n" + visual.hint("попробуйте другой сервис: .upload catbox · 0x0 · uguu"))
        return
    out = [visual.ok("Готово") + f"  {title} · {visual.hint(f'{time.monotonic() - t0:.1f} с')}",
           visual.field("прямая ссылка", f'<a href="{_esc(link)}">{_esc(link)}</a>', code=False),
           f"<code>{_esc(link)}</code>",
           visual.hint(f"{_esc(filename)} · {_mb(size)} · хранится {term}")]
    await engine.answer(event, "\n".join(out), )


@decorators.command('uploaders')
async def uploaders_cmd(event):
    """список файлообменников: что умеют, срок, лимит"""
    cur = await engine.get_config('service')
    lines = [visual.title("Uploader", sub="файлообменники")]
    for name, (title, term, lim) in SERVICES_META.items():
        mark = "◉" if name == cur else "○"
        lines.append(f"{mark} <b>{_esc(title)}</b>  <code>{name}</code>")
        lines.append(f"   <i>хранится {term} · до {lim} МБ</i>")
    lines.append("")
    lines.append(visual.hint("по умолчанию — .cfg uploader service · разовый выбор — .upload <сервис> ответом на файл"))
    await engine.answer(event, "\n".join(lines))
