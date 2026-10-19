"""
GiftSniper — снайпер подарков Telegram: новые лимитированные подарки в магазине и дешёвые NFT-подарки на рынке.

Магазин (новые подарки):
  Модуль следит за витриной подарков. Как только появляется новый подарок — сразу уведомление от бота: цена,
  тираж, сколько осталось, только для Premium или нет. С включённой автопокупкой (.gs auto) — сам покупает
  лимитированный подарок, если он подходит под условия из настроек (цена, тираж, бюджет на сутки).
  Подарки, которые уже были в магазине на момент первого запуска, не покупаются — только новые.
  Чтобы брать не любой новый подарок, а только нужные, — настройка store_keywords (слова из названия).
  Моделей у новых подарков из магазина ещё нет: они появляются после улучшения до NFT, поэтому модели,
  фоны и узоры задаются в правилах рынка.

Рынок (перепродажа NFT-подарков):
  .gs watch                                  мастер правила кнопками: подарок → модели → фоны → узоры → цена
                                             (у каждой модели видно редкость и сколько лотов сейчас на рынке)
  .gs watch <подарок> <макс_цена> [модели]   то же текстом; модели — через запятую, по части названия:
                                             .gs watch Plush Pepe 5000 Gold, Midas
  .gs unwatch <№>                            удалить правило;   .gs rules — список правил
  .gs snipe <№>                              автопокупка по правилу вкл/выкл (по умолчанию — только уведомления)
  .gs floor <подарок>                        минимальная цена на рынке и 5 самых дешёвых лотов

Ещё:
  .gs                панель: что включено, баланс звёзд, потрачено за сутки, правила
  .gs on | off       слежка вкл/выкл (и магазин, и рынок)
  .gs auto           автопокупка новых подарков из магазина вкл/выкл
  .gs shop           что сейчас продаётся в магазине из лимитированных
  .gs log            последние события и покупки

Под каждым уведомлением — кнопка «Купить» (покупка по нажатию, цену модуль перепроверяет перед оплатой).
Все траты идут со звёзд текущего аккаунта и ограничены настройкой daily_limit (.cfg giftsniper).
Автопокупка по умолчанию выключена.
"""

import asyncio
import html
import time

import bot
import db
import decorators
import engine
import visual

NAME = "GiftSniper"
MODULE_NAME = "giftsniper"
VERSION = "1.0"
AUTHOR = "FRX"
DESCRIPTION = "Новые лимитированные подарки и дешёвые NFT-подарки на рынке: уведомления и автопокупка в рамках бюджета"
TG_URL = "https://t.me/mescr_m"

S = {"task": None, "gifts": {}, "hash": 0, "pending": {}, "low_warned": set(), "last_shop": 0.0, "last_market": 0.0}


def _esc(x):
    return html.escape(str(x), quote=False)


def _num(n):
    return f"{int(n):,}".replace(",", " ")


def _today():
    return time.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- Telegram API

def _tl():
    from telethon.tl import functions, types
    return functions, types


def gift_emoji(g):
    try:
        for a in g.sticker.attributes:
            alt = getattr(a, "alt", None)
            if alt:
                return alt
    except Exception:
        pass
    return "🎁"


def gift_title(g):
    t = getattr(g, "title", None)
    return f"{gift_emoji(g)} {t}" if t else f"{gift_emoji(g)} подарок за {g.stars}★"


async def fetch_gifts(force=False):
    """Каталог магазина: {id: StarGift}. None — не изменился (по hash)."""
    F, T = _tl()
    res = await engine.client(F.payments.GetStarGiftsRequest(hash=0 if force else S["hash"]))
    if isinstance(res, T.payments.StarGiftsNotModified):
        return None
    S["hash"] = res.hash
    gifts = {g.id: g for g in res.gifts if isinstance(g, T.StarGift)}
    S["gifts"] = gifts
    return gifts


async def find_gift(query):
    """Подарок по id или части названия -> StarGift | None."""
    gifts = S["gifts"] or (await fetch_gifts(force=True)) or S["gifts"]
    q = str(query).strip().lower()
    if q.isdigit() and int(q) in gifts:
        return gifts[int(q)]
    named = [g for g in gifts.values() if getattr(g, "title", None)]
    for g in named:
        if g.title.lower() == q:
            return g
    for g in named:
        if q in g.title.lower() or q.replace(" ", "") in g.title.lower().replace(" ", ""):
            return g
    return None


async def stars_balance():
    F, T = _tl()
    st = await engine.client(F.payments.GetStarsStatusRequest(peer=T.InputPeerSelf()))
    b = st.balance
    return int(getattr(b, "amount", 0) or 0)


def _price_stars(unique):
    """Цена лота в звёздах или None (только за TON)."""
    for a in getattr(unique, "resell_amount", None) or []:
        if a.__class__.__name__ == "StarsAmount":
            return int(a.amount)
    return None


