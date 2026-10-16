"""
Remote — управление компьютером, на котором запущен FRX, прямо из Telegram. Только для владельца аккаунта:
команды гостей (.access) и чужие клики по панели модуль не выполняет.

  .remote (.pc)             панель с кнопками: скриншот, громкость, медиаклавиши, буфер обмена, блокировка,
                            сон / выключение / перезагрузка (сразу или по таймеру)
  .screen                   скриншот всех экранов
  .vol [0-100 | + | - | mute]   громкость: без аргумента — текущая
  .media <play | next | prev>   медиаклавиши (пауза/воспроизведение, следующий, предыдущий)
  .clip [текст]             буфер обмена: без аргумента — прочитать, с текстом (или ответом на сообщение) — записать
  .lock                     заблокировать экран
  .pcsleep [через]          сон;  .pcoff [через] — выключение;  .pcreboot [через] — перезагрузка
                            «через»: 30s, 15m, 1h30m, 90 (минуты). Без времени — сразу
  .pccancel                 отменить таймер

Работает на Windows, macOS и Linux с рабочим столом. На Linux нужны обычные утилиты рабочего стола:
громкость — pactl / wpctl / amixer, медиаклавиши — playerctl, буфер — wl-clipboard / xclip / xsel,
скриншот — Pillow (X11) или grim / gnome-screenshot / spectacle / scrot. В Docker и на сервере без экрана
доступны только сон / выключение / перезагрузка (если разрешены системой). В Termux — буфер (Termux:API).

Таймер переживает перезапуск FRX. Если FRX был выключен в момент срабатывания (например, компьютер уже
перезагрузили), действие не выполняется задним числом — таймер просто снимается.
"""

import asyncio
import html
import io
import os
import platform
import re
import shutil
import subprocess
import time

import db
import decorators
import engine
import visual

NAME = "Remote"
MODULE_NAME = "remote"
VERSION = "1.1"
AUTHOR = "FRX"
DESCRIPTION = "Управление ПК из Telegram: скриншот, громкость, медиаклавиши, буфер, блокировка, сон и выключение по таймеру"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/remote.py"
REQUIREMENTS = ["pycaw; sys_platform == 'win32'"]   # ставятся сами (core/requirements.py)

SYSTEM = platform.system()            # 'Windows' | 'Darwin' | 'Linux'
POWER = {'sleep': ('💤', 'сон'), 'off': ('⏻', 'выключение'), 'reboot': ('🔄', 'перезагрузка')}
_timer = {'task': None}


def _esc(x):
    return html.escape(str(x), quote=False)


class RemoteError(Exception):
    pass


# =========================================================================== доступ

def _owner_only(event=None):
    """Remote — только для владельца: гость с доступом к модулю всё равно получит отказ."""
    if engine.guest_id() is not None:
        return False
    if event is not None and not getattr(event, 'out', True):
        return False
    return True


async def _deny(event):
    try:
        await engine.answer(event, visual.err("Remote — только для владельца"))
    except Exception:
        pass


# =========================================================================== запуск утилит

def _run(args, timeout=10, input_text=None):
    """Запустить утилиту; RemoteError с понятным текстом, если её нет или она упала."""
    exe = shutil.which(args[0]) if not os.path.isabs(args[0]) else args[0]
    if not exe:
        raise RemoteError(f"нет утилиты {args[0]}")
    kw = {}
    if SYSTEM == 'Windows':
        kw['creationflags'] = 0x08000000           # CREATE_NO_WINDOW — без мелькающего окна консоли
    env = dict(os.environ)
    env.setdefault('LANG', 'en_US.UTF-8')          # pbpaste/pbcopy и xclip — в UTF-8
    try:
        r = subprocess.run([exe] + list(args[1:]), capture_output=True, timeout=timeout, env=env,
                           input=input_text.encode('utf-8') if input_text is not None else None, **kw)
    except subprocess.TimeoutExpired:
        raise RemoteError(f"{args[0]}: не ответил за {timeout} с")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or b'').decode('utf-8', 'replace').strip().splitlines()[-1:] or ['ошибка']
        raise RemoteError(f"{args[0]}: {tail[0][:200]}")
    return r.stdout.decode('utf-8', 'replace')


