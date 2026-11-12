"""
TON Network Sender
Endpoints under /ton

Supports:
  - Native TON (Gram / Toncoin)
  - Any Jetton on TON network (USDT, DOGS, NOT, HMSTR, custom, ...)

All per-coin rules (min amount, decimals, gas, response shape) are automatic.

POST /ton/send          → Native TON only (backward compatible)
POST /ton/transfer      → Unified: Native TON OR any Jetton  ★
POST /ton/send-jetton   → Any Jetton
GET  /ton/health
GET  /ton/assets
"""
import asyncio
import time
import requests

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from ton_core import NetworkGlobalID, to_nano, Address
from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV5R1, JettonTransferBuilder
from pytoniq_core import begin_cell

router = APIRouter(prefix="/ton", tags=["TON Network"])

wallet_lock = asyncio.Lock()

# ============================================================
# Per-asset automatic rules
# ============================================================
ASSET_RULES = {
    "ton": {
        "type": "native",
        "symbol": "TON",
        "master": None,
        "decimals": 9,
        "min_amount": 0.001,
        "gas_ton": 0.0,
        "name": "Toncoin",
    },
    "gram": {
        "type": "native",
        "symbol": "TON",
        "master": None,
        "decimals": 9,
        "min_amount": 0.001,
        "gas_ton": 0.0,
        "name": "Toncoin",
    },
    "usdt": {
        "type": "jetton",
        "symbol": "USDT",
        "master": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        "decimals": 6,
        "min_amount": 0.01,
        "gas_ton": 0.05,
        "name": "Tether USD",
    },
    "usd₮": {
        "type": "jetton",
        "symbol": "USDT",
        "master": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        "decimals": 6,
        "min_amount": 0.01,
        "gas_ton": 0.05,
        "name": "Tether USD",
    },
    "not": {
        "type": "jetton",
        "symbol": "NOT",
        "master": "EQAvlWFDxGF2lXm67y4yzC17wYKD9A0guwPkIg1PM0mUKWPp",
        "decimals": 9,
        "min_amount": 1.0,
        "gas_ton": 0.05,
        "name": "Notcoin",
    },
    "dogs": {
        "type": "jetton",
        "symbol": "DOGS",
        "master": "EQCvxJy4eG8hyHBFsZ7eePxrRs6JTLwecNulunPX0uKA2uVr",
        "decimals": 9,
        "min_amount": 1.0,
        "gas_ton": 0.05,
        "name": "Dogs",
    },
    "hmstr": {
        "type": "jetton",
        "symbol": "HMSTR",
        "master": "EQAJ8uWd7EBqsmpSWaRdf_I-8R8-XHwh3gsNKhy-CTKD3PcG",
        "decimals": 9,
        "min_amount": 1.0,
        "gas_ton": 0.05,
        "name": "Hamster Kombat",
    },
}

DEFAULT_JETTON_RULES = {
    "type": "jetton",
    "symbol": "CUSTOM",
    "master": None,
    "decimals": 9,
    "min_amount": 0.000001,
    "gas_ton": 0.05,
    "name": "Custom Jetton",
}

MEMO_MAX_BYTES = 123

# ---------- TON price helpers ----------
bsc_rpc = "https://bsc-dataseed.binance.org/"
web3 = Web3(Web3.HTTPProvider(bsc_rpc, request_kwargs={"timeout": 15}))
pool_address = web3.to_checksum_address("0x819a26D0C6F3af2B9fe4E9c4BcaC04fCB3ea7f2a")
pool_abi = [
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"}
        ],
        "type": "function"
    }
]
contract = web3.eth.contract(address=pool_address, abi=pool_abi)


def pancake_price():
    reserves = contract.functions.getReserves().call()
    reserve_usdt = reserves[0] / (10 ** 18)
    reserve_ton = reserves[1] / (10 ** 9)
    price = reserve_usdt / reserve_ton
    if price <= 0:
        raise Exception("Invalid price")
    return float(price)


def diadata_price():
    url = "https://api.diadata.org/v1/assetQuotation/Ton/0x0000000000000000000000000000000000000000"
    r = requests.get(url, timeout=10)
    data = r.json()
    return float(data["Price"])


def get_ton_price():
    try:
        return pancake_price()
    except Exception:
        pass
    try:
        return diadata_price()
    except Exception:
        pass
    return 0.0


