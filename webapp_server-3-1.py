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
WEBAPP_PUBLIC_URL = os.getenv("WEBAPP_PUBLIC_URL", "")  # e.g. https://yourapp.up.railway.app — used for the "Join game" button in friend invites

MAX_PLAYERS      = 6
LOBBY_WAIT       = 60     # seconds to wait for players before auto-start (starts with whoever is there)
READY_MIN        = 3      # minimum players required to vote to skip the wait
TURN_TIME        = 30     # seconds per player turn
VIP_SWAP_TIME    = 10     # VIP swap window after bust
INSURANCE_TIME   = 15     # seconds to decide on insurance
DEALER_DELAY     = 3.6    # pause between dealer actions (slightly slower pacing, was 3.0)
N_DECKS          = 6
MIN_BET          = 100
MAX_BET          = 1_000_000
COMMENT_COOLDOWN = 1.5    # seconds between quick comments, per player

# ── COSMETICS CATALOG (💎 diamonds only — no real-money purchase here) ────────
# All rendering is client-side (CSS/SVG/canvas) — no external art assets.
# NOTE: skin codes and frame codes are deliberately drawn from disjoint
# namespaces (no shared keys like the old "gold"/"gold" collision) so
# buy_cosmetic/equip_cosmetic can never misroute a frame as a skin or vice
# versa. The "kind" field sent by the client is still authoritative — see
# action_handler — this distinct-code set is a second, belt-and-braces guard.
# referral-milestone-only cosmetics — must be defined before the catalogs below
# since they're referenced inline there
REFERRAL_SKIN_MILESTONE  = 3
REFERRAL_FRAME_MILESTONE = 5

