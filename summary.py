"""
Summary — краткий пересказ переписки нейросетью: о чём говорили, что решили, какие вопросы остались.

  .sum                  непрочитанное в этом чате (если его нет — последние 200 сообщений)
  .sum 500              последние 500 сообщений
  .sum 3h · .sum 1d     за последние 3 часа / сутки (сегодня — .sum today)
  .sum (ответом)        от этого сообщения и до конца
  .sum … | вопрос       сфокусироваться: «.sum 1d | что решили по релизу?»
  .sum … me             ответ — в Избранное, а не в этот чат

Как считается: переписка режется на куски по бюджету токенов модели (сколько влезет в её окно с запасом),
каждый кусок пересказывается отдельным запросом, потом пересказы сводятся в один. Перед большим разбором
показывается оценка — сколько токенов и запросов уйдёт, и нужно подтвердить. Модель — та, что выбрана для чата
на сайте (вкладка neuro); окно модели угадывается по названию, а точно задаётся в context_tokens.
"""

import asyncio
import html
import re
import time
from datetime import datetime, timedelta

import decorators
import engine
import visual

NAME = "Summary"
MODULE_NAME = "summary"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Пересказ переписки нейросетью: непрочитанное, N сообщений или период; режет по токенам и сводит"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/summary.py"

# окно контекста по названию модели (токены); первое совпадение
CTX = [
    (r'gpt-4\.1|gemini-(1\.5|2|3)|gemini.*(pro|flash)|llama-4', 1_000_000), (r'claude', 200_000), (r'gpt-5|o[134]-|o[134]$', 200_000),
    (r'gpt-4o|gpt-4-turbo|grok|deepseek|mistral-(large|medium)|qwen|command-r|llama-3\.[1-3]', 128_000),
    (r'gpt-3\.5', 16_000), (r'gigachat', 32_000), (r'mistral|mixtral|codestral', 32_000), (r'llama|gemma|phi', 8_000),
]

SYSTEM = ("Ты делаешь краткие точные пересказы переписок из Telegram. Пиши по-русском, только по фактам из текста, без "
          "выдумок и без оценок людей. Имена участников сохраняй как в переписке.")
MAP_PROMPT = ("Ниже кусок переписки {part} (формат: [время] Имя: текст). Перескажи его сжато: основные темы, важные факты и "
              "цифры, решения и договорённости, открытые вопросы, кто что предложил. Списком, без вступления.{focus}\n\n{text}")
REDUCE_PROMPT = ("Ниже пересказы последовательных кусков одной переписки ({chat}). Сведи их в один итог: сначала 1–2 предложения "
                 "«о чём было», затем разделы «Главное», «Решили», «Открытые вопросы» (пропусти пустые). Кратко, без повторов, "
                 "без вступлений.{focus}\n\n{text}")
SINGLE_PROMPT = ("Перескажи переписку ({chat}): сначала 1–2 предложения «о чём было», затем разделы «Главное», «Решили», "
                 "«Открытые вопросы» (пропусти пустые). Кратко, без вступлений.{focus}\n\n{text}")


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='context_tokens', description='Окно контекста модели в токенах (0 — угадать по названию)',
                               data_type='int', default=0, valid={'min_value': 0, 'max_value': 10_000_000})
    await engine.create_config(key='chunk_max', description='Не больше стольких токенов переписки в одном запросе '
                               '(большие куски дешевле, но модели хуже держат детали)', data_type='int', default=60_000,
                               valid={'min_value': 1000, 'max_value': 1_000_000})
    await engine.create_config(key='confirm_over', description='Спрашивать подтверждение, если уйдёт больше стольких токенов',
                               data_type='int', default=15_000, valid={'min_value': 0, 'max_value': 10_000_000})
    await engine.create_config(key='max_messages', description='Самое большое число сообщений за один .sum', data_type='int',
                               default=5000, valid={'min_value': 50, 'max_value': 100_000})


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


def tokens(text):
    try:
        from core.llm import approx_tokens
        return approx_tokens(text)
    except Exception:
        return len(text) // 3 + 1


