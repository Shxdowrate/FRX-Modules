"""
Usernames — проверка юзернеймов: свободен ли, кто занял, что с ним на Fragment (аукцион, продажа, цена в TON).

  .un <имя> [имя2 …]       проверить (до 10 за раз; @ можно писать)
  .un gen <шаблон> [N]     подобрать свободные по шаблону: ? — буква, # — цифра, * — буква или цифра
                           (например .un gen frx_??? 10). Telegram ограничивает частоту — модуль проверяет не спеша
  .un watch <имя> [claim]  следить: как только имя освободится — сообщу от бота; с claim — сразу займу его
                           за новым публичным каналом «Резерв @имя» (потом передадите его или переименуете)
  .un unwatch <имя>        перестать следить;  .un list — за чем слежу
  .un mine                 ваши юзернеймы (в том числе купленные на Fragment)

Правила Telegram: 5–32 символа, латиница, цифры и «_», начинается с буквы, не заканчивается на «_».
Короткие имена (4 символа и меньше) продаются только на Fragment.
"""

import asyncio
import html
import random
import re
import string
import time

import aiohttp

import db
import decorators
import engine
import visual

NAME = "Usernames"
MODULE_NAME = "usernames"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Свободен ли юзернейм, кто занял, статус и цена на Fragment; подбор по шаблону; слежка с автозахватом"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/usernames.py"

VALID = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{3,31}$')


def _esc(x):
    return html.escape(str(x), quote=False)


def check_rules(name):
    if len(name) < 4:
        return "короче 4 символов не бывает"
    if not VALID.match(name):
        return "только латиница, цифры и _, начинается с буквы"
    if name.endswith('_') or '__' in name:
        return "не может заканчиваться на _ и содержать __"
    return None


# --------------------------------------------------------------------------- проверки

async def tg_check(name):
    """'free' | 'taken' | 'fragment' | 'invalid' | ('flood', сек)."""
    from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameOccupiedError
    from telethon.tl.functions.account import CheckUsernameRequest
    try:
        ok = await engine.client(CheckUsernameRequest(name))
        return 'free' if ok else 'taken'
    except UsernameOccupiedError:
        return 'taken'
    except UsernameInvalidError:
        return 'invalid'
    except FloodWaitError as e:
        return ('flood', e.seconds)
    except Exception as e:
        # у Telethon текст ошибки бывает пустым — смотрим и на имя класса (UsernamePurchaseAvailableError)
        sig = f"{type(e).__name__} {getattr(e, 'message', '')} {e}".upper().replace('_', '')
        if 'PURCHASEAVAILABLE' in sig:
            return 'fragment'
        if 'INVALID' in sig:
            return 'invalid'
        if 'OCCUPIED' in sig:
            return 'taken'
        raise


async def owner_of(name):
    try:
        ent = await engine.client.get_entity(name)
    except Exception:
        return None
    kind = 'бот' if getattr(ent, 'bot', False) else 'канал' if getattr(ent, 'broadcast', False) else \
        'группа' if getattr(ent, 'megagroup', False) or getattr(ent, 'title', None) else 'пользователь'
    title = getattr(ent, 'title', None) or " ".join(x for x in (getattr(ent, 'first_name', '') or '',
                                                                 getattr(ent, 'last_name', '') or '') if x)
    return {'kind': kind, 'title': title or name, 'id': ent.id, 'premium': bool(getattr(ent, 'premium', False))}


async def fragment(name):
    """{'status': 'available'|'auction'|'sale'|'sold'|'taken'|'unavailable', 'price': '1,234', 'url'} | None."""
    url = f"https://fragment.com/username/{name.lower()}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),
                                         headers={'User-Agent': 'Mozilla/5.0 (FRX Usernames)'}) as s:
            async with s.get(url, allow_redirects=True) as r:
                if r.status != 200:
                    return None
                final = str(r.url)
                page = await r.text()
    except Exception:
        return None
    if '/username/' not in final:
        return {'status': 'none', 'price': None, 'url': None}
    m = re.search(r'tm-section-header-status[^"]*\btm-status-(\w+)[^>]*>([^<]+)<', page)
    word = (m.group(2).strip().lower() if m else '')
    cls = m.group(1) if m else ''
    status = ('auction' if 'auction' in word else 'sale' if 'sale' in word else 'sold' if 'sold' in word
              else 'available' if 'available' in word else 'taken' if 'taken' in word or cls == 'taken'
              else 'unavailable' if 'unavail' in word or cls == 'unavail' else word or 'unknown')
    price = re.search(r'icon-ton[^>]*>\s*([\d][\d,.\s]*)<', page)
    return {'status': status, 'price': price.group(1).strip() if price else None, 'url': url}


