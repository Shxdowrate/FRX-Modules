"""
SysInfo — что за система крутит FRX: ОС, время работы, нагрузка, память, диски, Python, сеть Telegram.
Работает одинаково на Windows, Linux, macOS, Android (Termux) и в Docker.

  .sysinfo          сводка текстом
  .sysinfo card     то же карточкой-картинкой
  .sysinfo proc     что сейчас больше всего ест процессор и память
  .dc               ваш дата-центр Telegram и задержка ответа
"""

import asyncio
import html
import io
import os
import platform
import time
from pathlib import Path

import decorators
import engine
import visual

NAME = "SysInfo"
MODULE_NAME = "sysinfo"
VERSION = "1.2"
AUTHOR = "FRX"
DESCRIPTION = "Сведения о системе: ОС, аптайм, нагрузка, память, диски, Python, дата-центр Telegram"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/sysinfo.py"
REQUIREMENTS = ["psutil"]   # ставятся сами (core/requirements.py)

DC_CITY = {1: 'Майами', 2: 'Амстердам', 3: 'Майами', 4: 'Амстердам', 5: 'Сингапур'}


def _esc(x):
    return html.escape(str(x), quote=False)


def _size(n):
    n = float(n or 0)
    for unit in ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ'):
        if n < 1024 or unit == 'ТБ':
            return (f"{n:.0f} {unit}" if unit == 'Б' else f"{n:.1f} {unit}")
        n /= 1024


def _dur(sec):
    sec = int(sec or 0)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    parts = []
    if d:
        parts.append(f"{d} дн")
    if h:
        parts.append(f"{h} ч")
    if m or not parts:
        parts.append(f"{m} мин")
    return ' '.join(parts)


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:
        return None


# --------------------------------------------------------------------------- сбор данных

def _is_android():
    return ('ANDROID_ROOT' in os.environ or 'com.termux' in os.environ.get('PREFIX', '')
            or Path('/system/build.prop').exists())


def _is_wsl():
    try:
        return 'microsoft' in platform.release().lower() or 'WSL_DISTRO_NAME' in os.environ
    except Exception:
        return False


def _android_version():
    try:
        import subprocess
        v = subprocess.check_output(['getprop', 'ro.build.version.release'], timeout=3).decode().strip()
        return f"Android {v}" if v else 'Android'
    except Exception:
        return 'Android'


def _os_name():
    s = platform.system()
    if s == 'Linux':
        if _is_android():
            return f"{_android_version()} (Termux)"
        pretty = _linux_pretty() or 'Linux'
        return f"{pretty} · WSL" if _is_wsl() else pretty
    if s == 'Darwin':
        return f"macOS {platform.mac_ver()[0]}".strip()
    if s == 'Windows':
        rel = platform.release()
        try:                                   # Windows 11 называет себя «10» — отличаем по номеру сборки
            build = int(platform.version().split('.')[-1])
            if rel == '10' and build >= 22000:
                rel = '11'
        except Exception:
            pass
        try:
            ed = platform.win32_edition()
            return f"Windows {rel} {ed}".strip()
        except Exception:
            return f"Windows {rel}"
    if 'BSD' in s:
        return f"{s} {platform.release()}"
    return s or 'неизвестно'


def _linux_pretty():
    try:
        for line in Path('/etc/os-release').read_text().splitlines():
            if line.startswith('PRETTY_NAME='):
                return line.split('=', 1)[1].strip().strip('"')
    except Exception:
        pass
    return ''


def _in_docker():
    if os.environ.get('FRX_IN_DOCKER') or Path('/.dockerenv').exists() or Path('/run/.containerenv').exists():
        return True
    try:
        cg = Path('/proc/1/cgroup').read_text()
        return any(k in cg for k in ('docker', 'containerd', 'kubepods', 'libpod'))
    except Exception:
        return False


def _kernel():
    """Строка «ядра» по смыслу системы: сборка на Windows, Darwin на macOS, версия ядра на Linux/Android."""
    s = platform.system()
    if s == 'Windows':
        return f"сборка {platform.version()}"
    if s == 'Darwin':
        return f"Darwin {platform.release()}"
    return platform.release()