def _attrs(unique):
    out = {}
    for a in getattr(unique, "attributes", None) or []:
        n = a.__class__.__name__
        if n == "StarGiftAttributeModel":
            out["model"] = a.name
        elif n == "StarGiftAttributeBackdrop":
            out["backdrop"] = a.name
        elif n == "StarGiftAttributePattern":
            out["pattern"] = a.name
    return out


RARITY = {"StarGiftAttributeRarityUncommon": "uncommon", "StarGiftAttributeRarityRare": "rare",
          "StarGiftAttributeRarityEpic": "epic", "StarGiftAttributeRarityLegendary": "legendary"}
KINDS = (("model", "модели", "модель"), ("backdrop", "фоны", "фон"), ("pattern", "узоры", "узор"))


def _rarity(r):
    if r is None:
        return ""
    if r.__class__.__name__ == "StarGiftAttributeRarity":
        return f"{r.permille / 10:g}%"
    return RARITY.get(r.__class__.__name__, "")


def _attr_key(a):
    """(kind, id) для атрибута или id атрибута из ответа Telegram."""
    n = a.__class__.__name__
    if n in ("StarGiftAttributeModel", "StarGiftAttributeIdModel"):
        return "model", (a.document.id if hasattr(a, "document") else a.document_id)
    if n in ("StarGiftAttributeBackdrop", "StarGiftAttributeIdBackdrop"):
        return "backdrop", a.backdrop_id
    if n in ("StarGiftAttributePattern", "StarGiftAttributeIdPattern"):
        return "pattern", (a.document.id if hasattr(a, "document") else a.document_id)
    return None, None


async def gift_attributes(gift_id):
    """Все модели / фоны / узоры подарка с редкостью и числом лотов на рынке:
    {'model': [{'id', 'name', 'rarity', 'count'}, ...], 'backdrop': [...], 'pattern': [...]}"""
    F, T = _tl()
    res = await engine.client(F.payments.GetResaleStarGiftsRequest(
        gift_id=gift_id, offset="", limit=1, attributes_hash=0))
    counts = {}
    for c in getattr(res, "counters", None) or []:
        counts[_attr_key(c.attribute)] = c.count
    out = {"model": [], "backdrop": [], "pattern": []}
    for a in getattr(res, "attributes", None) or []:
        kind, aid = _attr_key(a)
        if kind is None:
            continue
        out[kind].append({"id": aid, "name": a.name, "rarity": _rarity(getattr(a, "rarity", None)),
                          "count": counts.get((kind, aid), 0)})
    for kind in out:
        out[kind].sort(key=lambda x: (-x["count"], x["name"]))
    return out


def _attr_ids(attrs):
    _, T = _tl()
    ids = []
    for a in attrs or []:
        if a["type"] == "model":
            ids.append(T.StarGiftAttributeIdModel(document_id=a["id"]))
        elif a["type"] == "backdrop":
            ids.append(T.StarGiftAttributeIdBackdrop(backdrop_id=a["id"]))
        elif a["type"] == "pattern":
            ids.append(T.StarGiftAttributeIdPattern(document_id=a["id"]))
    return ids


def lot_matches(rule, lot):
    """Подходит ли лот под фильтры правила (проверка на нашей стороне — на случай, если Telegram фильтр не применил)."""
    for kind, _, _ in KINDS:
        names = {a["name"].lower() for a in rule.get("attrs") or [] if a["type"] == kind}
        if names and (lot.get(kind) or "").lower() not in names:
            return False
    words = [w.strip().lower() for w in (rule.get("model") or "").split(",") if w.strip()]
    if words and not any(w in (lot.get("model") or "").lower() for w in words):
        return False
    return True


def rule_filters_text(rule):
    parts = []
    for kind, _, one in KINDS:
        names = [a["name"] for a in rule.get("attrs") or [] if a["type"] == kind]
        if names:
            parts.append(f"{one}: " + ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else ""))
    if rule.get("model"):
        parts.append(f"модель ~ {rule['model']}")
    return " · ".join(parts)


async def market(gift_id, limit=40, attrs=None):
    F, T = _tl()
    res = await engine.client(F.payments.GetResaleStarGiftsRequest(
        gift_id=gift_id, offset="", limit=limit, sort_by_price=True, attributes=_attr_ids(attrs) or None))
    lots = []
    for u in res.gifts:
        if u.__class__.__name__ != "StarGiftUnique":
            continue
        price = _price_stars(u)
        if price is None:
            continue
        lots.append({"slug": u.slug, "num": u.num, "title": u.title, "price": price, **_attrs(u)})
    lots.sort(key=lambda x: x["price"])
    return lots, getattr(res, "count", len(lots))


async def _recipient():
    F, T = _tl()
    who = (await engine.get_config("recipient") or "").strip()
    if not who or who.lower() in ("me", "я", "себе"):
        return T.InputPeerSelf(), "себе"
    ent = await engine.client.get_input_entity(who)
    return ent, who


async def _pay(invoice, max_total):
    """Получает форму, проверяет сумму и платит звёздами. -> списано звёзд."""
    F, T = _tl()
    form = await engine.client(F.payments.GetPaymentFormRequest(invoice=invoice))
    total = sum(int(p.amount) for p in getattr(form.invoice, "prices", []) or [])
    if max_total is not None and total > max_total:
        raise RuntimeError(f"цена выросла: {total}★ > {max_total}★")
    await engine.client(F.payments.SendStarsFormRequest(form_id=form.form_id, invoice=invoice))
    return total