FRAG_RU = {'auction': 'идёт аукцион', 'sale': 'продаётся', 'sold': 'продан', 'available': 'можно купить',
           'taken': 'занят (куплен)', 'unavailable': 'недоступен', 'none': 'нет на Fragment'}


async def card(name):
    rule = check_rules(name)
    if rule:
        return f"✗ <code>@{_esc(name)}</code> — {rule}", None
    st = await tg_check(name)
    if isinstance(st, tuple):
        return f"⏳ <code>@{_esc(name)}</code> — Telegram просит подождать {st[1]} с", ('flood', st[1])
    line = ''
    if st == 'free':
        line = f"🟢 <code>@{_esc(name)}</code> — <b>свободен</b>"
    elif st == 'invalid':
        line = f"✗ <code>@{_esc(name)}</code> — недопустим или зарезервирован Telegram"
    elif st == 'taken':
        o = await owner_of(name)
        line = f"🔴 <code>@{_esc(name)}</code> — занят" + (f": {o['kind']} «{_esc(o['title'])}»" if o else " (владелец скрыт)")
    elif st == 'fragment':
        line = f"💎 <code>@{_esc(name)}</code> — свободен, но Telegram продаёт его только через Fragment"
    if st in ('taken', 'fragment', 'invalid') or len(name) <= 4:
        fr = await fragment(name)
        if fr and fr['status'] != 'none':
            line += f"\n   💎 Fragment: {FRAG_RU.get(fr['status'], fr['status'])}" + (f" · {fr['price']} TON" if fr['price'] else "") \
                    + f' · <a href="{fr["url"]}">открыть</a>'
    return line, st


@decorators.on_start()
async def setup():
    await engine.create_config(key='watch_every', description='Как часто проверять имена из .un watch, минут', data_type='int',
                               default=30, valid={'min_value': 5, 'max_value': 1440})


# --------------------------------------------------------------------------- команды

def gen_names(pattern, n):
    letters, digits = string.ascii_lowercase, string.digits
    seen, out, tries = set(), [], 0
    while len(out) < n and tries < n * 50:
        tries += 1
        s = ''.join(random.choice(letters) if c == '?' else random.choice(digits) if c == '#'
                    else random.choice(letters + digits) if c == '*' else c for c in pattern)
        if s not in seen and not check_rules(s):
            seen.add(s)
            out.append(s)
    return out


