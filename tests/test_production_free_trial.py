"""生产免费模式不能在默认体验轮数之后因余额阻止使用。"""


def test_zero_price_preserves_balance_after_twenty_turns(tmp_path, monkeypatch):
    import server.auth as auth
    from core.billing import ALLOW, balance_status, ensure_account, reserve_turn, settle_turn

    monkeypatch.setattr(auth, "DB_DIR", tmp_path)
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr("config.TURN_PRICE_MICROS", 0)
    auth.init_db()
    with auth._get_conn() as conn:
        user_id = conn.insert_returning_id(
            "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
            ("synthetic-free-trial", "not-a-password", "synthetic"),
        )
        conn.commit()
    account_id = ensure_account(user_id)
    before = balance_status(account_id)["balance_micros"]
    for _ in range(20):
        assert reserve_turn(account_id) == ALLOW
        settle_turn(account_id, user_id=user_id, prompt_tokens=10, completion_tokens=2)
    assert balance_status(account_id)["balance_micros"] == before
    with auth._get_conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n, sum(amount_micros) AS total FROM ledger WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    assert row["n"] == 20 and row["total"] == 0
