#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Blackjack Telegram Bot — aiogram 3.x + PostgreSQL (asyncpg)
Group-only play, VIP-only swap, subscription-gated bonus/promo,
language+ToS onboarding, dealer slightly weaker, full admin economy tools.
pip install aiogram asyncpg
ENV: BOT_TOKEN  CREATOR_ID  DATABASE_URL  CHANNEL_USERNAME
"""
import asyncio, random, time, datetime, logging, os, sys, html, json
from typing import Optional
from urllib.parse import quote

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, PreCheckoutQuery, LabeledPrice,
    InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB,
    ReplyKeyboardMarkup as RKM, KeyboardButton as KB,
    ReplyKeyboardRemove, BufferedInputFile, FSInputFile, WebAppInfo)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# ── CONFIG ─────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]  # must be set in Railway Variables — no hardcoded fallback
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
CREATOR_ID       = int(os.getenv("CREATOR_ID", "6714200331"))
CREATOR_UN       = "alexplaay"
DATABASE_URL     = os.getenv("DATABASE_URL", "postgresql://localhost/bjbot")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@BJonlinec")   # e.g. "@mychannel" — required to claim bonus/promo
MIN_BET, MAX_BET, TURN_TIME = 100, 1_000_000, 30
LOBBY_WAIT          = 60
VIP_BUST_SWAP_TIME  = 30
START_BAL = 10_000
BONUS_VIP, BONUS_NORM = 5_000, 2_500
BONUS_MAX_BAL = 50_000   # balance above this — daily bonus is not given
REF_VIP_STARS  = 5      # 💎 diamonds for VIP referrer per invite
REF_NORM_STARS = 3      # 💎 diamonds for regular referrer per invite
REF_NEW_BAL  = 5_000    # chips for the invited friend
TRANSFER_FEE_PCT = 10
PM_CID = -999_999_999
REPORT_MIN_WORDS = 5

# ── SHOP ───────────────────────────────────────────────────────────────────
# Section 1: pay REAL Telegram Stars (⭐, XTR) → receive 💎 diamonds (bigger packs = better rate)
STAR_PACKS = {
    "sp1": ("💎 50",   50,   50),
    "sp2": ("💎 110",  100,  110),
    "sp3": ("💎 280",  250,  280),
    "sp4": ("💎 550",  500,  550),
    "sp5": ("💎 1200", 1000, 1200),
}  # code: (label, tg_stars_cost, bot_stars_received)

# Section 2: pay 💎 diamonds (internal currency, no real payment) → VIP or chips
SHOP_ITEMS = {
    "vipp":  ("👑 VIP Навсегда",           119, "vip",   -1,        None),
    "top1":  ("💰 100 000¢",               49,  "chips", 100_000,   None),
    "top2":  ("💰 210 000¢",               99,  "chips", 210_000,   None),
    "top3":  ("💰 550 000¢",               249, "chips", 550_000,   None),
    "top4":  ("💰 1 150 000¢",             499, "chips", 1_150_000, None),
    "top5":  ("💰 2 500 000¢",             999, "chips", 2_500_000, None),
}

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bj.log","a","utf-8")])
log = logging.getLogger("BJ")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()
pool: Optional[asyncpg.Pool] = None

_user_state: dict = {}   # uid -> "promo" | "report" | "settos_ru" | "settos_en"
_report_words: dict = {}  # uid -> accumulated text while composing a report (not used, single msg)

# ── TRANSLATIONS ───────────────────────────────────────────────────────────
LANGS = {"en": "English 🇬🇧", "ru": "Русский 🇷🇺"}
TX = {}

TX['en'] = {
    'choose_lang': '🌍 Choose your language:',
    'tos_title': '📜 <b>Terms of Service</b>',
    'tos_default': 'By using this bot you agree to play responsibly. Chips have no real-world value.',
    'tos_accept_btn': '✅ I agree',
    'welcome': '🃏 <b>Welcome to Blackjack Bot!</b>\n💰 Starting balance: <b>{bal:,}¢</b>\n\n⚠️ Works only in <b>groups</b> — add me to a group chat to play.',
    'lang_ok': '✅ Language: <b>English</b>',
    'help': '📚 <b>Blackjack Bot — Help</b>\n\nMost commands work as a /slash or as a plain word in a group chat (e.g. <code>/play</code> = <code>play</code>).\n\n<b>🃏 Blackjack</b> (groups only)\n/play — pick a bet and join the table\n/go — start the table now, skip the wait\n/bet &lt;amount&gt; — join with a custom bet\n/hit — draw a card\n/stand — stop drawing\n/double — double your bet for one more card\n/swap — replace your last card (👑 VIP only)\n/cancel — cancel the table, bets are returned (admins only)\n\n<b>🎲 Card Toss</b>\n/cards or /toss — reply to someone\'s message to toss against them\nHigher card level than theirs = guaranteed win, lower = guaranteed loss, equal = coin flip. Loser gets nothing.\n/upgrade — spend 🪙 tokens won from tosses to level up your card\n\n<b>👤 Account</b>\n/profile — balance, VIP status, game stats\n/bonus — daily reward (channel subscribers only, balance under 50,000¢)\n/top — leaderboard by balance, wins or win%\n/ref — your invite link, +3💎 per friend (+5💎 for 👑 VIP)\n/transfer @user &lt;amount&gt; — send chips, 10% fee (free for 👑 VIP)\n/shop — buy diamonds with ⭐, then VIP/chips with 💎 (PM only)\n/vip — VIP perks and price\n/promo — redeem a promo code\n/settings — language, rules, groups, support',
    'profile': '👤 {mention}\n💰 Balance: <b>{bal:,}¢</b>\n💎 Diamonds: <b>{bs:,}</b>\n{vip_line}\n\n📊 Wins <b>{bw}</b> · Losses <b>{bl}</b> · Games <b>{bg}</b> · Win rate <b>{pct}%</b>',
    'vip_active': '👑 VIP until <b>{d}</b> ({left} left)',
    'vip_perm': '👑 VIP: <b>forever ♾️</b>',
    'no_vip': '⚪ No VIP — /shop',
    'vip_info': '👑 <b>VIP</b>\n\n🎁 Daily bonus: <b>5,000¢</b> (vs 2,500¢)\n👑 Crown next to your name\n🔄 Card swap unlocked\n💸 No transfer fee (vs 10%)\n🔗 Referral bonus: <b>+5💎</b> per invite (vs +3💎)\n\n🛍 <b>Price</b>\nForever — <b>119💎</b>\n\n/shop',
    'bonus_ok': '🎁 Daily bonus +<b>{a:,}¢</b>\nBalance: <b>{b:,}¢</b>',
    'bonus_wait': '⏳ Already claimed today. Next in <b>{t}</b>.',
    'bonus_too_rich': '❌ Bonus is only available with a balance under <b>{m:,}¢</b>.',
    'need_sub': '⚠️ Subscribe to our channel to claim this.',
    'ref_msg': '🔗 <b>Invite friends</b>\n\nYou get <b>+3💎</b> per invite (👑 VIP: +5💎).\nYour friend gets <b>+5,000¢</b> too.\n\nTap below to share your link:',
    'ref_done': '🎉 Welcome bonus +<b>{a:,}¢</b>!',
    'ref_rwd_vip': '🎉 <b>{name}</b> joined via your link! +<b>5💎</b>',
    'ref_rwd_norm': '🎉 <b>{name}</b> joined via your link! +<b>3💎</b>',
    'top_title': '🏆 <b>Top 15 — {mode}</b>\n\n{lines}',
    'top_title_grp': '🏆 <b>Top 15 in this group — {mode}</b>\n\n{lines}',
    'top_bal_lbl': '💰 Balance',
    'top_win_lbl': '🏆 Wins',
    'top_pct_lbl': '📈 Win%',
    'top_line_bal': '<b>{i}.</b> {m}  —  <b>{v:,}¢</b>',
    'top_line_win': '<b>{i}.</b> {m}  —  <b>{v}</b> 🏆',
    'top_line_pct': '<b>{i}.</b> {m}  —  <b>{v}%</b>  ({g})',
    'top_empty': 'No players yet.',
    'shop': '🛍 <b>Shop</b>\n\nTwo sections:\n💎 <b>Buy diamonds</b> with real Telegram Stars ⭐\n🛍 <b>Buy VIP / chips</b> with your diamonds 💎',
    'shop_stars_title': '💎 <b>Buy diamonds</b>\n\nPay with real Telegram Stars ⭐ — bigger packs give a better rate.',
    'shop_items_title': '🛍 <b>VIP & Chips</b>\n\nPay with your 💎 diamonds balance: <b>{bs:,}💎</b>',
    'not_enough_stars': '❌ Not enough 💎 diamonds. You have <b>{have:,}💎</b>, need <b>{need:,}💎</b>.\nBuy more with real Telegram Stars via the shop.',
    'shop_pm_only': '🛍 Shop is only available in PM. Tap below to open it:',
    'confirm': '✅ <b>Confirm purchase</b>\n\n{item}\n<b>{stars}⭐</b>',
    'confirm_stars': '✅ <b>Confirm purchase</b>\n\n{item}\n<b>{stars}💎</b>',
    'pay_ok': '✅ Purchase confirmed — <b>{item}</b> is active.',
    'pay_ok_stars': '✅ <b>+{amt:,}💎</b> diamonds added to your balance!',
    'pay_fail': '❌ Payment failed. Try again, or contact support below.',
    'pay_fail_cr': '⚠️ Payment error from <code>{uid}</code> ({name}), item: <b>{item}</b>.',
    'lobby': '🃏 <b>Table #{n}</b>\n⏱ Starts in <b>{t}s</b> · Players: <b>{cnt}</b>\n\n{players}\n\nJoin: /bet &lt;amount&gt;  ·  Force start: /go',
    'game_start': '🃏 <b>Table #{n}</b> — dealing…',
    'dlabel': 'DEALER',
    'your_turn': '🎮 <b>Your move, {mention}</b>\n\n{board}\n\n⏱ <b>{secs}s</b> to act',
    'auto_stand': '⏱ Time\'s up — auto stand.',
    'd_reveals': '🃏 Dealer reveals hidden card…',
    'd_hits': '🃏 Dealer draws <b>{c}</b>  [{t}]',
    'd_stands': '🛑 Dealer stands at <b>[{t}]</b>',
    'd_busts': '💥 Dealer busts <b>[{t}]</b>!',
    'results': '🏁 <b>Table #{n} — Results</b>\n\n{dlr}\n\n{lines}',
    'r_bj': '🎰 {m}  Blackjack!\n{h}  +<b>{w:,}¢</b>',
    'r_win': '🏆 {m}  Win\n{h}  +<b>{w:,}¢</b>',
    'r_push': '🤝 {m}  Push\n{h}  returned <b>{b:,}¢</b>',
    'r_lose': '❌ {m}  Loss\n{h}  −<b>{b:,}¢</b>',
    'r_bust': '💥 {m}  Bust\n{h}  −<b>{b:,}¢</b>',
    'swap_ok_vip': '🔄 Card swapped (👑 VIP)',
    'swap_vip_only': '❌ Card swap is 👑 VIP-only.\nGet /vip to unlock it.',
    'swap_first': '❌ Draw a card first (/hit), then you can swap.',
    'vip_bust_swap': '💥 Bust!  {board}\n\n👑 VIP: <b>{secs}s</b> to swap your last card.',
    'stood': '🛑 {mention} stands.',
    'not_ur': '⚠️ Not your turn.',
    'no_game': '❌ No active game. /play to start.',
    'no_insurance': '❌ No insurance offer active right now.',
    'insurance_offer': '🛡 <b>Dealer shows an Ace!</b>\n\nInsurance is available — costs <b>half your bet</b>, pays 2:1 if the dealer has Blackjack.\n\nType /insure to take it or /noinsure to decline. ({secs}s)',
    'insurance_taken': '🛡 {mention} took insurance.',
    'insurance_declined': '❌ {mention} declined insurance.',
    'insurance_win': '🛡 {mention} insurance paid off: <b>+{w:,}¢</b>',
    'insurance_lose': '🛡 {mention} insurance lost: <b>-{l:,}¢</b>',
    'dealer_bj_reveal': '🃏 Dealer has <b>Blackjack!</b> Hands settle now.',
    'in_game': '⚠️ You\'re already at a table.',
    'bet_low': '❌ Minimum bet: <b>{m:,}¢</b>',
    'bet_high': '❌ Maximum bet: <b>{m:,}¢</b>',
    'no_bal': '❌ Not enough chips. Balance: <b>{b:,}¢</b>',
    'dbl_low': '❌ Need <b>{n:,}¢</b> more to double.',
    'doubled': '✅ Bet doubled to <b>{b:,}¢</b> — one card…',
    'joined': '✅ Joined table <b>#{n}</b>  (bet <b>{bet:,}¢</b>)',
    'play_choose': '🃏 Pick your bet\nBalance: <b>{bal:,}¢</b>\n\nTap a button or /bet &lt;amount&gt;:',
    'pm_no_play': '⚠️ Playing works only in <b>groups</b>. Add me to a group chat!',
    'settings': '⚙️ <b>Settings</b>',
    'report_ask': '📝 Describe your issue or idea (min <b>{n} words</b>):',
    'report_short': '❌ Please write at least <b>{n} words</b>.',
    'report_sent': '✅ Sent — thanks!',
    'report_recv': '📩 <b>Report</b> from {mention} (<code>{uid}</code>):\n\n{text}',
    'cancel_ok': '🚫 Game cancelled, bets returned.',
    'cancel_none': '❌ No active game here.',
    'cancel_not_adm': '⚠️ Only admins can cancel or force-start.',
    'grp_lang_ok': '✅ Group language: <b>{lang}</b>',
    'grp_adm_only': '⚠️ Only group admins can change this.',
    'promo_ask': '🎟 Enter your promo code:',
    'promo_m': '🎁 Promo applied +<b>{a:,}¢</b>\nBalance: <b>{b:,}¢</b>',
    'promo_v': '👑 Promo applied — VIP +<b>{d} days</b>',
    'promo_nf': '❌ Promo not found.',
    'promo_exp': '❌ Promo expired.',
    'promo_nu': '❌ No activations left.',
    'promo_dup': '⚠️ Already used.',
    'tr_ok': '✅ Sent <b>{a:,}¢</b> to {to}\nFee <b>{fee:,}¢</b>  ·  Balance <b>{b:,}¢</b>',
    'tr_ok_vip': '✅ Sent <b>{a:,}¢</b> to {to}\n👑 No fee  ·  Balance <b>{b:,}¢</b>',
    'tr_recv': '💸 {fr} sent you <b>{a:,}¢</b>!\nBalance: <b>{b:,}¢</b>',
    'tr_self': '❌ Can\'t transfer to yourself.',
    'tr_low': '❌ Minimum transfer: <b>100¢</b>',
    'tr_nob': '❌ Not enough chips. Balance: <b>{b:,}¢</b>',
    'tr_nf': '❌ Player not found — they need to start the bot first.',
    'tr_usage': 'ℹ️ /transfer @user &lt;amount&gt;\nor reply to a message + /transfer &lt;amount&gt;\n\nFee: 10%  (👑 VIP: 0%)',
    'pm_only': '⚠️ This only works in PM.',
    'banned': '🚫 Banned until <b>{until}</b>.',
    'btn_hit': '🃏 Hit',
    'btn_stand': '🛑 Stand',
    'btn_double': '✖️ Double',
    'btn_swap': '🔄 Swap',
    'btn_play': '🃏 Play',
    'btn_profile': '👤 Profile',
    'btn_bonus': '🎁 Bonus',
    'btn_shop': '🛍 Shop',
    'btn_shop_stars': '💎 Buy diamonds (for ⭐)',
    'btn_shop_items': '🛍 VIP & Chips (for 💎)',
    'btn_top': '🏆 Top',
    'btn_ref': '🔗 Referral',
    'btn_settings': '⚙️ Settings',
    'btn_lang': '🌍 Language',
    'btn_promo': '🎟 Promo',
    'btn_upgrade': '🆙 Upgrade',
    'btn_upgrade_confirm': '⬆️ Upgrade ({cost} 🪙)',
    'btn_help': '📚 Help',
    'btn_howto': '❓ How to play?',
    'how_to_play': (
        "❓ <b>How to play Blackjack — simple version</b>\n\n"
        "🎯 <b>Goal:</b> get your cards as close to <b>21</b> as possible, without going over. "
        "Beat the dealer's total — that's it.\n\n"
        "🃏 <b>Card values:</b>\n"
        "• Number cards = their number (2-10)\n"
        "• J, Q, K = <b>10</b>\n"
        "• Ace = <b>11</b> or <b>1</b> (whichever helps you more)\n\n"
        "▶️ <b>How a round works:</b>\n"
        "1️⃣ /play and pick a bet (or /go to start early)\n"
        "2️⃣ You and the dealer each get 2 cards. You see both of yours, but only ONE of the dealer's\n"
        "3️⃣ On your turn: /hit to take another card, or /stand to stop\n"
        "4️⃣ Go over 21 = you <b>bust</b> and lose instantly, no matter what the dealer has\n"
        "5️⃣ Once everyone's done, the dealer reveals their hidden card and must hit until 17+\n\n"
        "🏆 <b>Who wins:</b>\n"
        "• Your total closer to 21 than the dealer's (without busting) → you win, get 2× your bet back\n"
        "• Exactly 21 with your first 2 cards = <b>Blackjack</b> → pays 3:2 (bigger win!)\n"
        "• Same total as the dealer → push, you get your bet back\n"
        "• Dealer closer to 21, or you bust → you lose your bet\n\n"
        "🛡 <b>Insurance:</b> if the dealer's showing an Ace, you can /insure for half your bet — "
        "it pays 2:1 if the dealer turns out to have Blackjack.\n\n"
        "💡 Extra moves: /double (double your bet, one more card) and /swap (👑 VIP — swap your last card)."
    ),
    'btn_report': '📝 Support',
    'btn_vip': '👑 VIP',
    'btn_yes': '✅ Confirm',
    'btn_no': '❌ Cancel',
    'btn_back': '◀️ Back',
    'btn_top_bal': '💰 Balance',
    'btn_top_win': '🏆 Wins',
    'btn_top_pct': '📈 Win%',
    'btn_tos': '📜 Rules',
    'btn_groups': '🎮 Groups',
    'btn_support': '🆘 Support',
    'btn_share': '📤 Share link',
    'groups_title': '🎮 <b>Where to play</b>\n\nTap a group to join:',
    'groups_empty': '🎮 No groups yet.',
    'toss_no_reply': '⚠️ Reply to someone\'s message to toss against them.',
    'toss_self': '⚠️ You can\'t toss against yourself.',
    'toss_cooldown': '⏳ Cooldown: <b>{t}</b> left.',
    'toss_limit': '⚠️ Daily limit reached (5/5).',
    'toss_result_win': '🎲 <b>Card Toss</b>\n\n🃏 {a}  vs  🃏 {b}\n\n🏆 <b>{challenger}</b> wins!\n💰 +{chips:,}¢  🪙 +{tokens}',
    'toss_result_loss': '🎲 <b>Card Toss</b>\n\n🃏 {a}  vs  🃏 {b}\n\n🏆 <b>{opponent}</b> wins!\n😔 {challenger} loses. No reward this time.',
    'upgrade_menu': '🆙 <b>Card Upgrade</b>\n\nIn 🎲 Card Toss, higher level than your opponent = guaranteed win. Lower = guaranteed loss. Equal = coin flip.\n\n🃏 Level: <b>{lvl}</b>\n🪙 Tokens: <b>{tokens}</b>\n\nNext level: <b>{cost} 🪙</b>',
    'upgrade_ok': '✅ Card upgraded to level <b>{lvl}</b>!\n🪙 Tokens left: <b>{tokens}</b>',
    'upgrade_no_tokens': '❌ Not enough tokens. Need <b>{cost}</b>, you have <b>{tokens}</b>.',
}

TX['ru'] = {
    'choose_lang': '🌍 Выбери язык:',
    'tos_title': '📜 <b>Условия соглашения</b>',
    'tos_default': 'Используя бота, ты соглашаешься играть ответственно. Фишки не имеют реальной денежной ценности.',
    'tos_accept_btn': '✅ Принимаю',
    'welcome': '🃏 <b>Добро пожаловать в Blackjack Bot!</b>\n💰 Стартовый баланс: <b>{bal:,}¢</b>\n\n⚠️ Игра доступна только в <b>группах</b> — добавь бота в группу, чтобы играть.',
    'lang_ok': '✅ Язык: <b>Русский</b>',
    'help': '📚 <b>Blackjack Bot — Помощь</b>\n\nБольшинство команд работают как /слэш или просто словом в группе (например <code>/play</code> = <code>играть</code>).\n\n<b>🃏 Blackjack</b> (только в группах)\n/play — выбрать ставку и сесть за стол\n/go — начать стол сразу, не дожидаясь сбора игроков\n/bet &lt;сумма&gt; — сесть со своей ставкой\n/hit — взять карту\n/stand — остановиться, не брать карту\n/double — удвоить ставку и взять последнюю карту\n/swap — заменить последнюю карту (только 👑 VIP)\n/cancel — отменить стол, ставки вернутся (только админы)\n\n<b>🎲 Карточный бросок</b>\n/cards или /toss — ответь на сообщение игрока, чтобы бросить карту против него\nУровень выше — гарантированная победа, ниже — гарантированное поражение, равный — монетка. Проигравший не получает ничего.\n/upgrade — потрать 🪙 жетоны, выигранные в бросках, чтобы прокачать карту\n\n<b>👤 Аккаунт</b>\n/profile — баланс, статус VIP, статистика игр\n/bonus — ежедневная награда (только подписчикам канала, баланс должен быть меньше 50 000¢)\n/top — таблица лидеров по балансу, победам или % побед\n/ref — твоя ссылка для приглашений, +3💎 за друга (+5💎 для 👑 VIP)\n/transfer @юзер &lt;сумма&gt; — перевод фишек, комиссия 10% (бесплатно для 👑 VIP)\n/shop — купить алмазы за ⭐, а затем VIP/фишки за 💎 (только в ЛС)\n/vip — привилегии VIP и цена\n/promo — ввести промокод\n/settings — язык, правила, группы, поддержка',
    'profile': '👤 {mention}\n💰 Баланс: <b>{bal:,}¢</b>\n💎 Алмазы: <b>{bs:,}</b>\n{vip_line}\n\n📊 Побед <b>{bw}</b> · Поражений <b>{bl}</b> · Игр <b>{bg}</b> · % побед <b>{pct}%</b>',
    'vip_active': '👑 VIP до <b>{d}</b> (осталось {left})',
    'vip_perm': '👑 VIP: <b>навсегда ♾️</b>',
    'no_vip': '⚪ VIP не активен — /shop',
    'vip_info': '👑 <b>VIP</b>\n\n🎁 Ежедневный бонус: <b>5 000¢</b> (обычно 2 500¢)\n👑 Корона рядом с именем\n🔄 Доступна замена карты\n💸 Без комиссии за перевод (обычно 10%)\n🔗 Бонус за реферала: <b>+5💎</b> за друга (обычно +3💎)\n\n🛍 <b>Цена</b>\nНавсегда — <b>119💎</b>\n\n/shop',
    'bonus_ok': '🎁 Ежедневный бонус +<b>{a:,}¢</b>\nБаланс: <b>{b:,}¢</b>',
    'bonus_wait': '⏳ Бонус уже получен сегодня. Следующий через <b>{t}</b>.',
    'bonus_too_rich': '❌ Бонус доступен только при балансе меньше <b>{m:,}¢</b>.',
    'need_sub': '⚠️ Подпишись на наш канал, чтобы получить это.',
    'ref_msg': '🔗 <b>Зови друзей</b>\n\nТы получаешь <b>+3💎</b> за каждого друга (👑 VIP: +5💎).\nДруг получает <b>+5 000¢</b>.\n\nНажми кнопку ниже, чтобы поделиться ссылкой:',
    'ref_done': '🎉 Бонус за регистрацию по ссылке +<b>{a:,}¢</b>!',
    'ref_rwd_vip': '🎉 <b>{name}</b> зарегистрировался по твоей ссылке! +<b>5💎</b>',
    'ref_rwd_norm': '🎉 <b>{name}</b> зарегистрировался по твоей ссылке! +<b>3💎</b>',
    'top_title': '🏆 <b>Топ 15 — {mode}</b>\n\n{lines}',
    'top_title_grp': '🏆 <b>Топ 15 этой группы — {mode}</b>\n\n{lines}',
    'top_bal_lbl': '💰 Баланс',
    'top_win_lbl': '🏆 Победы',
    'top_pct_lbl': '📈 % побед',
    'top_line_bal': '<b>{i}.</b> {m}  —  <b>{v:,}¢</b>',
    'top_line_win': '<b>{i}.</b> {m}  —  <b>{v}</b> 🏆',
    'top_line_pct': '<b>{i}.</b> {m}  —  <b>{v}%</b>  ({g})',
    'top_empty': 'Пока нет игроков.',
    'shop': '🛍 <b>Магазин</b>\n\nДва раздела:\n💎 <b>Купить алмазы</b> за настоящие тг-звёзды ⭐\n🛍 <b>Купить VIP / фишки</b> за алмазы 💎',
    'shop_stars_title': '💎 <b>Купить алмазы</b>\n\nОплата настоящими тг-звёздами ⭐ — чем больше пакет, тем выгоднее курс.',
    'shop_items_title': '🛍 <b>VIP и фишки</b>\n\nОплата с баланса алмазов 💎: <b>{bs:,}💎</b>',
    'not_enough_stars': '❌ Недостаточно алмазов 💎. У тебя <b>{have:,}💎</b>, нужно <b>{need:,}💎</b>.\nПополни баланс в магазине за настоящие тг-звёзды.',
    'shop_pm_only': '🛍 Магазин доступен только в личных сообщениях. Открой его кнопкой ниже:',
    'confirm': '✅ <b>Подтверди покупку</b>\n\n{item}\n<b>{stars}⭐</b>',
    'confirm_stars': '✅ <b>Подтверди покупку</b>\n\n{item}\n<b>{stars}💎</b>',
    'pay_ok': '✅ Покупка подтверждена — <b>{item}</b> активировано.',
    'pay_ok_stars': '✅ <b>+{amt:,}💎</b> алмазов зачислено!',
    'pay_fail': '❌ Платёж не прошёл. Попробуй снова или напиши в поддержку.',
    'pay_fail_cr': '⚠️ Ошибка оплаты от <code>{uid}</code> ({name}), товар: <b>{item}</b>.',
    'lobby': '🃏 <b>Стол #{n}</b>\n⏱ Старт через <b>{t}с</b> · Игроков: <b>{cnt}</b>\n\n{players}\n\nВойти: /bet &lt;сумма&gt;  ·  Начать сразу: /go',
    'game_start': '🃏 <b>Стол #{n}</b> — раздаю карты…',
    'dlabel': 'ДИЛЕР',
    'your_turn': '🎮 <b>Твой ход, {mention}</b>\n\n{board}\n\n⏱ <b>{secs}с</b> на ход',
    'auto_stand': '⏱ Время вышло — авто-стоп.',
    'd_reveals': '🃏 Дилер открывает карту…',
    'd_hits': '🃏 Дилер берёт <b>{c}</b>  [{t}]',
    'd_stands': '🛑 Дилер стоп на <b>[{t}]</b>',
    'd_busts': '💥 Дилер перебор <b>[{t}]</b>!',
    'results': '🏁 <b>Стол #{n} — Результаты</b>\n\n{dlr}\n\n{lines}',
    'r_bj': '🎰 {m}  Блэкджек!\n{h}  +<b>{w:,}¢</b>',
    'r_win': '🏆 {m}  Победа\n{h}  +<b>{w:,}¢</b>',
    'r_push': '🤝 {m}  Ничья\n{h}  возврат <b>{b:,}¢</b>',
    'r_lose': '❌ {m}  Поражение\n{h}  −<b>{b:,}¢</b>',
    'r_bust': '💥 {m}  Перебор\n{h}  −<b>{b:,}¢</b>',
    'swap_ok_vip': '🔄 Карта заменена (👑 VIP)',
    'swap_vip_only': '❌ Замена карты — только для 👑 VIP.\n/vip, чтобы открыть.',
    'swap_first': '❌ Сначала возьми карту (/hit), потом можно менять.',
    'vip_bust_swap': '💥 Перебор!  {board}\n\n👑 VIP: <b>{secs}с</b>, чтобы заменить последнюю карту.',
    'stood': '🛑 {mention} стоп.',
    'not_ur': '⚠️ Сейчас не твой ход.',
    'no_game': '❌ Нет активной игры. /play, чтобы начать.',
    'no_insurance': '❌ Страховка сейчас не предлагается.',
    'insurance_offer': '🛡 <b>У дилера туз!</b>\n\nМожно взять страховку — стоит <b>половину ставки</b>, платит 2:1, если у дилера блэкджек.\n\nНапиши /insure чтобы взять или /noinsure чтобы отказаться. ({secs}с)',
    'insurance_taken': '🛡 {mention} взял(а) страховку.',
    'insurance_declined': '❌ {mention} отказался(-лась) от страховки.',
    'insurance_win': '🛡 Страховка {mention} сыграла: <b>+{w:,}¢</b>',
    'insurance_lose': '🛡 Страховка {mention} не сыграла: <b>-{l:,}¢</b>',
    'dealer_bj_reveal': '🃏 У дилера <b>блэкджек!</b> Раздача завершается.',
    'in_game': '⚠️ Ты уже за столом.',
    'bet_low': '❌ Минимальная ставка: <b>{m:,}¢</b>',
    'bet_high': '❌ Максимальная ставка: <b>{m:,}¢</b>',
    'no_bal': '❌ Недостаточно фишек. Баланс: <b>{b:,}¢</b>',
    'dbl_low': '❌ Не хватает <b>{n:,}¢</b> для удвоения.',
    'doubled': '✅ Ставка удвоена до <b>{b:,}¢</b> — одна карта…',
    'joined': '✅ Сел за стол <b>#{n}</b>  (ставка <b>{bet:,}¢</b>)',
    'play_choose': '🃏 Выбери ставку\nБаланс: <b>{bal:,}¢</b>\n\nНажми кнопку или /bet &lt;сумма&gt;:',
    'pm_no_play': '⚠️ Игра доступна только в <b>группах</b>. Добавь бота в группу!',
    'settings': '⚙️ <b>Настройки</b>',
    'report_ask': '📝 Опиши проблему или идею (минимум <b>{n} слов</b>):',
    'report_short': '❌ Напиши минимум <b>{n} слов</b>.',
    'report_sent': '✅ Отправлено — спасибо!',
    'report_recv': '📩 <b>Обращение</b> от {mention} (<code>{uid}</code>):\n\n{text}',
    'cancel_ok': '🚫 Игра отменена, ставки возвращены.',
    'cancel_none': '❌ Нет активной игры.',
    'cancel_not_adm': '⚠️ Только админы могут отменить или начать игру.',
    'grp_lang_ok': '✅ Язык группы: <b>{lang}</b>',
    'grp_adm_only': '⚠️ Только админы группы могут это менять.',
    'promo_ask': '🎟 Введи промокод:',
    'promo_m': '🎁 Промокод применён +<b>{a:,}¢</b>\nБаланс: <b>{b:,}¢</b>',
    'promo_v': '👑 Промокод применён — VIP +<b>{d} дней</b>',
    'promo_nf': '❌ Промокод не найден.',
    'promo_exp': '❌ Промокод истёк.',
    'promo_nu': '❌ Активации закончились.',
    'promo_dup': '⚠️ Уже использован.',
    'tr_ok': '✅ Отправлено <b>{a:,}¢</b> игроку {to}\nКомиссия <b>{fee:,}¢</b>  ·  Баланс <b>{b:,}¢</b>',
    'tr_ok_vip': '✅ Отправлено <b>{a:,}¢</b> игроку {to}\n👑 Без комиссии  ·  Баланс <b>{b:,}¢</b>',
    'tr_recv': '💸 {fr} перевёл тебе <b>{a:,}¢</b>!\nБаланс: <b>{b:,}¢</b>',
    'tr_self': '❌ Нельзя переводить самому себе.',
    'tr_low': '❌ Минимальный перевод: <b>100¢</b>',
    'tr_nob': '❌ Недостаточно фишек. Баланс: <b>{b:,}¢</b>',
    'tr_nf': '❌ Игрок не найден — он должен сначала запустить бота.',
    'tr_usage': 'ℹ️ /transfer @юзер &lt;сумма&gt;\nили ответь на сообщение + /transfer &lt;сумма&gt;\n\nКомиссия: 10%  (👑 VIP: 0%)',
    'pm_only': '⚠️ Это работает только в личных сообщениях.',
    'banned': '🚫 Бан до <b>{until}</b>.',
    'btn_hit': '🃏 Взять',
    'btn_stand': '🛑 Стоп',
    'btn_double': '✖️ Удвоить',
    'btn_swap': '🔄 Поменять',
    'btn_play': '🃏 Играть',
    'btn_profile': '👤 Профиль',
    'btn_bonus': '🎁 Бонус',
    'btn_shop': '🛍 Магазин',
    'btn_shop_stars': '💎 Купить алмазы (за ⭐)',
    'btn_shop_items': '🛍 VIP и фишки (за 💎)',
    'btn_top': '🏆 Топ',
    'btn_ref': '🔗 Реферал',
    'btn_settings': '⚙️ Настройки',
    'btn_lang': '🌍 Язык',
    'btn_promo': '🎟 Промокод',
    'btn_upgrade': '🆙 Прокачка',
    'btn_upgrade_confirm': '⬆️ Прокачать ({cost} 🪙)',
    'btn_help': '📚 Помощь',
    'btn_howto': '❓ Как играть?',
    'how_to_play': (
        "❓ <b>Как играть в блэкджек — просто и понятно</b>\n\n"
        "🎯 <b>Цель:</b> набрать картами как можно ближе к <b>21</b>, но не больше. "
        "И при этом обыграть дилера — вот и всё.\n\n"
        "🃏 <b>Сколько стоят карты:</b>\n"
        "• Цифры (2-10) — как написано\n"
        "• Валет, Дама, Король — <b>10</b>\n"
        "• Туз — <b>11</b> или <b>1</b> (как тебе выгоднее)\n\n"
        "▶️ <b>Как проходит раунд:</b>\n"
        "1️⃣ /play — выбери ставку (или /go, чтобы начать раньше)\n"
        "2️⃣ Тебе и дилеру раздают по 2 карты. Свои видишь обе, у дилера — только ОДНУ\n"
        "3️⃣ Твой ход: /hit — взять карту, /stand — остановиться\n"
        "4️⃣ Больше 21 — это <b>перебор</b>, сразу проигрыш, неважно что у дилера\n"
        "5️⃣ Когда все закончили — дилер открывает карту и обязан брать, пока не наберёт 17+\n\n"
        "🏆 <b>Кто выигрывает:</b>\n"
        "• Твоя сумма ближе к 21, чем у дилера (без перебора) → победа, получаешь ставку ×2\n"
        "• Ровно 21 с первых 2 карт = <b>Блэкджек</b> → выплата 3:2 (больше обычного!)\n"
        "• Сумма как у дилера → ничья, ставка возвращается\n"
        "• У дилера ближе к 21, или у тебя перебор → ставка проигрывает\n\n"
        "🛡 <b>Страховка:</b> если у дилера открыт туз, можно взять /insure за половину ставки — "
        "выплата 2:1, если у дилера окажется блэкджек.\n\n"
        "💡 Доп. ходы: /double (удвоить ставку, ещё одна карта) и /swap (только 👑 VIP — заменить последнюю карту)."
    ),
    'btn_report': '📝 Поддержка',
    'btn_vip': '👑 VIP',
    'btn_yes': '✅ Подтвердить',
    'btn_no': '❌ Отмена',
    'btn_back': '◀️ Назад',
    'btn_top_bal': '💰 Баланс',
    'btn_top_win': '🏆 Победы',
    'btn_top_pct': '📈 % побед',
    'btn_tos': '📜 Правила',
    'btn_groups': '🎮 Группы',
    'btn_support': '🆘 Поддержка',
    'btn_share': '📤 Поделиться ссылкой',
    'groups_title': '🎮 <b>Где поиграть</b>\n\nВыбери группу:',
    'groups_empty': '🎮 Групп пока нет.',
    'toss_no_reply': '⚠️ Ответь на сообщение игрока, чтобы бросить карту.',
    'toss_self': '⚠️ Нельзя бросить карту самому себе.',
    'toss_cooldown': '⏳ Откат: ещё <b>{t}</b>.',
    'toss_limit': '⚠️ Дневной лимит исчерпан (5/5).',
    'toss_result_win': '🎲 <b>Карточный бросок</b>\n\n🃏 {a}  vs  🃏 {b}\n\n🏆 <b>{challenger}</b> побеждает!\n💰 +{chips:,}¢  🪙 +{tokens}',
    'toss_result_loss': '🎲 <b>Карточный бросок</b>\n\n🃏 {a}  vs  🃏 {b}\n\n🏆 <b>{opponent}</b> побеждает!\n😔 {challenger} проигрывает. Награды нет.',
    'upgrade_menu': '🆙 <b>Прокачка карты</b>\n\nВ 🎲 Карточном броске: уровень выше, чем у соперника — гарантированная победа. Ниже — гарантированное поражение. Равный — монетка.\n\n🃏 Уровень: <b>{lvl}</b>\n🪙 Жетонов: <b>{tokens}</b>\n\nСледующий уровень: <b>{cost} 🪙</b>',
    'upgrade_ok': '✅ Карта прокачана до уровня <b>{lvl}</b>!\n🪙 Жетонов осталось: <b>{tokens}</b>',
    'upgrade_no_tokens': '❌ Недостаточно жетонов. Нужно <b>{cost}</b>, у тебя <b>{tokens}</b>.',
}

# ── ALIAS MAP ────────────────────────────────────────────────────────────────
_AL = {
    "play":"play","go":"go","bet":"bet","hit":"hit","stand":"stand",
    "double":"double","swap":"swap","cancel":"cancel",
    "profile":"profile","bonus":"bonus","shop":"shop",
    "top":"top","ref":"ref","vip":"vip","settings":"settings",
    "transfer":"transfer","promo":"promo","help":"help","cards":"cardtoss","toss":"cardtoss","upgrade":"upgrade",
    "играть":"play","го":"go","ставка":"bet","взять":"hit","стоп":"stand",
    "удвоить":"double","поменять":"swap","отмена":"cancel",
    "профиль":"profile","бонус":"bonus","магазин":"shop",
    "топ":"top","реферал":"ref","настройки":"settings",
    "перевод":"transfer","промокод":"promo","помощь":"help","карты":"cardtoss","бросок":"cardtoss","прокачка":"upgrade",
}

def _build_kb_cmd():
    m = {}
    pairs = [("btn_play","play"),("btn_profile","profile"),("btn_bonus","bonus"),
             ("btn_shop","shop"),("btn_top","top"),("btn_ref","ref"),
             ("btn_settings","settings"),("btn_vip","vip"),
             ("btn_hit","hit"),("btn_stand","stand"),
             ("btn_upgrade","upgrade"),
             ("btn_double","double"),("btn_swap","swap")]
    for lng in TX.values():
        for key, cmd in pairs:
            if key in lng: m[lng[key].lower()] = cmd
    return m
_KB_CMD = _build_kb_cmd()

# ── DATABASE (PostgreSQL via asyncpg) ────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users(
    uid BIGINT PRIMARY KEY,
    username TEXT DEFAULT '',
    name TEXT DEFAULT '',
    lang TEXT DEFAULT 'en',
    bal BIGINT DEFAULT 10000,
    vip_until BIGINT DEFAULT 0,
    vip_perm BOOLEAN DEFAULT FALSE,
    w_bj INTEGER DEFAULT 0,
    l_bj INTEGER DEFAULT 0,
    g_bj INTEGER DEFAULT 0,
    last_bonus BIGINT DEFAULT 0,
    ref_code TEXT UNIQUE,
    ref_by BIGINT,
    joined BIGINT DEFAULT 0,
    banned_until BIGINT DEFAULT 0,
    tos_lang TEXT DEFAULT '',
    bot_stars BIGINT DEFAULT 0,
    equipped_skin TEXT DEFAULT 'classic',
    equipped_frame TEXT DEFAULT 'none',
    quick_replies TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS game_history(
    id SERIAL PRIMARY KEY,
    uid BIGINT,
    ts BIGINT,
    bet BIGINT,
    result TEXT,
    change BIGINT
);
CREATE INDEX IF NOT EXISTS idx_game_history_uid ON game_history(uid, ts DESC);
CREATE TABLE IF NOT EXISTS group_settings(
    cid BIGINT PRIMARY KEY,
    lang TEXT DEFAULT 'en'
);
CREATE TABLE IF NOT EXISTS cosmetics_owned(
    uid BIGINT,
    item_code TEXT,
    acquired BIGINT,
    PRIMARY KEY(uid, item_code)
);
CREATE TABLE IF NOT EXISTS promos(
    code TEXT PRIMARY KEY,
    ptype TEXT,
    pval DOUBLE PRECISION,
    uses INTEGER,
    exp BIGINT,
    by_uid BIGINT,
    created BIGINT
);
CREATE TABLE IF NOT EXISTS promo_used(
    code TEXT,
    uid BIGINT,
    PRIMARY KEY(code, uid)
);
CREATE TABLE IF NOT EXISTS purchases(
    id SERIAL PRIMARY KEY,
    uid BIGINT,
    item TEXT,
    stars INTEGER,
    ts BIGINT
);
CREATE TABLE IF NOT EXISTS group_players(
    cid BIGINT,
    uid BIGINT,
    PRIMARY KEY(cid, uid)
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS cardtoss_stats(
    uid BIGINT PRIMARY KEY,
    tokens INTEGER DEFAULT 0,
    card_level INTEGER DEFAULT 1,
    tosses_today INTEGER DEFAULT 0,
    last_toss_reset BIGINT DEFAULT 0,
    last_toss BIGINT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bot_groups(
    id SERIAL PRIMARY KEY,
    title TEXT,
    username TEXT,
    link TEXT,
    added_by BIGINT,
    added_at BIGINT
);
"""

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with pool.acquire() as c:
        # Legacy rename — must run BEFORE schema creation so it only fires
        # if an old-style "duel_stats" table still exists.
        try:
            await c.execute("ALTER TABLE IF EXISTS duel_stats RENAME TO cardtoss_stats")
        except Exception:
            pass
        await c.execute(SCHEMA_SQL)
        # Migrations: add missing columns to existing tables safely
        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_until BIGINT DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS tos_lang TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_perm BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_until BIGINT DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_by BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_bonus BIGINT DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS w_bj INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS l_bj INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS g_bj INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_stars BIGINT DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS equipped_skin TEXT DEFAULT 'classic'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS equipped_frame TEXT DEFAULT 'none'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS quick_replies TEXT DEFAULT ''",
            # Legacy column renames (caught/no-op if already done or fresh install)
            "ALTER TABLE cardtoss_stats RENAME COLUMN duels_today TO tosses_today",
            "ALTER TABLE cardtoss_stats RENAME COLUMN last_duel_reset TO last_toss_reset",
            "ALTER TABLE cardtoss_stats RENAME COLUMN last_duel TO last_toss",
        ]
        for sql in migrations:
            try:
                await c.execute(sql)
            except Exception:
                pass
    log.info("DB ready (PostgreSQL)")

