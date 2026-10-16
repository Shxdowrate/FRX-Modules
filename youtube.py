"""
YouTube — поиск и скачивание видео (и не только с YouTube).

  .yt <запрос>                 поиск на YouTube: список — выбрали видео — качества с весом файла — подтверждение — загрузка
  .ytdl <ссылка> [audio|360|720|1080]   скачать по ссылке: без качества — выбор с весом и подтверждением. Понимает сотни сайтов: YouTube, Shorts, TikTok, VK,
                               Instagram, Twitter/X, Reddit, Twitch-клипы, SoundCloud… (всё, что умеет yt-dlp)

Качает yt-dlp (ставится сам при первом запуске), склейку делает ffmpeg (есть в FRX). Ход загрузки — полоской с МБ,
скоростью и оставшимся временем. Аудио — m4a без перекодирования (быстро) или mp3 (настройка audio_format).
Лимит размера — настройка max_mb (у Telegram для аккаунта — до 2 ГБ, с Premium — до 4 ГБ).
"""

import asyncio
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import decorators
import engine
import visual

NAME = "YouTube"
MODULE_NAME = "youtube"
VERSION = "1.1"
AUTHOR = "FRX"
DESCRIPTION = "Поиск на YouTube и скачивание видео/аудио по ссылке с сотен сайтов (yt-dlp)"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/youtube.py"
REQUIREMENTS = [("yt-dlp[default,deno]", "yt_dlp, deno")]   # ставятся сами (core/requirements.py)

URL_RE = re.compile(r'https?://\S+')


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='max_mb', description='Максимальный размер файла для отправки, МБ',
                               data_type='int', default=1900, valid={'min_value': 10, 'max_value': 4000})
    await engine.create_config(key='audio_format', description='Формат аудио: m4a — без перекодирования (быстрее), mp3 — '
                               'перекодирование', data_type='choice', default='m4a', options=['m4a', 'mp3'])
    await engine.create_config(key='results', description='Сколько результатов показывать в поиске',
                               data_type='int', default=8, valid={'min_value': 3, 'max_value': 15})


# --------------------------------------------------------------------------- yt-dlp

_install_lock = asyncio.Lock()



_deno_tried = {'on': False}


def _js():
    """JS-движок deno для yt-dlp (из pip-пакета «deno»): без него YouTube режет скорость ссылок в десятки раз."""
    try:
        import deno
        path = deno.find_deno_bin()
        return {'js_runtimes': {'deno': {'path': path}}} if path else {}
    except Exception:
        return {}

async def ytdlp():
    """Модуль yt_dlp; нет — ставит pip'ом в окружение FRX (один раз)."""
    try:
        import yt_dlp
        try:
            import deno  # noqa: F401
            return yt_dlp
        except ImportError:
            if _deno_tried['on']:
                return yt_dlp
            # старая установка без deno — доставим ниже (один раз)
    except ImportError:
        pass
    async with _install_lock:
        try:
            import yt_dlp
            import deno  # noqa: F401
            return yt_dlp
        except ImportError:
            proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp[default,deno]',
                                                        '--disable-pip-version-check', stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            _deno_tried['on'] = True
            if proc.returncode != 0:
                try:
                    import yt_dlp                 # yt-dlp есть, не встал только deno — работаем как раньше
                    return yt_dlp
                except ImportError:
                    raise RuntimeError("не удалось поставить yt-dlp: " + out.decode('utf-8', 'replace')[-300:])
            import importlib
            importlib.invalidate_caches()
            import yt_dlp
            return yt_dlp


def ffmpeg_path():
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


async def search(query, n):
    y = await ytdlp()

    def run():
        with y.YoutubeDL({**_js(), 'quiet': True, 'no_warnings': True, 'extract_flat': True, 'skip_download': True}) as ydl:
            return ydl.extract_info(f"ytsearch{n}:{query}", download=False).get('entries') or []
    return await asyncio.get_running_loop().run_in_executor(None, run)


