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
from calc_engine import MODEL_CONFIG, calculate_delivery_date, set_token_getter

app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app)

# 将token获取函数注入calc_engine，确保计算引擎使用与管理员缓存同步的token
set_token_getter(lambda: get_admin_secret("TENCENT_ACCESS_TOKEN") or ACCESS_TOKEN)

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
        text = cell_value["text"]
        # 如果text是日期格式（如 6月26日），转换为标准格式
        import re
        # 匹配 "6月26日" 格式
        match = re.match(r'^(\d{1,2})月(\d{1,2})日$', text.strip())
        if match:
            month = int(match.group(1))
            day = int(match.group(2))
            # 使用当前年份
            from datetime import datetime
            year = datetime.now().year
            return f"{year}-{month:02d}-{day:02d}"
        # 匹配 "2026年6月26日" 格式
        match = re.match(r'^(\d{4})年(\d{1,2})月(\d{1,2})日$', text.strip())
        if match:
            year = int(match.group(1))
            month = int(match.group(2))
            day = int(match.group(3))
            return f"{year}-{month:02d}-{day:02d}"
        return text
    if "number" in cell_value:
        return str(cell_value["number"])
    if "time" in cell_value:
        t = cell_value["time"]
        # 腾讯API返回的time对象中的year/month/day已经是本地时间（北京时间）
        # 直接使用，不需要时区转换
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


def get_next_empty_row(sheet_id, start_from=2):
    """获取表格下一个空行号（1-based），从A列第一个空行开始扫描（跳过表头第1行）
    优化：增大批次到500行，减少API调用次数
    安全：start_from 参数允许调用方指定从哪行开始扫描，避免多人并发时依赖全局缓存"""
    batch_size = 500
    for offset in range(start_from - 1, 2000, batch_size):
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
    resp = HTTP.post(url, headers=get_headers(), json=requests_body, timeout=30)
    return resp


def ensure_sheet_rows(min_row_count):
    """确保表格至少有 min_row_count 行，不足时自动添加行（每次批量添加500行）
    带缓存+锁：记住上次表格总行数，避免每次都调用API查询；加锁避免多人同时重复扩容"""
    # 快速路径：缓存命中直接返回（无锁）
    cached_count = _sheet_row_count_cache.get("count", 0)
    if cached_count >= min_row_count:
        return True

    # 慢速路径：加锁保护，确保只有一个线程执行扩容
    with _sheet_row_count_lock:
        # 双重检查：其他线程可能已经在等待期间完成了扩容
        cached_count = _sheet_row_count_cache.get("count", 0)
        if cached_count >= min_row_count:
            return True

        # 先获取当前表格信息
        url = f"{BASE_URL}/files/{FILE_ID}"
        resp = HTTP.get(url, headers=get_headers(), timeout=30)
        if resp.status_code != 200:
            return False
        data = resp.json()
        sheets = data.get("data", {}).get("sheets", [])
        current_row_count = 0
        for s in sheets:
            if s.get("sheetID") == SHEET_ID:
                current_row_count = s.get("rowCount", 0)
                break
        if current_row_count <= 0:
            for s in sheets:
                if s.get("sheetID") == SHEET_ID:
                    gp = s.get("gridProperties", {})
                    current_row_count = gp.get("rowCount", 0)
                    break

        _sheet_row_count_cache["count"] = current_row_count

        if current_row_count >= min_row_count:
            return True

        # 需要添加行数（每次至少加500行，避免频繁调用）
        rows_to_add = max(500, min_row_count - current_row_count)
        body = {
            "requests": [{
                "insertDimension": {
                    "range": {
                        "sheetID": SHEET_ID,
                        "dimension": "ROWS",
                        "startIndex": current_row_count + 1,
                        "endIndex": current_row_count + 1 + rows_to_add
                    }
                }
            }]
        }
        resp = batch_update(body)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("ret") == 0 or "responses" in result:
                _sheet_row_count_cache["count"] = current_row_count + rows_to_add
                return True
        return False


def is_date_string(value):
    """判断字符串是否为日期格式 YYYY-MM-DD 或 YYYY年M月D日"""
    if not value:
        return False
    import re
    text = str(value).strip()
    # 标准格式
    if re.match(r'^\d{4}-\d{2}-\d{2}$', text):
        return True
    # 中文格式（如 2026年6月25日）
    if re.match(r'^\d{4}年\d{1,2}月\d{1,2}日$', text):
        return True
    # 短格式（如 6月25日）
    if re.match(r'^\d{1,2}月\d{1,2}日$', text):
        return True
    return False


