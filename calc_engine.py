"""
可发货日期计算引擎 - 优化版
优化点：后台定时预抓取腾讯表格数据到内存缓存，计算时直接读内存
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
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
# 快速失败配置：无重试，短超时，非阻塞
adapter = requests.adapters.HTTPAdapter(
    pool_connections=5, pool_maxsize=10,
    max_retries=0,
    pool_block=False
)
HTTP.mount('https://', adapter)
HTTP.mount('http://', adapter)

# 超时配置：5秒平衡速度和可靠性
_HTTP_TIMEOUT = 5  # 单个请求5秒超时

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
    try:
        # 使用Session复用TCP连接，减少并发时的连接建立开销
        resp = HTTP.get(url, headers=get_headers(), timeout=_HTTP_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            # 检查腾讯API返回的业务错误码
            if "code" in data and data.get("code") != 0:
                err_code = data.get("code")
                err_msg = data.get("message", "unknown error")
                # 400006 = Authentication Internal Error，通常是Token过期
                if err_code == 400006 or "Authentication" in err_msg or "auth" in err_msg.lower():
                    _set_token_status("expired")
                    print(f"[WARN] Tencent API token expired: {err_code} {err_msg} for {range_str}", flush=True)
                else:
                    print(f"[WARN] Tencent API error: {err_code} {err_msg} for {range_str}", flush=True)
                return {}
            _set_token_status("ok")
            return data.get("gridData", {})
        print(f"[WARN] read_sheet_range HTTP {resp.status_code}: {range_str} {resp.text[:200]}", flush=True)
    except requests.exceptions.Timeout:
        print(f"[WARN] read_sheet_range timeout ({_HTTP_TIMEOUT}s): {range_str}", flush=True)
    except requests.exceptions.ConnectionError as e:
        print(f"[WARN] read_sheet_range connection error: {e}", flush=True)
    except requests.exceptions.RequestException as e:
        print(f"[WARN] read_sheet_range request error: {e}", flush=True)
    except Exception as e:
        print(f"[WARN] read_sheet_range unexpected error: {e}", flush=True)
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
    s = str(val).strip() if val else ""
    if not s:
        return None
    try:
        return float(s)
    except:
        pass
    # 尝试从混合文字中提取第一个数字（如"35吨交xx"→35）
    m = re.search(r'\d+(?:\.\d+)?', s)
    if m:
        try:
            return float(m.group())
        except:
            pass
    return None


# ========== 内存缓存系统（核心优化） ==========
# 结构: {cache_key: {"data": {...}, "ts": timestamp}}
_memory_cache = {}
_memory_cache_lock = threading.RLock()

# 后台预抓取的数据缓存
_preload_cache = {}
_preload_cache_lock = threading.RLock()

# Token 状态跟踪："ok" | "expired" | "unknown"
_token_status = "unknown"
_token_status_lock = threading.Lock()

# 缓存TTL配置
CACHE_TTL = 300  # 5分钟（按需缓存）
PRELOAD_INTERVAL = 300  # 后台每300秒（5分钟）预抓取一次


def _set_token_status(status):
    global _token_status
    with _token_status_lock:
        _token_status = status


def get_token_status():
    with _token_status_lock:
        return _token_status


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
    """获取工作表数据 - 优先从预加载缓存读取
    优化：只读取A列（日期）和产能列，避免读取中间列导致数据量过大"""
    cache_key = f"{sheet_id}:{start_row}:{capacity_col}:{limit_cell}"

    # 1. 先检查后台预加载缓存（最快）
    preloaded = _get_preloaded_data(cache_key)
    if preloaded is not None:
        if preloaded.get("date_capacity_map"):
            return preloaded

    # 2. 再检查按需缓存
    cached = _get_from_memory(cache_key)
    if cached is not None:
        return cached

    # 3. 从腾讯API读取（分两次读取，每次只读一列，大幅减少数据量）
    # 例如C310：原来读A4:AD228=6750个单元格，现在读A4:A228+AD4:AD228=450个单元格
    end_row = start_row + row_count - 1

    # 读取日期列（A列）
    date_range = f"A{start_row}:A{end_row}"
    date_grid = read_sheet_range(sheet_id, date_range)
    date_rows = date_grid.get("rows", [])

    # 读取产能列
    cap_range = f"{capacity_col}{start_row}:{capacity_col}{end_row}"
    cap_grid = read_sheet_range(sheet_id, cap_range)
    cap_rows = cap_grid.get("rows", [])

    date_capacity_map = {}
    dates_cached = []

    max_rows = max(len(date_rows), len(cap_rows))
    for i in range(max_rows):
        d = None
        # 从日期列获取日期
        if i < len(date_rows):
            date_values = date_rows[i].get("values", [])
            if date_values:
                cv = date_values[0].get("cellValue")
                if cv:
                    date_val = parse_cell_value(cv)
                    d = parse_date(date_val)
        dates_cached.append(d)

        # 从产能列获取产能
        if d is not None and i < len(cap_rows):
            cap_values = cap_rows[i].get("values", [])
            if cap_values:
                cv = cap_values[0].get("cellValue")
                if cv:
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

# 计算结果缓存
_calc_result_cache = {}

# 空行缓存
_empty_row_cache = {"row": 0, "timestamp": 0}


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
        return "请联系商务支持", f"型号 {model} 暂无排产数据，请检查型号是否正确"

    tonnage = parse_number(tonnage_str)
    if tonnage is None or tonnage <= 0:
        return "", "吨位不能为空或无效"

    expected_date = parse_date(expected_date_str)
    if expected_date is None:
        return "", f"期望发货日期格式无效: {expected_date_str}"

    sheet_id, start_row, capacity_col, limit_cell, row_count = config

    sheet_data = get_sheet_data(sheet_id, start_row, capacity_col, limit_cell, row_count)
    date_capacity_map = sheet_data["date_capacity_map"]
    limit_date = sheet_data["limit_date"]

    if not date_capacity_map:
        # 数据为空时清除缓存，下次重试从API读取
        cache_key = f"{sheet_id}:{start_row}:{capacity_col}:{limit_cell}"
        _memory_cache.pop(cache_key, None)
        _preload_cache.pop(cache_key, None)
        print(f"[calc] 型号{model}工作表数据为空 sheet={sheet_id} col={capacity_col}，可能是腾讯Token过期或网络问题", flush=True)
        return "请联系商务支持", "排产数据读取失败，请稍后重试或联系管理员检查Token"

    if limit_date is None:
        if date_capacity_map:
            limit_date = max(date_capacity_map.keys())
        else:
            return "请联系商务支持", "上限日期未设置且无排产数据"

    # 按日期排序，只考虑期望日期到上限日期之间的日期
    sorted_dates = sorted([d for d in date_capacity_map.keys() if expected_date <= d <= limit_date])

    if not sorted_dates:
        max_data_date = max(date_capacity_map.keys())
        return "请联系商务支持", f"排产数据只到{max_data_date.strftime('%m月%d日')}，期望日期{expected_date_str}超出范围"

    # 1. 所有日期产能都 >= 吨位 → 返回期望日期
    all_sufficient = all(date_capacity_map[d] >= tonnage for d in sorted_dates)
    if all_sufficient:
        return expected_date_str, ""

    # 2. 查找产能都 >= 吨位的连续区间，取最后一个区间的最小日期
    intervals = []
    current_start = None

    for d in sorted_dates:
        cap = date_capacity_map.get(d, 0)
        if cap >= tonnage:
            if current_start is None:
                current_start = d
        else:
            if current_start is not None:
                intervals.append((current_start, d - timedelta(days=1)))
                current_start = None

    # 处理最后一个未关闭的区间
    if current_start is not None:
        intervals.append((current_start, sorted_dates[-1]))

    if not intervals:
        return "请联系商务支持", f"从{expected_date_str}到{limit_date.strftime('%Y-%m-%d')}均无足够产能"

    # 取最后一个区间的起始日期
    last_interval = intervals[-1]
    result_date = last_interval[0]

    if result_date == expected_date:
        return expected_date_str, ""
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


def _preload_single_model(model, config):
    """预抓取单个型号的产能数据"""
    try:
        sheet_id, start_row, capacity_col, limit_cell, row_count = config
        cache_key = f"{sheet_id}:{start_row}:{capacity_col}:{limit_cell}"

        # 检查是否已有较新的预加载数据
        existing = _get_preloaded_data(cache_key)
        if existing is not None:
            return True, model, "已有缓存"

        end_row = start_row + row_count - 1
        date_range = f"A{start_row}:A{end_row}"
        cap_range = f"{capacity_col}{start_row}:{capacity_col}{end_row}"

        date_grid = read_sheet_range(sheet_id, date_range)
        date_rows = date_grid.get("rows", [])

        cap_grid = read_sheet_range(sheet_id, cap_range)
        cap_rows = cap_grid.get("rows", [])

        if not date_rows and not cap_rows:
            return False, model, "数据为空"

        date_capacity_map = {}
        dates_cached = []

        max_rows = max(len(date_rows), len(cap_rows))
        for i in range(max_rows):
            d = None
            if i < len(date_rows):
                date_values = date_rows[i].get("values", [])
                if date_values:
                    cv = date_values[0].get("cellValue")
                    if cv:
                        date_val = parse_cell_value(cv)
                        d = parse_date(date_val)
            dates_cached.append(d)

            if d is not None and i < len(cap_rows):
                cap_values = cap_rows[i].get("values", [])
                if cap_values:
                    cv = cap_values[0].get("cellValue")
                    if cv:
                        cap_str = parse_cell_value(cv)
                        cap_val = parse_number(cap_str)
                        if cap_val is not None:
                            date_capacity_map[d] = cap_val

        limit_date = _read_limit_date(sheet_id, limit_cell)

        result = {
            "date_capacity_map": date_capacity_map,
            "limit_date": limit_date
        }

        _set_preload_cache(cache_key, result)
        date_col_cache_key = f"{sheet_id}:{start_row}"
        _set_memory_cache(date_col_cache_key, dates_cached)

        return True, model, ""
    except Exception as e:
        return False, model, str(e)


def _preload_all_models():
    """预抓取所有型号的产能数据到内存 - 3并发（平衡速度和限流风险）"""
    print(f"[preload] 开始预抓取 {len(MODEL_CONFIG)} 个型号（并发3线程）...", flush=True)
    success = 0
    errors = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_preload_single_model, model, config): model
            for model, config in MODEL_CONFIG.items()
        }
        for future in as_completed(futures):
            model = futures[future]
            try:
                ok, m, err = future.result()
                if ok:
                    success += 1
                else:
                    errors.append(f"{m}: {err}")
                    print(f"[preload] {m} 失败: {err}", flush=True)
            except Exception as e:
                errors.append(f"{model}: {e}")
                print(f"[preload] {model} 异常: {e}", flush=True)

    print(f"[preload] 预抓取完成: {success}/{len(MODEL_CONFIG)} 个型号", flush=True)
    if errors:
        print(f"[preload] 失败详情: {errors[:5]}", flush=True)
    return success


def _preload_worker():
    """后台预抓取工作线程，根据北京时间动态调整间隔"""
    # 首次启动时延迟15秒，避免与第一个用户请求竞争资源
    _preload_stop_event.wait(15)
    if _preload_stop_event.is_set():
        return

    while not _preload_stop_event.is_set():
        try:
            _preload_all_models()
        except Exception as e:
            print(f"[preload] 预抓取异常: {e}", flush=True)

        # 根据北京时间决定等待间隔：白天(7-22点)120秒，夜间600秒
        # 增大间隔减少API调用频率，避免token问题时大量请求
        hour = datetime.now().hour
        wait_seconds = 120 if 7 <= hour < 22 else 600
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


def refresh_capacity_data():
    """手动刷新产能数据：清除所有缓存并重新预加载所有型号
    返回 (success_count, total_count, error_msg, token_status)"""
    import time as _time
    now = _time.time()

    # 重置token状态
    _set_token_status("unknown")

    # 1. 清除内存缓存
    with _memory_cache_lock:
        _memory_cache.clear()
        print(f"[refresh] 内存缓存已清除", flush=True)

    # 2. 清除预加载缓存
    with _preload_cache_lock:
        _preload_cache.clear()
        print(f"[refresh] 预加载缓存已清除", flush=True)

    # 3. 清除上限日期缓存
    _limit_date_cache.clear()
    print(f"[refresh] 上限日期缓存已清除", flush=True)

    # 4. 清除计算结果缓存
    _calc_result_cache.clear()
    print(f"[refresh] 计算结果缓存已清除", flush=True)

    # 5. 清除空行缓存
    global _empty_row_cache
    _empty_row_cache = {"row": 0, "timestamp": 0}
    print(f"[refresh] 空行缓存已清除", flush=True)

    # 6. 重新预加载所有型号
    print(f"[refresh] 开始重新预加载 {len(MODEL_CONFIG)} 个型号...", flush=True)
    try:
        success = _preload_all_models()
        token_status = get_token_status()
        print(f"[refresh] 刷新完成，成功加载 {success} 个型号，token状态={token_status}", flush=True)
        return success, len(MODEL_CONFIG), "", token_status
    except Exception as e:
        err_msg = str(e)
        token_status = get_token_status()
        print(f"[refresh] 刷新异常: {e}，token状态={token_status}", flush=True)
        return 0, len(MODEL_CONFIG), err_msg, token_status
