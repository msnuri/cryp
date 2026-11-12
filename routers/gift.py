"""
Telegram Gift API + Bot handlers
Endpoints under /gift
Bot runs via lifespan in main.py

Fully optional: if API_ID / API_HASH / BOT_TOKEN are missing,
the HTTP endpoints still load and the process never crashes.
"""
import os
import secrets
import json
import traceback
import asyncio

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/gift", tags=["Telegram Gift"])

API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "") or ""
BOT_TOKEN = os.getenv("BOT_TOKEN", "") or ""
SESSION_DIR = os.getenv("SESSION_DIR", "/data/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)
MAPPING_FILE = os.path.join(SESSION_DIR, "user_mapping.json")


def load_mapping():
    if os.path.exists(MAPPING_FILE):
        try:
            with open(MAPPING_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_mapping(mapping):
    with open(MAPPING_FILE, "w") as f:
        json.dump(mapping, f)


def make_session_path(name: str) -> str:
    return os.path.join(SESSION_DIR, name)


user_states = {}

# Bot client — only create when credentials exist
app_bot = None
Client = None
filters = None
errors = None
raw = None
Message = None
ReplyKeyboardMarkup = None
InlineKeyboardMarkup = None
InlineKeyboardButton = None
CallbackQuery = None

try:
    from pyrogram import Client as _Client, filters as _filters, errors as _errors, raw as _raw
    from pyrogram.types import (
        Message as _Message,
        ReplyKeyboardMarkup as _ReplyKeyboardMarkup,
        InlineKeyboardMarkup as _InlineKeyboardMarkup,
        InlineKeyboardButton as _InlineKeyboardButton,
        CallbackQuery as _CallbackQuery,
    )
    Client = _Client
    filters = _filters
    errors = _errors
    raw = _raw
    Message = _Message
    ReplyKeyboardMarkup = _ReplyKeyboardMarkup
    InlineKeyboardMarkup = _InlineKeyboardMarkup
    InlineKeyboardButton = _InlineKeyboardButton
    CallbackQuery = _CallbackQuery
except Exception as e:
    print(f"WARNING: pyrogram not available or import failed: {e}")

if API_ID and API_HASH and BOT_TOKEN and Client is not None:
    try:
        app_bot = Client(
            make_session_path("manager_bot"),
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
        )
    except Exception as e:
        print(f"WARNING: Failed to create Gift bot client: {e}")
        app_bot = None
else:
    print("WARNING: Gift bot disabled — set API_ID, API_HASH, BOT_TOKEN")


async def get_stars_balance(client):
    try:
        res = await client.invoke(raw.functions.payments.GetStarsStatus(peer=await client.resolve_peer("me")))
        if hasattr(res, "balance"):
            b = res.balance
            return getattr(b, "amount", b) if not isinstance(b, int) else b
        return 0
    except Exception:
        return 0


async def pick_gift_id(app, requested: int | None) -> int:
    gifts_obj = await app.invoke(raw.functions.payments.GetStarGifts(hash=0))
    gift_list = getattr(gifts_obj, "gifts", [])
    if not gift_list:
        raise RuntimeError("No gifts available in Telegram catalog.")
    if requested:
        for g in gift_list:
            if getattr(g, "id", None) == requested:
                return requested
    return gift_list[0].id


def get_gift_name(gift) -> str:
    name = getattr(gift, "title", None) or getattr(gift, "name", None)
    if name:
        return name
    sticker = getattr(gift, "sticker", None)
    if sticker:
        for attr in getattr(sticker, "attributes", []):
            if isinstance(attr, raw.types.DocumentAttributeSticker):
                emoji = getattr(attr, "alt", None)
                if emoji:
                    return emoji
    return "Gift"


def get_gift_emoji(gift) -> str:
    sticker = getattr(gift, "sticker", None)
    if sticker:
        for attr in getattr(sticker, "attributes", []):
            if isinstance(attr, raw.types.DocumentAttributeSticker):
                emoji = getattr(attr, "alt", None)
                if emoji:
                    return emoji
    return "🎁"


@router.api_route("/send-gift", methods=["GET", "POST"])
async def send_gift_api(
    target: str = Query(...),
    session: str = Query(...),
    message: str = Query("Enjoy!"),
    gift_id: str | None = Query(None),
    hide_name: bool = Query(False),
    include_upgrade: bool = Query(False),
):
    if not API_ID or not API_HASH or Client is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "message": "Gift service unavailable: API_ID / API_HASH not configured"
            }
        )

    clean_target = target.replace("@", "").strip()
    session_path = make_session_path(session)

    if not os.path.exists(session_path + ".session"):
        return JSONResponse(
            status_code=404,
            content={"status": "error", "message": f"API Key '{session}' not found!"}
        )

    client = Client(session_path, api_id=API_ID, api_hash=API_HASH)

    try:
        await client.start()
        await asyncio.sleep(1.5)

        try:
            peer = await client.resolve_peer(clean_target)
            has_history = False
            async for _ in client.get_chat_history(clean_target, limit=1):
                has_history = True
                break
            if not has_history:
                return JSONResponse(
                    status_code=403,
                    content={
                        "status": "error",
                        "message": "Security: Target user must DM this account first."
                    }
                )
        except Exception:
            return JSONResponse(
                status_code=403,
                content={"status": "error", "message": "User not found or no DM history."}
            )

        req_id = int(gift_id) if gift_id and gift_id.isnumeric() else None
        valid_gift_id = await pick_gift_id(client, req_id)

        invoice = raw.types.InputInvoiceStarGift(
            peer=peer,
            gift_id=valid_gift_id,
            message=raw.types.TextWithEntities(text=message, entities=[]),
            hide_name=True if hide_name else None,
            include_upgrade=True if include_upgrade else None
        )

        form = await client.invoke(raw.functions.payments.GetPaymentForm(invoice=invoice))
        form_id = getattr(form, "form_id", None) or getattr(form, "id", None)

        await client.invoke(raw.functions.payments.SendStarsForm(form_id=form_id, invoice=invoice))

        return JSONResponse(status_code=200, content={
            "status": "success",
            "message": "Gift sent successfully!",
            "data": {
                "target": clean_target,
                "key": session,
                "gift_id": str(valid_gift_id),
                "hidden": hide_name
            }
        })

    except Exception as e:
        err_name = type(e).__name__
        if errors and isinstance(e, errors.BalanceTooLow):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Balance too low! This account needs more Stars."}
            )
        if errors and isinstance(e, errors.FloodWait):
            return JSONResponse(
                status_code=429,
                content={"status": "error", "message": f"Rate limit: Please wait {getattr(e, 'value', '?')} seconds."}
            )
        if errors and isinstance(e, errors.UserPrivacyRestricted):
            return JSONResponse(
                status_code=403,
                content={"status": "error", "message": "User privacy settings prevent receiving this gift."}
            )
        if errors and isinstance(e, errors.AuthKeyUnregistered):
            return JSONResponse(
                status_code=401,
                content={"status": "error", "message": "Session expired! Please delete and recreate this API Key."}
            )
        if errors and isinstance(e, errors.RPCError):
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "error_code": getattr(e, "ID", None),
                    "message": f"Telegram Error: {getattr(e, 'MESSAGE', str(e))}"
                }
            )
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Internal Error: {str(e)}"}
        )
    finally:
        try:
            if client.is_connected:
                await client.stop()
        except Exception:
            pass


