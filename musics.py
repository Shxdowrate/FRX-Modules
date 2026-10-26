"""
Musics — поиск и скачивание музыки.

  .music <запрос>      поиск: «исполнитель - трек», «трек исполнитель» или как угодно. Каталог Deezer (десятки миллионов
                       треков), а чего нет в каталоге — YouTube и SoundCloud (малоизвестное, каверы, ремиксы).
                       Выбрали трек — модуль присылает аудио с обложкой и тегами
  .music <ссылка>      ссылка на трек Spotify, Яндекс Музыки, Apple Music, Deezer, YouTube, SoundCloud, VK — то же самое
  .lyrics <запрос>     текст песни (lrclib.net); ответом на трек — текст этого трека

Как это работает без аккаунтов: каталог и обложки — открытый API Deezer, по ссылкам Spotify/Яндекса/Apple модуль берёт
из страницы «исполнитель — название». Звук, по порядку:
  1. музыкальные инлайн-боты Telegram (настройка bots: @lybot — только уже скачанные у него треки, @deezermusicbot) —
     все разом, несколько формулировок запроса; файл уже лежит в Telegram и отправляется за 1–3 секунды;
  2. yt-dlp из открытых источников: YouTube и SoundCloud по очереди, лучшие по названию и длительности. Если YouTube
     просит «подтвердите, что вы не бот» (бывает с IP VPN/хостинга) — модуль на 30 минут обходит YouTube стороной,
     берёт SoundCloud и разово обновляет yt-dlp. m4a — без перекодирования (быстро); mp3 — по настройке format.
Ошибки видны в карточке трека и в журнале FRX (строки [musics]). Скачивание музыки — на ответственности пользователя:
соблюдайте авторские права в своей стране.
"""

import asyncio
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse

import aiohttp

import decorators
import engine
import visual

NAME = "Musics"
MODULE_NAME = "musics"
VERSION = "1.2"
AUTHOR = "FRX"
DESCRIPTION = "Музыка: поиск по каталогу Deezer и ссылкам Spotify/Яндекс/Apple — mp3 с обложкой; тексты песен"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/musics.py"
REQUIREMENTS = [("yt-dlp[default,deno]", "yt_dlp, deno"),
                "cryptg; sys_platform != 'android'"]   # cryptg: шифрование Telethon в C — выгрузка файлов в разы быстрее   # ставятся сами (core/requirements.py)

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
      'Accept-Language': 'ru,en;q=0.8'}
URL_RE = re.compile(r'https?://\S+')
_install_lock = asyncio.Lock()


def _esc(x):
    return html.escape(str(x), quote=False)


def _log_exc(where, e):
    """Ошибки модуля — в журнал FRX (print уходит в frx_full.log), а не в пустоту."""
    import traceback
    print(f"[musics] {where}: {type(e).__name__}: {e}\n" + ''.join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:])


def _err_text(e):
    msg = re.sub(r'\x1b\[[0-9;]*m', '', str(e)).replace('ERROR:', '').strip()
    if isinstance(e, RuntimeError) and msg:
        return msg[:300]
    return (f"{type(e).__name__}: {msg}" if msg else type(e).__name__)[:300]


@decorators.on_start()
async def setup():
    await engine.create_config(key='fast_bots', description='Сначала искать трек у музыкальных инлайн-ботов Telegram '
                               '(секунды вместо скачивания)', data_type='bool', default=True)
    await engine.create_config(key='bots', description='Музыкальные инлайн-боты для быстрого пути (по порядку)',
                               data_type='list', default=['lybot', 'deezermusicbot'])
    try:                                                    # старый список по умолчанию → добавить второго бота (один раз)
        if not await engine.db.get('musics:bots_v2') and (await engine.get_config('bots')) == ['lybot']:
            await engine.edit_config('bots', 'add', 'deezermusicbot')
        await engine.db.set('musics:bots_v2', True)
    except Exception as e:
        _log_exc('миграция bots', e)
    await engine.create_config(key='format', description='Формат при скачивании: m4a — без перекодирования (быстрее), '
                               'mp3 — перекодирование (дольше, совместимее)', data_type='choice', default='m4a',
                               options=['m4a', 'mp3'])
    await engine.create_config(key='bitrate', description='Качество mp3, кбит/с (только для format=mp3)', data_type='choice',
                               default='192', options=['128', '192', '256', '320'])
    await engine.create_config(key='results', description='Сколько треков показывать в поиске',
                               data_type='int', default=8, valid={'min_value': 3, 'max_value': 15})


