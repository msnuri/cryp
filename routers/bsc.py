"""
BSC (Binance Smart Chain) Sender
Native BNB + Any BEP20 token

POST /bsc/send      → BNB or any BEP20
POST /bsc/balance   → Check balances
GET  /bsc/assets    → Known tokens + rules
GET  /bsc/health
"""
import traceback
from decimal import Decimal
from time import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import TransactionNotFound, TimeExhausted

router = APIRouter(prefix="/bsc", tags=["BSC Network"])

CHAIN_ID = 56
DEFAULT_RPC = "https://bsc-dataseed.binance.org/"
GAS_LIMIT_FALLBACK = 100000
GAS_LIMIT_BNB = 21000
PRICE_CACHE_TTL = 30
MEMO_MAX = 0  # BEP20 transfer has no memo; native can use data but we skip for safety

# ERC20 / BEP20 minimal ABI
TOKEN_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]

ROUTER_ABI = [
    {
        "name": "getAmountsOut",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"},
        ],
        "outputs": [{"name": "", "type": "uint256[]"}],
    }
]

PANCAKE_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"

# symbol -> rules
ASSET_RULES = {
    "bnb": {
        "type": "native",
        "symbol": "BNB",
        "contract": None,
        "decimals": 18,
        "min_amount": 0.0001,
        "name": "BNB",
    },
    "usdt": {
        "type": "bep20",
        "symbol": "USDT",
        "contract": "0x55d398326f99059fF775485246999027B3197955",
        "decimals": 18,  # BSC USDT is 18
        "min_amount": 0.01,
        "name": "Tether USD (BSC)",
    },
    "usdc": {
        "type": "bep20",
        "symbol": "USDC",
        "contract": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        "decimals": 18,
        "min_amount": 0.01,
        "name": "USD Coin (BSC)",
    },
    "busd": {
        "type": "bep20",
        "symbol": "BUSD",
        "contract": "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56",
        "decimals": 18,
        "min_amount": 0.01,
        "name": "Binance USD",
    },
}

DEFAULT_BEP20 = {
    "type": "bep20",
    "symbol": "CUSTOM",
    "contract": None,
    "decimals": 18,
    "min_amount": 0.000001,
    "name": "Custom BEP20",
}

_price_cache = {"price": None, "timestamp": 0}


def format_decimal(value, precision=18):
    try:
        d = Decimal(str(value))
        s = f"{d:.{precision}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s if s else "0"
    except Exception:
        return str(value)


def to_serializable(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_serializable(v) for v in obj]
    return obj


def error_response(code, message, details=None, status_code=400):
    payload = {"success": False, "ok": False, "error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    return JSONResponse(content=to_serializable(payload), status_code=status_code)


def ok_response(data, message="ok"):
    return JSONResponse(
        content=to_serializable(
            {
                "success": True,
                "ok": True,
                "message": message,
                "timestamp": int(time()),
                "data": data,
            }
        )
    )


def get_bnb_price_pancake(w3: Web3) -> Decimal | None:
    try:
        router_c = w3.eth.contract(
            address=w3.to_checksum_address(PANCAKE_ROUTER), abi=ROUTER_ABI
        )
        amount_in = w3.to_wei(1, "ether")
        usdt = ASSET_RULES["usdt"]["contract"]
        amounts = router_c.functions.getAmountsOut(
            amount_in,
            [w3.to_checksum_address(WBNB), w3.to_checksum_address(usdt)],
        ).call()
        return Decimal(amounts[1]) / Decimal(10**18)
    except Exception:
        return None


async def get_bnb_usdt_price(w3: Web3) -> Decimal | None:
    global _price_cache
    now = time()
    if _price_cache["price"] is not None and (now - _price_cache["timestamp"]) < PRICE_CACHE_TTL:
        p = _price_cache["price"]
        return Decimal(str(p)) if not isinstance(p, Decimal) else p

    price = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT"
            )
            if resp.status_code == 200:
                price = Decimal(resp.json()["price"])
    except Exception:
        pass

    if price is None:
        price = get_bnb_price_pancake(w3)

    if price is not None:
        _price_cache = {"price": price, "timestamp": now}
    return price


