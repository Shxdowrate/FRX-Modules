"""
MediaTools — конвертер медиа прямо в чате. Ответьте командой на видео, голосовое, музыку, GIF, стикер или фото.

  .round            видео → кружок                    .voice        аудио/видео → голосовое
  .mp3 [кбит/с]     звук из видео/голосового → mp3     .gif          видео → GIF Telegram (без звука, по кругу)
  .tovideo          GIF / кружок / видеостикер → обычное видео
  .compress [МБ]    сжать видео примерно до размера    .cut 0:10 0:25   вырезать кусок (или .cut 15 — с 15-й секунды)
  .speed 2          ускорить/замедлить (0.25–4)        .reverse      задом наперёд (до минуты)
  .nosound          убрать звук (.беззвука)            .ts           картинка/видео → стикер (без пака)
  .toimg [file]     стикер → картинка (file — PNG-файлом, с прозрачностью)
  .minfo            что за файл: формат, размер, длительность, разрешение

Всё делает ffmpeg (FRX скачает его сам, если его нет) через общий медиакод FRX (`import media`).
"""

import asyncio
import io
import os
import re
import time

import decorators
import engine
import media
import visual

NAME = "MediaTools"
MODULE_NAME = "mediatools"
VERSION = "1.1"
AUTHOR = "FRX"
DESCRIPTION = "Конвертер медиа ответом: кружок, голосовое, mp3, GIF, сжатие, обрезка, скорость, реверс, стикер, картинка"


@decorators.on_start()
async def setup():
    await engine.create_config(key='max_mb', description='Самый большой файл, который можно обрабатывать, МБ',
                               data_type='int', default=300, valid={'min_value': 5, 'max_value': 2000})
    await engine.create_config(key='compress_mb', description='До скольких МБ сжимать видео по .compress без числа',
                               data_type='int', default=10, valid={'min_value': 1, 'max_value': 2000})


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


def _esc(x):
    return str(x).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _size(n):
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if n < 1024 or unit == 'ГБ':
            return f"{n:.0f} {unit}" if unit == 'Б' else f"{n:.1f} {unit}"
        n /= 1024


def _dur(sec):
    sec = int(round(sec or 0))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def parse_time(s):
    """'1:02:03' / '2:30' / '15' / '15.5' / '15s' / '2m' → секунды."""
    s = (s or '').strip().lower().rstrip('sс')
    if not s:
        return None
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(m|м|min|мин)', s)
    if m:
        return float(m.group(1)) * 60
    parts = s.split(':')
    try:
        nums = [float(x) for x in parts]
    except ValueError:
        return None
    t = 0.0
    for x in nums:
        t = t * 60 + x
    return t


# --------------------------------------------------------------------------- общее: скачать → конвертировать → отправить

class Fail(Exception):
    pass


def _ext(msg):
    if msg.voice:
        return '.ogg'
    if msg.video_note:
        return '.mp4'
    if msg.sticker:
        return {'video/webm': '.webm', 'application/x-tgsticker': '.tgs'}.get(msg.file.mime_type, '.webp')
    ext = (msg.file.ext or '') if msg.file else ''
    if ext in ('', '.bin', '.oga'):
        ext = {'audio/ogg': '.ogg', 'video/mp4': '.mp4', 'audio/mpeg': '.mp3'}.get(msg.file.mime_type if msg.file else '', ext or '.bin')
    return ext


def _stem(msg):
    name = (msg.file.name if msg.file else None) or ''
    return os.path.splitext(name)[0][:60] or 'frx'


async def fetch(event, need=('video', 'audio')):
    """Сообщение, на которое ответили → (msg, bytes, имя файла). need — что подходит: video / audio / image / sticker."""
    r = await event.get_reply_message() if event.is_reply else None
    if r is None or not (r.media and (r.file or r.photo)):
        raise Fail("Ответьте этой командой на медиа")
    kind = ('sticker' if r.sticker else 'image' if r.photo or (r.file.mime_type or '').startswith('image/')
            else 'video' if (r.video or r.video_note or r.gif or (r.file.mime_type or '').startswith('video/'))
            else 'audio' if (r.voice or r.audio or (r.file.mime_type or '').startswith('audio/')) else 'file')
    if kind not in need:
        what = {'video': 'видео', 'audio': 'аудио', 'image': 'картинку', 'sticker': 'стикер'}
        raise Fail("Эта команда — для: " + ", ".join(what[n] for n in need if n in what))
    limit = int(await cfg('max_mb', 300)) * 1024 * 1024
    size = getattr(r.file, 'size', 0) or 0
    if size > limit:
        raise Fail(f"Файл {_size(size)} — больше лимита {_size(limit)} (.cfg mediatools max_mb)")
    if not media.available():
        await engine.answer(event, visual.wait("Нужен ffmpeg — скачиваю (один раз)…"))
        await _ensure_ffmpeg(event)
    t0, last = time.monotonic(), [0.0]

    async def prog(cur, total):
        now = time.monotonic()
        if total and now - last[0] > 2 and now - t0 > 1.5:
            last[0] = now
            await engine.answer(event, visual.wait(f"Скачиваю {_size(cur)} из {_size(total)}…") + "\n"
                                + engine.progress_bar(int(cur * 100 / total), 14, show_percent=True))
    await engine.answer(event, visual.wait("Скачиваю…"))
    data = await engine.client.download_media(r, file=bytes, progress_callback=prog if size > 3 * 1024 * 1024 else None)
    if not data:
        raise Fail("Не скачалось")
    return r, data, 'in' + _ext(r), kind


