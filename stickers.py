"""
Stickers — свои стикерпаки и эмодзи-паки прямо из чата: ответьте на что угодно — FRX сам приведёт к правилам Telegram.

  .kang [эмодзи]        ответом на стикер / фото / картинку / видео / GIF / кружок — в ваш стикерпак
  .kemoji [эмодзи]      то же, но в эмодзи-пак (100×100). Ответом на сообщение с премиум-эмодзи — забрать их все
  .unkang               ответом на стикер или эмодзи из вашего пака — убрать его из пака
  .kangpack [название]  ответом на стикер — скопировать весь чужой пак к себе
  .packs                ваши паки: какой текущий, новый пак, удалить

Что делается само: картинки — webp, большая сторона 512 (эмодзи — 100×100, по центру); видео, GIF и кружки — видеостикер
webm VP9 без звука, до 3 секунд и 30 к/с, битрейт подбирается, пока файл не станет ≤ 256 КБ; анимированные .tgs
переносятся как есть. Заполнился пак (120 стикеров / 200 эмодзи) — заводится следующий.
Паки создаёт бот FRX, а владелец — вы: они видны в Telegram и правятся там же, в @Stickers.
"""

import asyncio
import html
import random
import string

import db
import decorators
import engine
import media
import visual

NAME = "Stickers"
MODULE_NAME = "stickers"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Свои стикерпаки и эмодзи-паки: .kang и .kemoji ответом на что угодно, всё конвертируется само"

KINDS = {'regular': 'стикеры', 'custom_emoji': 'эмодзи'}


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='default_emoji', description='Эмодзи стикера, если вы его не указали и у исходника его нет',
                               data_type='str', default='✨')
    await engine.create_config(key='send_result', description='После .kang присылать стикер из пака в чат (вместо сообщения «добавлен»)',
                               data_type='bool', default=False)


async def cfg(key):
    try:
        return await engine.get_config(key, module_name=MODULE_NAME)
    except KeyError:
        return None


async def dget(key, default=None):
    return await db.gget(f"{MODULE_NAME}:{key}", default)


async def dset(key, value):
    return await db.gset(f"{MODULE_NAME}:{key}", value)


def _bot():
    bot = getattr(engine, '_aiogram_bot', None)
    if bot is None:
        raise RuntimeError("паки создаёт бот FRX — включите его (стационарный бот в настройках)")
    return bot


# --------------------------------------------------------------------------- эмодзи из текста

def _emoji_cp(cp):
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF or 0x2300 <= cp <= 0x23FF
            or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299))


def emojis_in(text):
    """Все эмодзи из строки целыми последовательностями (ZWJ, тон кожи, флаги)."""
    out, i, n = [], 0, len(text or '')
    while i < n:
        cp = ord(text[i])
        if _emoji_cp(cp) or 0x1F1E6 <= cp <= 0x1F1FF:
            j = i + 1
            if 0x1F1E6 <= cp <= 0x1F1FF and j < n and 0x1F1E6 <= ord(text[j]) <= 0x1F1FF:
                j += 1
            while j < n:
                c2 = ord(text[j])
                if c2 in (0xFE0F, 0x20E3) or 0x1F3FB <= c2 <= 0x1F3FF or 0xE0020 <= c2 <= 0xE007F:
                    j += 1
                elif c2 == 0x200D and j + 1 < n:
                    j += 2
                else:
                    break
            out.append(text[i:j])
            i = j
        else:
            i += 1
    return out


# --------------------------------------------------------------------------- паки пользователя

def _rand():
    return 'p' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))


async def _owner():
    owner = getattr(engine, '_owner_id', None)
    if owner is None:
        owner = (await engine.client.get_me()).id
    return owner


async def packs():
    """[{'base', 'title', 'kind', 'names': [...]}] — паки, которые FRX завёл для вас."""
    return list(await dget('packs', []) or [])


async def save_packs(lst):
    await dset('packs', lst)


async def current(kind):
    """Текущий пак нужного вида; нет — заводим запись (сам пак Telegram создастся с первым стикером)."""
    lst = await packs()
    base = await dget(f'cur_{kind}')
    for p in lst:
        if p['base'] == base and p['kind'] == kind:
            return p
    for p in lst:
        if p['kind'] == kind:
            await dset(f'cur_{kind}', p['base'])
            return p
    me = await engine.client.get_me()
    who = me.first_name or 'FRX'
    owner = await _owner()
    p = {'base': f"frx{'e' if kind == 'custom_emoji' else ''}{owner}", 'kind': kind, 'names': [],
         'title': f"{who} — {'эмодзи' if kind == 'custom_emoji' else 'стикеры'}"[:60]}
    lst.append(p)
    await save_packs(lst)
    await dset(f'cur_{kind}', p['base'])
    return p


