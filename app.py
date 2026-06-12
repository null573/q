from flask import Flask, render_template, request, jsonify, abort
from flask_cors import CORS
import requests
import json
import os
from datetime import datetime
import functools
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from calc_engine import calculate_delivery_date

app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app)

# 腾讯表格配置（正式排队表格）
FILE_ID = "DRnhDemRIS25mdnFF"
SHEET_ID = "000007"       # 自助排队表格
MODEL_SHEET_ID = "000008"  # 牌号表格

# 用户表配置（和正式表格同一个文件）
USER_FILE_ID = "DRnhDemRIS25mdnFF"
USER_SHEET_ID = "s9osf8"

# 服务端配置（用于API调用）
CLIENT_ID = os.environ.get('CLIENT_ID', 'da815d1227294457b43413bdc16e3e90')
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjbHQiOiJkYTgxNWQxMjI3Mjk0NDU3YjQzNDEzYmRjMTZlM2U5MCIsInR5cCI6MSwiZXhwIjoxNzgyMDk0NTcyLjEwODc1MywiaWF0IjoxNzc5NTAyNTcyLjEwODc1Mywic3ViIjoiOWJjMTcyZTUzMzgxNDdkOGEzNWMxNDM4ZWE4ZDE1NzcifQ.rm3BIdD1V7FrCwdToT2arErs06xWF7hTqAh0KsCKsdw')
OPEN_ID = os.environ.get('OPEN_ID', '9bc172e5338147d8a35c1438ea8d1577')

BASE_URL = "https://docs.qq.com/openapi/spreadsheet/v3"

# 访问密码
ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', 'queue2025')


# ============ 授权中间件 ============