CARD_SKINS = {
    "classic":     {"name": "Classic",           "price": 0},
    "neonblue":    {"name": "⚡ Neon Blue",       "price": 12},
    "neonpurple":  {"name": "🟣 Neon Purple",     "price": 18},
    "casinored":   {"name": "🔴 Casino Red",      "price": 25},
    "tablegreen":  {"name": "🟢 Table Green",     "price": 32},
    "metalsteel":  {"name": "🪙 Metal Steel",     "price": 42},
    "obsidian":    {"name": "⬛ Obsidian",        "price": 52},
    "royalpattern":{"name": "🧿 Royal Pattern",   "price": 65},
    "aurora":      {"name": "🌈 Aurora",          "price": 79},
    "dragonscale": {"name": "🐉 Dragon Scale",    "price": 99,  "legendary": True},
    "celestial":   {"name": "✨ Celestial",       "price": 129, "legendary": True},
    "referral3":   {"name": "🎁 Referral Gold",   "price": None, "referral_only": True, "milestone": REFERRAL_SKIN_MILESTONE},
}
PROFILE_FRAMES = {
    "none":     {"name": "None",             "price": 0},
    "electric": {"name": "⚡ Электрическая",  "price": 25},
    "fire":     {"name": "🔥 Огненная",       "price": 39},
    "ocean":    {"name": "🌊 Океан",          "price": 55},
    "space":    {"name": "🌌 Космос",         "price": 75},
    "blossom":  {"name": "🌸 Сакура",         "price": 99},
    "diamond":  {"name": "💎 Алмазная",       "price": 129},
    "casino":   {"name": "💰 Казино",         "price": 155},
    "royal":    {"name": "👑 Королевская",    "price": 179},
    "phoenix":  {"name": "🦅 Phoenix",        "price": 219, "legendary": True},
    "oracle":   {"name": "🔮 Oracle",         "price": 259, "legendary": True},
    "referral5":{"name": "🎗️ Referral Circle","price": None, "referral_only": True, "milestone": REFERRAL_FRAME_MILESTONE},
}
# ── SHOP: VIP + chip packs, paid with 💎 diamonds (mirrors bj.py SHOP_ITEMS,
# kept in sync manually since bj.py and webapp_server.py are separate
# Railway services and don't import each other) ───────────────────────────────
VIP_PRICE_DIAMONDS = 119
CHIP_PACKS = {
    "top1": {"label": "100 000¢",     "chips": 100_000,   "price": 49},
    "top2": {"label": "210 000¢",     "chips": 210_000,   "price": 99},
    "top3": {"label": "550 000¢",     "chips": 550_000,   "price": 249},
    "top4": {"label": "1 150 000¢",   "chips": 1_150_000, "price": 499},
    "top5": {"label": "2 500 000¢",   "chips": 2_500_000, "price": 999},
}
# gift-a-friend packs: (diamonds sent, price in real ⭐ Telegram Stars). Always
# a fresh Stars purchase — you can never gift from your own diamond balance.
GIFT_DIAMOND_PACKS = {
    "gift1": (10, 12),
    "gift2": (25, 28),
    "gift3": (50, 52),
    "gift4": (100, 100),
    "gift5": (250, 235),
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

# ── SPLIT HELPERS ───────────────────────────────────────────────────────────
# A split player has two independent hands stored as hand/hand2 (+ matching
# bet/bet2, doubled/doubled2, done/done2, result/result2, win/win2). `hkey`
# gives the field-name suffix ("" or "2") for whichever hand is currently
# being played, so the existing single-hand action code can stay almost
# unchanged — it just reads/writes p[f"hand{k}"] etc instead of p["hand"].
def hkey(p):
    return "2" if p.get("active_hand") == 2 else ""

def hand_done(p, k=None):
    return p["done2"] if (k if k is not None else hkey(p)) == "2" else p["done"]

def dealer_should_hit(hand):
    """Standard casino rule, matches bj.py: hit on 16 or less, and on soft 17. No artificial weakness."""
    total = htot(hand)
    if total < 17: return True
    if total == 17 and is_soft(hand): return True
    return False

# ── GLOBAL STATE ──────────────────────────────────────────────────────────────
pool          = None
rooms         = {}   # rid → room dict
sessions      = {}   # sid → uid
event_store   = {}   # uid → list of pending events
poll_waiter   = {}   # uid → asyncio.Event  (будит long-poll)
last_seen     = {}   # uid → timestamp
in_room       = {}   # uid → rid
_last_comment = {}   # uid → timestamp
_invite_times = {}   # uid → list[timestamp], rolling-window rate limit for invite_friend_to_play
INVITE_RATE_LIMIT  = 3    # max invites
INVITE_RATE_WINDOW = 60   # per this many seconds

def _invite_rate_limited(uid: int) -> bool:
    """True if uid has already sent INVITE_RATE_LIMIT invites in the last
    INVITE_RATE_WINDOW seconds (anti-spam)."""
    now = time.time()
    times = [ts for ts in _invite_times.get(uid, []) if now - ts < INVITE_RATE_WINDOW]
    _invite_times[uid] = times
    if len(times) >= INVITE_RATE_LIMIT:
        return True
    times.append(now)
    return False
_avatar_cache = {}   # uid → (bytes, content_type, fetched_at) — see avatar_handler
AVATAR_TTL    = 1800  # 30 min; avoids hammering Telegram's API per-request (was the
                       # root cause behind "avatar sometimes doesn't show")
AVATAR_MISS_TTL = 60  # cache "no photo" misses too, but for a much shorter time

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

DEFAULT_QUICK_REPLIES = ["GG 🤝", "Удачи! 🍀", "Вот это раздача 😳", "Ещё раз?", "🔥🔥🔥"]
MAX_QUICK_REPLIES = 5

async def db_quick_replies(uid):
    u = await db_user(uid)
    raw = u["quick_replies"] if u else ""
    if not raw:
        return list(DEFAULT_QUICK_REPLIES)
    try:
        lst = json.loads(raw)
        if isinstance(lst, list):
            return [str(x)[:40] for x in lst[:MAX_QUICK_REPLIES]]
    except Exception:
        pass
    return list(DEFAULT_QUICK_REPLIES)

async def db_set_quick_replies(uid, phrases):
    clean = [str(p)[:40] for p in phrases if str(p).strip()][:MAX_QUICK_REPLIES]
    await pool.execute("UPDATE users SET quick_replies=$1 WHERE uid=$2",
                        json.dumps(clean, ensure_ascii=False), uid)
    return clean

async def db_add_history(uid, bet, result, change):
    await pool.execute(
        "INSERT INTO game_history(uid, ts, bet, result, change) VALUES($1,$2,$3,$4,$5)",
        uid, int(time.time()), bet, result, change)
    # keep only the most recent 30 rows per player — plenty for a "last 10" view
    # with headroom, without letting the table grow unbounded
    await pool.execute(
        "DELETE FROM game_history WHERE uid=$1 AND id NOT IN "
        "(SELECT id FROM game_history WHERE uid=$1 ORDER BY ts DESC LIMIT 30)", uid)

async def db_recent_games(uid, limit=10):
    rows = await pool.fetch(
        "SELECT ts, bet, result, change FROM game_history WHERE uid=$1 "
        "ORDER BY ts DESC LIMIT $2", uid, limit)
    return [{"ts": r["ts"], "bet": r["bet"], "result": r["result"], "change": r["change"]}
            for r in rows]

def _pair(a, b):
    return (a, b) if a < b else (b, a)

async def db_find_user(query: str):
    """Look up a user by numeric Telegram ID or by @username / username
    (case-insensitive) for the friends 'add by ID/username' search."""
    query = (query or "").strip().lstrip("@")
    if not query:
        return None
    if query.isdigit():
        return await pool.fetchrow(
            "SELECT uid, name, equipped_frame FROM users WHERE uid=$1", int(query))
    return await pool.fetchrow(
        "SELECT uid, name, equipped_frame FROM users WHERE lower(username)=lower($1)", query)

async def db_friend_status(uid, other):
    if uid == other: return "self"
    a, b = _pair(uid, other)
    row = await pool.fetchrow("SELECT status, requested_by FROM friendships WHERE uid_a=$1 AND uid_b=$2", a, b)
    if not row: return "none"
    if row["status"] == "accepted": return "friends"
    return "pending_outgoing" if row["requested_by"] == uid else "pending_incoming"

async def db_send_friend_request(uid, other):
    a, b = _pair(uid, other)
    existing = await pool.fetchrow("SELECT status, requested_by FROM friendships WHERE uid_a=$1 AND uid_b=$2", a, b)
    if existing:
        if existing["status"] == "accepted":
            return "friends"
        if existing["requested_by"] != uid:
            await pool.execute("UPDATE friendships SET status='accepted' WHERE uid_a=$1 AND uid_b=$2", a, b)
            return "friends"   # they'd already asked us — mutual request, instant friends
        return "pending_outgoing"
    await pool.execute(
        "INSERT INTO friendships(uid_a,uid_b,status,requested_by,created_at) VALUES($1,$2,'pending',$3,$4)",
        a, b, uid, int(time.time()))
    return "pending_outgoing"

async def db_respond_friend_request(uid, other, accept):
    a, b = _pair(uid, other)
    row = await pool.fetchrow("SELECT status, requested_by FROM friendships WHERE uid_a=$1 AND uid_b=$2", a, b)
    if not row or row["status"] != "pending" or row["requested_by"] == uid:
        return False
    if accept:
        await pool.execute("UPDATE friendships SET status='accepted' WHERE uid_a=$1 AND uid_b=$2", a, b)
    else:
        await pool.execute("DELETE FROM friendships WHERE uid_a=$1 AND uid_b=$2", a, b)
    return True

async def db_remove_friend(uid, other):
    a, b = _pair(uid, other)
    await pool.execute("DELETE FROM friendships WHERE uid_a=$1 AND uid_b=$2", a, b)

async def db_friends_list(uid):
    rows = await pool.fetch(
        "SELECT u.uid, u.name, u.equipped_frame, u.last_seen "
        "FROM friendships f JOIN users u ON u.uid = (CASE WHEN f.uid_a=$1 THEN f.uid_b ELSE f.uid_a END) "
        "WHERE (f.uid_a=$1 OR f.uid_b=$1) AND f.status='accepted' ORDER BY u.last_seen DESC", uid)
    return rows

async def db_incoming_requests(uid):
    rows = await pool.fetch(
        "SELECT u.uid, u.name, u.equipped_frame "
        "FROM friendships f JOIN users u ON u.uid = (CASE WHEN f.uid_a=$1 THEN f.uid_b ELSE f.uid_a END) "
        "WHERE (f.uid_a=$1 OR f.uid_b=$1) AND f.status='pending' AND f.requested_by != $1", uid)
    return rows

def fmt_last_seen(ts):
    if not ts: return "давно"
    secs = max(0, int(time.time()) - int(ts))
    if secs < 120: return "в сети"
    if secs < 3600: return f"{secs//60} мин. назад"
    if secs < 86400: return f"{secs//3600} ч. назад"
    return f"{secs//86400} дн. назад"

async def db_profile(uid, viewer_uid=None):
    """Full public profile shown when clicking any player's avatar."""
    u = await db_user(uid)
    if not u:
        return None
    g = u["g_bj"] or 0
    w = u["w_bj"] or 0
    pct = round(100 * w / g, 1) if g else 0.0
    return {
        "type":       "profile",
        "uid":        uid,
        "name":       u["name"],
        "balance":    u["bal"],
        "diamonds":   u["bot_stars"],
        "vip":        await db_is_vip(uid),
        "wins":       w,
        "losses":     u["l_bj"] or 0,
        "games":      g,
        "win_pct":    pct,
        "skin":       u["equipped_skin"] or "classic",
        "frame":      u["equipped_frame"] or "none",
        "history":    await db_recent_games(uid, 10),
        "friend_status": await db_friend_status(viewer_uid, uid) if viewer_uid else "self",
        "last_seen_txt": fmt_last_seen(u["last_seen"]),
    }

async def shop_payload(uid):
    owned = await db_owned(uid)
    skin, frame = await db_equipped(uid)
    return {
        "type": "shop_data",
        "diamonds": await db_stars(uid),
        "skins": [{"code": c, **v, "owned": (c == "classic" or c in owned)} for c, v in CARD_SKINS.items()],
        "frames": [{"code": c, **v, "owned": (c == "none" or c in owned)} for c, v in PROFILE_FRAMES.items()],
        "equipped_skin": skin,
        "equipped_frame": frame,
        "vip": await db_is_vip(uid),
        "vip_price": VIP_PRICE_DIAMONDS,
        "chip_packs": [{"code": c, **v} for c, v in CHIP_PACKS.items()],
    }

# ── initData VALIDATION ───────────────────────────────────────────────────────
def validate_init_data(raw):
    """Validate Telegram WebApp initData HMAC. Returns user dict or None."""
    try:
        pairs  = dict(parse_qsl(raw, keep_blank_values=True))
        h      = pairs.pop("hash", None)
        if not h: return None
        check  = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
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

# ── BROADCAST (long-poll через event_store) ───────────────────────────────────
async def send_uid(uid, msg):
    if uid not in event_store:
        event_store[uid] = []
    event_store[uid].append(msg)
    ev = poll_waiter.get(uid)
    if ev and not ev.is_set():
        ev.set()

async def bcast(room, msg):
    for p in list(room["players"]):
        await send_uid(p["uid"], msg)

# ── BLACKJACK DUEL (1v1, inline-mode) ──────────────────────────────────────────
# A duel is created/accepted in the bot process (bj-1.py) via inline mode; by
# the time a player opens the Mini App with ?duel=<id>, the `duels` row in
# Postgres is already status='accepted' and both stakes are already held.
# This section only runs the actual card game and pays out the winner.
duel_rooms = {}          # duel_id (int) -> room dict
DUEL_JOIN_TIMEOUT = 180  # refund + cancel if the 2nd player never shows up

def _new_duel_room(duel_id, a_uid, a_name, b_uid, b_name, bet):
    return {
        "id": duel_id, "bet": bet, "deck": make_deck(),
        "players": {
            a_uid: {"uid": a_uid, "name": a_name, "hand": [], "done": False, "connected": False},
            b_uid: {"uid": b_uid, "name": b_name, "hand": [], "done": False, "connected": False},
        },
        "started": False, "finished": False, "result_text": {},
    }

def _duel_opponent(room, uid):
    for p in room["players"].values():
        if p["uid"] != uid:
            return p
    return None

def _duel_view_hand(hand, finished):
    """Opponent's first card stays hidden until the duel is finished — same
    convention as the dealer's hole card in the normal table game."""
    if finished or not hand:
        return hand
    return [{"r": "?", "s": "?"}] + hand[1:]

async def _duel_payload(room, viewer_uid):
    me  = room["players"][viewer_uid]
    opp = _duel_opponent(room, viewer_uid)
    finished = room["finished"]
    opp_out = None
    if opp:
        opp_hand = _duel_view_hand(opp["hand"], finished)
        opp_out = {
            "uid": opp["uid"], "name": opp["name"], "hand": opp_hand,
            "total": htot(opp["hand"]) if finished else None,   # never leak total while hidden
            "done": opp["done"], "connected": opp["connected"],
        }
    payload = {
        "type": "duel_state", "duel_id": room["id"], "bet": room["bet"],
        "started": room["started"], "finished": finished,
        "me": {"uid": me["uid"], "name": me["name"], "hand": me["hand"],
               "total": htot(me["hand"]) if me["hand"] else 0, "done": me["done"]},
        "opp": opp_out,
        "result": room["result_text"].get(viewer_uid) if finished else None,
    }
    if finished:
        payload["balance"] = await db_bal(viewer_uid)   # so the client can sync the wallet after payout
    return payload

async def _duel_broadcast(room):
    for uid in room["players"]:
        await send_uid(uid, await _duel_payload(room, uid))

async def _duel_refund_if_abandoned(duel_id):
    await asyncio.sleep(DUEL_JOIN_TIMEOUT)
    room = duel_rooms.get(duel_id)
    if not room or room["started"] or room["finished"]:
        return
    room["finished"] = True
    for p in room["players"].values():
        await db_add(p["uid"], room["bet"])   # refund — opponent never connected
    await pool.execute("UPDATE duels SET status='cancelled' WHERE id=$1", duel_id)
    for uid in room["players"]:
        await send_uid(uid, {"type": "error", "msg": "duel_abandoned"})
    duel_rooms.pop(duel_id, None)

async def _duel_cleanup_later(duel_id):
    await asyncio.sleep(30)   # keep the finished state around briefly for late pollers
    duel_rooms.pop(duel_id, None)

async def _duel_maybe_finish(room):
    players = list(room["players"].values())
    if not all(p["done"] for p in players):
        return
    room["finished"] = True
    a, b = players
    ta, tb = htot(a["hand"]), htot(b["hand"])
    bust_a, bust_b = ta > 21, tb > 21
    bet = room["bet"]

    if bust_a and bust_b:            outcome = "push"
    elif bust_a:                     outcome = "b"
    elif bust_b:                     outcome = "a"
    elif ta > tb:                    outcome = "a"
    elif tb > ta:                    outcome = "b"
    else:                            outcome = "push"

    rt = room["result_text"]
    if outcome == "push":
        await db_add(a["uid"], bet); await db_add(b["uid"], bet)
        rt[a["uid"]] = rt[b["uid"]] = "push"
        changes = {a["uid"]: 0, b["uid"]: 0}
    else:
        winner, loser = (a, b) if outcome == "a" else (b, a)
        await db_add(winner["uid"], bet * 2)
        rt[winner["uid"]] = "win"; rt[loser["uid"]] = "lose"
        changes = {winner["uid"]: bet, loser["uid"]: -bet}

    for p in players:
        await db_add_history(p["uid"], bet, f"duel_{rt[p['uid']]}", changes[p["uid"]])
    try:
        await pool.execute("UPDATE duels SET status='done' WHERE id=$1", room["id"])
    except Exception:
        pass
    await _duel_broadcast(room)
    asyncio.create_task(_duel_cleanup_later(room["id"]))

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
            "split":       p.get("split", False),
            "active_hand": p.get("active_hand", 1),
            "bet2":     p.get("bet2", 0),
            "hand2":    p.get("hand2"),
            "total2":   htot(p["hand2"]) if p.get("hand2") else 0,
            "done2":    p.get("done2", False),
            "doubled2": p.get("doubled2", False),
            "result2":  p.get("result2"),
            "win2":     p.get("win2", 0),
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
    room["deck"]  = make_deck()  # fresh shuffled 6-deck shoe for every round
    dk = room["deck"]
    for p in room["players"]:
        p.update(hand=[dk.pop(), dk.pop()],
                 done=False, doubled=False, insured=False, result=None, win=0,
                 split=False, active_hand=1, hand2=None, bet2=0, doubled2=False,
                 done2=False, result2=None, win2=0)
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

async def _advance_after_hand(room, idx):
    """Call after the active hand is marked done. If the player split and
    hasn't played their second hand yet, switch to it and keep their turn
    (fresh timer). Otherwise move on to the next player."""
    p = room["players"][idx]
    if p.get("split") and hkey(p) == "" and not p["done2"]:
        p["active_hand"] = 2
        await bcast(room, state_msg(room))
        ctask(room, "turn_task")
        room["turn_task"] = asyncio.create_task(auto_stand(room, idx))
        return True   # stayed on the same player, second hand now active
    room["cur"] += 1
    return False

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
            p = room["players"][idx]
            k = hkey(p)
            p[f"done{k}"] = True
            await bcast(room, {"type": "auto_stand", "uid": p["uid"]})
            if not await _advance_after_hand(room, idx):
                await next_turn(room)
    except asyncio.CancelledError:
        pass

# ── PLAYER ACTIONS ────────────────────────────────────────────────────────────
async def _after_bust(room, uid, idx):
    """Handle bust: VIP gets swap window, others end turn (or move to their
    second hand, if they split and haven't played it yet)."""
    p = room["players"][idx]
    if await db_is_vip(uid):
        await bcast(room, state_msg(room))
        await send_uid(uid, {"type": "vip_bust", "secs": VIP_SWAP_TIME})
        ctask(room, "turn_task")
        room["turn_task"] = asyncio.create_task(vip_expire(room, idx))
    else:
        ctask(room, "turn_task")
        k = hkey(p)
        p[f"done{k}"] = True
        await bcast(room, state_msg(room))
        await bcast(room, {"type": "bust", "uid": uid})
        if not await _advance_after_hand(room, idx):
            await next_turn(room)

async def act_hit(room, uid):
    idx = room["cur"]
    p = room["players"][idx]
    k = hkey(p)
    p[f"hand{k}"].append(room["deck"].pop())
    if htot(p[f"hand{k}"]) > 21:
        await _after_bust(room, uid, idx)
    else:
        await bcast(room, state_msg(room))
        ctask(room, "turn_task")
        room["turn_task"] = asyncio.create_task(auto_stand(room, idx))

async def act_stand(room, uid):
    idx = room["cur"]
    p = room["players"][idx]
    k = hkey(p)
    p[f"done{k}"] = True
    ctask(room, "turn_task")
    if not await _advance_after_hand(room, idx):
        await bcast(room, state_msg(room))
        await next_turn(room)

async def act_split(room, uid):
    idx = room["cur"]
    p = room["players"][idx]
    if p.get("split") or len(p["hand"]) != 2:
        await send_uid(uid, {"type": "error", "msg": "cant_split"})
        return
    if await db_bal(uid) < p["bet"]:
        await send_uid(uid, {"type": "error", "msg": "no_balance_double"})
        return
    await db_add(uid, -p["bet"])
    second_card = p["hand"].pop()
    p["hand2"] = [second_card, room["deck"].pop()]
    p["hand"].append(room["deck"].pop())
    p["split"] = True
    p["bet2"] = p["bet"]
    p["active_hand"] = 1
    ctask(room, "turn_task")
    await bcast(room, state_msg(room))
    if htot(p["hand"]) > 21:
        await _after_bust(room, uid, idx)
    else:
        room["turn_task"] = asyncio.create_task(auto_stand(room, idx))

async def act_double(room, uid):
    idx = room["cur"]
    p   = room["players"][idx]
    k   = hkey(p)
    bet_field = f"bet{k}"
    if await db_bal(uid) < p[bet_field]:
        await send_uid(uid, {"type": "error", "msg": "no_balance_double"})
        return
    ctask(room, "turn_task")
    await db_add(uid, -p[bet_field])
    p[bet_field] *= 2
    p[f"doubled{k}"] = True
    p[f"hand{k}"].append(room["deck"].pop())
    if htot(p[f"hand{k}"]) > 21:
        await _after_bust(room, uid, idx)
    else:
        p[f"done{k}"] = True       # forced stand after double
        if not await _advance_after_hand(room, idx):
            await bcast(room, state_msg(room))
            await next_turn(room)

async def act_swap(room, uid):
    if not await db_is_vip(uid):
        await send_uid(uid, {"type": "error", "msg": "vip_only"})
        return
    idx = room["cur"]
    p   = room["players"][idx]
    k   = hkey(p)
    p[f"hand{k}"][-1] = room["deck"].pop()
    ctask(room, "turn_task")
    await bcast(room, state_msg(room))
    if htot(p[f"hand{k}"]) > 21:
        p[f"done{k}"] = True
        await bcast(room, {"type": "bust", "uid": uid})
        if not await _advance_after_hand(room, idx):
            await next_turn(room)
    else:
        room["turn_task"] = asyncio.create_task(auto_stand(room, idx))

async def vip_expire(room, idx):
    """VIP swap window expired — auto-end turn (or move to the second hand)."""
    try:
        await asyncio.sleep(VIP_SWAP_TIME)
        if room["state"] == "playing" and room["cur"] == idx:
            p = room["players"][idx]
            p[f"done{hkey(p)}"] = True
            if not await _advance_after_hand(room, idx):
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

    def resolve(hand, bet, allow_bj):
        pt = htot(hand)
        bust = pt > 21
        if bust and dl_bust:
            return "push", bet
        if bust:
            return "bust", 0
        if allow_bj and is_bj(hand) and not is_bj(room["dealer"]):
            return "blackjack", int(bet * 2.5)
        if dl_bust or pt > dl:
            return "win", bet * 2
        if pt == dl:
            return "push", bet
        return "lose", 0

    for p in room["players"]:
        is_split = p.get("split", False)
        p["result"], p["win"] = resolve(p["hand"], p["bet"], allow_bj=not is_split)
        total_win = p["win"]
        total_bet = p["bet"]
        if is_split:
            p["result2"], p["win2"] = resolve(p["hand2"], p["bet2"], allow_bj=False)
            total_win += p["win2"]
            total_bet += p["bet2"]

        if total_win: await db_add(p["uid"], total_win)
        # NB: a split round must count as ONE game for win%, not two — otherwise
        # w_bj/g_bj gets skewed every time someone splits (that was the reported
        # "win% shows wrong" bug). Net the two sub-hands into a single outcome.
        if is_split:
            if total_win > total_bet:   overall = "win"
            elif total_win < total_bet: overall = "lose"
            else:                       overall = "push"
            await db_stats(p["uid"], overall)
        else:
            await db_stats(p["uid"], p["result"])
        await db_add_history(p["uid"], total_bet, p["result"] if not is_split else "split", total_win - total_bet)
        results.append({
            "uid":     p["uid"],
            "result":  p["result"],
            "win":     p["win"],
            "result2": p.get("result2"),
            "win2":    p.get("win2", 0),
            "split":   is_split,
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
        f"SELECT uid, name, bal, w_bj, l_bj, equipped_frame FROM users ORDER BY {col} DESC LIMIT 20")
    return {
        "type": "leaderboard",
        "mode": mode,
        "rows": [{"uid": r["uid"], "name": r["name"], "bal": r["bal"],
                  "wins": r["w_bj"], "losses": r["l_bj"], "frame": r["equipped_frame"] or "none"} for r in rows],
    }

# ── DISCONNECT CLEANUP ────────────────────────────────────────────────────────
async def _on_disconnect(uid):
    rid = in_room.pop(uid, None)
    if not rid:
        return
    if isinstance(rid, str) and rid.startswith("duel:"):
        duel_id = int(rid.split(":", 1)[1])
        room = duel_rooms.get(duel_id)
        if room and not room["finished"]:
            room["players"][uid]["connected"] = False
            if room["started"]:
                room["players"][uid]["done"] = True   # treat disconnect as a stand, not a stall
                await _duel_maybe_finish(room)
            else:
                asyncio.create_task(_duel_refund_if_abandoned(duel_id))
        return
    if rid not in rooms:
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

# ── CLEANUP TASK: удаляет сессии которые не поллили > 30 сек ─────────────────
async def cleanup_loop():
    while True:
        await asyncio.sleep(30)
        stale = [uid for uid, ts in list(last_seen.items())
                 if time.time() - ts > 30]
        for uid in stale:
            last_seen.pop(uid, None)
            event_store.pop(uid, None)
            poll_waiter.pop(uid, None)
            sid_to_del = [s for s, u in sessions.items() if u == uid]
            for s in sid_to_del:
                sessions.pop(s, None)
            await _on_disconnect(uid)
            log.info(f"Cleaned stale session uid={uid}")

# ── AUTH HANDLER (POST /auth) ─────────────────────────────────────────────────
async def auth_handler(req):
    """Первый запрос: проверяет initData, возвращает session_id + профиль."""
    try:
        d = await req.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)

    ud = validate_init_data(d.get("init_data", ""))
    if not ud:
        return web.json_response({"error": "auth_failed"}, status=401)

    uid = ud["id"]
    u   = await db_user(uid)
    if not u:
        return web.json_response({"error": "user_not_found"}, status=404)

    sid = uuid.uuid4().hex
    sessions[sid]    = uid
    last_seen[uid]   = time.time()
    event_store[uid] = []

    return web.json_response({
        "session_id": sid,
        "uid":     uid,
        "name":    u["name"],
        "photo":   ud.get("photo_url", ""),
        "balance": u["bal"],
        "diamonds": u.get("bot_stars", 0),
        "lang":    u["lang"] or "en",
        "vip":     await db_is_vip(uid),
        "skin":    u.get("equipped_skin") or "classic",
        "frame":   u.get("equipped_frame") or "none",
        "quick_replies": await db_quick_replies(uid),
    })

# ── POLL HANDLER (GET /poll?sid=...) ──────────────────────────────────────────
async def poll_handler(req):
    """Long-poll: держит запрос до 8 сек, возвращает накопившиеся события."""
    sid = req.query.get("sid", "")
    uid = sessions.get(sid)
    if not uid:
        return web.json_response({"error": "invalid_session"}, status=401)

    last_seen[uid] = time.time()

    # Если событий нет — ждём до 8 сек
    if not event_store.get(uid):
        ev = asyncio.Event()
        poll_waiter[uid] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=8)
        except asyncio.TimeoutError:
            pass
        poll_waiter.pop(uid, None)

    events = list(event_store.get(uid, []))
    event_store[uid] = []
    return web.json_response({"events": events})

