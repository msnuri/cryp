# Unified Telegram + Crypto API

Previously 6 separate Railway projects → **now 1 project / 1 process**.

## Services (all under one domain)

| Prefix | Old Project | Endpoints |
|--------|-------------|-----------|
| `/usdt` | usdtsend | `GET /usdt/health`, `POST /usdt/balance`, `POST /usdt/send-usdt` |
| `/ton` | tonsend | `GET /ton/health`, `POST /ton/send` |
| `/jetton` | usddtzettonsend | `GET /jetton/health`, `POST /jetton/send` |
| `/check` | checkdetails | `GET /check/`, `GET /check/check_transaction/{wallet}?status=all\|today` |
| `/wallet` | zenwallet | `GET /wallet/health`, `GET /wallet/create_wallet`, `GET\|POST /wallet/wallet_info` |
| `/gift` | telegram-gift-api | `GET /gift/`, `GET\|POST /gift/send-gift` + Telegram Bot (optional) |

Root: `GET /` → full route map  
Health: `GET /health`

## Project Structure

```
.
├── main.py
├── requirements.txt
├── README.md
├── Procfile
└── routers/
    ├── __init__.py
    ├── usdt.py
    ├── ton.py
    ├── jetton.py
    ├── check.py
    ├── wallet.py
    └── gift.py
```

## Environment Variables (Railway)

```
# Core
PORT=8000

# Optional – Telegram Gift Bot
API_ID=...
API_HASH=...
BOT_TOKEN=...
SESSION_DIR=/data/sessions

# Optional – TON Center API key (for /check)
API_KEY=...
```

> **Important:** If `API_ID`, `API_HASH` or `BOT_TOKEN` are missing, the service **starts normally**.  
> The gift bot is simply disabled. All other endpoints (`/usdt`, `/ton`, `/jetton`, `/check`, `/wallet`) continue to work without any crash.

## Deploy (Railway)

1. Push this folder as a **single** Railway service.
2. Set the env vars above (only what you need).
3. Add a volume mount if you want persistent sessions: `/data/sessions`.
4. Start command (recommended):

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

5. Done — only **one** service, one public URL.

## Example calls

```bash
# BSC USDT
curl -X POST https://YOUR_DOMAIN/usdt/send-usdt \
  -H "PRIVATE_KEY: 0x..." \
  -H "SENDER: 0x..." \
  -H "RECEIVER: 0x..." \
  -H "USDT_ADDRESS: 0x55d398326f99059fF775485246999027B3197955" \
  -H "AMOUNT_USDT: 1.5"

# TON native
curl -X POST https://YOUR_DOMAIN/ton/send \
  -H "Content-Type: application/json" \
  -d '{"mnemonic":["word1","word2",...],"to_address":"UQ...","amount_ton":0.1,"memo":"hi"}'

# USDT Jetton
curl -X POST https://YOUR_DOMAIN/jetton/send \
  -H "Content-Type: application/json" \
  -d '{"mnemonic":["word1",...],"to_address":"UQ...","amount_usdt":5,"memo":"payment"}'

# Check txs
curl "https://YOUR_DOMAIN/check/check_transaction/UQ...?status=today"

# Create wallet
curl https://YOUR_DOMAIN/wallet/create_wallet

# Send gift (requires Telegram credentials + valid session)
curl "https://YOUR_DOMAIN/gift/send-gift?target=username&session=yourkey&message=Enjoy"
```
