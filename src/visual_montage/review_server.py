from __future__ import annotations

import argparse
import json
import mimetypes
import re
from collections import Counter, defaultdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _author(video_path: str) -> str:
    stem = Path(video_path).stem
    match = re.search(r"(@[^*_]+(?:[^*]*?))\*", stem)
    if match:
        return match.group(1).strip()
    match = re.search(r"_(@[^_]+)_", stem)
    return match.group(1).strip() if match else "unknown"


def _media_url(run_dir: Path, path: Path | None) -> str | None:
    if not path:
        return None
    try:
        relative = path.resolve().relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return None
    return f"/media/{quote(run_dir.name)}/{quote(relative.as_posix())}"


def _run_status(run_dir: Path, result: dict) -> str:
    if result:
        if result.get("ok"):
            return "completed"
        if int(result.get("failed_count") or 0) > 0:
            return "failed"
        if int(result.get("partial_count") or 0) > 0:
            return "partial"
    if (run_dir / "analysis" / "candidate-pool.json").is_file():
        return "analyzed"
    return "starting"


def _validation_error_zh(error: object) -> str:
    text = str(error)
    patterns = (
        (r"^timeline starts with gap:\s*(.+)$", r"时间线开头存在空隙：\1"),
        (r"^gap:\s*(.+)$", r"时间线存在空隙：\1"),
        (
            r"^insufficient unique candidates:\s*(.+)$",
            r"唯一候选不足：\1",
        ),
        (
            r"^insufficient distinct source videos:\s*(.+)$",
            r"不同源视频数量不足：\1",
        ),
        (
            r"^voiceover must contain at least two sentences$",
            "口播文案必须至少包含两句话",
        ),
        (r"^asset is not registered:\s*(.+)$", r"素材未注册：\1"),
    )
    for pattern, replacement in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def build_run_summary(run_dir: Path) -> dict:
    result = _read_json(run_dir / "result.json")
    pool = _read_json(run_dir / "analysis" / "candidate-pool.json")
    modified = datetime.fromtimestamp(run_dir.stat().st_mtime).isoformat(timespec="seconds")
    return {
        "run_id": run_dir.name,
        "category": result.get("category") or pool.get("category") or "unknown",
        "status": _run_status(run_dir, result),
        "modified_at": modified,
        "videos": int(pool.get("video_count") or 0),
        "candidates": int(pool.get("candidate_count") or len(pool.get("candidates") or [])),
        "analysis_failures": len(pool.get("failures") or result.get("analysis_failures") or []),
        "requested": int(result.get("requested_count") or 0),
        "committed": int(result.get("committed_count") or 0),
        "partial": int(result.get("partial_count") or 0),
        "failed": int(result.get("failed_count") or 0),
    }


