"""
ChatExport — выгрузка переписки в архив: читаемая HTML-страница + JSON + все файлы. Всё приходит в «Избранное».

  .export                 весь этот чат
  .export 1000            последние 1000 сообщений
  .export (ответом)       от этого сообщения и до конца
  .export @chat [N]       другой чат (по @username, id или ссылке)
  .export … nomedia       без файлов — только текст (быстро)
  .exportstop             остановить текущую выгрузку

Большой чат режется на части: каждая — отдельный ZIP со своей страницей и своими файлами (сообщения с … по …),
поэтому любую часть можно открыть отдельно. Размер части — part_mb (по умолчанию 1900 МБ, лимит Telegram — 2 ГБ).
Файлы больше max_file_mb не скачиваются — в странице будет пометка. Временные файлы удаляются сразу после отправки.
"""

import asyncio
import html
import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

import decorators
import engine
import visual

NAME = "ChatExport"
MODULE_NAME = "chatexport"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Выгрузка чата в Избранное: HTML + JSON + файлы, большой чат — частями"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/chatexport.py"

RUN = {'job': None}

CSS = """
*{box-sizing:border-box}body{margin:0;background:#0e1621;color:#e9eef3;font:15px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
header{position:sticky;top:0;background:#17212b;padding:14px 20px;border-bottom:1px solid #0b1118;z-index:2}
header h1{margin:0;font-size:18px}header p{margin:2px 0 0;color:#7f91a4;font-size:13px}
main{max-width:860px;margin:0 auto;padding:16px 12px 60px}
.day{text-align:center;margin:18px 0 8px}.day span{background:#1e2c3a;color:#aab8c5;border-radius:12px;padding:3px 12px;font-size:13px}
.m{display:flex;gap:10px;margin:4px 0;padding:2px 4px;border-radius:8px}.m:target{background:#2b5278aa}
.av{width:36px;height:36px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;font-weight:600;color:#fff}
.b{background:#182533;border-radius:12px;padding:7px 11px;max-width:min(640px,100%);overflow-wrap:anywhere}
.m.out .b{background:#2b5278}.n{font-weight:600;font-size:14px;margin-bottom:2px}.t{color:#6d7f8f;font-size:12px;margin-left:8px;font-weight:400}
.r{border-left:3px solid #6ab3f3;padding:2px 8px;margin:2px 0 5px;color:#aab8c5;font-size:13px;display:block;text-decoration:none}
.f{color:#7f91a4;font-size:13px;margin-bottom:3px}.svc{text-align:center;color:#7f91a4;font-size:13px;margin:8px 0}
.b img,.b video{max-width:100%;max-height:420px;border-radius:8px;display:block;margin:4px 0}.b audio{width:100%;margin:4px 0}
.file{display:inline-block;background:#0e1621;border-radius:8px;padding:6px 10px;margin:4px 0;color:#6ab3f3;text-decoration:none}
.skip{color:#e0a060;font-size:13px}a{color:#6ab3f3}pre,code{background:#0e1621;border-radius:5px;padding:1px 4px;font-family:Consolas,monospace}
pre{padding:8px;white-space:pre-wrap}.sp{background:#556;color:transparent;border-radius:3px}.sp:hover{color:inherit;background:none}
blockquote{border-left:3px solid #6ab3f3;margin:4px 0;padding:0 8px}
"""
COLORS = ['#e17076', '#faa774', '#a695e7', '#7bc862', '#6ec9cb', '#65aadd', '#ee7aae']


def _esc(x):
    return html.escape(str(x), quote=True)


@decorators.on_start()
async def setup():
    await engine.create_config(key='part_mb', description='Размер одной части архива, МБ (лимит Telegram — 2000, с Premium — 4000)',
                               data_type='int', default=1900, valid={'min_value': 50, 'max_value': 3900})
    await engine.create_config(key='max_file_mb', description='Файлы больше этого не скачивать (в выгрузке будет пометка), МБ',
                               data_type='int', default=500, valid={'min_value': 1, 'max_value': 3900})


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


def _size(n):
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if n < 1024 or unit == 'ГБ':
            return f"{n:.0f} {unit}" if unit == 'Б' else f"{n:.1f} {unit}"
        n /= 1024