def _ff_recheck():
    """Сбросить кэш поиска ffmpeg в ядре (core/media_tools.py) и поискать заново."""
    try:
        media._FF = None
    except Exception:
        pass
    return media.available()


async def _ensure_ffmpeg(event):
    # в ядре нет функции скачивания ffmpeg (media.ensure_ffmpeg не существует) —
    # сначала перепроверяем (ffmpeg мог появиться за это время), потом качаем тем же
    # загрузчиком, что и при старте FRX (frx_deps.install_ffmpeg -> user/data/bin/ffmpeg)
    if _ff_recheck():
        return
    await engine.answer(event, visual.wait("Скачиваю ffmpeg (≈150 МБ, один раз)…"))
    try:
        import frx_deps
        await asyncio.to_thread(frx_deps.install_ffmpeg)
    except Exception as e:
        raise Fail(f"Не удалось скачать ffmpeg: {e}")
    if not _ff_recheck():
        raise Fail("ffmpeg не найден и не скачался. Поставьте вручную: winget install ffmpeg "
                   "(Windows) или apt install ffmpeg (Linux) и перезапустите FRX")


def _named(data, name):
    f = io.BytesIO(data)
    f.name = name
    return f


async def run(event, need, work, label="Конвертирую…"):
    try:
        r, data, name, kind = await fetch(event, need)
        await engine.answer(event, visual.wait(label))
        await work(r, data, name, kind)
    except Fail as e:
        await engine.answer(event, visual.err(str(e)))
        return
    except Exception as e:
        await engine.answer(event, visual.err("Не получилось") + f"\n<code>{_esc(str(e)[:400])}</code>")
        return
    try:
        await event.delete()
    except Exception:
        pass


async def send(event, r, data, name, **kw):
    await engine.answer(event, visual.wait("Отправляю…"))
    await engine.client.send_file(event.chat_id, _named(data, name), reply_to=r.id, **kw)


# --------------------------------------------------------------------------- команды

@decorators.command('round', 'кружок')
async def round_cmd(event):
    """видео → кружок (квадрат, до минуты): ответом"""
    async def work(r, data, name, kind):
        out = await media.to_video_note(data, name)
        await send(event, r, out, 'round.mp4', video_note=True)
    await run(event, ('video',), work)


@decorators.command('voice', 'гс')
async def voice_cmd(event):
    """аудио или видео → голосовое сообщение: ответом"""
    async def work(r, data, name, kind):
        out = await media.to_voice(data, name)
        await send(event, r, out, 'voice.ogg', voice_note=True)
    await run(event, ('video', 'audio'), work)


@decorators.command('mp3')
async def mp3_cmd(event):
    """звук из видео/голосового/кружка → mp3: ответом; [кбит/с] — 64…320, по умолчанию 192"""
    m = re.search(r'\d+', engine.get_args())
    kbps = max(64, min(320, int(m.group()))) if m else 192

    async def work(r, data, name, kind):
        out = await media.to_mp3(data, name, kbps)
        from telethon.tl.types import DocumentAttributeAudio
        info = await media.probe(out, 'a.mp3')
        title = _stem(r) if r.file and r.file.name else 'Аудио'
        await send(event, r, out, f"{title}.mp3",
                   attributes=[DocumentAttributeAudio(duration=int(info['duration']), title=title, performer='FRX')])
    await run(event, ('video', 'audio'), work)