# ── ACTION HANDLER (POST /action) ─────────────────────────────────────────────
async def action_handler(req):
    """Принимает действия игрока. Авторизация по session_id."""
    try:
        d = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    sid = d.get("sid", "")
    uid = sessions.get(sid)
    if not uid:
        return web.json_response({"ok": False, "error": "invalid_session"}, status=401)

    last_seen[uid] = time.time()
    await pool.execute("UPDATE users SET last_seen=$1 WHERE uid=$2", int(time.time()), uid)
    act  = d.get("action", "")
    u    = await db_user(uid)
    room = rooms.get(in_room.get(uid))

    try:
        # ── SHOP ──────────────────────────────────────────────────────────────
        if act == "get_shop":
            await send_uid(uid, await shop_payload(uid))

        elif act == "buy_cosmetic":
            # FIX (was reported as "can't equip the gold frame"): the old code
            # guessed skin-vs-frame by checking `code in CARD_SKINS` first, so
            # a frame code that happened to also exist as a skin code (e.g. the
            # legacy "gold") silently got bought/equipped as the wrong kind.
            # The client now sends an explicit "kind", which is authoritative.
            kind = str(d.get("kind", ""))
            code = str(d.get("code", ""))
            catalog = CARD_SKINS if kind == "skin" else (PROFILE_FRAMES if kind == "frame" else None)
            if catalog is None or code not in catalog:
                await send_uid(uid, {"type": "error", "msg": "unknown_item"})
            elif catalog[code].get("referral_only"):
                await send_uid(uid, {"type": "error", "msg": "referral_only"})
            else:
                price = catalog[code]["price"]
                owned = await db_owned(uid)
                if code in owned or price == 0:
                    await send_uid(uid, {"type": "error", "msg": "already_owned"})
                else:
                    diamonds = await db_stars(uid)
                    if diamonds < price:
                        await send_uid(uid, {"type": "error", "msg": "not_enough_diamonds",
                                             "have": diamonds, "need": price})
                    else:
                        await db_add_stars(uid, -price)
                        await db_grant(uid, code)
                        await send_uid(uid, await shop_payload(uid))

        elif act == "equip_cosmetic":
            kind = str(d.get("kind", ""))
            code = str(d.get("code", ""))
            if kind == "skin" and code in CARD_SKINS:
                owned = await db_owned(uid)
                if code != "classic" and code not in owned:
                    await send_uid(uid, {"type": "error", "msg": "not_owned"})
                else:
                    await db_equip(uid, "skin", code)
                    await send_uid(uid, await shop_payload(uid))
            elif kind == "frame" and code in PROFILE_FRAMES:
                owned = await db_owned(uid)
                if code != "none" and code not in owned:
                    await send_uid(uid, {"type": "error", "msg": "not_owned"})
                else:
                    await db_equip(uid, "frame", code)
                    await send_uid(uid, await shop_payload(uid))
            else:
                await send_uid(uid, {"type": "error", "msg": "unknown_item"})

        elif act == "buy_vip":
            if await db_is_vip(uid):
                await send_uid(uid, {"type": "error", "msg": "already_owned"})
            else:
                diamonds = await db_stars(uid)
                if diamonds < VIP_PRICE_DIAMONDS:
                    await send_uid(uid, {"type": "error", "msg": "not_enough_diamonds",
                                         "have": diamonds, "need": VIP_PRICE_DIAMONDS})
                else:
                    await db_add_stars(uid, -VIP_PRICE_DIAMONDS)
                    await pool.execute("UPDATE users SET vip_perm=TRUE WHERE uid=$1", uid)
                    await send_uid(uid, await shop_payload(uid))

        elif act == "buy_chips":
            pack = CHIP_PACKS.get(str(d.get("pack", "")))
            if not pack:
                await send_uid(uid, {"type": "error", "msg": "unknown_item"})
            else:
                diamonds = await db_stars(uid)
                if diamonds < pack["price"]:
                    await send_uid(uid, {"type": "error", "msg": "not_enough_diamonds",
                                         "have": diamonds, "need": pack["price"]})
                else:
                    await db_add_stars(uid, -pack["price"])
                    await db_add(uid, pack["chips"])
                    await send_uid(uid, {"type": "chips_bought", "chips": pack["chips"],
                                         "balance": await db_bal(uid),
                                         "diamonds": await db_stars(uid)})

        elif act == "get_current_room":
            # Lets a reconnecting client (mini-app reopened/reloaded mid-round)
            # find out it's still seated somewhere, instead of being stuck on
            # the main menu until some unrelated event happens to fire next.
            if room:
                await send_uid(uid, state_msg(room))
            else:
                await send_uid(uid, {"type": "state", "room_id": None, "state": "none", "players": []})

        elif act == "get_leaderboard":
            mode = d.get("mode", "balance")
            if mode not in ("balance", "wins"): mode = "balance"
            await send_uid(uid, await leaderboard_payload(mode))

        elif act == "get_profile":
            try:
                target = int(d.get("target_uid", uid))
            except Exception:
                target = uid
            prof = await db_profile(target, viewer_uid=uid)
            await send_uid(uid, prof or {"type": "error", "msg": "unknown_item"})

        elif act == "send_friend_request":
            try: target = int(d.get("target_uid", 0))
            except Exception: target = 0
            if not target or target == uid:
                await send_uid(uid, {"type": "error", "msg": "unknown_item"})
            else:
                status = await db_send_friend_request(uid, target)
                if status == "friends":
                    try:
                        tlang = "ru"
                        await send_telegram_message(target,
                            f"🤝 Вы с {(await db_user(uid) or {}).get('name','игроком')} теперь друзья в Blackjack!")
                    except Exception:
                        pass
                else:
                    try:
                        me = await db_user(uid)
                        await send_telegram_message(target,
                            f"👥 <b>{(me or {}).get('name','Игрок')}</b> хочет добавить вас в друзья в Blackjack! "
                            f"Открой мини-апп → Друзья, чтобы принять или отклонить.")
                    except Exception:
                        pass
                await send_uid(uid, {"type": "friend_status", "target_uid": target, "status": status})

        elif act == "respond_friend_request":
            try: target = int(d.get("target_uid", 0))
            except Exception: target = 0
            accept = bool(d.get("accept"))
            ok = await db_respond_friend_request(uid, target, accept)
            new_status = await db_friend_status(uid, target) if ok else "none"
            await send_uid(uid, {"type": "friend_status", "target_uid": target,
                                 "status": new_status if ok else "error"})

        elif act == "remove_friend":
            try: target = int(d.get("target_uid", 0))
            except Exception: target = 0
            await db_remove_friend(uid, target)
            await send_uid(uid, {"type": "friend_status", "target_uid": target, "status": "none"})

        elif act == "get_friends":
            friends = await db_friends_list(uid)
            incoming = await db_incoming_requests(uid)
            await send_uid(uid, {
                "type": "friends_data",
                "friends": [{"uid": r["uid"], "name": r["name"], "frame": r["equipped_frame"] or "none",
                            "last_seen_txt": fmt_last_seen(r["last_seen"])} for r in friends],
                "incoming": [{"uid": r["uid"], "name": r["name"], "frame": r["equipped_frame"] or "none"} for r in incoming],
            })

        elif act == "find_friend":
            query = str(d.get("query", ""))[:64]
            found = await db_find_user(query)
            if not found:
                await send_uid(uid, {"type": "error", "msg": "user_not_found"})
            elif found["uid"] == uid:
                await send_uid(uid, {"type": "error", "msg": "cant_add_self"})
            else:
                status = await db_friend_status(uid, found["uid"])
                await send_uid(uid, {"type": "find_friend_result", "uid": found["uid"],
                                      "name": found["name"], "frame": found["equipped_frame"] or "none",
                                      "status": status})

        elif act == "invite_friend_to_play":
            try: target = int(d.get("target_uid", 0))
            except Exception: target = 0
            if _invite_rate_limited(uid):
                await send_uid(uid, {"type": "error", "msg": "invite_rate_limited"})
            else:
                status = await db_friend_status(uid, target)
                if status != "friends":
                    await send_uid(uid, {"type": "error", "msg": "not_friends"})
                else:
                    me = await db_user(uid)
                    room_id = in_room.get(uid)
                    try:
                        await send_telegram_invite(target, (me or {}).get("name", "Друг"), room_id)
                        await send_uid(uid, {"type": "invite_sent", "target_uid": target})
                    except Exception:
                        await send_uid(uid, {"type": "error", "msg": "network"})

        elif act == "create_gift_invoice":
            try:
                target = int(d.get("target_uid", 0))
                pack = str(d.get("pack", ""))
            except Exception:
                target, pack = 0, ""
            status = await db_friend_status(uid, target)
            if status != "friends":
                await send_uid(uid, {"type": "error", "msg": "not_friends"})
            elif pack not in GIFT_DIAMOND_PACKS:
                await send_uid(uid, {"type": "error", "msg": "unknown_item"})
            else:
                amount, tg_cost = GIFT_DIAMOND_PACKS[pack]
                try:
                    url = await create_gift_invoice_link(amount, tg_cost, target, uid)
                    await send_uid(uid, {"type": "gift_invoice", "url": url})
                except Exception as e:
                    log.warning(f"gift invoice error: {e}")
                    await send_uid(uid, {"type": "error", "msg": "network"})

        elif act == "set_quick_replies":
            phrases = d.get("phrases", [])
            if not isinstance(phrases, list):
                await send_uid(uid, {"type": "error", "msg": "bad_request"})
            else:
                saved = await db_set_quick_replies(uid, phrases)
                await send_uid(uid, {"type": "quick_replies", "phrases": saved})

        elif act == "react":
            if room:
                now = time.time()
                if now - _last_comment.get(uid, 0) >= COMMENT_COOLDOWN:
                    _last_comment[uid] = now
                    try:
                        target_uid = int(d.get("target_uid", uid))
                    except Exception:
                        target_uid = uid
                    emoji = str(d.get("emoji", ""))[:8]
                    in_seat = any(p["uid"] == target_uid for p in room["players"])
                    if emoji and in_seat:
                        await bcast(room, {"type": "reaction", "from_uid": uid,
                                           "target_uid": target_uid, "emoji": emoji})

        # ── JOIN ──────────────────────────────────────────────────────────────
        elif act == "leave":
            await _on_disconnect(uid)

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

        elif act == "join_duel":
            try: duel_id = int(d.get("duel_id", 0))
            except Exception: duel_id = 0
            drow = await pool.fetchrow("SELECT * FROM duels WHERE id=$1 AND status='accepted'", duel_id)
            if not drow or uid not in (drow["creator_uid"], drow["opponent_uid"]):
                await send_uid(uid, {"type": "error", "msg": "duel_not_found"})
            else:
                room = duel_rooms.get(duel_id)
                if not room:
                    room = _new_duel_room(duel_id, drow["creator_uid"], drow["creator_name"],
                                           drow["opponent_uid"], drow["opponent_name"], drow["bet"])
                    duel_rooms[duel_id] = room
                room["players"][uid]["connected"] = True
                in_room[uid] = f"duel:{duel_id}"
                if not any(t for t in (room.get("_abandon_task"),) if t):
                    room["_abandon_task"] = asyncio.create_task(_duel_refund_if_abandoned(duel_id))
                both = all(p["connected"] for p in room["players"].values())
                if both and not room["started"]:
                    room["started"] = True
                    for p in room["players"].values():
                        p["hand"] = [room["deck"].pop(), room["deck"].pop()]
                await _duel_broadcast(room)

        elif act == "duel_hit":
            try: duel_id = int(d.get("duel_id", 0))
            except Exception: duel_id = 0
            room = duel_rooms.get(duel_id)
            if not room or uid not in room["players"]:
                await send_uid(uid, {"type": "error", "msg": "duel_not_found"})
            elif room["started"] and not room["finished"] and not room["players"][uid]["done"]:
                p = room["players"][uid]
                if room["deck"]:
                    p["hand"].append(room["deck"].pop())
                if htot(p["hand"]) > 21:
                    p["done"] = True
                await _duel_broadcast(room)
                await _duel_maybe_finish(room)

        elif act == "duel_stand":
            try: duel_id = int(d.get("duel_id", 0))
            except Exception: duel_id = 0
            room = duel_rooms.get(duel_id)
            if not room or uid not in room["players"]:
                await send_uid(uid, {"type": "error", "msg": "duel_not_found"})
            elif room["started"] and not room["finished"] and not room["players"][uid]["done"]:
                room["players"][uid]["done"] = True
                await _duel_broadcast(room)
                await _duel_maybe_finish(room)

        elif act == "ready_skip":
            if room and room["state"] == "lobby":
                room["ready_votes"].add(uid)
                await bcast(room, state_msg(room))
                await maybe_skip_lobby(room)

        elif act in ("insure", "no_insure"):
            if room and room["state"] == "insurance":
                await player_insurance_choice(room, uid, act == "insure")

        elif act in ("hit", "stand", "double", "split", "swap"):
            if room and room["state"] == "playing":
                idx = room["cur"]
                if idx < len(room["players"]) and room["players"][idx]["uid"] == uid:
                    if act == "hit":    await act_hit(room, uid)
                    elif act == "stand":  await act_stand(room, uid)
                    elif act == "double": await act_double(room, uid)
                    elif act == "split":  await act_split(room, uid)
                    elif act == "swap":   await act_swap(room, uid)
                else:
                    await send_uid(uid, {"type": "error", "msg": "not_your_turn"})

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

