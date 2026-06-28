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
BOT_TOKEN        = os.getenv("BOT_TOKEN", "8161712628:AAHdnTBNyNehzvK4S0kMqnZh2spMtl5NEfU")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
CREATOR_ID       = int(os.getenv("CREATOR_ID", "6714200331"))
CREATOR_UN       = "alexplaay"
DATABASE_URL     = os.getenv("DATABASE_URL", "postgresql://localhost/bjbot")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@BJonlinec")   # e.g. "@mychannel" — required to claim bonus/promo
MIN_BET, TURN_TIME = 100, 30
LOBBY_WAIT          = 60
VIP_BUST_SWAP_TIME  = 30
START_BAL = 10_000
BONUS_VIP, BONUS_NORM = 5_000, 2_500
BONUS_MAX_BAL = 50_000   # balance above this — daily bonus is not given
REF_VIP_BAL  = 10_000   # chips for VIP referrer per invite
REF_NORM_BAL = 5_000    # chips for regular referrer per invite
REF_NEW_BAL  = 5_000    # chips for the invited friend
TRANSFER_FEE_PCT = 10
PM_CID = -999_999_999
REPORT_MIN_WORDS = 5

# ── SHOP ───────────────────────────────────────────────────────────────────
SHOP_ITEMS = {
    "vip30": ("👑 VIP 30 дней",           39,  "vip",   30,      None),
    "vip90": ("👑 VIP 90 дней (-15%)",    99,  "vip",   90,      None),
    "vipp":  ("👑 VIP Навсегда",          399, "vip",   -1,      None),
    "top1":  ("💰 100 000¢",              49,  "chips", 100_000, None),
    "top2":  ("💰 210 000¢ (-4%)",        99,  "chips", 210_000, None),
    "top3":  ("💰 550 000¢ (-8%)",        249, "chips", 550_000, None),
    "top4":  ("💰 1 150 000¢ (-11%)",     499, "chips", 1_150_000, None),
    "top5":  ("💰 2 500 000¢ (-18%)",     999, "chips", 2_500_000, None),
    "hot1":  ("🔥 100 000¢ + VIP 30д (-33%)", 59, "bundle", 100_000, 30),
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
    'help': '📚 <b>Blackjack Bot — Help</b>\n\nMost commands work as a /slash or as a plain word in a group chat (e.g. <code>/play</code> = <code>play</code>).\n\n<b>🃏 Blackjack</b> (groups only)\n/play — pick a bet and join the table\n/go — start the table now, skip the wait\n/bet &lt;amount&gt; — join with a custom bet\n/hit — draw a card\n/stand — stop drawing\n/double — double your bet for one more card\n/swap — replace your last card (👑 VIP only)\n/cancel — cancel the table, bets are returned (admins only)\n\n<b>🎲 Card Toss</b>\n/cards or /toss — reply to someone\'s message to toss against them\nWin chance starts at 50% and shifts with your card level vs theirs (25–75% range)\n/upgrade — spend 🪙 tokens won from tosses to level up your card (+1% win chance per level)\n\n<b>👤 Account</b>\n/profile — balance, VIP status, game stats\n/bonus — daily reward (channel subscribers only, balance under 50,000¢)\n/top — leaderboard by balance, wins or win%\n/ref — your invite link, +5,000¢ per friend (+10,000¢ for 👑 VIP)\n/transfer @user &lt;amount&gt; — send chips, 10% fee (free for 👑 VIP)\n/shop — buy VIP or chips (PM only)\n/vip — VIP perks and prices\n/promo — redeem a promo code\n/settings — language, rules, groups, support',
    'profile': '👤 {mention}\n💰 Balance: <b>{bal:,}¢</b>\n{vip_line}\n\n📊 Wins <b>{bw}</b> · Losses <b>{bl}</b> · Games <b>{bg}</b> · Win rate <b>{pct}%</b>',
    'vip_active': '👑 VIP until <b>{d}</b> ({left} left)',
    'vip_perm': '👑 VIP: <b>forever ♾️</b>',
    'no_vip': '⚪ No VIP — /shop',
    'vip_info': '👑 <b>VIP</b>\n\n🎁 Daily bonus: <b>5,000¢</b> (vs 2,500¢)\n👑 Crown next to your name\n🔄 Card swap unlocked\n💸 No transfer fee (vs 10%)\n🔗 Referral bonus: <b>+10,000¢</b> (vs +5,000¢)\n\n🛍 <b>Prices</b>\n30 days — <b>39⭐</b>\n90 days — <b>99⭐</b> (-15%)\nForever — <b>399⭐</b>\n\n/shop',
    'bonus_ok': '🎁 Daily bonus +<b>{a:,}¢</b>\nBalance: <b>{b:,}¢</b>',
    'bonus_wait': '⏳ Already claimed today. Next in <b>{t}</b>.',
    'bonus_too_rich': '❌ Bonus is only available with a balance under <b>{m:,}¢</b>.',
    'need_sub': '⚠️ Subscribe to our channel to claim this.',
    'ref_msg': '🔗 <b>Invite friends</b>\n\nYou get <b>+5,000¢</b> per invite (👑 VIP: +10,000¢).\nYour friend gets <b>+5,000¢</b> too.\n\nTap below to share your link:',
    'ref_done': '🎉 Welcome bonus +<b>{a:,}¢</b>!',
    'ref_rwd_vip': '🎉 <b>{name}</b> joined via your link! +<b>10,000¢</b>',
    'ref_rwd_norm': '🎉 <b>{name}</b> joined via your link! +<b>5,000¢</b>',
    'top_title': '🏆 <b>Top 15 — {mode}</b>\n\n{lines}',
    'top_title_grp': '🏆 <b>Top 15 in this group — {mode}</b>\n\n{lines}',
    'top_bal_lbl': '💰 Balance',
    'top_win_lbl': '🏆 Wins',
    'top_pct_lbl': '📈 Win%',
    'top_line_bal': '<b>{i}.</b> {m}  —  <b>{v:,}¢</b>',
    'top_line_win': '<b>{i}.</b> {m}  —  <b>{v}</b> 🏆',
    'top_line_pct': '<b>{i}.</b> {m}  —  <b>{v}%</b>  ({g})',
    'top_empty': 'No players yet.',
    'shop': '🛍 <b>Shop</b>\n\n👑 <b>VIP</b>\n30 days — <b>39⭐</b>\n90 days — <b>99⭐</b> (-15%)\nForever — <b>399⭐</b>\n\n💰 <b>Chips</b>\n100,000 — <b>49⭐</b>\n210,000 — <b>99⭐</b> (-4%)\n550,000 — <b>249⭐</b> (-8%)\n1,150,000 — <b>499⭐</b> (-11%)\n2,500,000 — <b>999⭐</b> (-18%)\n\n🔥 <b>Hot deal</b>\n100,000¢ + VIP 30d — <b>59⭐</b> (-33%)',
    'shop_pm_only': '🛍 Shop is only available in PM. Tap below to open it:',
    'confirm': '✅ <b>Confirm purchase</b>\n\n{item}\n<b>{stars}⭐</b>',
    'pay_ok': '✅ Purchase confirmed — <b>{item}</b> is active.',
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
    'in_game': '⚠️ You\'re already at a table.',
    'bet_low': '❌ Minimum bet: <b>{m:,}¢</b>',
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
    'tr_usage': 'ℹ️ /transfer @user &lt;amount&gt; [comment]\nor reply to a message + /transfer &lt;amount&gt; [comment]\n\nFee: 10%  (👑 VIP: 0%)',
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
    'btn_top': '🏆 Top',
    'btn_ref': '🔗 Referral',
    'btn_settings': '⚙️ Settings',
    'btn_lang': '🌍 Language',
    'btn_promo': '🎟 Promo',
    'btn_upgrade': '🆙 Upgrade',
    'btn_upgrade_confirm': '⬆️ Upgrade ({cost} 🪙)',
    'btn_help': '📚 Help',
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
    'toss_result_win': '🎲 <b>Card Toss</b>\n\n🃏 {a}  vs  🃏 {b}\n🎯 {challenger}\'s odds: <b>{chance}%</b>\n\n🏆 <b>{challenger}</b> wins!\n💰 +{chips:,}¢  🪙 +{tokens}',
    'toss_result_loss': '🎲 <b>Card Toss</b>\n\n🃏 {a}  vs  🃏 {b}\n🎯 {challenger}\'s odds: <b>{chance}%</b>\n\n🏆 <b>{opponent}</b> wins!\n😔 {challenger} loses.',
    'upgrade_menu': '🆙 <b>Card Upgrade</b>\n\nEach level: <b>+{bonus}%</b> win chance in 🎲 Card Toss.\n\n🃏 Level: <b>{lvl}</b>\n🪙 Tokens: <b>{tokens}</b>\n\nNext level: <b>{cost} 🪙</b>',
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
    'help': '📚 <b>Blackjack Bot — Помощь</b>\n\nБольшинство команд работают как /слэш или просто словом в группе (например <code>/play</code> = <code>играть</code>).\n\n<b>🃏 Blackjack</b> (только в группах)\n/play — выбрать ставку и сесть за стол\n/go — начать стол сразу, не дожидаясь сбора игроков\n/bet &lt;сумма&gt; — сесть со своей ставкой\n/hit — взять карту\n/stand — остановиться, не брать карту\n/double — удвоить ставку и взять последнюю карту\n/swap — заменить последнюю карту (только 👑 VIP)\n/cancel — отменить стол, ставки вернутся (только админы)\n\n<b>🎲 Карточный бросок</b>\n/cards или /toss — ответь на сообщение игрока, чтобы бросить карту против него\nШанс на победу начинается от 50% и меняется в зависимости от уровня твоей карты против его (диапазон 25–75%)\n/upgrade — потрать 🪙 жетоны, выигранные в бросках, чтобы прокачать карту (+1% к шансу за уровень)\n\n<b>👤 Аккаунт</b>\n/profile — баланс, статус VIP, статистика игр\n/bonus — ежедневная награда (только подписчикам канала, баланс должен быть меньше 50 000¢)\n/top — таблица лидеров по балансу, победам или % побед\n/ref — твоя ссылка для приглашений, +5 000¢ за друга (+10 000¢ для 👑 VIP)\n/transfer @юзер &lt;сумма&gt; — перевод фишек, комиссия 10% (бесплатно для 👑 VIP)\n/shop — купить VIP или фишки (только в ЛС)\n/vip — привилегии VIP и цены\n/promo — ввести промокод\n/settings — язык, правила, группы, поддержка',
    'profile': '👤 {mention}\n💰 Баланс: <b>{bal:,}¢</b>\n{vip_line}\n\n📊 Побед <b>{bw}</b> · Поражений <b>{bl}</b> · Игр <b>{bg}</b> · % побед <b>{pct}%</b>',
    'vip_active': '👑 VIP до <b>{d}</b> (осталось {left})',
    'vip_perm': '👑 VIP: <b>навсегда ♾️</b>',
    'no_vip': '⚪ VIP не активен — /shop',
    'vip_info': '👑 <b>VIP</b>\n\n🎁 Ежедневный бонус: <b>5 000¢</b> (обычно 2 500¢)\n👑 Корона рядом с именем\n🔄 Доступна замена карты\n💸 Без комиссии за перевод (обычно 10%)\n🔗 Бонус за реферала: <b>+10 000¢</b> (обычно +5 000¢)\n\n🛍 <b>Цены</b>\n30 дней — <b>39⭐</b>\n90 дней — <b>99⭐</b> (-15%)\nНавсегда — <b>399⭐</b>\n\n/shop',
    'bonus_ok': '🎁 Ежедневный бонус +<b>{a:,}¢</b>\nБаланс: <b>{b:,}¢</b>',
    'bonus_wait': '⏳ Бонус уже получен сегодня. Следующий через <b>{t}</b>.',
    'bonus_too_rich': '❌ Бонус доступен только при балансе меньше <b>{m:,}¢</b>.',
    'need_sub': '⚠️ Подпишись на наш канал, чтобы получить это.',
    'ref_msg': '🔗 <b>Зови друзей</b>\n\nТы получаешь <b>+5 000¢</b> за каждого друга (👑 VIP: +10 000¢).\nДруг тоже получает <b>+5 000¢</b>.\n\nНажми кнопку ниже, чтобы поделиться ссылкой:',
    'ref_done': '🎉 Бонус за регистрацию по ссылке +<b>{a:,}¢</b>!',
    'ref_rwd_vip': '🎉 <b>{name}</b> зарегистрировался по твоей ссылке! +<b>10 000¢</b>',
    'ref_rwd_norm': '🎉 <b>{name}</b> зарегистрировался по твоей ссылке! +<b>5 000¢</b>',
    'top_title': '🏆 <b>Топ 15 — {mode}</b>\n\n{lines}',
    'top_title_grp': '🏆 <b>Топ 15 этой группы — {mode}</b>\n\n{lines}',
    'top_bal_lbl': '💰 Баланс',
    'top_win_lbl': '🏆 Победы',
    'top_pct_lbl': '📈 % побед',
    'top_line_bal': '<b>{i}.</b> {m}  —  <b>{v:,}¢</b>',
    'top_line_win': '<b>{i}.</b> {m}  —  <b>{v}</b> 🏆',
    'top_line_pct': '<b>{i}.</b> {m}  —  <b>{v}%</b>  ({g})',
    'top_empty': 'Пока нет игроков.',
    'shop': '🛍 <b>Магазин</b>\n\n👑 <b>VIP</b>\n30 дней — <b>39⭐</b>\n90 дней — <b>99⭐</b> (-15%)\nНавсегда — <b>399⭐</b>\n\n💰 <b>Фишки</b>\n100 000 — <b>49⭐</b>\n210 000 — <b>99⭐</b> (-4%)\n550 000 — <b>249⭐</b> (-8%)\n1 150 000 — <b>499⭐</b> (-11%)\n2 500 000 — <b>999⭐</b> (-18%)\n\n🔥 <b>Горячее</b>\n100 000¢ + VIP 30д — <b>59⭐</b> (-33%)',
    'shop_pm_only': '🛍 Магазин доступен только в личных сообщениях. Открой его кнопкой ниже:',
    'confirm': '✅ <b>Подтверди покупку</b>\n\n{item}\n<b>{stars}⭐</b>',
    'pay_ok': '✅ Покупка подтверждена — <b>{item}</b> активировано.',
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
    'in_game': '⚠️ Ты уже за столом.',
    'bet_low': '❌ Минимальная ставка: <b>{m:,}¢</b>',
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
    'tr_usage': 'ℹ️ /transfer @юзер &lt;сумма&gt; [комментарий]\nили ответь на сообщение + /transfer &lt;сумма&gt; [комментарий]\n\nКомиссия: 10%  (👑 VIP: 0%)',
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
    'btn_top': '🏆 Топ',
    'btn_ref': '🔗 Реферал',
    'btn_settings': '⚙️ Настройки',
    'btn_lang': '🌍 Язык',
    'btn_promo': '🎟 Промокод',
    'btn_upgrade': '🆙 Прокачка',
    'btn_upgrade_confirm': '⬆️ Прокачать ({cost} 🪙)',
    'btn_help': '📚 Помощь',
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
    'toss_result_win': '🎲 <b>Карточный бросок</b>\n\n🃏 {a}  vs  🃏 {b}\n🎯 Шанс {challenger}: <b>{chance}%</b>\n\n🏆 <b>{challenger}</b> побеждает!\n💰 +{chips:,}¢  🪙 +{tokens}',
    'toss_result_loss': '🎲 <b>Карточный бросок</b>\n\n🃏 {a}  vs  🃏 {b}\n🎯 Шанс {challenger}: <b>{chance}%</b>\n\n🏆 <b>{opponent}</b> побеждает!\n😔 {challenger} проигрывает.',
    'upgrade_menu': '🆙 <b>Прокачка карты</b>\n\nКаждый уровень: <b>+{bonus}%</b> к шансу победы в 🎲 Карточном броске.\n\n🃏 Уровень: <b>{lvl}</b>\n🪙 Жетонов: <b>{tokens}</b>\n\nСледующий уровень: <b>{cost} 🪙</b>',
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
    tos_lang TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS group_settings(
    cid BIGINT PRIMARY KEY,
    lang TEXT DEFAULT 'en'
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
def cstr(c): return f"{c[0]}{c[1]}"
def hstr(h): return "  ".join(cstr(c) for c in h)
def is_bj(h): return len(h)==2 and htot(h)==21
def hdsp(h, hide=False):
    if hide and len(h)>=2: return f"{cstr(h[0])}  🂠  [{cval(h[0])}+?]"
    return f"{hstr(h)}  [{htot(h)}]"

DEALER_WEAKNESS = 0.10   # 10% chance the dealer makes a suboptimal early stand
def dealer_should_hit(total):
    """Normally hit while <17. 10% of the time, stand early (below 17) — weaker dealer."""
    if total >= 17: return False
    if random.random() < DEALER_WEAKNESS: return False
    return True

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
        if tb["state"] == "lobby": return tb
    # Если нет лобби — проверяем нет ли активной игры
    tabs = gtabs(acid)
    if tabs:
        # Один стол на группу — если есть активная игра, возвращаем её лобби или None
        pass
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

async def profile_txt(uid):
    u = await gu(uid); lang = await get_lang(uid); vl = await vip_left(uid)
    if u["vip_perm"]: vline = t(lang,"vip_perm")
    elif vl > 0:      vline = t(lang,"vip_active", d=fmt_ts(u["vip_until"]), left=fmt_dur(int(vl),lang))
    else:             vline = t(lang,"no_vip")
    mn = await mention(uid, u["name"])
    return t(lang,"profile", mention=mn, bal=u["bal"],
             vip_line=vline, bw=u["w_bj"], bl=u["l_bj"], bg=u["g_bj"], pct=win_pct(u))

async def top_txt(lang, mode="balance", cid=None):
    """In groups show group top, in PM always show global top."""
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
    """PM reply keyboard."""
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

def shop_kb():
    rows = []
    for code in ("vip30","vip90","vipp"):
        name, stars, *_ = SHOP_ITEMS[code]
        rows.append([IKB(text=f"{name} — {stars}⭐", callback_data=f"buy|{code}")])
    for code in ("top1","top2","top3","top4","top5"):
        name, stars, *_ = SHOP_ITEMS[code]
        rows.append([IKB(text=f"{name} — {stars}⭐", callback_data=f"buy|{code}")])
    name, stars, *_ = SHOP_ITEMS["hot1"]
    rows.append([IKB(text=f"{name} — {stars}⭐", callback_data="buy|hot1")])
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
        [IKB(text=t(lang,"btn_promo"), callback_data="set|promo"),
         IKB(text=t(lang,"btn_help"),  callback_data="set|help")],
        [IKB(text=t(lang,"btn_tos"),    callback_data="set|tos"),
         IKB(text=t(lang,"btn_groups"), callback_data="set|groups")],
        [IKB(text=t(lang,"btn_vip"),    callback_data="set|vip")],
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
async def do_transfer(sender_uid, target, amount, dest_cid, lang, comment=""):
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
    comment_line = f"\n💬 <i>{html.escape(comment)}</i>" if comment else ""
    if vip:
        await send(dest_cid, t(lang,"tr_ok_vip", a=amount, to=tm, b=su2["bal"]) + comment_line)
    else:
        await send(dest_cid, t(lang,"tr_ok", a=amount, to=tm, fee=fee, b=su2["bal"]) + comment_line)
    rlang = await get_lang(tu["uid"])
    sm = await mention(sender_uid, su["name"])
    tu2 = await gu(tu["uid"])
    recv_comment = f"\n💬 <i>{html.escape(comment)}</i>" if comment else ""
    await send(tu["uid"], t(rlang,"tr_recv", fr=sm, a=amount, b=tu2["bal"]) + recv_comment)

async def _parse_transfer(uid, cid, lang, parts, reply_msg):
    target = None; amount = None; comment = ""
    if reply_msg and reply_msg.from_user:
        target = reply_msg.from_user.id
        for i, p in enumerate(reversed(parts)):
            try:
                amount = int(p.replace(",","").replace("k","000").replace("K","000"))
                # всё после суммы — комментарий
                idx = len(parts) - 1 - i
                if idx + 1 < len(parts):
                    comment = " ".join(parts[idx+1:])
                break
            except Exception: pass
    elif len(parts) >= 3 and parts[1].startswith("@"):
        target = parts[1]
        try:
            amount = int(parts[2].replace(",","").replace("k","000").replace("K","000"))
            if len(parts) > 3:
                comment = " ".join(parts[3:])
        except Exception: pass
    if target is None or amount is None:
        await send(cid, t(lang,"tr_usage")); return
    await do_transfer(uid, target, amount, cid, lang, comment)

# ── GAME LOGIC ──────────────────────────────────────────────────────────────

async def bj_join(uid, fname, msg_cid, bet):
    if is_pm(msg_cid):
        lang = await get_lang(uid)
        await send(msg_cid, t(lang,"pm_no_play")); return
    acid = msg_cid
    lang = await get_glang(acid)
    await ensure_ids(uid, fname, "")
    if bet < MIN_BET: await temp(msg_cid, t(lang,"bet_low", m=MIN_BET)); return
    u = await gu(uid)
    if u["bal"] < bet: await temp(msg_cid, t(lang,"no_bal", b=u["bal"])); return
    if find_player_tab(acid, uid): await temp(msg_cid, t(lang,"in_game")); return
    # Один стол на группу — если идёт игра, нельзя начать новую
    existing = gtabs(acid)
    if existing and all(tb["state"] != "lobby" for tb in existing):
        await temp(msg_cid, t(lang,"no_game"), 4); return
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
    while dealer_should_hit(htot(dl)) and tb["deck"]:
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
    await send(cid, await profile_txt(uid))


# ── CARD TOSS SYSTEM ─────────────────────────────────────────────────────────
TOSS_COOLDOWN    = 15 * 60   # 15 minutes
TOSS_MAX_DAY     = 5
TOSS_CHIPS_MIN   = 100
TOSS_CHIPS_MAX   = 500
TOSS_TOKENS_MIN  = 50
TOSS_TOKENS_MAX  = 100
TOSS_BASE_CHANCE = 50   # %  — even odds when both players are the same level
TOSS_LEVEL_BONUS = 1    # %  — per level of advantage over the opponent
TOSS_MIN_CHANCE  = 25   # %  — floor, so a low-level player always has a shot
TOSS_MAX_CHANCE  = 75   # %  — ceiling, so it's never a guaranteed win
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

async def do_cardtoss(uid, fname, cid, reply_msg):
    lang = await get_lang(uid)
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
    # the card's level shifts the odds — higher level, better chance to win
    chance = TOSS_BASE_CHANCE + (ds["card_level"] - opp["card_level"]) * TOSS_LEVEL_BONUS
    chance = max(TOSS_MIN_CHANCE, min(TOSS_MAX_CHANCE, chance))

    # cards drawn just for flavor — the chance above decides the outcome
    a_card = random.choice(CARD_RANKS) + random.choice(CARD_SUITS)
    b_card = random.choice(CARD_RANKS) + random.choice(CARD_SUITS)
    chips  = random.randint(TOSS_CHIPS_MIN, TOSS_CHIPS_MAX)
    tokens = random.randint(TOSS_TOKENS_MIN, TOSS_TOKENS_MAX)
    i_win  = random.uniform(0, 100) <= chance

    await dbx("UPDATE cardtoss_stats SET tosses_today=tosses_today+1, last_toss=$1 WHERE uid=$2",
              now, uid)

    if i_win:
        await add_bal(uid, chips)
        await add_tokens(uid, tokens)
        msg = t(lang, "toss_result_win", a=a_card, b=b_card,
                challenger=fname, chance=round(chance), chips=chips, tokens=tokens)
    else:
        await add_bal(t_uid, chips)
        await add_tokens(t_uid, tokens)
        msg = t(lang, "toss_result_loss", a=a_card, b=b_card,
                challenger=fname, opponent=t_fname, chance=round(chance))

    await send(cid, msg)

async def do_upgrade(uid, cid):
    lang = await get_lang(uid)
    ds = await get_cardtoss(uid)
    lvl = ds["card_level"]
    cost = lvl * 10  # level 1 = 10, level 2 = 20, etc.
    if cid != uid:  # group — show info only
        await send(cid, t(lang, "upgrade_menu",
                          lvl=lvl, tokens=ds["tokens"], cost=cost, bonus=TOSS_LEVEL_BONUS))
        return
    # in PM — show with upgrade button
    kb = IKM(inline_keyboard=[[
        IKB(text=t(lang, "btn_upgrade_confirm", cost=cost), callback_data="upgrade|confirm")
    ]])
    await send(cid, t(lang, "upgrade_menu",
                      lvl=lvl, tokens=ds["tokens"], cost=cost, bonus=TOSS_LEVEL_BONUS), kb=kb)

async def do_bonus(uid, cid):
    lang = await get_lang(uid)
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
    lang = await get_lang(uid)
    if not is_pm(cid):
        kb = await shop_pm_kb(lang)
        await send(cid, t(lang,"shop_pm_only"), kb=kb)
        return
    await send(cid, t(lang,"shop"), kb=shop_kb())

async def do_top(uid, cid):
    lang = await get_lang(uid) if is_pm(cid) else await get_glang(cid)
    txt = await top_txt(lang, "balance", cid=cid)
    await send(cid, txt, kb=top_kb(lang,"balance"))

async def do_ref(uid, cid):
    lang = await get_lang(uid)
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
    lang = await get_lang(uid)
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

async def _dispatch(cmd, uid, fname, cid, raw_text, reply_msg, is_admin):
    parts = raw_text.split()
    if   cmd == "play":     await do_play(uid, fname, cid)
    elif cmd == "go":       await bj_go(uid, fname, cid)
    elif cmd == "bet":      await do_bet(uid, fname, cid, parts)
    elif cmd == "hit":      await _game_action(uid, cid, "hit")
    elif cmd == "stand":    await _game_action(uid, cid, "stand")
    elif cmd == "double":   await _game_action(uid, cid, "double")
    elif cmd == "swap":     await _game_action(uid, cid, "swap")
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
            await add_bal(ref["uid"], REF_VIP_BAL)
            await send(ref["uid"], t(rlang,"ref_rwd_vip", name=fname))
        else:
            await add_bal(ref["uid"], REF_NORM_BAL)
            await send(ref["uid"], t(rlang,"ref_rwd_norm", name=fname))

# ── SLASH HANDLERS ──────────────────────────────────────────────────────────
@dp.message(Command("play"))
async def cmd_play(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("play", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("go"))
async def cmd_go(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("go", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("bet"))
async def cmd_bet(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("bet", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("hit"))
async def cmd_hit(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("hit", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("stand"))
async def cmd_stand(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("stand", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("double"))
async def cmd_double(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("double", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("swap"))
async def cmd_swap(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("swap", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("cancel", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("profile"))
async def cmd_profile(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("profile", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("bonus"))
async def cmd_bonus(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("bonus", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("shop"))
async def cmd_shop(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("shop", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("top"))
async def cmd_top(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("top", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("ref"))
async def cmd_ref(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("ref", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("vip"))
async def cmd_vip(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("vip", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
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
        u = await gu(uid); lang = await get_lang(uid)
        await send(cid, t(lang,"banned", until=fmt_ts(u["banned_until"]))); return
    ia = await is_grp_admin(cid, uid) or uid == CREATOR_ID
    await _dispatch("promo", uid, fname, cid, msg.text or "", msg.reply_to_message, ia)

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    uid = msg.from_user.id; cid = msg.chat.id
    fname = msg.from_user.first_name or ""
    await ensure_ids(uid, fname, msg.from_user.username or "")
    if await is_banned(uid):
        u = await gu(uid); lang = await get_lang(uid)
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
        u = await gu(uid); lang = await get_lang(uid)
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

@dp.message(Command("history"))
async def cmd_history(msg: Message):
    if not _cr(msg): return
    p = (msg.text or "").split()
    if len(p) >= 2:
        tu = await _resolve_uid(p[1])
        if not tu: await send(msg.chat.id, "❌ Пользователь не найден"); return
        rows = await dba(
            "SELECT p.item, p.stars, p.ts, u.name FROM purchases p "
            "JOIN users u ON u.uid=p.uid WHERE p.uid=$1 ORDER BY p.ts DESC LIMIT 30", tu["uid"])
        title = f"📋 <b>История транзакций {html.escape(tu['name'])}</b>"
    else:
        rows = await dba(
            "SELECT p.item, p.stars, p.ts, u.name FROM purchases p "
            "JOIN users u ON u.uid=p.uid ORDER BY p.ts DESC LIMIT 30")
        title = "📋 <b>Последние 30 транзакций</b>"
    if not rows:
        await send(msg.chat.id, title + "\n\nПусто."); return
    lines = [title, ""]
    for r in rows:
        lines.append(f"👤 <b>{html.escape(r['name'])}</b>  {r['item']}  {r['stars']}⭐  <i>{fmt_ts(r['ts'])}</i>")
    await send(msg.chat.id, "\n".join(lines))

@dp.message(Command("rich"))
async def cmd_rich(msg: Message):
    if not _cr(msg): return
    rows = await dba(
        "SELECT u.uid, u.name, u.bal, u.w_bj, u.l_bj, u.g_bj, u.joined, "
        "COALESCE(SUM(p.stars),0) AS total_stars "
        "FROM users u LEFT JOIN purchases p ON p.uid=u.uid "
        "WHERE u.uid!=$1 GROUP BY u.uid ORDER BY u.bal DESC LIMIT 20", CREATOR_ID)
    lines = ["💎 <b>Топ 20 богатейших игроков</b>\n"]
    for i, r in enumerate(rows):
        mn = await mention(r["uid"], r["name"])
        source = []
        if r["total_stars"] > 0:
            source.append(f"🛍 {r['total_stars']}⭐ куплено")
        if r["w_bj"] > 0:
            source.append(f"🏆 {r['w_bj']} побед в игре")
        src_str = "  ·  ".join(source) if source else "📊 игровые выигрыши"
        lines.append(
            f"<b>{i+1}.</b> {mn}\n"
            f"   💰 {r['bal']:,}¢  ·  🎮 {r['g_bj']} игр\n"
            f"   📌 {src_str}\n"
            f"   📅 с {fmt_ts(r['joined'])}"
        )
    await send(msg.chat.id, "\n\n".join(lines))

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
        u = await gu(uid); lang = await get_lang(uid)
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
    quoted = "<blockquote>" + html.escape(tos_text) + "</blockquote>"
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
    act = c.data.split("|")[1]; lang = await get_lang(uid); await c.answer()
    if act == "lang":
        await sedit(cid, c.message.message_id, t(lang,"choose_lang"), kb=lang_kb("lang"))
    elif act == "tos":
        tos_text = await get_setting(f"tos_{lang}", t(lang,"tos_default"))
        quoted = "<blockquote>" + html.escape(tos_text) + "</blockquote>"
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
    elif act == "vip":
        await sedit(cid, c.message.message_id, t(lang,"vip_info"), kb=back_kb(lang))
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

@dp.callback_query(F.data.startswith("buy|"))
async def cb_buy(c: CallbackQuery):
    uid = c.from_user.id; item = c.data.split("|")[1]; lang = await get_lang(uid)
    if item not in SHOP_ITEMS: await c.answer(); return
    name, stars, *_ = SHOP_ITEMS[item]; await c.answer()
    kb = IKM(inline_keyboard=[[
        IKB(text=t(lang,"btn_yes"), callback_data=f"confirm|{item}"),
        IKB(text=t(lang,"btn_no"),  callback_data="confirm|cancel"),
    ]])
    await sedit(c.message.chat.id, c.message.message_id,
                t(lang,"confirm", item=name, stars=stars), kb=kb)

@dp.callback_query(F.data.startswith("confirm|"))
async def cb_confirm(c: CallbackQuery):
    uid = c.from_user.id; item = c.data.split("|")[1]; lang = await get_lang(uid); await c.answer()
    if item == "cancel":
        await sedit(c.message.chat.id, c.message.message_id, t(lang,"shop"), kb=shop_kb())
        return
    if item not in SHOP_ITEMS: return
    name, stars, *_ = SHOP_ITEMS[item]
    try: await bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception: pass
    try:
        await bot.send_invoice(
            chat_id=uid, title=name, description=f"Blackjack Bot — {name}",
            payload=f"bj_{item}_{uid}", currency="XTR",
            prices=[LabeledPrice(label=name, amount=stars)], provider_token="")
    except Exception as e:
        log.error(f"invoice error: {e}")
        await send(uid, t(lang,"pay_fail"), kb=support_kb(lang))
        await send(CREATOR_ID, t("en","pay_fail_cr", uid=uid,
                   name=c.from_user.first_name or "", item=name))

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    try: await q.answer(ok=True)
    except Exception: pass

@dp.message(F.successful_payment)
async def payment_ok(msg: Message):
    uid = msg.from_user.id
    payload = msg.successful_payment.invoice_payload
    # payload format: "bj_{item}_{uid}"
    parts = payload.split("_", 2)
    item = parts[1] if len(parts) >= 3 else None
    lang = await get_lang(uid)
    if not item or item not in SHOP_ITEMS:
        await send(uid, "✅ Payment received!"); return
    name, stars, kind, val, extra = SHOP_ITEMS[item]
    if kind == "vip":
        if val == -1: await set_vip_perm(uid)
        else:         await extend_vip(uid, val)
    elif kind == "chips":
        await add_bal(uid, val)
    elif kind == "bundle":
        await add_bal(uid, val)
        if extra: await extend_vip(uid, extra)
    await record_purchase(uid, item, stars)
    rk = await reply_kb(uid)
    await send(uid, t(lang,"pay_ok", item=name), kb=rk)

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
          cost=new_cost, bonus=TOSS_LEVEL_BONUS),
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
    log.info("Bot starting (aiogram3 + PostgreSQL)…")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message","callback_query","pre_checkout_query"])

if __name__ == "__main__":
    asyncio.run(main())