def _frx_root():
    try:
        from core.paths import DATA_DIR
        return Path(DATA_DIR).parent.parent
    except Exception:
        return Path.cwd()


def collect():
    """Снимок системы и процесса FRX (блокирующие вызовы — вызывать в to_thread)."""
    ps = _psutil()
    info = {
        'os': _os_name(),
        'kernel': _kernel(),
        'arch': platform.machine(),
        'host': platform.node(),
        'docker': _in_docker(),
        'python': platform.python_version(),
        'py_impl': platform.python_implementation(),
        'tz': _timezone(),
        'frx_version': getattr(engine, 'version', '?'),
    }
    now = time.time()
    if ps is not None:
        try:
            info['sys_uptime'] = now - ps.boot_time()
        except Exception:
            info['sys_uptime'] = None
        try:
            vm = ps.virtual_memory()
            info['ram_total'], info['ram_used'], info['ram_pct'] = vm.total, vm.used, vm.percent
        except Exception:
            pass
        try:
            sw = ps.swap_memory()
            info['swap_total'], info['swap_used'] = sw.total, sw.used
        except Exception:
            pass
        try:
            info['cpu_pct'] = ps.cpu_percent(interval=0.3)
            info['cpu_cores'] = ps.cpu_count(logical=True)
            info['cpu_phys'] = ps.cpu_count(logical=False)
        except Exception:
            pass
        if platform.system() != 'Windows':        # на Windows psutil эмулирует loadavg — первые секунды нули
            try:
                info['loadavg'] = ps.getloadavg()
            except Exception:
                pass
        try:
            bat = ps.sensors_battery()
            if bat is not None:
                info['battery'] = (bat.percent, bat.power_plugged)
        except Exception:
            pass
        try:
            p = ps.Process()
            with p.oneshot():
                info['frx_uptime'] = now - p.create_time()
                info['frx_ram'] = p.memory_info().rss
                info['frx_cpu'] = p.cpu_percent(interval=0.2)
                info['frx_threads'] = p.num_threads()
        except Exception:
            pass
        try:
            disks = []
            seen = set()
            for part in ps.disk_partitions(all=False):
                if part.fstype in ('', 'squashfs', 'overlay', 'tmpfs', 'devtmpfs') or part.mountpoint in seen:
                    continue
                try:
                    u = ps.disk_usage(part.mountpoint)
                except Exception:
                    continue
                if not u.total:
                    continue
                seen.add(part.mountpoint)
                disks.append({'mount': part.mountpoint, 'total': u.total, 'used': u.used, 'pct': u.percent})
            info['disks'] = disks[:5]
        except Exception:
            pass
    else:
        info['no_psutil'] = True
        try:
            import shutil
            u = shutil.disk_usage(_frx_root())
            info['disks'] = [{'mount': str(_frx_root()), 'total': u.total, 'used': u.used,
                              'pct': round(u.used * 100 / u.total, 1) if u.total else 0}]
        except Exception:
            pass
        try:
            info['cpu_cores'] = os.cpu_count()
        except Exception:
            pass
    info['cpu_name'] = _cpu_name()
    return info


def _timezone():
    try:
        from datetime import datetime
        now = datetime.now().astimezone()
        off = int(now.utcoffset().total_seconds())
        sign = '+' if off >= 0 else '-'
        utc = f"UTC{sign}{abs(off) // 3600:02}:{abs(off) % 3600 // 60:02}"
        name = os.environ.get('TZ') or ''
        if not name and platform.system() != 'Windows':
            try:                                   # /etc/localtime -> .../zoneinfo/Europe/Moscow
                link = os.path.realpath('/etc/localtime')
                if 'zoneinfo/' in link:
                    name = link.split('zoneinfo/', 1)[1]
            except Exception:
                pass
        if not name:
            name = now.tzname() or ''
            if not name.isascii():                 # Windows отдаёт локализованное имя в кодировке системы
                name = ''
        return f"{name} ({utc})" if name else utc
    except Exception:
        return '?'