# --------------------------------------------------------------------------- сеть

async def _get(url, as_json=True, params=None):
    async with aiohttp.ClientSession(headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(url, params=params) as r:
            r.raise_for_status()
            return await (r.json(content_type=None) if as_json else r.text())


async def deezer_search(q, n=8):
    data = await _get('https://api.deezer.com/search', params={'q': q, 'limit': n})
    return [_track(t) for t in data.get('data', []) if t.get('type') == 'track']


SEP_RE = re.compile(r'\s+[-—–]+\s+|\s*[—–]\s*|\s+-|-\s+')
NOISE_RE = re.compile(r'\((?:official|lyric|lyrics|audio|video|music video|клип|текст|премьера)[^)]*\)|\[[^\]]*\]|'
                      r'\b(?:official (?:music )?video|official audio|lyrics?|hd|hq|4k)\b', re.I)


def split_query(q):
    """«исполнитель - трек» (любые тире, с пробелами и без) → (исполнитель, трек) | None."""
    parts = [p.strip() for p in SEP_RE.split(q, 1)]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return None


def _norm(x):
    return ' '.join(re.findall(r'\w+', NOISE_RE.sub(' ', (x or '').lower().replace('ё', 'е'))))


def _key(t):
    return (_norm(t['artist']), _norm(re.sub(r'\s*\((?:feat|ft)\.?[^)]*\)', '', t['title'], flags=re.I)))


async def yt_candidates(q, n=5):
    """YouTube/SoundCloud поиск (для того, чего нет в Deezer) — в формате трека, с готовым source."""
    y = await ytdlp()

    def run(query):
        with y.YoutubeDL({**_js(), 'quiet': True, 'no_warnings': True, 'extract_flat': True}) as ydl:
            return ydl.extract_info(query, download=False).get('entries') or []
    loop = asyncio.get_running_loop()
    kinds = [('SoundCloud', f"scsearch4:{q}")]
    if time.monotonic() >= _yt_blocked['until']:           # YouTube не требует вход — ищем и там
        kinds.insert(0, ('YouTube', f"ytsearch{n}:{q}"))
    got = await asyncio.gather(*(loop.run_in_executor(None, run, x) for _k, x in kinds), return_exceptions=True)
    out = []
    for (kind, _q), entries in zip(kinds, got):
        if isinstance(entries, Exception):
            continue
        for e in entries:
            dur = int(e.get('duration') or 0)
            if dur and (dur < 40 or dur > 1200):          # не песня: шортсы, сборники на час
                continue
            title = NOISE_RE.sub('', e.get('title') or '').strip(' -|')
            artist = re.sub(r'\s*-\s*Topic$', '', e.get('channel') or e.get('uploader') or '').strip()
            sp = split_query(title)
            if sp:
                artist, title = sp
            url = e.get('url') or e.get('webpage_url') or ''
            if kind == 'YouTube' and not url.startswith('http'):
                url = f"https://www.youtube.com/watch?v={e.get('id')}"
            thumbs = e.get('thumbnails') or []
            cover = (thumbs[-1].get('url') if thumbs else '') or ''
            out.append({'title': title, 'artist': artist, 'album': '', 'cover': cover, 'cover_small': cover,
                        'duration': dur, 'link': url, 'explicit': False, 'source': url, 'from': kind})
    return out


async def smart_search(q, n=8):
    """Поиск «как в голове»: «исполнитель - трек», «трек - исполнитель», просто слова. Deezer: точный поиск по
    полям + обычный; мало нашлось — добавляем YouTube и SoundCloud (малоизвестное, каверы, ремиксы, демо)."""
    jobs = [deezer_search(q, n)]
    sp = split_query(q)
    if sp:
        a, t = sp
        jobs.insert(0, deezer_search(f'artist:"{a}" track:"{t}"', n))
        jobs.insert(1, deezer_search(f'artist:"{t}" track:"{a}"', 3))       # вдруг написали «трек - исполнитель»
    got = await asyncio.gather(*jobs, return_exceptions=True)
    seen, out = set(), []
    for res in got:
        if isinstance(res, Exception):
            continue
        for t in res:
            k = _key(t)
            if k not in seen:
                seen.add(k)
                out.append(t)
    words = set(_norm(q.replace('-', ' ')).split())
    exact = [t for t in out if words and words <= set(_norm(f"{t['artist']} {t['title']}").split())]
    if len(exact) < 2:
        try:
            for t in await yt_candidates(q, 5):
                k = _key(t)
                if k not in seen:
                    seen.add(k)
                    out.append(t)
        except Exception:
            pass
    def rank(t):
        name = set(_norm(f"{t['artist']} {t['title']}").split())
        exact = bool(words) and words <= name
        d = t.get('duration') or 0
        odd = bool(d) and not 60 <= d <= 480              # 10-минутные «EP / премьеры / сборники» — ниже песен
        extra = len(name - words)                         # «… (Официальная премьера EP)» — ниже чистого названия
        return (not exact, odd, bool(t.get('from')), extra if exact else 0)
    out.sort(key=rank)                                    # точные совпадения → песни обычной длины → каталог Deezer
    return out[:max(n, 10)]


def _track(t):
    return {'title': t.get('title') or '', 'artist': (t.get('artist') or {}).get('name') or '',
            'album': (t.get('album') or {}).get('title') or '', 'cover': (t.get('album') or {}).get('cover_big') or '',
            'cover_small': (t.get('album') or {}).get('cover_medium') or '', 'duration': int(t.get('duration') or 0),
            'link': t.get('link') or '', 'explicit': bool(t.get('explicit_lyrics'))}


async def meta_from_link(url):
    """Ссылка Spotify / Яндекс / Apple / Deezer → «исполнитель — название» (из страницы, без аккаунта)."""
    host = urllib.parse.urlparse(url).hostname or ''
    if 'deezer.' in host:
        m = re.search(r'/track/(\d+)', url)
        if m:
            return _track(await _get(f'https://api.deezer.com/track/{m.group(1)}'))
    page = await _get(url, as_json=False)

    def meta(prop):
        m = re.search(rf'<meta[^>]+(?:property|name)="{prop}"[^>]+content="([^"]*)"', page) or \
            re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{prop}"', page)
        return html.unescape(m.group(1)).strip() if m else ''
    title, desc = meta('og:title'), meta('og:description')
    page_title = html.unescape((re.search(r'<title>([^<]*)</title>', page) or [None, ''])[1]).strip()
    artist = ''
    if 'spotify.' in host:
        artist = meta('music:musician_description') or (desc.split('·')[0].strip() if '·' in desc else '')
        m = re.match(r'(.+?) - (?:song|песня).* by (.+?) \|', page_title, re.I)
        if m:
            title, artist = m.group(1), m.group(2)
    elif 'yandex.' in host:
        m = re.match(r'(.+?)\s+[—-]\s+(.+?)\.\s', page_title) or re.match(r'(.+?)\s+[—-]\s+(.+)', title)
        if m:
            artist, title = m.group(1), m.group(2)
    elif 'apple.' in host:
        m = re.match(r'(.+?) by (.+?) on Apple Music', page_title) or re.match(r'(.+?) — (.+)', title)
        if m:
            title, artist = m.group(1), m.group(2)
    if not title:
        title = page_title
    title = re.sub(r'\s*\|.*$', '', title).strip()
    found = await deezer_search(f"{artist} {title}".strip(), 1)        # обложка, альбом и длительность — из каталога
    if found:
        return found[0]
    return {'title': title, 'artist': artist, 'album': '', 'cover': meta('og:image'), 'cover_small': meta('og:image'),
            'duration': 0, 'link': url, 'explicit': False}


# --------------------------------------------------------------------------- yt-dlp


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
            proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp[default,deno]', '--disable-pip-version-check',
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
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


async def find_sources(track, exclude=()):
    """Полная версия трека: YouTube и SoundCloud → ссылки по убыванию похожести (длительность, название)."""
    y = await ytdlp()
    q = f"{track['artist']} - {track['title']}".strip(' -')

    def run(query):
        with y.YoutubeDL({**_js(), 'quiet': True, 'no_warnings': True, 'extract_flat': True}) as ydl:
            return ydl.extract_info(query, download=False).get('entries') or []
    loop = asyncio.get_running_loop()
    cands = []
    queries = [f"scsearch4:{q}"] + ([f"ytsearch5:{q}"] if time.monotonic() >= _yt_blocked['until'] else [])
    got = await asyncio.gather(*(loop.run_in_executor(None, run, x) for x in queries), return_exceptions=True)
    for res in got:
        if isinstance(res, Exception):
            print(f"[musics] поиск источника «{q}»: {_err_text(res)}")
        else:
            cands += res
    want = track.get('duration') or 0
    words = set(re.findall(r'\w+', q.lower()))

    def score(e):
        t = (e.get('title') or '').lower()
        s = len(words & set(re.findall(r'\w+', t))) * 3
        d = e.get('duration') or 0
        if want and d:
            s -= min(abs(d - want), 120) / 6
        for bad in ('live', 'cover', 'remix', 'karaoke', 'instrumental', 'slowed', 'sped up', 'nightcore', '8d'):
            if bad in t and bad not in q.lower():
                s -= 6
        if 'official' in t or 'topic' in (e.get('channel') or '').lower():
            s += 2
        if d and (d < 40 or d > 1200):
            s -= 20
        return s
    out = []
    for e in sorted(cands, key=score, reverse=True):
        url = e.get('url') or e.get('webpage_url') or (f"https://www.youtube.com/watch?v={e.get('id')}" if e.get('id') else '')
        if url and url not in out and url not in exclude:
            out.append(url)
    print(f"[musics] источники «{q}»: {len(out)} (из {len(cands)})")
    return out


async def via_bots(track):
    """Быстрый путь: трек у музыкального инлайн-бота (файл уже в Telegram) → ('doc', Document) | None.
    Боты ищут по-разному: на «исполнитель трек» @lybot часто отдаёт просто популярное у исполнителя, поэтому
    пробуем несколько формулировок и проверяем каждый результат по словам и длительности."""
    if not await engine.get_config('fast_bots'):
        return None
    from telethon.tl.types import DocumentAttributeAudio
    artist, title = (track.get('artist') or '').strip(), (track.get('title') or '').strip()
    clean_title = re.sub(r'\s*[(\[][^)\]]*[)\]]', '', title).strip() or title
    queries = list(dict.fromkeys(x for x in (f"{artist} {title}".strip(), f"{clean_title} {artist}".strip(), clean_title) if x))
    want = track.get('duration') or 0
    words = set(_norm(f"{artist} {clean_title}").split())
    async def one_bot(b):
        best, best_score, seen = None, -1e9, []
        for q in queries:
            t1 = time.monotonic()
            try:
                results = await asyncio.wait_for(engine.client.inline_query(b, q), 6)
            except Exception as e:
                print(f"[musics] @{b}: запрос «{q}» не прошёл — {_err_text(e)}")
                break                                        # бот недоступен — к следующему боту
            for r in list(results)[:50]:
                doc = r.document
                if doc is None:   # «статья» без файла (у @lybot — ещё не скачанный трек, его бот отдаёт только через /proxy)
                    continue
                a = next((x for x in doc.attributes if isinstance(x, DocumentAttributeAudio)), None)
                if a is None or a.voice:
                    continue
                got = set(_norm(f"{a.performer or ''} {a.title or ''} {r.title or ''} {r.description or ''}").split())
                sc = len(words & got) * 3 - (len(words - got) * 4)
                if want and a.duration:
                    sc -= min(abs(a.duration - want), 120) / 5
                seen.append((sc, f"{a.performer} — {a.title} ({a.duration} с)"))
                if sc > best_score:
                    best, best_score = ('doc', doc), sc
            top = '; '.join(f"{x[1]} [{x[0]:.0f}]" for x in sorted(seen, reverse=True)[:2])
            print(f"[musics] @{b}: «{q}» ({want} с) — результатов {len(results)}, лучший {best_score:.1f}, "
                  f"{time.monotonic() - t1:.1f} с · {top}")
            if best is not None and best_score >= -4:
                return best
            if not results:                                  # пусто — у этого бота трека нет, другие формулировки не спасут
                break
        return None

    bots = [x for x in (str(b).strip().lstrip('@') for b in (await engine.get_config('bots') or [])) if x]
    got = await asyncio.gather(*(one_bot(b) for b in bots), return_exceptions=True)   # все боты разом
    return next((g for g in got if g and not isinstance(g, Exception)), None)         # первый по порядку в настройке


async def download_mp3(src_url, track, on_progress=None):
    y = await ytdlp()
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("нет ffmpeg — pip install imageio-ffmpeg")
    tmp = tempfile.mkdtemp(prefix='frx_music_')
    loop = asyncio.get_running_loop()
    st = {'t': 0.0}

    def hook(d):
        if on_progress and d.get('status') == 'downloading' and time.monotonic() - st['t'] > 1.5:
            st['t'] = time.monotonic()
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            done = d.get('downloaded_bytes') or 0
            asyncio.run_coroutine_threadsafe(on_progress(done / total * 100 if total else 0, done, total,
                                                         d.get('speed') or 0), loop)
    fmt = (await engine.get_config('format')) or 'm4a'
    pp = {'key': 'FFmpegExtractAudio', 'preferredcodec': fmt}
    if fmt == 'mp3':
        pp['preferredquality'] = str(await engine.get_config('bitrate'))
    opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'ffmpeg_location': ff,
            # m4a: YouTube отдаёт AAC-дорожку как есть — ffmpeg её только перекладывает, без перекодирования
            'format': 'bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best' if fmt == 'm4a' else 'bestaudio/best',
            'outtmpl': os.path.join(tmp, 'track.%(ext)s'), 'progress_hooks': [hook],
            'http_chunk_size': 10 * 1024 * 1024,           # кусками по 10 МБ — YouTube не душит скорость
            'concurrent_fragment_downloads': 4, 'socket_timeout': 15, 'retries': 3,
            'postprocessors': [pp]}

    def run():
        with y.YoutubeDL({**_js(), **opts}) as ydl:
            return ydl.extract_info(src_url, download=True)
    info = await loop.run_in_executor(None, run)
    path = next((os.path.join(tmp, n) for n in os.listdir(tmp) if n.endswith('.' + fmt)), None) or \
        next((os.path.join(tmp, n) for n in os.listdir(tmp) if n.startswith('track.')), None)
    if not path:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("не удалось получить аудио")
    if not track.get('duration'):
        track['duration'] = int(info.get('duration') or 0)
    if not track.get('title'):
        track['title'] = info.get('track') or info.get('title') or ''
        track['artist'] = track.get('artist') or info.get('artist') or info.get('uploader') or ''
        track['cover_small'] = track.get('cover_small') or info.get('thumbnail') or ''
    cover = None
    if track.get('cover_small'):
        try:
            async with aiohttp.ClientSession(headers=UA) as s:
                async with s.get(track['cover_small']) as r:
                    if r.status == 200:
                        cover = os.path.join(tmp, 'cover.jpg')
                        with open(cover, 'wb') as f:
                            f.write(await r.read())
        except Exception:
            cover = None
    await loop.run_in_executor(None, _tag, ff, path, track, cover)
    return path, cover, tmp


