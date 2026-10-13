"""
VirusTotal — проверить файл, ссылку, домен, IP или хеш на 70+ антивирусах.

  .vt                ответом на файл — проверить (сначала ищется по хешу, нового файла нет в базе — загружается)
  .vt <ссылка>       проверить ссылку;  .vt <домен> / <IP> — репутация;  .vt <md5|sha1|sha256> — отчёт по хешу
  .vt rescan         ответом на файл — заново прогнать по антивирусам (если отчёт старый)
  .vt auto on|off    сам проверять по хешу присланные вам в личку программы и архивы (.exe .apk .msi .bat .js .zip…)
                     и предупреждать, если антивирусы считают файл опасным

Нужен бесплатный ключ API: зарегистрируйтесь на virustotal.com → профиль → API Key, потом .cfg virustotal api_key.
Бесплатный ключ: 4 запроса в минуту, 500 в день — модуль сам ждёт между запросами. Файл до 650 МБ.
Учтите: загруженный файл становится доступен исследователям VirusTotal — не отправляйте личные документы.
"""

import asyncio
import base64
import hashlib
import html
import ipaddress
import re
import time
from datetime import datetime
from pathlib import Path

import aiohttp

import decorators
import engine
import visual

NAME = "VirusTotal"
MODULE_NAME = "virustotal"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Проверка файлов, ссылок, доменов, IP и хешей на VirusTotal; автопроверка программ в личке"

API = 'https://www.virustotal.com/api/v3'
GUI = 'https://www.virustotal.com/gui'
RISKY_EXT = {'.exe', '.msi', '.bat', '.cmd', '.scr', '.com', '.pif', '.vbs', '.vbe', '.js', '.jse', '.wsf', '.ps1', '.hta',
             '.jar', '.apk', '.xapk', '.dll', '.lnk', '.iso', '.img', '.zip', '.rar', '.7z', '.docm', '.xlsm', '.pptm', '.sh', '.dmg', '.pkg'}
_LOCK = asyncio.Lock()
_LAST = [0.0]


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    c = engine.create_config
    await c(key='api_key', description='Ключ API VirusTotal (virustotal.com → профиль → API Key)', data_type='str', default='',
            valid={'max_len': 128})
    await c(key='auto', description='Сам проверять программы и архивы, присланные в личку (только по хешу)', data_type='bool',
            default=False)
    await c(key='rate', description='Пауза между запросами, сек. (бесплатный ключ — 4 в минуту, значит 15)', data_type='int',
            default=15, valid={'min_value': 0, 'max_value': 120})


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


class VTError(Exception):
    pass


async def vt(method, path, **kw):
    """Запрос к API с паузой между запросами (лимит бесплатного ключа)."""
    key = (await cfg('api_key', '') or '').strip()
    if not key:
        raise VTError("нет ключа API — virustotal.com → профиль → API Key, затем .cfg virustotal api_key")
    rate = int(await cfg('rate', 15))
    async with _LOCK:
        wait = _LAST[0] + rate - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST[0] = time.time()
    url = path if path.startswith('http') else API + path
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
        async with s.request(method, url, headers={'x-apikey': key, 'accept': 'application/json'}, **kw) as r:
            data = await r.json(content_type=None) if r.content_type and 'json' in r.content_type else None
            if r.status == 404:
                return None
            if r.status == 401:
                raise VTError("ключ API не подходит")
            if r.status == 429:
                raise VTError("лимит запросов исчерпан (4 в минуту / 500 в день) — попробуйте позже")
            if r.status >= 400:
                raise VTError(((data or {}).get('error') or {}).get('message') or f"ответ {r.status}")
            return data


async def wait_analysis(aid, on_tick=None):
    for i in range(40):
        d = await vt('GET', f'/analyses/{aid}')
        attrs = ((d or {}).get('data') or {}).get('attributes') or {}
        if attrs.get('status') == 'completed':
            return d
        if on_tick:
            await on_tick(i)
        await asyncio.sleep(8)
    raise VTError("VirusTotal долго анализирует — посмотрите отчёт по ссылке позже")