async def budget(ref):
    """(токенов переписки на один запрос, окно модели)"""
    conn, model = engine.llm.parse_ref(ref)
    ctx = int(await cfg('context_tokens', 0) or 0)
    if not ctx:
        low = model.lower()
        if conn.get('provider') == 'ollama':
            ctx = 4096                                  # Ollama по умолчанию режет контекст до num_ctx (~4k)
        else:
            ctx = next((n for pat, n in CTX if re.search(pat, low)), 16_000)
    per = int(min(ctx * 0.6 - 1500, int(await cfg('chunk_max', 60_000))))
    return max(800, per), ctx


# --------------------------------------------------------------------------- сбор переписки

def _who(ent):
    if ent is None:
        return '?'
    return getattr(ent, 'title', None) or ' '.join(x for x in (getattr(ent, 'first_name', '') or '',
                                                                getattr(ent, 'last_name', '') or '') if x) or 'Без имени'


def line_of(m, names):
    if m.action is not None:
        return None
    text = (m.raw_text or '').strip()
    if m.media is not None:
        label = ('фото' if m.photo else 'стикер' if m.sticker else 'голосовое' if m.voice else 'кружок' if m.video_note
                 else 'видео' if m.video else 'GIF' if m.gif else 'аудио' if m.audio else 'опрос' if m.poll else 'файл' if m.document else None)
        if m.poll:
            q = m.poll.poll.question
            text = f"[опрос: {getattr(q, 'text', q)}] " + text
        elif label:
            text = f"[{label}] " + text
    if not text:
        return None
    who = names.get(m.sender_id) or _who(m.sender)
    names[m.sender_id] = who
    reply = ''
    if m.reply_to_msg_id and m.reply_to_msg_id in names.get('_ids', {}):
        reply = f" (в ответ {names['_ids'][m.reply_to_msg_id]})"
    names.setdefault('_ids', {})[m.id] = who
    if m.fwd_from:
        reply += " (переслано)"
    t = m.date.astimezone().strftime('%d.%m %H:%M') if m.date else ''
    return f"[{t}] {who}{reply}: {' '.join(text.split())[:2000]}"


def parse_scope(args):
    """→ (limit | None, since: datetime | None, unread: bool)"""
    low = args.lower()
    m = re.search(r'\b(\d+)\s*(m|м|мин|h|ч|d|д|дн|w|нед)\b', low)
    if m:
        k = {'m': 1, 'м': 1, 'мин': 1, 'h': 60, 'ч': 60, 'd': 1440, 'д': 1440, 'дн': 1440, 'w': 10080, 'нед': 10080}[m.group(2)]
        return None, datetime.now().astimezone() - timedelta(minutes=int(m.group(1)) * k), False
    if re.search(r'\b(today|сегодня)\b', low):
        now = datetime.now().astimezone()
        return None, now.replace(hour=0, minute=0, second=0, microsecond=0), False
    m = re.search(r'\b(\d{1,6})\b', low)
    if m:
        return int(m.group(1)), None, False
    return None, None, True


async def collect(event, args):
    client = engine.client
    cap = int(await cfg('max_messages', 5000))
    limit, since, unread = parse_scope(args)
    from_id = event.reply_to_msg_id if event.is_reply else None
    what = ''
    if from_id:
        it = client.iter_messages(event.chat_id, min_id=from_id - 1, reverse=True, limit=cap)
        what = 'с отмеченного сообщения'
        msgs = [m async for m in it]
    else:
        if unread:
            n = 0
            try:
                from telethon.tl.functions.messages import GetPeerDialogsRequest
                res = await client(GetPeerDialogsRequest(peers=[await client.get_input_entity(event.chat_id)]))
                n = res.dialogs[0].unread_count if res.dialogs else 0
            except Exception:
                n = 0
            limit, what = (min(n, cap), f"непрочитанное ({n})") if n >= 5 else (200, "последние 200")
        elif limit:
            limit = min(limit, cap)
            what = f"последние {limit}"
        else:
            what = f"с {since.strftime('%d.%m %H:%M')}"
        msgs = []
        async for m in client.iter_messages(event.chat_id, limit=limit or cap):
            if since and m.date and m.date < since:
                break
            msgs.append(m)
        msgs.reverse()
    msgs = [m for m in msgs if m.id != event.id]
    names, lines = {}, []
    for m in msgs:
        ln = line_of(m, names)
        if ln:
            lines.append(ln)
    return lines, what


