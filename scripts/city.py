"""城市码表解析（从 boss_cdp_raw.py 增量拆分）。"""

import json
import logging
import os
import re

log = logging.getLogger("boss_cdp")

CITY_DATA_FILENAME = "city_codes.json"

_local_city_map_cache = None
_live_city_maps_cache = None


_IMPORTER_NAMES = []

def _record_importer():
    try:
        import sys as _sys
        frame = _sys._getframe(1)
        while frame is not None:
            g = frame.f_globals
            name = g.get("__name__")
            fpath = g.get("__file__", "")
            if name and fpath and str(fpath).endswith("boss_cdp_raw.py"):
                if name not in _IMPORTER_NAMES:
                    _IMPORTER_NAMES.append(name)
                break
            frame = frame.f_back
    except Exception:
        pass

_record_importer()

def _core():
    import sys as _sys
    candidates = [m for m in _sys.modules.values()
                 if getattr(m, "__file__", None)
                 and str(m.__file__).endswith("boss_cdp_raw.py")]
    if candidates:
        return candidates[-1]
    try:
        from . import boss_cdp_raw as _m
        return _m
    except Exception:
        if "boss_cdp_raw" in _sys.modules:
            return _sys.modules["boss_cdp_raw"]
        import boss_cdp_raw as _m
        return _m


class CityAPIResponseError(ValueError):
    """BOSS 城市接口返回业务错误或无效响应。"""


class CityResolutionError(ValueError):
    """无法把用户输入解析为有效城市码。"""


def _city_data_path():
    core = _core()
    repo_data = os.path.join(os.path.dirname(__file__), "..", "data", CITY_DATA_FILENAME)
    if os.path.isfile(repo_data):
        return os.path.normpath(repo_data)
    try:
        from importlib.resources import files
        pkg_data = files(core.__package__ or "__main__").joinpath("..", "data", CITY_DATA_FILENAME) \
            if getattr(core, "__package__", None) else None
    except Exception:
        pkg_data = None
    if pkg_data is not None and os.path.isfile(str(pkg_data)):
        return str(pkg_data)
    return os.path.normpath(repo_data)


def load_local_city_map():
    global _local_city_map_cache
    if _local_city_map_cache is not None:
        return _local_city_map_cache
    name_to_code = {}
    try:
        path = _city_data_path()
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for name, code in raw.items():
                if name and code is not None:
                    name_to_code[str(name)] = str(code)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        _core().log.debug(f"读取本地城市码表失败: {e}")
    code_to_name = {code: name for name, code in name_to_code.items()}
    _local_city_map_cache = name_to_code, code_to_name
    return _local_city_map_cache


def fetch_boss_json(url, timeout=10):
    core = _core()
    requests = core.requests
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise CityAPIResponseError(f"BOSS 城市接口返回非对象响应: {url}")
    code = data.get("code")
    if code != 0:
        message = data.get("message") or "未知错误"
        raise CityAPIResponseError(
            f"BOSS 城市接口返回业务错误 code={code}, message={message}: {url}"
        )
    if not isinstance(data.get("zpData"), dict):
        raise CityAPIResponseError(f"BOSS 城市接口响应缺少有效 zpData: {url}")
    return data


def load_live_city_maps(timeout=10):
    global _live_city_maps_cache
    if _live_city_maps_cache is not None:
        return _live_city_maps_cache
    core = _core()
    name_to_code = {}
    try:
        hot_city_data = core.fetch_boss_json(core.HOT_CITY_URL, timeout=timeout)
        for item in hot_city_data.get("zpData", {}).get("hotCityList", []):
            name = item.get("name")
            code = item.get("code")
            if name and code is not None:
                name_to_code[name] = str(code)
        city_group_data = core.fetch_boss_json(core.CITY_GROUP_URL, timeout=timeout)
        for group in city_group_data.get("zpData", {}).get("cityGroup", []):
            for item in group.get("cityList", []):
                name = item.get("name")
                code = item.get("code")
                if name and code is not None:
                    name_to_code.setdefault(name, str(code))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError,
            CityAPIResponseError) as e:
        _core().log.warning(f"加载 BOSS 在线城市映射失败: {e}")
    code_to_name = {code: name for name, code in name_to_code.items()}
    _live_city_maps_cache = name_to_code, code_to_name
    return _live_city_maps_cache


def resolve_city(city_input):
    core = _core()
    if not city_input:
        return city_input, city_input
    local_map, local_reverse = core.load_local_city_map()
    if city_input in local_map:
        return city_input, local_map[city_input]
    if city_input in local_reverse:
        return local_reverse[city_input], city_input
    live_map, live_reverse = core.load_live_city_maps()
    if city_input in live_map:
        return city_input, live_map[city_input]
    if city_input in live_reverse:
        return live_reverse[city_input], city_input
    if re.fullmatch(r"\d{9}", city_input):
        return city_input, city_input
    raise CityResolutionError(
        f"无法解析城市 '{city_input}'：本地城市码表和 BOSS 在线城市接口均未命中。"
        "请传入受支持的中文城市名或 9 位 city code；已停止抓取，"
        "避免将无效城市参数误判为 0 个岗位。"
    )


def list_cities(keyword=None, use_live=True):
    core = _core()
    name_to_code = {}
    if use_live:
        live_map, _ = core.load_live_city_maps()
        name_to_code.update(live_map)
    if not name_to_code:
        local_map, _ = core.load_local_city_map()
        name_to_code.update(local_map)
    if not name_to_code:
        print("⚠️ 无法加载城市码表（本地静态文件缺失且网络拉取失败）")
        return
    items = sorted(name_to_code.items(), key=lambda kv: kv[0])
    if keyword:
        keyword = keyword.strip()
        items = [(n, c) for n, c in items if keyword in n]
        if not items:
            print(f"没有匹配「{keyword}」的城市")
            return
    print(f"共 {len(items)} 个城市（支持中文城市名或城市码）：")
    for name, code in items:
        print(f"  {name}\t{code}")