# --------------------------------------------------------------------------- отчёт

def verdict(stats):
    mal, sus = stats.get('malicious', 0), stats.get('suspicious', 0)
    total = sum(stats.get(k, 0) for k in ('malicious', 'suspicious', 'undetected', 'harmless'))
    if mal >= 3:
        return '🔴', 'опасно', mal, total
    if mal or sus:
        return '🟡', 'подозрительно', mal + sus, total
    return '🟢', 'чисто', 0, total


def size_h(n):
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == 'Б' else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def report_text(kind, title, attrs, extra=()):
    stats = attrs.get('last_analysis_stats') or attrs.get('stats') or {}
    icon, word, n, total = verdict(stats)
    results = attrs.get('last_analysis_results') or attrs.get('results') or {}
    bad = [(eng, r.get('result') or r.get('category')) for eng, r in results.items()
           if r.get('category') in ('malicious', 'suspicious')]
    lines = [f"{icon} <b>VirusTotal · {word}</b>", visual.field(kind, _esc(title)[:200], code=kind != 'файл'),
             visual.field("сработало", f"{n} из {total}" if total else "ещё нет данных")]
    lines += list(extra)
    rep = attrs.get('reputation')
    if rep is not None:
        lines.append(visual.field("репутация", rep))
    cats = attrs.get('categories')
    if cats:
        lines.append(visual.field("категории", _esc(", ".join(sorted(set(cats.values()))[:5])), code=False))
    when = attrs.get('last_analysis_date') or attrs.get('date')
    if when:
        lines.append(visual.field("проверено", datetime.fromtimestamp(when).strftime('%d.%m.%Y %H:%M')))
    if bad:
        lines.append(visual.section("Нашли"))
        for eng, res in sorted(bad)[:15]:
            lines.append(f"▸ {_esc(eng)}: <code>{_esc(res)}</code>")
        if len(bad) > 15:
            lines.append(visual.hint(f"и ещё {len(bad) - 15}"))
    return "\n".join(lines), icon


async def show(event, text, link):
    try:
        form = await engine.create_inlineform(event.chat_id, text, buttons=[[("↗ Полный отчёт", f"url:{link}")]])
        try:
            await event.delete()
        except Exception:
            pass
        await form.close(remove_buttons=False)
    except Exception:
        await engine.answer(event, text + f'\n\n<a href="{link}">Полный отчёт на VirusTotal</a>')


def file_extra(attrs):
    out = []
    if attrs.get('size'):
        out.append(visual.field("размер", size_h(attrs['size'])))
    if attrs.get('type_description'):
        out.append(visual.field("тип", _esc(attrs['type_description']), code=False))
    if attrs.get('first_submission_date'):
        out.append(visual.field("впервые", datetime.fromtimestamp(attrs['first_submission_date']).strftime('%d.%m.%Y')))
    label = (attrs.get('popular_threat_classification') or {}).get('suggested_threat_label')
    if label:
        out.append(visual.field("угроза", _esc(label)))
    if attrs.get('sha256'):
        out.append(visual.field("sha256", attrs['sha256']))
    return out


# --------------------------------------------------------------------------- проверки

