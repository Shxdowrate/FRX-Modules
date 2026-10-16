"""
Birthdays — дни рождения: из Telegram (у кого указан ДР) и ваши записи. Бот напомнит заранее и в сам день.

  .bdays [all]                  ближайшие дни рождения (all — весь год)
  .bdadd @user 12.10[.1995]     добавить/исправить (или ответом на сообщение человека)
  .bdadd Имя Фамилия 12.10      человек без Telegram — просто имя
  .bddel <номер | @user>        убрать свою запись (номер — из .bdays all)
  .bdauto @user [текст]         в этот день поздравлять человека автоматически (ещё раз — выключить)

Напоминания приходят от бота FRX в notify_time (по умолчанию 09:00) — в сам день и за remind_days дней.
Свои записи главнее данных Telegram. Год рождения — по желанию, с ним бот напишет, сколько исполняется.
"""

import html
import re
from datetime import date, datetime

import db
import decorators
import engine
import visual

NAME = "Birthdays"
MODULE_NAME = "birthdays"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Дни рождения из Telegram и свои: список, напоминания от бота заранее, автопоздравление"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/birthdays.py"

MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='notify_time', description='Во сколько присылать напоминания (ЧЧ:ММ)', data_type='str',
                               default='09:00', valid={'max_len': 5, 'min_len': 4})
    await engine.create_config(key='remind_days', description='За сколько дней напоминать заранее (0 — в сам день)',
                               data_type='list', default=['0', '1', '7'])
    await engine.create_config(key='congrats_text', description='Текст автопоздравления ({name} — имя, {age} — сколько исполняется)',
                               data_type='str', default='С днём рождения, {name}! 🎉 Счастья, здоровья и всего самого лучшего!',
                               valid={'max_len': 1000})


async def cfg(key, default=None):
    try:
        v = await engine.get_config(key, module_name=MODULE_NAME)
        return default if v is None else v
    except KeyError:
        return default


def _who(u):
    return ' '.join(x for x in (getattr(u, 'first_name', '') or '', getattr(u, 'last_name', '') or '') if x) or (
        '@' + u.username if getattr(u, 'username', None) else str(u.id))


def parse_date(s):
    m = re.fullmatch(r'(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?', s.strip())
    if not m:
        return None
    d, mo = int(m.group(1)), int(m.group(2))
    y = int(m.group(3)) if m.group(3) else None
    if y is not None and y < 100:
        y += 1900 if y > date.today().year % 100 else 2000
    try:
        date(y or 2000, mo, d)
    except ValueError:
        return None
    if y and not 1900 <= y <= date.today().year:
        return None
    return d, mo, y


def next_date(d, mo, today=None):
    today = today or date.today()
    for y in (today.year, today.year + 1, today.year + 2):
        try:
            nd = date(y, mo, d)
        except ValueError:                                  # 29 февраля в невисокосный год → 28-е
            nd = date(y, 2, 28)
        if nd >= today:
            return nd
    return today


# --------------------------------------------------------------------------- данные

async def manual():
    return dict(await db.get('manual', {}) or {})


async def telegram_birthdays():
    from telethon.tl.functions.contacts import GetBirthdaysRequest
    try:
        res = await engine.client(GetBirthdaysRequest())
    except Exception as e:
        engine.tlog(f"[birthdays] Telegram: {e}")
        return {}
    users = {u.id: u for u in res.users}
    out = {}
    for c in res.contacts:
        b = c.birthday
        u = users.get(c.contact_id)
        out[str(c.contact_id)] = {'name': _who(u) if u else str(c.contact_id), 'day': b.day, 'month': b.month,
                                  'year': getattr(b, 'year', None), 'uid': c.contact_id, 'src': 'tg'}
    return out


async def everyone():
    """{key: {'name', 'day', 'month', 'year', 'uid'|None, 'src'}} — Telegram + свои (свои главнее)."""
    people = await telegram_birthdays()
    for k, v in (await manual()).items():
        people[k] = {**v, 'src': 'me'}
    return people


def upcoming(people, days=None):
    today = date.today()
    rows = []
    for k, p in people.items():
        nd = next_date(p['day'], p['month'], today)
        left = (nd - today).days
        if days is not None and left > days:
            continue
        age = nd.year - p['year'] if p.get('year') else None
        rows.append((left, nd, age, k, p))
    rows.sort(key=lambda r: (r[0], r[4]['name']))
    return rows