async def _budget_left():
    limit = int(await engine.get_config("daily_limit") or 0)
    spent = await db.get("spent", {})
    used = int(spent.get(_today(), 0))
    return (limit - used) if limit > 0 else 10 ** 12, used, limit


async def _spend(amount):
    spent = await db.get("spent", {})
    day = _today()
    spent = {day: int(spent.get(day, 0)) + int(amount)}       # храним только сегодня
    await db.set("spent", spent)


async def buy_store(gift, max_price=None):
    _, T = _tl()
    peer, who = await _recipient()
    hide = bool(await engine.get_config("hide_name"))
    inv = T.InputInvoiceStarGift(peer=peer, gift_id=gift.id, hide_name=hide or None)
    paid = await _pay(inv, max_price if max_price is not None else gift.stars)
    await _spend(paid)
    await _log(f"куплен {gift_title(gift)} за {paid}★ ({who})")
    return paid, who


async def buy_lot(slug, max_price):
    _, T = _tl()
    peer, who = await _recipient()
    inv = T.InputInvoiceStarGiftResale(slug=slug, to_id=peer)
    paid = await _pay(inv, max_price)
    await _spend(paid)
    await _log(f"куплен лот {slug} за {paid}★ ({who})")
    return paid, who


def _err_text(e):
    s = f"{type(e).__name__} {e}".upper()
    if "BALANCE_TOO_LOW" in s or "BALANCETOOLOW" in s:
        return "не хватает звёзд"
    if "USAGE_LIMITED" in s or "SOLD" in s:
        return "уже распродан"
    if "PREMIUM" in s:
        return "только для Telegram Premium"
    if "FLOOD" in s:
        return "Telegram просит подождать (флуд)"
    return f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- журнал и уведомления

async def _log(text):
    log = await db.get("log", [])
    log.append([int(time.time()), str(text)[:300]])
    await db.set("log", log[-60:])


async def _notify(text, buttons=None):
    try:
        await engine.bot_send_to_owner(text, buttons=buttons)
    except Exception as e:
        engine.tlog(f"[GiftSniper] уведомление не доставлено: {e}")


def _gift_card(g, head):
    lines = [visual.title(head), visual.field("подарок", _esc(gift_title(g)), code=False),
             visual.field("цена", f"{_num(g.stars)}★")]
    if g.limited:
        left = g.availability_remains
        total = g.availability_total
        lines.append(visual.field("тираж", f"{_num(total or 0)}" + (f" · осталось {_num(left)}" if left is not None else "")))
    else:
        lines.append(visual.field("тираж", "без лимита"))
    if g.require_premium:
        lines.append(visual.warn("только для Premium"))
    if g.per_user_total:
        lines.append(visual.field("на человека", g.per_user_total))
    if g.upgrade_stars:
        lines.append(visual.field("улучшение до NFT", f"{_num(g.upgrade_stars)}★"))
    if g.sold_out:
        lines.append(visual.err("распродан"))
    return lines


def _lot_link(slug):
    return f"https://t.me/nft/{slug}"


# --------------------------------------------------------------------------- проверки

async def check_shop(first=False):
    gifts = await fetch_gifts(force=first)
    if gifts is None:
        return
    seen = set(await db.get("seen", []))
    if not seen:
        # первый запуск: всё, что уже есть, — «старое», ничего не покупаем
        await db.set("seen", list(gifts))
        return
    new = [g for gid, g in gifts.items() if gid not in seen]
    if new:
        await db.set("seen", list(seen | set(gifts)))
    me = await engine.client.get_me()
    auto = bool(await db.get("auto_store", False))
    for g in sorted(new, key=lambda x: x.stars):
        lines = _gift_card(g, "Новый подарок")
        result = None
        if auto and g.limited and not g.sold_out:
            result = await _auto_store(g, me)
            if result:
                lines.append(result)
        buttons = None
        if not g.sold_out and not (result or "").startswith(visual.visual("status_success")):
            key = f"n{g.id}"
            S["pending"][key] = {"kind": "store", "gift_id": g.id, "price": g.stars}
            buttons = [[(f"Купить за {_num(g.stars)}★", f"gsb:{key}", "success")]]
        await _notify("\n".join(lines), buttons)
        await _log(f"новый подарок: {gift_title(g)} · {g.stars}★" + (" · лимит" if g.limited else ""))

    # «почти распродан» для лимитированных
    pct = int(await engine.get_config("low_stock_pct") or 0)
    if pct > 0:
        for g in gifts.values():
            if not g.limited or g.sold_out or not g.availability_total or g.id in S["low_warned"]:
                continue
            if g.availability_remains is not None and g.availability_remains * 100 <= pct * g.availability_total:
                S["low_warned"].add(g.id)
                await _notify("\n".join(_gift_card(g, "Почти распродан")))


