"""
Voinje — музыка и видео в голосовом чате Telegram. Потоком: звук идёт в чат сразу, без скачивания целиком.

Что играет:
  • поиск по каталогу Deezer (→ звук из открытых источников), YouTube, SoundCloud, радиостанции мира;
  • ссылки: YouTube (и плейлисты), SoundCloud, Яндекс Музыка, Spotify (треки, альбомы, плейлисты), прямые ссылки
    на файлы и потоки (mp3, m3u8, radio…) и всё, что понимает yt-dlp;
  • из памяти: ответом на аудио/видео/голосовое в Telegram (тоже потоком), ваша музыка из «Избранного»,
    файлы с компьютера (папка-библиотека и загрузка на сайте).

Ещё источники: YouTube Music (каталог; со входом в YouTube — ваша библиотека), Звук (без входа — 128 кбит/с, со входом —
320 кбит/с, лайки и плейлисты; неофициальный API).

Вход в сервисы (по желанию):
  • Яндекс Музыка — вход кодом (как на телевизоре) или токеном: полные треки вашей подписки, «Моя волна», лайки,
    плейлисты;
  • Spotify — вход через ваше приложение Spotify (Client ID): лайки, плейлисты, «что сейчас играет». Сам звук Spotify
    зашифрован (DRM) — его модуль не трогает: треки находятся и играют из открытых источников;
  • YouTube — файл cookies.txt (возрастные видео, Premium-качество, «Понравившиеся»).

Команды (в группе/канале, где голосовой чат):
  .vplay <запрос|ссылка>   играть (ответом на медиа — его);   .vvideo <…> — то же с видео
  .vsearch <запрос>        поиск с выбором источника;          .vp — панель плеера (кнопки)
  .vskip .vpause .vresume .vstop .vvol <1-200> .vseek <1:30|+15|-15> .vloop .vshuffle
  .vwave                   «Моя волна» Яндекс Музыки (бесконечно);  .vlikes [ym|sp] — лайки
  .vradio <запрос>         радиостанции;  .vauth — вход в сервисы
Сайт (.web) — вкладка «Voinje»: плеер, очередь, поиск по всем источникам, вход в сервисы, библиотека файлов.

Нужно: py-tgcalls (ставится сам), ffmpeg и ffprobe (нет в системе — скачиваются сами, ~100 МБ один раз).
"""

import asyncio
import base64
import hashlib
import html
import io
import json
import os
import random
import re
import secrets
import shutil
import sys
import time
import types
import urllib.parse
from pathlib import Path

import aiohttp

import db
import decorators
import engine
import visual
import web

NAME = "Voinje"
MODULE_NAME = "voinje"
VERSION = "1.2"
AUTHOR = "FRX"
DESCRIPTION = "Музыка и видео в голосовом чате потоком: YouTube, ссылки, Яндекс Музыка и Spotify со входом, радио, файлы"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/voinje.py"
REQUIREMENTS = [("yt-dlp[default,deno]", "yt_dlp, deno"), ("py-tgcalls>=3.0", "pytgcalls"), "ytmusicapi", "yandex-music", "zvuk-music"]   # ставятся сами (core/requirements.py)

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
      'Accept-Language': 'ru,en;q=0.8'}
URL_RE = re.compile(r'https?://\S+')


def _esc(x):
    return html.escape(str(x), quote=False)


def _dur(sec):
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return '—'
    if sec <= 0:
        return '—'
    h_, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h_}:{m:02}:{s:02}" if h_ else f"{m}:{s:02}"


def _parse_time(text):
    """'1:30' / '90' / '1:02:03' → секунды; '+15' / '-15' → ('rel', 15)."""
    t = (text or '').strip()
    rel = t[:1] in '+-' and t[1:2].isdigit()
    sign = -1 if t.startswith('-') else 1
    t = t.lstrip('+-')
    parts = t.split(':')
    try:
        sec = 0
        for p in parts:
            sec = sec * 60 + int(p)
    except ValueError:
        return None
    return ('rel', sign * sec) if rel else ('abs', sec)


def _norm(x):
    return ' '.join(re.findall(r'\w+', (x or '').lower().replace('ё', 'е')))


async def cfg(key):
    try:
        return await engine.get_config(key, module_name=MODULE_NAME)
    except KeyError:                  # настройка ещё не создана (модуль только что обновился) — значение по умолчанию
        return None


async def dget(key, default=None):
    return await db.gget(f"{MODULE_NAME}:{key}", default)


async def dset(key, value):
    return await db.gset(f"{MODULE_NAME}:{key}", value)


def data_dir():
    try:
        from core.paths import DATA_DIR
        d = Path(DATA_DIR) / 'voinje'
    except Exception:
        d = Path('data') / 'voinje'
    d.mkdir(parents=True, exist_ok=True)
    return d


def library_dir():
    """Музыка, загруженная на сайте. В резервную копию не идёт (метка .nobackup) — это ваши медиафайлы, а не данные FRX."""
    d = data_dir() / 'library'
    d.mkdir(parents=True, exist_ok=True)
    mark = d / '.nobackup'
    if not mark.exists():
        try:
            mark.write_text('файлы Voinje — не входят в бэкап FRX', encoding='utf-8')
        except OSError:
            pass
    return d


def bin_dir():
    """Программы (ffmpeg) — в data/bin: эту папку бэкап FRX пропускает."""
    try:
        from core.paths import DATA_DIR
        d = Path(DATA_DIR) / 'bin'
    except Exception:
        d = Path('data') / 'bin'
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- зависимости

_pip_lock = asyncio.Lock()


async def pip_install(*pkgs):
    async with _pip_lock:
        proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'pip', 'install', '-U', *pkgs,
                                                    '--disable-pip-version-check', stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"не удалось поставить {', '.join(pkgs)}: " + out.decode('utf-8', 'replace')[-400:])
        import importlib
        importlib.invalidate_caches()


async def need(module, package=None):
    """import модуля; нет — pip install (один раз)."""
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        await pip_install(package or module)
        return importlib.import_module(module)


_ydl_checked = {'done': False}


async def ytdlp():
    """yt-dlp + JS-движок deno: без него YouTube режет скорость ссылок (проверка «n») — видео и звук не успевают
    за воспроизведением, отсюда тишина и долгие загрузки. Ставится один раз: pip install "yt-dlp[default,deno]"."""
    y = await need('yt_dlp', 'yt-dlp[default,deno]')
    if not _ydl_checked['done']:
        _ydl_checked['done'] = True
        try:
            import deno  # noqa: F401
            import yt_dlp_ejs  # noqa: F401
        except ImportError:
            try:
                await pip_install('yt-dlp[default,deno]')
            except Exception as e:
                engine.tlog(f"[voinje] deno для yt-dlp не поставился: {e}")
    return y


def js_opts():
    """Путь к deno из pip-пакета (папка Scripts часто не в PATH)."""
    try:
        import deno
        path = deno.find_deno_bin()
        return {'js_runtimes': {'deno': {'path': path}}} if path else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- ffmpeg + ffprobe

FFMPEG_URLS = {
    'win': ['https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip',
            'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'],
    'linux': ['https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz'],
}
_ff_lock = asyncio.Lock()
_ff_state = {'ok': False, 'progress': None, 'error': None}


def _exe(name):
    return name + ('.exe' if os.name == 'nt' else '')


def _ff_dir():
    d = bin_dir() / 'ffmpeg'
    old = data_dir() / 'ffmpeg'                    # первая версия клала ffmpeg в data/voinje — переносим из бэкапа в bin
    if old.is_dir() and not d.exists():
        try:
            shutil.move(str(old), str(d))
        except OSError:
            pass
    return d


def _ff_ready():
    """ffmpeg и ffprobe видны по имени (их вызывает py-tgcalls) — свои кладём в PATH процесса."""
    own = _ff_dir()
    if (own / _exe('ffmpeg')).is_file() and (own / _exe('ffprobe')).is_file():
        path = os.environ.get('PATH', '')
        if str(own) not in path.split(os.pathsep):
            os.environ['PATH'] = str(own) + os.pathsep + path
    return bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


async def ensure_ffmpeg(on_progress=None):
    if _ff_ready():
        _ff_state['ok'] = True
        return True
    async with _ff_lock:
        if _ff_ready():
            return True
        plat = 'win' if os.name == 'nt' else ('linux' if sys.platform.startswith('linux') else None)
        if plat is None:
            raise RuntimeError("нужны ffmpeg и ffprobe: brew install ffmpeg")
        own = _ff_dir()
        own.mkdir(parents=True, exist_ok=True)
        last_err = None
        for url in FFMPEG_URLS[plat]:
            archive = own / ('dl.zip' if url.endswith('.zip') else 'dl.tar.xz')
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800, sock_read=60)) as s:
                    async with s.get(url, headers=UA) as r:
                        if r.status != 200:
                            raise RuntimeError(f"HTTP {r.status}")
                        total = int(r.headers.get('Content-Length') or 0)
                        done, t0 = 0, 0.0
                        with open(archive, 'wb') as f:
                            async for chunk in r.content.iter_chunked(1 << 20):
                                f.write(chunk)
                                done += len(chunk)
                                _ff_state['progress'] = (done, total)
                                if on_progress and time.monotonic() - t0 > 2:
                                    t0 = time.monotonic()
                                    await on_progress(done, total)
                await asyncio.get_running_loop().run_in_executor(None, _unpack_ff, archive, own)
                archive.unlink(missing_ok=True)
                if _ff_ready():
                    _ff_state.update(ok=True, progress=None, error=None)
                    return True
                last_err = "в архиве нет ffmpeg/ffprobe"
            except Exception as e:
                last_err = str(e)
                archive.unlink(missing_ok=True)
        _ff_state['error'] = last_err
        raise RuntimeError(f"не удалось скачать ffmpeg ({last_err}). Поставьте вручную: winget install ffmpeg "
                           "(Windows) или apt install ffmpeg (Linux) и перезапустите FRX")


def _unpack_ff(archive, dest):
    names = {_exe('ffmpeg'), _exe('ffprobe')}
    if str(archive).endswith('.zip'):
        import zipfile
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                base = os.path.basename(info.filename)
                if base in names:
                    with z.open(info) as src, open(dest / base, 'wb') as out:
                        shutil.copyfileobj(src, out)
    else:
        import tarfile
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                base = os.path.basename(m.name)
                if base in names and m.isfile():
                    with t.extractfile(m) as src, open(dest / base, 'wb') as out:
                        shutil.copyfileobj(src, out)
                    os.chmod(dest / base, 0o755)


# --------------------------------------------------------------------------- py-tgcalls (одна копия на процесс)

# Состояние, которое переживает перезагрузку модуля (иначе на клиенте Telethon копились бы обработчики py-tgcalls).
RT = sys.modules.get('_frx_voinje_runtime')
if RT is None:
    RT = types.ModuleType('_frx_voinje_runtime')
    RT.calls = None          # PyTgCalls
    RT.client = None         # Telethon-клиент, к которому он привязан
    RT.dispatch = None       # функция текущей версии модуля для событий py-tgcalls
    sys.modules['_frx_voinje_runtime'] = RT

_calls_lock = asyncio.Lock()
_help_cache = {}


def _patch_pytgcalls():
    """Ускорение: py-tgcalls перед КАЖДЫМ запуском потока вызывает «ffmpeg -h full» (на Windows это секунда-две),
    чтобы выкинуть неподдерживаемые флаги. Список флагов от запуска к запуску не меняется — кэшируем его."""
    import pytgcalls.ffmpeg as pf
    import pytgcalls.types.stream.media_stream as pm
    if getattr(pf.cleanup_commands, '_frx_cached', False):
        return

    async def cleanup_commands(commands, process_name=None, blacklist=None):
        name = process_name or commands[0]
        supported = _help_cache.get(name)
        if supported is None:
            try:
                proc = await asyncio.create_subprocess_exec(name, '-h', 'full', stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.PIPE)
                out, _ = await asyncio.wait_for(proc.communicate(), 20)
            except FileNotFoundError:
                from ntgcalls import FFmpegError
                raise FFmpegError(f'{name} not installed')
            supported = set()
            for names in re.findall(r'(?m)^ *(--?\w[\w-]*(?:, *--?\w[\w-]*)*)', out.decode('utf-8', 'replace')):
                supported.update(n.strip() for n in names.split(','))
            supported.add('-i')
            _help_cache[name] = supported
        result, ignore_next = [], False

        def is_flag(arg):
            return arg[0] == '-' and not arg[1:].replace('.', '', 1).isdigit()
        for v in commands:
            if not v:
                continue
            if is_flag(v):
                ignore_next = v not in supported or (blacklist is not None and v in blacklist)
            if not ignore_next:
                result.append(v)
            elif not is_flag(v):
                ignore_next = False
        return result
    cleanup_commands._frx_cached = True
    pf.cleanup_commands = cleanup_commands
    pm.cleanup_commands = cleanup_commands


async def get_calls():
    """PyTgCalls для текущего аккаунта (после смены аккаунта — новый)."""
    async with _calls_lock:
        client = engine.client
        if RT.calls is not None and RT.client is client:
            return RT.calls
        await ensure_ffmpeg()
        await need('pytgcalls', 'py-tgcalls')
        _patch_pytgcalls()
        from pytgcalls import PyTgCalls, filters as fl
        calls = PyTgCalls(client)

        @calls.on_update(fl.stream_end())
        async def _ended(_client, update):
            fn = RT.dispatch
            if fn is not None:
                try:
                    await fn('end', update)
                except Exception as e:
                    engine.tlog(f"[voinje] stream_end: {e}")
        await calls.start()
        RT.calls, RT.client = calls, client
        return calls


# =========================================================================== источники

def item(title, artist='', duration=0, src=None, thumb='', service='', live=False, video=False):
    """Трек/видео в очереди. src — откуда брать поток (разрешается в ссылку перед самым воспроизведением)."""
    return {'id': secrets.token_hex(4), 'title': title or 'Без названия', 'artist': artist or '', 'duration': int(duration or 0),
            'src': src or {}, 'thumb': thumb or '', 'service': service, 'live': bool(live), 'video': bool(video)}


def label(it):
    return f"{it['artist']} — {it['title']}" if it.get('artist') else it.get('title', '')


async def _get(url, as_json=True, params=None, headers=None, timeout=20):
    async with aiohttp.ClientSession(headers={**UA, **(headers or {})}, timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get(url, params=params) as r:
            r.raise_for_status()
            return await (r.json(content_type=None) if as_json else r.text())


# ------------------------------------------------------------------ yt-dlp

async def _ydl_opts(extra=None):
    opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'socket_timeout': 15, **js_opts()}
    ck = data_dir() / 'youtube_cookies.txt'
    if ck.is_file():
        opts['cookiefile'] = str(ck)
    opts.update(extra or {})
    return opts


async def ydl_info(url, extra=None):
    y = await ytdlp()
    opts = await _ydl_opts(extra)

    def run():
        with y.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    return await asyncio.get_running_loop().run_in_executor(None, run)


def _yt_item(e, video=False):
    url = e.get('webpage_url') or e.get('url') or ''
    if not url.startswith('http'):
        url = f"https://www.youtube.com/watch?v={e.get('id')}"
    thumbs = e.get('thumbnails') or []
    thumb = e.get('thumbnail') or (thumbs[-1].get('url') if thumbs else '')
    artist = re.sub(r'\s*-\s*Topic$', '', e.get('channel') or e.get('uploader') or '').strip()
    host = urllib.parse.urlparse(url).hostname or ''
    service = 'SoundCloud' if 'soundcloud' in host else 'YouTube' if ('youtu' in host) else (e.get('extractor_key') or 'Ссылка')
    return item(e.get('title'), artist, e.get('duration'), {'t': 'ydl', 'url': url}, thumb, service,
                live=bool(e.get('is_live')), video=video)


async def search_ydl(q, kind='yt', n=10):
    prefix = {'yt': 'ytsearch', 'sc': 'scsearch'}[kind]
    info = await ydl_info(f"{prefix}{n}:{q}", {'extract_flat': True})
    return [_yt_item(e) for e in (info.get('entries') or []) if e]


async def from_url(url, video=False):
    """Ссылка → [элементы] (плейлист — до 100 штук)."""
    host = (urllib.parse.urlparse(url).hostname or '').lower()
    if 'spotify.' in host:
        return await spotify_link(url)
    if 'music.yandex.' in host or 'music.ya.' in host:
        return await yandex_link(url)
    if 'deezer.' in host and '/track/' in url:
        m = re.search(r'/track/(\d+)', url)
        t = await _get(f'https://api.deezer.com/track/{m.group(1)}')
        return [_deezer_item(t)]
    if re.search(r'\.(mp3|m4a|aac|ogg|opus|flac|wav|mp4|mkv|webm|m3u8?|pls)(\?|$)', url, re.I):
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path)) or url
        return [item(name, '', 0, {'t': 'url', 'url': url}, '', 'Ссылка', live=url.lower().split('?')[0].endswith(('.m3u8', '.m3u', '.pls')),
                     video=video)]
    try:
        info = await ydl_info(url, {'extract_flat': 'in_playlist', 'noplaylist': False, 'playlistend': 100})
    except Exception:
        return [item(url, '', 0, {'t': 'url', 'url': url}, '', 'Ссылка', video=video)]
    if info.get('entries'):
        return [_yt_item(e, video) for e in info['entries'] if e][:100]
    return [_yt_item(info, video)]


# ------------------------------------------------------------------ Deezer (каталог)

def _deezer_item(t):
    al = t.get('album') or {}
    return item(t.get('title'), (t.get('artist') or {}).get('name'), t.get('duration'),
                {'t': 'match', 'q': f"{(t.get('artist') or {}).get('name', '')} - {t.get('title', '')}", 'dur': t.get('duration')},
                al.get('cover_big') or al.get('cover_medium') or '', 'Deezer')


SEP_RE = re.compile(r'\s+[-—–]+\s+|\s*[—–]\s*')


async def search_std(q, n=12):
    """Каталог Deezer (умный разбор «исполнитель - трек»); мало — добавляем YouTube."""
    jobs = [_get('https://api.deezer.com/search', params={'q': q, 'limit': n})]
    parts = [p.strip() for p in SEP_RE.split(q, 1)]
    if len(parts) == 2 and all(parts):
        jobs.insert(0, _get('https://api.deezer.com/search', params={'q': f'artist:"{parts[0]}" track:"{parts[1]}"', 'limit': n}))
    got = await asyncio.gather(*jobs, return_exceptions=True)
    seen, out = set(), []
    for res in got:
        if isinstance(res, Exception):
            continue
        for t in res.get('data', []):
            if t.get('type') != 'track':
                continue
            k = (_norm((t.get('artist') or {}).get('name')), _norm(t.get('title')))
            if k not in seen:
                seen.add(k)
                out.append(_deezer_item(t))
    if len(out) < 3:
        try:
            out += await search_ydl(q, 'yt', 6)
        except Exception:
            pass
    return out[:max(n, 10)]


async def match_youtube(q, want_dur=0):
    """«исполнитель - трек» → ссылка на лучшую полную версию (YouTube, затем SoundCloud)."""
    cands = []
    for kind in ('yt', 'sc'):
        try:
            info = await ydl_info(f"{'ytsearch' if kind == 'yt' else 'scsearch'}5:{q}", {'extract_flat': True})
            cands += [e for e in info.get('entries') or [] if e]
        except Exception:
            pass
        if cands and kind == 'yt':
            break
    if not cands:
        raise RuntimeError(f"не нашёл «{q}» в открытых источниках")
    words = set(_norm(q).split())

    def score(e):
        t = _norm(e.get('title'))
        s = len(words & set(t.split())) * 3
        d = e.get('duration') or 0
        if want_dur and d:
            s -= min(abs(d - want_dur), 120) / 6
        for bad in ('live', 'cover', 'remix', 'karaoke', 'instrumental', 'slowed', 'sped', 'nightcore', '8d', 'reverb'):
            if bad in t and bad not in _norm(q):
                s -= 6
        if 'official' in t or 'topic' in (e.get('channel') or '').lower():
            s += 2
        return s
    best = max(cands, key=score)
    return best.get('webpage_url') or best.get('url') or f"https://www.youtube.com/watch?v={best.get('id')}"


# ------------------------------------------------------------------ радио (radio-browser.info, без ключей)

async def search_radio(q, n=20):
    servers = ['de1', 'fi1', 'at1', 'nl1']
    random.shuffle(servers)
    for sv in servers:
        try:
            data = await _get(f'https://{sv}.api.radio-browser.info/json/stations/search',
                              params={'name': q, 'limit': n, 'hidebroken': 'true', 'order': 'clickcount', 'reverse': 'true'},
                              headers={'User-Agent': 'FRX-Voinje/1.0'}, timeout=12)
            break
        except Exception:
            data = None
    out = []
    for st in data or []:
        url = st.get('url_resolved') or st.get('url')
        if not url:
            continue
        sub = ' · '.join(x for x in (st.get('country'), (f"{st['bitrate']} кбит/с" if st.get('bitrate') else ''), st.get('tags', '')[:40]) if x)
        out.append(item(st.get('name', '').strip(), sub, 0, {'t': 'url', 'url': url}, st.get('favicon') or '', 'Радио', live=True))
    return out


# ------------------------------------------------------------------ из памяти: Telegram и файлы

AUDIO_EXT = ('.mp3', '.m4a', '.aac', '.ogg', '.opus', '.flac', '.wav', '.wma', '.mp4', '.mkv', '.webm', '.mov', '.avi')


def tg_item(msg, video=None):
    f = msg.file
    title = getattr(f, 'title', None) or getattr(f, 'name', None) or ('Голосовое' if msg.voice else 'Видео' if msg.video else 'Аудио')
    artist = getattr(f, 'performer', None) or ''
    dur = getattr(f, 'duration', None) or 0
    is_video = bool(msg.video or msg.video_note or (f.mime_type or '').startswith('video/'))
    return item(title, artist, dur, {'t': 'tg', 'chat': msg.chat_id, 'msg': msg.id, 'size': f.size or 0, 'mime': f.mime_type or ''},
                '', 'Telegram', video=is_video if video is None else video)


async def search_saved(q='', n=40):
    """Музыка из «Избранного» (то, что вы туда пересылали)."""
    from telethon.tl.types import InputMessagesFilterMusic
    out = []
    async for m in engine.client.iter_messages('me', filter=InputMessagesFilterMusic, limit=400 if q else n, search=None):
        it = tg_item(m, False)
        if q and _norm(q) not in _norm(label(it)):
            continue
        out.append(it)
        if len(out) >= n:
            break
    return out


async def library_dirs():
    dirs = [library_dir()]
    for d in await cfg('library_dirs') or []:
        p = Path(os.path.expanduser(str(d)))
        if p.is_dir():
            dirs.append(p)
    return dirs


