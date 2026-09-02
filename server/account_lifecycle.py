"""账户级数据导出与级联删除（FR-019）。

一个账号的数据散落在五处，少删一处就是一次合规事故：
1. data/users.db —— users / tokens / external_identities / consents / user_activity
2. exes/<user_id>/ —— 全部镜像，含 SKILL.md、向量库、对话归档、版本快照
3. 自定义贴纸目录 —— web/static/stickers/custom/u<user_id>/
4. data/feedback.jsonl —— 按行携带 user_id
5. 进程内缓存 —— 引擎实例与用量计数器

verify_deletion 把这五处逐一回查，删除后必须返回空列表。这个函数进 CI，
是「删除完整性不因架构升级而倒退」（PRD NFR-034）的看门人。
"""

import json
import logging
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("ex-memory")


def _sticker_dir(user_id: int) -> Path:
    from core.sticker_manager import CUSTOM_BASE

    return CUSTOM_BASE / f"u{user_id}"


def _feedback_path() -> Path:
    import config

    return config.PROJECT_DIR / "data" / "feedback.jsonl"


def _user_exes_dir(user_id: int) -> Path:
    import config

    return config.EXES_DIR / str(user_id)


def _owned_slugs(user_id: int) -> list[str]:
    import config

    return [
        slug
        for slug, owner, _ in config.iter_exe_dirs(require_meta=False)
        if owner == user_id
    ]


# ── 导出 ──


def export_account(user_id: int) -> Path:
    """把该账号的全部个人信息打包为 zip，返回临时文件路径。

    导出的是「个人信息」而非「数据库备份」：不含密码哈希与盐。
    """
    from server.auth import _get_conn
    from server.consent_store import list_consents

    tmp = tempfile.NamedTemporaryFile(
        prefix=f"ex-memory-account-{user_id}-", suffix=".zip", delete=False
    )
    zip_path = Path(tmp.name)
    tmp.close()

    try:
        with _get_conn() as conn:
            user_row = conn.execute(
                "SELECT id, username, role, created_at, phone_verified_at,"
                " age_confirmed_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            activity = [
                dict(r)
                for r in conn.execute(
                    "SELECT activity_date, active_seconds FROM user_activity"
                    " WHERE user_id = ? ORDER BY activity_date",
                    (user_id,),
                ).fetchall()
            ]
        if user_row is None:
            raise LookupError(f"账号 {user_id} 不存在")

        manifest = {
            "format": "ex-memory-account-export-v1",
            # FR-021：导出内容须带 AI 生成声明
            "ai_generated_notice": (
                "本导出包中的镜像人格、对话回复等内容由 AI 生成，"
                "不代表被模拟者本人的真实言论或意愿。"
            ),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "account": dict(user_row),
            "consents": list_consents(user_id),
            "activity": activity,
            "exes": _owned_slugs(user_id),
        }

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "account.json", json.dumps(manifest, ensure_ascii=False, indent=2)
            )
            exes_root = _user_exes_dir(user_id)
            if exes_root.exists():
                for path in sorted(exes_root.rglob("*")):
                    if path.is_dir() or path.is_symlink():
                        continue
                    if path.name == ".DS_Store" or path.suffix in (".lock", ".tmp"):
                        continue
                    zf.write(
                        path, (Path("exes") / path.relative_to(exes_root)).as_posix()
                    )
            feedback = _collect_feedback(user_id)
            if feedback:
                zf.writestr(
                    "feedback.jsonl",
                    "\n".join(json.dumps(e, ensure_ascii=False) for e in feedback),
                )
        return zip_path
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise


def _collect_feedback(user_id: int) -> list[dict]:
    path = _feedback_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("user_id") == user_id:
            out.append(entry)
    return out


# ── 删除 ──