def _first(*variants):
    """Пробует варианты по очереди: функция без аргументов или список-команда. Первый успешный — результат."""
    errors = []
    for v in variants:
        try:
            return v() if callable(v) else _run(v)
        except RemoteError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    raise RemoteError("; ".join(errors[-3:]) or "не поддерживается")


def _headless():
    if SYSTEM != 'Linux':
        return False
    return not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def _is_termux():
    return 'com.termux' in os.environ.get('PREFIX', '') or 'ANDROID_ROOT' in os.environ


# =========================================================================== Windows: клавиши и буфер

VK = {'vol_up': 0xAF, 'vol_down': 0xAE, 'mute': 0xAD, 'play': 0xB3, 'next': 0xB0, 'prev': 0xB1}


def _win_key(code, times=1):
    import ctypes
    user32 = ctypes.windll.user32
    for _ in range(times):
        user32.keybd_event(code, 0, 1, 0)                    # KEYEVENTF_EXTENDEDKEY
        user32.keybd_event(code, 0, 1 | 2, 0)                # + KEYEVENTF_KEYUP


def _win_volume_api():
    """Точная громкость через pycaw (если стоит). None — нет pycaw."""
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL, CoInitialize
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except Exception:
        return None
    try:
        CoInitialize()
    except Exception:
        pass
    dev = AudioUtilities.GetSpeakers()
    ep = getattr(dev, 'EndpointVolume', None)
    if ep is not None:
        return ep
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def _win_clip_get():
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetClipboardData.restype = wintypes.HANDLE
    k.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not u.OpenClipboard(None):
        raise RemoteError("буфер занят другой программой")
    try:
        h = u.GetClipboardData(13)                        # CF_UNICODETEXT
        if not h:
            return ''
        p = k.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def _win_clip_set(text):
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalAlloc.restype = wintypes.HGLOBAL
    k.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u.SetClipboardData.restype = wintypes.HANDLE
    data = (text + '\0').encode('utf-16-le')
    h = k.GlobalAlloc(0x0002, len(data))                  # GMEM_MOVEABLE
    p = k.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    k.GlobalUnlock(h)
    if not u.OpenClipboard(None):
        raise RemoteError("буфер занят другой программой")
    try:
        u.EmptyClipboard()
        if not u.SetClipboardData(13, h):
            raise RemoteError("не удалось записать в буфер")
    finally:
        u.CloseClipboard()


# =========================================================================== действия (синхронные — в to_thread)

def screenshot():
    """PNG всех экранов -> bytes."""
    if _is_termux():
        raise RemoteError("на Android скриншот экрана из Termux недоступен")
    try:
        from PIL import ImageGrab
        kw = {'all_screens': True} if SYSTEM in ('Windows', 'Darwin') else {}
        img = ImageGrab.grab(**kw)
        buf = io.BytesIO()
        img.save(buf, 'PNG', optimize=False)
        return buf.getvalue()
    except Exception as first_err:
        if SYSTEM != 'Linux':
            raise RemoteError(f"скриншот не вышел: {first_err}")
    if _headless():
        raise RemoteError("у этой системы нет экрана (сервер или Docker)")
    import tempfile
    path = os.path.join(tempfile.mkdtemp(prefix='frx_scr_'), 'screen.png')
    try:
        _first(['grim', path], ['gnome-screenshot', '-f', path], ['spectacle', '-b', '-n', '-o', path],
               ['scrot', '-o', path], ['import', '-window', 'root', path])
        with open(path, 'rb') as f:
            return f.read()
    finally:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def volume_get():
    """Громкость 0-100 и mute -> (int | None, bool | None)."""
    if SYSTEM == 'Windows':
        ep = _win_volume_api()
        if ep is None:
            return None, None
        return round(ep.GetMasterVolumeLevelScalar() * 100), bool(ep.GetMute())
    if SYSTEM == 'Darwin':
        out = _run(['osascript', '-e', 'get volume settings'])
        vol = re.search(r'output volume:(\d+)', out)
        mute = re.search(r'output muted:(\w+)', out)
        return (int(vol.group(1)) if vol else None), (mute.group(1) == 'true' if mute else None)

    def pactl():
        v = _run(['pactl', 'get-sink-volume', '@DEFAULT_SINK@'])
        m = _run(['pactl', 'get-sink-mute', '@DEFAULT_SINK@'])
        return int(re.search(r'(\d+)%', v).group(1)), 'yes' in m.lower()

    def wpctl():
        out = _run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'])
        return round(float(re.search(r'([\d.]+)', out).group(1)) * 100), 'MUTED' in out

    def amixer():
        out = _run(['amixer', 'get', 'Master'])
        return int(re.search(r'\[(\d+)%\]', out).group(1)), '[off]' in out
    try:
        return _first(pactl, wpctl, amixer)
    except RemoteError:
        return None, None


