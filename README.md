# astrbot_plugin_newapi_checkinapi

NewAPI 邮箱验证码绑定与每日签到额度直充插件。

## 功能

- 支持用户通过 NewAPI 数字 ID 或邮箱发起绑定。
- 机器人调用 NewAPI 管理接口查询用户资料。
- 用户确认绑定后，验证码发送到该 NewAPI 账号绑定邮箱。
- 用户在群聊或私聊中发送 6 位验证码即可完成绑定。
- 每个 NewAPI 账号只能绑定一个 QQ，每个 QQ 只能绑定一个 NewAPI 账号。
- 签到时通过 `POST /api/user/manage` 接口直接把额度加到账号上。
- 支持固定额度签到和随机额度签到。
- 签到达到目标额度阈值时自动扣除全部额度。
- 重复签到有惩罚扣除机制。
- 支持每日签到限制、重置小时、QQ 级签到记录（解绑后不丢失）。
- 支持简化额度显示（纯美元格式、自定义符号和位置）。
- 如果 NewAPI 账号邮箱为 `用户QQ号@qq.com`，可通过配置开启自动确认直接绑定，无需发送验证码。
- 解绑后有冷却期（默认 72 小时），冷却期内不可绑定新账号，防止刷签到。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/绑定 <数字ID或邮箱>` | 查询 NewAPI 用户并发起绑定 |
| `/确认绑定` | 确认绑定并发送邮箱验证码 |
| `/取消绑定` | 取消当前绑定流程 |
| `发送 6 位验证码` | 完成绑定 |
| `/签到` | 每日签到，额度直接加到绑定账号 |
| `/我的账号` | 查看账号信息、额度和调用次数 |
| `/解绑` | 解除绑定（进入冷却期） |
| `/签到帮助` | 显示帮助 |

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

3. 如果开启了 `auto_confirm_qq_email` 且 NewAPI 邮箱等于 `QQ号@qq.com`，直接绑定成功，跳过后续验证码流程。

4. 如果未开启邮箱验证（`require_email_verification: false`），直接绑定成功。

5. 插件展示账号 ID、用户名、显示名、脱敏邮箱、当前额度，并提示用户确认。

6. 用户发送：

   ```text
   /确认绑定
   ```

7. 插件向该 NewAPI 账号绑定邮箱发送 6 位验证码。

8. 用户在群聊或私聊中发送验证码，插件验证通过后保存绑定。

## 签到流程

用户发送：

```text
/签到
```

插件会：

1. 检查当前 QQ 是否已绑定。
2. 检查是否满足每日签到限制（基于 QQ 级记录，解绑重绑不重置）。
3. 请求 `GET /api/user/:id` 获取当前用户数据。
4. 如果签到达到目标额度阈值（`target_quota`），扣除全部额度。
5. 否则按固定额度或随机额度增加。
6. 保存本次签到时间并返回额度变化信息。

### 重复签到

当日已签到用户再次发送 `/签到`：

- 如果达到目标额度阈值，扣除全部额度。
- 否则按 `penalty_quota` 扣除惩罚额度（设为 0 仅提示不扣）。

## 配置说明

插件提供 `_conf_schema.json`，可在 AstrBot WebUI 中配置。

关键配置：

- `api_base_url`：NewAPI 站点地址
- `api_key`：NewAPI 管理员密钥
- `api_display_name`：全局 API 显示名称，默认 `NewAPI`
- `admin_user_id`：管理员用户 ID，请求头 `New-Api-User`
- `checkin_quota`：每次签到增加额度，默认 `500000`
- `checkin_quota_min` / `checkin_quota_max`：签到随机额度范围（同时大于 0 时启用随机，否则用固定额度）
- `target_quota`：目标额度阈值，签到达到该阈值时扣除全部额度（设为 0 关闭）
- `penalty_quota`：重复签到惩罚扣除额度（设为 0 仅提示不扣）
- `enable_daily_limit`：是否限制每日一次签到
- `reset_hour`：每日签到重置小时（北京时间）
- `require_email_verification`：是否启用邮箱验证码绑定
- `auto_confirm_qq_email`：当 NewAPI 邮箱等于 `QQ号@qq.com` 时自动确认并跳过验证直接绑定
- `unbind_cooldown_hours`：解绑后冷却时间（小时），默认 `72`，设为 `0` 关闭冷却
- `quota_to_money_rate`：额度转美元汇率，默认 `500000`
- `quota_symbol`：额度显示符号，默认 `$`
- `quota_symbol_position`：额度符号位置，可选 `before`（前）或 `after`（后）
- `verification_expire_seconds`：验证码有效期
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

包含绑定信息、QQ 级签到记录、解绑时间戳等。不会写入插件源码目录，避免插件更新时丢失数据。

## NewAPI 权限要求

配置的密钥需要具备以下管理接口权限：

- `GET /api/user/:id`
- `GET /api/user/search`
- `POST /api/user/manage`（额度管理）