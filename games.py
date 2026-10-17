"""
Games — 14 настольных и карточных игр на двоих прямо в чате, на инлайн-кнопках.

  .game (.g)               список игр кнопками — выберите и позовите соперника
  .game <игра> @user       вызвать конкретного человека (или ответом на его сообщение)
  .game <игра>             открытая игра: первый, кто нажмёт «Войти», становится соперником
  .games                   то же, что .game без аргументов

Игры: крестики-нолики, 4 в ряд, морской бой, шашки, шахматы, реверси, сапёр-дуэль,
камень-ножницы-бумага, виселица, быки и коровы, блэкджек, ним (спички), точки-квадраты, дурак.

Все игры — на двоих живых игроков (ботов звать нельзя). Ход делает только тот, чья очередь; кнопки чужой
игры не сработают. «Сдаться» завершает партию, «Реванш» начинает заново теми же соперниками.

Скрытое (карты в дураке и блэкджеке, свой флот в морском бое) показывается каждому игроку лично — в личке со
стационарным ботом: кнопка «🃏 Мои карты» под партией открывает бота, он присылает ваш личный экран с кнопками.
В дураке ходы делаются там же (карты соперника не видны никому).

Секреты (слово в виселице, число в быках и коровах) в общий чат не попадают никогда:
  • кнопка «🤫 Загадать в личке» — бот пришлёт личный экран, слово пишется ему сообщением, число — кнопками;
  • или прямо в чате, как шёпот: наберите «@бот загадать <слово|число>» и выберите вариант — в чат уйдёт
    только «🤫 … загадал(а)», без самого секрета;
  • или «🎲 Случайно» — загадает бот.
В виселице перед партией выбирается, кто загадывает (или слово загадывает бот, а угадывают оба по очереди).
"""

import asyncio
import html
import random
import secrets
import time

import bot
import decorators
import engine
import visual

NAME = "Games"
MODULE_NAME = "games"
VERSION = "1.2"
AUTHOR = "FRX"
DESCRIPTION = "14 игр на двоих на инлайн-кнопках: крестики-нолики, 4 в ряд, морской бой, шашки, шахматы, реверси, сапёр, КНБ, виселица, быки и коровы, блэкджек, ним, точки-квадраты, дурак"
TG_URL = "https://t.me/mescr_m"
GIT_URL = "https://github.com/Shxdowrate/FRX-Modules/blob/main/games.py"

MATCHES = {}          # mid -> Match
GAMES = {}            # key -> Game class (заполняется регистрацией в конце файла)
_BOT = {}             # кэш: username стационарного бота


def _esc(x):
    return html.escape(str(x), quote=False)


def _mention(pid, name):
    return f'<a href="tg://user?id={pid}">{_esc(name)}</a>'


def _who(user):
    return (" ".join(x for x in (getattr(user, 'first_name', ''), getattr(user, 'last_name', '')) if x)
            or getattr(user, 'username', None) or str(getattr(user, 'id', '?')))[:40]


@decorators.on_start()
async def setup():
    await engine.create_config('timeout', 'Через сколько минут бездействия партия закрывается', 'int', 15,
                               valid={'min_value': 1, 'max_value': 240})
    await engine.create_config('open_wait', 'Сколько минут ждать соперника в открытой игре', 'int', 10,
                               valid={'min_value': 1, 'max_value': 120})


async def _bot_username():
    """@username стационарного бота — для ссылки «Мои карты» (t.me/<бот>?start=…)."""
    if not _BOT.get('username'):
        try:
            me = await engine.bot.raw.get_me()
            _BOT['username'] = me.username
        except Exception:
            pass
    return _BOT.get('username')


@decorators.on_unload()
async def cleanup():
    for m in list(MATCHES.values()):
        t = m.__dict__.get('_timer')
        if t is not None and not t.done():
            t.cancel()
    MATCHES.clear()


# =========================================================================== базовый класс игры

class Game:
    KEY = ''
    NAME = ''
    EMOJI = '🎮'
    HELP = ''
    # Переопределяемые методы: setup(), body(), board_buttons(), handle(pid, tok)

    def __init__(self, match):
        self.m = match
        self.finished = False
        self.winner = None          # id победителя | 'draw' | None
        self.turn = 0               # индекс игрока, чей ход
        self.setup()

    # --- доступ к игрокам ---
    @property
    def players(self):
        return self.m.players

    def pid(self, i):
        return self.players[i]['id']

    def pname(self, i):
        return self.players[i]['name']

    def idx(self, pid):
        for i, p in enumerate(self.players):
            if p['id'] == pid:
                return i
        return -1

    def opp(self, i):
        return 1 - i

    def mark(self, i):
        return ('❌', '⭕')[i] if i in (0, 1) else '·'

    def cb(self, tok):
        return f"g:{self.m.mid}:{tok}"

    def pcb(self, tok):
        """callback_data кнопки ЛИЧНОГО экрана (в личке с ботом)."""
        return f"gp:{self.m.mid}:{tok}"

    # --- личный экран игрока (в личке с ботом); PRIVATE = True — у игры есть скрытое от соперника ---
    PRIVATE = False
    PRIVATE_LABEL = "🃏 Мои карты"

    def private_text(self, i):
        return ''

    def private_buttons(self, i):
        return []

    def private_label(self):
        """Подпись кнопки-ссылки на личный экран под партией (None — кнопки нет)."""
        return self.PRIVATE_LABEL if self.PRIVATE and not self.finished else None

    # --- секрет, который игрок загадывает тайно (слово / число): личка с ботом или «@бот загадать …» ---
    SECRET = None                     # None | 'word' | 'number'

    def needs_secret(self, i):
        return False

    def check_secret(self, value):
        """Текст ошибки, если value не годится в секрет; None — годится."""
        return None

    def set_secret(self, i, value):
        """Сохранить секрет игрока i (уже проверенный). Вернуть текст подтверждения."""
        return "Принято"

    # --- переопределяется играми ---
    def setup(self):
        pass

    def body(self):
        return ''

    def board_buttons(self):
        return []

    async def handle(self, pid, tok):
        """Вернуть (текст_ответа, alert) или None. Мутирует состояние, ставит self.finished/winner/turn."""
        return None

    # --- служебное ---
    def turn_line(self):
        if self.finished:
            if self.winner == 'draw':
                return visual.hint("ничья")
            i = self.idx(self.winner)
            return visual.ok("Победа: " + _mention(self.winner, self.pname(i)))
        i = self.turn
        return f"ход: {self.mark(i)} {_mention(self.pid(i), self.pname(i))}"


# =========================================================================== матч (форма, лобби, таймаут)

class Match:
    def __init__(self, game_key, chat_id, challenger, opponent=None):
        self.mid = secrets.token_hex(3)
        self.game_key = game_key
        self.chat_id = chat_id
        self.players = [challenger]                       # [{id,name}]; второй добавится при входе/принятии
        self.opponent = opponent                          # {id,name} при вызове конкретного человека, иначе None
        self.phase = 'invite' if opponent else 'open'     # invite | open | play | over
        self.game = None
        self.form = None
        self._timer = None
        self.created = time.time()
        self._shown = None                                # (текст, кнопки) последней отрисовки формы
        self.dm = {}                                      # id игрока -> {'msg': сообщение бота, 'shown': (текст, кнопки)}
        self.meta = {}                                    # то, что переживает реванш (например, кто загадывал в прошлый раз)

    # ---- запуск самой игры ----
    def start_game(self):
        self.game = GAMES[self.game_key](self)
        self.phase = 'play'

    def allowed_users(self):
        ids = ['me'] + [p['id'] for p in self.players]
        if self.opponent:
            ids.append(self.opponent['id'])
        return ids

    # ---- тексты ----
    def header(self):
        G = GAMES[self.game_key]
        return visual.title(G.NAME, sub=G.EMOJI + " игра")

    def lobby_text(self):
        G = GAMES[self.game_key]
        host = _mention(self.players[0]['id'], self.players[0]['name'])
        if self.phase == 'invite':
            return "\n".join([self.header(),
                              f"{host} зовёт сыграть {_mention(self.opponent['id'], self.opponent['name'])}",
                              visual.hint(G.HELP) if G.HELP else '',
                              "", visual.hint("соперник — «Принять», автор — «Отмена»")])
        return "\n".join([self.header(),
                          f"{host} ищет соперника",
                          visual.hint(G.HELP) if G.HELP else '',
                          "", visual.hint("нажмите «Войти», чтобы сыграть")])

    def lobby_buttons(self):
        if self.phase == 'invite':
            return [[("✅ Принять", f"g:{self.mid}:acc", "success"), ("✖ Отклонить", f"g:{self.mid}:dec")],
                    [("Отмена", f"g:{self.mid}:cancel", "danger")]]
        return [[("▶️ Войти", f"g:{self.mid}:join", "success")],
                [("Отмена", f"g:{self.mid}:cancel", "danger")]]

    def play_text(self):
        g = self.game
        parts = [self.header(),
                 f"{g.mark(0)} {_mention(g.pid(0), g.pname(0))}   vs   {g.mark(1)} {_mention(g.pid(1), g.pname(1))}",
                 g.turn_line(), ""]
        b = g.body()
        if b:
            parts.append(b)
        return "\n".join(p for p in parts if p is not None)

    def play_buttons(self):
        rows = list(self.game.board_buttons())
        label = self.game.private_label()
        if label and _BOT.get('username'):
            rows.append([(label, f"url:https://t.me/{_BOT['username']}?start=gm{self.mid}")])
        if self.game.finished:
            rows.append([("🔁 Реванш", f"g:{self.mid}:rematch", "success"), (visual.btn('Закрыть', 'close'), f"g:{self.mid}:close")])
        else:
            rows.append([("🏳 Сдаться", f"g:{self.mid}:resign", "danger")])
        return rows

    def text(self):
        return self.play_text() if self.phase in ('play', 'over') else self.lobby_text()

    def buttons(self):
        return self.play_buttons() if self.phase in ('play', 'over') else self.lobby_buttons()

    async def render(self):
        """Перерисовать форму партии (только если что-то поменялось) и личные экраны игроков."""
        if self.form is not None and not getattr(self.form, '_closed', False):
            text, buttons = self.text(), self.buttons()
            shown = (text, repr(buttons))
            if shown != self._shown:
                try:
                    await self.form.edit(text, buttons=buttons)
                    self._shown = shown
                except RuntimeError:                       # форму уже закрыли/удалили — молча
                    pass
                except Exception as e:
                    if 'not modified' in str(e).lower():
                        self._shown = shown
                    else:
                        engine.tlog(f"[Games/{self.game_key}] форма: {type(e).__name__}: {e}")
        if self.game is not None:
            for pid in list(self.dm):
                try:
                    await self.render_private(pid)
                except Exception as e:
                    engine.tlog(f"[Games/{self.game_key}] личный экран: {type(e).__name__}: {e}")

    def private_view(self, pid):
        g = self.game
        i = g.idx(pid) if g is not None else -1
        G = GAMES[self.game_key]
        head = visual.title(G.NAME, sub="ваш личный экран")
        if g is None or self.phase not in ('play', 'over') or i < 0:
            return head + "\n" + visual.hint("партия ещё не началась"), []
        if g.finished:
            text = "\n".join([head, g.turn_line(), "", g.private_text(i)])
            return text, []
        text = "\n".join([head, f"соперник: {_mention(g.pid(g.opp(i)), g.pname(g.opp(i)))}", "", g.private_text(i)])
        return text, list(g.private_buttons(i))

    async def render_private(self, pid, fresh=False):
        """Личный экран игрока в личке с ботом: новый (fresh) или правка уже отправленного."""
        text, buttons = self.private_view(pid)
        entry = self.dm.get(pid)
        if entry is not None and not fresh:
            shown = (text, repr(buttons))
            if shown == entry.get('shown'):
                return
            try:
                await engine.bot.edit(entry['msg'], text, buttons=buttons or None)
                entry['shown'] = shown
                return
            except Exception as e:
                if 'not modified' in str(e).lower():
                    entry['shown'] = shown
                    return
                fresh = True                                   # сообщение удалили / слишком старое — пришлём заново
        if fresh and entry is not None:
            try:
                await engine.bot.raw.delete_message(entry['msg'].chat.id, entry['msg'].message_id)
            except Exception:
                pass
        msg = await engine.bot.send_message(pid, text, buttons=buttons or None)
        self.dm[pid] = {'msg': msg, 'shown': (text, repr(buttons))}

    async def arm_timeout(self):
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        minutes = await engine.get_config('open_wait' if self.phase in ('invite', 'open') else 'timeout')
        self._timer = asyncio.ensure_future(self._expire(minutes * 60))

    async def _expire(self, delay):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self.close()
        if self.form is not None:
            try:
                note = visual.hint("партия закрыта по тайм-ауту" if self.phase != 'invite' else "вызов истёк")
                await self.form.edit(self.header() + "\n" + note, buttons=None)
            except Exception:
                pass

    def close(self):
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        MATCHES.pop(self.mid, None)
        for entry in list(self.dm.values()):                # личные экраны — без кнопок
            asyncio.ensure_future(_quiet(engine.bot.edit(entry['msg'], visual.hint("партия закрыта"), buttons=None)))
        self.dm.clear()


