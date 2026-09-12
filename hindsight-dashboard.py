#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hindsight-dashboard.py — Hindsight 本地管理面板

自托管的 Hindsight 只有 Swagger(/docs) 和 Prometheus(/metrics)，而官方 Control Plane
是个独立前端（默认 9999）。本面板是二者之间的一个轻量补充：单文件、纯标准库、
零依赖，把 Hindsight 的 REST 接口聚合成一个可视面板，并可选地把官方 Control Plane
内嵌成其中一个标签页 —— 一个入口既能看聚合概览，也能随时切到官方完整功能。

自带能力：
  · 概览：记忆总量/增长曲线（world / experience / observation 堆叠）、运行状态
  · 检索：recall 召回（带 final / 语义 / 重排 三路打分明细）与 reflect 反思答案
  · 记忆：全文搜索、类型筛选、分页浏览
  · 图谱：实体搜索 + 零依赖 SVG 力导向共现图（点击高亮邻居）
  · 用量：LLM 请求逐条明细、按天 token、按阶段累计调用（来自 /metrics）
  · 操作：任务队列、失败操作一键重试、排队任务取消
  · 配置：记忆库档案、directives、保留与合并参数（只读）
  · 官方界面：iframe 内嵌官方 Control Plane

用法:
    python3 hindsight-dashboard.py                       # http://127.0.0.1:8990
    python3 hindsight-dashboard.py --host 0.0.0.0        # 允许手机/局域网访问（建议配访问密钥）
    python3 hindsight-dashboard.py --api http://localhost:8888 --bank default

环境变量（可选）:
    HS_DASH_CONFIG_JSON   默认 ~/.hermes/hindsight/config.json（Hermes 的 Hindsight 插件配置，用作默认值）
    HS_DASH_KEY_FILE      默认 ~/.hermes/hindsight/access-key.txt（访问密钥文件）

安全模型:
    · 默认只监听 127.0.0.1；用 --host 0.0.0.0 暴露到局域网时，建议设置访问密钥。
    · 本机（loopback）免密；远程访问需要 ?k=<密钥> 或 Cookie。
    · 写操作（重试 / 反思 / 取消）默认只允许本机触发，可用 --allow-remote-write 放开。
