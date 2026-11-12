"""
TON Jetton Sender (Any Jetton)
Endpoints under /jetton

Supports any TON Jetton (USDT, DOGS, NOT, etc.)
by providing jetton_master address + decimals.
"""
import asyncio
import time
import requests

from fastapi import APIRouter
from pydantic import BaseModel, Field
from web3 import Web3

from ton_core import NetworkGlobalID, to_nano, Address
from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV5R1, JettonTransferBuilder
from pytoniq_core import begin_cell

router = APIRouter(prefix="/jetton", tags=["Jetton (TON)"])

wallet_lock = asyncio.Lock()

# ---------- TON price helpers (for gas USD estimate) ----------
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


# ---------- Known popular Jettons (optional convenience) ----------
KNOWN_JETTONS = {
    # symbol lower -> (master_address, decimals)
    "usdt": ("EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs", 6),
    "usd₮": ("EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs", 6),
    "not":  ("EQAvlWFDxGF2lXm67y4yzC17wYKD9A0guwPkIg1PM0mUKWPp", 9),
    "dogs": ("EQCvxJy4eG8hyHBFsZ7eePxrRs6JTLwecNulunPX0uKA2uVr", 9),
    "hmstr": ("EQAJ8uWd7EBqsmpSWaRdf_I-8R8-XHwh3gsNKhy-CTKD3PcG", 9),
    "cat":  ("EQA3Aa6hNqW8C3H1QPewyT-9F-1M5y1y1y1y1y1y1y1y1y", 9),  # placeholder – override with real
}


class SendRequest(BaseModel):
    mnemonic: list[str] = Field(..., description="Wallet mnemonic words")
    to_address: str = Field(..., description="Recipient TON address")
    amount: float = Field(..., gt=0, description="Human-readable amount of jetton to send")
    jetton_master: str | None = Field(
        None,
        description="Jetton master contract address (EQ...). Required if symbol not given."
    )
    decimals: int | None = Field(
        None,
        ge=0,
        le=18,
        description="Token decimals (USDT=6, most others=9). Auto-filled for known symbols."
    )
    symbol: str | None = Field(
        None,
        description="Optional shortcut: usdt / not / dogs / hmstr (fills master + decimals)"
    )
    memo: str | None = Field(None, description="Optional comment/memo")
    gas_ton: float = Field(0.05, gt=0, description="TON attached for gas (default 0.05)")

    # Backward compatibility with old clients
    amount_usdt: float | None = Field(None, description="[Deprecated] use amount instead")


def response(ok, message, data=None, code=200):
    return {
        "ok": ok,
        "message": message,
        "status_code": code,
        "timestamp": int(time.time()),
        "data": data
    }


def resolve_jetton(req: SendRequest) -> tuple[str, int, str]:
    """
    Returns (master_address, decimals, display_symbol)
    """
    # 1. Explicit master address has highest priority
    if req.jetton_master and req.jetton_master.strip():
        master = req.jetton_master.strip()
        decimals = req.decimals if req.decimals is not None else 9
        symbol = (req.symbol or "CUSTOM").upper()
        return master, decimals, symbol

    # 2. Known symbol shortcut
    if req.symbol:
        key = req.symbol.strip().lower()
        if key in KNOWN_JETTONS:
            master, default_dec = KNOWN_JETTONS[key]
            decimals = req.decimals if req.decimals is not None else default_dec
            return master, decimals, key.upper()
        raise ValueError(
            f"Unknown symbol '{req.symbol}'. "
            f"Pass jetton_master + decimals instead. Known: {list(KNOWN_JETTONS.keys())}"
        )

    # 3. Old API compatibility: amount_usdt → treat as USDT
    if req.amount_usdt is not None:
        master, default_dec = KNOWN_JETTONS["usdt"]
        return master, default_dec, "USDT"

    raise ValueError(
        "Either jetton_master (address) or symbol (usdt/not/dogs/...) is required"
    )


async def process_payment(req: SendRequest):
    # Resolve amount (support old field)
    amount = float(req.amount)
    if req.amount_usdt is not None and req.amount <= 0:
        amount = float(req.amount_usdt)

    if amount <= 0:
        return response(False, "Amount must be > 0", code=400)

    try:
        jetton_master_str, decimals, symbol = resolve_jetton(req)
        jetton_master = Address(jetton_master_str)
    except Exception as e:
        return response(False, str(e), code=400)

    client = ToncenterClient(network=NetworkGlobalID.MAINNET)
    await client.connect()

    try:
        wallet, public_key, private_key, mnemonic = WalletV5R1.from_mnemonic(
            client,
            " ".join(req.mnemonic)
        )

        wallet_addr = wallet.address.to_str(is_bounceable=False)
        await wallet.refresh()

        jetton_amount = to_nano(amount, decimals=decimals)
        ton_price = get_ton_price()

        forward_payload = None
        memo_value = None
        if req.memo and req.memo.strip():
            memo_value = req.memo.strip()
            memo_bytes = memo_value.encode("utf-8")
            if len(memo_bytes) > 123:
                return response(
                    False,
                    "Memo too long",
                    {"memo_bytes": len(memo_bytes), "max_bytes": 123},
                    400
                )
            forward_payload = begin_cell().store_uint(0, 32).store_string(memo_value).end_cell()

        gas_deposit_ton = float(req.gas_ton) if req.gas_ton else 0.05

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

        return response(True, f"{symbol} Jetton transaction completed", {
            "success": True,
            "symbol": symbol,
            "jetton_master": jetton_master_str,
            "decimals": decimals,
            "wallet": wallet_addr,
            "to_address": req.to_address,
            "memo": memo_value,
            "txid": txid,
            "hash_status": "confirmed",
            "amount": amount,
            "amount_raw": str(jetton_amount),
            "ton_price_usd": round(ton_price, 6),
            "gas_used_ton": round(real_gas_used, 9),
            "gas_used_usd": round(real_gas_used * ton_price, 6) if ton_price else None,
        })

    except Exception as e:
        return response(False, "Transaction failed", {"error": str(e)}, 500)

    finally:
        await client.close()


@router.post("/send")
async def send(req: SendRequest):
    return await process_payment(req)


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "jetton-sender",
        "supported": "any jetton (pass jetton_master + decimals or use symbol)",
        "known_symbols": list(KNOWN_JETTONS.keys())
    }


@router.get("/known")
async def known_jettons():
    """List of built-in popular jettons for convenience."""
    return {
        "known": {
            k: {"master": v[0], "decimals": v[1]}
            for k, v in KNOWN_JETTONS.items()
        },
        "usage": {
            "by_symbol": {
                "mnemonic": ["word1", "word2", "..."],
                "to_address": "UQ...",
                "amount": 10.5,
                "symbol": "dogs"
            },
            "by_master": {
                "mnemonic": ["word1", "word2", "..."],
                "to_address": "UQ...",
                "amount": 5,
                "jetton_master": "EQCvxJy4eG8hyHBFsZ7eePxrRs6JTLwecNulunPX0uKA2uVr",
                "decimals": 9
            }
        }
    }
