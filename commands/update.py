"""/update — 向已有镜像追加新素材。"""
from pathlib import Path
from config import get_ex_dir, get_embedding_config, get_collection_name
from core.validation import validate_slug
from commands import register


def cmd_update(slug: str):
    if not slug:
        print("用法: /update {镜像名称}")
        return

    try:
        slug = validate_slug(slug)
    except ValueError as e:
        print(f"错误: {e}")
        return

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        print(f"镜像 [{slug}] 不存在。")
        return

    print(f"\n=== 向 [{slug}] 追加新素材 ===")
    print("[A] 微信聊天记录")
    print("[B] 直接粘贴/口述")
    print("[C] QQ 聊天记录")
    choice = input("选择 (A/B/C): ").strip().upper()

    from memory.embedder import Embedder
    from memory.vector_store import VectorStore

    emb_cfg = get_embedding_config()
    embedder = None
    vector_store = None
    if emb_cfg["api_key"]:
        embedder = Embedder(api_key=emb_cfg["api_key"], base_url=emb_cfg["base_url"], model=emb_cfg["model"])
        vector_store = VectorStore(
            persist_dir=str(ex_dir / "chroma_db"),
            collection_name=get_collection_name(slug),
        )

    materials_summary = ""
    if choice == "A":
        file_path = input("微信聊天记录文件路径: ").strip().strip('"').strip("'")
        if not file_path or not Path(file_path).exists():
            print("文件不存在，已取消。")
            return

        from memory.ingest import ingest_wechat_file, build_materials_summary

        name = slug.replace("_", " ")
        messages, chunk_count = ingest_wechat_file(file_path, slug, name, vector_store, embedder)
        if messages:
            print(f"  解析完成：{len(messages)} 条消息，入库 {chunk_count} 个切片")
            materials_summary = build_materials_summary(
                vector_store, embedder,
                messages_count=len(messages),
                prefix=f"新增聊天记录 {len(messages)} 条\n",
            )
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

        from memory.ingest import ingest_text
        if vector_store and embedder:
            n = ingest_text(text, slug, "oral_update", vector_store, embedder)
            print(f"  入库完成：{n} 个切片")
    elif choice == "C":
        file_path = input("QQ 聊天记录文件路径: ").strip().strip('"').strip("'")
        if not file_path or not Path(file_path).exists():
            print("文件不存在，已取消。")
            return

        from memory.ingest import ingest_qq_file, build_materials_summary

        name = slug.replace("_", " ")
        messages, chunk_count = ingest_qq_file(file_path, slug, name, vector_store, embedder)
        if messages:
            print(f"  解析完成：{len(messages)} 条消息，入库 {chunk_count} 个切片")
            materials_summary = build_materials_summary(
                vector_store, embedder,
                messages_count=len(messages),
                prefix=f"新增 QQ 聊天记录 {len(messages)} 条\n",
            )
    else:
        print("已取消。")
        return

    if materials_summary:
        print("正在增量合并...")
        from pipeline.merger import merge_new_material
        source_type = {"A": "wechat", "B": "oral", "C": "qq"}.get(choice, "oral")
        result = merge_new_material(slug, materials_summary, source_type=source_type)

        if result.get("error"):
            print(f"合并失败: {result['error']}")
        else:
            print("合并完成！memory.md、persona.md、SKILL.md 已更新。")


register("update", cmd_update)
