import os
import json
import asyncio
import time
import random
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import UserNotParticipant, BadRequest, PeerIdInvalid

# --- CONFIGURATION ---
API_ID = 27684548
API_HASH = "425f7c25587e4e085a700adcae0dd5ac"
BOT_TOKEN = "8593564272:AAE5BluLzHprbNPKpNl6IJDVR5tnJ1-7pyQ"
BOT_USERNAME = "Rewardsdltbot"
FORCE_CHANNEL_USERNAME = "dealsandloottricks"
FORCE_CHANNEL_2_USERNAME = "tradedlt"
LOG_CHANNEL_ID = -1002224010991
ADMIN_USERNAME = "yucant"

# --- CAMPAIGN DEFINITIONS ---
CAMPAIGNS = {
    "sweets": {
        "label": "Sweets",
        "emoji": "🍬",
        "file": "sweets_codes.txt",
        "link": "https://www.bigbasket.com/sh/f9c23/"
    },
    "choco": {
        "label": "Chocolate",
        "emoji": "🍫",
        "file": "choco_codes.txt",
        "link": "https://www.bigbasket.com/sh/f9c23/"
    },
    "sugar": {
        "label": "Sugar",
        "emoji": "🧂",
        "file": "sugar_codes.txt",
        "link": "https://www.bigbasket.com/sh/eb24b"
    }
}

# --- DATABASE FILES ---
USERS_FILE = "users.json"
REF_FILE = "referrals.json"
BANNED_FILE = "banned.json"
CAPTCHA_FILE = "captchas.json"
COOLDOWN_FILE = "cooldowns.json"
PRICES_FILE = "prices.json"

BANNER_IMG = "https://cdn.grofers.com/cdn-cgi/image/f=auto,fit=scale-down,q=70,metadata=none,w=900/layout-engine/2022-05/Group-33704.jpg"
COOLDOWN_TIME = 600  # 10 minutes