def chunks_of(lines, per):
    out, cur, cur_t = [], [], 0
    for ln in lines:
        t = tokens(ln) + 1
        if cur and cur_t + t > per:
            out.append(cur)
            cur, cur_t = [], 0
        cur.append(ln[:per * 2] if t > per else ln)
        cur_t += min(t, per)
    if cur:
        out.append(cur)
    return out


def plan(lines, per):
    """Сколько уйдёт: (куски, запросов, токенов на входе ~)"""
    parts = chunks_of(lines, per)
    total_in = sum(tokens(ln) for ln in lines) + 300 * len(parts)
    reqs = len(parts)
    if len(parts) > 1:
        summaries = len(parts) * 500                       # ~500 токенов на пересказ куска
        levels = 1
        while summaries > per:
            groups = -(-summaries // per)
            reqs += groups
            summaries = groups * 500
            levels += 1
        reqs += 1
        total_in += len(parts) * 500 * levels
    return parts, reqs, total_in


# --------------------------------------------------------------------------- разбор

async def ask(ref, prompt, cancel, max_tokens=900):
    res = await engine.llm.chat(ref, [{'role': 'user', 'content': prompt}], system=SYSTEM, cancel=cancel, max_tokens=max_tokens,
                                temperature=0.2)
    return res.text.strip(), res.tokens_in + res.tokens_out


async def summarize(ref, parts, per, chat, focus, cancel, status):
    f = f"\nОсобенно обрати внимание: {focus}" if focus else ''
    spent = 0
    if len(parts) == 1:
        await status("Пересказываю…")
        text, t = await ask(ref, SINGLE_PROMPT.format(chat=chat, focus=f, text="\n".join(parts[0])), cancel, 1400)
        return text, spent + t, 1
    sums, reqs = [], 0
    for i, p in enumerate(parts, 1):
        if cancel.is_set():
            raise asyncio.CancelledError
        await status(f"Кусок {i} из {len(parts)}…")
        text, t = await ask(ref, MAP_PROMPT.format(part=f"{i}/{len(parts)}", focus=f, text="\n".join(p)), cancel)
        sums.append(f"— Кусок {i}:\n{text}")
        spent += t
        reqs += 1
    while sum(tokens(s) for s in sums) > per:                # пересказы сами не влезают — сводим группами
        groups = chunks_of(sums, per)
        nxt = []
        for i, g in enumerate(groups, 1):
            await status(f"Свожу пересказы: группа {i} из {len(groups)}…")
            text, t = await ask(ref, MAP_PROMPT.format(part=f"(пересказы, группа {i}/{len(groups)})", focus=f, text="\n\n".join(g)), cancel)
            nxt.append(f"— Часть {i}:\n{text}")
            spent += t
            reqs += 1
        sums = nxt
    await status("Свожу в итог…")
    text, t = await ask(ref, REDUCE_PROMPT.format(chat=chat, focus=f, text="\n\n".join(sums)), cancel, 1400)
    return text, spent + t, reqs + 1


def md_to_html(text):
    """Нейросети пишут Markdown — переводим жирный/заголовки/списки в HTML Telegram."""
    t = _esc(text)
    t = re.sub(r'^#{1,6}\s*(.+)$', r'<b>\1</b>', t, flags=re.M)
    t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
    t = re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])', r'<i>\1</i>', t)
    t = re.sub(r'^\s*[-*]\s+', '• ', t, flags=re.M)
    t = re.sub(r'`([^`]+)`', r'<code>\1</code>', t)
    return t


def split_msg(text, n=3900):
    out = []
    while len(text) > n:
        cut = text.rfind('\n', 0, n)
        cut = cut if cut > n // 2 else n
        out.append(text[:cut])
        text = text[cut:].lstrip('\n')
    out.append(text)
    return out


