# 百度网盘驱动插件 (drive.baidu)

## 概述

`drive.baidu` 是 T3FAP 的百度网盘驱动插件，实现 `DriveProvider` 接口，提供分享链接解析、文件转存、账号管理和文件系统操作能力。

## 为什么需要这个插件

T3FAP 核心引擎在收到分享链接时，会根据各 `drive.*` 插件在 `get_contract()` 中声明的 `share_url_patterns` 来路由链接。如果没有任何 drive 插件声明支持 `https://pan.baidu.com/s/` 链接，就会出现 **"分享链接暂不支持"** 错误。

本插件在 `get_contract()` 中声明了：

```python
"share_url_patterns": [
    "https://pan.baidu.com/s/",
    "https://pan.baidu.com/share/init?surl=",
    "https://pan.baidu.com/wap/",
]
```

这样 T3FAP 就能正确识别百度网盘分享链接并路由到本插件处理。

## 功能特性

- 分享链接解析：支持 `pan.baidu.com/s/xxx?pwd=xxx` 格式
- 分享浏览：列出分享目录中的文件和子目录
- 文件转存：将分享文件转存到指定网盘目录，支持选文件转存或全量转存
- 账号测试：验证 BDUSS/STOKEN Cookie 是否有效，显示账号信息和网盘容量
- 文件系统操作：列目录、创建目录、重命名、删除
- 下载链接获取：获取文件下载直链
- 自动重试：内置 API 请求重试和频率限制处理
- 纯标准库实现（urllib），无额外依赖

## 目录结构

```
plugins/drive.baidu/
  plugin.json           # 插件清单
  backend/
    plugin.py           # 后端入口（BaiduDrivePlugin）
  README.md             # 使用文档
```

## 安装方式

### 方式 1：插件市场安装

在 T3FAP 插件中心填写仓库地址：

```
https://github.com/lzylzyyz/T3FAP/tree/main/plugins
```

然后安装 `drive.baidu`。

### 方式 2：手动放置

将 `drive.baidu` 目录复制到 T3FAP 的 `plugins/` 目录下，重启服务。

## 配置说明

### 获取百度网盘 Cookie

1. 浏览器打开 [pan.baidu.com](https://pan.baidu.com) 并登录
2. 按 `F12` 打开开发者工具
3. 切换到 **Application** → **Cookies** → `https://pan.baidu.com`
4. 找到以下 Cookie 值并复制：

| Cookie 名称 | 说明 | 是否必填 |
|-------------|------|----------|
| `BDUSS` | 百度网盘登录凭证 | **是** |
| `STOKEN` | 百度安全令牌 | **是** |
| `PTOKEN` | 百度权限令牌 | 否 |

### 配置项

| 配置项 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| bduss | 是 | — | 百度网盘 BDUSS Cookie 值 |
| stoken | 是 | — | 百度网盘 STOKEN Cookie 值 |
| ptoken | 否 | — | 百度网盘 PTOKEN Cookie 值 |
| default_save_dir | 否 | / | 默认转存目录路径 |
| request_interval | 否 | 2 | API 请求最小间隔（秒） |
| timeout_seconds | 否 | 15 | 请求超时（秒） |
| retry_count | 否 | 2 | 失败重试次数 |

## 使用方法

### 测试账号

在 T3FAP 设置页面配置 Cookie 后，点击"测试"按钮验证账号是否有效。成功后会显示用户名和网盘容量信息。

### 解析分享链接

在 T3FAP 任务或资源页面输入百度网盘分享链接：

```
https://pan.baidu.com/s/1qu3azDndh0kIuT3yQQsI2A?pwd=rkn3
```

插件会自动：
1. 标准化链接格式
2. 使用提取码访问分享
3. 获取 uk / share_id / bdstoken
4. 列出分享中的文件列表

### 转存文件

解析成功后，选择要转存的文件，指定目标目录，插件会：
1. 确保目标目录存在（逐级创建）
2. 分批转存文件（每批最多 100 个）
3. 自动处理频率限制（-65 等待 10 秒重试）
4. 跳过已存在的文件（31061）

### 支持的分享链接格式

| 格式 | 示例 |
|------|------|
| 标准格式 | `https://pan.baidu.com/s/1qu3azDndh0kIuT3yQQsI2A?pwd=rkn3` |
| 无提取码 | `https://pan.baidu.com/s/1qu3azDndh0kIuT3yQQsI2A` |
| init 格式 | `https://pan.baidu.com/share/init?surl=1qu3azDndh0kIuT3yQQsI2A&pwd=rkn3` |

## 错误码说明

| 错误码 | 含义 | 处理方式 |
|--------|------|----------|
| -6 | 身份验证失败 | 检查 BDUSS/STOKEN 是否正确或已过期 |
| -9 | 文件不存在 | 确认文件是否已被删除 |
| -65 | 操作频率限制 | 自动等待 10 秒后重试 |
| 115 | 分享文件禁止分享 | 无法处理，联系分享者 |
| 145 | 分享链接已失效 | 链接已过期，需获取新链接 |
| 200025 | 提取码错误 | 检查提取码是否正确 |
| 31061 | 文件已存在 | 自动跳过，视为成功 |
| 31062 | 目录名非法 | 检查目录名是否含非法字符 |
| -33 | 转存超限 | 自动等待 5 秒后重试 |

## 技术实现

- 使用百度网盘 Web API（非开放平台 API），通过 Cookie 认证
- 分享解析：`pan.baidu.com/share/wxlist` + `pan.baidu.com/share/tplconfig`
- 文件转存：`pan.baidu.com/share/transfer`
- 文件列表：`pan.baidu.com/api/list`
- 创建目录：`pan.baidu.com/api/create`
- 下载链接：`pan.baidu.com/api/sharedownload`
- 纯标准库 `urllib` 实现 HTTP 请求，无外部依赖

## 版本历史

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| 1.0.0 | 2026-07-03 | 初始版本，支持分享解析、文件转存、账号管理、文件系统操作 |
