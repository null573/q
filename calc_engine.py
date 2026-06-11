"""
可发货日期计算引擎
根据型号、吨位、期望发货日期，从各工作表中计算可发货日期
"""

from datetime import datetime, timedelta
import requests

BASE_URL = "https://docs.qq.com/openapi/spreadsheet/v3"
FILE_ID = "DRkR6aXhGcWxLYVFR"

# 型号 -> (工作表sheetId, 日期列范围, 产能列范围, 上限日期单元格)
MODEL_CONFIG = {
    "F5631":  ("000005", "A6:A184", "J6:J184", "M1"),
    "F3500":  ("000005", "A6:A184", "K6:K184", "N1"),
    "C210":   ("000003", "A4:A183", "AC4:AC183", "E1"),
    "C220":   ("000003", "A4:A183", "AD4:AD183", "F1"),
    "C230":   ("000003", "A4:A183", "AE4:AE183", "G1"),
    "C240A":  ("000003", "A4:A183", "AF4:AF183", "H1"),
    "C3050A": ("000003", "A4:A183", "AG4:AG183", "I1"),
    "C280":   ("000003", "A4:A183", "AH4:AH183", "J1"),
    "330N":   ("00000a", "A3:A218", "H3:H218", "I1"),
    "F3600":  ("00000a", "A3:A218", "M3:M218", "O1"),
    "C204":   ("000006", "A4:A228", "AA4:AA228", "F2"),
    "C307":   ("000006", "A4:A228", "AB4:AB228", "G2"),
    "C305":   ("000006", "A4:A228", "AC4:AC228", "H2"),
    "C310":   ("000006", "A4:A228", "AD4:AD228", "I2"),
    "4110B":  ("000001", "A4:A188", "AB4:AB188", "I2"),
    "5118G":  ("000001", "A4:A188", "AD4:AD188", "L2"),
    "R4110":  ("000001", "A4:A188", "AE4:AE188", "K2"),
    "6001C":  ("000001", "A4:A188", "AF4:AF188", "M2"),
    "R403":   ("000001", "A4:A188", "AJ4:AJ188", "AK1"),
    "R6207":  ("000004", "A3:A203", "O3:O203", "I1"),
    "R6205":  ("000004", "A3:A203", "S3:S203", "J1"),
    "R6048":  ("000004", "A3:A203", "W3:W203", "K1"),
    "304铁桶": ("00000c", "A3:A188", "I3:I188", "L1"),
    "304吨桶": ("00000c", "A3:A188", "J3:J188", "M1"),
    "350T":   ("000009", "A3:A243", "N3:N243", "K1"),
    "8001A":  ("000009", "A3:A243", "Q3:Q243", "O1"),
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
    resp = requests.get(url, headers=get_headers(), timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("gridData", {})
    return {}


def read_single_cell(sheet_id, cell):
    """读取单个单元格（腾讯API读取单个单元格可能返回空，读取范围更稳定）"""
    # 将如 "M1" 转为 "M1:M1" 范围来读取
    grid_data = read_sheet_range(sheet_id, f"{cell}:{cell}")
    rows = grid_data.get("rows", [])
    if rows:
        for v in rows[0].get("values", []):
            cv = v.get("cellValue")
            if cv:
                return parse_cell_value(cv)
    return ""


def read_column_data(sheet_id, range_str):
    """读取一列数据，返回 [(row_index, value), ...]"""
    grid_data = read_sheet_range(sheet_id, range_str)
    rows = grid_data.get("rows", [])
    result = []
    for i, row in enumerate(rows):
        for v in row.get("values", []):
            cv = v.get("cellValue")
            if cv:
                val = parse_cell_value(cv)
                if val:
                    result.append((i, val))
                break
    return result


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
CACHE_TTL = 10  # 10秒缓存，平衡速度和数据实时性


def get_sheet_data(sheet_id, date_range, capacity_range, limit_cell):
    """获取工作表数据，带缓存"""
    cache_key = f"{sheet_id}:{date_range}:{capacity_range}:{limit_cell}"
    
    # 检查缓存
    if cache_key in cache:
        cached_data, cached_time = cache[cache_key]
        import time
        if time.time() - cached_time < CACHE_TTL:
            return cached_data
    
    # 读取日期列
    date_data = read_column_data(sheet_id, date_range)
    # 读取产能列
    capacity_data = read_column_data(sheet_id, capacity_range)
    # 读取上限日期
    limit_date_str = read_single_cell(sheet_id, limit_cell)
    limit_date = parse_date(limit_date_str)
    
    # 构建日期->产能映射
    date_capacity_map = {}
    for i, date_val in date_data:
        d = parse_date(date_val)
        if d:
            # 找对应行的产能
            cap_val = None
            for j, cap in capacity_data:
                if j == i:
                    cap_val = parse_number(cap)
                    break
            if cap_val is not None:
                date_capacity_map[d] = cap_val
    
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
    sheet_id, date_range, capacity_range, limit_cell = MODEL_CONFIG[model]
    
    # 5. 读取工作表数据
    sheet_data = get_sheet_data(sheet_id, date_range, capacity_range, limit_cell)
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
    filtered_dates = []
    filtered_caps = []
    
    for d, cap in date_capacity_map.items():
        if expected_date <= d <= limit_date:
            filtered_dates.append(d)
            filtered_caps.append(cap)
    
    if not filtered_dates:
        return "请联系商务支持", "期望日期超出可排产范围"
    
    # 检查产能最小值是否 >= 吨位
    min_cap = min(filtered_caps)
    
    if min_cap >= tonnage:
        # 期望日期当天就能满足
        return expected_date_str, ""
    
    # 找产能 < 吨位的行中最大的日期
    low_cap_dates = []
    for d, cap in date_capacity_map.items():
        if expected_date <= d <= limit_date and cap < tonnage:
            low_cap_dates.append(d)
    
    if not low_cap_dates:
        return "请联系商务支持", "无满足条件的排产日期"
    
    max_low_date = max(low_cap_dates)
    
    # 最大日期+1天
    result_date = max_low_date + timedelta(days=1)
    
    # 检查结果是否超过上限日期
    if result_date > limit_date:
        return "请联系商务支持", "计算日期超出上限"
    
    return result_date.strftime("%Y-%m-%d"), ""


def clear_cache():
    """清除缓存"""
    cache.clear()
