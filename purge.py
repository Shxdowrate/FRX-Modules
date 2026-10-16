"""
Purge — чистка сообщений.

  .purge            ответом на сообщение: удалить ВАШИ сообщения от него и до команды
  .purge all        то же, но все сообщения (в личке — у обоих; в группе — нужны права админа)
  .purge <N>        без ответа: ваши последние N сообщений
  .spurge …         то же самое без отчёта
  .del              ответом: удалить одно сообщение (и команду)
  .edit <текст>     ответом на ваше сообщение: переписать его
"""

import asyncio

import decorators
import engine
import visual

NAME = "Purge"
MODULE_NAME = "purge"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Чистка: .purge ответом — ваши сообщения до этого места; all — все (с правами); .del — одно"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/purge.py"


async def _purge(event, silent):
    args = engine.get_args().strip().lower().split()
    everyone = 'all' in args or 'все' in args
    count = next((int(a) for a in args if a.isdigit()), None)
    client = engine.client
    ids = []
    if event.is_reply:
        start = event.reply_to_msg_id
        async for m in client.iter_messages(event.chat_id, min_id=start - 1, max_id=event.id, reverse=True):
            if everyone or m.out:
                ids.append(m.id)
    elif count:
        async for m in client.iter_messages(event.chat_id, max_id=event.id, from_user='me', limit=count):
            ids.append(m.id)
    else:
        await engine.answer(event, visual.err("Ответьте на сообщение, с которого чистить, или укажите число") + "\n"
                            + visual.hint(".purge ответом · .purge 20 · .purge all ответом"))
        return
    ids.append(event.id)
    done = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            await client.delete_messages(event.chat_id, chunk, revoke=True)
            done += len(chunk)
        except Exception as e:
            if not silent:
                await client.send_message(event.chat_id, visual.err("Удалить все не вышло") + f"\n<code>{e}</code>", parse_mode='html')
            return
        await asyncio.sleep(0.3)
    if not silent:
        note = await client.send_message(event.chat_id, visual.ok(f"Удалено сообщений: {done - 1}"), parse_mode='html')
        await asyncio.sleep(2.5)
        try:
            await note.delete()
        except Exception:
            pass


@decorators.command('purge', 'p')
async def purge_cmd(event):
    """удалить ваши сообщения: ответом (до команды) или <число>; all — все сообщения (нужны права)"""
    await _purge(event, silent=False)


@decorators.command('spurge')
async def spurge_cmd(event):
    """то же, что .purge, без отчёта"""
    await _purge(event, silent=True)


@decorators.command('del', 'd')
async def del_cmd(event):
    """удалить сообщение: ответом"""
    ids = [event.id]
    if event.is_reply:
        ids.insert(0, event.reply_to_msg_id)
    try:
        await engine.client.delete_messages(event.chat_id, ids, revoke=True)
    except Exception as e:
        await engine.answer(event, visual.err("Не удалось удалить") + f"\n<code>{e}</code>")


@decorators.command('edit')
async def edit_cmd(event):
    """переписать ваше сообщение: ответом, <новый текст>"""
    text = engine.get_args()
    r = await event.get_reply_message() if event.is_reply else None
    if r is None or not r.out or not text:
        await engine.answer(event, visual.err("Ответьте на своё сообщение и напишите новый текст"))
        return
    await r.edit(text)
    await event.delete()