def tmp_root():
    try:
        from core.paths import DATA_DIR
        d = Path(DATA_DIR) / 'cache' / 'export'
    except Exception:
        d = Path('data') / 'cache' / 'export'
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe(name, n=60):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', str(name)).strip(' .')[:n] or 'chat'


def _who(ent):
    if ent is None:
        return 'Удалённый аккаунт'
    return getattr(ent, 'title', None) or ' '.join(x for x in (getattr(ent, 'first_name', '') or '',
                                                                getattr(ent, 'last_name', '') or '') if x) or 'Без имени'


def _service(msg):
    a = msg.action
    name = type(a).__name__.replace('MessageAction', '')
    return {'ChatAddUser': 'добавил(а) участника', 'ChatDeleteUser': 'покинул(а) чат или удалён(а)',
            'ChatJoinedByLink': 'вступил(а) по ссылке', 'ChatJoinedByRequest': 'вступил(а) по заявке',
            'ChatEditTitle': f"сменил(а) название на «{getattr(a, 'title', '')}»", 'ChatEditPhoto': 'сменил(а) фото чата',
            'ChatDeletePhoto': 'удалил(а) фото чата', 'PinMessage': 'закрепил(а) сообщение', 'ChatCreate': 'создал(а) группу',
            'ChannelCreate': 'создал(а) канал', 'GroupCall': 'начал(а) или завершил(а) звонок',
            'PhoneCall': 'звонок', 'ChatMigrateTo': 'группа стала супергруппой'}.get(name, name)


class Part:
    def __init__(self, root, n):
        self.n = n
        self.dir = root / f"part{n}"
        (self.dir / 'files').mkdir(parents=True, exist_ok=True)
        self.msgs = []
        self.size = 0
        self.files = 0


class Job:
    def __init__(self, event, chat, title):
        self.event, self.chat, self.title = event, chat, title
        self.stop = False
        self.count = self.files = self.skipped = self.bytes = 0
        self.parts_sent = 0
        self.t0 = time.time()
        self.last_status = 0.0
        self.root = tmp_root() / f"{int(time.time())}_{abs(hash(chat)) % 10000}"

    async def status(self, text, force=False):
        now = time.time()
        if force or now - self.last_status > 3:
            self.last_status = now
            try:
                await engine.answer(self.event, visual.wait(text) + "\n" + visual.hint(
                    f"сообщений {self.count} · файлов {self.files} ({_size(self.bytes)}) · частей отправлено {self.parts_sent}"
                    + " · .exportstop — остановить"))
            except Exception:
                pass


def message_record(m, part, fname, skipped_note):
    from telethon.extensions import html as tl_html
    s = m.sender
    rec = {'id': m.id, 'date': m.date.isoformat() if m.date else None, 'from_id': m.sender_id, 'from': _who(s) if s or m.sender_id else None,
           'out': bool(m.out), 'text': m.raw_text or '', 'reply_to': m.reply_to_msg_id,
           'html': tl_html.unparse(m.message or '', m.entities or []) if m.message else ''}
    if m.fwd_from:
        rec['forwarded_from'] = getattr(m.fwd_from, 'from_name', None) or (str(getattr(m.fwd_from.from_id, 'user_id', '') or
                                                                             getattr(m.fwd_from.from_id, 'channel_id', '') or '') if m.fwd_from.from_id else None)
        if m.forward and getattr(m.forward, 'sender', None) is not None:
            rec['forwarded_from'] = _who(m.forward.sender)
        elif m.forward and getattr(m.forward, 'chat', None) is not None:
            rec['forwarded_from'] = _who(m.forward.chat)
    if m.action is not None:
        rec['service'] = _service(m)
    if m.media is not None:
        kind = ('photo' if m.photo else 'sticker' if m.sticker else 'voice' if m.voice else 'video_note' if m.video_note
                else 'gif' if m.gif else 'video' if m.video else 'audio' if m.audio else 'poll' if m.poll
                else 'location' if m.geo else 'contact' if m.contact else 'document' if m.document else 'other')
        rec['media'] = {'type': kind}
        if fname:
            rec['media']['file'] = f"files/{fname}"
        if skipped_note:
            rec['media']['skipped'] = skipped_note
        if m.file and m.file.name:
            rec['media']['name'] = m.file.name
        if m.poll:
            q = m.poll.poll.question
            rec['media']['question'] = getattr(q, 'text', q)
            rec['media']['answers'] = [getattr(a.text, 'text', a.text) for a in m.poll.poll.answers]
        if m.geo:
            rec['media']['geo'] = [m.geo.lat, m.geo.long]
    return rec


