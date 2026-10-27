# 部署与运维

服务器：`8.134.253.167`（Alibaba Cloud Linux 4）。当前拓扑：

```
浏览器
  │  :80
  ▼
nginx 1.30
  ├── /              静态页面   /var/www/nutri_graph
  └── /chat /auth /kg /admin /health /docs /openapi.json
        │  反代（限流 + X-Real-IP）
        ▼
      uvicorn  127.0.0.1:8810   （systemd: nutri-graph.service，Restart=always）
        ├── PostgreSQL 16 (pgvector)  127.0.0.1:5432  docker: db-postgres
        ├── Neo4j 5.26                127.0.0.1:7687  docker: db-neo4j
        └── Milvus                    **未部署**（向量检索降级，图问答不受影响）
```

uvicorn 只绑回环，对外一律经过 nginx —— TLS、限流、静态页都在那一层。

## 为什么 nginx 配置写在那三个地方

`/etc/nginx/nginx.conf` 里已经有一个 `listen 80` 的默认站点，而且访问用的是 IP、
没有域名。这种情况下再写一个 `server_name _` 的 server 块不会被选中（第一个匹配的赢）。
所以走默认站点自带的扩展点：

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;
    include /etc/nginx/default.d/*.conf;   ← 我们挂在这里
}
```

| 文件 | 上下文 | 职责 |
|---|---|---|
| `/etc/nginx/conf.d/00-nutri-graph.conf` | `http` | `upstream` + `limit_req_zone`（限流区只能定义在 http 层） |
| `/etc/nginx/nutri-graph-proxy.conf` | 被 include 到 location | 反代公共参数，避免十行重复 |
| `/etc/nginx/default.d/nutri-graph-locations.conf` | `server`（80） | 静态页 + 各路径反代 + 限流引用 |

三个文件都由 `deploy/install.sh` 从仓库安装，`nginx.conf` 本身一个字没改。
**不要**在 `default.d` 里写 `root`（server 块已有 `root`，重复会报
`root directive is duplicate`）——要覆盖就写在 `location` 里。

同一台机 3000 端口还有另一个站点（`/opt/lx-aicoding/app/ui/dist`），互不影响。

## 安装 / 更新

```bash
cd /opt/code/nutri_graph
./deploy/install.sh
```

幂等，做五件事：前置检查（`.env` 可用性、docker 是否在跑）→ 装 systemd 并启动 →
铺前端静态文件 → 写 nginx 配置并 `nginx -t` + reload → 自检。被覆盖的文件都留 `.bak.<时间戳>`。

脚本会明确警告 `AUTH_REQUIRED=false` 这件事（见下面「已知风险」）。

## 日常运维

```bash
systemctl status nutri-graph           # 状态
journalctl -u nutri-graph -f           # 应用日志（含模型调用、短信、审核动作）
journalctl -u nutri-graph --since "10 min ago" -p err

systemctl restart nutri-graph          # 改完 .env 或代码后重启
nginx -t && systemctl reload nginx     # 改完 nginx 后

tail -f /var/log/nginx/access.log      # 谁在刷 /chat
tail -f /var/log/nginx/error.log       # 限流命中会记在这里
```

改前端：直接改 `/var/www/nutri_graph/` 下的文件即可（`index.html` 不缓存，改完刷新就见效）；
仓库里的源在 `frontend/`，改完重跑 `install.sh` 会覆盖过去。

### 上 HTTPS

现在只有 80（明文）。要上证书只需在 `default.d` 那个文件里加 `location` 之外的东西是不够的——
443 需要独立的 `server` 块，做法是新建 `/etc/nginx/conf.d/nutri-graph-ssl.conf`，
`listen 443 ssl`、填 `ssl_certificate`，再把 `default.d/nutri-graph-locations.conf` 用
`include` 引进去，最后在 80 的 server 里加 301 跳转。

## 登录（AUTH_REQUIRED=true）

线上开了强制登录：`/chat`、`/kg/*` 只认已登录用户，匿名身份也进不去（只能先登录）。
`/health`、`/auth/sms`、`/auth/token`、`/auth/refresh` 保持公开。

```
前端 / 客户端
  POST /auth/sms   {phone}                 → {ok, provider, expires_in, interval}
  POST /auth/token {phone, code}           → {access_token, refresh_token, user:{phone_mask,...}}
  GET  /auth/principal  Bearer <access>    → {kind:"user", phone_mask, role}
  POST /auth/refresh    {refresh_token}    → 新的一对（旋转，旧的立即失效）
  POST /auth/logout     {refresh_token}
```

- access 令牌 30 分钟，refresh 7 天；refresh **每次使用都旋转**，旧的重放会判定泄露并吊销整族
- 「登出后的令牌」和「被旋转掉的令牌」是两种 revoked，用 `replaced_by` 是否为空区分：
  前者返回 `invalid_refresh`（提示重新登录），后者返回 `token_reuse`（判定异常登录）。
  混成一种的话，用户点了退出下次回来会看到「检测到异常登录」，以为自己被盗号
- 前端把令牌存 localStorage（这页没有第三方脚本、也不把用户输入当 HTML 插入，XSS 面很小）。
  要更严就换 HttpOnly Cookie，但那需要后端配合改成 Cookie 下发

### 验证码拿不到怎么办（当前是 console 通道）

`SMS_PROVIDER=console` 时不真发短信，而 `DEBUG=false` 时接口**不会**回填验证码 ——
这是对的：一回填，任何人拿到别人手机号的码就能登录。所以验证码只进服务日志：

```bash
python -m scripts.latest_code                      # 最近一条
python -m scripts.latest_code --phone 13800138000  # 指定号码（日志里是掩码，命令帮你换算）
```

要给自己或面试官省这一步，把短信通道换成阿里云号码认证（`dypns`），见
`docs/plan-auth-and-dynamic-kg.md` §1.3 —— 个人认证账号走「短信认证」即可，不需要企业资质。

## 已知风险（按严重程度排）

1. ~~`/chat` 免登录~~ —— 已改为 `AUTH_REQUIRED=true`，未登录返回 401。
   限流仍保留（单 IP 10 次/分钟），防的是登录用户或被绕过的脚本刷额度。
2. **短信是模拟的。** `SMS_PROVIDER=console`，验证码只进日志，不会真发。上线要换 `dypns`。
3. **`DEBUG` 必须保持 `false`。** 若为 `true`，`/auth/sms` 会把验证码回填到响应里，
   等于任何人都能以任意手机号登录 —— 这会直接抵消 `AUTH_REQUIRED=true` 的全部意义。
4. **Milvus 没部署。** 向量降级和实体索引都用不了；`/health` 会显示 `degraded`（如实报告，不是故障）。
5. **`nutri/nutri` 是弱口令**，5432 绑 0.0.0.0，实际防线是 `pg_hba` 里的网段限制。
6. **没有 HTTPS。** 令牌是明文过 HTTP 传输，同网络可被截获。上证书的做法见上文。

## USDA FoodData Central 的 key

补录抓取用这个接口。几个高频困惑点：

- **浏览器直接打开 `https://api.nal.usda.gov/fdc/v1` 一定报 `API_KEY_MISSING`。**
  这个接口要求**每个请求**都带 `api_key` 参数，浏览器访问没带，不是服务坏了。
- 官方那句提示 `Get one at https://api.nal.usda.gov:443` 是错的（指向它自己），
  正确入口是 **https://fdc.nal.usda.gov/api-key-signup**，或总入口 https://api.data.gov/signup
- 表单只要 First name / Last name / Email，key 直接发到邮箱（40 个字符）
- 额度：注册 key **1000 次/小时**；`DEMO_KEY` 只有 **30 次/小时、50 次/天，且全站共享**
- 超额返回 429 并封 1 小时；响应头 `X-RateLimit-Remaining` 能看到剩余

自检（不用猜是 key 错还是超额）：

```bash
python -m scripts.check_fdc
```

它会打印 key 来源、剩余额度、命中的条目；`API_KEY_MISSING` / `API_KEY_INVALID` / `429`
三种情况分别给不同的处理建议。

配好之后：

```bash
# 编辑 /opt/code/nutri_graph/.env
# FDC_API_KEY=你的40位key
systemctl restart nutri-graph
python -m scripts.check_fdc        # 应显示 quota: 1000/1000 剩余
python -m scripts.run_fetch --limit 3
```
