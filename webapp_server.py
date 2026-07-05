#!/usr/bin/env python3
"""
webapp_server.py  –  Blackjack Mini App server
Runs independently alongside bj.py, shares the same PostgreSQL DB.

Install:
  pip install aiohttp asyncpg --break-system-packages

Environment variables (same as bot where applicable):
  BOT_TOKEN   – Telegram bot token (for initData validation) — REQUIRED, no fallback
  DB_DSN      – postgresql://localhost/bjbot
  PORT        – 8080 (default)

Run:
  python webapp_server.py
"""
import asyncio, json, hashlib, hmac, time, random, uuid, os, logging
from urllib.parse import parse_qsl
import aiohttp
from aiohttp import web
import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("webapp")

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]  # must be set in Railway Variables — no hardcoded fallback
DB_DSN      = os.getenv("DB_DSN",      "postgresql://localhost/bjbot")
PORT        = int(os.getenv("PORT", "8080"))

MAX_PLAYERS      = 6
LOBBY_WAIT       = 60     # seconds to wait for players before auto-start (starts with whoever is there)
READY_MIN        = 3      # minimum players required to vote to skip the wait
TURN_TIME        = 30     # seconds per player turn
VIP_SWAP_TIME    = 10     # VIP swap window after bust
INSURANCE_TIME   = 15     # seconds to decide on insurance
DEALER_DELAY     = 2.0    # pause between dealer actions
N_DECKS          = 2
MIN_BET          = 100
MAX_BET          = 1_000_000
COMMENT_COOLDOWN = 1.5    # seconds between quick comments, per player

# ── COSMETICS CATALOG (bot stars 💫 only — no real-money purchase) ────────────
# All rendering is done client-side in pure CSS/SVG — no external art assets,
# so nothing here can infringe on anyone else's IP and it stays lightweight.
CARD_SKINS = {
    "classic": {"name": "Classic",      "price": 0},
    "neon":    {"name": "Neon Nights",  "price": 150},
    "royal":   {"name": "Royal Velvet", "price": 200},
    "gold":    {"name": "Gold Foil",    "price": 250},
    "holo":    {"name": "Holo Shift",   "price": 300},
}
PROFILE_FRAMES = {
    "none":    {"name": "None",          "price": 0},
    "bronze":  {"name": "Bronze Ring",   "price": 80},
    "silver":  {"name": "Silver Shine",  "price": 150},
    "gold":    {"name": "Gold Sweep",    "price": 250},
    "diamond": {"name": "Diamond Aura",  "price": 400},
}

# ── CARD HELPERS ──────────────────────────────────────────────────────────────
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

