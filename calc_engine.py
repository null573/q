"""
可发货日期计算引擎 - 优化版
优化点：后台定时预抓取腾讯表格数据到内存缓存，计算时直接读内存
"""

from datetime import datetime, timedelta, date
import json
import os
import re
import requests
import time as time_module
import threading

BASE_URL = "https://docs.qq.com/openapi/spreadsheet/v3"
FILE_ID = "DRnhDemRIS25mdnFF"        # 产能数据表（新表格）
CONFIG_FILE_ID = "DRnhDemRIS25mdnFF"  # 配置表（新表格）
HTTP = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=2)
HTTP.mount('https://', adapter)
HTTP.mount('http://', adapter)

# 外部注入的token获取函数
_token_getter = None


def set_token_getter(fn):
    global _token_getter
    _token_getter = fn


def get_headers():
    if _token_getter:
        token = _token_getter()
        if token:
            return {
                "Content-Type": "application/json",
                "Access-Token": token,
                "Open-Id": os.environ.get("TENCENT_OPEN_ID", os.environ.get("OPEN_ID", "9bc172e5338147d8a35c1438ea8d1577")),
                "Client-Id": os.environ.get("TENCENT_CLIENT_ID", os.environ.get("CLIENT_ID", "da815d1227294457b43413bdc16e3e90"))
            }
    token = os.environ.get("TENCENT_ACCESS_TOKEN", "")
    if not token:
        token = os.environ.get("ACCESS_TOKEN", "")
    return {
        "Content-Type": "application/json",
        "Access-Token": token,
        "Open-Id": os.environ.get("TENCENT_OPEN_ID", os.environ.get("OPEN_ID", "9bc172e5338147d8a35c1438ea8d1577")),
        "Client-Id": os.environ.get("TENCENT_CLIENT_ID", os.environ.get("CLIENT_ID", "da815d1227294457b43413bdc16e3e90"))
    }


# ========== 预编译正则表达式 ==========
_DATE_RE_YYYY_MM_DD = re.compile(r'^(\d{4})年(\d{1,2})月(\d{1,2})日$')
_DATE_RE_MM_DD = re.compile(r'^(\d{1,2})月(\d{1,2})日$')
_DATE_RE_M_DOT_D = re.compile(r'^(\d{1,2})\.(\d{1,2})$')

# ========== 列字母→索引缓存 ==========
_COL_INDEX_CACHE = {}


def col_letter_to_index(col):
    if col in _COL_INDEX_CACHE:
        return _COL_INDEX_CACHE[col]
    result = 0
    for c in col:
        result = result * 26 + (ord(c) - ord('A') + 1)
    result -= 1
    _COL_INDEX_CACHE[col] = result
    return result


# 型号配置
MODEL_CONFIG = {
    "F5631":  ("000005", 6, "J", "M1", 179),
    "F3500":  ("000005", 6, "K", "N1", 179),
    "C210":   ("000003", 4, "AC", "E1", 180),
    "C220":   ("000003", 4, "AD", "F1", 180),
    "C230":   ("000003", 4, "AE", "G1", 180),
    "C240A":  ("000003", 4, "AF", "H1", 180),
    "C3050A": ("000003", 4, "AG", "I1", 180),
    "C280":   ("000003", 4, "AH", "J1", 180),
    "330N":   ("00000a", 3, "H", "I1", 216),
    "F3600":  ("00000a", 3, "M", "O1", 216),
    "C204":   ("000006", 4, "AA", "F2", 225),
    "C307":   ("000006", 4, "AB", "G2", 225),
    "C305":   ("000006", 4, "AC", "H2", 225),
    "C310":   ("000006", 4, "AD", "I2", 225),
    "4110B":  ("000001", 4, "AB", "I2", 185),
    "5118G":  ("000001", 4, "AD", "L2", 185),
    "R4110":  ("000001", 4, "AE", "K2", 185),
    "6001C":  ("000001", 4, "AF", "M2", 185),
    "R403":   ("000001", 4, "AJ", "AK1", 185),
    "R6207":  ("000004", 3, "O", "I1", 201),
    "R6205":  ("000004", 3, "S", "J1", 201),
    "R6048":  ("000004", 3, "W", "K1", 201),
    "304铁桶": ("00000c", 3, "I", "L1", 186),
    "304吨桶": ("00000c", 3, "J", "M1", 186),
    "350T":   ("000009", 3, "N", "K1", 241),
    "8001A":  ("000009", 3, "Q", "O1", 241),
    "INOVOL R8315": ("000004", 3, "AB", "AP1", 180),
}


