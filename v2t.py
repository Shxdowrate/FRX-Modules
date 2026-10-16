"""
V2T — голосовые и кружки в текст.

  .v2t        ответом на голосовое, кружок, видео или аудио — расшифровка
  .v2tauto    включить / выключить автоматическую расшифровку голосовых в личке (вам и собеседникам — ответом)

Чем расшифровывает (по очереди, пока не получится; настройка engine):
  1. Telegram — встроенная расшифровка (с Premium без ограничений, без Premium — несколько пробных в неделю);
  2. нейросеть из NeuroDash — подключение OpenAI или Groq (модели Whisper), если оно есть;
  3. Google Speech — бесплатно и без ключей (запись режется на куски по ~50 с).
"""

import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import tempfile

import aiohttp

import db
import decorators
import engine
import visual

NAME = "V2T"
MODULE_NAME = "v2t"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Голосовые и кружки в текст: Telegram → Whisper из NeuroDash → бесплатный Google; .v2t ответом, автоматически в личке"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/v2t.py"

GOOGLE_KEY = 'AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw'          # открытый ключ Chromium для Web Speech


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='engine', description='Чем расшифровывать: auto — по очереди; telegram / neuro / google — только этим',
                               data_type='choice', default='auto', options=['auto', 'telegram', 'neuro', 'google'])
    await engine.create_config(key='language', description='Язык речи для Google и Whisper (ru, en, uk…)', data_type='str', default='ru')


def ffmpeg():
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _is_media(m):
    return bool(m and (m.voice or m.video_note or m.audio or m.video or (m.file and (m.file.mime_type or '').startswith(('audio', 'video')))))


# --------------------------------------------------------------------------- 1. Telegram

async def via_telegram(msg):
    from telethon.tl.functions.messages import TranscribeAudioRequest
    peer = await engine.client.get_input_entity(msg.chat_id)
    for _ in range(30):
        r = await engine.client(TranscribeAudioRequest(peer=peer, msg_id=msg.id))
        if not r.pending and r.text:
            return r.text
        if not r.pending:
            break
        await asyncio.sleep(2)
    raise RuntimeError("Telegram не дал расшифровку")


# --------------------------------------------------------------------------- 2. Whisper из NeuroDash

def _whisper_conn():
    try:
        for c in engine.llm.connections():
            if c.get('provider') in ('openai', 'groq') and c.get('key'):
                return c
    except Exception:
        pass
    return None


async def via_neuro(path):
    c = _whisper_conn()
    if c is None:
        raise RuntimeError("в NeuroDash нет подключения OpenAI или Groq")
    model = 'whisper-large-v3' if c['provider'] == 'groq' else 'whisper-1'
    form = aiohttp.FormData()
    form.add_field('model', model)
    form.add_field('language', (await engine.get_config('language')).split('-')[0])
    with open(path, 'rb') as f:
        form.add_field('file', f.read(), filename=os.path.basename(path), content_type='application/octet-stream')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post(c['base_url'].rstrip('/') + '/audio/transcriptions', data=form,
                          headers={'Authorization': f"Bearer {c['key']}"}) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError((data.get('error') or {}).get('message') or f"ошибка {r.status}")
    return (data.get('text') or '').strip()


# --------------------------------------------------------------------------- 3. Google Speech

async def via_google(path):
    ff = ffmpeg()
    if not ff:
        raise RuntimeError("нет ffmpeg (pip install imageio-ffmpeg)")
    tmp = tempfile.mkdtemp(prefix='frx_v2t_')
    try:
        pattern = os.path.join(tmp, 'part%03d.flac')
        cmd = [ff, '-y', '-loglevel', 'error', '-i', path, '-ac', '1', '-ar', '16000', '-f', 'segment', '-segment_time', '50', pattern]
        flags = 0x08000000 if os.name == 'nt' else 0
        await asyncio.get_running_loop().run_in_executor(None, lambda: subprocess.run(cmd, check=True, timeout=300, creationflags=flags))
        lang = await engine.get_config('language')
        lang = {'ru': 'ru-RU', 'en': 'en-US', 'uk': 'uk-UA'}.get(lang, lang)
        out = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            for name in sorted(os.listdir(tmp)):
                with open(os.path.join(tmp, name), 'rb') as f:
                    body = f.read()
                async with s.post('http://www.google.com/speech-api/v2/recognize',
                                  params={'client': 'chromium', 'lang': lang, 'key': GOOGLE_KEY},
                                  data=body, headers={'Content-Type': 'audio/x-flac; rate=16000'}) as r:
                    raw = await r.text()
                best = ''
                for line in raw.splitlines():
                    try:
                        res = json.loads(line).get('result') or []
                    except ValueError:
                        continue
                    if res and res[0].get('alternative'):
                        best = res[0]['alternative'][0].get('transcript', '')
                        break
                if best:
                    out.append(best)
        if not out:
            raise RuntimeError("Google не распознал речь")
        return " ".join(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- общий путь

async def transcribe(msg):
    """(текст, чем) — пробует движки по очереди."""
    mode = await engine.get_config('engine')
    errors = []
    if mode in ('auto', 'telegram') and (msg.voice or msg.video_note):
        try:
            return await via_telegram(msg), 'Telegram'
        except Exception as e:
            errors.append(f"Telegram: {e}")
    if mode == 'telegram':
        raise RuntimeError("; ".join(errors) or "Telegram расшифровывает только голосовые и кружки")
    tmp = tempfile.mkdtemp(prefix='frx_v2t_src_')
    try:
        path = await msg.download_media(file=tmp + os.sep)
        if mode in ('auto', 'neuro') and (_whisper_conn() or mode == 'neuro'):
            try:
                return await via_neuro(path), 'Whisper (NeuroDash)'
            except Exception as e:
                errors.append(f"Whisper: {e}")
        if mode in ('auto', 'google'):
            try:
                return await via_google(path), 'Google'
            except Exception as e:
                errors.append(f"Google: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    raise RuntimeError("; ".join(errors) or "нечем расшифровать")


@decorators.command('v2t', 'stt', 'текст')
async def v2t_cmd(event):
    """расшифровать: ответом на голосовое, кружок, видео или аудио"""
    r = await event.get_reply_message() if event.is_reply else None
    if not _is_media(r):
        await engine.answer(event, visual.err("Ответьте на голосовое, кружок, видео или аудио"))
        return
    await engine.answer(event, visual.wait("Расшифровываю…"))
    try:
        text, how = await transcribe(r)
    except Exception as e:
        await engine.answer(event, visual.err("Не удалось расшифровать") + f"\n<code>{_esc(str(e)[:500])}</code>")
        return
    await engine.answer(event, f"🗣 <blockquote>{_esc(text)}</blockquote>\n" + visual.hint(how))


@decorators.command('v2tauto')
async def v2tauto_cmd(event):
    """авторасшифровка голосовых в личке: вкл / выкл"""
    on = not await db.get('auto', False)
    await db.set('auto', on)
    await engine.answer(event, visual.ok("Авторасшифровка в личке " + ("включена" if on else "выключена")))


@decorators.watcher()
async def v2t_watcher(event):
    if not event.is_private or not (event.voice or event.video_note) or not await db.get('auto', False):
        return
    try:
        text, how = await transcribe(event.message)
    except Exception:
        return
    if text:
        await event.reply(f"🗣 <blockquote>{_esc(text)}</blockquote>", parse_mode='html')