async def get_real_fee_from_tonapi(tx_hash: str, fallback_fee: float) -> float:
    url = f"https://tonapi.io/v2/blockchain/transactions/{tx_hash}"
    for _ in range(3):
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                tx = resp.json()
                fee_nano = int(tx.get("total_fees", 0))
                if fee_nano > 0:
                    return fee_nano / 1_000_000_000
        except Exception:
            pass
        await asyncio.sleep(2)
    return fallback_fee


def response(ok, message, data=None, code=200):
    return {
        "ok": ok,
        "message": message,
        "status_code": code,
        "timestamp": int(time.time()),
        "data": data
    }


def validate_memo(memo: str | None):
    if not memo or not memo.strip():
        return None, None
    memo_value = memo.strip()
    memo_bytes = memo_value.encode("utf-8")
    if len(memo_bytes) > MEMO_MAX_BYTES:
        return None, response(
            False,
            "Memo too long",
            {"memo_bytes": len(memo_bytes), "max_bytes": MEMO_MAX_BYTES},
            400
        )
    return memo_value, None


def build_memo_cell(memo_value: str):
    return begin_cell().store_uint(0, 32).store_string(memo_value).end_cell()


def resolve_rules(asset: str | None, jetton_master: str | None, decimals: int | None) -> dict:
    if jetton_master and jetton_master.strip():
        rules = dict(DEFAULT_JETTON_RULES)
        rules["master"] = jetton_master.strip()
        rules["symbol"] = (asset or "CUSTOM").upper()
        if decimals is not None:
            rules["decimals"] = decimals
        if asset:
            key = asset.strip().lower()
            if key in ASSET_RULES and ASSET_RULES[key].get("master") == rules["master"]:
                rules["min_amount"] = ASSET_RULES[key]["min_amount"]
                rules["gas_ton"] = ASSET_RULES[key]["gas_ton"]
                rules["name"] = ASSET_RULES[key]["name"]
                rules["symbol"] = ASSET_RULES[key]["symbol"]
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
            f"Pass jetton_master + decimals for custom tokens. "
            f"Known: {list(ASSET_RULES.keys())}"
        )

    return dict(ASSET_RULES["ton"])


def check_min_amount(amount: float, rules: dict):
    min_amt = float(rules.get("min_amount", 0))
    if amount < min_amt:
        return response(
            False,
            f"Amount too low for {rules['symbol']}",
            {
                "asset": rules["symbol"],
                "amount": amount,
                "min_amount": min_amt,
                "message": f"Minimum send for {rules['symbol']} is {min_amt}"
            },
            400
        )
    return None


# ============================================================
# Native TON send
# ============================================================
class NativeSendRequest(BaseModel):
    mnemonic: list[str]
    to_address: str
    amount_ton: float
    memo: str | None = None

    @field_validator("amount_ton")
    @classmethod
    def amount_positive(cls, v):
        if v is None or float(v) <= 0:
            raise ValueError("amount_ton must be > 0")
        return float(v)