app = Client("bb_giveaway_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- LOCKS ---
users_lock = asyncio.Lock()
ref_lock = asyncio.Lock()
code_lock = asyncio.Lock()
ban_lock = asyncio.Lock()
captcha_lock = asyncio.Lock()
cooldown_lock = asyncio.Lock()
prices_lock = asyncio.Lock()
claim_transaction_lock = asyncio.Lock() # Prevents double-spend race conditions

# --- DATABASE UTILS ---
async def load_prices():
    async with prices_lock:
        if not os.path.exists(PRICES_FILE):
            default_prices = {k: 2 for k in CAMPAIGNS.keys()}
            default_prices["sugar"] = 1 # Special default for sugar
            with open(PRICES_FILE, "w") as f: json.dump(default_prices, f)
            return default_prices
        with open(PRICES_FILE, "r") as f:
            return json.load(f)

async def set_price(item, price):
    async with prices_lock:
        prices = {}
        if os.path.exists(PRICES_FILE):
            with open(PRICES_FILE, "r") as f: prices = json.load(f)
        prices[item.lower()] = price
        with open(PRICES_FILE, "w") as f: json.dump(prices, f, indent=4)

async def is_banned(user_id):
    if not os.path.exists(BANNED_FILE): return False
    async with ban_lock:
        with open(BANNED_FILE, "r") as f:
            try: return str(user_id) in json.load(f)
            except: return False

async def ban_user(user_id):
    async with ban_lock:
        data = []
        if os.path.exists(BANNED_FILE):
            try:
                with open(BANNED_FILE, "r") as f: data = json.load(f)
            except: pass
        if str(user_id) not in data: data.append(str(user_id))
        with open(BANNED_FILE, "w") as f: json.dump(data, f, indent=4)

async def unban_user(user_id):
    async with ban_lock:
        data = []
        if os.path.exists(BANNED_FILE):
            try:
                with open(BANNED_FILE, "r") as f: data = json.load(f)
            except: pass
        if str(user_id) in data: data.remove(str(user_id))
        with open(BANNED_FILE, "w") as f: json.dump(data, f, indent=4)

async def is_captcha_passed(user_id):
    if not os.path.exists(CAPTCHA_FILE): return False
    async with captcha_lock:
        with open(CAPTCHA_FILE, "r") as f:
            try: return str(user_id) in json.load(f)
            except: return False

async def mark_captcha_passed(user_id):
    async with captcha_lock:
        data = []
        if os.path.exists(CAPTCHA_FILE):
            try:
                with open(CAPTCHA_FILE, "r") as f: data = json.load(f)
            except: pass
        if str(user_id) not in data: data.append(str(user_id))
        with open(CAPTCHA_FILE, "w") as f: json.dump(data, f, indent=4)

async def get_cooldown(user_id):
    if not os.path.exists(COOLDOWN_FILE): return 0
    async with cooldown_lock:
        with open(COOLDOWN_FILE, "r") as f:
            try: return json.load(f).get(str(user_id), 0)
            except: return 0

async def set_cooldown(user_id):
    async with cooldown_lock:
        data = {}
        if os.path.exists(COOLDOWN_FILE):
            try:
                with open(COOLDOWN_FILE, "r") as f: data = json.load(f)
            except: pass
        data[str(user_id)] = time.time()
        with open(COOLDOWN_FILE, "w") as f: json.dump(data, f, indent=4)

async def is_user_invalid_candidate(user_id):
    if not os.path.exists(USERS_FILE): return False
    async with users_lock:
        with open(USERS_FILE, "r") as f:
            try: return str(user_id) in json.load(f)
            except: return False

async def mark_user_as_invalid_candidate(user_id):
    async with users_lock:
        data = {}
        if os.path.exists(USERS_FILE):
            try:
                with open(USERS_FILE, "r") as f:
                    data = json.load(f)
                    if isinstance(data, list): data = {str(uid): True for uid in data}
            except: pass
        data[str(user_id)] = True
        with open(USERS_FILE, "w") as f: json.dump(data, f, indent=4)

# --- WALLET & REFERRAL UTILS ---
async def get_ref_data(user_id):
    str_id = str(user_id)
    async with ref_lock:
        data = {}
        if os.path.exists(REF_FILE):
            with open(REF_FILE, "r") as f:
                try: data = json.load(f)
                except: pass
                
        if str_id not in data:
            data[str_id] = {
                "referral_count": 0,
                "balance": 0,
                "pending_referrer": None,
                "history": []
            }
            for key in CAMPAIGNS.keys():
                data[str_id][f"{key}_claimed"] = 0
                
        # Ensure new keys exist for old users
        if "balance" not in data[str_id]: data[str_id]["balance"] = 0 
        if "history" not in data[str_id]: data[str_id]["history"] = []
        for key in CAMPAIGNS.keys():
            if f"{key}_claimed" not in data[str_id]: 
                data[str_id][f"{key}_claimed"] = 0
        
        with open(REF_FILE, "w") as f: json.dump(data, f, indent=4)
        return data[str_id]

async def update_wallet(user_id, amount_change, claim_type=None, cost=0):
    str_id = str(user_id)
    async with ref_lock:
        data = {}
        with open(REF_FILE, "r") as f: data = json.load(f)
        
        # Apply change and enforce zero-floor failsafe
        new_balance = data[str_id]["balance"] + amount_change
        data[str_id]["balance"] = max(0, new_balance) 
        
        if claim_type:
            data[str_id][f"{claim_type}_claimed"] += 1
            data[str_id]["history"].append({
                "campaign": claim_type,
                "cost_paid": cost,
                "date": time.strftime("%Y-%m-%d %H:%M:%S")
            })
            
        with open(REF_FILE, "w") as f: json.dump(data, f, indent=4)

async def increment_referral_count(referrer_id):
    str_id = str(referrer_id)
    async with ref_lock:
        data = {}
        with open(REF_FILE, "r") as f: data = json.load(f)
        
        data[str_id]["referral_count"] += 1
        data[str_id]["balance"] += 1 
        new_count = data[str_id]["referral_count"]
        
        with open(REF_FILE, "w") as f: json.dump(data, f, indent=4)
        return new_count

async def update_ref_data_simple(user_id, key, value):
    str_id = str(user_id)
    async with ref_lock:
        data = {}
        with open(REF_FILE, "r") as f: data = json.load(f)
        data[str_id][key] = value
        with open(REF_FILE, "w") as f: json.dump(data, f, indent=4)

async def get_and_remove_code(filename):
    async with code_lock:
        if not os.path.exists(filename): return None
        with open(filename, "r") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if not lines: return None
        code = lines[0]
        with open(filename, "w") as f:
            f.write("\n".join(lines[1:]))
        return code

# --- ADMIN HANDLERS ---
@app.on_message(filters.command(["ban", "unban"]) & filters.private)
async def ban_management(client, message: Message):
    if not message.from_user.username or message.from_user.username.lower() != ADMIN_USERNAME: return
    if len(message.command) < 2: return await message.reply_text("⚠️ Usage: `/ban <id>` or `/unban <id>`")
    
    target_id = message.command[1]
    if message.command[0] == "ban":
        await ban_user(target_id)
        await message.reply_text(f"✅  User `{target_id}` has been banned.")
    else:
        await unban_user(target_id)
        await message.reply_text(f"✅  User `{target_id}` has been unbanned.")

@app.on_message(filters.command(["refer", "updaterefer", "setprice"]) & filters.private)
async def set_campaign_price(client, message: Message):
    if not message.from_user.username or message.from_user.username.lower() != ADMIN_USERNAME: return
    valid_items = list(CAMPAIGNS.keys())
    
    if len(message.command) < 3: 
        return await message.reply_text(f"⚠️ Usage: `/refer <{'|'.join(valid_items)}> <amount>`\nExample: `/refer sugar 1`")
    
    item = message.command[1].lower()
    if item not in valid_items:
        return await message.reply_text(f"❌ Invalid category. Choose: {', '.join(valid_items)}")
        
    try:
        price = int(message.command[2])
        await set_price(item, price)
        await message.reply_text(f"✅ Setup updated: **{CAMPAIGNS[item]['label']}** now costs **{price} referrals**.")
    except ValueError:
        await message.reply_text("❌ Amount must be a number.")

@app.on_message(filters.command("donate") & filters.private)
async def donate_balance(client, message: Message):
    if not message.from_user.username or message.from_user.username.lower() != ADMIN_USERNAME: return
    if len(message.command) < 3: 
        return await message.reply_text("⚠️ Usage: `/donate <userid> <amount>`")
        
    target_id = message.command[1]
    try:
        amount = int(message.command[2])
        await get_ref_data(target_id) 
        await update_wallet(target_id, amount)
        await message.reply_text(f"✅ Successfully added **{amount}** referrals to user `{target_id}`'s balance.")
        try:
            await client.send_message(int(target_id), f"🎁 **Admin Bonus!**\nYou have received **{amount}** referrals added to your balance.")
        except: pass
    except ValueError:
        await message.reply_text("❌ Amount must be a number.")

# --- LEADERBOARD ---
@app.on_message(filters.command(["top", "leaderboard"]) & filters.private)
async def show_leaderboard(client, message: Message):
    async with ref_lock:
        if not os.path.exists(REF_FILE):
            return await message.reply_text("Leaderboard is currently empty.")
        with open(REF_FILE, "r") as f:
            try: data = json.load(f)
            except: data = {}
            
    # Sort users by total lifetime referrals descending
    sorted_users = sorted(data.items(), key=lambda x: x[1].get("referral_count", 0), reverse=True)
    top_10 = sorted_users[:10]
    
    if not top_10:
        return await message.reply_text("Leaderboard is currently empty.")
        
    text = "🏆 **Top 10 Referrers** 🏆\n━━━━━━━━━━━━━━━━━━━━\n"
    for i, (uid, info) in enumerate(top_10, 1):
        refs = info.get("referral_count", 0)
        text += f"**{i}.** `{uid}` - {refs} Referrals\n"
        
    await message.reply_text(text)

# --- CAPTCHA HANDLER ---
@app.on_callback_query(filters.regex(r"^cap_(\d+)_(\d+)_(.*)$"))
async def verify_captcha(client, callback: CallbackQuery):
    user_id = callback.from_user.id
    _, selected, correct, ref_id = callback.data.split("_")
    if selected != correct:
        await callback.answer("❌  Incorrect! Please send /start to try again.", show_alert=True)
        await callback.message.delete()
        return
        
    await callback.answer("✅  Verification Passed!", show_alert=True)
    await mark_captcha_passed(user_id)
    await callback.message.delete()
    
    first_name = callback.from_user.first_name
    is_invalid = await is_user_invalid_candidate(user_id)
    
    if ref_id != "none":
        if is_invalid:
            await client.send_message(user_id, "❌  <b>Alert:</b> You are already\na registered user.\nYou cannot be referred by someone else.", parse_mode=enums.ParseMode.HTML)
        elif str(ref_id) != str(user_id):
            await update_ref_data_simple(user_id, "pending_referrer", str(ref_id))
            
    await send_join_prompt(client, user_id, first_name)

# --- START AND VERIFICATION FLOW ---
@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message: Message):
    user_id = message.from_user.id
    if await is_banned(user_id):
        return await message.reply_text("You been banned due to fake refers please contact @maxxify to resolve")
        
    first_name = message.from_user.first_name
    args = message.command
    
    if not await is_captcha_passed(user_id):
        ref_id = args[1] if len(args) > 1 else "none"
        a, b = random.randint(1, 10), random.randint(1, 10)
        correct_ans = a + b
        
        options = list(set([opt for opt in [correct_ans, correct_ans + random.randint(1, 4), correct_ans - random.randint(1, 4)] if opt > 0]))
        while len(options) < 3:
            options.append(random.randint(2, 20))
            options = list(set(options))
            
        random.shuffle(options)
        
        # FIXED: Generates a proper 1D list of buttons, wrapped in [] for the row
        btns = [InlineKeyboardButton(str(opt), callback_data=f"cap_{opt}_{correct_ans}_{ref_id}") for opt in options]
        
        await message.reply_text(
            f"🤖 <b>Human Verification Required</b>\n\nWhat is {a} + {b}?",
            reply_markup=InlineKeyboardMarkup([btns]), parse_mode=enums.ParseMode.HTML
        )
        return
        
    try:
        await app.get_chat_member(FORCE_CHANNEL_USERNAME, user_id)
        await app.get_chat_member(FORCE_CHANNEL_2_USERNAME, user_id)
        if not await is_user_invalid_candidate(user_id):
            await mark_user_as_invalid_candidate(user_id)
        return await send_dashboard(message)
    except UserNotParticipant: pass
    except Exception: pass
        
    is_invalid = await is_user_invalid_candidate(user_id)
    if len(args) > 1:
        referrer_id = args[1]
        if is_invalid:
            await message.reply_text("❌ <b>Alert:</b> You are already a registered user.", parse_mode=enums.ParseMode.HTML)
        elif str(referrer_id) != str(user_id):
            await update_ref_data_simple(user_id, "pending_referrer", str(referrer_id))
            
    await send_join_prompt(client, user_id, first_name)

