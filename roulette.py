"""
Roulette — розыгрыши в чате: люди жмут «Участвую», набирается нужное число — победители определяются сами.

  .roulette кубик 5 2          5 участников, 2 победителя, кидаем 🎲 за каждого
  .roulette дартс 10           10 участников, 1 победитель, 🎯
  .roulette рандом 20 3 10м    20 участников, 3 победителя, честный рандом, сбор не дольше 10 минут
  .roulette слоты 6 1 | Пицца  после «|» — что разыгрываем

Режимы: кубик 🎲 · дартс 🎯 · слоты 🎰 · баскет 🏀 · футбол ⚽ · боулинг 🎳 — бросок делает сервер Telegram,
его не подкрутить; рандом — по хешу: хеш секрета публикуется ДО розыгрыша, сам секрет — после, и любой может
проверить, что результат не подогнан. Ничья на границе призовых мест — переброс между теми, кто сравнялся.
Каждый участвует один раз, боты не участвуют. Начать раньше или отменить может только тот, кто запустил.
"""

import asyncio
import hashlib
import html
import re
import secrets

import decorators
import engine
import visual

NAME = "Roulette"
MODULE_NAME = "roulette"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Розыгрыши в чате: кубик, дартс, слоты, баскет, футбол, боулинг или проверяемый рандом"

MODES = {
    'dice': ('🎲', 'кубик', ('кубик', 'кубики', 'dice', 'кости', 'куб')),
    'darts': ('🎯', 'дартс', ('дартс', 'darts', 'dart')),
    'slots': ('🎰', 'слоты', ('слоты', 'слот', 'slots', 'slot', 'казино', 'casino')),
    'basket': ('🏀', 'баскет', ('баскет', 'баскетбол', 'basket', 'basketball')),
    'football': ('⚽', 'футбол', ('футбол', 'football', 'soccer', 'мяч')),
    'bowling': ('🎳', 'боулинг', ('боулинг', 'bowling')),
    'random': ('🔀', 'рандом', ('рандом', 'random', 'rand', 'случайно', 'хеш', 'hash')),
}
GAMES = {}


def _esc(x):
    return html.escape(str(x), quote=False)


def _mention(p):
    return f'<a href="tg://user?id={p["id"]}">{_esc(p["name"])}</a>'


def _who(u):
    name = ' '.join(x for x in (getattr(u, 'first_name', '') or '', getattr(u, 'last_name', '') or '') if x)
    return name or (('@' + u.username) if getattr(u, 'username', None) else str(u.id))


def parse(args):
    """'кубик 5 2 10м | Приз' → (mode, players, winners, timeout_sec, prize) или текст ошибки."""
    prize, extra = '', []
    if '|' in args:
        args, prize = args.split('|', 1)
    mode, nums, timeout = None, [], None
    for raw in args.split():
        tok = raw.lower()
        m = re.fullmatch(r'(\d+)\s*(с|сек|s|sec|м|мин|m|min|ч|h)', tok)
        if m:
            k = {'с': 1, 'сек': 1, 's': 1, 'sec': 1, 'м': 60, 'мин': 60, 'm': 60, 'min': 60, 'ч': 3600, 'h': 3600}[m.group(2)]
            timeout = int(m.group(1)) * k
            continue
        if tok.isdigit():
            nums.append(int(tok))
            continue
        for key, (_, _, words) in MODES.items():
            if tok in words:
                mode = key
                break
        else:
            extra.append(raw)
    if mode is None:
        return "какой режим? кубик, дартс, слоты, баскет, футбол, боулинг или рандом"
    if not nums:
        return "сколько участников? например: .roulette кубик 5"
    players = nums[0]
    winners = nums[1] if len(nums) > 1 else 1
    if not 2 <= players <= 100:
        return "участников — от 2 до 100"
    if not 1 <= winners < players:
        return "победителей должно быть меньше, чем участников"
    if timeout is not None and not 10 <= timeout <= 7 * 86400:
        return "время сбора — от 10 секунд до недели"
    return mode, players, winners, timeout, (prize.strip() or ' '.join(extra))[:120]


def slot_score(v):
    """🎰 1…64 → очки: три семёрки > три одинаковых > пара > остальное."""
    reels = [((v - 1) >> s) & 3 for s in (0, 2, 4)]                # 0 BAR, 1 виноград, 2 лимон, 3 семёрка
    if reels[0] == reels[1] == reels[2]:
        return 1000 + reels[0] * 100
    if len(set(reels)) == 2:
        pair = max(set(reels), key=reels.count)
        return 500 + pair * 50 + sum(reels)
    return sum(reels)