def render_html(title, part, total_hint=''):
    out = [f"<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>{_esc(title)}</title><style>{CSS}</style><header><h1>{_esc(title)}</h1>"]
    if part.msgs:
        a, b = part.msgs[0], part.msgs[-1]
        out.append(f"<p>Часть {part.n}{total_hint} · {len(part.msgs)} сообщений · {(a['date'] or '')[:10]} — {(b['date'] or '')[:10]}"
                   f" · выгружено FRX</p>")
    out.append("</header><main>")
    ids = {r['id'] for r in part.msgs}
    byid = {r['id']: r for r in part.msgs}
    day = None
    prev_from = None
    for r in part.msgs:
        d = (r['date'] or '')[:10]
        if d != day:
            day, prev_from = d, None
            out.append(f"<div class=day><span>{_esc(d)}</span></div>")
        if r.get('service'):
            out.append(f"<div class=svc id=m{r['id']}>{_esc(r.get('from') or '')} {_esc(r['service'])}</div>")
            prev_from = None
            continue
        name = r.get('from') or title
        col = COLORS[abs(r.get('from_id') or 0) % 7]
        same = prev_from == r.get('from_id')
        prev_from = r.get('from_id')
        av = '' if same else ''.join(w[:1] for w in name.split()[:2]).upper()
        out.append(f"<div class='m{' out' if r['out'] else ''}' id=m{r['id']}>"
                   f"<div class=av style='background:{col if not same else 'transparent'}'>{_esc(av)}</div><div class=b>")
        if not same:
            out.append(f"<div class=n style='color:{col}'>{_esc(name)}<span class=t>{(r['date'] or '')[11:16]}</span></div>")
        else:
            out.append(f"<div class=t style='margin:0 0 2px'>{(r['date'] or '')[11:16]}</div>")
        if r.get('forwarded_from'):
            out.append(f"<div class=f>↪ переслано от {_esc(r['forwarded_from'])}</div>")
        if r.get('reply_to'):
            rt = byid.get(r['reply_to'])
            snippet = (rt['text'][:80] if rt and rt['text'] else 'сообщение') if rt else 'сообщение из другой части'
            href = f"#m{r['reply_to']}" if r['reply_to'] in ids else '#'
            out.append(f"<a class=r href='{href}'>{_esc((rt or {}).get('from') or '')} {_esc(snippet)}</a>")
        md = r.get('media')
        if md:
            f = md.get('file')
            t = md['type']
            if f and t in ('photo', 'sticker') or (f and f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))):
                out.append(f"<a href='{_esc(f)}'><img loading=lazy src='{_esc(f)}'></a>")
            elif f and t in ('video', 'gif', 'video_note'):
                extra = ' autoplay loop muted' if t == 'gif' else ''
                out.append(f"<video controls preload=none{extra} src='{_esc(f)}'></video>")
            elif f and t in ('voice', 'audio'):
                out.append(f"<audio controls preload=none src='{_esc(f)}'></audio>")
            elif f:
                out.append(f"<a class=file href='{_esc(f)}'>📎 {_esc(md.get('name') or os.path.basename(f))}</a>")
            elif t == 'poll':
                out.append(f"<div>📊 <b>{_esc(md.get('question'))}</b><br>" + "<br>".join("• " + _esc(a) for a in md.get('answers', [])) + "</div>")
            elif t == 'location' and md.get('geo'):
                la, lo = md['geo']
                out.append(f"<a class=file href='https://maps.google.com/?q={la},{lo}'>📍 {la:.5f}, {lo:.5f}</a>")
            if md.get('skipped'):
                out.append(f"<div class=skip>{_esc(md['skipped'])}</div>")
        if r.get('html'):
            body = r['html'].replace('\n', '<br>').replace('<spoiler>', "<span class=sp>").replace('</spoiler>', '</span>')
            out.append(f"<div>{body}</div>")
        out.append("</div></div>")
    out.append("</main></html>")
    return ''.join(out)


