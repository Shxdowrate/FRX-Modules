"""
Terminal — командная строка компьютера, на котором запущен FRX, прямо из Telegram. Windows, Linux, macOS, Termux.

  .t <команда>        выполнить (алиас .term). Вывод обновляется на лету, кнопка «⏹ Стоп» — остановить.
                      Длинный вывод целиком приходит файлом в конце.
  .tin <текст>        отправить строку на ввод запущенной команде (ответы на «Continue? [y/N]» и т. п.);
                      ответом на сообщение команды — именно ей, иначе — последней в этом чате
  .tkill              остановить команду (ответом — эту, иначе последнюю в чате);  .tkill all — все
  .tps                что сейчас выполняется
  .tcd [папка]        рабочая папка (запоминается); без аргумента — показать;  .tcd ~ — домашняя;  .tcd - — прошлая
  .tsh [оболочка]     auto · bash · sh · zsh · fish · pwsh · powershell · cmd;  без аргумента — какая сейчас и какие есть
  .thist              последние команды (кнопкой — повторить)

Оболочка по умолчанию: Windows — PowerShell (pwsh, если установлен), Linux/macOS — ваш $SHELL или bash/sh.
Вывод в Windows читается в UTF-8 (cmd — chcp 65001, PowerShell — OutputEncoding), кракозябр нет.
Только для владельца: гостям с доступом (.access) модуль команды не выполнит. Команда «cd …» сама по себе меняет
рабочую папку модуля (как в обычном терминале).
"""

import asyncio
import html
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import db
import decorators
import engine
import files
import visual

NAME = "Terminal"
MODULE_NAME = "terminal"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Командная строка Windows / Linux / macOS из Telegram: живой вывод, стоп, ввод, рабочая папка, история"

IS_WIN = os.name == 'nt'
SHELLS = ['auto', 'bash', 'sh', 'zsh', 'fish', 'pwsh', 'powershell', 'cmd']
ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]')

_JOBS = {}            # id -> Job
_NEXT = [0]


def _esc(x):
    return html.escape(str(x), quote=False)


# --------------------------------------------------------------------------- настройки

@decorators.on_start()
async def setup():
    c = engine.create_config
    await c(key='shell', description='Оболочка: auto · bash · sh · zsh · fish · pwsh · powershell · cmd', data_type='choice',
            default='auto', options=SHELLS)
    await c(key='timeout', description='Остановить команду через столько секунд (0 — не ограничивать)', data_type='int',
            default=900, valid={'min_value': 0, 'max_value': 86400})
    await c(key='interval', description='Как часто обновлять вывод, сек.', data_type='float', default=1.5,
            valid={'min_value': 0.8, 'max_value': 10})
    await c(key='tail', description='Сколько последних строк показывать в сообщении', data_type='int', default=40,
            valid={'min_value': 5, 'max_value': 200})
    await c(key='file_over', description='Присылать вывод файлом, если он длиннее N символов', data_type='int', default=3500,
            valid={'min_value': 500, 'max_value': 100000})


@decorators.on_unload()
async def cleanup():
    for job in list(_JOBS.values()):
        job.kill()


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


def _owner_only():
    return engine.guest_id() is None


# --------------------------------------------------------------------------- оболочка и папка

def available_shells():
    found = []
    for name in SHELLS[1:]:
        exe = {'powershell': 'powershell', 'pwsh': 'pwsh', 'cmd': 'cmd'}.get(name, name)
        if shutil.which(exe):
            found.append(name)
    return found


def resolve_shell(choice):
    if choice and choice != 'auto':
        return choice
    if IS_WIN:
        return 'pwsh' if shutil.which('pwsh') else 'powershell'
    env = os.path.basename(os.environ.get('SHELL', '') or '')
    if env in ('bash', 'zsh', 'fish', 'sh') and shutil.which(env):
        return env
    return 'bash' if shutil.which('bash') else 'sh'