def resolve_asset(asset: str | None, token_contract: str | None, decimals: int | None) -> dict:
    if token_contract and token_contract.strip():
        rules = dict(DEFAULT_BEP20)
        rules["contract"] = token_contract.strip()
        rules["symbol"] = (asset or "CUSTOM").upper()
        if decimals is not None:
            rules["decimals"] = decimals
        return rules

    if asset:
        key = asset.strip().lower()
        if key in ASSET_RULES:
            rules = dict(ASSET_RULES[key])
            if decimals is not None:
                rules["decimals"] = decimals
            return rules
        raise ValueError(
            f"Unknown asset '{asset}'. "
            f"Pass token_contract + decimals. Known: {list(ASSET_RULES.keys())}"
        )

    return dict(ASSET_RULES["bnb"])


class SendBody(BaseModel):
    private_key: str = Field(..., description="Hex private key")
    to_address: str
    amount: float = Field(..., gt=0)
    asset: str | None = Field(None, description="bnb | usdt | usdc | busd")
    token_contract: str | None = Field(None, description="Custom BEP20 contract 0x...")
    decimals: int | None = Field(None, ge=0, le=18)
    bsc_rpc: str | None = None


@router.post("/send")
async def bsc_send(body: SendBody):
    """Send native BNB or any BEP20 token on BSC."""
    try:
        rules = resolve_asset(body.asset, body.token_contract, body.decimals)
    except ValueError as e:
        return error_response("UNKNOWN_ASSET", str(e))

    amount = float(body.amount)
    min_amt = float(rules["min_amount"])
    if amount < min_amt:
        return error_response(
            "AMOUNT_TOO_LOW",
            f"Minimum for {rules['symbol']} is {min_amt}",
            {"asset": rules["symbol"], "amount": amount, "min_amount": min_amt},
        )

    rpc = body.bsc_rpc or DEFAULT_RPC
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        return error_response("CONNECTION_ERROR", "RPC connection failed", status_code=503)

    try:
        account = w3.eth.account.from_key(body.private_key)
        sender = w3.to_checksum_address(account.address)
        receiver = w3.to_checksum_address(body.to_address)
    except Exception as e:
        return error_response("INVALID_KEY_OR_ADDRESS", str(e))

    # ----- Native BNB -----
    if rules["type"] == "native":
        try:
            amount_wei = w3.to_wei(Decimal(str(amount)), "ether")
            before_bnb = w3.eth.get_balance(sender)
            gas_price = w3.eth.gas_price
            gas_limit = GAS_LIMIT_BNB
            total_need = amount_wei + gas_limit * gas_price
            if before_bnb < total_need:
                return error_response(
                    "INSUFFICIENT_BNB",
                    "Not enough BNB for amount + gas",
                    {
                        "balance_bnb": format_decimal(w3.from_wei(before_bnb, "ether")),
                        "required_bnb": format_decimal(w3.from_wei(total_need, "ether")),
                    },
                    402,
                )

            nonce = w3.eth.get_transaction_count(sender)
            tx = {
                "nonce": nonce,
                "to": receiver,
                "value": amount_wei,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "chainId": CHAIN_ID,
            }
            signed = w3.eth.account.sign_transaction(tx, body.private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 0:
                return error_response("FAILED", "Transaction reverted", status_code=400)

            after_bnb = w3.eth.get_balance(sender)
            gas_used = receipt.gasUsed
            gas_cost = gas_used * gas_price
            bnb_price = await get_bnb_usdt_price(w3)

            return ok_response(
                {
                    "success": True,
                    "network": "bsc",
                    "chain_id": CHAIN_ID,
                    "asset": "BNB",
                    "type": "native",
                    "name": rules["name"],
                    "sender": sender,
                    "receiver": receiver,
                    "amount": format_decimal(amount),
                    "decimals": 18,
                    "min_amount": min_amt,
                    "transaction_hash": w3.to_hex(tx_hash),
                    "txid": w3.to_hex(tx_hash),
                    "block_number": receipt.blockNumber,
                    "hash_status": "confirmed",
                    "before_balance_bnb": format_decimal(w3.from_wei(before_bnb, "ether")),
                    "after_balance_bnb": format_decimal(w3.from_wei(after_bnb, "ether")),
                    "gas_used": gas_used,
                    "gas_cost_bnb": format_decimal(w3.from_wei(gas_cost, "ether")),
                    "bnb_usdt_price": float(bnb_price) if bnb_price else None,
                },
                "BNB transfer completed",
            )
        except TimeExhausted:
            return error_response("TIMEOUT", "Transaction timeout", status_code=408)
        except Exception as e:
            return error_response(
                "SEND_FAILED", str(e), {"trace": traceback.format_exc()}, 500
            )

    # ----- BEP20 token -----
    try:
        contract_addr = w3.to_checksum_address(rules["contract"])
        token = w3.eth.contract(address=contract_addr, abi=TOKEN_ABI)

        # Auto decimals from chain if not forced
        try:
            onchain_dec = token.functions.decimals().call()
            decimals = int(body.decimals) if body.decimals is not None else int(onchain_dec)
        except Exception:
            decimals = int(rules["decimals"])

        amount_wei = int(Decimal(str(amount)) * (Decimal(10) ** decimals))
        before_token = token.functions.balanceOf(sender).call()
        if before_token < amount_wei:
            return error_response(
                "INSUFFICIENT_TOKEN",
                f"Low {rules['symbol']} balance",
                {
                    "balance": format_decimal(before_token / (10**decimals), decimals),
                    "required": format_decimal(amount, decimals),
                },
                402,
            )

        before_bnb = w3.eth.get_balance(sender)
        nonce = w3.eth.get_transaction_count(sender)
        gas_price = w3.eth.gas_price
        try:
            gas_limit = int(
                token.functions.transfer(receiver, amount_wei).estimate_gas({"from": sender})
                * 1.2
            )
        except Exception:
            gas_limit = GAS_LIMIT_FALLBACK

        if before_bnb < gas_limit * gas_price:
            return error_response(
                "INSUFFICIENT_BNB",
                "Not enough BNB for gas",
                {"balance_bnb": format_decimal(w3.from_wei(before_bnb, "ether"))},
                402,
            )

        tx = token.functions.transfer(receiver, amount_wei).build_transaction(
            {
                "chainId": CHAIN_ID,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "nonce": nonce,
            }
        )
        signed = w3.eth.account.sign_transaction(tx, body.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status == 0:
            return error_response("FAILED", "Transaction reverted", status_code=400)

        after_token = token.functions.balanceOf(sender).call()
        after_bnb = w3.eth.get_balance(sender)
        gas_used = receipt.gasUsed
        gas_cost = gas_used * gas_price
        bnb_price = await get_bnb_usdt_price(w3)

        # try symbol from chain
        symbol = rules["symbol"]
        try:
            symbol = token.functions.symbol().call() or symbol
        except Exception:
            pass

        return ok_response(
            {
                "success": True,
                "network": "bsc",
                "chain_id": CHAIN_ID,
                "asset": symbol,
                "type": "bep20",
                "name": rules.get("name", symbol),
                "token_contract": contract_addr,
                "sender": sender,
                "receiver": receiver,
                "amount": format_decimal(amount, decimals),
                "decimals": decimals,
                "min_amount": min_amt,
                "transaction_hash": w3.to_hex(tx_hash),
                "txid": w3.to_hex(tx_hash),
                "block_number": receipt.blockNumber,
                "hash_status": "confirmed",
                "before_balance_token": format_decimal(before_token / (10**decimals), decimals),
                "after_balance_token": format_decimal(after_token / (10**decimals), decimals),
                "before_balance_bnb": format_decimal(w3.from_wei(before_bnb, "ether")),
                "after_balance_bnb": format_decimal(w3.from_wei(after_bnb, "ether")),
                "gas_used": gas_used,
                "gas_cost_bnb": format_decimal(w3.from_wei(gas_cost, "ether")),
                "bnb_usdt_price": float(bnb_price) if bnb_price else None,
            },
            f"{symbol} transfer completed",
        )
    except TimeExhausted:
        return error_response("TIMEOUT", "Transaction timeout", status_code=408)
    except Exception as e:
        return error_response(
            "SEND_FAILED", str(e), {"trace": traceback.format_exc()}, 500
        )


@router.post("/balance")
async def bsc_balance(request: Request):
    """Check BNB + optional token balance. Body: private_key, optional token_contract/asset."""
    try:
        body = await request.json()
    except Exception:
        return error_response("INVALID_JSON", "Body must be JSON")

    private_key = body.get("private_key")
    if not private_key:
        return error_response("MISSING_PRIVATE_KEY", "private_key required")

    rpc = body.get("bsc_rpc") or DEFAULT_RPC
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        return error_response("CONNECTION_ERROR", "RPC failed", status_code=503)

    try:
        account = w3.eth.account.from_key(private_key)
        addr = w3.to_checksum_address(account.address)
    except Exception as e:
        return error_response("INVALID_PRIVATE_KEY", str(e))

    bnb_wei = w3.eth.get_balance(addr)
    bnb_bal = w3.from_wei(bnb_wei, "ether")
    bnb_price = await get_bnb_usdt_price(w3)

    result = {
        "address": addr,
        "network": "bsc",
        "bnb": {
            "wei": str(bnb_wei),
            "formatted": format_decimal(bnb_bal),
            "usd": format_decimal(Decimal(str(bnb_bal)) * bnb_price, 2) if bnb_price else None,
        },
        "price_bnb_usdt": float(bnb_price) if bnb_price else None,
        "tokens": {},
    }

    # optional single token
    asset = body.get("asset")
    token_contract = body.get("token_contract")
    if asset or token_contract:
        try:
            rules = resolve_asset(asset, token_contract, body.get("decimals"))
            if rules["type"] == "bep20" and rules["contract"]:
                c = w3.to_checksum_address(rules["contract"])
                token = w3.eth.contract(address=c, abi=TOKEN_ABI)
                dec = token.functions.decimals().call()
                bal = token.functions.balanceOf(addr).call()
                sym = rules["symbol"]
                try:
                    sym = token.functions.symbol().call() or sym
                except Exception:
                    pass
                result["tokens"][sym] = {
                    "contract": c,
                    "decimals": dec,
                    "balance": format_decimal(bal / (10**dec), dec),
                    "raw": str(bal),
                }
        except Exception as e:
            result["token_error"] = str(e)

    return ok_response(result)


@router.get("/assets")
async def bsc_assets():
    return {
        "network": "bsc",
        "chain_id": CHAIN_ID,
        "native": {
            k: {
                "symbol": v["symbol"],
                "decimals": v["decimals"],
                "min_amount": v["min_amount"],
                "name": v["name"],
            }
            for k, v in ASSET_RULES.items()
            if v["type"] == "native"
        },
        "bep20": {
            k: {
                "symbol": v["symbol"],
                "contract": v["contract"],
                "decimals": v["decimals"],
                "min_amount": v["min_amount"],
                "name": v["name"],
            }
            for k, v in ASSET_RULES.items()
            if v["type"] == "bep20"
        },
        "custom": "Pass token_contract + decimals for any BEP20",
    }


@router.get("/health")
async def bsc_health():
    return {
        "status": "ok",
        "service": "bsc-sender",
        "network": "bsc",
        "chain_id": CHAIN_ID,
        "supports": ["Native BNB", "Any BEP20 token"],
    }