@decorators.command('sum', 'summary', 'сводка', 'пересказ')
async def sum_cmd(event):
    """пересказ переписки: непрочитанное · [N] сообщений · [3h|1d|today] · ответом — с сообщения · | вопрос · me — в Избранное"""
    if not hasattr(engine, 'llm'):
        await engine.answer(event, visual.err("В ядре нет нейросетей — обновите FRX"))
        return
    raw = engine.get_args()
    args, _, focus = raw.partition('|')
    to_me = bool(re.search(r'(^|\s)(me|избранное)(\s|$)', args.lower()))
    args = re.sub(r'(^|\s)(me|избранное)(?=\s|$)', ' ', args, flags=re.I)
    await engine.llm.scan_local()
    ref = engine.llm.selected('chat')
    if not ref:
        await engine.answer(event, visual.err("Нет нейросети") + "\n" + visual.hint("добавьте ключ на сайте: .web → neuro"))
        return
    await engine.answer(event, visual.wait("Читаю переписку…"))
    try:
        lines, what = await collect(event, args)
    except Exception as e:
        await engine.answer(event, visual.err("Не прочиталось") + f"\n<code>{_esc(str(e)[:300])}</code>")
        return
    if len(lines) < 3:
        await engine.answer(event, visual.hint("Пересказывать нечего — слишком мало текста"))
        return
    chat_ent = await event.get_chat()
    chat = _who(chat_ent) if not getattr(chat_ent, 'is_self', False) else 'Избранное'
    per, ctx = await budget(ref)
    parts, reqs, est = plan(lines, per)
    model = ref.split('|', 1)[1]
    est_line = (f"{len(lines)} сообщений ({what}) · ≈{est:,} токенов · {reqs} "
                f"{'запрос' if reqs == 1 else 'запроса' if 2 <= reqs <= 4 else 'запросов'} · {_esc(model)}").replace(',', ' ')
    cancel = asyncio.Event()
    state = {'form': None}

    async def go():
        t0 = time.time()

        async def status(s):
            txt = visual.wait(s) + "\n" + visual.hint(est_line)
            try:
                if state['form']:
                    await state['form'].edit(txt, buttons=[[("Остановить", "su:stop", 'danger')]])
                else:
                    await engine.answer(event, txt)
            except Exception:
                pass
        try:
            text, spent, n = await summarize(ref, parts, per, chat, focus.strip(), cancel, status)
        except asyncio.CancelledError:
            msg = visual.warn("Остановлено")
            await (state['form'].edit(msg, buttons=[]) if state['form'] else engine.answer(event, msg))
            return
        except Exception as e:
            msg = visual.err("Нейросеть не ответила") + f"\n<code>{_esc(str(e)[:300])}</code>"
            await (state['form'].edit(msg, buttons=[]) if state['form'] else engine.answer(event, msg))
            return
        head = (f"📝 <b>{_esc(chat)}</b> · {_esc(what)}" + (f"\n<i>{_esc(focus.strip())}</i>" if focus.strip() else ''))
        foot = visual.hint(f"{len(lines)} сообщений · {spent:,} токенов · {n} запр. · {int(time.time() - t0)} с · {model}".replace(',', ' '))
        body = f"{head}\n\n{md_to_html(text)}\n\n{foot}"
        pieces = split_msg(body)
        if to_me:
            for p in pieces:
                await engine.client.send_message('me', p, parse_mode='html', link_preview=False)
            done = visual.ok("Пересказ — в Избранном")
            if state['form']:
                await state['form'].edit(done, buttons=[])
            else:
                await engine.answer(event, done)
            return
        if state['form']:
            await state['form'].delete()
            for p in pieces:
                await engine.client.send_message(event.chat_id, p, parse_mode='html', link_preview=False)
            return
        await engine.answer(event, pieces[0])
        for p in pieces[1:]:
            await engine.client.send_message(event.chat_id, p, parse_mode='html', link_preview=False)

    if est <= int(await cfg('confirm_over', 15000)) and reqs == 1:
        await go()
        return
    form = await engine.create_inlineform(event.chat_id, visual.title("Пересказ", sub=_esc(chat)) + "\n"
                                          + visual.field("объём", est_line, code=False) + "\n"
                                          + visual.hint(f"кусок до {per:,} токенов · окно модели ~{ctx:,}".replace(',', ' ')),
                                          buttons=[[("Пересказать", "su:go", 'primary'), ("Отмена", "su:no")]])
    state['form'] = form
    try:
        await event.delete()
    except Exception:
        pass

    @form.on_click(data_startswith='su:')
    async def _(click):
        op = click.data[3:]
        if op == 'no':
            await form.delete()
        elif op == 'stop':
            cancel.set()
            await click.answer("Останавливаю…")
        elif op == 'go':
            await click.answer("Поехали")
            asyncio.ensure_future(go())
