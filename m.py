import asyncio
import logging
import random
import json
import re
import aiohttp
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import BadRequest, MessageNotModified

# --- CONFIGURATION ---
API_ID = 27684548
API_HASH = "425f7c25587e4e085a700adcae0dd5ac"
BOT_TOKEN = "8292076194:AAHhARFwo52s1DQcJZFF9JD9C1TzkcIV17Q"
FORCE_JOIN_CHANNEL = "@dealsandloottricks"
LOG_CHANNEL_ID = -1002224010991

# --- SETUP ---
logging.basicConfig(level=logging.INFO)
app = Client(
    "swiggy_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

# --- FSM IMPLEMENTATION ---
USER_STORAGE = {}

class StatesGroup:
    pass

class State:
    def __init__(self, name):
        self.name = name
    def __str__(self):
        return self.name

class Form(StatesGroup):
    waiting_for_phone = State("Form.waiting_for_phone")
    waiting_for_name = State("Form.waiting_for_name")
    waiting_for_create_otp = State("Form.waiting_for_create_otp")
    waiting_for_delete_otp = State("Form.waiting_for_delete_otp")

class DeleteStates(StatesGroup):
    waiting_for_archive_otp = State("DeleteStates.waiting_for_archive_otp")

class ReferStates(StatesGroup):
    waiting_for_phone = State("ReferStates.waiting_for_phone")
    waiting_for_otp = State("ReferStates.waiting_for_otp")
    waiting_for_referral = State("ReferStates.waiting_for_referral")

class VoteStates(StatesGroup):
    waiting_for_phone = State("VoteStates.waiting_for_phone")
    waiting_for_otp = State("VoteStates.waiting_for_otp")
    waiting_for_retry = State("VoteStates.waiting_for_retry")

class FSMContext:
    def __init__(self, user_id):
        self.user_id = user_id

    async def set_state(self, state):
        if self.user_id not in USER_STORAGE:
            USER_STORAGE[self.user_id] = {"data": {}}
        USER_STORAGE[self.user_id]["state"] = str(state)

    async def get_state(self):
        return USER_STORAGE.get(self.user_id, {}).get("state")

    async def update_data(self, **kwargs):
        if self.user_id not in USER_STORAGE:
            USER_STORAGE[self.user_id] = {"data": {}}
        USER_STORAGE[self.user_id]["data"].update(kwargs)

    async def get_data(self):
        return USER_STORAGE.get(self.user_id, {}).get("data", {})

    async def clear(self):
        if self.user_id in USER_STORAGE:
            del USER_STORAGE[self.user_id]

def state_filter(state_obj):
    async def func(_, __, message):
        user_id = message.from_user.id
        current_state = USER_STORAGE.get(user_id, {}).get("state")
        return current_state == str(state_obj)
    return filters.create(func)

# --- UTILS ---
def generate_device_id():
    return ''.join(random.choices("0123456789abcdef", k=16))

def get_headers(device_id, tid="", sid="", token="", user_id=""):
    headers = {
        "Host": "profile.swiggy.com",
        "pl-version": "119",
        "user-agent": "Swiggy-Android",
        "content-type": "application/json; charset=utf-8",
        "version-code": "1580",
        "app-version": "4.98.0",
        "deviceid": device_id,
        "swuid": device_id,
        "accept": "application/json; charset=utf-8",
        "os-version": "12",
        "manufacturer": "SAMSUNG",
        "model-name": "SM-A315F"
    }
    if tid: headers["tid"] = tid
    if sid: headers["sid"] = sid
    if token: headers["token"] = token
    if user_id: headers["userid"] = user_id
    return headers

def get_disc_headers(device_id, tid, sid, token="", access_token="", referral_code=""):
    headers = {
        "Host": "disc.swiggy.com",
        "pl-version": "119",
        "user-agent": "Swiggy-Android",
        "content-type": "application/json; charset=utf-8",
        "version-code": "1580",
        "app-version": "4.98.0",
        "deviceid": device_id,
        "swuid": device_id,
        "accept": "application/json; charset=utf-8",
        "os-version": "12"
    }
    if tid: headers["tid"] = tid
    if sid: headers["sid"] = sid
    if token: headers["token"] = token
    if access_token: headers["x-oztok"] = access_token
    if referral_code: headers["referralcode"] = referral_code
    return headers

async def send_log(message: str):
    try:
        await app.send_message(LOG_CHANNEL_ID, message)
    except Exception as e:
        logging.error(f"Failed to send log: {e}")

async def check_membership(user_id: int):
    try:
        member = await app.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=user_id)
        return member.status not in [enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED]
    except BadRequest:
        return False
    except Exception:
        return False