async def create_gift_invoice_link(amount, tg_cost, target_uid, payer_uid):
    """Creates a real Telegram Stars invoice link for gifting `amount` diamonds
    to target_uid, paid for by payer_uid. Opened client-side via
    Telegram.WebApp.openInvoiceLink(). Payment completion (successful_payment)
    is handled by the BOT process (bj.py), which credits the recipient —
    webapp_server.py only ever creates the link, it never sees the payment
    itself since Stars payments are bot-native."""
    payload = f"bjgift_{amount}_{target_uid}_{payer_uid}"
    body = {
        "title": f"🎁 {amount}💎 gift",
        "description": "Gift diamonds to a friend in Blackjack",
        "payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": [{"label": f"{amount}💎", "amount": tg_cost}],
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
        async with s.post(f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink", json=body) as r:
            data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "createInvoiceLink failed"))
    return data["result"]

async def send_telegram_message(uid, text):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
        await s.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                     json={"chat_id": uid, "text": text, "parse_mode": "HTML"})

async def send_telegram_invite(target_uid, from_name, room_id):
    row = await pool.fetchrow("SELECT mute_invites_until FROM users WHERE uid=$1", target_uid)
    if row and row["mute_invites_until"] and row["mute_invites_until"] > time.time():
        return   # this player muted invite pings for now — don't send
    rows = []
    if WEBAPP_PUBLIC_URL:
        rows.append([{"text": "🎲 Присоединиться", "web_app": {"url": WEBAPP_PUBLIC_URL}}])
    rows.append([{"text": "🔕 Не беспокоить 1 день", "callback_data": "muteinv_1d"}])
    text = f"🎮 <b>{from_name}</b> приглашает вас сыграть в Blackjack!"
    payload = {"chat_id": target_uid, "text": text, "parse_mode": "HTML",
               "reply_markup": {"inline_keyboard": rows}}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
        await s.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)