async def send_join_prompt(client, chat_id, name):
    text = f"👋 <b>Welcome {name}!</b>\n\nTo win <b>Bigbasket Coupons</b>, you must join our channels first."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel 1", url=f"https://t.me/{FORCE_CHANNEL_USERNAME}")],
        [InlineKeyboardButton("📢 Join Channel 2", url=f"https://t.me/{FORCE_CHANNEL_2_USERNAME}")],
        [InlineKeyboardButton("✅    I Joined Both", callback_data="check_join")]
    ])
    await client.send_photo(chat_id=chat_id, photo=BANNER_IMG, caption=text, reply_markup=kb, parse_mode=enums.ParseMode.HTML)

@app.on_callback_query(filters.regex("check_join"))
async def check_join_callback(client, callback: CallbackQuery):
    user_id = callback.from_user.id
    if await is_banned(user_id): return await callback.answer("Banned.", show_alert=True)
    
    try:
        await app.get_chat_member(FORCE_CHANNEL_USERNAME, user_id)
        await app.get_chat_member(FORCE_CHANNEL_2_USERNAME, user_id)
        
        if not await is_user_invalid_candidate(user_id):
            user_data = await get_ref_data(user_id)
            referrer_id = user_data.get("pending_referrer")
            if referrer_id and str(referrer_id) != str(user_id):
                new_count = await increment_referral_count(referrer_id)
                await update_ref_data_simple(user_id, "pending_referrer", None)
                
                try: await client.send_message(int(referrer_id), f"🎉 <b>New Referral!</b>\n👤 {callback.from_user.first_name} joined.\n📊 Total Referrals: <b>{new_count}</b>")
                except: pass
                
            await mark_user_as_invalid_candidate(user_id)
            
        await send_dashboard(callback.message)
    except UserNotParticipant:
        await callback.answer("❌ You haven't joined both channels yet!", show_alert=True)

