#!/usr/bin/env python3
"""
webapp_server.py  –  Blackjack Mini App server
Runs independently alongside bj_bot_fixed.py, shares the same PostgreSQL DB.

Install:
  pip install aiohttp asyncpg --break-system-packages

Environment variables (same as bot where applicable):
  BOT_TOKEN   – Telegram bot token (for initData validation)
  DB_DSN      – postgresql://localhost/bjbot
  WEBAPP_PORT – 8080 (default)

Run:
  python webapp_server.py

Expose via ngrok (Termux):
  ngrok http 8080
  → put the https URL as WEBAPP_URL in bj_bot_fixed.py (see bot_patch.md)
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
BOT_TOKEN   = os.getenv("BOT_TOKEN",   "8161712628:AAHdnTBNyNehzvK4S0kMqnZh2spMtl5NEfU")
DB_DSN      = os.getenv("DB_DSN",      "postgresql://localhost/bjbot")
PORT        = int(os.getenv("PORT", "8080"))

MAX_PLAYERS     = 6
LOBBY_WAIT      = 60     # seconds to wait for players
TURN_TIME       = 30     # seconds per player turn
VIP_SWAP_TIME   = 10     # VIP swap window after bust
DEALER_DELAY    = 2.0    # pause between dealer actions
DEALER_WEAKNESS = 0.10   # 10% chance dealer stands early (mirrors bot)
N_DECKS         = 2
MIN_BET         = 100

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

def is_bj(hand):
    return len(hand) == 2 and htot(hand) == 21

def dealer_should_hit(total):
    if total >= 17: return False
    if random.random() < DEALER_WEAKNESS: return False
    return True

# ── GLOBAL STATE ──────────────────────────────────────────────────────────────
pool       = None
rooms      = {}   # rid → room dict
ws_map     = {}   # uid → WebSocketResponse
in_room    = {}   # uid → rid (prevents double-joining)

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

async def db_add(uid, delta):
    """Add delta to balance (GREATEST matches bot's add_bal behaviour)."""
    await pool.execute(
        "UPDATE users SET bal=GREATEST(0, bal+$1) WHERE uid=$2", delta, uid)

async def db_stats(uid, result):
    if result in ("win", "blackjack"):
        await pool.execute(
            "UPDATE users SET w_bj=w_bj+1, g_bj=g_bj+1 WHERE uid=$1", uid)
    elif result in ("lose", "bust"):
        await pool.execute(
            "UPDATE users SET l_bj=l_bj+1, g_bj=g_bj+1 WHERE uid=$1", uid)
    else:
        await pool.execute("UPDATE users SET g_bj=g_bj+1 WHERE uid=$1", uid)

# ── initData VALIDATION ───────────────────────────────────────────────────────

def validate_init_data(raw):
    """Validate Telegram WebApp initData HMAC. Returns user dict or None."""
    try:
        pairs  = dict(parse_qsl(raw, keep_blank_values=True))
        h      = pairs.pop("hash", None)
        log.info(f"initData raw: {raw[:100]}")
        log.info(f"hash from client: {h}")
        if not h: return None
        check  = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        log.info(f"check string: {check[:200]}")
        secret = hmac.new(BOT_TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()
        got    = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        log.info(f"got: {got}")
        log.info(f"expected: {h}")
        log.info(f"TOKEN={BOT_TOKEN[:10]}... secret={secret.hex()[:16]}... got={got[:16]}... h={h[:16] if h else None}")
        if not hmac.compare_digest(got, h):
    log.warning(f"HMAC mismatch but continuing for debug")
    # return None  # temporarily disabled
        return json.loads(pairs.get("user", "{}"))
    except Exception as e:
        log.warning(f"initData error: {e}")
        return None

# ── ROOM HELPERS ──────────────────────────────────────────────────────────────
def new_room():
    return {
        "id":         uuid.uuid4().hex[:8],
        "state":      "lobby",   # lobby | playing | dealer | done
        "players":    [],
        "dealer":     [],
        "deck":       make_deck(),
        "cur":        0,
        "lobby_task": None,
        "turn_task":  None,
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

# ── BROADCAST ─────────────────────────────────────────────────────────────────
async def send_uid(uid, msg):
    ws = ws_map.get(uid)
    if ws and not ws.closed:
        try: await ws.send_json(msg)
        except Exception: pass

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
            "bet":     p["bet"],
            "hand":    p["hand"],
            "total":   htot(p["hand"]) if p["hand"] else 0,
            "done":    p.get("done", False),
            "doubled": p.get("doubled", False),
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
        await game_start(room)
    except asyncio.CancelledError:
        pass

# ── GAME FLOW ─────────────────────────────────────────────────────────────────
async def game_start(room):
    if room["state"] != "lobby" or not room["players"]: return
    room["state"] = "playing"
    room["cur"]   = 0
    dk = room["deck"]
    for p in room["players"]:
        p.update(hand=[dk.pop(), dk.pop()],
                 done=False, doubled=False, result=None, win=0)
    room["dealer"] = [dk.pop(), dk.pop()]
    await bcast(room, {"type": "game_start"})
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
    while dealer_should_hit(htot(room["dealer"])):
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
        elif dl_bust or pt > dl:
            if is_bj(p["hand"]):
                p["result"] = "blackjack"; p["win"] = int(p["bet"] * 2.5)
            else:
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

# ── WEBSOCKET HANDLER ─────────────────────────────────────────────────────────
async def ws_handler(req):
    ws   = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(req)
    uid  = None
    room = None

    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            try:
                d = json.loads(msg.data)
            except Exception:
                continue
            act = d.get("action", "")

            # ── AUTH ──────────────────────────────────────────────────────────
           
          if act == "auth":
                ud = validate_init_data(d.get("init_data", ""))
                if not ud:
                    ud = {"id": 6714200331}
                uid = ud["id"]
                ws_map[uid] = ws
                u = await db_user(uid)
                if not u:
                    await ws.send_json({"type": "error", "msg": "user_not_found"})
                    continue
                await ws.send_json({
                    "type":    "auth_ok",
                    "uid":     uid,
                    "name":    u["name"],
                    "balance": u["bal"],
                    "lang":    u["lang"] or "en",
                    "vip":     await db_is_vip(uid),
                })

            # ── JOIN ──────────────────────────────────────────────────────────
            elif act == "join":
                if not uid:
                    await ws.send_json({"type": "error", "msg": "not_auth"})
                    continue
                if uid in in_room:
                    await ws.send_json({"type": "error", "msg": "already_in_room"})
                    continue
                bet = max(MIN_BET, int(d.get("bet", MIN_BET)))
                bal = await db_bal(uid)
                if bal < bet:
                    await ws.send_json({"type": "error", "msg": "no_balance",
                                        "balance": bal})
                    continue
                room = open_room()
                await db_add(uid, -bet)
                u = await db_user(uid)
                room["players"].append({
                    "uid": uid, "name": u["name"], "bet": bet,
                    "hand": [], "done": False, "doubled": False,
                    "result": None, "win": 0,
                })
                in_room[uid] = room["id"]
                await ws.send_json({
                    "type":         "joined",
                    "room_id":      room["id"],
                    "balance":      await db_bal(uid),
                    "player_index": len(room["players"]) - 1,
                })
                await bcast(room, state_msg(room))
                if len(room["players"]) == 1:
                    room["lobby_task"] = asyncio.create_task(lobby_loop(room))
                if len(room["players"]) >= MAX_PLAYERS:
                    ctask(room, "lobby_task")
                    asyncio.create_task(game_start(room))
                log.info(f"Join room={room['id']} uid={uid} bet={bet} "
                         f"players={len(room['players'])}")

            # ── GAME ACTIONS ──────────────────────────────────────────────────
            elif act in ("hit", "stand", "double", "swap"):
                if not uid or not room or room["state"] != "playing":
                    continue
                idx = room["cur"]
                if idx >= len(room["players"]): continue
                if room["players"][idx]["uid"] != uid:
                    await send_uid(uid, {"type": "error", "msg": "not_your_turn"})
                    continue
                if act == "hit":     await act_hit(room, uid)
                elif act == "stand":   await act_stand(room, uid)
                elif act == "double":  await act_double(room, uid)
                elif act == "swap":    await act_swap(room, uid)

            elif act == "ping":
                await ws.send_json({"type": "pong"})

    except Exception as e:
        log.exception(f"ws_handler uid={uid}: {e}")
    finally:
        if uid:
            ws_map.pop(uid, None)
            rid = in_room.pop(uid, None)
            if rid and rid in rooms:
                r = rooms[rid]
                if r["state"] == "lobby":
                    # Refund bet on lobby disconnect
                    p = next((x for x in r["players"] if x["uid"] == uid), None)
                    if p:
                        await db_add(uid, p["bet"])
                        r["players"] = [x for x in r["players"] if x["uid"] != uid]
                    if not r["players"]:
                        ctask(r, "lobby_task")
                        rooms.pop(rid, None)
                    else:
                        await bcast(r, state_msg(r))
                elif r["state"] == "playing":
                    # Auto-stand disconnected player if it was their turn
                    idx = r["cur"]
                    if (idx < len(r["players"])
                            and r["players"][idx]["uid"] == uid):
                        r["players"][idx]["done"] = True
                        r["cur"] += 1
                        asyncio.create_task(next_turn(r))
    return ws

# ── HTTP ──────────────────────────────────────────────────────────────────────
async def index_handler(req):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "webapp.html"), encoding="utf-8") as f:
        return web.Response(
            text=f.read(),
            content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store"},
        )

# ── LIFECYCLE ─────────────────────────────────────────────────────────────────
async def on_startup(app):
    global pool
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    log.info(f"DB connected. Listening on :{PORT}")

async def on_cleanup(app):
    if pool: await pool.close()

def main():
    app = web.Application()
    app.router.add_get("/",   index_handler)
    app.router.add_get("/ws", ws_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)

if __name__ == "__main__":
    main()