def _rc():
    return "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=8))

async def ensure_ids(uid: int, fname: str, uname: str = ""):
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO users(uid,username,name,bal,joined,ref_code) "
            "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT (uid) DO NOTHING",
            uid, uname, fname or "", START_BAL, int(time.time()), _rc())
        await c.execute("UPDATE users SET name=$1,username=$2 WHERE uid=$3",
            fname or "", uname, uid)

async def gu(uid: int) -> Optional[dict]:
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT * FROM users WHERE uid=$1", uid)
        return dict(r) if r else None

async def gu_un(un: str) -> Optional[dict]:
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT * FROM users WHERE LOWER(username)=$1",
            un.lstrip("@").lower())
        return dict(r) if r else None

async def dba(sql, *p):
    async with pool.acquire() as c:
        rows = await c.fetch(sql, *p)
        return [dict(r) for r in rows]

async def dbq(sql, *p):
    async with pool.acquire() as c:
        r = await c.fetchrow(sql, *p)
        return dict(r) if r else None

async def dbx(sql, *p):
    async with pool.acquire() as c:
        await c.execute(sql, *p)

async def add_bal(uid: int, d: int):
    await dbx("UPDATE users SET bal=GREATEST(0,bal+$1) WHERE uid=$2", d, uid)

async def add_stars(uid: int, d: int):
    """Bot's own internal currency (💎 алмазы) — separate from real Telegram Stars (⭐)."""
    await dbx("UPDATE users SET bot_stars=GREATEST(0,bot_stars+$1) WHERE uid=$2", d, uid)

