# 真实短信验证码接入说明（阿里云「短信认证」/ 号码认证服务 PNVS）

登录页现在默认走真实下发（`SMS_PROVIDER=dypns`）。**在下面的凭据补齐之前，
点「获取验证码」会拿到 503**，启动日志会直接列出缺哪几项 —— 这是故意的：
宁可明确说「通道没配好」，也不要假装发出去了。

---

## 1. 为什么是这个产品，不是「短信服务 SMS」

| | 短信服务 SMS | 短信认证 PNVS（本项目用的） |
|---|---|---|
| 签名 | 自己申请，需实名制报备 | 用平台**赠送签名**，不可自定义 |
| 模板 | 自己申请，需审核 | 用平台**赠送模板**，与赠送签名搭配 |
| 验证码 | 我方生成、自己落库校验 | **平台生成并托管**，只能调 `CheckSmsVerifyCode` 核验 |
| 个人实名账号 | 自用签名通不过运营商报备，实际发不出去 | 可用 |

代价：验证码不由我们生成，所以 `security/sms.py` 里 `DypnsProvider`
只有 `send()` / `verify()` 两个远端动作，本地 `sms_code` 表只在 `console`
模式（开发调试）下使用。

官方接入指引：<https://help.aliyun.com/zh/pnvs/use-cases/sms-verify-for-individual-developers>

---

## 2. 开通步骤

1. **开通号码认证服务**：控制台搜索「号码认证服务」→ 开通（短信认证按短信条数计费，
   核验免费；提交成功但运营商回执失败不计费）。
2. **拿赠送签名 / 赠送模板**：号码认证控制台 → 短信认证 → 「赠送签名配置」/
   「赠送模板配置」页面复制 `SignName` 与 `TemplateCode`。
   两者必须配套使用，自定义签名/模板会被拒绝。
3. **建 RAM 子账号 + AccessKey**：不要用主账号 AK。
   授权只给两个 Action 就够了：
   ```json
   {"Action": ["dypns:SendSmsVerifyCode", "dypns:CheckSmsVerifyCode"],
    "Resource": "*", "Effect": "Allow"}
   ```
   AK 走环境变量传入，不要写进代码或镜像。
4. **（可选）建认证方案**：控制台里建了方案就把名字填进 `SMS_SCHEME_NAME`；
   没建就留空，走「默认方案」。注意 `send` 和 `verify` 必须用**同一个**方案名，
   否则核验时找不到那条验证码。
5. **装 SDK**：`pip install -r requirements.txt`（已含
   `alibabacloud_dypnsapi20170525`），生产只装这一个包，不引其它阿里云依赖。

---

## 3. 填 .env

```dotenv
SMS_PROVIDER=dypns
ALIBABA_CLOUD_ACCESS_KEY_ID=LTAI...
ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
SMS_SIGN_NAME=控制台复制的赠送签名
SMS_TEMPLATE_CODE=控制台复制的赠送模板 CODE
SMS_SCHEME_NAME=                # 没建方案就留空
SMS_TEMPLATE_PARAM={"code":"##code##","min":"5"}
SMS_RETURN_VERIFY_CODE=false    # 联调可临时 true 让接口回传验证码，生产必须 false
```

改完**必须重启服务**才生效（`systemctl restart nutri-graph`）。

---

## 4. 自检

启动日志应当出现：

```
短信通道 dypns 就绪：签名=xxx 模板=SMS_xxxx 验证码 6 位 / 300 秒
```

缺东西时会是：

```
短信通道 dypns 不可用：缺少配置 ALIBABA_CLOUD_ACCESS_KEY_ID, ...；登录将无法发送验证码（/auth/sms 返回 503）
```

手工打一次（换成自己的手机号）：

```bash
curl -sS -X POST http://127.0.0.1:8000/auth/sms \
  -H 'Content-Type: application/json' -d '{"phone":"13800138000"}'
# {"ok":true,"provider":"dypns","phone_mask":"138****8000","expires_in":300,"interval":60,...}
```

拿到短信后：

```bash
curl -sS -X POST http://127.0.0.1:8000/auth/token \
  -H 'Content-Type: application/json' -d '{"phone":"13800138000","code":"短信里的6位"}'
```

---

## 5. 防刷与费用（别只看能不能发通）

通道本身不管短信轰炸，钱还是自己出。已经落地的几道闸（见 `config.py`）：

| 层 | 默认阈值 |
|---|---|
| 手机号 | 60s 一次 / 5 次每小时 / 10 次每天 |
| IP | 10 次每小时 |
| 设备指纹 | 10 次每天 |
| 全局熔断 | 200 条每天 |
| 图形验证码 | 同 IP 第 2 条起强制 |
| 核验失败 | 连续 5 次锁 15 分钟（IP 维度 50 次，防 NAT 误伤） |

`SMS_MAX_GLOBAL_DAY` 建议按预算设：一旦被打爆，超全局额度后所有人一起降级，
比账单失控好。

---

## 6. 回退本地调试

不接云端、只想本地跑通登录链路：

```bash
SMS_PROVIDER=console DEBUG=true uvicorn app:app
```

此时不真发短信，验证码打在日志里，`DEBUG=true` 时接口会回传 `dev_code`
（前端会把它显示在登录页的黄条里）。**生产不要开** `DEBUG`。