def slot_text(v):
    sym = ['BAR', '🍇', '🍋', '7️⃣']
    return ' '.join(sym[((v - 1) >> s) & 3] for s in (0, 2, 4))


class Game:
    def __init__(self, owner, chat_id, mode, players, winners, timeout, prize):
        self.owner, self.chat_id, self.mode = owner, chat_id, mode
        self.need, self.winners, self.timeout, self.prize = players, winners, timeout, prize
        self.people = []                        # [{'id', 'name'}]
        self.state = 'open'                     # open → rolling → done / cancelled
        self.form = None
        self.secret = secrets.token_hex(16)
        self.commit = hashlib.sha256(self.secret.encode()).hexdigest()
        self.timer = None
        self.lock = asyncio.Lock()
        self.log = []                           # строки хода розыгрыша

    def head(self):
        icon, label, _ = MODES[self.mode]
        t = [visual.title(f"{icon} Розыгрыш", sub=label)]
        if self.prize:
            t.append(f"🎁 <b>{_esc(self.prize)}</b>")
        t.append(visual.field("победителей", f"{self.winners} из {self.need}", code=False))
        return t

    def text(self):
        t = self.head()
        if self.state == 'open':
            t.append(visual.field("участвуют", f"{len(self.people)}/{self.need}", code=False))
            if self.people:
                t.append("")
                t += [visual.item(f"{i + 1}. {_mention(p)}") for i, p in enumerate(self.people)]
            t.append("")
            if self.mode == 'random':
                t.append(visual.hint(f"хеш секрета: <code>{self.commit[:32]}</code>…"))
            if self.timeout:
                m = self.timeout // 60
                t.append(visual.hint(f"сбор до {self.timeout} с" if self.timeout < 120 else f"сбор не дольше {m} мин"))
            t.append(visual.hint("жмите «Участвую» — начнётся само, как наберётся"))
        else:
            t.append("")
            t += self.log
        return "\n".join(t)

    def buttons(self):
        if self.state != 'open':
            return []
        return [[("🙋 Участвую", "rl:in", 'success'), ("Передумал", "rl:out")],
                [("▶ Начать", "rl:go", 'primary'), ("✖ Отменить", "rl:cancel", 'danger')]]

    async def redraw(self):
        try:
            await self.form.edit(self.text(), buttons=self.buttons())
        except Exception as e:
            engine.tlog(f"[roulette] edit: {e}")

    # ------------------------------------------------------------------ розыгрыш

    async def throw(self):
        """Бросок от сервера Telegram → (значение, подпись)."""
        from telethon.tl.types import InputMediaDice
        emo = MODES[self.mode][0]
        msg = await engine.client.send_message(self.chat_id, file=InputMediaDice(emo))
        v = msg.media.value
        if self.mode == 'slots':
            return slot_score(v), f"{emo} {slot_text(v)}"
        return v, f"{emo} {v}"

    async def play(self):
        try:
            await self._play()
        except Exception as e:
            engine.tlog(f"[roulette] {e!r}")
            await self.cancel(f"Розыгрыш сломался: {_esc(str(e)[:200])}")

    async def _play(self):
        async with self.lock:
            if self.state != 'open':
                return
            self.state = 'rolling'
        if self.timer:
            self.timer.cancel()
        pool = list(self.people)
        if self.mode == 'random':
            await self.play_random(pool)
            return
        places, rnd = [], 1
        self.log = [visual.wait("Бросаем…")]
        await self.redraw()
        while len(places) < self.winners:
            scores = []
            if rnd > 1:
                self.log.append("")
                self.log.append(visual.section(f"Переброс #{rnd - 1}: " + ", ".join(_esc(p['name']) for p in pool)))
            for p in pool:
                v, label = await self.throw()
                scores.append((v, p))
                self.log.append(visual.item(f"{_mention(p)} — {label}"))
                await self.redraw()
                await asyncio.sleep(1.3)
            scores.sort(key=lambda x: -x[0])
            left = self.winners - len(places)
            cut = scores[left - 1][0]
            sure = [p for v, p in scores if v > cut]
            tied = [p for v, p in scores if v == cut]
            places += sure
            if len(sure) + len(tied) <= left:            # все сравнявшиеся влезают в призовые
                places += tied
                break
            pool, rnd = tied, rnd + 1
            await asyncio.sleep(2)
        await asyncio.sleep(3.5)                           # пусть последняя анимация доиграет
        await self.finish(places[:self.winners])

    async def play_random(self, pool):
        def key(p):
            return hashlib.sha256(f"{self.secret}:{p['id']}".encode()).hexdigest()
        order = sorted(pool, key=key)
        ids = ','.join(str(p['id']) for p in pool)
        self.log = [visual.item(f"{i + 1}. {_mention(p)} — <code>{key(p)[:10]}</code>") for i, p in enumerate(order)]
        self.log += ["", visual.hint(f"секрет: <code>{self.secret}</code>"),
                     visual.hint("проверка: sha256(секрет) = хеш до розыгрыша; место = порядок sha256(«секрет:id»)"),
                     visual.hint(f"id участников: {ids}")]
        await self.finish(order[:self.winners])

    async def finish(self, won):
        self.state = 'done'
        self.log.append("")
        self.log.append(visual.ok("Победители:" if len(won) > 1 else "Победитель:"))
        self.log += [f"🏆 {_mention(p)}" for p in won]
        await self.redraw()
        GAMES.pop(id(self), None)
        names = ", ".join(_mention(p) for p in won)
        prize = f" — {_esc(self.prize)}" if self.prize else ""
        await engine.client.send_message(self.chat_id, f"🎉 {names}{prize}!", parse_mode='html')

    async def cancel(self, why="Розыгрыш отменён"):
        self.state = 'cancelled'
        if self.timer:
            self.timer.cancel()
        self.log = [visual.err(why)]
        await self.redraw()
        GAMES.pop(id(self), None)

    async def wait_timeout(self):
        try:
            await asyncio.sleep(self.timeout)
        except asyncio.CancelledError:
            return
        if self.state != 'open':
            return
        if len(self.people) > self.winners:
            asyncio.ensure_future(self.play())
        else:
            await self.cancel(f"Время вышло — набралось {len(self.people)}, а нужно больше {self.winners}")


