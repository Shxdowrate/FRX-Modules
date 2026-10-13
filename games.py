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
"""

import asyncio
import html
import random
import secrets
import time

import decorators
import engine
import visual

NAME = "Games"
MODULE_NAME = "games"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "14 игр на двоих на инлайн-кнопках: крестики-нолики, 4 в ряд, морской бой, шашки, шахматы, реверси, сапёр, КНБ, виселица, быки и коровы, блэкджек, ним, точки-квадраты, дурак"

MATCHES = {}          # mid -> Match
GAMES = {}            # key -> Game class (заполняется регистрацией в конце файла)


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

    async def handle_input(self, pid, data, value):
        """Ввод через InputButton (игры с секретным словом/числом)."""
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
        if self.form is not None:
            await self.form.edit(self.text(), buttons=self.buttons())

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
        MATCHES.pop(self.mid, None)
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
    try:
        res = await m.game.handle(uid, tok)
    except Exception as e:
        engine.tlog(f"[Games/{m.game_key}] {type(e).__name__}: {e}")
        await click.answer(f"ошибка: {str(e)[:150]}", show_alert=True)
        return
    if res is not None:
        text, alert = res if isinstance(res, tuple) else (res, False)
        if text:
            await click.answer(text, show_alert=alert)
    else:
        await click.answer()
    if m.phase == 'play' and m.game.finished:
        m.phase = 'over'
    await m.arm_timeout()
    await m.render()


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
        allowed = [chat_id] if chat_id < 0 else ['me', challenger['id']]   # в группе — все, в личке — вы
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

    @form.on_input()
    async def _(inp):
        await _on_input(inp, m.mid)


async def _on_input(inp, mid):
    m = MATCHES.get(mid)
    if m is None or m.game is None or m.game.finished:
        return
    uid = inp.from_user.id if inp.from_user else None
    i = m.game.idx(uid)
    if i < 0:
        return
    try:
        res = await m.game.handle_input(uid, inp.data, inp.value or '')
    except Exception as e:
        engine.tlog(f"[Games/{m.game_key}] input: {type(e).__name__}: {e}")
        res = None
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
        lines = [f"раунд <b>{min(self.round, self.BEST_OF)}</b> из {self.BEST_OF}",
                 f"счёт: {self.score[0]} : {self.score[1]}"]
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
    HELP = 'Набирайте очки ближе к 21, но не больше. «Взять» — ещё карта, «Хватит» — остановиться. Перебор (>21) — проигрыш.'
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
            reveal = self.finished or self.stood[i]
            s = self._score(self.hands[i])
            shown = self._hand_str(self.hands[i]) if (reveal or i == self.turn) else self._hand_str(self.hands[i], hide=True)
            score = f" = <b>{s}</b>" + (" 💥" if s > 21 else '') if (reveal or i == self.turn) else ''
            tag = " ✋" if self.stood[i] else ''
            lines.append(f"{self.mark(i)} {_esc(self.pname(i))}: {shown}{score}{tag}")
        return "\n".join(lines)

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
    HELP = 'Первый игрок загадывает слово, второй отгадывает по буквам. 6 ошибок — и отгадывающий проиграл.'
    STAGES = ['😀', '🙂', '😐', '😟', '😨', '😰', '💀']
    RU = list('абвгдеёжзийклмнопрстуфхцчшщъыьэюя')
    EN = list('abcdefghijklmnopqrstuvwxyz')

    def setup(self):
        self.word = None
        self.opened = set()
        self.wrong = set()
        self.lives = 6
        self.phase_set = True            # ждём загадку от игрока 0

    def _alphabet(self):
        return self.RU if (self.word and any(c in 'абвгдеёжзийклмнопрстуфхцчшщъыьэюя' for c in self.word)) else self.EN

    def _masked(self):
        return " ".join(c.upper() if (not c.isalpha() or c in self.opened) else '▢' for c in self.word)

    def body(self):
        if self.phase_set:
            return visual.hint(f"{_esc(self.pname(0))} загадывает слово…")
        lines = [self.STAGES[min(len(self.wrong), 6)],
                 f"<code>{_esc(self._masked())}</code>",
                 visual.field("осталось ошибок", self.lives - len(self.wrong), code=False)]
        if self.wrong:
            lines.append(visual.hint("мимо: " + ", ".join(sorted(self.wrong)).upper()))
        return "\n".join(lines)

    def board_buttons(self):
        if self.finished:
            return []
        if self.phase_set:
            return [[engine.InputButton("🤫 Загадать слово", "setword", hint="слово или фраза (видит только модуль)",
                                        for_user=self.pid(0))]]
        rows, row = [], []
        for ch in self._alphabet():
            used = ch in self.opened or ch in self.wrong
            row.append((ch.upper() if not used else '·', self.cb(f"l{ch}" if not used else "x")))
            if len(row) == 8:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    async def handle_input(self, pid, data, value):
        if data != 'setword' or not self.phase_set or self.idx(pid) != 0:
            return None
        w = (value or '').strip().lower()
        if not w or not any(c.isalpha() for c in w) or len(w) > 30:
            return None
        self.word = w
        self.phase_set = False
        self.turn = 1                     # отгадывает игрок 1
        return None

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if self.phase_set:
            return ("Ждём, пока загадают слово", False)
        if i != 1:
            return ("Отгадывает соперник", False)
        if not tok.startswith('l'):
            return None
        ch = tok[1:]
        if ch in self.opened or ch in self.wrong:
            return ("Эту букву уже называли", False)
        if ch in self.word:
            self.opened.add(ch)
            if all((not c.isalpha()) or c in self.opened for c in self.word):
                self.finished, self.winner = True, self.pid(1)
            return ("Есть такая!", False)
        self.wrong.add(ch)
        if len(self.wrong) >= self.lives:
            self.finished, self.winner = True, self.pid(0)
            return (f"Перебор ошибок. Слово: {self.word.upper()}", True)
        return ("Мимо", False)


# =========================================================================== 8. Быки и коровы

class BullsCows(Game):
    KEY = 'bulls'
    NAME = 'Быки и коровы'
    EMOJI = '🐂'
    ALIASES = ('быкиикоровы', 'bullsandcows', 'коровы')
    HELP = 'Каждый загадывает 4-значное число (цифры разные). По очереди угадываете число соперника: 🐂 — цифра на месте, 🐄 — есть, но не там. Кто первым наберёт 4 быка — победил.'

    def setup(self):
        self.secret = [None, None]       # загаданное каждым
        self.history = [[], []]          # [(guess, bulls, cows), ...] по игрокам
        self.turn = 0

    def _ready(self):
        return all(s is not None for s in self.secret)

    @staticmethod
    def _valid(num):
        return len(num) == 4 and num.isdigit() and len(set(num)) == 4

    @staticmethod
    def _bc(guess, secret):
        bulls = sum(a == b for a, b in zip(guess, secret))
        cows = sum(min(guess.count(d), secret.count(d)) for d in set(guess)) - bulls
        return bulls, cows

    def body(self):
        if not self._ready():
            waiting = [self.pname(i) for i in (0, 1) if self.secret[i] is None]
            return visual.hint("загадывают число: " + ", ".join(_esc(w) for w in waiting))
        lines = []
        for i in (0, 1):
            lines.append(f"{self.mark(i)} <b>{_esc(self.pname(i))}</b>")
            for g, b, c in self.history[i][-4:]:
                lines.append(f"   <code>{g}</code>  🐂{b} 🐄{c}")
            if not self.history[i]:
                lines.append("   " + visual.hint("ходов нет"))
        return "\n".join(lines)

    def board_buttons(self):
        if self.finished:
            return []
        if not self._ready():
            rows = []
            for i in (0, 1):
                if self.secret[i] is None:
                    rows.append([engine.InputButton(f"🔢 Загадать число ({self.pname(i)[:12]})", f"set{i}",
                                                    hint="4 разные цифры, напр. 1234", for_user=self.pid(i))])
            return rows
        return [[engine.InputButton("🎯 Ваш ход — число", "guess", hint="4 разные цифры")]]

    async def handle_input(self, pid, data, value):
        i = self.idx(pid)
        v = (value or '').strip()
        if data in ('set0', 'set1'):
            if int(data[-1]) != i or self.secret[i] is not None:
                return None
            if not self._valid(v):
                return None
            self.secret[i] = v
            return None
        if data == 'guess' and self._ready():
            if i != self.turn or not self._valid(v):
                return None
            b, c = self._bc(v, self.secret[self.opp(i)])
            self.history[i].append((v, b, c))
            if b == 4:
                self.finished, self.winner = True, self.pid(i)
            else:
                self.turn = self.opp(i)
            return None
        return None


# =========================================================================== 9. Сапёр-дуэль

class MinesweeperDuel(Game):
    KEY = 'ms'
    NAME = 'Сапёр-дуэль'
    EMOJI = '💣'
    ALIASES = ('minesweeper', 'сапёр', 'saper')
    HELP = 'По очереди открывайте клетки общего поля. Под клеткой может быть мина — кто её вскроет, проигрывает. Цифра — сколько мин рядом.'
    W, H, MINES = 6, 6, 6

    def setup(self):
        cells = [(r, c) for r in range(self.H) for c in range(self.W)]
        self.mines = set(random.sample(cells, self.MINES))
        self.open = {}                   # (r,c) -> число соседних мин
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

    DIGITS = {0: '·', 1: '1️⃣', 2: '2️⃣', 3: '3️⃣', 4: '4️⃣', 5: '5️⃣', 6: '6️⃣', 7: '7️⃣', 8: '8️⃣'}

    def body(self):
        left = self.W * self.H - self.MINES - len(self.open)
        return visual.hint(f"мин на поле: {self.MINES} · безопасных клеток осталось: {left}")

    def board_buttons(self):
        rows = []
        reveal = self.finished
        for r in range(self.H):
            row = []
            for c in range(self.W):
                if (r, c) in self.open:
                    row.append((self.DIGITS[self.open[(r, c)]], self.cb("x")))
                elif reveal and (r, c) in self.mines:
                    row.append(("💥", self.cb("x")))
                else:
                    row.append(("⬜", self.cb(f"o{r}{c}") if not self.finished else "x"))
            rows.append(row)
        return rows

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if not tok.startswith('o'):
            return None
        r, c = int(tok[1]), int(tok[2])
        if (r, c) in self.open:
            return ("Уже открыто", False)
        if (r, c) in self.mines:
            self.open[(r, c)] = -1
            self.finished, self.winner = True, self.pid(self.opp(i))
            return ("💥 Мина! Вы проиграли", True)
        self._flood(r, c)
        if len(self.open) >= self.W * self.H - self.MINES:
            self.finished, self.winner = True, 'draw'      # всё безопасное вскрыто — ничья
            return ("Поле пройдено — ничья", False)
        self.turn = self.opp(i)
        return None


# =========================================================================== 12. Точки-квадраты

class DotsAndBoxes(Game):
    KEY = 'dab'
    NAME = 'Точки-квадраты'
    EMOJI = '⬛'
    ALIASES = ('dotsandboxes', 'точки', 'квадраты')
    HELP = 'Проводите линии между точками. Замкнули квадрат — он ваш и вы ходите ещё раз. Больше квадратов в конце — победа.'
    N = 3                                 # 3x3 точек -> 2x2 квадрата

    def setup(self):
        self.h = {}                       # горизонтальные рёбра (r,c) между (r,c)-(r,c+1): r in 0..N-1, c in 0..N-2
        self.v = {}                       # вертикальные рёбра (r,c): r 0..N-2, c 0..N-1
        self.boxes = {}                   # (r,c) 0..N-2 -> owner
        self.turn = 0
        self.score = [0, 0]

    def _box_done(self, r, c, trial=None):
        edges = [('h', r, c), ('h', r + 1, c), ('v', r, c), ('v', r, c + 1)]
        def has(kind, rr, cc):
            d = self.h if kind == 'h' else self.v
            return (rr, cc) in d or (trial == (kind, rr, cc))
        return all(has(*e) for e in edges)

    def body(self):
        # рисуем поле: точки, рёбра, владельцы
        lines = []
        owner_mark = {0: '🅰', 1: '🅱'}
        for r in range(self.N):
            # ряд точек + горизонтальные рёбра
            line = ''
            for c in range(self.N):
                line += '•'
                if c < self.N - 1:
                    line += '──' if (r, c) in self.h else '  '
            lines.append(line)
            if r < self.N - 1:
                line = ''
                for c in range(self.N):
                    line += '│' if (r, c) in self.v else ' '
                    if c < self.N - 1:
                        o = self.boxes.get((r, c))
                        line += owner_mark[o] if o is not None else '  '
                lines.append(line)
        lines.append(f"\n🅰 {self.score[0]} : {self.score[1]} 🅱")
        return "<code>" + "\n".join(lines) + "</code>"

    def mark(self, i):
        return ('🅰', '🅱')[i] if i in (0, 1) else '·'

    def board_buttons(self):
        if self.finished:
            return []
        rows = []
        # горизонтальные рёбра
        hrow = []
        for r in range(self.N):
            for c in range(self.N - 1):
                if (r, c) not in self.h:
                    hrow.append((f"─{r}{c}", self.cb(f"h{r}{c}")))
        for k in range(0, len(hrow), 4):
            rows.append(hrow[k:k + 4])
        vrow = []
        for r in range(self.N - 1):
            for c in range(self.N):
                if (r, c) not in self.v:
                    vrow.append((f"│{r}{c}", self.cb(f"v{r}{c}")))
        for k in range(0, len(vrow), 4):
            rows.append(vrow[k:k + 4])
        return rows

    async def handle(self, pid, tok):
        i = self.idx(pid)
        if i != self.turn:
            return ("Сейчас не ваш ход", False)
        if tok[0] not in ('h', 'v'):
            return None
        kind, r, c = tok[0], int(tok[1]), int(tok[2])
        d = self.h if kind == 'h' else self.v
        if (r, c) in d:
            return ("Линия уже есть", False)
        d[(r, c)] = i
        # какие квадраты замкнулись?
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
        elif gained == 0:
            self.turn = self.opp(i)
        return ("+%d квадрат(а) — ходите ещё!" % gained, False) if gained else None


# =========================================================================== 3-bis. Морской бой

class Battleship(Game):
    KEY = 'bs'
    NAME = 'Морской бой'
    EMOJI = '🚢'
    ALIASES = ('battleship', 'морскойбой', 'корабли')
    HELP = 'Корабли расставляются автоматически. По очереди стреляете по полю соперника. Попадание — ход снова, мимо — ход переходит. Потопите все корабли.'
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
        mine_left = sum(len(s) for s in self.ships[me]) - len(self.hits[opp] & set().union(*self.ships[me]) if self.ships[me] else set())
        opp_cells = set().union(*self.ships[opp]) if self.ships[opp] else set()
        opp_left = len(opp_cells) - len(self.hits[me] & opp_cells)
        return visual.hint(f"стреляет {self.mark(me)} {_esc(self.pname(me))} · у соперника осталось клеток кораблей: {opp_left}")

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
        glyph = {None: '·', (0, False): '🔴', (0, True): '🔺', (1, False): '🔵', (1, True): '🔷'}
        lines = []
        for r in range(self.N):
            row = ''
            for c in range(self.N):
                if (r + c) % 2 == 0:
                    row += '⬜'
                else:
                    row += glyph[self.b[r][c]] if self.b[r][c] is None else glyph[self.b[r][c]]
            lines.append(row)
        extra = ''
        if self.sel:
            extra = "\n" + visual.hint(f"выбрана {chr(97 + self.sel[1])}{self.N - self.sel[0]} — выберите клетку")
        elif self._all_captures(self.turn):
            extra = "\n" + visual.hint("есть взятие — бить обязательно")
        return "<code>" + "\n".join(lines) + "</code>" + extra

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
        lines = []
        for r in range(8):
            row = ''
            for c in range(8):
                cell = self.b[r][c]
                if cell is None:
                    row += '▫️' if (r + c) % 2 else '▪️'
                else:
                    row += self.GLYPH[cell]
            lines.append(row)
        tail = ''
        if self.check and not self.finished:
            tail = "\n" + visual.warn("шах!")
        elif self.sel:
            tail = "\n" + visual.hint(f"выбрана {chr(97 + self.sel[1])}{8 - self.sel[0]}")
        return "<code>" + "\n".join(lines) + "</code>" + tail

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
    HELP = 'Подкидной дурак 1 на 1. Атакующий кладёт карту, защищающийся бьёт старшей той же масти или козырем. Не может — берёт. Кто останется с картами, когда колода кончилась, — дурак.'
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
        return "\n".join(lines)

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
        if self.finished:
            return []
        rows = []
        actor = self.attacker if self.phase == 'attack' else self.defender
        hand = self.hands[actor]
        if self.phase == 'attack':
            legal = set(self._legal_attack_cards(actor))
            cardrow = []
            for k, c in enumerate(hand):
                if k in legal:
                    cardrow.append((self._card(c), self.cb(f"a{k}")))
            for j in range(0, len(cardrow), 4):
                rows.append(cardrow[j:j + 4])
            ctl = []
            if self.table and not self._unbeaten():
                ctl.append(("✅ Бито", self.cb("bito"), "success"))
            if not cardrow and not (self.table and not self._unbeaten()):
                ctl.append(("— нечем ходить —", self.cb("x")))
            if ctl:
                rows.append(ctl)
        else:
            cardrow = [(self._card(c), self.cb(f"d{k}")) for k, c in enumerate(hand)]
            for j in range(0, len(cardrow), 4):
                rows.append(cardrow[j:j + 4])
            rows.append([("🤲 Беру", self.cb("take"), "danger")])
        hint_actor = self.attacker if self.phase == 'attack' else self.defender
        if hint_actor != actor:
            pass
        return rows

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
            if tok.startswith('a'):
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
            if tok.startswith('d'):
                k = int(tok[1:])
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