async def new_pack(kind, title):
    lst = await packs()
    p = {'base': _rand(), 'kind': kind, 'names': [], 'title': title.strip()[:60] or 'Мой пак'}
    lst.append(p)
    await save_packs(lst)
    await dset(f'cur_{kind}', p['base'])
    return p


async def add_to(p, data, fmt, emojis):
    """В пак p (с переходом на следующий, если полон). → результат media.pack_add."""
    res = await media.pack_add(_bot(), await _owner(), p['base'], p['title'], data, fmt, emojis, kind=p['kind'])
    lst = await packs()
    for x in lst:
        if x['base'] == p['base'] and res['name'] not in x['names']:
            x['names'].append(res['name'])
    await save_packs(lst)
    return res


# --------------------------------------------------------------------------- что лежит в сообщении → данные для пака

def _attrs(doc):
    from telethon.tl.types import DocumentAttributeCustomEmoji, DocumentAttributeSticker, DocumentAttributeVideo
    out = {'alt': None, 'set': None, 'emoji_kind': False, 'video': False, 'sticker': False}
    for a in getattr(doc, 'attributes', None) or []:
        if isinstance(a, DocumentAttributeSticker):
            out['alt'], out['set'], out['sticker'] = a.alt, a.stickerset, True
        elif isinstance(a, DocumentAttributeCustomEmoji):
            out['alt'], out['set'], out['emoji_kind'], out['sticker'] = a.alt, a.stickerset, True, True
        elif isinstance(a, DocumentAttributeVideo):
            out['video'] = True
    return out


async def prepare_doc(doc, kind, data=None):
    """Документ-стикер/эмодзи → (bytes, fmt, alt) под нужный вид пака."""
    a = _attrs(doc)
    mime = doc.mime_type or ''
    want_emoji = kind == 'custom_emoji'
    same = a['sticker'] and a['emoji_kind'] == want_emoji        # готовый стикер того же вида — без пересжатия
    if data is None:
        data = await engine.client.download_media(doc, file=bytes)
    if mime == 'application/x-tgsticker':
        if a['emoji_kind'] == want_emoji:
            return data, 'animated', a['alt']
        # .tgs в другой размер не пересчитать без рендера Lottie — берём превью картинкой
        thumb = await engine.client.download_media(doc, file=bytes, thumb=-1)
        if not thumb:
            raise RuntimeError("у анимированного стикера нет превью — перенести в эмодзи не получится")
        return await (media.image_to_emoji if want_emoji else media.image_to_sticker)(thumb), 'static', a['alt']
    if mime == 'video/webm':
        if same and len(data) <= 256 * 1024:
            return data, 'video', a['alt']
        return await media.video_to_sticker(data, 'in.webm', emoji=want_emoji), 'video', a['alt']
    if mime.startswith('image/'):
        return await (media.image_to_emoji if want_emoji else media.image_to_sticker)(data), 'static', a['alt']
    if mime.startswith('video/') or a['video']:
        return await media.video_to_sticker(data, 'in.mp4', emoji=want_emoji), 'video', a['alt']
    raise RuntimeError(f"не знаю, как сделать стикер из {mime or 'этого файла'}")


async def prepare_msg(msg, kind):
    want_emoji = kind == 'custom_emoji'
    if msg.photo:
        data = await engine.client.download_media(msg, file=bytes)
        return await (media.image_to_emoji if want_emoji else media.image_to_sticker)(data), 'static', None
    if msg.document:
        return await prepare_doc(msg.document, kind)
    raise RuntimeError("ответьте на стикер, фото, картинку, видео, GIF или кружок")


def _custom_ids(msg):
    from telethon.tl.types import MessageEntityCustomEmoji
    seen, out = set(), []
    for e in msg.entities or []:
        if isinstance(e, MessageEntityCustomEmoji) and e.document_id not in seen:
            seen.add(e.document_id)
            out.append(e.document_id)
    return out


async def _custom_docs(ids):
    from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
    return list(await engine.client(GetCustomEmojiDocumentsRequest(document_id=ids))) if ids else []


def _emojis(args, alt):
    got = emojis_in(args)[:20]
    return got or ([alt] if alt else None)


async def _default_emoji():
    return emojis_in(await cfg('default_emoji') or '✨')[:1] or ['✨']