def volume_set(value=None, delta=None, toggle_mute=False):
    """value — 0-100; delta — ±шаг; toggle_mute — вкл/выкл звук."""
    if SYSTEM == 'Windows':
        ep = _win_volume_api()
        if toggle_mute:
            if ep is not None:
                ep.SetMute(0 if ep.GetMute() else 1, None)
            else:
                _win_key(VK['mute'])
            return
        if ep is not None:
            cur = ep.GetMasterVolumeLevelScalar() * 100
            new = value if value is not None else cur + delta
            ep.SetMasterVolumeLevelScalar(max(0, min(100, new)) / 100, None)
            return
        # без pycaw: клавиши громкости, одно нажатие = 2 %
        if value is not None:
            _win_key(VK['vol_down'], 50)
            _win_key(VK['vol_up'], round(max(0, min(100, value)) / 2))
        else:
            _win_key(VK['vol_up'] if delta > 0 else VK['vol_down'], max(1, round(abs(delta) / 2)))
        return
    if SYSTEM == 'Darwin':
        if toggle_mute:
            _run(['osascript', '-e', 'set volume output muted (not (output muted of (get volume settings)))'])
        elif value is not None:
            _run(['osascript', '-e', f'set volume output volume {int(max(0, min(100, value)))}'])
        else:
            _run(['osascript', '-e', f'set volume output volume ((output volume of (get volume settings)) + ({int(delta)}))'])
        return
    if toggle_mute:
        _first(['pactl', 'set-sink-mute', '@DEFAULT_SINK@', 'toggle'], ['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', 'toggle'],
               ['amixer', '-q', 'set', 'Master', 'toggle'])
    elif value is not None:
        v = int(max(0, min(100, value)))
        _first(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{v}%'], ['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{v}%'],
               ['amixer', '-q', 'set', 'Master', f'{v}%'])
    else:
        d = int(delta)
        sign = '+' if d > 0 else '-'
        cur = (volume_get()[0] or 0) + d
        _first(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{sign}{abs(d)}%'] if cur <= 100 else
               ['pactl', 'set-sink-volume', '@DEFAULT_SINK@', '100%'],
               ['wpctl', 'set-volume', '-l', '1.0', '@DEFAULT_AUDIO_SINK@', f'{abs(d)}%{sign}'],
               ['amixer', '-q', 'set', 'Master', f'{abs(d)}%{sign}'])


def media_key(action):
    """action: play | next | prev."""
    if SYSTEM == 'Windows':
        _win_key(VK[action])
        return
    if SYSTEM == 'Darwin':
        verb = {'play': 'playpause', 'next': 'next track', 'prev': 'previous track'}[action]

        def app(name):
            def go():
                running = _run(['osascript', '-e', f'application "{name}" is running']).strip()
                if running != 'true':
                    raise RemoteError(f"{name} не запущен")
                _run(['osascript', '-e', f'tell application "{name}" to {verb}'])
            return go
        cli = {'play': 'togglePlayPause', 'next': 'next', 'prev': 'previous'}[action]
        _first(['nowplaying-cli', cli], app('Spotify'), app('Music'))
        return
    cmd = {'play': 'play-pause', 'next': 'next', 'prev': 'previous'}[action]
    _first(['playerctl', cmd])


def media_now():
    """Что играет в системном плеере (если это можно узнать) -> str | None."""
    try:
        if SYSTEM == 'Linux':
            return _run(['playerctl', 'metadata', '--format', '{{artist}} — {{title}}'], timeout=3).strip() or None
        if SYSTEM == 'Darwin':
            return _run(['nowplaying-cli', 'get', 'artist', 'title'], timeout=3).strip().replace('\n', ' — ') or None
    except Exception:
        return None
    return None


