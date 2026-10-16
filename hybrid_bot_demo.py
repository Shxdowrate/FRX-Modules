"""
examples/hybrid_bot_module.py

Пример ГИБРИДНОГО модуля — один файл, два набора хэндлеров:
    import decorators   -> хэндлеры ЮЗЕРБОТА (Telethon), как обычно
    import bot           -> хэндлеры СТАЦИОНАРНОГО бота (AIOgram), новые
                            декораторы из core/bot_decorators.py

`bot.*` НЕ влияют на юзербота — это полностью отдельный, параллельный
набор регистраций внутри Core (см. core/engine.py: bot_command_map,
bot_watchers_bot/bot_watchers_userbot, bot_telegram_events,
bot_on_error_handlers). Событие, которое получают bot.command()/
bot.watcher()/bot.telegram_event(), ВСЕГДА в стиле Telethon — либо
настоящее telethon-событие (сторона userbot), либо Telethon-совместимая
обёртка над aiogram-объектом (сторона bot, core/bot_events.py:
BotMessageEvent/BotChatMemberEvent) — event.raw_text/.text/.sender_id/
.chat_id/.out/.is_private/.is_group/await event.reply()/.respond()/
.edit()/.delete()/.download_media() работают одинаково независимо от
источника.

Чтобы протестировать: задайте aiogram_token в config.json, положите этот
файл в modules/loaded/ (или .loadmod им в юзерботе) — bot.* хэндлеры
заработают сразу же, если бот уже поднят, либо сразу после его старта.
"""

import engine
import decorators
import bot          # <-- новый модуль, доступен так же, как decorators/db/visual
import visual

NAME = "Hybrid Demo"
MODULE_NAME = "hybrid_bot_demo"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Демонстрация bot.command/watcher/on_start/on_load/cycle/telegram_event/on_error"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Userbot/blob/main/examples/hybrid_bot_demo.py"


# --------------------------------------------------------------------- #
# Хэндлеры ЮЗЕРБОТА (обычные, ничего нового)
# --------------------------------------------------------------------- #

@decorators.command('hybridping')
async def userbot_ping(event):
    """.hybridping — обычная команда юзербота, для сравнения"""
    await engine.answer(event, f"{visual.visual('status_success')} Юзербот на связи!")


# --------------------------------------------------------------------- #
# Хэндлеры СТАЦИОНАРНОГО БОТА — новые декораторы `import bot`
# --------------------------------------------------------------------- #

@bot.command('ping', 'пинг', access='public')
async def bot_ping(event):
    """/ping — доступна всем, кто пишет стационарному боту"""
    await event.reply("Pong! (стационарный бот)")


@bot.command('whoami', access='self')
async def bot_whoami(event):
    """/whoami — только владельцу юзербота (сверяется с core._owner_id)"""
    await event.reply(f"Ваш Telegram ID: {event.sender_id}")


@bot.watcher(watch='all', access='public')
async def watch_everywhere(event):
    """
    Следит за сообщениями и стационарного бота, и юзербота одновременно.
    event.origin подскажет, кто именно поймал сообщение ('bot'/'userbot').
    Если оба состоят в одном чате и увидели одно и то же сообщение —
    сработает только один раз (дедупликация по chat_id+message_id).

    Update 1.19: раньше здесь был engine.tlog() на КАЖДОЕ сообщение —
    это тот самый "бесконечный" мусор в терминале ("новое сообщение в
    чате ..."), который забивал вывод при активном использовании
    юзербота. Демо-хэндлер оставлен (пример watch='all'), но без лога
    на каждое срабатывание — используйте .tlog()/print() здесь сами,
    если понадобится, для отладки.
    """
    pass


@bot.on_start()
async def bot_on_start():
    """Срабатывает один раз, когда И юзербот, И стационарный бот полностью готовы."""
    engine.tlog(f"{visual.visual('module')} '{NAME}': стационарный бот подключён, можно работать")


@bot.on_load()
async def bot_on_load(module_info):
    """Срабатывает при загрузке ЛЮБОГО модуля — как decorators.on_load(), но для стороны бота."""
    if module_info.module_name != MODULE_NAME:
        return
    engine.tlog(f"{visual.visual('info')} '{NAME}' загружен, bot.*-хэндлеры зарегистрированы")


@bot.cycle(interval='1h')
async def bot_hourly():
    """Раз в час — только пока стационарный бот подключён"""
    engine.tlog(f"{visual.visual('info')} Стационарный бот: почасовая проверка ({NAME})")


@bot.telegram_event()
async def bot_membership_changed(event):
    """Изменение статуса участника в чате, где есть стационарный бот"""
    if event.is_bot_event:
        engine.tlog(f"{visual.visual('info')} Статус САМОГО бота в чате {event.chat_id}: {event.old_status} -> {event.new_status}")
    elif event.user_joined:
        engine.tlog(f"{visual.visual('status_success')} Пользователь {event.user_id} присоединился к чату {event.chat_id}")
    elif event.user_left:
        engine.tlog(f"{visual.visual('status_error')} Пользователь {event.user_id} покинул чат {event.chat_id}")


@bot.on_error()
async def bot_report_error(error_text, context):
    """Ошибки ТОЛЬКО из bot.*-хэндлеров (юзербот сюда не попадает)"""
    engine.tlog(f"{visual.visual('status_error')} Ошибка стационарного бота в модуле '{context['module']}' ({context['function']}): {error_text}")