# --- DASHBOARD UI ---
async def send_dashboard(message: Message):
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    data = await get_ref_data(user_id)
    prices = await load_prices()
    
    total_refs = data.get("referral_count", 0)
    balance = data.get("balance", 0) 
    ref_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    
    text = (
        "🎁 <b>Rewards Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Total Lifetime Referrals:</b> {total_refs}\n"
        f"🟢 <b>Available Spendable Balance:</b> {balance} Refs\n\n"
    )
    
    # Generate dynamic claims list
    for c_key, c_info in CAMPAIGNS.items():
        claimed = data.get(f"{c_key}_claimed", 0)
        text += f"{c_info['emoji']} <b>{c_info['label']} Claimed:</b> {claimed}\n"
        
    text += "\n🛒 <b>Redeem Store:</b>\n"
    
    # Generate dynamic prices list
    for c_key, c_info in CAMPAIGNS.items():
        text += f"• {c_info['label']} Code: Requires {prices.get(c_key, 2)} Refs\n"
        
    text += f"\n🔗 <b>Your Referral Link:</b>\n<code>{ref_link}</code>\n"
    
    btns = []
    
    # Generate dynamic buttons
    for c_key, c_info in CAMPAIGNS.items():
        cost = prices.get(c_key, 2)
        if balance >= cost:
            btns.append([InlineKeyboardButton(f"{c_info['emoji']} Redeem {c_info['label']} ({cost} Refs)", callback_data=f"claim_{c_key}")])
        else:
            btns.append([InlineKeyboardButton(f"🔒 {c_info['label']} (Need {cost - balance} more refs)", callback_data="locked")])
            
    btns.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])
    kb = InlineKeyboardMarkup(btns)
    
    if isinstance(message, Message):
        if getattr(message, "photo", None): await message.edit_caption(caption=text, reply_markup=kb, parse_mode=enums.ParseMode.HTML)
        else: await message.reply_photo(photo=BANNER_IMG, caption=text, reply_markup=kb, parse_mode=enums.ParseMode.HTML)