async def search_files(q='', n=200):
    out = []
    for d in await library_dirs():
        for p in sorted(d.rglob('*')):
            if p.is_file() and p.suffix.lower() in AUDIO_EXT and (not q or _norm(q) in _norm(p.stem)):
                stem = p.stem
                artist, title = (stem.split(' - ', 1) + [''])[:2] if ' - ' in stem else ('', stem)
                out.append(item(title or stem, artist, 0, {'t': 'file', 'path': str(p)}, '', 'Файл',
                                video=p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov', '.avi')))
                if len(out) >= n:
                    return out
    return out


# =========================================================================== Яндекс Музыка

# Вход «кодом» — как на телевизоре (OAuth device flow): Яндекс показывает код, вы вводите его на ya.ru/device
# под своим аккаунтом. Это идентификатор официального приложения Яндекс Музыки — токен даёт доступ только
# к музыке вашего аккаунта (и хранится только у вас, в базе FRX).
YM_CLIENT_ID = '23cabbbdc6cd418abb4b39c32c41195d'
YM_CLIENT_SECRET = '53bc75238f0c4d08a118e51fe9203300'
_ym = {'client': None, 'token': None, 'login': None, 'anon': None}


async def ym_client(anon_ok=True):
    """ClientAsync Яндекс Музыки: со входом — ваш; без — анонимный (поиск и 30-секундные отрывки)."""
    ym = await need('yandex_music', 'yandex-music')
    token = await dget('ym_token')
    if token:
        if _ym['client'] is not None and _ym['token'] == token:
            return _ym['client']
        client = await ym.ClientAsync(token).init()
        _ym.update(client=client, token=token)
        try:
            acc = client.me.account
            _ym['login'] = acc.login or acc.display_name or acc.full_name
        except Exception:
            _ym['login'] = None
        return client
    if not anon_ok:
        raise RuntimeError("войдите в Яндекс Музыку: .vauth")
    if _ym['anon'] is None:
        _ym['anon'] = await ym.ClientAsync().init()
    return _ym['anon']


async def ym_logged():
    return bool(await dget('ym_token'))


async def ym_device_start():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post('https://oauth.yandex.ru/device/code',
                          data={'client_id': YM_CLIENT_ID, 'device_id': secrets.token_hex(8), 'device_name': 'FRX Voinje'}) as r:
            d = await r.json(content_type=None)
    if 'device_code' not in d:
        raise RuntimeError(d.get('error_description') or d.get('error') or 'Яндекс не выдал код')
    _ym['device'] = {'code': d['device_code'], 'until': time.time() + int(d.get('expires_in', 300)), 'next': time.time() + 4,
                     'interval': int(d.get('interval', 5)), 'user_code': d['user_code'],
                     'url': d.get('verification_url', 'https://ya.ru/device')}
    return _ym['device']


async def ym_device_poll():
    """None — ещё ждём; True — вошли; исключение — код истёк/отклонён."""
    dev = _ym.get('device')
    if not dev:
        raise RuntimeError("сначала запросите код")
    if time.time() > dev['until']:
        _ym.pop('device', None)
        raise RuntimeError("код истёк — запросите новый")
    if time.time() < dev.get('next', 0):            # Яндекс просит не чаще, чем раз в interval секунд
        return None
    dev['next'] = time.time() + dev.get('interval', 5)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post('https://oauth.yandex.ru/token', data={'grant_type': 'device_code', 'code': dev['code'],
                                                                      'client_id': YM_CLIENT_ID, 'client_secret': YM_CLIENT_SECRET},
                              headers={'User-Agent': 'FRX-Voinje'}) as r:
                text = await r.text()
        d = json.loads(text) if text.strip().startswith('{') else {'error': 'not_json', 'error_description': text[:120]}
    except Exception as e:                           # сеть моргнула — это не повод бросать вход, просто ждём дальше
        engine.tlog(f"[voinje] Яндекс: проверка входа: {e}")
        return None
    if d.get('access_token'):
        try:
            await ym_set_token(d['access_token'])
        except Exception as e:
            _ym.pop('device', None)
            raise RuntimeError(f"Яндекс выдал токен, но Музыка его не приняла: {e}")
        _ym.pop('device', None)
        return True
    err = d.get('error') or ''
    if err in ('authorization_pending', 'slow_down', 'not_json', ''):
        if err == 'slow_down':
            dev['interval'] = dev.get('interval', 5) + 5
        return None
    engine.tlog(f"[voinje] Яндекс: вход отклонён: {d}")
    _ym.pop('device', None)
    human = {'invalid_grant': 'код истёк или уже использован — запросите новый',
             'access_denied': 'вход отклонён на странице Яндекса',
             'invalid_client': 'Яндекс не принял приложение — попробуйте вход токеном'}.get(err)
    raise RuntimeError(human or f"{err}: {d.get('error_description') or 'вход не удался'}")


async def ym_set_token(token):
    token = (token or '').strip()
    m = re.search(r'access_token=([^&\s]+)', token)          # вставили целую ссылку после входа
    if m:
        token = m.group(1)
    if not token:
        await dset('ym_token', None)
        _ym.update(client=None, token=None, login=None)
        return None
    ym = await need('yandex_music', 'yandex-music')
    client = await ym.ClientAsync(token).init()               # проверка: токен рабочий
    await dset('ym_token', token)
    _ym.update(client=client, token=token)
    try:
        acc = client.me.account
        _ym['login'] = acc.login or acc.display_name
    except Exception:
        _ym['login'] = None
    return _ym['login']


def _ym_item(t, logged):
    artists = ', '.join(a.name for a in (t.artists or []) if a.name)
    cover = ('https://' + t.cover_uri.replace('%%', '400x400')) if getattr(t, 'cover_uri', None) else ''
    tid = f"{t.id}:{t.albums[0].id}" if getattr(t, 'albums', None) else str(t.id)
    dur = int((t.duration_ms or 0) / 1000)
    if logged:
        src = {'t': 'ym', 'id': tid}
    else:
        src = {'t': 'match', 'q': f"{artists} - {t.title}", 'dur': dur}
    return item(t.title + (f" ({t.version})" if getattr(t, 'version', None) else ''), artists, dur, src, cover, 'Яндекс Музыка')


async def ym_search(q, n=15):
    client = await ym_client()
    res = await client.search(q, type_='track')
    tracks = (res.tracks.results if res and res.tracks else [])[:n]
    logged = await ym_logged()
    return [_ym_item(t, logged) for t in tracks if t.available is not False]


async def ym_likes(n=200):
    client = await ym_client(anon_ok=False)
    likes = await client.users_likes_tracks()
    tracks = await likes.fetch_tracks_async() if likes else []
    return [_ym_item(t, True) for t in tracks[:n] if t.available is not False]


async def ym_playlists():
    client = await ym_client(anon_ok=False)
    pls = await client.users_playlists_list()
    return [{'id': f"{p.owner.uid}:{p.kind}", 'title': p.title, 'count': p.track_count} for p in pls or []]


async def ym_playlist(pid, n=300):
    client = await ym_client(anon_ok=False)
    uid, kind = pid.split(':', 1)
    pl = await client.users_playlists(int(kind), uid)
    tracks = await pl.fetch_tracks_async()
    return [_ym_item(t.track if hasattr(t, 'track') and t.track else t, True) for t in tracks[:n]]


async def ym_wave(station='user:onyourwave', after=None):
    """Порция «Моей волны» (или другой станции): [элементы]."""
    client = await ym_client(anon_ok=False)
    res = await client.rotor_station_tracks(station, queue=after)
    out = []
    for seq in res.sequence or []:
        if seq.track and seq.track.available is not False:
            it = _ym_item(seq.track, True)
            it['wave'] = station
            out.append(it)
    return out


async def ym_stream_url(track_id):
    client = await ym_client(anon_ok=False)
    infos = await client.tracks_download_info(track_id, get_direct_links=True)
    if not infos:
        raise RuntimeError("Яндекс не отдал ссылку на трек (нет подписки?)")
    full = [i for i in infos if not i.preview and i.direct_link] or [i for i in infos if i.direct_link]
    if not full:
        raise RuntimeError("Яндекс не отдал ссылку на трек")
    best = max(full, key=lambda i: ((i.codec == 'mp3') * 1000 + (i.bitrate_in_kbps or 0)))
    return best.direct_link


async def yandex_link(url):
    """Ссылка Яндекс Музыки: трек / альбом / плейлист."""
    client = await ym_client()
    logged = await ym_logged()
    m = re.search(r'/album/(\d+)/track/(\d+)', url) or re.search(r'/track/(\d+)', url)
    if m and 'track' in url:
        tid = f"{m.group(2)}:{m.group(1)}" if m.lastindex == 2 else m.group(1)
        tracks = await client.tracks([tid])
        return [_ym_item(t, logged) for t in tracks]
    m = re.search(r'/album/(\d+)', url)
    if m:
        album = await client.albums_with_tracks(int(m.group(1)))
        return [_ym_item(t, logged) for vol in album.volumes or [] for t in vol]
    m = re.search(r'/users/([^/]+)/playlists/(\d+)', url)
    if m:
        pl = await client.users_playlists(int(m.group(2)), m.group(1))
        tracks = await pl.fetch_tracks_async()
        return [_ym_item(t.track if getattr(t, 'track', None) else t, logged) for t in tracks]
    m = re.search(r'/playlists/([\w-]+)', url)
    if m:
        pl = await client.playlist(m.group(1))
        tracks = await pl.fetch_tracks_async()
        return [_ym_item(t.track if getattr(t, 'track', None) else t, logged) for t in tracks]
    raise RuntimeError("не понял ссылку Яндекс Музыки")


# =========================================================================== Spotify

# Вход через ваше приложение Spotify (developer.spotify.com → Create app → Redirect URI ниже) — PKCE, без секрета.
# Звук Spotify зашифрован (DRM) и модулем не используется: из Spotify берутся только списки (лайки, плейлисты,
# «сейчас играет»), а сами треки находятся в открытых источниках.
SP_REDIRECT = 'http://127.0.0.1:8888/callback'
SP_SCOPES = 'user-library-read playlist-read-private playlist-read-collaborative user-read-currently-playing user-read-recently-played user-top-read'
_sp = {}


async def sp_login_url(client_id):
    client_id = (client_id or '').strip()
    if not re.fullmatch(r'[0-9a-fA-F]{32}', client_id):
        raise RuntimeError("Client ID — 32 символа из настроек приложения на developer.spotify.com")
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    await dset('sp_pending', {'client_id': client_id, 'verifier': verifier})
    return 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
        'client_id': client_id, 'response_type': 'code', 'redirect_uri': SP_REDIRECT, 'scope': SP_SCOPES,
        'code_challenge_method': 'S256', 'code_challenge': challenge})


async def _sp_token_request(data):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post('https://accounts.spotify.com/api/token', data=data) as r:
            d = await r.json(content_type=None)
    if 'access_token' not in d:
        raise RuntimeError(d.get('error_description') or d.get('error') or 'Spotify не выдал токен')
    return d


async def sp_finish(pasted):
    """После входа браузер уходит на 127.0.0.1:8888/callback?code=… (страница не откроется — так и надо):
    адрес из строки браузера вставляется сюда."""
    pend = await dget('sp_pending')
    if not pend:
        raise RuntimeError("сначала нажмите «войти в Spotify»")
    m = re.search(r'[?&]code=([^&\s]+)', pasted or '')
    if not m:
        err = re.search(r'[?&]error=([^&\s]+)', pasted or '')
        raise RuntimeError(f"Spotify: {err.group(1)}" if err else "в адресе нет code=… — скопируйте адрес целиком")
    d = await _sp_token_request({'grant_type': 'authorization_code', 'code': urllib.parse.unquote(m.group(1)),
                                 'redirect_uri': SP_REDIRECT, 'client_id': pend['client_id'], 'code_verifier': pend['verifier']})
    auth = {'client_id': pend['client_id'], 'access': d['access_token'], 'refresh': d.get('refresh_token'),
            'until': time.time() + int(d.get('expires_in', 3600)) - 60}
    await dset('sp_auth', auth)
    await dset('sp_pending', None)
    me = await sp_api('/me')
    auth['name'] = me.get('display_name') or me.get('id')
    await dset('sp_auth', auth)
    return auth['name']


async def sp_api(path, params=None):
    auth = await dget('sp_auth')
    if not auth:
        raise RuntimeError("войдите в Spotify: .vauth")
    if time.time() > auth.get('until', 0):
        d = await _sp_token_request({'grant_type': 'refresh_token', 'refresh_token': auth['refresh'], 'client_id': auth['client_id']})
        auth.update(access=d['access_token'], until=time.time() + int(d.get('expires_in', 3600)) - 60,
                    refresh=d.get('refresh_token') or auth['refresh'])
        await dset('sp_auth', auth)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get('https://api.spotify.com/v1' + path, params=params,
                         headers={'Authorization': f"Bearer {auth['access']}"}) as r:
            if r.status == 204:
                return {}
            d = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(f"Spotify: {(d.get('error') or {}).get('message') or r.status}")
            return d


def _sp_item(t):
    if not t or not t.get('name'):
        return None
    artists = ', '.join(a.get('name', '') for a in t.get('artists') or [])
    imgs = (t.get('album') or {}).get('images') or []
    dur = int((t.get('duration_ms') or 0) / 1000)
    return item(t['name'], artists, dur, {'t': 'match', 'q': f"{artists} - {t['name']}", 'dur': dur},
                imgs[0]['url'] if imgs else '', 'Spotify')


async def sp_search(q, n=15):
    d = await sp_api('/search', {'q': q, 'type': 'track', 'limit': n})
    return [x for x in (_sp_item(t) for t in (d.get('tracks') or {}).get('items') or []) if x]


async def sp_likes(n=200):
    out, url = [], '/me/tracks'
    params = {'limit': 50, 'offset': 0}
    while len(out) < n:
        d = await sp_api(url, params)
        out += [x for x in (_sp_item(i.get('track')) for i in d.get('items') or []) if x]
        if not d.get('next'):
            break
        params['offset'] += 50
    return out[:n]


async def sp_playlists():
    d = await sp_api('/me/playlists', {'limit': 50})
    return [{'id': p['id'], 'title': p.get('name'), 'count': (p.get('tracks') or {}).get('total')} for p in d.get('items') or []]


async def sp_playlist(pid, n=300):
    out, params = [], {'limit': 100, 'offset': 0}
    while len(out) < n:
        d = await sp_api(f'/playlists/{pid}/tracks', params)
        out += [x for x in (_sp_item(i.get('track')) for i in d.get('items') or []) if x]
        if not d.get('next'):
            break
        params['offset'] += 100
    return out[:n]


async def sp_now():
    d = await sp_api('/me/player/currently-playing')
    it = _sp_item(d.get('item')) if d else None
    if it is None:
        raise RuntimeError("в Spotify сейчас ничего не играет")
    return [it]


async def spotify_link(url):
    """Ссылка Spotify: со входом — через API, без входа — из открытой встраиваемой страницы."""
    m = re.search(r'(track|album|playlist)[/:]([A-Za-z0-9]{10,})', url)
    if not m:
        raise RuntimeError("не понял ссылку Spotify")
    kind, sid = m.groups()
    if await dget('sp_auth'):
        try:
            if kind == 'track':
                return [_sp_item(await sp_api(f'/tracks/{sid}'))]
            if kind == 'playlist':
                return await sp_playlist(sid)
            d = await sp_api(f'/albums/{sid}')
            cover = ((d.get('images') or [{}])[0]).get('url', '')
            out = []
            for t in (d.get('tracks') or {}).get('items') or []:
                it = _sp_item({**t, 'album': {'images': [{'url': cover}]}})
                if it:
                    out.append(it)
            return out
        except Exception:
            pass
    page = await _get(f'https://open.spotify.com/embed/{kind}/{sid}', as_json=False)
    mm = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not mm:
        raise RuntimeError("Spotify не отдал страницу — войдите (.vauth) или пришлите ссылку иначе")
    ent = (((json.loads(mm.group(1)).get('props') or {}).get('pageProps') or {}).get('state') or {}).get('data', {}).get('entity') or {}
    cover = ''
    imgs = (ent.get('visualIdentity') or {}).get('image') or ent.get('coverArt', {}).get('sources') or []
    if imgs:
        cover = imgs[-1].get('url', '')
    if kind == 'track':
        artists = ', '.join(a.get('name', '') for a in ent.get('artists') or [])
        dur = int((ent.get('duration') or 0) / 1000)
        return [item(ent.get('name') or ent.get('title'), artists, dur, {'t': 'match', 'q': f"{artists} - {ent.get('name') or ent.get('title')}", 'dur': dur},
                     cover, 'Spotify')]
    out = []
    for t in ent.get('trackList') or []:
        dur = int((t.get('duration') or 0) / 1000)
        out.append(item(t.get('title'), t.get('subtitle', ''), dur, {'t': 'match', 'q': f"{t.get('subtitle', '')} - {t.get('title')}", 'dur': dur},
                        cover, 'Spotify'))
    return out


# =========================================================================== YouTube cookies

async def yt_cookies_set(path_or_bytes):
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    text = data.decode('utf-8', 'replace')
    if 'youtube.com' not in text or ('# Netscape' not in text and '\t' not in text):
        raise RuntimeError("это не cookies.txt (Netscape) с youtube.com — выгрузите его расширением «Get cookies.txt LOCALLY»")
    (data_dir() / 'youtube_cookies.txt').write_bytes(data)


YT_BROWSERS = ['firefox', 'chrome', 'edge', 'opera', 'brave', 'vivaldi', 'chromium']


async def yt_from_browser(browser):
    """Вход в YouTube через браузер на этом компьютере: берём cookies youtube.com/google.com из вашего браузера
    (вы там уже вошли в аккаунт) и сохраняем как cookies.txt. Пароль никуда не вводится."""
    if browser not in YT_BROWSERS:
        raise RuntimeError("неизвестный браузер")
    await ytdlp()
    from http.cookiejar import MozillaCookieJar

    def run():
        from yt_dlp.cookies import extract_cookies_from_browser
        jar = extract_cookies_from_browser(browser)
        out = MozillaCookieJar(str(data_dir() / 'youtube_cookies.txt'))
        n = 0
        for c in jar:
            if any(d in (c.domain or '') for d in ('youtube.com', 'google.com', 'youtu.be')):
                out.set_cookie(c)
                n += 1
        if not any(c.name in ('SAPISID', '__Secure-3PAPISID', 'LOGIN_INFO') for c in out):
            raise RuntimeError("в этом браузере вы не вошли в YouTube — откройте youtube.com, войдите и повторите")
        out.save(ignore_discard=True, ignore_expires=True)
        return n
    try:
        n = await asyncio.get_running_loop().run_in_executor(None, run)
    except RuntimeError:
        raise
    except Exception as e:
        msg = str(e)
        if browser in ('chrome', 'edge', 'brave', 'opera', 'vivaldi', 'chromium') and os.name == 'nt':
            msg += (" — новые Chrome/Edge на Windows шифруют cookies так, что их не прочитать снаружи (и пока браузер "
                    "открыт, файл занят). Закройте браузер и повторите, выберите Firefox или загрузите cookies.txt")
        raise RuntimeError(msg[:400])
    await dset('yt_browser', browser)
    return n


async def yt_account():
    """Проверка входа: открываем «Понравившиеся» (доступны только со входом)."""
    info = await ydl_info('https://www.youtube.com/playlist?list=LL', {'extract_flat': 'in_playlist', 'noplaylist': False, 'playlistend': 1})
    return info.get('channel') or info.get('uploader') or 'аккаунт'


def yt_cookies_on():
    return (data_dir() / 'youtube_cookies.txt').is_file()


def yt_cookies_drop():
    (data_dir() / 'youtube_cookies.txt').unlink(missing_ok=True)


async def services_state():
    sp = await dget('sp_auth')
    ymt = await dget('ym_token')
    if ymt and _ym.get('login') is None:
        try:
            await ym_client()
        except Exception:
            pass
    dev = _ym.get('device')
    return {
        'ym': {'on': bool(ymt), 'login': _ym.get('login'),
               'device': {'code': dev['user_code'], 'url': dev['url'], 'left': max(0, int(dev['until'] - time.time()))} if dev else None},
        'sp': {'on': bool(sp), 'name': (sp or {}).get('name'), 'pending': bool(await dget('sp_pending')),
               'client_id': (sp or {}).get('client_id') or (await dget('sp_pending') or {}).get('client_id') or '',
               'redirect': SP_REDIRECT},
        'zv': {'on': bool(await dget('zv_token')), 'name': await dget('zv_name')},
        'yt': {'on': yt_cookies_on(), 'browser': await dget('yt_browser'), 'browsers': YT_BROWSERS},
    }


# =========================================================================== Telegram-медиа потоком

