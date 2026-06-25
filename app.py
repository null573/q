"""
销售排队系统 - 优化版
优化点：
1. 后台定时预抓取腾讯表格产能数据到内存缓存
2. 计算可发货日期时直接读内存，无需等待API
3. 添加 /api/capacity-status 端点查看缓存状态
"""

from flask import Flask, request, jsonify, render_template
from functools import wraps
from datetime import datetime, timezone, timedelta
import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# ========== 管理员配置 ==========
ADMIN_EMPLOYEE_ID = '20150465'
ADMIN_KEYS = ["TENCENT_ACCESS_TOKEN", "RENDER_API_KEY", "GITHUB_TOKEN"]
RENDER_SERVICE_ID = "srv-cq9d3d2j1k6c7396s8q0"
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ========== 腾讯表格配置 ==========
BASE_URL = "https://docs.qq.com/openapi/spreadsheet/v3"
FILE_ID = "DRmxUY0RBQVJXRXpC"
SHEET_ID = "000002"
MODEL_SHEET_ID = "fkayvi"
USER_FILE_ID = "DRmxUY0RBQVJXRXpC"
USER_SHEET_ID = "s9osf8"

# ========== 导入计算引擎 ==========
from calc_engine import (
    calculate_delivery_date, get_sheet_data, read_sheet_range,
    parse_cell_value, parse_date, parse_number, col_letter_to_index,
    MODEL_CONFIG, _get_model_config, _preload_all_models,
    _get_preloaded_data, _set_preload_cache, _get_from_memory,
    _set_memory_cache, start_preload_thread, stop_preload_thread,
    _preload_cache, _preload_cache_lock, _memory_cache, _memory_cache_lock,
    PRELOAD_INTERVAL, set_token_getter
)

# ========== 管理员密钥管理（内存缓存） ==========
_admin_secrets = {}
_admin_secrets_lock = threading.RLock()


def get_admin_secret(key):
    with _admin_secrets_lock:
        if key in _admin_secrets:
            return _admin_secrets[key]
    val = os.environ.get(key, "")
    return val


def set_admin_secret(key, value):
    with _admin_secrets_lock:
        _admin_secrets[key] = value


def mask_secret(value):
    if not value or len(value) < 8:
        return "***"
    return value[:4] + "****" + value[-4:]


# ========== Token 获取函数（注入到 calc_engine） ==========
def _get_current_token():
    token = get_admin_secret("TENCENT_ACCESS_TOKEN")
    if token:
        return token
    return os.environ.get("TENCENT_ACCESS_TOKEN", os.environ.get("ACCESS_TOKEN", ""))


set_token_getter(_get_current_token)


# ========== 认证装饰器 ==========
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        password = request.headers.get('X-Access-Password', '')
        employee_id = request.headers.get('X-Employee-Id', '')
        if not password or not employee_id:
            return jsonify({"success": False, "error": "未授权"}), 401
        return f(*args, **kwargs)
    return decorated


def require_ligang_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        employee_id = request.headers.get('X-Employee-Id', '')
        if str(employee_id) != ADMIN_EMPLOYEE_ID:
            return jsonify({"success": False, "error": "无权限"}), 403
        return f(*args, **kwargs)
    return decorated


def is_user_admin(employee_id):
    return str(employee_id) == ADMIN_EMPLOYEE_ID


# ========== 订单数据缓存 ==========
_orders_cache = {"data": None, "ts": 0, "lock": threading.RLock()}
_ORDERS_CACHE_TTL = 30  # 30秒


def fetch_all_orders_raw():
    """从腾讯表格读取所有订单数据"""
    all_rows = []
    batch_size = 50
    for offset in range(0, 200, batch_size):
        start = offset + 1
        end = offset + batch_size
        range_str = f"A{start}:L{end}"
        grid_data = read_sheet_range(SHEET_ID, range_str)
        rows = grid_data.get("rows", [])
        all_rows.extend(rows)
        if len(rows) < batch_size:
            break
    return all_rows