async def _auto_store(g, me):
    max_price = int(await engine.get_config("max_price") or 0)
    max_supply = int(await engine.get_config("max_supply") or 0)
    count = int(await engine.get_config("buy_count") or 1)
    kw = [w.strip().lower() for w in (await engine.get_config("store_keywords") or []) if str(w).strip()]
    if kw and not any(w in (getattr(g, "title", None) or "").lower() for w in kw):
        return visual.hint("автопокупка: не подходит под store_keywords — пропускаю")
    if getattr(g, "auction", False):
        return visual.hint("автопокупка: подарок продаётся через аукцион — только вручную в Telegram")
    if max_price and g.stars > max_price:
        return visual.hint(f"автопокупка: дороже {max_price}★ — пропускаю")
    if max_supply and (g.availability_total or 0) > max_supply:
        return visual.hint(f"автопокупка: тираж больше {max_supply} — пропускаю")
    if g.require_premium and not getattr(me, "premium", False):
        return visual.hint("автопокупка: нужен Premium")
    if g.per_user_remains is not None:
        count = min(count, g.per_user_remains)
    bought = 0
    reason = None
    for _ in range(max(0, count)):
        left, _, _ = await _budget_left()
        if g.stars > left:
            reason = "дневной лимит исчерпан"
            break
        try:
            await buy_store(g)
            bought += 1
        except Exception as e:
            reason = _err_text(e)
            break
    if bought:
        return visual.ok(f"Куплено автоматически: {bought} шт." + (f" (дальше: {reason})" if reason else ""))
    return visual.err(f"автопокупка не удалась: {reason}") if reason else None


async def check_market():
    rules = await db.get("rules", [])
    changed = False
    for r in rules:
        try:
            lots, _ = await market(r["gift_id"], attrs=r.get("attrs"))
        except Exception as e:
            engine.tlog(f"[GiftSniper] рынок {r.get('title')}: {e}")
            continue
        seen = set(r.get("seen", []))
        for lot in lots:
            if lot["price"] > r["max"]:
                break
            if lot["slug"] in seen:
                continue
            if not lot_matches(r, lot):
                continue
            seen.add(lot["slug"])
            changed = True
            lines = [visual.title("Лот на рынке", sub=f"правило: ≤ {_num(r['max'])}★"),
                     visual.field("подарок", f"{_esc(lot['title'])} #{lot['num']}", code=False),
                     visual.field("цена", f"{_num(lot['price'])}★")]
            for k, label in (("model", "модель"), ("backdrop", "фон"), ("pattern", "узор")):
                if lot.get(k):
                    lines.append(visual.field(label, _esc(lot[k]), code=False))
            bought = False
            if r.get("auto"):
                left, _, _ = await _budget_left()
                if lot["price"] > left:
                    lines.append(visual.warn("автопокупка: дневной лимит исчерпан"))
                else:
                    try:
                        paid, who = await buy_lot(lot["slug"], r["max"])
                        lines.append(visual.ok(f"Куплено за {_num(paid)}★ ({_esc(who)})"))
                        bought = True
                    except Exception as e:
                        lines.append(visual.err("автопокупка не удалась: " + _esc(_err_text(e))))
            buttons = [[("↗ Открыть", "url:" + _lot_link(lot["slug"]))]]
            if not bought:
                key = f"l{lot['slug']}"[:56]
                S["pending"][key] = {"kind": "lot", "slug": lot["slug"], "price": lot["price"]}
                buttons[0].append((f"Купить за {_num(lot['price'])}★", f"gsb:{key}", "success"))
            await _notify("\n".join(lines), buttons)
            await _log(f"лот {lot['slug']} · {lot['price']}★" + (" · куплен" if bought else ""))
        r["seen"] = list(seen)[-500:]
    if changed:
        await db.set("rules", rules)


async def _loop():
    first = True
    while True:
        try:
            if await db.get("enabled", True):
                now = time.monotonic()
                shop_iv = int(await engine.get_config("shop_interval") or 15)
                market_iv = int(await engine.get_config("market_interval") or 30)
                if first or now - S["last_shop"] >= shop_iv:
                    S["last_shop"] = now
                    await check_shop(first=first)
                    first = False
                if now - S["last_market"] >= market_iv:
                    S["last_market"] = now
                    await check_market()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            sec = getattr(e, "seconds", None)
            engine.tlog(f"[GiftSniper] {type(e).__name__}: {e}")
            if sec:
                await asyncio.sleep(min(int(sec), 600))
        await asyncio.sleep(3)


def _start_loop():
    t = S.get("task")
    if t is None or t.done():
        S["task"] = asyncio.create_task(engine._safe_call(MODULE_NAME, _loop))


# --------------------------------------------------------------------------- жизненный цикл