def build_argv(shell, command):
    if shell == 'cmd':
        return ['cmd', '/d', '/s', '/c', f'chcp 65001>nul & {command}']
    if shell in ('powershell', 'pwsh'):
        pre = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; "
               "$ProgressPreference='SilentlyContinue'; ")
        return [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', pre + command]
    exe = shutil.which(shell) or shell
    return [exe, '-c', command]


def decode(data: bytes) -> str:
    for enc in ('utf-8', 'cp866' if IS_WIN else 'latin-1', 'cp1251'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', 'replace')


async def get_cwd():
    p = await db.get('cwd', None)
    if p and Path(p).is_dir():
        return p
    return str(Path.home())


def expand(path, cwd):
    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"').strip("'")))
    p = Path(path)
    if not p.is_absolute():
        p = Path(cwd) / p
    return str(p.resolve())


# --------------------------------------------------------------------------- задача

class Job:
    def __init__(self, command, cwd, shell, chat_id):
        _NEXT[0] += 1
        self.id = _NEXT[0]
        self.command = command
        self.cwd = cwd
        self.shell = shell
        self.chat_id = chat_id
        self.msg_id = None
        self.out = bytearray()
        self.proc = None
        self.code = None
        self.started = time.time()
        self.done = asyncio.Event()
        self.killed = False
        self._threaded = False

    async def start(self):
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1', TERM='dumb', NO_COLOR='1', CLICOLOR='0')
        if sys.platform.startswith('linux') and not env.get('LANG'):
            env['LANG'] = 'C.UTF-8'
        argv = build_argv(self.shell, self.command)
        kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.cwd, env=env)
        if IS_WIN:
            kw['creationflags'] = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        else:
            kw['start_new_session'] = True
        try:
            self.proc = await asyncio.create_subprocess_exec(*argv, **kw)
            asyncio.ensure_future(self._pump())
        except NotImplementedError:                    # цикл событий без подпроцессов — читаем в потоке
            self._threaded = True
            self.proc = subprocess.Popen(argv, **kw)
            loop = asyncio.get_running_loop()
            threading.Thread(target=self._pump_thread, args=(loop,), daemon=True).start()

    async def _pump(self):
        try:
            while True:
                chunk = await self.proc.stdout.read(4096)
                if not chunk:
                    break
                self.out += chunk
                if len(self.out) > 8_000_000:
                    del self.out[:len(self.out) - 6_000_000]
            self.code = await self.proc.wait()
        finally:
            self.done.set()

    def _pump_thread(self, loop):
        try:
            for chunk in iter(lambda: self.proc.stdout.read1(4096) if hasattr(self.proc.stdout, 'read1')
                              else self.proc.stdout.read(4096), b''):
                self.out += chunk
            self.code = self.proc.wait()
        finally:
            loop.call_soon_threadsafe(self.done.set)

    def text(self):
        return ANSI.sub('', decode(bytes(self.out))).replace('\r\n', '\n')

    def running(self):
        return not self.done.is_set()

    def write(self, line):
        if not self.running() or self.proc is None or self.proc.stdin is None:
            return False
        data = (line + ('\r\n' if IS_WIN else '\n')).encode('utf-8')
        try:
            self.proc.stdin.write(data)
            if not self._threaded:
                asyncio.ensure_future(self.proc.stdin.drain())
            else:
                self.proc.stdin.flush()
            return True
        except Exception:
            return False

    def kill(self):
        if self.proc is None or not self.running():
            return
        self.killed = True
        pid = self.proc.pid
        try:
            if IS_WIN:
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception:
                    self.proc.terminate()

                def hard():
                    time.sleep(3)
                    if self.running():
                        try:
                            os.killpg(os.getpgid(pid), signal.SIGKILL)
                        except Exception:
                            pass
                threading.Thread(target=hard, daemon=True).start()
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _dur(sec):
    sec = int(sec)
    return f"{sec // 60} мин {sec % 60} с" if sec >= 60 else f"{sec} с"


async def render(job, final=False):
    tail = int(await cfg('tail', 40))
    text = job.text().rstrip('\n')
    lines = text.split('\n') if text else []
    shown = lines[-tail:]
    body = '\n'.join(shown)
    if len(body) > 3300:
        body = '…' + body[-3300:]
    head = f"⌘ <b>{_esc(job.shell)}</b> · <code>{_esc(_short_path(job.cwd))}</code>\n<code>$ {_esc(job.command[:300])}</code>"
    if final:
        if job.killed:
            status = f"⏹ остановлено · {_dur(time.time() - job.started)}"
        else:
            mark = '✓' if job.code == 0 else '✗'
            status = f"{mark} код {job.code} · {_dur(time.time() - job.started)}"
    else:
        status = f"⟳ выполняется · {_dur(time.time() - job.started)}"
    more = f"\n<i>… ещё {len(lines) - len(shown)} строк выше</i>" if len(lines) > len(shown) else ''
    return f"{head}{more}\n<pre>{_esc(body) if body else '(пусто)'}</pre>\n{status}"


def _short_path(p):
    home = str(Path.home())
    return '~' + p[len(home):] if p.startswith(home) else p


# --------------------------------------------------------------------------- команды

@decorators.command('t', 'term', 'терм')
async def t_cmd(event):
    """выполнить команду в терминале: <команда>"""
    if not _owner_only():
        await engine.answer(event, visual.err("Терминал — только для владельца"))
        return
    command = engine.get_args().strip()
    if not command and event.is_reply:
        command = ((await event.get_reply_message()).raw_text or '').strip()
    if not command:
        await engine.answer(event, visual.err("Что выполнить? .t <команда>") + "\n"
                            + visual.hint(".tcd — папка · .tsh — оболочка · .thist — история"))
        return
    cwd = await get_cwd()
    m = re.fullmatch(r'\s*cd(?:\s+(.+))?\s*', command)
    if m:                                               # «cd папка» — меняем рабочую папку модуля
        await _cd(event, m.group(1) or '~')
        return
    hist = await db.get('history', []) or []
    hist = [h for h in hist if h != command][-29:] + [command]
    await db.set('history', hist)
    shell = resolve_shell(await cfg('shell', 'auto'))
    await run_job(event, command, cwd, shell)


async def run_job(event, command, cwd, shell):
    job = Job(command, cwd, shell, event.chat_id)
    try:
        await job.start()
    except FileNotFoundError:
        await engine.answer(event, visual.err(f"Оболочка «{_esc(shell)}» не найдена") + "\n" + visual.hint(".tsh — выбрать другую"))
        return
    except Exception as e:
        await engine.answer(event, visual.err(f"Не запустилось: {_esc(e)}"))
        return
    _JOBS[job.id] = job
    form = None
    try:
        form = await engine.create_inlineform(event.chat_id, await render(job), buttons=[[("⏹ Стоп", f"tk:{job.id}", "danger")]])
        await event.delete()

        @form.on_click(data=f"tk:{job.id}")
        async def _(click):
            job.kill()
            await click.answer("Останавливаю…")
    except Exception:
        form = None
        await engine.answer(event, await render(job))
        job.msg_id = event.id

    async def show(text, buttons=None):
        try:
            if form is not None:
                await form.edit(text, buttons=buttons)
            else:
                await engine.answer(event, text)
        except Exception:
            pass

    interval = float(await cfg('interval', 1.5))
    limit = int(await cfg('timeout', 900))
    last = None
    try:
        while job.running():
            try:
                await asyncio.wait_for(job.done.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if limit and time.time() - job.started > limit and job.running():
                job.kill()
            if job.running():
                text = await render(job)
                if text != last:
                    await show(text, [[("⏹ Стоп", f"tk:{job.id}", "danger")]])
                    last = text
        final = await render(job, final=True)
        if limit and time.time() - job.started > limit and job.killed:
            final += f"\n<i>остановлено по таймауту {limit} с (.cfg terminal)</i>"
        await show(final, None)
        if form is not None:
            try:
                await form.close(remove_buttons=False)
            except Exception:
                pass
        full = job.text()
        if len(full) > int(await cfg('file_over', 3500)):
            obj = await files.create_file('text/plain', f"terminal_{int(job.started)}.txt",
                                          f"$ {job.command}\n# {job.cwd} · {job.shell} · код {job.code}\n\n{full}")
            await engine.send_message(event.chat_id, visual.hint(f"полный вывод · {len(full)} символов"), file=obj['path'])
    finally:
        _JOBS.pop(job.id, None)


def _job_for(event, reply):
    jobs = [j for j in _JOBS.values() if j.chat_id == event.chat_id and j.running()]
    if reply is not None:
        for j in jobs:
            if j.msg_id == reply.id:
                return j
    return jobs[-1] if jobs else None


@decorators.command('tin')
async def tin_cmd(event):
    """отправить строку на ввод запущенной команде: <текст>"""
    if not _owner_only():
        return
    reply = await event.get_reply_message() if event.is_reply else None
    job = _job_for(event, reply)
    if job is None:
        await engine.answer(event, visual.err("В этом чате ничего не выполняется"))
        return
    ok = job.write(engine.get_args())
    await event.delete() if ok else await engine.answer(event, visual.err("Команда не принимает ввод"))


@decorators.command('tkill')
async def tkill_cmd(event):
    """остановить команду (ответом — эту, иначе последнюю в чате); all — все"""
    if not _owner_only():
        return
    if engine.get_args().strip().lower() in ('all', 'все'):
        n = 0
        for j in list(_JOBS.values()):
            if j.running():
                j.kill()
                n += 1
        await engine.answer(event, visual.ok(f"Остановлено: {n}"))
        return
    reply = await event.get_reply_message() if event.is_reply else None
    job = _job_for(event, reply)
    if job is None:
        await engine.answer(event, visual.err("В этом чате ничего не выполняется"))
        return
    job.kill()
    await engine.answer(event, visual.ok(f"Останавливаю: {_esc(job.command[:80])}"))


@decorators.command('tps')
async def tps_cmd(event):
    """что сейчас выполняется"""
    if not _owner_only():
        return
    jobs = [j for j in _JOBS.values() if j.running()]
    lines = [visual.title("Terminal", sub=f"выполняется: {len(jobs)}")]
    for j in jobs:
        lines.append(visual.item(f"#{j.id} <code>{_esc(j.command[:80])}</code> · {_dur(time.time() - j.started)}"
                                 f" · pid {getattr(j.proc, 'pid', '?')}"))
    if not jobs:
        lines.append(visual.hint("ничего"))
    await engine.answer(event, "\n".join(lines))


async def _cd(event, arg):
    cwd = await get_cwd()
    arg = (arg or '').strip()
    if arg == '-':
        target = await db.get('prev_cwd', None) or cwd
    else:
        target = expand(arg or '~', cwd)
    if not Path(target).is_dir():
        await engine.answer(event, visual.err(f"Нет такой папки: <code>{_esc(target)}</code>"))
        return
    await db.set('prev_cwd', cwd)
    await db.set('cwd', target)
    await engine.answer(event, visual.ok("Рабочая папка") + f"\n<code>{_esc(target)}</code>")


@decorators.command('tcd')
async def tcd_cmd(event):
    """рабочая папка: [путь]; ~ — домашняя, - — прошлая; без аргумента — показать"""
    if not _owner_only():
        return
    arg = engine.get_args().strip()
    if not arg:
        cwd = await get_cwd()
        try:
            entries = sorted(Path(cwd).iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:30]
        except Exception:
            entries = []
        lines = [visual.title("Terminal", sub="рабочая папка"), f"<code>{_esc(cwd)}</code>", ""]
        lines += [("📁 " if p.is_dir() else "▤ ") + _esc(p.name) for p in entries]
        await engine.answer(event, "\n".join(lines))
        return
    await _cd(event, arg)


@decorators.command('tsh')
async def tsh_cmd(event):
    """оболочка: auto · bash · sh · zsh · fish · pwsh · powershell · cmd; без аргумента — текущая и доступные"""
    if not _owner_only():
        return
    a = engine.get_args().strip().lower()
    if a:
        if a not in SHELLS:
            await engine.answer(event, visual.err("Есть: " + " · ".join(SHELLS)))
            return
        if a != 'auto' and a not in available_shells():
            await engine.answer(event, visual.err(f"«{_esc(a)}» на этом компьютере не найдена"))
            return
        await engine.edit_config('shell', 'edit', a)
    cur = await cfg('shell', 'auto')
    await engine.answer(event, "\n".join([
        visual.title("Terminal", sub="оболочка"),
        visual.field("выбрана", cur + (f" → {resolve_shell(cur)}" if cur == 'auto' else '')),
        visual.field("система", 'Windows' if IS_WIN else sys.platform),
        visual.field("есть", " · ".join(available_shells()) or '—', code=False)]))


@decorators.command('thist')
async def thist_cmd(event):
    """последние команды; кнопкой — повторить"""
    if not _owner_only():
        return
    hist = list(reversed(await db.get('history', []) or []))[:15]
    if not hist:
        await engine.answer(event, visual.hint("История пуста"))
        return
    text = visual.title("Terminal", sub="история") + "\n" + "\n".join(f"{i + 1}. <code>{_esc(h[:120])}</code>"
                                                                       for i, h in enumerate(hist))
    try:
        form = await engine.create_inlineform(event.chat_id, text, timeout=600,
                                              buttons=[[(f"⟳ {i + 1}. {h[:40]}", f"th:{i}")] for i, h in enumerate(hist[:10])]
                                              + [[(visual.btn('Закрыть', 'close'), "th:x")]])
    except Exception:
        await engine.answer(event, text)
        return
    await event.delete()

    @form.on_click(data_startswith='th:')
    async def _(click):
        d = click.data[3:]
        await form.delete()
        if d == 'x':
            return
        cmd = hist[int(d)]
        msg = await engine.client.send_message(event.chat_id, f"{await engine.get_prefix()}t {cmd}")
        from telethon import events
        ev = events.NewMessage.Event(msg)
        ev._set_client(engine.client)
        await engine._on_new_message(ev)


@decorators.on_error()
async def _errors(error_text, context):
    if context.get('module') == MODULE_NAME:
        engine.botlog(error_text, level='error', function=context.get('function'))
