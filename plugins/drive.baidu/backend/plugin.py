"""Baidu Netdisk drive plugin for T3FAP."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# SDK imports — defensive in case HealthReport is not yet exported
# ---------------------------------------------------------------------------
try:
    from core.sdk import BasePlugin, HealthReport
except ImportError:
    try:
        from core.sdk import BasePlugin

        class HealthReport:  # type: ignore[no-redef]
            def __init__(self, status: str = "ok", message: str = "") -> None:
                self.status = status
                self.message = message

    except ImportError:
        # Last-resort: define both so the module at least loads
        class BasePlugin:  # type: ignore[no-redef]
            pass

        class HealthReport:  # type: ignore[no-redef]
            def __init__(self, status: str = "ok", message: str = "") -> None:
                self.status = status
                self.message = message


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BAIDU_PAN = "https://pan.baidu.com"

_SHARE_URL_RE = re.compile(
    r"^https?://pan\.baidu\.com/s/[a-zA-Z0-9_-]+(?:\?pwd=[a-zA-Z0-9]+)?$"
)

_INIT_URL_RE = re.compile(
    r"^https?://pan\.baidu\.com/share/init\?surl=([a-zA-Z0-9_-]+)(?:&pwd=([a-zA-Z0-9]+))?$"
)

_ERR: dict[int, str] = {
    -6: "BDUSS/STOKEN 无效或已过期",
    -9: "文件不存在",
    -62: "参数错误",
    -65: "操作过于频繁，请稍后再试",
    -33: "转存超限，请稍后再试",
    -7: "路径名非法或不支持该操作",
    2: "参数错误",
    4: "存储异常",
    115: "分享文件禁止分享",
    145: "分享链接已失效",
    200025: "提取码错误",
    31023: "目录状态不明确",
    31061: "文件已存在",
    31062: "目录名非法",
    31066: "路径不存在",
    31064: "转存文件列表为空",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _s(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val)


def _cookie_str(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _fmt_err(raw: str) -> str:
    try:
        d = json.loads(raw)
        errno = d.get("errno") or d.get("error_code") or d.get("errNo")
        if errno is not None:
            ei = int(errno)
            known = _ERR.get(ei)
            if known:
                return known
            msg = d.get("errmsg") or d.get("error_msg") or ""
            return f"百度网盘错误(errno={ei}): {msg}" if msg else f"百度网盘错误(errno={ei})"
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    m = re.search(r"error_code:\s*(-?\d+)", raw)
    if m:
        ei = int(m.group(1))
        return _ERR.get(ei, f"百度网盘错误(errno={ei})")
    return raw if raw else "未知错误"


def _parse_share_url(raw_url: str) -> tuple[str, str | None]:
    url = raw_url.strip()

    m = _INIT_URL_RE.match(url)
    if m:
        surl = m.group(1)
        short = surl[1:] if surl.startswith("1") else surl
        pwd = m.group(2) or None
        norm = f"https://pan.baidu.com/s/1{short}"
        if pwd:
            norm += f"?pwd={pwd}"
        return norm, pwd

    if _SHARE_URL_RE.match(url):
        p = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(p.query)
        return url, q.get("pwd", [None])[0]

    parsed = urllib.parse.urlparse(url)
    if "pan.baidu.com" in (parsed.hostname or ""):
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "s":
            surl = parts[1]
            q = urllib.parse.parse_qs(parsed.query)
            pwd = q.get("pwd", [None])[0]
            norm = f"https://pan.baidu.com/s/{surl}"
            if pwd:
                norm += f"?pwd={pwd}"
            return norm, pwd

    return url, None


# ---------------------------------------------------------------------------
# BaiduPanError
# ---------------------------------------------------------------------------
class BaiduPanError(Exception):
    def __init__(self, errno: int, data: dict[str, Any] | None = None) -> None:
        self.errno = errno
        self.data = data or {}
        self.message = _fmt_err(json.dumps(data)) if data else _ERR.get(errno, "未知错误")
        super().__init__(f"[errno={errno}] {self.message}")


# ---------------------------------------------------------------------------
# BaiduPanClient
# ---------------------------------------------------------------------------
class BaiduPanClient:
    """Baidu Netdisk Web API client using BDUSS + STOKEN cookies."""

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
        self._last = 0.0

    def _wait(self) -> None:
        now = time.time()
        gap = now - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.time()

    def _req(self, method: str, url: str, body: bytes | None) -> dict[str, Any]:
        last_err: Exception | None = None
        for attempt in range(self.retry + 1):
            self._wait()
            try:
                req = urllib.request.Request(url, method=method, data=body)
                req.add_header("Cookie", _cookie_str(self.cookies))
                req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                req.add_header("Referer", _BAIDU_PAN + "/")
                if body is not None:
                    req.add_header("Content-Type", "application/x-www-form-urlencoded")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("errno") not in (None, 0):
                    errno = data.get("errno")
                    if errno == 31061:
                        return data
                    raise BaiduPanError(errno, data)
                return data
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (401, 403):
                    raise BaiduPanError(-6, {"errno": -6, "errmsg": "认证失败"}) from e
                if attempt < self.retry:
                    time.sleep(min(2 ** (attempt + 1), 10))
                    continue
                raise BaiduPanError(-1, {"errno": -1, "errmsg": f"HTTP {e.code}: {e.reason}"}) from e
            except urllib.error.URLError as e:
                last_err = e
                if attempt < self.retry:
                    time.sleep(min(2 ** (attempt + 1), 10))
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
                    time.sleep(min(2 ** (attempt + 1), 10))
                    continue
                raise BaiduPanError(-99, {"errno": -99, "errmsg": str(e)}) from e
        raise BaiduPanError(-99, {"errno": -99, "errmsg": str(last_err or "未知错误")})

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return self._req("GET", url, None)

    def _post(self, url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        body = urllib.parse.urlencode(data).encode("utf-8") if data else None
        return self._req("POST", url, body)

    # -- account --
    def user_info(self) -> dict[str, Any]:
        return self._get(f"{_BAIDU_PAN}/rest/2.0/xpan/nas", {"method": "uinfo"})

    def quota(self) -> dict[str, Any]:
        return self._get(f"{_BAIDU_PAN}/api/quota", {"checkfree": "1", "checkexpire": "1"})

    # -- share --
    def access_share(self, share_url: str, pwd: str | None = None) -> dict[str, Any]:
        params: dict[str, str] = {
            "clienttype": "0", "app_id": "250528", "web": "1",
            "channel": "dingding", "url": share_url,
        }
        if pwd:
            params["pwd"] = pwd
        data = self._get(f"{_BAIDU_PAN}/share/wxlist", params)
        if data.get("errno", 0) != 0:
            raise BaiduPanError(data.get("errno", -1), data)
        uk = _s(data.get("uk") or data.get("share_uk"))
        share_id = _s(data.get("shareid") or data.get("share_id"))
        bdstoken = _s(data.get("bdstoken", ""))
        if not uk or not share_id:
            d2 = self._get(f"{_BAIDU_PAN}/share/tplconfig", {
                "fields": "sign,uk,shareid,timestamp,bdstoken", "url": share_url,
                **({"pwd": pwd} if pwd else {}),
            })
            if d2.get("errno", 0) != 0:
                raise BaiduPanError(d2.get("errno", -1), d2)
            inner = d2.get("data", {})
            uk = _s(inner.get("uk"))
            share_id = _s(inner.get("shareid"))
            bdstoken = _s(inner.get("bdstoken"))
        return {"uk": uk, "share_id": share_id, "bdstoken": bdstoken,
                "share_url": share_url, "pwd": pwd or ""}

    def list_share_files(
        self, share_url: str, uk: str, share_id: str, bdstoken: str,
        parent_path: str = "/", page: int = 1, page_size: int = 100,
    ) -> list[dict[str, Any]]:
        all_files: list[dict[str, Any]] = []
        while True:
            params: dict[str, str] = {
                "clienttype": "0", "app_id": "250528", "web": "1",
                "page": str(page), "num": str(page_size),
                "dir": parent_path, "url": share_url,
            }
            data = self._get(f"{_BAIDU_PAN}/share/wxlist", params)
            if data.get("errno", 0) != 0:
                raise BaiduPanError(data.get("errno", -1), data)
            file_list = data.get("list", [])
            if not file_list:
                break
            for item in file_list:
                all_files.append({
                    "fs_id": _s(item.get("fs_id")),
                    "name": _s(item.get("server_filename")),
                    "type": "folder" if item.get("isdir", 0) == 1 else "file",
                    "size": int(item.get("size", 0)),
                    "path": _s(item.get("path"), parent_path + _s(item.get("server_filename"))),
                    "is_dir": item.get("isdir", 0) == 1,
                })
            total = int(data.get("total_count", len(all_files)))
            if len(file_list) < page_size or len(all_files) >= total:
                break
            page += 1
        return all_files

    def transfer(self, share_url: str, uk: str, share_id: str,
                 fs_ids: list[str], target_dir: str) -> dict[str, Any]:
        if not fs_ids:
            return {"success": True, "saved_count": 0, "errno": 0}
        url = f"{_BAIDU_PAN}/share/transfer?app_id=250528&web=1&channel=dingding&clienttype=0"
        post: dict[str, str] = {
            "fsidlist": json.dumps(fs_ids), "path": target_dir, "url": share_url,
        }
        if uk:
            post["uk"] = uk
        if share_id:
            post["shareid"] = share_id
        data = self._post(url, post)
        errno = data.get("errno", 0)
        if errno in (0, 31061):
            info = data.get("info", {}) or {}
            inner = info.get("ErrNo", errno) if isinstance(info, dict) else errno
            if inner in (0, 31061, -33):
                saved = info.get("savedCount", 0) if isinstance(info, dict) else 0
                return {"success": True, "saved_count": saved or len(fs_ids), "errno": 0}
            raise BaiduPanError(inner, data)
        raise BaiduPanError(errno, data)

    # -- fs --
    def list_files(self, dir_path: str = "/", page: int = 1, page_size: int = 100) -> dict[str, Any]:
        return self._get(f"{_BAIDU_PAN}/api/list", {
            "order": "name", "desc": "0", "showempty": "0", "web": "5",
            "page": str(page), "num": str(page_size), "dir": dir_path,
        })

    def makedir(self, path: str) -> dict[str, Any]:
        return self._post(f"{_BAIDU_PAN}/api/create?a=commit", {"path": path, "isdir": "1"})

    def ensure_dir(self, path: str) -> dict[str, Any]:
        path = path.strip()
        if not path or path == "/":
            return {"success": True, "existed": True}
        try:
            parent = path.rstrip("/").rsplit("/", 1)[0] or "/"
            name = path.rstrip("/").rsplit("/", 1)[-1]
            data = self.list_files(parent, 1, 200)
            if data.get("errno", 0) == 0:
                for item in data.get("list", []):
                    if item.get("server_filename") == name and item.get("isdir", 0) == 1:
                        return {"success": True, "existed": True}
        except BaiduPanError:
            pass
        result = self.makedir(path)
        errno = result.get("errno", 0)
        if errno in (0, 31061):
            return {"success": True, "existed": errno == 31061}
        raise BaiduPanError(errno, result)

    def rename(self, old_path: str, new_path: str) -> dict[str, Any]:
        return self._post(f"{_BAIDU_PAN}/api/filemanager?opera=rename",
                          {"path": old_path, "newpath": new_path})

    def delete(self, paths: list[str]) -> dict[str, Any]:
        return self._post(f"{_BAIDU_PAN}/api/filemanager?opera=delete",
                          {"filelist": json.dumps(paths)})

    def download_link(self, fs_id: str) -> dict[str, Any]:
        return self._post(f"{_BAIDU_PAN}/api/sharedownload?app_id=250528&web=1&clienttype=0",
                          {"fsidlist": json.dumps([fs_id])})


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------
class BaiduDrivePlugin(BasePlugin):
    """Baidu Netdisk drive plugin for T3FAP."""

    plugin_id = "drive.baidu"
    plugin_name = "百度网盘驱动插件"
    plugin_version = "1.0.0"

    # -- lifecycle --
    def health(self, ctx: dict[str, Any]) -> HealthReport:
        return HealthReport(status="ok", message="百度网盘驱动插件就绪")

    # -- runtime config helpers --
    def _cfg(self, key: str, default: Any = None) -> Any:
        c = getattr(self, "_runtime_config", {}) or {}
        return c.get(key, default)

    def _client(self) -> BaiduPanClient:
        bduss = _s(self._cfg("bduss", "")).strip()
        if not bduss:
            raise BaiduPanError(-6, {"errno": -6, "errmsg": "缺少 BDUSS 配置"})
        return BaiduPanClient(
            bduss=bduss,
            stoken=_s(self._cfg("stoken", "")),
            ptoken=_s(self._cfg("ptoken", "")),
            timeout=int(self._cfg("timeout_seconds", 15)),
            retry=int(self._cfg("retry_count", 2)),
            interval=float(self._cfg("request_interval", 2)),
        )

    def _configured(self) -> bool:
        return bool(_s(self._cfg("bduss", "")).strip())

    # -- contract & account --
    def get_contract(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "cloud_type": "baidu",
            "display_name": "百度网盘",
            "account_mode": "user",
            "capabilities": ["drive.account", "drive.fs", "drive.share", "drive.download"],
            "account_form_schema": [
                {"key": "bduss", "label": "BDUSS Cookie", "type": "string",
                 "required": True, "default": "", "description": "百度网盘 BDUSS Cookie", "secret": True},
                {"key": "stoken", "label": "STOKEN Cookie", "type": "string",
                 "required": True, "default": "", "description": "百度网盘 STOKEN Cookie", "secret": True},
                {"key": "ptoken", "label": "PTOKEN Cookie (optional)", "type": "string",
                 "required": False, "default": "", "description": "百度网盘 PTOKEN Cookie", "secret": True},
            ],
            "supported_auth_types": ["cookie"],
            "supported_actions": {
                "account": ["test", "refresh"],
                "fs": ["list", "get_item", "mkdir", "rename", "delete"],
                "share": ["parse", "browse", "save"],
                "file": ["download_link"],
            },
            "share_url_patterns": [
                "https://pan.baidu.com/s/",
                "https://pan.baidu.com/share/init?surl=",
                "https://pan.baidu.com/wap/",
            ],
        }

    def get_account_form_schema(self) -> list[dict[str, Any]]:
        return self.get_contract()["account_form_schema"]

    def test_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        bduss = _s(payload.get("bduss")).strip()
        if not bduss:
            return {"success": False, "message": "BDUSS Cookie 不能为空"}
        try:
            c = BaiduPanClient(bduss=bduss, stoken=_s(payload.get("stoken")),
                               ptoken=_s(payload.get("ptoken")), timeout=15, retry=0)
            q = c.quota()
            u = c.user_info()
            total = int(q.get("total", 0))
            used = int(q.get("used", 0))
            name = _s(u.get("baidu_name") or u.get("name", "(未知)"))
            return {
                "success": True,
                "message": f"账号验证成功: {name}",
                "data": {
                    "username": name,
                    "total_gb": round(total / 1073741824, 2),
                    "used_gb": round(used / 1073741824, 2),
                    "free_gb": round((total - used) / 1073741824, 2),
                },
            }
        except BaiduPanError as e:
            return {"success": False, "message": e.message, "errno": e.errno}
        except Exception as e:
            return {"success": False, "message": f"测试失败: {e}"}

    def create_account_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "bduss": _s(payload.get("bduss")).strip(),
            "stoken": _s(payload.get("stoken")).strip(),
            "ptoken": _s(payload.get("ptoken")).strip(),
        }

    def get_account_info(self, account_ref: dict[str, Any]) -> dict[str, Any]:
        try:
            c = self._client()
            q = c.quota()
            total = int(q.get("total", 0))
            used = int(q.get("used", 0))
            return {
                "account_id": _s(account_ref.get("account_id"), "baidu-default"),
                "plugin_id": self.plugin_id,
                "cloud_type": "baidu",
                "display_name": "百度网盘",
                "status": "ok",
                "total_gb": round(total / 1073741824, 2),
                "used_gb": round(used / 1073741824, 2),
                "free_gb": round((total - used) / 1073741824, 2),
                "supported_actions": ["list", "parse_share", "save_share", "download"],
            }
        except Exception as e:
            return {
                "account_id": _s(account_ref.get("account_id"), "baidu-default"),
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

    # -- filesystem --
    def list_files(self, account_ref: dict[str, Any], parent_id: str, page: int, page_size: int) -> dict[str, Any]:
        try:
            c = self._client()
            d = c.list_files(parent_id if parent_id and parent_id != "0" else "/", page, page_size)
            if d.get("errno", 0) != 0:
                raise BaiduPanError(d.get("errno", -1), d)
            items = [{
                "id": _s(i.get("fs_id")),
                "name": _s(i.get("server_filename")),
                "type": "folder" if i.get("isdir", 0) == 1 else "file",
                "parent_id": parent_id,
                "size": int(i.get("size", 0)),
                "path": _s(i.get("path")),
                "modified": _s(i.get("local_mtime")),
            } for i in d.get("list", [])]
            return {"items": items, "total": int(d.get("total", len(items))),
                    "parent_id": parent_id, "path_nodes": []}
        except BaiduPanError as e:
            return {"items": [], "total": 0, "parent_id": parent_id, "error": e.message}
        except Exception as e:
            return {"items": [], "total": 0, "parent_id": parent_id, "error": str(e)}

    def get_item(self, account_ref: dict[str, Any], item_id: str) -> dict[str, Any]:
        return {"id": item_id, "name": item_id, "type": "file", "parent_id": "0"}

    def list_folders(self, account_ref: dict[str, Any], parent_id: str) -> dict[str, Any]:
        r = self.list_files(account_ref, parent_id, 1, 200)
        r["items"] = [i for i in r.get("items", []) if i.get("type") == "folder"]
        r["total"] = len(r["items"])
        return r

    def resolve_path(self, account_ref: dict[str, Any], item_id: str) -> dict[str, Any]:
        return {"items": [], "total": 0, "parent_id": item_id,
                "path_nodes": [{"id": item_id, "name": item_id}]}

    def mkdir(self, account_ref: dict[str, Any], parent_id: str, name: str) -> dict[str, Any]:
        try:
            c = self._client()
            parent = parent_id if parent_id and parent_id != "0" else "/"
            path = parent.rstrip("/") + "/" + name.strip("/")
            c.ensure_dir(path)
            return {"success": True, "item_id": path, "name": name, "path": path}
        except BaiduPanError as e:
            return {"success": False, "message": e.message, "errno": e.errno}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def rename(self, account_ref: dict[str, Any], item_id: str, new_name: str) -> dict[str, Any]:
        try:
            c = self._client()
            parent = item_id.rstrip("/").rsplit("/", 1)[0] or "/"
            new_path = parent + "/" + new_name.strip("/")
            c.rename(item_id, new_path)
            return {"success": True, "item_id": new_path, "name": new_name}
        except BaiduPanError as e:
            return {"success": False, "message": e.message, "errno": e.errno}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def delete(self, account_ref: dict[str, Any], item_ids: list[str]) -> dict[str, Any]:
        try:
            c = self._client()
            c.delete(item_ids)
            return {"success": True, "deleted_count": len(item_ids)}
        except BaiduPanError as e:
            return {"success": False, "message": e.message, "errno": e.errno}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def create_share(self, account_ref: dict[str, Any], item_ids: list[str], options: dict[str, Any]) -> dict[str, Any]:
        return {"success": False, "message": "创建分享暂未实现"}

    # -- share parse / browse / save --
    def parse_share(self, account_ref: dict[str, Any], share_ref: dict[str, Any]) -> dict[str, Any]:
        raw_url = _s(share_ref.get("share_url")).strip()
        if not raw_url:
            raise BaiduPanError(-1, {"errno": -1, "errmsg": "分享链接不能为空"})
        norm_url, extracted_pwd = _parse_share_url(raw_url)
        pwd = _s(share_ref.get("pwd")).strip() or (extracted_pwd or "")
        if not _SHARE_URL_RE.match(norm_url):
            raise BaiduPanError(-1, {"errno": -1,
                                     "errmsg": f"不支持的百度网盘分享链接格式: {norm_url}"})
        try:
            c = self._client()
            si = c.access_share(norm_url, pwd if pwd else None)
            files = c.list_share_files(norm_url, si["uk"], si["share_id"], si["bdstoken"])
            name = files[0]["name"] if len(files) == 1 else f"百度网盘分享 ({len(files)} 个文件)" if files else "百度网盘分享"
            return {
                "share_id": si["share_id"],
                "share_name": name,
                "share_url": norm_url,
                "normalized_url": norm_url,
                "can_save": True,
                "root_id": "/",
                "uk": si["uk"],
                "bdstoken": si["bdstoken"],
                "pwd": pwd,
                "files": files,
                "file_count": len(files),
            }
        except BaiduPanError:
            raise
        except Exception as e:
            raise BaiduPanError(-99, {"errno": -99, "errmsg": f"解析失败: {e}"}) from e

    def browse_share(self, account_ref: dict[str, Any], share_ref: dict[str, Any],
                     parent_id: str | None = None) -> dict[str, Any]:
        try:
            c = self._client()
            raw_url = _s(share_ref.get("share_url")).strip()
            norm_url, extracted_pwd = _parse_share_url(raw_url)
            pwd = _s(share_ref.get("pwd")).strip() or (extracted_pwd or "")
            si = c.access_share(norm_url, pwd if pwd else None)
            parent = parent_id if parent_id else "/"
            files = c.list_share_files(norm_url, si["uk"], si["share_id"], si["bdstoken"], parent)
            items = [{"id": f["fs_id"], "name": f["name"], "type": f["type"],
                      "parent_id": parent, "size": f["size"], "path": f["path"]} for f in files]
            return {"items": items, "total": len(items), "parent_id": parent, "path_nodes": []}
        except BaiduPanError as e:
            return {"items": [], "total": 0, "parent_id": parent_id or "/", "error": e.message}
        except Exception as e:
            return {"items": [], "total": 0, "parent_id": parent_id or "/", "error": str(e)}

    def save_share(self, account_ref: dict[str, Any], share_ref: dict[str, Any],
                   target_parent_id: str,
                   selected_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        try:
            c = self._client()
            raw_url = _s(share_ref.get("share_url")).strip()
            norm_url, extracted_pwd = _parse_share_url(raw_url)
            pwd = _s(share_ref.get("pwd")).strip() or (extracted_pwd or "")
            si = c.access_share(norm_url, pwd if pwd else None)

            if selected_items:
                fs_ids = [_s(i.get("fs_id")) for i in selected_items if i.get("fs_id")]
            else:
                all_files = c.list_share_files(norm_url, si["uk"], si["share_id"], si["bdstoken"])
                fs_ids = [f["fs_id"] for f in all_files]

            if not fs_ids:
                return {"success": False, "message": "没有可转存的文件", "saved_count": 0}

            target = target_parent_id if target_parent_id and target_parent_id != "0" else _s(self._cfg("default_save_dir", "/"))
            if target and target != "/":
                c.ensure_dir(target)

            saved = 0
            errors: list[str] = []
            for i in range(0, len(fs_ids), 100):
                batch = fs_ids[i:i + 100]
                try:
                    r = c.transfer(norm_url, si["uk"], si["share_id"], batch, target)
                    saved += r.get("saved_count", len(batch))
                except BaiduPanError as e:
                    if e.errno == -65:
                        time.sleep(10)
                        try:
                            r = c.transfer(norm_url, si["uk"], si["share_id"], batch, target)
                            saved += r.get("saved_count", len(batch))
                        except BaiduPanError as e2:
                            errors.append(e2.message)
                    elif e.errno == -33:
                        time.sleep(5)
                        try:
                            r = c.transfer(norm_url, si["uk"], si["share_id"], batch, target)
                            saved += r.get("saved_count", len(batch))
                        except BaiduPanError as e2:
                            errors.append(e2.message)
                    elif e.errno == 31061:
                        saved += len(batch)
                    else:
                        errors.append(e.message)

            return {
                "success": saved > 0 or not errors,
                "message": f"转存完成: 成功 {saved} 个文件到 {target}" +
                           (f"，失败 {len(errors)} 批: {'; '.join(errors[:3])}" if errors else ""),
                "saved_count": saved,
                "target_parent_id": target,
                "errors": errors,
            }
        except BaiduPanError as e:
            return {"success": False, "message": e.message, "saved_count": 0, "errno": e.errno}
        except Exception as e:
            return {"success": False, "message": f"转存失败: {e}", "saved_count": 0}

    # -- download --
    def get_download_link(self, account_ref: dict[str, Any], item_id: str) -> dict[str, Any]:
        try:
            c = self._client()
            data = c.download_link(item_id)
            if data.get("errno", 0) != 0:
                raise BaiduPanError(data.get("errno", -1), data)
            return {"item_id": item_id, "url": data.get("dlink", ""),
                    "headers": {"Cookie": _cookie_str(c.cookies), "User-Agent": "Mozilla/5.0"}}
        except BaiduPanError as e:
            return {"item_id": item_id, "url": "", "headers": {}, "error": e.message}
        except Exception as e:
            return {"item_id": item_id, "url": "", "headers": {}, "error": str(e)}

    # -- supported actions --
    def get_supported_actions(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "account": ["test", "refresh"],
            "fs": ["list", "get_item", "mkdir", "rename", "delete"],
            "share": ["parse", "browse", "save"],
            "file": ["download_link"],
        }


# Export plugin instance — this is what T3FAP loads
plugin = BaiduDrivePlugin()