@decorators.on_load()
async def _setup(module_info):
    if module_info.module_name != MODULE_NAME:
        return
    c = engine.create_config
    await c("max_price", "Автопокупка новых: максимальная цена подарка, ★ (0 — любая)", "int", 1000,
            valid={"min_value": 0, "max_value": 1_000_000})
    await c("max_supply", "Автопокупка новых: только если тираж не больше (0 — любой)", "int", 0,
            valid={"min_value": 0, "max_value": 100_000_000})
    await c("store_keywords", "Автопокупка новых: только подарки, в названии которых есть одно из слов (пусто — любые)",
            "list", [])
    await c("buy_count", "Автопокупка новых: сколько штук брать", "int", 1, valid={"min_value": 1, "max_value": 100})
    await c("daily_limit", "Бюджет на сутки, ★ — все покупки модуля (0 — без лимита)", "int", 3000,
            valid={"min_value": 0, "max_value": 10_000_000})
    await c("recipient", "Кому дарить: пусто — себе, или @username", "str", "")
    await c("hide_name", "Скрывать имя отправителя в подарке", "bool", True)
    await c("shop_interval", "Как часто проверять магазин, сек", "int", 15, valid={"min_value": 5, "max_value": 3600})
    await c("market_interval", "Как часто проверять рынок, сек", "int", 30, valid={"min_value": 10, "max_value": 3600})
    await c("low_stock_pct", "Предупредить, когда лимитированного осталось ≤ N% (0 — не надо)", "int", 0,
            valid={"min_value": 0, "max_value": 50})


@decorators.on_start()
async def _on_start():
    _start_loop()


@decorators.on_unload()
async def _on_unload():
    t = S.get("task")
    if t is not None:
        t.cancel()


# --------------------------------------------------------------------------- кнопка «Купить» под уведомлением

@bot.callback(data_startswith="gsb:", access="self")
async def _buy_click(click):
    key = click.data.split(":", 1)[1]
    p = S["pending"].get(key)
    if p is None:
        await click.answer("Кнопка устарела — модуль перезапускался", show_alert=True)
        return
    left, _, _ = await _budget_left()
    if p["price"] > left:
        await click.answer(f"Дневной лимит: осталось {left}★", show_alert=True)
        return
    try:
        if p["kind"] == "store":
            g = S["gifts"].get(p["gift_id"]) or await find_gift(p["gift_id"])
            if g is None:
                raise RuntimeError("подарка уже нет в магазине")
            paid, who = await buy_store(g, max_price=p["price"])
        else:
            paid, who = await buy_lot(p["slug"], p["price"])
    except Exception as e:
        await click.answer("Не вышло: " + _err_text(e)[:180], show_alert=True)
        return
    S["pending"].pop(key, None)
    await click.answer(f"Куплено за {paid}★ ({who})", show_alert=True)


# --------------------------------------------------------------------------- команды

async def _panel_text():
    enabled = await db.get("enabled", True)
    auto = await db.get("auto_store", False)
    rules = await db.get("rules", [])
    left, used, limit = await _budget_left()
    try:
        bal = f"{_num(await stars_balance())}★"
    except Exception:
        bal = "—"
    lines = [
        visual.title("GiftSniper", sub="подарки Telegram"),
        visual.field("слежка", "вкл" if enabled else "выкл"),
        visual.field("автопокупка новых", "вкл" if auto else "выкл"),
        visual.field("баланс", bal),
        visual.field("потрачено сегодня", f"{_num(used)}★" + (f" из {_num(limit)}★" if limit else "")),
        visual.section(f"Правила рынка ({len(rules)})"),
    ]
    if not rules:
        lines.append(visual.hint(".gs watch — мастер правила (подарок, модели, фоны, узоры, цена)"))
    for i, r in enumerate(rules, 1):
        f = rule_filters_text(r)
        m = f" · {_esc(f)}" if f else ""
        lines.append(visual.item(f"{i}. {_esc(r['title'])} ≤ {_num(r['max'])}★{m}" + (" · <b>снайп</b>" if r.get("auto") else "")))
    return "\n".join(lines)


def _panel_buttons(enabled, auto):
    auto_btn = (visual.btn("Автопокупка", "on" if auto else "off"), "gs_auto")
    if auto:
        auto_btn = auto_btn + ("danger",)          # красная — пока включена, тратит звёзды сама
    return [
        [(visual.btn("Слежка", "on" if enabled else "off"), "gs_toggle"), auto_btn],
        [(visual.btn("Проверить сейчас"), "gs_check", "primary"), (visual.btn("Закрыть", "close"), "gs_close")],
    ]


async def _panel(event):
    async def buttons():
        return _panel_buttons(await db.get("enabled", True), await db.get("auto_store", False))

    form = await engine.create_inlineform(event.chat_id, await _panel_text(), buttons=await buttons())

    @form.on_click(data="gs_close")
    async def _(click):
        await form.delete()

    @form.on_click(data="gs_toggle")
    async def _(click):
        await db.set("enabled", not await db.get("enabled", True))
        _start_loop()
        await form.edit(await _panel_text(), buttons=await buttons())

    @form.on_click(data="gs_auto")
    async def _(click):
        new = not await db.get("auto_store", False)
        await db.set("auto_store", new)
        if new:
            await click.answer("Автопокупка новых подарков включена. Условия — .cfg giftsniper", show_alert=True)
        await form.edit(await _panel_text(), buttons=await buttons())

    @form.on_click(data="gs_check")
    async def _(click):
        await click.answer("Проверяю…")
        try:
            await check_shop()
            await check_market()
        except Exception as e:
            await click.answer(_err_text(e)[:180], show_alert=True)
        await form.edit(await _panel_text(), buttons=await buttons())