def _when(left):
    return "🎂 сегодня" if left == 0 else "завтра" if left == 1 else f"через {left} дн."


def _age_word(n):
    if n % 10 == 1 and n % 100 != 11:
        return 'год'
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return 'года'
    return 'лет'


def _person(p):
    return f'<a href="tg://user?id={p["uid"]}">{_esc(p["name"])}</a>' if p.get('uid') else _esc(p['name'])


# --------------------------------------------------------------------------- команды

@decorators.command('bdays', 'birthdays', 'др')
async def bdays_cmd(event):
    """ближайшие дни рождения (60 дней); all — весь год с номерами для .bddel"""
    show_all = engine.get_args().strip().lower() in ('all', 'все', 'всё')
    await engine.answer(event, visual.wait("Собираю дни рождения…"))
    people = await everyone()
    rows = upcoming(people, None if show_all else 60)
    if not rows:
        await engine.answer(event, visual.hint("Ближайших дней рождения нет") + "\n" + visual.hint(".bdays all · .bdadd @user 12.10"))
        return
    auto = set(await db.get('auto', []) or [])
    out = [visual.title("Дни рождения", sub="весь год" if show_all else "ближайшие 60 дней")]
    for i, (left, nd, age, k, p) in enumerate(rows, 1):
        num = f"<b>{i}.</b> " if show_all else ''
        a = f" · исполнится {age} {_age_word(age)}" if age else ''
        flags = (" ✍" if p['src'] == 'me' else '') + (" 🤖" if k in auto else '')
        out.append(f"{num}{_person(p)}{flags} — {nd.day} {MONTHS[nd.month - 1]}, {_when(left)}{a}")
    out.append("")
    out.append(visual.hint("✍ — ваша запись · 🤖 — автопоздравление · .bdadd @user 12.10"))
    await engine.answer(event, "\n".join(out))


async def _target(event, token):
    """@user / id / ответ → (key, name, uid)"""
    if token and (token.startswith('@') or token.lstrip('-').isdigit()):
        u = await engine.client.get_entity(int(token) if token.lstrip('-').isdigit() else token)
        return str(u.id), _who(u), u.id
    if event.is_reply:
        r = await event.get_reply_message()
        u = await r.get_sender() if r else None
        if u is not None:
            return str(u.id), _who(u), u.id
    return None


@decorators.command('bdadd', 'дрдобавить')
async def bdadd_cmd(event):
    """добавить ДР: @user 12.10[.1995] · ответом: 12.10 · Имя Фамилия 12.10"""
    args = engine.get_args().split()
    dpos = next((i for i, a in enumerate(args) if parse_date(a)), None)
    if dpos is None:
        await engine.answer(event, visual.err("Какая дата?") + "\n" + visual.hint(".bdadd @user 12.10 · .bdadd @user 12.10.1995 · .bdadd Мама 3.03"))
        return
    d, mo, y = parse_date(args[dpos])
    rest = [a for i, a in enumerate(args) if i != dpos]
    try:
        tgt = await _target(event, rest[0] if rest else None)
    except Exception as e:
        await engine.answer(event, visual.err("Не нашёл человека") + f"\n<code>{_esc(str(e)[:200])}</code>")
        return
    if tgt is None:
        name = ' '.join(rest).strip()
        if not name:
            await engine.answer(event, visual.err("Чей день рождения? @user, ответом или имя"))
            return
        key, uid = 'n:' + name.lower(), None
    else:
        key, name, uid = tgt
    m = await manual()
    m[key] = {'name': name[:60], 'day': d, 'month': mo, 'year': y, 'uid': uid}
    await db.set('manual', m)
    left = (next_date(d, mo) - date.today()).days
    await engine.answer(event, visual.ok(f"Записал: {_esc(name)} — {d} {MONTHS[mo - 1]}" + (f" {y}" if y else '')) + f" ({_when(left)})")


