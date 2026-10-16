"""
PollPlot — результаты опроса картинкой: столбцы или «пончик», проценты, победитель, правильный ответ викторины.

  .pp                 ответом на опрос — картинка-график (алиас .pollplot)
  .pp pie             то же «пончиком»
  .pp live [часы]     картинка сама обновляется каждые 5 минут (по умолчанию 24 часа; .pp stop — ответом — хватит)
  .pp voters          кто за что голосовал — для открытых (неанонимных) опросов, файлом CSV и списком
  .pp text            результаты текстом с полосками

Если результаты скрыты, пока вы не проголосуете, — проголосуйте и повторите.
"""

import asyncio
import html
import inspect
import io
import time
from pathlib import Path

import decorators
import engine
import files
import visual

NAME = "PollPlot"
MODULE_NAME = "pollplot"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Результаты опроса графиком (столбцы или пончик), живое обновление, кто за что голосовал"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/pollplot.py"

PALETTE = [(255, 122, 69), (90, 160, 255), (120, 210, 120), (240, 190, 70), (190, 120, 240), (70, 200, 200),
           (240, 100, 150), (160, 170, 185), (255, 160, 110), (130, 190, 255)]
BG, FG, MUTED, TRACK = (24, 25, 30), (240, 240, 245), (150, 152, 165), (44, 46, 54)
_LIVE = {}


def _esc(x):
    return html.escape(str(x), quote=False)


def _txt(x):
    return getattr(x, 'text', x) or ''


# --------------------------------------------------------------------------- данные опроса

async def poll_data(msg):
    """{'q', 'answers': [(текст, голоса, правильный, выбран)], 'total', 'quiz', 'multi', 'closed', 'public'} | None."""
    media = getattr(msg, 'media', None)
    poll = getattr(media, 'poll', None)
    if poll is None:
        return None
    results = getattr(media, 'results', None)
    if not results or results.results is None:
        try:
            from telethon.tl.functions.messages import GetPollResultsRequest
            kw = {'peer': await engine.client.get_input_entity(msg.chat_id), 'msg_id': msg.id}
            if 'poll_hash' in inspect.signature(GetPollResultsRequest.__init__).parameters:
                kw['poll_hash'] = poll.hash
            upd = await engine.client(GetPollResultsRequest(**kw))
            for u in getattr(upd, 'updates', []) or []:
                if getattr(u, 'results', None) is not None:
                    results = u.results
        except Exception:
            pass
    by_opt = {}
    if results and results.results:
        by_opt = {r.option: r for r in results.results}
    answers = []
    for a in poll.answers:
        r = by_opt.get(a.option)
        answers.append((_txt(a.text), getattr(r, 'voters', 0) or 0, bool(getattr(r, 'correct', False)),
                        bool(getattr(r, 'chosen', False))))
    return {'q': _txt(poll.question), 'answers': answers, 'total': getattr(results, 'total_voters', 0) or 0,
            'visible': bool(results and results.results), 'quiz': bool(poll.quiz), 'multi': bool(poll.multiple_choice),
            'closed': bool(poll.closed), 'public': bool(poll.public_voters), 'options': [a.option for a in poll.answers]}


# --------------------------------------------------------------------------- картинка

FONT_URL = 'https://raw.githubusercontent.com/google/fonts/main/ofl/notosans/NotoSans%5Bwdth,wght%5D.ttf'


def _font_path():
    try:
        from core.paths import DATA_DIR
        d = Path(DATA_DIR) / 'pollplot'
    except Exception:
        d = Path('user/data/pollplot')
    d.mkdir(parents=True, exist_ok=True)
    return d / 'NotoSans.ttf'


async def _ensure_font():
    """Свой шрифт с кириллицей (один раз, ≈0.6 МБ); без сети — системные шрифты."""
    p = _font_path()
    if p.is_file():
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
            async with s.get(FONT_URL) as r:
                if r.status == 200:
                    p.write_bytes(await r.read())
    except Exception:
        pass


def _font(size, bold=False):
    from PIL import ImageFont
    for p in [_font_path()]:
        if p.is_file():
            try:
                f = ImageFont.truetype(str(p), size)
                try:
                    f.set_variation_by_name('Bold' if bold else 'Regular')
                except Exception:
                    pass
                return f
            except Exception:
                pass
    for name in (('segoeuib.ttf', 'arialbd.ttf', 'DejaVuSans-Bold.ttf') if bold else ('segoeui.ttf', 'arial.ttf', 'DejaVuSans.ttf')):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, width, max_lines=3):
    words, lines, cur = text.split(), [], ''
    for w in words:
        t = (cur + ' ' + w).strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + '…'
    return lines or ['']