async def _quiet(coro):
    try:
        await coro
    except Exception:
        pass


# =========================================================================== клики

async def _on_click(click, mid, tok):
    m = MATCHES.get(mid)
    if m is None:
        await click.answer("Эта партия уже закрыта", show_alert=True)
        return
    u = click.from_user
    uid = u.id if u else None

    # --- лобби ---
    if m.phase in ('invite', 'open'):
        if tok == 'cancel':
            if uid != m.players[0]['id']:
                await click.answer("Отменить может только автор", show_alert=True)
                return
            m.close()
            await m.form.delete()
            return
        if tok == 'dec':
            if m.phase == 'invite' and uid == m.opponent['id']:
                m.close()
                await m.form.edit(m.header() + "\n" + visual.hint("соперник отказался"), buttons=None)
            else:
                await click.answer("Это предложение не вам")
            return
        if tok in ('acc', 'join'):
            if u is None or getattr(u, 'bot', False):
                await click.answer("Боты не играют")
                return
            if uid == m.players[0]['id']:
                await click.answer("Нельзя играть с самим собой — позовите соперника")
                return
            if m.phase == 'invite' and uid != m.opponent['id']:
                await click.answer("Вызов адресован другому человеку")
                return
            m.players.append({'id': uid, 'name': _who(u)})
            m.start_game()
            await click.answer("Поехали!")
            await m.arm_timeout()
            await m.render()
            return
        return

    # --- игра окончена: реванш / закрыть ---
    if tok == 'close':
        m.close()
        await m.form.delete()
        return
    if tok == 'rematch':
        if uid not in [p['id'] for p in m.players]:
            await click.answer("Реванш — для игроков этой партии", show_alert=True)
            return
        m.players = [m.players[0], m.players[1]]          # тот же состав
        m.start_game()
        await click.answer("Реванш!")
        await m.arm_timeout()
        await m.render()
        return
    if tok == 'resign':
        i = m.game.idx(uid)
        if i < 0:
            await click.answer("Вы не в этой партии", show_alert=True)
            return
        m.game.finished = True
        m.game.winner = m.game.pid(m.game.opp(i))
        m.phase = 'over'
        await click.answer("Вы сдались")
        await m.arm_timeout()
        await m.render()
        return

    # --- ход в игре ---
    if m.game.finished:
        await click.answer("Партия окончена")
        return
    i = m.game.idx(uid)
    if i < 0:
        await click.answer("Вы не в этой партии", show_alert=True)
        return
    if tok == 'x':                                   # «пустая» клетка / неактивная кнопка — ничего не меняется
        await click.answer()
        return
    await _apply_move(m, uid, tok, click)


# =========================================================================== команды

async def _resolve_opponent(event, rest):
    """Из @username, ответа или id — {id,name} соперника, либо None (открытая игра)."""
    rest = rest.strip()
    if event.is_reply:
        r = await event.get_reply_message()
        if r and r.sender_id and not getattr(await r.get_sender(), 'bot', False):
            s = await r.get_sender()
            return {'id': r.sender_id, 'name': _who(s)}
    if rest:
        token = rest.split()[0]
        try:
            ent = await engine.client.get_entity(int(token) if token.lstrip('-').isdigit() else token)
            if getattr(ent, 'bot', False):
                return None
            return {'id': ent.id, 'name': _who(ent)}
        except Exception:
            return None
    return None


@decorators.command('game', 'g', 'игра')
async def game_cmd(event):
    """игра на двоих: <игра> [@соперник] — список без аргументов"""
    args = engine.get_args().strip()
    if not args:
        await _menu(event)
        return
    key = _find_game(args.split()[0])
    if key is None:
        await _menu(event, note=visual.err(f"Нет игры «{_esc(args.split()[0])}»"))
        return
    rest = args[len(args.split()[0]):].strip()
    opp = await _resolve_opponent(event, rest)
    me = await engine.client.get_me()
    if opp and opp['id'] == me.id:
        opp = None
    challenger = {'id': me.id, 'name': _who(me)}
    try:
        await _launch(event.chat_id, key, challenger, opp)
    except Exception as e:
        await engine.answer(event, visual.err("Нужен стационарный бот для кнопок") + f"\n<code>{_esc(str(e)[:200])}</code>")
        return
    try:
        await event.delete()
    except Exception:
        pass


async def _launch(chat_id, key, challenger, opponent):
    """Создать матч и его инлайн-форму (лобби). Кнопки доступны автору, сопернику и — в группе — всем участникам."""
    m = Match(key, chat_id, challenger, opponent=opponent)
    MATCHES[m.mid] = m
    if opponent:
        allowed = ['me', challenger['id'], opponent['id']]
    else:
        allowed = 'all'            # открытая игра: «Войти» может любой, кто видит сообщение (и в группе, и в личке)
    await _bot_username()
    m.form = await engine.create_inlineform(chat_id, m.lobby_text(), buttons=m.lobby_buttons(), for_user=allowed)
    _bind(m.form, m)
    await m.arm_timeout()
    return m


@decorators.command('games')
async def games_cmd(event):
    """список игр на двоих"""
    await _menu(event)


def _find_game(q):
    q = q.lower().strip()
    if q in GAMES:
        return q
    for key, G in GAMES.items():
        if q in (G.NAME.lower(), *[a.lower() for a in getattr(G, 'ALIASES', ())]):
            return key
    for key, G in GAMES.items():
        if G.NAME.lower().startswith(q) or key.startswith(q):
            return key
    return None