def _cpu_name():
    try:
        if platform.system() == 'Linux':
            for line in Path('/proc/cpuinfo').read_text().splitlines():
                if line.lower().startswith('model name'):
                    return line.split(':', 1)[1].strip()
        elif platform.system() == 'Windows':
            return os.environ.get('PROCESSOR_IDENTIFIER', '').strip() or platform.processor()
        elif platform.system() == 'Darwin':
            import subprocess
            return subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string'], timeout=3).decode().strip()
    except Exception:
        pass
    return platform.processor() or platform.machine()


def top_processes(n=6):
    ps = _psutil()
    if ps is None:
        return None
    procs = []
    for p in ps.process_iter(['name', 'pid']):
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(0.4)
    for p in ps.process_iter(['name', 'pid']):
        try:
            with p.oneshot():
                procs.append((p.info['name'] or '?', p.info['pid'], p.cpu_percent(None), p.memory_info().rss))
        except Exception:
            continue
    procs.sort(key=lambda x: (-x[2], -x[3]))
    return procs[:n]


# --------------------------------------------------------------------------- текст

def _bar(pct, width=12):
    pct = max(0, min(100, pct or 0))
    full = int(round(pct / 100 * width))
    return '█' * full + '░' * (width - full)


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _cores_text(info):
    logical, phys = info.get('cpu_cores'), info.get('cpu_phys')
    if phys and logical and phys != logical:
        return f"{phys} {_plural(phys, 'ядро', 'ядра', 'ядер')} · {logical} {_plural(logical, 'поток', 'потока', 'потоков')}"
    n = phys or logical
    return f"{n} {_plural(n, 'ядро', 'ядра', 'ядер')}" if n else ''


def text_report(info):
    out = [visual.title("Система", sub=f"FRX {_esc(info['frx_version'])}" + (" · Docker" if info['docker'] else ''))]
    out.append(visual.field("ОС", _esc(info['os'])))
    out.append(visual.field("ядро", f"{_esc(info['kernel'])} · {_esc(info['arch'])}", code=False))
    if info.get('host'):
        out.append(visual.field("хост", _esc(info['host']), code=False))
    if info.get('sys_uptime') is not None:
        out.append(visual.field("система работает", _dur(info['sys_uptime']), code=False))
    if info.get('frx_uptime') is not None:
        out.append(visual.field("FRX работает", _dur(info['frx_uptime']), code=False))
    out.append("")
    if info.get('cpu_name'):
        out.append(visual.field("процессор", _esc(info['cpu_name']), code=False))
    if 'cpu_pct' in info:
        out.append(f"CPU  {_bar(info['cpu_pct'])} {info['cpu_pct']:.0f}% · {_cores_text(info)}")
    elif info.get('cpu_cores'):
        out.append(visual.field("ядер", info['cpu_cores'], code=False))
    if info.get('loadavg'):
        out.append(visual.field("загрузка (1/5/15 мин)", " / ".join(f"{x:.2f}" for x in info['loadavg']), code=False))
    if 'ram_total' in info:
        out.append(f"ОЗУ  {_bar(info['ram_pct'])} {info['ram_pct']:.0f}% · {_size(info['ram_used'])} из {_size(info['ram_total'])}")
    if info.get('battery'):
        pct, plugged = info['battery']
        out.append(visual.field("батарея", f"{pct:.0f}%" + (" · заряжается" if plugged else ''), code=False))
    if info.get('swap_total'):
        out.append(visual.field("подкачка", f"{_size(info['swap_used'])} из {_size(info['swap_total'])}", code=False))
    if 'frx_ram' in info:
        extra = f" · {info['frx_cpu']:.0f}% CPU" if info.get('frx_cpu') is not None else ''
        extra += (f" · {info['frx_threads']} {_plural(info['frx_threads'], 'поток', 'потока', 'потоков')}"
                  if info.get('frx_threads') else '')
        out.append(visual.field("FRX ест", f"{_size(info['frx_ram'])}{extra}", code=False))
    if info.get('disks'):
        out.append("")
        out.append(visual.section("Диски"))
        for d in info['disks']:
            out.append(f"{_esc(d['mount'])}  {_bar(d['pct'], 10)} {_size(d['used'])} / {_size(d['total'])}")
    out.append("")
    out.append(visual.field("Python", f"{info['python']} ({info['py_impl']})", code=False))
    out.append(visual.field("часовой пояс", _esc(info['tz']), code=False))
    if info.get('no_psutil'):
        out.append("")
        out.append(visual.hint("psutil не стоит — подробности о нагрузке недоступны (поставится сам при следующем запуске FRX)"))
    return "\n".join(out)


