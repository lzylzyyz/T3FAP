"""
百度网盘驱动插件 - T3FAP DriveProvider

实现分享链接解析、文件转存、账号管理和文件系统操作。
使用百度网盘 Web API，通过 BDUSS + STOKEN Cookie 认证。
纯标准库实现，无外部依赖。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any

from core.sdk import BasePlugin, HealthReport


# ============================================================
# 常量
# ============================================================

_BAIDU_PAN_BASE = "https://pan.baidu.com"
_REST_BASE = "https://pan.baidu.com/rest/2.0/xpc"

# 分享链接 URL 正则 - 匹配 https://pan.baidu.com/s/xxxx?pwd=xxxx
_SHARE_URL_RE = re.compile(
    r"^https?://pan\.baidu\.com/s/[a-zA-Z0-9_-]+(?:\?pwd=[a-zA-Z0-9]+)?$"
)

# share/init?surl=xxx 格式 → 转成 /s/xxx 格式
_INIT_URL_RE = re.compile(
    r"^https?://pan\.baidu\.com/share/init\?surl=([a-zA-Z0-9_-]+)(?:&pwd=([a-zA-Z0-9]+))?$"
)

# 百度错误码含义
_ERROR_MESSAGES: dict[int, str] = {
    -6: "身份验证失败，请检查 BDUSS/STOKEN 是否正确或已过期",
    -9: "文件不存在",
    -62: "参数错误",
    -65: "操作过于频繁，请稍后再试",
    2: "参数错误",
    -7: "路径名非法或不支持该操作",
    115: "分享文件禁止分享",
    145: "分享链接已失效",
    200025: "提取码错误",
    31023: "目录状态不明确",
    31061: "文件已存在",
    31062: "目录名非法",
    31066: "路径不存在",
    31064: "转存文件列表为空",
    -33: "转存超限，请稍后再试",
    4: "存储好像出问题了",
}


# ============================================================
# 辅助函数
# ============================================================

def _build_cookie_str(cookies: dict[str, str]) -> str:
    """将 cookie 字典拼成 Cookie 请求头格式。"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _parse_share_url(raw_url: str) -> tuple[str, str | None]:
    """
    标准化百度分享链接。

    支持两种输入格式:
      https://pan.baidu.com/s/1AbCdE?pwd=xxxx
      https://pan.baidu.com/share/init?surl=1AbCdE&pwd=xxxx

    返回 (标准分享链接, 提取码或None)
    """
    url = raw_url.strip()

    # share/init?surl=xxx 格式
    m = _INIT_URL_RE.match(url)
    if m:
        surl = m.group(1)
        # surl 通常以 1 开头，对应的 /s/ 链接需要去掉前缀 1
        short = surl[1:] if surl.startswith("1") else surl
        pwd = m.group(2) or None
        normalized = f"https://pan.baidu.com/s/1{short}"
        if pwd:
            normalized += f"?pwd={pwd}"
        return normalized, pwd

    # /s/xxx 格式
    if _SHARE_URL_RE.match(url):
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        pwd = params.get("pwd", [None])[0]
        return url, pwd

    # 尝试从任意 URL 中提取 s 参数
    parsed = urllib.parse.urlparse(url)
    if "pan.baidu.com" in (parsed.hostname or ""):
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 2 and path_parts[0] == "s":
            surl = path_parts[1]
            params = urllib.parse.parse_qs(parsed.query)
            pwd = params.get("pwd", [None])[0]
            normalized = f"https://pan.baidu.com/s/{surl}"
            if pwd:
                normalized += f"?pwd={pwd}"
            return normalized, pwd

    return url, None


def _format_baidu_error(err_str: str) -> str:
    """从百度 API 返回中提取用户友好的错误信息。"""
    # 尝试 JSON 解析
    try:
        data = json.loads(err_str)
        errno = data.get("errno") or data.get("error_code") or data.get("errNo")
        if errno is not None:
            errno_int = int(errno)
            known = _ERROR_MESSAGES.get(errno_int)
            if known:
                return known
            errmsg = data.get("errmsg") or data.get("error_msg") or ""
            if errmsg:
                return f"百度网盘错误 (errno={errno_int}): {errmsg}"
            return f"百度网盘错误 (errno={errno_int})"
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # 匹配 error_code: N 格式
    m = re.search(r"error_code:\s*(-?\d+)", err_str)
    if m:
        errno_int = int(m.group(1))
        known = _ERROR_MESSAGES.get(errno_int)
        if known:
            return known
        return f"百度网盘错误 (errno={errno_int})"

    return err_str if err_str else "未知错误"


def _safe_str(val: Any, default: str = "") -> str:
    """安全转字符串。"""
    if val is None:
        return default
    return str(val)


# ============================================================
# 百度网盘 API 客户端
# ============================================================

