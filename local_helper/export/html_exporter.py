"""生成单 HTML、分类资源和完整性清单。"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Iterable

from local_helper.export.media import MediaResolver, RESOURCE_DIRECTORIES
from local_helper.wechat_macos.reader import Message, MessageKind


@dataclass(frozen=True)
class ExportResult:
    output_dir: Path
    html_file: Path
    manifest_file: Path
    status: str
    message_count: int


def export_conversation(
    *,
    messages: Iterable[Message],
    output_root: Path,
    account_root: Path,
    session_wxid: str,
    display_name: str,
    owner_wxid: str,
    wechat_version: str,
    schema_fingerprint: str,
    exporter_version: str,
    media_databases: tuple[Path, ...] = (),
) -> ExportResult:
    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink():
        raise ValueError("输出根目录不能是符号链接")
    output_root = output_root.resolve()
    safe_name = _safe_export_name(display_name)
    final_dir = _unique_output_dir(output_root, f"{safe_name}({session_wxid})")
    temp_dir = Path(
        tempfile.mkdtemp(prefix=".ex-memory-export-", dir=output_root)
    ).resolve()
    try:
        for directory in RESOURCE_DIRECTORIES:
            (temp_dir / directory).mkdir()
        resolver = MediaResolver(
            account_root=account_root,
            session_wxid=session_wxid,
            export_root=temp_dir,
            media_databases=media_databases,
        )
        html_path = temp_dir / f"{safe_name}.html"
        counts: Counter[str] = Counter()
        media_success: Counter[str] = Counter()
        missing_count = 0
        unknown_count = 0

        with html_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(_html_prefix(display_name))
            first = True
            for message in messages:
                exported, missing = resolver.export_for_message(message)
                if message.kind is MessageKind.UNKNOWN:
                    unknown_count += 1
                    raw_path = (
                        temp_dir / "raw" / f"{message.shard}-{message.local_id}.txt"
                    )
                    raw_path.write_text(message.content, encoding="utf-8")
                record = {
                    "id": f"{message.shard}:{message.local_id}",
                    "time": message.create_time,
                    "sender": message.sender_wxid,
                    "mine": message.sender_wxid == owner_wxid or message.direction == 1,
                    "kind": message.kind.value,
                    "rawType": message.raw_type,
                    "subtype": message.app_subtype,
                    "content": message.content,
                    "media": [asdict(item) for item in exported],
                    "missing": [asdict(item) for item in missing],
                }
                stream.write(("" if first else ",\n") + _safe_json(record))
                first = False
                counts[message.kind.value] += 1
                for item in exported:
                    media_success[item.category] += 1
                missing_count += len(missing)
            stream.write(_html_suffix())

        message_count = sum(counts.values())
        status = "complete" if unknown_count == 0 and missing_count == 0 else "partial"
        manifest = {
            "format": "ex-memory-wechat-export-v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "session": {"display_name": display_name, "wxid": session_wxid},
            "message_count": message_count,
            "message_types": dict(sorted(counts.items())),
            "media": {
                "exported": dict(sorted(media_success.items())),
                "missing": missing_count,
            },
            "unknown_message_count": unknown_count,
            "wechat_version": wechat_version,
            "schema_fingerprint": schema_fingerprint,
            "exporter_version": exporter_version,
        }
        manifest_path = temp_dir / "export-manifest.json"
        _write_json_atomic(manifest_path, manifest)
        os.replace(temp_dir, final_dir)
        return ExportResult(
            output_dir=final_dir,
            html_file=final_dir / html_path.name,
            manifest_file=final_dir / manifest_path.name,
            status=status,
            message_count=message_count,
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _safe_export_name(name: str) -> str:
    cleaned = re.sub(r"[/:\\\x00-\x1f]", "_", name).strip(" .")
    return (cleaned or "wechat-conversation")[:120]


def _unique_output_dir(root: Path, base_name: str) -> Path:
    candidate = root / base_name
    if not candidate.exists():
        return candidate
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    index = 0
    while True:
        suffix = f"-{timestamp}" if index == 0 else f"-{timestamp}-{index}"
        candidate = root / f"{base_name}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _safe_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _write_json_atomic(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _html_prefix(display_name: str) -> str:
    title = escape(display_name, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - 微信聊天导出</title><style>
:root{{--green:#95ec69;--bg:#ededed;--text:#191919}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}header{{position:sticky;top:0;z-index:3;background:#f7f7f7;border-bottom:1px solid #ddd;padding:12px 16px}}h1{{font-size:18px;margin:0 0 10px}}.tools{{display:flex;gap:8px;flex-wrap:wrap}}input,select,button{{font:inherit;padding:7px 10px;border:1px solid #ccc;border-radius:6px;background:white}}main{{max-width:920px;margin:auto;padding:18px}}.msg{{display:flex;margin:12px 0;gap:8px}}.msg.mine{{justify-content:flex-end}}.bubble{{max-width:72%;padding:9px 12px;border-radius:7px;background:white;white-space:pre-wrap;overflow-wrap:anywhere}}.mine .bubble{{background:var(--green)}}.meta{{font-size:11px;color:#888;margin-bottom:4px}}.media{{display:block;max-width:100%;margin-top:7px}}audio,video{{width:min(520px,100%)}}.missing{{color:#b54708;font-size:12px;margin-top:6px}}.pager{{display:flex;justify-content:center;align-items:center;gap:10px;padding:16px}}.empty{{text-align:center;color:#888;padding:48px}}
</style></head><body><header><h1>{title}</h1><div class="tools"><input id="search" placeholder="搜索消息"><select id="kind"><option value="">全部类型</option></select></div></header><main><section id="messages"></section><div class="pager"><button id="prev">上一页</button><span id="page"></span><button id="next">下一页</button></div></main>
<script>const DATA=[
"""


