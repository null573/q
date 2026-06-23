"""
可发货日期计算引擎
根据型号、吨位、期望发货日期，从各工作表中计算可发货日期
"""

from datetime import datetime, timedelta, date
import json
import os
import re
import requests
import time as time_module

BASE_URL = "https://docs.qq.com/openapi/spreadsheet/v3"
FILE_ID = "DRnhDemRIS25mdnFF"
HTTP = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=2)
HTTP.mount('https://', adapter)
HTTP.mount('http://', adapter)

# 外部注入的token获取函数（由app.py设置，确保与管理员缓存同步）
_token_getter = None


def set_token_getter(fn):
    """设置外部token获取函数，由app.py调用以同步管理员缓存的token"""
    global _token_getter
    _token_getter = fn


def get_headers():
    """获取腾讯表格API请求头
    优先使用外部注入的token获取函数（与管理员缓存同步），
    fallback到环境变量"""
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


# ========== 预编译正则表达式（避免每次调用重复编译） ==========
_DATE_RE_YYYY_MM_DD = re.compile(r'^(\d{4})年(\d{1,2})月(\d{1,2})日$')
_DATE_RE_MM_DD = re.compile(r'^(\d{1,2})月(\d{1,2})日$')
_DATE_RE_M_DOT_D = re.compile(r'^(\d{1,2})\.(\d{1,2})$')
_LIMIT_CELL_RE = re.compile(r'^([A-Z]+)(\d+)$')


# ========== 列字母→索引缓存（固定映射，避免重复计算） ==========
_COL_INDEX_CACHE = {}


def col_letter_to_index(col):
    """将列字母转为0-based索引，带缓存"""
    if col in _COL_INDEX_CACHE:
        return _COL_INDEX_CACHE[col]
    result = 0
    for c in col:
        result = result * 26 + (ord(c) - ord('A') + 1)
    result -= 1
    _COL_INDEX_CACHE[col] = result
    return result


# 型号 -> (工作表sheetId, 日期列起始行, 产能列字母, 上限日期单元格, 数据行数)
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
    """解析单元格值"""
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