async def _menu(event, note=''):
    lines = [visual.title("Games", sub="14 игр на двоих")]
    if note:
        lines.append(note)
    lines.append(visual.hint("выберите игру, затем позовите соперника (или откройте вход для всех)"))
    rows, row = [], []
    for key, G in GAMES.items():
        row.append((f"{G.EMOJI} {G.NAME}", f"menu:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([(visual.btn('Закрыть', 'close'), "menu:close")])
    try:
        form = await engine.create_inlineform(event.chat_id, "\n".join(lines), buttons=rows)
    except Exception:
        names = ", ".join(f"{G.EMOJI}{G.NAME}" for G in GAMES.values())
        await engine.answer(event, "\n".join(lines) + "\n\n" + names + "\n"
                            + visual.hint("запуск: .game <игра> [@соперник]"))
        return
    try:
        await event.delete()
    except Exception:
        pass

    @form.on_click(data_startswith='menu:')
    async def _(click):
        key = click.data[5:]
        if key == 'close':
            await form.delete()
            return
        G = GAMES[key]
        await form.edit("\n".join([visual.title(G.NAME, sub=G.EMOJI),
                                   visual.hint(G.HELP) if G.HELP else '',
                                   "", visual.hint("как позвать: открытая игра — «Войти всем», или .game "
                                                   + key + " @user, чтобы вызвать человека")]),
                        buttons=[[("▶️ Открытая игра", f"mkopen:{key}")],
                                 [(visual.btn('Назад', 'back'), "menu:back")]])

    @form.on_click(data='menu:back')
    async def _(click):
        await form.edit("\n".join(lines), buttons=rows)

    @form.on_click(data_startswith='mkopen:')
    async def _(click):
        key = click.data[7:]
        u = click.from_user
        if u is None or getattr(u, 'bot', False):
            return
        challenger = {'id': u.id, 'name': _who(u)}
        await form.delete()               # закрываем меню и создаём отдельную форму-лобби с правильным доступом
        await _launch(form.chat_id, key, challenger, None)


# Форма матча слушает свои клики по префиксу g:<mid>:. Форма меню после «Открытой игры»
# превращается в форму матча — на неё навешивается тот же обработчик.
def _bind(form, m):
    @form.on_click(data_startswith=f"g:{m.mid}:")
    async def _(click):
        await _on_click(click, m.mid, click.data.split(':', 2)[2])


# =========================================================================== секреты: загадать тайно

def _secret_matches(uid):
    """Партии, где этот игрок прямо сейчас должен что-то загадать (свежие — первыми)."""
    out = []
    for m in MATCHES.values():
        g = m.game
        if g is None or g.finished or m.phase != 'play' or g.SECRET is None:
            continue
        i = g.idx(uid)
        if i >= 0 and g.needs_secret(i):
            out.append(m)
    out.sort(key=lambda m: m.created, reverse=True)
    return out


async def _accept_secret(m, uid, value):
    """Проверить и сохранить секрет игрока. Возвращает (ok, текст)."""
    g = m.game
    if g is None or g.finished or m.phase != 'play':
        return False, "Партия уже закрыта"
    i = g.idx(uid)
    if i < 0 or not g.needs_secret(i):
        return False, "Сейчас загадывать не нужно"
    value = (value or '').strip()
    err = g.check_secret(value)
    if err:
        return False, err
    msg = g.set_secret(i, value)
    await m.arm_timeout()
    await m.render()
    return True, msg


def _secret_what(kind):
    return 'слово' if kind == 'word' else 'число'


@bot.inline_query('загадать', 'secret', 'загадай', access='public')
async def _secret_query(query):
    """загадать слово или число для партии в Games — соперник его не увидит"""
    matches = _secret_matches(query.user_id)
    if not matches:
        return [engine.InlineResult(
            "Нечего загадывать", description="нет партии, где вы сейчас загадываете слово или число",
            text=visual.hint("Сейчас загадывать нечего — начните виселицу или быков и коров: .game"))]
    m = matches[0]
    g = m.game
    value = (query.text or '').strip()
    what = _secret_what(g.SECRET)
    if not value:
        return [engine.InlineResult(
            f"🤫 Наберите {what} после «загадать»", description="его не увидит никто, кроме вас",
            text=visual.hint(f"{what.capitalize()} так и не набрали — попробуйте ещё раз: @бот загадать …"))]
    err = g.check_secret(value)
    if err:
        return [engine.InlineResult(f"⚠️ {err}", description=f"{GAMES[m.game_key].NAME}: {what} не подходит",
                                    text=visual.warn(f"{what.capitalize()} не подошло: {_esc(err)}"))]
    name = query.first_name or query.username or str(query.user_id)
    masked = "▢" * min(len(value), 20)

    async def chosen(_inline_message_id):        # запрос приходит на каждую букву — сохраняем только отправленное
        ok, msg = await _accept_secret(m, query.user_id, value)
        if not ok:
            engine.tlog(f"[Games] загадать: {msg}")

    return [engine.InlineResult(
        f"🤫 Загадать {what}: {masked}", description=f"{GAMES[m.game_key].NAME} — в чат уйдёт только «загадал(а)»",
        text=f"🤫 <b>{_esc(name)}</b> загадал(а) {what}", on_chosen=chosen)]


@bot.watcher(watch='bot', access='public')
async def _secret_dm(event):
    """Слово/число, присланное боту в личку, — секрет для текущей партии."""
    if not getattr(event, 'is_private', False):
        return
    text = (event.raw_text or '').strip()
    if not text or text.startswith('/'):
        return
    matches = _secret_matches(event.sender_id)
    if not matches:
        return
    m = matches[0]
    ok, msg = await _accept_secret(m, event.sender_id, text)
    if ok:
        try:
            await event.delete()                         # чтобы секрет не висел даже в личке
        except Exception:
            pass
        await event.respond(visual.ok(msg) + "\n" + visual.hint("возвращайтесь в чат с партией"))
    else:
        await event.respond(visual.warn(msg) + "\n" + visual.hint("пришлите ещё раз"))


# =========================================================================== личные экраны (личка с ботом)

@bot.start_link('gm')
async def _private_start(event, payload):
    """«🃏 Мои карты» под партией → t.me/<бот>?start=gm<id партии> → бот присылает игроку его личный экран."""
    m = MATCHES.get(payload)
    if m is None or m.game is None:
        await event.reply(visual.warn("Эта партия уже закрыта"))
        return
    uid = event.sender_id
    if m.game.idx(uid) < 0:
        await event.reply(visual.warn("Вы не играете в этой партии — смотреть чужие карты нельзя"))
        return
    if not m.game.PRIVATE:
        await event.reply(visual.hint("В этой игре всё видно на общем поле"))
        return
    try:
        await event.delete()                             # «/start gm…» в личке не нужен
    except Exception:
        pass
    await m.render_private(uid, fresh=True)


@bot.callback(data_startswith='gp:')
async def _private_click(click):
    """Кнопки личного экрана: gp:<id партии>:<ход>."""
    try:
        _, mid, tok = click.data.split(':', 2)
    except ValueError:
        return
    m = MATCHES.get(mid)
    if m is None or m.game is None:
        await click.answer("Эта партия уже закрыта", show_alert=True)
        return
    uid = click.user_id
    if m.game.idx(uid) < 0:
        await click.answer("Вы не в этой партии", show_alert=True)
        return
    if m.game.finished:
        await click.answer("Партия окончена")
        await m.render()
        return
    if tok == 'refresh':
        await click.answer()
        await m.render_private(uid, fresh=True)
        return
    if tok == 'x':
        await click.answer()
        return
    await _apply_move(m, uid, tok, click)


async def _apply_move(m, uid, tok, click):
    """Ход игрока (из формы в чате или с личного экрана): handle → ответ на нажатие → перерисовка."""
    try:
        res = await m.game.handle(uid, tok)
    except Exception as e:
        engine.tlog(f"[Games/{m.game_key}] {type(e).__name__}: {e}")
        await click.answer("Не получилось сделать ход — попробуйте ещё раз", show_alert=True)
        await m.render()
        return
    if res is not None:
        text, alert = res if isinstance(res, tuple) else (res, False)
        if text:
            await click.answer(str(text)[:190], show_alert=alert)
        else:
            await click.answer()
    else:
        await click.answer()
    if m.phase == 'play' and m.game.finished:
        m.phase = 'over'
    await m.arm_timeout()
    await m.render()


# =========================================================================== 1. Крестики-нолики

class TicTacToe(Game):
    KEY = 'ttt'
    NAME = 'Крестики-нолики'
    EMOJI = '⭕'
    ALIASES = ('tictactoe', 'крестики', 'хо', 'xo')
    HELP = 'Поставьте три своих знака в ряд — по горизонтали, вертикали или диагонали.'
    LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]

    def setup(self):
        self.board = [None] * 9

    def _winner_line(self):
        for a, b, c in self.LINES:
            if self.board[a] is not None and self.board[a] == self.board[b] == self.board[c]:
                return self.board[a]
        return None

    def body(self):
        return visual.hint("три в ряд — и вы выиграли")

    def board_buttons(self):
        rows = []
        for r in range(3):
            row = []
            for c in range(3):
                i = r * 3 + c
                v = self.board[i]
                row.append((self.mark(v) if v is not None else '·', self.cb(f"c{i}" if v is None and not self.finished else "x")))
            rows.append(row)
        return rows

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if not tok.startswith('c'):
            return None
        pos = int(tok[1:])
        if self.board[pos] is not None:
            return ("Клетка занята", False)
        self.board[pos] = i
        w = self._winner_line()
        if w is not None:
            self.finished, self.winner = True, self.pid(w)
        elif all(x is not None for x in self.board):
            self.finished, self.winner = True, 'draw'
        else:
            self.turn = self.opp(i)
        return None


# =========================================================================== 2. Четыре в ряд

class ConnectFour(Game):
    KEY = 'c4'
    NAME = '4 в ряд'
    EMOJI = '🔴'
    ALIASES = ('connect4', 'четыревряд', 'four')
    HELP = 'Бросайте фишки в столбцы. Соберите 4 свои фишки подряд: по горизонтали, вертикали или диагонали.'
    COLS, ROWS = 7, 6

    def setup(self):
        self.grid = [[None] * self.COLS for _ in range(self.ROWS)]   # grid[row][col], row 0 — верх

    def _drop(self, col, who):
        for r in range(self.ROWS - 1, -1, -1):
            if self.grid[r][col] is None:
                self.grid[r][col] = who
                return r
        return None

    def _win(self, r, c, who):
        for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
            cnt = 1
            for s in (1, -1):
                rr, cc = r + dr * s, c + dc * s
                while 0 <= rr < self.ROWS and 0 <= cc < self.COLS and self.grid[rr][cc] == who:
                    cnt += 1
                    rr, cc = rr + dr * s, cc + dc * s
            if cnt >= 4:
                return True
        return False

    def body(self):
        chips = {0: '🔴', 1: '🟡', None: '⚪'}
        lines = ["".join(chips[self.grid[r][c]] for c in range(self.COLS)) for r in range(self.ROWS)]
        lines.append("1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣")
        return "\n".join(lines)

    def mark(self, i):
        return ('🔴', '🟡')[i] if i in (0, 1) else '·'

    def board_buttons(self):
        if self.finished:
            return []
        row = []
        for c in range(self.COLS):
            full = self.grid[0][c] is not None
            row.append(("⛔" if full else f"{c + 1}", self.cb(f"d{c}" if not full else "x")))
        return [row[:4], row[4:]]

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if not tok.startswith('d'):
            return None
        col = int(tok[1:])
        r = self._drop(col, i)
        if r is None:
            return ("Столбец полон", False)
        if self._win(r, col, i):
            self.finished, self.winner = True, self.pid(i)
        elif all(self.grid[0][c] is not None for c in range(self.COLS)):
            self.finished, self.winner = True, 'draw'
        else:
            self.turn = self.opp(i)
        return None


# =========================================================================== 3. Реверси

class Reversi(Game):
    KEY = 'rev'
    NAME = 'Реверси'
    EMOJI = '⚫'
    ALIASES = ('reversi', 'othello', 'отелло')
    HELP = 'Ставьте фишку так, чтобы зажать ряд чужих между своими — они станут вашими. Больше фишек в конце — победа.'
    N = 8
    DIRS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    def setup(self):
        self.g = [[None] * self.N for _ in range(self.N)]
        c = self.N // 2
        self.g[c - 1][c - 1] = self.g[c][c] = 1
        self.g[c - 1][c] = self.g[c][c - 1] = 0
        self.passes = 0

    def mark(self, i):
        return ('⚫', '⚪')[i] if i in (0, 1) else '·'

    def _flips(self, r, c, who):
        if self.g[r][c] is not None:
            return []
        out = []
        for dr, dc in self.DIRS:
            line, rr, cc = [], r + dr, c + dc
            while 0 <= rr < self.N and 0 <= cc < self.N and self.g[rr][cc] == self.opp(who):
                line.append((rr, cc))
                rr, cc = rr + dr, cc + dc
            if line and 0 <= rr < self.N and 0 <= cc < self.N and self.g[rr][cc] == who:
                out += line
        return out

    def _moves(self, who):
        return {(r, c): self._flips(r, c, who) for r in range(self.N) for c in range(self.N) if self._flips(r, c, who)}

    def _count(self):
        b = sum(r.count(0) for r in self.g)
        w = sum(r.count(1) for r in self.g)
        return b, w

    def body(self):
        b, w = self._count()
        head = f"⚫ {b} : {w} ⚪"
        return head

    def board_buttons(self):
        if self.finished:
            return []
        moves = self._moves(self.turn)
        rows = []
        for r in range(self.N):
            row = []
            for c in range(self.N):
                v = self.g[r][c]
                if v is not None:
                    row.append((self.mark(v), self.cb("x")))
                elif (r, c) in moves:
                    row.append(("▫️", self.cb(f"p{r}{c}")))
                else:
                    row.append(("·", self.cb("x")))
            rows.append(row)
        return rows

    def _advance(self):
        other = self.opp(self.turn)
        if self._moves(other):
            self.turn = other
            self.passes = 0
        elif self._moves(self.turn):
            self.passes = 1               # соперник пропускает, ходит снова текущий
        else:
            self._finish()

    def _finish(self):
        b, w = self._count()
        self.finished = True
        self.winner = 'draw' if b == w else self.pid(0 if b > w else 1)

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if not tok.startswith('p'):
            return None
        r, c = int(tok[1]), int(tok[2])
        flips = self._flips(r, c, i)
        if not flips:
            return ("Сюда нельзя", False)
        self.g[r][c] = i
        for rr, cc in flips:
            self.g[rr][cc] = i
        if all(self.g[r][c] is not None for r in range(self.N) for c in range(self.N)):
            self._finish()
        else:
            self._advance()
        msg = None
        if not self.finished and self.passes:
            msg = ("Сопернику некуда ходить — ваш ход снова", False)
        return msg


# =========================================================================== 4. Ним (спички)

class Nim(Game):
    KEY = 'nim'
    NAME = 'Ним (спички)'
    EMOJI = '🔥'
    ALIASES = ('спички', 'matches', 'ним')
    HELP = 'Берите 1–3 спички из любого ряда за ход. Кто заберёт последнюю спичку — проигрывает.'

    def setup(self):
        self.rows = [3, 5, 7]

    def body(self):
        lines = []
        for n, cnt in enumerate(self.rows):
            lines.append(f"<b>{n + 1}.</b> " + ("🪵" * cnt if cnt else "—") + f"  <i>{cnt}</i>")
        lines.append(visual.hint("кто возьмёт последнюю — проиграл"))
        return "\n".join(lines)

    def board_buttons(self):
        if self.finished:
            return []
        rows = []
        for n, cnt in enumerate(self.rows):
            if cnt <= 0:
                continue
            row = [(f"ряд {n + 1}: −{k}", self.cb(f"t{n}:{k}")) for k in range(1, min(3, cnt) + 1)]
            rows.append(row)
        return rows

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if not tok.startswith('t'):
            return None
        rn, k = tok[1:].split(':')
        rn, k = int(rn), int(k)
        if not (0 <= rn < len(self.rows)) or not (1 <= k <= min(3, self.rows[rn])):
            return ("Так нельзя", False)
        self.rows[rn] -= k
        if sum(self.rows) == 0:           # взял последнюю — проиграл
            self.finished, self.winner = True, self.pid(self.opp(i))
        else:
            self.turn = self.opp(i)
        return None


# =========================================================================== 5. Камень-ножницы-бумага

class RPS(Game):
    KEY = 'rps'
    NAME = 'Камень-ножницы-бумага'
    EMOJI = '✊'
    ALIASES = ('кнб', 'rockpaperscissors', 'цуефа')
    HELP = 'Три раунда. Каждый тайно выбирает камень, ножницы или бумагу. Кто выиграет больше раундов — победитель.'
    BEST_OF = 3
    ICON = {'r': '✊', 's': '✌️', 'p': '✋'}
    NAMEM = {'r': 'камень', 's': 'ножницы', 'p': 'бумага'}

    def setup(self):
        self.round = 1
        self.score = [0, 0]
        self.choice = [None, None]
        self.last = ''

    def body(self):
        rnd = (f"раунд <b>{self.round}</b> из {self.BEST_OF}" if self.round <= self.BEST_OF
               else f"дополнительный раунд <b>{self.round - self.BEST_OF}</b>")
        lines = [rnd, f"счёт: <b>{self.score[0]} : {self.score[1]}</b>"]
        if self.last:
            lines.append(self.last)
        if not self.finished:
            picked = [p for p, ch in zip((0, 1), self.choice) if ch is not None]
            if picked:
                lines.append(visual.hint("выбрал(и): " + ", ".join(self.pname(p) for p in picked) + " — ждём второго"))
            else:
                lines.append(visual.hint("оба выбирают…"))
        return "\n".join(lines)

    def board_buttons(self):
        if self.finished:
            return []
        return [[(f"{self.ICON['r']} Камень", self.cb("c:r")), (f"{self.ICON['s']} Ножницы", self.cb("c:s")),
                 (f"{self.ICON['p']} Бумага", self.cb("c:p"))]]

    def _beats(self, a, b):
        return (a, b) in (('r', 's'), ('s', 'p'), ('p', 'r'))

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if not tok.startswith('c:'):
            return None
        if self.choice[i] is not None:
            return ("Вы уже выбрали в этом раунде", False)
        self.choice[i] = tok[2:]
        if None in self.choice:
            return ("Выбор принят — ждём соперника", False)
        a, b = self.choice
        if a == b:
            self.last = f"{self.ICON[a]} vs {self.ICON[b]} — ничья в раунде"
        elif self._beats(a, b):
            self.score[0] += 1
            self.last = f"{self.ICON[a]} бьёт {self.ICON[b]} — раунд за {_esc(self.pname(0))}"
        else:
            self.score[1] += 1
            self.last = f"{self.ICON[b]} бьёт {self.ICON[a]} — раунд за {_esc(self.pname(1))}"
        self.choice = [None, None]
        need = self.BEST_OF // 2 + 1
        if self.score[0] >= need or self.score[1] >= need or self.round >= self.BEST_OF:
            if self.score[0] == self.score[1]:
                self.round += 1          # ничья по счёту — доп. раунд
            else:
                self.finished, self.winner = True, self.pid(0 if self.score[0] > self.score[1] else 1)
        else:
            self.round += 1
        return ("Раунд сыгран", False)


# =========================================================================== 6. Блэкджек

class Blackjack(Game):
    KEY = 'bj'
    NAME = 'Блэкджек'
    EMOJI = '🃏'
    ALIASES = ('blackjack', '21', 'очко')
    HELP = ('Набирайте очки ближе к 21, но не больше. «Взять» — ещё карта, «Хватит» — остановиться. Перебор (>21) — '
            'проигрыш. Свои карты видите только вы: «🃏 Мои карты» (личка с ботом); на общем поле — только их число.')
    PRIVATE = True
    SUITS = '♠♥♦♣'
    RANKS = [('A', 11), ('2', 2), ('3', 3), ('4', 4), ('5', 5), ('6', 6), ('7', 7), ('8', 8), ('9', 9),
             ('10', 10), ('J', 10), ('Q', 10), ('K', 10)]

    def setup(self):
        self.deck = [(r, v, s) for r, v in self.RANKS for s in self.SUITS]
        random.shuffle(self.deck)
        self.hands = [[self.deck.pop(), self.deck.pop()], [self.deck.pop(), self.deck.pop()]]
        self.stood = [False, False]
        self.turn = 0

    def _score(self, hand):
        total = sum(v for _, v, _ in hand)
        aces = sum(1 for r, _, _ in hand if r == 'A')
        while total > 21 and aces:
            total -= 10
            aces -= 1
        return total

    def _hand_str(self, hand, hide=False):
        if hide:
            return f"{hand[0][0]}{hand[0][2]} 🂠"
        return " ".join(f"{r}{s}" for r, v, s in hand)

    def body(self):
        lines = []
        for i in (0, 1):
            tag = " ✋ хватит" if self.stood[i] else ''
            if self.finished:
                s = self._score(self.hands[i])
                lines.append(f"{self.mark(i)} {_esc(self.pname(i))}: {self._hand_str(self.hands[i])} = <b>{s}</b>"
                             + (" 💥" if s > 21 else ''))
            else:                       # чужие карты не видны: только сколько их
                lines.append(f"{self.mark(i)} {_esc(self.pname(i))}: " + "🂠" * len(self.hands[i]) + tag)
        if not self.finished:
            lines.append(visual.hint("свои карты и очки — «🃏 Мои карты» (в личке с ботом)"))
        return "\n".join(lines)

    def private_text(self, i):
        s = self._score(self.hands[i])
        lines = [f"🃏 ваши карты: <b>{self._hand_str(self.hands[i])}</b> = <b>{s}</b>" + (" 💥" if s > 21 else ''),
                 f"у соперника карт: {len(self.hands[self.opp(i)])}" + (" (остановился)" if self.stood[self.opp(i)] else '')]
        if not self.finished:
            if self.stood[i]:
                lines.append(visual.hint("вы остановились — ждём соперника"))
            elif i == self.turn:
                lines.append(visual.hint("ваш ход: «Взять» или «Хватит»"))
            else:
                lines.append(visual.hint("ждём хода соперника"))
        return "\n".join(lines)

    def private_buttons(self, i):
        if self.finished or self.stood[i] or i != self.turn:
            return [] if self.finished else [[("🔄 Обновить", self.pcb("refresh"))]]
        return [[("🃏 Взять", self.pcb("hit"), "success"), ("✋ Хватит", self.pcb("stand"))]]

    def board_buttons(self):
        if self.finished:
            return []
        return [[("🃏 Взять", self.cb("hit"), "success"), ("✋ Хватит", self.cb("stand"))]]

    def _maybe_finish(self):
        if all(self.stood) or all(self._score(h) >= 21 for h in self.hands):
            s0, s1 = self._score(self.hands[0]), self._score(self.hands[1])
            b0, b1 = s0 > 21, s1 > 21
            if b0 and b1:
                self.winner = 'draw'
            elif b0:
                self.winner = self.pid(1)
            elif b1:
                self.winner = self.pid(0)
            elif s0 == s1:
                self.winner = 'draw'
            else:
                self.winner = self.pid(0 if s0 > s1 else 1)
            self.finished = True
            return True
        return False

    def _next(self):
        o = self.opp(self.turn)
        if not self.stood[o] and self._score(self.hands[o]) < 21:
            self.turn = o

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if self.stood[i]:
            return ("Вы уже остановились", False)
        if tok == 'hit':
            self.hands[i].append(self.deck.pop())
            s = self._score(self.hands[i])
            if s > 21:
                self.stood[i] = True
                if not self._maybe_finish():
                    self._next()
                return (f"Перебор: {s}", True)
            if s == 21:
                self.stood[i] = True
                if not self._maybe_finish():
                    self._next()
                return ("21! стоп", False)
            return (f"ваши очки: {s}", False)
        if tok == 'stand':
            self.stood[i] = True
            if not self._maybe_finish():
                self._next()
            return ("Вы остановились", False)
        return None


# =========================================================================== 7. Виселица

class Hangman(Game):
    KEY = 'hang'
    NAME = 'Виселица'
    EMOJI = '🪢'
    ALIASES = ('hangman', 'виселица')
    HELP = ('Один загадывает слово, другой отгадывает по буквам: 6 ошибок — и отгадывающий проиграл. Кто загадывает, '
            'выбираете перед партией. Можно отдать слово боту — тогда угадываете оба по очереди.')
    PRIVATE = True
    SECRET = 'word'
    LIVES = 6
    STAGES = ['😀', '🙂', '😐', '😟', '😨', '😰', '💀']
    RU = list('абвгдежзийклмнопрстуфхцчшщъыьэюя')          # «ё» считается за «е»
    EN = list('abcdefghijklmnopqrstuvwxyz')
    WORDS = ('абрикос', 'айсберг', 'акварель', 'алфавит', 'апельсин', 'архипелаг', 'астронавт', 'балкон', 'барабан',
             'батискаф', 'библиотека', 'бинокль', 'ботинок', 'вертолёт', 'велосипед', 'вулкан', 'гармошка', 'гербарий',
             'гитара', 'горизонт', 'дельфин', 'дирижабль', 'жонглёр', 'журавль', 'загадка', 'звездопад', 'зеркало',
             'игрушка', 'календарь', 'каникулы', 'карандаш', 'карусель', 'кенгуру', 'клавиатура', 'компас', 'крокодил',
             'лабиринт', 'лимонад', 'малина', 'маяк', 'метель', 'микроскоп', 'мороженое', 'навигатор', 'носорог',
             'облако', 'одуванчик', 'океан', 'орбита', 'паровоз', 'пингвин', 'планета', 'подсолнух', 'попугай',
             'радуга', 'ракета', 'рюкзак', 'самовар', 'светлячок', 'сенокос', 'скрипка', 'снеговик', 'созвездие',
             'телескоп', 'трамвай', 'тюльпан', 'ураган', 'фейерверк', 'фламинго', 'фонарик', 'футбол', 'хамелеон',
             'шахматы', 'шоколад', 'экватор', 'эскимо', 'якорь', 'ящерица', 'черепаха', 'головоломка')

    def setup(self):
        self.stage = 'roles'              # roles → set → guess
        self.setter = None                # индекс загадывающего; None — слово загадал бот (solo)
        self.solo = False
        self.word = None
        self.opened = set()
        self.wrong = set()
        self.turn = 0

    # ---- секрет ----
    @staticmethod
    def _norm(value):
        return " ".join((value or '').lower().replace('ё', 'е').split())

    def check_secret(self, value):
        w = self._norm(value)
        letters = [c for c in w if c.isalpha()]
        if len(letters) < 2:
            return "нужно слово хотя бы из 2 букв"
        if len(w) > 30:
            return "слишком длинно — до 30 символов"
        if any(not (c.isalpha() or c in ' -') for c in w):
            return "только буквы, пробел и дефис"
        ru = all(c in self.RU for c in letters)
        en = all(c in self.EN for c in letters)
        if not (ru or en):
            return "буквы одного алфавита — русского или латиницы"
        return None

    def needs_secret(self, i):
        return self.stage == 'set' and i == self.setter

    def set_secret(self, i, value):
        self.word = self._norm(value)
        self.stage = 'guess'
        self.turn = self.opp(i)
        self.m.meta['hang_setter'] = self.pid(i)
        return f"Слово загадано: {self.word.upper()} — соперник отгадывает"

    # ---- вид ----
    def private_label(self):
        return "🤫 Загадать в личке" if self.stage == 'set' and not self.finished else None

    def mark(self, i):
        if self.stage == 'guess' and not self.solo and i in (0, 1):
            return '🤫' if i == self.setter else '🔎'
        return super().mark(i)

    def _alphabet(self):
        return self.RU if (self.word and any(c in self.RU for c in self.word)) else self.EN

    def _masked(self):
        return " ".join(('·' if c == ' ' else (c.upper() if (not c.isalpha() or c in self.opened) else '▢'))
                        for c in self.word)

    def _solved(self):
        return all((not c.isalpha()) or c in self.opened for c in self.word)

    def turn_line(self):
        if self.finished:
            return super().turn_line()
        if self.stage == 'roles':
            return visual.hint("выбираем, кто загадывает")
        if self.stage == 'set':
            return f"загадывает: {_mention(self.pid(self.setter), self.pname(self.setter))}"
        who = "ход" if self.solo else "отгадывает"
        return f"{who}: {self.mark(self.turn)} {_mention(self.pid(self.turn), self.pname(self.turn))}"

    def body(self):
        if self.stage == 'roles':
            lines = ["<b>Кто загадывает слово?</b>"]
            last = self.m.meta.get('hang_setter')
            if last is not None and self.idx(last) >= 0:
                lines.append(visual.hint(f"в прошлый раз загадывал(а) {_esc(self.pname(self.idx(last)))}"))
            lines.append(visual.hint("«Слово от бота» — угадываете оба по очереди, кто откроет слово — победил"))
            return "\n".join(lines)
        if self.stage == 'set':
            bot_name = _BOT.get('username')
            how = [visual.hint(f"{_esc(self.pname(self.setter))} загадывает слово — в чате его не будет видно"),
                   visual.item("кнопка «🤫 Загадать в личке» — слово пишется боту"),
                   visual.item(f"или прямо здесь: <code>@{bot_name or 'бот'} загадать слово</code>"),
                   visual.item("или «🎲 Случайное слово»")]
            return "\n".join(how)
        lines = [self.STAGES[min(len(self.wrong), self.LIVES)] + "  " + engine.progress_bar(
                     100 * (self.LIVES - len(self.wrong)) / self.LIVES, self.LIVES, 'heart'),
                 f"<code>{_esc(self._masked())}</code>",
                 visual.hint(f"букв: {sum(c.isalpha() for c in self.word)}" + (" · слово загадал бот" if self.solo else ''))]
        if self.wrong:
            lines.append(visual.hint("мимо: " + ", ".join(sorted(self.wrong)).upper()))
        if self.finished:
            lines.append(visual.field("слово", self.word.upper()))
        return "\n".join(lines)

    def board_buttons(self):
        if self.finished:
            return []
        if self.stage == 'roles':
            return [[("🤫 Загадаю я", self.cb("role:me"), "primary"), ("🎲 Жребий", self.cb("role:rnd"))],
                    [("🤖 Слово от бота — угадываем оба", self.cb("role:bot"))]]
        if self.stage == 'set':
            return [[("🎲 Случайное слово", self.cb("rnd"), "primary")],
                    [("↩ Выбрать заново", self.cb("roles"))]]
        rows, row = [], []
        for ch in self._alphabet():
            used = ch in self.opened or ch in self.wrong
            if ch in self.opened:
                label = '✅'
            elif ch in self.wrong:
                label = '·'
            else:
                label = ch.upper()
            row.append((label, self.cb(f"l{ch}" if not used else "x")))
            if len(row) == 8:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    def private_text(self, i):
        if self.stage == 'roles':
            return visual.hint("ждём, пока решите, кто загадывает")
        if self.stage == 'set':
            if i == self.setter:
                return "\n".join(["🤫 <b>Пришлите слово сообщением прямо сюда</b>",
                                  visual.hint("соперник его не увидит; можно фразу до 30 символов"),
                                  visual.hint("или нажмите «🎲 Случайное слово»")])
            return visual.hint("слово загадывает соперник — ждите, отгадывать будете в общем чате")
        if not self.solo and i == self.setter:
            return f"ваше слово: <b>{_esc(self.word.upper())}</b>\n" + visual.hint("соперник отгадывает в общем чате")
        return visual.hint("вы отгадываете — буквы нажимайте в общем чате")

    def private_buttons(self, i):
        if self.stage == 'set' and i == self.setter and not self.finished:
            return [[("🎲 Случайное слово", self.pcb("rnd"), "primary")]]
        return []

    # ---- ходы ----
    def _start_set(self, setter):
        self.setter = setter
        self.stage = 'set'
        self.turn = setter

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if self.stage == 'roles':
            if tok == 'role:me':
                self._start_set(i)
                return ("Загадывайте — соперник не увидит слово", False)
            if tok == 'role:rnd':
                self._start_set(random.randrange(2))
                return (f"Жребий: загадывает {self.pname(self.setter)}", True)
            if tok == 'role:bot':
                self.solo = True
                self.word = random.choice(self.WORDS).replace('ё', 'е')
                self.stage = 'guess'
                self.turn = random.randrange(2)
                return (f"Слово загадано! Первым ходит {self.pname(self.turn)}", False)
            return None
        if self.stage == 'set':
            if tok == 'roles':
                self.setter = None
                self.stage = 'roles'
                return None
            if tok == 'rnd':
                if i != self.setter:
                    return ("Слово загадывает соперник", False)
                word = random.choice(self.WORDS)
                self.set_secret(i, word)
                return (f"Загадано слово: {self.word.upper()}", True)
            return ("Ждём, пока загадают слово", False)
        # --- отгадывание ---
        if i != self.turn:
            return ("Сейчас не ваш ход" if self.solo else "Отгадывает соперник", False)
        if not tok.startswith('l'):
            return None
        ch = tok[1:]
        if ch in self.opened or ch in self.wrong:
            return ("Эту букву уже называли", False)
        if ch in self.word:
            self.opened.add(ch)
            if self._solved():
                self.finished, self.winner = True, self.pid(i)
                return ("Слово отгадано! 🎉", False)
            return (f"Есть такая — {self.word.count(ch)} шт.", False)
        self.wrong.add(ch)
        if len(self.wrong) >= self.LIVES:
            self.finished = True
            self.winner = 'draw' if self.solo else self.pid(self.setter)
            return (f"Ошибки кончились. Слово: {self.word.upper()}", True)
        if self.solo:
            self.turn = self.opp(i)
        return ("Мимо", False)


# =========================================================================== 8. Быки и коровы

class BullsCows(Game):
    KEY = 'bulls'
    NAME = 'Быки и коровы'
    EMOJI = '🐂'
    ALIASES = ('быкиикоровы', 'bullsandcows', 'коровы', 'быки')
    HELP = ('Каждый тайно загадывает 4-значное число из разных цифр. По очереди угадываете число соперника: '
            '🐂 — цифра на своём месте, 🐄 — есть, но не там. Кто первым откроет число — победил '
            '(у второго игрока остаётся ответная попытка).')
    PRIVATE = True
    SECRET = 'number'
    DIGITS = '1234567890'

    def setup(self):
        self.secret = [None, None]       # загаданное каждым
        self.history = [[], []]          # [(guess, bulls, cows), ...] по игрокам
        self.draft = ''                  # число, которое набирает тот, чей ход (видно всем — это не секрет)
        self.sdraft = ['', '']           # набор секрета на личном экране
        self.last_chance = False
        self.turn = 0

    def _ready(self):
        return all(s is not None for s in self.secret)

    @staticmethod
    def _valid(num):
        return len(num) == 4 and num.isdigit() and len(set(num)) == 4

    @staticmethod
    def _bc(guess, secret):
        bulls = sum(a == b for a, b in zip(guess, secret))
        cows = len(set(guess) & set(secret)) - bulls
        return bulls, cows

    @staticmethod
    def _slots(s):
        return " ".join(list(s) + ['▢'] * (4 - len(s)))

    # ---- секрет ----
    def needs_secret(self, i):
        return self.secret[i] is None and not self.finished

    def check_secret(self, value):
        v = (value or '').strip().replace(' ', '')
        return None if self._valid(v) else "нужно 4 разные цифры, например 1234"

    def set_secret(self, i, value):
        self.secret[i] = value.strip().replace(' ', '')
        self.sdraft[i] = ''
        if self._ready():
            self.turn = 0
            return f"Число {self.secret[i]} загадано — игра началась!"
        return f"Число {self.secret[i]} загадано — ждём соперника"

    def private_label(self):
        if self.finished:
            return None
        return "🔢 Загадать в личке" if not self._ready() else "🔢 Моё число"

    def private_text(self, i):
        if self.secret[i] is None:
            return "\n".join(["🔢 <b>Загадайте число</b> — 4 разные цифры",
                              f"<code>{self._slots(self.sdraft[i])}</code>",
                              visual.hint("наберите кнопками или пришлите сообщением; соперник не увидит")])
        lines = [f"ваше число: <b><code>{self.secret[i]}</code></b>"]
        tries = self.history[self.opp(i)]
        if tries:
            lines.append(visual.hint("соперник пробовал: " + ", ".join(g for g, _b, _c in tries[-6:])))
        if not self._ready():
            lines.append(visual.hint("ждём, пока загадает соперник"))
        return "\n".join(lines)

    def private_buttons(self, i):
        if self.secret[i] is not None or self.finished:
            return []
        d = self.sdraft[i]
        keys = [(ch if ch not in d else '·', self.pcb(f"k{ch}" if ch not in d and len(d) < 4 else "x"))
                for ch in self.DIGITS]
        return [keys[:5], keys[5:],
                [("⌫", self.pcb("kdel")),
                 ("✅ Загадать", self.pcb("kok"), "success") if len(d) == 4 else ("✅ Загадать", self.pcb("kok"))],
                [("🎲 Случайное число", self.pcb("rnd"))]]

    # ---- вид ----
    def turn_line(self):
        if self.finished:
            return super().turn_line()
        if not self._ready():
            return visual.hint("загадываем числа")
        pre = "последняя попытка" if self.last_chance else "ход"
        return f"{pre}: {self.mark(self.turn)} {_mention(self.pid(self.turn), self.pname(self.turn))}"

    def body(self):
        if not self._ready():
            bot_name = _BOT.get('username')
            lines = []
            for i in (0, 1):
                state = "✅ загадал(а)" if self.secret[i] is not None else "⏳ думает…"
                lines.append(f"{self.mark(i)} {_esc(self.pname(i))} — {state}")
            lines += ["", visual.hint("как загадать, чтобы никто не увидел:"),
                      visual.item("«🔢 Загадать в личке» — кнопками у бота"),
                      visual.item(f"или здесь: <code>@{bot_name or 'бот'} загадать 1234</code>"),
                      visual.item("или «🎲 Загадать случайно»")]
            return "\n".join(lines)
        lines = []
        for i in (0, 1):
            n = len(self.history[i])
            lines.append(f"{self.mark(i)} <b>{_esc(self.pname(i))}</b>" + (f"  <i>попыток: {n}</i>" if n else ''))
            for k, (g, b, c) in enumerate(self.history[i][-5:], start=max(1, n - 4)):
                lines.append(f"   <code>{k:>2}. {g}</code>  🐂{b} 🐄{c}")
            if not n:
                lines.append("   " + visual.hint("ходов нет"))
        if self.finished:
            lines.append(visual.hint(f"числа: {_esc(self.pname(0))} — {self.secret[0]} · "
                                     f"{_esc(self.pname(1))} — {self.secret[1]}"))
        else:
            lines += ["", f"набирает {self.mark(self.turn)}: <code>{self._slots(self.draft)}</code>"]
            if self.last_chance:
                lines.append(visual.warn("соперник уже угадал — последний шанс сравнять"))
        return "\n".join(lines)

    def board_buttons(self):
        if self.finished:
            return []
        if not self._ready():
            return [[("🎲 Загадать случайно", self.cb("rnd"), "primary")]]
        d = self.draft
        keys = [(ch if ch not in d else '·', self.cb(f"d{ch}" if ch not in d and len(d) < 4 else "x"))
                for ch in self.DIGITS]
        ok = ("✅ Проверить", self.cb("ok"), "success") if len(d) == 4 else ("✅ Проверить", self.cb("ok"))
        return [keys[:5], keys[5:], [("⌫ Стереть", self.cb("del")), ok]]

    # ---- ходы ----
    async def handle(self, pid, tok):
        i = self.idx(pid)
        # секрет: кнопки личного экрана и «случайно»
        if tok == 'rnd':
            if self.secret[i] is not None:
                return ("Вы уже загадали число", False)
            num = "".join(random.sample(self.DIGITS, 4))
            self.set_secret(i, num)
            return (f"Ваше число: {num} — запомните (видно только вам)", True)
        if tok.startswith('k'):
            if self.secret[i] is not None:
                return ("Число уже загадано", False)
            key = tok[1:]
            if key == 'del':
                self.sdraft[i] = self.sdraft[i][:-1]
                return None
            if key == 'ok':
                if not self._valid(self.sdraft[i]):
                    return ("Нужно 4 разные цифры", False)
                return (self.set_secret(i, self.sdraft[i]), False)
            if key.isdigit() and key not in self.sdraft[i] and len(self.sdraft[i]) < 4:
                self.sdraft[i] += key
            return None
        # угадывание
        if not self._ready():
            return ("Сначала оба загадывают числа", False)
        if i != self.turn:
            return ("Сейчас ход соперника", False)
        if tok == 'del':
            self.draft = self.draft[:-1]
            return None
        if tok.startswith('d'):
            ch = tok[1:]
            if ch.isdigit() and ch not in self.draft and len(self.draft) < 4:
                self.draft += ch
            return None
        if tok != 'ok':
            return None
        guess = self.draft
        if not self._valid(guess):
            return ("Наберите 4 разные цифры", False)
        self.draft = ''
        b, c = self._bc(guess, self.secret[self.opp(i)])
        self.history[i].append((guess, b, c))
        if self.last_chance:                                   # ответная попытка второго игрока
            self.finished = True
            self.winner = 'draw' if b == 4 else self.pid(self.opp(i))
            return ("Тоже угадали — ничья!" if b == 4 else f"{guess}: 🐂{b} 🐄{c} — не вышло", True)
        if b == 4:
            if i == 0 and len(self.history[1]) < len(self.history[0]):
                self.last_chance = True
                self.turn = 1
                return ("🐂🐂🐂🐂 Угадали! У соперника ответная попытка", True)
            self.finished, self.winner = True, self.pid(i)
            return ("🐂🐂🐂🐂 Угадали!", True)
        self.turn = self.opp(i)
        return (f"{guess}: 🐂{b} 🐄{c}", False)


# =========================================================================== 9. Сапёр-дуэль

class MinesweeperDuel(Game):
    KEY = 'ms'
    NAME = 'Сапёр-дуэль'
    EMOJI = '💣'
    ALIASES = ('minesweeper', 'сапёр', 'сапер', 'saper')
    HELP = ('По очереди открывайте клетки общего поля. Под клеткой может быть мина — кто её вскроет, проигрывает. '
            'Цифра — сколько мин вокруг. Первый ход всегда безопасный.')
    W, H, MINES = 6, 6, 7
    DIGITS = {0: '▫️', 1: '1️⃣', 2: '2️⃣', 3: '3️⃣', 4: '4️⃣', 5: '5️⃣', 6: '6️⃣', 7: '7️⃣', 8: '8️⃣'}

    def setup(self):
        cells = [(r, c) for r in range(self.H) for c in range(self.W)]
        self.mines = set(random.sample(cells, self.MINES))
        self.open = {}                   # (r,c) -> число соседних мин
        self.boom = None                 # клетка, где взорвались
        self.turn = 0

    def _neighbors(self, r, c):
        return [(r + dr, c + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                if (dr or dc) and 0 <= r + dr < self.H and 0 <= c + dc < self.W]

    def _count(self, r, c):
        return sum((rr, cc) in self.mines for rr, cc in self._neighbors(r, c))

    def _flood(self, r, c):
        stack = [(r, c)]
        while stack:
            cr, cc = stack.pop()
            if (cr, cc) in self.open or (cr, cc) in self.mines:
                continue
            n = self._count(cr, cc)
            self.open[(cr, cc)] = n
            if n == 0:
                stack += self._neighbors(cr, cc)

    def _safe_left(self):
        return self.W * self.H - self.MINES - len(self.open)

    def body(self):
        if self.finished and self.boom is not None:
            return visual.hint(f"💥 мина! {_esc(self.pname(self.opp(self.idx(self.winner))))} подорвался(ась)")
        return visual.hint(f"мин: {self.MINES} · безопасных клеток осталось: {self._safe_left()}")

    def board_buttons(self):
        rows = []
        for r in range(self.H):
            row = []
            for c in range(self.W):
                cell = (r, c)
                if cell == self.boom:
                    row.append(("💥", self.cb("x")))
                elif cell in self.open:
                    row.append((self.DIGITS.get(self.open[cell], '·'), self.cb("x")))
                elif self.finished and cell in self.mines:
                    row.append(("💣", self.cb("x")))
                elif self.finished:
                    row.append(("⬜", self.cb("x")))
                else:
                    row.append(("⬜", self.cb(f"o{r}{c}")))
            rows.append(row)
        return rows

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if not tok.startswith('o') or len(tok) != 3:
            return None
        r, c = int(tok[1]), int(tok[2])
        cell = (r, c)
        if cell in self.open:
            return ("Уже открыто", False)
        if not self.open and cell in self.mines:              # первый ход не взрывается: мину переносим
            free = [(rr, cc) for rr in range(self.H) for cc in range(self.W)
                    if (rr, cc) not in self.mines and (rr, cc) != cell]
            self.mines.discard(cell)
            self.mines.add(random.choice(free))
        if cell in self.mines:
            self.boom = cell
            self.finished, self.winner = True, self.pid(self.opp(i))
            return ("💥 Мина! Вы проиграли", True)
        before = len(self.open)
        self._flood(r, c)
        if self._safe_left() <= 0:
            self.finished, self.winner = True, 'draw'      # всё безопасное вскрыто — ничья
            return ("Поле пройдено — ничья", False)
        self.turn = self.opp(i)
        opened = len(self.open) - before
        return (f"Открыто клеток: {opened}", False) if opened > 1 else None


# =========================================================================== 12. Точки-квадраты

class DotsAndBoxes(Game):
    KEY = 'dab'
    NAME = 'Точки-квадраты'
    EMOJI = '⬛'
    ALIASES = ('dotsandboxes', 'точки', 'квадраты')
    HELP = ('Проводите линии между соседними точками (жмите на ⋯ и ⋮). Замкнули квадрат — он ваш, и вы ходите ещё раз. '
            'Больше квадратов в конце — победа.')
    N = 4                                 # 4×4 точки → 3×3 квадрата (нечётно — ничьей не бывает)

    def setup(self):
        self.h = {}                       # горизонтальные рёбра (r,c): (r,c)–(r,c+1); r 0..N-1, c 0..N-2 -> кто провёл
        self.v = {}                       # вертикальные рёбра (r,c): (r,c)–(r+1,c);   r 0..N-2, c 0..N-1
        self.boxes = {}                   # (r,c) 0..N-2 -> owner
        self.last = None                  # последнее ребро
        self.turn = 0
        self.score = [0, 0]

    def mark(self, i):
        return ('🟥', '🟦')[i] if i in (0, 1) else '·'

    def _box_done(self, r, c):
        return (r, c) in self.h and (r + 1, c) in self.h and (r, c) in self.v and (r, c + 1) in self.v

    def body(self):
        total = (self.N - 1) ** 2
        return "\n".join([f"🟥 <b>{self.score[0]}</b> : <b>{self.score[1]}</b> 🟦",
                          visual.hint(f"свободных квадратов: {total - len(self.boxes)}")])

    def board_buttons(self):
        size = 2 * self.N - 1
        rows = []
        for R in range(size):
            row = []
            for C in range(size):
                if R % 2 == 0 and C % 2 == 0:                     # точка
                    row.append(("•", self.cb("x")))
                elif R % 2 == 0:                                  # горизонтальное ребро
                    key = (R // 2, C // 2)
                    if key in self.h:
                        row.append(("➖" if self.last != ('h',) + key else "🔸", self.cb("x")))
                    else:
                        row.append(("⋯", self.cb(f"h{key[0]}{key[1]}" if not self.finished else "x")))
                elif C % 2 == 0:                                  # вертикальное ребро
                    key = (R // 2, C // 2)
                    if key in self.v:
                        row.append(("❙" if self.last != ('v',) + key else "🔸", self.cb("x")))
                    else:
                        row.append(("⋮", self.cb(f"v{key[0]}{key[1]}" if not self.finished else "x")))
                else:                                             # квадрат
                    o = self.boxes.get((R // 2, C // 2))
                    row.append((self.mark(o) if o is not None else "▫️", self.cb("x")))
            rows.append(row)
        return rows

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if len(tok) != 3 or tok[0] not in ('h', 'v'):
            return None
        kind, r, c = tok[0], int(tok[1]), int(tok[2])
        d = self.h if kind == 'h' else self.v
        if (r, c) in d:
            return ("Линия уже есть", False)
        d[(r, c)] = i
        self.last = (kind, r, c)
        gained = 0
        for br in range(self.N - 1):
            for bc in range(self.N - 1):
                if (br, bc) not in self.boxes and self._box_done(br, bc):
                    self.boxes[(br, bc)] = i
                    self.score[i] += 1
                    gained += 1
        if len(self.boxes) >= (self.N - 1) ** 2:
            self.finished = True
            self.winner = 'draw' if self.score[0] == self.score[1] else self.pid(0 if self.score[0] > self.score[1] else 1)
            return None
        if gained == 0:
            self.turn = self.opp(i)
            return None
        return (f"+{gained} — ходите ещё!", False)


# =========================================================================== 3-bis. Морской бой

class Battleship(Game):
    KEY = 'bs'
    NAME = 'Морской бой'
    EMOJI = '🚢'
    ALIASES = ('battleship', 'морскойбой', 'корабли')
    HELP = ('Корабли расставляются автоматически. По очереди стреляете по полю соперника. Попадание — ход снова, мимо — '
            'ход переходит. Потопите все корабли. Свой флот и куда попал соперник — «🚢 Мой флот» (личка с ботом).')
    PRIVATE = True
    PRIVATE_LABEL = "🚢 Мой флот"
    N = 6
    FLEET = [3, 2, 2, 1, 1]

    def setup(self):
        self.ships = [self._place(), self._place()]       # [ [frozenset,...], ... ] по игрокам
        self.shots = [set(), set()]                       # клетки, куда стрелял игрок i (по сопернику)
        self.hits = [set(), set()]                        # попадания игрока i
        self.turn = 0

    def _place(self):
        for _ in range(500):
            occ = set()
            ships = []
            ok = True
            for size in self.FLEET:
                placed = False
                for _ in range(100):
                    horiz = random.random() < 0.5
                    if horiz:
                        r = random.randrange(self.N)
                        c = random.randrange(self.N - size + 1)
                        cells = {(r, c + k) for k in range(size)}
                    else:
                        r = random.randrange(self.N - size + 1)
                        c = random.randrange(self.N)
                        cells = {(r + k, c) for k in range(size)}
                    halo = set()
                    for (rr, cc) in cells:
                        for dr in (-1, 0, 1):
                            for dc in (-1, 0, 1):
                                halo.add((rr + dr, cc + dc))
                    if cells & occ or halo & occ:
                        continue
                    occ |= cells
                    ships.append(frozenset(cells))
                    placed = True
                    break
                if not placed:
                    ok = False
                    break
            if ok:
                return ships
        return ships

    def _sunk(self, defender, cell_set):
        """корабль defender, которому принадлежит cell, потоплен?"""
        for ship in self.ships[defender]:
            if cell_set in ship:
                return ship if ship <= self.hits[self.opp(defender)] else None
        return None

    def body(self):
        me, opp = self.turn, self.opp(self.turn)
        opp_cells = set().union(*self.ships[opp]) if self.ships[opp] else set()
        opp_left = len(opp_cells) - len(self.hits[me] & opp_cells)
        if self.finished:
            return visual.hint("поле победителя: 🚢 — уцелевшие корабли соперника, ❌ — потопленные")
        return "\n".join([f"🎯 стреляет {self.mark(me)} <b>{_esc(self.pname(me))}</b> по полю соперника",
                          visual.hint(f"у соперника целых клеток кораблей: {opp_left} · 💥 попал · ❌ потоплен · 🔵 мимо")])

    def board_buttons(self):
        me, opp = self.turn, self.opp(self.turn)
        opp_cells = set().union(*self.ships[opp]) if self.ships[opp] else set()
        sunk_cells = set()
        for ship in self.ships[opp]:
            if ship <= self.hits[me]:
                sunk_cells |= ship
        rows = []
        for r in range(self.N):
            row = []
            for c in range(self.N):
                cell = (r, c)
                if self.finished:
                    if cell in sunk_cells or (cell in opp_cells and cell in self.hits[me]):
                        row.append(("❌", self.cb("x")))
                    elif cell in opp_cells:
                        row.append(("🚢", self.cb("x")))
                    elif cell in self.shots[me]:
                        row.append(("🔵", self.cb("x")))
                    else:
                        row.append(("🌊", self.cb("x")))
                    continue
                if cell in sunk_cells:
                    row.append(("❌", self.cb("x")))
                elif cell in self.hits[me]:
                    row.append(("💥", self.cb("x")))
                elif cell in self.shots[me]:
                    row.append(("🔵", self.cb("x")))
                else:
                    row.append(("🌊", self.cb(f"f{r}{c}")))
            rows.append(row)
        return rows

    def private_text(self, i):
        """Свой флот: где корабли, куда попал и куда промахнулся соперник."""
        o = self.opp(i)
        own = set().union(*self.ships[i]) if self.ships[i] else set()
        sunk = set()
        for ship in self.ships[i]:
            if ship <= self.hits[o]:
                sunk |= ship
        rows = []
        for r in range(self.N):
            line = ""
            for c in range(self.N):
                cell = (r, c)
                if cell in sunk:
                    line += "❌"
                elif cell in own and cell in self.hits[o]:
                    line += "💥"
                elif cell in own:
                    line += "🚢"
                elif cell in self.shots[o]:
                    line += "🔵"
                else:
                    line += "🌊"
            rows.append(line)
        left = len(own) - len(own & self.hits[o])
        status = ("ваш выстрел" if self.turn == i else "стреляет соперник") if not self.finished else ""
        return "\n".join(["<b>Ваш флот</b>  " + visual.hint(f"целых клеток: {left}"), *rows,
                          visual.hint("🚢 корабль · 💥 подбит · ❌ потоплен · 🔵 промах соперника"),
                          visual.hint(status) if status else ""]).rstrip()

    def private_buttons(self, i):
        return [] if self.finished else [[("🔄 Обновить", self.pcb("refresh"))]]

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if not tok.startswith('f'):
            return None
        r, c = int(tok[1]), int(tok[2])
        cell = (r, c)
        if cell in self.shots[i]:
            return ("Сюда уже стреляли", False)
        self.shots[i].add(cell)
        opp = self.opp(i)
        opp_cells = set().union(*self.ships[opp]) if self.ships[opp] else set()
        if cell in opp_cells:
            self.hits[i].add(cell)
            if opp_cells <= self.hits[i]:
                self.finished, self.winner = True, self.pid(i)
                return ("💥 Потоплен последний — победа!", True)
            ship = self._sunk(opp, cell)
            return ("☠️ Корабль потоплен! стреляйте ещё" if ship else "💥 Попадание! стреляйте ещё", False)
        self.turn = opp
        return ("🌊 Мимо", False)


# =========================================================================== 4-bis. Шашки

class Checkers(Game):
    KEY = 'dam'
    NAME = 'Шашки'
    EMOJI = '🔴'
    ALIASES = ('checkers', 'draughts', 'шашки')
    HELP = 'Русские шашки. Простая ходит по диагонали вперёд, бьёт в любую сторону. Бить обязательно. Дошла до края — дамка. Побеждает тот, кто забрал все шашки соперника или запер их.'
    N = 8

    def setup(self):
        # board[r][c] = None | (owner, king). owner 0 сверху (идёт вниз), 1 снизу (идёт вверх)
        self.b = [[None] * self.N for _ in range(self.N)]
        for r in range(self.N):
            for c in range(self.N):
                if (r + c) % 2 == 1:
                    if r < 3:
                        self.b[r][c] = (0, False)
                    elif r > 4:
                        self.b[r][c] = (1, False)
        self.turn = 0
        self.sel = None                   # (r,c) выбранная шашка
        self.chain = None                 # (r,c) обязана продолжать серию взятий

    def mark(self, i):
        return ('🔴', '🔵')[i] if i in (0, 1) else '·'

    def _piece_captures(self, r, c):
        """ходы-взятия для шашки на (r,c): [(tr,tc, captured(r,c)), ...]"""
        owner, king = self.b[r][c]
        res = []
        dirs = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
        for dr, dc in dirs:
            if not king:
                mr, mc = r + dr, c + dc
                lr, lc = r + 2 * dr, c + 2 * dc
                if 0 <= lr < self.N and 0 <= lc < self.N and self.b[lr][lc] is None:
                    mid = self.b[mr][mc] if 0 <= mr < self.N and 0 <= mc < self.N else None
                    if mid is not None and mid[0] != owner:
                        res.append((lr, lc, (mr, mc)))
            else:
                rr, cc = r + dr, c + dc
                seen = None
                while 0 <= rr < self.N and 0 <= cc < self.N:
                    cell = self.b[rr][cc]
                    if cell is None:
                        if seen is not None:
                            res.append((rr, cc, seen))
                    elif cell[0] != owner and seen is None:
                        seen = (rr, cc)
                    else:
                        break
                    rr, cc = rr + dr, cc + dc
        return res

    def _piece_moves(self, r, c):
        """тихие ходы (без взятия)"""
        owner, king = self.b[r][c]
        res = []
        if not king:
            fdir = 1 if owner == 0 else -1
            for dc in (-1, 1):
                nr, nc = r + fdir, c + dc
                if 0 <= nr < self.N and 0 <= nc < self.N and self.b[nr][nc] is None:
                    res.append((nr, nc))
        else:
            for dr, dc in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                rr, cc = r + dr, c + dc
                while 0 <= rr < self.N and 0 <= cc < self.N and self.b[rr][cc] is None:
                    res.append((rr, cc))
                    rr, cc = rr + dr, cc + dc
        return res

    def _all_captures(self, owner):
        out = {}
        for r in range(self.N):
            for c in range(self.N):
                if self.b[r][c] and self.b[r][c][0] == owner:
                    caps = self._piece_captures(r, c)
                    if caps:
                        out[(r, c)] = caps
        return out

    def _legal_targets(self, r, c):
        """куда может пойти выбранная (r,c) с учётом обязательного взятия"""
        owner = self.b[r][c][0]
        caps_all = self._all_captures(owner)
        if self.chain is not None:
            if (r, c) != self.chain:
                return [], True
            return self._piece_captures(r, c), True
        if caps_all:
            return caps_all.get((r, c), []), True
        return [(tr, tc, None) for tr, tc in self._piece_moves(r, c)], False

    def _has_any_move(self, owner):
        if self._all_captures(owner):
            return True
        for r in range(self.N):
            for c in range(self.N):
                if self.b[r][c] and self.b[r][c][0] == owner and self._piece_moves(r, c):
                    return True
        return False

    def body(self):
        mine = [sum(1 for row in self.b for x in row if x and x[0] == i) for i in (0, 1)]
        lines = [f"🔴 {mine[0]} : {mine[1]} 🔵   " + visual.hint("🔺🔷 — дамки")]
        if self.finished:
            return "\n".join(lines)
        if self.chain:
            lines.append(visual.warn("серия взятий — бейте дальше той же шашкой"))
        elif self.sel:
            lines.append(visual.hint(f"выбрана {chr(97 + self.sel[1])}{self.N - self.sel[0]} — ✅ куда можно пойти"))
        elif self._all_captures(self.turn):
            lines.append(visual.warn("есть взятие — бить обязательно"))
        else:
            lines.append(visual.hint("выберите свою шашку"))
        return "\n".join(lines)

    def board_buttons(self):
        if self.finished:
            return []
        glyph = {None: '·', (0, False): '🔴', (0, True): '🔺', (1, False): '🔵', (1, True): '🔷'}
        targets = {}
        if self.sel:
            tg, _forced = self._legal_targets(*self.sel)
            targets = {(t[0], t[1]): t for t in tg}
        rows = []
        for r in range(self.N):
            row = []
            for c in range(self.N):
                if (r + c) % 2 == 0:
                    row.append(("▪️", self.cb("x")))
                    continue
                cell = self.b[r][c]
                if self.sel == (r, c):
                    row.append(("✴️", self.cb("uns")))
                elif (r, c) in targets:
                    row.append(("✅", self.cb(f"m{r}{c}")))
                elif cell and cell[0] == self.turn:
                    row.append((glyph[cell], self.cb(f"s{r}{c}")))
                else:
                    row.append((glyph[cell] if cell else "·", self.cb("x")))
            rows.append(row)
        return rows

    def _promote(self, r, c):
        owner, king = self.b[r][c]
        if not king and ((owner == 0 and r == self.N - 1) or (owner == 1 and r == 0)):
            self.b[r][c] = (owner, True)
            return True
        return False

    def _end_turn_or_continue(self, i, r, c, captured):
        if captured and self._piece_captures(r, c):
            self.chain = (r, c)
            self.sel = (r, c)
            return ("Бьём дальше!", False)
        self.chain = None
        self.sel = None
        promoted = self._promote(r, c)
        self.turn = self.opp(i)
        if not self._has_any_move(self.turn):
            self.finished, self.winner = True, self.pid(i)
            return ("Сопернику некуда ходить — победа!", True)
        return ("Дамка!" if promoted else None, False) if promoted else None

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if tok == 'uns':
            if self.chain is not None:
                return ("Серию взятий нельзя прерывать", False)
            self.sel = None
            return None
        if tok.startswith('s'):
            r, c = int(tok[1]), int(tok[2])
            if not self.b[r][c] or self.b[r][c][0] != i:
                return ("Это не ваша шашка", False)
            caps = self._all_captures(i)
            if caps and (r, c) not in caps:
                return ("Есть обязательное взятие другой шашкой", False)
            if self.chain is not None and (r, c) != self.chain:
                return ("Доигрывайте серию начатой шашкой", False)
            self.sel = (r, c)
            return None
        if tok.startswith('m'):
            if not self.sel:
                return ("Сначала выберите шашку", False)
            r, c = int(tok[1]), int(tok[2])
            tg, _f = self._legal_targets(*self.sel)
            move = next((t for t in tg if (t[0], t[1]) == (r, c)), None)
            if move is None:
                return ("Сюда нельзя", False)
            sr, sc = self.sel
            piece = self.b[sr][sc]
            self.b[sr][sc] = None
            self.b[r][c] = piece
            captured = move[2]
            if captured:
                self.b[captured[0]][captured[1]] = None
            return self._end_turn_or_continue(i, r, c, captured)
        return None


# =========================================================================== 5-bis. Шахматы

class Chess(Game):
    KEY = 'chess'
    NAME = 'Шахматы'
    EMOJI = '♟'
    ALIASES = ('chess', 'шахматы')
    HELP = 'Классические шахматы. Выберите фигуру, затем клетку. Пешка на последней линии становится ферзём. Рокировки и взятия на проходе нет. Мат — победа, пат — ничья.'
    N = 8
    GLYPH = {(0, 'K'): '♔', (0, 'Q'): '♕', (0, 'R'): '♖', (0, 'B'): '♗', (0, 'N'): '♘', (0, 'P'): '♙',
             (1, 'K'): '♚', (1, 'Q'): '♛', (1, 'R'): '♜', (1, 'B'): '♝', (1, 'N'): '♞', (1, 'P'): '♟'}

    def setup(self):
        self.b = [[None] * 8 for _ in range(8)]
        back = ['R', 'N', 'B', 'Q', 'K', 'B', 'N', 'R']
        for c in range(8):
            self.b[0][c] = (1, back[c])       # чёрные сверху
            self.b[1][c] = (1, 'P')
            self.b[6][c] = (0, 'P')
            self.b[7][c] = (0, back[c])        # белые снизу
        self.turn = 0                          # 0 = белые
        self.sel = None
        self.check = False

    def mark(self, i):
        return ('♙', '♟')[i] if i in (0, 1) else '·'

    def _in(self, r, c):
        return 0 <= r < 8 and 0 <= c < 8

    def _pseudo(self, r, c, board=None):
        board = board or self.b
        color, p = board[r][c]
        res = []
        def add(nr, nc, capture_only=False, move_only=False):
            if not self._in(nr, nc):
                return False
            t = board[nr][nc]
            if t is None:
                if not capture_only:
                    res.append((nr, nc))
                return True
            if t[0] != color and not move_only:
                res.append((nr, nc))
            return False
        if p == 'P':
            fwd = -1 if color == 0 else 1
            start = 6 if color == 0 else 1
            if self._in(r + fwd, c) and board[r + fwd][c] is None:
                res.append((r + fwd, c))
                if r == start and board[r + 2 * fwd][c] is None:
                    res.append((r + 2 * fwd, c))
            for dc in (-1, 1):
                nr, nc = r + fwd, c + dc
                if self._in(nr, nc) and board[nr][nc] is not None and board[nr][nc][0] != color:
                    res.append((nr, nc))
        elif p == 'N':
            for dr, dc in ((-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1)):
                add(r + dr, c + dc)
        elif p == 'K':
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr or dc:
                        add(r + dr, c + dc)
        else:
            dirs = {'B': ((-1, -1), (-1, 1), (1, -1), (1, 1)),
                    'R': ((-1, 0), (1, 0), (0, -1), (0, 1)),
                    'Q': ((-1, -1), (-1, 1), (1, -1), (1, 1), (-1, 0), (1, 0), (0, -1), (0, 1))}[p]
            for dr, dc in dirs:
                nr, nc = r + dr, c + dc
                while self._in(nr, nc):
                    if not add(nr, nc):
                        break
                    nr, nc = nr + dr, nc + dc
        return res

    def _king(self, color, board=None):
        board = board or self.b
        for r in range(8):
            for c in range(8):
                if board[r][c] == (color, 'K'):
                    return (r, c)
        return None

    def _attacked(self, r, c, by_color, board=None):
        board = board or self.b
        for rr in range(8):
            for cc in range(8):
                if board[rr][cc] and board[rr][cc][0] == by_color:
                    if (r, c) in self._pseudo(rr, cc, board):
                        return True
        return False

    def _in_check(self, color, board=None):
        board = board or self.b
        k = self._king(color, board)
        return k is not None and self._attacked(k[0], k[1], 1 - color, board)

    def _legal(self, r, c):
        color = self.b[r][c][0]
        out = []
        for nr, nc in self._pseudo(r, c):
            nb = [row[:] for row in self.b]
            nb[nr][nc] = nb[r][c]
            nb[r][c] = None
            if nb[nr][nc][1] == 'P' and nr in (0, 7):
                nb[nr][nc] = (color, 'Q')
            if not self._in_check(color, nb):
                out.append((nr, nc))
        return out

    def _any_legal(self, color):
        for r in range(8):
            for c in range(8):
                if self.b[r][c] and self.b[r][c][0] == color and self._legal(r, c):
                    return True
        return False

    def body(self):
        if self.finished:
            res = getattr(self, 'result', None)
            return visual.hint({'mate': "мат", 'stalemate': "пат — ничья"}.get(res, "партия окончена"))
        lines = []
        if self.check:
            lines.append(visual.warn("шах!"))
        if self.sel:
            lines.append(visual.hint(f"выбрана {chr(97 + self.sel[1])}{8 - self.sel[0]} — ✅ куда можно пойти, ✴️ — отменить"))
        else:
            side = "белые ♙" if self.turn == 0 else "чёрные ♟"
            lines.append(visual.hint(f"ходят {side}: выберите фигуру"))
        return "\n".join(lines)

    def board_buttons(self):
        if self.finished:
            return []
        targets = {}
        if self.sel:
            targets = {t: True for t in self._legal(*self.sel)}
        rows = []
        for r in range(8):
            row = []
            for c in range(8):
                cell = self.b[r][c]
                if self.sel == (r, c):
                    row.append(("✴️", self.cb("uns")))
                elif (r, c) in targets:
                    row.append(("✅", self.cb(f"m{r}{c}")))
                elif cell and cell[0] == self.turn:
                    row.append((self.GLYPH[cell], self.cb(f"s{r}{c}")))
                else:
                    row.append((self.GLYPH[cell] if cell else ('▫️' if (r + c) % 2 else '▪️'), self.cb("x")))
            rows.append(row)
        return rows

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if tok == 'uns':
            self.sel = None
            return None
        if tok.startswith('s'):
            r, c = int(tok[1]), int(tok[2])
            if not self.b[r][c] or self.b[r][c][0] != i:
                return ("Это не ваша фигура", False)
            if not self._legal(r, c):
                return ("У этой фигуры нет ходов", False)
            self.sel = (r, c)
            return None
        if tok.startswith('m'):
            if not self.sel:
                return ("Сначала выберите фигуру", False)
            r, c = int(tok[1]), int(tok[2])
            if (r, c) not in self._legal(*self.sel):
                return ("Так нельзя — остаётесь под шахом", False)
            sr, sc = self.sel
            piece = self.b[sr][sc]
            self.b[sr][sc] = None
            if piece[1] == 'P' and r in (0, 7):
                piece = (piece[0], 'Q')
            self.b[r][c] = piece
            self.sel = None
            opp = self.opp(i)
            self.check = self._in_check(opp)
            if not self._any_legal(opp):
                self.finished = True
                self.winner = self.pid(i) if self.check else 'draw'   # мат или пат
                self.result = 'mate' if self.check else 'stalemate'
                return ("Мат!" if self.check else "Пат — ничья", True)
            self.turn = opp
            return ("Шах!", False) if self.check else None
        return None


# =========================================================================== 14. Дурак

class Durak(Game):
    KEY = 'durak'
    NAME = 'Дурак'
    EMOJI = '🃏'
    ALIASES = ('durak', 'дурак')
    HELP = ('Подкидной дурак 1 на 1. Атакующий кладёт карту, защищающийся бьёт старшей той же масти или козырем. '
            'Не может — берёт. Кто останется с картами, когда колода кончилась, — дурак. '
            'Свои карты каждый видит и разыгрывает в личке с ботом (кнопка «🃏 Мои карты»).')
    PRIVATE = True
    RANKS = ['6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    SUITS = {'♠': 's', '♥': 'h', '♦': 'd', '♣': 'c'}
    SUIT_CH = ['♠', '♥', '♦', '♣']

    def setup(self):
        deck = [(r, s) for s in self.SUIT_CH for r in self.RANKS]
        random.shuffle(deck)
        self.trump_card = deck[-1]
        self.trump = deck[-1][1]
        self.deck = deck
        self.hands = [[], []]
        for _ in range(6):
            for h in self.hands:
                h.append(self.deck.pop(0))
        self._sort_all()
        # первый ход — у кого меньший козырь
        self.attacker = self._first_attacker()
        self.defender = self.opp(self.attacker)
        self.turn = self.attacker
        self.table = []                    # [{'atk':card, 'dfn':card|None}]
        self.phase = 'attack'
        self.msg = ''

    def _rank_val(self, r):
        return self.RANKS.index(r)

    def _first_attacker(self):
        best = None
        who = 0
        for i in (0, 1):
            for r, s in self.hands[i]:
                if s == self.trump:
                    if best is None or self._rank_val(r) < best:
                        best, who = self._rank_val(r), i
        return who

    def _sort(self, hand):
        hand.sort(key=lambda c: (c[1] == self.trump, self.SUIT_CH.index(c[1]), self._rank_val(c[0])))

    def _sort_all(self):
        for h in self.hands:
            self._sort(h)

    def _beats(self, atk, dfn):
        (ar, as_), (dr, ds) = atk, dfn
        if as_ == ds:
            return self._rank_val(dr) > self._rank_val(ar)
        return ds == self.trump and as_ != self.trump

    def _table_ranks(self):
        rs = set()
        for pair in self.table:
            rs.add(pair['atk'][0])
            if pair['dfn']:
                rs.add(pair['dfn'][0])
        return rs

    def _unbeaten(self):
        return [p for p in self.table if p['dfn'] is None]

    def mark(self, i):
        return ('🅰', '🅱')[i] if i in (0, 1) else '·'

    def _card(self, c):
        return f"{c[0]}{c[1]}"

    def body(self):
        lines = [visual.field("козырь", f"{self._card(self.trump_card)}", code=False)
                 + f"  ·  в колоде {len(self.deck)}"]
        who = f"⚔️ атакует {self.mark(self.attacker)} {_esc(self.pname(self.attacker))}"
        lines.append(who)
        if self.table:
            tb = []
            for p in self.table:
                s = self._card(p['atk'])
                s += f"／{self._card(p['dfn'])}" if p['dfn'] else "／▢"
                tb.append(s)
            lines.append("стол: " + "  ".join(tb))
        else:
            lines.append(visual.hint("стол пуст"))
        lines.append(visual.hint(f"карт: {self.pname(0)} — {len(self.hands[0])} · {self.pname(1)} — {len(self.hands[1])}"))
        if self.msg:
            lines.append(self.msg)
        if not self.finished:
            actor = self.attacker if self.phase == 'attack' else self.defender
            what = "ходит / подкидывает" if self.phase == 'attack' else "отбивается"
            lines.append(f"сейчас {what}: {self.mark(actor)} {_mention(self.pid(actor), self.pname(actor))}")
            lines.append(visual.hint("карты и ходы — у каждого в личке с ботом: «🃏 Мои карты»"))
        else:
            lines.append(visual.hint("на руках: " + " · ".join(
                f"{_esc(self.pname(i))}: " + (" ".join(self._card(c) for c in self.hands[i]) or "—") for i in (0, 1))))
        return "\n".join(lines)

    def turn_line(self):
        if self.finished:
            return super().turn_line()
        actor = self.attacker if self.phase == 'attack' else self.defender
        return f"ход: {self.mark(actor)} {_mention(self.pid(actor), self.pname(actor))}"

    # ---- личный экран: только свои карты ----
    def private_text(self, i):
        hand = " ".join(self._card(c) for c in self.hands[i]) or "—"
        lines = [visual.field("козырь", self._card(self.trump_card), code=False) + f"  ·  в колоде {len(self.deck)}",
                 f"🃏 ваши карты: <b>{hand}</b>",
                 f"у соперника карт: {len(self.hands[self.opp(i)])}"]
        if self.table:
            lines.append("стол: " + "  ".join(self._card(p['atk']) + ("／" + self._card(p['dfn']) if p['dfn'] else "／▢")
                                              for p in self.table))
        if self.msg:
            lines.append(self.msg)
        if self.finished:
            return "\n".join(lines)
        actor = self.attacker if self.phase == 'attack' else self.defender
        if actor != i:
            lines.append(visual.hint("ждём хода соперника"))
        elif self.phase == 'attack':
            lines.append(visual.hint("ваш ход: выберите карту" + (" или «Бито»" if self.table and not self._unbeaten() else "")))
        else:
            lines.append(visual.hint("отбивайтесь: выберите карту или «Беру»"))
        return "\n".join(lines)

    def private_buttons(self, i):
        if self.finished:
            return []
        actor = self.attacker if self.phase == 'attack' else self.defender
        if actor != i:
            return [[("🔄 Обновить", self.pcb("refresh"))]]
        rows = []
        if self.phase == 'attack':
            legal = set(self._legal_attack_cards(i))
            cards = [(self._card(c), self.pcb(f"a{k}")) for k, c in enumerate(self.hands[i]) if k in legal]
            rows += [cards[j:j + 4] for j in range(0, len(cards), 4)]
            if self.table and not self._unbeaten():
                rows.append([("✅ Бито", self.pcb("bito"), "success")])
            elif not cards:
                rows.append([("— нечем ходить —", self.pcb("x"))])
        else:
            target = next((p for p in self.table if p['dfn'] is None), None)
            cards = [(self._card(c), self.pcb(f"d{k}")) for k, c in enumerate(self.hands[i])
                     if target is not None and self._beats(target['atk'], c)]
            rows += [cards[j:j + 4] for j in range(0, len(cards), 4)]
            rows.append([("🤲 Беру", self.pcb("take"), "danger")])
        return rows

    def _legal_attack_cards(self, i):
        hand = self.hands[i]
        if not self.table:
            return list(range(len(hand)))
        ranks = self._table_ranks()
        # нельзя подкинуть больше, чем у защищающегося карт, и не больше 6
        if len(self.table) >= 6 or len(self._unbeaten()) > 0:
            pass
        limit = min(6, len(self.hands[self.defender]) + len([p for p in self.table if p['dfn']]))
        if len(self.table) >= limit:
            return []
        return [k for k, c in enumerate(hand) if c[0] in ranks]

    def board_buttons(self):
        return []                       # карты на общем поле не показываем — только в личке у каждого

    def _draw_up(self):
        order = [self.attacker, self.defender]
        for i in order:
            while len(self.hands[i]) < 6 and self.deck:
                self.hands[i].append(self.deck.pop(0))
            self._sort(self.hands[i])

    def _check_end(self):
        if self.deck:
            return False
        empt = [i for i in (0, 1) if not self.hands[i]]
        if len(empt) == 2:
            self.finished, self.winner = True, 'draw'
            return True
        if len(empt) == 1 and not self.table:
            self.finished, self.winner = True, self.pid(empt[0])  # вышел из карт — победил
            return True
        return False

    def _end_bout(self, defender_took):
        self.table = []
        self._draw_up()
        if self._check_end():
            return
        if defender_took:
            pass                            # атакующий остаётся
        else:
            self.attacker, self.defender = self.defender, self.attacker
        self.turn = self.attacker
        self.phase = 'attack'

    async def handle(self, pid, tok):
        i = self.idx(pid)
        actor = self.attacker if self.phase == 'attack' else self.defender
        if i != actor:
            return ("Сейчас ход соперника", False)
        if tok == 'x':
            return None
        if self.phase == 'attack':
            if tok == 'bito':
                if self._unbeaten():
                    return ("Сначала соперник должен отбиться", False)
                if not self.table:
                    return ("Сначала сходите", False)
                self.msg = "Бито"
                self._end_bout(defender_took=False)
                return None
            if tok.startswith('a') and tok[1:].isdigit():
                k = int(tok[1:])
                if k not in self._legal_attack_cards(i):
                    return ("Этой картой нельзя", False)
                card = self.hands[i].pop(k)
                self.table.append({'atk': card, 'dfn': None})
                self.phase = 'defend'
                self.turn = self.defender
                self.msg = ''
                return None
        else:
            if tok == 'take':
                for p in self.table:
                    self.hands[self.defender].append(p['atk'])
                    if p['dfn']:
                        self.hands[self.defender].append(p['dfn'])
                self._sort(self.hands[self.defender])
                self.msg = f"{_esc(self.pname(self.defender))} берёт"
                self._end_bout(defender_took=True)
                return None
            if tok.startswith('d') and tok[1:].isdigit():
                k = int(tok[1:])
                if not (0 <= k < len(self.hands[self.defender])):
                    return ("Карта уже ушла — обновите экран", False)
                card = self.hands[self.defender][k]
                target = next((p for p in self.table if p['dfn'] is None), None)
                if target is None:
                    return ("Нечего бить", False)
                if not self._beats(target['atk'], card):
                    return ("Эта карта не бьёт", False)
                target['dfn'] = self.hands[self.defender].pop(k)
                self.phase = 'attack'
                self.turn = self.attacker
                return None
        return None


# =========================================================================== регистрация игр

for _cls in (TicTacToe, ConnectFour, Battleship, Checkers, Chess, Reversi, MinesweeperDuel,
             RPS, Hangman, BullsCows, Blackjack, Nim, DotsAndBoxes, Durak):
    GAMES[_cls.KEY] = _cls