def render(d, style='bars'):
    from PIL import Image, ImageDraw
    W, pad = 1080, 56
    fq, fa, fs, fp = _font(46, True), _font(34, True), _font(28), _font(40, True)
    tmp = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    qlines = _wrap(tmp, d['q'], fq, W - 2 * pad, 3)
    total = d['total']
    votes = [a[1] for a in d['answers']]
    best = max(votes) if votes else 0
    denom = total if total else 1
    head_h = pad + len(qlines) * 58 + 74
    if style == 'pie':
        H = head_h + 560 + len(d['answers']) * 58 + pad
    else:
        H = head_h + len(d['answers']) * 118 + 40 + pad
    im = Image.new('RGB', (W, H), BG)
    dr = ImageDraw.Draw(im)
    y = pad
    for ln in qlines:
        dr.text((pad, y), ln, font=fq, fill=FG)
        y += 58
    kind = ('викторина' if d['quiz'] else 'опрос') + (' · несколько ответов' if d['multi'] else '') + \
           (' · завершён' if d['closed'] else '') + (' · открытый' if d['public'] else ' · анонимный')
    dr.text((pad, y + 4), f"{total} голос{_plural(total)} · {kind}", font=fs, fill=MUTED)
    y = head_h
    if style == 'pie':
        cx, cy, r = W // 2, y + 260, 240
        start = -90.0
        if total:
            for i, (text, v, correct, chosen) in enumerate(d['answers']):
                if not v:
                    continue
                ext = 360.0 * v / denom
                dr.pieslice([cx - r, cy - r, cx + r, cy + r], start, start + ext, fill=PALETTE[i % len(PALETTE)])
                start += ext
        else:
            dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=TRACK)
        dr.ellipse([cx - r * 0.58, cy - r * 0.58, cx + r * 0.58, cy + r * 0.58], fill=BG)
        tw = dr.textlength(str(total), font=_font(72, True))
        dr.text((cx - tw / 2, cy - 52), str(total), font=_font(72, True), fill=FG)
        lbl = "голосов"
        dr.text((cx - dr.textlength(lbl, font=fs) / 2, cy + 28), lbl, font=fs, fill=MUTED)
        y = cy + r + 50
        for i, (text, v, correct, chosen) in enumerate(d['answers']):
            col = PALETTE[i % len(PALETTE)]
            dr.rounded_rectangle([pad, y + 8, pad + 30, y + 38], 8, fill=col)
            pct = f"{100 * v / denom:.0f}%"
            label = _wrap(dr, ('✓ ' if correct else '') + text, fa, W - 2 * pad - 220, 1)[0]
            dr.text((pad + 50, y), label, font=fa, fill=FG)
            dr.text((W - pad - dr.textlength(pct, font=fp), y - 2), pct, font=fp, fill=FG if v == best and v else MUTED)
            y += 58
    else:
        bar_w = W - 2 * pad
        for i, (text, v, correct, chosen) in enumerate(d['answers']):
            col = PALETTE[i % len(PALETTE)] if not d['quiz'] else ((120, 210, 120) if correct else (230, 95, 95))
            pct = 100 * v / denom
            label = ('✓ ' if correct else '') + text + ('  · ваш' if chosen else '')
            label = _wrap(dr, label, fa, bar_w - 240, 1)[0]
            dr.text((pad, y), label, font=fa, fill=FG)
            p = f"{pct:.0f}%"
            dr.text((W - pad - dr.textlength(p, font=fp), y - 4), p, font=fp, fill=FG if v == best and v else MUTED)
            by = y + 54
            dr.rounded_rectangle([pad, by, pad + bar_w, by + 30], 15, fill=TRACK)
            if v:
                dr.rounded_rectangle([pad, by, pad + max(30, int(bar_w * v / denom)), by + 30], 15, fill=col)
            cnt = f"{v}"
            dr.text((pad + bar_w - dr.textlength(cnt, font=fs) - 12, by - 1), cnt, font=fs, fill=FG if v else MUTED)
            y += 118
    dr.text((pad, H - pad + 6), "FRX · PollPlot", font=_font(22), fill=(90, 92, 105))
    buf = io.BytesIO()
    im.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


def _plural(n):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return 'ов'
    return {1: '', 2: 'а', 3: 'а', 4: 'а'}.get(n % 10, 'ов')


def text_report(d):
    denom = d['total'] or 1
    best = max((a[1] for a in d['answers']), default=0)
    lines = [f"📊 <b>{_esc(d['q'])}</b>", visual.hint(f"{d['total']} голос{_plural(d['total'])}")]
    for text, v, correct, chosen in d['answers']:
        pct = 100 * v / denom
        bar = engine.progress_bar(pct, 10, 'bar')
        lines.append(f"{'🏆' if v == best and v else '▸'} {'✓ ' if correct else ''}{_esc(text)}\n   {bar} <b>{pct:.0f}%</b> · {v}")
    return "\n".join(lines)


async def save_png(data):
    obj = await files.create_file('image/png', f"poll_{int(time.time())}.png", data)
    return obj['path']


# --------------------------------------------------------------------------- команды

