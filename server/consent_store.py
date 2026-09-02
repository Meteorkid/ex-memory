"""同意留痕与数据主体请求。

同意必须版本化：出事时要能举证「这个用户在什么时候同意了哪一版协议」。
只记「同意过」而不记版本，等于没有留痕。
"""

import logging
from typing import Optional

logger = logging.getLogger("ex-memory")

# 协议类型。third_party_data 是本产品特有的那一条：
# 用户上传的是自己与他人的聊天记录，处理的是第三方的个人信息，
# 必须单独勾选，不能与总协议捆绑。
POLICY_TERMS = "terms"
POLICY_PRIVACY = "privacy"
POLICY_THIRD_PARTY_DATA = "third_party_data"
POLICY_EMOTION_ANALYSIS = "emotion_analysis"

KNOWN_POLICIES = {
    POLICY_TERMS,
    POLICY_PRIVACY,
    POLICY_THIRD_PARTY_DATA,
    POLICY_EMOTION_ANALYSIS,
}


def record_consent(
    user_id: int,
    policy_type: str,
    policy_version: str,
    *,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """记录一次同意。同一用户可对同一协议的不同版本多次同意，全部保留。"""
    if policy_type not in KNOWN_POLICIES:
        raise ValueError(f"未知协议类型: {policy_type}")
    if not policy_version.strip():
        raise ValueError("协议版本不能为空")

    from server.auth import _get_conn

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO consents (user_id, policy_type, policy_version, ip, user_agent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, policy_type, policy_version.strip(), ip, user_agent),
        )
        conn.commit()


def list_consents(user_id: int) -> list[dict]:
    """该用户的完整同意历史，供数据主体查询与举证。"""
    from server.auth import _get_conn

    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT policy_type, policy_version, granted_at, ip, user_agent
            FROM consents WHERE user_id = ? ORDER BY granted_at
            """,
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def has_consented(user_id: int, policy_type: str, policy_version: str) -> bool:
    """是否同意过指定版本。版本不匹配即视为未同意——协议改版要重新征得同意。"""
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM consents
            WHERE user_id = ? AND policy_type = ? AND policy_version = ?
            LIMIT 1
            """,
            (user_id, policy_type, policy_version),
        ).fetchone()
        return row is not None


# ── 数据主体请求（被模拟者投诉、逝者近亲属主张）──

CLAIM_SUBJECT = "subject_complaint"
CLAIM_DECEASED_KIN = "deceased_kin"
CLAIM_OTHER = "other"
KNOWN_CLAIMS = {CLAIM_SUBJECT, CLAIM_DECEASED_KIN, CLAIM_OTHER}


def create_subject_request(
    claim_type: str,
    contact: str,
    *,
    target_slug: Optional[str] = None,
    target_hint: Optional[str] = None,
    detail: Optional[str] = None,
    identity_evidence: Optional[str] = None,
) -> int:
    """受理一条数据主体请求。

    提交方通常没有本站账号——被模拟者从来不是用户，逝者近亲属也多半不是。
    所以这个入口必须免登录，代价是要有独立限流，否则就是个开放的滥用入口。
    """
    if claim_type not in KNOWN_CLAIMS:
        raise ValueError(f"未知的主张类型: {claim_type}")
    if not contact.strip():
        raise ValueError("联系方式不能为空")

    from server.auth import _get_conn

    with _get_conn() as conn:
        request_id = conn.insert_returning_id(
            """
            INSERT INTO subject_requests (
                claim_type, contact, target_slug, target_hint, detail, identity_evidence
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                claim_type,
                contact.strip(),
                target_slug,
                target_hint,
                detail,
                identity_evidence,
            ),
        )
        conn.commit()
        return int(request_id)


def list_subject_requests(status: str = "received", limit: int = 100) -> list[dict]:
    from server.auth import _get_conn

    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, claim_type, contact, target_slug, target_hint,
                   status, created_at
            FROM subject_requests WHERE status = ?
            ORDER BY created_at LIMIT ?
            """,
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def resolve_subject_request(
    request_id: int, handled_by: str, status: str, note: str = ""
) -> bool:
    """处置一条数据主体请求。status: verifying / actioned / rejected。"""
    if status not in ("verifying", "actioned", "rejected"):
        raise ValueError("状态只能是 verifying / actioned / rejected")
    from server.auth import _get_conn

    with _get_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE subject_requests
            SET status = ?, handled_by = ?, resolution_note = ?,
                resolved_at = CASE WHEN ? IN ('actioned', 'rejected')
                                   THEN datetime('now') ELSE resolved_at END
            WHERE id = ?
            """,
            (status, handled_by, note, status, request_id),
        )
        conn.commit()
        return cursor.rowcount > 0
