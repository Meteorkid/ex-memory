"""钱包、红包、转账数据管理。"""

import json
import random
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config import get_ex_dir
from core.file_utils import atomic_write_json

logger = logging.getLogger("ex-memory")

# ── 红包触发条件 ──

RED_PACKET_TRIGGERS = {
    "sentimental": {
        "keywords": ["想你", "以前", "记得", "那时候", "回忆", "如果", "后悔", "对不起", "谢谢"],
        "amount_range": (0.52, 5.20),
        "notes": ["要开心哦", "突然想到你", "给你的", "别难过了", "520", "一切都会好的"],
    },
    "make_up": {
        "keywords": ["对不起", "错了", "别生气", "和好", "原谅", "不理我"],
        "amount_range": (5.20, 20.00),
        "notes": ["我错了嘛", "别生气了", "请你喝奶茶", "和好吧", "原谅我"],
    },
    "celebrate": {
        "keywords": ["生日快乐", "纪念日", "恭喜", "厉害", "棒", "加油", "新年", "节日"],
        "amount_range": (6.66, 18.88),
        "notes": ["生日快乐🎂", "恭喜恭喜", "你真棒", "节日快乐", "庆祝一下"],
    },
    "random_cute": {
        "keywords": ["撒娇", "饿了", "困了", "累", "好烦", "想哭", "委屈"],
        "amount_range": (0.01, 5.20),
        "notes": ["拿去买糖", "请你吃好吃的", "乖", "开心一点", "给你的小惊喜"],
    },
}

# ── 钱包管理 ──


def get_wallet_path(slug: str) -> Path:
    return get_ex_dir(slug) / "wallet.json"


def load_wallet(slug: str) -> dict:
    """加载钱包数据，不存在则初始化。"""
    p = get_wallet_path(slug)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"balance": 0.0, "transactions": []}


def save_wallet(slug: str, wallet: dict) -> None:
    atomic_write_json(get_wallet_path(slug), wallet)


def add_transaction(slug: str, tx_type: str, amount: float, note: str = "", ref_id: str = "") -> dict:
    """添加一笔交易记录。返回更新后的钱包。"""
    wallet = load_wallet(slug)
    if tx_type in ("red_packet_received", "transfer_received"):
        wallet["balance"] = round(wallet["balance"] + amount, 2)
    elif tx_type in ("red_packet_sent", "transfer_sent"):
        wallet["balance"] = max(0, round(wallet["balance"] - amount, 2))

    wallet["transactions"].append({
        "type": tx_type,
        "amount": round(amount, 2),
        "note": note,
        "ref_id": ref_id,
        "time": datetime.now().isoformat(),
    })
    save_wallet(slug, wallet)
    return wallet


# ── 红包管理 ──


def get_redpackets_path(slug: str) -> Path:
    return get_ex_dir(slug) / "red_packets.json"


def load_redpackets(slug: str) -> list[dict]:
    p = get_redpackets_path(slug)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def save_redpackets(slug: str, packets: list[dict]) -> None:
    atomic_write_json(get_redpackets_path(slug), packets)


def create_redpacket(slug: str, trigger: str = "random_cute") -> Optional[dict]:
    """生成一个红包。返回红包数据或 None（触发条件不满足时）。"""
    packets = load_redpackets(slug)

    # 冷却：1 小时内最多 3 个红包
    one_hour_ago = datetime.now() - timedelta(hours=1)
    recent = [rp for rp in packets
              if datetime.fromisoformat(rp["created_at"]) > one_hour_ago]
    if len(recent) >= 3:
        return None

    cfg = RED_PACKET_TRIGGERS.get(trigger, RED_PACKET_TRIGGERS["random_cute"])
    lo, hi = cfg["amount_range"]
    amount = round(random.uniform(lo, hi), 2)
    note = random.choice(cfg["notes"])

    rp = {
        "id": f"rp_{len(packets)+1}_{int(datetime.now().timestamp())}",
        "type": "normal",
        "amount": amount,
        "note": note,
        "from": "ta",
        "status": "pending",
        "trigger": trigger,
        "created_at": datetime.now().isoformat(),
    }
    packets.append(rp)
    save_redpackets(slug, packets)
    return rp


def open_redpacket(slug: str, rp_id: str) -> Optional[dict]:
    """打开红包。返回红包数据，None 表示不存在或已开。"""
    packets = load_redpackets(slug)
    for rp in packets:
        if rp["id"] == rp_id:
            if rp["status"] != "pending":
                return None
            rp["status"] = "opened"
            save_redpackets(slug, packets)
            # 入账
            add_transaction(slug, "red_packet_received", rp["amount"],
                          note=rp["note"], ref_id=rp_id)
            return rp
    return None


def detect_redpacket_trigger(user_message: str, ai_reply: str) -> Optional[str]:
    """根据对话内容判断是否触发红包。返回 trigger 类型或 None。"""
    combined = (user_message + " " + ai_reply).lower()

    for trigger_name, cfg in RED_PACKET_TRIGGERS.items():
        for kw in cfg["keywords"]:
            if kw in combined:
                # 30% 概率触发（避免每次都发）
                if random.random() < 0.3:
                    return trigger_name

    return None


# ── 转账管理 ──


def get_transfers_path(slug: str) -> Path:
    return get_ex_dir(slug) / "transfers.json"


def load_transfers(slug: str) -> list[dict]:
    p = get_transfers_path(slug)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def save_transfers(slug: str, transfers: list[dict]) -> None:
    atomic_write_json(get_transfers_path(slug), transfers)


def create_transfer(slug: str, amount: float, note: str = "",
                    direction: str = "ta_to_me") -> dict:
    """创建转账。"""
    transfers = load_transfers(slug)
    tx = {
        "id": f"tx_{len(transfers)+1}_{int(datetime.now().timestamp())}",
        "amount": round(amount, 2),
        "note": note,
        "direction": direction,  # ta_to_me 或 me_to_ta
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
    }
    transfers.append(tx)
    save_transfers(slug, transfers)
    return tx


def confirm_transfer(slug: str, tx_id: str, action: str = "receive") -> Optional[dict]:
    """确认转账。action: receive / return"""
    transfers = load_transfers(slug)
    for tx in transfers:
        if tx["id"] == tx_id:
            if tx["status"] != "pending":
                return None
            if action == "receive":
                tx["status"] = "received"
                tx_type = "transfer_received" if tx["direction"] == "ta_to_me" else "transfer_sent"
            else:
                tx["status"] = "returned"
                tx_type = None

            save_transfers(slug, transfers)
            if tx_type:
                add_transaction(slug, tx_type, tx["amount"],
                              note=tx["note"], ref_id=tx_id)
            return tx
    return None