def parse_cell_value(cell_value):
    if not cell_value:
        return ""
    if "text" in cell_value:
        return cell_value["text"]
    if "number" in cell_value:
        return str(cell_value["number"])
    if "time" in cell_value:
        t = cell_value["time"]
        return f"{t['year']}-{t['month']:02d}-{t['day']:02d}"
    return ""


def read_sheet_range(sheet_id, range_str, file_id=None):
    fid = file_id if file_id else FILE_ID
    url = f"{BASE_URL}/files/{fid}/{sheet_id}/{range_str}"
    resp = HTTP.get(url, headers=get_headers(), timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("gridData", {})
    print(f"[WARN] read_sheet_range failed: {resp.status_code} {range_str} {resp.text[:200]}", flush=True)
    return {}


def read_single_cell(sheet_id, cell):
    grid_data = read_sheet_range(sheet_id, f"{cell}:{cell}")
    rows = grid_data.get("rows", [])
    if rows:
        for v in rows[0].get("values", []):
            cv = v.get("cellValue")
            if cv:
                return parse_cell_value(cv)
    return ""


_DATE_FMT1 = "%Y-%m-%d"
_DATE_FMT2 = "%Y/%m/%d"


def parse_date(date_str):
    s = str(date_str).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, _DATE_FMT1).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(s, _DATE_FMT2).date()
    except ValueError:
        pass
    m = _DATE_RE_YYYY_MM_DD.match(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _DATE_RE_MM_DD.match(s)
    if m:
        try:
            return date(date.today().year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    m = _DATE_RE_M_DOT_D.match(s)
    if m:
        try:
            return date(date.today().year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def parse_number(val):
    try:
        return float(str(val).strip())
    except:
        return None


# ========== 内存缓存系统（核心优化） ==========
# 结构: {cache_key: {"data": {...}, "ts": timestamp}}
_memory_cache = {}
_memory_cache_lock = threading.RLock()

# 后台预抓取的数据缓存
_preload_cache = {}
_preload_cache_lock = threading.RLock()

# 缓存TTL配置
CACHE_TTL = 300  # 5分钟（按需缓存）
PRELOAD_INTERVAL = 300  # 后台每300秒（5分钟）预抓取一次


def _get_from_memory(cache_key):
    """从内存缓存读取，带锁"""
    with _memory_cache_lock:
        if cache_key in _memory_cache:
            entry = _memory_cache[cache_key]
            if time_module.time() - entry["ts"] < CACHE_TTL:
                return entry["data"]
    return None


def _set_memory_cache(cache_key, data):
    """写入内存缓存，带锁"""
    with _memory_cache_lock:
        _memory_cache[cache_key] = {"data": data, "ts": time_module.time()}


def _get_preloaded_data(cache_key):
    """获取后台预抓取的数据"""
    with _preload_cache_lock:
        if cache_key in _preload_cache:
            entry = _preload_cache[cache_key]
            # 预加载数据有效期更长（2倍间隔）
            if time_module.time() - entry["ts"] < PRELOAD_INTERVAL * 2:
                return entry["data"]
    return None


def _set_preload_cache(cache_key, data):
    """写入后台预抓取缓存"""
    with _preload_cache_lock:
        _preload_cache[cache_key] = {"data": data, "ts": time_module.time()}


def _read_date_column(sheet_id, start_row, row_count):
    """读取A列日期数据"""
    cache_key = f"{sheet_id}:{start_row}"
    cached = _get_from_memory(cache_key)
    if cached is not None:
        return cached

    end_row = start_row + row_count - 1
    range_str = f"A{start_row}:A{end_row}"
    grid_data = read_sheet_range(sheet_id, range_str)
    rows = grid_data.get("rows", [])

    dates = []
    for row in rows:
        values = row.get("values", [])
        if not values:
            dates.append(None)
            continue
        cv = values[0].get("cellValue")
        if cv:
            date_val = parse_cell_value(cv)
            dates.append(parse_date(date_val))
        else:
            dates.append(None)

    _set_memory_cache(cache_key, dates)
    return dates


def get_sheet_data(sheet_id, start_row, capacity_col, limit_cell, row_count):
    """获取工作表数据 - 优先从预加载缓存读取"""
    cache_key = f"{sheet_id}:{start_row}:{capacity_col}:{limit_cell}"

    # 1. 先检查后台预加载缓存（最快）
    preloaded = _get_preloaded_data(cache_key)
    if preloaded is not None:
        # 预加载数据可能因为部署后FILE_ID变更而包含空数据，需要验证
        if preloaded.get("date_capacity_map"):
            return preloaded
        # 预加载缓存数据为空，跳过，走API实时读取

    # 2. 再检查按需缓存
    cached = _get_from_memory(cache_key)
    if cached is not None:
        return cached

    # 3. 从腾讯API读取（最慢，但必要时）
    capacity_col_index = col_letter_to_index(capacity_col)
    end_row = start_row + row_count - 1
    range_str = f"A{start_row}:{capacity_col}{end_row}"
    grid_data = read_sheet_range(sheet_id, range_str)
    rows = grid_data.get("rows", [])

    date_capacity_map = {}
    dates_cached = []

    for i, row in enumerate(rows):
        values = row.get("values", [])
        if len(values) < capacity_col_index + 1:
            dates_cached.append(None)
            continue

        cv = values[0].get("cellValue")
        if cv:
            date_val = parse_cell_value(cv)
            d = parse_date(date_val)
            dates_cached.append(d)
        else:
            dates_cached.append(None)
            continue

        cv = values[capacity_col_index].get("cellValue")
        if not cv:
            continue
        cap_str = parse_cell_value(cv)
        cap_val = parse_number(cap_str)
        if cap_val is not None:
            date_capacity_map[d] = cap_val

    # 更新A列缓存
    date_col_cache_key = f"{sheet_id}:{start_row}"
    _set_memory_cache(date_col_cache_key, dates_cached)

    # 读取上限日期
    limit_date = _read_limit_date(sheet_id, limit_cell)

    result = {
        "date_capacity_map": date_capacity_map,
        "limit_date": limit_date
    }

    _set_memory_cache(cache_key, result)
    return result


# 上限日期缓存
_limit_date_cache = {}
_LIMIT_DATE_CACHE_TTL = 300


def _read_limit_date(sheet_id, limit_cell):
    cache_key = f"{sheet_id}:{limit_cell}"
    now = time_module.time()

    if cache_key in _limit_date_cache:
        data, ts = _limit_date_cache[cache_key]
        if now - ts < _LIMIT_DATE_CACHE_TTL:
            return data

    limit_date_str = read_single_cell(sheet_id, limit_cell)
    limit_date = parse_date(limit_date_str)

    _limit_date_cache[cache_key] = (limit_date, now)
    return limit_date


# 配置表缓存
_model_config_cache = {}
_model_config_cache_time = 0
MODEL_CONFIG_CACHE_TTL = 300


def _load_model_configs_from_sheet():
    global _model_config_cache, _model_config_cache_time

    now = time_module.time()
    if _model_config_cache and (now - _model_config_cache_time < MODEL_CONFIG_CACHE_TTL):
        return _model_config_cache

    config_sheet_id = "dc53jt"
    grid_data = read_sheet_range(config_sheet_id, "A2:F200", file_id=CONFIG_FILE_ID)
    rows = grid_data.get("rows", [])

    configs = {}
    for row in rows:
        values = row.get("values", [])
        if len(values) < 6:
            continue
        cells = []
        for v in values[:6]:
            cv = v.get("cellValue")
            cells.append(parse_cell_value(cv) if cv else "")

        model_name = cells[0].strip()
        if not model_name:
            continue
        try:
            sheet_id = cells[1].strip()
            if sheet_id.isdigit() and len(sheet_id) < 6:
                sheet_id = sheet_id.zfill(6)
            start_row = int(cells[2])
            capacity_col = cells[3].strip()
            limit_cell = cells[4].strip()
            row_count = int(cells[5])
            configs[model_name] = (sheet_id, start_row, capacity_col, limit_cell, row_count)
        except (ValueError, IndexError):
            continue

    _model_config_cache = configs
    _model_config_cache_time = now
    return configs


def _get_model_config(model):
    if model in MODEL_CONFIG:
        return MODEL_CONFIG[model]
    sheet_configs = _load_model_configs_from_sheet()
    if model in sheet_configs:
        return sheet_configs[model]
    return None


def calculate_delivery_date(model, tonnage_str, expected_date_str, occupied_capacity=None):
    config = _get_model_config(model)
    if not config:
        return "请联系商务支持", f"型号 {model} 暂无排产数据"

    tonnage = parse_number(tonnage_str)
    if tonnage is None or tonnage <= 0:
        return "", "吨位不能为空"

    expected_date = parse_date(expected_date_str)
    if expected_date is None:
        return "", "期望发货日期不能为空"

    sheet_id, start_row, capacity_col, limit_cell, row_count = config

    sheet_data = get_sheet_data(sheet_id, start_row, capacity_col, limit_cell, row_count)
    date_capacity_map = sheet_data["date_capacity_map"]
    limit_date = sheet_data["limit_date"]

    if not date_capacity_map:
        return "请联系商务支持", "工作表数据为空"

    if limit_date is None:
        limit_date = max(date_capacity_map.keys())
        if not limit_date:
            return "请联系商务支持", "上限日期未设置"

    filtered_caps = []
    low_cap_dates = []

    for d, cap in date_capacity_map.items():
        if expected_date <= d <= limit_date:
            filtered_caps.append(cap)
            if cap < tonnage:
                low_cap_dates.append(d)

    if not filtered_caps:
        max_data_date = max(date_capacity_map.keys())
        return "请联系商务支持", f"排产数据只到{max_data_date.strftime('%m月%d日')}，期望日期{expected_date_str}超出范围"

    if min(filtered_caps) >= tonnage:
        return expected_date_str, ""

    if not low_cap_dates:
        return "请联系商务支持", "无满足条件的排产日期"

    max_low_date = max(low_cap_dates)
    result_date = max_low_date + timedelta(days=1)

    if result_date > limit_date:
        return "请联系商务支持", "计算日期超出上限"

    return result_date.strftime("%Y-%m-%d"), ""


def clear_cache():
    global _model_config_cache_time
    _memory_cache.clear()
    _preload_cache.clear()
    _limit_date_cache.clear()
    _model_config_cache.clear()
    _model_config_cache_time = 0


# ========== 后台预抓取线程 ==========
_preload_thread = None
_preload_stop_event = threading.Event()


def _preload_all_models():
    """预抓取所有型号的产能数据到内存"""
    print(f"[preload] 开始预抓取 {len(MODEL_CONFIG)} 个型号...", flush=True)
    success = 0

    for model, config in MODEL_CONFIG.items():
        try:
            sheet_id, start_row, capacity_col, limit_cell, row_count = config
            cache_key = f"{sheet_id}:{start_row}:{capacity_col}:{limit_cell}"

            # 检查是否已有较新的预加载数据
            existing = _get_preloaded_data(cache_key)
            if existing is not None:
                continue  # 跳过，已有有效缓存

            capacity_col_index = col_letter_to_index(capacity_col)
            end_row = start_row + row_count - 1
            range_str = f"A{start_row}:{capacity_col}{end_row}"
            grid_data = read_sheet_range(sheet_id, range_str)
            rows = grid_data.get("rows", [])

            date_capacity_map = {}
            dates_cached = []

            for row in rows:
                values = row.get("values", [])
                if len(values) < capacity_col_index + 1:
                    dates_cached.append(None)
                    continue

                cv = values[0].get("cellValue")
                if cv:
                    date_val = parse_cell_value(cv)
                    d = parse_date(date_val)
                    dates_cached.append(d)
                else:
                    dates_cached.append(None)
                    continue

                cv = values[capacity_col_index].get("cellValue")
                if not cv:
                    continue
                cap_str = parse_cell_value(cv)
                cap_val = parse_number(cap_str)
                if cap_val is not None:
                    date_capacity_map[d] = cap_val

            # 读取上限日期
            limit_date = _read_limit_date(sheet_id, limit_cell)

            result = {
                "date_capacity_map": date_capacity_map,
                "limit_date": limit_date
            }

            # 写入预加载缓存
            _set_preload_cache(cache_key, result)

            # 同时更新A列缓存
            date_col_cache_key = f"{sheet_id}:{start_row}"
            _set_memory_cache(date_col_cache_key, dates_cached)

            success += 1
        except Exception as e:
            print(f"[preload] {model} 预抓取失败: {e}", flush=True)

    print(f"[preload] 预抓取完成: {success}/{len(MODEL_CONFIG)} 个型号", flush=True)


def _preload_worker():
    """后台预抓取工作线程，根据北京时间动态调整间隔"""
    while not _preload_stop_event.is_set():
        try:
            _preload_all_models()
        except Exception as e:
            print(f"[preload] 预抓取异常: {e}", flush=True)

        # 根据北京时间决定等待间隔：白天(7-22点)60秒，夜间400秒
        hour = datetime.now().hour
        wait_seconds = 60 if 7 <= hour < 22 else 400
        _preload_stop_event.wait(wait_seconds)


def start_preload_thread():
    """启动后台预抓取线程"""
    global _preload_thread
    if _preload_thread is not None and _preload_thread.is_alive():
        return  # 已启动

    _preload_stop_event.clear()
    _preload_thread = threading.Thread(target=_preload_worker, daemon=True)
    _preload_thread.start()
    print("[preload] 后台预抓取线程已启动", flush=True)


def stop_preload_thread():
    """停止后台预抓取线程"""
    global _preload_thread
    if _preload_thread is not None:
        _preload_stop_event.set()
        _preload_thread.join(timeout=5)
        _preload_thread = None