async def get_stars(uid: int) -> int:
    u = await gu(uid)
    return u["bot_stars"] if u else 0

async def is_vip(uid: int) -> bool:
    u = await gu(uid)
    return bool(u and (u["vip_perm"] or u["vip_until"] > int(time.time())))

async def vip_left(uid: int):
    u = await gu(uid)
    if not u: return 0
    if u["vip_perm"]: return float("inf")
    return max(0, u["vip_until"] - int(time.time()))

async def extend_vip(uid: int, days: int):
    u = await gu(uid)
    base = max(u["vip_until"] if u else 0, int(time.time()))
    await dbx("UPDATE users SET vip_until=$1 WHERE uid=$2", base + days*86400, uid)

async def set_vip_perm(uid: int):
    await dbx("UPDATE users SET vip_perm=TRUE, vip_until=9999999999 WHERE uid=$1", uid)

async def take_vip(uid: int):
    await dbx("UPDATE users SET vip_perm=FALSE, vip_until=0 WHERE uid=$1", uid)

async def is_banned(uid: int) -> bool:
    u = await gu(uid)
    return bool(u and u["banned_until"] > int(time.time()))

async def ban_user(uid: int, days: int):
    until = int(time.time()) + days*86400
    await dbx("UPDATE users SET banned_until=$1 WHERE uid=$2", until, uid)
    return until

async def get_lang(uid: int) -> str:
    u = await gu(uid)
    return u["lang"] if u else "en"

async def set_lang_u(uid: int, lang: str):
    await dbx("UPDATE users SET lang=$1 WHERE uid=$2", lang, uid)

async def get_glang(cid: int) -> str:
    r = await dbq("SELECT lang FROM group_settings WHERE cid=$1", cid)
    return r["lang"] if r else "en"

async def set_glang(cid: int, lang: str):
    await dbx(
        "INSERT INTO group_settings(cid,lang) VALUES($1,$2) "
        "ON CONFLICT (cid) DO UPDATE SET lang=$2", cid, lang)

async def eff_lang(cid: int, uid: int) -> str:
    """The language to actually display: personal preference in PM,
    but in a group EVERYONE sees the single language the group admin picked —
    a user's own /settings language never leaks into a group chat."""
    return await get_lang(uid) if is_pm(cid) else await get_glang(cid)

async def bump_stats(uid: int, won=False, lost=False):
    if won:    await dbx("UPDATE users SET w_bj=w_bj+1,g_bj=g_bj+1 WHERE uid=$1", uid)
    elif lost: await dbx("UPDATE users SET l_bj=l_bj+1,g_bj=g_bj+1 WHERE uid=$1", uid)
    else:      await dbx("UPDATE users SET g_bj=g_bj+1 WHERE uid=$1", uid)

async def track_group_player(cid: int, uid: int):
    await dbx(
        "INSERT INTO group_players(cid,uid) VALUES($1,$2) ON CONFLICT DO NOTHING", cid, uid)

async def get_setting(key: str, default=""):
    r = await dbq("SELECT value FROM settings WHERE key=$1", key)
    return r["value"] if r else default

async def set_setting(key: str, value: str):
    await dbx(
        "INSERT INTO settings(key,value) VALUES($1,$2) "
        "ON CONFLICT (key) DO UPDATE SET value=$2", key, value)

async def record_purchase(uid: int, item: str, stars: int):
    await dbx("INSERT INTO purchases(uid,item,stars,ts) VALUES($1,$2,$3,$4)",
        uid, item, stars, int(time.time()))

async def is_subscribed(uid: int) -> bool:
    if not CHANNEL_USERNAME: return True   # gating disabled if not configured
    try:
        mb = await bot.get_chat_member(CHANNEL_USERNAME, uid)
        return mb.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning(f"is_subscribed uid={uid} error: {e} — returning True to avoid blocking")
        return True   # if check fails (bot not admin of channel?), allow user

def win_pct(u): return round(u["w_bj"] / u["g_bj"] * 100, 1) if u and u["g_bj"] else 0.0
def fmt_ts(ts): return datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
def fmt_dur(s, lang="en"):
    s = int(s)
    dv,hv,mv,sv = ("д","ч","м","с") if lang=="ru" else ("d","h","m","s")
    if s >= 86400: return f"{s//86400}{dv} {(s%86400)//3600}{hv}"
    if s >= 3600:  return f"{s//3600}{hv} {(s%3600)//60}{mv}"
    return f"{s//60}{mv} {s%60}{sv}"
def is_pm(cid): return cid > 0
def resolve(msg_cid): return PM_CID if is_pm(msg_cid) else msg_cid

