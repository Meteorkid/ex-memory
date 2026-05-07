"""前任记忆智能体 — 主入口。"""

import sys
import shutil
from pathlib import Path

from config import EXES_DIR, get_ex_dir, get_collection_name


def print_banner():
    print()
    print("  ┌─────────────────────────────────────┐")
    print("  │     前任记忆智能体  ex-memory        │")
    print("  │  把一段记忆，变成可以对话的人        │")
    print("  └─────────────────────────────────────┘")
    print()
    print("  /create   创建新的记忆镜像")
    print("  /{名称}   进入已有镜像对话")
    print("  /list     列出所有镜像")
    print("  /help     帮助")
    print("  /exit     退出")
    print()


def cmd_create(_=""):
    """创建新的记忆镜像。"""
    from pipeline.orchestrator import run_create_flow
    run_create_flow()


def cmd_list(_=""):
    """列出所有已创建的镜像。"""
    if not EXES_DIR.exists():
        print("还没有创建任何镜像。输入 /create 开始。")
        return

    exes = [d for d in EXES_DIR.iterdir() if d.is_dir() and (d / "meta.json").exists()]
    if not exes:
        print("还没有创建任何镜像。输入 /create 开始。")
        return

    import json
    print("\n[已创建的镜像]")
    for ex_dir in sorted(exes):
        meta_path = ex_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            name = meta.get("name", ex_dir.name)
            state = meta.get("pipeline_state", "unknown")
            created = meta.get("created_at", "")[:10]
            print(f"  /{ex_dir.name:<15} {name}  ({state}, {created})")
        except Exception:
            print(f"  /{ex_dir.name:<15} (读取失败)")
    print()


def cmd_help(_=""):
    """显示帮助。"""
    print("""
[可用指令]

  /create          创建新的记忆镜像
  /{名称}          进入已有镜像的对话模式
  /list            列出所有镜像
  /update {名称}   向已有镜像追加新素材
  /reflect {名称}  关系反思分析
  /backup {名称}   备份镜像版本
  /rollback {名称} {版本}  回滚到指定版本
  /let-go {名称}   删除镜像（不可逆）
  /help            显示帮助
  /exit            退出
""")


def cmd_update(slug: str):
    """向已有镜像追加新素材。"""
    if not slug:
        print("用法: /update {镜像名称}")
        return

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        print(f"镜像 [{slug}] 不存在。")
        return

    print(f"\n=== 向 [{slug}] 追加新素材 ===")
    print("[A] 微信聊天记录")
    print("[B] 直接粘贴/口述")
    choice = input("选择 (A/B): ").strip().upper()

    if choice == "A":
        file_path = input("微信聊天记录文件路径: ").strip().strip('"').strip("'")
        if not file_path or not Path(file_path).exists():
            print("文件不存在，已取消。")
            return
        from parsers.wechat_parser import parse as wechat_parse
        from memory.chunker import Chunker
        from memory.vector_store import VectorStore
        from memory.embedder import Embedder
        from config import get_embedding_config

        name = slug.replace("_", " ")
        messages = wechat_parse(file_path, target_name=name)
        print(f"  解析完成：{len(messages)} 条消息")

        chunker = Chunker()
        chunks = chunker.chunk_messages(messages, source="wechat", chat_id=f"wechat_{slug}_update")

        emb_cfg = get_embedding_config()
        embedder = Embedder(api_key=emb_cfg["api_key"], base_url=emb_cfg["base_url"], model=emb_cfg["model"])
        chroma_dir = str(ex_dir / "chroma_db")
        vector_store = VectorStore(persist_dir=chroma_dir, collection_name=get_collection_name(slug))

        if chunks:
            vector_store.ingest(chunks, embedder)
            print(f"  入库完成：{vector_store.count()} 条记录")

        # 构建材料摘要用于增量合并
        sample_results = vector_store.search("日常对话", embedder, top_k=20)
        materials_summary = f"新增聊天记录 {len(messages)} 条\n\n"
        if sample_results:
            for r in sample_results[:10]:
                materials_summary += f"- {r.get('display_text', '')[:200]}\n"

    elif choice == "B":
        print("请粘贴内容（输入空行结束）：")
        lines = []
        while True:
            try:
                line = input()
                if line == "":
                    break
                lines.append(line)
            except EOFError:
                break
        text = "\n".join(lines)
        if not text.strip():
            print("未输入内容，已取消。")
            return

        materials_summary = text

        from memory.chunker import Chunker
        from memory.vector_store import VectorStore
        from memory.embedder import Embedder
        from config import get_embedding_config

        chunker = Chunker()
        chunks = chunker.chunk_text(text, source="oral_update")

        emb_cfg = get_embedding_config()
        embedder = Embedder(api_key=emb_cfg["api_key"], base_url=emb_cfg["base_url"], model=emb_cfg["model"])
        chroma_dir = str(ex_dir / "chroma_db")
        vector_store = VectorStore(persist_dir=chroma_dir, collection_name=get_collection_name(slug))

        if chunks:
            vector_store.ingest(chunks, embedder)
            print(f"  入库完成：{vector_store.count()} 条记录")
    else:
        print("已取消。")
        return

    # 增量合并
    print("正在增量合并...")
    from pipeline.merger import merge_new_material
    result = merge_new_material(slug, materials_summary, source_type="wechat" if choice == "A" else "oral")

    if result.get("error"):
        print(f"合并失败: {result['error']}")
    else:
        print("合并完成！memory.md、persona.md、SKILL.md 已更新。")