async def watch_wizard(event):
    """Мастер правила рынка кнопками: подарок → модели → фоны → узоры → цена."""
    st = {"step": "gift", "gift": None, "attrs": None, "sel": {k: set() for k, _, _ in KINDS}, "q": None, "page": 0}
    gifts = S["gifts"] or (await fetch_gifts(force=True)) or S["gifts"]
    catalog = [g for g in gifts.values() if getattr(g, "title", None) and (g.upgrade_stars or g.availability_resale)]
    catalog.sort(key=lambda g: (-(g.availability_resale or 0), g.title))
    order = [k for k, _, _ in KINDS]

    def head():
        lines = [visual.title("Новое правило рынка")]
        if st["gift"]:
            lines.append(visual.field("подарок", _esc(st["gift"].title), code=False))
        for kind, many, _ in KINDS:
            chosen = [a["name"] for a in (st["attrs"] or {}).get(kind, []) if a["id"] in st["sel"][kind]]
            if chosen:
                lines.append(visual.field(many, _esc(", ".join(chosen[:6]) + (f" +{len(chosen) - 6}" if len(chosen) > 6 else "")), code=False))
            elif st["gift"] and order.index(kind) < order.index(st["step"]) if st["step"] in order else st["step"] == "price":
                lines.append(visual.field(many, "любые", code=False))
        return lines

    def search_row(hint):
        row = [engine.SearchButton("🔍 Найти", "find", hint=hint)]
        if st["q"]:
            row.append(("✕ Сбросить", "find_reset"))
        return row

    def screen():
        rows_tail = [[(visual.btn("Закрыть", "close"), "wz_close")]]
        if st["step"] == "gift":
            found = engine.search_items(catalog, st["q"], key=lambda g: [g.title]) if st["q"] else catalog
            items = [(f"{g.title}" + (f" · на рынке {_num(g.availability_resale)}" if g.availability_resale else ""),
                      f"g:{g.id}") for g in found]
            rows, st["page"], _ = engine.picker_page(items, st["page"], 7, "pg:", search=search_row("название подарка"))
            text = "\n".join(head() + ["", visual.hint("выберите подарок")])
            if not found:
                text += "\n" + visual.warn("ничего не нашлось")
            return text, rows + rows_tail
        if st["step"] == "price":
            text = "\n".join(head() + ["", visual.hint("последний шаг — максимальная цена в звёздах")])
            if st.get("floor") is not None:
                text += "\n" + visual.field("сейчас дешевле всего", f"{_num(st['floor'])}★")
            elif st.get("floor_checked"):
                text += "\n" + visual.hint("лотов с такими фильтрами сейчас нет — правило дождётся")
            return text, [[engine.InputButton(visual.btn("Ввести цену"), "price", hint="максимальная цена, ★", style="primary")],
                          [(visual.btn("Назад", "back"), "wz_back")]] + rows_tail
        kind = st["step"]
        many = dict((k, m) for k, m, _ in KINDS)[kind]
        pool = st["attrs"][kind]
        found = engine.search_items(pool, st["q"], key=lambda a: [a["name"]]) if st["q"] else pool
        items = []
        for a in found:
            mark = "◉" if a["id"] in st["sel"][kind] else "○"
            extra = " · ".join(x for x in (a["rarity"], f"{a['count']} на рынке" if a["count"] else "") if x)
            items.append((f"{mark} {a['name']}" + (f" · {extra}" if extra else ""), f"t:{pool.index(a)}"))
        rows, st["page"], _ = engine.picker_page(items, st["page"], 7, "pg:", search=search_row(f"{many}: название"))
        n = len(st["sel"][kind])
        nav = [(visual.btn("Назад", "back"), "wz_back")]
        if n:
            nav.append((f"✕ Снять ({n})", "wz_clear"))
        nav.append(((f"Дальше: выбрано {n}" if n else "Дальше: любые") + " ▸", "wz_next", "primary"))
        text = "\n".join(head() + ["", visual.hint(f"{many}: отметьте нужные (можно несколько) или пропустите — тогда любые")])
        return text, rows + [nav] + rows_tail

    form = await engine.create_inlineform(event.chat_id, *screen(), timeout=900)

    async def redraw():
        await form.edit(*screen())

    async def go(step):
        st.update(step=step, q=None, page=0)
        if step == "price":
            st["floor"], st["floor_checked"] = None, False
            try:
                lots, _ = await market(st["gift"].id, limit=5, attrs=_wz_attrs())
                lots = [l for l in lots if lot_matches({"attrs": _wz_attrs()}, l)]
                st["floor"] = lots[0]["price"] if lots else None
            except Exception:
                pass
            st["floor_checked"] = True
        await redraw()

    def _wz_attrs():
        out = []
        for kind, _, _ in KINDS:
            for a in (st["attrs"] or {}).get(kind, []):
                if a["id"] in st["sel"][kind]:
                    out.append({"type": kind, "id": a["id"], "name": a["name"]})
        return out

    @form.on_click(data="wz_close")
    async def _(click):
        await form.delete()

    @form.on_click(data_startswith="pg:")
    async def _(click):
        st["page"] = int(click.data.split(":", 1)[1])
        await redraw()

    @form.on_search(data="find")
    async def _(inp):
        st["q"] = inp.value.strip() or None
        st["page"] = 0
        await redraw()

    @form.on_click(data="find_reset")
    async def _(click):
        st["q"], st["page"] = None, 0
        await redraw()

    @form.on_click(data_startswith="g:")
    async def _(click):
        gid = int(click.data.split(":", 1)[1])
        st["gift"] = S["gifts"].get(gid) or next((g for g in catalog if g.id == gid), None)
        await click.answer("Загружаю модели…")
        try:
            st["attrs"] = await gift_attributes(gid)
        except Exception as e:
            await click.answer("Не получил атрибуты: " + _err_text(e)[:150], show_alert=True)
            return
        st["sel"] = {k: set() for k, _, _ in KINDS}
        # пропускаем пустые шаги (у подарка может не быть, например, узоров)
        nxt = next((k for k in order if st["attrs"][k]), "price")
        await go(nxt)

    @form.on_click(data_startswith="t:")
    async def _(click):
        kind = st["step"]
        a = st["attrs"][kind][int(click.data.split(":", 1)[1])]
        sel = st["sel"][kind]
        sel.symmetric_difference_update({a["id"]})
        await redraw()

    @form.on_click(data="wz_clear")
    async def _(click):
        st["sel"][st["step"]].clear()
        await redraw()

    @form.on_click(data="wz_next")
    async def _(click):
        i = order.index(st["step"])
        nxt = next((k for k in order[i + 1:] if st["attrs"][k]), "price")
        await go(nxt)

    @form.on_click(data="wz_back")
    async def _(click):
        if st["step"] == "price":
            prev = next((k for k in reversed(order) if st["attrs"][k]), None)
        else:
            i = order.index(st["step"])
            prev = next((k for k in reversed(order[:i]) if st["attrs"][k]), None)
        if prev is None:
            st.update(gift=None, attrs=None)
            prev = "gift"
        await go(prev)

    @form.on_input(data="price")
    async def _(inp):
        v = inp.value.strip().replace(" ", "").replace("★", "")
        if not v.isdigit() or int(v) <= 0:
            await form.edit("\n".join(head() + ["", visual.err("Нужно целое число звёзд, например 5000")]),
                            buttons=screen()[1])
            return
        rule = {"gift_id": st["gift"].id, "title": st["gift"].title, "max": int(v), "model": None,
                "attrs": _wz_attrs(), "auto": False, "seen": []}
        rules = await db.get("rules", [])
        rules.append(rule)
        await db.set("rules", rules)
        S["last_market"] = 0
        n = len(rules)
        f = rule_filters_text(rule)
        await form.edit("\n".join([
            visual.ok("Правило сохранено"),
            visual.field("подарок", _esc(rule["title"]), code=False),
            visual.field("до", f"{_num(rule['max'])}★"),
            visual.field("фильтры", _esc(f) if f else "любые", code=False),
            visual.hint(f"сейчас — только уведомления; покупать само — .gs snipe {n}"),
        ]), buttons=[[(f"Включить снайп №{n}", f"wz_snipe:{n}", "danger")], [(visual.btn("Готово", "close"), "wz_close")]])

    @form.on_click(data_startswith="wz_snipe:")
    async def _(click):
        n = int(click.data.split(":", 1)[1])
        rules = await db.get("rules", [])
        if 0 < n <= len(rules):
            rules[n - 1]["auto"] = True
            await db.set("rules", rules)
            await click.answer(f"Снайп по правилу №{n} включён — покупка сама, в рамках daily_limit", show_alert=True)
        await form.edit(form.text, buttons=[[(visual.btn("Готово", "close"), "wz_close")]])