def _tag(ff, path, track, cover):
    """Теги ID3 и обложка внутрь mp3 (ffmpeg)."""
    import subprocess
    base, ext = os.path.splitext(path)
    out = base + '_t' + ext
    cmd = [ff, '-y', '-loglevel', 'error', '-i', path]
    if cover and ext == '.mp3':
        cmd += ['-i', cover, '-map', '0:a', '-map', '1:v', '-c:v', 'mjpeg', '-disposition:v', 'attached_pic',
                '-metadata:s:v', 'title=Album cover']
    else:
        cmd += ['-map', '0:a']
    cmd += ['-c:a', 'copy'] + (['-id3v2_version', '3'] if ext == '.mp3' else []) + ['-metadata', f"title={track['title']}", '-metadata', f"artist={track['artist']}",
            '-metadata', f"album={track.get('album') or ''}", out]
    try:
        subprocess.run(cmd, check=True, timeout=60, creationflags=0x08000000 if os.name == 'nt' else 0)
        os.replace(out, path)
    except Exception:
        pass


def _mb(n):
    return f"{n / 1048576:.1f} МБ"


_yt_blocked = {'until': 0.0, 'upgraded': False}
BOT_CHECK_RE = re.compile(r"not a bot|Sign in to confirm|HTTP Error 429|cookies", re.I)