# --- HANDLERS ---
@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    welcome_text = (
        "<b>Welcome to the Swiggy Tool Bot!</b> 🤖\n\n"
        "Please select an option below:"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Check Registration", callback_data="mode_check")],
        [InlineKeyboardButton(text="🆕 Create Account", callback_data="mode_create")],
        [InlineKeyboardButton(text="🗑 Delete Account", callback_data="mode_delete")],
        [InlineKeyboardButton(text="💸 Refer and Earn", callback_data="mode_refer")],
        [InlineKeyboardButton(text="🗳 Vote and Earn 100₹", callback_data="mode_vote")]
    ])
    await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)

@app.on_callback_query(filters.regex("^mode_"))
async def process_callback_mode(client: Client, callback: CallbackQuery):
    user_id = callback.from_user.id
    state = FSMContext(user_id)
    
    is_member = await check_membership(user_id)
    if not is_member:
        await callback.message.reply_text(
            f"❌  <b>Access Denied!</b>\n\nPlease join {FORCE_JOIN_CHANNEL} to use this bot.",
            parse_mode=enums.ParseMode.HTML
        )
        return

    mode = callback.data.split("_")[1]
    await state.update_data(mode=mode)
    
    msg_text = "📞 <b>Enter Mobile Number:</b>"
    await callback.message.reply_text(msg_text, parse_mode=enums.ParseMode.HTML)
    
    if mode == "refer":
        await state.set_state(ReferStates.waiting_for_phone)
    elif mode == "vote":
        await state.set_state(VoteStates.waiting_for_phone)
    else:
        await state.set_state(Form.waiting_for_phone)
        
    await callback.answer()

@app.on_message(filters.text & state_filter(Form.waiting_for_phone))
async def process_phone(client: Client, message: Message):
    state = FSMContext(message.from_user.id)
    phone = message.text.strip()
    
    if not phone.isdigit() or len(phone) != 10:
        await message.reply_text("❌  Invalid number. Enter a 10-digit number.")
        return
        
    data = await state.get_data()
    mode = data.get("mode")
    device_id = generate_device_id()
    await state.update_data(phone=phone, device_id=device_id)
    
    if mode == "check":
        await perform_check_registration(message, phone)
        await state.clear()
    elif mode == "create":
        await message.reply_text("👤 <b>Enter Name for Account:</b>", parse_mode=enums.ParseMode.HTML)
        await state.set_state(Form.waiting_for_name)
    elif mode == "delete":
        await send_otp_generic(message, state, phone, device_id, Form.waiting_for_delete_otp)

