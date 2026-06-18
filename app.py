from flask import Flask, render_template, request, jsonify, abort
from flask_cors import CORS
import requests
import json
import os
import base64
from datetime import datetime, timezone, timedelta
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from calc_engine import MODEL_CONFIG, calculate_delivery_date

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
HTTP = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=2)
HTTP.mount('https://', adapter)
HTTP.mount('http://', adapter)

# 访问密码
ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', 'queue2025')
ADMIN_EMPLOYEE_ID = "20150465"
ADMIN_KEYS = ["TENCENT_ACCESS_TOKEN", "RENDER_API_KEY", "GITHUB_TOKEN"]
RENDER_API_KEY_BOOTSTRAP = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "srv-d8l6eet7vvec73evlu7g")
GITHUB_TOKEN_BOOTSTRAP = os.environ.get("GITHUB_TOKEN", "")

BEIJING_TZ = timezone(timedelta(hours=8))
USER_CACHE_TTL = 120
MODEL_CACHE_TTL = 300

_users_cache = {"data": None, "timestamp": 0}
_models_cache = {"data": None, "timestamp": 0}
_admin_secret_cache = {"data": {}, "timestamp": 0}


def get_beijing_time_str():
    """返回北京时间字符串，用于写入腾讯表提交时间"""
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def mask_secret(value):
    """只显示前4位和后4位，避免日志泄露密钥"""
    if not value:
        return ""
    s = str(value)
    if len(s) <= 8:
        return f"{s[:1]}***{s[-1:]}"
    return f"{s[:4]}***{s[-4:]}"


def decode_token_expiry(token):
    """解析 JWT access_token 的 exp 字段（秒）"""
    if not token or not isinstance(token, str):
        return 0
    parts = token.split(".")
    if len(parts) != 3:
        return 0
    try:
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        exp = data.get("exp")
        return int(exp) if isinstance(exp, (int, float)) else 0
    except Exception:
        return 0


def get_admin_secret(name):
    """从当前实例缓存或 Render 环境变量读取管理员凭证。"""
    if name in _admin_secret_cache["data"]:
        return _admin_secret_cache["data"][name]

    fallback = os.environ.get(name, "")
    if name == "TENCENT_ACCESS_TOKEN" and not fallback:
        fallback = ACCESS_TOKEN
    elif name == "RENDER_API_KEY" and not fallback:
        fallback = RENDER_API_KEY_BOOTSTRAP
    elif name == "GITHUB_TOKEN" and not fallback:
        fallback = GITHUB_TOKEN_BOOTSTRAP
    return fallback


