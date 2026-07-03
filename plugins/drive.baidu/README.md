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
- **防御性导入**：SDK 缺少 `HealthReport` 时自动 fallback，不会崩溃

## 目录结构

```
plugins/drive.baidu/
  plugin.json           # 插件清单
  backend/
    __init__.py         # Python 包标识（必须存在）
    plugin.py           # 后端入口（BaiduDrivePlugin）
  README.md             # 使用文档
```

> **重要**：`backend/__init__.py` 必须存在，否则 T3FAP 的插件加载器无法将 `backend` 作为 Python 包导入，会报 500 错误。

## 安装方式

### 方式 1：手动放置到容器

```bash
# 将插件目录复制到 T3FAP 容器的 plugins 目录
docker cp drive.baidu t3fap:/app/plugins/

# 重启 T3FAP 容器使插件生效
docker restart t3fap
```

### 方式 2：挂载卷

在 `docker-compose.yml` 中挂载插件目录：

```yaml
volumes:
  - ./plugins/drive.baidu:/app/plugins/drive.baidu
```

### 方式 3：T3FAP 插件中心

如果 T3FAP 支持从仓库安装，填写仓库地址并安装 `drive.baidu`。

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

## 安装报 500 错误排查

如果安装时出现 `Request failed: 500`，按以下步骤排查：

### 1. 检查文件结构

确保 `backend/__init__.py` 存在：

```bash
docker exec t3fap ls -la /app/plugins/drive.baidu/backend/
# 必须看到 __init__.py 和 plugin.py
```

### 2. 查看容器日志

```bash
docker logs t3fap --tail 50 2>&1 | grep -i error
```

日志中会有具体的 Python traceback，例如：
- `ImportError: cannot import name 'HealthReport'` → SDK 版本问题
- `ModuleNotFoundError: No module named 'backend'` → 缺少 `__init__.py`
- `SyntaxError` → 文件编码问题

### 3. 手动测试导入

```bash
docker exec t3fap python -c "
import sys
sys.path.insert(0, '/app/plugins/drive.baidu')
import importlib
mod = importlib.import_module('backend.plugin')
print('OK:', mod.plugin.plugin_id)
"
```

### 4. 常见原因

| 原因 | 解决方法 |
|------|----------|
| 缺少 `backend/__init__.py` | 创建空文件（本插件已包含） |
| SDK 没有 `HealthReport` | 本插件已做防御处理，不会崩溃 |
| 文件编码错误 | 确保 UTF-8 无 BOM |
| `plugin.json` 格式错误 | 用 `python -c "import json; json.load(open('plugin.json'))"` 检查 |

## 技术实现

- 使用百度网盘 Web API（非开放平台 API），通过 Cookie 认证
- 分享解析：`pan.baidu.com/share/wxlist` + `pan.baidu.com/share/tplconfig`
- 文件转存：`pan.baidu.com/share/transfer`
- 文件列表：`pan.baidu.com/api/list`
- 创建目录：`pan.baidu.com/api/create`
- 下载链接：`pan.baidu.com/api/sharedownload`
- 纯标准库 `urllib` 实现 HTTP 请求，无外部依赖
- 防御性导入：`try/except` 包裹 SDK 导入，`HealthReport` 缺失时自动 fallback

## 版本历史

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| 1.0.0 | 2026-07-04 | 重写版本：防御性导入、删除多余方法、精简 JSON、对齐官方 drive.demo 结构 |
| 0.9.0 | 2026-07-03 | 初始版本 |
