#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_test.py — 端到端自检 / CI 用。

起一个假数据 Hindsight API + 一个面板实例，把每个标签页用到的接口都打一遍，
再直接对访问密钥的判定逻辑做单元校验。全部通过退出码 0，任一失败退出码 1。

    python3 dev/smoke_test.py            # 静默模式，只报结论
    python3 dev/smoke_test.py --verbose  # 打印每项明细

只用标准库，不需要装任何依赖；在 macOS / Linux / Windows 上都能跑。
"""

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "hindsight-dashboard.py")
MOCK = os.path.join(ROOT, "dev", "mock_hindsight_api.py")
BANK = "atlas"

# 绕过系统代理：否则设了 HTTP_PROXY 的环境里连 127.0.0.1 也会被代理接管
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

RESULTS = []


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(url, timeout=40):
    """轮询直到有响应，返回是否就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with OPENER.open(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


NO_REDIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def http(url, method="GET", payload=None, timeout=60, follow=True):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    opener = OPENER if follow else NO_REDIRECT_OPENER
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    return bool(ok)


def load_dashboard_module():
    spec = importlib.util.spec_from_file_location("hindsight_dashboard_under_test", DASHBOARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 一、访问密钥判定逻辑（直接测 Handler._check_key，不依赖真实非回环来源）
# ---------------------------------------------------------------------------
def test_access_key_logic():
    mod = load_dashboard_module()
    key = "TESTKEY12345"

    class FakeCtx:
        """_check_key 只用到 access_key / client_address / headers，以及 _is_local()。

        _is_local 直接复用 Handler 上的真实实现（普通函数赋成类属性即成为方法），
        这样测的是真代码，而不是另写一份判定逻辑。
        """

        def __init__(self, addr, cookie=None, access_key=key):
            self.access_key = access_key
            self.client_address = (addr, 5000)
            self.headers = {"Cookie": cookie} if cookie else {}

    H = mod.Handler
    FakeCtx._is_local = H._is_local   # 必须在 H 定义之后

    cases = [
        ("本机 loopback 免密", FakeCtx("127.0.0.1"), {}, True, False),
        ("本机 IPv6 loopback 免密", FakeCtx("::1"), {}, True, False),
        ("未配置密钥时远程放行", FakeCtx("10.0.0.9", access_key=""), {}, True, False),
        ("远程无密钥拒绝", FakeCtx("10.0.0.9"), {}, False, False),
        ("远程密钥错误拒绝", FakeCtx("10.0.0.9"), {"k": ["WRONG"]}, False, False),
        ("远程 ?k= 正确放行并种 cookie", FakeCtx("10.0.0.9"), {"k": [key]}, True, True),
        ("远程正确 cookie 放行", FakeCtx("10.0.0.9", cookie=f"{mod.KEY_COOKIE}={key}"), {}, True, False),
        ("远程错误 cookie 拒绝", FakeCtx("10.0.0.9", cookie=f"{mod.KEY_COOKIE}=nope"), {}, False, False),
        ("多 cookie 中命中正确项", FakeCtx("10.0.0.9", cookie=f"other=1; {mod.KEY_COOKIE}={key}; x=2"), {}, True, False),
    ]
    for name, ctx, q, want_ok, want_cookie in cases:
        got_ok, got_cookie = H._check_key(ctx, q)
        check(f"密钥门：{name}", got_ok == want_ok and got_cookie == want_cookie,
              f"ok={got_ok}/{want_ok} cookie={got_cookie}/{want_cookie}")


# ---------------------------------------------------------------------------
# 二、端到端：假数据 API + 面板
# ---------------------------------------------------------------------------
def test_end_to_end(verbose):
    mock_port, dash_port = free_port(), free_port()
    api = f"http://127.0.0.1:{mock_port}"
    base = f"http://127.0.0.1:{dash_port}"
    env = dict(os.environ)
    env["HS_DASH_CONFIG_JSON"] = os.path.join(ROOT, "no-such-config.json")   # 避免读到本机配置
    env["HS_DASH_KEY_FILE"] = os.path.join(ROOT, "no-such-key.txt")          # 避免启用密钥门
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)

    procs = []
    try:
        procs.append(subprocess.Popen([sys.executable, MOCK, "--port", str(mock_port)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env))
        procs.append(subprocess.Popen([sys.executable, DASHBOARD, "--port", str(dash_port),
                                       "--api", api, "--bank", BANK],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env))

        if not check("面板启动并就绪", wait_ready(f"{base}/api/summary?period=7d")):
            return

        st, d = http(f"{base}/api/summary?period=30d")
        check("概览：/api/summary", st == 200 and d.get("bank") == BANK,
              f"HTTP {st} bank={d.get('bank') if isinstance(d, dict) else d}")
        if isinstance(d, dict):
            check("概览：记忆总数已解析", (d.get("stats") or {}).get("total_nodes", 0) > 0,
                  f"total_nodes={(d.get('stats') or {}).get('total_nodes')}")
            check("概览：增长曲线有数据点", len((d.get("timeseries") or {}).get("buckets") or []) == 30,
                  f"buckets={len((d.get('timeseries') or {}).get('buckets') or [])}")
            check("概览：健康状态 healthy", (d.get("health") or {}).get("status") == "healthy")
            check("概览：回环监听下不泄露网卡地址", d.get("lan_ips") == [], f"lan_ips={d.get('lan_ips')}")

        for path, key, label in [
            (f"/api/memories?bank={BANK}&limit=3", "items", "记忆列表"),
            (f"/api/entities?bank={BANK}&limit=5", "items", "实体列表"),
            (f"/api/graph?bank={BANK}&limit=20", "nodes", "实体图谱"),
            (f"/api/ops?bank={BANK}&limit=3", "operations", "操作队列"),
            (f"/api/llm-requests?bank={BANK}&limit=3", "items", "LLM 明细"),
            (f"/api/config?bank={BANK}", "profile", "记忆库配置"),
        ]:
            st, d = http(base + path)
            ok = st == 200 and isinstance(d, dict) and (key in d)
            check(f"接口：{label}", ok, f"HTTP {st}")

        st, d = http(f"{base}/api/ops?bank={BANK}&status=failed&limit=3")
        failed = (d.get("operations") or []) if isinstance(d, dict) else []
        check("接口：失败操作可筛选", st == 200 and len(failed) == 3 and
              all(o.get("status") == "failed" for o in failed), f"HTTP {st} 条数={len(failed)}")
        st, d = http(f"{base}/api/ops?bank={BANK}&limit=20&offset=0")
        first = (d.get("operations") or [{}])[0].get("id") if isinstance(d, dict) else None
        st2, d2 = http(f"{base}/api/ops?bank={BANK}&limit=20&offset=10")
        second = (d2.get("operations") or [{}])[0].get("id") if isinstance(d2, dict) else None
        check("接口：操作队列分页位移正确", first and second and first != second,
              f"offset0={first} offset10={second}")

        st, d = http(f"{base}/api/recall?bank={BANK}", "POST", {"query": "构建超时", "budget": "low"})
        results = (d.get("results") or []) if isinstance(d, dict) else []
        check("接口：recall 召回并返回打分", st == 200 and results and
              "final" in (results[0].get("scores") or {}), f"HTTP {st} 命中={len(results)}")

        st, d = http(f"{base}/api/reflect?bank={BANK}", "POST", {"query": "这个项目的偏好？", "budget": "low"})
        check("接口：reflect 返回答案", st == 200 and isinstance(d, dict) and bool(d.get("text")),
              f"HTTP {st}")

        st, d = http(f"{base}/api/retry?bank={BANK}&op=op-0003", "POST", {})
        check("接口：失败操作重试入队", st == 200 and isinstance(d, dict) and d.get("success") is True,
              f"HTTP {st}")

        st, _ = http(f"{base}/api/memories")   # bank 缺省时用服务端默认库
        check("接口：缺省 bank 参数不报 500", st in (200, 404), f"HTTP {st}")

        # 静态资源与深链接
        for path, label in [("/", "首页 HTML"), ("/metrics-ui", "原始 metrics 页")]:
            st, body = http(base + path)
            check(f"页面：{label}", st == 200 and isinstance(body, str) and len(body) > 200, f"HTTP {st}")
        st, body = http(base + "/docs", follow=False)
        # 不跟随重定向：应回 302（且 Location 指向请求方主机，而不是写死的 localhost）
        check("页面：Swagger 跳转 302", st in (301, 302), f"HTTP {st}")

    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


def main():
    ap = argparse.ArgumentParser(description="Hindsight Dashboard 自检")
    ap.add_argument("--verbose", action="store_true")
    ap.parse_args()

    print("── 访问密钥判定逻辑 ──")
    test_access_key_logic()
    print("\n── 端到端（假数据 API + 面板）──")
    test_end_to_end(True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"\n结果：{passed}/{len(RESULTS)} 项通过")
    if failed:
        print("失败项：")
        for n, d in failed:
            print(f"  · {n}  {d}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