# --- 1. CHECK REGISTRATION ---
async def perform_check_registration(message, phone):
    url = "https://www.swiggy.com/mapi/auth/signin-check"
    headers = {
        "authority": "www.swiggy.com",
        "origin": "https://www.swiggy.com",
        "referer": "https://www.swiggy.com/auth",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Linux; Android 10) Chrome/137 Mobile",
        "platform": "mweb",
        "user-id": "0"
    }
    payload = {"mobile": phone, "countryCode": "91", "countryKey": "IN"}
    status_msg = await message.reply_text("🔍 Checking...")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json()
                is_registered = data.get("data", {}).get("registered")
                if is_registered is True:
                    text = f"✅  <b>Number:</b> {phone}\n<b>Status:</b> REGISTERED (User Exists)"
                elif is_registered is False:
                    text = f"❌  <b>Number:</b> {phone}\n<b>Status:</b> NOT REGISTERED (Fresh)"
                else:
                    text = f"⚠️ <b>Number:</b> {phone}\n<b>Status:</b> Unknown Response"
                await status_msg.edit_text(text, parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            await status_msg.edit_text(f"❌  Error: {str(e)}")

# --- 2. CREATE ACCOUNT FLOW ---
@app.on_message(filters.text & state_filter(Form.waiting_for_name))
async def process_name_create(client: Client, message: Message):
    state = FSMContext(message.from_user.id)
    name = message.text.strip()
    await state.update_data(name=name)
    data = await state.get_data()
    await send_otp_generic(message, state, data['phone'], data['device_id'], Form.waiting_for_create_otp)

@app.on_message(filters.text & state_filter(Form.waiting_for_create_otp))
async def process_create_otp_verify(client: Client, message: Message):
    state = FSMContext(message.from_user.id)
    otp = message.text.strip()
    data = await state.get_data()
    
    verify_url = "https://profile.swiggy.com/api/v3/app/login/verify"
    headers = get_headers(data['device_id'], tid=data['tid'], sid=data['sid'])
    headers["otp_source"] = "Sms-automatic-reverification"
    payload = {
        "otp": otp,
        "cloningSignalsData": {
            "appFilesDirPathInvalid": 0, "developerModeEnabled": 1,
            "deviceModelVmos": 0, "emulatorStatus": 0,
            "packageName": "in.swiggy.android", "workProfileEnabled": 0
        }
    }
    
    status_msg = await message.reply_text("🔄 Verifying & Creating Account...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(verify_url, headers=headers, params={"otp_source": "Sms-automatic"}, json=payload) as resp:
                verify_res = await resp.json()
                if verify_res.get("statusCode") != 0:
                    await status_msg.edit_text(f"❌  OTP Verification Failed: {verify_res.get('statusMessage')}")
                    await state.clear()
                    return
                
                verified_tid = verify_res.get("tid")
                verified_sid = verify_res.get("sid")
                
                signup_url = "https://profile.swiggy.com/api/v3/app/signup"
                signup_headers = get_headers(data['device_id'], tid=verified_tid, sid=verified_sid)
                signup_payload = {
                    "cloningSignalsData": payload["cloningSignalsData"],
                    "signUp": {
                        "email": "",
                        "mobile": data['phone'],
                        "name": data['name']
                    }
                }
                async with session.post(signup_url, headers=signup_headers, json=signup_payload) as signup_resp:
                    signup_data = await signup_resp.json()
                    if signup_data.get("statusCode") == 0:
                        res_text = (
                            f"✅  <b>Account Created Successfully!</b>\n\n"
                            f"👤 Name: {data['name']}\n"
                            f"📱 Mobile: {data['phone']}\n"
                            f"🆔 Customer ID: {signup_data.get('data', {}).get('customer_id')}"
                        )
                        await status_msg.edit_text(res_text, parse_mode=enums.ParseMode.HTML)
                        await send_log(f"🆕 [CREATE] Success | {data['phone']}")
                    else:
                        await status_msg.edit_text(f"⚠️ Signup Failed: {signup_data.get('statusMessage')}\n(User might already exist)")
        except Exception as e:
            await status_msg.edit_text(f"❌  Error: {str(e)}")
        await state.clear()

# --- 3. DELETE ACCOUNT FLOW (Shared Logic) ---
async def send_otp_generic(message, state, phone, device_id, next_state):
    url = "https://profile.swiggy.com/api/v3/app/sms_otp"
    headers = get_headers(device_id)
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, params={"mobile": phone}) as resp:
                data = await resp.json()
                if data.get("statusCode") == 0:
                    await state.update_data(tid=data.get("tid"), sid=data.get("sid"))
                    await message.reply_text(f"✅  OTP Sent to {phone}.\n\n👇 <b>Enter Login OTP:</b>", parse_mode=enums.ParseMode.HTML)
                    await state.set_state(next_state)
                else:
                    await message.reply_text(f"❌  Error sending OTP: {data.get('statusMessage')}")
                    await state.clear()
        except Exception as e:
            await message.reply_text("❌  Network Error.")
            await state.clear()

@app.on_message(filters.text & state_filter(Form.waiting_for_delete_otp))
async def process_delete_login_verify(client: Client, message: Message):
    state = FSMContext(message.from_user.id)
    otp = message.text.strip()
    user_data = await state.get_data()
    device_id = user_data['device_id']
    verify_url = "https://profile.swiggy.com/api/v3/app/login/verify"
    headers = get_headers(device_id, tid=user_data['tid'], sid=user_data['sid'])
    payload = {
        "cloningSignalsData": {
            "appFilesDirPathInvalid": 0, "developerModeEnabled": 1,
            "deviceModelVmos": 0, "emulatorStatus": 0,
            "packageName": "in.swiggy.android", "workProfileEnabled": 0
        },
        "otp": otp
    }
    await message.reply_text("🔄 Verifying Login...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(verify_url, headers=headers, params={"otp_source": "Sms-automatic"}, json=payload) as resp:
                login_res = await resp.json()
                if login_res.get("statusCode") != 0:
                    await message.reply_text(f"❌  Login Failed: {login_res.get('statusMessage')}")
                    await state.clear()
                    return
                
                swiggy_user = login_res.get("data", {})
                token = swiggy_user.get("token")
                tid = login_res.get("tid")
                sid = login_res.get("sid")
                
                await request_delete_archive_otp(message, state, session, device_id, tid, sid, token)
        except Exception as e:
            await message.reply_text(f"❌  Error: {str(e)}")
            await state.clear()

async def request_delete_archive_otp(message_obj, state, session, device_id, tid, sid, token):
    check_url = "https://profile.swiggy.com/api/v1/delete/user_check"
    del_headers = get_headers(device_id, tid=tid, sid=sid, token=token)
    
    async with session.get(check_url, headers=del_headers) as del_resp:
        del_data = await del_resp.json()
        if del_data.get("statusCode") == 28:
            await state.update_data(
                token=token,
                tid=del_data.get("tid", tid),
                sid=del_data.get("sid", sid)
            )
            await message_obj.reply_text(
                "⚠️ <b>Archive OTP Sent!</b>\n"
                "This is the final step to delete.\n"
                "👇 <b>Enter the Archive OTP below:</b>",
                parse_mode=enums.ParseMode.HTML
            )
            await state.set_state(DeleteStates.waiting_for_archive_otp)
        else:
            await message_obj.reply_text(f"❌  Delete Init Failed: {del_data.get('statusMessage')}")
            await state.clear()

@app.on_message(filters.text & state_filter(DeleteStates.waiting_for_archive_otp))
async def final_delete_confirm(client: Client, message: Message):
    state = FSMContext(message.from_user.id)
    otp = message.text.strip()
    data = await state.get_data()
    delete_url = "https://profile.swiggy.com/api/v1/delete/user"
    headers = get_headers(data['device_id'], tid=data['tid'], sid=data['sid'], token=data['token'])
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(delete_url, headers=headers, json={"otp": otp}) as resp:
                res = await resp.json()
                if res.get("statusCode") == 33:
                    await message.reply_text(f"✅  <b>Account Deleted Successfully!</b>\nPhone: {data.get('phone', 'Unknown')}", parse_mode=enums.ParseMode.HTML)
                    await send_log(f"🗑 [DELETE] Success | {data.get('phone', 'Unknown')}")
                    
                    # Logic for "Delete and Refer Again" intent
                    if data.get("delete_intent") == "refer_again":
                        await message.reply_text("⏳ Waiting 10 seconds before sending OTP for referral again...")
                        await asyncio.sleep(10)
                        
                        # Generate a fresh device ID for the next refer loop
                        new_device_id = generate_device_id()
                        await state.update_data(device_id=new_device_id, delete_intent=None)
                        
                        # Start refer flow automatically with the same number
                        await send_otp_generic(message, state, data['phone'], new_device_id, ReferStates.waiting_for_otp)
                    else:
                        await state.clear()
                else:
                    await message.reply_text(f"❌  Delete Failed: {res.get('statusMessage')}")
                    await state.clear()
        except Exception as e:
            await message.reply_text(f"❌  Error: {str(e)}")
            await state.clear()

# --- 4. REFER AND EARN FLOW ---
@app.on_message(filters.text & state_filter(ReferStates.waiting_for_phone))
async def process_refer_phone(client: Client, message: Message):
    state = FSMContext(message.from_user.id)
    phone = message.text.strip()
    
    if not phone.isdigit() or len(phone) != 10:
        await message.reply_text("❌  Invalid number. Enter a 10-digit number.")
        return
        
    device_id = generate_device_id()
    await state.update_data(phone=phone, device_id=device_id)
    await send_otp_generic(message, state, phone, device_id, ReferStates.waiting_for_otp)

@app.on_message(filters.text & state_filter(ReferStates.waiting_for_otp))
async def process_refer_otp(client: Client, message: Message):
    state = FSMContext(message.from_user.id)
    otp = message.text.strip()
    data = await state.get_data()
    device_id = data['device_id']
    phone = data['phone']
    
    verify_url = "https://profile.swiggy.com/api/v3/app/login/verify"
    headers = get_headers(device_id, tid=data['tid'], sid=data['sid'])
    payload = {
        "cloningSignalsData": {
            "appFilesDirPathInvalid": 0, "developerModeEnabled": 1,
            "deviceModelVmos": 0, "emulatorStatus": 0,
            "packageName": "in.swiggy.android", "workProfileEnabled": 0
        },
        "otp": otp
    }
    
    status_msg = await message.reply_text("🔄 Verifying Login...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(verify_url, headers=headers, params={"otp_source": "Sms-consent"}, json=payload) as resp:
                login_res = await resp.json()
                if login_res.get("statusCode") != 0:
                    await status_msg.edit_text(f"❌  Login Failed: {login_res.get('statusMessage')}")
                    await state.clear()
                    return
                
                swiggy_user = login_res.get("data", {})
                is_registered = swiggy_user.get("registered", True)
                
                tid = login_res.get("tid")
                sid = login_res.get("sid")
                token = swiggy_user.get("token", "")
                user_id = swiggy_user.get("customer_id", "")
                
                if is_registered:
                    await status_msg.edit_text("🔄 Account is registered, logging in...")
                else:
                    await status_msg.edit_text("🔄 Account not registered. Creating new account...")
                    signup_url = "https://profile.swiggy.com/api/v3/app/signup"
                    signup_headers = get_headers(device_id, tid=tid, sid=sid)
                    signup_payload = {
                        "cloningSignalsData": payload["cloningSignalsData"],
                        "signUp": {"mobile": phone, "name": "SwiggyUser"}
                    }
                    async with session.post(signup_url, headers=signup_headers, json=signup_payload) as signup_resp:
                        signup_res = await signup_resp.json()
                        if signup_res.get("statusCode") != 0:
                            await status_msg.edit_text(f"❌  Signup Failed: {signup_res.get('statusMessage')}")
                            await state.clear()
                            return
                        signup_data = signup_res.get("data", {})
                        token = signup_data.get("token")
                        user_id = signup_data.get("customer_id")
                        tid = signup_res.get("tid", tid)
                        sid = signup_res.get("sid", sid)

                await state.update_data(tid=tid, sid=sid, token=token, user_id=user_id)
                await status_msg.edit_text(
                    "✅  <b>Logged In Successfully!</b>\n\n👇 <b>Paste your Referral URL or Code:</b>\n"
                    "(e.g., https://r.swiggy.com/hbl/holi2026-1dDYh2bSPb)", 
                    parse_mode=enums.ParseMode.HTML
                )
                await state.set_state(ReferStates.waiting_for_referral)
                
        except Exception as e:
            await status_msg.edit_text(f"❌  Error: {str(e)}")
            await state.clear()

@app.on_message(filters.text & state_filter(ReferStates.waiting_for_referral))
async def process_referral_code(client: Client, message: Message):
    state = FSMContext(message.from_user.id)
    ref_input = message.text.strip()
    data = await state.get_data()
    device_id = data['device_id']
    
    # Extract just the code if the user pastes a full URL
    match = re.search(r'holi2026-([A-Za-z0-9]+)', ref_input)
    if match:
        ref_code = match.group(1)
    else:
        # Fallback if they just pasted the raw code or another format
        ref_code = ref_input.split("/")[-1].split("?")[0].strip()

    status_msg = await message.reply_text(f"🔄 Processing your referral code: <b>{ref_code}</b>...", parse_mode=enums.ParseMode.HTML)
    
    async with aiohttp.ClientSession() as session:
        try:
            # 1. Get Access Token
            signin_url = "https://disc.swiggy.com/v1/accounts/signInWithTID"
            auth_headers = get_disc_headers(device_id, tid=data['tid'], sid=data['sid'], token=data['token'])
            auth_payload = {
                "tid": data['tid'],
                "token": data['token'],
                "user_id": str(data['user_id'])
            }
            async with session.post(signin_url, headers=auth_headers, json=auth_payload) as auth_resp:
                auth_data = await auth_resp.json()
                access_token = auth_data.get("access_token")
                
                if not access_token:
                    await status_msg.edit_text("❌ Failed to fetch access token.")
                    await state.clear()
                    return

            # 2. Call Campaign Details
            camp_details_url = "https://disc.swiggy.com/api/v1/campaign/details"
            camp_headers = get_disc_headers(
                device_id, tid=data['tid'], sid=data['sid'], token=data['token'],
                access_token=access_token, referral_code=ref_code
            )
            camp_headers["campaignid"] = "holi2026"
            camp_headers["platform"] = "Swiggy-Android"
            
            async with session.get(camp_details_url, headers=camp_headers) as _:
                pass 

            # 3. Submit Campaign
            submit_url = "https://disc.swiggy.com/api/v1/campaign/submit"
            submit_payload = {"statusNumber": 1}
            async with session.post(submit_url, headers=camp_headers, json=submit_payload) as submit_resp:
                submit_res = await submit_resp.json()
                
                log_text = (
                    f"💸 <b>[REFERRAL] Completed</b>\n"
                    f"📱 Mobile: <code>{data.get('phone')}</code>\n"
                    f"🎟 Code Used: {ref_code}\n"
                    f"📝 Response: {json.dumps(submit_res)}"
                )
                await send_log(log_text)
                
                res_msg = (
                    f"✅  <b>Referral Execution Complete!</b>\n\n"
                    f"<b>Response:</b>\n<code>{json.dumps(submit_res, indent=2)}</code>\n\n"
                    f"⚠️ <i>Disclaimer: Continuous deletion could lead to account ban.</i>"
                )
                
                # Interactive options to Delete & Refer Again or Refer New
                post_action_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🗑 Delete and refer again", callback_data="delete_and_refer")],
                    [InlineKeyboardButton(text="💸 Refer Again (New Number)", callback_data="mode_refer")]
                ])
                
                await status_msg.edit_text(res_msg, parse_mode=enums.ParseMode.HTML, reply_markup=post_action_kb)
                
        except Exception as e:
            await status_msg.edit_text(f"❌  Error during execution: {str(e)}")
            await state.clear()

@app.on_callback_query(filters.regex("^delete_and_refer"))
async def process_delete_and_refer(client: Client, callback: CallbackQuery):
    state = FSMContext(callback.from_user.id)
    data = await state.get_data()
    
    if not data or 'token' not in data:
        await callback.answer("Session expired. Please start over.", show_alert=True)
        return
        
    # Set intent so final_delete_confirm knows to loop back to refer
    await state.update_data(delete_intent="refer_again")
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Initiating delete...")
    
    async with aiohttp.ClientSession() as session:
        await request_delete_archive_otp(
            callback.message, state, session, 
            data['device_id'], data['tid'], data['sid'], data['token']
        )

# --- 5. VOTE AND EARN FLOW ---
@app.on_message(filters.text & state_filter(VoteStates.waiting_for_phone))
async def process_vote_phone(client: Client, message: Message):
    state = FSMContext(message.from_user.id)
    phone = message.text.strip()
    
    if not phone.isdigit() or len(phone) != 10:
        await message.reply_text("❌  Invalid number. Enter a 10-digit number.")
        return
        
    device_id = generate_device_id()
    await state.update_data(phone=phone, device_id=device_id)
    await send_otp_generic(message, state, phone, device_id, VoteStates.waiting_for_otp)

@app.on_message(filters.text & state_filter(VoteStates.waiting_for_otp))
async def process_vote_otp(client: Client, message: Message):
    state = FSMContext(message.from_user.id)
    otp = message.text.strip()
    data = await state.get_data()
    device_id = data['device_id']
    phone = data['phone']
    
    verify_url = "https://profile.swiggy.com/api/v3/app/login/verify"
    headers = get_headers(device_id, tid=data['tid'], sid=data['sid'])
    payload = {
        "cloningSignalsData": {
            "appFilesDirPathInvalid": 0, "developerModeEnabled": 1,
            "deviceModelVmos": 0, "emulatorStatus": 0,
            "packageName": "in.swiggy.android", "workProfileEnabled": 0
        },
        "otp": otp
    }
    
    status_msg = await message.reply_text("🔄 Verifying Login...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(verify_url, headers=headers, params={"otp_source": "Sms-automatic"}, json=payload) as resp:
                login_res = await resp.json()
                if login_res.get("statusCode") != 0:
                    await status_msg.edit_text(f"❌  Login Failed: {login_res.get('statusMessage')}")
                    await state.clear()
                    return
                
                swiggy_user = login_res.get("data", {})
                is_registered = swiggy_user.get("registered", True)
                
                if not is_registered:
                    await status_msg.edit_text("❌ This number is not registered on Swiggy. Please use a registered number for Vote and Earn.")
                    await state.clear()
                    return

                tid = login_res.get("tid")
                sid = login_res.get("sid")
                token = swiggy_user.get("token", "")
                
                await state.update_data(tid=tid, sid=sid, token=token)
                await status_msg.edit_text("✅  Logged In Successfully!\nStarting Vote and Earn process...")
                
                await run_voting_process(status_msg, state, await state.get_data())
                
        except Exception as e:
            await status_msg.edit_text(f"❌  Error: {str(e)}")
            await state.clear()

async def run_voting_process(message_obj: Message, state: FSMContext, data: dict):
    device_id = data.get('device_id')
    tid = data.get('tid')
    sid = data.get('sid')
    token = data.get('token')

    url = "https://profile.swiggy.com/api/rx-awards/rx"
    params = {
        "category": "Andhra Food",
        "bu": "Food_Delivery",
        "cityId": "1",
        "getRewards": "true",
        "campaignId": "RXAWARD2026"
    }
    
    headers = {
        "Host": "profile.swiggy.com",
        "apiversion": "v2",
        "longitude": "84.8608652",
        "latitude": "25.4818788",
        "version-code": "1580",
        "deviceid": device_id,
        "business-line": "FOOD",
        "tid": tid,
        "user-agent": "Mozilla/5.0 (Linux; Android 12; SM-A315F Build/SP1A.210812.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/145.0.7632.79 Mobile Safari/537.36",
        "sid": sid,
        "token": token,
        "content-type": "application/json",
        "accept": "*/*",
        "origin": "https://webviews.swiggy.com"
    }

    def find_food_earned(d):
        if isinstance(d, dict):
            if 'totalEarnedByBL' in d and 'FOOD' in d['totalEarnedByBL']:
                return d['totalEarnedByBL']['FOOD']
            for k, v in d.items():
                res = find_food_earned(v)
                if res is not None:
                    return res
        elif isinstance(d, list):
            for item in d:
                res = find_food_earned(item)
                if res is not None:
                    return res
        return None

    last_earned = None
    unchanged_count = 0
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 200:
                        try:
                            # Bypass strict mime-type check by forcing content_type=None
                            json_data = await resp.json(content_type=None)
                        except Exception as parse_e:
                            raw_text = await resp.text()
                            logging.error(f"Failed to parse JSON: {parse_e}\nResponse: {raw_text}")
                            try:
                                await message_obj.edit_text("⚠️ API returned invalid JSON format. Retrying...")
                            except MessageNotModified:
                                pass
                            await asyncio.sleep(2)
                            continue
                        
                        food_rupees = find_food_earned(json_data)
                        
                        if food_rupees is not None:
                            try:
                                await message_obj.edit_text(f"✅ Current Food Rupees Earned: **{food_rupees}**")
                            except MessageNotModified:
                                pass  # Prevent telegram flood warnings
                            
                            if food_rupees >= 100:
                                await message_obj.reply_text("🎉 **You earned 100 rupees!** Process stopped.")
                                await state.clear()
                                break
                            
                            if food_rupees == last_earned:
                                unchanged_count += 1
                            else:
                                last_earned = food_rupees
                                unchanged_count = 0
                            
                            if unchanged_count >= 7:
                                kb = InlineKeyboardMarkup([
                                    [InlineKeyboardButton("🔄 Try Again", callback_data="vote_retry")],
                                    [InlineKeyboardButton("❌ Abort", callback_data="vote_abort")]
                                ])
                                await message_obj.reply_text(
                                    "⚠️ Offer is currently having some problem (balance not updating).\nDo you want to try again or abort?", 
                                    reply_markup=kb
                                )
                                await state.set_state(VoteStates.waiting_for_retry)
                                break
                        else:
                            try:
                                await message_obj.edit_text("❌ Could not find 'totalEarnedByBL -> FOOD' in the response. Retrying...")
                            except MessageNotModified:
                                pass
                    else:
                        try:
                            await message_obj.edit_text(f"⚠️ Request failed! Status code: {resp.status}")
                        except MessageNotModified:
                            pass
            except Exception as e:
                try:
                    await message_obj.edit_text(f"❌ Error during request: {str(e)}")
                except MessageNotModified:
                    pass
                
            # Keep 2 sec wait between requests
            await asyncio.sleep(2)

@app.on_callback_query(filters.regex("^vote_retry$"))
async def process_vote_retry(client: Client, callback: CallbackQuery):
    state = FSMContext(callback.from_user.id)
    data = await state.get_data()
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Retrying Vote and Earn...")
    
    # Send a new status message and resume loop
    status_msg = await callback.message.reply_text("⏳ Resuming Vote and Earn requests...")
    await run_voting_process(status_msg, state, data)

@app.on_callback_query(filters.regex("^vote_abort$"))
async def process_vote_abort(client: Client, callback: CallbackQuery):
    state = FSMContext(callback.from_user.id)
    
    await callback.message.edit_text("🛑 Vote and Earn aborted by user.")
    await state.clear()
    await callback.answer("Process aborted")

# --- MAIN ---
if __name__ == "__main__":
    print("🤖 Swiggy Bot Started...")
    app.run()