# --------------------------------------------------------------------------- карточка

_FONTS = {}


def _card_font(size, bold=False):
    """Шрифт с кириллицей: системные → JetBrains Mono из user/data/fonts (если есть) → встроенный Pillow."""
    key = (size, bold)
    if key in _FONTS:
        return _FONTS[key]
    from PIL import ImageFont
    try:
        from core.paths import DATA_DIR
        data = Path(DATA_DIR)
    except Exception:
        data = Path('user') / 'data'
    font = None
    if font is None:
        names = (('DejaVuSans-Bold.ttf', 'arialbd.ttf', 'segoeuib.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
                  '/system/fonts/Roboto-Bold.ttf') if bold else
                 ('DejaVuSans.ttf', 'arial.ttf', 'segoeui.ttf', '/System/Library/Fonts/Supplemental/Arial.ttf',
                  '/system/fonts/Roboto-Regular.ttf'))
        names += (str(data / 'fonts' / ('JetBrainsMono-Bold.ttf' if bold else 'JetBrainsMono-Regular.ttf')),)
        for name in names:
            try:
                font = ImageFont.truetype(name, size)
                break
            except Exception:
                continue
    if font is None:
        try:
            font = ImageFont.load_default(size)
        except TypeError:
            font = ImageFont.load_default()
    _FONTS[key] = font
    return font


def card_image(info):
    from PIL import Image, ImageDraw
    W = 900
    rows = []

    def add(label, value, pct=None):
        rows.append((label, value, pct))
    add("ОС", info['os'])
    add("Ядро", f"{info['kernel']} · {info['arch']}")
    if info.get('sys_uptime') is not None:
        add("Система работает", _dur(info['sys_uptime']))
    if info.get('frx_uptime') is not None:
        add(f"FRX {info['frx_version']} работает", _dur(info['frx_uptime']))
    if info.get('cpu_name'):
        add("Процессор", info['cpu_name'][:48])
    if 'cpu_pct' in info:
        cores = _cores_text(info)
        add("Загрузка CPU", f"{info['cpu_pct']:.0f}%" + (f"  ·  {cores}" if cores else ''), info['cpu_pct'])
    if 'ram_total' in info:
        add("Память", f"{_size(info['ram_used'])} / {_size(info['ram_total'])}", info['ram_pct'])
    if 'frx_ram' in info:
        add("FRX занимает", _size(info['frx_ram']))
    for d in info.get('disks', [])[:3]:
        add(f"Диск {d['mount']}", f"{_size(d['used'])} / {_size(d['total'])}", d['pct'])
    add("Python", f"{info['python']} ({info['py_impl']})")
    add("Часовой пояс", info['tz'])

    pad, rh, top = 40, 58, 150
    H = top + rh * len(rows) + 40
    img = Image.new('RGB', (W, H), (24, 26, 33))
    d = ImageDraw.Draw(img)
    for y in range(H):                                    # мягкий градиент
        k = y / H
        d.line([(0, y), (W, y)], fill=(int(24 + k * 10), int(26 + k * 8), int(33 + k * 14)))
    d.rectangle([0, 0, W, 96], fill=(255, 122, 69))
    d.text((pad, 26), "FRX · система", font=_card_font(40, True), fill=(255, 255, 255))
    d.text((W - pad - d.textlength(info['host'][:30], font=_card_font(22)), 40),
           info['host'][:30], font=_card_font(22), fill=(255, 240, 230))
    y = top
    for label, value, pct in rows:
        lf = _card_font(22, True)
        while len(label) > 4 and d.textlength(label, font=lf) > 285:
            label = label[:-2] + '…'
        d.text((pad, y), label, font=lf, fill=(150, 160, 175))
        vx = pad + 300
        if pct is not None:
            bw, bh = 150, 16
            bx, by = W - pad - bw, y + 6
            d.rounded_rectangle([bx, by, bx + bw, by + bh], 8, fill=(45, 48, 58))
            col = (110, 200, 130) if pct < 70 else (240, 190, 90) if pct < 90 else (235, 100, 90)
            d.rounded_rectangle([bx, by, bx + max(bh, int(bw * pct / 100)), by + bh], 8, fill=col)
            vf = _card_font(24)
            val = str(value)
            while val and d.textlength(val, font=vf) > bx - vx - 20:
                val = val[:-1]
            d.text((vx, y), val, font=vf, fill=(235, 238, 243))
        else:
            d.text((vx, y), str(value), font=_card_font(24), fill=(235, 238, 243))
        y += rh
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.name = 'sysinfo.png'
    buf.seek(0)
    return buf