def write_temp_row(row_index_0based, model, tonnage, expected_date):
    """写入临时数据到腾讯表格（只写A/B/D列，E列保留公式）
    用于calculate-date时触发腾讯表格公式计算可发货日期
    """
    row_values = [
        build_cell_value(model),                        # A: 型号
        build_cell_value(tonnage, is_number=True),      # B: 吨位
        build_cell_value(""),                            # C: 客户（空）
        build_cell_value(expected_date, is_date=True),  # D: 期望发货日期
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


def read_calculated_date_from_row(row_index_1based):
    """从指定行的E列读取公式计算结果"""
    grid_data = read_sheet_range(SHEET_ID, f"E{row_index_1based}:E{row_index_1based}")
    rows = grid_data.get("rows", [])
    if rows and len(rows) > 0:
        values = rows[0].get("values", [])
        if values:
            cv = values[0].get("cellValue")
            if cv:
                return parse_cell_value(cv)
    return ""


def clear_temp_row(row_index_1based):
    """清空临时写入的行（只清空A/B/D列，不动E列公式）
    安全机制：如果该行已提交（K列有提交人ID），则不清空
    """
    if row_index_1based < 2:
        return

    # 安全机制：检查该行是否已提交（K列是否有提交人ID）
    try:
        grid_data = read_sheet_range(SHEET_ID, f"K{row_index_1based}:K{row_index_1based}")
        rows = grid_data.get("rows", [])
        if rows and len(rows) > 0:
            values = rows[0].get("values", [])
            if values:
                cv = values[0].get("cellValue")
                if cv and parse_cell_value(cv).strip():
                    # K列有值，说明该行已提交，不清理
                    print(f"[clear_temp_row] 跳过已提交行 row={row_index_1based}")
                    return
    except Exception as e:
        print(f"[clear_temp_row] 检查行状态失败 row={row_index_1based}: {e}")
        # 检查失败时，不清理（安全优先）
        return

    requests = [
        # 清空A-D列
        {
            "updateRangeRequest": {
                "sheetId": SHEET_ID,
                "gridData": {
                    "startRow": row_index_1based - 1,
                    "startColumn": 0,
                    "rows": [{
                        "values": [
                            build_cell_value(""),   # A: 型号
                            build_cell_value(""),   # B: 吨位
                            build_cell_value(""),   # C: 客户
                            build_cell_value(""),   # D: 期望发货日期
                        ]
                    }]
                }
            }
        }
    ]
    body = {"requests": requests}
    return batch_update(body)


def write_order_row(row_index_0based, model, tonnage, customer, expected_date, calculated_date, queue_date, submitter, remark, serial_no, submitter_id, submit_time):
    """写入一行完整订单数据到腾讯表格（row_index_0based从0开始）
    E列（可发货日期）由腾讯表格公式计算，不覆盖
    使用updateRangeRequest写入A-D列，然后单独写入F-L列
    """
    queue_date_is_date = is_date_string(queue_date)

    requests = []

    # 1. 写入A-D列（型号、吨位、客户、期望发货日期）
    requests.append({
        "updateRangeRequest": {
            "sheetId": SHEET_ID,
            "gridData": {
                "startRow": row_index_0based,
                "startColumn": 0,
                "rows": [{
                    "values": [
                        build_cell_value(model),                        # A: 型号
                        build_cell_value(tonnage, is_number=True),      # B: 吨位
                        build_cell_value(customer),                      # C: 客户
                        build_cell_value(expected_date, is_date=True),  # D: 期望发货日期
                    ]
                }]
            }
        }
    })

    # 2. 写入F-L列（跳过E列，保留公式）
    requests.append({
        "updateRangeRequest": {
            "sheetId": SHEET_ID,
            "gridData": {
                "startRow": row_index_0based,
                "startColumn": 5,  # F列开始
                "rows": [{
                    "values": [
                        build_cell_value(queue_date, is_date=queue_date_is_date),  # F: 排队日期
                        build_cell_value(submitter),                     # G: 提交人
                        build_cell_value(remark),                        # H: 备注
                        build_cell_value(serial_no),                     # I: 序号
                        build_cell_value(""),                            # J: 上次录入
                        build_cell_value(submitter_id),                  # K: 提交人ID
                        build_cell_value(submit_time),                   # L: 提交时间
                    ]
                }]
            }
        }
    })

    body = {"requests": requests}
    return batch_update(body)


def delete_row(row_index_1based):
    """软删除订单：仅清空吨位（B列），J列写入DELETED标识，保留其他数据"""
    if row_index_1based < 2:
        raise ValueError("无效行号，不能删除表头或不存在的行")

    requests = [
        # B列：清空吨位
        {
            "updateRangeRequest": {
                "sheetId": SHEET_ID,
                "gridData": {
                    "startRow": row_index_1based - 1,
                    "startColumn": 1,  # B列
                    "rows": [{"values": [build_cell_value("")]}]
                }
            }
        },
        # J列：写入DELETED标识
        {
            "updateRangeRequest": {
                "sheetId": SHEET_ID,
                "gridData": {
                    "startRow": row_index_1based - 1,
                    "startColumn": 9,  # J列
                    "rows": [{"values": [build_cell_value("DELETED")]}]
                }
            }
        }
    ]
    body = {"requests": requests}
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
_PENDING_ROW_CACHE_TTL = 300

# 临时写入行跟踪：记录用户session临时占用的行，用于5分钟超时清理
_temp_row_tracker = {}  # key: f"{employee_id}:{model}", value: {"row_index": int, "timestamp": float, "submitter_id": str}
_temp_row_lock = threading.Lock()
_TEMP_ROW_TIMEOUT = 300  # 5分钟超时（秒）

# 表格总行数缓存（带锁保护，避免多人同时触发重复扩容）
_sheet_row_count_cache = {"count": 0}
_sheet_row_count_lock = threading.Lock()

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
    """计算可发货日期：写入A/B/D列到腾讯表格触发公式计算，然后读取E列结果"""
    try:
        data = request.json
        model = data.get('model', '')
        tonnage = data.get('tonnage', '')
        expected_date = data.get('expected_date', '')
        pending_row_index = data.get('pending_row_index', 0)
        submitter_id = data.get('submitter_id', '')
        force_refresh = data.get('force_refresh', False)

        import time
        now = time.time()

        # 清理超时的临时行
        _cleanup_expired_temp_rows()

        # 1. 确定目标行：始终复用同一行（只要没提交）
        # 优先使用前端传入的pending_row_index（当前正在编辑的行）
        # 如果前端没有传入，查找该用户的临时行（不区分型号，因为用户可能在修改型号）
        target_row = 0
        temp_key = f"{submitter_id}:{model}"

        if pending_row_index > 0:
            # 前端传入了当前行号，直接使用（用户正在编辑这一行）
            target_row = pending_row_index
        else:
            # 检查是否已有该用户+型号的临时行
            with _temp_row_lock:
                if temp_key in _temp_row_tracker:
                    tracked = _temp_row_tracker[temp_key]
                    if now - tracked["timestamp"] < _TEMP_ROW_TIMEOUT:
                        target_row = tracked["row_index"]

        # 如果没有找到行，找空行
        if target_row == 0:
            target_row = get_next_empty_row(SHEET_ID, start_from=2)
            ensure_sheet_rows(target_row + 10)

        # 2. 写入临时数据（A/B/D列），触发腾讯表格公式计算
        write_resp = write_temp_row(target_row - 1, model, tonnage, expected_date)
        write_result = write_resp.json()
        if "responses" not in write_result:
            return jsonify({"success": False, "error": "写入临时数据失败"})

        # 3. 等待公式计算完成（腾讯表格公式计算有延迟，轮询读取E列）
        calculated_date = ""
        max_wait = 15  # 最多等待15秒

        # 优化：先立即检查一次，如果公式已经计算好直接返回
        e_value = read_calculated_date_from_row(target_row)
        if e_value and e_value.strip():
            if is_date_string(e_value):
                calculated_date = e_value
            elif e_value not in ["", "计算中..."]:
                calculated_date = e_value

        if not calculated_date:
            wait_interval = 0.3  # 每0.3秒检查一次
            elapsed = 0
            while elapsed < max_wait and not calculated_date:
                time.sleep(wait_interval)
                elapsed += wait_interval
                e_value = read_calculated_date_from_row(target_row)
                if e_value and e_value.strip():
                    if is_date_string(e_value):
                        calculated_date = e_value
                        break
                    elif e_value not in ["", "计算中..."]:
                        calculated_date = e_value
                        break

        # 3.5 缓存查询：如果同一参数已有结果且未过期，直接返回
        if not force_refresh:
            cache_key = f"{model}:{tonnage}:{expected_date}"
            import time as _time_module_calc
            if (_calc_result_cache.get("key") == cache_key
                and _calc_result_cache.get("result")
                and _time_module_calc.time() - _calc_result_cache.get("timestamp", 0) < _CALC_CACHE_TTL):
                # 清理临时行（之前写入但未使用的）
                try:
                    clear_temp_row(target_row)
                except:
                    pass
                return jsonify({
                    "success": True,
                    "calculated_date": _calc_result_cache["result"],
                    "row_index": 0,
                    "message": ""
                })

        # 4. 记录临时行信息
        with _temp_row_lock:
            _temp_row_tracker[temp_key] = {
                "row_index": target_row,
                "timestamp": now,
                "submitter_id": submitter_id
            }

        # 5. 更新缓存（仅在非强制刷新时）
        if not force_refresh:
            cache_key = f"{model}:{tonnage}:{expected_date}"
            _calc_result_cache["key"] = cache_key
            _calc_result_cache["result"] = calculated_date
            _calc_result_cache["timestamp"] = now

        return jsonify({
            "success": True,
            "calculated_date": calculated_date,
            "row_index": target_row,
            "message": "" if calculated_date else "公式计算超时，请重试"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def _cleanup_expired_temp_rows():
    """清理超时的临时行（5分钟未提交则清空）"""
    import time
    now = time.time()
    expired_keys = []

    with _temp_row_lock:
        for key, info in _temp_row_tracker.items():
            if now - info["timestamp"] > _TEMP_ROW_TIMEOUT:
                expired_keys.append(key)
                # 清空超时的临时行
                try:
                    clear_temp_row(info["row_index"])
                except Exception as e:
                    print(f"[cleanup] 清空临时行失败 row={info['row_index']}: {e}")

        for key in expired_keys:
            del _temp_row_tracker[key]


@app.route('/api/clear-temp-row', methods=['POST'])
def clear_temp_row_api():
    """清空指定临时行（页面刷新/关闭时调用）
    注意：sendBeacon请求不携带自定义headers，所以不强制认证
    通过row_index直接清空即可，无需验证用户身份
    """
    try:
        data = request.json or {}
        row_index = data.get('row_index', 0)

        if row_index > 0:
            # 清空该行的A/B/D列（保留E列公式）
            clear_temp_row(row_index)

            # 从临时行跟踪器中移除（不验证submitter_id，因为sendBeacon可能没有headers）
            with _temp_row_lock:
                keys_to_remove = []
                for key, info in _temp_row_tracker.items():
                    if info["row_index"] == row_index:
                        keys_to_remove.append(key)
                for key in keys_to_remove:
                    del _temp_row_tracker[key]

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/cleanup-user-temp-rows', methods=['POST'])
@require_auth
def cleanup_user_temp_rows():
    """清理当前用户所有遗留的临时行（页面加载时调用）"""
    try:
        data = request.json or {}
        submitter_id = data.get('submitter_id', '')

        if not submitter_id:
            return jsonify({"success": True, "message": "无用户ID"})

        # 找到该用户所有过期的临时行（超过5分钟）
        import time
        now = time.time()
        expired_rows = []

        with _temp_row_lock:
            keys_to_remove = []
            for key, info in _temp_row_tracker.items():
                if info["submitter_id"] == submitter_id:
                    # 检查是否超时（超过5分钟）
                    if now - info["timestamp"] > _TEMP_ROW_TIMEOUT:
                        expired_rows.append(info["row_index"])
                        keys_to_remove.append(key)

            for key in keys_to_remove:
                del _temp_row_tracker[key]

        # 清空过期的临时行
        for row_index in expired_rows:
            try:
                clear_temp_row(row_index)
            except Exception as e:
                print(f"[cleanup] 清空临时行失败 row={row_index}: {e}")

        return jsonify({"success": True, "cleared_rows": expired_rows})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/orders', methods=['POST'])
@require_auth
def create_order():
    """创建订单：完整写入记录，不覆盖E列公式
    使用calculate-date阶段已确定的行号，确保在同一行交互"""
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

        # 确定目标行
        target_row = row_index
        if target_row <= 0:
            return jsonify({"success": False, "error": "未找到目标行，请先计算可发货日期"})

        # 检查目标行是否被当前用户的临时数据占用
        temp_key = f"{submitter_id}:{model}"
        with _temp_row_lock:
            if temp_key in _temp_row_tracker:
                tracked_row = _temp_row_tracker[temp_key]["row_index"]
                if tracked_row != target_row:
                    # 如果行号不一致，使用跟踪的行号
                    target_row = tracked_row

        # 确保行数足够
        ensure_sheet_rows(target_row + 10)

        # 写入完整数据（不覆盖E列）
        write_row_idx = target_row - 1
        serial_no = str(target_row)
        resp = write_order_row(
            write_row_idx, model, tonnage, customer, expected_date,
            "", queue_date, submitter, remark, serial_no, submitter_id, submit_time
        )
        result = resp.json()

        if "responses" in result:
            updated = result["responses"][0].get("updateRangeResponse", {}).get("updatedCells", 0)
            if updated > 0:
                # 清理临时行跟踪
                with _temp_row_lock:
                    if temp_key in _temp_row_tracker:
                        del _temp_row_tracker[temp_key]
                clear_order_caches()
                return jsonify({"success": True, "message": "订单创建成功"})
            return jsonify({"success": False, "error": "写入0个单元格"})
        else:
            err_str = json.dumps(result, ensure_ascii=False)
            return jsonify({"success": False, "error": err_str})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# 全局缓存：原始订单数据（不过滤权限和日期）
_orders_cache = {"data": None, "timestamp": 0}
# 全局缓存：过滤+排序后的结果（按用户和view_mode缓存）
_filtered_cache = {"timestamp": 0}
CACHE_TTL = 120  # 缓存120秒，减少API调用频率

def clear_order_caches():
    """清空订单相关缓存，确保增删改后页面重新读取腾讯表最新数据
    注意：不清空 _sheet_row_count_cache，因为写入行不会减少表格总行数"""
    _orders_cache["data"] = None
    _orders_cache["timestamp"] = 0
    _filtered_cache.clear()
    _filtered_cache["timestamp"] = 0
    _pending_row_cache["data"] = None
    _pending_row_cache["timestamp"] = 0


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

            # 过滤已删除的行（J列=DELETED）
            if row_data[9] and row_data[9].strip().upper() == "DELETED":
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
    """按用户表权限判断是否可查看/操作订单
    安全：当 view_mode='all' 且用户信息获取失败时，默认允许查看（降级为全部可见）"""
    access_level = (current_user or {}).get("access_level", "self")
    if view_mode == "mine":
        return is_same_submitter(order, submitter_id, submitter_name)
    if access_level == "admin":
        return True
    # view_mode="all" 但用户信息不完整时（access_level="self"且无部门信息），
    # 降级为全部可见，避免全部排队显示为空
    if access_level in ("department", "self"):
        current_dept = str((current_user or {}).get("department", "")).strip()
        if not current_dept:
            # 用户信息不完整，降级为全部可见
            return True
        order_user = get_order_submitter_user(order)
        order_dept = str((order_user or {}).get("department", "")).strip()
        return bool(current_dept and order_dept and current_dept == order_dept)
    return is_same_submitter(order, submitter_id, submitter_name)


def normalize_view_mode(current_user, requested_view_mode):
    """根据权限标准化视图：管理员全部，经理本部门，业务员本部门（隐藏客户）"""
    access_level = (current_user or {}).get("access_level", "self")
    if requested_view_mode == "all":
        # 管理员、经理、业务员都可以看全部排队（业务员隐藏客户名称在前端处理）
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


@app.route('/api/debug/capacity', methods=['GET'])
@require_auth
@require_ligang_admin
def debug_capacity():
    """临时调试：查看型号的产能数据和已占用产能"""
    model = request.args.get('model', 'C305')
    from calc_engine import _get_model_config, get_sheet_data, read_sheet_range, col_letter_to_index, parse_cell_value, parse_date
    config = _get_model_config(model)
    if not config:
        return jsonify({"success": False, "error": f"型号 {model} 未找到配置"})
    sheet_id, start_row, capacity_col, limit_cell, row_count = config
    
    # 直接读取原始API响应，用于调试
    capacity_col_index = col_letter_to_index(capacity_col)
    end_row = start_row + row_count - 1
    range_str = f"A{start_row}:{capacity_col}{end_row}"
    raw_grid_data = read_sheet_range(sheet_id, range_str)
    raw_rows = raw_grid_data.get("rows", [])
    
    # 测试1: 只读取 AG:AJ 列（小范围，4列），看公式是否能计算
    small_range = f"AG{start_row}:AJ{end_row}"
    small_resp = read_sheet_range(sheet_id, small_range)
    small_rows_count = len(small_resp.get("rows", []))
    
    # 测试2: 读取 AG:AJ 列但只取 6月21日之后的行 (row 24 onwards)
    jun21_start = start_row + 20  # 6月21日大约在 row 24
    jun21_range = f"AG{jun21_start}:AJ{end_row}"
    jun21_resp = read_sheet_range(sheet_id, jun21_range)
    jun21_rows_count = len(jun21_resp.get("rows", []))
    
    # 测试3: 读取单行 AJ27 看公式值
    single_resp = read_sheet_range(sheet_id, "AJ27:AJ27")
    single_rows_count = len(single_resp.get("rows", []))
    
    # 测试4: 读取 A4:AJ188 的原始返回行数（对比）
    full_rows_count = len(raw_rows)
    
    # 解析原始数据
    raw_dates = []
    aj_values = []
    # AJ公式: =AJ_prev + AG(产量) - AI(销售) - AH(自用)
    ag_col_idx = col_letter_to_index("AG")  # 产量
    ah_col_idx = col_letter_to_index("AH")  # 自用
    ai_col_idx = col_letter_to_index("AI")  # 销售
    formula_debug = []
    for i, row in enumerate(raw_rows):
        values = row.get("values", [])
        if values:
            cv = values[0].get("cellValue")
            if cv:
                date_val = parse_cell_value(cv)
                d = parse_date(date_val)
                raw_dates.append({"row_index": i, "date": date_val, "parsed": str(d) if d else None, "values_len": len(values)})
                # 检查AJ列值
                if len(values) > capacity_col_index:
                    aj_cv = values[capacity_col_index].get("cellValue")
                    aj_val = parse_cell_value(aj_cv) if aj_cv else None
                    aj_values.append({"row_index": i, "date": date_val, "aj_value": aj_val, "aj_raw": str(aj_cv) if aj_cv else None})
                # 检查 AG/AH/AI
                if d and str(d) >= "2026-06-18" and str(d) <= "2026-06-30":
                    def get_col_val(idx):
                        if len(values) > idx:
                            c = values[idx].get("cellValue")
                            return parse_cell_value(c) if c else None
                        return None
                    formula_debug.append({
                        "date": date_val,
                        "AG(产量)": get_col_val(ag_col_idx),
                        "AH(自用)": get_col_val(ah_col_idx),
                        "AI(销售)": get_col_val(ai_col_idx),
                        "AJ(结余)": get_col_val(capacity_col_index),
                    })
    
    # 解析小范围数据 (AG:AJ) - 检查values是否为空
    small_debug = []
    small_empty_values = 0
    small_nonempty_values = 0
    small_raw_samples = []  # 原始数据样本
    for i, row in enumerate(small_resp.get("rows", [])):
        values = row.get("values", [])
        if not values:
            small_empty_values += 1
        else:
            small_nonempty_values += 1
            # 取样本：row 20-25 的原始 values
            if 20 <= i <= 25:
                vals_summary = []
                for j, v in enumerate(values[:4]):
                    cv = v.get("cellValue")
                    vals_summary.append(str(cv) if cv else "None")
                small_raw_samples.append({"row": i, "values": vals_summary})
            def get_v(idx):
                if len(values) > idx:
                    c = values[idx].get("cellValue")
                    return parse_cell_value(c) if c else None
                return None
            if i >= 18 and i <= 30:  # 6月19日~7月1日
                small_debug.append({
                    "row_in_range": i,
                    "AG": get_v(0),
                    "AH": get_v(1),
                    "AI": get_v(2),
                    "AJ": get_v(3),
                })
    
    # 解析6月21日后小范围
    jun21_debug = []
    for i, row in enumerate(jun21_resp.get("rows", [])):
        values = row.get("values", [])
        if values:
            def get_v(idx):
                if len(values) > idx:
                    c = values[idx].get("cellValue")
                    return parse_cell_value(c) if c else None
                return None
            if i < 10:
                jun21_debug.append({
                    "row_in_range": i,
                    "AG": get_v(0),
                    "AH": get_v(1),
                    "AI": get_v(2),
                    "AJ": get_v(3),
                })
    
    # 解析单行 AJ27
    single_debug = []
    for row in single_resp.get("rows", []):
        values = row.get("values", [])
        for v in values:
            cv = v.get("cellValue")
            single_debug.append(str(cv) if cv else None)
    
    sheet_data = get_sheet_data(sheet_id, start_row, capacity_col, limit_cell, row_count)
    date_capacity_map = sheet_data["date_capacity_map"]
    limit_date = sheet_data["limit_date"]

    # 获取已占用产能
    occupied = {}
    try:
        all_orders = fetch_all_orders_raw()
        for order in all_orders:
            if str(order.get("model", "")).strip() != model:
                continue
            qd = str(order.get("queue_date", "")).strip()
            if qd:
                d = parse_date(qd)
                if d:
                    t = parse_number(order.get("tonnage", "0"))
                    if t and t > 0:
                        occupied[d] = occupied.get(d, 0) + t
    except:
        pass

    # 构建结果
    dates = sorted(date_capacity_map.keys())
    result = {
        "model": model,
        "config": {"sheet_id": sheet_id, "start_row": start_row, "capacity_col": capacity_col, "limit_cell": limit_cell, "row_count": row_count, "range": range_str},
        "limit_date": str(limit_date) if limit_date else None,
        "total_dates": len(dates),
        "date_range": f"{dates[0]} ~ {dates[-1]}" if dates else "empty",
        "raw_api": {
            "requested_range": range_str,
            "returned_rows": len(raw_rows),
            "first_10_dates": raw_dates[:10],
            "last_10_dates": raw_dates[-10:] if len(raw_dates) >= 10 else raw_dates,
            "aj_around_june20": [v for v in aj_values if v["date"] in ["2026-06-19", "2026-06-20", "2026-06-21", "2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25"]],
            "all_dates_with_aj": [{"date": v["date"], "aj": v["aj_value"]} for v in aj_values],
            "wide_range_aj": [],
            "formula_debug": formula_debug,
            "small_range_AG_AJ": small_debug,
            "small_raw_samples": small_raw_samples,
            "jun21_range_AG_AJ": [],
            "single_AJ27": [],
            "range_comparison": {
                "A4_AJ188_rows": full_rows_count,
                "AG4_AJ188_rows": small_rows_count,
                "AG24_AJ188_rows": jun21_rows_count,
                "AJ27_AJ27_rows": single_rows_count,
                "AG4_AJ188_empty_values": small_empty_values,
                "AG4_AJ188_nonempty_values": small_nonempty_values,
            },
        },
        "capacity_data": [],
        "occupied_summary": {str(k): v for k, v in sorted(occupied.items())}
    }
    for d in dates:
        cap = date_capacity_map[d]
        occ = occupied.get(d, 0)
        result["capacity_data"].append({
            "date": str(d),
            "capacity": cap,
            "occupied": occ,
            "remaining": cap - occ
        })
    return jsonify({"success": True, "data": result})


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
    """后台线程：启动时预热建立初始缓存 + 定期keepalive防止Render休眠"""
    import threading
    def run():
        # 启动时等待服务完全就绪，然后尝试预热10次建立初始缓存
        time.sleep(5)
        for attempt in range(10):
            try:
                fetch_all_orders_raw()
                if _orders_cache["data"] and len(_orders_cache["data"]) > 0:
                    print(f"[warmup] 初始缓存建立成功: {len(_orders_cache['data'])}条")
                    # 同时预热pending rows缓存
                    try:
                        _get_pending_rows()
                        print(f"[warmup] pending rows缓存建立成功")
                    except:
                        pass
                    break
            except Exception as e:
                print(f"[warmup] 初始预热失败(attempt {attempt+1}): {e}")
            time.sleep(3)

        # 定期keepalive：每4分50秒访问一次自身，防止Render免费版休眠（休眠阈值约5分钟）
        import requests as req
        keepalive_url = os.environ.get('KEEPALIVE_URL', '')
        if not keepalive_url:
            # 自动检测自身URL
            try:
                r = req.get('https://q-en4c.onrender.com/', timeout=10)
                keepalive_url = 'https://q-en4c.onrender.com'
            except:
                keepalive_url = ''
        if keepalive_url:
            print(f"[keepalive] 启动定期保活: {keepalive_url}")
            while True:
                time.sleep(290)  # 4分50秒
                try:
                    req.get(keepalive_url, timeout=10)
                except:
                    pass
    t = threading.Thread(target=run, daemon=True)
    t.start()


def _warmup_capacity_cache():
    """后台线程：启动时并行预热所有型号的产能数据缓存，减少首次计算延迟"""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def run():
        time.sleep(8)  # 等待token注入完成
        from calc_engine import MODEL_CONFIG, get_sheet_data

        # 按sheet分组，同sheet的A列只读一次（共享缓存）
        sheet_groups = {}
        for model, config in MODEL_CONFIG.items():
            sheet_id = config[0]
            if sheet_id not in sheet_groups:
                sheet_groups[sheet_id] = []
            sheet_groups[sheet_id].append((model, config))

        total = len(MODEL_CONFIG)
        success = 0

        # 并行预热不同sheet的数据
        def warmup_sheet(sheet_id, models):
            nonlocal success
            for model, config in models:
                try:
                    get_sheet_data(*config)
                    success += 1
                except Exception as e:
                    print(f"[warmup-capacity] {model} 预热失败: {e}")

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for sheet_id, models in sheet_groups.items():
                futures.append(executor.submit(warmup_sheet, sheet_id, models))
            for f in as_completed(futures):
                pass  # 等待所有完成

        print(f"[warmup-capacity] 产能缓存预热完成: {success}/{total} 个型号")

        # 预热完成后立即触发一次自身keepalive，确保实例活跃
        try:
            import requests as req
            req.get('https://q-en4c.onrender.com/', timeout=10)
        except:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()


_warmup_keepalive()
_warmup_capacity_cache()

# ==================== 型号产能配置管理 API ====================

MODEL_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'model_configs.json')

def _load_model_configs():
    """加载型号配置"""
    try:
        if os.path.exists(MODEL_CONFIG_FILE):
            with open(MODEL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return {}

def _save_model_configs(configs):
    """保存型号配置"""
    with open(MODEL_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)

@app.route('/api/admin/model-configs', methods=['GET'])
@require_auth
def get_model_configs():
    """获取当前型号配置（合并代码内置 + 用户添加）"""
    try:
        current_user_id = request.args.get('submitter_id', '')
        if current_user_id != ADMIN_EMPLOYEE_ID:
            return jsonify({"success": False, "error": "无权限"})
        from calc_engine import MODEL_CONFIG
        user_configs = _load_model_configs()
        # 合并显示：代码内置 + 用户添加
        merged = {}
        for k, v in MODEL_CONFIG.items():
            merged[k] = {"sheet_id": v[0], "start_row": v[1], "capacity_col": v[2], "limit_cell": v[3], "row_count": v[4], "source": "内置"}
        for k, v in user_configs.items():
            merged[k] = {"sheet_id": v[0], "start_row": v[1], "capacity_col": v[2], "limit_cell": v[3], "row_count": v[4], "source": "用户添加"}
        return jsonify({"success": True, "configs": merged})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/admin/model-configs', methods=['POST'])
@require_auth
def save_model_config():
    """保存型号配置"""
    try:
        current_user_id = request.args.get('submitter_id', '')
        if current_user_id != ADMIN_EMPLOYEE_ID:
            return jsonify({"success": False, "error": "无权限"})
        data = request.json
        model = data.get('model', '').strip()
        sheet_id = data.get('sheet_id', '').strip()
        start_row = data.get('start_row', 0)
        capacity_col = data.get('capacity_col', '').strip().upper()
        limit_cell = data.get('limit_cell', '').strip().upper()
        row_count = data.get('row_count', 0)

        if not model or not sheet_id or not capacity_col or not limit_cell:
            return jsonify({"success": False, "error": "型号、Sheet ID、产能列、上限日期单元格不能为空"})
        try:
            start_row = int(start_row)
            row_count = int(row_count)
        except:
            return jsonify({"success": False, "error": "起始行号和行数必须是数字"})

        configs = _load_model_configs()
        configs[model] = [sheet_id, start_row, capacity_col, limit_cell, row_count]
        _save_model_configs(configs)
        return jsonify({"success": True, "message": f"型号 {model} 配置已保存"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)