# ── CARDS ───────────────────────────────────────────────────────────────────
SUITS = ["♠","♥","♦","♣"]
RANKS = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
def new_deck(n=6):
    d = [(r,s) for s in SUITS for r in RANKS] * n
    random.shuffle(d); return d
def cval(c):
    if c[0] == "A": return 11
    if c[0] in ("J","Q","K"): return 10
    return int(c[0])
def htot(h):
    t = sum(cval(c) for c in h); a = sum(1 for c in h if c[0]=="A")
    while t > 21 and a: t -= 10; a -= 1
    return t
def is_soft(h):
    """True if hand total counts an ace as 11 (i.e. 'soft')."""
    t = sum(cval(c) for c in h); a = sum(1 for c in h if c[0]=="A")
    while t > 21 and a: t -= 10; a -= 1
    return a > 0
def cstr(c): return f"{c[0]}{c[1]}"
def hstr(h): return "  ".join(cstr(c) for c in h)
def is_bj(h): return len(h)==2 and htot(h)==21
def hdsp(h, hide=False):
    if hide and len(h)>=2: return f"{cstr(h[0])}  🂠  [{cval(h[0])}+?]"
    return f"{hstr(h)}  [{htot(h)}]"

def dealer_should_hit(hand):
    """Standard casino rule: dealer hits on 16 or less, and also on soft 17. Stands on hard 17+ or soft 18+."""
    total = htot(hand)
    if total < 17: return True
    if total == 17 and is_soft(hand): return True
    return False

# ── MESSAGING ───────────────────────────────────────────────────────────────
def t(lang_or_uid, key, **kw):
    lang = lang_or_uid if isinstance(lang_or_uid, str) else "en"
    d = TX.get(lang, TX["en"])
    tpl = d.get(key, TX["en"].get(key, key))
    try: return tpl.format(**kw)
    except Exception: return tpl

async def crown(uid):
    return "👑 " if await is_vip(uid) else ""

async def mention(uid, name):
    cr = await crown(uid)
    return f'<a href="tg://user?id={uid}">{cr}{html.escape(str(name))}</a>'

async def sdel(cid, mid):
    if not mid: return
    try: await bot.delete_message(cid, mid)
    except Exception: pass

async def sedit(cid, mid, text, kb=None):
    if not mid: return
    try: await bot.edit_message_text(text, chat_id=cid, message_id=mid, reply_markup=kb)
    except Exception: pass

async def send(cid, text, kb=None, reply_to=None):
    try:
        return await bot.send_message(cid, text, reply_markup=kb, reply_to_message_id=reply_to)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.warning(f"send({cid}): {e}")
        return None

async def temp(cid, text, delay=5):
    m = await send(cid, text)
    if m:
        async def _later():
            await asyncio.sleep(delay); await sdel(cid, m.message_id)
        asyncio.create_task(_later())

async def is_grp_admin(cid, uid):
    try:
        mb = await bot.get_chat_member(cid, uid)
        return mb.status in ("administrator", "creator")
    except Exception: return False

# ── TABLE STATE (groups only, unlimited seats) ───────────────────────────────
_tabs: dict = {}
_tmrs: dict = {}

def gtabs(acid): return list(_tabs.get(acid, []))
def stabs(acid, lst): _tabs[acid] = lst
def find_tab(acid, n):
    for tb in gtabs(acid):
        if tb["n"] == n: return tb
    return None
def find_player_tab(acid, uid):
    for tb in gtabs(acid):
        if any(p["uid"] == uid for p in tb["players"]): return tb
    return None
def new_tab(acid, n):
    return dict(n=n, state="lobby", acid=acid,
                players=[], dealer=[], deck=new_deck(), cur=0,
                lobby_mid={}, board_mid={}, action_mid={})
def get_or_new(acid):
    for tb in gtabs(acid):
        if tb["state"]=="lobby": return tb
    tabs = gtabs(acid)
    n = max((x["n"] for x in tabs), default=0) + 1
    tb = new_tab(acid, n); tabs.append(tb); stabs(acid, tabs); return tb
def del_tab(acid, n):
    stabs(acid, [x for x in gtabs(acid) if x["n"]!=n])

def ctmr(acid, n):
    tk = _tmrs.pop((acid, n), None)
    if tk and not tk.done(): tk.cancel()

def stmr(acid, n, delay, coro_fn, *args):
    ctmr(acid, n)
    async def _runner():
        try:
            await asyncio.sleep(delay)
            await coro_fn(*args)
        except asyncio.CancelledError:
            pass
    _tmrs[(acid, n)] = asyncio.create_task(_runner())

# ── TABLE MSG HELPERS ───────────────────────────────────────────────────────
async def tab_lang(tb):
    return await get_glang(tb["acid"])

async def tab_send_all(tb, text, kb=None):
    await send(tb["acid"], text, kb=kb)

async def _tab_upsert(tb, store, dest, text, kb=None):
    mid = store.get(0)
    if mid: await sedit(dest, mid, text, kb=kb)
    else:
        m = await send(dest, text, kb=kb)
        if m: store[0] = m.message_id

async def tab_set_lobby(tb, text):
    await _tab_upsert(tb, tb["lobby_mid"], tb["acid"], text)
async def tab_del_lobby(tb):
    await sdel(tb["acid"], tb["lobby_mid"].get(0)); tb["lobby_mid"].clear()
async def tab_set_board(tb, text):
    await _tab_upsert(tb, tb["board_mid"], tb["acid"], text)
async def tab_del_board(tb):
    await sdel(tb["acid"], tb["board_mid"].get(0)); tb["board_mid"].clear()

async def tab_set_action(tb, text, kb=None):
    mid = tb["action_mid"].pop(0, None)
    if mid: await sdel(tb["acid"], mid)
    m = await send(tb["acid"], text, kb=kb)
    if m: tb["action_mid"][0] = m.message_id

async def tab_del_action(tb):
    await sdel(tb["acid"], tb["action_mid"].pop(0, None)); tb["action_mid"].clear()

# ── TEXT BUILDERS ───────────────────────────────────────────────────────────
async def lobby_txt(tb, lang, secs=LOBBY_WAIT):
    rows = []
    for i, p in enumerate(tb["players"]):
        m = await mention(p["uid"], p["name"])
        rows.append(f"  {i+1}. {m} — {p['bet']:,}¢")
    body = "\n".join(rows) or "  —"
    return t(lang, "lobby", n=tb["n"], t=secs, cnt=len(tb["players"]), players=body)

async def board_txt(tb, lang, hi=-1, full_dlr=False):
    dlbl = TX.get(lang, TX["en"]).get("dlabel", "DEALER")
    lines = [f"<b>{dlbl}:</b>  {hdsp(tb['dealer'], hide=not full_dlr)}\n"]
    for i, p in enumerate(tb["players"]):
        pfx  = "▶️ " if i==hi else "   "
        done = " ✅" if p.get("done") else ""
        m = await mention(p["uid"], p["name"])
        lines.append(f"{pfx}{i+1}. {m}: {hdsp(p['hand'])}{done}")
    return "\n".join(lines)

async def profile_txt(uid, cid):
    u = await gu(uid); lang = await eff_lang(cid, uid); vl = await vip_left(uid)
    if u["vip_perm"]: vline = t(lang,"vip_perm")
    elif vl > 0:      vline = t(lang,"vip_active", d=fmt_ts(u["vip_until"]), left=fmt_dur(int(vl),lang))
    else:             vline = t(lang,"no_vip")
    mn = await mention(uid, u["name"])
    return t(lang,"profile", mention=mn, bal=u["bal"], bs=u["bot_stars"],
             vip_line=vline, bw=u["w_bj"], bl=u["l_bj"], bg=u["g_bj"], pct=win_pct(u))

async def top_txt(lang, mode="balance", cid=None):
    """If cid given (a group), restrict to players tracked for that group."""
    grp_filter = cid is not None and not is_pm(cid)
    if mode == "balance":
        mlbl = t(lang,"top_bal_lbl")
        if grp_filter:
            rows = await dba(
                "SELECT u.uid,u.name,u.bal FROM users u "
                "JOIN group_players g ON g.uid=u.uid AND g.cid=$1 "
                "WHERE u.uid!=$2 AND u.g_bj>0 ORDER BY u.bal DESC LIMIT 15", cid, CREATOR_ID)
        else:
            rows = await dba(
                "SELECT uid,name,bal FROM users WHERE uid!=$1 AND g_bj>0 ORDER BY bal DESC LIMIT 15", CREATOR_ID)
        lines = []
        for i, r in enumerate(rows):
            m = await mention(r["uid"], r["name"])
            lines.append(t(lang,"top_line_bal", i=i+1, m=m, v=r["bal"]))
    elif mode == "wins":
        mlbl = t(lang,"top_win_lbl")
        if grp_filter:
            rows = await dba(
                "SELECT u.uid,u.name,u.w_bj FROM users u "
                "JOIN group_players g ON g.uid=u.uid AND g.cid=$1 "
                "WHERE u.uid!=$2 AND u.g_bj>0 ORDER BY u.w_bj DESC LIMIT 15", cid, CREATOR_ID)
        else:
            rows = await dba(
                "SELECT uid,name,w_bj FROM users WHERE uid!=$1 AND g_bj>0 ORDER BY w_bj DESC LIMIT 15", CREATOR_ID)
        lines = []
        for i, r in enumerate(rows):
            m = await mention(r["uid"], r["name"])
            lines.append(t(lang,"top_line_win", i=i+1, m=m, v=r["w_bj"]))
    else:
        mlbl = t(lang,"top_pct_lbl")
        if grp_filter:
            rows = await dba(
                "SELECT u.uid,u.name,u.w_bj,u.g_bj FROM users u "
                "JOIN group_players g ON g.uid=u.uid AND g.cid=$1 "
                "WHERE u.g_bj>=5 AND u.uid!=$2 ORDER BY (u.w_bj::float/u.g_bj) DESC LIMIT 15", cid, CREATOR_ID)
        else:
            rows = await dba(
                "SELECT uid,name,w_bj,g_bj FROM users WHERE g_bj>=5 AND uid!=$1 "
                "ORDER BY (w_bj::float/g_bj) DESC LIMIT 15", CREATOR_ID)
        lines = []
        for i, r in enumerate(rows):
            m = await mention(r["uid"], r["name"])
            lines.append(t(lang,"top_line_pct", i=i+1, m=m,
                v=round(r["w_bj"]/r["g_bj"]*100,1), g=r["g_bj"]))
    body = "\n".join(lines) if lines else t(lang,"top_empty")
    key = "top_title_grp" if grp_filter else "top_title"
    return t(lang, key, mode=mlbl, lines=body)

# ── KEYBOARDS ───────────────────────────────────────────────────────────────
async def reply_kb(uid):
    """PM reply keyboard — NO play button (play is group-only)."""
    lang = await get_lang(uid)
    kb = [
        [KB(text=t(lang,"btn_profile")), KB(text=t(lang,"btn_bonus"))],
        [KB(text=t(lang,"btn_shop")),    KB(text=t(lang,"btn_top"))],
        [KB(text=t(lang,"btn_ref")),     KB(text=t(lang,"btn_upgrade"))],
        [KB(text=t(lang,"btn_settings"))],
    ]
    return RKM(keyboard=kb, resize_keyboard=True, is_persistent=True)

async def bet_kb(acid, uid):
    u = await gu(uid); bal = u["bal"] if u else 0
    bets = [100, 500, 1_000, 5_000, 10_000, 50_000]
    rows, row = [], []
    for b in bets:
        if bal >= b:
            row.append(IKB(text=f"{b:,}¢", callback_data=f"QB|{acid}|{b}"))
            if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    return IKM(inline_keyboard=rows)

def game_kb(lang, acid, n):
    """No inline buttons in groups — players type commands."""
    return None

def shop_main_kb(lang):
    return IKM(inline_keyboard=[
        [IKB(text=t(lang,"btn_shop_stars"), callback_data="shopm|stars")],
        [IKB(text=t(lang,"btn_shop_items"), callback_data="shopm|items")],
    ])

def shop_starpacks_kb(lang):
    rows = [[IKB(text=f"{label} — {tg}⭐", callback_data=f"buystar|{code}")]
            for code,(label,tg,bs) in STAR_PACKS.items()]
    rows.append([IKB(text=t(lang,"btn_back"), callback_data="shopm|back")])
    return IKM(inline_keyboard=rows)

def shop_items_kb(lang):
    rows = [[IKB(text=f"{name} — {cost}💎", callback_data=f"buyitem|{code}")]
            for code,(name,cost,*_) in SHOP_ITEMS.items()]
    rows.append([IKB(text=t(lang,"btn_back"), callback_data="shopm|back")])
    return IKM(inline_keyboard=rows)

async def shop_pm_kb(lang):
    try: bname = (await bot.get_me()).username
    except Exception: bname = "blackjack_bot"
    return IKM(inline_keyboard=[[
        IKB(text=t(lang,"btn_shop"), url=f"https://t.me/{bname}?start=shop")
    ]])

def settings_kb(lang):
    lang_label = f"{t(lang,'btn_lang')}: {LANGS.get(lang, lang)}"
    return IKM(inline_keyboard=[
        [IKB(text=lang_label, callback_data="set|lang")],
        [IKB(text=t(lang,"btn_howto"), callback_data="set|howto")],
        [IKB(text=t(lang,"btn_promo"), callback_data="set|promo"),
         IKB(text=t(lang,"btn_help"),  callback_data="set|help")],
        [IKB(text=t(lang,"btn_tos"),    callback_data="set|tos"),
         IKB(text=t(lang,"btn_groups"), callback_data="set|groups")],
        [IKB(text=t(lang,"btn_report"), callback_data="set|report")],
    ])

def back_kb(lang):
    return IKM(inline_keyboard=[[IKB(text=t(lang,"btn_back"), callback_data="set|back")]])

def channel_kb(lang):
    if not CHANNEL_USERNAME: return None
    uname = CHANNEL_USERNAME.lstrip("@")
    return IKM(inline_keyboard=[[IKB(text=f"📢 {CHANNEL_USERNAME}", url=f"https://t.me/{uname}")]])

def support_kb(lang):
    return IKM(inline_keyboard=[[IKB(text=t(lang,"btn_support"), url=f"https://t.me/{CREATOR_UN}")]])

def share_kb(lang, link):
    url = f"https://t.me/share/url?url={quote(link, safe='')}"
    return IKM(inline_keyboard=[[IKB(text=t(lang,"btn_share"), url=url)]])

def top_kb(lang, mode="balance"):
    def lbl(m, k): return ("▶ " if mode==m else "") + t(lang, k)
    return IKM(inline_keyboard=[[
        IKB(text=lbl("balance","btn_top_bal"), callback_data="top|balance"),
        IKB(text=lbl("wins",   "btn_top_win"), callback_data="top|wins"),
        IKB(text=lbl("pct",    "btn_top_pct"), callback_data="top|pct"),
    ]])

def lang_kb(prefix="lang"):
    items = list(LANGS.items())
    rows = []
    for i in range(0, len(items), 2):
        rows.append([IKB(text=name, callback_data=f"{prefix}|{code}") for code,name in items[i:i+2]])
    return IKM(inline_keyboard=rows)

def tos_kb(lang):
    return IKM(inline_keyboard=[[IKB(text=t(lang,"tos_accept_btn"), callback_data=f"tos_ok|{lang}")]])

# ── PROMO ───────────────────────────────────────────────────────────────────
async def apply_promo(uid, code, dest_cid):
    lang = await get_lang(uid); now = int(time.time()); code = (code or "").strip().upper()
    if not await is_subscribed(uid):
        await send(dest_cid, t(lang,"need_sub"), kb=channel_kb(lang)); return
    if not code: await send(dest_cid, t(lang,"promo_nf")); return
    pr = await dbq("SELECT * FROM promos WHERE code=$1", code)
    if not pr:           await send(dest_cid, t(lang,"promo_nf"));  return
    if pr["exp"] < now:  await send(dest_cid, t(lang,"promo_exp")); return
    if pr["uses"] <= 0:  await send(dest_cid, t(lang,"promo_nu"));  return
    used = await dbq("SELECT 1 used FROM promo_used WHERE code=$1 AND uid=$2", code, uid)
    if used: await send(dest_cid, t(lang,"promo_dup")); return
    await dbx("UPDATE promos SET uses=uses-1 WHERE code=$1", code)
    await dbx("INSERT INTO promo_used VALUES($1,$2)", code, uid)
    if pr["ptype"] == "money":
        await add_bal(uid, int(pr["pval"]))
        u = await gu(uid)
        await send(dest_cid, t(lang,"promo_m", a=int(pr["pval"]), b=u["bal"]))
    else:
        d = int(pr["pval"]); await extend_vip(uid, d)
        await send(dest_cid, t(lang,"promo_v", d=d))

