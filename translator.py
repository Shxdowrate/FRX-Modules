"""
Translator — переводчик (Google Translate, без ключей).

  .tr [язык] [текст]   перевести текст или сообщение, на которое ответили. Язык — код (en, ru, de, ja…) или
                       название («английский»). Без языка — на ваш основной; если текст уже на нём — на второй.
  .trs [язык]          ответом на своё сообщение: заменить его переводом (написать иностранцу «на его языке»)
  @бот tr [язык] текст перевод в любом чате
"""

import html
import re

import aiohttp

import bot
import decorators
import engine
import visual

NAME = "Translator"
MODULE_NAME = "translator"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Переводчик Google без ключей: .tr язык — ответом или текстом; .trs — заменить своё сообщение переводом"

NAMES = {'английский': 'en', 'англ': 'en', 'русский': 'ru', 'рус': 'ru', 'немецкий': 'de', 'французский': 'fr',
         'испанский': 'es', 'итальянский': 'it', 'португальский': 'pt', 'украинский': 'uk', 'белорусский': 'be',
         'казахский': 'kk', 'китайский': 'zh-CN', 'японский': 'ja', 'корейский': 'ko', 'турецкий': 'tr', 'арабский': 'ar',
         'польский': 'pl', 'чешский': 'cs', 'иврит': 'iw', 'хинди': 'hi', 'грузинский': 'ka', 'армянский': 'hy',
         'узбекский': 'uz', 'татарский': 'tt', 'финский': 'fi', 'шведский': 'sv', 'нидерландский': 'nl', 'греческий': 'el',
         'вьетнамский': 'vi', 'тайский': 'th', 'индонезийский': 'id', 'латынь': 'la', 'эсперанто': 'eo'}
FLAG = {'en': '🇬🇧', 'ru': '🇷🇺', 'de': '🇩🇪', 'fr': '🇫🇷', 'es': '🇪🇸', 'it': '🇮🇹', 'pt': '🇵🇹', 'uk': '🇺🇦', 'be': '🇧🇾',
        'kk': '🇰🇿', 'zh-CN': '🇨🇳', 'zh': '🇨🇳', 'ja': '🇯🇵', 'ko': '🇰🇷', 'tr': '🇹🇷', 'ar': '🇸🇦', 'pl': '🇵🇱', 'cs': '🇨🇿',
        'iw': '🇮🇱', 'he': '🇮🇱', 'hi': '🇮🇳', 'ka': '🇬🇪', 'hy': '🇦🇲', 'uz': '🇺🇿', 'fi': '🇫🇮', 'sv': '🇸🇪', 'nl': '🇳🇱',
        'el': '🇬🇷', 'vi': '🇻🇳', 'th': '🇹🇭', 'id': '🇮🇩'}
LANG_RE = re.compile(r'^(?:[a-z]{2,3}(?:-[A-Za-z]{2,4})?)$')


def _esc(x):
    return html.escape(str(x), quote=False)


@decorators.on_start()
async def setup():
    await engine.create_config(key='main', description='Основной язык (на него переводится по умолчанию)', data_type='str', default='ru')
    await engine.create_config(key='second', description='Второй язык (если текст уже на основном)', data_type='str', default='en')


async def translate(text, target, source='auto'):
    params = {'client': 'gtx', 'sl': source, 'tl': target, 'dt': 't', 'q': text}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post('https://translate.googleapis.com/translate_a/single', data=params,
                          headers={'User-Agent': 'Mozilla/5.0'}) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
    out = ''.join(part[0] for part in (data[0] or []) if part and part[0])
    return out, data[2] if len(data) > 2 and isinstance(data[2], str) else source


def _lang(word):
    w = (word or '').strip().lower()
    if w in NAMES:
        return NAMES[w]
    if LANG_RE.match(w):
        return 'zh-CN' if w == 'zh' else w
    return None


async def _smart(text, target=None):
    main, second = await engine.get_config('main'), await engine.get_config('second')
    tr, src = await translate(text, target or main)
    if not target and src == main:
        tr, src = await translate(text, second)
        target = second
    return tr, src, target or main


def _head(src, dst):
    return f"{FLAG.get(src, '🌐')} <code>{_esc(src)}</code> → {FLAG.get(dst, '🌐')} <code>{_esc(dst)}</code>"


async def _parse(event):
    args = engine.get_args().strip()
    first, _, rest = args.partition(' ')
    target = _lang(first)
    text = rest if target else args
    if not text.strip() and event.is_reply:
        r = await event.get_reply_message()
        text = (r.raw_text or '') if r else ''
    return target, text.strip()


@decorators.command('tr', 'translate', 'перевод')
async def tr_cmd(event):
    """перевести: [язык] [текст] (или ответом на сообщение)"""
    target, text = await _parse(event)
    if not text:
        await engine.answer(event, visual.err("Что переводить?") + "\n" + visual.hint(".tr en привет · ответом: .tr"))
        return
    try:
        tr, src, dst = await _smart(text, target)
    except Exception as e:
        await engine.answer(event, visual.err("Перевод не удался") + f"\n<code>{_esc(e)}</code>")
        return
    await engine.answer(event, f"{_head(src, dst)}\n\n{_esc(tr)}")


@decorators.command('trs')
async def trs_cmd(event):
    """заменить своё сообщение переводом: ответом, [язык]"""
    r = await event.get_reply_message() if event.is_reply else None
    target = _lang(engine.get_args().strip()) or await engine.get_config('second')
    if r is None or not r.out or not r.raw_text:
        await engine.answer(event, visual.err("Ответьте на своё сообщение с текстом"))
        return
    try:
        tr, _src = await translate(r.raw_text, target)
    except Exception as e:
        await engine.answer(event, visual.err("Перевод не удался") + f"\n<code>{_esc(e)}</code>")
        return
    await r.edit(tr)
    await event.delete()


@bot.inline_query('tr', 'translate', access='public')
async def tr_inline(query):
    """перевод: [язык] текст"""
    first, _, rest = query.text.partition(' ')
    target = _lang(first)
    text = (rest if target else query.text).strip()
    if not text:
        return [engine.InlineResult("Перевод: tr [язык] текст", text=visual.hint("<code>@бот tr en привет</code>"))]
    try:
        tr, src, dst = await _smart(text, target)
    except Exception as e:
        return [engine.InlineResult("Перевод не удался", description=str(e)[:100], text=visual.err("Перевод не удался"))]
    return [engine.InlineResult(f"{FLAG.get(dst, '🌐')} {tr[:80]}", description=f"{src} → {dst}", text=_esc(tr)),
            engine.InlineResult("С оригиналом", description=text[:100], text=f"{_esc(text)}\n\n{_head(src, dst)}\n{_esc(tr)}")]