@decorators.command('gif')
async def gif_cmd(event):
    """видео → GIF Telegram (без звука, зацикленный, до минуты): ответом"""
    async def work(r, data, name, kind):
        out = await media.to_gif(data, name)
        from telethon.tl.types import DocumentAttributeAnimated
        await send(event, r, out, 'animation.mp4', attributes=[DocumentAttributeAnimated()])
    await run(event, ('video',), work)


@decorators.command('tovideo', 'видео')
async def tovideo_cmd(event):
    """GIF, кружок или видеостикер → обычное видео: ответом"""
    async def work(r, data, name, kind):
        dec = ['-c:v', 'libvpx-vp9'] if name.endswith('.webm') else []
        out = await media._convert(data, os.path.splitext(name)[1], '.mp4', lambda s, d: dec + [
            '-i', s, '-vf', "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", '-c:v', 'libx264', '-preset', 'veryfast',
            '-crf', '22', '-c:a', 'aac', '-movflags', '+faststart', d])
        await send(event, r, out, 'video.mp4', supports_streaming=True)
    await run(event, ('video', 'sticker'), work)


@decorators.command('compress', 'сжать')
async def compress_cmd(event):
    """сжать видео примерно до N МБ: ответом; .compress 8"""
    m = re.search(r'\d+(?:[.,]\d+)?', engine.get_args())
    target = float(m.group().replace(',', '.')) if m else float(await cfg('compress_mb', 10))

    async def work(r, data, name, kind):
        if len(data) <= target * 1024 * 1024:
            raise Fail(f"Видео уже {_size(len(data))} — меньше {target:g} МБ")
        out = await media.compress_video(data, name, target)
        await send(event, r, out, f"{_stem(r)}_{target:g}mb.mp4", supports_streaming=True,
                   caption=f"{_size(len(data))} → {_size(len(out))}")
    await run(event, ('video',), work, f"Сжимаю до ~{target:g} МБ… (это может занять время)")


@decorators.command('cut', 'обрезать', 'trim')
async def cut_cmd(event):
    """вырезать кусок видео/аудио: ответом; .cut 0:10 0:25 · .cut 15 (с 15-й секунды до конца) · .cut 1:00-1:30"""
    parts = [p for p in re.split(r'[\s\-–—]+', engine.get_args().strip()) if p]
    start = parse_time(parts[0]) if parts else None
    end = parse_time(parts[1]) if len(parts) > 1 else None
    if start is None or (len(parts) > 1 and end is None) or (end is not None and end <= start):
        await engine.answer(event, visual.err("С какой и до какой секунды?") + "\n" + visual.hint(".cut 0:10 0:25 · .cut 15 · .cut 1:00-1:30"))
        return

    async def work(r, data, name, kind):
        out = await media.trim(data, name, start, end)
        ext = os.path.splitext(name)[1]
        kw = {'voice_note': True} if r.voice else {'video_note': True} if r.video_note else {}
        if kind == 'video' and not r.video_note:
            kw['supports_streaming'] = True
        await send(event, r, out, f"{_stem(r)}_cut{ext}", **kw)
    await run(event, ('video', 'audio'), work, "Вырезаю…")


@decorators.command('speed', 'скорость')
async def speed_cmd(event):
    """ускорить или замедлить: ответом; .speed 2 · .speed 0.5 (от 0.25 до 4)"""
    m = re.search(r'\d+(?:[.,]\d+)?', engine.get_args())
    factor = float(m.group().replace(',', '.')) if m else 2.0
    if not 0.25 <= factor <= 4:
        await engine.answer(event, visual.err("Скорость — от 0.25 до 4"))
        return

    async def work(r, data, name, kind):
        out = await media.speed(data, name, factor)
        if kind == 'audio':
            if r.voice:
                await send(event, r, await media.to_voice(out, 'a.mp3'), 'voice.ogg', voice_note=True)
            else:
                await send(event, r, out, f"{_stem(r)}_x{factor:g}.mp3")
        else:
            await send(event, r, out, f"{_stem(r)}_x{factor:g}.mp4", supports_streaming=True)
    await run(event, ('video', 'audio'), work, f"Меняю скорость ×{factor:g}…")


@decorators.command('reverse', 'реверс')
async def reverse_cmd(event):
    """задом наперёд (видео до минуты): ответом"""
    async def work(r, data, name, kind):
        out = await media.reverse(data, name)
        await send(event, r, out, 'reverse.mp4', **({'video_note': True} if r.video_note else {}))
    await run(event, ('video',), work, "Переворачиваю…")