def require_auth(f):
    """装饰器：检查请求头中的密码是否正确"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        password = request.headers.get('X-Access-Password', '')
        if password != ACCESS_PASSWORD:
            return jsonify({"success": False, "error": "未授权", "need_auth": True}), 401
        return f(*args, **kwargs)
    return decorated


# ============ 授权路由 ============

@app.route('/auth/check')
def auth_check():
    """检查密码是否正确"""
    password = request.headers.get('X-Access-Password', '')
    if password == ACCESS_PASSWORD:
        return jsonify({"authorized": True})
    return jsonify({"authorized": False})


@app.route('/auth/login', methods=['POST'])
def auth_login():
    """员工号+密码验证"""
    data = request.json
    employee_id = data.get('employee_id', '')
    password = data.get('password', '')
    users = read_users()
    for user in users:
        if user["employee_id"] == employee_id:
            if user["password"] == password:
                return jsonify({
                    "success": True,
                    "user": {
                        "name": user["name"],
                        "employee_id": user["employee_id"]
                    },
                    "access_password": ACCESS_PASSWORD
                })
            else:
                return jsonify({"success": False, "error": "密码错误"})
    return jsonify({"success": False, "error": "员工号不存在"})


@app.route('/auth/users', methods=['GET'])
def auth_users():
    """返回用户列表（用于前端下拉选择）"""
    users = read_users()
    return jsonify({
        "success": True,
        "users": [{"name": u["name"], "employee_id": u["employee_id"]} for u in users]
    })


def get_headers():
    """获取腾讯表格API请求头"""
    return {
        "Content-Type": "application/json",
        "Access-Token": ACCESS_TOKEN,
        "Open-Id": OPEN_ID,
        "Client-Id": CLIENT_ID
    }


def read_users():
    """读取用户表（A2:D30），返回 [{name, employee_id, password, is_admin}, ...]"""
    url = f"{BASE_URL}/files/{USER_FILE_ID}/{USER_SHEET_ID}/A2:D30"
    resp = requests.get(url, headers=get_headers(), timeout=30)
    users = []
    if resp.status_code == 200:
        data = resp.json()
        rows = data.get("gridData", {}).get("rows", [])
        for row in rows:
            values = row.get("values", [])
            row_data = [parse_cell_value(v.get("cellValue")) for v in values]
            if len(row_data) >= 3 and row_data[0] and row_data[1]:
                # D列标注"管理员"表示是管理员
                is_admin = len(row_data) >= 4 and row_data[3] == "管理员"
                users.append({
                    "name": row_data[0],
                    "employee_id": row_data[1],
                    "password": row_data[2],
                    "is_admin": is_admin
                })
    return users


def is_user_admin(employee_id):
    """检查用户是否是管理员"""
    users = read_users()
    for user in users:
        if user["employee_id"] == employee_id:
            return user.get("is_admin", False)
    return False


def parse_cell_value(cell_value):
    """解析单元格值，统一返回字符串，过滤Excel空日期默认值"""
    if not cell_value:
        return ""
    if "text" in cell_value:
        return cell_value["text"]
    if "number" in cell_value:
        return str(cell_value["number"])
    if "time" in cell_value:
        t = cell_value["time"]
        result = f"{t['year']}-{t['month']:02d}-{t['day']:02d}"
        # 过滤Excel空日期默认值
        if result == "1899-12-30":
            return ""
        return result
    if "select" in cell_value:
        vals = cell_value["select"].get("value", [])
        return vals[0] if vals else ""
    if "link" in cell_value:
        return cell_value["link"].get("text", cell_value["link"].get("url", ""))
    return ""


def build_cell_value(value, is_date=False, is_number=False, font_size=14):
    """构建单元格写入值，支持设置字号"""
    cell = {}
    if not value or str(value).strip() == "":
        cell = {"cellValue": {"text": ""}}
    elif is_number:
        try:
            cell = {"cellValue": {"number": float(value)}}
        except (ValueError, TypeError):
            cell = {"cellValue": {"text": str(value)}}
    elif is_date:
        try:
            parts = str(value).split("-")
            if len(parts) == 3 and len(parts[0]) == 4:
                cell = {"cellValue": {"time": {
                    "year": int(parts[0]), "month": int(parts[1]), "day": int(parts[2])
                }}}
            else:
                cell = {"cellValue": {"text": str(value)}}
        except:
            cell = {"cellValue": {"text": str(value)}}
    else:
        cell = {"cellValue": {"text": str(value)}}
    
    # 设置字号
    if font_size:
        cell["textFormat"] = {"fontSize": font_size}
    
    return cell


def read_sheet_range(sheet_id, range_str):
    """读取表格范围数据，返回gridData"""
    url = f"{BASE_URL}/files/{FILE_ID}/{sheet_id}/{range_str}"
    resp = requests.get(url, headers=get_headers(), timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("gridData", {})
    return {}


def get_next_empty_row(sheet_id):
    """获取表格下一个空行号（1-based），从A列第一个空行开始扫描（跳过表头第1行）"""
    # 分批读取，每批200行，扫描到2000行
    batch_size = 200
    for offset in range(0, 2000, batch_size):
        start = offset + 1  # 1-based
        end = offset + batch_size
        range_str = f"A{start}:A{end}"
        grid_data = read_sheet_range(sheet_id, range_str)
        rows = grid_data.get("rows", [])
        
        for i in range(len(rows)):
            row = rows[i]
            actual_row = start + i  # 1-based实际行号
            if actual_row < 2:
                continue  # 跳过表头
            has_data = False
            for v in row.get("values", []):
                cv = v.get("cellValue")
                if cv:
                    text = parse_cell_value(cv)
                    if text.strip():
                        has_data = True
                        break
            if not has_data:
                return actual_row
    
    return 2001  # 如果前2000行都满了


def batch_update(requests_body):
    """执行批量更新操作"""
    url = f"{BASE_URL}/files/{FILE_ID}/batchUpdate"
    resp = requests.post(url, headers=get_headers(), json=requests_body)
    return resp


def is_date_string(value):
    """判断字符串是否为日期格式 YYYY-MM-DD"""
    if not value:
        return False
    import re
    return bool(re.match(r'^\d{4}-\d{2}-\d{2}$', str(value).strip()))


def write_order_row(row_index_0based, model, tonnage, customer, expected_date, calculated_date, queue_date, submitter, remark, serial_no, submitter_id, submit_time):
    """写入一行订单数据到腾讯表格（row_index_0based从0开始）
    E列（可发货日期）现在由本地计算引擎计算后写入，不再依赖腾讯表格公式
    优化：合并为单次batchUpdate，减少API调用
    """
    # 构建整行12列数据
    queue_date_is_date = is_date_string(queue_date)
    
    # E列值
    if calculated_date and is_date_string(calculated_date):
        e_value = build_cell_value(calculated_date, is_date=True)
    elif calculated_date:
        e_value = build_cell_value(calculated_date)
    else:
        e_value = build_cell_value("")
    
    row_values = [
        build_cell_value(model),                        # A: 型号
        build_cell_value(tonnage, is_number=True),      # B: 吨位
        build_cell_value(customer),                      # C: 客户
        build_cell_value(expected_date, is_date=True),  # D: 期望发货日期
        e_value,                                         # E: 可发货日期
        build_cell_value(queue_date, is_date=queue_date_is_date),  # F: 排队日期
        build_cell_value(submitter),                     # G: 提交人
        build_cell_value(remark),                        # H: 备注
        build_cell_value(serial_no),                     # I: 序号
        build_cell_value(""),                            # J: 上次录入
        build_cell_value(submitter_id),                  # K: 提交人ID
        build_cell_value(submit_time),                   # L: 提交时间
    ]
    
    body = {
        "requests": [{
            "updateRangeRequest": {
                "sheetId": SHEET_ID,
                "gridData": {
                    "startRow": row_index_0based,
                    "startColumn": 0,
                    "rows": [{"values": row_values}]
                }
            }
        }]
    }
    return batch_update(body)


def delete_row(row_index_1based):
    """删除一行（row_index_1based从1开始）"""
    body = {
        "requests": [{
            "deleteDimensionRequest": {
                "sheetId": SHEET_ID,
                "dimension": "ROW",
                "startIndex": row_index_1based,
                "endIndex": row_index_1based + 1
            }
        }]
    }
    return batch_update(body)


# ==================== 路由 ====================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/models', methods=['GET'])
@require_auth
def get_models():
    """获取型号列表（从牌号表格A列）"""
    try:
        grid_data = read_sheet_range(MODEL_SHEET_ID, "A1:A100")
        rows = grid_data.get("rows", [])
        models = []
        for row in rows:
            for v in row.get("values", []):
                cv = v.get("cellValue")
                if cv:
                    text = parse_cell_value(cv)
                    if text:
                        models.append(text)
        return jsonify({"success": True, "models": models})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# 计算结果缓存：避免重复计算
_calc_result_cache = {"key": None, "result": None, "timestamp": 0}
_CALC_CACHE_TTL = 30  # 30秒缓存

# 待处理行缓存
_pending_row_cache = {"data": None, "timestamp": 0}
_PENDING_ROW_CACHE_TTL = 30

def _get_pending_rows():
    """获取所有待处理行（F列为空的行），带缓存"""
    import time
    now = time.time()
    if _pending_row_cache["data"] is not None and (now - _pending_row_cache["timestamp"]) < _PENDING_ROW_CACHE_TTL:
        return _pending_row_cache["data"]

    pending = {}  # model -> row_index
    batch_size = 200
    for offset in range(0, 2000, batch_size):
        start = offset + 1
        end = offset + batch_size
        range_str = f"A{start}:F{end}"
        grid_data = read_sheet_range(SHEET_ID, range_str)
        rows = grid_data.get("rows", [])

        for i in range(len(rows)):
            row = rows[i]
            actual_row = start + i
            if actual_row < 3:
                continue
            values = row.get("values", [])
            row_data = [parse_cell_value(v.get("cellValue")) for v in values]
            a_val = row_data[0] if len(row_data) > 0 else ""
            f_val = row_data[5] if len(row_data) > 5 else ""
            if a_val and not f_val.strip():
                # 如果同一型号有多行待处理，取最后一行
                pending[a_val] = actual_row

        if len(rows) < batch_size:
            break

    _pending_row_cache["data"] = pending
    _pending_row_cache["timestamp"] = now
    return pending


@app.route('/api/calculate-date', methods=['POST'])
@require_auth
def calculate_date():
    """计算可发货日期：只计算不写入，带缓存加速"""
    try:
        data = request.json
        model = data.get('model', '')
        tonnage = data.get('tonnage', '')
        expected_date = data.get('expected_date', '')
        pending_row_index = data.get('pending_row_index', 0)

        # 1. 检查计算结果缓存
        import time
        now = time.time()
        cache_key = f"{model}:{tonnage}:{expected_date}"
        if (_calc_result_cache["key"] == cache_key and
            (now - _calc_result_cache["timestamp"]) < _CALC_CACHE_TTL):
            calculated_date = _calc_result_cache["result"]
        else:
            # 使用本地计算引擎计算可发货日期
            calculated_date, error_msg = calculate_delivery_date(model, tonnage, expected_date)
            _calc_result_cache["key"] = cache_key
            _calc_result_cache["result"] = calculated_date
            _calc_result_cache["timestamp"] = now

        # 2. 查找是否已有该型号的待处理行（使用缓存）
        target_row = 0
        if pending_row_index > 0:
            target_row = pending_row_index
        else:
            pending = _get_pending_rows()
            if model in pending:
                target_row = pending[model]

        return jsonify({
            "success": True,
            "calculated_date": calculated_date,
            "row_index": target_row
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/orders', methods=['POST'])
@require_auth
def create_order():
    """创建订单：负责所有写入操作，calculate_date只计算不写入"""
    try:
        data = request.json
        model = data.get('model', '')
        tonnage = data.get('tonnage', '')
        customer = data.get('customer', '')
        expected_date = data.get('expected_date', '')
        queue_date = data.get('queue_date', '')
        submitter = data.get('submitter', '未知用户')
        submitter_id = data.get('submitter_id', '')
        row_index = data.get('row_index', 0)  # 1-based，由calculate_date返回

        remark = f"{tonnage}{customer}"
        submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if row_index > 0:
            # 更新已有行（由calculate_date查找的行）
            write_row_idx = row_index - 1  # 转为0-based
            serial_no = str(row_index)
        else:
            # 新建行：找到第一个空行
            empty_row = get_next_empty_row(SHEET_ID)
            write_row_idx = empty_row - 1
            serial_no = str(empty_row)

        # 计算可发货日期（用于写入E列，有缓存）
        calc_date_for_write, _ = calculate_delivery_date(model, tonnage, expected_date)
        
        resp = write_order_row(
            write_row_idx, model, tonnage, customer, expected_date,
            calc_date_for_write, queue_date, submitter, remark, serial_no, submitter_id, submit_time
        )
        result = resp.json()

        if "responses" in result:
            updated = result["responses"][0].get("updateRangeResponse", {}).get("updatedCells", 0)
            if updated > 0:
                return jsonify({"success": True, "message": "订单创建成功"})
            return jsonify({"success": False, "error": "写入0个单元格"})
        else:
            return jsonify({"success": False, "error": json.dumps(result, ensure_ascii=False)})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# 全局缓存：原始订单数据（不过滤权限和日期）
_orders_cache = {"data": None, "timestamp": 0}
# 全局缓存：过滤+排序后的结果（按view_mode缓存）
_filtered_cache = {"mine": None, "all": None, "timestamp": 0}
CACHE_TTL = 60  # 缓存60秒

def _read_batch(sheet_id, range_str):
    """读取一批数据，供并行调用"""
    return read_sheet_range(sheet_id, range_str)

def fetch_all_orders_raw():
    """从腾讯表格读取所有订单原始数据，带缓存，并行读取加速"""
    now = datetime.now().timestamp()
    if _orders_cache["data"] is not None and (now - _orders_cache["timestamp"]) < CACHE_TTL:
        return _orders_cache["data"]

    # Step 1: 并行扫描A列，找到数据边界（4个线程，每批500行）
    last_data_row = 1
    scan_ranges = []
    batch_size = 500
    for offset in range(0, 2000, batch_size):
        start = offset + 1
        end = offset + batch_size
        scan_ranges.append((start, end))

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_read_batch, SHEET_ID, f"A{s}:A{e}"): (s, e) for s, e in scan_ranges}
        for future in as_completed(futures):
            grid_data = future.result()
            rows = grid_data.get("rows", [])
            start, end = futures[future]
            for i, row in enumerate(rows):
                actual_row = start + i
                values = row.get("values", [])
                if values:
                    cv = values[0].get("cellValue")
                    if cv:
                        text = parse_cell_value(cv)
                        if text.strip():
                            last_data_row = actual_row

    if last_data_row <= 1:
        _orders_cache["data"] = []
        _orders_cache["timestamp"] = now
        return []

    # Step 2: 并行读取有数据的范围（A2:Llast_data_row），每批200行
    all_rows_by_offset = {}
    data_ranges = []
    batch_size = 200
    for offset in range(1, last_data_row, batch_size):
        start = offset + 1
        end = min(offset + batch_size, last_data_row)
        data_ranges.append((offset, start, end))

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_read_batch, SHEET_ID, f"A{s}:L{e}"): (offset, s, e) for offset, s, e in data_ranges}
        for future in as_completed(futures):
            grid_data = future.result()
            rows = grid_data.get("rows", [])
            offset = futures[future][0]
            all_rows_by_offset[offset] = rows

    # 按offset排序合并
    all_rows = []
    for offset in sorted(all_rows_by_offset.keys()):
        all_rows.extend(all_rows_by_offset[offset])

    # Step 3: 解析数据
    orders = []
    current_row = 2  # 从第2行开始（跳过表头第1行）
    for offset in sorted(all_rows_by_offset.keys()):
        rows = all_rows_by_offset[offset]
        start_row = offset + 1  # 该批次的起始行号（1-based）
        for i, row in enumerate(rows):
            actual_row = start_row + i  # 实际表格行号（1-based）
            values = row.get("values", [])
            if not values:
                continue

            def get_col(idx):
                if idx < len(values):
                    cv = values[idx].get("cellValue")
                    if cv:
                        return parse_cell_value(cv)
                return ""

            row_data = [get_col(j) for j in range(12)]
            if not row_data[0]:
                continue

            orders.append({
                "row_index": actual_row,  # 实际表格行号（1-based）
                "model": row_data[0],
                "tonnage": row_data[1],
                "customer": row_data[2],
                "expected_date": row_data[3],
                "calculated_date": row_data[4],
                "queue_date": row_data[5],
                "submitter": row_data[6],
                "remark": row_data[7],
                "serial_no": row_data[8],
                "last_entry": row_data[9],
                "submitter_id": row_data[10],
                "submit_time": row_data[11]
            })

    _orders_cache["data"] = orders
    _orders_cache["timestamp"] = now
    # 清除过滤缓存（原始数据已更新）
    _filtered_cache["mine"] = None
    _filtered_cache["all"] = None
    return orders

def get_filtered_orders(submitter_id, is_admin, view_mode):
    """获取过滤+排序后的订单列表，带缓存"""
    now = datetime.now().timestamp()
    cache_key = "all" if is_admin and view_mode == "all" else "mine"

    # 检查过滤缓存是否有效（基于原始数据的时间戳）
    if (_filtered_cache[cache_key] is not None and
        _filtered_cache["timestamp"] == _orders_cache["timestamp"] and
        (now - _orders_cache["timestamp"]) < CACHE_TTL):
        return _filtered_cache[cache_key]

    # 读取原始数据
    all_orders = fetch_all_orders_raw()
    today = datetime.now().date()

    # 过滤
    orders = []
    for order in all_orders:
        row_submitter_id = order["submitter_id"]

        # 权限过滤
        if not is_admin:
            if submitter_id and row_submitter_id and row_submitter_id != submitter_id:
                continue
        else:
            if view_mode == 'mine':
                if not row_submitter_id or row_submitter_id != submitter_id:
                    continue

        # 期望发货日期过滤：仅显示期望发货日期>=今天的订单
        expected_date_str = order["expected_date"]
        if expected_date_str:
            try:
                expected_date = datetime.strptime(expected_date_str, "%Y-%m-%d").date()
                if expected_date < today:
                    continue
            except:
                pass

        orders.append(order)

    # 默认按排队日期升序排列（空日期排最后）
    def sort_key(o):
        qd = o.get("queue_date", "")
        if qd and len(qd) >= 10:
            return (0, qd)
        return (1, "")
    orders.sort(key=sort_key)

    # 存入缓存
    _filtered_cache[cache_key] = orders
    _filtered_cache["timestamp"] = _orders_cache["timestamp"]
    return orders


@app.route('/api/orders', methods=['GET'])
@require_auth
def get_orders():
    """获取订单列表：管理员可查看所有或仅自己的；仅显示期望发货日期>=今天的订单；支持分页"""
    try:
        submitter_id = request.args.get('submitter_id', '')
        is_admin = is_user_admin(submitter_id)
        view_mode = request.args.get('view_mode', 'mine' if not is_admin else 'all')
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        if page < 1:
            page = 1
        if per_page < 1:
            per_page = 20
        if per_page > 100:
            per_page = 100

        # 使用缓存的过滤结果
        orders = get_filtered_orders(submitter_id, is_admin, view_mode)

        # 分页
        total = len(orders)
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_orders = orders[start_idx:end_idx]

        return jsonify({
            "success": True,
            "orders": paginated_orders,
            "is_admin": is_admin,
            "view_mode": view_mode,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": (total + per_page - 1) // per_page
            }
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/orders/<int:row_index>', methods=['GET'])
@require_auth
def get_order(row_index):
    """获取单条订单：直接读取指定行，速度快"""
    try:
        submitter_id = request.args.get('submitter_id', '')
        is_admin = is_user_admin(submitter_id)

        # 直接读取指定行
        grid_data = read_sheet_range(SHEET_ID, f"A{row_index}:L{row_index}")
        rows = grid_data.get("rows", [])
        if not rows:
            return jsonify({"success": False, "error": "订单不存在"})

        values = rows[0].get("values", [])
        if not values:
            return jsonify({"success": False, "error": "订单不存在"})

        def get_col(idx):
            if idx < len(values):
                cv = values[idx].get("cellValue")
                if cv:
                    return parse_cell_value(cv)
            return ""

        row_data = [get_col(j) for j in range(12)]

        # 检查权限
        row_submitter_id = row_data[10]
        if not is_admin and submitter_id and row_submitter_id and row_submitter_id != submitter_id:
            return jsonify({"success": False, "error": "无权查看他人订单"})

        order = {
            "row_index": row_index,
            "model": row_data[0],
            "tonnage": row_data[1],
            "customer": row_data[2],
            "expected_date": row_data[3],
            "calculated_date": row_data[4],
            "queue_date": row_data[5],
            "submitter": row_data[6],
            "remark": row_data[7],
            "serial_no": row_data[8],
            "last_entry": row_data[9],
            "submitter_id": row_submitter_id,
            "submit_time": row_data[11]
        }

        return jsonify({"success": True, "order": order})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/orders/<int:row_index>', methods=['PUT'])
@require_auth
def update_order(row_index):
    """修改订单：管理员可修改所有，其他人只能修改自己的"""
    try:
        data = request.json
        model = data.get('model', '')
        tonnage = data.get('tonnage', '')
        customer = data.get('customer', '')
        expected_date = data.get('expected_date', '')
        queue_date = data.get('queue_date', '')
        submitter = data.get('submitter', '')
        submitter_id = data.get('submitter_id', '')

        remark = f"{tonnage}{customer}"
        is_admin = is_user_admin(submitter_id)

        # 读取原订单检查权限和吨位
        grid_data = read_sheet_range(SHEET_ID, f"A{row_index}:L{row_index}")
        rows = grid_data.get("rows", [])
        if rows:
            orig_values = [parse_cell_value(v.get("cellValue")) for v in rows[0].get("values", [])]
            original_tonnage = orig_values[1] if len(orig_values) > 1 else "0"
            row_submitter_id = orig_values[10] if len(orig_values) > 10 else ""
            # 权限检查：非管理员只能操作自己的数据
            if not is_admin and row_submitter_id and row_submitter_id != submitter_id:
                return jsonify({"success": False, "error": "无权修改他人订单"})
            try:
                if float(tonnage) > float(original_tonnage):
                    return jsonify({"success": False, "error": "吨位只能改小不能改大"})
            except ValueError:
                pass
        else:
            return jsonify({"success": False, "error": "订单不存在"})

        # 更新（row_index是1-based，转为0-based）
        write_idx = row_index - 1
        # 计算可发货日期（用于写入E列）
        calc_date_for_update, _ = calculate_delivery_date(model, tonnage, expected_date)
        resp = write_order_row(
            write_idx, model, tonnage, customer, expected_date,
            calc_date_for_update, queue_date, submitter, remark, str(write_idx), submitter_id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        result = resp.json()

        if "responses" in result:
            return jsonify({"success": True, "message": "订单修改成功"})
        else:
            return jsonify({"success": False, "error": json.dumps(result, ensure_ascii=False)})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/orders/<int:row_index>', methods=['DELETE'])
@require_auth
def delete_order(row_index):
    """删除订单：管理员可删除所有，其他人只能删除自己的"""
    try:
        submitter_id = request.args.get('submitter_id', '')
        is_admin = is_user_admin(submitter_id)

        # 读取原订单检查权限
        grid_data = read_sheet_range(SHEET_ID, f"A{row_index}:L{row_index}")
        rows = grid_data.get("rows", [])
        if rows:
            orig_values = [parse_cell_value(v.get("cellValue")) for v in rows[0].get("values", [])]
            row_submitter_id = orig_values[10] if len(orig_values) > 10 else ""
            # 权限检查：非管理员只能操作自己的数据
            if not is_admin and row_submitter_id and row_submitter_id != submitter_id:
                return jsonify({"success": False, "error": "无权删除他人订单"})
        else:
            return jsonify({"success": False, "error": "订单不存在"})

        resp = delete_row(row_index)
        result = resp.json()
        if "responses" in result:
            deleted = result["responses"][0].get("deleteDimensionResponse", {}).get("deleted", 0)
            if deleted > 0:
                return jsonify({"success": True, "message": "订单删除成功"})
        return jsonify({"success": False, "error": json.dumps(result, ensure_ascii=False)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/users/password', methods=['PUT'])
@require_auth
def update_password():
    """修改密码：验证旧密码后更新腾讯文档用户表"""
    try:
        data = request.json
        employee_id = data.get('employee_id', '')
        old_password = data.get('old_password', '')
        new_password = data.get('new_password', '')

        if not employee_id or not old_password or not new_password:
            return jsonify({"success": False, "error": "参数不完整"})

        if len(new_password) < 6:
            return jsonify({"success": False, "error": "新密码至少6位"})

        if not (any(c.isalpha() for c in new_password) and any(c.isdigit() for c in new_password)):
            return jsonify({"success": False, "error": "密码必须同时包含字母和数字"})

        # 读取用户表找到对应行
        url = f"{BASE_URL}/files/{USER_FILE_ID}/{USER_SHEET_ID}/A2:C30"
        resp = requests.get(url, headers=get_headers(), timeout=30)
        if resp.status_code != 200:
            return jsonify({"success": False, "error": "读取用户表失败"})

        data_resp = resp.json()
        rows = data_resp.get("gridData", {}).get("rows", [])
        target_row = None
        for i, row in enumerate(rows):
            values = row.get("values", [])
            row_data = [parse_cell_value(v.get("cellValue")) for v in values]
            if len(row_data) >= 2 and row_data[1] == employee_id:
                if len(row_data) >= 3 and row_data[2] == old_password:
                    target_row = i + 2  # A2 开始，所以 +2
                else:
                    return jsonify({"success": False, "error": "旧密码错误"})
                break

        if target_row is None:
            return jsonify({"success": False, "error": "员工号不存在"})

        # 更新密码（C列，文本格式）
        body = {
            "requests": [{
                "updateRangeRequest": {
                    "sheetId": USER_SHEET_ID,
                    "gridData": {
                        "startRow": target_row - 1,  # 0-based
                        "startColumn": 2,  # C列
                        "rows": [{"values": [{"cellValue": {"text": new_password}}]}]
                    }
                }
            }]
        }
        update_resp = requests.post(
            f"{BASE_URL}/files/{USER_FILE_ID}/batchUpdate",
            headers=get_headers(),
            json=body,
            timeout=30
        )
        result = update_resp.json()
        if "responses" in result:
            return jsonify({"success": True, "message": "密码修改成功"})
        else:
            return jsonify({"success": False, "error": json.dumps(result, ensure_ascii=False)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/test-connection', methods=['GET'])
@require_auth
def test_connection():
    """测试腾讯表格连接"""
    try:
        url = f"{BASE_URL}/files/{FILE_ID}"
        resp = requests.get(url, headers=get_headers())
        if resp.status_code == 200:
            data = resp.json()
            sheets = data.get("properties", [])
            sheet_names = [s["title"] for s in sheets]
            return jsonify({
                "success": True,
                "message": "连接成功",
                "sheets": sheet_names,
                "total_sheets": len(sheets)
            })
        else:
            return jsonify({"success": False, "error": f"连接失败，状态码: {resp.status_code}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
