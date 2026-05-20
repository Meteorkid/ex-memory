"""Gradio Web 前端：前任记忆智能体的全功能 Web 界面。"""

import sys
import json
from pathlib import Path

import gradio as gr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import EXES_DIR, init_app, get_llm_config
from core.validation import validate_slug, validate_user_input
from core.factory import create_engine_and_store
from core.sticker_selector import STICKERS
from core.token_counter import TokenCounter


# --- 状态管理 ---

class AppState:
    def __init__(self):
        self.current_slug: str = ""
        self.current_name: str = ""
        self.history: list[dict] = []
        self.counter = TokenCounter()
        self.engine = None


# --- 镜像列表 ---

def list_exes() -> list[list]:
    """获取所有镜像列表，用于 Gradio DataFrame。"""
    rows = []
    if not EXES_DIR.exists():
        return rows
    for d in sorted(EXES_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            rows.append([
                meta.get("name", d.name),
                d.name,
                meta.get("pipeline_state", "unknown"),
                meta.get("created_at", "")[:10],
            ])
        except Exception:
            pass
    return rows


def refresh_exe_list():
    rows = list_exes()
    return gr.Dataframe(
        value=rows,
        headers=["名称", "Slug", "状态", "创建日期"],
        interactive=False,
    )


# --- 创建向导 ---

def create_exe(name: str, basic_info: str, personality: str, progress=gr.Progress()):
    if not name.strip():
        return "[错误] 代号不能为空"

    slug = name.strip().lower().replace(" ", "_")
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        return f"[错误] {e}"

    from config import get_ex_dir
    ex_dir = get_ex_dir(slug)
    if ex_dir.exists():
        return f"[错误] 镜像 [{slug}] 已存在"

    progress(0.1, desc="创建目录...")
    from pipeline.orchestrator import run_create_flow_api

    progress(0.3, desc="生成记忆镜像...")
    result = run_create_flow_api(slug=slug, name=name, answers=[basic_info, personality])

    if result.get("error"):
        return f"[错误] {result['error']}"

    progress(1.0, desc="完成")
    return f"[成功] 镜像 [{name}] 创建完成！在「对话」标签页选择 /{slug} 开始对话。"


# --- 对话引擎 ---

def load_exe(slug: str, session: AppState):
    """加载镜像引擎，返回欢迎消息。"""
    if not slug.strip():
        return [], "", "请先选择镜像", session
    try:
        slug = validate_slug(slug.strip())
    except ValueError as e:
        return [], "", f"错误: {e}", session

    from config import get_ex_dir
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        return [], "", f"镜像 [{slug}] 不存在", session

    session.current_slug = slug
    session.current_name = slug
    session.history = []
    session.counter = TokenCounter()

    try:
        engine, vs, _ = create_engine_and_store(slug)
        session.engine = engine
        msg = f"已进入 [{slug}] 的对话模式"
        if vs:
            msg += f" (向量库 {vs.count()} 条记录)"
        return [], msg, _token_status(session), session
    except Exception as e:
        return [], "", f"加载失败: {e}", session


def _token_status(session: AppState) -> str:
    """生成 token 消耗状态文本。"""
    c = session.counter
    if c.prompt_tokens == 0 and c.completion_tokens == 0:
        return "Token: 暂无消耗"
    return (f"Token: {c.prompt_tokens + c.completion_tokens} "
            f"(提示 {c.prompt_tokens} + 生成 {c.completion_tokens}) | "
            f"轮次: {c.turns}")


def _sticker_emoji(sticker_ids: list[str]) -> str:
    """将贴纸 ID 列表转为 emoji 字符串，用于在 chatbot 中显示。"""
    if not sticker_ids:
        return ""
    parts = []
    for sid in sticker_ids:
        info = STICKERS.get(sid)
        if info:
            parts.append(info["emoji"])
    return " ".join(parts)


def chat(message: str, history: list[list], session: AppState):
    """处理一轮对话（非流式，含贴纸和 token 统计）。"""
    if not session.current_slug or session.engine is None:
        history.append([message, "请先在左侧选择一个镜像开始对话。"])
        return "", history, _token_status(session), session

    try:
        message = validate_user_input(message)
    except ValueError as e:
        history.append([message, f"[输入错误] {e}"])
        return "", history, _token_status(session), session

    parsed_history = []
    for h in history:
        parsed_history.append({"role": "user", "content": h[0]})
        if h[1]:
            parsed_history.append({"role": "assistant", "content": h[1]})

    try:
        reply, stickers, usage = session.engine.chat(message, parsed_history[-100:])

        sticker_text = _sticker_emoji(stickers)
        display_reply = (reply + "\n\n" + sticker_text).strip() if sticker_text else reply

        session.counter.update(usage)
        history.append([message, display_reply])

        from pipeline.correction_handler import detect_correction, handle_correction
        if detect_correction(message):
            result = handle_correction(
                slug=session.current_slug,
                user_msg=message,
                last_reply=reply,
                history=parsed_history,
            )
            history.append(["", f"({result})"])

    except Exception as e:
        history.append([message, f"[错误] {e}"])

    return "", history, _token_status(session), session


def chat_stream(message: str, history: list[list], session: AppState):
    """处理一轮对话（流式，含贴纸/红包/转账）。"""
    if not session.current_slug or session.engine is None:
        history.append([message, "请先在左侧选择一个镜像开始对话。"])
        yield history, _token_status(session), session
        return

    try:
        message = validate_user_input(message)
    except ValueError as e:
        history.append([message, f"[输入错误] {e}"])
        yield history, _token_status(session), session
        return

    parsed_history = []
    for h in history:
        parsed_history.append({"role": "user", "content": h[0]})
        if h[1]:
            parsed_history.append({"role": "assistant", "content": h[1]})

    try:
        full_reply = ""
        sticker_ids = []
        special_msgs = []

        history.append([message, ""])
        last_idx = len(history) - 1

        for chunk in session.engine.chat_stream(message, parsed_history[-100:]):
            if chunk["type"] == "text":
                full_reply += chunk["content"]
                history[last_idx][1] = full_reply
                yield history, _token_status(session), session
            elif chunk["type"] == "sticker":
                sticker_ids.append(chunk["id"])
                emoji = _sticker_emoji([chunk["id"]])
                if emoji:
                    history[last_idx][1] = full_reply + ("\n\n" + emoji if emoji else "")
                    yield history, _token_status(session), session
            elif chunk["type"] == "red_packet":
                special_msgs.append(f"🧧 红包: {chunk['note']} (¥{chunk['amount']:.2f})")
            elif chunk["type"] == "transfer":
                special_msgs.append(f"💸 转账: ¥{chunk['amount']:.2f}")

        if special_msgs:
            history[last_idx][1] += "\n\n" + "\n".join(special_msgs)

        from core.validation import estimate_tokens
        session.counter.prompt_tokens += estimate_tokens(message)
        session.counter.completion_tokens += estimate_tokens(full_reply)
        session.counter.turns += 1

        yield history, _token_status(session), session

    except Exception as e:
        history.append([message, f"[错误] {e}"])
        yield history, _token_status(session), session


def _dispatch_chat(message: str, history: list[list], use_stream: bool, session: AppState):
    """根据流式开关分发到 chat 或 chat_stream。"""
    if use_stream:
        return chat_stream(message, history, session)
    else:
        return chat(message, history, session)


# --- 镜像管理 ---

def delete_exe(slug: str):
    if not slug.strip():
        return "请输入镜像名称"
    try:
        slug = validate_slug(slug.strip())
    except ValueError as e:
        return f"[错误] {e}"
    import shutil
    from config import get_ex_dir
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        return f"镜像 [{slug}] 不存在"
    shutil.rmtree(ex_dir)
    return f"镜像 [{slug}] 已删除"


def backup_exe(slug: str):
    if not slug.strip():
        return "请输入镜像名称"
    try:
        slug = validate_slug(slug.strip())
    except ValueError as e:
        return f"[错误] {e}"
    from core.version_manager import backup
    version = backup(slug)
    return f"备份成功: {version}"


# --- UI 构建 ---

def build_ui():
    init_app()

    with gr.Blocks(
        title="ex-memory — 前任记忆智能体",
        theme=gr.themes.Soft(),
        css="""
        .main-header { text-align: center; margin-bottom: 20px; }
        .main-header h1 { font-size: 2em; margin-bottom: 0; }
        .main-header p { color: #666; font-size: 0.9em; }
        .warning-text { color: #e74c3c; font-size: 0.85em; }
        """
    ) as app:

        session_state = gr.State(AppState)

        gr.HTML("""
        <div class="main-header">
            <h1>前任记忆智能体</h1>
            <p>把一段记忆，变成可以对话的人</p>
        </div>
        """)

        with gr.Tabs():
            # ---- 创建镜像 ----
            with gr.Tab("创建镜像"):
                gr.Markdown("### 创建新的记忆镜像")

                with gr.Row():
                    with gr.Column(scale=1):
                        create_name = gr.Textbox(
                            label="代号",
                            placeholder="ta的昵称或别名",
                        )
                        create_basic = gr.Textbox(
                            label="基本信息",
                            placeholder="在一起多久、分手多久、ta的职业等",
                            lines=3,
                        )
                        create_personality = gr.Textbox(
                            label="性格画像",
                            placeholder="MBTI、星座、性格特点等",
                            lines=3,
                        )
                        create_btn = gr.Button("创建镜像", variant="primary")
                        create_output = gr.Textbox(label="结果", lines=4)

                    with gr.Column(scale=1):
                        gr.Markdown("""
                        ### 说明
                        填写基本信息后点击创建，AI 会自动生成：
                        - **memory.md** — 关系记忆
                        - **persona.md** — 五层人格画像
                        - **SKILL.md** — 对话系统指令

                        创建完成后在「对话」标签页开始聊天。
                        """)

                create_btn.click(
                    create_exe,
                    inputs=[create_name, create_basic, create_personality],
                    outputs=[create_output],
                )

            # ---- 对话 ----
            with gr.Tab("对话"):
                with gr.Row():
                    with gr.Column(scale=1):
                        chat_slug = gr.Textbox(
                            label="镜像名称",
                            placeholder="输入镜像 slug（如 xiaoming）",
                        )
                        load_btn = gr.Button("进入对话", variant="primary")

                    with gr.Column(scale=2):
                        chat_status = gr.Textbox(
                            label="状态",
                            value="请先选择镜像",
                            interactive=False,
                        )
                        token_status = gr.Textbox(
                            label="Token 消耗",
                            value="Token: 暂无消耗",
                            interactive=False,
                            elem_id="token-status",
                        )

                chatbot = gr.Chatbot(
                    label="对话",
                    height=500,
                    placeholder="开始你们的对话...",
                )

                with gr.Row():
                    msg_input = gr.Textbox(
                        label="",
                        placeholder="输入消息...",
                        scale=7,
                    )
                    stream_toggle = gr.Checkbox(
                        label="流式", value=True,
                        scale=1, min_width=60,
                    )
                    send_btn = gr.Button("发送", variant="primary", scale=1)

                load_btn.click(
                    load_exe,
                    inputs=[chat_slug, session_state],
                    outputs=[chatbot, chat_status, token_status, session_state],
                )

                send_btn.click(
                    _dispatch_chat,
                    inputs=[msg_input, chatbot, stream_toggle, session_state],
                    outputs=[msg_input, chatbot, token_status, session_state],
                )
                msg_input.submit(
                    _dispatch_chat,
                    inputs=[msg_input, chatbot, stream_toggle, session_state],
                    outputs=[msg_input, chatbot, token_status, session_state],
                )

                # 加载镜像时更新状态
                load_btn.click(
                    lambda s: s,
                    inputs=[chat_slug],
                    outputs=[chat_status],
                )

            # ---- 管理 ----
            with gr.Tab("管理"):
                gr.Markdown("### 镜像管理")

                exe_list = gr.Dataframe(
                    value=list_exes(),
                    headers=["名称", "Slug", "状态", "创建日期"],
                    interactive=False,
                    label="已创建的镜像",
                )
                refresh_btn = gr.Button("刷新列表")

                with gr.Row():
                    with gr.Column():
                        manage_slug = gr.Textbox(
                            label="镜像 Slug",
                            placeholder="输入要操作的镜像名称",
                        )
                    with gr.Column():
                        manage_backup_btn = gr.Button("备份")
                        manage_delete_btn = gr.Button("删除（不可逆）", variant="stop")

                manage_output = gr.Textbox(label="结果", lines=3)

                refresh_btn.click(
                    lambda: gr.Dataframe(value=list_exes(), headers=["名称", "Slug", "状态", "创建日期"], interactive=False),
                    outputs=[exe_list],
                )

                manage_backup_btn.click(
                    backup_exe,
                    inputs=[manage_slug],
                    outputs=[manage_output],
                )
                manage_delete_btn.click(
                    delete_exe,
                    inputs=[manage_slug],
                    outputs=[manage_output],
                )

            # ---- 设置 ----
            with gr.Tab("设置"):
                gr.Markdown("### API 配置")

                llm_cfg = get_llm_config()
                gr.Textbox(label="LLM Model", value=llm_cfg["model"], interactive=False)
                gr.Textbox(label="LLM Base URL", value=llm_cfg["base_url"], interactive=False)
                gr.Textbox(label="LLM API Key", value="***" if llm_cfg["api_key"] else "未配置", interactive=False)

                gr.Markdown("""
                ### 密钥管理
                API Key 存储在 `.env` 文件或 macOS Keychain 中。
                修改 `.env` 后重启生效。
                使用 `/keychain set llm {key}` 将密钥存入 Keychain。
                """)

        return app


def run_web(host: str = "0.0.0.0", port: int = 7860):
    """启动 Gradio Web 界面。"""
    app = build_ui()
    app.queue()  # 启用队列以支持 streaming
    app.launch(
        server_name=host,
        server_port=port,
        share=False,
        inbrowser=True,
    )


if __name__ == "__main__":
    run_web()