@decorators.command('nosound', 'беззвука')
async def mute_cmd(event):
    """убрать звук из видео: ответом"""
    async def work(r, data, name, kind):
        out = await media._convert(data, os.path.splitext(name)[1], '.mp4', lambda s, d: [
            '-i', s, '-an', '-c:v', 'copy', '-movflags', '+faststart', d])
        await send(event, r, out, f"{_stem(r)}_mute.mp4", **({'video_note': True} if r.video_note else {'supports_streaming': True}))
    await run(event, ('video',), work, "Убираю звук…")


@decorators.command('ts', 'tosticker')
async def ts_cmd(event):
    """картинка или видео → стикер (просто прислать, без пака; в пак — .kang)"""
    from telethon.tl.types import DocumentAttributeImageSize, DocumentAttributeSticker, DocumentAttributeVideo, InputStickerSetEmpty

    async def work(r, data, name, kind):
        if kind == 'image' or (kind == 'sticker' and name.endswith('.webp')):
            out = await media.image_to_sticker(data)
            from PIL import Image
            w, h = Image.open(io.BytesIO(out)).size
            await send(event, r, out, 'sticker.webp', force_document=False, mime_type='image/webp',
                       attributes=[DocumentAttributeSticker(alt='✨', stickerset=InputStickerSetEmpty()), DocumentAttributeImageSize(w=w, h=h)])
            return
        out = await media.video_to_sticker(data, name)
        info = await media.probe(out, 's.webm')
        await send(event, r, out, 'sticker.webm', force_document=False, mime_type='video/webm',
                   attributes=[DocumentAttributeSticker(alt='✨', stickerset=InputStickerSetEmpty()),
                               DocumentAttributeVideo(duration=info['duration'] or 3, w=info['width'] or 512, h=info['height'] or 512)])
    await run(event, ('image', 'video', 'sticker'), work, "Делаю стикер…")


@decorators.command('toimg', 'ti', 'картинка')
async def toimg_cmd(event):
    """стикер (в том числе видео и анимированный) → картинка: ответом; [file] — PNG-файлом с прозрачностью"""
    as_file = bool(re.search(r'\b(file|файл|png|doc)\b', engine.get_args().lower()))

    async def work(r, data, name, kind):
        from PIL import Image
        if name.endswith('.tgs'):
            data = await engine.client.download_media(r, file=bytes, thumb=-1)
            if not data:
                raise Fail("У анимированного стикера нет превью")
        elif name.endswith(('.webm', '.mp4')):
            dec = ['-c:v', 'libvpx-vp9'] if name.endswith('.webm') else []
            data = await media._convert(data, os.path.splitext(name)[1], '.png',
                                        lambda s, d: dec + ['-i', s, '-frames:v', '1', d], timeout=60)
        im = Image.open(io.BytesIO(data)).convert('RGBA')
        if not as_file:                                      # фото без прозрачности — подложим белый фон
            bg = Image.new('RGB', im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel('A'))
            im = bg
        buf = io.BytesIO()
        im.save(buf, 'PNG')
        await send(event, r, buf.getvalue(), 'sticker.png', force_document=as_file)
    await run(event, ('sticker', 'image', 'video'), work, "Достаю картинку…")


@decorators.command('minfo', 'медиа')
async def minfo_cmd(event):
    """что за медиа: формат, размер, длительность, разрешение, звук: ответом"""
    try:
        r, data, name, kind = await fetch(event, ('video', 'audio', 'image', 'sticker', 'file'))
    except Fail as e:
        await engine.answer(event, visual.err(str(e)))
        return
    rows = [visual.title("Медиа", sub=_esc(r.file.name or name)),
            visual.field("тип", r.file.mime_type or '?'),
            visual.field("размер", _size(len(data)), code=False)]
    try:
        if kind in ('video', 'audio') or name.endswith('.webm'):
            info = await media.probe(data, name)
            if info['duration']:
                rows.append(visual.field("длительность", _dur(info['duration']), code=False))
            if info['has_video']:
                rows.append(visual.field("кадр", f"{info['width']}×{info['height']}" + (f" · {info['fps']:g} к/с" if info['fps'] else '')))
            rows.append(visual.field("звук", "есть" if info['has_audio'] else "нет", code=False))
            if info['duration'] and len(data):
                rows.append(visual.field("битрейт", f"{len(data) * 8 / info['duration'] / 1000:.0f} кбит/с", code=False))
        elif kind in ('image', 'sticker') and not name.endswith('.tgs'):
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            rows.append(visual.field("кадр", f"{im.width}×{im.height} · {im.mode}"))
    except Exception as e:
        rows.append(visual.hint(f"подробности не прочитались: {_esc(str(e)[:100])}"))
    await engine.answer(event, "\n".join(rows))
