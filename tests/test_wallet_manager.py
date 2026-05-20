"""钱包管理并发安全测试。"""

import json
from concurrent.futures import ThreadPoolExecutor


def test_open_redpacket_is_single_settlement(tmp_path, monkeypatch):
    from core.wallet_manager import open_redpacket

    ex_dir = tmp_path / "exes" / "owned"
    ex_dir.mkdir(parents=True)
    monkeypatch.setattr("core.wallet_manager.get_ex_dir", lambda s: ex_dir)

    packet = {
        "id": "rp_1",
        "type": "normal",
        "amount": 8.88,
        "note": "test",
        "from": "ta",
        "status": "pending",
        "trigger": "test",
        "created_at": "2026-05-19T12:00:00",
    }
    (ex_dir / "red_packets.json").write_text(json.dumps([packet]), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: open_redpacket("owned", "rp_1"), range(8)))

    settled = [r for r in results if r is not None]
    wallet = json.loads((ex_dir / "wallet.json").read_text(encoding="utf-8"))
    packets = json.loads((ex_dir / "red_packets.json").read_text(encoding="utf-8"))

    assert len(settled) == 1
    assert wallet["balance"] == 8.88
    assert len(wallet["transactions"]) == 1
    assert packets[0]["status"] == "opened"