# ── TRANSFER ────────────────────────────────────────────────────────────────
async def do_transfer(sender_uid, target, amount, dest_cid, lang):
    if amount < 100: await send(dest_cid, t(lang,"tr_low")); return
    su = await gu(sender_uid)
    if not su: return
    tu = await gu(target) if isinstance(target, int) else await gu_un(target)
    if not tu: await send(dest_cid, t(lang,"tr_nf")); return
    if tu["uid"] == sender_uid: await send(dest_cid, t(lang,"tr_self")); return
    vip = await is_vip(sender_uid)
    fee = 0 if vip else int(amount * TRANSFER_FEE_PCT / 100)
    total = amount + fee
    if su["bal"] < total: await send(dest_cid, t(lang,"tr_nob", b=su["bal"])); return
    await add_bal(sender_uid, -total); await add_bal(tu["uid"], amount)
    su2 = await gu(sender_uid)
    tm = await mention(tu["uid"], tu["name"])
    if vip:
        await send(dest_cid, t(lang,"tr_ok_vip", a=amount, to=tm, b=su2["bal"]))
    else:
        await send(dest_cid, t(lang,"tr_ok", a=amount, to=tm, fee=fee, b=su2["bal"]))
    rlang = await get_lang(tu["uid"])
    sm = await mention(sender_uid, su["name"])
    tu2 = await gu(tu["uid"])
    await send(tu["uid"], t(rlang,"tr_recv", fr=sm, a=amount, b=tu2["bal"]))

async def _parse_transfer(uid, cid, lang, parts, reply_msg):
    target = None; amount = None
    if reply_msg and reply_msg.from_user:
        target = reply_msg.from_user.id
        for p in reversed(parts):
            try:
                amount = int(p.replace(",","").replace("k","000").replace("K","000")); break
            except Exception: pass
    elif len(parts) >= 3 and parts[1].startswith("@"):
        target = parts[1]
        try: amount = int(parts[2].replace(",","").replace("k","000").replace("K","000"))
        except Exception: pass
    if target is None or amount is None:
        await send(cid, t(lang,"tr_usage")); return
    await do_transfer(uid, target, amount, cid, lang)

# ── GAME LOGIC ──────────────────────────────────────────────────────────────

async def bj_join(uid, fname, msg_cid, bet):
    if is_pm(msg_cid):
        lang = await get_lang(uid)
        await send(msg_cid, t(lang,"pm_no_play")); return
    acid = msg_cid
    lang = await get_glang(acid)
    await ensure_ids(uid, fname, "")
    if bet < MIN_BET: await temp(msg_cid, t(lang,"bet_low", m=MIN_BET)); return
    if bet > MAX_BET: await temp(msg_cid, t(lang,"bet_high", m=MAX_BET)); return
    u = await gu(uid)
    if u["bal"] < bet: await temp(msg_cid, t(lang,"no_bal", b=u["bal"])); return
    if find_player_tab(acid, uid): await temp(msg_cid, t(lang,"in_game")); return
    tb = get_or_new(acid)
    await add_bal(uid, -bet)
    await track_group_player(acid, uid)
    tb["players"].append(dict(uid=uid, name=fname or "P", bet=bet,
                               hand=[], done=False, doubled=False))
    stabs(acid, gtabs(acid))
    ltxt = await lobby_txt(tb, lang, LOBBY_WAIT)
    await tab_set_lobby(tb, ltxt)
    await temp(msg_cid, t(lang,"joined", n=tb["n"], bet=bet), 4)
    if len(tb["players"]) == 1:
        stmr(acid, tb["n"], LOBBY_WAIT, bj_close, acid, tb["n"])

async def bj_close(acid, n):
    tb = find_tab(acid, n)
    if not tb or tb["state"] != "lobby": return
    lang = await tab_lang(tb)
    await tab_del_lobby(tb)
    if not tb["players"]: del_tab(acid, n); return
    await send(acid, t(lang,"game_start", n=n))
    dk = tb["deck"]
    for p in tb["players"]: p["hand"] = [dk.pop(), dk.pop()]
    tb["dealer"] = [dk.pop(), dk.pop()]
    tb["state"] = "playing"; tb["cur"] = 0
    stabs(acid, gtabs(acid))
    if tb["dealer"][0][0] == "A":
        await bj_offer_insurance(acid, n)
    else:
        await bj_prompt(acid, n)

INSURANCE_TIME = 15

async def bj_offer_insurance(acid, n):
    tb = find_tab(acid, n)
    if not tb: return
    lang = await tab_lang(tb)
    tb["state"] = "insurance"; tb["ins_resp"] = {}
    stabs(acid, gtabs(acid))
    btxt = await board_txt(tb, lang)
    await tab_set_board(tb, btxt)
    await tab_set_action(tb, t(lang,"insurance_offer", secs=INSURANCE_TIME))
    stmr(acid, n, INSURANCE_TIME, bj_insurance_resolve, acid, n)

async def bj_insurance_choice(acid, n, uid, want):
    tb = find_tab(acid, n)
    if not tb or tb["state"] != "insurance": return
    p = next((p for p in tb["players"] if p["uid"] == uid), None)
    if not p or uid in tb["ins_resp"]: return
    tb["ins_resp"][uid] = want
    lang = await tab_lang(tb)
    mn = await mention(uid, p["name"])
    await send(tb["acid"], t(lang, "insurance_taken" if want else "insurance_declined", mention=mn))
    if len(tb["ins_resp"]) >= len(tb["players"]):
        ctmr(acid, n)
        await bj_insurance_resolve(acid, n)

async def bj_insurance_resolve(acid, n):
    tb = find_tab(acid, n)
    if not tb or tb["state"] != "insurance": return
    lang = await tab_lang(tb)
    await tab_del_action(tb)
    dealer_bj = is_bj(tb["dealer"])
    lines = []
    for p in tb["players"]:
        if not tb["ins_resp"].get(p["uid"], False): continue
        u = await gu(p["uid"])
        cost = min(p["bet"] // 2, u["bal"])
        if cost <= 0: continue
        await add_bal(p["uid"], -cost)
        mn = await mention(p["uid"], p["name"])
        if dealer_bj:
            profit = cost * 2
            await add_bal(p["uid"], cost + profit)
            lines.append(t(lang,"insurance_win", mention=mn, w=profit))
        else:
            lines.append(t(lang,"insurance_lose", mention=mn, l=cost))
    if lines: await tab_send_all(tb, "\n".join(lines))
    tb["state"] = "playing"
    if dealer_bj:
        for p in tb["players"]: p["done"] = True
        tb["cur"] = len(tb["players"])
        stabs(acid, gtabs(acid))
        await tab_send_all(tb, t(lang,"dealer_bj_reveal"))
        await bj_dealer(acid, n)
    else:
        stabs(acid, gtabs(acid))
        await bj_prompt(acid, n)

async def bj_prompt(acid, n):
    tb = find_tab(acid, n)
    if not tb: return
    idx = tb["cur"]
    if idx >= len(tb["players"]): await bj_dealer(acid, n); return
    p = tb["players"][idx]; uid = p["uid"]
    lang = await tab_lang(tb)
    btxt = await board_txt(tb, lang, hi=idx)
    await tab_set_board(tb, btxt)
    mn = await mention(uid, p["name"])
    atxt = t(lang,"your_turn", mention=mn, board=btxt, secs=TURN_TIME)
    await tab_set_action(tb, atxt, kb=game_kb(lang, acid, n))
    stmr(acid, n, TURN_TIME, bj_autostand, acid, n, idx)

async def bj_autostand(acid, n, idx):
    tb = find_tab(acid, n)
    if not tb or tb["cur"] != idx: return
    await tab_del_action(tb)
    tb["players"][idx]["done"] = True; tb["cur"] += 1
    stabs(acid, gtabs(acid))
    await tab_send_all(tb, t(await tab_lang(tb), "auto_stand"))
    await bj_prompt(acid, n)

async def bj_vip_bust_expire(acid, n, idx):
    tb = find_tab(acid, n)
    if not tb or tb["cur"] != idx: return
    await tab_del_action(tb)
    tb["players"][idx]["done"] = True; tb["cur"] += 1
    stabs(acid, gtabs(acid))
    await bj_prompt(acid, n)

async def bj_action(acid, n, uid, act):
    tb = find_tab(acid, n)
    if not tb or tb["state"] != "playing": return
    idx = tb["cur"]
    if idx >= len(tb["players"]): return
    p = tb["players"][idx]
    lang = await tab_lang(tb)
    dest = tb["acid"]
    if p["uid"] != uid: await send(dest, t(lang,"not_ur")); return
    ctmr(acid, n); await tab_del_action(tb); dk = tb["deck"]

    if act == "hit":
        p["hand"].append(dk.pop())
        if htot(p["hand"]) > 21:
            if await is_vip(uid):
                btxt = await board_txt(tb, lang, hi=idx)
                await tab_set_board(tb, btxt)
                kb = None
                await tab_set_action(tb,
                    t(lang,"vip_bust_swap", secs=VIP_BUST_SWAP_TIME, board=btxt), kb=kb)
                stmr(acid, n, VIP_BUST_SWAP_TIME, bj_vip_bust_expire, acid, n, idx)
                return
            else:
                p["done"] = True; tb["cur"] += 1
                stabs(acid, gtabs(acid)); await bj_prompt(acid, n)
                return
        stabs(acid, gtabs(acid)); await bj_prompt(acid, n)

    elif act == "stand":
        mn = await mention(uid, p["name"])
        await send(dest, t(lang,"stood", mention=mn))
        p["done"] = True; tb["cur"] += 1
        stabs(acid, gtabs(acid)); await bj_prompt(acid, n)

    elif act == "double":
        u = await gu(uid)
        if u["bal"] < p["bet"]:
            await send(dest, t(lang,"dbl_low", n=p["bet"]-u["bal"]))
            stmr(acid, n, TURN_TIME, bj_autostand, acid, n, idx); return
        await add_bal(uid, -p["bet"]); p["bet"] *= 2; p["doubled"] = True
        p["hand"].append(dk.pop())
        await send(dest, t(lang,"doubled", b=p["bet"]))
        if htot(p["hand"]) > 21 and await is_vip(uid):
            btxt = await board_txt(tb, lang, hi=idx)
            await tab_set_board(tb, btxt)
            await tab_set_action(tb,
                t(lang,"vip_bust_swap", secs=VIP_BUST_SWAP_TIME, board=btxt))
            stmr(acid, n, VIP_BUST_SWAP_TIME, bj_vip_bust_expire, acid, n, idx)
            return
        p["done"] = True; tb["cur"] += 1
        stabs(acid, gtabs(acid)); await bj_prompt(acid, n)

    elif act == "swap":
        if not await is_vip(uid):
            await send(dest, t(lang,"swap_vip_only"))
            was_bust = htot(p["hand"]) > 21
            if was_bust:
                p["done"] = True; tb["cur"] += 1
                stabs(acid, gtabs(acid)); await bj_prompt(acid, n); return
            stmr(acid, n, TURN_TIME, bj_autostand, acid, n, idx); return
        was_bust = htot(p["hand"]) > 21
        if len(p["hand"]) < 3 and not was_bust:
            await send(dest, t(lang,"swap_first"))
            stmr(acid, n, TURN_TIME, bj_autostand, acid, n, idx); return
        p["hand"][-1] = dk.pop()
        if not was_bust: await send(dest, t(lang,"swap_ok_vip"))
        if htot(p["hand"]) > 21:
            p["done"] = True; tb["cur"] += 1
        stabs(acid, gtabs(acid)); await bj_prompt(acid, n)

async def bj_dealer(acid, n):
    tb = find_tab(acid, n)
    if not tb: return
    lang = await tab_lang(tb)
    await tab_del_action(tb)
    await tab_send_all(tb, t(lang,"d_reveals")); await asyncio.sleep(0.8)
    await tab_set_board(tb, await board_txt(tb, lang, full_dlr=True))
    dl = tb["dealer"]
    while dealer_should_hit(dl) and tb["deck"]:
        await asyncio.sleep(0.8); c = tb["deck"].pop(); dl.append(c)
        await tab_send_all(tb, t(lang,"d_hits", c=cstr(c), t=htot(dl)))
    if htot(dl) <= 21: await tab_send_all(tb, t(lang,"d_stands", t=htot(dl)))
    else:              await tab_send_all(tb, t(lang,"d_busts",  t=htot(dl)))
    await asyncio.sleep(0.5); await bj_results(acid, n)

async def bj_results(acid, n):
    tb = find_tab(acid, n)
    if not tb: return
    lang = await tab_lang(tb); dt = htot(tb["dealer"]); lines = []
    dlbl = TX.get(lang, TX["en"]).get("dlabel","DEALER")
    dlr = f"<b>{dlbl}:</b>  {hstr(tb['dealer'])}  [{dt}]"
    for p in tb["players"]:
        uid = p["uid"]; bet = p["bet"]; pt = htot(p["hand"])
        mn  = await mention(uid, p["name"])
        hand = f"<code>{hstr(p['hand'])}</code> [{pt}]"
        if is_bj(p["hand"]) and not is_bj(tb["dealer"]):
            win = int(bet * 1.5); await add_bal(uid, bet + win)
            await bump_stats(uid, won=True)
            lines.append(t(lang,"r_bj", m=mn, h=hand, w=win))
        elif pt > 21:
            await bump_stats(uid, lost=True)
            lines.append(t(lang,"r_bust", m=mn, h=hand, b=bet))
        elif dt > 21 or pt > dt:
            await add_bal(uid, bet * 2)
            await bump_stats(uid, won=True)
            lines.append(t(lang,"r_win", m=mn, h=hand, w=bet))
        elif pt == dt:
            await add_bal(uid, bet)
            await bump_stats(uid)
            lines.append(t(lang,"r_push", m=mn, h=hand, b=bet))
        else:
            await bump_stats(uid, lost=True)
            lines.append(t(lang,"r_lose", m=mn, h=hand, b=bet))
    await tab_del_board(tb)
    await tab_send_all(tb, t(lang,"results", n=n, dlr=dlr, lines="\n".join(lines)))
    del_tab(acid, n)

async def bj_cancel(acid, n):
    tb = find_tab(acid, n)
    if not tb: return False
    ctmr(acid, n)
    await tab_del_lobby(tb); await tab_del_board(tb); await tab_del_action(tb)
    for p in tb["players"]: await add_bal(p["uid"], p["bet"])
    del_tab(acid, n); return True

async def bj_go(uid, fname, msg_cid):
    if is_pm(msg_cid):
        lang = await get_lang(uid); await send(msg_cid, t(lang,"pm_no_play")); return
    acid = msg_cid
    lang = await get_glang(acid)
    await ensure_ids(uid, fname, "")
    tb = None
    for x in gtabs(acid):
        if x["state"]=="lobby" and any(p["uid"]==uid for p in x["players"]):
            tb = x; break
    if not tb:
        await temp(msg_cid, t(lang,"cancel_none"), 4); return
    allowed = await is_grp_admin(msg_cid, uid) or uid == CREATOR_ID
    if not allowed:
        await temp(msg_cid, t(lang,"cancel_not_adm"), 4); return
    ctmr(acid, tb["n"])
    asyncio.create_task(bj_close(acid, tb["n"]))

# ── COMMAND IMPLEMENTATIONS ──────────────────────────────────────────────────
async def do_play(uid, fname, msg_cid):
    if is_pm(msg_cid):
        lang = await get_lang(uid)
        await send(msg_cid, t(lang,"pm_no_play")); return
    acid = msg_cid
    lang = await get_glang(acid)
    await ensure_ids(uid, fname, "")
    u = await gu(uid)
    kb = await bet_kb(acid, uid)
    await send(msg_cid, t(lang,"play_choose", bal=u["bal"]), kb=kb)

async def do_bet(uid, fname, msg_cid, parts):
    if is_pm(msg_cid):
        lang = await get_lang(uid); await send(msg_cid, t(lang,"pm_no_play")); return
    lang = await get_glang(msg_cid)
    if len(parts) < 2:
        await send(msg_cid, t(lang,"bet_low", m=MIN_BET)); return
    try:
        bet = int(parts[1].replace(",","").replace("k","000").replace("K","000"))
    except Exception:
        await send(msg_cid, t(lang,"bet_low", m=MIN_BET)); return
    await bj_join(uid, fname, msg_cid, bet)

async def do_profile(uid, cid):
    await send(cid, await profile_txt(uid, cid))


# ── CARD TOSS SYSTEM ─────────────────────────────────────────────────────────
TOSS_COOLDOWN    = 15 * 60   # 15 minutes
TOSS_MAX_DAY     = 5
TOSS_CHIPS_MIN   = 100
TOSS_CHIPS_MAX   = 500
TOSS_TOKENS_MIN  = 50
TOSS_TOKENS_MAX  = 100
CARD_SUITS = ["♠","♥","♦","♣"]
CARD_RANKS = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]

