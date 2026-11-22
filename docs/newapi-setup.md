# 把 New-API 接到 keyvault 上

这份文档只讲接入操作，不讲原理。原理看 [README](../README.md)。

---

## 情况一：New-API 直接跑在宿主机（exe / 二进制 / python）

最简单。keyvault 默认就在 `127.0.0.1:3997`，两边同一个网络命名空间，直接通。

**1. 先确认 keyvault 活着**

```bash
curl http://127.0.0.1:3997/health
```

拿到 `{"status":"ok","routes":[...]}` 就对了。

**2. 打开 New-API 后台 → 渠道 → 新建/编辑**

只改两个字段：

```
类型        OpenAI（或按上游实际类型选）
Base URL    http://127.0.0.1:3997/relay/openai
密钥        vault-only
模型        gpt-4o,gpt-4o-mini,...      ← 保持原样
```

`openai` 是 `keys.json` 里的路由名，换成你的实际名字。

**3. 点「测试」**

- 通了 → 完成，key 已经不在数据库里了
- 报 `unknown route: xxx` → 路由名拼错，或者 keys.json 里没这条
- 报 `route disabled: xxx` → 这条的 `enabled` 是 `false`
- 报 401/403 → key 本身有问题，或者 `auth_header` / `auth_prefix` 填错
- 报 404 且上游是 Gemini 这类 → 大概率是少了 `strip_prefix`

**4. 把旧渠道的真 key 清掉**

新建一条渠道验证通过后，**把原来那条填真 key 的渠道删掉或停用**。不然数据库里还留着一份明文，改了个寂寞。

---

## 情况二：New-API 也跑在 Docker 里

麻烦点，因为容器里的 `127.0.0.1` 指容器自己，够不到宿主机。

### 方案 A：两个都用 host 网络（最省事）

```bash
docker run -d --name new-api --network host ... 原参数 ...
docker run -d --name keyvault --network host -v "$PWD/keys.json:/app/keys.json:ro" keyvault
```

两边都在宿主机网络里，`127.0.0.1:3997` 直接可用。base_url 照情况一填。

> **Linux 可用，Mac/Windows 不行。** Docker Desktop 的 `--network host` 是半残的，别指望。

### 方案 B：独立网络 + 容器名（跨平台都能用）

```bash
docker network create ai-net

docker run -d --name keyvault --network ai-net \
  -v "$PWD/keys.json:/app/keys.json:ro" keyvault

docker run -d --name new-api --network ai-net ... 原参数 ...
```

因为同一个自定义网络里有 DNS，**容器名可以直接当主机名**。所以：

```
Base URL    http://keyvault:3997/relay/openai
```

注意这时 keyvault **不能**只绑 127.0.0.1，否则别的容器连不进来。用镜像默认的 `KEYVAULT_HOST=0.0.0.0`，
然后靠「不发布端口」来隔离——`docker run` 时**不要加 `-p`**，它就只有同一网络里的容器能访问，
宿主机外部依然碰不到。这是安全的做法，比映射到 `0.0.0.0:3997` 好得多。

### 方案 C：Linux 下用 docker0 网关地址

```bash
ip -4 addr show docker0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}'
# 通常是 172.17.0.1

# keyvault 绑 0.0.0.0，但只映射到回环没必要——
# 因为接下来只让 docker0 网段访问
```

```
Base URL    http://172.17.0.1:3997/relay/openai
```

需要 keyvault 监听 `0.0.0.0` 或用 `KEYVAULT_HOST=172.17.0.1`。**同时要用防火墙挡住非 docker0 网段**，
否则这就是把注入器挂在局域网上。不如方案 B 干净，除非你有别的理由。

---

## 验证 key 真的没进数据库

改完之后自己核一遍，别只信后台页面：

```bash
# SQLite 版 New-API
sqlite3 one-api.db "select id, name, base_url, key from channels;"
```

`key` 列应该全是 `vault-only`，`base_url` 全是 `127.0.0.1:3997/relay/...` 或 `keyvault:3997/relay/...`。

如果还有一行是真 key，那条渠道就是漏的。

---

## 常见问题

**Q：`vault-only` 这个占位串一定要用这个词吗？**

不用，任何字符串都行。用 `vault-only` 只是因为它一眼就能看出「这是占位，不是真 key」——
以后你或者别人翻到数据库，不会误以为这是个失效的 key 而去排查上游。

**Q：New-API 的「渠道测试」会不会把 key 打印到日志？**

New-API 的日志会记录请求信息，但打的是它自己发给 keyvault 的那个 `vault-only`。
真 key 只在 keyvault 进程里，它的日志不含 key。所以两边日志都是干净的。

**Q：升级 New-API 会不会覆盖 base_url？**

不会。渠道配置在数据库里，升级不动它。

**Q：以后换上游 key 怎么办？**

改 `keys.json` 保存，**不用重启、不用动 New-API、不用重启渠道**。下一个请求自动用新的。
这是 keyvault 相对「把 key 写死在数据库」的一个附带好处：轮换成本从「登后台改一条渠道」
变成「改一个文件」。

**Q：一个上游要放两个 key 做轮换（429 时切换）怎么办？**

New-API 本身的「多渠道 + 权重/优先级」就是干这个的。建两条渠道，
一条指 `relay/openai`，一条指 `relay/openai2`，keys.json 里配两条路由指向同一个 upstream，
两个不同的 key。New-API 的失败重试会自己切。

**Q：流式（SSE）会不会被缓冲成一次性返回？**

不会。回写是按 8KB 分块 `flush` 的，`Connection: close` 也设了。这点专门处理过，
因为攒完再发等于把流式废掉。