@router.get("/")
async def gift_health():
    return {
        "status": "ok",
        "service": "telegram-gift",
        "bot_enabled": app_bot is not None,
        "credentials_configured": bool(API_ID and API_HASH and BOT_TOKEN)
    }


# -------------------------
# BOT HANDLERS — only register when bot client exists
# -------------------------
if app_bot is not None and filters is not None:

    @app_bot.on_message(filters.command("start") & filters.private)
    async def start_cmd(c, m: Message):
        await m.reply(
            "🤖 **Control Panel Connected.**",
            reply_markup=ReplyKeyboardMarkup(
                [["➕ Create API Key", "⚙️ API Key Settings"], ["🎁 Gift List"]],
                resize_keyboard=True
            )
        )

    @app_bot.on_callback_query(filters.regex("^gift_page:"))
    async def handle_gift_pagination(c: Client, query: CallbackQuery):
        user_id = str(query.from_user.id)
        action = query.data.split(":")[1] if ":" in query.data else None

        state = user_states.get(user_id, {}).get("gift_browser")
        if not state:
            await query.answer("Session expired. Please request gift list again.", show_alert=True)
            await query.message.delete()
            return

        gifts = state["gifts"]
        current_page = state["page"]
        per_page = 16
        total_pages = (len(gifts) + per_page - 1) // per_page

        if action == "next":
            if current_page + 1 < total_pages:
                new_page = current_page + 1
            else:
                await query.answer("You're on the last page.")
                return
        elif action == "prev":
            if current_page - 1 >= 0:
                new_page = current_page - 1
            else:
                await query.answer("You're on the first page.")
                return
        elif action == "close":
            await query.message.delete()
            del user_states[user_id]["gift_browser"]
            await query.answer("Gift list closed.")
            return
        else:
            await query.answer("Invalid action.")
            return

        state["page"] = new_page
        start_idx = new_page * per_page
        end_idx = start_idx + per_page
        page_gifts = gifts[start_idx:end_idx]

        lines_g = []
        for gift in page_gifts:
            name = get_gift_name(gift)
            emoji = get_gift_emoji(gift)
            gift_id = getattr(gift, "id", "?")
            price = getattr(gift, "stars", 0)
            lines_g.append(f"{emoji} **{name}**\n🆔 `{gift_id}` – {price}⭐\n")

        text_msg = f"**🎁 Available Gifts** (Page {new_page+1}/{total_pages})\n\n" + "\n".join(lines_g)

        buttons = []
        nav_buttons = []
        if new_page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data="gift_page:prev"))
        if new_page + 1 < total_pages:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data="gift_page:next"))
        if nav_buttons:
            buttons.append(nav_buttons)
        buttons.append([InlineKeyboardButton("❌ Close", callback_data="gift_page:close")])

        await query.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(buttons))
        await query.answer()

    @app_bot.on_message(filters.text & filters.private)
    async def handle_bot_logic(c, m: Message):
        user_id = str(m.from_user.id)
        text = m.text
        mapping = load_mapping()

        if text == "❌ Cancel":
            if user_id in user_states:
                if "client" in user_states[user_id]:
                    try:
                        await user_states[user_id]["client"].disconnect()
                    except Exception:
                        pass
                if "gift_browser" in user_states[user_id]:
                    del user_states[user_id]["gift_browser"]
                del user_states[user_id]
            await m.reply(
                "Cancelled.",
                reply_markup=ReplyKeyboardMarkup(
                    [["➕ Create API Key", "⚙️ API Key Settings"], ["🎁 Gift List"]],
                    resize_keyboard=True
                )
            )
            return

        if text == "🎁 Gift List":
            try:
                gifts_obj = await app_bot.invoke(raw.functions.payments.GetStarGifts(hash=0))
                gifts = getattr(gifts_obj, "gifts", [])
                if not gifts:
                    await m.reply("No gifts available at the moment.")
                    return

                if user_id not in user_states:
                    user_states[user_id] = {}
                user_states[user_id]["gift_browser"] = {
                    "gifts": gifts,
                    "page": 0
                }

                per_page = 16
                start_idx = 0
                end_idx = min(per_page, len(gifts))
                page_gifts = gifts[start_idx:end_idx]

                lines_g = []
                for gift in page_gifts:
                    name = get_gift_name(gift)
                    emoji = get_gift_emoji(gift)
                    gift_id = getattr(gift, "id", "?")
                    price = getattr(gift, "stars", 0)
                    lines_g.append(f"{emoji} **{name}**\n🆔 `{gift_id}` – {price}⭐\n")

                total_pages = (len(gifts) + per_page - 1) // per_page
                text_msg = f"**🎁 Available Gifts** (Page 1/{total_pages})\n\n" + "\n".join(lines_g)

                buttons = []
                nav_buttons = []
                if total_pages > 1:
                    nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data="gift_page:next"))
                if nav_buttons:
                    buttons.append(nav_buttons)
                buttons.append([InlineKeyboardButton("❌ Close", callback_data="gift_page:close")])

                sent = await m.reply(text_msg, reply_markup=InlineKeyboardMarkup(buttons))
                user_states[user_id]["gift_browser"]["message_id"] = sent.id
                user_states[user_id]["gift_browser"]["chat_id"] = sent.chat.id

            except Exception as e:
                await m.reply(f"❌ Failed to fetch gift list: {e}")
            return

        if text == "➕ Create API Key":
            user_states[user_id] = {"step": "phone"}
            await m.reply(
                "📱 Send **Phone Number** (with +):",
                reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)
            )
            return

        if text == "⚙️ API Key Settings":
            user_keys = mapping.get(user_id, [])
            valid_keys = [k for k in user_keys if os.path.exists(make_session_path(k) + ".session")]
            if not valid_keys:
                await m.reply("No Keys found.")
                return
            btns = [[k] for k in valid_keys]
            btns.append(["❌ Cancel"])
            await m.reply("Select Key:", reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True))
            return

        user_keys = mapping.get(user_id, [])
        if text in user_keys:
            key_name = text
            path = make_session_path(key_name)
            client = Client(path, api_id=API_ID, api_hash=API_HASH)
            status = await m.reply(f"⌛ Connecting to `{key_name}`...")
            try:
                await client.connect()
                me = await client.get_me()
                stars = await get_stars_balance(client)
                info = (
                    f"📊 **API Key Details**\n\n"
                    f"👤 Name: {me.first_name}\n"
                    f"🆔 ID: `{me.id}`\n"
                    f"⭐️ Balance: **{stars} Stars**\n"
                    f"📂 Key: `{key_name}`"
                )
                await status.delete()
                await m.reply(
                    info,
                    reply_markup=ReplyKeyboardMarkup(
                        [[f"🗑 Delete {key_name}"], ["❌ Cancel"]],
                        resize_keyboard=True
                    )
                )
            except Exception as e:
                if errors and isinstance(e, (errors.AuthKeyUnregistered, errors.SessionExpired)):
                    if os.path.exists(path + ".session"):
                        os.remove(path + ".session")
                    if key_name in user_keys:
                        user_keys.remove(key_name)
                    mapping[user_id] = user_keys
                    save_mapping(mapping)
                    await status.edit_text(f"❌ Key `{key_name}` expired and auto-deleted.")
                else:
                    await status.edit_text(f"❌ Error: {e}")
            finally:
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:
                    pass
            return

        if text.startswith("🗑 Delete "):
            target = text.replace("🗑 Delete ", "").strip()
            if target in mapping.get(user_id, []):
                path = make_session_path(target) + ".session"
                if os.path.exists(path):
                    os.remove(path)
                mapping[user_id].remove(target)
                save_mapping(mapping)
                await m.reply(
                    f"✅ Key `{target}` deleted.",
                    reply_markup=ReplyKeyboardMarkup(
                        [["➕ Create API Key", "⚙️ API Key Settings"], ["🎁 Gift List"]],
                        resize_keyboard=True
                    )
                )
            return

        if user_id in user_states:
            state = user_states[user_id]
            if "step" in state:
                if state["step"] == "phone":
                    state["phone"] = text.replace(" ", "")
                    state["name"] = secrets.token_hex(4)
                    client = Client(make_session_path(state["name"]), API_ID, API_HASH)
                    try:
                        await client.connect()
                        code = await client.send_code(state["phone"])
                        state.update({"hash": code.phone_code_hash, "client": client, "step": "otp"})
                        await m.reply(
                            "📩 **OTP Sent!** Enter code:",
                            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)
                        )
                    except Exception as e:
                        await m.reply(f"❌ Error: {e}")
                        del user_states[user_id]
                    return

                if state["step"] == "otp":
                    formatted_otp = " ".join(list(text.replace(" ", "").strip()))
                    try:
                        await state["client"].sign_in(state["phone"], state["hash"], formatted_otp)
                        if user_id not in mapping:
                            mapping[user_id] = []
                        mapping[user_id].append(state["name"])
                        save_mapping(mapping)
                        await m.reply(
                            f"✅ Created: `{state['name']}`",
                            reply_markup=ReplyKeyboardMarkup(
                                [["➕ Create API Key", "⚙️ API Key Settings"], ["🎁 Gift List"]],
                                resize_keyboard=True
                            )
                        )
                        await state["client"].disconnect()
                        del user_states[user_id]
                    except Exception as e:
                        if errors and isinstance(e, errors.SessionPasswordNeeded):
                            state["step"] = "2fa"
                            await m.reply(
                                "🔐 **2FA Required.** Send Password:",
                                reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)
                            )
                        else:
                            await m.reply(f"❌ Error: {e}")
                            del user_states[user_id]
                    return

                if state["step"] == "2fa":
                    try:
                        await state["client"].check_password(text.strip())
                        if user_id not in mapping:
                            mapping[user_id] = []
                        mapping[user_id].append(state["name"])
                        save_mapping(mapping)
                        await m.reply(
                            f"✅ Created: `{state['name']}`",
                            reply_markup=ReplyKeyboardMarkup(
                                [["➕ Create API Key", "⚙️ API Key Settings"], ["🎁 Gift List"]],
                                resize_keyboard=True
                            )
                        )
                        await state["client"].disconnect()
                        del user_states[user_id]
                    except Exception as e:
                        await m.reply(f"❌ 2FA Error: {e}")