class BaiduPanClient:
    """
    百度网盘 Web API 客户端。

    使用 BDUSS + STOKEN Cookie 认证，通过 urllib 调用百度网盘 Web API。
    所有方法都设置了超时和错误处理。
    """

    def __init__(
        self,
        bduss: str,
        stoken: str = "",
        ptoken: str = "",
        timeout: int = 15,
        retry: int = 2,
        interval: float = 2.0,
    ) -> None:
        self.cookies: dict[str, str] = {"BDUSS": bduss.strip()}
        if stoken.strip():
            self.cookies["STOKEN"] = stoken.strip()
        if ptoken.strip():
            self.cookies["PTOKEN"] = ptoken.strip()

        self.timeout = timeout
        self.retry = retry
        self.interval = interval
        self._last_request = 0.0

    # --- 内部 HTTP 工具 ---

    def _wait_interval(self) -> None:
        """确保请求之间有最小间隔。"""
        now = time.time()
        elapsed = now - self._last_request
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_request = time.time()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发起 GET 请求，返回 JSON。"""
        if params:
            query = urllib.parse.urlencode(params)
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{query}"

        return self._request("GET", url, None)

    def _post(self, url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """发起 POST 请求，返回 JSON。"""
        body = None
        if data:
            body = urllib.parse.urlencode(data).encode("utf-8")
        return self._request("POST", url, body)

    def _request(self, method: str, url: str, body: bytes | None) -> dict[str, Any]:
        """带重试的 HTTP 请求。"""
        last_err: Exception | None = None

        for attempt in range(self.retry + 1):
            self._wait_interval()
            try:
                req = urllib.request.Request(url, method=method, data=body)
                req.add_header("Cookie", _build_cookie_str(self.cookies))
                req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                req.add_header("Referer", _BAIDU_PAN_BASE + "/")
                if body is not None:
                    req.add_header("Content-Type", "application/x-www-form-urlencoded")

                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")

                data = json.loads(raw)
                # 百度 API 返回 errno=0 表示成功
                if isinstance(data, dict) and data.get("errno") not in (None, 0):
                    # 特殊处理：31061 文件已存在 → 返回成功语义
                    errno = data.get("errno")
                    if errno == 31061:
                        return data
                    raise BaiduPanError(errno, data)
                return data

            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (401, 403):
                    raise BaiduPanError(-6, {"errno": -6, "errmsg": "认证失败，请检查 Cookie"}) from e
                if attempt < self.retry:
                    wait = min(2 ** (attempt + 1), 10)
                    time.sleep(wait)
                    continue
                raise BaiduPanError(-1, {"errno": -1, "errmsg": f"HTTP {e.code}: {e.reason}"}) from e

            except urllib.error.URLError as e:
                last_err = e
                if attempt < self.retry:
                    wait = min(2 ** (attempt + 1), 10)
                    time.sleep(wait)
                    continue
                raise BaiduPanError(-2, {"errno": -2, "errmsg": f"网络错误: {e.reason}"}) from e

            except json.JSONDecodeError as e:
                last_err = e
                if attempt < self.retry:
                    time.sleep(2)
                    continue
                raise BaiduPanError(-3, {"errno": -3, "errmsg": "响应解析失败"}) from e

            except BaiduPanError:
                raise

            except Exception as e:
                last_err = e
                if attempt < self.retry:
                    wait = min(2 ** (attempt + 1), 10)
                    time.sleep(wait)
                    continue
                raise BaiduPanError(-99, {"errno": -99, "errmsg": str(e)}) from e

        raise BaiduPanError(-99, {"errno": -99, "errmsg": str(last_err or "未知错误")})

    # --- 账号相关 ---

    def user_info(self) -> dict[str, Any]:
        """获取当前登录用户信息。"""
        url = f"{_BAIDU_PAN_BASE}/rest/2.0/xpan/nas"
        params = {"method": "uinfo"}
        return self._get(url, params)

    def quota(self) -> dict[str, Any]:
        """获取网盘容量信息。"""
        url = f"{_BAIDU_PAN_BASE}/api/quota"
        params = {"checkfree": "1", "checkexpire": "1"}
        return self._get(url, params)

    # --- 分享链接解析 ---

    def access_share(self, share_url: str, pwd: str | None = None) -> dict[str, Any]:
        """
        访问分享链接，获取 uk / share_id / bdstoken 等信息。

        返回示例:
          {
            "uk": 12345678,
            "share_id": 87654321,
            "bdstoken": "abcdef...",
            "share_title": "分享标题",
            ...
          }
        """
        url = f"{_BAIDU_PAN_BASE}/share/wxlist"
        params: dict[str, str] = {
            "clienttype": "0",
            "app_id": "250528",
            "web": "1",
            "channel": "dingding",
            "url": share_url,
        }
        if pwd:
            params["pwd"] = pwd

        data = self._get(url, params)

        # 检查错误
        errno = data.get("errno", 0)
        if errno != 0:
            raise BaiduPanError(errno, data)

        # 提取关键信息
        uk = _safe_str(data.get("uk") or data.get("share_uk"))
        share_id = _safe_str(data.get("shareid") or data.get("share_id"))
        bdstoken = _safe_str(data.get("bdstoken") or data.get("bdstoken", ""))

        if not uk or not share_id:
            # 尝试另一种 API
            url2 = f"{_BAIDU_PAN_BASE}/share/tplconfig"
            params2: dict[str, str] = {
                "fields": "sign,uk,shareid,timestamp,bdstoken",
                "url": share_url,
            }
            if pwd:
                params2["pwd"] = pwd
            data2 = self._get(url2, params2)
            errno2 = data2.get("errno", 0)
            if errno2 != 0:
                raise BaiduPanError(errno2, data2)
            inner = data2.get("data", {})
            uk = _safe_str(inner.get("uk"))
            share_id = _safe_str(inner.get("shareid"))
            bdstoken = _safe_str(inner.get("bdstoken"))
            data.update(inner)

        return {
            "uk": uk,
            "share_id": share_id,
            "bdstoken": bdstoken,
            "share_url": share_url,
            "pwd": pwd or "",
            "raw": data,
        }

    def share_paths(self, share_url: str, pwd: str | None = None) -> list[dict[str, Any]]:
        """
        获取分享链接根目录文件列表。

        返回文件列表，每个文件包含:
          - fs_id: 文件 ID
          - server_filename: 文件名
          - size: 文件大小（字节）
          - isdir: 是否目录 (1=目录, 0=文件)
          - path: 文件在分享中的路径
        """
        share_info = self.access_share(share_url, pwd)
        return self._list_share_files(
            share_info["uk"],
            share_info["share_id"],
            share_info["bdstoken"],
            share_url,
            parent_path="/",
        )

    def _list_share_files(
        self,
        uk: str,
        share_id: str,
        bdstoken: str,
        share_url: str,
        parent_path: str = "/",
        page: int = 1,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """分页获取分享目录内容。"""
        all_files: list[dict[str, Any]] = []

        while True:
            url = f"{_BAIDU_PAN_BASE}/share/wxlist"
            params: dict[str, str] = {
                "clienttype": "0",
                "app_id": "250528",
                "web": "1",
                "page": str(page),
                "num": str(page_size),
                "dir": parent_path,
                "url": share_url,
            }
            data = self._get(url, params)
            errno = data.get("errno", 0)
            if errno != 0:
                raise BaiduPanError(errno, data)

            file_list = data.get("list", [])
            if not file_list:
                break

            for item in file_list:
                fs_id = _safe_str(item.get("fs_id"))
                name = _safe_str(item.get("server_filename"))
                is_dir = int(item.get("isdir", 0))
                size = int(item.get("size", 0))
                path = _safe_str(item.get("path"), parent_path + name)
                all_files.append({
                    "fs_id": fs_id,
                    "name": name,
                    "type": "folder" if is_dir else "file",
                    "size": size,
                    "path": path,
                    "parent_path": parent_path,
                    "is_dir": is_dir == 1,
                })

            # 检查是否还有更多
            total = int(data.get("total_count", len(all_files)))
            if len(file_list) < page_size or len(all_files) >= total:
                break
            page += 1

        return all_files

    def list_share_dir(
        self,
        uk: str,
        share_id: str,
        bdstoken: str,
        share_url: str,
        parent_path: str = "/",
    ) -> list[dict[str, Any]]:
        """列出分享子目录内容。"""
        return self._list_share_files(uk, share_id, bdstoken, share_url, parent_path)

    # --- 文件转存 ---

    def transfer_share_files(
        self,
        uk: str,
        share_id: str,
        bdstoken: str,
        share_url: str,
        fs_ids: list[str],
        target_dir: str,
    ) -> dict[str, Any]:
        """
        将分享文件转存到指定网盘目录。

        返回:
          {
            "success": True/False,
            "saved_count": N,
            "errno": 0,
            "info": {...}
          }
        """
        if not fs_ids:
            return {"success": True, "saved_count": 0, "errno": 0, "info": {}}

        url = f"{_BAIDU_PAN_BASE}/share/transfer"
        params: dict[str, str] = {
            "app_id": "250528",
            "web": "1",
            "channel": "dingding",
            "clienttype": "0",
        }
        post_data: dict[str, str] = {
            "fsidlist": json.dumps(fs_ids),
            "path": target_dir,
            "url": share_url,
        }
        if uk:
            post_data["uk"] = uk
        if share_id:
            post_data["shareid"] = share_id

        data = self._post(f"{url}?{urllib.parse.urlencode(params)}", post_data)

        errno = data.get("errno", 0)
        # errno=0 或 errno=31061(已存在) 都视为成功
        if errno in (0, 31061):
            info = data.get("info", {}) or {}
            # info 可能包含 ErrNo 字段
            inner_errno = info.get("ErrNo", errno) if isinstance(info, dict) else errno
            if inner_errno in (0, 31061, -33):
                saved = info.get("savedCount", 0) if isinstance(info, dict) else 0
                return {
                    "success": True,
                    "saved_count": saved or len(fs_ids),
                    "errno": 0,
                    "info": info,
                }
            raise BaiduPanError(inner_errno, data)

        raise BaiduPanError(errno, data)

    # --- 文件系统操作 ---

    def list_files(self, dir_path: str = "/", page: int = 1, page_size: int = 100) -> dict[str, Any]:
        """列出网盘目录文件。"""
        url = f"{_BAIDU_PAN_BASE}/api/list"
        params: dict[str, str] = {
            "order": "name",
            "desc": "0",
            "showempty": "0",
            "web": "5",
            "page": str(page),
            "num": str(page_size),
            "dir": dir_path,
        }
        return self._get(url, params)

    def makedir(self, path: str) -> dict[str, Any]:
        """创建目录。"""
        url = f"{_BAIDU_PAN_BASE}/api/create"
        params = {"a": "commit"}
        post_data = {"path": path, "isdir": "1"}
        return self._post(f"{url}?{urllib.parse.urlencode(params)}", post_data)

    def ensure_dir(self, path: str) -> dict[str, Any]:
        """确保目录存在，不存在则逐级创建。"""
        path = path.strip()
        if not path or path == "/":
            return {"success": True, "path": path, "existed": True}

        # 尝试列举父目录确认目录是否存在
        parent = path.rstrip("/").rsplit("/", 1)[0] or "/"
        name = path.rstrip("/").rsplit("/", 1)[-1]

        try:
            data = self.list_files(parent, 1, 200)
            errno = data.get("errno", 0)
            if errno == 0:
                items = data.get("list", [])
                for item in items:
                    if item.get("server_filename") == name and item.get("isdir", 0) == 1:
                        return {"success": True, "path": path, "existed": True}
        except BaiduPanError:
            pass

        # 创建目录
        result = self.makedir(path)
        errno = result.get("errno", 0)
        if errno in (0, 31061):  # 0=成功, 31061=已存在
            return {"success": True, "path": path, "existed": errno == 31061}
        raise BaiduPanError(errno, result)

    def rename(self, old_path: str, new_path: str) -> dict[str, Any]:
        """重命名文件或目录。"""
        url = f"{_BAIDU_PAN_BASE}/api/filemanager"
        params = {"opera": "rename"}
        post_data = {"path": old_path, "newpath": new_path}
        return self._post(f"{url}?{urllib.parse.urlencode(params)}", post_data)

    def delete(self, path_list: list[str]) -> dict[str, Any]:
        """删除文件或目录。"""
        url = f"{_BAIDU_PAN_BASE}/api/filemanager"
        params = {"opera": "delete"}
        post_data = {"filelist": json.dumps(path_list)}
        return self._post(f"{url}?{urllib.parse.urlencode(params)}", post_data)

    def get_download_link(self, fs_id: str) -> dict[str, Any]:
        """获取文件下载链接。"""
        url = f"{_BAIDU_PAN_BASE}/api/sharedownload"
        params = {"app_id": "250528", "web": "1", "clienttype": "0"}
        post_data = {"fsidlist": json.dumps([fs_id])}
        return self._post(f"{url}?{urllib.parse.urlencode(params)}", post_data)


# ============================================================
# 异常
# ============================================================

class BaiduPanError(Exception):
    """百度网盘 API 错误。"""

    def __init__(self, errno: int, data: dict[str, Any] | None = None) -> None:
        self.errno = errno
        self.data = data or {}
        self.message = _format_baidu_error(json.dumps(data)) if data else _ERROR_MESSAGES.get(errno, "未知错误")
        super().__init__(f"[errno={errno}] {self.message}")


# ============================================================
# 插件主类
# ============================================================

class BaiduDrivePlugin(BasePlugin):
    """百度网盘驱动插件。"""

    plugin_id = "drive.baidu"
    plugin_name = "百度网盘驱动插件"
    plugin_version = "1.0.0"

    # ------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------

    def health(self, ctx: dict[str, Any] | None = None) -> HealthReport:
        return HealthReport(status="ok", message="百度网盘驱动插件就绪")

    def install(self) -> dict[str, Any]:
        return {"success": True, "message": f"{self.plugin_name} 已安装"}

    def enable(self) -> dict[str, Any]:
        return {"success": True, "message": f"{self.plugin_name} 已启用"}

    def disable(self) -> dict[str, Any]:
        return {"success": True, "message": f"{self.plugin_name} 已禁用"}

    # ------------------------------------------------------
    # 运行时配置辅助
    # ------------------------------------------------------

    def _get_config(self, key: str, default: Any = None) -> Any:
        """从运行时配置中取值。"""
        cfg = getattr(self, "_runtime_config", {}) or {}
        return cfg.get(key, default)

    def _get_client(self) -> BaiduPanClient:
        """根据运行时配置创建百度网盘客户端。"""
        bduss = self._get_config("bduss", "")
        stoken = self._get_config("stoken", "")
        ptoken = self._get_config("ptoken", "")

        if not bduss.strip():
            raise BaiduPanError(-6, {"errno": -6, "errmsg": "缺少 BDUSS Cookie 配置"})

        return BaiduPanClient(
            bduss=bduss,
            stoken=stoken,
            ptoken=ptoken,
            timeout=int(self._get_config("timeout_seconds", 15)),
            retry=int(self._get_config("retry_count", 2)),
            interval=float(self._get_config("request_interval", 2)),
        )

    def _is_configured(self) -> bool:
        return bool(self._get_config("bduss", "").strip())

    def _mask(self, val: str, keep: int = 8) -> str:
        """脱敏显示。"""
        if not val:
            return "(空)"
        if len(val) <= keep:
            return val[:2] + "***"
        return val[:keep] + "***"

    # ------------------------------------------------------
    # DriveProvider: 合约与账号
    # ------------------------------------------------------

    def get_contract(self) -> dict[str, Any]:
        """返回驱动合约，声明支持的 URL 模式和能力。"""
        return {
            "plugin_id": self.plugin_id,
            "cloud_type": "baidu",
            "display_name": "百度网盘",
            "account_mode": "user",
            "capabilities": [
                "drive.account",
                "drive.fs",
                "drive.share",
                "drive.download",
            ],
            "account_form_schema": [
                {
                    "key": "bduss",
                    "label": "BDUSS Cookie",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "description": "百度网盘 BDUSS Cookie 值",
                    "secret": True,
                },
                {
                    "key": "stoken",
                    "label": "STOKEN Cookie",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "description": "百度网盘 STOKEN Cookie 值",
                    "secret": True,
                },
                {
                    "key": "ptoken",
                    "label": "PTOKEN Cookie（可选）",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "description": "百度网盘 PTOKEN Cookie 值",
                    "secret": True,
                },
            ],
            "supported_auth_types": ["cookie"],
            "supported_actions": {
                "account": ["test", "refresh"],
                "fs": ["list", "get_item", "mkdir", "rename", "delete"],
                "share": ["parse", "browse", "save"],
                "file": ["download_link"],
            },
            # 关键: 声明支持的分享链接 URL 模式
            "share_url_patterns": [
                "https://pan.baidu.com/s/",
                "https://pan.baidu.com/share/init?surl=",
                "https://pan.baidu.com/wap/",
            ],
        }

    def get_account_form_schema(self) -> list[dict[str, Any]]:
        return self.get_contract()["account_form_schema"]

    def test_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        测试百度网盘账号是否有效。

        payload:
          - bduss: BDUSS Cookie 值
          - stoken: STOKEN Cookie 值
          - ptoken: (可选) PTOKEN Cookie 值
        """
        bduss = _safe_str(payload.get("bduss")).strip()
        stoken = _safe_str(payload.get("stoken")).strip()
        ptoken = _safe_str(payload.get("ptoken")).strip()

        if not bduss:
            return {"success": False, "message": "BDUSS Cookie 不能为空"}

        try:
            client = BaiduPanClient(
                bduss=bduss,
                stoken=stoken,
                ptoken=ptoken,
                timeout=15,
                retry=0,
            )
            quota = client.quota()
            user = client.user_info()

            total = int(quota.get("total", 0))
            used = int(quota.get("used", 0))
            total_gb = round(total / (1024 ** 3), 2)
            used_gb = round(used / (1024 ** 3), 2)
            free_gb = round((total - used) / (1024 ** 3), 2)

            name = _safe_str(user.get("baidu_name") or user.get("name", "(未知)"))

            return {
                "success": True,
                "message": f"账号验证成功: {name}",
                "data": {
                    "username": name,
                    "total_gb": total_gb,
                    "used_gb": used_gb,
                    "free_gb": free_gb,
                    "bduss_masked": self._mask(bduss),
                },
            }
        except BaiduPanError as e:
            return {"success": False, "message": e.message, "errno": e.errno}
        except Exception as e:
            return {"success": False, "message": f"测试失败: {e}"}

    def create_account_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """规范化账号配置。"""
        return {
            "bduss": _safe_str(payload.get("bduss")).strip(),
            "stoken": _safe_str(payload.get("stoken")).strip(),
            "ptoken": _safe_str(payload.get("ptoken")).strip(),
        }

    def get_account_info(self, account_ref: dict[str, Any]) -> dict[str, Any]:
        """获取账号信息。"""
        try:
            client = self._get_client()
            quota = client.quota()
            total = int(quota.get("total", 0))
            used = int(quota.get("used", 0))
            total_gb = round(total / (1024 ** 3), 2)
            used_gb = round(used / (1024 ** 3), 2)

            return {
                "account_id": _safe_str(account_ref.get("account_id"), "baidu-default"),
                "plugin_id": self.plugin_id,
                "cloud_type": "baidu",
                "display_name": "百度网盘",
                "status": "ok",
                "total_gb": total_gb,
                "used_gb": used_gb,
                "free_gb": round(total_gb - used_gb, 2),
                "supported_actions": ["list", "parse_share", "save_share", "download"],
            }
        except Exception as e:
            return {
                "account_id": _safe_str(account_ref.get("account_id"), "baidu-default"),
                "plugin_id": self.plugin_id,
                "cloud_type": "baidu",
                "display_name": "百度网盘",
                "status": "error",
                "error": str(e),
                "supported_actions": [],
            }

    def refresh_account(self, account_ref: dict[str, Any]) -> dict[str, Any]:
        return self.get_account_info(account_ref)

    def start_scan_login(self) -> dict[str, Any]:
        return {"success": False, "message": "百度网盘暂不支持扫码登录，请使用 Cookie 方式配置"}

    def get_scan_status(self, scan_id: str) -> dict[str, Any]:
        return {"success": False, "message": "暂不支持扫码登录"}

    def cancel_scan_login(self, scan_id: str) -> dict[str, Any]:
        return {"success": False, "message": "暂不支持扫码登录"}

    # ------------------------------------------------------
    # DriveProvider: 文件系统
    # ------------------------------------------------------

    def list_files(
        self,
        account_ref: dict[str, Any],
        parent_id: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        """列出网盘目录文件。parent_id 即目录路径。"""
        try:
            client = self._get_client()
            dir_path = parent_id if parent_id and parent_id != "0" else "/"
            data = client.list_files(dir_path, page, page_size)

            errno = data.get("errno", 0)
            if errno != 0:
                raise BaiduPanError(errno, data)

            items = []
            for item in data.get("list", []):
                items.append({
                    "id": _safe_str(item.get("fs_id")),
                    "name": _safe_str(item.get("server_filename")),
                    "type": "folder" if item.get("isdir", 0) == 1 else "file",
                    "parent_id": parent_id,
                    "size": int(item.get("size", 0)),
                    "path": _safe_str(item.get("path")),
                    "modified": _safe_str(item.get("local_mtime")),
                })

            return {
                "items": items,
                "total": int(data.get("total", len(items))),
                "parent_id": parent_id,
                "path_nodes": [],
            }
        except BaiduPanError as e:
            return {"items": [], "total": 0, "parent_id": parent_id, "error": e.message}
        except Exception as e:
            return {"items": [], "total": 0, "parent_id": parent_id, "error": str(e)}

    def get_item(self, account_ref: dict[str, Any], item_id: str) -> dict[str, Any]:
        return {
            "id": item_id,
            "name": item_id,
            "type": "file",
            "parent_id": "0",
        }

    def list_folders(self, account_ref: dict[str, Any], parent_id: str) -> dict[str, Any]:
        """只列出目录。"""
        result = self.list_files(account_ref, parent_id, 1, 200)
        folders = [item for item in result.get("items", []) if item.get("type") == "folder"]
        result["items"] = folders
        result["total"] = len(folders)
        return result

    def resolve_path(self, account_ref: dict[str, Any], item_id: str) -> dict[str, Any]:
        return {
            "items": [],
            "total": 0,
            "parent_id": item_id,
            "path_nodes": [{"id": item_id, "name": item_id}],
        }

    def mkdir(self, account_ref: dict[str, Any], parent_id: str, name: str) -> dict[str, Any]:
        """创建目录。parent_id 是父目录路径。"""
        try:
            client = self._get_client()
            parent_path = parent_id if parent_id and parent_id != "0" else "/"
            new_path = parent_path.rstrip("/") + "/" + name.strip("/")
            result = client.ensure_dir(new_path)
            return {"success": True, "item_id": new_path, "name": name, "path": new_path}
        except BaiduPanError as e:
            return {"success": False, "message": e.message, "errno": e.errno}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def rename(self, account_ref: dict[str, Any], item_id: str, new_name: str) -> dict[str, Any]:
        """重命名。item_id 是文件路径。"""
        try:
            client = self._get_client()
            parent = item_id.rstrip("/").rsplit("/", 1)[0] or "/"
            new_path = parent + "/" + new_name.strip("/")
            client.rename(item_id, new_path)
            return {"success": True, "item_id": new_path, "name": new_name}
        except BaiduPanError as e:
            return {"success": False, "message": e.message, "errno": e.errno}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def delete(self, account_ref: dict[str, Any], item_ids: list[str]) -> dict[str, Any]:
        """删除文件。item_ids 是文件路径列表。"""
        try:
            client = self._get_client()
            client.delete(item_ids)
            return {"success": True, "deleted_count": len(item_ids)}
        except BaiduPanError as e:
            return {"success": False, "message": e.message, "errno": e.errno}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def create_share(self, account_ref: dict[str, Any], item_ids: list[str], options: dict[str, Any]) -> dict[str, Any]:
        return {"success": False, "message": "创建分享暂未实现"}

    # ------------------------------------------------------
    # DriveProvider: 分享链接解析与转存
    # ------------------------------------------------------

    def parse_share(self, account_ref: dict[str, Any], share_ref: dict[str, Any]) -> dict[str, Any]:
        """
        解析百度网盘分享链接。

        share_ref:
          - share_url: 分享链接 URL
          - pwd: 提取码（可选，如 URL 中已包含则自动提取）

        返回:
          - share_id: 分享 ID
          - share_name: 分享名称
          - share_url: 标准化后的分享链接
          - normalized_url: 标准化后的分享链接
          - can_save: 是否可转存
          - root_id: 根目录标识
          - uk: 分享者 uk
          - bdstoken: bdstoken
          - files: 文件列表
        """
        raw_url = _safe_str(share_ref.get("share_url")).strip()
        if not raw_url:
            raise BaiduPanError(-1, {"errno": -1, "errmsg": "分享链接不能为空"})

        normalized_url, extracted_pwd = _parse_share_url(raw_url)
        pwd = _safe_str(share_ref.get("pwd")).strip() or (extracted_pwd or "")

        if not _SHARE_URL_RE.match(normalized_url):
            raise BaiduPanError(
                -1,
                {"errno": -1, "errmsg": f"不支持的百度网盘分享链接格式: {normalized_url}"},
            )

        try:
            client = self._get_client()
            share_info = client.access_share(normalized_url, pwd if pwd else None)

            uk = share_info["uk"]
            share_id = share_info["share_id"]
            bdstoken = share_info["bdstoken"]

            files = client._list_share_files(
                uk, share_id, bdstoken, normalized_url,
                parent_path="/", page=1, page_size=100,
            )

            share_name = "百度网盘分享"
            if files:
                if len(files) == 1:
                    share_name = files[0]["name"]
                else:
                    share_name = f"百度网盘分享 ({len(files)} 个文件)"

            return {
                "share_id": share_id,
                "share_name": share_name,
                "share_url": normalized_url,
                "normalized_url": normalized_url,
                "can_save": True,
                "root_id": "/",
                "uk": uk,
                "bdstoken": bdstoken,
                "pwd": pwd,
                "files": files,
                "file_count": len(files),
            }
        except BaiduPanError as e:
            raise
        except Exception as e:
            raise BaiduPanError(-99, {"errno": -99, "errmsg": f"解析分享链接失败: {e}"}) from e

    def browse_share(
        self,
        account_ref: dict[str, Any],
        share_ref: dict[str, Any],
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """
        浏览分享目录内容。

        parent_id 即目录在分享中的路径（如 / 或 /子目录）。
        """
        try:
            client = self._get_client()
            raw_url = _safe_str(share_ref.get("share_url")).strip()
            normalized_url, extracted_pwd = _parse_share_url(raw_url)
            pwd = _safe_str(share_ref.get("pwd")).strip() or (extracted_pwd or "")

            share_info = client.access_share(normalized_url, pwd if pwd else None)
            parent_path = parent_id if parent_id else "/"

            files = client._list_share_files(
                share_info["uk"],
                share_info["share_id"],
                share_info["bdstoken"],
                normalized_url,
                parent_path=parent_path,
            )

            items = [
                {
                    "id": f["fs_id"],
                    "name": f["name"],
                    "type": f["type"],
                    "parent_id": parent_path,
                    "size": f["size"],
                    "path": f["path"],
                }
                for f in files
            ]

            return {
                "items": items,
                "total": len(items),
                "parent_id": parent_path,
                "path_nodes": [],
            }
        except BaiduPanError as e:
            return {"items": [], "total": 0, "parent_id": parent_id or "/", "error": e.message}
        except Exception as e:
            return {"items": [], "total": 0, "parent_id": parent_id or "/", "error": str(e)}

    def save_share(
        self,
        account_ref: dict[str, Any],
        share_ref: dict[str, Any],
        target_parent_id: str,
        selected_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        将分享文件转存到指定网盘目录。

        参数:
          - share_ref: { share_url, pwd }
          - target_parent_id: 目标目录路径（如 /T3FAP）
          - selected_items: 选中的文件列表，每项包含 fs_id 字段。
            如果为 None，则转存全部文件。
        """
        try:
            client = self._get_client()
            raw_url = _safe_str(share_ref.get("share_url")).strip()
            normalized_url, extracted_pwd = _parse_share_url(raw_url)
            pwd = _safe_str(share_ref.get("pwd")).strip() or (extracted_pwd or "")

            # 解析分享获取文件列表
            share_info = client.access_share(normalized_url, pwd if pwd else None)

            # 确定要转存的文件
            if selected_items:
                fs_ids = [_safe_str(item.get("fs_id")) for item in selected_items if item.get("fs_id")]
            else:
                all_files = client._list_share_files(
                    share_info["uk"],
                    share_info["share_id"],
                    share_info["bdstoken"],
                    normalized_url,
                    parent_path="/",
                )
                fs_ids = [f["fs_id"] for f in all_files]

            if not fs_ids:
                return {
                    "success": False,
                    "message": "没有可转存的文件",
                    "saved_count": 0,
                }

            # 确保目标目录存在
            target_dir = target_parent_id if target_parent_id and target_parent_id != "0" else self._get_config("default_save_dir", "/")
            if target_dir and target_dir != "/":
                client.ensure_dir(target_dir)

            # 分批转存（每批最多 100 个文件）
            batch_size = 100
            total_saved = 0
            errors: list[str] = []

            for i in range(0, len(fs_ids), batch_size):
                batch = fs_ids[i:i + batch_size]
                try:
                    result = client.transfer_share_files(
                        share_info["uk"],
                        share_info["share_id"],
                        share_info["bdstoken"],
                        normalized_url,
                        batch,
                        target_dir,
                    )
                    total_saved += result.get("saved_count", len(batch))
                except BaiduPanError as e:
                    # -65 频率限制 → 等待后重试
                    if e.errno == -65:
                        time.sleep(10)
                        try:
                            result = client.transfer_share_files(
                                share_info["uk"],
                                share_info["share_id"],
                                share_info["bdstoken"],
                                normalized_url,
                                batch,
                                target_dir,
                            )
                            total_saved += result.get("saved_count", len(batch))
                        except BaiduPanError as e2:
                            errors.append(e2.message)
                    # -33 转存超限 → 等待后重试一次
                    elif e.errno == -33:
                        time.sleep(5)
                        try:
                            result = client.transfer_share_files(
                                share_info["uk"],
                                share_info["share_id"],
                                share_info["bdstoken"],
                                normalized_url,
                                batch,
                                target_dir,
                            )
                            total_saved += result.get("saved_count", len(batch))
                        except BaiduPanError as e2:
                            errors.append(e2.message)
                    # 31061 文件已存在 → 跳过
                    elif e.errno == 31061:
                        total_saved += len(batch)
                    else:
                        errors.append(e.message)

            return {
                "success": total_saved > 0 or not errors,
                "message": (
                    f"转存完成: 成功 {total_saved} 个文件到 {target_dir}"
                    + (f"，失败 {len(errors)} 批: {'; '.join(errors[:3])}" if errors else "")
                ),
                "saved_count": total_saved,
                "target_parent_id": target_dir,
                "errors": errors,
            }
        except BaiduPanError as e:
            return {
                "success": False,
                "message": e.message,
                "saved_count": 0,
                "errno": e.errno,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"转存失败: {e}",
                "saved_count": 0,
            }

    # ------------------------------------------------------
    # DriveProvider: 下载
    # ------------------------------------------------------

    def get_download_link(self, account_ref: dict[str, Any], item_id: str) -> dict[str, Any]:
        """获取文件下载链接。item_id 是 fs_id。"""
        try:
            client = self._get_client()
            data = client.get_download_link(item_id)
            errno = data.get("errno", 0)
            if errno != 0:
                raise BaiduPanError(errno, data)
            dlink = data.get("dlink", "")
            return {
                "item_id": item_id,
                "url": dlink,
                "headers": {
                    "Cookie": _build_cookie_str(client.cookies),
                    "User-Agent": "Mozilla/5.0",
                },
            }
        except BaiduPanError as e:
            return {"item_id": item_id, "url": "", "headers": {}, "error": e.message}
        except Exception as e:
            return {"item_id": item_id, "url": "", "headers": {}, "error": str(e)}

    # ------------------------------------------------------
    # DriveProvider: 能力声明
    # ------------------------------------------------------

    def get_supported_actions(self, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "account": ["test", "refresh"],
            "fs": ["list", "get_item", "mkdir", "rename", "delete"],
            "share": ["parse", "browse", "save"],
            "file": ["download_link"],
        }

    # ------------------------------------------------------
    # 通知测试（兼容自动化接口）
    # ------------------------------------------------------

    def test_notification(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """测试百度网盘连接是否正常。"""
        test_config = config or {}
        original_config = getattr(self, "_runtime_config", {})

        if test_config:
            try:
                self._runtime_config = {**original_config, **test_config}
                result = self.test_account(test_config)
            finally:
                self._runtime_config = original_config
        else:
            if not self._is_configured():
                return {
                    "success": False,
                    "message": "未配置 BDUSS Cookie，请先在设置中配置百度网盘账号",
                    "data": {"test": True},
                }
            result = self.test_account({
                "bduss": self._get_config("bduss", ""),
                "stoken": self._get_config("stoken", ""),
                "ptoken": self._get_config("ptoken", ""),
            })

        return {
            "success": result.get("success", False),
            "message": result.get("message", ""),
            "data": {
                "test": True,
                **result.get("data", {}),
            },
        }


# 导出全局插件实例
plugin = BaiduDrivePlugin()