@decorators.command('bddel', 'друдалить')
async def bddel_cmd(event):
    """убрать свою запись: номер из .bdays all или @user"""
    arg = engine.get_args().strip()
    m = await manual()
    key = None
    if arg.isdigit():
        rows = upcoming(await everyone())
        i = int(arg) - 1
        if 0 <= i < len(rows):
            key = rows[i][3]
    else:
        try:
            tgt = await _target(event, arg or None)
            key = tgt[0] if tgt else ('n:' + arg.lower() if arg else None)
        except Exception:
            key = 'n:' + arg.lower()
    if key not in m:
        await engine.answer(event, visual.err("Такой вашей записи нет") + "\n" + visual.hint("дни рождения из Telegram удалить нельзя — их указывает сам человек"))
        return
    p = m.pop(key)
    await db.set('manual', m)
    await engine.answer(event, visual.ok(f"Убрал: {_esc(p['name'])}"))


@decorators.command('bdauto', 'дравто')
async def bdauto_cmd(event):
    """автопоздравление в ЛС в день рождения: @user [свой текст] (ещё раз — выключить)"""
    args = engine.get_args().strip()
    token, _, text = args.partition(' ')
    try:
        tgt = await _target(event, token if token.startswith('@') or token.lstrip('-').isdigit() else None)
    except Exception as e:
        await engine.answer(event, visual.err("Не нашёл человека") + f"\n<code>{_esc(str(e)[:200])}</code>")
        return
    if tgt is None:
        await engine.answer(event, visual.err("Кого поздравлять? @user или ответом"))
        return
    if not (token.startswith('@') or token.lstrip('-').isdigit()):
        text = args
    key, name, uid = tgt
    auto = list(await db.get('auto', []) or [])
    texts = dict(await db.get('auto_text', {}) or {})
    if key in auto and not text:
        auto.remove(key)
        texts.pop(key, None)
        await db.set('auto', auto)
        await db.set('auto_text', texts)
        await engine.answer(event, visual.ok(f"Автопоздравление для {_esc(name)} выключено"))
        return
    if key not in auto:
        auto.append(key)
    if text:
        texts[key] = text[:1000]
    await db.set('auto', auto)
    await db.set('auto_text', texts)
    people = await everyone()
    known = key in people
    await engine.answer(event, visual.ok(f"Поздравлю {_esc(name)} автоматически") + "\n" + (
        visual.hint(f"текст: {_esc(text or await cfg('congrats_text'))}") if known else
        visual.warn("но дата неизвестна — добавьте: .bdadd @user 12.10")))


# --------------------------------------------------------------------------- напоминания

@decorators.cycle(interval='5m')
async def bd_tick():
    now = datetime.now()
    try:
        hh, mm = [int(x) for x in str(await cfg('notify_time', '09:00')).split(':')[:2]]
    except ValueError:
        hh, mm = 9, 0
    if (now.hour, now.minute) < (hh, mm):
        return
    today = now.date().isoformat()
    if await db.get('last_day') == today:
        return
    await db.set('last_day', today)
    days = sorted({int(x) for x in (await cfg('remind_days', ['0', '1', '7']) or []) if str(x).strip().isdigit()})
    people = await everyone()
    rows = [r for r in upcoming(people, max(days) if days else 0) if r[0] in days]
    auto = set(await db.get('auto', []) or [])
    texts = await db.get('auto_text', {}) or {}
    lines = []
    for left, nd, age, k, p in rows:
        a = f" — исполняется {age} {_age_word(age)}" if age else ''
        line = f"{'🎂' if left == 0 else '🗓'} {_person(p)}: {_when(left)}, {nd.day} {MONTHS[nd.month - 1]}{a}"
        if left == 0 and k in auto and p.get('uid'):
            tpl = texts.get(k) or await cfg('congrats_text', 'С днём рождения, {name}! 🎉')
            try:
                await engine.client.send_message(p['uid'], tpl.replace('{name}', p['name'].split()[0]).replace('{age}', str(age or '')))
                line += " · ✅ поздравил автоматически"
            except Exception as e:
                line += f" · ⚠ не поздравилось: {_esc(str(e)[:80])}"
        lines.append(line)
    if not lines:
        return
    body = "🎉 <b>Дни рождения</b>\n\n" + "\n".join(lines)
    try:
        await engine.bot_send_to_owner(body)
    except Exception:
        await engine.client.send_message('me', body, parse_mode='html')