@decorators.command("gs", "gift", "giftsniper")
async def gs_cmd(event):
    """снайпер подарков: [on|off|auto|shop|watch|unwatch|snipe|rules|floor|log]"""
    args = engine.get_args().split()
    sub = args[0].lower() if args else ""

    if not sub:
        await _panel(event)
        try:
            await event.delete()
        except Exception:
            pass
        return

    if sub in ("on", "off"):
        await db.set("enabled", sub == "on")
        _start_loop()
        await engine.answer(event, visual.ok(f"Слежка {'включена' if sub == 'on' else 'выключена'}"))
        return

    if sub == "auto":
        new = not await db.get("auto_store", False)
        await db.set("auto_store", new)
        mp = await engine.get_config("max_price")
        lim = await engine.get_config("daily_limit")
        await engine.answer(event, visual.ok(f"Автопокупка новых подарков {'включена' if new else 'выключена'}")
                            + (f"\n{visual.hint(f'до {mp}★ за штуку, не больше {lim}★ в сутки — .cfg giftsniper')}" if new else ""))
        return

    if sub == "shop":
        await engine.answer(event, visual.wait("Смотрю магазин…"))
        gifts = await fetch_gifts(force=True) or S["gifts"]
        lim = [g for g in gifts.values() if g.limited and not g.sold_out]
        lim.sort(key=lambda g: g.stars)
        lines = [visual.title("Магазин", sub=f"лимитированных в продаже: {len(lim)}")]
        for g in lim[:30]:
            left = f" · осталось {_num(g.availability_remains)}/{_num(g.availability_total)}" if g.availability_total else ""
            lines.append(visual.item(f"{_esc(gift_title(g))} — <b>{_num(g.stars)}★</b>{left} · <code>{g.id}</code>"))
        if not lim:
            lines.append(visual.hint("сейчас ничего лимитированного не продаётся"))
        await engine.answer(event, "\n".join(lines))
        return

    if sub in ("watch", "new") and len(args) == 1:
        await watch_wizard(event)
        try:
            await event.delete()
        except Exception:
            pass
        return

    if sub == "watch":
        if len(args) < 3:
            await engine.answer(event, visual.err("Формат: .gs watch <подарок> <макс_цена> [модели через запятую] — или просто .gs watch"))
            return
        # цена — первое число после названия (название может быть из нескольких слов)
        idx = next((i for i in range(2, len(args)) if args[i].isdigit()), None)
        if idx is None:
            await engine.answer(event, visual.err("Не вижу цену: .gs watch <подарок> <макс_цена> [модель]"))
            return
        name = " ".join(args[1:idx])
        g = await find_gift(name)
        if g is None:
            await engine.answer(event, visual.err(f"Подарок «{_esc(name)}» не найден — посмотрите названия в .gs shop или укажите id"))
            return
        rule = {"gift_id": g.id, "title": getattr(g, "title", None) or gift_title(g), "max": int(args[idx]),
                "model": " ".join(args[idx + 1:]) or None, "attrs": [], "auto": False, "seen": []}
        rules = await db.get("rules", [])
        rules.append(rule)
        await db.set("rules", rules)
        S["last_market"] = 0
        await engine.answer(event, visual.ok(f"Слежу за рынком: {_esc(rule['title'])} ≤ {_num(rule['max'])}★")
                            + (f"\n{visual.field('модели', _esc(rule['model']), code=False)}" if rule["model"] else "")
                            + f"\n{visual.hint(f'автопокупка по правилу — .gs snipe {len(rules)}')}")
        return

    if sub in ("unwatch", "snipe"):
        rules = await db.get("rules", [])
        try:
            i = int(args[1]) - 1
            r = rules[i]
        except (IndexError, ValueError):
            await engine.answer(event, visual.err("Укажите номер правила из .gs rules"))
            return
        if sub == "unwatch":
            rules.pop(i)
            text = visual.ok(f"Правило удалено: {_esc(r['title'])}")
        else:
            r["auto"] = not r.get("auto")
            text = (visual.ok(f"Снайп включён: {_esc(r['title'])} ≤ {_num(r['max'])}★ — буду покупать сам")
                    if r["auto"] else visual.ok(f"Снайп выключен: {_esc(r['title'])} — только уведомления"))
        await db.set("rules", rules)
        await engine.answer(event, text)
        return

    if sub == "rules":
        await engine.answer(event, await _panel_text())
        return

    if sub == "floor":
        name = " ".join(args[1:])
        g = await find_gift(name) if name else None
        if g is None:
            await engine.answer(event, visual.err("Формат: .gs floor <подарок> (название или id)"))
            return
        await engine.answer(event, visual.wait("Смотрю рынок…"))
        lots, count = await market(g.id, limit=20)
        lines = [visual.title("Рынок", sub=_esc(getattr(g, "title", None) or gift_title(g))),
                 visual.field("лотов за звёзды", _num(len(lots))) if lots else visual.hint("лотов за звёзды нет")]
        if lots:
            lines.insert(1, visual.field("минимальная цена", f"{_num(lots[0]['price'])}★"))
        for lot in lots[:5]:
            extra = f" · {_esc(lot['model'])}" if lot.get("model") else ""
            lines.append(visual.item(f"<a href=\"{_lot_link(lot['slug'])}\">#{lot['num']}</a> — <b>{_num(lot['price'])}★</b>{extra}"))
        await engine.answer(event, "\n".join(lines))
        return

    if sub == "log":
        log = await db.get("log", [])
        lines = [visual.title("GiftSniper", sub="журнал")]
        for ts, text in log[-20:][::-1]:
            lines.append(visual.item(f"<code>{time.strftime('%d.%m %H:%M', time.localtime(ts))}</code> {_esc(text)}"))
        if not log:
            lines.append(visual.hint("пока пусто"))
        await engine.answer(event, "\n".join(lines))
        return

    await engine.answer(event, visual.err("Не знаю такую команду — см. .help giftsniper"))
