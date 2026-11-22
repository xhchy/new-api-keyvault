# keyvault

**让 New-API 的后台不再明文显示上游 API key。**

单文件、零依赖、纯标准库。给自建 New-API 的人用：上游真 key 不进数据库，后台谁看都是 `vault-only`。

```
                    New-API 数据库里的样子
        ┌──────────┬──────────────────────┬───────────┐
        │ 渠道      │ base_url             │ key       │
        ├──────────┼──────────────────────┼───────────┤
        │ openai   │ http://127.0.0.1:3997/relay/openai │ vault-only │
        │ gemini   │ http://127.0.0.1:3997/relay/gemini │ vault-only │
        │ anthropic│ http://127.0.0.1:3997/relay/anthropic │ vault-only │
        └──────────┴──────────────────────┴───────────┘
                              │
                              ▼  本机 3997 注入真 key
                    ┌──────────────────┐
                    │    keyvault.py   │  ← keys.json（600，不入库）
                    └──────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        api.openai.com   generativelanguage  api.anthropic.com
```

---

## 为什么需要它

New-API 的设计里，渠道的 key 是**明文存在数据库、明文展示给管理员**的。这是它的功能，不是 bug——它得用这个 key 去调上游。

问题是这个设计有个副作用：**admin 账号的可信度 = 你全部上游凭证的安全等级。**

- 让朋友登后台看用量 → 他能在渠道页复制走你所有 key
- 数据库备份传到网盘/发给自己 → 里面是明文
- 装了别人写的渠道管理插件/脚本 → 它读得到全部 key

大多数自建站长的处理方式是不给后台，或者建一个权限受限的账号。但只要看过一次渠道页，key 就已经出去了。

keyvault 的做法是**把 key 从数据库里拿出去**：数据库里只留一个没有意义的占位串，真 key 在本机一个文件里，转发的那一刻才注入。

---

## 快速开始

需要 Python 3.8+。没有 pip 依赖，不用装任何东西。

```bash
# 1) 配置
cp keys.example.json keys.json
chmod 600 keys.json            # Windows: icacls keys.json /inheritance:r /grant:r "%USERNAME%":R

# 2) 编辑 keys.json，把 YOUR_ 开头的值换成真 key，改完自检
python keyvault.py --check

# 3) 起服务
python keyvault.py
# [20:00:00] 已加载 4 条路由：anthropic, gemini, openai, openrouter
# [20:00:00] keyvault 就绪  http://127.0.0.1:3997/relay/<route>/...
```

验证：

```bash
curl http://127.0.0.1:3997/health
# {"status":"ok","routes":["anthropic","gemini","openai","openrouter"]}

curl http://127.0.0.1:3997/__routes      # 路由全貌，不含 key
```

---

## 接到 New-API

对每一条渠道做两件事：

| 字段 | 填什么 |
|---|---|
| **base_url** | `http://127.0.0.1:3997/relay/<route>`，`<route>` 是 keys.json 里的路由名 |
| **key** | `vault-only`（字面量，随便什么都行，只要不是真 key） |

其余（模型、分组、倍率、优先级）保持原样。

然后点渠道的「测试」，能通就说明注入正常。

> **New-API 自己也是 Docker 跑的话**，容器里的 `127.0.0.1` 指容器自己，够不到宿主机的 keyvault。
> 两种解法：用 `network_mode: host`，或者把 base_url 写成宿主机在 docker 网络里的地址
> （Linux 下通常是 `http://172.17.0.1:3997/relay/<route>`，Mac/Windows 用 `host.docker.internal`）。
> 详见 [docs/newapi-setup.md](docs/newapi-setup.md)。

---

## 配置字段

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `upstream` | ✅ | — | 上游根地址，带 `http://` 或 `https://` |
| `key` | | `""` | 空 = 不注入认证头，适合 keyless 上游 |
| `auth_header` | | `Authorization` | Anthropic 用 `x-api-key`，Azure 用 `api-key` |
| `auth_prefix` | | `Bearer ` | 设成 `""` 就只发 key 本体，不加前缀 |
| `strip_prefix` | | `""` | 剥掉请求路径前缀，见下面的坑 |
| `proxy` | | `false` | 这条路由走全局代理 |
| `proxy_url` | | 环境变量 | 单独指定这条的代理 |
| `extra_headers` | | `{}` | 附加固定请求头 |
| `timeout` | | `300` | 这条路由的上游超时（秒） |
| `enabled` | | `true` | `false` 临时停用，不删配置 |
| `note` | | — | 给自己看的备注，不会转发 |

### `strip_prefix` 是干什么的

New-API 会往 base_url 后面拼 `/v1/chat/completions` 这类路径。而有些上游的 OpenAI 兼容端点本身已经带了一层：

```
base_url = https://generativelanguage.googleapis.com/v1beta/openai
New-API 拼完 = https://generativelanguage.googleapis.com/v1beta/openai/v1/chat/completions
                                                                     ^^^^ 多余的
实际该打的是 = https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
```