@decorators.command('un', 'username', 'юзернейм')
async def un_cmd(event):
    """юзернеймы: <имя …> — проверить; gen <шаблон> [N]; watch <имя> [claim]; unwatch; list; mine"""
    raw = engine.get_args().strip()
    parts = raw.split()
    sub = parts[0].lower() if parts else ''
    if sub == 'mine':
        me = await engine.client.get_me()
        names = [u.username for u in (getattr(me, 'usernames', None) or []) if getattr(u, 'active', True)]
        if me.username and me.username not in names:
            names.insert(0, me.username)
        await engine.answer(event, visual.title("Ваши юзернеймы", sub=str(len(names))) + "\n"
                            + ("\n".join(f"▸ @{_esc(n)}" for n in names) or visual.hint("нет")))
        return
    if sub in ('list', 'список'):
        w = await db.get('watch', {}) or {}
        lines = [visual.title("Слежу", sub=str(len(w)))]
        for n, it in w.items():
            lines.append(f"▸ @{_esc(n)}" + (" · займу сам" if it.get('claim') else "")
                         + f" · с {time.strftime('%d.%m', time.localtime(it.get('since', 0)))}")
        if not w:
            lines.append(visual.hint("ничего — .un watch <имя>"))
        await engine.answer(event, "\n".join(lines))
        return
    if sub in ('watch', 'unwatch'):
        if len(parts) < 2:
            await engine.answer(event, visual.err(f"Формат: .un {sub} <имя>"))
            return
        name = parts[1].lstrip('@')
        w = await db.get('watch', {}) or {}
        if sub == 'unwatch':
            w.pop(name.lower(), None)
            await db.set('watch', w)
            await engine.answer(event, visual.ok(f"Больше не слежу за @{_esc(name)}"))
            return
        rule = check_rules(name)
        if rule:
            await engine.answer(event, visual.err(rule))
            return
        w[name.lower()] = {'name': name, 'claim': len(parts) > 2 and parts[2].lower() in ('claim', 'занять'), 'since': int(time.time())}
        await db.set('watch', w)
        await engine.answer(event, visual.ok(f"Слежу за @{_esc(name)}") + "\n"
                            + visual.hint("освободится — займу каналом «Резерв»" if w[name.lower()]['claim'] else
                                          "освободится — напишу от бота"))
        return
    if sub in ('gen', 'подбор'):
        if len(parts) < 2:
            await engine.answer(event, visual.err("Формат: .un gen frx_??? 10   (? буква, # цифра, * что угодно)"))
            return
        n = min(int(parts[2]), 25) if len(parts) > 2 and parts[2].isdigit() else 10
        cands = gen_names(parts[1].lstrip('@'), n)
        if not cands:
            await engine.answer(event, visual.err("По шаблону не получается допустимых имён"))
            return
        free = []
        for i, c in enumerate(cands, 1):
            await engine.answer(event, visual.wait(f"Проверяю {i}/{len(cands)}… свободных: {len(free)}"))
            st = await tg_check(c)
            if isinstance(st, tuple):
                await engine.answer(event, visual.warn(f"Telegram просит подождать {st[1]} с — остановился") + "\n"
                                    + ("\n".join(f"🟢 <code>@{x}</code>" for x in free) or ""))
                return
            if st == 'free':
                free.append(c)
            await asyncio.sleep(2.5)
        await engine.answer(event, visual.title("Свободные", sub=f"{len(free)} из {len(cands)}") + "\n"
                            + ("\n".join(f"🟢 <code>@{x}</code>" for x in free) or visual.hint("ни одного — попробуйте другой шаблон")))
        return
    names = [p.lstrip('@').strip(',') for p in parts if p.strip('@,')][:10]
    if not names and event.is_reply:
        r = await event.get_reply_message()
        names = re.findall(r'@([a-zA-Z][\w]{3,31})', r.raw_text or '')[:10]
    if not names:
        await engine.answer(event, visual.err("Какое имя проверить? .un durov"))
        return
    await engine.answer(event, visual.wait("Проверяю…"))
    lines = []
    for nm in names:
        text, st = await card(nm)
        lines.append(text)
        if isinstance(st, tuple):
            break
        if len(names) > 1:
            await asyncio.sleep(1.5)
    await engine.answer(event, "\n".join(lines))


@decorators.cycle(interval='5m')
async def watcher_cycle():
    w = await db.get('watch', {}) or {}
    if not w:
        return
    every = int(await engine.get_config('watch_every') or 30) * 60
    last = await db.get('last_check', 0) or 0
    if time.time() - last < every:
        return
    await db.set('last_check', int(time.time()))
    for key, it in list(w.items()):
        st = await tg_check(it['name'])
        if isinstance(st, tuple):
            break
        if st == 'free':
            note = f"🟢 Юзернейм <code>@{_esc(it['name'])}</code> освободился!"
            if it.get('claim'):
                try:
                    from telethon.tl.functions.channels import CreateChannelRequest, UpdateUsernameRequest
                    res = await engine.client(CreateChannelRequest(title=f"Резерв @{it['name']}",
                                                                   about="Имя занято через FRX Usernames", broadcast=True))
                    ch = res.chats[0]
                    await engine.client(UpdateUsernameRequest(ch, it['name']))
                    note += f"\n✓ Занял: канал «Резерв @{_esc(it['name'])}» — передайте имя куда нужно"
                except Exception as e:
                    note += f"\n✗ Занять не вышло: {_esc(e)}"
            w.pop(key, None)
            await db.set('watch', w)
            try:
                await engine.bot_send_to_owner(note)
            except Exception:
                await engine.client.send_message('me', engine.safe_html(note), parse_mode='html')
        await asyncio.sleep(3)