def read_sheet_range(sheet_id, range_str):
    """读取表格范围数据"""
    url = f"{BASE_URL}/files/{FILE_ID}/{sheet_id}/{range_str}"
    resp = HTTP.get(url, headers=get_headers(), timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("gridData", {})
    return {}


def read_single_cell(sheet_id, cell):
    """读取单个单元格（腾讯API读取单个单元格可能返回空，读取范围更稳定）"""
    grid_data = read_sheet_range(sheet_id, f"{cell}:{cell}")
    rows = grid_data.get("rows", [])
    if rows:
        for v in rows[0].get("values", []):
            cv = v.get("cellValue")
            if cv:
                return parse_cell_value(cv)
    return ""


# 预编译日期解析格式，避免每次调用重复创建
_DATE_FMT1 = "%Y-%m-%d"
_DATE_FMT2 = "%Y/%m/%d"


def parse_date(date_str):
    """解析日期字符串为date对象
    支持格式: YYYY-MM-DD, YYYY/MM/DD, YYYY年MM月DD日, MM月DD日(默认当年), M.D"""
    s = str(date_str).strip()
    if not s:
        return None

    # 1. 标准格式 YYYY-MM-DD / YYYY/MM/DD
    try:
        return datetime.strptime(s, _DATE_FMT1).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(s, _DATE_FMT2).date()
    except ValueError:
        pass

    # 2. 中文格式: YYYY年MM月DD日
    m = _DATE_RE_YYYY_MM_DD.match(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 3. 中文格式: MM月DD日 (默认当年)
    m = _DATE_RE_MM_DD.match(s)
    if m:
        try:
            return date(date.today().year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass

    # 4. 纯数字: M.D (如 6.30)
    m = _DATE_RE_M_DOT_D.match(s)
    if m:
        try:
            return date(date.today().year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass

    return None


def parse_number(val):
    """解析数字"""
    try:
        return float(str(val).strip())
    except:
        return None


# ========== 缓存：工作表数据 ==========
cache = {}
CACHE_TTL = 300  # 300秒缓存（产能数据变化不频繁，延长缓存时间）

# A列日期数据缓存（按sheet_id+start_row分组，同sheet多个型号共享）
_date_col_cache = {}
_DATE_COL_CACHE_TTL = 300


def _read_date_column(sheet_id, start_row, row_count):
    """读取A列日期数据，带独立缓存（同sheet多个型号共享）"""
    cache_key = f"{sheet_id}:{start_row}"
    now = time_module.time()

    if cache_key in _date_col_cache:
        data, ts = _date_col_cache[cache_key]
        if now - ts < _DATE_COL_CACHE_TTL:
            return data

    end_row = start_row + row_count - 1
    range_str = f"A{start_row}:A{end_row}"
    grid_data = read_sheet_range(sheet_id, range_str)
    rows = grid_data.get("rows", [])

    # 解析日期列表（保持行顺序）
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

    _date_col_cache[cache_key] = (dates, now)
    return dates


def get_sheet_data(sheet_id, start_row, capacity_col, limit_cell, row_count):
    """获取工作表数据，带缓存。
    优化：一次读取A列到产能列的范围（避免API截断末尾空行），提取日期和产能"""
    cache_key = f"{sheet_id}:{start_row}:{capacity_col}:{limit_cell}"

    now = time_module.time()
    # 检查缓存
    if cache_key in cache:
        cached_data, cached_time = cache[cache_key]
        if now - cached_time < CACHE_TTL:
            return cached_data

    capacity_col_index = col_letter_to_index(capacity_col)

    # 一次读取A列到产能列的范围（多列读取不会截断末尾空行）
    end_row = start_row + row_count - 1
    range_str = f"A{start_row}:{capacity_col}{end_row}"
    grid_data = read_sheet_range(sheet_id, range_str)
    rows = grid_data.get("rows", [])

    # DEBUG: 记录读取详情
    debug_info = {
        "range": range_str,
        "requested_rows": row_count,
        "returned_rows": len(rows),
        "capacity_col_index": capacity_col_index,
        "first_few_dates": [],
        "last_few_dates": [],
        "skipped_rows": 0
    }

    # 提取日期（A列）和产能列
    # 同时更新A列缓存（同sheet多个型号共享）
    date_col_cache_key = f"{sheet_id}:{start_row}"
    dates_cached = []

    date_capacity_map = {}

    for i, row in enumerate(rows):
        values = row.get("values", [])
        if len(values) < capacity_col_index + 1:
            dates_cached.append(None)
            debug_info["skipped_rows"] += 1
            if i < 5:
                debug_info["first_few_dates"].append(f"row{i}:values_len={len(values)}")
            continue

        # A列日期
        cv = values[0].get("cellValue")
        if cv:
            date_val = parse_cell_value(cv)
            d = parse_date(date_val)
            dates_cached.append(d)
            if i < 5:
                debug_info["first_few_dates"].append(f"row{i}:{date_val}->{d}")
            if i >= len(rows) - 5:
                debug_info["last_few_dates"].append(f"row{i}:{date_val}->{d}")
        else:
            dates_cached.append(None)
            debug_info["skipped_rows"] += 1
            continue

        # 产能列
        cv = values[capacity_col_index].get("cellValue")
        if not cv:
            continue
        cap_str = parse_cell_value(cv)
        cap_val = parse_number(cap_str)
        if cap_val is not None:
            date_capacity_map[d] = cap_val

    # DEBUG: 打印调试信息
    print(f"[DEBUG get_sheet_data] sheet={sheet_id} model={cache_key} {debug_info}", flush=True)

    # 更新A列缓存
    _date_col_cache[date_col_cache_key] = (dates_cached, now)

    # 读取上限日期（带独立缓存，避免重复API调用）
    limit_date = _read_limit_date(sheet_id, limit_cell)

    result = {
        "date_capacity_map": date_capacity_map,
        "limit_date": limit_date
    }

    cache[cache_key] = (result, now)
    return result


# 上限日期缓存
_limit_date_cache = {}
_LIMIT_DATE_CACHE_TTL = 300


def _read_limit_date(sheet_id, limit_cell):
    """读取上限日期，带独立缓存"""
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


# 配置表缓存：从腾讯表格配置表读取的型号配置
_model_config_cache = {}
_model_config_cache_time = 0
MODEL_CONFIG_CACHE_TTL = 300  # 300秒缓存


def _load_model_configs_from_sheet():
    """从腾讯表格配置表读取型号配置（带300秒缓存）"""
    global _model_config_cache, _model_config_cache_time

    now = time_module.time()
    if _model_config_cache and (now - _model_config_cache_time < MODEL_CONFIG_CACHE_TTL):
        return _model_config_cache

    config_sheet_id = "dc53jt"
    grid_data = read_sheet_range(config_sheet_id, "A2:F200")
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
    """获取型号配置：先查硬编码，再查腾讯表格配置表"""
    if model in MODEL_CONFIG:
        return MODEL_CONFIG[model]
    sheet_configs = _load_model_configs_from_sheet()
    if model in sheet_configs:
        return sheet_configs[model]
    return None


def calculate_delivery_date(model, tonnage_str, expected_date_str, occupied_capacity=None):
    """
    计算可发货日期

    参数:
        model: 型号
        tonnage_str: 吨位（字符串）
        expected_date_str: 期望发货日期（字符串 YYYY-MM-DD）
        occupied_capacity: 已占用产能字典（保留参数，库存余额型列不需要）

    返回:
        (calculated_date_str, message)
    """
    # 1. 检查型号是否在配置中
    config = _get_model_config(model)
    if not config:
        return "请联系商务支持", f"型号 {model} 暂无排产数据"

    # 2. 解析吨位
    tonnage = parse_number(tonnage_str)
    if tonnage is None or tonnage <= 0:
        return "", "吨位不能为空"

    # 3. 解析期望日期
    expected_date = parse_date(expected_date_str)
    if expected_date is None:
        return "", "期望发货日期不能为空"

    # 4. 获取工作表配置
    sheet_id, start_row, capacity_col, limit_cell, row_count = config

    # 5. 读取工作表数据（带缓存）
    sheet_data = get_sheet_data(sheet_id, start_row, capacity_col, limit_cell, row_count)
    date_capacity_map = sheet_data["date_capacity_map"]
    limit_date = sheet_data["limit_date"]

    if not date_capacity_map:
        return "请联系商务支持", "工作表数据为空"

    # 如果上限日期未设置，自动使用产能表中的最大日期
    if limit_date is None:
        limit_date = max(date_capacity_map.keys())
        if not limit_date:
            return "请联系商务支持", "上限日期未设置"

    # 6. 公式逻辑（产能列为库存余额，直接使用原始值）：
    #    从期望日期开始往后找，如果所有日期的库存余额 >= 吨位，返回期望日期
    #    否则找到库存余额 < 吨位的最大日期，+1天

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

    # 检查库存余额最小值是否 >= 吨位
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
    """清除缓存"""
    cache.clear()
