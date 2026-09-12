# Hindsight Dashboard

[English](README.md) · **简体中文**

[![CI](https://github.com/GerateGuo/hindsight-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/GerateGuo/hindsight-dashboard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)

一个**单文件、零依赖**的 Web 管理面板，用于 [Hindsight](https://github.com/vectorize-io/hindsight)——Vectorize 出品的 AI Agent 记忆系统。

不用 `pip install`，不用构建，不用 Node。一个只依赖 Python 标准库的文件，直接对接 Hindsight 的 REST 接口。

![概览](docs/screenshot-overview.png)

## 为什么做这个

如果你自托管 Hindsight，手头大概只有这几样东西：

- **Swagger UI**（`/docs`）——适合逐个戳接口，但不适合整体观察你的记忆库。
- **Prometheus 指标**（`/metrics`）——原始计数器，瞟一眼看不出什么。
- **官方 Control Plane**（独立的 Next.js 应用，默认端口 `9999`）——功能完整，但偏通用，而且需要 Node 工具链才能装。

这个项目补的正是中间的空白：一个轻量的聚合 + 运维视图，任何能跑 Python 的地方都能跑，并且可以**把官方 Control Plane 直接内嵌成其中一个标签页**——既能一眼看全局，也不用放弃官方那套完整功能。

## 功能

| 标签页 | 内容 |
|---|---|
| **概览** | 记忆 / 链接 / 实体 / 文档数量，按事实类型（`world` / `experience` / `observation`）堆叠的增长曲线，7 / 30 / 90 天范围切换，最近记忆，运行状态（数据库、合并、内存占用、连接池） |
| **检索试验** | 跑 `recall` 并看到**各路检索的打分明细**（`final` / `semantic` / `reranker`），以及 `reflect` 综合推理出的答案 |
| **记忆** | 全文搜索、按事实类型筛选、分页浏览 |
| **实体图谱** | 可搜索的实体列表（带提及次数）+ **不依赖任何图形库的 SVG 力导向共现图**（点节点高亮邻居） |
| **用量** | 逐条 LLM 调用明细（阶段、模型、耗时、token、状态）、按天 token 柱状图、按阶段累计调用、进程资源 |
| **操作诊断** | 异步任务队列（按状态筛选）、**失败操作一键重试**、取消排队中的任务 |
| **配置** | 记忆库档案、mission、directives、保留与合并参数（只读） |
| **官方界面** | 用 iframe 内嵌的官方 Control Plane |

![实体图谱](docs/screenshot-graph.png)

![操作诊断](docs/screenshot-ops.png)

## 快速开始

环境要求：**Python 3.9+**，别的什么都不用。

```bash
curl -O https://raw.githubusercontent.com/GerateGuo/hindsight-dashboard/main/hindsight-dashboard.py
python3 hindsight-dashboard.py --api http://localhost:8888 --bank my-bank
# 打开 http://127.0.0.1:8990
```

Hindsight 跑在别处？把 `--api` 指过去：

```bash
python3 hindsight-dashboard.py --api http://192.168.1.10:8988 --bank my-bank --port 8990
```

如果你在用 [Hermes Agent](https://github.com/NousResearch/hermes-agent)，默认值会自动从 `~/.hermes/hindsight/config.json`（`api_url` + `bank_id`）读取，所以直接 `python3 hindsight-dashboard.py` 通常就够了。

### 命令行参数

```
--port PORT                  监听端口（默认 8990）
--host HOST                  绑定地址（默认 127.0.0.1）
--api API                    Hindsight API 地址
--bank BANK                  记忆库 ID（bank_id）
--cp CP                      官方 Control Plane 地址，用于内嵌标签页与跳转链接
--access-key KEY             远程访问密钥（也可以放密钥文件里）
--allow-remote-write         允许非本机客户端触发重试 / 反思 / 取消
```

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `HS_DASH_CONFIG_JSON` | `~/.hermes/hindsight/config.json` | 可选配置，用于推断 `api_url` / `bank_id` |
| `HS_DASH_KEY_FILE` | `~/.hermes/hindsight/access-key.txt` | 存放远程访问密钥的文件 |

## 安全模型

这个面板能读到你全部的 memory，而且部分按钮会消耗 LLM token——所以默认是收紧的：

- **默认只监听回环地址。** 绑定 `127.0.0.1`，同网络里没有任何设备能连上。
- **远程访问需要密钥。** 用 `--host 0.0.0.0` 时应设置访问密钥。本机（`127.0.0.1`）请求免密，桌面使用体验不变；远程客户端需要带上一次 `?k=<密钥>`（命中后会种下一个 HttpOnly、有效期 30 天的 cookie，并立刻把地址栏里的密钥抹掉），或者携带该 cookie。没有密钥的请求只会拿到登录页（不含任何数据），`/api/*` 返回 `401`。
- **远程写操作默认只读。** `POST /api/retry`、`/api/reflect` 以及取消操作，只要不来自回环地址就会被拒（`403`），除非你显式加 `--allow-remote-write`。

> **Hindsight 本体也要锁。** Hindsight 的 REST API **没有任何认证**——只要端口 `8888`/`8988` 能被访问到，同网络里任何人都能读走甚至清空你的全部记忆。请把它绑到回环地址（`HINDSIGHT_API_HOST=127.0.0.1`），让面板 / Control Plane 在本地代理访问。

## 工作原理

面板本质上是一个很薄的**服务端代理 + 一个静态页面**。它调用下列 Hindsight 接口，拼成一个 `/api/summary` 返回给前端：

```
/health · /version · /metrics
/v1/default/banks
/v1/default/banks/{bank}/stats
/v1/default/banks/{bank}/stats/memories-timeseries?period=7d|30d|90d
/v1/default/banks/{bank}/memories/list · memories/{id} · memories/recall
/v1/default/banks/{bank}/reflect
/v1/default/banks/{bank}/entities · entities/graph
/v1/default/banks/{bank}/operations · operations/{id}/retry
/v1/default/banks/{bank}/llm-requests · llm-requests/stats
/v1/default/banks/{bank}/profile · config · directives
```

自己的路由：`/api/summary`、`/api/memories`、`/api/entities`、`/api/graph`、`/api/ops`、`/api/llm-requests`、`/api/config`、`/api/cancel`（GET），以及 `/api/recall`、`/api/reflect`、`/api/retry`（POST）。支持深链接：`#overview`、`#recall`、`#memories`、`#graph`、`#usage`、`#ops`、`#config`、`#official`。还可以直接用 URL 跑一次检索：

```
http://localhost:8990/?q=部署+超时&mode=recall&budget=low#recall
```

## 开发

仓库自带一个返回假数据的 mock API，不需要真的部署 Hindsight，也能开发或给界面截图：

```bash
python3 dev/mock_hindsight_api.py --port 8899
python3 hindsight-dashboard.py --api http://localhost:8899 --bank atlas --port 8995
```

本 README 里的所有截图都是用这个 mock 生成的。

### 测试

`dev/smoke_test.py` 会起一个 mock API 加一个面板实例，把界面依赖的每个接口都打一遍，再对访问密钥的判定逻辑做单元校验。零依赖、不访问外网，约 15 秒：

```bash
python3 dev/smoke_test.py
```

CI 跑的就是它，覆盖 Python 3.9 / 3.11 / 3.13。欢迎贡献——请保持这个测试常绿且不引入依赖。

## 一些值得知道的坑

- **代理环境变量。** 如果设了 `HTTP_PROXY`（Clash / VPN 之类），连 `localhost` 的请求也会被代理接管并返回 `502`。面板内部用空的 `ProxyHandler` 构造成 opener 来绕开；你自己用 `curl` 测试时记得加 `--noproxy '*'`。
- **`period` 取值范围。** `memories-timeseries` 只支持 `7d` / `30d` / `90d`；传 `14d` 这类不支持的值得不到报错，会被**静默降级成 `7d`**。
- **launchd 会缓存 plist。** macOS 上改完 LaunchAgent 的 plist，只跑 `launchctl kickstart -k` **不会**重新加载环境变量——要用 `launchctl bootout` + `launchctl bootstrap`。
- **iframe 内嵌可行**，因为官方 Control Plane 没有下发 `X-Frame-Options` / `frame-ancestors`；如果上游哪天加了，这个「官方界面」标签页就得改回用跳转链接。
- **明文 HTTP。** 密钥在局域网里是明文传输的。如果在意，就把服务绑到 VPN 网卡（例如 Tailscale），或者在前面套一层 TLS。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。