"""

import argparse
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 可选：读取 Hermes 的 Hindsight 插件配置，用来推断默认的 API 地址与记忆库名
CONFIG_JSON = os.path.expanduser(os.getenv("HS_DASH_CONFIG_JSON", "~/.hermes/hindsight/config.json"))
ACCESS_KEY_FILE = os.path.expanduser(os.getenv("HS_DASH_KEY_FILE", "~/.hermes/hindsight/access-key.txt"))
KEY_COOKIE = "hs_dash_key"

DEFAULT_API = "http://localhost:8888"      # Hindsight API 默认端口
DEFAULT_CP = "http://localhost:9999"       # 官方 Control Plane 默认端口
DEFAULT_BANK = "default"

# 关键：绕过系统代理。若本机开着 ClashX 之类的代理，未显式绕过的 localhost 请求
# 会被代理接管并返回 502，表现为「面板连不上本地 API」。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_access_key():
    """读取远程访问密钥：文件不存在或为空时返回空串（即不启用密钥门）。

    密钥门只作用于非本机来源，因此本机使用体验不受影响。
    """
    try:
        with open(ACCESS_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:  # noqa: BLE001
        return ""


def load_local_config():
    api, bank = DEFAULT_API, DEFAULT_BANK
    try:
        with open(CONFIG_JSON, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("api_url"):
            api = cfg["api_url"].rstrip("/")
        if cfg.get("bank_id"):
            bank = cfg["bank_id"]
    except Exception:
        pass
    return api, bank


# ---------------------------------------------------------------------------
# API 通信
# ---------------------------------------------------------------------------
def _request(method, url, payload=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        return {"_error": f"HTTP {e.code}", "_body": body}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def api_get(api, path, params=None, timeout=30):
    url = api.rstrip("/") + path
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    return _request("GET", url, None, timeout)


def api_post(api, path, payload=None, timeout=120):
    return _request("POST", api.rstrip("/") + path, payload or {}, timeout)


def api_delete(api, path, timeout=30):
    return _request("DELETE", api.rstrip("/") + path, None, timeout)


# ---------------------------------------------------------------------------
# Prometheus 指标
# ---------------------------------------------------------------------------
METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9eE+\-.]+)$")
METRIC_LABEL_PRIORITY = ("scope", "task_type", "operation_type", "status", "method", "path", "model")


def parse_metrics(text, prefixes):
    """只挑有意义的 label 值拼短键（原始 label 串太长会把面板撑爆）。"""
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = METRIC_RE.match(line.strip())
        if not m:
            continue
        name, labels, val = m.group(1), m.group(2) or "", m.group(3)
        if not any(name.startswith(p) for p in prefixes):
            continue
        try:
            fval = float(val)
        except ValueError:
            continue
        lm = {}
        for part in labels.strip("{}").split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                lm[k.strip()] = v.strip().strip('"')
        parts = [lm[p] for p in METRIC_LABEL_PRIORITY if lm.get(p)]
        key = " · ".join(parts) if parts else "合计"
        out.setdefault(name, {})[key] = out.setdefault(name, {}).get(key, 0.0) + fval
    return out


def get_metrics(api):
    try:
        with _OPENER.open(api.rstrip("/") + "/metrics", timeout=15) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return {}
    return parse_metrics(text, (
        "hindsight_llm_tokens_", "hindsight_llm_calls_total",
        "hindsight_operation_operations_total", "hindsight_http_requests_total",
        "hindsight_process_memory_bytes", "hindsight_process_cpu_seconds",
        "hindsight_process_threads", "hindsight_db_pool_",
    ))


# 仅当面板监听在非回环地址（例如 --host 0.0.0.0）时，才在页面底部提示局域网访问地址。
# 默认只监听本机时该提示毫无意义，还会把本机网卡 IP 渲染进页面（截图/投屏时等于泄露内网拓扑）。
SHOW_LAN_HINT = False

_IPS_CACHE = {"t": 0.0, "ips": []}


def local_ips(max_age=60):
    """本机非环回 IPv4（局域网/VPN 网卡地址），带 60s 缓存。用于在页面上直接显示可供手机访问的地址。"""
    import subprocess
    import time
    now = time.time()
    if _IPS_CACHE["ips"] and now - _IPS_CACHE["t"] < max_age:
        return _IPS_CACHE["ips"]
    ips = []
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                ip = line.split()[1]
                if not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
    except Exception:  # noqa: BLE001
        try:
            import socket
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except Exception:  # noqa: BLE001
            pass
    _IPS_CACHE["t"] = now
    _IPS_CACHE["ips"] = ips
    return ips


def build_summary(api, bank, period="30d"):
    import datetime
    qb = urllib.parse.quote(bank)
    banks = api_get(api, "/v1/default/banks")
    return {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "api": api,
        "bank": bank,
        "lan_ips": local_ips() if SHOW_LAN_HINT else [],
        "health": api_get(api, "/health", timeout=8),
        "version": api_get(api, "/version", timeout=8),
        "banks": banks.get("banks", []) if isinstance(banks, dict) else [],
        "stats": api_get(api, f"/v1/default/banks/{qb}/stats"),
        "profile": api_get(api, f"/v1/default/banks/{qb}/profile", timeout=10),
        "timeseries": api_get(api, f"/v1/default/banks/{qb}/stats/memories-timeseries", {"period": period}),
        "llm_stats": api_get(api, f"/v1/default/banks/{qb}/llm-requests/stats", {"period": "30d"}),
        "ops_recent": api_get(api, f"/v1/default/banks/{qb}/operations", {"limit": 12, "exclude_parents": "true"}),
        "ops_failed": api_get(api, f"/v1/default/banks/{qb}/operations", {"status": "failed", "limit": 10}),
        "memories": api_get(api, f"/v1/default/banks/{qb}/memories/list", {"limit": 8}),
        "entities": api_get(api, f"/v1/default/banks/{qb}/entities", {"limit": 10}),
        "metrics": get_metrics(api),
    }


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hindsight 控制面板</title>
<style>
  :root{
    --bg:#0e1117; --panel:#161b24; --panel2:#1c2230; --line:#28303f;
    --fg:#e6ebf5; --muted:#8b97ab; --accent:#7c9cff; --ok:#43d19e;
    --warn:#f5b544; --bad:#f2637b; --obs:#c48bff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.55 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif}
  a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
  .wrap{max-width:1240px;margin:0 auto;padding:18px 20px 60px}
  header{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
  h1{font-size:18px;margin:0;font-weight:650;letter-spacing:.3px}
  .pill{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);
        background:var(--panel);color:var(--muted);white-space:nowrap}
  .pill.ok{color:var(--ok);border-color:#1e5c47}
  .pill.bad{color:var(--bad);border-color:#5c2033}
  .spacer{flex:1}
  button,select,input[type=text],input[type=number]{background:var(--panel2);color:var(--fg);
    border:1px solid var(--line);border-radius:8px;padding:6px 11px;font-size:13px;font-family:inherit}
  button{cursor:pointer}
  button:hover{border-color:var(--accent);color:#fff}
  button:disabled{opacity:.5;cursor:default}
  button.primary{background:#243056;border-color:#3a4c86}
  select{cursor:pointer}
  nav{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin-bottom:16px}
  nav button{background:transparent;border:none;border-bottom:2px solid transparent;border-radius:0;
    padding:8px 14px;color:var(--muted);font-size:13.5px}
  nav button:hover{color:var(--fg)}
  nav button.on{color:var(--fg);border-bottom-color:var(--accent);font-weight:600}
  .tab{display:none} .tab.on{display:block}
  .grid{display:grid;gap:12px}
  .cards{grid-template-columns:repeat(auto-fit,minmax(148px,1fr))}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 15px}
  .card .k{font-size:12px;color:var(--muted);margin-bottom:5px}
  .card .v{font-size:22px;font-weight:650;letter-spacing:.4px}
  .card .s{font-size:11.5px;color:var(--muted);margin-top:4px}
  section{background:var(--panel);border:1px solid var(--line);border-radius:12px;
          padding:15px 17px;margin-top:13px}
  section > h2{font-size:13.5px;margin:0 0 12px;font-weight:600}
  section > h2 span{color:var(--muted);font-weight:400;font-size:12px;margin-left:8px}
  .two{display:grid;grid-template-columns:1fr 1fr;gap:13px}
  .two.wide{grid-template-columns:1.35fr 1fr}
  @media(max-width:900px){.two,.two.wide{grid-template-columns:1fr}}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;color:var(--muted);font-weight:500;font-size:12px;padding:0 8px 8px 0;
     border-bottom:1px solid var(--line);white-space:nowrap}
  td{padding:7px 8px 7px 0;border-bottom:1px solid #1f2632;vertical-align:top}
  tr:last-child td{border-bottom:none}
  .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:6px;
       border:1px solid var(--line);color:var(--muted);white-space:nowrap}
  .tag.ok{color:var(--ok);border-color:#1e5c47}
  .tag.bad{color:var(--bad);border-color:#5c2033}
  .tag.warn{color:var(--warn);border-color:#5a4520}
  .tag.obs{color:var(--obs);border-color:#43305c}
  .tag.acc{color:var(--accent);border-color:#31406e}
  .mem{font-size:13px;line-height:1.6}
  .mem .meta{font-size:11.5px;color:var(--muted);margin-top:4px}
  .empty{color:var(--muted);font-size:13px;padding:8px 0}
  .bar{height:8px;border-radius:4px;background:var(--panel2);overflow:hidden}
  .bar > i{display:block;height:100%}
  .err{color:var(--bad);font-size:12px;word-break:break-all}
  code{background:var(--panel2);padding:1px 5px;border-radius:5px;font-size:12px}
  .legend{display:flex;gap:14px;font-size:12px;color:var(--muted);margin-bottom:6px;flex-wrap:wrap}
  .legend b{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .foot{margin-top:22px;font-size:12px;color:var(--muted)}
  .chips{display:flex;gap:6px;flex-wrap:wrap}
  .chip{font-size:11.5px;padding:2px 9px;border-radius:999px;background:var(--panel2);
        border:1px solid var(--line);color:var(--muted);cursor:pointer}
  .chip.on{background:#243056;border-color:#3a4c86;color:#fff}
  .score{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
  .answer{background:#141b2b;border:1px solid #2b3a5c;border-radius:10px;padding:12px 14px;
          font-size:13.5px;line-height:1.75;white-space:pre-wrap}
  .split{display:grid;grid-template-columns:300px 1fr;gap:13px}
  @media(max-width:900px){.split{grid-template-columns:1fr}}
  .list{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:10px}
  .list .it{padding:7px 11px;border-bottom:1px solid #1f2632;cursor:pointer;font-size:13px}
  .list .it:hover{background:var(--panel2)}
  .list .it.on{background:#243056}
  .list .it:last-child{border-bottom:none}
  .list .it .r1{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
  .list .it .r1 span:first-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .list .it .r1 span:last-child{color:var(--muted);font-variant-numeric:tabular-nums;flex:none}
  iframe{width:100%;height:78vh;border:1px solid var(--line);border-radius:12px;background:#fff}
  .spin{display:inline-block;width:12px;height:12px;border:2px solid var(--line);
        border-top-color:var(--accent);border-radius:50%;animation:sp .8s linear infinite;
        vertical-align:-1px;margin-right:6px}
  @keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Hindsight 控制面板</h1>
    <span class="pill" id="pBank">库 —</span>
    <span class="pill" id="pVer">API —</span>
    <span class="pill" id="pHealth">状态 —</span>
    <span class="spacer"></span>
    <a href="#" id="lnkCP">官方控制台 ↗</a>
    <a href="#" id="lnkDocs">Swagger ↗</a>
    <a href="/metrics-ui">metrics ↗</a>
    <button id="btnRefresh">刷新</button>
  </header>

  <div id="errBar" style="display:none;background:#3a1c26;border:1px solid #7a2b3f;color:#ffc9d4;
       border-radius:10px;padding:9px 13px;margin-bottom:13px;font-size:12.5px;word-break:break-all"></div>

  <nav id="nav">
    <button data-tab="overview" class="on">概览</button>
    <button data-tab="recall">检索试验</button>
    <button data-tab="memories">记忆</button>
    <button data-tab="graph">实体图谱</button>
    <button data-tab="usage">用量</button>
    <button data-tab="ops">操作诊断</button>
    <button data-tab="config">配置</button>
    <button data-tab="official">官方界面</button>
  </nav>

  <!-- 概览 -->
  <div class="tab on" id="tab-overview">
    <div class="grid cards" id="cards"></div>
    <section>
      <h2>记忆增长<span id="tsRange"></span></h2>
      <div class="legend" style="justify-content:space-between;align-items:center">
        <div class="legend" style="margin:0">
          <span><b style="background:#7c9cff"></b>world 客观事实</span>
          <span><b style="background:#43d19e"></b>experience 经验</span>
          <span><b style="background:#c48bff"></b>observation 提炼结论</span>
        </div>
        <div class="chips" id="periodChips">
          <span class="chip on" data-period="7d">7 天</span>
          <span class="chip" data-period="30d">30 天</span>
          <span class="chip" data-period="90d">90 天</span>
        </div>
      </div>
      <div id="chartTS"></div>
    </section>
    <div class="two wide">
      <section>
        <h2>最近记忆<span>点标签去「记忆」页搜索</span></h2>
        <div id="recentMems"></div>
      </section>
      <section>
        <h2>运行状态<span id="featList"></span></h2>
        <div id="runState"></div>
      </section>
    </div>
  </div>

  <!-- 检索试验 -->
  <div class="tab" id="tab-recall">
    <section>
      <h2>召回 / 反思<span>recall 走四路混合检索 · reflect 会调用 LLM 综合推理</span></h2>
      <div class="row" style="margin-bottom:12px">
        <input type="text" id="qRecall" style="flex:1;min-width:260px" placeholder="输入查询，例如：这个项目用的什么工具链？">
        <select id="selBudget"><option value="low">budget low</option><option value="mid" selected>mid</option><option value="high">high</option></select>
        <select id="selMode"><option value="recall">召回 recall</option><option value="reflect">反思 reflect</option></select>
        <button class="primary" id="btnRun">运行</button>
      </div>
      <div id="recallOut"><div class="empty">输入查询后点「运行」。召回返回打分明细；反思会给出综合答案与来源。</div></div>
    </section>
  </div>

  <!-- 记忆 -->
  <div class="tab" id="tab-memories">
    <section>
      <h2>记忆浏览<span id="memMeta"></span></h2>
      <div class="row" style="margin-bottom:10px">
        <input type="text" id="qMem" style="flex:1;min-width:240px" placeholder="全文搜索…">
        <span class="chips" id="typeChips">
          <span class="chip on" data-type="">全部</span>
          <span class="chip" data-type="world">world</span>
          <span class="chip" data-type="experience">experience</span>
          <span class="chip" data-type="observation">observation</span>
        </span>
        <button id="btnMemPrev">上一页</button>
        <button id="btnMemNext">下一页</button>
      </div>
      <div id="memList"></div>
    </section>
  </div>

  <!-- 实体图谱 -->
  <div class="tab" id="tab-graph">
    <div class="split">
      <section>
        <h2>实体<span id="entTotal"></span></h2>
        <input type="text" id="qEnt" placeholder="筛选实体名…" style="width:100%;margin-bottom:9px">
        <div class="list" id="entList"></div>
      </section>
      <section>
        <h2>共现图谱<span>Top 实体及其连接强度，点击左侧可高亮</span></h2>
        <div class="row" style="margin-bottom:8px">
          <span class="pill">图内节点 <b id="gNodeN">0</b></span>
          <span class="pill">图内连接 <b id="gEdgeN">0</b></span>
          <span class="pill">全库实体 <b id="gTotal">0</b></span>
        </div>
        <div id="graphBox"><div class="empty">加载中…</div></div>
      </section>
    </div>
  </div>

  <!-- 用量 -->
  <div class="tab" id="tab-usage">
    <div class="grid cards" id="usageCards"></div>
    <section>
      <h2>LLM 请求明细<span>每次抽取/合并/验证调用</span></h2>
      <div class="row" style="margin-bottom:10px">
        <span class="chips" id="llmStatusChips">
          <span class="chip on" data-status="">全部</span>
          <span class="chip" data-status="success">成功</span>
          <span class="chip" data-status="error">失败</span>
        </span>
        <button id="btnLlmPrev">上一页</button>
        <button id="btnLlmNext">下一页</button>
      </div>
      <div id="llmTable"></div>
    </section>
    <div class="two">
      <section><h2>按天 tokens</h2><div id="llmDaily"></div></section>
      <section><h2>累计调用（按阶段）</h2><div id="llmScope"></div></section>
    </div>
  </div>

  <!-- 操作诊断 -->
  <div class="tab" id="tab-ops">
    <div class="grid cards" id="opCards"></div>
    <section>
      <h2>失败的操作<span>重试会重新入队并触发 LLM 抽取</span></h2>
      <div id="failed"></div>
    </section>
    <section>
      <h2>任务队列<span>按状态筛选</span></h2>
      <div class="row" style="margin-bottom:10px">
        <span class="chips" id="opStatusChips">
          <span class="chip on" data-status="">全部</span>
          <span class="chip" data-status="pending">pending</span>
          <span class="chip" data-status="processing">processing</span>
          <span class="chip" data-status="completed">completed</span>
          <span class="chip" data-status="failed">failed</span>
        </span>
        <button id="btnOpPrev">上一页</button><button id="btnOpNext">下一页</button>
      </div>
      <div id="opsTable"></div>
    </section>
  </div>

  <!-- 配置 -->
  <div class="tab" id="tab-config">
    <div class="two">
      <section><h2>记忆库档案<span>mission / disposition 影响 reflect</span></h2><div id="cfgProfile"></div></section>
      <section><h2>指令 Directives</h2><div id="cfgDirectives"></div></section>
    </div>
    <section><h2>保留与合并参数<span>只读展示，改配置请用官方界面</span></h2><div id="cfgRetain"></div></section>
  </div>

  <!-- 官方界面 -->
  <div class="tab" id="tab-official">
    <section style="padding:12px">
      <div class="row" style="margin-bottom:10px">
        <span class="pill">官方 Hindsight Control Plane</span>
        <a href="#" id="lnkCP2">在新窗口打开 ↗</a>
        <span class="muted" style="font-size:12px;color:var(--muted)">深度配置（Webhooks / 审计日志 / 逐条 trace）在官方界面更完整；本面板只读代理。</span>
      </div>
      <iframe id="cpFrame" src="about:blank"></iframe>
    </section>
  </div>

  <div class="foot">
    数据源 <code id="fApi"></code> · 最后刷新 <span id="fTs">—</span> ·
    本机可读写（重试/取消/反思）；<b>局域网与手机为只读</b>，防止同网段误点消耗 token
    <div id="fNet" style="margin-top:6px"></div>
  </div>
</div>

<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => (s==null?'':String(s)).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const nf = (n) => (typeof n==='number'? n.toLocaleString('en-US') : (n==null?'—':String(n)));
const kf = (n) => n>=1e6 ? (n/1e6).toFixed(2)+'M' : n>=1e3 ? (n/1e3).toFixed(1)+'k' : String(n);
const shortTime = (s) => s ? s.replace('T',' ').replace('+00:00','').slice(0,16) : '—';
const dur = (ms) => ms==null ? '—' : ms<1000 ? ms+'ms' : (ms/1000).toFixed(1)+'s';
const card = (k,v,s) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s||''}</div></div>`;
function typeTag(t){ const c = t==='observation'?'obs':(t==='experience'?'ok':'acc'); return `<span class="tag ${c}">${esc(t||'?')}</span>`; }

let BANK='', PERIOD='7d', CP_URL='http://localhost:9999', DATA=null;
let memState={q:'',type:'',offset:0,limit:20,total:0};
let opState={status:'',offset:0,limit:20,total:0};
let llmState={status:'',offset:0,limit:20,total:0};
let entCache=[];

async function jget(url){
  const r = await fetch(url);
  const t = await r.text();
  try { return normalize(JSON.parse(t)); } catch(e){ return {_error:'解析失败', _body:t.slice(0,200)}; }
}
async function jpost(url, body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
  const t = await r.text();
  try { return normalize(JSON.parse(t)); } catch(e){ return {_error:'解析失败', _body:t.slice(0,200)}; }
}
/* 服务端拒绝（403/404）返回 {"detail": "..."}，统一转成 _error 才能正确提示，否则会被当成成功 */
function normalize(d){
  if(d && typeof d==='object' && !d._error && typeof d.detail==='string' && Object.keys(d).length===1){
    d._error = d.detail;
  }
  return d;
}
/* 全局错误横幅：JS 抛错时不再静默留白，直接写明出错位置 */
window.addEventListener('error', (e)=>{
  const bar = document.getElementById('errBar'); if(!bar) return;
  bar.style.display='block';
  bar.textContent = '页面脚本出错：' + (e.message||'') + ' @ ' + String(e.filename||'').split('/').pop() + ':' + (e.lineno||'');
});
window.addEventListener('unhandledrejection', (e)=>{
  const bar = document.getElementById('errBar'); if(!bar) return;
  bar.style.display='block';
  bar.textContent = '异步请求出错：' + String((e.reason && (e.reason.message||e.reason)) || '');
});
const B = () => 'bank='+encodeURIComponent(BANK);

/* ---------------- 概览 ---------------- */
function cpBase(){
  // 官方控制台地址随访问主机变化：本机打开用 localhost，其他设备用当前访问用的主机名/IP。
  // 官方 CP 监听 0.0.0.0:9999，所以两种都能直接连上（写死 localhost 会让手机端 iframe 打不开）。
  const host = location.hostname || 'localhost';
  return 'http://' + host + ':9999';
}
function renderHeader(d){
  BANK = d.bank || BANK;
  $('#pBank').textContent = '库 ' + BANK;
  $('#pVer').textContent = 'API ' + ((d.version||{}).api_version||'?');
  const ok = (d.health||{}).status === 'healthy';
  $('#pHealth').textContent = ok ? 'healthy' : '异常';
  $('#pHealth').className = 'pill ' + (ok?'ok':'bad');
  $('#fApi').textContent = d.api; $('#fTs').textContent = shortTime(d.ts);
  const cp = cpBase(); CP_URL = cp;
  $('#lnkCP').href = cp; $('#lnkCP2').href = cp;
  // Swagger 也要跟着访问主机走（API 监听 0.0.0.0:8988），否则手机点开会是手机自己的 localhost
  $('#lnkDocs').href = 'http://' + (location.hostname||'localhost') + ':8988/docs';
  const f = (d.version||{}).features||{};
  const on = Object.entries(f).filter(([,v])=>v).map(([k])=>k);
  $('#featList').textContent = ' 已启用：' + (on.join(' · ') || '—');
  // 手机/其他设备访问地址（服务端探测本机网卡，列出全部非环回 IPv4）
  const ips = d.lan_ips || [];
  $('#fNet').innerHTML = ips.length
    ? '手机访问（同一 Wi-Fi）：' + ips.map(ip=>`<code>http://${esc(ip)}:8990</code>`).join(' / ') + ' · 官方界面同 IP 的 :9999'
    : '';
}
function tokenTotals(d){
  const b = (d.llm_stats||{}).buckets||[];
  const t = {input:0, output:0, cached:0, total:0};
  b.forEach(x=>{ const tk=x.tokens||{}; t.input+=tk.input||0; t.output+=tk.output||0; t.cached+=tk.cached||0; t.total+=tk.total||0; });
  return t;
}
function renderCards(d){
  const st=d.stats||{}, ts=d.timeseries||{}, buckets=ts.buckets||[];
  const last7 = buckets.slice(-7).reduce((a,b)=>a+(b.world||0)+(b.experience||0)+(b.observation||0),0);
  const tok = tokenTotals(d);
  $('#cards').innerHTML = [
    card('记忆总数', nf(st.total_nodes), `文档 ${nf(st.total_documents)} · 结论 ${nf(st.total_observations)}`),
    card('关系链接', nf(st.total_links), Object.entries(st.links_by_link_type||{}).map(([k,v])=>`${k} ${v}`).join(' · ')),
    card('实体节点', nf((d.entities||{}).total), '知识图谱'),
    card('近 7 天新增', nf(last7), 'world+exp+obs'),
    card('LLM Tokens', kf(tok.total), `入 ${kf(tok.input)} · 出 ${kf(tok.output)}`),
    card('待处理 / 失败', `${nf(st.pending_operations)} / ${nf(st.failed_operations)}`, `已完成 ${nf((st.operations_by_status||{}).completed)}`),
  ].join('');
  const parts = Object.entries((st.nodes_by_fact_type)||{}).map(([k,v])=>`${k} ${v}`).join(' · ');
  $('#tsRange').textContent = ` 近 ${ts.period||PERIOD} · ${parts}`;
}
function renderChart(d){
  const b=(d.timeseries||{}).buckets||[];
  if(!b.length){ $('#chartTS').innerHTML='<div class="empty">暂无数据</div>'; return; }
  const W=1100,H=190,pad=34,n=b.length,bw=Math.max(5,Math.min(46,(W-pad*2)/n-8));
  const every=Math.max(1,Math.ceil(n/12));
  const max=Math.max(1,...b.map(x=>(x.world||0)+(x.experience||0)+(x.observation||0)));
  const cols={world:'#7c9cff',experience:'#43d19e',observation:'#c48bff'};
  let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="none"><line x1="${pad}" y1="${H-24}" x2="${W-10}" y2="${H-24}" stroke="#28303f"/>`;
  b.forEach((x,i)=>{
    let cx=pad+i*((W-pad*2)/n)+((W-pad*2)/n-bw)/2, y=H-24;
    ['world','experience','observation'].forEach(k=>{
      const v=x[k]||0; if(!v) return;
      const h=Math.max(1,(v/max)*(H-60)); y-=h;
      s+=`<rect x="${cx}" y="${y}" width="${bw}" height="${h}" fill="${cols[k]}" rx="2"/>`;
    });
    const tot=(x.world||0)+(x.experience||0)+(x.observation||0);
    if(tot>0&&n<=31) s+=`<text x="${cx+bw/2}" y="${y-5}" fill="#8b97ab" font-size="11" text-anchor="middle">${tot}</text>`;
    if(i%every===0) s+=`<text x="${cx+bw/2}" y="${H-8}" fill="#8b97ab" font-size="11" text-anchor="middle">${esc((x.time||'').slice(5,10))}</text>`;
  });
  $('#chartTS').innerHTML = s+'</svg>';
}
function memHTML(items){
  if(!items||!items.length) return '<div class="empty">没有匹配的记忆</div>';
  return items.map(m=>`<div style="padding:8px 0;border-bottom:1px solid #1f2632">
      <div class="mem">${esc((m.text||'').slice(0,420))}</div>
      <div class="meta">${typeTag(m.fact_type||m.type)} ${esc(shortTime(m.mentioned_at||m.date))}
        ${m.proof_count?` · 证据 ${m.proof_count}`:''}${m.context?` · ${esc(m.context)}`:''}
        ${m.document_id?` · doc ${esc(String(m.document_id).slice(0,20))}`:''}</div></div>`).join('');
}
function renderRunState(d){
  const st=d.stats||{}, m=d.metrics||{};
  const mem=firstVal(m['hindsight_process_memory_bytes']), cpu=firstVal(m['hindsight_process_cpu_seconds']);
  const pool=Object.entries(m['hindsight_db_pool_size']||{}).map(([k,v])=>`${k} ${v}`).join(' ');
  $('#runState').innerHTML = `<table>
    <tr><td>数据库</td><td class="num">${esc((d.health||{}).database||'—')}</td></tr>
    <tr><td>上次合并</td><td class="num">${esc(shortTime(st.last_consolidated_at))}</td></tr>
    <tr><td>待合并 / 合并失败</td><td class="num">${nf(st.pending_consolidation)} / ${nf(st.failed_consolidation)}</td></tr>
    <tr><td>进程内存 / CPU</td><td class="num">${mem!=null?(mem/1048576).toFixed(0)+' MB':'—'} / ${cpu!=null?cpu.toFixed(0)+'s':'—'}</td></tr>
    <tr><td>DB 连接池</td><td class="num">${esc(pool||'—')}</td></tr>
    <tr><td>接口地址</td><td class="num">${esc(d.api)}</td></tr></table>`;
}
function firstVal(o){ if(!o) return null; const v=Object.values(o); return v.length?v[0]:null; }

/* ---------------- 检索试验 ---------------- */
async function runQuery(){
  const q = $('#qRecall').value.trim(); if(!q) return;
  const mode = $('#selMode').value, budget = $('#selBudget').value;
  $('#btnRun').disabled = true;
  $('#recallOut').innerHTML = `<div class="empty"><span class="spin"></span>${mode==='reflect'?'LLM 正在综合记忆…（可能 20-60s）':'检索中…'}</div>`;
  const r = mode==='reflect'
    ? await jpost('/api/reflect?'+B(), {query:q, budget})
    : await jpost('/api/recall?'+B(), {query:q, budget, trace:true});
  $('#btnRun').disabled = false;
  if(r._error){ $('#recallOut').innerHTML = `<div class="err">调用失败：${esc(r._error)} ${esc(r._body||'')}</div>`; return; }
  if(mode==='reflect'){
    const ans = r.answer || r.response || r.text || JSON.stringify(r).slice(0,600);
    const src = r.sources || r.memories || r.evidence || [];
    $('#recallOut').innerHTML = `<div class="answer">${esc(ans)}</div>
      ${src.length?`<div class="legend" style="margin-top:12px">来源 ${src.length} 条</div>
      ${src.map(s=>`<div style="padding:6px 0;border-bottom:1px solid #1f2632"><div class="mem">${esc((s.text||s.content||'').slice(0,240))}</div>
        <div class="meta" style="font-size:11.5px;color:var(--muted)">${typeTag(s.type||s.fact_type)} ${esc(shortTime(s.mentioned_at||s.date))}</div></div>`).join('')}`:''}`;
    return;
  }
  const items = r.results || r.items || [];
  if(!items.length){ $('#recallOut').innerHTML = '<div class="empty">没有召回结果</div>'; return; }
  $('#recallOut').innerHTML = `<div class="legend">命中 ${items.length} 条${r.trace_id?` · trace ${esc(String(r.trace_id).slice(0,8))}`:''}</div>` +
    items.map(x=>{
      const sc = x.scores||{};
      return `<div style="padding:9px 0;border-bottom:1px solid #1f2632">
        <div class="mem">${esc((x.text||'').slice(0,400))}</div>
        <div class="meta" style="font-size:11.5px;color:var(--muted)">
          ${typeTag(x.type||x.fact_type)} ${esc(shortTime(x.mentioned_at||x.date))}
          <span class="score">final ${(sc.final??0).toFixed(3)} · 语义 ${(sc.semantic??0).toFixed(3)} · 重排 ${(sc.reranker??0).toFixed(3)}</span>
          ${(x.entities||[]).length?`<br>${x.entities.slice(0,8).map(e=>`<span class="chip" style="cursor:default">${esc(e)}</span>`).join(' ')}`:''}
        </div></div>`;
    }).join('');
}

/* ---------------- 记忆 ---------------- */
function memQuery(){
  const q = 'bank='+encodeURIComponent(BANK)+'&q='+encodeURIComponent(memState.q)+'&type='+memState.type+
            '&limit='+memState.limit+'&offset='+memState.offset;
  return q;
}
async function loadMemories(){
  const d = await jget('/api/memories?'+memQuery());
  if(d._error){ $('#memList').innerHTML=`<div class="err">${esc(d._error)}</div>`; return; }
  memState.total = d.total||0;
  $('#memMeta').textContent = ` 共 ${nf(memState.total)} 条 · 第 ${Math.floor(memState.offset/memState.limit)+1} 页`;
  $('#memList').innerHTML = memHTML(d.items);
}
/* ---------------- 实体图谱 ---------------- */
async function loadEntities(){
  const q = $('#qEnt').value.trim().toLowerCase();
  const d = await jget('/api/entities?bank='+encodeURIComponent(BANK)+'&limit=200');
  const items = d.items||[];
  entCache = items;
  $('#entTotal').textContent = ` 共 ${nf(d.total)} 个（显示前 ${items.length}）`;
  const show = q ? items.filter(e=>String(e.canonical_name).toLowerCase().includes(q)) : items;
  const max = Math.max(1,...show.map(e=>e.mention_count||0));
  $('#gTotal').textContent = nf(d.total);   // 全库实体数来自 /entities，不是图谱返回的 total_entities
  $('#entList').innerHTML = show.length ? show.map(e=>`
    <div class="it" data-ent="${esc(e.id)}" data-name="${esc(e.canonical_name)}">
      <div class="r1"><span>${esc(e.canonical_name)}</span><span>${nf(e.mention_count)}</span></div>
      <div class="bar" style="margin-top:4px"><i style="width:${Math.round((e.mention_count||0)/max*100)}%;background:#43d19e"></i></div>
    </div>`).join('') : '<div class="empty" style="padding:10px">无匹配</div>';
  document.querySelectorAll('#entList .it').forEach(el=>{
    el.onclick = ()=>{ document.querySelectorAll('#entList .it').forEach(x=>x.classList.remove('on')); el.classList.add('on'); highlightNode(el.dataset.ent); };
  });
}
let GRAPH={nodes:[],edges:[],pos:{}};
async function loadGraph(){
  const g = await jget('/api/graph?bank='+encodeURIComponent(BANK)+'&limit=80');
  if(g._error){ $('#graphBox').innerHTML=`<div class="err">${esc(g._error)}</div>`; return; }
  GRAPH.nodes = (g.nodes||[]).map(n=>n.data); GRAPH.edges = (g.edges||[]).map(e=>e.data);
  $('#gNodeN').textContent = GRAPH.nodes.length; $('#gEdgeN').textContent = GRAPH.edges.length;
  drawGraph();
}
function layoutGraph(W,H){
  const n = GRAPH.nodes, e = GRAPH.edges;
  const pos = {};
  n.forEach((node,i)=>{ const a=2*Math.PI*i/Math.max(1,n.length); pos[node.id]={x:W/2+Math.cos(a)*W*0.32, y:H/2+Math.sin(a)*H*0.32, vx:0, vy:0}; });
  for(let it=0; it<260; it++){
    for(let i=0;i<n.length;i++){
      for(let j=i+1;j<n.length;j++){
        const a=pos[n[i].id], b=pos[n[j].id];
        let dx=b.x-a.x, dy=b.y-a.y, d2=dx*dx+dy*dy||1, d=Math.sqrt(d2);
        const f = 4200/d2; const fx=dx/d*f, fy=dy/d*f;
        a.vx-=fx; a.vy-=fy; b.vx+=fx; b.vy+=fy;
      }
    }
    e.forEach(edge=>{
      const a=pos[edge.source], b=pos[edge.target]; if(!a||!b) return;
      let dx=b.x-a.x, dy=b.y-a.y, d=Math.sqrt(dx*dx+dy*dy)||1;
      const k = 0.0016*Math.min(3, Math.log10((edge.weight||1)+10));
      const fx=dx*k, fy=dy*k;
      a.vx+=fx; a.vy+=fy; b.vx-=fx; b.vy-=fy;
    });
    n.forEach(node=>{
      const p=pos[node.id];
      p.vx += (W/2-p.x)*0.0012; p.vy += (H/2-p.y)*0.0012;
      p.vx*=0.86; p.vy*=0.86;
      p.x=Math.max(28,Math.min(W-28,p.x+p.vx)); p.y=Math.max(20,Math.min(H-20,p.y+p.vy));
    });
  }
  return pos;
}
function drawGraph(){
  const W=780,H=470;
  if(!GRAPH.nodes.length){ $('#graphBox').innerHTML='<div class="empty">暂无实体</div>'; return; }
  const pos = layoutGraph(W,H); GRAPH.pos = pos;
  const maxM = Math.max(1,...GRAPH.nodes.map(n=>n.mentionCount||0));
  let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" style="background:#121620;border-radius:10px">`;
  GRAPH.edges.forEach(e=>{
    const a=pos[e.source], b=pos[e.target]; if(!a||!b) return;
    const w = Math.min(4, 0.6+Math.log10((e.weight||1)+1));
    s+=`<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" stroke="#3a4a66" stroke-width="${w}" opacity="0.75"/>`;
  });
  GRAPH.nodes.forEach(n=>{
    const p=pos[n.id]; const r=6+14*Math.sqrt((n.mentionCount||1)/maxM);
    s+=`<circle class="gnode" data-id="${esc(n.id)}" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r.toFixed(1)}"
         fill="#2b3a5c" stroke="#7c9cff" stroke-width="1.6"/>`;
    s+=`<text x="${p.x.toFixed(1)}" y="${(p.y-r-5).toFixed(1)}" fill="#c9d4e8" font-size="11.5" text-anchor="middle">${esc(n.label)}</text>`;
  });
  $('#graphBox').innerHTML = s+'</svg>';
  document.querySelectorAll('.gnode').forEach(c=>{
    c.style.cursor='pointer';
    c.onclick = ()=>highlightNode(c.dataset.id);
  });
}
function highlightNode(id){
  document.querySelectorAll('.gnode').forEach(c=>{
    const on = c.dataset.id===id;
    const linked = GRAPH.edges.some(e=>(e.source===id&&e.target===c.dataset.id)||(e.target===id&&e.source===c.dataset.id));
    c.setAttribute('stroke', on?'#43d19e':(linked?'#f5b544':'#7c9cff'));
    c.setAttribute('stroke-width', on?'3.4':(linked?'2.4':'1.6'));
    c.setAttribute('fill', on?'#1e5c47':(linked?'#3a3520':'#2b3a5c'));
  });
}
/* ---------------- 用量 ---------------- */
function renderUsage(d){
  const t = tokenTotals(d), m = d.metrics||{};
  const calls = Object.values(m['hindsight_llm_calls_total']||{}).reduce((a,b)=>a+b,0);
  $('#usageCards').innerHTML = [
    card('近 30 天 tokens', kf(t.total), `入 ${nf(t.input)} · 出 ${nf(t.output)}`),
    card('命中缓存', kf(t.cached), t.total?`命中率 ${(t.cached/t.total*100).toFixed(1)}%`:'—'),
    card('累计 LLM 调用', nf(calls), '进程启动至今'),
    card('内存占用', firstVal(m['hindsight_process_memory_bytes'])!=null?((firstVal(m['hindsight_process_memory_bytes'])/1048576).toFixed(0)+' MB'):'—',
         `线程 ${firstVal(m['hindsight_process_threads'])??'—'}`),
  ].join('');
  const b=(d.llm_stats||{}).buckets||[];
  const max=Math.max(1,...b.map(x=>(x.tokens||{}).total||0));
  $('#llmDaily').innerHTML = b.length ? b.map(x=>{
    const v=(x.tokens||{}).total||0;
    return `<div style="margin:6px 0"><div style="display:flex;justify-content:space-between;font-size:12px;color:#8b97ab">
      <span>${esc((x.time||'').slice(0,10))} · ${nf(x.total)} 次</span><span>${nf(v)} tok</span></div>
      <div class="bar"><i style="width:${Math.round(v/max*100)}%;background:#7c9cff"></i></div></div>`;
  }).join('') : '<div class="empty">暂无数据</div>';
  const sc = Object.entries(m['hindsight_llm_calls_total']||{}).sort((a,b)=>b[1]-a[1]);
  const maxc = Math.max(1,...sc.map(x=>x[1]));
  $('#llmScope').innerHTML = sc.length ? sc.map(([k,v])=>`<div style="margin:6px 0">
      <div style="display:flex;justify-content:space-between;font-size:12px"><span>${esc(k)}</span><span style="color:#8b97ab">${nf(v)}</span></div>
      <div class="bar"><i style="width:${Math.round(v/maxc*100)}%;background:#43d19e"></i></div></div>`).join('') : '<div class="empty">暂无</div>';
}
async function loadLLM(){
  const d = await jget('/api/llm-requests?bank='+encodeURIComponent(BANK)+'&status='+llmState.status+'&limit='+llmState.limit+'&offset='+llmState.offset);
  if(d._error){ $('#llmTable').innerHTML=`<div class="err">${esc(d._error)}</div>`; return; }
  llmState.total=d.total||0;
  const items=d.items||[];
  $('#llmTable').innerHTML = items.length ? `<div class="legend">共 ${nf(llmState.total)} 条 · 第 ${Math.floor(llmState.offset/llmState.limit)+1} 页</div>
    <table><tr><th>时间</th><th>阶段</th><th>模型</th><th class="num">耗时</th><th class="num">入/出 tok</th><th>状态</th></tr>` +
    items.map(x=>`<tr><td style="color:#8b97ab;white-space:nowrap">${esc(shortTime(x.started_at))}</td>
      <td>${esc(x.operation||x.scope||'')}</td><td style="color:#8b97ab">${esc(x.model||'')}</td>
      <td class="num">${dur(x.duration_ms)}</td>
      <td class="num">${nf(x.input_tokens)} / ${nf(x.output_tokens)}</td>
      <td><span class="tag ${x.status==='success'?'ok':'bad'}">${esc(x.status)}</span></td></tr>`).join('')+'</table>'
    : '<div class="empty">没有记录</div>';
}
/* ---------------- 操作 ---------------- */
function renderOps(d){
  const st=d.stats||{}, by=st.operations_by_status||{};
  $('#opCards').innerHTML = [card('已完成',nf(by.completed)),card('失败',nf(by.failed)),
    card('排队中',nf(st.pending_operations)),card('合并失败',nf(st.failed_consolidation))].join('');
  const ops=((d.ops_failed||{}).operations)||[];
  $('#failed').innerHTML = ops.length ? ops.map(o=>`
    <div style="padding:8px 0;border-bottom:1px solid #1f2632">
      <div class="row" style="justify-content:space-between">
        <span>${esc(o.task_type)} <span class="tag bad">失败</span> <span style="color:#8b97ab;font-size:12px">${esc(shortTime(o.created_at))} · 重试 ${o.retry_count||0} 次</span></span>
        <button data-retry="${esc(o.id)}">重试</button>
      </div>
      <div class="err" style="margin-top:4px">${esc((o.error_message||'').slice(0,220))}</div>
    </div>`).join('') : '<div class="empty">没有失败的操作，记忆流水线健康呢~</div>';
  document.querySelectorAll('[data-retry]').forEach(btn=>{
    btn.onclick = async ()=>{
      if(!confirm('重新入队这个操作？会触发一次 LLM 抽取（有 token 开销）。')) return;
      btn.disabled=true; btn.textContent='…';
      const j = await jpost('/api/retry?'+B()+'&op='+encodeURIComponent(btn.dataset.retry));
      btn.textContent = j._error ? '失败' : '已入队';
      setTimeout(loadOps, 1500);
    };
  });
}
async function loadOps(){
  const d = await jget('/api/ops?bank='+encodeURIComponent(BANK)+'&status='+opState.status+'&limit='+opState.limit+'&offset='+opState.offset);
  if(d._error){ $('#opsTable').innerHTML=`<div class="err">${esc(d._error)}</div>`; return; }
  opState.total=d.total||0;
  const ops=d.operations||[];
  $('#opsTable').innerHTML = ops.length ? `<div class="legend">共 ${nf(opState.total)} 条 · 第 ${Math.floor(opState.offset/opState.limit)+1} 页</div>
    <table><tr><th>类型</th><th>状态</th><th>创建</th><th>耗时</th><th class="num">项</th><th></th></tr>` +
    ops.map(o=>{
      const cls = o.status==='completed'?'ok':(o.status==='failed'?'bad':'warn');
      const ms = o.created_at&&o.updated_at ? (new Date(o.updated_at)-new Date(o.created_at)) : null;
      return `<tr><td>${esc(o.task_type)}</td><td><span class="tag ${cls}">${esc(o.status)}</span></td>
        <td style="color:#8b97ab">${esc(shortTime(o.created_at))}</td><td>${dur(ms)}</td>
        <td class="num">${nf(o.items_count)}</td>
        <td class="num">${o.status==='failed'?`<button data-retry2="${esc(o.id)}">重试</button>`:''}
        ${o.status==='pending'?`<button data-cancel="${esc(o.id)}">取消</button>`:''}</td></tr>`;}).join('')+'</table>'
    : '<div class="empty">没有任务</div>';
  document.querySelectorAll('[data-retry2]').forEach(b=>b.onclick=async()=>{
    if(!confirm('重新入队？会触发 LLM 抽取。')) return; b.disabled=true;
    await jpost('/api/retry?'+B()+'&op='+encodeURIComponent(b.dataset.retry2)); setTimeout(loadOps,1200);
  });
  document.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=async()=>{
    if(!confirm('取消这个排队中的任务？')) return; b.disabled=true;
    await jget('/api/cancel?'+B()+'&op='+encodeURIComponent(b.dataset.cancel)); setTimeout(loadOps,1200);
  });
}
/* ---------------- 配置 ---------------- */
async function loadConfig(){
  if(!DATA) await loadSummary();                 // 深链接直进本页时 DATA 可能还是 null，先补上
  const d = await jget('/api/config?'+B());
  if(d._error){ $('#cfgProfile').innerHTML = `<div class="err">配置读取失败：${esc(d._error)}</div>`; return; }
  const p = d.profile||{}, cfg=d.config||{};
  const banks = (DATA && DATA.banks) || [];      // 修改原因: DATA 为 null 时这里抛异常会让下面三块全部空白
  const me = banks.find(b=>b.bank_id===BANK) || {};
  $('#cfgProfile').innerHTML = `<table>
    <tr><td>库名</td><td class="num">${esc(p.name||BANK)}</td></tr>
    <tr><td>mission</td><td class="num">${esc(p.mission||'（未设置）')}</td></tr>
    <tr><td>background</td><td class="num">${esc((p.background||'（未设置）').slice(0,200))}</td></tr>
    <tr><td>怀疑 / 字面 / 共情</td><td class="num">${['skepticism','literalism','empathy'].map(k=>`${k.slice(0,4)} ${(p.disposition||{})[k]??'—'}`).join(' · ')}</td></tr>
    <tr><td>记忆条数</td><td class="num">${nf(me.fact_count)}</td></tr>
    <tr><td>创建 / 最后写入</td><td class="num">${esc(shortTime(me.created_at))} / ${esc(shortTime(me.last_document_at))}</td></tr></table>
    <div class="legend" style="margin-top:10px">mission 与 disposition 只影响 reflect 的推理风格，不改变 recall。</div>`;
  const dirs = d.directives||[];
  $('#cfgDirectives').innerHTML = dirs.length ? dirs.map(x=>`<div style="padding:6px 0;border-bottom:1px solid #1f2632">
      <div>${esc(x.text||x.content||JSON.stringify(x))}</div>
      <div style="font-size:11.5px;color:var(--muted)">${x.active===false?'已停用':'生效中'} ${esc(shortTime(x.created_at))}</div></div>`).join('')
    : '<div class="empty">没有指令。指令是硬规则，比如「永远引用来源」，在官方界面可以添加。</div>';
  const keys = ['retain_chunk_size','retain_extraction_mode','retain_chunk_batch_size','enable_observations','enable_auto_consolidation',
    'consolidation_max_memories_per_round','consolidation_llm_batch_size','consolidation_llm_parallelism'];
  $('#cfgRetain').innerHTML = '<table>'+keys.map(k=>`<tr><td><code>${k}</code></td><td class="num">${esc(cfg[k]===null||cfg[k]===undefined?'—':String(cfg[k]))}</td></tr>`).join('')+'</table>';
}
/* ---------------- 主流程 ---------------- */
async function loadSummary(){
  $('#btnRefresh').disabled = true;
  const d = await jget('/api/summary?'+B()+'&period='+PERIOD);
  $('#btnRefresh').disabled = false;
  if(d._error){ $('#pHealth').textContent='Hindsight 未响应'; $('#pHealth').className='pill bad'; return; }
  DATA=d; renderHeader(d); renderCards(d); renderChart(d);
  $('#recentMems').innerHTML = memHTML(((d.memories||{}).items)||[]);
  renderOps(d); renderUsage(d); renderRunState(d);
}
async function loadTab(name){
  if(!DATA || name==='overview') await loadSummary();   // 保证依赖 DATA 的页面（如配置页）有数据
  if(name==='memories') await loadMemories();
  if(name==='graph'){ await loadEntities(); await loadGraph(); }
  if(name==='usage') await loadLLM();
  if(name==='ops') await loadOps();
  if(name==='config') await loadConfig();
  if(name==='official'){ const f=$('#cpFrame'); if(!f.src || f.src==='about:blank') f.src = CP_URL+'/dashboard'; }
}
function activateTab(name){
  const btn = document.querySelector(`#nav button[data-tab="${name}"]`);
  if(!btn) return;
  document.querySelectorAll('#nav button').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  btn.classList.add('on'); $('#tab-'+name).classList.add('on');
  if(location.hash !== '#'+name) history.replaceState(null,'','#'+name);
  loadTab(name);
}
document.querySelectorAll('#nav button').forEach(b=>{
  b.onclick = ()=>activateTab(b.dataset.tab);
});
window.addEventListener('hashchange', ()=>activateTab(location.hash.replace('#','')));
document.querySelectorAll('#periodChips .chip').forEach(c=>c.onclick=()=>{
  document.querySelectorAll('#periodChips .chip').forEach(x=>x.classList.remove('on'));
  c.classList.add('on'); PERIOD=c.dataset.period; loadSummary();
});
document.querySelectorAll('#typeChips .chip').forEach(c=>c.onclick=()=>{
  document.querySelectorAll('#typeChips .chip').forEach(x=>x.classList.remove('on'));
  c.classList.add('on'); memState.type=c.dataset.type; memState.offset=0; loadMemories();
});
document.querySelectorAll('#opStatusChips .chip').forEach(c=>c.onclick=()=>{
  document.querySelectorAll('#opStatusChips .chip').forEach(x=>x.classList.remove('on'));
  c.classList.add('on'); opState.status=c.dataset.status; opState.offset=0; loadOps();
});
document.querySelectorAll('#llmStatusChips .chip').forEach(c=>c.onclick=()=>{
  document.querySelectorAll('#llmStatusChips .chip').forEach(x=>x.classList.remove('on'));
  c.classList.add('on'); llmState.status=c.dataset.status; llmState.offset=0; loadLLM();
});
$('#btnMemPrev').onclick = ()=>{ memState.offset=Math.max(0,memState.offset-memState.limit); loadMemories(); };
$('#btnMemNext').onclick = ()=>{ if(memState.offset+memState.limit<memState.total){ memState.offset+=memState.limit; loadMemories(); } };
$('#btnOpPrev').onclick = ()=>{ opState.offset=Math.max(0,opState.offset-opState.limit); loadOps(); };
$('#btnOpNext').onclick = ()=>{ if(opState.offset+opState.limit<opState.total){ opState.offset+=opState.limit; loadOps(); } };
$('#btnLlmPrev').onclick = ()=>{ llmState.offset=Math.max(0,llmState.offset-llmState.limit); loadLLM(); };
$('#btnLlmNext').onclick = ()=>{ if(llmState.offset+llmState.limit<llmState.total){ llmState.offset+=llmState.limit; loadLLM(); } };
let memTimer=null, entTimer=null;
$('#qMem').addEventListener('input', e=>{ clearTimeout(memTimer); memTimer=setTimeout(()=>{ memState.q=e.target.value.trim(); memState.offset=0; loadMemories(); }, 350); });
$('#qEnt').addEventListener('input', ()=>{ clearTimeout(entTimer); entTimer=setTimeout(loadEntities, 200); });
$('#btnRun').onclick = runQuery;
$('#qRecall').addEventListener('keydown', e=>{ if(e.key==='Enter') runQuery(); });
$('#btnRefresh').onclick = ()=>loadSummary();
const initHash = location.hash.replace('#','');
loadSummary();                                  // 头部/概览数据始终加载，深链接进来也不会空白
if(initHash && initHash!=='overview') activateTab(initHash);
/* 支持 /?q=关键词#recall 直接带查询运行（便于脚本化/分享） */
const _qs = new URLSearchParams(location.search);
if(_qs.get('q')){
  if(!initHash) activateTab('recall'); else if(initHash!=='recall') activateTab('recall');
  $('#qRecall').value = _qs.get('q');
  if(_qs.get('mode')) $('#selMode').value = _qs.get('mode');
  if(_qs.get('budget')) $('#selBudget').value = _qs.get('budget');
  runQuery();
}
setInterval(loadSummary, 60000);
</script>
</body>
</html>
"""


LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hindsight 面板 · 需要访问密钥</title>
<style>
  body{margin:0;background:#0e1117;color:#e6ebf5;display:flex;min-height:100vh;align-items:center;
       justify-content:center;font:14px/1.6 -apple-system,"PingFang SC",Arial,sans-serif}
  .box{background:#161b24;border:1px solid #28303f;border-radius:14px;padding:26px 24px;width:min(92vw,360px)}
  h1{font-size:16px;margin:0 0 6px}
  p{color:#8b97ab;font-size:12.5px;margin:0 0 16px}
  input{width:100%;box-sizing:border-box;background:#1c2230;border:1px solid #28303f;color:#e6ebf5;
        border-radius:9px;padding:10px 12px;font-size:16px;letter-spacing:2px;text-align:center}
  button{width:100%;margin-top:12px;background:#243056;border:1px solid #3a4c86;color:#fff;
         border-radius:9px;padding:10px;font-size:14px;cursor:pointer}
  .err{color:#f2637b;font-size:12.5px;margin-top:10px;min-height:16px}
</style></head>
<body><div class="box">
  <h1>Hindsight 控制面板</h1>
  <p>此面板对局域网开放，需要访问密钥。密钥保存在服务端配置的密钥文件里（默认 <code>~/.hermes/hindsight/access-key.txt</code>）。</p>
  <input id="k" placeholder="访问密钥" autocomplete="off" autocapitalize="characters" autofocus>
  <button onclick="go()">进入</button>
  <div class="err" id="e"></div>
</div>
<script>
function go(){
  const v = document.getElementById('k').value.trim();
  if(!v){ document.getElementById('e').textContent='请输入密钥'; return; }
  location.href = '/?k=' + encodeURIComponent(v);
}
document.getElementById('k').addEventListener('keydown', e=>{ if(e.key==='Enter') go(); });
if(location.search.indexOf('bad=1')>-1) document.getElementById('e').textContent='密钥不正确，请重试';
</script></body></html>
"""


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "HindsightDashboard/2.0"
    api_base = DEFAULT_API
    cp_url = DEFAULT_CP
    default_bank = DEFAULT_BANK
    allow_remote_write = False   # 局域网只读；True 时允许远程触发重试/反思（会花 token）
    access_key = ""              # 非空时，非本机访问必须携带密钥（?k= 或 cookie）

    def log_message(self, fmt, *args):
        pass

    def _is_local(self):
        host = self.client_address[0] if self.client_address else ""
        return host.startswith("127.") or host in ("::1", "localhost", "")

    def _check_key(self, q):
        """返回 (是否放行, 是否需要在响应里写 cookie)。本机或未配密钥时一律放行。"""
        if self._is_local() or not self.access_key:
            return True, False
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == KEY_COOKIE and hmac.compare_digest(v, self.access_key):
                    return True, False
        if q.get("k", [""])[0] and hmac.compare_digest(q["k"][0], self.access_key):
            return True, True      # 通过 ?k=密钥 进入，顺便种下 cookie
        return False, False

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for h, v in (extra_headers or []):
            self.send_header(h, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _parse(self):
        p = urllib.parse.urlparse(self.path)
        return p.path, urllib.parse.parse_qs(p.query)

    def _bank(self, q):
        return q.get("bank", [self.default_bank])[0] or self.default_bank

    def _qb(self, q):
        return urllib.parse.quote(self._bank(q))

    def do_GET(self):
        path, q = self._parse()
        api = self.api_base
        ok, set_cookie = self._check_key(q)
        if not ok:
            if path.startswith("/api/"):
                return self._send(401, json.dumps({"detail": "需要访问密钥：请在网址后加 ?k=密钥"}, ensure_ascii=False))
            return self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
        extra = [("Set-Cookie", f"{KEY_COOKIE}={self.access_key}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")] if set_cookie else None
        if set_cookie and not path.startswith("/api/"):
            # 用 ?k=密钥 打开页面时，种好 cookie 后把密钥从地址栏抹掉（避免留在手机历史里）
            rest = {k: v for k, v in q.items() if k != "k"}
            loc = path + ("?" + urllib.parse.urlencode(rest, doseq=True) if rest else "")
            self.send_response(302)
            self.send_header("Location", loc)
            for h, v in extra:
                self.send_header(h, v)
            self.end_headers()
            return
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8", extra_headers=extra)
        if path == "/docs":
            self.send_response(302)
            self.send_header("Location", api + "/docs")
            self.end_headers()
            return
        if path == "/metrics-ui":
            try:
                with _OPENER.open(api + "/metrics", timeout=15) as r:
                    text = r.read().decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001
                text = f"metrics 获取失败: {e}"
            body = ("<html><head><meta charset='utf-8'><title>Hindsight metrics</title>"
                    "<style>body{background:#0e1117;color:#e6ebf5;font:12px ui-monospace,Menlo,monospace;"
                    "padding:18px;white-space:pre-wrap;word-break:break-all}</style></head><body>"
                    + text.replace("&", "&amp;").replace("<", "&lt;") + "</body></html>")
            return self._send(200, body, "text/html; charset=utf-8")

        if path == "/api/summary":
            d = build_summary(api, self._bank(q), q.get("period", ["30d"])[0])
            d["cp_url"] = self.cp_url
            return self._send(200, json.dumps(d, ensure_ascii=False))
        if path == "/api/memories":
            return self._send(200, json.dumps(api_get(api, f"/v1/default/banks/{self._qb(q)}/memories/list", {
                "q": q.get("q", [""])[0], "type": q.get("type", [""])[0],
                "limit": q.get("limit", ["20"])[0], "offset": q.get("offset", ["0"])[0],
            }), ensure_ascii=False))
        if path == "/api/memory":
            mid = q.get("id", [""])[0]
            data = api_get(api, f"/v1/default/banks/{self._qb(q)}/memories/{urllib.parse.quote(mid)}")
            data["_history"] = api_get(api, f"/v1/default/banks/{self._qb(q)}/memories/{urllib.parse.quote(mid)}/history", timeout=15)
            return self._send(200, json.dumps(data, ensure_ascii=False))
        if path == "/api/entities":
            return self._send(200, json.dumps(api_get(api, f"/v1/default/banks/{self._qb(q)}/entities", {
                "limit": q.get("limit", ["200"])[0], "offset": q.get("offset", ["0"])[0],
            }), ensure_ascii=False))
        if path == "/api/graph":
            return self._send(200, json.dumps(api_get(api, f"/v1/default/banks/{self._qb(q)}/entities/graph", {
                "limit": q.get("limit", ["45"])[0],
            }), ensure_ascii=False))
        if path == "/api/ops":
            return self._send(200, json.dumps(api_get(api, f"/v1/default/banks/{self._qb(q)}/operations", {
                "status": q.get("status", [""])[0], "type": q.get("type", [""])[0],
                "limit": q.get("limit", ["20"])[0], "offset": q.get("offset", ["0"])[0],
                "exclude_parents": "true",
            }), ensure_ascii=False))
        if path == "/api/llm-requests":
            return self._send(200, json.dumps(api_get(api, f"/v1/default/banks/{self._qb(q)}/llm-requests", {
                "status": q.get("status", [""])[0], "limit": q.get("limit", ["20"])[0],
                "offset": q.get("offset", ["0"])[0],
            }), ensure_ascii=False))
        if path == "/api/config":
            return self._send(200, json.dumps({
                "profile": api_get(api, f"/v1/default/banks/{self._qb(q)}/profile"),
                "config": api_get(api, f"/v1/default/banks/{self._qb(q)}/config").get("config", {}),
                "directives": api_get(api, f"/v1/default/banks/{self._qb(q)}/directives").get("items", []),
            }, ensure_ascii=False))
        if path == "/api/reflect":
            pass  # POST only
        if path == "/api/cancel":
            op = q.get("op", [""])[0]
            return self._send(200, json.dumps(api_delete(api, f"/v1/default/banks/{self._qb(q)}/operations/{urllib.parse.quote(op)}"), ensure_ascii=False))
        return self._send(404, json.dumps({"detail": "Not Found"}))

    def do_POST(self):
        path, q = self._parse()
        ok, _ = self._check_key(q)
        if not ok:
            return self._send(401, json.dumps({"detail": "需要访问密钥：请在网址后加 ?k=密钥"}, ensure_ascii=False))
        # 写操作（重试/反思）会消耗 LLM token。默认只允许本机触发，
        # 局域网/手机来访一律拒绝，防止同网段设备误点烧钱。
        if not self.allow_remote_write and not self._is_local():
            return self._send(403, json.dumps({
                "detail": "写操作仅限本机：本面板对局域网开放只读访问。需从手机触发重试/反思，请给服务加 --allow-remote-write",
            }, ensure_ascii=False))
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {}
        api, qb = self.api_base, self._qb(q)
        if path == "/api/recall":
            payload = {"query": body.get("query", ""), "budget": body.get("budget", "mid")}
            if body.get("trace"):
                payload["trace"] = True
            return self._send(200, json.dumps(api_post(api, f"/v1/default/banks/{qb}/memories/recall", payload, timeout=180), ensure_ascii=False))
        if path == "/api/reflect":
            payload = {"query": body.get("query", ""), "budget": body.get("budget", "mid")}
            return self._send(200, json.dumps(api_post(api, f"/v1/default/banks/{qb}/reflect", payload, timeout=300), ensure_ascii=False))
        if path == "/api/retry":
            op = q.get("op", [""])[0]
            return self._send(200, json.dumps(api_post(api, f"/v1/default/banks/{qb}/operations/{urllib.parse.quote(op)}/retry"), ensure_ascii=False))
        return self._send(404, json.dumps({"detail": "Not Found"}))


def main():
    ap = argparse.ArgumentParser(description="Hindsight 本地管理面板（单文件、零依赖）")
    ap.add_argument("--port", type=int, default=8990)
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机；填 0.0.0.0 可让局域网/手机访问")
    ap.add_argument("--api", default=None, help="Hindsight API 地址，默认从插件配置或内置默认值推断")
    ap.add_argument("--bank", default=None, help="记忆库名（bank_id），默认从插件配置推断")
    ap.add_argument("--cp", default=DEFAULT_CP, help="官方 Control Plane 地址，用于 iframe 内嵌与跳转链接")
    ap.add_argument("--allow-remote-write", action="store_true",
                    help="放开远程写操作（重试/反思/取消）；默认仅本机可写，远程只读")
    ap.add_argument("--access-key", default=None,
                    help="远程访问密钥；默认读密钥文件（可由 HS_DASH_KEY_FILE 指定），本机免密")
    args = ap.parse_args()

    cfg_api, cfg_bank = load_local_config()
    Handler.api_base = (args.api or cfg_api).rstrip("/")
    Handler.default_bank = args.bank or cfg_bank
    Handler.cp_url = args.cp.rstrip("/")
    Handler.allow_remote_write = args.allow_remote_write
    Handler.access_key = (args.access_key if args.access_key is not None else load_access_key()).strip()

    global SHOW_LAN_HINT
    SHOW_LAN_HINT = args.host not in ("127.0.0.1", "localhost", "::1")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[hindsight-dashboard] http://{args.host}:{args.port} → API {Handler.api_base} · 库 {Handler.default_bank}"
          f" · Control Plane {Handler.cp_url} · 远程密钥门 {'开' if Handler.access_key else '关'} · 远程写 {'允许' if Handler.allow_remote_write else '只读'}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
