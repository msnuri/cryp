"""
Zen Wallet - Create & Info
Endpoints under /wallet
"""
import asyncio
import requests
from web3 import Web3
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV5R1
from ton_core import NetworkGlobalID
from tonsdk.crypto import mnemonic_new

router = APIRouter(prefix="/wallet", tags=["Zen Wallet"])

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
    response = requests.get(url, timeout=10)
    data = response.json()
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


async def get_wallet_info(mnemonic_words):
    client = ToncenterClient(network=NetworkGlobalID.MAINNET)
    await client.connect()
    try:
        wallet, _, _, _ = WalletV5R1.from_mnemonic(client, " ".join(mnemonic_words))
        await wallet.refresh()
        bounceable = wallet.address.to_str(is_bounceable=True)
        non_bounceable = wallet.address.to_str(is_bounceable=False)
        raw = wallet.address.to_str(is_bounceable=False, is_user_friendly=False)
        balance_ton = wallet.balance / 1e9
        ton_price = get_ton_price()
        balance_usd = balance_ton * ton_price
        return {
            "addresses": {
                "bounceable": bounceable,
                "non_bounceable": non_bounceable,
                "raw": raw
            },
            "balance": {
                "ton": round(balance_ton, 9),
                "usd": round(balance_usd, 6)
            },
            "ton_price_usd": round(ton_price, 6)
        }
    finally:
        await client.close()


async def create_new_wallet():
    mnemonic_words = mnemonic_new()
    client = ToncenterClient(network=NetworkGlobalID.MAINNET)
    await client.connect()
    try:
        wallet, _, _, _ = WalletV5R1.from_mnemonic(client, " ".join(mnemonic_words))
        await wallet.refresh()
        bounceable = wallet.address.to_str(is_bounceable=True)
        non_bounceable = wallet.address.to_str(is_bounceable=False)
        raw = wallet.address.to_str(is_bounceable=False, is_user_friendly=False)
        return {
            "mnemonic": mnemonic_words,
            "mnemonic_text": " ".join(mnemonic_words),
            "addresses": {
                "bounceable": bounceable,
                "non_bounceable": non_bounceable,
                "raw": raw
            },
            "balance": {
                "ton": 0.0,
                "usd": 0.0
            }
        }
    finally:
        await client.close()


def process_mnemonic_string(mnemonic_str):
    if not mnemonic_str:
        raise ValueError("Empty mnemonic")
    words = mnemonic_str.strip().split()
    if len(words) not in (12, 24):
        raise ValueError(f"Mnemonic must have 12 or 24 words, got {len(words)}")
    return words


@router.get("/")
async def home():
    return {
        "status": "running",
        "endpoints": {
            "GET /wallet/create_wallet": "Generate new wallet",
            "GET /wallet/wallet_info?mnemonic=...": "Wallet info",
            "POST /wallet/wallet_info": "Wallet info (JSON body)",
            "GET /wallet/health": "Health check"
        }
    }


@router.get("/health")
async def health():
    return {"status": "healthy", "service": "zenwallet"}


@router.get("/create_wallet")
async def create_wallet_api():
    try:
        result = await create_new_wallet()
        return {"success": True, "data": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


class MnemonicBody(BaseModel):
    mnemonic: str


@router.post("/wallet_info")
async def wallet_info_post(body: MnemonicBody):
    try:
        words = process_mnemonic_string(body.mnemonic)
        result = await get_wallet_info(words)
        return {"success": True, "data": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@router.get("/wallet_info")
async def wallet_info_get(mnemonic: str = Query(...)):
    try:
        words = process_mnemonic_string(mnemonic)
        result = await get_wallet_info(words)
        return {"success": True, "data": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@router.get("/wallet_info/{mnemonic:path}")
async def wallet_info_path(mnemonic: str):
    try:
        words = process_mnemonic_string(mnemonic)
        result = await get_wallet_info(words)
        return {"success": True, "data": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})