def get_all_orders():
    """获取所有订单（带缓存）"""
    with _orders_cache["lock"]:
        if _orders_cache["data"] is not None and time.time() - _orders_cache["ts"] < _ORDERS_CACHE_TTL:
            return _orders_cache["data"]

    all_rows = fetch_all_orders_raw()
    orders = []
    today = datetime.now().date()

    for i, row in enumerate(all_rows):
        if i == 0:
            continue
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

        queue_date_str = row_data[5]
        if queue_date_str:
            try:
                queue_date = datetime.strptime(queue_date_str, "%Y-%m-%d").date()
                if queue_date < today:
                    continue
            except:
                pass

        orders.append({
            "row_index": i + 1,
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

    with _orders_cache["lock"]:
        _orders_cache["data"] = orders
        _orders_cache["ts"] = time.time()

    return orders


# ========== API 路由 ==========

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/models', methods=['GET'])
@require_auth
def get_models():
    """获取型号列表"""
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


@app.route('/api/calculate-date', methods=['POST'])
@require_auth
def calculate_date():
    """计算可发货日期 - 优化版：优先从内存缓存读取"""
    try:
        data = request.json
        model = data.get('model', '')
        tonnage = data.get('tonnage', '')
        expected_date = data.get('expected_date', '')
        pending_row_index = data.get('pending_row_index', 0)

        # 核心优化：计算引擎优先从预加载缓存读取产能数据
        start_time = time.time()
        calculated_date, error_msg = calculate_delivery_date(model, tonnage, expected_date)
        calc_time = round((time.time() - start_time) * 1000, 2)

        # 查找待处理行
        target_row = 0
        if pending_row_index > 0:
            target_row = pending_row_index
        else:
            batch_size = 50
            for offset in range(0, 200, batch_size):
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
                    if a_val == model and not f_val.strip():
                        target_row = actual_row
                        break

                if target_row > 0:
                    break
                if len(rows) < batch_size:
                    break

        return jsonify({
            "success": True,
            "calculated_date": calculated_date,
            "row_index": target_row,
            "calc_time_ms": calc_time,
            "source": "preloaded_cache" if _get_preloaded_data(f"{_get_model_config(model)[0]}:{_get_model_config(model)[1]}:{_get_model_config(model)[2]}:{_get_model_config(model)[3]}") is not None else "memory_cache" if _get_from_memory(f"{_get_model_config(model)[0]}:{_get_model_config(model)[1]}:{_get_model_config(model)[2]}:{_get_model_config(model)[3]}") is not None else "api"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/capacity-status', methods=['GET'])
@require_auth
def capacity_status():
    """查看产能缓存状态（调试用）"""
    try:
        current_user_id = request.args.get('submitter_id', '')
        if current_user_id != ADMIN_EMPLOYEE_ID:
            return jsonify({"success": False, "error": "无权限"})

        status = {
            "preloaded_cache": {},
            "memory_cache": {},
            "preload_interval_seconds": PRELOAD_INTERVAL,
            "model_count": len(MODEL_CONFIG)
        }

        now = time.time()

        # 预加载缓存状态
        with _preload_cache_lock:
            for key, entry in _preload_cache.items():
                age = round(now - entry["ts"], 1)
                data = entry["data"]
                status["preloaded_cache"][key] = {
                    "age_seconds": age,
                    "date_count": len(data.get("date_capacity_map", {})),
                    "limit_date": str(data.get("limit_date")) if data.get("limit_date") else None
                }

        # 内存缓存状态
        with _memory_cache_lock:
            for key, entry in _memory_cache.items():
                age = round(now - entry["ts"], 1)
                status["memory_cache"][key] = {
                    "age_seconds": age,
                    "has_data": entry["data"] is not None
                }

        return jsonify({"success": True, "status": status})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/trigger-preload', methods=['POST'])
@require_auth
def trigger_preload():
    """手动触发预抓取（调试用）"""
    try:
        current_user_id = request.args.get('submitter_id', '')
        if current_user_id != ADMIN_EMPLOYEE_ID:
            return jsonify({"success": False, "error": "无权限"})

        start_time = time.time()
        _preload_all_models()
        elapsed = round(time.time() - start_time, 2)

        return jsonify({
            "success": True,
            "message": f"预抓取完成，耗时 {elapsed} 秒",
            "model_count": len(MODEL_CONFIG)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ========== 启动时初始化 ==========
@app.before_request
def init_preload():
    """确保预加载线程已启动"""
    start_preload_thread()


if __name__ == '__main__':
    # 启动预加载线程
    start_preload_thread()
    app.run(debug=True, host='0.0.0.0', port=5000)