def set_admin_secret(name, value):
    """把管理员凭证写入当前主服务的 Render 环境变量，并更新当前实例缓存。"""
    render_key = get_admin_secret("RENDER_API_KEY") or RENDER_API_KEY_BOOTSTRAP
    if not render_key:
        raise RuntimeError("主服务尚未配置 RENDER_API_KEY，无法写入 Render 环境变量")
    if not RENDER_SERVICE_ID:
        raise RuntimeError("主服务尚未配置 RENDER_SERVICE_ID，无法定位 Render 服务")

    resp = HTTP.put(
        f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars/{name}",
        headers={
            "Authorization": f"Bearer {render_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"value": str(value)},
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"写入 Render 环境变量失败 {resp.status_code}: {resp.text[:200]}")

    _admin_secret_cache["data"][name] = str(value)

    deploy_resp = HTTP.post(
        f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys",
        headers={
            "Authorization": f"Bearer {render_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"clearCache": "do_not_clear"},
        timeout=15,
    )
    if deploy_resp.status_code not in (200, 201, 409):
        raise RuntimeError(f"已写入环境变量，但触发 Render 部署失败 {deploy_resp.status_code}: {deploy_resp.text[:200]}")


@app.after_request
def add_cache_headers(response):
    """静态资源强缓存；动态接口按数据实时性分别控制缓存"""
    path = request.path or ""
    if path.startswith("/static/"):
        # 带版本号的静态资源可长期缓存；不带版本号的不缓存
        if "?v=" in request.query_string.decode():
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        response.headers.pop("Pragma", None)
        response.headers.pop("Expires", None)
    elif path == "/api/models":
        response.headers["Cache-Control"] = "private, max-age=300"
        response.headers.pop("Pragma", None)
        response.headers.pop("Expires", None)
    elif path == "/auth/users":
        response.headers["Cache-Control"] = "private, max-age=120"
        response.headers.pop("Pragma", None)
        response.headers.pop("Expires", None)
    elif path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    else:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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


def require_ligang_admin(f):
    """仅允许李刚（员工号 20150465）访问管理员凭证接口"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        employee_id = normalize_user_key(request.headers.get('X-Employee-Id', ''))
        if employee_id != ADMIN_EMPLOYEE_ID:
            return jsonify({"success": False, "error": "无管理员权限"}), 403
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
        "Access-Token": get_admin_secret("TENCENT_ACCESS_TOKEN") or ACCESS_TOKEN,
        "Open-Id": OPEN_ID,
        "Client-Id": CLIENT_ID
    }


def read_users():
    """读取用户表，带短时缓存，返回用户权限信息"""
    now = time.time()
    if _users_cache["data"] is not None and (now - _users_cache["timestamp"]) < USER_CACHE_TTL:
        return _users_cache["data"]

    url = f"{BASE_URL}/files/{USER_FILE_ID}/{USER_SHEET_ID}/A2:F200"
    resp = HTTP.get(url, headers=get_headers(), timeout=30)
    users = []
    if resp.status_code == 200:
        data = resp.json()
        rows = data.get("gridData", {}).get("rows", [])
        for row in rows:
            values = row.get("values", [])
            row_data = [parse_cell_value(v.get("cellValue")) for v in values]
            if len(row_data) >= 3 and row_data[0] and row_data[1]:
                role = row_data[3].strip() if len(row_data) >= 4 else ""
                department = row_data[4].strip() if len(row_data) >= 5 else ""
                permission_text = row_data[5].strip() if len(row_data) >= 6 else ""
                is_admin = role == "管理员" or permission_text == "能操作所有数据"
                is_manager = role == "经理" or permission_text == "能操作本部门所有数据"
                access_level = "admin" if is_admin else ("department" if is_manager else "self")
                users.append({
                    "name": row_data[0],
                    "employee_id": row_data[1],
                    "password": row_data[2],
                    "is_admin": is_admin,
                    "is_manager": is_manager,
                    "role": role,
                    "department": department,
                    "access_level": access_level,
                    "permission": permission_text
                })
    _users_cache["data"] = users
    _users_cache["timestamp"] = now
    return users


def is_user_admin(employee_id):
    """检查用户是否是管理员"""
    user = get_user_by_id(employee_id)
    return bool(user and user.get("access_level") == "admin")


def get_user_by_id(employee_id):
    """按员工号读取用户"""
    current_id = normalize_user_key(employee_id)
    if not current_id:
        return None
    for user in read_users():
        if normalize_user_key(user.get("employee_id", "")) == current_id:
            return user
    return None


def get_user_by_name(name):
    """按姓名读取用户，兼容历史订单缺少员工号的情况"""
    current_name = str(name or "").strip()
    if not current_name:
        return None
    for user in read_users():
        if str(user.get("name", "")).strip() == current_name:
            return user
    return None


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

    # 设置字号：腾讯表格读取到的实际格式字段是 cellFormat.textFormat.fontSize
    if font_size:
        text_format = {
            "fontSize": font_size,
            "font": "SimSun"
        }
        cell["cellFormat"] = {
            "textFormat": text_format
        }
        # 保留兼容字段，避免不同接口版本识别差异
        cell["textFormat"] = text_format

    return cell


def read_sheet_range(sheet_id, range_str):
    """读取表格范围数据，返回gridData"""
    url = f"{BASE_URL}/files/{FILE_ID}/{sheet_id}/{range_str}"
    resp = HTTP.get(url, headers=get_headers(), timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("gridData", {})
    return {}


def get_next_empty_row(sheet_id):
    """获取表格下一个空行号（1-based），从A列第一个空行开始扫描（跳过表头第1行）
    使用缓存：从上次找到的空行号开始向后搜索，避免每次从第2行扫起"""
    cached_row = _last_empty_row_cache.get("row", 0)
    start_search = max(2, cached_row)  # 至少从第2行开始
    batch_size = 200
    for offset in range(start_search - 1, 2000, batch_size):
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
                _last_empty_row_cache["row"] = actual_row
                return actual_row

    return 2001  # 如果前2000行都满了


def batch_update(requests_body):
    """执行批量更新操作"""
    url = f"{BASE_URL}/files/{FILE_ID}/batchUpdate"
    resp = HTTP.post(url, headers=get_headers(), json=requests_body, timeout=30)
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
    """清空一行内容（row_index_1based从1开始），不删除物理行，避免下面订单行号上移"""
    if row_index_1based < 2:
        raise ValueError("无效行号，不能删除表头或不存在的行")

    empty_values = [build_cell_value("") for _ in range(12)]
    body = {
        "requests": [{
            "updateRangeRequest": {
                "sheetId": SHEET_ID,
                "gridData": {
                    "startRow": row_index_1based - 1,
                    "startColumn": 0,
                    "rows": [{"values": empty_values}]
                }
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
        now = time.time()
        if _models_cache["data"] is not None and (now - _models_cache["timestamp"]) < MODEL_CACHE_TTL:
            return jsonify({"success": True, "models": _models_cache["data"]})

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
        # 合并计算配置中的型号，避免牌号表漏填时下拉选项不全
        models = list(dict.fromkeys(models + list(MODEL_CONFIG.keys())))
        _models_cache["data"] = models
        _models_cache["timestamp"] = now
        return jsonify({"success": True, "models": models})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# 计算结果缓存：避免重复计算
_calc_result_cache = {"key": None, "result": None, "timestamp": 0}
_CALC_CACHE_TTL = 30  # 30秒缓存

# 待处理行缓存
_pending_row_cache = {"data": None, "timestamp": 0}
_PENDING_ROW_CACHE_TTL = 30
_last_empty_row_cache = {"row": 0}  # 缓存上次找到的空行号

def _get_pending_rows():
    """获取所有待处理行（F列为空的行），带缓存"""
    import time
    now = time.time()
    if _pending_row_cache["data"] is not None and (now - _pending_row_cache["timestamp"]) < _PENDING_ROW_CACHE_TTL:
        return _pending_row_cache["data"]

    pending = {}  # model -> row_index
    batch_size = 200

    # 先扫描前500行（大多数情况下数据在前500行）
    initial_scan_limit = 500
    found_pending_in_initial = False
    for offset in range(0, initial_scan_limit, batch_size):
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
                found_pending_in_initial = True

        if len(rows) < batch_size:
            break

    # 如果前500行没有待处理行，扩展扫描到2000行
    if not found_pending_in_initial:
        for offset in range(initial_scan_limit, 2000, batch_size):
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
        submit_time = get_beijing_time_str()

        if row_index > 0:
            # 检查该行A列是否为空，如果不为空则找新行
            grid_data = read_sheet_range(SHEET_ID, f"A{row_index}:A{row_index}")
            rows = grid_data.get("rows", [])
            a_col_empty = True
            if rows and len(rows) > 0:
                for v in rows[0].get("values", []):
                    cv = v.get("cellValue")
                    if cv:
                        text = parse_cell_value(cv)
                        if text.strip():
                            a_col_empty = False
                            break

            if a_col_empty:
                # A列为空，可以写入
                write_row_idx = row_index - 1  # 转为0-based
                serial_no = str(row_index)
            else:
                # A列有数据，找新行
                empty_row = get_next_empty_row(SHEET_ID)
                write_row_idx = empty_row - 1
                serial_no = str(empty_row)
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
                clear_order_caches()
                return jsonify({"success": True, "message": "订单创建成功"})
            return jsonify({"success": False, "error": "写入0个单元格"})
        else:
            return jsonify({"success": False, "error": json.dumps(result, ensure_ascii=False)})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# 全局缓存：原始订单数据（不过滤权限和日期）
_orders_cache = {"data": None, "timestamp": 0}
# 全局缓存：过滤+排序后的结果（按用户和view_mode缓存）
_filtered_cache = {"timestamp": 0}
CACHE_TTL = 120  # 缓存120秒，减少API调用频率

def clear_order_caches():
    """清空订单相关缓存，确保增删改后页面重新读取腾讯表最新数据"""
    _orders_cache["data"] = None
    _orders_cache["timestamp"] = 0
    _filtered_cache.clear()
    _filtered_cache["timestamp"] = 0
    _pending_row_cache["data"] = None
    _pending_row_cache["timestamp"] = 0
    _last_empty_row_cache["row"] = 0


def clear_user_caches():
    """清空用户缓存，用于改密码后立即生效"""
    _users_cache["data"] = None
    _users_cache["timestamp"] = 0

def _read_batch(sheet_id, range_str):
    """读取一批数据，供并行调用"""
    return read_sheet_range(sheet_id, range_str)

def fetch_all_orders_raw():
    """从腾讯表格读取所有订单原始数据，带缓存，并行读取加速"""
    now = datetime.now().timestamp()
    # 缓存命中条件：数据非空且在TTL内（或没有强制刷新标记）
    cache_valid = _orders_cache["data"] and len(_orders_cache["data"]) > 0 and (now - _orders_cache["timestamp"]) < CACHE_TTL
    if cache_valid and not _orders_cache.get("_refresh_flag"):
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
            batch_has_data = False
            for i, row in enumerate(rows):
                actual_row = start + i
                values = row.get("values", [])
                if values:
                    cv = values[0].get("cellValue")
                    if cv:
                        text = parse_cell_value(cv)
                        if text.strip():
                            last_data_row = max(last_data_row, actual_row)
                            batch_has_data = True
            # 不再提前停止——数据可能不连续（中间有空行）
            # if not batch_has_data:
            #     break

    if last_data_row <= 1:
        # API读取失败时，如果有有效缓存则返回缓存（避免显示空）
        if _orders_cache["data"] and len(_orders_cache["data"]) > 0:
            return _orders_cache["data"]
        # 不缓存空结果，避免空数组污染缓存
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

    # 如果解析结果为空但有缓存数据，使用缓存（降级）
    if not orders and _orders_cache["data"] is not None and len(_orders_cache["data"]) > 0:
        return _orders_cache["data"]

    # 只有当解析结果非空时才更新缓存
    if orders:
        _orders_cache["data"] = orders
        _orders_cache["timestamp"] = now
        # 清除过滤缓存（原始数据已更新）
        _filtered_cache.clear()
        _filtered_cache["timestamp"] = 0
    return orders

def normalize_user_key(value):
    """标准化员工号，兼容腾讯表数字单元格可能出现的20150465.0"""
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def is_same_submitter(order, submitter_id, submitter_name):
    """判断订单是否属于当前用户：优先员工号，兼容历史数据按姓名兜底"""
    current_id = normalize_user_key(submitter_id)
    row_id = normalize_user_key(order.get("submitter_id", ""))
    current_name = str(submitter_name or "").strip()
    row_name = str(order.get("submitter", "")).strip()

    if current_id and row_id and current_id == row_id:
        return True
    if current_name and row_name and current_name == row_name:
        return True
    return False


def order_matches_expected(order, expected):
    """删除前校验当前行仍是用户点选的订单，防止行号变化导致误删"""
    if not expected:
        return True
    keys = ["model", "customer", "submitter_id", "submit_time"]
    for key in keys:
        expected_value = str(expected.get(key, "") or "").strip()
        if expected_value and str(order.get(key, "") or "").strip() != expected_value:
            return False
    return True


def resolve_submitter_name(submitter_id, submitter_name=""):
    """优先使用前端传来的姓名；没有姓名时按员工号从用户表查姓名"""
    name = str(submitter_name or "").strip()
    if name and name != "用户":
        return name

    current_id = normalize_user_key(submitter_id)
    if not current_id:
        return name
    for user in read_users():
        if normalize_user_key(user.get("employee_id", "")) == current_id:
            return str(user.get("name", "")).strip()
    return name


def get_order_submitter_user(order):
    """获取订单提交人的用户记录，优先员工号，兼容历史姓名"""
    return get_user_by_id(order.get("submitter_id", "")) or get_user_by_name(order.get("submitter", ""))


def can_operate_order(order, current_user, submitter_id, submitter_name, view_mode="mine"):
    """按用户表权限判断是否可查看/操作订单"""
    access_level = (current_user or {}).get("access_level", "self")
    if view_mode == "mine" or access_level == "self":
        return is_same_submitter(order, submitter_id, submitter_name)
    if access_level == "admin":
        return True
    if access_level == "department":
        current_dept = str((current_user or {}).get("department", "")).strip()
        order_user = get_order_submitter_user(order)
        order_dept = str((order_user or {}).get("department", "")).strip()
        return bool(current_dept and order_dept and current_dept == order_dept)
    return is_same_submitter(order, submitter_id, submitter_name)


def normalize_view_mode(current_user, requested_view_mode):
    """根据权限标准化视图：管理员全部，经理本部门，业务员本人"""
    access_level = (current_user or {}).get("access_level", "self")
    if requested_view_mode == "all" and access_level in ("admin", "department"):
        return "all"
    return "mine"


def get_filtered_orders(submitter_id, current_user, view_mode, submitter_name=""):
    """获取过滤+排序后的订单列表，带缓存"""
    now = datetime.now().timestamp()
    submitter_name = resolve_submitter_name(submitter_id, submitter_name)
    access_level = (current_user or {}).get("access_level", "self")
    is_mine_view = view_mode == "mine"
    cache_key = f"{access_level}:{view_mode}:{normalize_user_key(submitter_id)}:{submitter_name}:{(current_user or {}).get('department', '')}"

    # 检查过滤缓存是否有效（基于原始数据的时间戳）
    if (_filtered_cache.get(cache_key) is not None and
        _filtered_cache.get("timestamp", 0) == _orders_cache.get("timestamp", 0) and
        (now - _orders_cache["timestamp"]) < CACHE_TTL):
        return _filtered_cache[cache_key]

    # 读取原始数据
    all_orders = fetch_all_orders_raw()
    today = datetime.now().date()

    # 过滤
    orders = []
    for order in all_orders:
        # 权限过滤：管理员全部、经理本部门、业务员本人
        if not can_operate_order(order, current_user, submitter_id, submitter_name, view_mode):
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

    # 缓存过滤结果（包括我的排队和全部排队）
    _filtered_cache[cache_key] = orders
    _filtered_cache["timestamp"] = _orders_cache["timestamp"]

    return orders


@app.route('/api/orders', methods=['GET'])
@require_auth
def get_orders():
    """获取订单列表：管理员可查看所有或仅自己的；仅显示期望发货日期>=今天的订单；支持分页"""
    try:
        if request.args.get('refresh') == '1':
            _orders_cache["timestamp"] = 0  # 标记缓存过期，但不清空数据（降级用）

        submitter_id = request.args.get('submitter_id', '')
        submitter_name = request.args.get('submitter_name', '')
        current_user = get_user_by_id(submitter_id) or {}
        is_admin = current_user.get("access_level") == "admin"
        requested_view_mode = request.args.get('view_mode', 'mine')
        view_mode = normalize_view_mode(current_user, requested_view_mode)
        model_filter = request.args.get('model_filter', '').strip()
        customer_filter = request.args.get('customer_filter', '').strip().lower()
        sort_type = request.args.get('sort', '').strip()
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        if page < 1:
            page = 1
        if per_page < 1:
            per_page = 20
        if per_page > 100:
            per_page = 100

        # 使用缓存的过滤结果
        orders = get_filtered_orders(submitter_id, current_user, view_mode, submitter_name)
        if model_filter:
            orders = [o for o in orders if o.get("model", "") == model_filter]
        if customer_filter:
            orders = [o for o in orders if customer_filter in o.get("customer", "").lower()]
        if sort_type:
            def sort_key(o):
                if sort_type == "model":
                    return o.get("model", "")
                if sort_type == "queueDate":
                    return o.get("queue_date", "") or "9999-12-31"
                if sort_type == "tonnage":
                    try:
                        return float(o.get("tonnage", 0) or 0)
                    except ValueError:
                        return 0
                return ""
            orders = sorted(orders, key=sort_key)

        # 分页
        total = len(orders)
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_orders = orders[start_idx:end_idx]

        return jsonify({
            "success": True,
            "orders": paginated_orders,
            "is_admin": is_admin,
            "access_level": current_user.get("access_level", "self"),
            "department": current_user.get("department", ""),
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
        submitter_name = request.args.get('submitter_name', '')
        current_user = get_user_by_id(submitter_id) or {}

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
            "submitter_id": row_data[10],
            "submit_time": row_data[11]
        }

        # 检查权限
        if not can_operate_order(order, current_user, submitter_id, submitter_name, "all"):
            return jsonify({"success": False, "error": "无权查看他人订单"})

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
        current_user = get_user_by_id(submitter_id) or {}

        # 读取原订单检查权限和吨位
        grid_data = read_sheet_range(SHEET_ID, f"A{row_index}:L{row_index}")
        rows = grid_data.get("rows", [])
        if rows:
            orig_values = [parse_cell_value(v.get("cellValue")) for v in rows[0].get("values", [])]
            original_tonnage = orig_values[1] if len(orig_values) > 1 else "0"
            original_order = {
                "submitter": orig_values[6] if len(orig_values) > 6 else "",
                "submitter_id": orig_values[10] if len(orig_values) > 10 else ""
            }
            # 权限检查：管理员全部、经理本部门、业务员本人
            if not can_operate_order(original_order, current_user, submitter_id, submitter, "all"):
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
            calc_date_for_update, queue_date, submitter, remark, str(row_index), submitter_id,
            get_beijing_time_str()
        )
        result = resp.json()

        if "responses" in result:
            clear_order_caches()
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
        data = request.get_json(silent=True) or {}
        expected_order = data.get("order") or data
        submitter_id = request.args.get('submitter_id', '')
        submitter_name = request.args.get('submitter_name', '')
        current_user = get_user_by_id(submitter_id) or {}

        # 读取原订单检查权限
        grid_data = read_sheet_range(SHEET_ID, f"A{row_index}:L{row_index}")
        rows = grid_data.get("rows", [])
        if rows:
            orig_values = [parse_cell_value(v.get("cellValue")) for v in rows[0].get("values", [])]
            original_order = {
                "model": orig_values[0] if len(orig_values) > 0 else "",
                "customer": orig_values[2] if len(orig_values) > 2 else "",
                "submitter": orig_values[6] if len(orig_values) > 6 else "",
                "submitter_id": orig_values[10] if len(orig_values) > 10 else "",
                "submit_time": orig_values[11] if len(orig_values) > 11 else ""
            }
            if not order_matches_expected(original_order, expected_order):
                return jsonify({"success": False, "error": "订单行号已变化，请刷新后重试，未执行删除"})
            # 权限检查：管理员全部、经理本部门、业务员本人
            if not can_operate_order(original_order, current_user, submitter_id, submitter_name, "all"):
                return jsonify({"success": False, "error": "无权删除他人订单"})
        else:
            return jsonify({"success": False, "error": "订单不存在"})

        resp = delete_row(row_index)
        result = resp.json()
        if "responses" in result:
            clear_order_caches()
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
        url = f"{BASE_URL}/files/{USER_FILE_ID}/{USER_SHEET_ID}/A2:C200"
        resp = HTTP.get(url, headers=get_headers(), timeout=30)
        if resp.status_code != 200:
            return jsonify({"success": False, "error": "读取用户表失败"})

        data_resp = resp.json()
        rows = data_resp.get("gridData", {}).get("rows", [])
        target_row = None
        for i, row in enumerate(rows):
            values = row.get("values", [])
            row_data = [parse_cell_value(v.get("cellValue")) for v in values]
            if len(row_data) >= 2 and normalize_user_key(row_data[1]) == normalize_user_key(employee_id):
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
        update_resp = HTTP.post(
            f"{BASE_URL}/files/{USER_FILE_ID}/batchUpdate",
            headers=get_headers(),
            json=body,
            timeout=30
        )
        result = update_resp.json()
        if "responses" in result:
            clear_user_caches()
            return jsonify({"success": True, "message": "密码修改成功"})
        else:
            return jsonify({"success": False, "error": json.dumps(result, ensure_ascii=False)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ============ 管理员凭证管理（仅李刚 20150465） ============

def summarize_admin_key(name):
    value = get_admin_secret(name)
    item = {"name": name, "present": bool(value), "masked": mask_secret(value) if value else ""}
    if name == "TENCENT_ACCESS_TOKEN" and value:
        exp = decode_token_expiry(value)
        item["expires_at"] = exp
        if exp:
            item["remaining_seconds"] = exp - int(time.time())
            item["expires_at_text"] = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    return item


def validate_tencent_token(token):
    try:
        headers = {
            "Content-Type": "application/json",
            "Access-Token": token,
            "Open-Id": OPEN_ID,
            "Client-Id": CLIENT_ID,
        }
        url = f"{BASE_URL}/files/{USER_FILE_ID}/{USER_SHEET_ID}/A1:A1"
        resp = HTTP.get(url, headers=headers, timeout=20)
        text = resp.text
        if resp.status_code != 200:
            return False, f"腾讯接口 {resp.status_code}: {text[:160]}"
        try:
            data = resp.json()
        except Exception:
            return False, f"腾讯接口返回异常: {text[:160]}"
        if data.get("code") and data.get("code") != 0:
            return False, f"腾讯接口错误 code={data.get('code')}: {data.get('message', '')}"
        if data.get("gridData") is not None:
            return True, ""
        return False, f"腾讯接口未返回数据: {text[:160]}"
    except Exception as e:
        return False, f"腾讯接口异常: {e}"


def validate_render_key(token):
    try:
        resp = HTTP.get(
            "https://api.render.com/v1/services?limit=1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=20,
        )
        if resp.status_code == 200:
            return True, ""
        return False, f"Render {resp.status_code}: {resp.text[:160]}"
    except Exception as e:
        return False, f"Render 接口异常: {e}"


def validate_github_token(token):
    try:
        resp = HTTP.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
        )
        if resp.status_code == 200:
            return True, ""
        return False, f"GitHub {resp.status_code}: {resp.text[:160]}"
    except Exception as e:
        return False, f"GitHub 接口异常: {e}"


def validate_admin_key(key, value):
    if key == "TENCENT_ACCESS_TOKEN":
        return validate_tencent_token(value)
    if key == "RENDER_API_KEY":
        return validate_render_key(value)
    if key == "GITHUB_TOKEN":
        return validate_github_token(value)
    return False, "未知配置项"


@app.route('/api/admin/status', methods=['GET'])
@require_auth
@require_ligang_admin
def admin_status():
    return jsonify({"success": True, "items": [summarize_admin_key(k) for k in ADMIN_KEYS]})


@app.route('/api/admin/validate', methods=['POST'])
@require_auth
@require_ligang_admin
def admin_validate():
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    value = str(data.get("value") or "").strip()
    if key not in ADMIN_KEYS:
        return jsonify({"success": False, "error": "未知配置项"}), 400
    if not value:
        return jsonify({"success": False, "error": "请输入 token 后再校验"}), 400
    ok, err = validate_admin_key(key, value)
    return jsonify({"success": ok, "error": err})


@app.route('/api/admin/deploy', methods=['POST'])
def admin_trigger_deploy():
    """触发 Render 重新部署（无需认证，仅用于CI/CD）"""
    render_key = get_admin_secret("RENDER_API_KEY")
    if not render_key:
        return jsonify({"success": False, "error": "未配置 RENDER_API_KEY"}), 500
    try:
        resp = HTTP.post(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys",
            headers={
                "Authorization": f"Bearer {render_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"clearCache": "do_not_clear"},
            timeout=15,
        )
        if resp.status_code in (200, 201, 409):
            return jsonify({"success": True, "status": resp.status_code})
        return jsonify({"success": False, "error": f"Render API 返回 {resp.status_code}"}), 502
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/admin/update', methods=['POST'])
@require_auth
@require_ligang_admin
def admin_update():
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    value = str(data.get("value") or "").strip()
    if key not in ADMIN_KEYS:
        return jsonify({"success": False, "error": "未知配置项"}), 400
    if not value:
        return jsonify({"success": False, "error": "请输入新的值"}), 400

    ok, err = validate_admin_key(key, value)
    if not ok:
        return jsonify({"success": False, "error": f"校验未通过：{err}"}), 400

    try:
        set_admin_secret(key, value)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    log_entry = {
        "key": key,
        "masked": mask_secret(value),
        "by": ADMIN_EMPLOYEE_ID,
        "at": datetime.now(timezone.utc).isoformat()
    }
    return jsonify({
        "success": True,
        "effective": True,
        "storage": "render_env",
        "message": "已写入 Render 环境变量，当前实例已临时生效，并已触发重新部署",
        "log": log_entry
    })


@app.route('/api/admin/health', methods=['GET'])
@require_auth
@require_ligang_admin
def admin_health():
    issues = []
    token = get_admin_secret("TENCENT_ACCESS_TOKEN") or ACCESS_TOKEN
    ok, err = validate_tencent_token(token)
    if not ok:
        issues.append({"key": "TENCENT_ACCESS_TOKEN", "level": "error", "message": err})

    exp = decode_token_expiry(token)
    if exp:
        remain = exp - int(time.time())
        if remain <= 0:
            issues.append({"key": "TENCENT_ACCESS_TOKEN", "level": "error", "message": "腾讯 access_token 已过期，请尽快更新"})
        elif remain < 24 * 3600:
            issues.append({"key": "TENCENT_ACCESS_TOKEN", "level": "warn", "message": f"腾讯 access_token 将在 {remain // 3600} 小时内过期，请及时更新"})

    render_key = get_admin_secret("RENDER_API_KEY")
    if render_key:
        ok, err = validate_render_key(render_key)
        if not ok:
            issues.append({"key": "RENDER_API_KEY", "level": "error", "message": err})

    github_key = get_admin_secret("GITHUB_TOKEN")
    if github_key:
        ok, err = validate_github_token(github_key)
        if not ok:
            issues.append({"key": "GITHUB_TOKEN", "level": "error", "message": err})

    return jsonify({"success": True, "healthy": len(issues) == 0, "issues": issues})


@app.route('/api/test-connection', methods=['GET'])
@require_auth
def test_connection():
    """测试腾讯表格连接"""
    try:
        url = f"{BASE_URL}/files/{FILE_ID}"
        resp = HTTP.get(url, headers=get_headers(), timeout=30)
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


def _warmup_keepalive():
    """后台线程：启动时预热建立初始缓存"""
    import threading
    def run():
        # 启动时等待服务完全就绪，然后尝试预热10次建立初始缓存
        time.sleep(5)
        for attempt in range(10):
            try:
                fetch_all_orders_raw()
                if _orders_cache["data"] and len(_orders_cache["data"]) > 0:
                    print(f"[warmup] 初始缓存建立成功: {len(_orders_cache['data'])}条")
                    break
            except Exception as e:
                print(f"[warmup] 初始预热失败(attempt {attempt+1}): {e}")
            time.sleep(3)
    t = threading.Thread(target=run, daemon=True)
    t.start()

_warmup_keepalive()

# ==================== 计算公式管理 API ====================

FORMULA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'formulas.json')

def _load_formulas():
    """加载公式配置"""
    try:
        if os.path.exists(FORMULA_FILE):
            with open(FORMULA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return {}

def _save_formulas(formulas):
    """保存公式配置"""
    with open(FORMULA_FILE, 'w', encoding='utf-8') as f:
        json.dump(formulas, f, ensure_ascii=False, indent=2)

@app.route('/api/admin/formulas', methods=['GET'])
@require_auth
def get_formulas():
    """获取当前公式配置"""
    try:
        current_user_id = request.args.get('submitter_id', '')
        if current_user_id != ADMIN_EMPLOYEE_ID:
            return jsonify({"success": False, "error": "无权限"})
        formulas = _load_formulas()
        return jsonify({"success": True, "formulas": formulas})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/admin/formulas', methods=['POST'])
@require_auth
def save_formulas():
    """保存公式配置"""
    try:
        current_user_id = request.args.get('submitter_id', '')
        if current_user_id != ADMIN_EMPLOYEE_ID:
            return jsonify({"success": False, "error": "无权限"})
        data = request.json
        formulas = data.get('formulas', {})
        _save_formulas(formulas)
        return jsonify({"success": True, "message": f"已保存 {len(formulas)} 条公式"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