async def scan_file(event, reply, rescan=False):
    doc = reply.file
    if doc is None:
        await engine.answer(event, visual.err("Ответьте на сообщение с файлом"))
        return
    size = doc.size or 0
    if size > 650 * 1024 * 1024:
        await engine.answer(event, visual.err("Больше 650 МБ VirusTotal не принимает"))
        return
    name = doc.name or f"file{doc.ext or ''}"
    await engine.answer(event, visual.wait(f"Скачиваю {_esc(name)} ({size_h(size)})…"))
    data = await engine.client.download_media(reply, file=bytes)
    sha = hashlib.sha256(data).hexdigest()
    await engine.answer(event, visual.wait("Ищу по хешу…"))
    d = await vt('GET', f'/files/{sha}')
    if d is not None and rescan:
        r = await vt('POST', f'/files/{sha}/analyse')
        aid = ((r or {}).get('data') or {}).get('id')
        if aid:
            await engine.answer(event, visual.wait("Перепроверяю на антивирусах…"))
            await wait_analysis(aid)
        d = await vt('GET', f'/files/{sha}')
    if d is None:
        await engine.answer(event, visual.wait("Файла нет в базе — загружаю на VirusTotal…"))
        form = aiohttp.FormData()
        form.add_field('file', data, filename=name)
        if size > 32 * 1024 * 1024:
            up = await vt('GET', '/files/upload_url')
            r = await vt('POST', up['data'], data=form)
        else:
            r = await vt('POST', '/files', data=form)
        aid = ((r or {}).get('data') or {}).get('id')

        async def tick(i):
            await engine.answer(event, visual.wait("Антивирусы проверяют файл" + "." * (i % 3 + 1)))
        await wait_analysis(aid, tick)
        await asyncio.sleep(3)
        d = await vt('GET', f'/files/{sha}')
        if d is None:
            raise VTError("анализ готов, но отчёт ещё не появился — повторите .vt через минуту")
    attrs = d['data']['attributes']
    text, _ = report_text('файл', name, attrs, file_extra(attrs))
    await show(event, text, f"{GUI}/file/{sha}")


def url_id(url):
    return base64.urlsafe_b64encode(url.encode()).decode().strip('=')


async def scan_url(event, url):
    if not re.match(r'^[a-z]+://', url, re.I):
        url = 'http://' + url
    await engine.answer(event, visual.wait("Проверяю ссылку…"))
    uid = url_id(url)
    d = await vt('GET', f'/urls/{uid}')
    fresh = d is not None and time.time() - (d['data']['attributes'].get('last_analysis_date') or 0) < 7 * 86400
    if not fresh:
        r = await vt('POST', '/urls', data={'url': url})
        aid = ((r or {}).get('data') or {}).get('id')

        async def tick(i):
            await engine.answer(event, visual.wait("Антивирусы проверяют ссылку" + "." * (i % 3 + 1)))
        await wait_analysis(aid, tick)
        d = await vt('GET', f'/urls/{uid}')
    if d is None:
        raise VTError("отчёт пока не готов — повторите через минуту")
    attrs = d['data']['attributes']
    extra = []
    if attrs.get('title'):
        extra.append(visual.field("страница", _esc(attrs['title'][:100]), code=False))
    if attrs.get('last_final_url') and attrs['last_final_url'] != url:
        extra.append(visual.field("ведёт на", _esc(attrs['last_final_url'][:200])))
    text, _ = report_text('ссылка', url, attrs, extra)
    await show(event, text, f"{GUI}/url/{uid}")


async def scan_host(event, host, is_ip):
    await engine.answer(event, visual.wait("Смотрю репутацию…"))
    d = await vt('GET', f"/{'ip_addresses' if is_ip else 'domains'}/{host}")
    if d is None:
        await engine.answer(event, visual.err("VirusTotal ничего не знает об этом адресе"))
        return
    a = d['data']['attributes']
    extra = []
    if is_ip:
        for k, label in (('country', 'страна'), ('as_owner', 'сеть'), ('network', 'диапазон')):
            if a.get(k):
                extra.append(visual.field(label, _esc(a[k]), code=False))
    else:
        if a.get('registrar'):
            extra.append(visual.field("регистратор", _esc(a['registrar']), code=False))
        if a.get('creation_date'):
            extra.append(visual.field("создан", datetime.fromtimestamp(a['creation_date']).strftime('%d.%m.%Y')))
    text, _ = report_text('IP' if is_ip else 'домен', host, a, extra)
    await show(event, text, f"{GUI}/{'ip-address' if is_ip else 'domain'}/{host}")