async def get_cardtoss(uid: int) -> dict:
    row = await dbq("SELECT * FROM cardtoss_stats WHERE uid=$1", uid)
    if not row:
        await dbx("INSERT INTO cardtoss_stats(uid) VALUES($1) ON CONFLICT DO NOTHING", uid)
        return {"uid": uid, "tokens": 0, "card_level": 1,
                "tosses_today": 0, "last_toss_reset": 0, "last_toss": 0}
    return dict(row)

async def add_tokens(uid: int, amount: int):
    await dbx("INSERT INTO cardtoss_stats(uid,tokens) VALUES($1,$2) ON CONFLICT(uid) "
              "DO UPDATE SET tokens=cardtoss_stats.tokens+$2", uid, amount)

def level_to_card(level: int) -> str:
    """Maps a player's card_level to a representative rank, for display flavor only —
    the actual outcome is decided by comparing power levels directly, not by this card."""
    tiers = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]
    idx = min(level - 1, len(tiers) - 1) if level >= 1 else 0
    idx = max(idx, 0)
    return tiers[idx] + random.choice(CARD_SUITS)

async def do_cardtoss(uid, fname, cid, reply_msg):
    lang = await eff_lang(cid, uid)
    if not reply_msg or not reply_msg.from_user:
        await send(cid, t(lang, "toss_no_reply")); return
    t_uid = reply_msg.from_user.id
    t_fname = reply_msg.from_user.first_name or ""
    if t_uid == uid:
        await send(cid, t(lang, "toss_self")); return

    ds = await get_cardtoss(uid)
    now = int(time.time())
    # reset daily counter if new day
    if now - ds["last_toss_reset"] >= 86400:
        await dbx("UPDATE cardtoss_stats SET tosses_today=0, last_toss_reset=$1 WHERE uid=$2", now, uid)
        ds["tosses_today"] = 0

    if ds["tosses_today"] >= TOSS_MAX_DAY:
        await send(cid, t(lang, "toss_limit")); return

    cd = ds["last_toss"] + TOSS_COOLDOWN - now
    if cd > 0:
        await send(cid, t(lang, "toss_cooldown", t=fmt_dur(cd, lang))); return

    opp = await get_cardtoss(t_uid)
    my_lvl, opp_lvl = ds["card_level"], opp["card_level"]

    # power level decides the outcome directly — higher level always wins,
    # lower level always loses, equal level is a coin flip.
    if my_lvl > opp_lvl:
        i_win = True
    elif my_lvl < opp_lvl:
        i_win = False
    else:
        i_win = random.random() < 0.5

    a_card = level_to_card(my_lvl)
    b_card = level_to_card(opp_lvl)
    chips  = random.randint(TOSS_CHIPS_MIN, TOSS_CHIPS_MAX)
    tokens = random.randint(TOSS_TOKENS_MIN, TOSS_TOKENS_MAX)

    await dbx("UPDATE cardtoss_stats SET tosses_today=tosses_today+1, last_toss=$1 WHERE uid=$2",
              now, uid)

    if i_win:
        # only the challenger's win pays out — losing grants nothing to either side
        await add_bal(uid, chips)
        await add_tokens(uid, tokens)
        msg = t(lang, "toss_result_win", a=a_card, b=b_card,
                challenger=fname, chips=chips, tokens=tokens)
    else:
        msg = t(lang, "toss_result_loss", a=a_card, b=b_card,
                challenger=fname, opponent=t_fname)

    await send(cid, msg)

async def do_upgrade(uid, cid):
    lang = await eff_lang(cid, uid)
    ds = await get_cardtoss(uid)
    lvl = ds["card_level"]
    cost = lvl * 10  # level 1 = 10, level 2 = 20, etc.
    if cid != uid:  # group — show info only
        await send(cid, t(lang, "upgrade_menu",
                          lvl=lvl, tokens=ds["tokens"], cost=cost))
        return
    # in PM — show with upgrade button
    kb = IKM(inline_keyboard=[[
        IKB(text=t(lang, "btn_upgrade_confirm", cost=cost), callback_data="upgrade|confirm")
    ]])
    await send(cid, t(lang, "upgrade_menu",
                      lvl=lvl, tokens=ds["tokens"], cost=cost), kb=kb)

async def do_bonus(uid, cid):
    lang = await eff_lang(cid, uid)
    if not await is_subscribed(uid):
        await send(cid, t(lang,"need_sub"), kb=channel_kb(lang)); return
    now = int(time.time()); u = await gu(uid)
    if now - u["last_bonus"] < 86400:
        nxt = u["last_bonus"] + 86400 - now
        await send(cid, t(lang,"bonus_wait", t=fmt_dur(nxt, lang))); return
    if u["bal"] > BONUS_MAX_BAL:
        await send(cid, t(lang,"bonus_too_rich", m=BONUS_MAX_BAL)); return
    amt = BONUS_VIP if await is_vip(uid) else BONUS_NORM
    await add_bal(uid, amt)
    await dbx("UPDATE users SET last_bonus=$1 WHERE uid=$2", now, uid)
    u2 = await gu(uid)
    await send(cid, t(lang,"bonus_ok", a=amt, b=u2["bal"]))

async def do_shop(uid, cid):
    lang = await eff_lang(cid, uid)
    if not is_pm(cid):
        kb = await shop_pm_kb(lang)
        await send(cid, t(lang,"shop_pm_only"), kb=kb)
        return
    await send(cid, t(lang,"shop"), kb=shop_main_kb(lang))

async def do_top(uid, cid):
    lang = await get_lang(uid) if is_pm(cid) else await get_glang(cid)
    txt = await top_txt(lang, "balance", cid=cid)
    await send(cid, txt, kb=top_kb(lang,"balance"))

async def do_ref(uid, cid):
    lang = await eff_lang(cid, uid)
    try: bname = (await bot.get_me()).username
    except Exception: bname = "blackjack_bot"
    u = await gu(uid); link = f"https://t.me/{bname}?start=ref_{u['ref_code']}"
    await send(cid, t(lang,"ref_msg"), kb=share_kb(lang, link))

async def do_vip(uid, cid):
    lang = await get_lang(uid) if is_pm(cid) else await get_glang(cid)
    if not is_pm(cid):
        kb = await shop_pm_kb(lang)
        await send(cid, t(lang,"vip_info"), kb=kb)
        return
    await send(cid, t(lang,"vip_info"))

async def do_settings(uid, cid):
    lang = await eff_lang(cid, uid)
    await send(cid, t(lang,"settings"), kb=settings_kb(lang))

async def do_cancel(uid, cid, is_admin):
    if is_pm(cid): return
    acid = cid
    lang = await get_glang(cid)
    tb = find_player_tab(acid, uid)
    if not tb:
        await send(cid, t(lang,"cancel_none")); return
    if not is_admin:
        await send(cid, t(lang,"cancel_not_adm")); return
    await bj_cancel(acid, tb["n"])
    await send(cid, t(lang,"cancel_ok"))

async def _game_action(uid, cid, act):
    if is_pm(cid): return
    acid = cid
    tb = find_player_tab(acid, uid)
    lang = await get_glang(acid)
    if not tb: await temp(cid, t(lang,"no_game"), 4); return
    asyncio.create_task(bj_action(acid, tb["n"], uid, act))

async def _insurance_action(uid, cid, want):
    if is_pm(cid): return
    acid = cid
    tb = find_player_tab(acid, uid)
    lang = await get_glang(acid)
    if not tb or tb["state"] != "insurance":
        await temp(cid, t(lang,"no_insurance"), 4); return
    asyncio.create_task(bj_insurance_choice(acid, tb["n"], uid, want))

async def _dispatch(cmd, uid, fname, cid, raw_text, reply_msg, is_admin):
    parts = raw_text.split()
    if   cmd == "play":     await do_play(uid, fname, cid)
    elif cmd == "go":       await bj_go(uid, fname, cid)
    elif cmd == "bet":      await do_bet(uid, fname, cid, parts)
    elif cmd == "hit":      await _game_action(uid, cid, "hit")
    elif cmd == "stand":    await _game_action(uid, cid, "stand")
    elif cmd == "double":   await _game_action(uid, cid, "double")
    elif cmd == "swap":     await _game_action(uid, cid, "swap")
    elif cmd == "insure":   await _insurance_action(uid, cid, True)
    elif cmd == "noinsure": await _insurance_action(uid, cid, False)
    elif cmd == "cancel":   await do_cancel(uid, cid, is_admin)
    elif cmd == "profile":  await do_profile(uid, cid)
    elif cmd == "bonus":    await do_bonus(uid, cid)
    elif cmd == "cardtoss": await do_cardtoss(uid, fname, cid, reply_msg)
    elif cmd == "upgrade":  await do_upgrade(uid, cid)
    elif cmd == "shop":     await do_shop(uid, cid)
    elif cmd == "top":      await do_top(uid, cid)
    elif cmd == "ref":      await do_ref(uid, cid)
    elif cmd == "vip":      await do_vip(uid, cid)
    elif cmd == "settings": await do_settings(uid, cid)
    elif cmd == "help":
        lang = await get_lang(uid) if is_pm(cid) else await get_glang(cid)
        await send(cid, t(lang, "help"))
    elif cmd == "transfer":
        lang = await get_lang(uid) if is_pm(cid) else await get_glang(cid)
        await _parse_transfer(uid, cid, lang, parts, reply_msg)
    elif cmd == "promo":
        if is_pm(cid):
            lang = await get_lang(uid); _user_state[uid] = "promo"
            await send(cid, t(lang,"promo_ask"), kb=back_kb(lang))
        else:
            lang = await get_glang(cid); await send(cid, t(lang,"pm_only"))

# ── /start ──────────────────────────────────────────────────────────────────
_pending_ref: dict = {}   # uid -> ref_code captured at /start, applied after ToS accept

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    uid = msg.from_user.id; fname = msg.from_user.first_name or ""
    cid = msg.chat.id
    await ensure_ids(uid, fname, msg.from_user.username or "")
    args = msg.text.split()
    if len(args) > 1 and args[1] == "shop":
        await do_shop(uid, uid); return
    if len(args) > 1 and args[1].startswith("ref_"):
        _pending_ref[uid] = args[1][4:].upper()
    if not is_pm(cid):
        lang = await get_glang(cid)
        u = await gu(uid)
        await send(cid, t(lang,"welcome", bal=u["bal"]))
        return
    # In PM: if user already accepted ToS, show main menu directly
    u = await gu(uid)
    if u and u.get("tos_lang"):
        lang = u["lang"] or "en"
        rk = await reply_kb(uid)
        await send(cid, t(lang,"welcome", bal=u["bal"]), kb=rk)
        return
    await send(cid, t("en","choose_lang") + " / " + t("ru","choose_lang"), kb=lang_kb("setlang"))

async def _apply_pending_ref(uid, fname, lang):
    rc = _pending_ref.pop(uid, None)
    if not rc: return
    ref = await dbq("SELECT uid FROM users WHERE ref_code=$1", rc)
    u0 = await gu(uid)
    if ref and ref["uid"] != uid and not u0["ref_by"]:
        await dbx("UPDATE users SET ref_by=$1 WHERE uid=$2", ref["uid"], uid)
        await add_bal(uid, REF_NEW_BAL)
        await send(uid, t(lang,"ref_done", a=REF_NEW_BAL))
        rlang = await get_lang(ref["uid"])
        if await is_vip(ref["uid"]):
            await add_stars(ref["uid"], REF_VIP_STARS)
            await send(ref["uid"], t(rlang,"ref_rwd_vip", name=fname))
        else:
            await add_stars(ref["uid"], REF_NORM_STARS)
            await send(ref["uid"], t(rlang,"ref_rwd_norm", name=fname))