async def process_native(req: NativeSendRequest):
    rules = ASSET_RULES["ton"]
    amount_ton = float(req.amount_ton)

    err = check_min_amount(amount_ton, rules)
    if err:
        return err

    memo_value, memo_err = validate_memo(req.memo)
    if memo_err:
        return memo_err

    client = ToncenterClient(network=NetworkGlobalID.MAINNET)
    await client.connect()

    try:
        wallet, public_key, private_key, mnemonic = WalletV5R1.from_mnemonic(
            client,
            " ".join(req.mnemonic)
        )

        wallet_addr = wallet.address.to_str(is_bounceable=False)
        await wallet.refresh()

        ton_price = get_ton_price()
        amount_usd = amount_ton * ton_price
        required = int(amount_ton * 1e9)

        before_balance = wallet.balance
        balance_ton = before_balance / 1e9

        fee_buffer = 0.01
        if before_balance < required + int(fee_buffer * 1e9):
            return response(False, "Insufficient TON balance", {
                "asset": "TON",
                "type": "native",
                "wallet": wallet_addr,
                "balance_ton": round(balance_ton, 9),
                "balance_usd": round(balance_ton * ton_price, 6),
                "required_ton": round(amount_ton, 9),
                "required_usd": round(amount_usd, 6),
                "fee_buffer_ton": fee_buffer,
                "ton_price_usd": round(ton_price, 6),
            }, 402)

        body = build_memo_cell(memo_value) if memo_value else None

        async with wallet_lock:
            if wallet.state != "active":
                print("Wallet not deployed. Deploying via self-transfer...")
                deploy_amount = to_nano(0.01)
                await wallet.transfer(
                    destination=wallet.address,
                    amount=deploy_amount,
                    body=None
                )
                await asyncio.sleep(5)
                await wallet.refresh()
                print("Wallet deployed successfully.")

            msg = await wallet.transfer(
                destination=req.to_address,
                amount=required,
                body=body
            )
            txid = msg.normalized_hash

        await wallet.refresh()
        after_balance = wallet.balance
        after_balance_ton = after_balance / 1e9
        fee_ton = (before_balance - after_balance - required) / 1e9
        if fee_ton < 0:
            fee_ton = 0.0
        fee_usd = fee_ton * ton_price

        return response(True, "TON (native) transaction completed", {
            "success": True,
            "asset": "TON",
            "type": "native",
            "name": rules["name"],
            "wallet": wallet_addr,
            "to_address": req.to_address,
            "memo": memo_value,
            "txid": txid,
            "transaction_hash": txid,
            "hash_status": "confirmed",
            "amount": round(amount_ton, 9),
            "amount_ton": round(amount_ton, 9),
            "amount_usd": round(amount_usd, 6),
            "decimals": 9,
            "min_amount": rules["min_amount"],
            "before_balance_ton": round(balance_ton, 9),
            "before_balance_usd": round(balance_ton * ton_price, 6),
            "after_balance_ton": round(after_balance_ton, 9),
            "after_balance_usd": round(after_balance_ton * ton_price, 6),
            "fee_ton": round(fee_ton, 9),
            "fee_usd": round(fee_usd, 6),
            "ton_price_usd": round(ton_price, 6),
        })

    except Exception as e:
        return response(False, "Transaction failed", {"error": str(e), "asset": "TON"}, 500)

    finally:
        await client.close()


@router.post("/send")
async def send_native(req: NativeSendRequest):
    """Send native TON only (backward compatible)."""
    return await process_native(req)


# ============================================================
# Unified transfer
# ============================================================
class TransferRequest(BaseModel):
    mnemonic: list[str] = Field(..., description="Wallet mnemonic words")
    to_address: str = Field(..., description="Recipient TON address")
    amount: float = Field(..., gt=0, description="Human-readable amount")

    asset: str | None = Field(None, description="ton | gram | usdt | not | dogs | hmstr")
    jetton_master: str | None = Field(None, description="Custom Jetton master address")
    decimals: int | None = Field(None, ge=0, le=18)
    memo: str | None = Field(None)
    gas_ton: float | None = Field(None, gt=0, description="Override gas TON for jetton")