def make_deck():
    deck = [{"r": r, "s": s}
            for _ in range(N_DECKS) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

def cval(c):
    r = c["r"]
    if r in ("J", "Q", "K"): return 10
    if r == "A":              return 11
    return int(r)

def htot(hand):
    total = sum(cval(c) for c in hand)
    aces  = sum(1 for c in hand if c["r"] == "A")
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total

def is_soft(hand):
    total = sum(cval(c) for c in hand)
    aces  = sum(1 for c in hand if c["r"] == "A")
    while total > 21 and aces:
        total -= 10; aces -= 1
    return aces > 0

def is_bj(hand):
    return len(hand) == 2 and htot(hand) == 21

def dealer_should_hit(hand):
    """Standard casino rule, matches bj.py: hit on 16 or less, and on soft 17. No artificial weakness."""
    total = htot(hand)
    if total < 17: return True
    if total == 17 and is_soft(hand): return True
    return False

# ── GLOBAL STATE ──────────────────────────────────────────────────────────────
pool          = None
rooms         = {}   # rid → room dict
sse_map       = {}   # uid → asyncio.Queue  (заменяет ws_map)
in_room       = {}   # uid → rid (prevents double-joining)
_last_comment = {}   # uid → timestamp, simple per-connection rate limit

# ── DB HELPERS ────────────────────────────────────────────────────────────────
async def db_user(uid):
    return await pool.fetchrow("SELECT * FROM users WHERE uid=$1", uid)

async def db_is_vip(uid):
    u = await db_user(uid)
    if not u: return False
    return bool(u["vip_perm"]) or (
        u["vip_until"] is not None and u["vip_until"] > int(time.time()))

async def db_bal(uid):
    row = await pool.fetchrow("SELECT bal FROM users WHERE uid=$1", uid)
    return row["bal"] if row else 0

async def db_stars(uid):
    row = await pool.fetchrow("SELECT bot_stars FROM users WHERE uid=$1", uid)
    return row["bot_stars"] if row else 0

async def db_add(uid, delta):
    """Add delta to chip balance (GREATEST matches bot's add_bal behaviour)."""
    await pool.execute(
        "UPDATE users SET bal=GREATEST(0, bal+$1) WHERE uid=$2", delta, uid)

async def db_add_stars(uid, delta):
    await pool.execute(
        "UPDATE users SET bot_stars=GREATEST(0, bot_stars+$1) WHERE uid=$2", delta, uid)

async def db_stats(uid, result):
    if result in ("win", "blackjack"):
        await pool.execute(
            "UPDATE users SET w_bj=w_bj+1, g_bj=g_bj+1 WHERE uid=$1", uid)
    elif result in ("lose", "bust"):
        await pool.execute(
            "UPDATE users SET l_bj=l_bj+1, g_bj=g_bj+1 WHERE uid=$1", uid)
    else:
        await pool.execute("UPDATE users SET g_bj=g_bj+1 WHERE uid=$1", uid)

async def db_owned(uid):
    rows = await pool.fetch("SELECT item_code FROM cosmetics_owned WHERE uid=$1", uid)
    return {r["item_code"] for r in rows}

async def db_grant(uid, code):
    await pool.execute(
        "INSERT INTO cosmetics_owned(uid,item_code,acquired) VALUES($1,$2,$3) "
        "ON CONFLICT DO NOTHING", uid, code, int(time.time()))

async def db_equip(uid, kind, code):
    col = "equipped_skin" if kind == "skin" else "equipped_frame"
    await pool.execute(f"UPDATE users SET {col}=$1 WHERE uid=$2", code, uid)

async def db_equipped(uid):
    u = await db_user(uid)
    if not u: return "classic", "none"
    return u["equipped_skin"] or "classic", u["equipped_frame"] or "none"

async def shop_payload(uid):
    owned = await db_owned(uid)
    skin, frame = await db_equipped(uid)
    return {
        "type": "shop_data",
        "stars": await db_stars(uid),
        "skins": [{"code": c, **v, "owned": (c == "classic" or c in owned)} for c, v in CARD_SKINS.items()],
        "frames": [{"code": c, **v, "owned": (c == "none" or c in owned)} for c, v in PROFILE_FRAMES.items()],
        "equipped_skin": skin,
        "equipped_frame": frame,
    }

# ── initData VALIDATION ───────────────────────────────────────────────────────
def validate_init_data(raw):
    """Validate Telegram WebApp initData HMAC. Returns user dict or None."""
    try:
        pairs  = dict(parse_qsl(raw, keep_blank_values=True))
        h      = pairs.pop("hash", None)
        if not h: return None
        check  = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(BOT_TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()
        got    = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(got, h): return None
        return json.loads(pairs.get("user", "{}"))
    except Exception as e:
        log.warning(f"initData error: {e}")
        return None

# ── ROOM HELPERS ──────────────────────────────────────────────────────────────
def new_room():
    return {
        "id":          uuid.uuid4().hex[:8],
        "state":       "lobby",   # lobby | insurance | playing | dealer | done
        "players":     [],
        "dealer":      [],
        "deck":        make_deck(),
        "cur":         0,
        "lobby_task":  None,
        "turn_task":   None,
        "ready_votes": set(),
        "ins_resp":    {},
    }

def open_room():
    """Return an existing open lobby room or create a new one."""
    for r in rooms.values():
        if r["state"] == "lobby" and len(r["players"]) < MAX_PLAYERS:
            return r
    r = new_room()
    rooms[r["id"]] = r
    return r

def ctask(room, key):
    """Cancel and clear asyncio task stored in room[key]."""
    t = room.get(key)
    if t and not t.done(): t.cancel()
    room[key] = None

# ── BROADCAST (SSE через asyncio.Queue) ───────────────────────────────────────
async def send_uid(uid, msg):
    q = sse_map.get(uid)
    if q:
        try: q.put_nowait(msg)
        except asyncio.QueueFull: pass

async def bcast(room, msg):
    for p in list(room["players"]):
        await send_uid(p["uid"], msg)

# ── STATE MESSAGE ─────────────────────────────────────────────────────────────
def state_msg(room, reveal=False):
    """Build the full 'state' packet to broadcast."""
    show_dealer = reveal or room["state"] in ("dealer", "done")
    dlr         = room["dealer"]
    if show_dealer or not dlr:
        dlr_out   = dlr
        dlr_total = htot(dlr) if dlr else None
    else:
        dlr_out   = [dlr[0], {"r": "?", "s": "?"}] if len(dlr) >= 2 else dlr
        dlr_total = None

    ps = []
    for p in room["players"]:
        ps.append({
            "uid":     p["uid"],
            "name":    p["name"],
            "photo":   p.get("photo", ""),
            "skin":    p.get("skin", "classic"),
            "frame":   p.get("frame", "none"),
            "bet":     p["bet"],
            "hand":    p["hand"],
            "total":   htot(p["hand"]) if p["hand"] else 0,
            "done":    p.get("done", False),
            "doubled": p.get("doubled", False),
            "insured": p.get("insured", False),
            "result":  p.get("result"),
            "win":     p.get("win", 0),
        })

    return {
        "type":         "state",
        "room_id":      room["id"],
        "state":        room["state"],
        "players":      ps,
        "dealer":       dlr_out,
        "dealer_total": dlr_total,
        "cur":          room["cur"],
        "ready_count":  len(room["ready_votes"]),
        "ready_needed": max(READY_MIN, 1),
    }

# ── LOBBY ─────────────────────────────────────────────────────────────────────
async def lobby_loop(room):
    try:
        for secs in range(LOBBY_WAIT, 0, -1):
            if room["state"] != "lobby": return
            await bcast(room, {
                "type":  "tick",
                "secs":  secs,
                "count": len(room["players"]),
                "max":   MAX_PLAYERS,
            })
            await asyncio.sleep(1)
        # Timer ran out — start with however many players are seated (even just 1)
        await game_start(room)
    except asyncio.CancelledError:
        pass

async def maybe_skip_lobby(room):
    """If there are enough players and everyone seated has voted ready, start now."""
    if room["state"] != "lobby": return
    n = len(room["players"])
    if n >= READY_MIN and len(room["ready_votes"]) >= n:
        ctask(room, "lobby_task")
        await game_start(room)

# ── GAME FLOW ─────────────────────────────────────────────────────────────────
async def game_start(room):
    if room["state"] != "lobby" or not room["players"]: return
    room["state"] = "playing"
    room["cur"]   = 0
    dk = room["deck"]
    for p in room["players"]:
        p.update(hand=[dk.pop(), dk.pop()],
                 done=False, doubled=False, insured=False, result=None, win=0)
    room["dealer"] = [dk.pop(), dk.pop()]
    await bcast(room, {"type": "game_start"})
    if room["dealer"][0]["r"] == "A":
        await offer_insurance(room)
    else:
        await bcast(room, state_msg(room))
        await next_turn(room)

# ── INSURANCE ─────────────────────────────────────────────────────────────────
async def offer_insurance(room):
    room["state"] = "insurance"
    room["ins_resp"] = {}
    await bcast(room, {"type": "insurance_offer", "secs": INSURANCE_TIME})
    await bcast(room, state_msg(room))
    ctask(room, "turn_task")
    room["turn_task"] = asyncio.create_task(insurance_timeout(room))

async def insurance_timeout(room):
    try:
        await asyncio.sleep(INSURANCE_TIME)
        if room["state"] == "insurance":
            await resolve_insurance(room)
    except asyncio.CancelledError:
        pass

async def player_insurance_choice(room, uid, want):
    if room["state"] != "insurance": return
    if uid in room["ins_resp"]: return
    room["ins_resp"][uid] = bool(want)
    await bcast(room, {"type": "insurance_choice", "uid": uid, "took": bool(want)})
    if len(room["ins_resp"]) >= len(room["players"]):
        ctask(room, "turn_task")
        await resolve_insurance(room)

async def resolve_insurance(room):
    if room["state"] != "insurance": return
    dealer_bj = is_bj(room["dealer"])
    results = []
    for p in room["players"]:
        if not room["ins_resp"].get(p["uid"]): continue
        cost = min(p["bet"] // 2, await db_bal(p["uid"]))
        if cost <= 0: continue
        await db_add(p["uid"], -cost)
        p["insured"] = True
        if dealer_bj:
            payout = cost * 3
            await db_add(p["uid"], payout)
            results.append({"uid": p["uid"], "won": True,  "amount": payout - cost})
        else:
            results.append({"uid": p["uid"], "won": False, "amount": cost})
    room["state"] = "playing"
    await bcast(room, {"type": "insurance_result", "dealer_bj": dealer_bj, "results": results})
    if dealer_bj:
        for p in room["players"]: p["done"] = True
        room["cur"] = len(room["players"])
        await bcast(room, state_msg(room))
        await dealer_go(room)
    else:
        await bcast(room, state_msg(room))
        await next_turn(room)

async def next_turn(room):
    if room["state"] != "playing": return
    idx = room["cur"]
    if idx >= len(room["players"]):
        await dealer_go(room); return
    p = room["players"][idx]
    await bcast(room, state_msg(room))
    await bcast(room, {"type": "your_turn", "uid": p["uid"], "secs": TURN_TIME})
    ctask(room, "turn_task")
    room["turn_task"] = asyncio.create_task(auto_stand(room, idx))

async def auto_stand(room, idx):
    try:
        await asyncio.sleep(TURN_TIME)
        if room["state"] == "playing" and room["cur"] == idx:
            room["players"][idx]["done"] = True
            room["cur"] += 1
            await bcast(room, {"type": "auto_stand",
                               "uid": room["players"][idx]["uid"]})
            await next_turn(room)
    except asyncio.CancelledError:
        pass

# ── PLAYER ACTIONS ────────────────────────────────────────────────────────────
async def _after_bust(room, uid, idx):
    """Handle bust: VIP gets swap window, others end turn."""
    if await db_is_vip(uid):
        await bcast(room, state_msg(room))
        await send_uid(uid, {"type": "vip_bust", "secs": VIP_SWAP_TIME})
        ctask(room, "turn_task")
        room["turn_task"] = asyncio.create_task(vip_expire(room, idx))
    else:
        ctask(room, "turn_task")
        room["players"][idx]["done"] = True
        room["cur"] += 1
        await bcast(room, state_msg(room))
        await bcast(room, {"type": "bust", "uid": uid})
        await next_turn(room)

async def act_hit(room, uid):
    idx = room["cur"]
    room["players"][idx]["hand"].append(room["deck"].pop())
    if htot(room["players"][idx]["hand"]) > 21:
        await _after_bust(room, uid, idx)
    else:
        await bcast(room, state_msg(room))
        ctask(room, "turn_task")
        room["turn_task"] = asyncio.create_task(auto_stand(room, idx))

async def act_stand(room, uid):
    idx = room["cur"]
    room["players"][idx]["done"] = True
    room["cur"] += 1
    ctask(room, "turn_task")
    await bcast(room, state_msg(room))
    await next_turn(room)

async def act_double(room, uid):
    idx = room["cur"]
    p   = room["players"][idx]
    if await db_bal(uid) < p["bet"]:
        await send_uid(uid, {"type": "error", "msg": "no_balance_double"})
        return
    ctask(room, "turn_task")
    await db_add(uid, -p["bet"])
    p["bet"] *= 2
    p["doubled"] = True
    p["hand"].append(room["deck"].pop())
    if htot(p["hand"]) > 21:
        await _after_bust(room, uid, idx)
    else:
        p["done"] = True       # forced stand after double
        room["cur"] += 1
        await bcast(room, state_msg(room))
        await next_turn(room)

async def act_swap(room, uid):
    if not await db_is_vip(uid):
        await send_uid(uid, {"type": "error", "msg": "vip_only"})
        return
    idx = room["cur"]
    p   = room["players"][idx]
    p["hand"][-1] = room["deck"].pop()
    ctask(room, "turn_task")
    await bcast(room, state_msg(room))
    if htot(p["hand"]) > 21:
        p["done"] = True
        room["cur"] += 1
        await bcast(room, {"type": "bust", "uid": uid})
        await next_turn(room)
    else:
        room["turn_task"] = asyncio.create_task(auto_stand(room, idx))

async def vip_expire(room, idx):
    """VIP swap window expired — auto-end turn."""
    try:
        await asyncio.sleep(VIP_SWAP_TIME)
        if room["state"] == "playing" and room["cur"] == idx:
            room["players"][idx]["done"] = True
            room["cur"] += 1
            await bcast(room, state_msg(room))
            await next_turn(room)
    except asyncio.CancelledError:
        pass

# ── DEALER ────────────────────────────────────────────────────────────────────
async def dealer_go(room):
    room["state"] = "dealer"
    await bcast(room, {"type": "dealer_turn"})
    await bcast(room, state_msg(room, reveal=True))
    await asyncio.sleep(DEALER_DELAY)
    while dealer_should_hit(room["dealer"]):
        room["dealer"].append(room["deck"].pop())
        await bcast(room, state_msg(room, reveal=True))
        await asyncio.sleep(DEALER_DELAY)
    await game_end(room)

async def game_end(room):
    dl      = htot(room["dealer"])
    dl_bust = dl > 21
    results = []

    for p in room["players"]:
        pt   = htot(p["hand"])
        bust = pt > 21
        if bust:
            p["result"] = "bust";      p["win"] = 0
        elif is_bj(p["hand"]) and not is_bj(room["dealer"]):
            p["result"] = "blackjack"; p["win"] = int(p["bet"] * 2.5)
        elif dl_bust or pt > dl:
            p["result"] = "win";       p["win"] = p["bet"] * 2
        elif pt == dl:
            p["result"] = "push";      p["win"] = p["bet"]
        else:
            p["result"] = "lose";      p["win"] = 0

        if p["win"]: await db_add(p["uid"], p["win"])
        await db_stats(p["uid"], p["result"])
        results.append({
            "uid":     p["uid"],
            "result":  p["result"],
            "win":     p["win"],
            "balance": await db_bal(p["uid"]),
        })

    room["state"] = "done"

    # Release players immediately so they can join new games right away
    for p in room["players"]:
        in_room.pop(p["uid"], None)

    await bcast(room, state_msg(room, reveal=True))
    await bcast(room, {"type": "results", "results": results,
                       "dealer_total": dl})
    log.info(f"Room {room['id']} finished — {len(results)} players.")

    async def cleanup():
        await asyncio.sleep(30)
        rooms.pop(room["id"], None)
    asyncio.create_task(cleanup())

# ── LEADERBOARD ───────────────────────────────────────────────────────────────
async def leaderboard_payload(mode="balance"):
    col = {"balance": "bal", "wins": "w_bj"}.get(mode, "bal")
    rows = await pool.fetch(
        f"SELECT uid, name, bal, w_bj, l_bj FROM users ORDER BY {col} DESC LIMIT 20")
    return {
        "type": "leaderboard",
        "mode": mode,
        "rows": [{"uid": r["uid"], "name": r["name"], "bal": r["bal"],
                  "wins": r["w_bj"], "losses": r["l_bj"]} for r in rows],
    }

# ── DISCONNECT CLEANUP ────────────────────────────────────────────────────────
async def _on_disconnect(uid):
    """Вызывается когда SSE соединение закрывается."""
    rid = in_room.pop(uid, None)
    if not rid or rid not in rooms:
        return
    r = rooms[rid]
    if r["state"] == "lobby":
        p = next((x for x in r["players"] if x["uid"] == uid), None)
        if p:
            await db_add(uid, p["bet"])
            r["players"] = [x for x in r["players"] if x["uid"] != uid]
            r["ready_votes"].discard(uid)
        if not r["players"]:
            ctask(r, "lobby_task")
            rooms.pop(rid, None)
        else:
            await bcast(r, state_msg(r))
    elif r["state"] == "playing":
        idx = r["cur"]
        if idx < len(r["players"]) and r["players"][idx]["uid"] == uid:
            r["players"][idx]["done"] = True
            r["cur"] += 1
            asyncio.create_task(next_turn(r))

# ── SSE HANDLER (сервер → клиент) ─────────────────────────────────────────────
async def sse_handler(req):
    """GET /sse?init_data=...  — открывает поток событий для клиента."""
    init_data = req.query.get("init_data", "")
    ud = validate_init_data(init_data)

    resp = web.StreamResponse()
    resp.headers["Content-Type"]      = "text/event-stream"
    resp.headers["Cache-Control"]     = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"   # Railway/nginx не буферизует
    resp.headers["Access-Control-Allow-Origin"] = "*"
    await resp.prepare(req)

    if not ud:
        await resp.write(b'data: {"type":"error","msg":"auth_failed"}\n\n')
        return resp

    uid = ud["id"]
    u   = await db_user(uid)
    if not u:
        await resp.write(b'data: {"type":"error","msg":"user_not_found"}\n\n')
        return resp

    # Регистрируем очередь
    q = asyncio.Queue(maxsize=200)
    sse_map[uid] = q

    # Сразу отправляем auth_ok
    auth_ok = {
        "type":    "auth_ok",
        "uid":     uid,
        "name":    u["name"],
        "photo":   ud.get("photo_url", ""),
        "balance": u["bal"],
        "stars":   u.get("bot_stars", 0),
        "lang":    u["lang"] or "en",
        "vip":     await db_is_vip(uid),
        "skin":    u.get("equipped_skin") or "classic",
        "frame":   u.get("equipped_frame") or "none",
    }
    try:
        await resp.write(f'data: {json.dumps(auth_ok)}\n\n'.encode())
    except Exception:
        sse_map.pop(uid, None)
        return resp

    log.info(f"SSE open uid={uid}")
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20)
                await resp.write(f'data: {json.dumps(msg)}\n\n'.encode())
            except asyncio.TimeoutError:
                # Heartbeat — не даёт Railway закрыть idle-соединение
                await resp.write(b': keep-alive\n\n')
    except Exception:
        pass
    finally:
        sse_map.pop(uid, None)
        await _on_disconnect(uid)
        log.info(f"SSE close uid={uid}")

    return resp

# ── ACTION HANDLER (клиент → сервер) ──────────────────────────────────────────
async def action_handler(req):
    """POST /action  — принимает действия игрока."""
    try:
        d = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    if len(str(d)) > 4000:
        return web.json_response({"ok": False, "error": "too_large"}, status=400)

    init_data = d.get("init_data", "")
    ud = validate_init_data(init_data)
    if not ud:
        return web.json_response({"ok": False, "error": "auth_failed"}, status=401)

    uid = ud["id"]
    act = d.get("action", "")

    if uid not in sse_map:
        return web.json_response({"ok": False, "error": "no_sse"}, status=400)

    u    = await db_user(uid)
    room = rooms.get(in_room.get(uid))

    try:
        # ── SHOP ──────────────────────────────────────────────────────────────
        if act == "get_shop":
            await send_uid(uid, await shop_payload(uid))

        elif act == "buy_cosmetic":
            code    = str(d.get("code", ""))
            catalog = CARD_SKINS if code in CARD_SKINS else (PROFILE_FRAMES if code in PROFILE_FRAMES else None)
            if not catalog:
                await send_uid(uid, {"type": "error", "msg": "unknown_item"})
            else:
                price = catalog[code]["price"]
                owned = await db_owned(uid)
                if code in owned or price == 0:
                    await send_uid(uid, {"type": "error", "msg": "already_owned"})
                else:
                    stars = await db_stars(uid)
                    if stars < price:
                        await send_uid(uid, {"type": "error", "msg": "not_enough_stars",
                                             "have": stars, "need": price})
                    else:
                        await db_add_stars(uid, -price)
                        await db_grant(uid, code)
                        await send_uid(uid, await shop_payload(uid))

        elif act == "equip_cosmetic":
            code = str(d.get("code", ""))
            if code in CARD_SKINS:
                owned = await db_owned(uid)
                if code != "classic" and code not in owned:
                    await send_uid(uid, {"type": "error", "msg": "not_owned"})
                else:
                    await db_equip(uid, "skin", code)
                    await send_uid(uid, await shop_payload(uid))
            elif code in PROFILE_FRAMES:
                owned = await db_owned(uid)
                if code != "none" and code not in owned:
                    await send_uid(uid, {"type": "error", "msg": "not_owned"})
                else:
                    await db_equip(uid, "frame", code)
                    await send_uid(uid, await shop_payload(uid))
            else:
                await send_uid(uid, {"type": "error", "msg": "unknown_item"})

        elif act == "get_leaderboard":
            mode = d.get("mode", "balance")
            if mode not in ("balance", "wins"): mode = "balance"
            await send_uid(uid, await leaderboard_payload(mode))

        # ── JOIN ──────────────────────────────────────────────────────────────
        elif act == "join":
            if uid in in_room:
                await send_uid(uid, {"type": "error", "msg": "already_in_room"})
            else:
                try:   bet = int(d.get("bet", MIN_BET))
                except Exception: bet = MIN_BET
                bet = max(MIN_BET, min(MAX_BET, bet))
                bal = await db_bal(uid)
                if bal < bet:
                    await send_uid(uid, {"type": "error", "msg": "no_balance", "balance": bal})
                else:
                    room = open_room()
                    await db_add(uid, -bet)
                    u = await db_user(uid)
                    room["players"].append({
                        "uid": uid, "name": u["name"],
                        "photo": str(d.get("photo", ""))[:300],
                        "skin":  u.get("equipped_skin") or "classic",
                        "frame": u.get("equipped_frame") or "none",
                        "bet":   bet,
                        "hand": [], "done": False, "doubled": False, "insured": False,
                        "result": None, "win": 0,
                    })
                    in_room[uid] = room["id"]
                    await send_uid(uid, {
                        "type": "joined", "room_id": room["id"],
                        "balance": await db_bal(uid),
                        "player_index": len(room["players"]) - 1,
                    })
                    await bcast(room, state_msg(room))
                    if len(room["players"]) == 1:
                        room["lobby_task"] = asyncio.create_task(lobby_loop(room))
                    if len(room["players"]) >= MAX_PLAYERS:
                        ctask(room, "lobby_task")
                        asyncio.create_task(game_start(room))
                    log.info(f"Join room={room['id']} uid={uid} bet={bet} players={len(room['players'])}")

        # ── READY SKIP ────────────────────────────────────────────────────────
        elif act == "ready_skip":
            if room and room["state"] == "lobby":
                room["ready_votes"].add(uid)
                await bcast(room, state_msg(room))
                await maybe_skip_lobby(room)

        # ── INSURANCE ─────────────────────────────────────────────────────────
        elif act in ("insure", "no_insure"):
            if room and room["state"] == "insurance":
                await player_insurance_choice(room, uid, act == "insure")

        # ── GAME ACTIONS ──────────────────────────────────────────────────────
        elif act in ("hit", "stand", "double", "swap"):
            if room and room["state"] == "playing":
                idx = room["cur"]
                if idx < len(room["players"]) and room["players"][idx]["uid"] == uid:
                    if act == "hit":    await act_hit(room, uid)
                    elif act == "stand":  await act_stand(room, uid)
                    elif act == "double": await act_double(room, uid)
                    elif act == "swap":   await act_swap(room, uid)
                else:
                    await send_uid(uid, {"type": "error", "msg": "not_your_turn"})

        # ── QUICK COMMENTS ────────────────────────────────────────────────────
        elif act == "comment":
            if room:
                now = time.time()
                if now - _last_comment.get(uid, 0) >= COMMENT_COOLDOWN:
                    _last_comment[uid] = now
                    text = str(d.get("text", ""))[:24]
                    if text.strip():
                        await bcast(room, {"type": "comment", "uid": uid, "text": text})

    except Exception as e:
        log.exception(f"action_handler uid={uid} act={act}: {e}")
        return web.json_response({"ok": False, "error": "internal"}, status=500)

    return web.json_response({"ok": True})

# ── HTTP ──────────────────────────────────────────────────────────────────────
async def index_handler(req):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "webapp.html"), encoding="utf-8") as f:
        return web.Response(
            text=f.read(),
            content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store"},
        )