async def _upgrade_ytdlp():
    """YouTube меняется часто — свежий yt-dlp чинит большинство «внезапно перестало качать». Один раз за запуск."""
    if _yt_blocked['upgraded']:
        return
    _yt_blocked['upgraded'] = True
    proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'pip', 'install', '-U', '--pre', 'yt-dlp[default,deno]',
                                                '--disable-pip-version-check', stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    print(f"[musics] обновление yt-dlp: код {proc.returncode} · " + out.decode('utf-8', 'replace').strip().splitlines()[-1][:200]
          if out else f"[musics] обновление yt-dlp: код {proc.returncode}")


def _is_yt(url):
    return any(h in (urllib.parse.urlparse(url).hostname or '') for h in ('youtube.', 'youtu.be'))


async def _send_doc(chat_id, found, reply_to):
    kind, obj = found
    try:
        msg = await engine.client.send_file(chat_id, obj, reply_to=reply_to)
        print(f"[musics] отправлен файл бота → чат {chat_id}, сообщение {getattr(msg, 'id', None)}")
        return msg
    except Exception as e:                                  # файл бота протух/недоступен — качаем сами
        _log_exc('отправка файла бота', e)
        return None


async def send_track(chat_id, track, show, reply_to=None):
    """Трек в чат. Порядок: музыкальные инлайн-боты (секунды) → источник из поиска → другие источники
    YouTube/SoundCloud по очереди. YouTube просит «докажи, что не бот» — пропускаем его и берём SoundCloud."""
    from telethon.tl.types import DocumentAttributeAudio
    t0 = time.monotonic()
    if track.get('title'):                                  # быстрый путь — и для находок YouTube/SoundCloud тоже
        await show(visual.wait("Ищу трек…"))
        try:
            doc = await via_bots(track)
        except Exception as e:
            _log_exc('инлайн-боты', e)
            doc = None
        msg = await _send_doc(chat_id, doc, reply_to) if doc is not None else None
        if msg is not None:
            await show(visual.ok("Готово") + f"  {_esc(track['artist'])} — {_esc(track['title'])}  "
                       + visual.hint(f"{time.monotonic() - t0:.1f} с"))
            return msg

    async def prog(pct, done=0, total=0, speed=0):
        extra = f"{_mb(done)} / {_mb(total)}" if total else _mb(done)
        if speed:
            extra += f" · {speed / 1048576:.1f} МБ/с"
        await show(visual.wait("Скачиваю…") + f"\n<code>{engine.progress_bar(pct, 12, 'bar')}</code> {pct:.0f}%  {extra}")

    tried, errors, searched = [], [], False
    queue = [track['source']] if track.get('source') else []
    result = None
    for _round in range(6):
        if not queue:
            if searched or not track.get('title'):
                break
            searched = True
            await show(visual.wait("Ищу полную версию…"))
            queue = await find_sources(track, exclude=tried)
            if not queue:
                break
        src = queue.pop(0)
        if _is_yt(src) and time.monotonic() < _yt_blocked['until']:
            continue                                        # YouTube сейчас требует вход — сразу к SoundCloud
        tried.append(src)
        t1 = time.monotonic()
        try:
            result = await download_mp3(src, track, prog)
            print(f"[musics] скачано {_mb(os.path.getsize(result[0]))} за {time.monotonic() - t1:.0f} с · {src}")
            break
        except Exception as e:
            errors.append(e)
            _log_exc(f'скачивание {src}', e)
            if _is_yt(src) and BOT_CHECK_RE.search(str(e)):
                _yt_blocked['until'] = time.monotonic() + 1800
                asyncio.ensure_future(_upgrade_ytdlp())
                await show(visual.wait("YouTube не отдаёт трек — пробую SoundCloud…"))
                queue = [u for u in queue if not _is_yt(u)]
    if result is None:
        last = errors[-1] if errors else None
        if any(BOT_CHECK_RE.search(str(x)) for x in errors) or (not errors and time.monotonic() < _yt_blocked['until']):
            raise RuntimeError("YouTube требует вход («подтвердите, что вы не бот») — так бывает с IP VPN/хостинга; "
                               "SoundCloud и инлайн-боты трек не нашли. Попробуйте позже, без VPN, или другую версию из списка")
        if last is not None:
            raise RuntimeError(re.sub(r'\x1b\[[0-9;]*m', '', str(last)).replace('ERROR:', '').strip()[:250])
        raise RuntimeError("трек не нашёлся ни у ботов, ни в открытых источниках")
    path, cover, tmp = result
    try:
        size = os.path.getsize(path)
        await show(visual.wait(f"Отправляю · {_mb(size)}…"))
        up = {'t': time.monotonic()}

        async def uprog(done, total):
            if time.monotonic() - up['t'] > 2:
                up['t'] = time.monotonic()
                pct = done / total * 100 if total else 0
                await show(visual.wait("Отправляю…") + f"\n<code>{engine.progress_bar(pct, 12, 'bar')}</code> {pct:.0f}%  {_mb(done)} / {_mb(total)}")
        name = f"{track['artist']} - {track['title']}".strip(' -')
        safe = re.sub(r'[\\/:*?"<>|]+', '', name)[:120] + os.path.splitext(path)[1]
        final = os.path.join(tmp, safe)
        os.replace(path, final)
        msg = await engine.client.send_file(
            chat_id, final, reply_to=reply_to, thumb=cover, progress_callback=uprog,
            attributes=[DocumentAttributeAudio(duration=int(track.get('duration') or 0), title=track['title'][:64], performer=track['artist'][:64])])
        print(f"[musics] скачано: {name} → чат {chat_id}, сообщение {getattr(msg, 'id', None)}, {time.monotonic() - t0:.0f} с")
        await show(visual.ok("Готово") + f"  {_esc(name)}  " + visual.hint(f"{time.monotonic() - t0:.0f} с"))
        return msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _dur(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02}"