# --------------------------------------------------------------------------- команды

@decorators.command('sysinfo', 'sys', 'система')
async def sysinfo_cmd(event):
    """сведения о системе: .sysinfo · card — картинкой · proc — топ процессов"""
    arg = engine.get_args().strip().lower()
    if arg in ('proc', 'процессы', 'top'):
        await proc_cmd(event)
        return
    await engine.answer(event, visual.wait("Собираю…"))
    info = await asyncio.to_thread(collect)
    if arg in ('card', 'карточка', 'pic', 'img'):
        try:
            buf = await asyncio.to_thread(card_image, info)
        except Exception as e:
            await engine.answer(event, text_report(info) + "\n" + visual.hint(f"картинка не вышла: {_esc(str(e)[:120])}"))
            return
        await engine.client.send_file(event.chat_id, buf, reply_to=event.reply_to_msg_id, force_document=False)
        try:
            await event.delete()               # команда гостя — не наше сообщение, удалить не выйдет
        except Exception:
            pass
        return
    await engine.answer(event, text_report(info))


async def proc_cmd(event):
    await engine.answer(event, visual.wait("Смотрю процессы…"))
    procs = await asyncio.to_thread(top_processes, 7)
    if procs is None:
        await engine.answer(event, visual.err("Нужен psutil") + "\n" + visual.hint("поставится сам при следующем запуске FRX"))
        return
    out = [visual.title("Процессы", sub="больше всего нагрузки")]
    for name, pid, cpu, rss in procs:
        out.append(f"<code>{cpu:4.0f}%</code> · {_size(rss):>8} · {_esc(name[:34])}")
    await engine.answer(event, "\n".join(out))


@decorators.command('dc', 'datacenter')
async def dc_cmd(event):
    """ваш дата-центр Telegram и задержка ответа сервера"""
    await engine.answer(event, visual.wait("Пингую Telegram…"))
    try:
        dc = engine.client.session.dc_id
    except Exception:
        dc = None
    from telethon.tl.functions import PingRequest
    import secrets
    lats = []
    for _ in range(5):
        t0 = time.monotonic()
        try:
            await engine.client(PingRequest(ping_id=secrets.randbits(63)))
            lats.append((time.monotonic() - t0) * 1000)
        except Exception:
            pass
        await asyncio.sleep(0.15)
    out = [visual.title("Telegram", sub=f"дата-центр {dc}" if dc else "дата-центр")]
    if dc in DC_CITY:
        out.append(visual.field("расположение", f"DC{dc} · {DC_CITY[dc]}", code=False))
    if lats:
        out.append(visual.field("задержка", f"{min(lats):.0f}–{max(lats):.0f} мс (среднее {sum(lats) / len(lats):.0f})", code=False))
    else:
        out.append(visual.err("Telegram не ответил на пинг"))
    await engine.answer(event, "\n".join(out))