@decorators.command('pp', 'pollplot')
async def pp_cmd(event):
    """результаты опроса картинкой — ответом на опрос: [pie] · live [часы] · stop · voters · text"""
    arg = engine.get_args().strip().lower().split()
    sub = arg[0] if arg else ''
    msg = await event.get_reply_message() if event.is_reply else None
    if sub == 'stop':
        key = (event.chat_id, msg.id if msg else None)
        hit = [k for k in _LIVE if k[0] == event.chat_id and (msg is None or k[1] == msg.id or _LIVE[k]['out'] == msg.id)]
        for k in hit:
            _LIVE.pop(k)['task'].cancel()
        await engine.answer(event, visual.ok(f"Живых графиков остановлено: {len(hit)}"))
        return
    if msg is None or getattr(getattr(msg, 'media', None), 'poll', None) is None:
        await engine.answer(event, visual.err("Ответьте на сообщение с опросом"))
        return
    d = await poll_data(msg)
    if not d['visible'] and not d['closed']:
        await engine.answer(event, visual.err("Результаты скрыты, пока вы не проголосуете — проголосуйте и повторите"))
        return
    if sub == 'text':
        await engine.answer(event, text_report(d))
        return
    if sub == 'voters':
        await voters(event, msg, d)
        return
    style = 'pie' if 'pie' in arg or 'пончик' in arg else 'bars'
    await engine.answer(event, visual.wait("Рисую…"))
    await _ensure_font()
    path = await save_png(await asyncio.to_thread(render, d, style))
    caption = f"📊 {_esc(d['q'][:200])} · {d['total']} голос{_plural(d['total'])}"
    if sub == 'live':
        hours = float(arg[1]) if len(arg) > 1 and arg[1].replace('.', '', 1).isdigit() else 24
        await event.delete()
        out = await engine.client.send_file(event.chat_id, path, caption=caption, reply_to=msg.id)
        Path(path).unlink(missing_ok=True)
        key = (event.chat_id, msg.id)
        if key in _LIVE:
            _LIVE.pop(key)['task'].cancel()
        _LIVE[key] = {'out': out.id, 'task': asyncio.ensure_future(_live(event.chat_id, msg.id, out.id, style, hours))}
        return
    await engine.answer(event, caption, file=path)


async def _live(chat_id, poll_id, out_id, style, hours):
    until = time.time() + hours * 3600
    last = None
    try:
        while time.time() < until:
            await asyncio.sleep(300)
            msg = await engine.client.get_messages(chat_id, ids=poll_id)
            if msg is None:
                break
            d = await poll_data(msg)
            sig = (d['total'], tuple(a[1] for a in d['answers']), d['closed'])
            if sig != last:
                last = sig
                data = await asyncio.to_thread(render, d, style)
                path = await save_png(data)
                try:
                    await engine.client.edit_message(chat_id, out_id, f"📊 {d['q'][:200]} · {d['total']} голос{_plural(d['total'])}"
                                                     + (" · обновляется" if not d['closed'] else " · итог"), file=path)
                finally:
                    Path(path).unlink(missing_ok=True)
            if d['closed']:
                break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        engine.tlog(f"[pollplot] live: {e}")
    finally:
        _LIVE.pop((chat_id, poll_id), None)


async def voters(event, msg, d):
    if not d['public']:
        await engine.answer(event, visual.err("Опрос анонимный — кто как голосовал, Telegram не показывает"))
        return
    from telethon.tl.functions.messages import GetPollVotesRequest
    await engine.answer(event, visual.wait("Собираю голоса…"))
    peer = await engine.client.get_input_entity(event.chat_id)
    rows = []
    by_option = {}
    for i, opt in enumerate(d['options']):
        offset = None
        while True:
            try:
                res = await engine.client(GetPollVotesRequest(peer=peer, id=msg.id, option=opt, offset=offset, limit=50))
            except Exception as e:
                await engine.answer(event, visual.err(f"Telegram не отдал голоса: {_esc(e)}"))
                return
            users = {u.id: u for u in getattr(res, 'users', [])}
            for v in res.votes:
                uid = getattr(getattr(v, 'peer', None), 'user_id', None)
                u = users.get(uid)
                name = " ".join(x for x in (getattr(u, 'first_name', '') or '', getattr(u, 'last_name', '') or '') if x) or str(uid)
                by_option.setdefault(i, []).append((uid, name, getattr(u, 'username', None)))
                rows.append((d['answers'][i][0], uid, name, getattr(u, 'username', '') or ''))
            offset = getattr(res, 'next_offset', None)
            if not offset:
                break
    lines = [f"🗳 <b>{_esc(d['q'])}</b>"]
    for i, (text, v, correct, chosen) in enumerate(d['answers']):
        people = by_option.get(i, [])
        lines.append(f"\n<b>{'✓ ' if correct else ''}{_esc(text)}</b> — {len(people)}")
        shown = people[:25]
        lines.append(", ".join(f'<a href="tg://user?id={u}">{_esc(n)}</a>' for u, n, _ in shown) or "—")
        if len(people) > 25:
            lines.append(visual.hint(f"и ещё {len(people) - 25} — в файле"))
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['вариант', 'id', 'имя', 'username'])
    w.writerows(rows)
    obj = await files.create_file('text/csv', f"poll_{msg.id}.csv", '﻿' + buf.getvalue())
    await engine.answer(event, "\n".join(lines)[:4000])
    await engine.send_message(event.chat_id, visual.hint(f"все голоса · {len(rows)}"), file=obj['path'])