async def scan_hash(event, h):
    await engine.answer(event, visual.wait("Ищу по хешу…"))
    d = await vt('GET', f'/files/{h}')
    if d is None:
        await engine.answer(event, visual.hint("Такого файла в базе VirusTotal нет — пришлите сам файл и .vt ответом"))
        return
    a = d['data']['attributes']
    name = a.get('meaningful_name') or (a.get('names') or [h])[0]
    text, _ = report_text('файл', name, a, file_extra(a))
    await show(event, text, f"{GUI}/file/{a.get('sha256', h)}")


@decorators.command('vt', 'virustotal')
async def vt_cmd(event):
    """проверить на VirusTotal: ответом на файл; <ссылка | домен | IP | хеш>; rescan; auto on|off"""
    arg = engine.get_args().strip()
    low = arg.lower()
    try:
        if low.startswith('auto'):
            v = low.split()[1] if len(low.split()) > 1 else ''
            if v in ('on', 'off', 'вкл', 'выкл'):
                await engine.edit_config('auto', 'edit', v in ('on', 'вкл'))
            await engine.answer(event, visual.ok("Автопроверка программ в личке: " + ("вкл" if await cfg('auto') else "выкл")))
            return
        reply = await event.get_reply_message() if event.is_reply else None
        if reply is not None and reply.file is not None and not reply.photo:
            await scan_file(event, reply, rescan=low == 'rescan')
            return
        if not arg and reply is not None:
            arg = (reply.raw_text or '').strip().split()[0] if reply.raw_text else ''
        if not arg:
            await engine.answer(event, visual.err("Что проверить? Ответом на файл, или .vt <ссылка | домен | IP | хеш>"))
            return
        if re.fullmatch(r'[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', arg):
            await scan_hash(event, arg.lower())
            return
        try:
            ipaddress.ip_address(arg)
            await scan_host(event, arg, True)
            return
        except ValueError:
            pass
        if '/' in arg or '?' in arg or re.match(r'^[a-z]+://', arg, re.I):
            await scan_url(event, arg)
            return
        if re.fullmatch(r'(?i)[a-z0-9а-яё-]+(\.[a-z0-9а-яё-]+)+', arg):
            await scan_host(event, arg.lower(), False)
            return
        await engine.answer(event, visual.err("Не понял, что это — ссылка, домен, IP или хеш?"))
    except VTError as e:
        await engine.answer(event, visual.err(f"VirusTotal: {_esc(e)}"))
    except aiohttp.ClientError as e:
        await engine.answer(event, visual.err(f"Сеть: {_esc(e)}"))


@decorators.watcher()
async def auto_check(event):
    if event.out or not event.is_private or not event.file or not await cfg('auto', False):
        return
    name = event.file.name or ''
    if Path(name).suffix.lower() not in RISKY_EXT or (event.file.size or 0) > 100 * 1024 * 1024:
        return
    if not (await cfg('api_key', '') or '').strip():
        return
    try:
        data = await engine.client.download_media(event.message, file=bytes)
        sha = hashlib.sha256(data).hexdigest()
        d = await vt('GET', f'/files/{sha}')
    except Exception as e:
        engine.tlog(f"[virustotal] авто: {e}")
        return
    if d is None:
        return
    attrs = d['data']['attributes']
    icon, word, n, total = verdict(attrs.get('last_analysis_stats') or {})
    if icon == '🟢':
        return
    text, _ = report_text('файл', name, attrs, file_extra(attrs))
    sender = await event.get_sender()
    who = " ".join(x for x in (getattr(sender, 'first_name', '') or '', getattr(sender, 'last_name', '') or '') if x)
    text += "\n\n" + visual.warn(f"Этот файл прислал(а) {_esc(who or event.sender_id)} — не открывайте его")
    try:
        await engine.bot_send_to_owner(text, buttons=[[("↗ Отчёт", f"url:{GUI}/file/{sha}")]])
    except Exception:
        await engine.client.send_message('me', engine.safe_html(text), parse_mode='html')