# --------------------------------------------------------------------------- тексты песен

async def lyrics(artist, title):
    try:
        data = await _get('https://lrclib.net/api/search', params={'track_name': title, 'artist_name': artist})
    except Exception:
        data = []
    if not data:
        try:
            data = await _get('https://lrclib.net/api/search', params={'q': f"{artist} {title}"})
        except Exception:
            data = []
    for d in data or []:
        if d.get('plainLyrics'):
            return d['plainLyrics']
    return None


# --------------------------------------------------------------------------- команды

@decorators.command('music', 'mus')
async def music_cmd(event):
    """музыка: <запрос> или <ссылка Spotify / Яндекс / Apple / Deezer / YouTube / SoundCloud>"""
    q = engine.get_args().strip()
    if not q and event.is_reply:
        r = await event.get_reply_message()
        q = (r.raw_text or '').strip() if r else ''
    if not q:
        await engine.answer(event, visual.err("Что ищем?") + "\n" + visual.hint(".music исполнитель трек — или ссылка"))
        return

    async def show(text):
        try:
            await engine.answer(event, text)
        except Exception:
            pass
    m = URL_RE.search(q)
    if m:
        url = m.group(0)
        host = urllib.parse.urlparse(url).hostname or ''
        try:
            if any(h in host for h in ('youtube.', 'youtu.be', 'soundcloud.', 'vk.com', 'vk.ru')):
                await show(visual.wait("Скачиваю…"))
                await send_track(event.chat_id, {'title': '', 'artist': '', 'duration': 0, 'source': url}, show,
                                 reply_to=event.reply_to_msg_id)
                return
            await show(visual.wait("Читаю ссылку…"))
            track = await meta_from_link(url)
            await send_track(event.chat_id, track, show, reply_to=event.reply_to_msg_id)
        except Exception as e:
            await show(visual.err("Не получилось") + f"\n<code>{_esc(str(e)[:300])}</code>")
        return

    await show(visual.wait(f"Ищу «{_esc(q)}»…"))
    try:
        tracks = await smart_search(q, await engine.get_config('results'))
    except Exception as e:
        await show(visual.err("Поиск не удался") + f"\n<code>{_esc(e)}</code>")
        return
    if not tracks:
        await show(visual.hint("Ничего не нашлось"))
        return

    def list_text():
        lines = [visual.title("Музыка", sub=f"«{_esc(q)}»")]
        for i, t in enumerate(tracks, 1):
            lines.append(f"<b>{i}.</b> {_esc(t['artist'])} — {_esc(t['title'])}  <i>{_dur(t['duration'])}"
                         + (" · 🅴" if t['explicit'] else '') + (f" · {t['from']}" if t.get('from') else '') + "</i>")
        return "\n".join(lines)

    def list_buttons():
        rows = [[(f"{i + 1}. {t['artist'][:16]} — {t['title'][:24]}", f"mu:o:{i}")] for i, t in enumerate(tracks)]
        rows.append([(visual.btn('Закрыть', 'close'), "mu:close")])
        return rows

    try:
        form = await engine.create_inlineform(event.chat_id, list_text(), buttons=list_buttons())
    except Exception:
        await show(list_text() + "\n\n" + visual.hint("выбор кнопками — со стационарным ботом"))
        return
    await show(visual.ok("Музыка") + "  " + visual.hint("выберите трек ниже"))
    chat_id, busy = event.chat_id, {'on': False}

    def card(t, extra=''):
        return "\n".join([f"🎵 <b>{_esc(t['title'])}</b>", visual.field("исполнитель", _esc(t['artist']), code=False),
                          visual.field("альбом", _esc(t['album'] or '—'), code=False), visual.field("длительность", _dur(t['duration']))]
                         + ([f'<a href="{_esc(t["cover"])}">&#8203;</a>'] if t.get('cover') else []) + ([extra] if extra else []))

    def card_buttons(i):
        return [[("⬇ Прислать", f"mu:d:{i}", "success"), ("📜 Текст", f"mu:l:{i}")],
                [(visual.btn('К списку', 'back'), "mu:list")]]

    @form.on_click(data_startswith='mu:')
    async def _(click):
        d = click.data[3:]
        if d == 'close':
            await form.delete()
        elif d == 'list':
            await form.edit(list_text(), buttons=list_buttons())
        elif d.startswith('o:'):
            i = int(d[2:])
            await form.edit(card(tracks[i]), buttons=card_buttons(i))
        elif d.startswith('l:'):
            i = int(d[2:])
            t = tracks[i]
            await click.answer("ищу текст…")
            text = await lyrics(t['artist'], t['title'])
            body = _esc(text[:3000]) + ("…" if text and len(text) > 3000 else '') if text else visual.hint("текст не нашёлся")
            await form.edit(f"📜 <b>{_esc(t['artist'])} — {_esc(t['title'])}</b>\n\n{body}", buttons=card_buttons(i))
        elif d.startswith('d:'):
            if busy['on']:
                await click.answer("уже скачиваю — подождите", show_alert=True)
                return
            i = int(d[2:])
            t = dict(tracks[i])
            busy['on'] = True
            await click.answer("скачиваю…")

            async def fshow(text):
                try:
                    await form.edit(card(t, text), buttons=None)
                except Exception:
                    pass
            ok = False
            try:
                await send_track(chat_id, t, fshow)
                ok = True
            except Exception as e:
                _log_exc('скачивание', e)
                err = visual.err("Не получилось") + f"\n<code>{_esc(_err_text(e))}</code>"
            finally:
                busy['on'] = False
            try:
                await form.edit(card(t, visual.ok("Отправлено") if ok else err), buttons=card_buttons(i))
            except Exception:
                pass


