# astrbot_plugin_newapi_checkinapi

NewAPI 邮箱验证码绑定与每日签到额度直充插件。

## 功能

- 支持用户通过 NewAPI 数字 ID 或邮箱发起绑定。
- 机器人调用 NewAPI 管理接口查询用户资料。
- 用户确认绑定后，验证码发送到该 NewAPI 账号绑定邮箱。
- 用户在群聊或私聊中发送 6 位验证码即可完成绑定。
- 每个 NewAPI 账号只能绑定一个 QQ，每个 QQ 只能绑定一个 NewAPI 账号。
- 签到时直接读取账号原额度，并通过 NewAPI 更新接口把奖励额度加到账号上。
- 支持每日签到限制、重置小时、签到额度、SMTP 邮件模板等配置。
- 如果 NewAPI 账号邮箱为 `用户QQ号@qq.com`，会在确认信息中标识；可通过配置开启自动确认并直接发送验证码。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/绑定 <数字ID或邮箱>` | 查询 NewAPI 用户并发起绑定 |
| `/确认绑定` | 确认绑定并发送邮箱验证码 |
| `/取消绑定` | 取消当前绑定流程 |
| `发送 6 位验证码` | 完成绑定 |
| `/签到` | 每日签到，额度直接加到绑定账号 |
| `/我的绑定` | 查看绑定与签到状态 |
| `/查询额度` | 查询绑定账号额度 |
| `/解绑` | 解除绑定 |
| `/NewAPI签到帮助` | 显示帮助 |

## 绑定流程

1. 用户发送：

   ```text
   /绑定 123
   ```

   或：

   ```text
   /绑定 user@example.com
   ```

2. 插件请求 NewAPI：
   - 数字 ID：`GET /api/user/:id`
   - 邮箱/用户名：`GET /api/user/search?keyword=...`

3. 插件展示账号 ID、用户名、显示名、脱敏邮箱、当前额度，并提示用户确认。

4. 用户发送：

   ```text
   /确认绑定
   ```

5. 插件向该 NewAPI 账号绑定邮箱发送 6 位验证码。

6. 用户在群聊或私聊中发送验证码，插件验证通过后保存绑定。

## 签到流程

用户发送：

```text
/签到
```

插件会：

1. 检查当前 QQ 是否已绑定。
2. 检查是否满足每日签到限制。
3. 请求 `GET /api/user/:id` 获取当前用户数据。
4. 计算 `新额度 = 原 quota + checkin_quota`。
5. 请求 `PUT /api/user/` 更新用户额度。
6. 保存本次签到时间并返回原额度、增加额度、现额度。

## 配置说明

插件提供 `_conf_schema.json`，可在 AstrBot WebUI 中配置。

关键配置：

- `api_base_url`：NewAPI 站点地址，默认 `https://api.xinjianya.top/`
- `api_key`：NewAPI 管理员密钥
- `admin_user_id`：管理员用户 ID，请求头 `New-Api-User`
- `checkin_quota`：每次签到增加额度，默认 `500000`
- `enable_daily_limit`：是否限制每日一次签到
- `reset_hour`：每日签到重置小时（北京时间）
- `verification_expire_seconds`：验证码有效期
- `auto_confirm_qq_email`：当 NewAPI 邮箱等于 `QQ号@qq.com` 时是否自动进入验证码发送步骤
- `smtp_*` / `from_address`：验证码邮件发送配置

## SMTP 注意事项

如使用 QQ 邮箱发信：

- `smtp_host`: `smtp.qq.com`
- `smtp_port`: `465`
- `smtp_use_ssl`: `true`
- `smtp_username`: 发件邮箱
- `smtp_password`: QQ 邮箱 SMTP 授权码，不是 QQ 密码
- `from_address`: 发件邮箱，通常与 `smtp_username` 一致

## 数据存储

绑定数据存储在 AstrBot 插件数据目录：

```text
data/plugins_data/astrbot_plugin_newapipro/state.json
```

不会写入插件源码目录，避免插件更新时丢失数据。

## NewAPI 权限要求

配置的密钥需要具备以下管理接口权限：

- `GET /api/user/:id`
- `GET /api/user/search`
- `PUT /api/user/`

更新额度时插件会携带用户已有的 `username`、`display_name`、`email`、`role`、`status`、`group` 等基础字段，并只修改 `quota` 为增加后的值。