async def flush(job, part, last=False):
    """Часть → ZIP → в Избранное → удалить с диска."""
    if not part.msgs:
        shutil.rmtree(part.dir, ignore_errors=True)
        return
    (part.dir / 'messages.html').write_text(render_html(job.title, part), encoding='utf-8')
    (part.dir / 'messages.json').write_text(json.dumps({'chat': job.title, 'part': part.n, 'messages': part.msgs},
                                                       ensure_ascii=False, indent=1), encoding='utf-8')
    name = f"{safe(job.title, 40)}_{time.strftime('%Y-%m-%d')}_part{part.n}.zip"
    zpath = part.dir.parent / name

    def make():
        with zipfile.ZipFile(zpath, 'w', allowZip64=True) as z:
            for p in sorted(part.dir.rglob('*')):
                if p.is_file():
                    comp = zipfile.ZIP_DEFLATED if p.suffix in ('.html', '.json') else zipfile.ZIP_STORED
                    z.write(p, p.relative_to(part.dir).as_posix(), compress_type=comp)
    await job.status(f"Упаковываю часть {part.n}…", True)
    await asyncio.to_thread(make)
    shutil.rmtree(part.dir, ignore_errors=True)
    a, b = part.msgs[0], part.msgs[-1]
    cap = (f"📦 <b>{_esc(job.title)}</b> · часть {part.n}\n{len(part.msgs)} сообщений · {part.files} файлов · "
           f"{(a['date'] or '')[:10]} — {(b['date'] or '')[:10]}\n<i>откройте messages.html из архива</i>")
    size = zpath.stat().st_size

    async def prog(cur, total):
        await job.status(f"Отправляю часть {part.n} в Избранное: {_size(cur)} из {_size(total)}")
    try:
        await engine.client.send_file('me', str(zpath), caption=cap, parse_mode='html', force_document=True,
                                      progress_callback=prog if size > 20 * 1024 * 1024 else None)
        job.parts_sent += 1
    finally:
        zpath.unlink(missing_ok=True)


async def run_export(job, limit, with_media, from_id):
    client = engine.client
    part_limit = int(await cfg('part_mb', 1900)) * 1024 * 1024
    max_file = int(await cfg('max_file_mb', 500)) * 1024 * 1024
    part = Part(job.root, 1)
    sem = asyncio.Semaphore(4)

    async def fetch(m):
        """→ (имя файла | None, пометка о пропуске | None)"""
        if not with_media or m.media is None or not (m.file or m.photo) or m.poll or m.geo or m.contact:
            return None, None
        size = getattr(m.file, 'size', 0) or 0
        if size > max_file:
            job.skipped += 1
            return None, f"файл {_size(size)} не скачан (больше max_file_mb)"
        if size > part_limit:
            job.skipped += 1
            return None, "файл больше части архива — не скачан"
        ext = (m.file.ext if m.file else None) or ('.jpg' if m.photo else '')
        base = safe(os.path.splitext(m.file.name)[0], 40) if (m.file and m.file.name) else type(m.media).__name__.replace('MessageMedia', '').lower()
        fname = f"{m.id}_{base}{ext}"
        async with sem:
            try:
                await client.download_media(m, file=str(part.dir / 'files' / fname))
            except Exception as e:
                job.skipped += 1
                return None, f"не скачалось: {str(e)[:80]}"
        p = part.dir / 'files' / fname
        if not p.is_file():
            return None, None
        job.files += 1
        job.bytes += p.stat().st_size
        part.files += 1
        part.size += p.stat().st_size
        return fname, None

    if limit:
        msgs = [m async for m in client.iter_messages(job.chat, limit=limit)]
        msgs.reverse()

        async def source():
            for m in msgs:
                yield m
        it = source()
    else:
        it = client.iter_messages(job.chat, reverse=True, min_id=(from_id - 1) if from_id else 0)

    batch = []

    async def process(batch):
        nonlocal part
        results = await asyncio.gather(*(fetch(m) for m in batch))
        for m, (fname, note) in zip(batch, results):
            rec = message_record(m, part, fname, note)
            part.msgs.append(rec)
            part.size += len(rec['html'] or rec['text']) * 3 + 300
            job.count += 1
        if part.size >= part_limit * 0.92:
            await flush(job, part)
            part = Part(job.root, part.n + 1)

    async for m in it:
        if job.stop:
            break
        if getattr(m, 'id', None) == job.event.id:
            continue
        batch.append(m)
        size_in_batch = sum((getattr(x.file, 'size', 0) or 0) for x in batch if x.file)
        if len(batch) >= 40 or part.size + size_in_batch >= part_limit * 0.9:
            await process(batch)
            batch = []
            await job.status(f"Выгружаю «{job.title}»…")
    if batch and not job.stop:
        await process(batch)
    await flush(job, part, last=True)                  # и при остановке — уже собранное не пропадает


