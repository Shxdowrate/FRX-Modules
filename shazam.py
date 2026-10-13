"""
Shazam — распознать трек по звуку. Ответьте этой командой на голосовое, аудио, видео или видео-кружок —
модуль узнает, что за песня играет, и покажет название, исполнителя, обложку и кнопки.

  .shazam (.sh)       ответом на голосовое / аудио / видео / кружок — распознать трек

Кнопки под результатом — ссылки: найти трек на YouTube, в Яндекс Музыке и Spotify, страница на shazam.com.

Нужен пакет shazamio (ставится сам) и ffmpeg (FRX скачивает его сам, либо системный). На Python 3.13
дополнительно нужен audioop-lts (ставится сам).
"""

import asyncio
import html
import io
import time

import decorators
import engine
import visual

NAME = "Shazam"
MODULE_NAME = "shazam"
VERSION = "1.2"
AUTHOR = "FRX"
DESCRIPTION = "Распознать трек ответом на голосовое/аудио/видео/кружок — название, исполнитель, обложка, ссылки на сервисы"
REQUIREMENTS = ["shazamio", "audioop-lts; python_version >= '3.13'"]   # ставятся сами (core/requirements.py)

_install_lock = asyncio.Lock()


def _esc(x):
    return html.escape(str(x), quote=False)


async def _pip(*pkgs):
    import sys
    async with _install_lock:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'pip', 'install', '--upgrade', *pkgs,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError((err or b'').decode('utf-8', 'replace').strip().splitlines()[-1:][0] if err else 'pip error')


async def _ensure_shazamio():
    try:
        import shazamio  # noqa: F401
        return
    except Exception:
        pass
    import sys
    pkgs = ['shazamio']
    if sys.version_info >= (3, 13):
        pkgs.append('audioop-lts')             # в 3.13 убрали модуль audioop, нужный pydub внутри shazamio
    await _pip(*pkgs)
    import importlib
    importlib.invalidate_caches()
    import shazamio  # noqa: F401


# --------------------------------------------------------------------------- распознавание

async def _to_audio_bytes(reply):
    """Скачать медиа из ответа и привести к mp3 (shazamio так надёжнее всего распознаёт)."""
    data = io.BytesIO()
    await engine.client.download_media(reply, file=data)
    raw = data.getvalue()
    try:
        import media
        return await media.to_mp3(raw, name=(reply.file.name or 'in.bin'))
    except Exception:
        return raw                              # без ffmpeg — отдадим как есть, shazamio попробует сам


def _pick(track):
    """Из сырого ответа Shazam достаём аккуратные поля."""
    title = track.get('title') or ''
    artist = track.get('subtitle') or ''
    images = track.get('images') or {}
    cover = images.get('coverarthq') or images.get('coverart') or ''
    url = track.get('url') or ''
    album, year, genre = '', '', ''
    for sec in track.get('sections') or []:
        for md in sec.get('metadata') or []:
            t, text = (md.get('title') or '').lower(), md.get('text') or ''
            if 'album' in t:
                album = text
            elif t in ('released', 'release date') or 'год' in t:
                year = text
            elif 'genre' in t or 'жанр' in t:
                genre = text
    genre = genre or (track.get('genres') or {}).get('primary', '')
    return {'title': title, 'artist': artist, 'cover': cover, 'url': url, 'album': album, 'year': year, 'genre': genre}


async def recognize(audio_bytes):
    from shazamio import Shazam
    result = await Shazam().recognize(audio_bytes)
    track = (result or {}).get('track')
    if not track:
        return None
    return _pick(track)


# --------------------------------------------------------------------------- команда

@decorators.command('shazam', 'sh', 'шазам')
async def shazam_cmd(event):
    """распознать трек: ответом на голосовое / аудио / видео / кружок"""
    if not event.is_reply:
        await engine.answer(event, visual.err("Ответьте командой на голосовое, аудио, видео или кружок"))
        return
    reply = await event.get_reply_message()
    if reply is None or reply.file is None or not (reply.audio or reply.voice or reply.video or reply.video_note
                                                   or (reply.file.mime_type or '').startswith(('audio/', 'video/'))):
        await engine.answer(event, visual.err("В ответе нет звука или видео"))
        return

    await engine.answer(event, visual.wait("Распознаю трек…"))
    try:
        await _ensure_shazamio()
    except Exception as e:
        await engine.answer(event, visual.err("Нужен пакет shazamio") + f"\n<code>{_esc(str(e)[:200])}</code>"
                            + "\n" + visual.hint("поставьте вручную: pip install shazamio"))
        return

    try:
        audio = await _to_audio_bytes(reply)
    except Exception as e:
        await engine.answer(event, visual.err("Не удалось прочитать звук") + f"\n<code>{_esc(str(e)[:200])}</code>")
        return

    try:
        info = await recognize(audio)
    except Exception as e:
        await engine.answer(event, visual.err("Shazam не ответил") + f"\n<code>{_esc(str(e)[:250])}</code>"
                            + "\n" + visual.hint("попробуйте фрагмент подлиннее или почётче"))
        return
    if not info:
        await engine.answer(event, visual.hint("Трек не распознан") + "\n"
                            + visual.hint("нужно хотя бы несколько секунд чистой музыки"))
        return

    await _show(event, info)


async def _show(event, info):
    query = f"{info['artist']} - {info['title']}".strip(' -')
    lines = [visual.title("Shazam", sub="распознано"),
             f"🎵 <b>{_esc(info['title'])}</b>"]
    if info['artist']:
        lines.append(f"<i>{_esc(info['artist'])}</i>")
    if info['album']:
        lines.append(visual.field("альбом", _esc(info['album']), code=False))
    meta = " · ".join(x for x in (info['genre'], info['year']) if x)
    if meta:
        lines.append(visual.field("", _esc(meta), code=False))
    if info['cover']:
        lines.append(f'<a href="{_esc(info["cover"])}">&#8203;</a>')
    text = "\n".join(lines)

    from urllib.parse import quote
    q = quote(query)
    rows = [[("▶ YouTube", f"url:https://www.youtube.com/results?search_query={q}"),
             ("Яндекс Музыка", f"url:https://music.yandex.ru/search?text={q}")],
            [("Spotify", f"url:https://open.spotify.com/search/{q}")]]
    if info['url']:
        rows[1].append(("🔗 Shazam", f"url:{info['url']}"))
    rows.append([(visual.btn('Закрыть', 'close'), "shz:close")])

    try:
        form = await engine.create_inlineform(event.chat_id, text, buttons=rows)
    except Exception:
        await engine.answer(event, text)
        return

    @form.on_click(data='shz:close')
    async def _(click):
        await form.delete()