def cmd_reflect(slug: str):
    """关系反思分析。"""
    if not slug:
        print("用法: /reflect {镜像名称}")
        return

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        print(f"镜像 [{slug}] 不存在。")
        return

    from config import get_llm_config
    from openai import OpenAI
    from pathlib import Path

    cfg = get_llm_config()
    if not cfg["api_key"]:
        print("未配置 LLM API Key，无法执行反思分析。")
        return

    # 读取 memory.md
    memory_path = ex_dir / "memory.md"
    if not memory_path.exists():
        print("缺少 memory.md，请先完成创建流程。")
        return

    memory_content = memory_path.read_text(encoding="utf-8")
    reflect_prompt = (Path(__file__).resolve().parent / "prompts" / "reflect.md").read_text(encoding="utf-8")

    print("正在进行关系反思分析（可能需要 1-2 分钟）...")

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": reflect_prompt},
            {"role": "user", "content": f"请根据以下关系记忆进行反思分析：\n\n{memory_content}"},
        ],
        temperature=0.7,
    )
    reflection = response.choices[0].message.content

    # 保存到 reflections.md
    reflections_path = ex_dir / "reflections.md"
    reflections_path.write_text(reflection, encoding="utf-8")

    print(f"\n{reflection}")
    print(f"\n已保存到 {reflections_path}")


def cmd_backup(slug: str):
    """备份镜像版本。"""
    if not slug:
        print("用法: /backup {镜像名称}")
        return

    from core.version_manager import backup as do_backup
    try:
        version_name = do_backup(slug)
        print(f"备份成功！版本：{version_name}")
    except FileNotFoundError as e:
        print(f"备份失败: {e}")


def cmd_rollback(args: str):
    """回滚到指定版本。用法: /rollback {名称} {版本}"""
    parts = args.split()
    if len(parts) < 2:
        print("用法: /rollback {镜像名称} {版本号}")
        print("先用 /backup {名称} 查看版本列表。")
        return

    slug, version = parts[0], parts[1]

    from core.version_manager import rollback as do_rollback, list_versions
    try:
        do_rollback(slug, version)
        print(f"已回滚 [{slug}] 到版本 {version}")
    except FileNotFoundError as e:
        print(f"回滚失败: {e}")
        versions = list_versions(slug)
        if versions:
            print("可用版本：")
            for v in versions:
                print(v)


def cmd_let_go(slug: str):
    """删除镜像（不可逆）。"""
    if not slug:
        print("用法: /let-go {镜像名称}")
        return

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        print(f"镜像 [{slug}] 不存在。")
        return

    confirm = input(f"确认删除 [{slug}]？这是不可逆操作。(输入 yes 确认): ").strip()
    if confirm != "yes":
        print("已取消。")
        return

    shutil.rmtree(ex_dir)
    print(f"\n镜像 [{slug}] 已删除。")
    print("那些记忆，终将化为风中的碎片。\n")