# ── SLASH HANDLERS ──────────────────────────────────────────────────────────
@dp.message(Command("play"))
async def cmd_play(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("play", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("go"))
async def cmd_go(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("go", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("bet"))
async def cmd_bet(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("bet", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("hit"))
async def cmd_hit(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("hit", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("stand"))
async def cmd_stand(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("stand", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("double"))
async def cmd_double(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("double", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("swap"))
async def cmd_swap(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("swap", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("insure"))
async def cmd_insure(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("insure", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("noinsure"))
async def cmd_noinsure(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("noinsure", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("cancel", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("profile"))
async def cmd_profile(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("profile", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("bonus"))
async def cmd_bonus(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("bonus", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("shop"))
async def cmd_shop(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("shop", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("top"))
async def cmd_top(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("top", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("ref"))
async def cmd_ref(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("ref", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("vip"))
async def cmd_vip(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("vip", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("settings", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("promo"))
async def cmd_promo(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")

    # Creator only: /promo <тип> <количество> <активаций> <дни_действия> — создать промокод
    if uid == CREATOR_ID:
        p = (msg.text or "").split()
        if len(p) >= 5:
            traw = p[1].lower()
            ptype = "vip" if traw in ("vip", "вип") else \
                    "money" if traw in ("chips", "фишки", "money", "деньги") else None
            if ptype is None:
                await send(cid, "📋 /promo <chips|vip> <количество> <активаций> <дни_действия>")
                return
            try:
                pval = float(p[2]); uses = int(p[3]); days = int(p[4])
            except Exception:
                await send(cid, "📋 /promo <chips|vip> <количество> <активаций> <дни_действия>")
                return
            code = _rc()
            exp = int(time.time()) + days * 86400
            await dbx(
                "INSERT INTO promos(code,ptype,pval,uses,exp,by_uid,created) VALUES($1,$2,$3,$4,$5,$6,$7)",
                code, ptype, pval, uses, exp, uid, int(time.time()))
            if ptype == "vip":
                await send(cid, f"✅ Промокод <code>{code}</code>  👑 VIP {int(pval)} д  × {uses}  (действует {days} дн.)")
            else:
                await send(cid, f"✅ Промокод <code>{code}</code>  💰 {int(pval):,}¢  × {uses}  (действует {days} дн.)")
            return

    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("promo", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("help", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("cards", "toss"))
async def cmd_cards(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("cardtoss", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("upgrade"))
async def cmd_upgrade(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    await do_upgrade(uid, cid)

@dp.message(Command("transfer"))
async def cmd_transfer(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    await ensure_ids(uid, msg.from_user.first_name or "", msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    lang = await get_lang(uid) if is_pm(cid) else await get_glang(cid)
    await _parse_transfer(uid, cid, lang, (msg.text or "").split(), msg.reply_to_message)

# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────
def _cr(msg: Message): return msg.from_user.id == CREATOR_ID

async def _resolve_uid(arg: str):
    if arg.lstrip("-").isdigit(): return await gu(int(arg))
    return await gu_un(arg)

@dp.message(Command("addc"))
async def cmd_addc(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) < 4:
        await send(msg.chat.id, "📋 /addc <КОД> <фишки> <активаций> [дней_действия]"); return
    code = p[1].upper(); chips = float(p[2]); uses = int(p[3])
    exp = int(time.time()) + int(p[4])*86400 if len(p)>4 else int(time.time())+365*86400
    await dbx(
        "INSERT INTO promos(code,ptype,pval,uses,exp,by_uid,created) VALUES($1,$2,$3,$4,$5,$6,$7) "
        "ON CONFLICT (code) DO UPDATE SET ptype=$2,pval=$3,uses=$4,exp=$5",
        code, "money", chips, uses, exp, msg.from_user.id, int(time.time()))
    await send(msg.chat.id, f"✅ Промокод <code>{code}</code>  💰 {int(chips):,}¢  × {uses} активаций")

@dp.message(Command("addvip"))
async def cmd_addvip(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) < 4:
        await send(msg.chat.id, "📋 /addvip <КОД> <дней_вип> <активаций> [дней_действия]"); return
    code = p[1].upper(); days = float(p[2]); uses = int(p[3])
    exp = int(time.time()) + int(p[4])*86400 if len(p)>4 else int(time.time())+365*86400
    await dbx(
        "INSERT INTO promos(code,ptype,pval,uses,exp,by_uid,created) VALUES($1,$2,$3,$4,$5,$6,$7) "
        "ON CONFLICT (code) DO UPDATE SET ptype=$2,pval=$3,uses=$4,exp=$5",
        code, "vip", days, uses, exp, msg.from_user.id, int(time.time()))
    await send(msg.chat.id, f"✅ Промокод <code>{code}</code>  👑 VIP {int(days)} д  × {uses} активаций")

@dp.message(Command("botstats"))
async def cmd_botstats(msg: Message):
    if not _cr(msg): return
    u = (await dbq("SELECT COUNT(*) c FROM users"))["c"]
    v = (await dbq("SELECT COUNT(*) c FROM users WHERE vip_perm=TRUE OR vip_until>$1", int(time.time())))["c"]
    b = (await dbq("SELECT COALESCE(SUM(bal),0) c FROM users"))["c"]
    g = (await dbq("SELECT COALESCE(SUM(g_bj),0) c FROM users"))["c"]
    pr = (await dbq("SELECT COUNT(*) c FROM promos"))["c"]
    bn = (await dbq("SELECT COUNT(*) c FROM users WHERE banned_until>$1", int(time.time())))["c"]
    lines = [
        "📊 <b>Bot Stats</b>",
        "👤 Пользователей: <b>" + str(u) + "</b>",
        "👑 VIP: <b>" + str(v) + "</b>",
        "🚫 В бане: <b>" + str(bn) + "</b>",
        "💰 Баланс всех: <b>" + "{:,}".format(b) + "¢</b>",
        "🎮 Игр: <b>" + str(g) + "</b>",
        "🎟 Промокодов: <b>" + str(pr) + "</b>",
    ]
    await send(msg.chat.id, "\n".join(lines))

@dp.message(Command("botland"))
async def cmd_botland(msg: Message):
    if not _cr(msg): return
    rows = await dba("SELECT lang, COUNT(*) cnt FROM users GROUP BY lang ORDER BY cnt DESC")
    flags = {"en": "🇬🇧", "ru": "🇷🇺"}
    lines = [flags.get(r["lang"], "🌍") + " <b>" + r["lang"] + "</b>: " + str(r["cnt"]) + " чел." for r in rows]
    await send(msg.chat.id, "🌍 <b>Языки пользователей:</b>\n" + "\n".join(lines))

@dp.message(Command("givevip"))
async def cmd_givevip(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) < 3:
        await send(msg.chat.id, "📋 /givevip <uid|@user> <дней|-1=навсегда>"); return
    tu = await _resolve_uid(p[1])
    if not tu: await send(msg.chat.id, "❌ Пользователь не найден"); return
    d = int(p[2])
    if d == -1:
        await set_vip_perm(tu["uid"]); label = "навсегда ♾️"
    else:
        await extend_vip(tu["uid"], d); label = f"{d} дней"
    mn = await mention(tu["uid"], tu["name"])
    await send(msg.chat.id, f"✅ VIP <b>{label}</b> → {mn} (<code>{tu['uid']}</code>)")

@dp.message(Command("givec"))
async def cmd_givec(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) < 3:
        await send(msg.chat.id, "📋 /givec <uid|@user> <фишки>"); return
    tu = await _resolve_uid(p[1])
    if not tu: await send(msg.chat.id, "❌ Пользователь не найден"); return
    try: chips = int(p[2])
    except Exception: await send(msg.chat.id, "❌ Некорректное число"); return
    await add_bal(tu["uid"], chips)
    u2 = await gu(tu["uid"])
    mn = await mention(tu["uid"], tu["name"])
    await send(msg.chat.id,
        "✅ +<b>" + "{:,}".format(chips) + "¢</b> → " + mn +
        "\nБаланс: <b>" + "{:,}".format(u2["bal"]) + "¢</b>")

@dp.message(Command("takec"))
async def cmd_takec(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) < 3:
        await send(msg.chat.id, "📋 /takec <uid|@user> <фишки>"); return
    tu = await _resolve_uid(p[1])
    if not tu: await send(msg.chat.id, "❌ Пользователь не найден"); return
    try: chips = int(p[2])
    except Exception: await send(msg.chat.id, "❌ Некорректное число"); return
    await add_bal(tu["uid"], -chips)
    u2 = await gu(tu["uid"])
    mn = await mention(tu["uid"], tu["name"])
    await send(msg.chat.id,
        "✅ −<b>" + "{:,}".format(chips) + "¢</b> у " + mn +
        "\nБаланс: <b>" + "{:,}".format(u2["bal"]) + "¢</b>")

@dp.message(Command("givestars"))
async def cmd_givestars(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) < 3:
        await send(msg.chat.id, "📋 /givestars <uid|@user> <алмазы>"); return
    tu = await _resolve_uid(p[1])
    if not tu: await send(msg.chat.id, "❌ Пользователь не найден"); return
    try: amt = int(p[2])
    except Exception: await send(msg.chat.id, "❌ Некорректное число"); return
    await add_stars(tu["uid"], amt)
    u2 = await gu(tu["uid"])
    mn = await mention(tu["uid"], tu["name"])
    await send(msg.chat.id,
        "✅ +<b>" + "{:,}".format(amt) + "💎</b> → " + mn +
        "\nБаланс звёзд: <b>" + "{:,}".format(u2["bot_stars"]) + "💎</b>")

@dp.message(Command("takestars"))
async def cmd_takestars(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) < 3:
        await send(msg.chat.id, "📋 /takestars <uid|@user> <алмазы>"); return
    tu = await _resolve_uid(p[1])
    if not tu: await send(msg.chat.id, "❌ Пользователь не найден"); return
    try: amt = int(p[2])
    except Exception: await send(msg.chat.id, "❌ Некорректное число"); return
    await add_stars(tu["uid"], -amt)
    u2 = await gu(tu["uid"])
    mn = await mention(tu["uid"], tu["name"])
    await send(msg.chat.id,
        "✅ −<b>" + "{:,}".format(amt) + "💎</b> у " + mn +
        "\nБаланс звёзд: <b>" + "{:,}".format(u2["bot_stars"]) + "💎</b>")

@dp.message(Command("reftop"))
async def cmd_reftop(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    days = 7
    if len(p) >= 2:
        try: days = max(1, int(p[1]))
        except Exception:
            await send(msg.chat.id, "📋 /reftop <дней>  (например /reftop 7)"); return
    since = int(time.time()) - days*86400
    rows = await dba(
        "SELECT ref_by AS uid, COUNT(*) AS n FROM users "
        "WHERE ref_by IS NOT NULL AND joined >= $1 "
        "GROUP BY ref_by ORDER BY n DESC LIMIT 15", since)
    if not rows:
        await send(msg.chat.id, f"📊 За последние {days} дн. рефералов не было."); return
    lines = []
    for i, r in enumerate(rows):
        ru = await gu(r["uid"])
        mn = await mention(r["uid"], ru["name"] if ru else str(r["uid"]))
        lines.append(f"{i+1}. {mn} — <b>{r['n']}</b> реф.")
    await send(msg.chat.id, f"📊 <b>Топ по рефералам за {days} дн.</b>\n\n" + "\n".join(lines))

@dp.message(Command("takevip"))
async def cmd_takevip(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) < 2:
        await send(msg.chat.id, "📋 /takevip <uid|@user>"); return
    tu = await _resolve_uid(p[1])
    if not tu: await send(msg.chat.id, "❌ Пользователь не найден"); return
    await take_vip(tu["uid"])
    mn = await mention(tu["uid"], tu["name"])
    await send(msg.chat.id, f"✅ VIP снят у {mn}")

@dp.message(Command("ban"))
async def cmd_ban(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) < 3:
        await send(msg.chat.id, "📋 /ban <uid|@user> <дней>"); return
    tu = await _resolve_uid(p[1])
    if not tu: await send(msg.chat.id, "❌ Пользователь не найден"); return
    try: days = int(p[2])
    except Exception: await send(msg.chat.id, "❌ Некорректное число"); return
    until = await ban_user(tu["uid"], days)
    mn = await mention(tu["uid"], tu["name"])
    await send(msg.chat.id, f"🚫 {mn} забанен до <b>{fmt_ts(until)}</b>")

@dp.message(Command("economy"))
async def cmd_economy(msg: Message):
    if not _cr(msg): return
    total = (await dbq("SELECT COALESCE(SUM(bal),0) c FROM users"))["c"]
    cnt   = (await dbq("SELECT COUNT(*) c FROM users"))["c"]
    avg   = total / cnt if cnt else 0
    base_str = await get_setting("economy_baseline", "")
    if not base_str:
        await set_setting("economy_baseline", str(total))
        base = total
    else:
        base = float(base_str)
    infl = ((total - base) / base * 100) if base else 0
    purchases_cnt = (await dbq("SELECT COUNT(*) c FROM purchases"))["c"]
    purchases_stars = (await dbq("SELECT COALESCE(SUM(stars),0) c FROM purchases"))["c"]
    lines = [
        "💹 <b>Экономика бота</b>",
        "💰 Всего фишек в обороте: <b>" + "{:,}".format(int(total)) + "¢</b>",
        "👤 Пользователей: <b>" + str(cnt) + "</b>",
        "📊 Средний баланс: <b>" + "{:,}".format(int(avg)) + "¢</b>",
        "📈 Инфляция от базовой точки: <b>" + f"{infl:.1f}" + "%</b>",
        "🛍 Покупок: <b>" + str(purchases_cnt) + "</b>  (" + "{:,}".format(purchases_stars) + "⭐)",
    ]
    await send(msg.chat.id, "\n".join(lines))

@dp.message(Command("setbaseline"))
async def cmd_setbaseline(msg: Message):
    if not _cr(msg): return
    total = (await dbq("SELECT COALESCE(SUM(bal),0) c FROM users"))["c"]
    await set_setting("economy_baseline", str(total))
    await send(msg.chat.id, f"✅ Точка отсчёта инфляции обновлена: {int(total):,}¢")

@dp.message(Command("richtop"))
async def cmd_richtop(msg: Message):
    if not _cr(msg): return
    rows = await dba("SELECT uid,name,bal,joined,w_bj,l_bj,g_bj FROM users WHERE uid!=$1 "
                      "ORDER BY bal DESC LIMIT 15", CREATOR_ID)
    lines = ["💎 <b>Топ богатых (полная история)</b>"]
    for i, r in enumerate(rows):
        mn = await mention(r["uid"], r["name"])
        lines.append(
            f"{i+1}. {mn} (<code>{r['uid']}</code>)\n"
            f"   💰 {r['bal']:,}¢  ·  🎮 {r['g_bj']} игр  ·  🏆 {r['w_bj']}W/{r['l_bj']}L\n"
            f"   📅 с {fmt_ts(r['joined'])}"
        )
    await send(msg.chat.id, "\n\n".join(lines))

@dp.message(Command("purchases"))
async def cmd_purchases(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) >= 2:
        tu = await _resolve_uid(p[1])
        if not tu: await send(msg.chat.id, "❌ Пользователь не найден"); return
        rows = await dba("SELECT * FROM purchases WHERE uid=$1 ORDER BY ts DESC LIMIT 30", tu["uid"])
        title = f"🛍 <b>Покупки {tu['name']}</b> (<code>{tu['uid']}</code>)"
    else:
        rows = await dba("SELECT * FROM purchases ORDER BY ts DESC LIMIT 30")
        title = "🛍 <b>Последние 30 покупок</b>"
    if not rows:
        await send(msg.chat.id, title + "\n\nПусто."); return
    lines = [title, ""]
    for r in rows:
        lines.append(f"<code>{r['uid']}</code>  {r['item']}  {r['stars']}⭐  {fmt_ts(r['ts'])}")
    await send(msg.chat.id, "\n".join(lines))

@dp.message(Command("export"))
async def cmd_export(msg: Message):
    if not _cr(msg): return
    rows = await dba("SELECT * FROM users")
    promos = await dba("SELECT * FROM promos")
    settings_rows = await dba("SELECT * FROM settings")
    data = {"users": rows, "promos": promos, "settings": settings_rows,
            "exported_at": int(time.time())}
    buf = json.dumps(data, default=str, ensure_ascii=False, indent=2).encode("utf-8")
    fname = f"bj_export_{int(time.time())}.json"
    await msg.answer_document(BufferedInputFile(buf, filename=fname),
        caption=f"📦 Экспорт: {len(rows)} пользователей")

@dp.message(Command("import"))
async def cmd_import(msg: Message):
    if not _cr(msg): return
    if not msg.reply_to_message or not msg.reply_to_message.document:
        await send(msg.chat.id, "📋 Ответь командой /import на сообщение с JSON-файлом экспорта"); return
    doc = msg.reply_to_message.document
    file = await bot.get_file(doc.file_id)
    buf = await bot.download_file(file.file_path)
    try:
        data = json.loads(buf.read().decode("utf-8"))
    except Exception as e:
        await send(msg.chat.id, f"❌ Не удалось разобрать JSON: {e}"); return
    n = 0
    for u in data.get("users", []):
        try:
            await dbx(
                "INSERT INTO users(uid,username,name,lang,bal,vip_until,vip_perm,"
                "w_bj,l_bj,g_bj,last_bonus,ref_code,ref_by,joined,banned_until) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) "
                "ON CONFLICT (uid) DO UPDATE SET bal=$5,vip_until=$6,vip_perm=$7,"
                "w_bj=$8,l_bj=$9,g_bj=$10,banned_until=$15",
                u["uid"], u.get("username",""), u.get("name",""), u.get("lang","en"),
                u["bal"], u.get("vip_until",0), u.get("vip_perm",False),
                u.get("w_bj",0), u.get("l_bj",0), u.get("g_bj",0),
                u.get("last_bonus",0), u.get("ref_code") or _rc(), u.get("ref_by"),
                u.get("joined", int(time.time())), u.get("banned_until",0))
            n += 1
        except Exception as e:
            log.warning(f"import row failed uid={u.get('uid')}: {e}")
    await send(msg.chat.id, f"✅ Импортировано {n} пользователей")

@dp.message(Command("settos"))
async def cmd_settos(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split(maxsplit=2)
    if len(p) < 2 or p[1] not in ("ru","en"):
        await send(msg.chat.id, "📋 /settos <ru|en> <текст соглашения>\n\n"
                   "Можно отправить без текста — бот спросит текст следующим сообщением."); return
    lang = p[1]
    if len(p) >= 3:
        await set_setting(f"tos_{lang}", p[2])
        await send(msg.chat.id, f"✅ ToS для {lang} обновлено.")
    else:
        _user_state[msg.from_user.id] = f"settos_{lang}"
        await send(msg.chat.id, f"📝 Пришли текст соглашения для <b>{lang}</b> следующим сообщением:")

@dp.message(Command("broadcast"))
async def cmd_broadcast(msg: Message):
    if not _cr(msg): return
    raw = (msg.text or "").split(maxsplit=1)
    args = raw[1] if len(raw) > 1 else ""

    # "BTN:" marker separates message text from button definitions.
    # Buttons: "Текст|ссылка; Текст2|ссылка2"
    body_text, _, btn_part = args.partition("BTN:")
    body_text = body_text.strip()
    btn_part = btn_part.strip()

    kb = None
    if btn_part:
        rows_kb = []
        for part in btn_part.split(";"):
            part = part.strip()
            if "|" not in part:
                continue
            label, url = part.split("|", 1)
            label, url = label.strip(), url.strip()
            if label and url:
                rows_kb.append([IKB(text=label, url=url)])
        if rows_kb:
            kb = IKM(inline_keyboard=rows_kb)

    if not msg.reply_to_message and not body_text:
        await send(msg.chat.id,
            "📋 <b>/broadcast</b>\n\n"
            "Текст: <code>/broadcast Привет всем!</code>\n"
            "Фото/видео/опрос/файл: ответь этой командой на нужное сообщение — разойдётся всем как есть\n"
            "Кнопки (можно добавить в обоих случаях): "
            "<code>/broadcast BTN: Текст|ссылка; Текст2|ссылка2</code>")
        return

    rows = await dba("SELECT uid FROM users"); n = 0; fail = 0

    if msg.reply_to_message:
        src_cid, src_mid = msg.chat.id, msg.reply_to_message.message_id
        for row in rows:
            try:
                await bot.copy_message(chat_id=row["uid"], from_chat_id=src_cid,
                                        message_id=src_mid, reply_markup=kb)
                n += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
    else:
        for row in rows:
            try:
                await bot.send_message(row["uid"], body_text, reply_markup=kb)
                n += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)

    await send(msg.chat.id, f"✅ Отправлено: <b>{n}</b>  ❌ Не доставлено: <b>{fail}</b>")

@dp.message(Command("addgroup"))
async def cmd_addgroup(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split(maxsplit=3)
    if len(p) < 4:
        await send(msg.chat.id, "📋 /addgroup &lt;ссылка&gt; &lt;@username&gt; &lt;Название&gt;"); return
    link = p[1]; username = p[2] if p[2] != "-" else None; title = p[3]
    await dbx("INSERT INTO bot_groups(title,username,link,added_by,added_at) VALUES($1,$2,$3,$4,$5)",
              title, username, link, msg.from_user.id, int(time.time()))
    await send(msg.chat.id, f"✅ Группа добавлена: <b>{html.escape(title)}</b>")

@dp.message(Command("rmgroup"))
async def cmd_rmgroup(msg: Message):
    if not _cr(msg): return
    rows = await dba("SELECT id, title FROM bot_groups ORDER BY id ASC")
    if not rows:
        await send(msg.chat.id, "❌ Нет групп."); return
    p = (msg.text or "").split()
    if len(p) < 2:
        lines = ["📋 /rmgroup &lt;id&gt;\n"] + [f"<code>{r['id']}</code> — {html.escape(r['title'])}" for r in rows]
        await send(msg.chat.id, "\n".join(lines)); return
    try: gid = int(p[1])
    except Exception: await send(msg.chat.id, "❌ Укажи числовой id."); return
    await dbx("DELETE FROM bot_groups WHERE id=$1", gid)
    await send(msg.chat.id, f"✅ Группа #{gid} удалена.")

@dp.message(Command("listgroups"))
async def cmd_listgroups(msg: Message):
    if not _cr(msg): return
    rows = await dba("SELECT id, title, link FROM bot_groups ORDER BY id ASC")
    if not rows:
        await send(msg.chat.id, "Групп нет."); return
    lines = [f"<code>{r['id']}</code> — <b>{html.escape(r['title'])}</b>  {r['link']}" for r in rows]
    await send(msg.chat.id, "🎮 <b>Группы:</b>\n" + "\n".join(lines))

@dp.message(Command("setgrplang"))
async def cmd_setgrplang(msg: Message):
    cid = msg.chat.id
    if is_pm(cid):
        lang = await get_lang(msg.from_user.id)
        await send(cid, t(lang,"pm_only")); return
    if not await is_grp_admin(cid, msg.from_user.id) and msg.from_user.id != CREATOR_ID:
        lang = await get_glang(cid)
        await send(cid, t(lang,"grp_adm_only")); return
    await send(cid, "🌍 Choose group language / Выбери язык группы:", kb=lang_kb("grplang"))

# ── TEXT ROUTER ─────────────────────────────────────────────────────────────
@dp.message(F.text)
async def text_router(msg: Message):
    if not msg.text: return
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    raw = msg.text.strip(); lo = raw.lower()
    await ensure_ids(uid, fname, msg.from_user.username or "")

    if await is_banned(uid) and not raw.startswith("/start"):
        u = await gu(uid); lang = await eff_lang(cid, uid)
        if is_pm(cid): await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"])))
        return

    if is_pm(cid):
        state = _user_state.get(uid)
        if state == "promo":
            del _user_state[uid]; await apply_promo(uid, raw, cid); return
        if state == "report":
            words = raw.split()
            lang = await get_lang(uid)
            if len(words) < REPORT_MIN_WORDS:
                await send(cid, t(lang,"report_short", n=REPORT_MIN_WORDS)); return
            del _user_state[uid]
            await send(cid, t(lang,"report_sent"))
            mn = await mention(uid, fname)
            await send(CREATOR_ID, t("en","report_recv", mention=mn, uid=uid, text=html.escape(raw)))
            return
        if state in ("settos_ru", "settos_en") and uid == CREATOR_ID:
            lang = state.split("_")[1]
            del _user_state[uid]
            await set_setting(f"tos_{lang}", raw)
            await send(cid, f"✅ ToS для {lang} обновлено.")
            return

    if not is_pm(cid) and not raw.startswith("/"):
        # Try full text match first (reply keyboard buttons)
        cmd = _KB_CMD.get(lo)
        if not cmd:
            # Try first word, stripping leading @ and non-letter chars
            words = lo.split()
            if words:
                # strip leading non-alphanumeric (emojis etc.) from first word
                first = words[0].lstrip("@")
                cmd = _AL.get(first)
            if not cmd:
                # try joining all words (e.g. "взять карту")
                cmd = _AL.get(" ".join(words)) if words else None
        if not cmd: return
        ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
        await _dispatch(cmd, uid, fname, cid, raw, msg.reply_to_message, ia)
        return

    if is_pm(cid):
        cmd = _KB_CMD.get(lo)
        if not cmd:
            words = lo.split()
            if words:
                first = words[0].lstrip("@")
                cmd = _AL.get(first)
        if cmd:
            await _dispatch(cmd, uid, fname, cid, raw, msg.reply_to_message, True)

# ── CALLBACK HANDLERS ───────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("setlang|"))
async def cb_setlang(c: CallbackQuery):
    uid = c.from_user.id; lang = c.data.split("|")[1]
    await ensure_ids(uid, c.from_user.first_name or "", c.from_user.username or "")
    await set_lang_u(uid, lang); await c.answer()
    try: await c.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    tos_text = await get_setting(f"tos_{lang}", t(lang,"tos_default"))
    quoted = "<blockquote expandable>" + html.escape(tos_text) + "</blockquote>"
    await send(uid, t(lang,"tos_title") + "\n\n" + quoted, kb=tos_kb(lang))

@dp.callback_query(F.data.startswith("tos_ok|"))
async def cb_tos_ok(c: CallbackQuery):
    uid = c.from_user.id; lang = c.data.split("|")[1]
    fname = c.from_user.first_name or ""
    await c.answer()
    try: await c.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await dbx("UPDATE users SET tos_lang=$1 WHERE uid=$2", lang, uid)
    await _apply_pending_ref(uid, fname, lang)
    u = await gu(uid)
    rk = await reply_kb(uid)
    await send(uid, t(lang,"welcome", bal=u["bal"]), kb=rk)

@dp.callback_query(F.data.startswith("lang|"))
async def cb_lang(c: CallbackQuery):
    uid = c.from_user.id; lang = c.data.split("|")[1]
    await ensure_ids(uid, c.from_user.first_name or "", c.from_user.username or "")
    await set_lang_u(uid, lang); await c.answer()
    try: await c.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    rk = await reply_kb(uid)
    await send(uid, t(lang,"lang_ok"), kb=rk)

@dp.callback_query(F.data.startswith("grplang|"))
async def cb_grplang(c: CallbackQuery):
    cid = c.message.chat.id; lang = c.data.split("|")[1]
    if not await is_grp_admin(cid, c.from_user.id) and c.from_user.id != CREATOR_ID:
        glang = await get_glang(cid)
        await c.answer(t(glang,"grp_adm_only"), show_alert=True); return
    await set_glang(cid, lang); await c.answer()
    try: await c.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await send(cid, t(lang,"grp_lang_ok", lang=LANGS.get(lang,lang)))

@dp.callback_query(F.data.startswith("set|"))
async def cb_settings(c: CallbackQuery):
    uid = c.from_user.id; cid = c.message.chat.id
    act = c.data.split("|")[1]; lang = await eff_lang(cid, uid); await c.answer()
    if act == "lang":
        await sedit(cid, c.message.message_id, t(lang,"choose_lang"), kb=lang_kb("lang"))
    elif act == "tos":
        tos_text = await get_setting(f"tos_{lang}", t(lang,"tos_default"))
        quoted = "<blockquote expandable>" + html.escape(tos_text) + "</blockquote>"
        await sedit(cid, c.message.message_id, f"{t(lang,'tos_title')}\n\n{quoted}", kb=back_kb(lang))
    elif act == "groups":
        rows = await dba("SELECT title, link FROM bot_groups ORDER BY id ASC")
        if not rows:
            await sedit(cid, c.message.message_id, t(lang,"groups_empty"), kb=back_kb(lang))
        else:
            kb2 = [[IKB(text=r["title"], url=r["link"])] for r in rows]
            kb2.append([IKB(text=t(lang,"btn_back"), callback_data="set|back")])
            await sedit(cid, c.message.message_id, t(lang,"groups_title"), kb=IKM(inline_keyboard=kb2))
    elif act == "promo":
        if is_pm(cid):
            _user_state[uid] = "promo"
            await sedit(cid, c.message.message_id, t(lang,"promo_ask"), kb=back_kb(lang))
        else:
            await c.answer(t(lang,"pm_only"), show_alert=True)
    elif act == "help":
        await sedit(cid, c.message.message_id, t(lang,"help"), kb=back_kb(lang))
    elif act == "howto":
        await sedit(cid, c.message.message_id, t(lang,"how_to_play"), kb=back_kb(lang))
    elif act == "report":
        if is_pm(cid):
            _user_state[uid] = "report"
            await sedit(cid, c.message.message_id,
                t(lang,"report_ask", n=REPORT_MIN_WORDS), kb=back_kb(lang))
        else:
            await c.answer(t(lang,"pm_only"), show_alert=True)
    elif act == "back":
        _user_state.pop(uid, None)
        await sedit(cid, c.message.message_id, t(lang,"settings"), kb=settings_kb(lang))

@dp.callback_query(F.data.startswith("top|"))
async def cb_top(c: CallbackQuery):
    mode = c.data.split("|")[1]; uid = c.from_user.id; cid = c.message.chat.id
    lang = await get_lang(uid) if is_pm(cid) else await get_glang(cid)
    await c.answer()
    txt = await top_txt(lang, mode, cid=cid)
    await sedit(cid, c.message.message_id, txt, kb=top_kb(lang,mode))

@dp.callback_query(F.data.startswith("QB|"))
async def cb_qbet(c: CallbackQuery):
    parts = c.data.split("|"); acid = int(parts[1]); bet = int(parts[2])
    uid = c.from_user.id; fname = c.from_user.first_name or ""
    await ensure_ids(uid, fname, c.from_user.username or ""); await c.answer()
    try: await bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception: pass
    await bj_join(uid, fname, c.message.chat.id, bet)

@dp.callback_query(F.data.startswith("shopm|"))
async def cb_shopm(c: CallbackQuery):
    uid = c.from_user.id; cid = c.message.chat.id
    lang = await eff_lang(cid, uid); act = c.data.split("|")[1]; await c.answer()
    if act == "stars":
        await sedit(cid, c.message.message_id, t(lang,"shop_stars_title"), kb=shop_starpacks_kb(lang))
    elif act == "items":
        bs = await get_stars(uid)
        await sedit(cid, c.message.message_id, t(lang,"shop_items_title", bs=bs), kb=shop_items_kb(lang))
    else:  # back
        await sedit(cid, c.message.message_id, t(lang,"shop"), kb=shop_main_kb(lang))

@dp.callback_query(F.data.startswith("buystar|"))
async def cb_buystar(c: CallbackQuery):
    uid = c.from_user.id; cid = c.message.chat.id
    lang = await eff_lang(cid, uid); code = c.data.split("|")[1]
    if code not in STAR_PACKS: await c.answer(); return
    label, tg_cost, bs_amt = STAR_PACKS[code]; await c.answer()
    kb = IKM(inline_keyboard=[[
        IKB(text=t(lang,"btn_yes"), callback_data=f"confirmstar|{code}"),
        IKB(text=t(lang,"btn_no"),  callback_data="shopm|stars"),
    ]])
    await sedit(cid, c.message.message_id, t(lang,"confirm", item=label, stars=tg_cost), kb=kb)

@dp.callback_query(F.data.startswith("confirmstar|"))
async def cb_confirmstar(c: CallbackQuery):
    uid = c.from_user.id; code = c.data.split("|")[1]
    lang = await get_lang(uid); await c.answer()
    if code not in STAR_PACKS: return
    label, tg_cost, bs_amt = STAR_PACKS[code]
    try: await bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception: pass
    try:
        await bot.send_invoice(
            chat_id=uid, title=label, description=f"Blackjack Bot — {label}",
            payload=f"bjstar_{code}_{uid}", currency="XTR",
            prices=[LabeledPrice(label=label, amount=tg_cost)], provider_token="")
    except Exception as e:
        log.error(f"star invoice error: {e}")
        await send(uid, t(lang,"pay_fail"), kb=support_kb(lang))
        await send(CREATOR_ID, t("en","pay_fail_cr", uid=uid,
                   name=c.from_user.first_name or "", item=label))

@dp.callback_query(F.data.startswith("buyitem|"))
async def cb_buyitem(c: CallbackQuery):
    uid = c.from_user.id; cid = c.message.chat.id
    lang = await eff_lang(cid, uid); item = c.data.split("|")[1]
    if item not in SHOP_ITEMS: await c.answer(); return
    name, cost, *_ = SHOP_ITEMS[item]; await c.answer()
    bs = await get_stars(uid)
    if bs < cost:
        await c.answer(t(lang,"not_enough_stars", have=bs, need=cost).replace("<b>","").replace("</b>",""), show_alert=True)
        return
    kb = IKM(inline_keyboard=[[
        IKB(text=t(lang,"btn_yes"), callback_data=f"confirmitem|{item}"),
        IKB(text=t(lang,"btn_no"),  callback_data="shopm|items"),
    ]])
    await sedit(cid, c.message.message_id, t(lang,"confirm_stars", item=name, stars=cost), kb=kb)

@dp.callback_query(F.data.startswith("confirmitem|"))
async def cb_confirmitem(c: CallbackQuery):
    uid = c.from_user.id; cid = c.message.chat.id
    lang = await eff_lang(cid, uid); item = c.data.split("|")[1]; await c.answer()
    if item not in SHOP_ITEMS: return
    name, cost, kind, val, extra = SHOP_ITEMS[item]
    bs = await get_stars(uid)
    if bs < cost:
        await sedit(cid, c.message.message_id,
                     t(lang,"not_enough_stars", have=bs, need=cost), kb=shop_items_kb(lang))
        return
    await add_stars(uid, -cost)
    if kind == "vip":
        if val == -1: await set_vip_perm(uid)
        else:         await extend_vip(uid, val)
    elif kind == "chips":
        await add_bal(uid, val)
    await record_purchase(uid, item, cost)
    await sedit(cid, c.message.message_id, t(lang,"pay_ok", item=name), kb=None)

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    try: await q.answer(ok=True)
    except Exception: pass

@dp.message(F.successful_payment)
async def payment_ok(msg: Message):
    uid = msg.from_user.id
    payload = msg.successful_payment.invoice_payload
    # payload format: "bjstar_{code}_{uid}" — real TG Stars purchase, credits diamonds
    parts = payload.split("_", 2)
    kind_tag = parts[0] if parts else ""
    code = parts[1] if len(parts) >= 3 else None
    lang = await get_lang(uid)
    if kind_tag == "bjstar" and code in STAR_PACKS:
        label, tg_cost, bs_amt = STAR_PACKS[code]
        await add_stars(uid, bs_amt)
        await record_purchase(uid, f"star_{code}", tg_cost)
        rk = await reply_kb(uid)
        await send(uid, t(lang,"pay_ok_stars", amt=bs_amt), kb=rk)
    else:
        await send(uid, "✅ Payment received!")

@dp.callback_query(F.data == "upgrade|confirm")
async def cb_upgrade(c: CallbackQuery):
    uid = c.from_user.id
    lang = await get_lang(uid)
    ds = await get_cardtoss(uid)
    lvl = ds["card_level"]
    cost = lvl * 10
    if ds["tokens"] < cost:
        await c.answer(t(lang, "upgrade_no_tokens", cost=cost, tokens=ds["tokens"]).replace("<b>","").replace("</b>",""), show_alert=True)
        return
    await dbx("UPDATE cardtoss_stats SET tokens=tokens-$1, card_level=card_level+1 WHERE uid=$2", cost, uid)
    ds2 = await get_cardtoss(uid)
    await c.answer()
    new_cost = ds2["card_level"] * 10
    await c.message.edit_text(
        t(lang, "upgrade_ok", lvl=ds2["card_level"], tokens=ds2["tokens"]) + "\n\n" +
        t(lang, "upgrade_menu", lvl=ds2["card_level"], tokens=ds2["tokens"],
          cost=new_cost),
        parse_mode="HTML",
        reply_markup=IKM(inline_keyboard=[[
            IKB(text=t(lang, "btn_upgrade_confirm", cost=new_cost), callback_data="upgrade|confirm")
        ]])
    )

def _is_game_cb(data: str) -> bool:
    return bool(data) and data[:3] in ("BJH", "BJS", "BJD", "BJW")

@dp.callback_query(F.data.func(_is_game_cb))
async def cb_game(c: CallbackQuery):
    prefix = c.data[:3]; parts = c.data.split("|")
    acid = int(parts[1]); n = int(parts[2]); uid = c.from_user.id
    ACT = {"BJH":"hit","BJS":"stand","BJD":"double","BJW":"swap"}
    await c.answer()
    asyncio.create_task(bj_action(acid, n, uid, ACT[prefix]))

# ── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    log.info("Diamondting (aiogram3 + PostgreSQL)…")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message","callback_query","pre_checkout_query"])

if __name__ == "__main__":
    asyncio.run(main())
