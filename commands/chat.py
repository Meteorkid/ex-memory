"""/{slug} — 进入指定镜像的对话模式。"""
import logging
from config import get_ex_dir, ARCHIVE_THRESHOLD
from core.validation import validate_slug, validate_user_input
from commands import register

logger = logging.getLogger("ex-memory")


def cmd_chat(slug: str):
    """进入指定镜像的对话模式（ChatSession + 纠正检测）。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        print(f"错误: {e}")
        return

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        print(f"镜像 [{slug}] 不存在。输入 /create 创建。")
        return

    from core.session import ChatSession
    from core.factory import create_engine_and_store
    from pipeline.correction_handler import detect_correction, handle_correction

    session = ChatSession()
    session.slug = slug

    session.engine, _, _ = create_engine_and_store(slug)

    session.register_command("backup", lambda _: _do_backup(slug), "备份当前镜像版本")
    session.register_command("reflect", lambda _: _do_reflect(slug), "关系反思分析")

    original_chat = session._chat

    def _chat_with_correction(user_msg: str):
        try:
            user_msg = validate_user_input(user_msg)
        except ValueError as e:
            print(f"\n[输入错误]: {e}")
            return

        try:
            reply, usage = session.engine.chat(user_msg, session.history)
            print(f"\n{session.slug}: {reply}")

            session.counter.update(usage)
            session.history.append({"role": "user", "content": user_msg})
            session.history.append({"role": "assistant", "content": reply})
            session.turn_count += 1

            if detect_correction(user_msg):
                print("\n（检测到纠正意图，正在处理...）")
                result = handle_correction(
                    slug=slug, user_msg=user_msg, last_reply=reply, history=session.history
                )
                print(result)
                session.engine._load()

            if len(session.history) > session.talk_length * 2:
                session.history = session.history[-(session.talk_length * 2):]

            if session.turn_count >= ARCHIVE_THRESHOLD:
                session._maybe_archive()
                session.turn_count = 0

        except Exception as e:
            logger.error("对话出错: %s", e, exc_info=True)
            print(f"\n[错误]: {e}")

    session._chat = _chat_with_correction

    print(f"\n--- 进入 [{slug}] 的对话模式 (输入 /help 查看指令) ---\n")

    from prompt_toolkit import prompt as pt_prompt
    session.running = True

    while session.running:
        try:
            user_input = pt_prompt("\n我: ").strip()
            if not user_input:
                continue
            if user_input.startswith("/"):
                session._process_command(user_input)
            else:
                session._chat(user_input)
        except KeyboardInterrupt:
            session.do_exit()
        except EOFError:
            session.do_exit()
        except Exception as e:
            logger.error("运行异常: %s", e, exc_info=True)
            print(f"运行异常: {e}")
            break

    session.counter.display_summary()


def _do_backup(slug: str):
    from commands.backup import cmd_backup
    cmd_backup(slug)


def _do_reflect(slug: str):
    from commands.reflect import cmd_reflect
    cmd_reflect(slug)


# 不注册到 COMMANDS —— chat 由 run.py 作为兜底分发