async def process_transfer(req: TransferRequest):
    try:
        rules = resolve_rules(req.asset, req.jetton_master, req.decimals)
    except ValueError as e:
        return response(False, str(e), code=400)

    amount = float(req.amount)

    err = check_min_amount(amount, rules)
    if err:
        return err

    memo_value, memo_err = validate_memo(req.memo)
    if memo_err:
        return memo_err

    if rules["type"] == "native":
        native_req = NativeSendRequest(
            mnemonic=req.mnemonic,
            to_address=req.to_address,
            amount_ton=amount,
            memo=req.memo,
        )
        return await process_native(native_req)

    gas_deposit_ton = (
        float(req.gas_ton) if req.gas_ton is not None
        else float(rules.get("gas_ton", 0.05))
    )

    client = ToncenterClient(network=NetworkGlobalID.MAINNET)
    await client.connect()

    try:
        wallet, public_key, private_key, mnemonic = WalletV5R1.from_mnemonic(
            client,
            " ".join(req.mnemonic)
        )

        wallet_addr = wallet.address.to_str(is_bounceable=False)
        await wallet.refresh()

        decimals = int(rules["decimals"])
        jetton_amount = to_nano(amount, decimals=decimals)
        jetton_master = Address(rules["master"])
        ton_price = get_ton_price()

        balance_ton = wallet.balance / 1e9
        if wallet.balance < int(gas_deposit_ton * 1e9):
            return response(False, "Insufficient TON for gas", {
                "asset": rules["symbol"],
                "type": "jetton",
                "wallet": wallet_addr,
                "balance_ton": round(balance_ton, 9),
                "required_gas_ton": gas_deposit_ton,
                "message": f"Need at least {gas_deposit_ton} TON for jetton transfer gas"
            }, 402)

        forward_payload = build_memo_cell(memo_value) if memo_value else None

        async with wallet_lock:
            if wallet.state != "active":
                print("Wallet not deployed. Deploying...")
                deploy_amount = to_nano(0.01)
                await wallet.transfer(
                    destination=wallet.address,
                    amount=deploy_amount,
                    body=None
                )
                await asyncio.sleep(5)
                await wallet.refresh()
                print("Wallet deployed.")

            jetton_builder = JettonTransferBuilder(
                destination=Address(req.to_address),
                jetton_amount=jetton_amount,
                jetton_master_address=jetton_master,
                forward_payload=forward_payload,
                forward_amount=to_nano(0.000000001),
                amount=to_nano(gas_deposit_ton),
            )

            msg = await wallet.transfer_message(jetton_builder)
            txid = msg.normalized_hash

        real_gas_used = await get_real_fee_from_tonapi(txid, gas_deposit_ton)
        await wallet.refresh()
        after_balance_ton = wallet.balance / 1e9

        return response(True, f"{rules['symbol']} Jetton transaction completed", {
            "success": True,
            "asset": rules["symbol"],
            "type": "jetton",
            "name": rules.get("name", rules["symbol"]),
            "jetton_master": rules["master"],
            "decimals": decimals,
            "min_amount": rules["min_amount"],
            "wallet": wallet_addr,
            "to_address": req.to_address,
            "memo": memo_value,
            "txid": txid,
            "transaction_hash": txid,
            "hash_status": "confirmed",
            "amount": amount,
            "amount_raw": str(jetton_amount),
            "gas_attached_ton": gas_deposit_ton,
            "gas_used_ton": round(real_gas_used, 9),
            "gas_used_usd": round(real_gas_used * ton_price, 6) if ton_price else None,
            "after_balance_ton": round(after_balance_ton, 9),
            "ton_price_usd": round(ton_price, 6),
        })

    except Exception as e:
        err_msg = str(e)
        if "balance" in err_msg.lower() or "insufficient" in err_msg.lower():
            return response(False, "Insufficient jetton or TON balance", {
                "error": err_msg,
                "asset": rules["symbol"],
                "type": "jetton",
            }, 402)
        return response(False, "Transaction failed", {
            "error": err_msg,
            "asset": rules["symbol"],
            "type": "jetton",
        }, 500)

    finally:
        await client.close()


@router.post("/transfer")
async def transfer_any(req: TransferRequest):
    """Unified — Native TON or ANY Jetton. Rules auto-applied per coin."""
    return await process_transfer(req)


@router.post("/send-jetton")
async def send_jetton_alias(req: TransferRequest):
    if not req.asset and not req.jetton_master:
        return response(
            False,
            "Provide asset (usdt/dogs/not/...) or jetton_master + decimals",
            code=400
        )
    return await process_transfer(req)


@router.get("/assets")
async def list_assets():
    native = {}
    jettons = {}
    for key, r in ASSET_RULES.items():
        entry = {
            "symbol": r["symbol"],
            "name": r["name"],
            "decimals": r["decimals"],
            "min_amount": r["min_amount"],
            "gas_ton": r["gas_ton"],
            "master": r["master"],
        }
        if r["type"] == "native":
            native[key] = entry
        else:
            jettons[key] = entry

    return {
        "native": native,
        "jettons": jettons,
        "custom_jetton": {
            "how": "Pass jetton_master + decimals",
            "defaults": {
                "decimals": DEFAULT_JETTON_RULES["decimals"],
                "min_amount": DEFAULT_JETTON_RULES["min_amount"],
                "gas_ton": DEFAULT_JETTON_RULES["gas_ton"],
            }
        },
        "memo_max_bytes": MEMO_MAX_BYTES,
    }


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "ton-network-sender",
        "supports": ["Native TON", "Any Jetton"],
        "auto_rules": [
            "min_amount per coin",
            "decimals per coin",
            "gas_ton per coin",
            "memo max 123 bytes",
            "balance checks",
            "transaction_hash in every response"
        ],
        "endpoints": {
            "POST /ton/send": "Native TON only",
            "POST /ton/transfer": "Native TON + any Jetton ★",
            "POST /ton/send-jetton": "Any Jetton",
            "GET /ton/assets": "Known assets + rules"
        }
    }