def _html_suffix() -> str:
    return """
];
const PAGE_SIZE=300;let page=0;const box=document.getElementById('messages');const search=document.getElementById('search');const kind=document.getElementById('kind');
for(const value of [...new Set(DATA.map(item=>item.kind))].sort()){const option=document.createElement('option');option.value=value;option.textContent=value;kind.append(option)}
function filtered(){const query=search.value.trim().toLowerCase();return DATA.filter(item=>(!kind.value||item.kind===kind.value)&&(!query||item.content.toLowerCase().includes(query)||item.sender.toLowerCase().includes(query)))}
function mediaNode(item){const path=item.relative_path;const ext=path.split('.').pop().toLowerCase();let node;if(['jpg','jpeg','png','gif','webp','heic'].includes(ext)){node=document.createElement('img');node.loading='lazy'}else if(['mp4','mov','m4v'].includes(ext)){node=document.createElement('video');node.controls=true}else if(['mp3','m4a','wav','amr','aud'].includes(ext)){node=document.createElement('audio');node.controls=true}else{node=document.createElement('a');node.textContent='打开附件';node.target='_blank'}node.className='media';node.src=path;node.href=path;return node}
function render(){const items=filtered();const pages=Math.max(1,Math.ceil(items.length/PAGE_SIZE));page=Math.min(page,pages-1);box.replaceChildren();const fragment=document.createDocumentFragment();for(const item of items.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE)){const row=document.createElement('article');row.className='msg'+(item.mine?' mine':'');const bubble=document.createElement('div');bubble.className='bubble';const meta=document.createElement('div');meta.className='meta';meta.textContent=`${new Date(item.time*1000).toLocaleString()} · ${item.sender} · ${item.kind}`;const content=document.createElement('div');content.textContent=item.content||`[${item.kind}]`;bubble.append(meta,content);for(const media of item.media)bubble.append(mediaNode(media));for(const missing of item.missing){const warning=document.createElement('div');warning.className='missing';warning.textContent=`资源不可用：${missing.hint}`;bubble.append(warning)}row.append(bubble);fragment.append(row)}if(!items.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='没有匹配的消息';fragment.append(empty)}box.append(fragment);document.getElementById('page').textContent=`${page+1} / ${pages} · ${items.length} 条`;document.getElementById('prev').disabled=page===0;document.getElementById('next').disabled=page>=pages-1}
search.addEventListener('input',()=>{page=0;render()});kind.addEventListener('change',()=>{page=0;render()});document.getElementById('prev').onclick=()=>{page--;render();scrollTo(0,0)};document.getElementById('next').onclick=()=>{page++;render();scrollTo(0,0)};render();
</script></body></html>"""