@decorators.on_unload()
async def cleanup():
    for g in list(GAMES.values()):
        if g.timer:
            g.timer.cancel()


@decorators.command('roulette', 'рулетка', 'розыгрыш', 'giveaway')
async def roulette_cmd(event):
    """розыгрыш: .roulette кубик 5 2 — режим, участников, победителей; [10м] — время сбора; | приз"""
    args = engine.get_args().strip()
    got = parse(args) if args else "что разыгрываем?"
    if isinstance(got, str):
        modes = " · ".join(f"{i} {l}" for i, l, _ in MODES.values())
        await engine.answer(event, visual.err(got.capitalize()) + "\n" + visual.hint(
            ".roulette кубик 5 2 · .roulette рандом 20 3 10м | Приз") + "\n" + visual.hint(modes))
        return
    mode, players, winners, timeout, prize = got
    owner = getattr(engine, '_owner_id', None) or (await engine.client.get_me()).id
    g = Game(owner, event.chat_id, mode, players, winners, timeout, prize)
    try:
        g.form = await engine.create_inlineform(event.chat_id, g.text(), buttons=g.buttons(), for_user=['me', event.chat_id])
    except Exception as e:
        await engine.answer(event, visual.err("Форма не создалась") + f"\n<code>{_esc(str(e)[:300])}</code>")
        return
    GAMES[id(g)] = g
    await event.delete()
    if timeout:
        g.timer = asyncio.ensure_future(g.wait_timeout())

    @g.form.on_click(data_startswith='rl:')
    async def _(click):
        u = click.from_user
        op = click.data[3:]
        if u is None:
            return
        if g.state != 'open':
            await click.answer("Розыгрыш уже идёт или закончен")
            return
        if op == 'in':
            if getattr(u, 'bot', False):
                await click.answer("Боты не участвуют")
                return
            if any(p['id'] == u.id for p in g.people):
                await click.answer("Вы уже участвуете")
                return
            if len(g.people) >= g.need:
                await click.answer("Мест уже нет")
                return
            g.people.append({'id': u.id, 'name': _who(u)[:40]})
            await click.answer(f"Вы в игре! {len(g.people)}/{g.need}")
            if len(g.people) >= g.need:
                asyncio.ensure_future(g.play())
                return
            await g.redraw()
        elif op == 'out':
            before = len(g.people)
            g.people = [p for p in g.people if p['id'] != u.id]
            await click.answer("Вы вышли" if len(g.people) < before else "Вас и не было в списке")
            if len(g.people) < before:
                await g.redraw()
        elif op in ('go', 'cancel'):
            if u.id != g.owner:
                await click.answer("Это может только тот, кто запустил розыгрыш", show_alert=True)
                return
            if op == 'cancel':
                await click.answer("Отменено")
                await g.cancel()
                return
            if len(g.people) <= g.winners:
                await click.answer(f"Нужно хотя бы {g.winners + 1} участника", show_alert=True)
                return
            await click.answer("Начинаем!")
            asyncio.ensure_future(g.play())