async def avatar_handler(req):
    """Proxy a player's current Telegram profile photo. 404 if unavailable
    (private settings, no photo, etc) — client falls back to an initials avatar.

    Cached in-process: previously every single <img> request re-did 3
    sequential Telegram API calls with no caching at all, which is slow and,
    under a flaky mobile connection, prone to timing out — the likely cause
    of the reported "avatar sometimes doesn't show" bug. Now a hit is served
    straight from memory for AVATAR_TTL seconds, and even a miss (no public
    photo) is cached briefly so a bad network blip doesn't retry-storm."""
    try:
        uid = int(req.match_info.get("uid", ""))
    except Exception:
        return web.Response(status=400)

    cached = _avatar_cache.get(uid)
    if cached:
        img, ctype, fetched_at, is_miss = cached
        ttl = AVATAR_MISS_TTL if is_miss else AVATAR_TTL
        if time.time() - fetched_at < ttl:
            if is_miss:
                return web.Response(status=404)
            return web.Response(body=img, content_type=ctype,
                                 headers={"Cache-Control": "public, max-age=1800"})

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos",
                              params={"user_id": uid, "limit": 1}) as r:
                data = await r.json()
            photos = (data.get("result") or {}).get("photos") or []
            if not photos:
                _avatar_cache[uid] = (None, None, time.time(), True)
                return web.Response(status=404)
            file_id = photos[0][-1]["file_id"]
            async with s.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                              params={"file_id": file_id}) as r:
                fdata = await r.json()
            file_path = (fdata.get("result") or {}).get("file_path")
            if not file_path:
                _avatar_cache[uid] = (None, None, time.time(), True)
                return web.Response(status=404)
            async with s.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}") as r:
                img = await r.read()
                ctype = r.headers.get("Content-Type", "image/jpeg")
        _avatar_cache[uid] = (img, ctype, time.time(), False)
        return web.Response(body=img, content_type=ctype,
                             headers={"Cache-Control": "public, max-age=1800"})
    except Exception as e:
        log.warning(f"avatar fetch failed uid={uid}: {e}")
        # Serve a stale cached copy rather than nothing, if we have one at all
        if cached and not cached[3]:
            return web.Response(body=cached[0], content_type=cached[1],
                                 headers={"Cache-Control": "public, max-age=300"})
        return web.Response(status=404)

# ── LIFECYCLE ─────────────────────────────────────────────────────────────────
async def on_startup(app):
    global pool
    asyncio.create_task(cleanup_loop())
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    log.info(f"DB connected. Listening on :{PORT}")

async def on_cleanup(app):
    if pool: await pool.close()

def main():
    app = web.Application()
    app.router.add_get("/",              index_handler)
    app.router.add_get("/avatar/{uid}",  avatar_handler)
    app.router.add_post("/auth",         auth_handler)   # initData → session_id
    app.router.add_get("/poll",          poll_handler)   # long-poll события
    app.router.add_post("/action",       action_handler) # действия игрока
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)

if __name__ == "__main__":
    main()