@decorators.command('export', 'экспорт', 'выгрузка')
async def export_cmd(event):
    """выгрузить чат в Избранное (HTML + JSON + файлы): [N] последних · ответом — от сообщения · [@чат] · [nomedia]"""
    if RUN['job'] is not None:
        await engine.answer(event, visual.err("Уже идёт выгрузка — дождитесь или .exportstop"))
        return
    args = engine.get_args().split()
    with_media = not any(a.lower() in ('nomedia', 'безфайлов', 'text', 'текст') for a in args)
    limit = next((int(a) for a in args if a.isdigit() and int(a) < 10 ** 7), None)
    target = next((a for a in args if a.startswith('@') or a.startswith('https://t.me/') or re.fullmatch(r'-100\d+|-\d+', a)), None)
    try:
        if target:
            ent = await engine.client.get_entity(int(target) if target.lstrip('-').isdigit() else target)
        else:
            ent = await event.get_chat()
    except Exception as e:
        await engine.answer(event, visual.err("Чат не найден") + f"\n<code>{_esc(str(e)[:200])}</code>")
        return
    from telethon.utils import get_peer_id
    chat = get_peer_id(ent)
    title = 'Избранное' if getattr(ent, 'is_self', False) else _who(ent)
    from_id = event.reply_to_msg_id if (event.is_reply and not target) else None
    job = Job(event, chat, title)
    RUN['job'] = job
    await job.status(f"Выгружаю «{title}»…", True)
    try:
        await run_export(job, limit, with_media, from_id)
    except Exception as e:
        engine.tlog(f"[chatexport] {e!r}")
        await engine.answer(event, visual.err("Выгрузка прервалась") + f"\n<code>{_esc(str(e)[:300])}</code>"
                            + (f"\n{visual.hint(f'отправлено частей: {job.parts_sent}')}" if job.parts_sent else ''))
        return
    finally:
        RUN['job'] = None
        shutil.rmtree(job.root, ignore_errors=True)
    took = int(time.time() - job.t0)
    head = visual.warn("Выгрузка остановлена") if job.stop else visual.ok(f"«{_esc(title)}» выгружен в Избранное")
    summary = (f"{head}\n" + visual.field("сообщений", job.count, code=False) + "\n"
               + visual.field("файлов", f"{job.files} · {_size(job.bytes)}", code=False) + "\n"
               + visual.field("частей", job.parts_sent, code=False)
               + (("\n" + visual.field("не скачано", job.skipped, code=False)) if job.skipped else '')
               + "\n" + visual.hint(f"за {took // 60} мин {took % 60} с"))
    await engine.answer(event, summary)
    if event.chat_id != (await engine.client.get_me()).id:
        await engine.client.send_message('me', summary, parse_mode='html')


@decorators.command('exportstop', 'стопэкспорт')
async def exportstop_cmd(event):
    """остановить текущую выгрузку (уже отправленные части останутся)"""
    job = RUN['job']
    if job is None:
        await engine.answer(event, visual.hint("Сейчас ничего не выгружается"))
        return
    job.stop = True
    await engine.answer(event, visual.ok("Останавливаю…"))