class TgStream:
    """Маленький HTTP-сервер на 127.0.0.1: отдаёт файл из Telegram кусками прямо во время скачивания (с Range —
    ffmpeg может перематывать). Так голосовое/аудио/видео из чата начинает играть сразу, а не после загрузки."""
    CHUNK = 512 * 1024

    def __init__(self):
        self.runner = None
        self.port = None
        self.tokens = {}

    async def url(self, src):
        if self.runner is None:
            from aiohttp import web as aweb
            app = aweb.Application()
            app.router.add_get('/tg/{token}', self.handle)
            self.runner = aweb.AppRunner(app, access_log=None)
            await self.runner.setup()
            site = aweb.TCPSite(self.runner, '127.0.0.1', 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
        token = secrets.token_urlsafe(12)
        self.tokens[token] = src
        return f"http://127.0.0.1:{self.port}/tg/{token}"

    async def handle(self, request):
        from aiohttp import web as aweb
        src = self.tokens.get(request.match_info['token'])
        if not src:
            return aweb.Response(status=404)
        msg = await engine.client.get_messages(src['chat'], ids=src['msg'])
        if msg is None or msg.file is None:
            return aweb.Response(status=404)
        size = msg.file.size or 0
        start, end = 0, size - 1
        rng = request.headers.get('Range', '')
        m = re.match(r'bytes=(\d*)-(\d*)', rng)
        if m and size:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
        if size and start >= size:
            return aweb.Response(status=416, headers={'Content-Range': f'bytes */{size}'})
        resp = aweb.StreamResponse(status=206 if m else 200, headers={
            'Content-Type': msg.file.mime_type or 'application/octet-stream', 'Accept-Ranges': 'bytes',
            'Content-Length': str(end - start + 1)})
        if m:
            resp.headers['Content-Range'] = f'bytes {start}-{end}/{size}'
        await resp.prepare(request)
        aligned = start - start % self.CHUNK
        skip, left = start - aligned, end - start + 1
        try:
            async for chunk in engine.client.iter_download(msg.media, offset=aligned, request_size=self.CHUNK, file_size=size):
                if skip:
                    chunk, skip = chunk[skip:], 0
                if len(chunk) > left:
                    chunk = chunk[:left]
                await resp.write(chunk)
                left -= len(chunk)
                if left <= 0:
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def close(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None


TG = TgStream()


# =========================================================================== поток для py-tgcalls

_info_cache = {}        # ссылка → (время, info yt-dlp): переключение «видео/звук» и повтор трека не ждут yt-dlp заново
_match_cache = {}       # «исполнитель - трек» → ссылка на найденную полную версию
INFO_TTL = 20 * 60


async def _info(url):
    hit = _info_cache.get(url)
    if hit and time.monotonic() - hit[0] < INFO_TTL:
        return hit[1]
    info = await ydl_info(url)
    if info.get('entries'):
        info = info['entries'][0]
    _info_cache[url] = (time.monotonic(), info)
    if len(_info_cache) > 200:
        for k in sorted(_info_cache, key=lambda k: _info_cache[k][0])[:50]:
            _info_cache.pop(k, None)
    return info


def _pick_formats(info, video, max_h):
    """Сами выбираем дорожки из одного ответа yt-dlp: звук (m4a лучше всего), видео до max_h (h264 — его проще
    кодировать на лету). → (видео|None, звук|None, комбинированный|None)."""
    fmts = [f for f in info.get('formats') or [] if f.get('url')]
    if not fmts:
        return None, None, info
    audio_only = [f for f in fmts if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none')]
    combined = [f for f in fmts if f.get('vcodec') not in (None, 'none') and f.get('acodec') not in (None, 'none')]
    best_a = max(audio_only, key=lambda f: ((f.get('ext') == 'm4a'), f.get('abr') or f.get('tbr') or 0), default=None)
    if not video:
        if best_a:
            return None, best_a, None
        return None, None, max(combined or fmts, key=lambda f: (-(f.get('height') or 0), f.get('tbr') or 0))
    vids = [f for f in fmts if f.get('vcodec') not in (None, 'none') and (f.get('height') or 0) <= max_h
            and not str(f.get('vcodec', '')).startswith('av01')]
    v_only = [f for f in vids if f.get('acodec') == 'none']
    score = lambda f: ((f.get('height') or 0), str(f.get('vcodec', '')).startswith('avc'), f.get('tbr') or 0)
    if v_only and best_a:
        return max(v_only, key=score), best_a, None
    comb = [f for f in vids if f.get('acodec') not in (None, 'none')]
    if comb:
        return None, None, max(comb, key=score)
    return None, None, max(combined or fmts, key=lambda f: f.get('tbr') or 0)


async def resolve(it, video):
    """Элемент → dict(media, audio, headers, live, w, h). Ссылки YouTube/Яндекса живут недолго — их берём
    перед игрой, но ответ yt-dlp кэшируется (смена видео/звук, перемотка и повтор — без новой загрузки)."""
    src = it.get('src') or {}
    t = src.get('t')
    if t == 'match':
        q = src.get('q') or label(it)
        url = _match_cache.get(q)
        if url is None:
            url = _match_cache[q] = await match_youtube(q, src.get('dur') or it.get('duration') or 0)
        src = {'t': 'ydl', 'url': url}
        t = 'ydl'
    if t == 'zv':
        return {'media': await zv_stream_url(src['id']), 'video': False}
    if t == 'ym':
        return {'media': await ym_stream_url(src['id']), 'video': False}
    if t == 'tg':
        return {'media': await TG.url(src), 'video': None}
    if t == 'file':
        return {'media': src['path'], 'video': None}
    if t == 'url':
        return {'media': src['url'], 'live': bool(it.get('live')), 'video': None}
    if t == 'ydl':
        info = await _info(src['url'])
        if not it.get('duration') and info.get('duration'):
            it['duration'] = int(info['duration'])
        if not it.get('thumb') and info.get('thumbnail'):
            it['thumb'] = info['thumbnail']
        live = bool(info.get('is_live'))
        v, a, comb = _pick_formats(info, video, int(await cfg('video_quality') or 720))
        if v and a:
            return {'media': v['url'], 'audio': a['url'], 'headers': v.get('http_headers') or info.get('http_headers'),
                    'live': live, 'video': True, 'w': v.get('width'), 'h': v.get('height')}
        if a:
            return {'media': a['url'], 'headers': a.get('http_headers') or info.get('http_headers'), 'live': live, 'video': False}
        f = comb or info
        has_v = f.get('vcodec') not in (None, 'none') if f.get('vcodec') else None
        return {'media': f.get('url') or info.get('url'), 'headers': f.get('http_headers') or info.get('http_headers'),
                'live': live, 'video': has_v if video else False, 'w': f.get('width'), 'h': f.get('height')}
    raise RuntimeError("неизвестный источник")


def make_stream(r, want_video, aq, vq, seek):
    """MediaStream без лишних проверок: если про поток всё известно (yt-dlp сказал, есть ли видео и какого размера),
    не запускаем ffprobe по сети — это экономит 1–5 секунд на каждом треке и перемотке."""
    from pytgcalls.types import MediaStream
    from pytgcalls.ffmpeg import build_command
    from pytgcalls.list_to_cmd import list_to_cmd
    import pytgcalls.ffmpeg as pf

    ff = f"-ss {int(seek)}" if seek and not r.get('live') else None
    show_video = bool(want_video and r.get('video') is not False)
    known = r.get('video') is not None and (not show_video or (r.get('w') and r.get('h')))

    class FastStream(MediaStream):
        async def check_stream(self):
            if not known:
                return await super().check_stream()
            if self.camera is not None:
                vp = self._video_parameters
                ow, oh = int(r['w']), int(r['h'])
                ratio = ow / oh
                nw = min(ow, vp.width)
                nh = int(nw / ratio)
                if nh > vp.height:
                    nh = vp.height
                    nw = int(nh * ratio)
                vp.width, vp.height = nw - nw % 2, nh - nh % 2
                self.camera.path = list_to_cmd(await pf.cleanup_commands(build_command(
                    'ffmpeg', self._ffmpeg_parameters, self._media_path, vp, [], self._headers, bool(r.get('live')))))
            if self.microphone is not None:
                path = self._audio_path or self._media_path
                self._audio_path = path
                self.microphone.path = list_to_cmd(await pf.cleanup_commands(build_command(
                    'ffmpeg', self._ffmpeg_parameters, path, self._audio_parameters, [], self._headers, bool(r.get('live')))))

    if show_video:
        return FastStream(r['media'], audio_parameters=aq, video_parameters=vq, audio_path=r.get('audio'),
                          headers=r.get('headers'), ffmpeg_parameters=ff, video_flags=MediaStream.Flags.AUTO_DETECT)
    return FastStream(r.get('audio') or r['media'], audio_parameters=aq, headers=r.get('headers'), ffmpeg_parameters=ff,
                      video_flags=MediaStream.Flags.IGNORE)


async def prefetch(it, video):
    """Заранее разрешить следующий трек (поиск полной версии, ответ yt-dlp) — переход без паузы."""
    try:
        src = it.get('src') or {}
        if src.get('t') in ('match', 'ydl'):
            await resolve(it, video)
    except Exception:
        pass


# =========================================================================== плеер

LOOP_MODES = ['off', 'queue', 'track']
LOOP_ICON = {'off': '➡️', 'queue': '🔁', 'track': '🔂'}
PLAYERS = {}          # chat_id -> Player


class Player:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.title = str(chat_id)
        self.queue = []
        self.history = []
        self.current = None
        self.state = 'idle'            # idle | loading | playing | paused | error
        self.error = None
        self.video = False
        self.volume = 100
        self.loop = 'off'
        self.seek_base = 0
        self.started = 0.0
        self.paused_at = None
        self.paused_total = 0.0
        self.token = None
        self.lock = asyncio.Lock()
        self.panels = []
        self.wave = None               # станция Яндекса для бесконечной «волны»
        self.synced = False
        self.t0 = None
        self.pending = 0               # сколько фоновых действий ещё идёт (сайт показывает «загрузка…»)
        self._panel_task = None

    # ---------------------------------------------------------------- время
    def position(self):
        if not self.current or self.state not in ('playing', 'paused'):
            return 0
        now = self.paused_at or time.monotonic()
        return max(0, int(self.seek_base + now - self.started - self.paused_total))

    def snapshot(self):
        cur = self.current
        return {'chat': self.chat_id, 'title': self.title, 'state': 'loading' if self.pending else self.state, 'error': self.error, 'video': self.video,
                'volume': self.volume, 'loop': self.loop, 'position': self.position(), 'wave': bool(self.wave),
                'current': _pub(cur) if cur else None, 'queue': [_pub(x) for x in self.queue[:200]],
                'queue_total': len(self.queue), 'history': len(self.history)}

    # ---------------------------------------------------------------- воспроизведение
    async def _start(self, it, seek=0):
        from pytgcalls.types import AudioQuality, VideoQuality
        calls = await get_calls()
        self.state, self.error, self.current = 'loading', None, it
        self.changed()
        want_video = bool(self.video or it.get('video'))
        r = await resolve(it, want_video)
        it['live'] = it.get('live') or bool(r.get('live'))
        aq = {'studio': AudioQuality.STUDIO, 'high': AudioQuality.HIGH, 'medium': AudioQuality.MEDIUM}.get(
            await cfg('audio_quality'), AudioQuality.HIGH)
        vq = {360: VideoQuality.SD_360p, 480: VideoQuality.SD_480p, 720: VideoQuality.HD_720p,
              1080: VideoQuality.FHD_1080p}.get(int(await cfg('video_quality') or 720), VideoQuality.HD_720p)
        self.token = secrets.token_hex(4)
        await calls.play(self.chat_id, make_stream(r, want_video, aq, vq, seek))
        self.seek_base, self.started, self.paused_at, self.paused_total = seek, time.monotonic(), None, 0.0
        self.synced = False
        self.t0 = None
        try:                                # отметка часов звонка в момент запуска: позиция = часы − отметка
            self.t0 = await calls.time(self.chat_id)
        except Exception:
            self.t0 = None
        self.state = 'playing'
        if self.volume != 100:
            try:
                await calls.change_volume_call(self.chat_id, self.volume)
            except Exception:
                pass
        self.changed()
        if it.get('wave') and len(self.queue) < 2:
            asyncio.ensure_future(self._refill_wave())
        if self.queue:
            asyncio.ensure_future(prefetch(self.queue[0], self.video or self.queue[0].get('video')))

    async def sync_position(self):
        """Позиция — по часам самого звонка (ntgcalls): первые секунды уходят на буферизацию, и наш таймер
        иначе убегал бы вперёд звука."""
        if self.state != 'playing' or RT.calls is None:
            return
        try:
            t = await RT.calls.time(self.chat_id)
        except Exception:
            return
        if t is None or t < 0 or self.t0 is None:
            return
        played = t - self.t0
        if played < 0 or played > (time.monotonic() - self.started) + 5:     # часы звонка ведут себя иначе — не трогаем
            return
        self.started = time.monotonic() - played
        self.paused_total = 0.0
        self.synced = True

    def run(self, coro):
        """Долгое действие (переход, перемотка, видео) — в фоне: кнопки и сайт отвечают сразу, а состояние
        «загрузка…» видно, пока поток не пошёл."""
        async def wrap():
            try:
                await coro
            except Exception as e:
                self.state, self.error = ('error' if not self.current else self.state), str(e)[:200]
                engine.tlog(f"[voinje] {e}")
            finally:
                self.pending = max(0, self.pending - 1)
                self.changed()
        self.pending += 1
        self.changed()
        return asyncio.ensure_future(wrap())

    async def play_next(self, skip_error=0):
        """Следующий из очереди (с учётом повтора). Пусто — тишина (из звонка не выходим, ждём новых треков)."""
        async with self.lock:
            await self._next_locked(skip_error)

    async def _next_locked(self, skip_error=0):
        prev = self.current
        if prev is not None:
            if self.loop == 'track' and skip_error == 0:
                self.queue.insert(0, prev)
            else:
                self.history.append(prev)
                self.history = self.history[-100:]
                if self.loop == 'queue':
                    self.queue.append(dict(prev, id=secrets.token_hex(4)))
        if not self.queue and self.wave:
            await self._refill_wave()
        if not self.queue:
            self.current, self.state = None, 'idle'
            self.changed()
            if await cfg('leave_when_empty'):
                await self.stop(leave=True, locked=True)
            return
        it = self.queue.pop(0)
        try:
            await self._start(it)
        except Exception as e:
            self.state, self.error = 'error', f"{label(it)}: {str(e)[:200]}"
            engine.tlog(f"[voinje] {self.error}")
            self.current = None
            self.changed()
            if skip_error < 5 and self.queue:
                await self._next_locked(skip_error + 1)

    async def add(self, items, now=False):
        """В очередь (now — сразу играть первым). Если ничего не играет — запускаем."""
        items = [x for x in items if x]
        if not items:
            return 0
        async with self.lock:
            if now:
                self.queue[0:0] = items
            else:
                self.queue.extend(items)
            if now or self.current is None or self.state in ('idle', 'error'):
                if now and self.current is not None:
                    self.history.append(self.current)
                    self.current = None
                await self._next_locked()
            else:
                self.changed()
        return len(items)

    async def _refill_wave(self):
        if not self.wave:
            return
        try:
            after = self.current['src'].get('id', '').split(':')[0] if self.current and self.current.get('wave') else None
            more = await ym_wave(self.wave, after)
            known = {x['src'].get('id') for x in self.queue} | ({self.current['src'].get('id')} if self.current else set())
            self.queue.extend(x for x in more if x['src'].get('id') not in known)
            self.changed()
        except Exception as e:
            engine.tlog(f"[voinje] волна: {e}")

    async def skip(self):
        await self.play_next()

    async def prev(self):
        async with self.lock:
            if self.position() > 5 and self.current:
                await self._start(self.current)
                return
            if not self.history:
                return
            back = self.history.pop()
            if self.current:
                self.queue.insert(0, self.current)
            self.current = None
            self.queue.insert(0, back)
            await self._next_locked()

    async def pause(self):
        calls = await get_calls()
        if self.state == 'playing':
            await calls.pause(self.chat_id)
            self.paused_at, self.state = time.monotonic(), 'paused'
            self.changed()

    async def resume(self):
        calls = await get_calls()
        if self.state == 'paused':
            await calls.resume(self.chat_id)
            self.paused_total += time.monotonic() - (self.paused_at or time.monotonic())
            self.paused_at, self.state = None, 'playing'
            self.changed()

    async def toggle(self):
        await (self.resume() if self.state == 'paused' else self.pause())

    async def seek(self, sec):
        if not self.current or self.current.get('live'):
            return
        sec = max(0, int(sec))
        if self.current.get('duration'):
            sec = min(sec, max(0, self.current['duration'] - 3))
        async with self.lock:
            await self._start(self.current, sec)

    async def set_volume(self, v):
        self.volume = max(1, min(200, int(v)))
        try:
            await (await get_calls()).change_volume_call(self.chat_id, self.volume)
        except Exception:
            pass
        self.changed()

    async def set_video(self, on):
        self.video = bool(on)
        if self.current and self.state in ('playing', 'paused'):
            async with self.lock:
                await self._start(self.current, self.position())
        self.changed()

    def cycle_loop(self):
        self.loop = LOOP_MODES[(LOOP_MODES.index(self.loop) + 1) % len(LOOP_MODES)]
        self.changed()

    def shuffle(self):
        random.shuffle(self.queue)
        self.changed()

    def remove(self, iid):
        self.queue = [x for x in self.queue if x['id'] != iid]
        self.changed()

    def move(self, iid, delta):
        i = next((n for n, x in enumerate(self.queue) if x['id'] == iid), None)
        if i is None:
            return
        j = max(0, min(len(self.queue) - 1, i + delta))
        self.queue.insert(j, self.queue.pop(i))
        self.changed()

    async def jump(self, iid):
        async with self.lock:
            i = next((n for n, x in enumerate(self.queue) if x['id'] == iid), None)
            if i is None:
                return
            it = self.queue.pop(i)
            self.queue.insert(0, it)
            await self._next_locked()

    async def stop(self, leave=True, locked=False):
        self.queue.clear()
        self.wave = None
        if self.current:
            self.history.append(self.current)
        self.current, self.state = None, 'idle'
        if leave and RT.calls is not None:
            try:
                await RT.calls.leave_call(self.chat_id)
            except Exception:
                pass
        self.changed()
        if leave:
            PLAYERS.pop(self.chat_id, None)
            for f in list(self.panels):
                try:
                    await f.edit(visual.hint("Voinje: воспроизведение остановлено"), buttons=None)
                    await f.close()
                except Exception:
                    pass
            self.panels.clear()

    # ---------------------------------------------------------------- события
    def changed(self):
        """Что-то поменялось — панели в Telegram обновятся (не чаще раза в 2 с)."""
        try:                                        # «слушает …» для других модулей (Aura): RT.now_by = {чат: трек}
            now = getattr(RT, 'now_by', None)
            if now is None:
                now = RT.now_by = {}
            if self.state == 'playing' and self.current:
                now[self.chat_id] = label(self.current)
            else:
                now.pop(self.chat_id, None)
        except Exception:
            pass
        if self.panels and (self._panel_task is None or self._panel_task.done()):
            self._panel_task = asyncio.ensure_future(self._refresh_panels())

    async def _refresh_panels(self):
        await asyncio.sleep(1.5)
        for f in list(self.panels):
            try:
                await f.edit(panel_text(self), buttons=panel_buttons(self))
            except Exception as e:
                if 'not modified' not in str(e).lower():
                    self.panels.remove(f)


def _pub(it):
    out = {k: it.get(k) for k in ('id', 'title', 'artist', 'duration', 'thumb', 'service', 'live', 'video')}
    src = it.get('src') or {}
    if src.get('t') == 'ym':                         # трек Яндекса — на сайте можно лайкнуть
        out['ym'] = src['id']
        liked = _ym.get('liked')
        out['liked'] = (src['id'].split(':')[0] in liked) if liked is not None else False
    return out


async def player(chat_id, title=None):
    p = PLAYERS.get(chat_id)
    if p is None:
        p = PLAYERS[chat_id] = Player(chat_id)
        p.volume = int(await cfg('volume') or 100)
        try:
            ent = await engine.client.get_entity(chat_id)
            p.title = getattr(ent, 'title', None) or getattr(ent, 'first_name', None) or str(chat_id)
        except Exception:
            pass
    if title:
        p.title = title
    return p


async def _on_tgcalls(kind, update):
    if kind != 'end':
        return
    from pytgcalls.types import StreamEnded
    p = PLAYERS.get(getattr(update, 'chat_id', None))
    if p is None or p.state not in ('playing', 'paused'):
        return
    cur = p.current or {}
    has_video = bool(p.video or cur.get('video'))
    st = getattr(update, 'stream_type', None)
    if has_video and st is not None and st & StreamEnded.Type.VIDEO and not (st & StreamEnded.Type.AUDIO):
        return                                     # у видео ждём конца звука — иначе трек «закончится» дважды
    token = p.token
    await asyncio.sleep(0.3)
    if p.token == token:
        await p.play_next()

RT.dispatch = _on_tgcalls


async def _ticker():
    """Раз в 15 с — обновить полоску прогресса в панелях Telegram."""
    while True:
        await asyncio.sleep(15)
        for p in list(PLAYERS.values()):
            if p.state == 'playing':
                await p.sync_position()
                if p.panels:
                    p.changed()


# =========================================================================== Telegram: панель и команды

SOURCES = [('std', '🎵 Музыка'), ('ym', '🟡 Яндекс'), ('yt', '▶️ YouTube'), ('sc', '☁️ SoundCloud'), ('sp', '🟢 Spotify'),
           ('radio', '📻 Радио'), ('tg', '💾 Избранное'), ('files', '📁 Файлы')]
SOURCE_NAME = dict(SOURCES)


async def search(source, q, n=12):
    if source == 'std':
        return await search_std(q, n)
    if source in ('yt', 'sc'):
        return await search_ydl(q, source, n)
    if source == 'ym':
        return await ym_search(q, n)
    if source == 'sp':
        return await sp_search(q, n)
    if source == 'radio':
        return await search_radio(q, n)
    if source == 'tg':
        return await search_saved(q, max(n, 40))
    if source == 'files':
        return await search_files(q)
    raise RuntimeError("неизвестный источник")


async def resolve_query(text, video=False):
    """Текст команды → [элементы]: ссылка — по ней, иначе — первый результат поиска в источнике по умолчанию."""
    m = URL_RE.search(text)
    if m:
        items = await from_url(m.group(0), video)
    else:
        src = await cfg('source') or 'std'
        items = (await search(src, text, 5))[:1]
        if not items and src != 'yt':
            items = (await search_ydl(text, 'yt', 3))[:1]
    for x in items:
        x['video'] = x.get('video') or video
    return items


def _bar(p):
    cur = p.current
    if not cur:
        return ''
    if cur.get('live'):
        return "🔴 прямой эфир · " + _dur(p.position())
    total = cur.get('duration') or 0
    pos = p.position()
    pct = pos / total * 100 if total else 0
    return f"<code>{engine.progress_bar(pct, 14, 'line')}</code>\n{_dur(pos)} / {_dur(total)}"


def panel_text(p):
    state = {'playing': '▶️ играет', 'paused': '⏸ пауза', 'loading': '⏳ загружаю…', 'idle': '⏹ тишина',
             'error': '⚠️ ошибка'}.get('loading' if p.pending else p.state, p.state)
    lines = [visual.title("Voinje", sub=f"{_esc(p.title)} · {state}")]
    cur = p.current
    if cur:
        lines.append(f"{'🎬' if (p.video or cur.get('video')) else '🎵'} <b>{_esc(cur['title'])}</b>")
        if cur.get('artist'):
            lines.append(f"<i>{_esc(cur['artist'])}</i>  · {_esc(cur.get('service') or '')}")
        lines.append(_bar(p))
    elif p.error:
        lines.append(visual.err(_esc(p.error)))
    else:
        lines.append(visual.hint("очередь пуста — добавьте трек кнопкой ➕ или .vplay"))
    if p.queue:
        lines.append("")
        lines.append(visual.section(f"Дальше · {len(p.queue)}"))
        for i, x in enumerate(p.queue[:4], 1):
            lines.append(f"{i}. {_esc(label(x))[:60]}  <i>{_dur(x.get('duration'))}</i>")
        if len(p.queue) > 4:
            lines.append(visual.hint(f"… и ещё {len(p.queue) - 4}"))
    extra = [f"🔊 {p.volume}%", LOOP_ICON[p.loop] + {'off': ' без повтора', 'queue': ' очередь', 'track': ' трек'}[p.loop]]
    if p.wave:
        extra.append("🌊 волна")
    if p.video:
        extra.append("🎬 видео")
    lines.append("")
    lines.append(visual.hint(" · ".join(extra)))
    return "\n".join(lines)


def panel_buttons(p):
    play = ("⏸", "vj:pause") if p.state == 'playing' else ("▶️", "vj:resume", "success")
    return [
        [("⏮", "vj:prev"), play, ("⏭", "vj:skip")],
        [("⏪ 15", "vj:back"), ("🔉", "vj:vol:-"), ("🔊", "vj:vol:+"), ("15 ⏩", "vj:fwd")],
        [(LOOP_ICON[p.loop], "vj:loop"), ("🔀", "vj:shuffle"), ("🎬" if not p.video else "🎵", "vj:video"),
         ("📜 Очередь", "vj:queue")],
        [engine.InputButton("➕ Добавить", "add", hint="название, «исполнитель - трек» или ссылка"),
         ("⏹ Стоп", "vj:stop", "danger")],
        [(visual.btn('Скрыть панель', 'close'), "vj:hide")]]


async def open_panel(event, p):
    everyone = await cfg('everyone_controls')
    form = await engine.create_inlineform(event.chat_id, panel_text(p), buttons=panel_buttons(p),
                                          for_user=['me', event.chat_id] if everyone else None)
    p.panels.append(form)
    st = {'view': 'main', 'page': 0}

    async def show_main():
        st['view'] = 'main'
        await form.edit(panel_text(p), buttons=panel_buttons(p))

    async def show_queue():
        st['view'] = 'queue'
        per = 6
        pages = max(1, -(-len(p.queue) // per))
        st['page'] = max(0, min(st['page'], pages - 1))
        chunk = p.queue[st['page'] * per:(st['page'] + 1) * per]
        lines = [visual.title("Очередь", sub=f"{len(p.queue)} · стр. {st['page'] + 1}/{pages}")]
        rows = []
        for n, x in enumerate(chunk, st['page'] * per + 1):
            lines.append(f"{n}. {_esc(label(x))[:70]}  <i>{_dur(x.get('duration'))}</i>")
            rows.append([(f"▶ {n}", f"vj:jump:{x['id']}"), ("⬆", f"vj:up:{x['id']}"), ("✕", f"vj:rm:{x['id']}", "danger")])
        if not p.queue:
            lines.append(visual.hint("пусто"))
        nav = ([("◂", "vj:qp:-1")] if st['page'] > 0 else []) + ([("▸", "vj:qp:1")] if st['page'] < pages - 1 else [])
        if nav:
            rows.append(nav)
        rows.append([("🧹 Очистить", "vj:clear", "danger"), (visual.btn('Назад', 'back'), "vj:main")])
        await form.edit("\n".join(lines), buttons=rows)

    @form.on_click(data_startswith='vj:')
    async def _(click):
        d = click.data[3:]
        try:
            if d == 'hide':
                if form in p.panels:
                    p.panels.remove(form)
                await form.delete()
                return
            if d == 'pause':
                await p.pause()
            elif d == 'resume':
                await p.resume()
            elif d == 'skip':
                await click.answer("дальше…")
                p.run(p.skip())
            elif d == 'prev':
                p.run(p.prev())
            elif d in ('back', 'fwd'):
                p.run(p.seek(p.position() + (15 if d == 'fwd' else -15)))
            elif d.startswith('vol:'):
                await p.set_volume(p.volume + (10 if d.endswith('+') else -10))
                await click.answer(f"громкость {p.volume}%")
            elif d == 'loop':
                p.cycle_loop()
            elif d == 'shuffle':
                p.shuffle()
                await click.answer("перемешано")
            elif d == 'video':
                await click.answer("переключаю…")
                p.run(p.set_video(not p.video))
            elif d == 'stop':
                await p.stop()
                return
            elif d == 'queue':
                st['page'] = 0
                await show_queue()
                return
            elif d == 'main':
                await show_main()
                return
            elif d.startswith('qp:'):
                st['page'] += int(d[3:])
                await show_queue()
                return
            elif d.startswith('jump:'):
                p.run(p.jump(d[5:]))
            elif d.startswith('up:'):
                p.move(d[3:], -1)
                await show_queue()
                return
            elif d.startswith('rm:'):
                p.remove(d[3:])
                await show_queue()
                return
            elif d == 'clear':
                p.queue.clear()
                p.wave = None
                p.changed()
                await show_queue()
                return
            await click.answer()
            if st['view'] == 'main':
                await show_main()
        except Exception as e:
            await click.answer(f"ошибка: {str(e)[:150]}", show_alert=True)

    @form.on_input(data='add')
    async def _(inp):
        q = (inp.value or '').strip()
        if not q:
            return
        try:
            n = await p.add(await resolve_query(q, p.video))
            if not n:
                raise RuntimeError("ничего не нашлось")
        except Exception as e:
            p.error = str(e)[:200]
        await show_main()
    return form


async def _chat_player(event):
    if event.is_private and event.chat_id == (await engine.client.get_me()).id:
        raise RuntimeError("в Избранном голосового чата нет — команда работает в группе или канале с голосовым чатом")
    return await player(event.chat_id)


async def _prepare(event):
    """Проверить окружение, при первом запуске — скачать ffmpeg с прогрессом."""
    if not _ff_ready():
        async def prog(done, total):
            pct = done / total * 100 if total else 0
            await engine.answer(event, visual.wait("Voinje: первый запуск — скачиваю ffmpeg…") +
                                f"\n<code>{engine.progress_bar(pct, 14, 'bar')}</code> {done / 1048576:.0f} / {total / 1048576:.0f} МБ")
        await engine.answer(event, visual.wait("Voinje: первый запуск — скачиваю ffmpeg (один раз, ~100 МБ)…"))
        await ensure_ffmpeg(prog)
    if RT.calls is None or RT.client is not engine.client:
        await engine.answer(event, visual.wait("Voinje: подключаю голосовые чаты…"))
        await get_calls()


async def _play_cmd(event, video):
    q = engine.get_args().strip()
    try:
        p = await _chat_player(event)
        await _prepare(event)
        items = []
        r = await event.get_reply_message() if event.is_reply else None
        if not q and r is not None and r.file is not None:
            items = [tg_item(r, True if video else None)]
        elif not q and r is not None and r.raw_text:
            q = r.raw_text.strip()
        if not items:
            if not q:
                if p.current or p.queue:
                    await event.delete()
                    await open_panel(event, p)
                    return
                await engine.answer(event, visual.err("Что играть?") + "\n" + visual.hint(
                    ".vplay запрос или ссылка · ответом на аудио/видео · .vsearch — поиск с выбором источника"))
                return
            await engine.answer(event, visual.wait(f"Ищу «{_esc(q[:80])}»…"))
            items = await resolve_query(q, video)
        if not items:
            await engine.answer(event, visual.err("Ничего не нашлось"))
            return
        if video:
            p.video = True
        playing = p.current is not None and p.state in ('playing', 'paused', 'loading')
        await p.add(items)
        what = label(items[0]) + (f" и ещё {len(items) - 1}" if len(items) > 1 else '')
        if p.state == 'error' and not p.current:
            await engine.answer(event, visual.err("Не запустилось") + f"\n<code>{_esc(p.error or '')}</code>")
            return
        if playing:
            await engine.answer(event, visual.ok("В очереди") + f"  {_esc(what)}")
        else:
            await event.delete()
        if not p.panels:
            await open_panel(event, p)
    except Exception as e:
        await engine.answer(event, visual.err("Voinje") + f"\n<code>{_esc(str(e)[:400])}</code>")


@decorators.command('vplay', 'vp')
async def vplay_cmd(event):
    """играть в голосовом чате: <запрос | ссылка> или ответом на аудио/видео; без аргументов — панель"""
    await _play_cmd(event, False)


@decorators.command('vvideo', 'vv')
async def vvideo_cmd(event):
    """видео в голосовом чате: <запрос | ссылка> или ответом на видео"""
    await _play_cmd(event, True)


async def _with_player(event, fn, ok_text=None):
    p = PLAYERS.get(event.chat_id)
    if p is None:
        await engine.answer(event, visual.hint("Здесь ничего не играет — .vplay"))
        return
    try:
        res = await fn(p)
        await engine.answer(event, visual.ok(ok_text or res or "Готово"))
    except Exception as e:
        await engine.answer(event, visual.err(_esc(str(e)[:300])))


@decorators.command('vskip', 'vnext')
async def vskip_cmd(event):
    """следующий трек"""
    await _with_player(event, lambda p: p.skip(), "Дальше")


@decorators.command('vpause')
async def vpause_cmd(event):
    """пауза"""
    await _with_player(event, lambda p: p.pause(), "Пауза")


@decorators.command('vresume')
async def vresume_cmd(event):
    """продолжить"""
    await _with_player(event, lambda p: p.resume(), "Играет")


@decorators.command('vstop')
async def vstop_cmd(event):
    """остановить и выйти из голосового чата"""
    await _with_player(event, lambda p: p.stop(), "Остановлено")


@decorators.command('vvol')
async def vvol_cmd(event):
    """громкость: <1–200>"""
    v = engine.get_args().strip().rstrip('%')
    if not v.isdigit():
        await engine.answer(event, visual.err("Громкость — число 1–200: .vvol 80"))
        return
    await _with_player(event, lambda p: p.set_volume(int(v)), f"Громкость {min(200, max(1, int(v)))}%")


@decorators.command('vseek')
async def vseek_cmd(event):
    """перемотка: <1:30 | 90 | +15 | -15>"""
    t = _parse_time(engine.get_args())
    if t is None:
        await engine.answer(event, visual.err("Например: .vseek 1:30 · .vseek +15"))
        return

    async def go(p):
        target = p.position() + t[1] if t[0] == 'rel' else t[1]
        await p.seek(target)
        return f"Перемотано на {_dur(target)}"
    await _with_player(event, go)


@decorators.command('vloop')
async def vloop_cmd(event):
    """повтор: выкл → очередь → трек"""
    async def go(p):
        p.cycle_loop()
        return {'off': 'Без повтора', 'queue': 'Повтор очереди', 'track': 'Повтор трека'}[p.loop]
    await _with_player(event, go)


@decorators.command('vshuffle')
async def vshuffle_cmd(event):
    """перемешать очередь"""
    async def go(p):
        p.shuffle()
        return "Очередь перемешана"
    await _with_player(event, go)


@decorators.command('vq', 'vpanel')
async def vpanel_cmd(event):
    """панель плеера с кнопками"""
    try:
        p = await _chat_player(event)
    except Exception as e:
        await engine.answer(event, visual.err(_esc(e)))
        return
    await event.delete()
    await open_panel(event, p)


# =========================================================================== «сейчас играет» (NowPlaying)

def _np_font(size, bold=False):
    from PIL import ImageFont
    names = (('DejaVuSans-Bold.ttf', 'arialbd.ttf', 'segoeuib.ttf') if bold
             else ('DejaVuSans.ttf', 'arial.ttf', 'segoeui.ttf'))
    names += (str(data_dir() / 'fonts' / ('JetBrainsMono-Bold.ttf' if bold else 'JetBrainsMono-Regular.ttf')),)
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _np_card(cur, cover_bytes, pos, total, state, live=False):
    """Карточка «сейчас играет»: обложка слева, название/исполнитель, полоска прогресса."""
    from PIL import Image, ImageDraw, ImageFilter
    W, H = 900, 320
    img = Image.new('RGB', (W, H), (18, 18, 24))
    cover = None
    if cover_bytes:
        try:
            cover = Image.open(io.BytesIO(cover_bytes)).convert('RGB')
        except Exception:
            cover = None
    if cover is not None:                              # размытая обложка — фон
        bg = cover.resize((W, W)).crop((0, (W - H) // 2, W, (W - H) // 2 + H)).filter(ImageFilter.GaussianBlur(28))
        dark = Image.new('RGB', (W, H), (0, 0, 0))
        img = Image.blend(bg, dark, 0.45)
    d = ImageDraw.Draw(img)
    pad = 40
    art = H - pad * 2
    if cover is not None:
        c = cover.resize((art, art))
        mask = Image.new('L', (art, art), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, art, art], 26, fill=255)
        img.paste(c, (pad, pad), mask)
    else:
        d.rounded_rectangle([pad, pad, pad + art, pad + art], 26, fill=(45, 45, 58))
        d.text((pad + art // 2 - 24, pad + art // 2 - 30), '♪', font=_np_font(64, True), fill=(120, 120, 140))
    tx = pad + art + 36
    tw = W - tx - pad
    d.text((tx, pad + 6), state.upper(), font=_np_font(22, True), fill=(255, 150, 90))

    def fit(text, font, maxw):
        text = str(text)
        if d.textlength(text, font=font) <= maxw:
            return text
        while text and d.textlength(text + '…', font=font) > maxw:
            text = text[:-1]
        return text + '…'
    d.text((tx, pad + 46), fit(cur.get('title') or 'Без названия', _np_font(40, True), tw),
           font=_np_font(40, True), fill=(240, 240, 248))
    if cur.get('artist'):
        d.text((tx, pad + 104), fit(cur['artist'], _np_font(28), tw), font=_np_font(28), fill=(180, 180, 195))
    if cur.get('service'):
        d.text((tx, pad + 150), fit(cur['service'], _np_font(22), tw), font=_np_font(22), fill=(150, 150, 165))

    by = H - pad - 34
    bw = tw
    d.rounded_rectangle([tx, by, tx + bw, by + 8], 4, fill=(70, 70, 85))
    if live:
        d.rounded_rectangle([tx, by, tx + bw, by + 8], 4, fill=(235, 90, 90))
        d.text((tx, by + 16), '● прямой эфир', font=_np_font(22, True), fill=(235, 120, 120))
    else:
        frac = (pos / total) if total else 0
        frac = max(0, min(1, frac))
        d.rounded_rectangle([tx, by, tx + max(8, int(bw * frac)), by + 8], 4, fill=(255, 140, 80))

        def mmss(s):
            s = max(0, int(s))
            return f"{s // 60}:{s % 60:02}"
        d.text((tx, by + 16), mmss(pos), font=_np_font(22), fill=(190, 190, 205))
        tt = mmss(total) if total else '—'
        d.text((tx + bw - d.textlength(tt, font=_np_font(22)), by + 16), tt, font=_np_font(22), fill=(190, 190, 205))
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.name = 'nowplaying.png'
    buf.seek(0)
    return buf


async def _np_current(event):
    """Что показывать как «сейчас играет»: активный плеер этого чата → иначе Spotify «сейчас играет»."""
    p = PLAYERS.get(event.chat_id)
    if p is not None and p.current and p.state in ('playing', 'paused', 'loading'):
        cur = p.current
        state = {'playing': 'сейчас играет', 'paused': 'на паузе', 'loading': 'загрузка'}.get(p.state, 'играет')
        return cur, p.position(), cur.get('duration') or 0, state, bool(cur.get('live'))
    # ни один плеер не активен — пробуем Spotify «что слушаю»
    try:
        if (await services_state())['sp']['on']:
            items = await sp_now()
            if items:
                return items[0], 0, items[0].get('duration') or 0, 'слушаю в Spotify', False
    except Exception:
        pass
    return None


@decorators.command('vnow', 'vnp', 'nowplaying')
async def vnow_cmd(event):
    """карточка «сейчас играет»: обложка и прогресс (голосовой чат или ваш Spotify)"""
    await engine.answer(event, visual.wait("Собираю карточку…"))
    data = await _np_current(event)
    if data is None:
        await engine.answer(event, visual.hint("Сейчас ничего не играет") + "\n"
                            + visual.hint(".vplay — включить в голосовом чате · Spotify «что слушаю» — после .vauth"))
        return
    cur, pos, total, state, live = data
    cover = None
    if cur.get('thumb'):
        got = await _img(cur['thumb'])
        if got:
            cover = got[0]
    try:
        buf = await asyncio.to_thread(_np_card, cur, cover, pos, total, state, live)
    except Exception as e:
        line = f"🎵 <b>{_esc(cur.get('title'))}</b>" + (f"\n<i>{_esc(cur.get('artist'))}</i>" if cur.get('artist') else '')
        await engine.answer(event, visual.title("Сейчас играет") + "\n" + line + "\n"
                            + visual.hint(f"картинка не вышла: {_esc(str(e)[:120])}"))
        return
    caption = visual.hint(f"{_esc(cur.get('artist') or '')} — {_esc(cur.get('title') or '')}".strip(' —'))
    await engine.client.send_file(event.chat_id, buf, caption=caption, parse_mode='html',
                                  reply_to=event.reply_to_msg_id, force_document=False)
    try:
        await event.delete()
    except Exception:
        pass


@decorators.command('vsearch', 'vs')
async def vsearch_cmd(event):
    """поиск с выбором источника: <запрос>"""
    q = engine.get_args().strip()
    if not q:
        await engine.answer(event, visual.err("Что ищем?") + "\n" + visual.hint(".vsearch запрос"))
        return
    st = {'src': await cfg('source') or 'std', 'res': [], 'err': None}
    in_saved = event.chat_id == (await engine.client.get_me()).id

    async def run():
        try:
            st['res'], st['err'] = await search(st['src'], q, 10), None
        except Exception as e:
            st['res'], st['err'] = [], str(e)[:200]

    def text():
        lines = [visual.title("Voinje · поиск", sub=f"«{_esc(q)}» · {SOURCE_NAME[st['src']]}")]
        if st['err']:
            lines.append(visual.err(_esc(st['err'])))
        for i, x in enumerate(st['res'], 1):
            lines.append(f"<b>{i}.</b> {_esc(label(x))[:80]}  <i>{'эфир' if x.get('live') else _dur(x.get('duration'))}</i>")
        if not st['res'] and not st['err']:
            lines.append(visual.hint("ничего не нашлось"))
        if in_saved:
            lines.append("\n" + visual.hint("в Избранном играть негде — запустите поиск в группе с голосовым чатом"))
        return "\n".join(lines)

    def buttons():
        rows, row = [], []
        for key, name in SOURCES:
            row.append(((('• ' + name) if key == st['src'] else name), f"vs:s:{key}"))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        for i, x in enumerate(st['res']):
            rows.append([(f"▶ {i + 1}. {label(x)[:30]}", f"vs:p:{i}"), ("➕", f"vs:a:{i}"), ("🎬", f"vs:v:{i}")])
        rows.append([(visual.btn('Закрыть', 'close'), "vs:close")])
        return rows

    await engine.answer(event, visual.wait("Ищу…"))
    await run()
    form = await engine.create_inlineform(event.chat_id, text(), buttons=buttons())
    await event.delete()

    @form.on_click(data_startswith='vs:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
            return
        if d.startswith('s:'):
            st['src'] = d[2:]
            await click.answer("ищу…")
            await run()
            await form.edit(text(), buttons=buttons())
            return
        if in_saved:
            await click.answer("В Избранном нет голосового чата — нужна группа", show_alert=True)
            return
        kind, i = d[0], int(d[2:])
        x = dict(st['res'][i], id=secrets.token_hex(4))
        if kind == 'v':
            x['video'] = True
        try:
            await click.answer("запускаю…" if kind != 'a' else "в очереди")
            await _prepare_silent()
            p = await player(event.chat_id)
            await p.add([x], now=(kind in ('p', 'v')))
            if not p.panels:
                await open_panel(event, p)
        except Exception as e:
            await click.answer(f"ошибка: {str(e)[:150]}", show_alert=True)


async def _prepare_silent():
    await ensure_ffmpeg()
    await get_calls()


@decorators.command('vradio')
async def vradio_cmd(event):
    """радиостанции: <название / жанр / город>"""
    q = engine.get_args().strip()
    if not q:
        await engine.answer(event, visual.err("Например: .vradio lofi · .vradio Европа Плюс"))
        return
    try:
        p = await _chat_player(event)
        await _prepare(event)
        st = await search_radio(q, 1)
        if not st:
            await engine.answer(event, visual.err("Станция не нашлась"))
            return
        await p.add(st, now=True)
        await event.delete()
        if not p.panels:
            await open_panel(event, p)
    except Exception as e:
        await engine.answer(event, visual.err("Voinje") + f"\n<code>{_esc(str(e)[:300])}</code>")


@decorators.command('vwave')
async def vwave_cmd(event):
    """«Моя волна» Яндекс Музыки — бесконечно"""
    try:
        p = await _chat_player(event)
        await _prepare(event)
        p.wave = 'user:onyourwave'
        more = await ym_wave(p.wave)
        p.queue = []
        await p.add(more, now=True)
        await event.delete()
        if not p.panels:
            await open_panel(event, p)
    except Exception as e:
        await engine.answer(event, visual.err("Волна") + f"\n<code>{_esc(str(e)[:300])}</code>")


@decorators.command('vlikes')
async def vlikes_cmd(event):
    """ваши лайки: [ym | sp] (по умолчанию — где вы вошли)"""
    which = engine.get_args().strip().lower()
    try:
        p = await _chat_player(event)
        await _prepare(event)
        if which in ('sp', 'spotify') or (not which and not await ym_logged() and await dget('sp_auth')):
            items = await sp_likes()
        else:
            items = await ym_likes()
        if await cfg('shuffle_likes'):
            random.shuffle(items)
        await p.add(items)
        await engine.answer(event, visual.ok(f"В очереди: {len(items)} треков"))
        if not p.panels:
            await open_panel(event, p)
    except Exception as e:
        await engine.answer(event, visual.err("Лайки") + f"\n<code>{_esc(str(e)[:300])}</code>")


@decorators.command('vauth', 'vlogin')
async def vauth_cmd(event):
    """вход в сервисы: Яндекс Музыка, Spotify, YouTube (лучше — в Избранном или на сайте)"""
    r = await event.get_reply_message() if event.is_reply else None
    if r is not None and r.file is not None and (r.file.name or '').endswith('.txt'):
        try:
            await yt_cookies_set(await r.download_media(file=bytes))
            await engine.answer(event, visual.ok("YouTube: cookies сохранены"))
        except Exception as e:
            await engine.answer(event, visual.err(_esc(e)))
        return

    async def text():
        s = await services_state()
        lines = [visual.title("Voinje · сервисы")]
        lines.append("🟡 <b>Яндекс Музыка</b> — " + (f"вошли: {_esc(s['ym']['login'] or 'аккаунт')}" if s['ym']['on'] else "не вошли"))
        if s['ym']['device']:
            lines.append(f"   откройте <a href=\"{_esc(s['ym']['device']['url'])}\">{_esc(s['ym']['device']['url'])}</a> и введите код "
                         f"<code>{_esc(s['ym']['device']['code'])}</code>, потом нажмите «Проверить»")
        lines.append("🟢 <b>Spotify</b> — " + (f"вошли: {_esc(s['sp']['name'] or 'аккаунт')}" if s['sp']['on'] else "не вошли"))
        if s['sp']['pending'] and not s['sp']['on']:
            lines.append("   после входа браузер откроет пустую страницу 127.0.0.1 — скопируйте её адрес и вставьте кнопкой «Вставить адрес»")
        lines.append("▶️ <b>YouTube</b> — " + ("cookies загружены" if s['yt']['on'] else "без cookies")
                     + "  " + visual.hint("cookies.txt — ответом на файл: .vauth"))
        lines.append("")
        lines.append(visual.hint("Spotify: нужен ваш Client ID (developer.spotify.com → Create app, Redirect URI: "
                                 f"{SP_REDIRECT}). Звук Spotify зашифрован — треки играют из открытых источников."))
        lines.append(visual.hint("Удобнее всего — на сайте: .web → вкладка Voinje"))
        return "\n".join(lines)

    async def buttons():
        s = await services_state()
        rows = []
        if s['ym']['on']:
            rows.append([("🟡 Выйти из Яндекса", "va:ym_out", "danger")])
        elif s['ym']['device']:
            rows.append([("✅ Проверить вход", "va:ym_poll", "success"), ("Новый код", "va:ym_code")])
        else:
            rows.append([("🟡 Войти в Яндекс (код)", "va:ym_code", "primary"),
                         engine.InputButton("Токен", "ym_token", hint="токен Яндекс Музыки")])
        if s['sp']['on']:
            rows.append([("🟢 Выйти из Spotify", "va:sp_out", "danger")])
        else:
            rows.append([engine.InputButton("🟢 Spotify: Client ID", "sp_id", hint="Client ID из developer.spotify.com")]
                        + ([engine.InputButton("Вставить адрес", "sp_url", hint="адрес 127.0.0.1:8888/callback?code=…")]
                           if s['sp']['pending'] else []))
        if s['yt']['on']:
            rows.append([("▶️ Удалить cookies YouTube", "va:yt_out", "danger")])
        rows.append([(visual.btn('Закрыть', 'close'), "va:close")])
        return rows

    form = await engine.create_inlineform(event.chat_id, await text(), buttons=await buttons())
    await event.delete()

    async def redraw(extra=None):
        await form.edit(await text() + (("\n\n" + extra) if extra else ''), buttons=await buttons())

    @form.on_click(data_startswith='va:')
    async def _(click):
        d = click.data[3:]
        try:
            if d == 'close':
                await form.delete()
                return
            if d == 'ym_code':
                dev = await ym_device_start()
                await click.answer(f"код: {dev['user_code']}")
            elif d == 'ym_poll':
                ok = await ym_device_poll()
                await click.answer("вошли!" if ok else "ещё не подтверждено — введите код и нажмите снова", show_alert=not ok)
            elif d == 'ym_out':
                await ym_set_token('')
            elif d == 'sp_out':
                await dset('sp_auth', None)
            elif d == 'yt_out':
                yt_cookies_drop()
            await redraw()
        except Exception as e:
            await redraw(visual.err(_esc(str(e)[:300])))

    @form.on_input()
    async def _(inp):
        v = (inp.value or '').strip()
        try:
            if inp.data == 'ym_token':
                login = await ym_set_token(v)
                await redraw(visual.ok(f"Яндекс: вошли {_esc(login or '')}"))
            elif inp.data == 'sp_id':
                url = await sp_login_url(v)
                await redraw(visual.ok("Откройте ссылку и войдите в Spotify:") + f'\n<a href="{_esc(url)}">войти в Spotify</a>')
            elif inp.data == 'sp_url':
                name = await sp_finish(v)
                await redraw(visual.ok(f"Spotify: вошли {_esc(name or '')}"))
        except Exception as e:
            await redraw(visual.err(_esc(str(e)[:300])))


# =========================================================================== «приложения» сайта: подборки и карточки

def card(title, sub='', img='', open_=None, round_=False):
    """Карточка (альбом, плейлист, исполнитель, станция): по клику открывается вид open_ = {'view', 'id'}."""
    return {'title': title or '', 'sub': sub or '', 'img': img or '', 'open': open_ or {}, 'round': bool(round_)}


def tracks(title, items, play_all=True):
    return {'kind': 'tracks', 'title': title, 'items': items, 'all': play_all}


def cards(title, items):
    return {'kind': 'cards', 'title': title, 'items': items}


def _ymimg(uri, size='400x400'):
    return ('https://' + uri.replace('%%', size)) if uri else ''


async def _ym_liked_ids(force=False):
    if not force and _ym.get('liked') is not None and time.monotonic() - _ym.get('liked_t', 0) < 300:
        return _ym['liked']
    try:
        client = await ym_client(anon_ok=False)
        likes = await client.users_likes_tracks()
        _ym['liked'] = {str(t.id).split(':')[0] for t in (likes.tracks if likes else [])}
        _ym['liked_t'] = time.monotonic()
    except Exception:
        _ym['liked'] = set()
    return _ym['liked']


async def _mark_liked(items):
    if not await ym_logged():
        return items
    liked = await _ym_liked_ids()
    for x in items:
        if (x.get('src') or {}).get('t') == 'ym':
            x['liked'] = x['src']['id'].split(':')[0] in liked
    return items


def _deezer_album_card(a):
    return card(a.get('title'), (a.get('artist') or {}).get('name', ''), a.get('cover_big') or a.get('cover_medium'),
                {'view': 'album', 'id': a.get('id')})


def _deezer_pl_card(p):
    return card(p.get('title'), f"{p.get('nb_tracks', '')} треков" if p.get('nb_tracks') else '',
                p.get('picture_big') or p.get('picture_medium'), {'view': 'playlist', 'id': p.get('id')})


async def browse_std(view, arg, q):
    if view == 'search':
        return {'title': f"«{q}»", 'sections': [tracks('Треки', await search_std(q, 25))]}
    if view == 'album':
        a = await _get(f'https://api.deezer.com/album/{int(arg)}')
        cover = a.get('cover_big', '')
        items = [_deezer_item({**t, 'album': {'cover_big': cover}}) for t in (a.get('tracks') or {}).get('data', [])]
        return {'title': a.get('title'), 'sub': (a.get('artist') or {}).get('name', ''), 'img': cover,
                'sections': [tracks('Треки', items)]}
    if view == 'playlist':
        p = await _get(f'https://api.deezer.com/playlist/{int(arg)}')
        items = [_deezer_item(t) for t in (p.get('tracks') or {}).get('data', [])]
        return {'title': p.get('title'), 'sub': f"{len(items)} треков", 'img': p.get('picture_big', ''),
                'sections': [tracks('Треки', items)]}
    ch = await _get('https://api.deezer.com/chart/0', params={'limit': 30})
    return {'title': 'Музыка', 'sub': 'чарт и подборки — звук из открытых источников',
            'sections': [tracks('Чарт', [_deezer_item(t) for t in (ch.get('tracks') or {}).get('data', [])]),
                         cards('Альбомы недели', [_deezer_album_card(a) for a in (ch.get('albums') or {}).get('data', [])]),
                         cards('Плейлисты', [_deezer_pl_card(p) for p in (ch.get('playlists') or {}).get('data', [])])]}


async def browse_ym(view, arg, q):
    logged = await ym_logged()
    client = await ym_client()
    if view == 'search':
        res = await client.search(q, type_='all')
        secs = []
        if res and res.tracks:
            secs.append(tracks('Треки', await _mark_liked([_ym_item(t, logged) for t in res.tracks.results[:20]])))
        if res and res.artists:
            secs.append(cards('Исполнители', [card(a.name, '', _ymimg(a.cover.uri if a.cover else a.og_image, '300x300'),
                                                   {'view': 'artist', 'id': a.id}, True) for a in res.artists.results[:12]]))
        if res and res.albums:
            secs.append(cards('Альбомы', [card(a.title, ', '.join(x.name for x in a.artists or []), _ymimg(a.cover_uri),
                                               {'view': 'album', 'id': a.id}) for a in res.albums.results[:12]]))
        if res and res.playlists:
            secs.append(cards('Плейлисты', [card(p.title, f"{p.track_count or ''} треков", p.get_og_image_url('400x400') if p.og_image else '',
                                                 {'view': 'playlist', 'id': f"{p.owner.uid}:{p.kind}"}) for p in res.playlists.results[:12]]))
        return {'title': f"«{q}»", 'sections': secs}
    if view == 'album':
        a = await client.albums_with_tracks(int(arg))
        items = [_ym_item(t, logged) for vol in a.volumes or [] for t in vol]
        return {'title': a.title, 'sub': ', '.join(x.name for x in a.artists or []) + (f" · {a.year}" if getattr(a, 'year', None) else ''),
                'img': _ymimg(a.cover_uri), 'sections': [tracks('Треки', await _mark_liked(items))]}
    if view == 'artist':
        tr = await client.artists_tracks(int(arg), page_size=50)
        al = await client.artists_direct_albums(int(arg), page_size=30)
        info = (await client.artists([int(arg)]))[0]
        return {'title': info.name, 'sub': 'исполнитель', 'img': _ymimg(info.cover.uri if info.cover else info.og_image, '300x300'),
                'round': True,
                'sections': [tracks('Популярное', await _mark_liked([_ym_item(t, logged) for t in (tr.tracks if tr else [])])),
                             cards('Альбомы', [card(a.title, str(a.year or ''), _ymimg(a.cover_uri), {'view': 'album', 'id': a.id})
                                               for a in (al.albums if al else [])])]}
    if view == 'playlist':
        uid, kind = str(arg).split(':', 1)
        pl = await client.users_playlists(int(kind), uid)
        tr = await pl.fetch_tracks_async()
        items = [_ym_item(t.track if getattr(t, 'track', None) else t, logged) for t in tr]
        return {'title': pl.title, 'sub': f"{len(items)} треков", 'img': pl.get_og_image_url('400x400') if pl.og_image else '',
                'sections': [tracks('Треки', await _mark_liked(items))]}
    if not logged:
        ch = await client.chart('world')
        items = [_ym_item(t.track, False) for t in (ch.chart.tracks if ch and ch.chart else [])[:30] if t.track]
        return {'title': 'Яндекс Музыка', 'sub': 'войдите — будут полные треки, «Моя волна», лайки и плейлисты',
                'login': True, 'sections': [tracks('Чарт', items)]}
    if view == 'likes':
        return {'title': 'Мне нравится', 'sub': 'ваши лайки', 'sections': [tracks('Треки', await _mark_liked(await ym_likes(500)))]}
    if view == 'playlists':
        own = await client.users_playlists_list()
        liked = await client.users_likes_playlists()
        mk = lambda p: card(p.title, f"{p.track_count or 0} треков", p.get_og_image_url('400x400') if p.og_image else '',
                            {'view': 'playlist', 'id': f"{p.owner.uid}:{p.kind}"})
        return {'title': 'Плейлисты', 'sections': [cards('Мои', [mk(p) for p in own or []]),
                                                    cards('Сохранённые', [mk(l.playlist) for l in liked or [] if l.playlist])]}
    if view == 'albums':
        likes = await client.users_likes_albums()
        return {'title': 'Альбомы', 'sections': [cards('Сохранённые альбомы', [
            card(l.album.title, ', '.join(x.name for x in l.album.artists or []), _ymimg(l.album.cover_uri), {'view': 'album', 'id': l.album.id})
            for l in likes or [] if l.album])]}
    if view == 'artists':
        likes = await client.users_likes_artists()
        return {'title': 'Исполнители', 'sections': [cards('Любимые исполнители', [
            card(l.artist.name, '', _ymimg(l.artist.cover.uri if l.artist.cover else l.artist.og_image, '300x300'),
                 {'view': 'artist', 'id': l.artist.id}, True) for l in likes or [] if l.artist])]}
    # главная
    secs = []
    stations = []
    try:
        dash = await client.rotor_stations_dashboard()
        for st in (dash.stations if dash else [])[:10]:
            sid = f"{st.station.id.type}:{st.station.id.tag}"
            stations.append({'id': sid, 'title': st.station.name, 'color': getattr(getattr(st.station, 'icon', None), 'background_color', '') or ''})
    except Exception:
        pass
    try:
        ch = await client.chart('world')
        secs.append(tracks('Чарт', await _mark_liked([_ym_item(t.track, True) for t in (ch.chart.tracks if ch and ch.chart else [])[:30] if t.track])))
    except Exception:
        pass
    try:
        nr = await client.new_releases()
        albums = await client.albums((nr.new_releases or [])[:16]) if nr else []
        secs.append(cards('Новые релизы', [card(a.title, ', '.join(x.name for x in a.artists or []), _ymimg(a.cover_uri),
                                                {'view': 'album', 'id': a.id}) for a in albums or []]))
    except Exception:
        pass
    return {'title': 'Яндекс Музыка', 'sub': f"вы вошли как {_ym.get('login') or 'пользователь'}", 'wave': True,
            'stations': stations, 'sections': secs}


async def browse_sp(view, arg, q):
    if not await dget('sp_auth'):
        return {'title': 'Spotify', 'sub': 'войдите — будут ваши лайки, плейлисты и «что сейчас играет»', 'login': True, 'sections': []}
    def pl_card(p):
        imgs = p.get('images') or []
        return card(p.get('name'), f"{(p.get('tracks') or {}).get('total', '')} треков", imgs[0]['url'] if imgs else '',
                    {'view': 'playlist', 'id': p['id']})

    def al_card(a):
        imgs = a.get('images') or []
        return card(a.get('name'), ', '.join(x.get('name', '') for x in a.get('artists') or []), imgs[0]['url'] if imgs else '',
                    {'view': 'album', 'id': a['id']})
    if view == 'search':
        d = await sp_api('/search', {'q': q, 'type': 'track,album,playlist', 'limit': 20})
        return {'title': f"«{q}»", 'sections': [
            tracks('Треки', [x for x in (_sp_item(t) for t in (d.get('tracks') or {}).get('items') or []) if x]),
            cards('Альбомы', [al_card(a) for a in (d.get('albums') or {}).get('items') or [] if a]),
            cards('Плейлисты', [pl_card(p) for p in (d.get('playlists') or {}).get('items') or [] if p])]}
    if view == 'playlist':
        p = await sp_api(f'/playlists/{arg}', {'fields': 'name,images,tracks.total'})
        imgs = p.get('images') or []
        return {'title': p.get('name'), 'img': imgs[0]['url'] if imgs else '', 'sections': [tracks('Треки', await sp_playlist(arg))]}
    if view == 'album':
        return {'title': 'Альбом', 'sections': [tracks('Треки', await spotify_link(f'https://open.spotify.com/album/{arg}'))]}
    if view == 'likes':
        return {'title': 'Любимые треки', 'sections': [tracks('Треки', await sp_likes(500))]}
    if view == 'playlists':
        d = await sp_api('/me/playlists', {'limit': 50})
        return {'title': 'Плейлисты', 'sections': [cards('Мои плейлисты', [pl_card(p) for p in d.get('items') or [] if p])]}
    if view == 'albums':
        d = await sp_api('/me/albums', {'limit': 50})
        return {'title': 'Альбомы', 'sections': [cards('Сохранённые альбомы', [al_card(i['album']) for i in d.get('items') or [] if i.get('album')])]}
    secs = []
    try:
        secs.append(tracks('Сейчас в Spotify', await sp_now(), play_all=False))
    except Exception:
        pass
    try:
        d = await sp_api('/me/top/tracks', {'limit': 30, 'time_range': 'short_term'})
        secs.append(tracks('Ваш топ за месяц', [x for x in (_sp_item(t) for t in d.get('items') or []) if x]))
    except Exception:
        pass
    try:
        d = await sp_api('/me/player/recently-played', {'limit': 30})
        seen, rec = set(), []
        for i in d.get('items') or []:
            x = _sp_item(i.get('track'))
            if x and label(x) not in seen:
                seen.add(label(x))
                rec.append(x)
        secs.append(tracks('Недавно слушали', rec))
    except Exception:
        pass
    auth = await dget('sp_auth') or {}
    return {'title': 'Spotify', 'sub': f"вы вошли как {auth.get('name') or 'пользователь'} · звук — из открытых источников",
            'sections': secs}


async def browse_yt(view, arg, q, kind='yt'):
    if view == 'search':
        return {'title': f"«{q}»", 'sections': [{'kind': 'videos' if kind == 'yt' else 'tracks', 'title': 'Результаты',
                                                  'items': await search_ydl(q, kind, 24), 'all': kind != 'yt'}]}
    if view == 'link':
        items = await from_url(q, video=False)
        return {'title': items[0]['title'] if len(items) == 1 else 'Плейлист', 'sections': [tracks('По ссылке', items)]}
    if kind == 'yt' and view == 'liked':
        if not yt_cookies_on():
            return {'title': 'Понравившиеся', 'sub': 'нужны cookies YouTube — загрузите их слева', 'sections': []}
        info = await ydl_info('https://www.youtube.com/playlist?list=LL', {'extract_flat': 'in_playlist', 'noplaylist': False, 'playlistend': 100})
        return {'title': 'Понравившиеся', 'sections': [{'kind': 'videos', 'title': 'Видео', 'all': True,
                                                         'items': [_yt_item(e) for e in info.get('entries') or [] if e]}]}
    if kind == 'yt':
        try:
            items = await search_ydl('музыка хиты', 'yt', 12)
        except Exception:
            items = []
        return {'title': 'YouTube', 'sub': 'поиск, ссылки, плейлисты; видео — кнопкой 🎬',
                'sections': [{'kind': 'videos', 'title': 'Для начала', 'items': items, 'all': False}]}
    return {'title': 'SoundCloud', 'sub': 'найдите трек, микс или подкаст', 'sections': []}


async def browse_radio(view, arg, q):
    if view == 'search':
        return {'title': f"«{q}»", 'sections': [{'kind': 'stations', 'title': 'Станции', 'items': await search_radio(q, 40), 'all': False}]}
    async def fetch(path, params):
        for sv in ('de1', 'fi1', 'at1', 'nl1'):
            try:
                return await _get(f'https://{sv}.api.radio-browser.info/json/{path}', params=params,
                                  headers={'User-Agent': 'FRX-Voinje/1.0'}, timeout=12)
            except Exception:
                continue
        return []

    def conv(lst):
        out = []
        for st in lst or []:
            url = st.get('url_resolved') or st.get('url')
            if url:
                sub = ' · '.join(x for x in (st.get('country'), (f"{st['bitrate']} кбит/с" if st.get('bitrate') else '')) if x)
                out.append(item(st.get('name', '').strip(), sub, 0, {'t': 'url', 'url': url}, st.get('favicon') or '', 'Радио', live=True))
        return out
    ru = await fetch('stations/search', {'countrycode': 'RU', 'limit': 30, 'order': 'clickcount', 'reverse': 'true', 'hidebroken': 'true'})
    top = await fetch('stations/topclick/30', {'hidebroken': 'true'})
    return {'title': 'Радио', 'sub': 'тысячи станций мира — прямой эфир', 'tags': ['lofi', 'jazz', 'rock', 'pop', 'chill', 'classical',
                                                                                 'news', 'dance', 'шансон', 'метал'],
            'sections': [{'kind': 'stations', 'title': 'Популярное в России', 'items': conv(ru), 'all': False},
                         {'kind': 'stations', 'title': 'Популярное в мире', 'items': conv(top), 'all': False}]}


async def browse(app_, view, arg, q):
    view = view or 'home'
    q = (q or '').strip()[:200]
    if app_ == 'std':
        return await browse_std(view, arg, q)
    if app_ == 'ym':
        return await browse_ym(view, arg, q)
    if app_ == 'sp':
        return await browse_sp(view, arg, q)
    if app_ in ('yt', 'sc'):
        return await browse_yt(view, arg, q, app_)
    if app_ == 'radio':
        return await browse_radio(view, arg, q)
    if app_ == 'ytm':
        return await browse_ytm(view, arg, q)
    if app_ == 'zv':
        return await browse_zv(view, arg, q)
    if app_ == 'tg':
        return {'title': 'Telegram', 'sub': 'музыка из «Избранного» — перешлите туда треки, и они появятся здесь',
                'sections': [tracks('Музыка в Избранном', await search_saved(q, 200))]}
    if app_ == 'files':
        return {'title': 'Файлы', 'sub': 'ваша библиотека на компьютере', 'files': True,
                'sections': [tracks('Библиотека', await search_files(q))]}
    raise RuntimeError("неизвестное приложение")


# =========================================================================== ещё источники: YouTube Music, Звук

# ------------------------------------------------------------------ YouTube Music (ytmusicapi; вход — тот же, что у YouTube)

_ytm = {'client': None, 'sig': None}


async def ytm_client():
    """YTMusic: со входом в YouTube (cookies) — ваша библиотека, без — каталог и поиск."""
    await need('ytmusicapi')
    ck = data_dir() / 'youtube_cookies.txt'
    sig = ck.stat().st_mtime if ck.is_file() else None
    if _ytm['client'] is not None and _ytm['sig'] == sig:
        return _ytm['client']

    def build():
        from ytmusicapi import YTMusic
        if sig is None:
            return YTMusic(language='ru', location='RU')
        from http.cookiejar import MozillaCookieJar
        jar = MozillaCookieJar(str(ck))
        jar.load(ignore_discard=True, ignore_expires=True)
        cookie = '; '.join(f"{c.name}={c.value}" for c in jar if 'youtube.com' in (c.domain or ''))
        headers = {'cookie': cookie, 'x-goog-authuser': '0', 'origin': 'https://music.youtube.com',
                   'user-agent': UA['User-Agent'], 'accept': '*/*', 'content-type': 'application/json'}
        try:
            return YTMusic(auth=headers, language='ru', location='RU')
        except Exception:
            return YTMusic(language='ru', location='RU')
    client = await asyncio.get_running_loop().run_in_executor(None, build)
    _ytm.update(client=client, sig=sig)
    return client


async def ytm(fn, *a, **kw):
    client = await ytm_client()
    return await asyncio.get_running_loop().run_in_executor(None, lambda: getattr(client, fn)(*a, **kw))


def _thumb(lst):
    lst = lst or []
    return (max(lst, key=lambda t: t.get('width') or 0).get('url') if lst else '') or ''


def _ytm_item(t):
    vid = t.get('videoId')
    if not vid:
        return None
    artists = ', '.join(a.get('name', '') for a in t.get('artists') or [] if a.get('name'))
    dur = t.get('duration_seconds') or 0
    if not dur and t.get('duration'):
        p = _parse_time(t['duration'])
        dur = p[1] if p else 0
    return item(t.get('title'), artists, dur, {'t': 'ydl', 'url': f"https://music.youtube.com/watch?v={vid}"},
                _thumb(t.get('thumbnails')), 'YouTube Music')


def _ytm_card(x, kind):
    if kind == 'album':
        return card(x.get('title'), ', '.join(a.get('name', '') for a in x.get('artists') or []) or str(x.get('year') or ''),
                    _thumb(x.get('thumbnails')), {'view': 'album', 'id': x.get('browseId')})
    if kind == 'artist':
        return card(x.get('artist') or x.get('title'), '', _thumb(x.get('thumbnails')), {'view': 'artist', 'id': x.get('browseId')}, True)
    return card(x.get('title'), x.get('author') or x.get('description') or (f"{x.get('count')} треков" if x.get('count') else ''),
                _thumb(x.get('thumbnails')), {'view': 'playlist', 'id': x.get('playlistId') or x.get('browseId')})


async def browse_ytm(view, arg, q):
    logged = yt_cookies_on()
    if view == 'search':
        songs, albums, artists, pls = await asyncio.gather(
            ytm('search', q, filter='songs', limit=25), ytm('search', q, filter='albums', limit=12),
            ytm('search', q, filter='artists', limit=10), ytm('search', q, filter='playlists', limit=12), return_exceptions=True)
        ok = lambda r: r if isinstance(r, list) else []
        return {'title': f"«{q}»", 'sections': [tracks('Треки', [x for x in (_ytm_item(t) for t in ok(songs)) if x]),
                                                 cards('Исполнители', [_ytm_card(a, 'artist') for a in ok(artists)]),
                                                 cards('Альбомы', [_ytm_card(a, 'album') for a in ok(albums)]),
                                                 cards('Плейлисты', [_ytm_card(p, 'playlist') for p in ok(pls)])]}
    if view == 'album':
        a = await ytm('get_album', arg)
        img = _thumb(a.get('thumbnails'))
        items = [x for x in (_ytm_item({**t, 'thumbnails': t.get('thumbnails') or a.get('thumbnails')}) for t in a.get('tracks') or []) if x]
        return {'title': a.get('title'), 'sub': ', '.join(x.get('name', '') for x in a.get('artists') or []) + (f" · {a.get('year')}" if a.get('year') else ''),
                'img': img, 'sections': [tracks('Треки', items)]}
    if view == 'playlist':
        p = await ytm('get_playlist', arg, limit=300)
        return {'title': p.get('title'), 'sub': f"{p.get('trackCount') or len(p.get('tracks') or [])} треков", 'img': _thumb(p.get('thumbnails')),
                'sections': [tracks('Треки', [x for x in (_ytm_item(t) for t in p.get('tracks') or []) if x])]}
    if view == 'artist':
        a = await ytm('get_artist', arg)
        songs = (a.get('songs') or {}).get('results') or []
        secs = [tracks('Популярное', [x for x in (_ytm_item(t) for t in songs) if x])]
        for key, title in (('albums', 'Альбомы'), ('singles', 'Синглы')):
            res = (a.get(key) or {}).get('results') or []
            if res:
                secs.append(cards(title, [_ytm_card(x, 'album') for x in res]))
        return {'title': a.get('name'), 'sub': 'исполнитель', 'img': _thumb(a.get('thumbnails')), 'round': True, 'sections': secs}
    if view == 'likes':
        if not logged:
            return {'title': 'Мне нравится', 'sub': 'войдите в YouTube (приложение YouTube → слева «Войти через браузер»)', 'sections': []}
        d = await ytm('get_liked_songs', limit=500)
        return {'title': 'Мне нравится', 'sections': [tracks('Треки', [x for x in (_ytm_item(t) for t in d.get('tracks') or []) if x])]}
    if view == 'playlists':
        if not logged:
            return {'title': 'Плейлисты', 'sub': 'войдите в YouTube, чтобы видеть свои плейлисты', 'sections': []}
        pls = await ytm('get_library_playlists', limit=100)
        return {'title': 'Плейлисты', 'sections': [cards('Мои плейлисты', [_ytm_card(p, 'playlist') for p in pls or []])]}
    if view == 'albums':
        if not logged:
            return {'title': 'Альбомы', 'sub': 'войдите в YouTube, чтобы видеть свою библиотеку', 'sections': []}
        al = await ytm('get_library_albums', limit=100)
        return {'title': 'Альбомы', 'sections': [cards('В библиотеке', [_ytm_card(a, 'album') for a in al or []])]}
    secs = []
    try:
        home = await ytm('get_home', limit=6)
        for shelf in home or []:
            songs, others = [], []
            for c in shelf.get('contents') or []:
                if c.get('videoId'):
                    it = _ytm_item(c)
                    if it:
                        songs.append(it)
                elif c.get('playlistId'):
                    others.append(_ytm_card(c, 'playlist'))
                elif c.get('browseId'):
                    others.append(_ytm_card(c, 'album' if c.get('year') or c.get('type') == 'Album' else 'artist' if c.get('subscribers') else 'album'))
            if songs:
                secs.append(tracks(shelf.get('title', ''), songs))
            if others:
                secs.append(cards(shelf.get('title', ''), others))
    except Exception as e:
        engine.tlog(f"[voinje] YouTube Music: главная: {e}")
    return {'title': 'YouTube Music', 'sub': 'вы вошли через YouTube' if logged else 'каталог YouTube Music; со входом в YouTube — ваша библиотека',
            'sections': secs}


# ------------------------------------------------------------------ Звук (zvuk.com, неофициальный API; токен — ваш)

_zv = {'client': None, 'token': None}


async def zv_client(anon_ok=True):
    zm = await need('zvuk_music', 'zvuk-music')
    token = await dget('zv_token')
    if not token and not anon_ok:
        raise RuntimeError("войдите в Звук")
    key = token or 'anon'
    if _zv['client'] is not None and _zv['token'] == key:
        return _zv['client']
    if token:
        client = await zm.ClientAsync(token=token).init()
    else:
        try:                          # без входа — анонимный токен: каталог и треки в 128 кбит/с
            anon = await asyncio.get_running_loop().run_in_executor(None, zm.ClientAsync.get_anonymous_token)
            client = await zm.ClientAsync(token=anon).init()
        except Exception:
            client = await zm.ClientAsync().init()
    _zv.update(client=client, token=key)
    return client


async def zv_set_token(token):
    token = (token or '').strip().strip('"')
    m = re.search(r'"token"\s*:\s*"([^"]+)"', token)
    if m:
        token = m.group(1)
    if not token:
        await dset('zv_token', None)
        _zv.update(client=None, token=None)
        return None
    zm = await need('zvuk_music', 'zvuk-music')
    client = await zm.ClientAsync(token=token).init()
    prof = None
    try:
        prof = await client.get_profile()
    except Exception:
        pass
    await dset('zv_token', token)
    _zv.update(client=client, token=token)
    name = None
    try:
        p = getattr(prof, 'result', None) or prof
        name = getattr(p, 'name', None) or getattr(p, 'username', None) or getattr(p, 'email', None)
    except Exception:
        pass
    await dset('zv_name', name)
    return name or 'аккаунт'


async def zv_from_browser(browser):
    """Токен Звука из вашего браузера: берём cookies zvuk.com и спрашиваем профиль — как в инструкции, только сами."""
    await ytdlp()

    def cookies():
        from yt_dlp.cookies import extract_cookies_from_browser
        jar = extract_cookies_from_browser(browser)
        return {c.name: c.value for c in jar if 'zvuk.com' in (c.domain or '')}
    try:
        ck = await asyncio.get_running_loop().run_in_executor(None, cookies)
    except Exception as e:
        raise RuntimeError(f"не прочитал cookies браузера: {str(e)[:200]}")
    if not ck:
        raise RuntimeError("в этом браузере вы не вошли на zvuk.com")
    async with aiohttp.ClientSession(cookies=ck, headers=UA, timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get('https://zvuk.com/api/v2/tiny/profile') as r:
            d = await r.json(content_type=None)
    token = (((d or {}).get('result') or {}).get('profile') or {}).get('token') or ((d or {}).get('result') or {}).get('token')
    if not token:
        raise RuntimeError("Звук не отдал токен — войдите на zvuk.com в этом браузере")
    return await zv_set_token(token)


def _zv_img(img, size=300):
    try:
        return img.get_url(size, size) if img else ''
    except Exception:
        return ''


def _zv_item(t, logged):
    artists = ', '.join(getattr(a, 'title', '') for a in (t.artists or []))
    rel = getattr(t, 'release', None)
    dur = int(getattr(t, 'duration', 0) or 0)
    src = {'t': 'zv', 'id': str(t.id)}          # без входа тоже играет — анонимно, в 128 кбит/с
    it = item(t.title, artists, dur, src, _zv_img(getattr(rel, 'image', None)), 'Звук')
    return it


def _zv_card(x, kind):
    if kind == 'release':
        return card(x.title, ', '.join(getattr(a, 'title', '') for a in (getattr(x, 'artists', None) or [])), _zv_img(x.image),
                    {'view': 'album', 'id': str(x.id)})
    if kind == 'artist':
        return card(x.title, '', _zv_img(x.image), {'view': 'artist', 'id': str(x.id)}, True)
    return card(x.title, (getattr(x, 'description', '') or '')[:60], _zv_img(getattr(x, 'image', None)), {'view': 'playlist', 'id': str(x.id)})


async def browse_zv(view, arg, q):
    logged = bool(await dget('zv_token'))
    client = await zv_client()
    if view == 'search':
        res = await client.search(q, limit=20, podcasts=False, episodes=False, profiles=False, books=False)
        items = lambda r: (r.items if r else []) or []
        return {'title': f"«{q}»", 'sections': [
            tracks('Треки', [_zv_item(t, logged) for t in items(res.tracks)]),
            cards('Исполнители', [_zv_card(a, 'artist') for a in items(res.artists)]),
            cards('Альбомы', [_zv_card(r, 'release') for r in items(res.releases)]),
            cards('Плейлисты', [_zv_card(p, 'playlist') for p in items(res.playlists)])]}
    if view == 'album':
        r = await client.get_release(arg)
        img = _zv_img(r.image, 400)
        its = []
        for t in r.tracks or []:
            it = _zv_item(t, logged)
            it['thumb'] = it['thumb'] or img
            its.append(it)
        return {'title': r.title, 'sub': ', '.join(a.title for a in r.artists or []), 'img': img, 'sections': [tracks('Треки', its)]}
    if view == 'playlist':
        pls = await client.get_playlists([arg])
        p = pls[0] if pls else None
        tr = await client.get_playlist_tracks(arg, limit=300)
        return {'title': getattr(p, 'title', 'Плейлист'), 'img': _zv_img(getattr(p, 'image', None), 400),
                'sections': [tracks('Треки', [_zv_item(t, logged) for t in tr or []])]}
    if view == 'artist':
        a = await client.get_artist(arg, with_releases=True, with_popular_tracks=True, tracks_limit=30, releases_limit=30)
        return {'title': a.title, 'sub': 'исполнитель', 'img': _zv_img(a.image, 400), 'round': True,
                'sections': [tracks('Популярное', [_zv_item(t, logged) for t in a.popular_tracks or []]),
                             cards('Релизы', [_zv_card(r, 'release') for r in a.releases or []])]}
    if not logged:
        secs = []
        try:
            ids = await client.get_editorial_playlist_ids()
            pls = await client.get_playlists([str(i) for i in (ids or [])[:18]])
            secs.append(cards('Подборки редакции', [_zv_card(p, 'playlist') for p in pls]))
        except Exception:
            pass
        return {'title': 'Звук', 'sub': 'без входа — 128 кбит/с; войдите — 320 кбит/с, ваши лайки и плейлисты', 'login': True, 'sections': secs}
    if view == 'likes':
        return {'title': 'Мне нравится', 'sections': [tracks('Треки', [_zv_item(t, True) for t in await client.get_liked_tracks()])]}
    if view == 'playlists':
        col = await client.get_collection()
        ids = [str(x.id) for x in (col.playlists if col else []) or []][:60]
        pls = await client.get_playlists(ids) if ids else []
        return {'title': 'Плейлисты', 'sections': [cards('Мои плейлисты', [_zv_card(p, 'playlist') for p in pls])]}
    if view == 'albums':
        col = await client.get_collection()
        ids = [str(x.id) for x in (col.releases if col else []) or []][:60]
        rels = await client.get_releases(ids) if ids else []
        return {'title': 'Альбомы', 'sections': [cards('Сохранённые', [_zv_card(r, 'release') for r in rels])]}
    secs = []
    try:
        ids = await client.get_editorial_playlist_ids()
        pls = await client.get_playlists([str(i) for i in (ids or [])[:18]])
        secs.append(cards('Подборки редакции', [_zv_card(p, 'playlist') for p in pls]))
    except Exception as e:
        engine.tlog(f"[voinje] Звук: подборки: {e}")
    try:
        liked = await client.get_liked_tracks()
        secs.insert(0, tracks('Недавно понравилось', [_zv_item(t, True) for t in liked[:15]]))
    except Exception:
        pass
    return {'title': 'Звук', 'sub': f"вы вошли как {await dget('zv_name') or 'пользователь'}", 'sections': secs}


async def zv_stream_url(track_id):
    """mp3: 320 кбит/с со входом, 128 — без. FLAC у Звука под DRM — его не берём."""
    zm = await need('zvuk_music', 'zvuk-music')
    client = await zv_client()
    last = None
    for qual in (zm.enums.Quality.HIGH, zm.enums.Quality.MID):
        try:
            return await client.get_stream_url(track_id, qual)
        except Exception as e:
            last = e
    raise RuntimeError(f"Звук не отдал трек: {last}")


# =========================================================================== сайт: разметка, стили, скрипт

WEB_HTML = r'''
<div class="vj" data-app="std">
  <div class="vj-top">
    <div class="vj-hint">где играть →</div>
    <div class="vj-chatpick" title="где играть: группа или канал с голосовым чатом">
      <span class="vj-live"></span><select class="vj-chat"></select>
      <button class="vj-ibtn vj-chats-refresh" type="button" title="обновить список чатов">↻</button>
    </div>
  </div>
  <nav class="vj-apps"></nav>
  <div class="vj-ff" hidden></div>
  <div class="vj-shell">
    <aside class="vj-side"><div class="vj-nav"></div><div class="vj-acc"></div></aside>
    <main class="vj-main">
      <form class="vj-search"><span class="vj-search-ic">⌕</span><input class="vj-q" autocomplete="off"><button class="vj-ibtn vj-q-x" type="button" hidden>✕</button></form>
      <div class="vj-view"></div>
    </main>
    <aside class="vj-queue" hidden>
      <div class="vj-qhead"><b>Очередь</b><span class="vj-qcount"></span><span class="vj-grow"></span>
        <button class="vj-ibtn vj-qclear" type="button" title="очистить">⌫</button><button class="vj-ibtn vj-qclose" type="button">✕</button></div>
      <div class="vj-qnow"></div>
      <div class="vj-qlist"></div>
    </aside>
  </div>
  <div class="vj-bar">
    <div class="vj-b-left">
      <div class="vj-b-cover"><span>♫</span></div>
      <div class="vj-b-meta"><div class="vj-b-title">Ничего не играет</div><div class="vj-b-artist">выберите чат и трек</div></div>
      <button class="vj-ibtn vj-like" type="button" hidden title="мне нравится">♡</button>
    </div>
    <div class="vj-b-mid">
      <div class="vj-ctrls">
        <button class="vj-ibtn" data-op="shuffle" title="перемешать очередь">⤮</button>
        <button class="vj-ibtn vj-big" data-op="prev" title="назад">⏮</button>
        <button class="vj-play" data-op="toggle" title="пауза / играть">▶</button>
        <button class="vj-ibtn vj-big" data-op="skip" title="дальше">⏭</button>
        <button class="vj-ibtn" data-op="loop" title="повтор">⟳</button>
      </div>
      <div class="vj-prog"><span class="vj-pos">0:00</span><div class="vj-track"><i></i><b></b></div><span class="vj-dur">0:00</span></div>
    </div>
    <div class="vj-b-right">
      <button class="vj-ibtn vj-vid" type="button" title="показывать видео в голосовом чате">🎬</button>
      <label class="vj-vol"><span>🔊</span><input type="range" min="1" max="200" value="100"></label>
      <button class="vj-ibtn vj-qbtn" type="button" title="очередь">☰<sup class="vj-qn"></sup></button>
      <button class="vj-ibtn vj-stop" type="button" title="остановить и выйти из голосового чата">⏹</button>
    </div>
  </div>
</div>
'''

WEB_CSS = r'''
.vj { --app: #ff5a14; --app2: #ff8a00; --card: rgba(255,255,255,.045); --card2: rgba(255,255,255,.08); position: relative; padding-bottom: 8px; }
.vj[data-app="ym"] { --app: #ffcc00; --app2: #ff9f1c; } .vj[data-app="sp"] { --app: #1ed760; --app2: #1db954; }
.vj[data-app="yt"] { --app: #ff1f3d; --app2: #ff6b6b; } .vj[data-app="sc"] { --app: #ff7700; --app2: #ffae42; }
.vj[data-app="radio"] { --app: #a78bfa; --app2: #f472b6; } .vj[data-app="tg"] { --app: #2aabee; --app2: #5ed1ff; }
.vj[data-app="files"] { --app: #cbd5e1; --app2: #94a3b8; }
.vj[data-app="ytm"] { --app: #ff2d55; --app2: #ff6b81; } .vj[data-app="zv"] { --app: #ff3dac; --app2: #8b5cf6; }
.vj button { font-family: inherit; }
.vj-top { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
.vj-hint { margin-left: auto; color: var(--ink-3); font-size: 13px; }
.vj-top .vj-chatpick { margin-left: 0; }
.vj-brand { display: flex; align-items: center; gap: 10px; font-size: 22px; }
.vj-logo { width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center; background: linear-gradient(135deg, #ff2a1c, #ff8a00); box-shadow: 0 0 24px rgba(255, 90, 20, .5); font-size: 18px; }
.vj-chatpick { margin-left: auto; display: flex; align-items: center; gap: 8px; background: var(--card); border-radius: 999px; padding: 4px 6px 4px 14px; max-width: min(460px, 60vw); }
.vj-live { width: 8px; height: 8px; border-radius: 50%; background: rgba(255,255,255,.25); flex: none; }
.vj-live.on { background: #48d27a; box-shadow: 0 0 10px #48d27a; animation: vj-pulse 1.6s ease-in-out infinite; }
@keyframes vj-pulse { 50% { opacity: .45; } }
.vj-chat { background: transparent; border: 0; color: var(--ink); font: inherit; font-size: 14px; min-width: 0; flex: 1; outline: none; cursor: pointer; }
.vj-chat option { background: #121214; }
.vj-ibtn { border: 0; background: transparent; color: var(--ink-2); width: 34px; height: 34px; border-radius: 50%; cursor: pointer; font-size: 16px; display: inline-grid; place-items: center; transition: background .15s, color .15s, transform .12s; flex: none; position: relative; }
.vj-ibtn:hover { background: var(--card2); color: var(--ink); }
.vj-ibtn[hidden], .vj [hidden] { display: none !important; } .vj-ibtn:active { transform: scale(.9); }
.vj-ibtn.on { color: var(--app); } .vj-ibtn.on::after { content: ''; position: absolute; bottom: 3px; width: 4px; height: 4px; border-radius: 50%; background: var(--app); }
/* приложения */
.vj-apps { display: flex; flex-wrap: wrap; gap: 8px; padding: 2px 2px 14px; }
.vj-appbtn { --c: #fff; display: flex; align-items: center; gap: 10px; padding: 8px 16px 8px 8px; border-radius: 999px; border: 1px solid transparent; background: var(--card); color: var(--ink-2); cursor: pointer; white-space: nowrap; font-size: 14px; transition: background .2s, color .2s, border-color .2s, transform .2s; }
.vj-appbtn:hover { background: var(--card2); color: var(--ink); transform: translateY(-1px); }
.vj-appbtn i { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; font-style: normal; font-size: 15px; background: color-mix(in srgb, var(--c) 22%, transparent); color: var(--c); }
.vj-appbtn.on { background: color-mix(in srgb, var(--c) 16%, rgba(0,0,0,.4)); border-color: color-mix(in srgb, var(--c) 55%, transparent); color: #fff; }
.vj-appbtn.on i { background: var(--c); color: #111; }
.vj-appbtn .ok { width: 6px; height: 6px; border-radius: 50%; background: #48d27a; margin-left: -4px; }
.vj-ff { margin: 0 0 10px; padding: 10px 14px; border-radius: 10px; background: rgba(255, 170, 60, .1); color: #ffd28a; font-size: 13px; }
/* каркас */
.vj-shell { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 18px; align-items: start; }
.vj-shell.q { grid-template-columns: 220px minmax(0, 1fr) 340px; }
.vj-side { position: sticky; top: calc(var(--nav-h, 64px) + 10px); display: flex; flex-direction: column; gap: 14px; }
.vj-nav { display: flex; flex-direction: column; gap: 2px; background: var(--card); border-radius: 14px; padding: 8px; }
.vj-navi { display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-radius: 10px; color: var(--ink-2); cursor: pointer; font-size: 14px; border: 0; background: none; text-align: left; transition: background .15s, color .15s; }
.vj-navi:hover { background: var(--card2); color: var(--ink); }
.vj-navi.on { background: color-mix(in srgb, var(--app) 18%, transparent); color: #fff; }
.vj-navi span { width: 20px; text-align: center; opacity: .9; }
.vj-acc { background: var(--card); border-radius: 14px; padding: 12px; font-size: 13px; display: flex; flex-direction: column; gap: 8px; }
.vj-acc:empty { display: none; }
.vj-acc .who { display: flex; align-items: center; gap: 8px; } .vj-acc .who i { width: 8px; height: 8px; border-radius: 50%; background: #48d27a; }
.vj-acc .muted { color: var(--ink-3); font-size: 12px; line-height: 1.4; }
.vj-bsel { background: rgba(0,0,0,.35); border: 1px solid var(--line); border-radius: 8px; color: var(--ink); padding: 7px 10px; font: inherit; font-size: 13px; }
.vj-pill.vj-small { padding: 8px 14px; font-size: 13px; justify-content: center; }
.vj-main { min-width: 0; }
.vj-search { display: flex; align-items: center; gap: 8px; background: var(--card); border-radius: 999px; padding: 4px 6px 4px 16px; margin-bottom: 16px; border: 1px solid transparent; transition: border-color .2s, background .2s; }
.vj-search:focus-within { border-color: color-mix(in srgb, var(--app) 60%, transparent); background: var(--card2); }
.vj-search-ic { color: var(--ink-3); font-size: 18px; }
.vj-q { flex: 1; min-width: 0; background: transparent; border: 0; outline: none; color: var(--ink); font: inherit; font-size: 15px; padding: 8px 0; }
/* вид */
.vj-hero { position: relative; display: flex; align-items: flex-end; gap: 20px; padding: 26px 24px 22px; border-radius: 18px; overflow: hidden; margin-bottom: 22px;
  background: linear-gradient(135deg, color-mix(in srgb, var(--app) 42%, #000) 0%, color-mix(in srgb, var(--app2) 18%, #000) 55%, rgba(0,0,0,.2) 100%); }
.vj-hero::after { content: ''; position: absolute; inset: 0; background: radial-gradient(circle at 85% 0%, color-mix(in srgb, var(--app) 35%, transparent), transparent 55%); pointer-events: none; }
.vj-hero-img { width: 150px; height: 150px; flex: none; border-radius: 12px; object-fit: cover; box-shadow: 0 18px 40px rgba(0,0,0,.5); position: relative; z-index: 1; background: var(--card2); }
.vj-hero-img.round { border-radius: 50%; }
.vj-hero-txt { position: relative; z-index: 1; min-width: 0; }
.vj-hero h2 { margin: 0 0 6px; font-size: clamp(26px, 4vw, 44px); line-height: 1.05; overflow-wrap: anywhere; }
.vj-hero p { margin: 0; color: rgba(255,255,255,.75); font-size: 14px; }
.vj-hero-act { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; }
.vj-pill { display: inline-flex; align-items: center; gap: 8px; border: 0; border-radius: 999px; padding: 10px 20px; cursor: pointer; font-size: 14px; font-weight: 700; background: rgba(255,255,255,.12); color: #fff; transition: transform .15s, background .15s; }
.vj-pill:hover { transform: scale(1.04); background: rgba(255,255,255,.2); }
.vj-pill.main { background: var(--app); color: #111; } .vj-pill.main:hover { background: var(--app2); }
/* Моя волна */
.vj-wave { position: relative; height: 210px; border-radius: 20px; overflow: hidden; margin-bottom: 18px; cursor: pointer; display: flex; align-items: center; padding: 0 34px;
  background: #160f05; }
.vj-wave .orb { position: absolute; border-radius: 50%; filter: blur(30px); opacity: .85; animation: vj-orb 9s ease-in-out infinite; }
.vj-wave .o1 { width: 260px; height: 260px; background: #ffcc00; left: 45%; top: -60px; }
.vj-wave .o2 { width: 220px; height: 220px; background: #ff4d6d; left: 62%; top: 40px; animation-delay: -3s; }
.vj-wave .o3 { width: 200px; height: 200px; background: #7c3aed; left: 78%; top: -30px; animation-delay: -6s; }
@keyframes vj-orb { 0%, 100% { transform: translate(0, 0) scale(1); } 33% { transform: translate(-30px, 20px) scale(1.12); } 66% { transform: translate(25px, -15px) scale(.92); } }
.vj-wave-txt { position: relative; z-index: 1; }
.vj-wave h3 { margin: 0; font-size: 38px; } .vj-wave p { margin: 6px 0 16px; color: rgba(255,255,255,.75); }
.vj-wave .vj-pill.main { background: #fff; }
.vj-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 22px; }
.vj-chip { border: 0; border-radius: 999px; padding: 8px 14px; background: var(--card2); color: var(--ink); cursor: pointer; font-size: 13px; transition: background .15s, transform .15s; }
.vj-chip:hover { background: color-mix(in srgb, var(--app) 30%, transparent); transform: translateY(-1px); }
/* секции */
.vj-sec { margin-bottom: 26px; }
.vj-sec-h { display: flex; align-items: center; gap: 10px; margin: 0 0 10px; }
.vj-sec-h h4 { margin: 0; font-size: 20px; } .vj-sec-h .vj-grow { flex: 1; }
.vj-link { border: 0; background: none; color: var(--ink-2); cursor: pointer; font-size: 13px; padding: 6px 8px; border-radius: 8px; }
.vj-link:hover { color: var(--ink); background: var(--card2); }
.vj-grow { flex: 1; }
/* треки */
.vj-tracks { display: flex; flex-direction: column; }
.vj-tr { display: grid; grid-template-columns: 34px 44px minmax(0, 1fr) auto 52px; align-items: center; gap: 12px; padding: 6px 10px; border-radius: 10px; cursor: pointer; transition: background .12s; }
.vj-tr:hover { background: var(--card2); }
.vj-tr.now { background: color-mix(in srgb, var(--app) 14%, transparent); }
.vj-tr.now .vj-tr-t { color: var(--app); }
.vj-tr-n { color: var(--ink-3); font-size: 13px; text-align: center; font-variant-numeric: tabular-nums; }
.vj-tr:hover .vj-tr-n span { display: none; } .vj-tr-n b { display: none; color: var(--ink); } .vj-tr:hover .vj-tr-n b { display: inline; }
.vj-th { position: relative; overflow: hidden; flex: none; width: 44px; height: 44px; border-radius: 6px; background: var(--card2); display: grid; place-items: center; color: var(--ink-3); }
.vj-th img, .vj-b-cover img, .vj-card-img img, .vj-vid-img img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; }
.vj img { max-width: 100%; }
.vj-th img, .vj-card-img img, .vj-vid-img img, .vj-b-cover img { opacity: 0; transition: opacity .25s; } .vj img.ok { opacity: 1; }
.vj-th.round { border-radius: 50%; }
.vj-tr-m { min-width: 0; } .vj-tr-t { font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.vj-tr-a { font-size: 12.5px; color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
.vj-tr-act { display: flex; gap: 2px; opacity: 0; transition: opacity .12s; } .vj-tr:hover .vj-tr-act, .vj-tr-act .liked { opacity: 1; }
.vj-tr:hover .vj-tr-act { opacity: 1; }
.vj-tr-act .vj-ibtn { width: 30px; height: 30px; font-size: 14px; } .vj-tr-act .liked { color: #ff4d6d; opacity: 1; }
.vj-tr-d { color: var(--ink-3); font-size: 12.5px; text-align: right; font-variant-numeric: tabular-nums; }
.vj-live-tag { color: #ff4d6d; font-size: 11px; font-weight: 700; }
/* карточки */
.vj-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 16px; }
.vj-card { position: relative; padding: 10px; border-radius: 14px; background: var(--card); cursor: pointer; transition: background .2s, transform .2s; min-width: 0; }
.vj-card:hover { background: var(--card2); transform: translateY(-2px); }
.vj-card-img { position: relative; aspect-ratio: 1; border-radius: 10px; overflow: hidden; background: linear-gradient(135deg, color-mix(in srgb, var(--app) 40%, #000), #111); display: grid; place-items: center; font-size: 34px; color: rgba(255,255,255,.5); box-shadow: 0 8px 22px rgba(0,0,0,.35); }
.vj-card-img.round { border-radius: 50%; }
.vj-card-img img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.vj-card-play { position: absolute; right: 8px; bottom: 8px; width: 42px; height: 42px; border-radius: 50%; border: 0; background: var(--app); color: #111; font-size: 16px; cursor: pointer; opacity: 0; transform: translateY(8px); transition: opacity .2s, transform .2s; box-shadow: 0 8px 18px rgba(0,0,0,.45); }
.vj-card:hover .vj-card-play { opacity: 1; transform: none; }
.vj-card-t { margin-top: 10px; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.vj-card-s { font-size: 12px; color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
/* видео */
.vj-vids { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 16px; }
.vj-vid-c { cursor: pointer; min-width: 0; }
.vj-vid-img { position: relative; aspect-ratio: 16/9; border-radius: 12px; overflow: hidden; background: var(--card2); display: grid; place-items: center; font-size: 28px; color: rgba(255,255,255,.35); }
.vj-vid-img img { position: absolute; inset: 0; }
.vj-vid-img img { width: 100%; height: 100%; object-fit: cover; transition: transform .3s; } .vj-vid-c:hover .vj-vid-img img { transform: scale(1.05); }
.vj-vid-dur { position: absolute; right: 6px; bottom: 6px; background: rgba(0,0,0,.8); color: #fff; font-size: 11.5px; padding: 2px 6px; border-radius: 4px; }
.vj-vid-act { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; gap: 10px; background: rgba(0,0,0,.45); opacity: 0; transition: opacity .2s; }
.vj-vid-c:hover .vj-vid-act { opacity: 1; }
.vj-vid-act button { border: 0; border-radius: 999px; padding: 8px 14px; background: rgba(255,255,255,.92); color: #111; cursor: pointer; font-size: 13px; font-weight: 700; }
.vj-vid-act button.v { background: var(--app); color: #fff; }
.vj-vid-t { margin-top: 8px; font-size: 14px; line-height: 1.3; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.vj-vid-s { font-size: 12px; color: var(--ink-3); margin-top: 3px; }
/* станции */
.vj-stations { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
.vj-st { display: flex; align-items: center; gap: 12px; padding: 10px; border-radius: 12px; background: var(--card); cursor: pointer; transition: background .15s; min-width: 0; }
.vj-st:hover { background: var(--card2); } .vj-st.now { background: color-mix(in srgb, var(--app) 18%, transparent); }
.vj-st .vj-th { width: 48px; height: 48px; border-radius: 10px; }
/* вход */
.vj-login { display: flex; flex-direction: column; gap: 14px; padding: 22px; border-radius: 18px; background: var(--card); margin-bottom: 22px; border: 1px solid color-mix(in srgb, var(--app) 35%, transparent); }
.vj-login h3 { margin: 0; font-size: 22px; }
.vj-login .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.vj-login input { flex: 1; min-width: 200px; background: rgba(0,0,0,.35); border: 1px solid var(--line); border-radius: 10px; padding: 11px 14px; color: var(--ink); font: inherit; font-size: 14px; outline: none; }
.vj-login input:focus { border-color: var(--app); }
.vj-login .row .vj-pill { flex: none; }
.vj-code { font-family: var(--mono); font-size: 34px; letter-spacing: .2em; color: var(--app); background: rgba(0,0,0,.35); padding: 8px 18px; border-radius: 12px; }
.vj-steps { margin: 0; padding-left: 20px; color: var(--ink-2); font-size: 13.5px; line-height: 1.7; }
.vj-steps code { background: rgba(255,255,255,.08); padding: 1px 6px; border-radius: 4px; font-size: 12px; }
.vj-err { color: #ff9b8f; font-size: 13px; } .vj-note { color: var(--ink-3); font-size: 12.5px; line-height: 1.45; }
.vj-drop { border: 2px dashed rgba(255,255,255,.18); border-radius: 16px; padding: 26px; text-align: center; margin-bottom: 20px; transition: border-color .2s, background .2s; color: var(--ink-2); }
.vj-drop.over { border-color: var(--app); background: color-mix(in srgb, var(--app) 10%, transparent); }
.vj-drop label { color: var(--app); cursor: pointer; text-decoration: underline; }
.vj-upbar { height: 4px; background: rgba(255,255,255,.1); border-radius: 2px; overflow: hidden; margin-top: 12px; } .vj-upbar i { display: block; height: 100%; width: 0; background: var(--app); transition: width .2s; }
.vj-empty { padding: 50px 20px; text-align: center; color: var(--ink-3); }
.vj-skel { height: 56px; border-radius: 10px; margin-bottom: 6px; background: linear-gradient(90deg, var(--card) 0%, var(--card2) 50%, var(--card) 100%); background-size: 200% 100%; animation: vj-sk 1.2s linear infinite; }
@keyframes vj-sk { to { background-position: -200% 0; } }
/* очередь */
.vj-queue { position: sticky; top: calc(var(--nav-h, 64px) + 10px); background: var(--card); border-radius: 16px; padding: 12px; max-height: calc(100vh - var(--nav-h, 64px) - 140px); display: flex; flex-direction: column; }
.vj-qhead { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; } .vj-qhead b { font-size: 16px; } .vj-qcount { color: var(--ink-3); font-size: 13px; }
.vj-qnow:empty { display: none; } .vj-qnow { margin-bottom: 10px; }
.vj-qlbl { font-size: 12px; color: var(--ink-3); margin: 6px 4px; text-transform: uppercase; letter-spacing: .06em; }
.vj-qlist { overflow: auto; flex: 1; }
.vj-qi { display: grid; grid-template-columns: 40px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 6px; border-radius: 10px; }
.vj-qi:hover { background: var(--card2); } .vj-qi .vj-th { width: 40px; height: 40px; }
.vj-qi-act { display: flex; opacity: 0; } .vj-qi:hover .vj-qi-act { opacity: 1; } .vj-qi-act .vj-ibtn { width: 28px; height: 28px; font-size: 13px; }
/* плеер снизу */
.vj-bar { position: sticky; bottom: 10px; z-index: 20; margin-top: 18px; display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 1.4fr) minmax(0, 1fr); align-items: center; gap: 16px;
  padding: 10px 16px; border-radius: 18px; background: rgba(14, 14, 16, .92); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); box-shadow: 0 -2px 0 rgba(255,255,255,.04), 0 18px 50px rgba(0,0,0,.6); border: 1px solid rgba(255,255,255,.06); }
.vj-b-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.vj-b-cover { width: 56px; height: 56px; border-radius: 8px; flex: none; overflow: hidden; display: grid; place-items: center; background: linear-gradient(135deg, var(--app), #222); font-size: 22px; position: relative; }
.vj-b-cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.vj-playing .vj-b-cover { box-shadow: 0 0 22px color-mix(in srgb, var(--app) 55%, transparent); }
.vj-b-meta { min-width: 0; } .vj-b-title { font-size: 14.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.vj-b-artist { font-size: 12.5px; color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
.vj-like { font-size: 18px; } .vj-like.liked { color: #ff4d6d; }
.vj-b-mid { display: flex; flex-direction: column; align-items: center; gap: 4px; min-width: 0; }
.vj-ctrls { display: flex; align-items: center; gap: 10px; }
.vj-big { font-size: 18px; }
.vj-play { width: 44px; height: 44px; border-radius: 50%; border: 0; background: #fff; color: #111; font-size: 17px; cursor: pointer; transition: transform .12s, background .2s; display: grid; place-items: center; }
.vj-play:hover { transform: scale(1.07); } .vj-play:active { transform: scale(.93); }
.vj-play.busy { background: conic-gradient(var(--app) 0 25%, #fff 25% 100%); animation: vj-spin 1s linear infinite; }
@keyframes vj-spin { to { transform: rotate(360deg); } }
.vj-prog { display: flex; align-items: center; gap: 10px; width: 100%; font-size: 11.5px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
.vj-prog > span { width: 40px; text-align: center; flex: none; }
.vj-track { position: relative; flex: 1; height: 4px; border-radius: 2px; background: rgba(255,255,255,.16); cursor: pointer; }
.vj-track::before { content: ''; position: absolute; inset: -8px 0; }
.vj-track i { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 2px; background: #fff; width: 0; }
.vj-track:hover i { background: var(--app); }
.vj-track b { position: absolute; top: 50%; width: 12px; height: 12px; margin: -6px 0 0 -6px; border-radius: 50%; background: #fff; left: 0; opacity: 0; transition: opacity .15s; }
.vj-track:hover b { opacity: 1; }
.vj-track.live i { width: 100% !important; background: linear-gradient(90deg, #ff4d6d, var(--app)); opacity: .7; }
.vj-track.busy i { width: 100% !important; background: repeating-linear-gradient(90deg, rgba(255,255,255,.15) 0 12px, var(--app) 12px 24px); background-size: 48px 100%; animation: vj-busy .7s linear infinite; }
@keyframes vj-busy { to { background-position: 48px 0; } }
.vj-b-right { display: flex; align-items: center; justify-content: flex-end; gap: 4px; }
.vj-vol { display: flex; align-items: center; gap: 6px; color: var(--ink-2); font-size: 14px; }
.vj-vol input { width: 100px; accent-color: var(--app); }
.vj-qn { position: absolute; top: 2px; right: 0; font-size: 9px; background: var(--app); color: #111; border-radius: 8px; padding: 0 4px; line-height: 14px; }
.vj-qn:empty { display: none; }
@media (max-width: 1180px) { .vj-shell.q { grid-template-columns: 190px minmax(0, 1fr); } .vj-shell.q .vj-queue { position: fixed; right: 12px; top: 90px; bottom: 110px; width: min(340px, 92vw); z-index: 30; max-height: none; background: #111114; box-shadow: 0 20px 60px rgba(0,0,0,.6); } }
@media (max-width: 860px) {
  .vj-shell, .vj-shell.q { grid-template-columns: minmax(0, 1fr); }
  .vj-side { position: static; } .vj-nav { flex-direction: row; overflow-x: auto; } .vj-navi { white-space: nowrap; }
  .vj-bar { grid-template-columns: minmax(0, 1fr) auto; } .vj-b-mid { grid-column: 1 / -1; order: 3; } .vj-vol { display: none; }
  .vj-tr { grid-template-columns: 44px minmax(0, 1fr) auto; } .vj-tr-n, .vj-tr-d { display: none; } .vj-tr-act { opacity: 1; }
  .vj-hero { flex-direction: column; align-items: flex-start; } .vj-hero-img { width: 110px; height: 110px; }
}
'''

WEB_JS = r'''
(() => {
'use strict';
FRXApps.register('voinje', (app) => {
  const R = app.root, V = R.querySelector('.vj');
  const $ = (s) => R.querySelector(s), $$ = (s) => [...R.querySelectorAll(s)];
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; };
  const btn = (cls, text, title, fn) => { const b = el('button', cls, text); b.type = 'button'; if (title) b.title = title; if (fn) b.addEventListener('click', (e) => { e.stopPropagation(); fn(e); }); return b; };
  const dur = (s) => { s = Math.max(0, Math.floor(s || 0)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60; return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); };
  const store = { get(k, d) { try { const v = localStorage.getItem('vj_' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
                  set(k, v) { try { localStorage.setItem('vj_' + k, JSON.stringify(v)); } catch (e) { /* нет хранилища */ } } };
  const call = async (action, data) => { try { return await app.call(action, data); } catch (e) { app.toast(e.message, 'error'); throw e; } };
  const imgUrl = (u) => (u && /^https:/.test(u) ? app.res('img', { u }) : '');
  const pic = (u, cls, ph) => {
    const w = el('div', cls); w.textContent = ph || '♫';
    if (u && /^https:/.test(u)) { const i = el('img'); i.alt = ''; i.decoding = 'async'; i.onload = () => i.classList.add('ok'); i.onerror = () => i.remove(); i.src = imgUrl(u); w.appendChild(i); }
    return w;
  };

  const APPS = [
    { id: 'std', name: 'Музыка', ic: '♪', c: '#ff5a14', nav: [['home', '⌂', 'Главная'], ['search', '⌕', 'Поиск']], ph: 'исполнитель - трек, название или ссылка' },
    { id: 'ym', name: 'Яндекс Музыка', ic: 'Я', c: '#ffcc00', nav: [['home', '⌂', 'Главная'], ['likes', '♥', 'Мне нравится'], ['playlists', '≡', 'Плейлисты'], ['albums', '◉', 'Альбомы'], ['artists', '☺', 'Исполнители'], ['search', '⌕', 'Поиск']], ph: 'треки, альбомы, исполнители, подкасты' },
    { id: 'sp', name: 'Spotify', ic: '●', c: '#1ed760', nav: [['home', '⌂', 'Главная'], ['likes', '♥', 'Любимые треки'], ['playlists', '≡', 'Плейлисты'], ['albums', '◉', 'Альбомы'], ['search', '⌕', 'Поиск']], ph: 'что хотите послушать?' },
    { id: 'ytm', name: 'YouTube Music', ic: '◎', c: '#ff2d55', nav: [['home', '⌂', 'Главная'], ['likes', '♥', 'Мне нравится'], ['playlists', '≡', 'Плейлисты'], ['albums', '◉', 'Альбомы'], ['search', '⌕', 'Поиск']], ph: 'треки, альбомы, исполнители YouTube Music' },
    { id: 'zv', name: 'Звук', ic: 'З', c: '#ff3dac', nav: [['home', '⌂', 'Главная'], ['likes', '♥', 'Мне нравится'], ['playlists', '≡', 'Плейлисты'], ['albums', '◉', 'Альбомы'], ['search', '⌕', 'Поиск']], ph: 'треки, альбомы и исполнители в Звуке' },
    { id: 'yt', name: 'YouTube', ic: '▶', c: '#ff1f3d', nav: [['home', '⌂', 'Главная'], ['liked', '♥', 'Понравившиеся'], ['search', '⌕', 'Поиск']], ph: 'поиск на YouTube или ссылка на видео / плейлист' },
    { id: 'sc', name: 'SoundCloud', ic: '☁', c: '#ff7700', nav: [['home', '⌕', 'Поиск']], ph: 'треки, миксы, подкасты на SoundCloud' },
    { id: 'radio', name: 'Радио', ic: '◉', c: '#a78bfa', nav: [['home', '⌂', 'Станции'], ['search', '⌕', 'Поиск']], ph: 'станция, жанр или город' },
    { id: 'tg', name: 'Telegram', ic: '✈', c: '#2aabee', nav: [['home', '♫', 'Избранное']], ph: 'поиск по музыке в Избранном' },
    { id: 'files', name: 'Файлы', ic: '▤', c: '#cbd5e1', nav: [['home', '▤', 'Библиотека']], ph: 'поиск по файлам' },
  ];
  const A = (id) => APPS.find((a) => a.id === id) || APPS[0];

  let chat = store.get('chat', null);
  let cur = store.get('app', 'std');
  let viewStack = [];          // история внутри приложения (для «назад»)
  let P = null, polled = 0, busy = false, svc = null, viewSeq = 0;

  /* ---------------- приложения ---------------- */
  function drawApps() {
    const box = $('.vj-apps'); box.replaceChildren();
    APPS.forEach((a) => {
      const b = el('button', 'vj-appbtn' + (a.id === cur ? ' on' : '')); b.type = 'button'; b.style.setProperty('--c', a.c);
      b.append(el('i', '', a.ic), el('span', '', a.name));
      if (svc && ((a.id === 'ym' && svc.ym.on) || (a.id === 'sp' && svc.sp.on) || (a.id === 'yt' && svc.yt.on))) b.appendChild(el('span', 'ok'));
      b.addEventListener('click', () => openApp(a.id));
      box.appendChild(b);
    });
  }
  function drawSide(view) {
    const a = A(cur), nav = $('.vj-nav'); nav.replaceChildren();
    if (viewStack.length > 1) { const b = btn('vj-navi', '', 'назад', back); b.append(el('span', '', '‹'), document.createTextNode('Назад')); nav.appendChild(b); }
    a.nav.forEach(([id, ic, name]) => {
      const b = btn('vj-navi' + (view === id ? ' on' : ''), '', '', () => go({ view: id }, true));
      b.append(el('span', '', ic), document.createTextNode(name)); nav.appendChild(b);
    });
    drawAcc();
  }
  function drawAcc() {
    const box = $('.vj-acc'); box.replaceChildren();
    if (!svc) return;
    if (cur === 'ym' && svc.ym.on) {
      const w = el('div', 'who'); w.append(el('i'), el('b', '', svc.ym.login || 'аккаунт')); box.append(w, btn('vj-link', 'выйти', '', async () => { await call('ym_out'); await loadSvc(); go({ view: 'home' }, true); }));
    } else if (cur === 'sp' && svc.sp.on) {
      const w = el('div', 'who'); w.append(el('i'), el('b', '', svc.sp.name || 'аккаунт')); box.append(w, el('div', 'muted', 'звук — из открытых источников: сам звук Spotify зашифрован'), btn('vj-link', 'выйти', '', async () => { await call('sp_out'); await loadSvc(); go({ view: 'home' }, true); }));
    } else if (cur === 'zv' && svc.zv && svc.zv.on) {
      const w = el('div', 'who'); w.append(el('i'), el('b', '', svc.zv.name || 'аккаунт')); box.append(w, el('div', 'muted', '320 кбит/с'), btn('vj-link', 'выйти', '', async () => { await call('zv_out'); await loadSvc(); go({ view: 'home' }, true); }));
    } else if (cur === 'ytm') {
      box.append(el('div', 'muted', svc.yt.on ? 'Библиотека — через вход в YouTube ✓' : 'Вход — тот же, что у YouTube: откройте приложение YouTube и нажмите «Войти через браузер».'));
    } else if (cur === 'yt') {
      const ck = Object.assign(el('input'), { type: 'file', accept: '.txt', hidden: true });
      ck.addEventListener('change', async () => { if (!ck.files[0]) return; await app.upload('upload_cookies', ck.files[0]); app.toast('cookies сохранены', 'ok'); await loadSvc(); drawAcc(); });
      if (svc.yt.on) {
        const w = el('div', 'who'); w.append(el('i'), el('b', '', svc.yt.browser ? 'вошли через ' + svc.yt.browser : 'вошли (cookies)'));
        box.append(w, btn('vj-link', 'выйти', '', async () => { await call('yt_out'); await loadSvc(); drawAcc(); }));
      } else {
        box.append(el('b', '', 'Войти в YouTube'), el('div', 'muted', 'через браузер на этом компьютере, где вы уже вошли в YouTube. Пароль не нужен.'));
        const sel = el('select', 'vj-bsel'); (svc.yt.browsers || []).forEach((b) => { const o = el('option', '', b); o.value = b; sel.appendChild(o); });
        const err = el('div', 'muted');
        box.append(sel, btn('vj-pill main vj-small', 'Войти через браузер', '', async () => {
          err.textContent = 'читаю cookies…';
          try { const r = await app.call('yt_browser', { browser: sel.value }); app.toast('YouTube: ' + (r.account || 'вход выполнен'), 'ok'); await loadSvc(); drawAcc(); }
          catch (e) { err.textContent = e.message; }
        }), err, el('div', 'muted', 'или файл cookies.txt (расширение «Get cookies.txt LOCALLY»):'), btn('vj-link', 'загрузить cookies.txt', '', () => ck.click()), ck);
      }
    } else if (cur === 'files') {
      box.append(el('div', 'muted', 'Файлы библиотеки не входят в бэкап FRX. Свои папки с музыкой — настройка library_dirs.'));
    }
  }
  function openApp(id) {
    cur = id; store.set('app', id); V.dataset.app = id;
    viewStack = [];
    $('.vj-q').value = ''; $('.vj-q').placeholder = A(id).ph;
    drawApps();
    go(store.get('view_' + id, { view: 'home' }), true);
  }
  function back() { viewStack.pop(); const v = viewStack.pop(); if (v) go(v); }

  /* ---------------- виды ---------------- */
  async function go(v, reset) {
    if (reset) viewStack = [];
    viewStack.push(v);
    if (!v.q) store.set('view_' + cur, { view: ['album', 'artist', 'playlist', 'link', 'show', 'item'].includes(v.view) ? 'home' : v.view });
    drawSide(v.view);
    const box = $('.vj-view'), seq = ++viewSeq;
    if (v.view === 'search' && !v.q) { box.replaceChildren(el('div', 'vj-empty', 'Введите запрос в строке поиска сверху')); $('.vj-q').focus(); return; }
    box.replaceChildren(...Array.from({ length: 6 }, () => el('div', 'vj-skel')));
    try {
      const r = await app.call('browse', { app: cur, view: v.view, id: v.id, q: v.q });
      if (seq !== viewSeq) return;
      render(r.view, v);
    } catch (e) {
      if (seq !== viewSeq) return;
      box.replaceChildren(el('div', 'vj-empty', e.message));
    }
  }
  function hero(d, firstTracks) {
    const h = el('div', 'vj-hero');
    if (d.img) { const i = el('img', 'vj-hero-img ok' + (d.round ? ' round' : '')); i.alt = ''; i.onerror = () => i.remove(); i.src = imgUrl(d.img); h.appendChild(i); }
    const t = el('div', 'vj-hero-txt'); t.append(el('h2', '', d.title || ''), el('p', '', d.sub || ''));
    if (firstTracks && firstTracks.length) {
      const act = el('div', 'vj-hero-act');
      act.append(btn('vj-pill main', '▶  Слушать', '', () => add(firstTracks, true)),
        btn('vj-pill', '⤮  Вперемешку', '', () => add([...firstTracks].sort(() => Math.random() - 0.5), true)),
        btn('vj-pill', '+  В очередь', '', () => add(firstTracks, false)));
      t.appendChild(act);
    }
    if (d.buttons && d.buttons.length) {
      const act = t.querySelector('.vj-hero-act') || t.appendChild(el('div', 'vj-hero-act'));
      d.buttons.forEach((b) => act.appendChild(btn('vj-pill', b.title, '', async () => { const r = await call(b.call, b.data || {}); if (r && r.toast) app.toast(r.toast, 'ok'); go(viewStack.pop()); })));
    }
    h.appendChild(t);
    return h;
  }
  function render(d, v) {
    const box = $('.vj-view'); box.replaceChildren();
    const firstTr = (d.sections || []).find((s) => s.kind === 'tracks' && s.all && s.items.length);
    if (d.wave) box.appendChild(waveCard(d.stations || []));
    else box.appendChild(hero(d, ['album', 'playlist', 'artist', 'likes', 'link', 'item'].includes(v.view) && firstTr ? firstTr.items : null));
    if (d.login) box.appendChild(cur === 'ym' ? loginYm() : cur === 'zv' ? loginZv() : loginSp());
    if (d.tags) { const c = el('div', 'vj-chips'); d.tags.forEach((t) => c.appendChild(btn('vj-chip', t, '', () => { $('.vj-q').value = t; go({ view: 'search', q: t }); }))); box.appendChild(c); }
    if (d.files) box.appendChild(dropZone());
    (d.sections || []).forEach((s) => { if (s.items && s.items.length) box.appendChild(section(s)); });
    if (!(d.sections || []).some((s) => s.items && s.items.length) && !d.login && !d.files) box.appendChild(el('div', 'vj-empty', 'Здесь пока пусто'));
    markNow();
  }
  function section(s) {
    const w = el('div', 'vj-sec');
    const hd = el('div', 'vj-sec-h'); hd.append(el('h4', '', s.title), el('span', 'vj-grow'));
    const items = s.items;
    if (s.all && items.length > 1) hd.append(btn('vj-link', '▶ всё', 'слушать всё', () => add(items, true)), btn('vj-link', '+ в очередь', '', () => add(items, false)));
    w.appendChild(hd);
    if (s.kind === 'cards') { const g = el('div', 'vj-cards'); items.forEach((c) => g.appendChild(cardEl(c))); w.appendChild(g); }
    else if (s.kind === 'videos') { const g = el('div', 'vj-vids'); items.forEach((it) => g.appendChild(videoEl(it))); w.appendChild(g); }
    else if (s.kind === 'stations') { const g = el('div', 'vj-stations'); items.forEach((it) => g.appendChild(stationEl(it))); w.appendChild(g); }
    else { const g = el('div', 'vj-tracks'); items.forEach((it, i) => g.appendChild(trackEl(it, i))); w.appendChild(g); }
    return w;
  }
  function trackEl(it, i) {
    const r = el('div', 'vj-tr'); r.dataset.key = keyOf(it);
    const n = el('div', 'vj-tr-n'); n.append(el('span', '', String(i + 1)), el('b', '', '▶'));
    const m = el('div', 'vj-tr-m'); m.append(el('div', 'vj-tr-t', it.title), el('div', 'vj-tr-a', it.artist || it.service || ''));
    const act = el('div', 'vj-tr-act');
    if (it.liked !== undefined) { const l = btn('vj-ibtn' + (it.liked ? ' liked' : ''), it.liked ? '♥' : '♡', 'мне нравится', async () => { it.liked = !it.liked; l.textContent = it.liked ? '♥' : '♡'; l.classList.toggle('liked', it.liked); await call('ym_like', { id: it.src.id, on: it.liked }); }); act.appendChild(l); }
    act.appendChild(btn('vj-ibtn', '+', 'в очередь', () => add([it], false)));
    if (!it.live && ['YouTube', 'YouTube Music', 'Ссылка', 'Telegram', 'Файл'].includes(it.service)) act.appendChild(btn('vj-ibtn', '🎬', 'как видео', () => add([it], true, true)));
    if (it.src && it.src.t === 'file' && /[\\/]library[\\/]/.test(it.src.path)) act.appendChild(btn('vj-ibtn', '🗑', 'удалить файл', async () => { if (confirm('Удалить файл из библиотеки?')) { await call('lib_delete', { path: it.src.path }); go(viewStack.pop()); } }));
    r.append(n, pic(it.thumb, 'vj-th'), m, act, el('div', 'vj-tr-d', it.live ? '' : dur(it.duration)));
    if (it.live) r.lastChild.appendChild(el('span', 'vj-live-tag', 'ЭФИР'));
    r.addEventListener('click', () => add([it], true));
    return r;
  }
  function cardEl(c) {
    const w = el('div', 'vj-card');
    const im = pic(c.img, 'vj-card-img' + (c.round ? ' round' : ''), c.round ? '☺' : '♫');
    if (c.open && c.open.view !== 'artist') im.appendChild(btn('vj-card-play', '▶', 'слушать', async () => {
      const r = await call('browse', { app: cur, view: c.open.view, id: c.open.id });
      const t = (r.view.sections || []).find((s) => s.kind === 'tracks');
      if (t) add(t.items, true);
    }));
    w.append(im, el('div', 'vj-card-t', c.title), el('div', 'vj-card-s', c.sub || ''));
    w.addEventListener('click', () => { if (c.open && c.open.view) go({ view: c.open.view, id: c.open.id }); });
    return w;
  }
  function videoEl(it) {
    const w = el('div', 'vj-vid-c'); w.dataset.key = keyOf(it);
    const im = pic(it.thumb, 'vj-vid-img', '▶');
    if (it.duration) im.appendChild(el('span', 'vj-vid-dur', dur(it.duration)));
    const act = el('div', 'vj-vid-act');
    act.append(btn('', '♪ звук', 'играть звук', () => add([it], true)), btn('v', '🎬 видео', 'показывать видео', () => add([it], true, true)), btn('', '+', 'в очередь', () => add([it], false)));
    im.appendChild(act);
    w.append(im, el('div', 'vj-vid-t', it.title), el('div', 'vj-vid-s', it.artist || ''));
    return w;
  }
  function stationEl(it) {
    const w = el('div', 'vj-st'); w.dataset.key = keyOf(it);
    const m = el('div', 'vj-tr-m'); m.append(el('div', 'vj-tr-t', it.title), el('div', 'vj-tr-a', it.artist || ''));
    w.append(pic(it.thumb, 'vj-th', '◉'), m);
    w.addEventListener('click', () => add([it], true));
    return w;
  }
  function waveCard(stations) {
    const f = document.createDocumentFragment();
    const w = el('div', 'vj-wave');
    w.append(el('i', 'orb o1'), el('i', 'orb o2'), el('i', 'orb o3'));
    const t = el('div', 'vj-wave-txt'); t.append(el('h3', '', 'Моя волна'), el('p', '', 'бесконечный поток под ваш вкус'), btn('vj-pill main', '▶  Запустить', '', () => wave('user:onyourwave')));
    w.appendChild(t); w.addEventListener('click', () => wave('user:onyourwave'));
    f.appendChild(w);
    if (stations.length) { const c = el('div', 'vj-chips'); stations.forEach((st) => c.appendChild(btn('vj-chip', st.title, 'волна: ' + st.title, () => wave(st.id)))); f.appendChild(c); }
    return f;
  }
  async function wave(station) {
    if (!chat) { pickChatHint(); return; }
    const r = await call('wave', { chat, station }); P = r.player; polled = Date.now(); renderPlayer(); app.toast('волна запущена', 'ok');
  }

  /* ---------------- вход ---------------- */
  let ymTimer = null;
  function loginYm() {
    const w = el('div', 'vj-login');
    w.append(el('h3', '', 'Войти в Яндекс Музыку'), el('div', 'vj-note', 'Вход кодом — как на телевизоре: код вводится на сайте Яндекса под вашим аккаунтом. Токен хранится только в базе FRX на этом компьютере.'));
    const row = el('div', 'row'), codeBox = el('div', 'row'), err = el('div', 'vj-err');
    const show = (c, u, left) => {
      const open = Object.assign(el('a', 'vj-pill main', 'Открыть ' + u.replace(/^https?:\/\//, '')), { href: u, target: '_blank', rel: 'noopener' });
      const cnt = el('span', 'vj-note');
      codeBox.replaceChildren(el('span', 'vj-code', c), open, cnt);
      let until = Date.now() + (left || 300) * 1000;
      clearInterval(ymTimer);
      ymTimer = setInterval(async () => {
        if (!app.alive() || !codeBox.isConnected) { clearInterval(ymTimer); return; }
        const s = Math.max(0, Math.round((until - Date.now()) / 1000));
        cnt.textContent = 'ждём подтверждения… код действует ещё ' + dur(s);
        if (s <= 0) { clearInterval(ymTimer); codeBox.replaceChildren(); err.textContent = 'код истёк — нажмите «получить код» ещё раз'; return; }
        if (s % 3) return;
        try { const r = await app.call('ym_poll'); if (r.ok) { clearInterval(ymTimer); app.toast('Яндекс Музыка: вы вошли', 'ok'); await loadSvc(); go({ view: 'home' }, true); } err.textContent = ''; }
        catch (e) { clearInterval(ymTimer); codeBox.replaceChildren(); err.textContent = e.message; }
      }, 1000);
    };
    row.appendChild(btn('vj-pill main', 'Получить код', '', async () => { err.textContent = ''; try { const r = await app.call('ym_code'); show(r.code, r.url, r.left); } catch (e) { err.textContent = e.message; } }));
    w.append(row, codeBox, err);
    if (svc && svc.ym.device) show(svc.ym.device.code, svc.ym.device.url, svc.ym.device.left);
    const tok = Object.assign(el('input'), { type: 'password', placeholder: 'или вставьте токен Яндекс Музыки' });
    const r2 = el('div', 'row'); r2.append(tok, btn('vj-pill', 'Войти токеном', '', async () => { try { await app.call('ym_token', { token: tok.value }); app.toast('вы вошли', 'ok'); await loadSvc(); go({ view: 'home' }, true); } catch (e) { err.textContent = e.message; } }));
    w.appendChild(r2);
    return w;
  }
  function loginZv() {
    const w = el('div', 'vj-login');
    w.append(el('h3', '', 'Войти в Звук'), el('div', 'vj-note', 'У Звука нет входа для сторонних программ, поэтому берём токен вашего аккаунта — из браузера, где вы уже вошли на zvuk.com, или вставьте его вручную. Токен хранится только в базе FRX.'));
    const err = el('div', 'vj-err');
    const sel = el('select', 'vj-bsel'); ((svc && svc.yt.browsers) || ['firefox', 'chrome', 'edge']).forEach((b) => { const o = el('option', '', b); o.value = b; sel.appendChild(o); });
    const r1 = el('div', 'row'); r1.append(sel, btn('vj-pill main', 'Войти через браузер', '', async () => { err.textContent = 'читаю…'; try { await app.call('zv_browser', { browser: sel.value }); app.toast('Звук: вы вошли', 'ok'); await loadSvc(); go({ view: 'home' }, true); } catch (e) { err.textContent = e.message; } }));
    const tok = Object.assign(el('input'), { type: 'password', placeholder: 'или токен: откройте zvuk.com/api/v2/tiny/profile и скопируйте "token"' });
    const r2 = el('div', 'row'); r2.append(tok, btn('vj-pill', 'Войти токеном', '', async () => { try { await app.call('zv_token', { token: tok.value }); app.toast('Звук: вы вошли', 'ok'); await loadSvc(); go({ view: 'home' }, true); } catch (e) { err.textContent = e.message; } }));
    w.append(r1, r2, err, el('div', 'vj-note', 'Без входа тоже играет — в 128 кбит/с. Неофициальный API: если Звук что-то поменяет, вход может перестать работать до обновления модуля.'));
    return w;
  }
  function loginSp() {
    const w = el('div', 'vj-login');
    w.append(el('h3', '', 'Войти в Spotify'));
    const steps = el('ol', 'vj-steps');
    const li = (...parts) => { const l = el('li'); parts.forEach((p) => l.append(typeof p === 'string' ? document.createTextNode(p) : p)); steps.appendChild(l); };
    li('Откройте ', Object.assign(el('a', '', 'developer.spotify.com/dashboard'), { href: 'https://developer.spotify.com/dashboard', target: '_blank', rel: 'noopener' }), ' → Create app.');
    li('Redirect URI: ', el('code', '', (svc && svc.sp.redirect) || 'http://127.0.0.1:8888/callback'), ', API — Web API. Сохраните.');
    li('Скопируйте Client ID сюда и нажмите «Войти».');
    li('После входа откроется страница, которая не загрузится, — так и должно быть: скопируйте её адрес и вставьте во второе поле.');
    w.appendChild(steps);
    const err = el('div', 'vj-err');
    const cid = Object.assign(el('input'), { placeholder: 'Client ID', value: (svc && svc.sp.client_id) || '' });
    const r1 = el('div', 'row'); r1.append(cid, btn('vj-pill main', 'Войти', '', async () => { try { const r = await app.call('sp_url', { client_id: cid.value }); window.open(r.url, '_blank', 'noopener'); await loadSvc(); go({ view: 'home' }, true); } catch (e) { err.textContent = e.message; } }));
    w.appendChild(r1);
    if (svc && svc.sp.pending) {
      const back = Object.assign(el('input'), { placeholder: 'http://127.0.0.1:8888/callback?code=…' });
      const r2 = el('div', 'row'); r2.append(back, btn('vj-pill main', 'Готово', '', async () => { try { await app.call('sp_finish', { url: back.value }); app.toast('Spotify: вы вошли', 'ok'); await loadSvc(); go({ view: 'home' }, true); } catch (e) { err.textContent = e.message; } }));
      w.appendChild(r2);
    }
    w.append(err, el('div', 'vj-note', 'Сам звук Spotify зашифрован (DRM) — модуль берёт из Spotify только списки, а треки играют из открытых источников.'));
    return w;
  }
  function dropZone() {
    const z = el('div', 'vj-drop');
    const inp = Object.assign(el('input'), { type: 'file', multiple: true, accept: 'audio/*,video/*', hidden: true });
    const lab = el('label', '', 'выберите файлы'); lab.appendChild(inp);
    const bar = el('div', 'vj-upbar'); bar.hidden = true; bar.appendChild(el('i'));
    z.append(document.createTextNode('Перетащите сюда аудио или видео — или '), lab, bar);
    const up = async (files) => {
      for (const f of files) { bar.hidden = false; try { await app.upload('upload_lib', f, {}, (p) => { bar.firstChild.style.width = Math.round(p * 100) + '%'; }); app.toast('загружено: ' + f.name, 'ok'); } catch (e) { app.toast(f.name + ': ' + e.message, 'error'); } }
      bar.hidden = true; go(viewStack.pop());
    };
    z.addEventListener('dragover', (e) => { e.preventDefault(); z.classList.add('over'); });
    z.addEventListener('dragleave', () => z.classList.remove('over'));
    z.addEventListener('drop', (e) => { e.preventDefault(); z.classList.remove('over'); up([...e.dataTransfer.files]); });
    inp.addEventListener('change', () => up([...inp.files]));
    return z;
  }

  /* ---------------- поиск ---------------- */
  const q = $('.vj-q');
  q.placeholder = A(cur).ph;
  q.addEventListener('input', () => { $('.vj-q-x').hidden = !q.value; });
  $('.vj-q-x').addEventListener('click', () => { q.value = ''; $('.vj-q-x').hidden = true; q.focus(); });
  $('.vj-search').addEventListener('submit', (e) => {
    e.preventDefault();
    const t = q.value.trim(); if (!t) return;
    if (/^https?:\/\//.test(t)) go({ view: 'link', q: t });
    else go({ view: A(cur).nav.some((n) => n[0] === 'search') ? 'search' : 'search', q: t });
  });

  /* ---------------- чат ---------------- */
  const chatSel = $('.vj-chat');
  async function loadChats() {
    const r = await call('chats');
    chatSel.replaceChildren();
    const ph = el('option', '', 'Где играть? — выберите группу или канал'); ph.value = ''; chatSel.appendChild(ph);
    r.chats.forEach((c) => { const o = el('option', '', (c.playing ? '▶ ' : c.call ? '🎙 ' : '') + c.title); o.value = c.id; chatSel.appendChild(o); });
    const other = el('option', '', '+ другой чат (@username или id)…'); other.value = '__other'; chatSel.appendChild(other);
    if (chat && [...chatSel.options].some((o) => o.value === String(chat))) chatSel.value = String(chat);
  }
  chatSel.addEventListener('change', async () => {
    if (chatSel.value === '__other') {
      const t = prompt('Чат: @username, ссылка t.me/… или id');
      if (!t) { chatSel.value = chat ? String(chat) : ''; return; }
      const r = await call('connect', { target: t.replace(/^https?:\/\/t\.me\//, '') });
      chat = r.chat; store.set('chat', chat); await loadChats();
    } else { chat = chatSel.value ? Number(chatSel.value) : null; store.set('chat', chat); }
    tick(true);
  });
  $('.vj-chats-refresh').addEventListener('click', loadChats);
  function pickChatHint() { app.toast('сначала выберите чат вверху — где играть', 'error'); chatSel.focus(); const p = $('.vj-chatpick'); p.animate([{ transform: 'translateX(0)' }, { transform: 'translateX(-6px)' }, { transform: 'translateX(6px)' }, { transform: 'translateX(0)' }], { duration: 300 }); }

  /* ---------------- воспроизведение ---------------- */
  const keyOf = (it) => it ? (it.title + '|' + (it.artist || '')).toLowerCase() : '';
  async function add(items, now, video) {
    if (!chat) { pickChatHint(); return; }
    if (P && now) { P.state = 'loading'; renderPlayer(); }
    const r = await call('add', { chat, items, now, video });
    P = r.player; polled = Date.now(); renderPlayer();
    if (!now) app.toast(items.length > 1 ? `в очереди: ${items.length}` : 'в очереди', 'ok');
  }
  async function control(op, value) {
    if (!chat) { pickChatHint(); return; }
    if (P && op === 'toggle') { P.state = P.state === 'playing' ? 'paused' : 'playing'; renderPlayer(); }
    if (P && ['skip', 'prev', 'jump', 'seek', 'video'].includes(op)) { P.state = 'loading'; renderPlayer(); }
    const r = await call('control', { chat, op, value });
    P = r.player; polled = Date.now(); renderPlayer();
  }
  $$('.vj-ctrls [data-op]').forEach((b) => b.addEventListener('click', () => control(b.dataset.op)));
  const vol = $('.vj-vol input');
  vol.addEventListener('change', () => control('volume', Number(vol.value)));
  $('.vj-vid').addEventListener('click', () => control('video', !(P && P.video)));
  $('.vj-stop').addEventListener('click', () => { if (confirm('Остановить и выйти из голосового чата?')) control('stop'); });
  $('.vj-like').addEventListener('click', async () => { const c = P && P.current; if (!c || c.liked === undefined) return; c.liked = !c.liked; renderPlayer(); await call('ym_like', { id: c.ym, on: c.liked }); });
  const shell = $('.vj-shell'), qbox = $('.vj-queue');
  const setQueue = (on) => { qbox.hidden = !on; shell.classList.toggle('q', on); $('.vj-qbtn').classList.toggle('on', on); store.set('queue', on); };
  $('.vj-qbtn').addEventListener('click', () => setQueue(qbox.hidden));
  $('.vj-qclose').addEventListener('click', () => setQueue(false));
  $('.vj-qclear').addEventListener('click', () => control('clear'));
  setQueue(store.get('queue', false));
  $('.vj-track').addEventListener('click', (e) => {
    const c = P && P.current; if (!c || c.live || !c.duration) return;
    const r = e.currentTarget.getBoundingClientRect();
    control('seek', Math.round(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * c.duration));
  });

  function renderPlayer() {
    const c = P && P.current, st = P ? P.state : 'idle';
    V.classList.toggle('vj-playing', st === 'playing');
    $('.vj-live').classList.toggle('on', !!(P && (st === 'playing' || st === 'paused' || st === 'loading')));
    $('.vj-b-title').textContent = c ? c.title : (st === 'error' ? 'Не запустилось' : 'Ничего не играет');
    $('.vj-b-artist').textContent = c ? [c.artist, c.service].filter(Boolean).join(' · ') : (P && P.error ? P.error : (chat ? 'выберите трек' : 'выберите чат вверху'));
    const cov = $('.vj-b-cover'), u = c && c.thumb ? imgUrl(c.thumb) : '';
    if (cov.dataset.u !== u) { cov.dataset.u = u; cov.replaceChildren(el('span', '', '♫')); if (u) { const i = el('img'); i.alt = ''; i.onload = () => i.classList.add('ok'); i.onerror = () => i.remove(); i.src = u; cov.appendChild(i); } }
    const play = $('.vj-play'); play.textContent = st === 'playing' ? '❚❚' : '▶'; play.classList.toggle('busy', st === 'loading');
    const like = $('.vj-like'); like.hidden = !(c && c.liked !== undefined && c.liked !== null);
    if (c) { like.textContent = c.liked ? '♥' : '♡'; like.classList.toggle('liked', !!c.liked); }
    const lp = P ? P.loop : 'off', lb = $('[data-op="loop"]');
    lb.textContent = lp === 'track' ? '⟳¹' : '⟳'; lb.classList.toggle('on', lp !== 'off'); lb.title = { off: 'без повтора', queue: 'повтор очереди', track: 'повтор трека' }[lp];
    if (document.activeElement !== vol) vol.value = P ? P.volume : 100;
    $('.vj-vid').classList.toggle('on', !!(P && P.video));
    $('.vj-qn').textContent = P && P.queue_total ? String(P.queue_total) : '';
    renderProgress(); renderQueue(); markNow();
  }
  function renderProgress() {
    const c = P && P.current, t = $('.vj-track');
    t.classList.toggle('live', !!(c && c.live)); t.classList.toggle('busy', !!(P && P.state === 'loading'));
    let pos = P ? P.position : 0;
    if (P && P.state === 'playing') pos += (Date.now() - polled) / 1000;
    const total = c ? c.duration : 0;
    if (total) pos = Math.min(pos, total);
    const pct = total ? pos / total * 100 : 0;
    t.querySelector('i').style.width = pct + '%'; t.querySelector('b').style.left = pct + '%';
    $('.vj-pos').textContent = c ? (c.live ? 'эфир' : dur(pos)) : '0:00';
    $('.vj-dur').textContent = c && !c.live ? dur(total) : '';
  }
  function qItem(it, now) {
    const r = el('div', 'vj-qi');
    const m = el('div', 'vj-tr-m'); m.append(el('div', 'vj-tr-t', it.title), el('div', 'vj-tr-a', it.artist || it.service || ''));
    const act = el('div', 'vj-qi-act');
    if (!now) act.append(btn('vj-ibtn', '▶', 'играть сейчас', () => control('jump', it.id)), btn('vj-ibtn', '↑', 'выше', () => control('up', it.id)), btn('vj-ibtn', '✕', 'убрать', () => control('remove', it.id)));
    r.append(pic(it.thumb, 'vj-th'), m, act);
    if (now) m.firstChild.style.color = 'var(--app)';
    return r;
  }
  function renderQueue() {
    const items = P ? P.queue : [];
    $('.vj-qcount').textContent = P && P.queue_total ? String(P.queue_total) : '';
    const now = $('.vj-qnow'), c = P && P.current;
    const nsig = c ? c.id : '';
    if (now.dataset.sig !== nsig) { now.dataset.sig = nsig; now.replaceChildren(...(c ? [el('div', 'vj-qlbl', 'Сейчас играет'), qItem(c, true)] : [])); }
    const list = $('.vj-qlist'), sig = items.map((x) => x.id).join(',');
    if (list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    list.replaceChildren(...(items.length ? [el('div', 'vj-qlbl', 'Далее'), ...items.map((it) => qItem(it, false))] : [el('div', 'vj-empty', 'очередь пуста')]));
  }
  function markNow() {
    const k = P && P.current ? keyOf(P.current) : '';
    $$('.vj-tr, .vj-st, .vj-vid-c').forEach((r) => r.classList.toggle('now', !!k && r.dataset.key === k));
  }

  /* ---------------- сервисы и опрос ---------------- */
  async function loadSvc() { try { svc = await app.call('services'); } catch (e) { svc = null; } drawApps(); drawAcc(); }
  async function tick(force) {
    if (!app.alive() || (busy && !force)) return;
    busy = true;
    try {
      const s = await app.call('state', { chat });
      P = s.player; polled = Date.now();
      const ff = $('.vj-ff');
      if (!s.ff.ok) { ff.hidden = false; ff.textContent = s.ff.error ? 'ffmpeg: ' + s.ff.error : s.ff.progress ? `Первый запуск: скачиваю ffmpeg… ${Math.round(s.ff.progress[0] / 1048576)} из ${Math.round((s.ff.progress[1] || 0) / 1048576)} МБ` : 'Первый запуск: готовлю ffmpeg…'; }
      else ff.hidden = true;
      renderPlayer();
    } catch (e) { /* сайт переподключается */ }
    busy = false;
  }
  document.addEventListener('keydown', (e) => {
    if (!app.alive() || e.target.closest('input, textarea, select')) return;
    if (e.code === 'Space') { e.preventDefault(); control('toggle'); }
    else if (e.key === 'ArrowRight' && e.altKey) control('skip');
    else if (e.key === 'ArrowLeft' && e.altKey) control('prev');
  });
  (function loop() { if (!app.alive()) return; tick(); setTimeout(loop, P && P.state === 'loading' ? 600 : 1500); })();
  (function anim() { if (!app.alive()) return; if (P && P.state === 'playing') renderProgress(); requestAnimationFrame(anim); })();
  V.dataset.app = cur;
  drawApps();
  loadSvc().then(() => openApp(cur));
  loadChats().catch(() => {});
  app.call('prepare').catch(() => {});
});
})();
'''


# =========================================================================== жизненный цикл

_tasks = []


@decorators.on_start()
async def setup():
    await engine.create_config(key='source', description='Где искать по .vplay <текст>: std — каталог Deezer, ym — Яндекс, '
                               'yt — YouTube, sc — SoundCloud', data_type='choice', default='std', options=['std', 'ym', 'yt', 'sc'])
    await engine.create_config(key='audio_quality', description='Качество звука в голосовом чате', data_type='choice',
                               default='high', options=['medium', 'high', 'studio'])
    await engine.create_config(key='video_quality', description='Качество видео (высота)', data_type='choice', default='720',
                               options=['360', '480', '720', '1080'])
    await engine.create_config(key='volume', description='Громкость при входе в чат, %', data_type='int', default=100,
                               valid={'min_value': 1, 'max_value': 200})
    await engine.create_config(key='leave_when_empty', description='Выходить из голосового чата, когда очередь кончилась',
                               data_type='bool', default=False)
    await engine.create_config(key='everyone_controls', description='Кнопки панели в группе может жать кто угодно (иначе — только вы)',
                               data_type='bool', default=False)
    await engine.create_config(key='shuffle_likes', description='Перемешивать лайки при .vlikes', data_type='bool', default=True)
    await engine.create_config(key='library_dirs', description='Папки с музыкой на компьютере (вкладка «Файлы» и поиск)',
                               data_type='list', default=[])
    _tasks.append(asyncio.ensure_future(_ticker()))
    if not _ff_ready():                            # ffmpeg — заранее и тихо, чтобы первый .vplay не ждал
        async def bg():
            try:
                await ensure_ffmpeg()
            except Exception as e:
                engine.tlog(f"[voinje] ffmpeg: {e}")
        _tasks.append(asyncio.ensure_future(bg()))


@decorators.on_unload()
async def cleanup():
    """Выгрузка модуля или выключение FRX: выходим из голосовых чатов (не дольше пары секунд на чат), гасим
    сервер Telegram-медиа и потоки py-tgcalls — иначе процесс мог не завершиться."""
    for t in _tasks:
        t.cancel()
    for p in list(PLAYERS.values()):
        try:
            await asyncio.wait_for(p.stop(), 3)
        except Exception:
            pass
    RT.dispatch = None
    getattr(RT, 'now_by', {}).clear()
    try:
        await asyncio.wait_for(TG.close(), 2)
    except Exception:
        pass
    calls = RT.calls
    if calls is not None:
        try:
            calls.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        RT.calls, RT.client = None, None


# =========================================================================== сайт: вкладка Voinje

IMG_HOSTS = ('avatars.yandex.net', 'ytimg.com', 'ggpht.com', 'dzcdn.net', 'scdn.co', 'spotifycdn.com', 'sndcdn.com',
             'googleusercontent.com', 'mzstatic.com', 'vkuserphoto.ru', 'userapi.com')
_img_cache = {}


async def _img(url):
    # обложки и логотипы радиостанций с любых https-адресов (сайт FRX сам не грузит чужие картинки); только картинки до 3 МБ
    host = (urllib.parse.urlparse(url).hostname or '').lower()
    if not url.startswith('https://') or not host or host in ('localhost',) or re.fullmatch(r'[\d.:]+', host):
        return None
    if url in _img_cache:
        return _img_cache[url]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url, headers=UA) as r:
            ctype = r.headers.get('Content-Type', '')
            if r.status != 200 or not ctype.startswith('image/'):
                return None
            body = await r.read()
    if len(body) > 3 * 1024 * 1024:
        return None
    if len(_img_cache) > 300:
        _img_cache.clear()
    _img_cache[url] = (body, ctype.split(';')[0])
    return _img_cache[url]


def _clean_items(items):
    out = []
    for x in items or []:
        if isinstance(x, dict) and isinstance(x.get('src'), dict) and x['src'].get('t') in ('ydl', 'url', 'tg', 'file', 'ym', 'match', 'zv'):
            out.append(dict(item(x.get('title'), x.get('artist'), x.get('duration'), x['src'], x.get('thumb'), x.get('service'),
                                 x.get('live'), x.get('video')), wave=x.get('wave')))
    return out


async def _chats():
    from telethon.tl.types import Channel, Chat
    out = []
    async for d in engine.client.iter_dialogs(limit=150):
        e = d.entity
        if isinstance(e, (Channel, Chat)) and not getattr(e, 'left', False):
            if isinstance(e, Channel) and e.broadcast and not (e.creator or e.admin_rights):
                continue
            out.append({'id': d.id, 'title': d.name, 'call': bool(getattr(e, 'call_active', False)),
                        'playing': d.id in PLAYERS})
    out.sort(key=lambda c: (not c['playing'], not c['call']))
    return out


@web.app("Voinje", icon="♫", html=WEB_HTML, css=WEB_CSS, js=WEB_JS, width='full')
async def voinje_web(req):
    a, d = req.action, req.data
    chat = d.get('chat')
    chat = int(chat) if isinstance(chat, (int, str)) and str(chat).lstrip('-').isdigit() else None

    if a == 'res':
        if req.data.get('name') == 'img':
            got = await _img(req.query.get('u', ''))
            return got or (b'', 'image/png')
        return None

    if a == 'state':
        p = PLAYERS.get(chat) if chat else None
        if p is not None and p.state == 'playing':
            await p.sync_position()
        return {'players': [{'chat': x.chat_id, 'title': x.title, 'state': x.state,
                             'now': label(x.current) if x.current else ''} for x in PLAYERS.values()],
                'player': p.snapshot() if p else None,
                'ff': {'ok': _ff_ready(), 'progress': _ff_state.get('progress'), 'error': _ff_state.get('error')},
                'calls': RT.calls is not None}
    if a == 'chats':
        return {'chats': await _chats()}
    if a == 'connect':
        target = str(d.get('target') or '').strip()
        ent = await engine.client.get_entity(int(target) if target.lstrip('-').isdigit() else target)
        from telethon.utils import get_peer_id
        cid = get_peer_id(ent)
        await player(cid, getattr(ent, 'title', None))
        return {'chat': cid}
    if a == 'prepare':
        if not _ff_ready():
            asyncio.ensure_future(ensure_ffmpeg())
        else:
            await get_calls()
        return {'ok': True}

    # ---- приложения: подборки, альбомы, плейлисты, поиск
    if a == 'browse':
        view = await browse(str(d.get('app') or 'std'), str(d.get('view') or 'home'), d.get('id'), str(d.get('q') or ''))
        return {'view': view}
    if a == 'ym_like':
        client = await ym_client(anon_ok=False)
        tid = str(d.get('id') or '')
        if d.get('on'):
            await client.users_likes_tracks_add(tid)
        else:
            await client.users_likes_tracks_remove(tid)
        liked = await _ym_liked_ids()
        (liked.add if d.get('on') else liked.discard)(tid.split(':')[0])
        return {'ok': True}

    # ---- поиск и подборки
    if a == 'search':
        return {'items': await search(d.get('source') or 'std', str(d.get('q') or '').strip()[:200], int(d.get('n') or 20))}
    if a == 'open':
        return {'items': await from_url(str(d.get('url') or '').strip())}
    if a == 'collection':
        kind = d.get('kind')
        fn = {'ym_likes': ym_likes, 'sp_likes': sp_likes, 'sp_now': sp_now, 'saved': lambda: search_saved('', 100),
              'files': lambda: search_files('', 500), 'ym_wave': lambda: ym_wave('user:onyourwave')}.get(kind)
        if fn is not None:
            return {'items': await fn()}
        if kind == 'ym_playlists':
            return {'lists': await ym_playlists()}
        if kind == 'sp_playlists':
            return {'lists': await sp_playlists()}
        if kind == 'ym_playlist':
            return {'items': await ym_playlist(str(d.get('id')))}
        if kind == 'sp_playlist':
            return {'items': await sp_playlist(str(d.get('id')))}
        raise RuntimeError("неизвестная подборка")

    # ---- управление
    if a in ('add', 'control', 'wave'):
        if chat is None:
            raise RuntimeError("выберите чат")
        await ensure_ffmpeg()
        p = await player(chat)
        if a == 'add':
            items = _clean_items(d.get('items'))
            if d.get('video'):
                for x in items:
                    x['video'] = True
            p.run(p.add(items, now=bool(d.get('now'))))          # запуск — в фоне: ответ сайту сразу
            await asyncio.sleep(0.05)
            return {'added': len(items), 'player': p.snapshot()}
        if a == 'wave':
            p.wave = str(d.get('station') or 'user:onyourwave')

            async def go():
                await p.add(await ym_wave(p.wave), now=True)
            p.run(go())
            return {'player': p.snapshot()}
        op, v = d.get('op'), d.get('value')
        if op == 'toggle':
            await p.toggle()
        elif op == 'skip':
            p.run(p.skip())
        elif op == 'prev':
            p.run(p.prev())
        elif op == 'stop':
            await p.stop()
            return {'player': None}
        elif op == 'seek':
            p.run(p.seek(int(v or 0)))
        elif op == 'volume':
            await p.set_volume(int(v or 100))
        elif op == 'loop':
            p.cycle_loop()
        elif op == 'shuffle':
            p.shuffle()
        elif op == 'video':
            p.run(p.set_video(bool(v)))
        elif op == 'remove':
            p.remove(str(v))
        elif op == 'up':
            p.move(str(v), -1)
        elif op == 'down':
            p.move(str(v), 1)
        elif op == 'jump':
            p.run(p.jump(str(v)))
        elif op == 'clear':
            p.queue.clear()
            p.wave = None
            p.changed()
        return {'player': p.snapshot()}

    # ---- сервисы
    if a == 'services':
        return await services_state()
    if a == 'ym_code':
        dev = await ym_device_start()
        return {'code': dev['user_code'], 'url': dev['url']}
    if a == 'ym_poll':
        return {'ok': bool(await ym_device_poll())}
    if a == 'ym_token':
        return {'login': await ym_set_token(str(d.get('token') or ''))}
    if a == 'ym_out':
        await ym_set_token('')
        return {'ok': True}
    if a == 'sp_url':
        return {'url': await sp_login_url(str(d.get('client_id') or ''))}
    if a == 'sp_finish':
        return {'name': await sp_finish(str(d.get('url') or ''))}
    if a == 'sp_out':
        await dset('sp_auth', None)
        await dset('sp_pending', None)
        return {'ok': True}
    if a == 'zv_token':
        return {'name': await zv_set_token(str(d.get('token') or ''))}
    if a == 'zv_browser':
        return {'name': await zv_from_browser(str(d.get('browser') or ''))}
    if a == 'zv_out':
        await zv_set_token('')
        await dset('zv_name', None)
        return {'ok': True}
    if a == 'yt_browser':
        n = await yt_from_browser(str(d.get('browser') or ''))
        try:
            who = await yt_account()
        except Exception:
            who = None
        return {'cookies': n, 'account': who}
    if a == 'yt_out':
        yt_cookies_drop()
        await dset('yt_browser', None)
        return {'ok': True}

    # ---- файлы
    if a == 'upload_cookies':
        await yt_cookies_set(req.file)
        return {'ok': True}
    if a == 'upload_lib':
        name = re.sub(r'[\\/:*?"<>|]+', '_', os.path.basename(req.filename or 'track'))[:150]
        if not name.lower().endswith(AUDIO_EXT):
            raise RuntimeError("это не аудио/видео файл")
        dest = library_dir() / name
        n = 1
        while dest.exists():
            dest = library_dir() / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        shutil.copyfile(req.file, dest)
        return {'ok': True, 'name': dest.name}
    if a == 'lib_delete':
        path = Path(str(d.get('path') or '')).resolve()
        if library_dir().resolve() not in path.parents:
            raise RuntimeError("удалять можно только файлы из библиотеки Voinje")
        path.unlink(missing_ok=True)
        return {'ok': True}
    raise RuntimeError(f"неизвестное действие {a}")