@app.on_callback_query(filters.regex("refresh"))
async def refresh_cb(client, callback: CallbackQuery):
    if await is_banned(callback.from_user.id): return
    
    # FIXED: Added try/except to prevent Telegram "MessageNotModified" crash
    try:
        await send_dashboard(callback.message)
        await callback.answer("Refreshed!")
    except Exception:
        await callback.answer("Everything is already up to date!", show_alert=False)

@app.on_callback_query(filters.regex("locked"))
async def locked_cb(client, callback: CallbackQuery):
    await callback.answer("You don't have enough available balance for this item!", show_alert=True)

# --- UNIVERSAL CLAIM FLOW ---
# FIXED: changed .jn to .join
@app.on_callback_query(filters.regex(f"^claim_({'|'.join(CAMPAIGNS.keys())})$"))
async def init_claim(client, callback: CallbackQuery):
    user_id = callback.from_user.id
    if await is_banned(user_id): return await callback.answer("Banned", show_alert=True)
    
    item = callback.data.split("_")[1]
    prices = await load_prices()
    cost = prices.get(item, 2)
    campaign = CAMPAIGNS[item]
    
    data = await get_ref_data(user_id)
    if data["balance"] < cost:
        return await callback.answer(f"❌ Not enough balance. Need {cost}.", show_alert=True)
        
    terms = (
        f"📜 <b>Terms & Conditions ({campaign['label']}):</b>\n\n"
        "1️⃣ New Accounts & New Device Only.\n"
        "2️⃣ use this method @bbgmethod if not works\n\n"
        f"<i>Do you agree to spend {cost} Referrals?</i>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Agree & Get {campaign['label']} Code", callback_data=f"get_{item}")]])
    await callback.message.edit_caption(caption=terms, reply_markup=kb, parse_mode=enums.ParseMode.HTML)