def _done_text(res, kind, n=1, preview=None):
    what = 'эмодзи' if kind == 'custom_emoji' else 'стикер'
    head = f"Добавлено {n} {what}" if n > 1 else f"{what.capitalize()} добавлен"
    lines = [visual.ok(head + (" — пак создан" if res.get('created') else "")),
             f"<a href=\"{res['link']}\">{_esc(res['title'])}</a>"]
    if preview:
        lines.append(preview)
    return "\n".join(lines)


async def _last_emoji_tag(name):
    """Последний эмодзи пака в виде <tg-emoji> — чтобы показать его прямо в ответе."""
    try:
        doc = await media.pack_last_document(engine.client, name)
        alt = _attrs(doc)['alt'] or '✨'
        return f'<tg-emoji emoji-id="{doc.id}">{alt}</tg-emoji>'
    except Exception:
        return None


# --------------------------------------------------------------------------- команды

async def kang(event, kind):
    args = engine.get_args()
    r = await event.get_reply_message() if event.is_reply else None
    if r is None:
        await engine.answer(event, visual.err("Ответьте на стикер, фото, видео или GIF") + "\n" + visual.hint(
            ".kang 😎 — в стикерпак · .kemoji — в эмодзи-пак · .packs — ваши паки"))
        return
    await engine.answer(event, visual.wait("Конвертирую…"))
    try:
        p = await current(kind)
        if kind == 'custom_emoji' and not (r.photo or r.document) and _custom_ids(r):
            docs = await _custom_docs(_custom_ids(r)[:50])
            res, n = None, 0
            for i, doc in enumerate(docs):
                data, fmt, alt = await prepare_doc(doc, kind)
                res = await add_to(p, data, fmt, _emojis(args, alt) or await _default_emoji())
                n += 1
                if len(docs) > 3 and i % 3 == 2:
                    await engine.answer(event, visual.wait(f"Добавляю эмодзи {i + 1}/{len(docs)}…"))
            await engine.answer(event, _done_text(res, kind, n))
            return
        if not (r.photo or r.document) and _custom_ids(r):          # премиум-эмодзи → в стикер (увеличим)
            docs = await _custom_docs(_custom_ids(r)[:1])
            data, fmt, alt = await prepare_doc(docs[0], kind)
        else:
            data, fmt, alt = await prepare_msg(r, kind)
        res = await add_to(p, data, fmt, _emojis(args, alt) or await _default_emoji())
    except Exception as e:
        await engine.answer(event, visual.err("Не добавилось") + f"\n<code>{_esc(str(e)[:300])}</code>")
        return
    if kind == 'regular' and await cfg('send_result'):
        try:
            doc = await media.pack_last_document(engine.client, res['name'])
            await engine.client.send_file(event.chat_id, doc, reply_to=event.reply_to_msg_id)
            await event.delete()
            return
        except Exception:
            pass
    preview = await _last_emoji_tag(res['name']) if kind == 'custom_emoji' else None
    await engine.answer(event, _done_text(res, kind, 1, preview))


@decorators.command('kang', 'стикер', 'steal')
async def kang_cmd(event):
    """в ваш стикерпак: ответом на стикер/фото/видео/GIF/кружок; [эмодзи] — под какими эмодзи искать стикер"""
    await kang(event, 'regular')


@decorators.command('kemoji', 'эмодзи', 'kangemoji')
async def kemoji_cmd(event):
    """в ваш эмодзи-пак (100×100): ответом на медиа; на сообщение с премиум-эмодзи — забрать их все; [эмодзи]"""
    await kang(event, 'custom_emoji')


async def _set_of(doc):
    """Стикер/эмодзи → (short_name пака, список документов пака, индекс документа)."""
    from telethon.tl.functions.messages import GetStickerSetRequest
    st = _attrs(doc)['set']
    if st is None or type(st).__name__ == 'InputStickerSetEmpty':
        return None, [], -1
    res = await engine.client(GetStickerSetRequest(stickerset=st, hash=0))
    ids = [d.id for d in res.documents]
    return res.set.short_name, res, ids.index(doc.id) if doc.id in ids else -1