`"strip_prefix": "/v1"` 就是把多出来的那一层削掉。没有这个字段，你会看到一个 404，而 404 的正文通常不会告诉你是路径拼错了。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `KEYVAULT_HOST` | `127.0.0.1` | 监听地址，**改之前先读下面那一节** |
| `KEYVAULT_PORT` | `3997` | 监听端口 |
| `KEYVAULT_KEYS` | `./keys.json` | 配置路径 |
| `KEYVAULT_PREFIX` | `/relay/` | 路径前缀 |
| `KEYVAULT_PROXY` | `http://127.0.0.1:7897` | 全局代理 |
| `KEYVAULT_TIMEOUT` | `300` | 上游超时秒数 |
| `KEYVAULT_MAX_BODY` | `33554432` | 请求体上限（32MB），超过直接 413 |
| `KEYVAULT_LOG` | `info` | `off` / `info` / `debug` |

**热重载**：改完 `keys.json` 保存即可，下一个请求自动生效，不用重启。

---

## 安全边界（请读完）

这个程序**不做鉴权**，这是刻意的——它只服务本机的 New-API，多一层鉴权只是多一层能配错的东西。

由此推出一条硬要求：

> **必须绑 127.0.0.1。** 一旦绑到 `0.0.0.0`，同网段任何人都能通过它调用你的上游账号，
> 而且用的是你的额度、你的账号、你的风控记录。程序在非回环地址启动时会打警告，但拦不住你。

它保护什么、不保护什么，说清楚：

**保护**
- New-API 数据库和后台页面里没有真 key
- 数据库备份、导出、给别人的后台账号，都拿不到上游凭证
- 日志里不记 key，只有路由名 / 状态码 / 耗时 / 字节数

**不保护**
- 能读到 `keys.json` 文件的人（同机同用户、或拿到文件系统的人）
- 本机进程内存（真 key 在进程里是明文，这是任何注入方案都绕不过的）
- New-API 的**用户** key（那本来就是给用户的，跟这里无关）
- 上游那边记的是你的账号，风控、封号风险不变

也就是说它解决的是「**凭证的可见范围**」，不是「凭证被偷」。想更进一步，得靠 OS 级隔离（单独用户跑、文件 ACL、或上 KMS/Vault）——那是另一个量级的工程，这份脚本不假装能解决。

---

## Docker

```bash
docker build -t keyvault .
docker run -d --name keyvault \
  --network host \
  -v "$PWD/keys.json:/app/keys.json:ro" \
  --restart unless-stopped \
  keyvault
```

`--network host` 是为了让宿主机上的 New-API 能用 `127.0.0.1:3997` 访问到它。
如果你更愿意用 bridge 网络，去掉 `--network host`，把 New-API 的 base_url 改成容器名或固定 IP 即可。

---

## 和别的方案比

| 方案 | 差别 |
|---|---|
| 直接填真 key | 简单，但后台、备份、任何拿到 admin 的人都能看到 |
| 建一个受限权限的 New-API 账号 | 挡的是「操作」，不是「看」——渠道页的 key 依然明文 |
| 用 Vault / Infisical 这类专业密钥管理 | 更完整，但要部署一整套、改 New-API 的调用链，为一个个人站过度了 |
| 每次换 key 就轮换一遍 | 治不了「看得到」这个问题，只是缩短了暴露窗口 |
| **keyvault** | 一个文件、一个占位串、一条 base_url，改完就生效 |

它的定位很窄：**只解决「后台不该显示真 key」这一件事**，顺手解决了「不同上游认证头不一样」和「路径多一层」这两个天天要踩的坑。它不是密钥管理系统，别当那个用。

---

## License

AGPL-3.0-or-later。见 [LICENSE](LICENSE)。

从 [Free-API-Collection](https://github.com/xhchy/Free-API-Collection) 的 `vault/` 抽出来单独维护。

---

# English

**Stop New-API from showing your upstream API keys in the admin panel.**

A single-file, zero-dependency Python proxy. Point a New-API channel's `base_url` at
`http://127.0.0.1:3997/relay/<route>`, put `vault-only` in its key field, and the real key
stays in a local `keys.json` that never touches the database — it's injected per request.

```bash
cp keys.example.json keys.json && chmod 600 keys.json   # then fill in real keys
python keyvault.py --check
python keyvault.py
```

Anything that can read the admin panel, a database dump, or a backup now sees `vault-only`
instead of a working credential.

It also handles the two things that break every self-hosted setup: providers that don't
accept `Authorization: Bearer` (Anthropic uses `x-api-key`, Azure uses `api-key`), and the
double-`/v1` path that makes Gemini's OpenAI-compatible endpoint return 404.

**It has no authentication on purpose.** Bind it to `127.0.0.1` only. Exposing it to a
network hands anyone on that network your upstream account.

It limits who can *see* your credentials, not who can *steal* them — file access and
process memory are out of scope. For that you need OS-level isolation or a real secrets
manager.

AGPL-3.0-or-later.