# FIXED: changed .jn to .join
@app.on_callback_query(filters.regex(f"^get_({'|'.join(CAMPAIGNS.keys())})$"))
async def process_claim(client, callback: CallbackQuery):
    user_id = callback.from_user.id
    if await is_banned(user_id): return await callback.answer("Banned", show_alert=True)
    
    item = callback.data.split("_")[1]
    campaign = CAMPAIGNS[item]
    
    # Cooldown Check
    last_claim = await get_cooldown(user_id)
    if time.time() - last_claim < COOLDOWN_TIME:
        mins, secs = divmod(int(COOLDOWN_TIME - (time.time() - last_claim)), 60)
        return await callback.answer(f"⏳ Wait {mins}m {secs}s before claiming again.", show_alert=True)
        
    prices = await load_prices()
    cost = prices.get(item, 2)
    
    # SINGLE TRANSACTION LOCK: Prevents double-spending
    async with claim_transaction_lock:
        
        # 1. Final Balance Check Inside Lock
        data = await get_ref_data(user_id)
        if data["balance"] < cost:
            return await callback.answer("❌ Error: Verification failed. Not enough refs.", show_alert=True)
            
        # 2. Get Code
        code = await get_and_remove_code(campaign["file"])
        if not code: return await callback.answer(f"💔 {campaign['label']} Codes are Out of Stock!", show_alert=True)
        
        # 3. Deduct Balance & Log (Atomic via update_wallet internal lock)
        await update_wallet(user_id, -cost, claim_type=item, cost=cost)
        
    # Apply Cooldown after successful lock exit
    await set_cooldown(user_id)
    
    msg = (
        f"🎉 <b>{campaign['label']} Code Generated Successfully!</b>\n\n"
        f"🎫 Code: <code>{code}</code>\n"
        f"🛍️ <b>Order Here:</b> <a href='{campaign['link']}'>Click to Order</a>\n\n"
        f"⚠️ <i>Copy the code and use it immediately. (-{cost} Refs deducted)</i>"
    )
    await callback.message.edit_caption(caption=msg, parse_mode=enums.ParseMode.HTML)
    
    try: await client.send_message(LOG_CHANNEL_ID, f"🎁 <b>#{campaign['label']}Claimed</b>\n👤 {callback.from_user.mention}\n🎫 `{code}`\n💰 Cost: {cost}")
    except: pass

if __name__ == "__main__":
    for f in [USERS_FILE, REF_FILE, COOLDOWN_FILE]:
        if not os.path.exists(f): 
            with open(f, "w") as file: json.dump({}, file)
            
    for f in [BANNED_FILE, CAPTCHA_FILE]:
        if not os.path.exists(f): 
            with open(f, "w") as file: json.dump([], file)
            
    for c_info in CAMPAIGNS.values():
        if not os.path.exists(c_info["file"]): open(c_info["file"], "w").close()
        
    print("🤖 Enterprise Bot Started with Anti-Race Condition Lock & Dynamic Campaigns...")
    app.run()