@decorators.command('lyrics', 'lyr')
async def lyrics_cmd(event):
    """текст песни: <исполнитель — название>, или ответом на трек"""
    q = engine.get_args().strip()
    artist, title = '', q
    if not q and event.is_reply:
        r = await event.get_reply_message()
        a = getattr(getattr(r, 'file', None), 'performer', None) if r else None
        t = getattr(getattr(r, 'file', None), 'title', None) if r else None
        artist, title = a or '', t or ''
    elif ' - ' in q or ' — ' in q:
        artist, title = [x.strip() for x in re.split(r'\s+[—-]\s+', q, 1)]
    if not title:
        await engine.answer(event, visual.err("Укажите песню") + "\n" + visual.hint(".lyrics исполнитель — название"))
        return
    if not artist:
        found = await deezer_search(title, 1)
        if found:
            artist, title = found[0]['artist'], found[0]['title']
    await engine.answer(event, visual.wait("Ищу текст…"))
    text = await lyrics(artist, title)
    if not text:
        await engine.answer(event, visual.hint(f"Текст «{_esc(artist)} — {_esc(title)}» не нашёлся"))
        return
    await engine.answer(event, f"📜 <b>{_esc(artist)} — {_esc(title)}</b>\n\n<blockquote expandable>{_esc(text[:3800])}</blockquote>")