def clip_get():
    """Буфер -> ('text', str) | ('image', png bytes)."""
    if SYSTEM in ('Windows', 'Darwin'):
        try:
            from PIL import ImageGrab
            img = ImageGrab.grabclipboard()
            if img is not None and hasattr(img, 'save'):
                buf = io.BytesIO()
                img.save(buf, 'PNG')
                return 'image', buf.getvalue()
        except Exception:
            pass
    if SYSTEM == 'Windows':
        return 'text', _win_clip_get()
    if SYSTEM == 'Darwin':
        return 'text', _run(['pbpaste'])
    return 'text', _first(['termux-clipboard-get'], ['wl-paste', '--no-newline'], ['xclip', '-selection', 'clipboard', '-o'],
                          ['xsel', '--clipboard', '--output'])


def clip_set(text):
    if SYSTEM == 'Windows':
        _win_clip_set(text)
        return
    if SYSTEM == 'Darwin':
        _run(['pbcopy'], input_text=text)
        return

    def runner(*cmd):
        return lambda: _run(list(cmd), input_text=text)
    _first(runner('termux-clipboard-set'), runner('wl-copy'), runner('xclip', '-selection', 'clipboard'),
           runner('xsel', '--clipboard', '--input'))


def lock_screen():
    if SYSTEM == 'Windows':
        import ctypes
        if not ctypes.windll.user32.LockWorkStation():
            raise RemoteError("Windows отказал в блокировке")
        return
    if SYSTEM == 'Darwin':
        _first(['pmset', 'displaysleepnow'])
        return
    _first(['loginctl', 'lock-session'], ['xdg-screensaver', 'lock'], ['gnome-screensaver-command', '-l'],
           ['dm-tool', 'lock'], ['xflock4'])


def power(action):
    """action: sleep | off | reboot."""
    if SYSTEM == 'Windows':
        if action == 'sleep':
            import ctypes
            # SetSuspendState(hibernate=False, force=True, wakeupEventsDisabled=False); при включённой гибернации
            # Windows может уйти в неё — это настройка системы (powercfg /h off)
            if not ctypes.windll.powrprof.SetSuspendState(False, True, False):
                raise RemoteError("Windows отказал в переходе в сон")
            return
        _run(['shutdown', '/s' if action == 'off' else '/r', '/t', '0'])
        return
    if SYSTEM == 'Darwin':
        if action == 'sleep':
            _run(['pmset', 'sleepnow'])
        else:
            _run(['osascript', '-e', f'tell application "System Events" to {"shut down" if action == "off" else "restart"}'])
        return
    if action == 'sleep':
        _first(['systemctl', 'suspend'], ['loginctl', 'suspend'])
    elif action == 'off':
        _first(['systemctl', 'poweroff'], ['loginctl', 'poweroff'], ['shutdown', '-h', 'now'])
    else:
        _first(['systemctl', 'reboot'], ['loginctl', 'reboot'], ['shutdown', '-r', 'now'])


# =========================================================================== таймер питания

def parse_delay(text):
    """'30s' '15m' '1h30m' '90' (минуты) '1:30' (ч:мм) -> секунды | None."""
    t = (text or '').strip().lower().replace(' ', '')
    if not t:
        return None
    if t.isdigit():
        return int(t) * 60
    m = re.fullmatch(r'(\d+):(\d{1,2})', t)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    total, pos = 0, 0
    units = {'s': 1, 'с': 1, 'm': 60, 'м': 60, 'min': 60, 'мин': 60, 'h': 3600, 'ч': 3600, 'd': 86400, 'д': 86400}
    for m in re.finditer(r'(\d+)(min|мин|[smhdсмчд])', t):
        if m.start() != pos:
            return None
        total += int(m.group(1)) * units[m.group(2)]
        pos = m.end()
    return total if pos == len(t) and total > 0 else None