@decorators.command('unkang', 'delsticker', 'убрать')
async def unkang_cmd(event):
    """убрать стикер или эмодзи из вашего пака: ответом на него"""
    r = await event.get_reply_message() if event.is_reply else None
    doc = None
    if r is not None:
        if r.sticker:
            doc = r.document
        elif _custom_ids(r):
            docs = await _custom_docs(_custom_ids(r)[:1])
            doc = docs[0] if docs else None
    if doc is None:
        await engine.answer(event, visual.err("Ответьте на стикер или эмодзи из вашего пака"))
        return
    try:
        bot = _bot()
        me = await bot.get_me()
        name, _, idx = await _set_of(doc)
        if not name or not name.lower().endswith(f"_by_{me.username}".lower()) or idx < 0:
            raise RuntimeError("это не ваш пак (паки FRX называются …_by_" + me.username + ")")
        st = await bot.get_sticker_set(name)
        await bot.delete_sticker_from_set(st.stickers[idx].file_id)
    except Exception as e:
        await engine.answer(event, visual.err("Не убралось") + f"\n<code>{_esc(str(e)[:300])}</code>")
        return
    await engine.answer(event, visual.ok(f"Убрано из «{_esc(st.title)}»") + f" · осталось {len(st.stickers) - 1}")


@decorators.command('kangpack', 'копипак')
async def kangpack_cmd(event):
    """скопировать чужой пак целиком к себе: ответом на любой стикер/эмодзи из него; [название] нового пака"""
    args = engine.get_args().strip()
    r = await event.get_reply_message() if event.is_reply else None
    doc = None
    if r is not None:
        if r.sticker:
            doc = r.document
        elif _custom_ids(r):
            docs = await _custom_docs(_custom_ids(r)[:1])
            doc = docs[0] if docs else None
    if doc is None:
        await engine.answer(event, visual.err("Ответьте на стикер или эмодзи из пака, который копировать"))
        return
    try:
        _bot()
        _, res, _ = await _set_of(doc)
        if not res:
            raise RuntimeError("стикер не из пака")
    except Exception as e:
        await engine.answer(event, visual.err("Пак не открылся") + f"\n<code>{_esc(str(e)[:300])}</code>")
        return
    kind = 'custom_emoji' if getattr(res.set, 'emojis', False) else 'regular'
    alts = {}
    for pk in res.packs:
        for did in pk.documents:
            alts.setdefault(did, []).append(pk.emoticon)
    docs = list(res.documents)[:media.PACK_LIMIT[kind] * 3]
    p = await new_pack(kind, args or res.set.title)
    first, done, skipped, last = None, 0, 0, 0.0
    from aiogram.exceptions import TelegramRetryAfter
    for i, d in enumerate(docs):
        try:
            data, fmt, alt = await prepare_doc(d, kind)
            ems = (alts.get(d.id) or ([alt] if alt else None) or await _default_emoji())[:20]
            for _ in range(3):
                try:
                    out = await add_to(p, data, fmt, ems)
                    break
                except TelegramRetryAfter as fl:
                    await asyncio.sleep(fl.retry_after + 1)
            else:
                raise RuntimeError("Telegram просит подождать")
            first = first or out
            done += 1
        except Exception as e:
            skipped += 1
            engine.tlog(f"[stickers] kangpack {d.id}: {e}")
        now = asyncio.get_event_loop().time()
        if now - last > 2.5:
            last = now
            pct = int((i + 1) * 100 / len(docs))
            await engine.answer(event, visual.wait(f"Копирую «{_esc(res.set.title)}»: {i + 1}/{len(docs)}") + "\n"
                                + engine.progress_bar(pct, 14, show_percent=True))
    if not first:
        await engine.answer(event, visual.err("Ничего не скопировалось — подробности в логах"))
        return
    lst = await packs()
    names = next((x['names'] for x in lst if x['base'] == p['base']), [first['name']])
    links = " · ".join(f"<a href=\"{media._pack_link(n, kind)}\">{'пак ' + str(k + 1) if len(names) > 1 else _esc(p['title'])}</a>"
                       for k, n in enumerate(names))
    await engine.answer(event, visual.ok(f"Скопировано {done} из {len(docs)}") + (f" · пропущено {skipped}" if skipped else "")
                        + "\n" + links + "\n" + visual.hint("Этот пак теперь текущий — .kang кладёт сюда; сменить: .packs"))


@decorators.command('newpack', 'новыйпак')
async def newpack_cmd(event):
    """новый пак: .newpack Название — стикеры, .newpack emoji Название — эмодзи (создастся с первым .kang)"""
    args = engine.get_args().strip()
    kind = 'regular'
    first = args.split(maxsplit=1)[0].lower() if args else ''
    if first in ('emoji', 'эмодзи', 'e'):
        kind, args = 'custom_emoji', args.split(maxsplit=1)[1] if len(args.split(maxsplit=1)) > 1 else ''
    if not args:
        await engine.answer(event, visual.err("Как назвать пак?") + "\n" + visual.hint(".newpack Мемы · .newpack emoji Реакции"))
        return
    p = await new_pack(kind, args)
    await engine.answer(event, visual.ok(f"Пак «{_esc(p['title'])}» — теперь текущий") + "\n" + visual.hint(
        f"он появится в Telegram с первым {'.kemoji' if kind == 'custom_emoji' else '.kang'}"))