def main():
    """主入口。"""
    print_banner()

    if len(sys.argv) > 1:
        # 支持命令行参数：python run.py /create 或 python run.py /{slug}
        arg = sys.argv[1]
        if arg == "/create":
            cmd_create()
        elif arg == "/list":
            cmd_list()
        elif arg == "/help":
            cmd_help()
        elif arg.startswith("/"):
            # 直接进入对话
            _start_chat(arg[1:])
        return

    # 交互模式
    while True:
        try:
            user_input = input("\n> ").strip()
            if not user_input:
                continue

            if user_input.startswith("/"):
                parts = user_input[1:].split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""

                if cmd == "create":
                    cmd_create()
                elif cmd == "list":
                    cmd_list()
                elif cmd == "help":
                    cmd_help()
                elif cmd == "update":
                    cmd_update(arg)
                elif cmd == "reflect":
                    cmd_reflect(arg)
                elif cmd == "backup":
                    cmd_backup(arg)
                elif cmd == "rollback":
                    cmd_rollback(arg)
                elif cmd == "let-go":
                    cmd_let_go(arg)
                elif cmd in ("exit", "quit"):
                    print("再见。")
                    break
                else:
                    # 尝试作为镜像名称进入对话
                    _start_chat(cmd)
            else:
                print("输入 /help 查看可用指令。")

        except KeyboardInterrupt:
            print("\n再见。")
            break
        except EOFError:
            print("\n再见。")
            break


def _start_chat(slug: str):
    """进入指定镜像的对话模式。"""
    from core.session import ChatSession
    from pipeline.correction_handler import detect_correction, handle_correction

    session = ChatSession()
    session.slug = slug

    # 检查镜像是否存在
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        print(f"镜像 [{slug}] 不存在。输入 /create 创建。")
        return

    # 初始化引擎
    from core.engine import ChatEngine
    from memory.vector_store import VectorStore
    from memory.embedder import Embedder
    from config import get_embedding_config

    embedder = None
    vector_store = None
    chroma_dir = ex_dir / "chroma_db"
    if chroma_dir.exists() and any(chroma_dir.iterdir()):
        try:
            emb_cfg = get_embedding_config()
            embedder = Embedder(
                api_key=emb_cfg["api_key"],
                base_url=emb_cfg["base_url"],
                model=emb_cfg["model"],
            )
            vector_store = VectorStore(
                persist_dir=str(chroma_dir),
                collection_name=get_collection_name(slug),
            )
            print(f"--- 向量库已加载 ({vector_store.count()} 条记录) ---")
        except Exception as e:
            print(f"--- 向量库加载失败: {e}，将以纯文本模式运行 ---")

    from core.engine import ChatEngine
    session.engine = ChatEngine(slug=slug, vector_store=vector_store, embedder=embedder)

    # 注册命令
    session.register_command("backup", lambda _: cmd_backup(slug), "备份当前镜像版本")
    session.register_command("reflect", lambda _: cmd_reflect(slug), "关系反思分析")

    # 覆写 _chat 以集成纠正检测
    original_chat = session._chat

    def _chat_with_correction(user_msg: str):
        """带纠正检测的聊天处理。"""
        # 先正常聊天
        try:
            reply, usage = session.engine.chat(user_msg, session.history)
            print(f"\n{session.slug}: {reply}")

            session.counter.update(usage)
            session.history.append({"role": "user", "content": user_msg})
            session.history.append({"role": "assistant", "content": reply})
            session.turn_count += 1

            # 检测纠正
            if detect_correction(user_msg):
                print("\n（检测到纠正意图，正在处理...）")
                result = handle_correction(
                    slug=slug,
                    user_msg=user_msg,
                    last_reply=reply,
                    history=session.history,
                )
                print(result)

                # 重新加载引擎以应用纠正
                session.engine._load()

            # 维持对话长度
            if len(session.history) > session.talk_length * 2:
                session.history = session.history[-(session.talk_length * 2):]

            # 归档触发
            from config import ARCHIVE_THRESHOLD
            if session.turn_count >= ARCHIVE_THRESHOLD:
                session._maybe_archive()
                session.turn_count = 0

        except Exception as e:
            print(f"\n[错误]: {e}")

    session._chat = _chat_with_correction

    print(f"\n--- 进入 [{slug}] 的对话模式 (输入 /help 查看指令) ---\n")

    # 手动运行对话循环
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
            print(f"运行异常: {e}")
            break

    session.counter.display_summary()


if __name__ == "__main__":
    main()