def build_run_report(run_dir: Path) -> dict:
    result = _read_json(run_dir / "result.json")
    pool = _read_json(run_dir / "analysis" / "candidate-pool.json")
    diversity = _read_json(run_dir / "diversity-report.json")
    candidates = list(pool.get("candidates") or [])
    failures = list(pool.get("failures") or result.get("analysis_failures") or [])
    raw_by_video = {}
    raw_dir = run_dir / "analysis" / "raw"
    if raw_dir.is_dir():
        for path in raw_dir.glob("*.json"):
            raw = _read_json(path)
            material = raw.get("material") or {}
            video_id = str(material.get("video_id") or path.stem)
            raw_by_video[video_id] = raw

    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in candidates:
        grouped[str(item.get("video_id") or "unknown")].append(item)
    sources = []
    for video_id, items in grouped.items():
        sample = items[0]
        raw = raw_by_video.get(video_id) or {}
        duration = raw.get("duration_seconds")
        event_counts = Counter(str(item.get("event") or "unknown") for item in items)
        sources.append({
            "video_id": video_id,
            "author": _author(str(sample.get("video_path") or "")),
            "filename": Path(str(sample.get("video_path") or "")).name,
            "duration_seconds": duration,
            "candidate_count": len(items),
            "candidate_ratio": round(len(items) / max(1, len(candidates)), 4),
            "events": dict(event_counts.most_common()),
            "route": raw.get("analysis_route"),
            "elapsed_seconds": raw.get("elapsed_seconds"),
            "cache_hit": bool((raw.get("cache") or {}).get("hit")),
        })
    failed_ids = {str(item.get("video_id") or "") for item in failures}
    for item in failures:
        video_id = str(item.get("video_id") or "unknown")
        if video_id in grouped:
            continue
        video_path = str(item.get("path") or item.get("video_path") or "")
        sources.append({
            "video_id": video_id,
            "author": _author(video_path),
            "filename": Path(video_path).name,
            "duration_seconds": None,
            "candidate_count": 0,
            "candidate_ratio": 0,
            "events": {},
            "route": None,
            "elapsed_seconds": None,
            "cache_hit": False,
            "error": str(item.get("error") or "analysis failed"),
        })
    sources.sort(key=lambda item: (-int(item["candidate_count"]), item["video_id"]))

    creative_result = {
        str(item.get("creative_id")): item for item in result.get("creatives") or []
    }
    creatives = []
    creatives_dir = run_dir / "creatives"
    if creatives_dir.is_dir():
        for creative_dir in sorted(path for path in creatives_dir.iterdir() if path.is_dir()):
            plan = _read_json(creative_dir / "compose-plan.json")
            item_result = creative_result.get(creative_dir.name) or {}
            campaign = plan.get("campaign") or {}
            selected = list(plan.get("selected_candidates") or [])
            cover = item_result.get("cover") or {}
            cover_image2 = creative_dir / "cover" / "cover-image2.png"
            cover_preview = creative_dir / "cover" / "cover-preview.jpg"
            cover_clean = creative_dir / "cover" / "cover-clean.jpg"
            cover_options = creative_dir / "cover" / "cover-frame-options.jpg"
            display_cover = (
                cover_image2
                if cover_image2.is_file()
                else cover_preview if cover_preview.is_file() else None
            )
            image2 = cover.get("image2") or {}
            creatives.append({
                "creative_id": creative_dir.name,
                "status": item_result.get("status") or (
                    "planned" if plan else "starting"
                ),
                "candidate_count": len(selected),
                "source_count": len({str(value.get("video_id")) for value in selected}),
                "authors": sorted({_author(str(value.get("video_path") or "")) for value in selected}),
                "validation": plan.get("validation") or {},
                "cover_title": cover.get("title"),
                "cover_preview_url": _media_url(run_dir, display_cover),
                "cover_clean_url": _media_url(
                    run_dir, cover_clean if cover_clean.is_file() else None
                ),
                "cover_options_url": _media_url(
                    run_dir, cover_options if cover_options.is_file() else None
                ),
                "image2": image2,
                "voiceover_text": campaign.get("voiceover_text"),
                "packaging": {
                    "openpage": campaign.get("product_openpage_asset_id"),
                    "recording": campaign.get("product_recording_asset_id"),
                    "endcard": campaign.get("endcard_asset_id"),
                    "logo": campaign.get("logo_asset_id"),
                    "cover_logo": campaign.get("cover_logo_asset_id"),
                },
                "draft_name": item_result.get("draft_name"),
                "draft_path": item_result.get("draft_path"),
                "bgm_id": item_result.get("bgm_id"),
            })

    warnings = []
    candidate_count = len(candidates)
    if failures:
        warnings.append({"level": "red", "message": f"{len(failures)} 条源视频分析失败"})
    if candidate_count and candidate_count < 55:
        warnings.append({"level": "yellow", "message": f"候选池低于 55 个候选的生产目标（{candidate_count}/55）"})
    for source in sources:
        if float(source.get("candidate_ratio") or 0) > 0.25:
            warnings.append({"level": "red", "message": f"{source['author']} 贡献了候选池的 {source['candidate_ratio']:.0%}，单一来源占比过高"})
    pair_overlaps = [
        float(item.get("candidate_overlap_ratio") or 0)
        for item in diversity.get("creative_pairs") or []
    ]
    max_pair_overlap = max(pair_overlaps, default=0.0)
    if max_pair_overlap > 0.2:
        warnings.append({"level": "red", "message": f"两条成片之间的最大候选重合率为 {max_pair_overlap:.0%}，超过 20% 阈值"})
    never_used = diversity.get("never_used_candidate_ratio")
    if never_used is not None and float(never_used) < 0.8:
        warnings.append({"level": "yellow", "message": f"历史未使用候选占比为 {float(never_used):.0%}，低于 80% 目标"})
    partial_or_failed = int(result.get("partial_count") or 0) + int(result.get("failed_count") or 0)
    if partial_or_failed:
        warnings.append({"level": "red", "message": f"{partial_or_failed} 条成片处于部分完成或失败状态"})
    for creative in creatives:
        errors = list((creative.get("validation") or {}).get("errors") or [])
        if errors:
            warnings.append({
                "level": "red",
                "message": (
                    f"{creative['creative_id']}："
                    + "；".join(_validation_error_zh(error) for error in errors)
                ),
            })
        if creative.get("image2") and not creative["image2"].get("ok", True):
            warnings.append({"level": "yellow", "message": f"{creative['creative_id']}：Image2 生成失败，当前使用备用封面"})

    contact_sheet = run_dir / "analysis" / "contact-sheet.jpg"
    summary = build_run_summary(run_dir)
    summary.update({
        "routing": pool.get("routing_summary") or {},
        "contact_sheet_url": _media_url(run_dir, contact_sheet if contact_sheet.is_file() else None),
        "sources": sources,
        "failures": failures,
        "warnings": warnings,
        "diversity": {
            "selected_candidate_count": diversity.get("selected_candidate_count"),
            "unique_candidate_count": diversity.get("unique_candidate_count"),
            "maximum_candidate_use_count": diversity.get("maximum_candidate_use_count"),
            "never_used_candidate_ratio": never_used,
            "maximum_pairwise_candidate_overlap_ratio": max_pair_overlap,
            "pairs": diversity.get("creative_pairs") or [],
        },
        "creatives": creatives,
        "result_warnings": result.get("warnings") or [],
        "failed_video_ids": sorted(failed_ids),
    })
    return summary


HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Visual Montage Monitor</title><style>
:root{--bg:#0b0d12;--panel:#141821;--panel2:#1b202b;--line:#2a3140;--text:#f4f6fb;--muted:#96a0b5;--red:#ff5a67;--yellow:#ffbf47;--green:#45d483;--blue:#74a7ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{height:64px;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 24px;position:sticky;top:0;background:rgba(11,13,18,.94);backdrop-filter:blur(14px);z-index:3}header h1{font-size:17px;margin:0}header span{margin-left:auto;color:var(--muted);font-size:12px}.layout{display:grid;grid-template-columns:280px minmax(0,1fr);min-height:calc(100vh - 64px)}aside{border-right:1px solid var(--line);padding:16px;overflow:auto}.run{padding:12px;border:1px solid transparent;border-radius:12px;margin-bottom:8px;cursor:pointer;background:var(--panel)}.run:hover,.run.active{border-color:var(--blue);background:var(--panel2)}.run b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sub{font-size:11px;color:var(--muted);margin-top:7px;display:flex;gap:8px}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}.completed{background:var(--green)}.partial,.analyzed{background:var(--yellow)}.failed{background:var(--red)}.starting{background:var(--blue)}main{padding:24px;overflow:hidden}.titlebar{display:flex;gap:12px;align-items:flex-start;margin-bottom:18px}.titlebar h2{margin:0;font-size:25px}.badge{padding:5px 9px;border-radius:999px;background:var(--panel2);color:var(--muted);font-size:11px}.cards{display:grid;grid-template-columns:repeat(6,minmax(105px,1fr));gap:10px;margin-bottom:18px}.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px}.card .value{font-size:24px;font-weight:700}.card .label{font-size:11px;color:var(--muted);margin-top:5px}.section{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:16px}.section h3{font-size:15px;margin:0 0 14px}.alerts{display:grid;gap:8px}.alert{padding:10px 12px;border-radius:10px;background:var(--panel2);font-size:12px;border-left:3px solid var(--yellow)}.alert.red{border-color:var(--red)}.alert.yellow{border-color:var(--yellow)}.empty{color:var(--muted);font-size:12px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500;position:sticky;top:64px;background:var(--panel)}td code{color:#b7ccff}.bar{height:6px;background:#232a38;border-radius:4px;overflow:hidden;min-width:80px;margin-top:5px}.bar i{display:block;height:100%;background:var(--blue)}.contact{max-width:100%;display:block;border-radius:10px;background:#090a0d}.creative-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}.creative{background:var(--panel2);border:1px solid var(--line);border-radius:14px;overflow:hidden}.creative img{width:100%;aspect-ratio:9/16;object-fit:cover;max-height:420px;background:#090a0d}.creative-body{padding:14px}.creative h4{margin:0 0 8px}.copy{font-size:12px;line-height:1.55;color:#d9deea;background:#10131a;border-radius:8px;padding:9px;margin-top:8px}.kv{display:grid;grid-template-columns:90px 1fr;gap:5px;font-size:11px;margin-top:8px}.kv span:nth-child(odd){color:var(--muted)}.overlap{display:flex;gap:8px;flex-wrap:wrap}.pill{padding:6px 8px;background:var(--panel2);border-radius:8px;font-size:11px}.pill.bad{color:var(--red)}@media(max-width:900px){.layout{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid var(--line);max-height:220px}.cards{grid-template-columns:repeat(2,1fr)}main{padding:14px}}
th{position:static;top:auto}th,td{overflow-wrap:anywhere}th:nth-child(1){width:22%}th:nth-child(2){width:7%}th:nth-child(3){width:9%}th:nth-child(4){width:7%}th:nth-child(5){width:31%}th:nth-child(6){width:24%}
.run-group{margin-bottom:10px}.run-group-head{display:flex;align-items:center;gap:8px;padding:10px 8px;color:#dce2ef;font-size:12px;font-weight:700;cursor:pointer;border-radius:9px}.run-group-head:hover{background:var(--panel)}.run-group-head .chevron{width:14px;color:var(--muted);transition:transform .15s}.run-group-head .count{margin-left:auto;color:var(--muted);font-size:10px;font-weight:500}.run-group-body{padding-left:10px;border-left:1px solid var(--line);margin-left:14px}.run-group.collapsed .run-group-body{display:none}.run-group.collapsed .chevron{transform:rotate(-90deg)}.run-group .run{padding:10px;margin-bottom:6px}.run-group .run b{font-size:12px}
</style></head><body><header><h1>Visual Montage · 只读监控</h1><span id="refresh">每15秒自动刷新</span></header><div class="layout"><aside><div id="runs"></div></aside><main id="main"><div class="empty">正在加载批次…</div></main></div>
<script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let active=new URLSearchParams(location.search).get('run')||'';let runs=[];const savedCollapsed=localStorage.getItem('vm-collapsed-categories');let collapsed=new Set(JSON.parse(savedCollapsed||'[]'));let collapseInitialized=savedCollapsed!==null;
async function getJSON(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw new Error(await r.text());return r.json()}
function runList(){const groups=new Map();runs.forEach(r=>{const key=r.category||'unknown';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(r)});document.querySelector('#runs').innerHTML=[...groups.entries()].map(([category,items])=>`<div class="run-group ${collapsed.has(category)?'collapsed':''}" data-category="${esc(category)}"><div class="run-group-head"><span class="chevron">▾</span><span>${esc(category)}</span><span class="count">${items.length} 个任务</span></div><div class="run-group-body">${items.map(r=>`<div class="run ${r.run_id===active?'active':''}" data-id="${esc(r.run_id)}"><b><i class="dot ${esc(r.status)}"></i>${esc(r.run_id)}</b><div class="sub"><span>${r.candidates}候选</span><span>${r.analysis_failures}失败</span></div></div>`).join('')}</div></div>`).join('');document.querySelectorAll('.run-group-head').forEach(el=>el.onclick=()=>{const group=el.parentElement;const category=group.dataset.category;if(collapsed.has(category))collapsed.delete(category);else collapsed.add(category);localStorage.setItem('vm-collapsed-categories',JSON.stringify([...collapsed]));group.classList.toggle('collapsed')});document.querySelectorAll('.run').forEach(el=>el.onclick=e=>{e.stopPropagation();active=el.dataset.id;history.replaceState(null,'','?run='+encodeURIComponent(active));runList();loadRun()})}
function metric(value,label){return `<div class="card"><div class="value">${esc(value)}</div><div class="label">${esc(label)}</div></div>`}
function render(d){const div=d.diversity||{};const alerts=(d.warnings||[]).map(x=>`<div class="alert ${esc(x.level)}">${esc(x.message)}</div>`).join('')||'<div class="empty">没有检测到自动告警</div>';const sourceRows=(d.sources||[]).map(s=>`<tr><td>${esc(s.author)}<br><code>${esc(s.video_id.replace(/^.*?_/,''))}</code></td><td>${s.duration_seconds==null?'—':Number(s.duration_seconds).toFixed(1)+'s'}</td><td><b>${s.candidate_count}</b><div class="bar"><i style="width:${Math.min(100,s.candidate_ratio*100)}%"></i></div></td><td>${(s.candidate_ratio*100).toFixed(1)}%</td><td>${esc(Object.entries(s.events||{}).map(([k,v])=>k+': '+v).join(' · '))}</td><td>${s.error?'<span style="color:var(--red)">'+esc(s.error)+'</span>':esc(s.route||'—')}</td></tr>`).join('');const pairs=(div.pairs||[]).map(p=>`<span class="pill ${p.candidate_overlap_ratio>.2?'bad':''}">${esc(p.a)} × ${esc(p.b)}：候选 ${(p.candidate_overlap_ratio*100).toFixed(0)}% / 来源 ${(p.source_overlap_ratio*100).toFixed(0)}%</span>`).join('')||'<span class="empty">尚无成片重合数据</span>';const creativeCards=(d.creatives||[]).map(c=>`<article class="creative">${c.cover_preview_url?`<img loading="lazy" src="${c.cover_preview_url}">`:''}<div class="creative-body"><h4>${esc(c.creative_id)} <span class="badge">${esc(c.status)}</span></h4><div class="sub"><span>${c.candidate_count}候选</span><span>${c.source_count}源视频</span><span>${esc(c.bgm_id||'无BGM')}</span></div><div class="copy"><b>封面：</b>${esc(c.cover_title||'尚未生成')}</div><div class="copy"><b>口播：</b>${esc(c.voiceover_text||'尚未生成')}</div><div class="kv"><span>开屏</span><span>${esc(c.packaging.openpage||'—')}</span><span>录屏</span><span>${esc(c.packaging.recording||'—')}</span><span>尾贴</span><span>${esc(c.packaging.endcard||'—')}</span><span>作者</span><span>${esc(c.authors.join('、'))}</span><span>剪映草稿</span><span>${esc(c.draft_name||'—')}</span></div>${c.image2&&c.image2.error?`<div class="alert yellow" style="margin-top:10px">Image2：${esc(c.image2.error)}</div>`:''}</div></article>`).join('')||'<div class="empty">尚未生成成片方案</div>';document.querySelector('#main').innerHTML=`<div class="titlebar"><div><h2>${esc(d.run_id)}</h2><div class="sub"><span>${esc(d.category)}</span><span>更新 ${esc(d.modified_at)}</span></div></div><span class="badge">${esc(d.status)}</span></div><div class="cards">${metric(d.videos,'输入视频')}${metric(d.candidates,'有效候选')}${metric(d.analysis_failures,'分析失败')}${metric(d.requested,'请求成片')}${metric(d.committed,'已完成')}${metric(d.partial+d.failed,'异常成片')}</div><section class="section"><h3>自动告警</h3><div class="alerts">${alerts}</div></section><section class="section"><h3>候选池来源分布</h3><div style="overflow:auto"><table><thead><tr><th>作者 / 视频</th><th>时长</th><th>候选</th><th>占比</th><th>事件</th><th>路由 / 错误</th></tr></thead><tbody>${sourceRows}</tbody></table></div></section><section class="section"><h3>Contact Sheet</h3>${d.contact_sheet_url?`<img class="contact" loading="lazy" src="${d.contact_sheet_url}">`:'<div class="empty">尚未生成 Contact Sheet</div>'}</section><section class="section"><h3>成片重复度</h3><div class="cards">${metric(div.unique_candidate_count??'—','唯一候选')}${metric(div.selected_candidate_count??'—','候选使用位')}${metric(div.maximum_candidate_use_count??'—','单候选最高使用')}${metric(div.never_used_candidate_ratio==null?'—':(div.never_used_candidate_ratio*100).toFixed(0)+'%','未使用候选占比')}${metric((div.maximum_pairwise_candidate_overlap_ratio*100).toFixed(0)+'%','最大候选重合')}</div><div class="overlap">${pairs}</div></section><section class="section"><h3>成片、封面文案、口播与包装素材</h3><div class="creative-grid">${creativeCards}</div></section>`}
async function loadRuns(){try{runs=await getJSON('/api/runs');if(!active&&runs.length)active=runs[0].run_id;if(!collapseInitialized){const activeCategory=(runs.find(r=>r.run_id===active)||{}).category;collapsed=new Set([...new Set(runs.map(r=>r.category||'unknown'))].filter(category=>category!==activeCategory));localStorage.setItem('vm-collapsed-categories',JSON.stringify([...collapsed]));collapseInitialized=true}runList();await loadRun()}catch(e){document.querySelector('#main').innerHTML='<div class="alert red">'+esc(e)+'</div>'}}
async function loadRun(){if(!active)return;try{render(await getJSON('/api/runs/'+encodeURIComponent(active)))}catch(e){document.querySelector('#main').innerHTML='<div class="alert red">'+esc(e)+'</div>'}}
loadRuns();setInterval(async()=>{await loadRuns();document.querySelector('#refresh').textContent='刚刚刷新 · 每15秒'},15000);
</script></body></html>"""


class MonitorHandler(BaseHTTPRequestHandler):
    runs_root: Path

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            data = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            return
        if path == "/api/runs":
            runs = [
                build_run_summary(item)
                for item in self.runs_root.iterdir()
                if item.is_dir() and not item.name.startswith(".")
            ] if self.runs_root.is_dir() else []
            runs.sort(key=lambda item: item["modified_at"], reverse=True)
            self._json(runs)
            return
        if path.startswith("/api/runs/"):
            run_id = path.removeprefix("/api/runs/")
            run_dir = (self.runs_root / run_id).resolve()
            try:
                run_dir.relative_to(self.runs_root.resolve())
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not run_dir.is_dir():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(build_run_report(run_dir))
            return
        if path.startswith("/media/"):
            parts = path.removeprefix("/media/").split("/", 1)
            if len(parts) != 2:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            target = (self.runs_root / parts[0] / parts[1]).resolve()
            try:
                target.relative_to((self.runs_root / parts[0]).resolve())
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self._file(target)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        self._json({"error": "read-only monitor: writes are disabled"}, status=405)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[review-server] {self.address_string()} {format % args}")


def serve_monitor(*, runs_root: Path, host: str, port: int, run_id: str = "") -> None:
    runs_root = runs_root.resolve()
    handler = type("ConfiguredMonitorHandler", (MonitorHandler,), {"runs_root": runs_root})
    server = ThreadingHTTPServer((host, port), handler)
    initial = f"?run={quote(run_id)}" if run_id else ""
    print(f"Read-only monitor: http://{host}:{port}/{initial}", flush=True)
    print(f"Runs root: {runs_root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only visual montage run monitor")
    parser.add_argument("--runs-root", type=Path, default=Path("data/runs"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args(argv)
    serve_monitor(
        runs_root=args.runs_root,
        host=args.host,
        port=args.port,
        run_id=args.run_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
