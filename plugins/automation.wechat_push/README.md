# automation.wechat_push — 微信推送通知自动化插件

监听 T3FAP 平台的任务和系统事件，通过 [微信推送服务](https://wechat.powerautomate.top) 将通知消息实时推送到微信。

## 功能特性

- 实时推送任务完成、失败、开始等事件通知到微信
- 支持自定义事件订阅列表，按需开启通知类型
- 内置失败自动重试机制，保障消息送达
- 可配置消息标题前缀，便于在微信中快速识别来源
- 可控制推送内容的详细程度（精简模式 / 详细模式）
- 纯标准库实现（urllib），无额外依赖

## 推送接口

```
POST https://wechat.powerautomate.top/api/push
Content-Type: application/json

{
  "uid": "你的推送UID",
  "title": "消息标题",
  "content": "消息正文"
}
```

## 目录结构

```
plugins/automation.wechat_push/
  plugin.json           # 插件清单
  backend/
    plugin.py           # 后端入口
  README.md             # 本文件
```

## 安装方式

### 方式一：插件中心安装

在 T3FAP 插件中心填入本仓库地址：

```
https://github.com/lzylzyyz/T3FAP/tree/main/plugins
```

然后搜索 `wechat_push` 即可安装。

### 方式二：手动放置

将 `automation.wechat_push` 目录复制到 T3FAP 数据目录的 `plugins/` 下，重启服务即可。

## 配置说明

安装后在插件设置页面填写以下配置：

| 配置项 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `enabled` | boolean | 否 | `true` | 是否启用插件 |
| `uid` | string | **是** | — | 在 wechat.powerautomate.top 获取的推送 UID |
| `push_url` | string | 否 | `https://wechat.powerautomate.top/api/push` | 推送接口地址，可替换为自建反代 |
| `title_prefix` | string | 否 | `[T3FAP]` | 消息标题统一前缀 |
| `enabled_events` | string | 否 | `task.completed,task.failed` | 订阅事件列表，逗号分隔 |
| `include_detail` | boolean | 否 | `true` | 是否在内容中包含事件详细信息 |
| `timeout_seconds` | integer | 否 | `10` | 请求超时时间（秒） |
| `retry_count` | integer | 否 | `2` | 失败重试次数 |

### 获取 UID

1. 访问 [wechat.powerautomate.top](https://wechat.powerautomate.top)
2. 使用微信扫码登录
3. 在个人中心复制你的推送 UID

### 支持的事件类型

| 事件类型 | 说明 |
| --- | --- |
| `task.completed` | 任务执行完成 |
| `task.failed` | 任务执行失败 |
| `task.started` | 任务开始执行 |
| `resource.found` | 发现新资源 |
| `system.warning` | 系统告警 |

## 推送消息示例

### 任务完成

```
标题：[T3FAP] 任务完成
正文：任务「斗破苍穹 第4集 下载」已执行完成。

任务ID：task_20260703_001
来源：catalog.tencent
```

### 任务失败

```
标题：[T3FAP] 任务失败
正文：任务「庆余年 第3集 转存」执行失败：
网盘空间不足，转存失败。

任务ID：task_20260703_002
摘要：Quark网盘转存失败
```

## 开发说明

- 后端入口：`backend.plugin:plugin`
- 继承：`BasePlugin` + `AutomationProvider`
- HTTP 请求使用 Python 标准库 `urllib`，无需安装额外依赖
- 所有网络请求均设置了超时控制
- 敏感配置（uid）已标记为 `secret: true`

## 版本历史

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| 1.0.0 | 2026-07-03 | 初始版本，支持事件订阅、消息构建、HTTP推送、失败重试 |

## License

MIT