def _dur(sec):
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return '—'
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def _num(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return '—'
    for div, suf in ((1e9, ' млрд'), (1e6, ' млн'), (1e3, ' тыс.')):
        if n >= div:
            return f"{n / div:.1f}".rstrip('0').rstrip('.') + suf
    return str(n)


HEIGHTS = (360, 480, 720, 1080, 1440, 2160)


def _fsize(f, dur):
    size = f.get('filesize') or f.get('filesize_approx')
    if not size and f.get('tbr') and dur:
        size = f['tbr'] * 1000 / 8 * dur
    return int(size or 0)


async def probe(url):
    """Что можно скачать и сколько это весит: {'info', 'sizes': {'audio': байт, '360': байт, …}} (без скачивания)."""
    y = await ytdlp()

    def run():
        with y.YoutubeDL({**_js(), 'quiet': True, 'no_warnings': True, 'noplaylist': True, 'skip_download': True}) as ydl:
            return ydl.extract_info(url, download=False)
    info = await asyncio.get_running_loop().run_in_executor(None, run)
    if info.get('entries'):
        info = info['entries'][0]
    dur = info.get('duration') or 0
    fmts = info.get('formats') or []
    audios = [f for f in fmts if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none')]
    m4a = [f for f in audios if f.get('ext') == 'm4a'] or audios
    best_a = max(m4a, key=lambda f: f.get('abr') or f.get('tbr') or 0, default=None)
    a_size = _fsize(best_a, dur) if best_a else 0
    sizes = {}
    if best_a or any(f.get('acodec') not in (None, 'none') for f in fmts):
        sizes['audio'] = a_size
    videos = [f for f in fmts if f.get('vcodec') not in (None, 'none') and f.get('height')]
    top = max((f['height'] for f in videos), default=0)
    for h in HEIGHTS:
        if h > top and not (h == HEIGHTS[0] and top):     # выше исходника не предлагаем; низкое видео — как 360
            continue
        cand = [f for f in videos if f['height'] <= h] or [min(videos, key=lambda f: f['height'])]
        best = max(cand, key=lambda f: (f['height'], f.get('ext') == 'mp4', f.get('tbr') or 0))
        size = _fsize(best, dur)
        if best.get('acodec') in (None, 'none'):
            size += a_size
        sizes[str(h)] = size
    if not sizes:                                            # сайт не отдал список форматов — пусть качает как есть
        sizes['720'] = 0
    return {'info': info, 'sizes': sizes}


def _mb(n):
    if not n:
        return '? МБ'
    return f"{n / 1048576:.0f} МБ" if n >= 100 * 1048576 else f"{n / 1048576:.1f} МБ"


def _label(mode):
    return 'Аудио' if mode == 'audio' else f'{mode}p'


def size_buttons(sizes, prefix, limit):
    """Кнопки качеств с весом: «🎬 720p · 45 МБ». Больше лимита — неактивная кнопка с пометкой."""
    rows, row = [], []
    for mode, size in sizes.items():
        if mode == 'audio':
            continue
        over = size and size > limit
        row.append((f"{'🚫' if over else '🎬'} {mode}p · {_mb(size)}", f"{prefix}big" if over else f"{prefix}c:{mode}",
                    None if over else ('primary' if mode == '720' else None)))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if 'audio' in sizes:
        rows.append([(f"🎵 Аудио · {_mb(sizes['audio'])}", f"{prefix}c:audio", "success")])
    return rows


def confirm_text(mode, size):
    return (visual.section("Скачать?") + "\n" + visual.field("качество", _label(mode)) + "\n"
            + visual.field("вес", ("~" + _mb(size)) if size else "неизвестен"))


async def download(url, mode, on_progress=None):
    """mode: 'audio' | '360' | '480' | '720' | '1080'. Возвращает (путь, info, папка) — папку удалите после отправки."""
    y = await ytdlp()
    ff = ffmpeg_path()
    tmp = tempfile.mkdtemp(prefix='frx_yt_')
    state = {'last': 0.0}
    loop = asyncio.get_running_loop()

    def hook(d):
        if on_progress is None or d.get('status') != 'downloading':
            return
        now = time.monotonic()
        if now - state['last'] < 2:
            return
        state['last'] = now
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        done = d.get('downloaded_bytes') or 0
        pct = done / total * 100 if total else 0
        asyncio.run_coroutine_threadsafe(on_progress(pct, done, total, d.get('speed') or 0), loop)

    opts = {'quiet': True, 'no_warnings': True, 'outtmpl': os.path.join(tmp, '%(title).80s [%(id)s].%(ext)s'),
            'progress_hooks': [hook], 'noplaylist': True, 'writethumbnail': True, 'restrictfilenames': False,
            'concurrent_fragment_downloads': 4, 'http_chunk_size': 10 * 1024 * 1024, 'socket_timeout': 15, 'retries': 3}
    if ff:
        opts['ffmpeg_location'] = ff
    if mode == 'audio':
        fmt = await engine.get_config('audio_format') or 'm4a'
        opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best' if fmt == 'm4a' else 'bestaudio/best'
        if ff:
            pp = {'key': 'FFmpegExtractAudio', 'preferredcodec': fmt}
            if fmt == 'mp3':
                pp['preferredquality'] = '192'
            opts['postprocessors'] = [pp,
                                      {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}]
    else:
        h = int(mode)
        opts['format'] = (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/bv*[height<={h}]+ba/b[height<={h}]/b"
                          if ff else f"b[height<={h}][ext=mp4]/b[height<={h}]/b")
        opts['merge_output_format'] = 'mp4'
        if ff:
            opts['postprocessors'] = [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}]

    def run():
        with y.YoutubeDL({**_js(), **opts}) as ydl:
            return ydl.extract_info(url, download=True)
    info = await loop.run_in_executor(None, run)
    if info.get('entries'):
        info = info['entries'][0]
    media, thumb = None, None
    for name in os.listdir(tmp):
        p = os.path.join(tmp, name)
        if name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
            thumb = p
        elif not name.endswith(('.part', '.ytdl')):
            if media is None or os.path.getsize(p) > os.path.getsize(media):
                media = p
    if media is None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("файл не скачался")
    info['_thumb'] = _small_thumb(thumb)
    return media, info, tmp


def _small_thumb(path):
    """Миниатюра для Telegram: JPEG до 320 px (нужен Pillow; нет — без миниатюры)."""
    if not path:
        return None
    try:
        from PIL import Image
        im = Image.open(path).convert('RGB')
        im.thumbnail((320, 320))
        out = path.rsplit('.', 1)[0] + '_tg.jpg'
        im.save(out, 'JPEG', quality=85)
        return out
    except Exception:
        return path if path.lower().endswith(('.jpg', '.jpeg')) and os.path.getsize(path) < 200_000 else None


async def send(chat_id, path, info, mode, reply_to=None, on_progress=None):
    from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo
    title = info.get('title') or os.path.basename(path)
    url = info.get('webpage_url') or info.get('original_url') or ''
    dur = int(info.get('duration') or 0)
    caption = f"<b>{_esc(title)}</b>\n{_esc(info.get('uploader') or info.get('channel') or '')}"
    if url:
        caption += f"\n<a href=\"{_esc(url)}\">{'источник'}</a>"
    if mode == 'audio':
        attrs = [DocumentAttributeAudio(duration=dur, title=title[:60], performer=(info.get('artist') or info.get('uploader') or '')[:60])]
    else:
        attrs = [DocumentAttributeVideo(duration=dur, w=int(info.get('width') or 0), h=int(info.get('height') or 0), supports_streaming=True)]
    return await engine.client.send_file(chat_id, path, caption=caption, parse_mode='html', attributes=attrs,
                                         thumb=info.get('_thumb'), reply_to=reply_to, supports_streaming=mode != 'audio',
                                         progress_callback=on_progress)


async def fetch_and_send(chat_id, url, mode, show, reply_to=None):
    """show(text) — обновить статус. Скачивает, проверяет размер, отправляет, чистит за собой."""
    started = time.monotonic()

    async def dl_progress(pct, done, total, speed):
        bar = engine.progress_bar(pct, 12, 'bar')
        eta = f" · ещё ~{_dur((total - done) / speed)}" if speed and total > done else ''
        info = f"{_mb(done)} из {_mb(total)}" if total else _mb(done)
        await show(visual.wait(f"Скачиваю · {_label(mode)}") + f"\n<code>{bar}</code> {pct:.0f}%\n"
                   + visual.hint(f"{info} · {speed / 1048576:.1f} МБ/с{eta}"))
    await show(visual.wait("Готовлю загрузку…"))
    path, info, tmp = await download(url, mode, dl_progress)
    try:
        size = os.path.getsize(path)
        limit = await engine.get_config('max_mb') * 1048576
        if size > limit:
            await show(visual.err("Файл слишком большой") + "\n" + visual.field("размер", f"{size / 1048576:.0f} МБ")
                       + "\n" + visual.hint("выберите качество пониже или увеличьте max_mb в .config youtube"))
            return None
        last = {'t': 0.0}
        await show(visual.wait("Отправляю в Telegram…") + "\n" + visual.hint(_mb(size)))

        async def up_progress(sent, total):
            if time.monotonic() - last['t'] < 3:
                return
            last['t'] = time.monotonic()
            pct = sent / total * 100 if total else 0
            await show(visual.wait("Отправляю в Telegram…") + f"\n<code>{engine.progress_bar(pct, 12, 'bar')}</code> {pct:.0f}%\n"
                       + visual.hint(f"{_mb(sent)} из {_mb(total)}"))
        msg = await send(chat_id, path, info, mode, reply_to=reply_to, on_progress=up_progress)
        await show(visual.ok("Готово") + "  " + visual.hint(f"{size / 1048576:.1f} МБ · {time.monotonic() - started:.0f} с"))
        return msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- команды

@decorators.command('ytdl', 'dl')
async def ytdl_cmd(event):
    """скачать по ссылке: <ссылка> [audio|360|480|720|1080] (или ответом на сообщение со ссылкой)"""
    args = engine.get_args().strip()
    m = URL_RE.search(args)
    if not m and event.is_reply:
        r = await event.get_reply_message()
        m = URL_RE.search((r.raw_text or '') if r else '')
    if not m:
        await engine.answer(event, visual.err("Нужна ссылка") + "\n" + visual.hint(".ytdl https://youtu.be/… [audio|720]"))
        return
    url = m.group(0)
    low = args.lower().replace(url.lower(), '')
    mode = 'audio' if re.search(r'\b(audio|mp3|m4a|аудио|музыка|звук)\b', low) else next(
        (q for q in ('2160', '1440', '1080', '720', '480', '360') if q in low), None)

    async def show(text):
        try:
            await engine.answer(event, text)
        except Exception:
            pass
    if mode:                     # качество указано явно — сразу качаем (вес — в ходе загрузки)
        try:
            await fetch_and_send(event.chat_id, url, mode, show, reply_to=event.reply_to_msg_id)
        except Exception as e:
            await show(visual.err("Не удалось скачать") + f"\n<code>{_esc(str(e)[:400])}</code>")
        return

    await show(visual.wait("Смотрю, что по ссылке…"))
    try:
        pr = await probe(url)
    except Exception as e:
        await show(visual.err("Не открылось") + f"\n<code>{_esc(str(e)[:400])}</code>")
        return
    info, sizes = pr['info'], pr['sizes']
    head = "\n".join([visual.title(_esc(info.get('title') or url)),
                      visual.field("автор", _esc(info.get('uploader') or info.get('channel') or '—'), code=False),
                      visual.field("длительность", _dur(info.get('duration'))),
                      f'<a href="{_esc(info.get("webpage_url") or url)}">{_esc(info.get("extractor_key") or "источник")}</a>'])
    limit = await engine.get_config('max_mb') * 1048576
    close = [[(visual.btn('Закрыть', 'close'), "yd:close")]]

    def choose():
        return size_buttons(sizes, "yd:", limit) + close
    try:
        form = await engine.create_inlineform(event.chat_id, head + "\n\n" + visual.hint("выберите качество — вес указан на кнопке"),
                                              buttons=choose())
    except Exception:
        best = next((k for k in ('720', '480', '360', '1080') if k in sizes), next(iter(sizes)))
        await show(head + "\n\n" + visual.hint(f"кнопки — со стационарным ботом; скачать: .ytdl ссылка {best}"))
        return
    await show(visual.ok("Загрузка") + "  " + visual.hint("выберите качество ниже"))
    busy = {'on': False}

    @form.on_click(data_startswith='yd:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
        elif d == 'big':
            await click.answer("Больше лимита max_mb — выберите качество пониже (или поднимите max_mb в .config youtube)",
                               show_alert=True)
        elif d == 'back':
            await form.edit(head + "\n\n" + visual.hint("выберите качество — вес указан на кнопке"), buttons=choose())
        elif d.startswith('c:'):
            md = d[2:]
            await form.edit(head + "\n\n" + confirm_text(md, sizes.get(md, 0)),
                            buttons=[[("✅ Скачать", f"yd:g:{md}", "success"), (visual.btn('Назад', 'back'), "yd:back")]])
        elif d.startswith('g:'):
            if busy['on']:
                await click.answer("уже качаю — подождите", show_alert=True)
                return
            md = d[2:]
            busy['on'] = True
            await click.answer("качаю…")

            async def fshow(text):
                try:
                    await form.edit(head + "\n\n" + text, buttons=None)
                except Exception:
                    pass
            try:
                await fetch_and_send(event.chat_id, url, md, fshow, reply_to=event.reply_to_msg_id)
            except Exception as ex:
                await fshow(visual.err("Не удалось скачать") + f"\n<code>{_esc(str(ex)[:300])}</code>")
            finally:
                busy['on'] = False
            await form.edit(head + "\n\n" + visual.ok("Готово"), buttons=[[(visual.btn('Другое качество', 'back'), "yd:back")]] + close)


@decorators.command('yt', 'youtube')
async def yt_cmd(event):
    """поиск на YouTube: <запрос> — выбрать видео и скачать видео или звук"""
    q = engine.get_args().strip()
    if not q:
        await engine.answer(event, visual.err("Что ищем?") + "\n" + visual.hint(".yt запрос"))
        return
    if URL_RE.match(q):
        await ytdl_cmd(event)
        return
    await engine.answer(event, visual.wait(f"Ищу «{_esc(q)}»…"))
    try:
        entries = await search(q, await engine.get_config('results'))
    except Exception as e:
        await engine.answer(event, visual.err("Поиск не удался") + f"\n<code>{_esc(e)}</code>")
        return
    entries = [e for e in entries if e.get('id')]
    if not entries:
        await engine.answer(event, visual.hint("Ничего не нашлось"))
        return

    def list_text():
        lines = [visual.title("YouTube", sub=f"«{_esc(q)}»")]
        for i, e in enumerate(entries, 1):
            lines.append(f"<b>{i}.</b> {_esc(e.get('title'))}\n    <i>{_esc(e.get('channel') or e.get('uploader') or '')}"
                         f" · {_dur(e.get('duration'))} · {_num(e.get('view_count'))} просм.</i>")
        return "\n".join(lines)

    def list_buttons():
        rows, row = [], []
        for i in range(len(entries)):
            row.append((str(i + 1), f"yt:o:{i}"))
            if len(row) == 5:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([(visual.btn('Закрыть', 'close'), "yt:close")])
        return rows

    try:
        form = await engine.create_inlineform(event.chat_id, list_text(), buttons=list_buttons())
    except Exception:
        await engine.answer(event, list_text() + "\n\n" + visual.hint("кнопки — со стационарным ботом; скачать — .ytdl ссылка"))
        return
    await engine.answer(event, visual.ok("YouTube") + "  " + visual.hint("результаты ниже"))
    chat_id, busy, probes = event.chat_id, {'on': False}, {}

    def video_text(e):
        url = f"https://youtu.be/{e['id']}"
        return "\n".join([visual.title(_esc(e.get('title'))),
                          visual.field("канал", _esc(e.get('channel') or e.get('uploader') or '—'), code=False),
                          visual.field("длительность", _dur(e.get('duration'))),
                          visual.field("просмотры", _num(e.get('view_count'))),
                          f'<a href="{url}">{url}</a>'])

    @form.on_click(data_startswith='yt:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
        elif d == 'list':
            await form.edit(list_text(), buttons=list_buttons())
        elif d.startswith('o:'):
            i = int(d[2:])
            e = entries[i]
            tail = [[('Открыть на YouTube', f"url:https://youtu.be/{e['id']}")], [(visual.btn('К результатам', 'back'), "yt:list")]]
            if i not in probes:
                await form.edit(video_text(e) + "\n\n" + visual.wait("Смотрю качества и вес…"), buttons=tail)
                try:
                    probes[i] = (await probe(f"https://www.youtube.com/watch?v={e['id']}"))['sizes']
                except Exception as ex:
                    await form.edit(video_text(e) + "\n\n" + visual.err("Не открылось") + f"\n<code>{_esc(str(ex)[:300])}</code>",
                                    buttons=tail)
                    return
            limit = await engine.get_config('max_mb') * 1048576
            await form.edit(video_text(e) + "\n\n" + visual.hint("выберите качество — вес указан на кнопке"),
                            buttons=size_buttons(probes[i], f"yt:{i}:", limit) + tail)
        elif d == 'big' or d.endswith(':big'):
            await click.answer("Больше лимита max_mb — выберите качество пониже (или поднимите max_mb в .config youtube)",
                               show_alert=True)
        elif re.match(r'\d+:c:', d):
            idx, _, mode = d.split(':')
            e = entries[int(idx)]
            size = probes.get(int(idx), {}).get(mode, 0)
            await form.edit(video_text(e) + "\n\n" + confirm_text(mode, size), buttons=[
                [("✅ Скачать", f"yt:g:{idx}:{mode}", "success"), (visual.btn('Назад', 'back'), f"yt:o:{idx}")]])
        elif d.startswith('g:'):
            if busy['on']:
                await click.answer("уже качаю — подождите", show_alert=True)
                return
            _, idx, mode = d.split(':')
            e = entries[int(idx)]
            busy['on'] = True
            await click.answer("качаю…")

            async def show(text):
                try:
                    await form.edit(video_text(e) + "\n\n" + text, buttons=None)
                except Exception:
                    pass
            try:
                await fetch_and_send(chat_id, f"https://www.youtube.com/watch?v={e['id']}", mode, show)
            except Exception as ex:
                await show(visual.err("Не удалось скачать") + f"\n<code>{_esc(str(ex)[:300])}</code>")
            finally:
                busy['on'] = False
            await form.edit(video_text(e) + "\n\n" + visual.ok("Отправлено"),
                            buttons=[[(visual.btn('К видео', 'back'), f"yt:o:{idx}"), (visual.btn('К результатам', 'back'), "yt:list")]])
