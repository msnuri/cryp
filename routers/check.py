"""
TON Transaction Checker
Endpoints under /check
"""
import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import aiohttp
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/check", tags=["TON Transaction Check"])

TONCENTER_API_URL = "https://toncenter.com/api/v3/transactions"
API_KEY = os.getenv("API_KEY")
BATCH_LIMIT = 100
REQUEST_DELAY = 0.15
MAX_RETRIES = 5
REQUEST_TIMEOUT = 30


def nano_to_ton(value) -> float:
    try:
        return round(int(value) / 1_000_000_000, 9)
    except:
        return 0.0


def extract_comment(msg: Optional[Dict]) -> Optional[str]:
    try:
        if not msg:
            return None
        content = msg.get("message_content")
        if content and "decoded" in content:
            decoded = content["decoded"]
            if decoded and decoded.get("@type") == "text_comment":
                return decoded.get("comment")
        return None
    except:
        return None


def transform_transaction(tx: Dict[str, Any]) -> Dict[str, Any]:
    try:
        timestamp = tx.get("now") or tx.get("utime")
        tx_date = None
        if timestamp:
            tx_date = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()

        incoming = None
        in_msg = tx.get("in_msg")
        if in_msg:
            value = int(in_msg.get("value", 0))
            incoming = {
                "from": in_msg.get("source"),
                "to": in_msg.get("destination"),
                "amount_ton": nano_to_ton(value),
                "memo": extract_comment(in_msg),
                "status": "success" if value > 0 else "failed"
            }

        outgoing = []
        for msg in tx.get("out_msgs", []):
            value = int(msg.get("value", 0))
            outgoing.append({
                "from": msg.get("source"),
                "to": msg.get("destination"),
                "amount_ton": nano_to_ton(value),
                "memo": extract_comment(msg),
                "status": "success" if value > 0 else "failed"
            })

        return {
            "lt": str(tx.get("lt")),
            "hash": tx.get("hash"),
            "date": tx_date,
            "incoming": incoming,
            "outgoing": outgoing
        }
    except Exception as e:
        return {
            "error": str(e),
            "raw_transaction": tx
        }


def is_today(date_string: str) -> bool:
    try:
        tx_date = datetime.fromisoformat(date_string.replace("Z", "+00:00")).date()
        today = datetime.now(timezone.utc).date()
        return tx_date == today
    except:
        return False


async def fetch_batch(session: aiohttp.ClientSession, wallet: str, offset: int):
    params = {
        "account": wallet,
        "limit": BATCH_LIMIT,
        "offset": offset,
        "sort": "desc"
    }
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(
                TONCENTER_API_URL,
                params=params,
                timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    return {
                        "success": False,
                        "status_code": response.status,
                        "error": error_text,
                        "transactions": []
                    }
                data = await response.json()
                transactions = data.get("transactions", [])
                return {
                    "success": True,
                    "status_code": response.status,
                    "transactions": transactions
                }
        except asyncio.TimeoutError:
            if attempt == MAX_RETRIES - 1:
                return {
                    "success": False,
                    "status_code": 408,
                    "error": "Request timeout",
                    "transactions": []
                }
        except aiohttp.ClientError as e:
            if attempt == MAX_RETRIES - 1:
                return {
                    "success": False,
                    "status_code": 503,
                    "error": str(e),
                    "transactions": []
                }
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                return {
                    "success": False,
                    "status_code": 500,
                    "error": str(e),
                    "transactions": []
                }
        await asyncio.sleep(1)


async def fetch_all_transactions(wallet: str):
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    timeout = aiohttp.ClientTimeout(total=None)
    all_transactions = []
    errors = []
    offset = 0

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        while True:
            result = await fetch_batch(session, wallet, offset)
            if not result["success"]:
                errors.append({
                    "offset": offset,
                    "status_code": result.get("status_code"),
                    "error": result.get("error")
                })
                break
            batch_transactions = result["transactions"]
            if not batch_transactions:
                break
            transformed = [transform_transaction(tx) for tx in batch_transactions]
            all_transactions.extend(transformed)
            offset += BATCH_LIMIT
            if len(batch_transactions) < BATCH_LIMIT:
                break
            await asyncio.sleep(REQUEST_DELAY)

    return {
        "transactions": all_transactions,
        "errors": errors
    }


@router.get("/")
async def home():
    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "message": "TON Transaction API Running",
            "routes": {
                "all_transactions": "/check/check_transaction/WALLET?status=all",
                "today_transactions": "/check/check_transaction/WALLET?status=today"
            }
        }
    )


@router.get("/check_transaction/{wallet}")
async def check_transaction(
    wallet: str,
    status: str = Query("all", description="all or today")
):
    try:
        status = status.lower()
        if status not in ["all", "today"]:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "status_code": 400,
                    "error": "Invalid status. Use all or today"
                }
            )
        result = await fetch_all_transactions(wallet)
        transactions = result["transactions"]
        errors = result["errors"]
        if status == "today":
            transactions = [
                tx for tx in transactions
                if tx.get("date") and is_today(tx["date"])
            ]
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "status_code": 200,
                "wallet": wallet,
                "filter": status,
                "server_time": datetime.now(timezone.utc).isoformat(),
                "total_transactions": len(transactions),
                "errors": errors,
                "transactions": transactions
            }
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "status_code": 500,
                "error": str(e),
                "transactions": []
            }
        )
