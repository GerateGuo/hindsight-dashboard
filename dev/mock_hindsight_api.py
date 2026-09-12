#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_hindsight_api.py — 一个只返回假数据的 Hindsight API 替身，用于本地开发面板。

不需要真的部署 Hindsight，也不需要任何 API key：起这个服务，指向它跑面板就行。

    python3 dev/mock_hindsight_api.py --port 8899
    python3 hindsight-dashboard.py --api http://localhost:8899 --bank atlas --port 8995

它实现了面板会调用的全部只读接口（stats / timeseries / memories / entities /
graph / operations / llm-requests / config / metrics）以及 recall、reflect、retry
三个写接口的桩实现。所有数据都是固定的合成内容，与任何真实用户无关。
"""

import argparse
import json
import math
import random
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BANK = "atlas"

MEMORIES = [
    ("world", "Atlas 后端从 SQLite 迁移到 PostgreSQL 15，迁移脚本放在 migrations/ 下，用 alembic 管理版本。"),
    ("world", "CI 跑在自建 runner 上，构建时间从 11 分钟压到 4 分钟，主要靠开启依赖缓存。"),
    ("world", "团队约定：所有对外 HTTP 接口必须显式声明超时，禁止使用框架默认值。"),
    ("experience", "给解析器加缓存那次改动，收益比预期大：p99 从 820ms 降到 145ms。"),
    ("experience", "在评审里被指出：只在文档里写「已弃用」而不加运行时告警，半年后没人记得。"),
    ("observation", "这个项目对「显式优于隐式」的偏好非常一致，配置、超时、错误处理都要写清楚。"),
    ("observation", "构建与部署相关的问题几乎都出在缓存假设不一致上，而不是代码本身。"),
    ("world", "监控面板用 Grafana，告警走 PagerDuty，值班轮换是每周一换。"),
    ("experience", "夜间跑批任务曾经因为时钟漂移导致重复执行，后来改成基于幂等键去重。"),
    ("world", "代码仓默认分支是 main，发布走 tag + 自动生成 changelog。"),
    ("observation", "重构类任务如果能配套一个回归测试，落地速度明显更快。"),
    ("experience", "引入结构化日志后，排查线上问题的时间平均缩短了一半。"),
]

ENTITIES = [
    ("PostgreSQL", 412), ("CI runner", 358), ("Grafana", 291), ("Atlas API", 264),
    ("alembic", 217), ("PagerDuty", 186), ("缓存策略", 173), ("幂等键", 154),
    ("结构化日志", 141), ("回归测试", 126), ("发布流程", 118), ("超时配置", 97),
    ("时钟漂移", 74), ("依赖缓存", 68), ("告警规则", 61), ("变更窗口", 55),
]

OPS_TYPES = ["retain", "batch_retain", "consolidation", "refresh_mental_models"]


def rnd(seed):
    return random.Random(seed)


def iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat()


def banks_payload():
    now = datetime.now(timezone.utc)
    return {"banks": [{
        "bank_id": BANK, "name": "Atlas 项目笔记",
        "disposition": {"skepticism": 3, "literalism": 3, "empathy": 2},
        "mission": "记录 Atlas 项目的工程决策与踩坑经验",
        "created_at": iso(now - timedelta(days=96)),
        "updated_at": iso(now), "fact_count": 1284,
        "last_document_at": iso(now - timedelta(hours=3)),
    }]}


def timeseries(period):
    days = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}.get(period, 7)
    now = datetime.now(timezone.utc)
    out = []
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        r = rnd(d.toordinal())
        base = max(0, int(6 + 10 * math.sin(i / 9.0) + r.randint(-3, 6)))
        if i < 2:
            base = int(base * 0.4)
        out.append({"time": iso(d), "world": base, "experience": int(base * 0.6),
                    "observation": int(base * 0.8)})
    return {"bank_id": BANK, "period": period, "trunc": "day",
            "time_field": "created_at", "buckets": out}


def llm_requests(limit, offset, status=""):
    now = datetime.now(timezone.utc)
    items = []
    for i in range(offset, min(offset + limit, 87)):
        r = rnd(1000 + i)
        op = ["retain_extract_facts", "consolidation", "verification"][i % 3]
        ok = (i % 11 != 0)
        st = "success" if ok else "error"
        if status and status != st:
            continue
        items.append({
            "id": f"req-{i:04d}", "bank_id": BANK, "operation": op, "scope": op,
            "provider": "openai", "model": "example-model",
            "status": st, "started_at": iso(now - timedelta(minutes=17 * i + 4)),
            "ended_at": iso(now - timedelta(minutes=17 * i + 3)),
            "duration_ms": 1200 + r.randint(0, 26000),
            "input_tokens": 1400 + r.randint(0, 5200),
            "output_tokens": 380 + r.randint(0, 3100),
            "cached_tokens": r.randint(0, 900),
        })
    return {"bank_id": BANK, "total": 87, "limit": limit, "offset": offset, "items": items}


def operations(limit, offset, status="", exclude_parents=False):
    now = datetime.now(timezone.utc)
    items = []
    for i in range(offset, min(offset + limit, 42)):
        r = rnd(2000 + i)
        t = OPS_TYPES[i % len(OPS_TYPES)]
        if status == "failed":
            if i % 7 != 3:
                continue
            st = "failed"
        elif status:
            st = status
        else:
            st = ["completed", "completed", "completed", "processing", "pending"][i % 5]
        created = now - timedelta(minutes=13 * i + 2)
        items.append({
            "id": f"op-{i:04d}", "task_type": t, "items_count": 1 if "retain" in t else 0,
            "document_id": None, "filename": None, "created_at": iso(created),
            "updated_at": iso(created + timedelta(seconds=20 + r.randint(0, 90))),
            "status": st,
            "error_message": ("Fact extraction failed: upstream connection reset" if st == "failed" else None),
            "retry_count": 0 if st != "failed" else 2,
            "next_retry_at": None, "progress": None,
        })
    return {"bank_id": BANK, "total": 42, "limit": limit, "offset": offset, "operations": items}


def memories_list(limit, offset, q="", ftype=""):
    now = datetime.now(timezone.utc)
    pool = [m for m in MEMORIES if (not ftype or m[0] == ftype) and (not q or q.lower() in m[1].lower())]
    items = []
    for i, (t, text) in enumerate(pool[offset:offset + limit]):
        ts = now - timedelta(hours=3 + i * 7)
        items.append({"id": f"mem-{offset+i:04d}", "text": text, "context": "engineering notes",
                      "fact_type": t, "type": t, "date": iso(ts), "mentioned_at": iso(ts),
                      "entities": [], "proof_count": 1 + (i % 3), "document_id": None})
    return {"items": items, "total": len(pool), "limit": limit, "offset": offset}


def stats():
    return {
        "bank_id": BANK, "total_nodes": 1284, "total_links": 9721, "total_documents": 96,
        "nodes_by_fact_type": {"world": 612, "experience": 298, "observation": 374},
        "links_by_link_type": {"temporal": 6210, "semantic": 2380, "entity": 940, "caused_by": 191},
        "pending_operations": 2, "failed_operations": 3,
        "operations_by_status": {"completed": 402, "failed": 3, "pending": 2},
        "last_consolidated_at": iso(datetime.now(timezone.utc) - timedelta(hours=3)),
        "pending_consolidation": 1, "failed_consolidation": 0, "total_observations": 374,
    }


def entities(limit, offset):
    items = [{"id": f"ent-{i:03d}", "canonical_name": n, "mention_count": c,
              "first_seen": iso(datetime.now(timezone.utc) - timedelta(days=90)),
              "last_seen": iso(datetime.now(timezone.utc) - timedelta(hours=i)),
              "metadata": {}} for i, (n, c) in enumerate(ENTITIES[offset:offset + limit])]
    return {"items": items, "total": len(ENTITIES), "limit": limit, "offset": offset}


def graph(limit):
    n = min(len(ENTITIES), max(4, limit // 2))
    nodes = [{"data": {"id": f"ent-{i:03d}", "label": ENTITIES[i][0],
                       "mentionCount": ENTITIES[i][1], "color": "#42a5f5"}} for i in range(n)]
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            r = rnd(i * 100 + j)
            if r.random() < 0.22:
                edges.append({"data": {
                    "id": f"ent-{i:03d}-ent-{j:03d}", "source": f"ent-{i:03d}",
                    "target": f"ent-{j:03d}", "linkType": "cooccurrence",
                    "weight": r.randint(4, 120), "color": "#ffd700",
                    "lineStyle": "solid", "lastCooccurred": iso(datetime.now(timezone.utc)),
                }})
    return {"nodes": nodes, "edges": edges[:limit], "total_entities": n,
            "total_edges": len(edges), "limit": limit}


def metrics_text():
    lines = [
        'hindsight_llm_calls_total{scope="retain_extract_facts",model="example-model"} 812.0',
        'hindsight_llm_calls_total{scope="consolidation",model="example-model"} 461.0',
        'hindsight_llm_calls_total{scope="verification",model="example-model"} 27.0',
        'hindsight_llm_tokens_input_tokens_total{scope="retain_extract_facts"} 2418300.0',
        'hindsight_llm_tokens_output_tokens_total{scope="retain_extract_facts"} 910400.0',
        'hindsight_llm_tokens_cached_input_tokens_total{scope="retain_extract_facts"} 402100.0',
        'hindsight_operation_operations_total{task_type="retain",status="completed"} 402.0',
        'hindsight_operation_operations_total{task_type="consolidation",status="completed"} 118.0',
        'hindsight_process_memory_bytes 268435456.0',
        'hindsight_process_cpu_seconds 214.5',
        'hindsight_process_threads 14.0',
        'hindsight_db_pool_size 5.0',
        'hindsight_db_pool_idle 3.0',
    ]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        ints = lambda k, d: int(q.get(k, d) or d)

        if p == "/health":
            return self._json({"status": "healthy", "database": "connected"})
        if p == "/version":
            return self._json({"api_version": "0.0.0-mock", "features": {
                "observations": True, "mcp": True, "worker": True, "bank_config_api": True,
                "file_upload_api": True, "llm_trace": True, "audit_log": False}})
        if p == "/metrics":
            body = metrics_text().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if p == "/v1/default/banks":
            return self._json(banks_payload())
        if p.endswith("/stats/memories-timeseries"):
            return self._json(timeseries(q.get("period", "7d")))
        if p.endswith("/llm-requests/stats"):
            b = timeseries(q.get("period", "30d"))["buckets"]
            out = [{"time": x["time"], "statuses": {"success": 4}, "total": 4,
                    "tokens": {"input": 6400, "output": 2300, "cached": 900, "total": 9600}}
                   for x in b]
            return self._json({"bank_id": BANK, "period": q.get("period", "30d"),
                               "trunc": "day", "start": out[0]["time"] if out else None,
                               "buckets": out})
        if p.endswith("/operations"):
            return self._json(operations(ints("limit", 20), ints("offset", 0),
                                         q.get("status", ""), q.get("exclude_parents") == "true"))
        if p.endswith("/llm-requests"):
            return self._json(llm_requests(ints("limit", 20), ints("offset", 0), q.get("status", "")))
        if p.endswith("/memories/list"):
            return self._json(memories_list(ints("limit", 20), ints("offset", 0),
                                           q.get("q", ""), q.get("type", "")))
        if p.endswith("/entities/graph"):
            return self._json(graph(ints("limit", 40)))
        if p.endswith("/entities"):
            return self._json(entities(ints("limit", 200), ints("offset", 0)))
        if p.endswith("/config"):
            return self._json({"bank_id": BANK, "config": {
                "retain_chunk_size": 3000, "retain_extraction_mode": "concise",
                "retain_chunk_batch_size": 100, "enable_observations": True,
                "enable_auto_consolidation": True, "consolidation_max_memories_per_round": 100,
                "consolidation_llm_batch_size": 8, "consolidation_llm_parallelism": 4}})
        if p.endswith("/profile"):
            return self._json({"bank_id": BANK, "name": "Atlas 项目笔记",
                               "disposition": {"skepticism": 3, "literalism": 3, "empathy": 2},
                               "mission": "记录 Atlas 项目的工程决策与踩坑经验", "background": ""})
        if p.endswith("/directives"):
            return self._json({"items": [{"id": "dir-1", "text": "回答部署相关问题时必须引用具体版本号",
                                          "active": True,
                                          "created_at": iso(datetime.now(timezone.utc))}]})
        if p.endswith("/mental-models"):
            return self._json({"items": []})
        if p.endswith("/stats"):
            return self._json(stats())
        return self._json({"detail": "Not Found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        ln = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(ln).decode() or "{}") if ln else {}
        except json.JSONDecodeError:
            body = {}
        if u.path.endswith("/memories/recall"):
            now = datetime.now(timezone.utc)
            res = []
            for i, (t, text) in enumerate(MEMORIES[:6]):
                r = rnd(i)
                res.append({"id": f"mem-{i:04d}", "text": text, "type": t,
                            "entities": ["Atlas API", "PostgreSQL", "CI runner"][: 1 + i % 3],
                            "mentioned_at": iso(now - timedelta(days=i)),
                            "tags": ["project:atlas"],
                            "scores": {"final": round(0.92 - i * 0.06, 3),
                                       "semantic": round(0.71 - i * 0.05, 3),
                                       "reranker": round(0.68 - i * 0.04, 3)}})
            return self._json({"results": res, "trace_id": "trace-mock-0001"})
        if u.path.endswith("/reflect"):
            return self._json({
                "text": "根据已有记录：这个项目对「显式优于隐式」的偏好非常一致——配置、超时、\n"
                        "错误处理都要求写明。历史上构建与部署类问题多来自缓存假设不一致，\n"
                        "而重构类任务如果配套回归测试，落地速度会明显更快。",
                "usage": {"input_tokens": 3120, "output_tokens": 260}})
        if "/operations/" in u.path and u.path.endswith("/retry"):
            return self._json({"success": True, "message": "Operation queued for retry"})
        return self._json({"detail": "Not Found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="假数据 Hindsight API（开发面板用）")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"[mock-hindsight] http://{a.host}:{a.port} （镜像库 {BANK}，全部为合成数据）", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
