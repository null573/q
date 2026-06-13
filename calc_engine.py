"""
可发货日期计算引擎
根据型号、吨位、期望发货日期，从各工作表中计算可发货日期
"""

from datetime import datetime, timedelta
import requests

BASE_URL = "https://docs.qq.com/openapi/spreadsheet/v3"
FILE_ID = "DRnhDemRIS25mdnFF"
HTTP = requests.Session()

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
}


def get_headers():
    """获取腾讯表格API请求头"""
    return {
        "Content-Type": "application/json",
        "Access-Token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjbHQiOiJkYTgxNWQxMjI3Mjk0NDU3YjQzNDEzYmRjMTZlM2U5MCIsInR5cCI6MSwiZXhwIjoxNzgyMDk0NTcyLjEwODc1MywiaWF0IjoxNzc5NTAyNTcyLjEwODc1Mywic3ViIjoiOWJjMTcyZTUzMzgxNDdkOGEzNWMxNDM4ZWE4ZDE1NzcifQ.rm3BIdD1V7FrCwdToT2arErs06xWF7hTqAh0KsCKsdw",
        "Open-Id": "9bc172e5338147d8a35c1438ea8d1577",
        "Client-Id": "da815d1227294457b43413bdc16e3e90"
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


def parse_date(date_str):
    """解析日期字符串为date对象"""
    try:
        return datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
    except:
        return None


def parse_number(val):
    """解析数字"""
    try:
        return float(str(val).strip())
    except:
        return None


# 缓存：工作表数据
cache = {}
CACHE_TTL = 60  # 60秒缓存，平衡速度和数据实时性


def get_sheet_data(sheet_id, start_row, capacity_col, limit_cell, row_count):
    """获取工作表数据，带缓存。优化：一次性读取日期列+产能列，减少API调用"""
    cache_key = f"{sheet_id}:{start_row}:{capacity_col}:{limit_cell}"

    # 检查缓存
    if cache_key in cache:
        cached_data, cached_time = cache[cache_key]
        import time
        if time.time() - cached_time < CACHE_TTL:
            return cached_data

    # 优化：一次性读取A列（日期）和产能列（如J列）
    # 构建范围如 A6:J184，这样一次API调用获取所有需要的数据
    end_row = start_row + row_count - 1
    range_str = f"A{start_row}:{capacity_col}{end_row}"
    grid_data = read_sheet_range(sheet_id, range_str)
    rows = grid_data.get("rows", [])

    # 解析数据：A列是日期，最后一列是产能
    date_capacity_map = {}
    capacity_col_index = -1  # 产能列在返回数据中的索引

    # 确定产能列的索引（根据列字母计算）
    # A=0, B=1, ..., Z=25, AA=26, AB=27, ...
    def col_letter_to_index(col):
        result = 0
        for c in col:
            result = result * 26 + (ord(c) - ord('A') + 1)
        return result - 1

    capacity_col_index = col_letter_to_index(capacity_col)

    for i, row in enumerate(rows):
        values = row.get("values", [])
        if len(values) < capacity_col_index + 1:
            continue

        # 解析A列日期
        date_val = ""
        for v in values[0:1]:
            cv = v.get("cellValue")
            if cv:
                date_val = parse_cell_value(cv)
                break

        # 解析产能列
        cap_val = None
        if len(values) > capacity_col_index:
            cv = values[capacity_col_index].get("cellValue")
            if cv:
                cap_str = parse_cell_value(cv)
                cap_val = parse_number(cap_str)

        if date_val and cap_val is not None:
            d = parse_date(date_val)
            if d:
                date_capacity_map[d] = cap_val

    # 读取上限日期
    limit_date_str = read_single_cell(sheet_id, limit_cell)
    limit_date = parse_date(limit_date_str)

    result = {
        "date_capacity_map": date_capacity_map,
        "limit_date": limit_date
    }

    # 存入缓存
    import time
    cache[cache_key] = (result, time.time())

    return result


def calculate_delivery_date(model, tonnage_str, expected_date_str):
    """
    计算可发货日期

    参数:
        model: 型号
        tonnage_str: 吨位（字符串）
        expected_date_str: 期望发货日期（字符串 YYYY-MM-DD）

    返回:
        (calculated_date_str, message)
        calculated_date_str: 计算出的可发货日期，或"请联系商务支持"
        message: 错误信息（如果有）
    """
    # 1. 检查型号是否在配置中
    if model not in MODEL_CONFIG:
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
    sheet_id, start_row, capacity_col, limit_cell, row_count = MODEL_CONFIG[model]

    # 5. 读取工作表数据（优化后：1次API调用）
    sheet_data = get_sheet_data(sheet_id, start_row, capacity_col, limit_cell, row_count)
    date_capacity_map = sheet_data["date_capacity_map"]
    limit_date = sheet_data["limit_date"]

    if not date_capacity_map:
        return "请联系商务支持", "工作表数据为空"

    if limit_date is None:
        return "请联系商务支持", "上限日期未设置"

    # 6. 公式逻辑：
    #    筛选日期在 [expected_date, limit_date] 范围内的行
    #    如果对应产能列的最小值 >= 吨位，返回期望日期
    #    否则，找产能 < 吨位的行中最大的日期，+1天

    # 筛选符合条件的日期和产能
    filtered_caps = []
    low_cap_dates = []

    for d, cap in date_capacity_map.items():
        if expected_date <= d <= limit_date:
            filtered_caps.append(cap)
            if cap < tonnage:
                low_cap_dates.append(d)

    if not filtered_caps:
        return "请联系商务支持", "期望日期超出可排产范围"

    # 检查产能最小值是否 >= 吨位
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