async def avatar_handler(req):
    """Proxy a player's current Telegram profile photo. 404 if unavailable
    (private settings, no photo, etc) — client falls back to an initials avatar."""
    try:
        uid = int(req.match_info.get("uid", ""))
    except Exception:
        return web.Response(status=400)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            async with s.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos",
                              params={"user_id": uid, "limit": 1}) as r:
                data = await r.json()
            photos = (data.get("result") or {}).get("photos") or []
            if not photos:
                return web.Response(status=404)
            file_id = photos[0][-1]["file_id"]
            async with s.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                              params={"file_id": file_id}) as r:
                fdata = await r.json()
            file_path = (fdata.get("result") or {}).get("file_path")
            if not file_path:
                return web.Response(status=404)
            async with s.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}") as r:
                img = await r.read()
                ctype = r.headers.get("Content-Type", "image/jpeg")
        return web.Response(body=img, content_type=ctype,
                             headers={"Cache-Control": "public, max-age=1800"})
    except Exception as e:
        log.warning(f"avatar fetch failed uid={uid}: {e}")
        return web.Response(status=404)

# ── LIFECYCLE ─────────────────────────────────────────────────────────────────
async def on_startup(app):
    global pool
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    log.info(f"DB connected. Listening on :{PORT}")

async def on_cleanup(app):
    if pool: await pool.close()

def main():
    app = web.Application()
    app.router.add_get("/",              index_handler)
    app.router.add_get("/avatar/{uid}",  avatar_handler)
    app.router.add_get("/sse",           sse_handler)     # сервер → клиент
    app.router.add_post("/action",       action_handler)  # клиент → сервер
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)

if __name__ == "__main__":
    main()
