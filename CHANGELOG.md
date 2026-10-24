# Changelog

## v1.0.0 (2026-10-24)

首个可用版本。

### 新增

- 独立 WebUI 页面「QQ群聊一键操作」。
- 群列表读取：OneBot `get_group_list`、历史消息缓存、全局主动回复白名单、手动添加群号。
- 单群配置：
  - `remark` 自定义群备注（不修改平台群名）；
  - `auto_reply_enabled` 主动回复开关；
  - `meme_enabled` 自动表情包开关；
  - `respond_enabled` 默认 AI 回应开关；
  - `reply_probability` 独立主动回复概率（0～1，页面按百分比显示）。
- 原子写入、`.bak` 备份、损坏恢复与并发写保护。
- AstrBot 主动回复群级接管：未配置群保持原逻辑，配置群使用独立概率，保留全局开关/方式/白名单。
- 默认 AI 聊天拦截：不 `stop_event()`，保留管理员指令和其他插件功能。
- meme_manager 事件来源门控与 stealer 自动发送门控。
- 后端输入校验、WebUI 登录校验、API Key 只读限制。
- 离线测试 52 项。
- 在隔离的 AstrBot v4.29.1 实例中验证插件加载、页面打开、页面保存、API 校验和重启后配置持久化。
- 在当前 AstrBot 主实例中通过 WebUI 重载插件，验证插件激活、页面发现和 OneBot 群列表读取（未修改群配置、未发送群消息、未调用模型）。

### 已知限制

- 主动回复接管依赖 AstrBot `GroupChatContext.need_active_reply` 内部方法。
- meme_manager / stealer 适配分别按 5.2.5 / 3.1.6 的运行时行为测试。
- 只控制自动发表情，不控制表情收集/偷取。
