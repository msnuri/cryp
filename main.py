"""
Unified Telegram + Crypto API
All 6 previous Railway services in ONE project / ONE process.

Routes:
  /usdt/*       → BSC USDT send + balance
  /ton/*        → TON native + ANY Jetton (unified)
  /jetton/*     → Jetton sender (also available)
  /check/*      → TON transaction checker
  /wallet/*     → Create wallet + wallet info
  /gift/*       → Telegram Star Gift + Bot (optional)
  /bsc/*        → BSC native BNB + any BEP20
  /send/*       → Unified multi-network send (ton | bsc)
"""
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

load_dotenv()

from routers.usdt import router as usdt_router
from routers.ton import router as ton_router
from routers.jetton import router as jetton_router
from routers.check import router as check_router
from routers.wallet import router as wallet_router
from routers.gift import router as gift_router, app_bot
from routers.bsc import router as bsc_router
from routers.send import router as send_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    async def _start_bot():
        if app_bot is None:
            print("⚠️  Gift bot skipped (missing API_ID / API_HASH / BOT_TOKEN)")
            return
        try:
            await app_bot.start()
            print("✅ Telegram Gift Bot Online!")
        except Exception as e:
            print(f"⚠️  Bot start failed (check API_ID/API_HASH/BOT_TOKEN): {e}")

    # Start bot in background so healthcheck is not blocked
    bot_task = asyncio.create_task(_start_bot())
    yield
    if app_bot is not None:
        try:
            await app_bot.stop()
        except Exception:
            pass
        if not bot_task.done():
            bot_task.cancel()


app = FastAPI(
    title="Unified Telegram + Crypto API",
    description="All services (sendusdt, jetton, ton, bot, zenwallet, checkdetails) in one project",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount all routers
app.include_router(usdt_router)
app.include_router(ton_router)
app.include_router(jetton_router)
app.include_router(check_router)
app.include_router(wallet_router)
app.include_router(gift_router)
app.include_router(bsc_router)
app.include_router(send_router)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Unified API is running",
        "gift_bot_enabled": app_bot is not None,
        "services": {
            "usdt": {
                "health": "GET /usdt/health",
                "balance": "POST /usdt/balance",
                "send": "POST /usdt/send-usdt"
            },
            "ton": {
                "health": "GET /ton/health",
                "assets": "GET /ton/assets",
                "send": "POST /ton/send  (native TON only)",
                "transfer": "POST /ton/transfer  (native TON + any Jetton) ★ recommended",
                "send_jetton": "POST /ton/send-jetton"
            },
            "jetton": {
                "health": "GET /jetton/health",
                "known": "GET /jetton/known",
                "send": "POST /jetton/send  (any jetton: usdt/dogs/not or custom master)"
            },
            "check": {
                "home": "GET /check/",
                "transactions": "GET /check/check_transaction/{wallet}?status=all|today"
            },
            "wallet": {
                "health": "GET /wallet/health",
                "create": "GET /wallet/create_wallet",
                "info": "GET|POST /wallet/wallet_info"
            },
            "gift": {
                "health": "GET /gift/",
                "send": "GET|POST /gift/send-gift",
                "bot": "enabled" if app_bot is not None else "disabled (missing credentials)"
            },
            "bsc": {
                "health": "GET /bsc/health",
                "assets": "GET /bsc/assets",
                "balance": "POST /bsc/balance",
                "send": "POST /bsc/send  (BNB + any BEP20)"
            },
            "send": {
                "health": "GET /send/health",
                "networks": "GET /send/networks",
                "send": "POST /send  (network=ton|bsc)"
            }
        }
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "unified-api",
        "gift_bot": "enabled" if app_bot is not None else "disabled"
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