def _fmt_left(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    if h:
        return f"{h} ч {m:02} мин"
    if m:
        return f"{m} мин {s:02} с" if m < 10 else f"{m} мин"
    return f"{s} с"


async def _timer_info():
    t = await db.get('timer')
    if not t:
        return None
    return t


async def timer_set(action, delay):
    await timer_cancel()
    await db.set('timer', {'action': action, 'at': time.time() + delay})
    _arm(action, delay)


async def timer_cancel():
    task = _timer.get('task')
    if task is not None and not task.done():
        task.cancel()
    _timer['task'] = None
    await db.delete('timer')


def _arm(action, delay):
    async def fire():
        try:
            await asyncio.sleep(max(0, delay - 60))
            if delay >= 60:                                  # за минуту — предупреждение владельцу
                icon, word = POWER[action]
                try:
                    await engine.bot_send_to_owner(visual.warn(f"{icon} Через минуту — {word} компьютера. Отменить: .pccancel"))
                except Exception:
                    pass
                await asyncio.sleep(60)
            await db.delete('timer')
            _timer['task'] = None
            await asyncio.to_thread(power, action)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            engine.tlog(f"[Remote] таймер ({action}): {e}")
            try:
                await engine.bot_send_to_owner(visual.err("Remote: таймер не сработал") + f"\n<code>{_esc(str(e)[:300])}</code>")
            except Exception:
                pass
    _timer['task'] = asyncio.ensure_future(fire())


@decorators.on_start()
async def setup():
    await engine.create_config(key='screens_to', description='Куда присылать скриншоты: chat — в чат команды/панели, '
                               'saved — в «Избранное» (на экране бывает личное)', data_type='choice', default='saved',
                               options=['saved', 'chat'])
    await engine.create_config(key='screen_as_file', description='Скриншот файлом (без сжатия Telegram)', data_type='bool',
                               default=False)
    await engine.create_config(key='volume_step', description='Шаг кнопок громкости, %', data_type='int', default=10,
                               valid={'min_value': 2, 'max_value': 50})
    await engine.create_config(key='confirm_power', description='Спрашивать подтверждение перед сном/выключением/перезагрузкой',
                               data_type='bool', default=True)
    t = await db.get('timer')
    if t:
        left = t['at'] - time.time()
        if left > 5:
            _arm(t['action'], left)
        else:                                  # время прошло, пока FRX не работал, — задним числом не выполняем
            await db.delete('timer')
            engine.tlog(f"[Remote] таймер «{t['action']}» просрочен — снят без выполнения")


@decorators.on_unload()
async def cleanup():
    task = _timer.get('task')
    if task is not None and not task.done():
        task.cancel()                          # запись в db остаётся — после перезагрузки модуля таймер поднимется снова
    _timer['task'] = None


# =========================================================================== отправка скриншота

async def send_screenshot(chat_id, reply_to=None):
    data = await asyncio.to_thread(screenshot)
    target = 'me' if await engine.get_config('screens_to') == 'saved' else chat_id
    as_file = await engine.get_config('screen_as_file')
    buf = io.BytesIO(data)
    buf.name = time.strftime('screen_%Y%m%d_%H%M%S.png')
    caption = visual.hint(f"🖥 {_esc(platform.node())} · {time.strftime('%H:%M:%S')}")
    big = len(data) > 9 * 1024 * 1024
    await engine.client.send_file(target, buf, caption=caption, parse_mode='html', force_document=as_file or big,
                                  reply_to=reply_to if target == chat_id else None)
    return target


# =========================================================================== панель

async def _status_text(note=''):
    vol, mute = await asyncio.to_thread(volume_get)
    lines = [visual.title("Remote", sub=f"{_esc(platform.node())} · {_esc(SYSTEM if SYSTEM != 'Darwin' else 'macOS')}")]
    if vol is not None:
        lines.append(f"🔊 <code>{engine.progress_bar(vol, 12, 'bar')}</code> {vol}%" + (" · 🔇 без звука" if mute else ''))
    else:
        lines.append(visual.hint("🔊 точная громкость недоступна — кнопки всё равно работают"
                                 + (" (для точной: pip install pycaw)" if SYSTEM == 'Windows' else '')))
    now = await asyncio.to_thread(media_now)
    if now:
        lines.append(f"🎵 {_esc(now[:80])}")
    t = await _timer_info()
    if t:
        icon, word = POWER[t['action']]
        lines.append(f"⏱ {icon} {word} через <b>{_fmt_left(t['at'] - time.time())}</b>")
    if _headless():
        lines.append(visual.hint("экрана нет — работают сон / выключение / перезагрузка"))
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def _main_buttons(has_timer):
    return [
        [("📸 Скриншот", "rm:shot", "primary"), ("📋 Буфер", "rm:clip")],
        [("🔉", "rm:vol:-"), ("🔇", "rm:mute"), ("🔊", "rm:vol:+")],
        [("⏮", "rm:media:prev"), ("⏯", "rm:media:play"), ("⏭", "rm:media:next")],
        [("🔒 Блокировка", "rm:lock"), ("💤 Сон", "rm:pw:sleep")],
        [("⏻ Выключить", "rm:pw:off", "danger"), ("🔄 Перезагрузить", "rm:pw:reboot")],
        [("⏱ Таймер", "rm:timer")] + ([("✕ Отменить таймер", "rm:tcancel", "danger")] if has_timer else []),
        [("⟳ Обновить", "rm:main"), (visual.btn('Закрыть', 'close'), "rm:close")],
    ]


async def open_panel(event):
    st = {'timer_action': 'off'}
    chat_id = event.chat_id
    form = await engine.create_inlineform(chat_id, await _status_text(),
                                          buttons=_main_buttons(bool(await _timer_info())), for_user=None)

    async def show_main(note=''):
        await form.edit(await _status_text(note), buttons=_main_buttons(bool(await _timer_info())))

    async def show_timer(note=''):
        a = st['timer_action']
        rows = [[(("◉ " if a == k else "○ ") + POWER[k][1], f"rm:ta:{k}") for k in POWER],
                [("15 мин", "rm:tset:900"), ("30 мин", "rm:tset:1800"), ("1 ч", "rm:tset:3600")],
                [("2 ч", "rm:tset:7200"), ("3 ч", "rm:tset:10800"),
                 engine.InputButton("✏ Своё", "tcustom", hint="через сколько: 45m, 1h30m, 2:15 или минуты")],
                [(visual.btn('Назад', 'back'), "rm:main")]]
        icon, word = POWER[a]
        text = "\n".join([visual.title("Таймер", sub=f"{icon} {word}"), visual.hint("выберите действие и время")]
                         + ([note] if note else []))
        await form.edit(text, buttons=rows)

    async def show_confirm(action, delay=0):
        icon, word = POWER[action]
        when = f" через {_fmt_left(delay)}" if delay else " сейчас"
        await form.edit(visual.warn(f"{icon} <b>{word.capitalize()}{when}?</b>"),
                        buttons=[[("Да", f"rm:do:{action}:{delay}", "danger"), ("Нет", "rm:main")]])

    async def do_power(action, delay):
        icon, word = POWER[action]
        if delay:
            await timer_set(action, delay)
            await show_main(visual.ok(f"{icon} {word.capitalize()} через {_fmt_left(delay)}"))
            return
        await form.edit(visual.wait(f"{icon} {word.capitalize()}…"), buttons=None)
        try:
            await timer_cancel()
            await asyncio.to_thread(power, action)
        except Exception as e:
            await show_main(visual.err(f"{word.capitalize()} не вышло") + f"\n<code>{_esc(str(e)[:300])}</code>")

    @form.on_click(data_startswith='rm:')
    async def _(click):
        d = click.data[3:]
        try:
            if d == 'close':
                await form.delete()
            elif d == 'main':
                await click.answer()
                await show_main()
            elif d == 'shot':
                await click.answer("снимаю…")
                where = await send_screenshot(chat_id)
                await show_main(visual.ok("Скриншот в «Избранном»" if where == 'me' else "Скриншот отправлен"))
            elif d == 'clip':
                await click.answer()
                kind, val = await asyncio.to_thread(clip_get)
                if kind == 'image':
                    buf = io.BytesIO(val)
                    buf.name = 'clipboard.png'
                    target = 'me' if await engine.get_config('screens_to') == 'saved' else chat_id
                    await engine.client.send_file(target, buf, caption=visual.hint("📋 картинка из буфера"), parse_mode='html')
                    body = visual.ok("В буфере картинка — отправлена" + (" в «Избранное»" if target == 'me' else ''))
                else:
                    body = (f"<pre>{_esc(val[:3000])}</pre>" + (visual.hint(f"… ещё {len(val) - 3000} символов")
                                                               if len(val) > 3000 else '')) if val else visual.hint("буфер пуст")
                await form.edit("\n".join([visual.title("Буфер обмена"), body]),
                                buttons=[[engine.InputButton("✏ Записать в буфер", "clipset", hint="текст для буфера обмена")],
                                         [(visual.btn('Назад', 'back'), "rm:main")]])
            elif d.startswith('vol:'):
                step = await engine.get_config('volume_step')
                await asyncio.to_thread(volume_set, None, step if d.endswith('+') else -step)
                vol, _m = await asyncio.to_thread(volume_get)
                await click.answer(f"громкость {vol}%" if vol is not None else "готово")
                await show_main()
            elif d == 'mute':
                await asyncio.to_thread(volume_set, None, None, True)
                await click.answer("звук переключён")
                await show_main()
            elif d.startswith('media:'):
                await asyncio.to_thread(media_key, d[6:])
                await click.answer({'play': '⏯', 'next': '⏭', 'prev': '⏮'}[d[6:]])
                await asyncio.sleep(0.6)
                await show_main()
            elif d == 'lock':
                await asyncio.to_thread(lock_screen)
                await click.answer("экран заблокирован")
            elif d.startswith('pw:'):
                action = d[3:]
                if await engine.get_config('confirm_power'):
                    await show_confirm(action)
                else:
                    await do_power(action, 0)
            elif d.startswith('do:'):
                _, action, delay = d.split(':')
                await do_power(action, int(delay))
            elif d == 'timer':
                await click.answer()
                await show_timer()
            elif d.startswith('ta:'):
                st['timer_action'] = d[3:]
                await show_timer()
            elif d.startswith('tset:'):
                await do_power(st['timer_action'], int(d[5:]))
            elif d == 'tcancel':
                await timer_cancel()
                await click.answer("таймер отменён")
                await show_main(visual.ok("Таймер отменён"))
        except RemoteError as e:
            await click.answer(str(e)[:190], show_alert=True)
        except Exception as e:
            await click.answer(f"ошибка: {str(e)[:180]}", show_alert=True)

    @form.on_input(data='clipset')
    async def _(inp):
        try:
            await asyncio.to_thread(clip_set, inp.value or '')
            await show_main(visual.ok("Записано в буфер"))
        except Exception as e:
            await show_main(visual.err("Буфер") + f"\n<code>{_esc(str(e)[:300])}</code>")

    @form.on_input(data='tcustom')
    async def _(inp):
        delay = parse_delay(inp.value)
        if not delay:
            await show_timer(visual.err("Не понял время") + " " + visual.hint("примеры: 45m, 1h30m, 2:15, 90"))
            return
        await do_power(st['timer_action'], delay)

    return form


# =========================================================================== команды

def _guarded(func):
    async def wrapper(event):
        if not _owner_only(event):
            await _deny(event)
            return
        try:
            await func(event)
        except RemoteError as e:
            await engine.answer(event, visual.err("Remote") + f"\n<code>{_esc(str(e)[:400])}</code>")
    wrapper.__doc__ = func.__doc__
    wrapper.__name__ = func.__name__
    return wrapper


@decorators.command('remote', 'pc')
@_guarded
async def remote_cmd(event):
    """панель управления компьютером"""
    try:
        await open_panel(event)
        try:
            await event.delete()
        except Exception:
            pass
    except Exception as e:
        await engine.answer(event, await _status_text(visual.hint(f"кнопки — со стационарным ботом ({_esc(str(e)[:120])})")))


@decorators.command('screen', 'scr')
@_guarded
async def screen_cmd(event):
    """скриншот всех экранов компьютера"""
    await engine.answer(event, visual.wait("Снимаю экран…"))
    where = await send_screenshot(event.chat_id, event.reply_to_msg_id)
    if where == 'me' and event.chat_id != (await engine.client.get_me()).id:
        await engine.answer(event, visual.ok("Скриншот в «Избранном»"))
    else:
        await event.delete()


@decorators.command('vol')
@_guarded
async def vol_cmd(event):
    """громкость компьютера: [0-100 | + | - | mute]"""
    a = engine.get_args().strip().lower()
    step = await engine.get_config('volume_step')
    if a in ('mute', 'm', 'тихо', 'unmute'):
        await asyncio.to_thread(volume_set, None, None, True)
    elif a in ('+', 'up', 'громче'):
        await asyncio.to_thread(volume_set, None, step)
    elif a in ('-', 'down', 'тише'):
        await asyncio.to_thread(volume_set, None, -step)
    elif re.fullmatch(r'[+-]?\d{1,3}%?', a or 'x'):
        n = int(a.rstrip('%'))
        if a[0] in '+-':
            await asyncio.to_thread(volume_set, None, n)
        else:
            await asyncio.to_thread(volume_set, max(0, min(100, n)))
    elif a:
        await engine.answer(event, visual.err("Формат: .vol 40 · .vol + · .vol - · .vol mute"))
        return
    vol, mute = await asyncio.to_thread(volume_get)
    if vol is None:
        await engine.answer(event, visual.ok("Готово") + ("" if not a else "  " + visual.hint("точное значение недоступно")))
        return
    await engine.answer(event, f"🔊 <code>{engine.progress_bar(vol, 12, 'bar')}</code> {vol}%" + (" · 🔇 без звука" if mute else ''))


@decorators.command('media')
@_guarded
async def media_cmd(event):
    """медиаклавиши: <play | next | prev>"""
    a = engine.get_args().strip().lower()
    action = {'play': 'play', 'pause': 'play', 'p': 'play', 'next': 'next', 'n': 'next', '>': 'next',
              'prev': 'prev', 'back': 'prev', '<': 'prev'}.get(a)
    if not action:
        await engine.answer(event, visual.err("Формат: .media play · next · prev"))
        return
    await asyncio.to_thread(media_key, action)
    await asyncio.sleep(0.6)
    now = await asyncio.to_thread(media_now)
    await engine.answer(event, visual.ok({'play': '⏯ Пауза / воспроизведение', 'next': '⏭ Следующий', 'prev': '⏮ Предыдущий'}[action])
                        + (f"\n🎵 {_esc(now[:80])}" if now else ''))


@decorators.command('clip')
@_guarded
async def clip_cmd(event):
    """буфер обмена компьютера: без аргументов — прочитать; [текст] или ответом — записать"""
    text = engine.get_args()
    if not text and event.is_reply:
        r = await event.get_reply_message()
        text = (r.raw_text or '') if r else ''
    if text:
        await asyncio.to_thread(clip_set, text)
        await engine.answer(event, visual.ok("Записано в буфер") + "  " + visual.hint(f"{len(text)} символов"))
        return
    kind, val = await asyncio.to_thread(clip_get)
    if kind == 'image':
        buf = io.BytesIO(val)
        buf.name = 'clipboard.png'
        await engine.client.send_file('me' if await engine.get_config('screens_to') == 'saved' else event.chat_id, buf,
                                      caption=visual.hint("📋 картинка из буфера"), parse_mode='html')
        await engine.answer(event, visual.ok("В буфере картинка — отправлена"))
        return
    if not val:
        await engine.answer(event, visual.hint("Буфер пуст"))
        return
    await engine.answer(event, visual.title("Буфер обмена") + f"\n<pre>{_esc(val[:3500])}</pre>"
                        + (visual.hint(f"… ещё {len(val) - 3500} символов") if len(val) > 3500 else ''))


@decorators.command('lock')
@_guarded
async def lock_cmd(event):
    """заблокировать экран компьютера"""
    await asyncio.to_thread(lock_screen)
    await engine.answer(event, visual.ok("🔒 Экран заблокирован"))


async def _power_cmd(event, action):
    icon, word = POWER[action]
    a = engine.get_args().strip()
    if a:
        delay = parse_delay(a)
        if not delay:
            await engine.answer(event, visual.err("Не понял время") + "\n" + visual.hint("примеры: 30s, 15m, 1h30m, 2:15, 90 (минуты)"))
            return
        await timer_set(action, delay)
        await engine.answer(event, visual.ok(f"{icon} {word.capitalize()} через {_fmt_left(delay)}") + "\n" + visual.hint("отменить: .pccancel"))
        return
    await engine.answer(event, visual.wait(f"{icon} {word.capitalize()}…"))
    await timer_cancel()
    await asyncio.to_thread(power, action)


@decorators.command('pcsleep')
@_guarded
async def pcsleep_cmd(event):
    """сон компьютера: [через] — 15m, 1h30m, 90"""
    await _power_cmd(event, 'sleep')


@decorators.command('pcoff')
@_guarded
async def pcoff_cmd(event):
    """выключить компьютер: [через] — 15m, 1h30m, 90"""
    await _power_cmd(event, 'off')


@decorators.command('pcreboot')
@_guarded
async def pcreboot_cmd(event):
    """перезагрузить компьютер: [через] — 15m, 1h30m, 90"""
    await _power_cmd(event, 'reboot')


@decorators.command('pccancel')
@_guarded
async def pccancel_cmd(event):
    """отменить таймер сна / выключения / перезагрузки"""
    t = await _timer_info()
    await timer_cancel()
    await engine.answer(event, visual.ok("Таймер отменён") if t else visual.hint("Таймера нет"))