def delete_account(user_id: int) -> dict:
    """级联删除账号的全部个人信息，返回删除回执。

    safety_events 的处置由 config.SAFETY_EVENT_DELETION_MODE 决定：
    anonymize（默认）抹掉关联只留类别与时间，delete 整行删除。
    这是法务裁量点，两条路都已实现并各有测试，改配置即可切换。
    """
    from server.auth import _get_conn

    receipt = {
        "user_id": user_id,
        "deleted_at": datetime.now(timezone.utc).isoformat(),
        "exes": _owned_slugs(user_id),
    }

    exes_root = _user_exes_dir(user_id)
    if exes_root.exists():
        shutil.rmtree(exes_root)

    sticker_dir = _sticker_dir(user_id)
    if sticker_dir.exists():
        shutil.rmtree(sticker_dir)

    receipt["feedback_removed"] = _purge_feedback(user_id)

    with _get_conn() as conn:
        receipt["safety_events"] = _handle_safety_events(conn, user_id)
        for table in ("tokens", "consents", "user_activity", "external_identities"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()

    _purge_memory_caches(user_id)
    logger.info("账号已删除 user_id=%s exes=%s", user_id, receipt["exes"])
    return receipt


def _handle_safety_events(conn, user_id: int) -> str:
    """按配置处置安全事件，返回实际采取的方式。

    anonymize 抹掉的是全部可关联字段（user_id / slug / 片段 / 输入哈希），
    只留类别、严重度与时间——剩下的部分不再指向任何自然人。
    delete 则整行清除，不留任何痕迹。
    """
    import config

    if config.SAFETY_EVENT_DELETION_MODE == "delete":
        conn.execute("DELETE FROM safety_events WHERE user_id = ?", (user_id,))
        return "deleted"

    conn.execute(
        """
        UPDATE safety_events
        SET user_id = NULL, excerpt = NULL, input_hash = NULL, slug = NULL
        WHERE user_id = ?
        """,
        (user_id,),
    )
    return "anonymized"


def _purge_feedback(user_id: int) -> int:
    path = _feedback_path()
    if not path.exists():
        return 0
    from core.file_utils import atomic_write

    kept, removed = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            kept.append(line)  # 解析不了的行保守保留
            continue
        if entry.get("user_id") == user_id:
            removed += 1
        else:
            kept.append(line)
    atomic_write(path, "\n".join(kept) + ("\n" if kept else ""))
    return removed


def _purge_memory_caches(user_id: int) -> None:
    """清掉进程内缓存，否则删号后引擎实例还握着人格文本。"""
    import server.routes as routes

    routes._engine_cache.evict_where(lambda key: key[0] == user_id)
    routes._session_counters.evict_where(lambda key: key[0] == user_id)


# ── 完整性回查 ──


def verify_deletion(user_id: int) -> list[str]:
    """回查五处存储，返回残留描述列表。删干净时返回空列表。

    这是 NFR-034 的看门人：数据平面迁移到 Postgres / 对象存储 / pgvector
    之后，如果删除路径没同步改造，这里会立刻变红。
    """
    from server.auth import _get_conn

    residues: list[str] = []

    exes_root = _user_exes_dir(user_id)
    if exes_root.exists():
        residues.append(f"镜像目录残留: {exes_root}")

    sticker_dir = _sticker_dir(user_id)
    if sticker_dir.exists():
        residues.append(f"自定义贴纸残留: {sticker_dir}")

    if _collect_feedback(user_id):
        residues.append("反馈记录残留: data/feedback.jsonl")

    with _get_conn() as conn:
        for table, column in (
            ("users", "id"),
            ("tokens", "user_id"),
            ("consents", "user_id"),
            ("user_activity", "user_id"),
            ("external_identities", "user_id"),
        ):
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?",
                (user_id,),
            ).fetchone()
            if row["n"]:
                residues.append(f"{table} 残留 {row['n']} 行")
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM safety_events WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row["n"]:
            residues.append(f"safety_events 仍关联该账号 {row['n']} 行")

    import server.routes as routes

    if any(k[0] == user_id for k in routes._engine_cache):
        residues.append("进程内引擎缓存残留")
    if any(k[0] == user_id for k in routes._session_counters):
        residues.append("进程内用量计数残留")

    return residues