@decorators.command('packs', 'паки')
async def packs_cmd(event):
    """ваши стикер- и эмодзи-паки: выбрать текущий, создать, удалить"""
    state = {'confirm': None}

    async def info():
        bot = _bot()
        lst = await packs()

        async def one(n):
            try:
                st = await bot.get_sticker_set(n)
                return n, len(st.stickers), st.title
            except Exception:
                return n, None, None
        names = [n for p in lst for n in p['names']]
        got = {n: (c, t) for n, c, t in await asyncio.gather(*(one(n) for n in names))}
        return lst, got

    async def render():
        lst, got = await info()
        cur = {k: await dget(f'cur_{k}') for k in KINDS}
        out = [visual.title("Stickers", sub="ваши паки")]
        btns = []
        for kind, label in KINDS.items():
            mine = [p for p in lst if p['kind'] == kind]
            out.append("")
            out.append(visual.section(label.capitalize()))
            if not mine:
                out.append(visual.item("пока нет — " + (".kemoji" if kind == 'custom_emoji' else ".kang") + " ответом на картинку"))
            for p in mine:
                i = lst.index(p)
                star = "★ " if cur[kind] == p['base'] else ""
                parts = []
                for n in p['names']:
                    c, t = got.get(n, (None, None))
                    if c is None:
                        continue
                    parts.append(f"<a href=\"{media._pack_link(n, kind)}\">{_esc(t)}</a> · {c}")
                body = ", ".join(parts) if parts else "<i>ещё пустой — появится с первым стикером</i>"
                out.append(visual.item(f"{star}<b>{_esc(p['title'])}</b> — {body}"))
                row = [(("★ " if star else "") + p['title'][:24], f"stk:cur:{i}", 'primary' if star else None),
                       ("🗑", f"stk:del:{i}", 'danger')]
                btns.append([b if b[2] else b[:2] for b in row])
        if state['confirm'] is not None and state['confirm'] < len(lst):
            p = lst[state['confirm']]
            out += ["", visual.warn(f"Удалить «{_esc(p['title'])}» из Telegram целиком? Это не отменить.")]
            btns = [[("Да, удалить", f"stk:yes:{state['confirm']}", 'danger'), ("Нет", "stk:no")]]
            return "\n".join(out), btns
        out += ["", visual.hint(".kang / .kemoji — в текущий (★) · .kangpack — скопировать чужой")]
        btns.append([engine.InputButton("＋ Стикерпак", "new_regular", hint="Название нового стикерпака"),
                     engine.InputButton("＋ Эмодзи-пак", "new_custom_emoji", hint="Название нового эмодзи-пака")])
        btns.append([(visual.btn('Закрыть', 'close'), "stk:close")])
        return "\n".join(out), btns

    try:
        text, btns = await render()
    except Exception as e:
        await engine.answer(event, visual.err(str(e)))
        return
    form = await engine.create_inlineform(event.chat_id, text, buttons=btns)
    await event.delete()

    @form.on_click(data_startswith='stk:')
    async def _(click):
        _, op, *rest = click.data.split(':')
        lst = await packs()
        if op == 'close':
            await form.delete()
            return
        i = int(rest[0]) if rest and rest[0].isdigit() else -1
        if op == 'cur' and 0 <= i < len(lst):
            await dset(f"cur_{lst[i]['kind']}", lst[i]['base'])
        elif op == 'del' and 0 <= i < len(lst):
            state['confirm'] = i
        elif op == 'no':
            state['confirm'] = None
        elif op == 'yes' and 0 <= i < len(lst):
            state['confirm'] = None
            p = lst.pop(i)
            for n in p['names']:
                try:
                    await _bot().delete_sticker_set(n)
                except Exception as e:
                    engine.tlog(f"[stickers] delete {n}: {e}")
            await save_packs(lst)
            if await dget(f"cur_{p['kind']}") == p['base']:
                await dset(f"cur_{p['kind']}", None)
            await click.answer("Пак удалён")
        t, b = await render()
        await form.edit(t, buttons=b)

    for kind in KINDS:
        def make(kind):
            @form.on_input(data=f'new_{kind}')
            async def _(inp):
                v = (inp.value or '').strip()
                if v:
                    await new_pack(kind, v)
                t, b = await render()
                await form.edit(t, buttons=b)
        make(kind)
