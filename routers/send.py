"""
Unified multi-network send
POST /send

Routes to TON or BSC based on `network` field.
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/send", tags=["Unified Send"])


class UnifiedSendRequest(BaseModel):
    network: str = Field(..., description="ton | bsc")
    amount: float = Field(..., gt=0)
    to_address: str

    # Asset selection
    asset: str | None = Field(
        None,
        description="ton/gram/usdt/dogs/not/hmstr (TON) or bnb/usdt/usdc/busd (BSC)",
    )
    # TON jetton
    jetton_master: str | None = None
    # BSC token
    token_contract: str | None = None
    decimals: int | None = Field(None, ge=0, le=18)

    # Auth — one of these depending on network
    mnemonic: list[str] | None = Field(None, description="Required for TON")
    private_key: str | None = Field(None, description="Required for BSC")

    memo: str | None = None
    gas_ton: float | None = None
    bsc_rpc: str | None = None


@router.post("")
@router.post("/")
async def unified_send(req: UnifiedSendRequest):
    network = (req.network or "").strip().lower()

    if network in ("ton", "telegram-open-network"):
        if not req.mnemonic:
            return {
                "ok": False,
                "success": False,
                "status_code": 400,
                "message": "mnemonic required for TON network",
            }
        from routers.ton import TransferRequest, process_transfer

        ton_req = TransferRequest(
            mnemonic=req.mnemonic,
            to_address=req.to_address,
            amount=req.amount,
            asset=req.asset,
            jetton_master=req.jetton_master,
            decimals=req.decimals,
            memo=req.memo,
            gas_ton=req.gas_ton,
        )
        result = await process_transfer(ton_req)
        if isinstance(result, dict):
            result.setdefault("network", "ton")
        return result

    if network in ("bsc", "binance", "bnb"):
        if not req.private_key:
            return {
                "ok": False,
                "success": False,
                "status_code": 400,
                "message": "private_key required for BSC network",
            }
        from routers.bsc import SendBody, bsc_send

        body = SendBody(
            private_key=req.private_key,
            to_address=req.to_address,
            amount=req.amount,
            asset=req.asset,
            token_contract=req.token_contract,
            decimals=req.decimals,
            bsc_rpc=req.bsc_rpc,
        )
        return await bsc_send(body)

    return {
        "ok": False,
        "success": False,
        "status_code": 400,
        "message": f"Unsupported network '{req.network}'. Use: ton | bsc",
        "supported_networks": {
            "ton": {
                "auth": "mnemonic",
                "assets": ["ton", "gram", "usdt", "dogs", "not", "hmstr", "any jetton via jetton_master"],
            },
            "bsc": {
                "auth": "private_key",
                "assets": ["bnb", "usdt", "usdc", "busd", "any BEP20 via token_contract"],
            },
        },
    }


@router.get("/networks")
async def list_networks():
    return {
        "networks": {
            "ton": {
                "name": "The Open Network",
                "auth": "mnemonic (12/24 words)",
                "native": "TON (Gram)",
                "tokens": "Any Jetton (USDT, DOGS, NOT, HMSTR, custom master)",
                "endpoints": ["POST /ton/transfer", "POST /send"],
            },
            "bsc": {
                "name": "BNB Smart Chain",
                "auth": "private_key",
                "native": "BNB",
                "tokens": "Any BEP20 (USDT, USDC, BUSD, custom contract)",
                "endpoints": ["POST /bsc/send", "POST /send"],
                "chain_id": 56,
            },
        },
        "not_supported_yet": [
            "ethereum",
            "polygon",
            "arbitrum",
            "solana",
            "tron",
        ],
        "note": "Only networks already in this codebase (TON + BSC/web3) are enabled. Others need extra SDKs.",
    }


@router.get("/health")
async def send_health():
    return {
        "status": "ok",
        "service": "unified-send",
        "networks": ["ton", "bsc"],
    }
