from flask import Flask, render_template, request, jsonify, abort
from flask_cors import CORS
import requests
import json
import os
from datetime import datetime
import functools

app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app)

# 腾讯表格配置
FILE_ID = "DRkR6aXhGcWxLYVFR"
SHEET_ID = "000007"       # 自助排队表格
MODEL_SHEET_ID = "000008"  # 牌号表格

# 用户表配置
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
    """读取用户表（A2:C30），返回 [{name, employee_id, password}, ...]"""
    url = f"{BASE_URL}/files/{USER_FILE_ID}/{USER_SHEET_ID}/A2:C30"
    resp = requests.get(url, headers=get_headers(), timeout=30)
    users = []
    if resp.status_code == 200:
        data = resp.json()
        rows = data.get("gridData", {}).get("rows", [])
        for row in rows:
            values = row.get("values", [])
            row_data = [parse_cell_value(v.get("cellValue")) for v in values]
            if len(row_data) >= 3 and row_data[0] and row_data[1]:
                users.append({
                    "name": row_data[0],
                    "employee_id": row_data[1],
                    "password": row_data[2]
                })
    return users


def parse_cell_value(cell_value):
    """解析单元格值，统一返回字符串"""
    if not cell_value:
        return ""
    if "text" in cell_value:
        return cell_value["text"]
    if "number" in cell_value:
        return str(cell_value["number"])
    if "time" in cell_value:
        t = cell_value["time"]
        return f"{t['year']}-{t['month']:02d}-{t['day']:02d}"
    if "select" in cell_value:
        vals = cell_value["select"].get("value", [])
        return vals[0] if vals else ""
    if "link" in cell_value:
        return cell_value["link"].get("text", cell_value["link"].get("url", ""))
    return ""


def build_cell_value(value, is_date=False, is_number=False):
    """构建单元格写入值"""
    if not value or str(value).strip() == "":
        return {"cellValue": {"text": ""}}
    if is_number:
        try:
            return {"cellValue": {"number": float(value)}}
        except (ValueError, TypeError):
            pass
    if is_date:
        try:
            parts = str(value).split("-")
            if len(parts) == 3 and len(parts[0]) == 4:
                return {"cellValue": {"time": {
                    "year": int(parts[0]), "month": int(parts[1]), "day": int(parts[2])
                }}}
        except:
            pass
    return {"cellValue": {"text": str(value)}}


def read_sheet_range(sheet_id, range_str):
    """读取表格范围数据，返回gridData"""
    url = f"{BASE_URL}/files/{FILE_ID}/{sheet_id}/{range_str}"
    resp = requests.get(url, headers=get_headers(), timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("gridData", {})
    return {}


def get_next_empty_row(sheet_id):
    """获取表格下一个空行号（1-based），从第3行开始扫描（跳过表头第1-2行）"""
    # 分批读取，每批50行
    batch_size = 50
    for offset in range(0, 200, batch_size):
        start = offset + 1  # 1-based
        end = offset + batch_size
        range_str = f"A{start}:A{end}"
        grid_data = read_sheet_range(sheet_id, range_str)
        rows = grid_data.get("rows", [])
        
        for i in range(len(rows)):
            row = rows[i]
            actual_row = start + i  # 1-based实际行号
            if actual_row < 3:
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
    
    return 201  # 如果前200行都满了


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


def write_order_row(row_index_0based, model, tonnage, customer, expected_date, queue_date, submitter, remark, serial_no, submitter_id, submit_time):
    """写入一行订单数据到腾讯表格（row_index_0based从0开始）
    注意：E列（可发货日期）有公式保护，完全不写入，避免覆盖公式结果
    """
    # A-D列 (0-3)
    values_left = [
        build_cell_value(model),                        # A: 型号
        build_cell_value(tonnage, is_number=True),      # B: 吨位（数值）
        build_cell_value(customer),                      # C: 客户
        build_cell_value(expected_date, is_date=True),  # D: 期望发货日期
    ]
    # F-L列 (5-11) - 跳过E列
    # F列智能判断：如果是日期格式用日期写入，否则用文本写入
    queue_date_is_date = is_date_string(queue_date)
    values_right = [
        build_cell_value(queue_date, is_date=queue_date_is_date),  # F: 输入发货日期排队
        build_cell_value(submitter),       # G: 提交人
        build_cell_value(remark),          # H: 备注
        build_cell_value(serial_no),       # I: 序号
        build_cell_value(""),              # J: 上次录入
        build_cell_value(submitter_id),    # K: 提交人ID
        build_cell_value(submit_time),     # L: 提交时间
    ]

    # 分两次写入：先写A-D，再写F-L
    body = {
        "requests": [
            {
                "updateRangeRequest": {
                    "sheetId": SHEET_ID,
                    "gridData": {
                        "startRow": row_index_0based,
                        "startColumn": 0,
                        "rows": [{"values": values_left}]
                    }
                }
            },
            {
                "updateRangeRequest": {
                    "sheetId": SHEET_ID,
                    "gridData": {
                        "startRow": row_index_0based,
                        "startColumn": 5,
                        "rows": [{"values": values_right}]
                    }
                }
            }
        ]
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


@app.route('/api/calculate-date', methods=['POST'])
@require_auth
def calculate_date():
    """计算可发货日期：先检查是否有匹配的待提交行，有则复用，无则写入新行"""
    try:
        data = request.json
        model = data.get('model', '')
        tonnage = data.get('tonnage', '')
        customer = data.get('customer', '')
        expected_date = data.get('expected_date', '')
        pending_row_index = data.get('pending_row_index', 0)  # 前端已有的待提交行号

        # 1. 优先复用前端传来的行号（pending_row_index 是前端当前正在编辑的行）
        existing_row = pending_row_index if pending_row_index > 0 else 0

        # 2. 如果前端没有传来行号，再按型号查找未提交的待处理行
        if existing_row == 0:
            batch_size = 50
            for offset in range(0, 200, batch_size):
                start = offset + 1
                end = offset + batch_size
                range_str = f"A{start}:F{end}"
                grid_data = read_sheet_range(SHEET_ID, range_str)
                rows = grid_data.get("rows", [])

                for i in range(len(rows)):
                    row = rows[i]
                    actual_row = start + i  # 1-based
                    if actual_row < 3:
                        continue  # 跳过表头
                    values = row.get("values", [])
                    row_data = [parse_cell_value(v.get("cellValue")) for v in values]

                    # 检查A列型号匹配
                    a_val = row_data[0] if len(row_data) > 0 else ""
                    f_val = row_data[5] if len(row_data) > 5 else ""  # F列排队日期

                    if a_val == model and not f_val.strip():
                        existing_row = actual_row
                        break

                if existing_row > 0:
                    break
                if len(rows) < batch_size:
                    break

        if existing_row > 0:
            # 复用已有行：更新A-D列数据（跳过E列）
            write_row_idx = existing_row - 1
            remark = f"{tonnage}{customer}"
            # 只更新A-D列
            body = {
                "requests": [{
                    "updateRangeRequest": {
                        "sheetId": SHEET_ID,
                        "gridData": {
                            "startRow": write_row_idx,
                            "startColumn": 0,
                            "rows": [{"values": [
                                build_cell_value(model),
                                build_cell_value(tonnage, is_number=True),
                                build_cell_value(customer),
                                build_cell_value(expected_date, is_date=True),
                            ]}]
                        }
                    }
                }]
            }
            resp = batch_update(body)
            target_row = existing_row
        else:
            # 新建行
            empty_row = get_next_empty_row(SHEET_ID)
            write_row_idx = empty_row - 1
            serial_no = write_row_idx
            remark = f"{tonnage}{customer}"
            resp = write_order_row(
                write_row_idx, model, tonnage, customer, expected_date,
                "", "", remark, str(serial_no), "", ""
            )
            target_row = empty_row

        result = resp.json()

        if "responses" not in result:
            return jsonify({"success": False, "error": f"写入数据失败: {json.dumps(result, ensure_ascii=False)}"})

        # 读取E列计算结果（尝试多种方式）
        import time
        time.sleep(2)
        
        calculated_date = ""
        
        # 方式1：直接读取E列
        e_data = read_sheet_range(SHEET_ID, f"E{target_row}:E{target_row}")
        e_rows = e_data.get("rows", [])
        if e_rows:
            for v in e_rows[0].get("values", []):
                cv = v.get("cellValue")
                if cv:
                    calculated_date = parse_cell_value(cv)
        
        # 方式2：如果直接读取为空，尝试读取整行
        if not calculated_date:
            full_data = read_sheet_range(SHEET_ID, f"A{target_row}:L{target_row}")
            full_rows = full_data.get("rows", [])
            if full_rows:
                values = full_rows[0].get("values", [])
                if len(values) > 4:
                    cv = values[4].get("cellValue")
                    if cv:
                        calculated_date = parse_cell_value(cv)
        
        # 方式3：如果还是为空，再等2秒重试一次
        if not calculated_date:
            time.sleep(2)
            e_data2 = read_sheet_range(SHEET_ID, f"E{target_row}:E{target_row}")
            e_rows2 = e_data2.get("rows", [])
            if e_rows2:
                for v in e_rows2[0].get("values", []):
                    cv = v.get("cellValue")
                    if cv:
                        calculated_date = parse_cell_value(cv)

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
    """创建订单：如果有row_index则更新已有行，否则新建行"""
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
            # 更新已有行（由calculate_date创建的行）
            write_row_idx = row_index - 1  # 转为0-based
            # 读取原序号，保持不变
            grid_data = read_sheet_range(SHEET_ID, f"I{row_index}:I{row_index}")
            rows = grid_data.get("rows", [])
            serial_no = str(write_row_idx)
            if rows:
                for v in rows[0].get("values", []):
                    cv = v.get("cellValue")
                    if cv:
                        serial_no = parse_cell_value(cv) or str(write_row_idx)
        else:
            # 新建行：找到第一个空行
            empty_row = get_next_empty_row(SHEET_ID)
            write_row_idx = empty_row - 1
            serial_no = str(write_row_idx)

        resp = write_order_row(
            write_row_idx, model, tonnage, customer, expected_date,
            queue_date, submitter, remark, serial_no, submitter_id, submit_time
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


@app.route('/api/orders', methods=['GET'])
@require_auth
def get_orders():
    """获取订单列表"""
    try:
        submitter_id = request.args.get('submitter_id', '')

        # 分批读取表格数据，每批50行，避免RangeSize过大导致400001错误
        all_rows = []
        batch_size = 50
        for offset in range(0, 200, batch_size):
            start = offset + 1
            end = offset + batch_size
            range_str = f"A{start}:L{end}"
            grid_data = read_sheet_range(SHEET_ID, range_str)
            rows = grid_data.get("rows", [])
            all_rows.extend(rows)
            # 如果返回行数少于batch_size，说明已到末尾
            if len(rows) < batch_size:
                break

        orders = []
        today = datetime.now().date()

        for i, row in enumerate(all_rows):
            if i == 0:
                continue  # 跳过表头

            values = row.get("values", [])
            if not values:
                continue

            # 解析各列（按索引取值，空列可能不存在）
            def get_col(idx):
                if idx < len(values):
                    cv = values[idx].get("cellValue")
                    if cv:
                        return parse_cell_value(cv)
                return ""

            row_data = [get_col(j) for j in range(12)]

            # 至少A列有数据才显示
            if not row_data[0]:
                continue

            # 检查权限（只过滤有submitter_id的行，空行不过滤）
            row_submitter_id = row_data[10]
            if submitter_id and row_submitter_id and row_submitter_id != submitter_id:
                continue

            # 检查排队日期是否过期
            queue_date_str = row_data[5]
            if queue_date_str:
                try:
                    queue_date = datetime.strptime(queue_date_str, "%Y-%m-%d").date()
                    if queue_date < today:
                        continue
                except:
                    pass  # 非日期格式（如"请联系商务支持"）不检查过期

            order = {
                "row_index": i + 1,  # 1-based
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
            orders.append(order)

        return jsonify({"success": True, "orders": orders})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/orders/<int:row_index>', methods=['PUT'])
@require_auth
def update_order(row_index):
    """修改订单"""
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

        # 读取原订单检查权限和吨位
        grid_data = read_sheet_range(SHEET_ID, f"A{row_index}:L{row_index}")
        rows = grid_data.get("rows", [])
        if rows:
            orig_values = [parse_cell_value(v.get("cellValue")) for v in rows[0].get("values", [])]
            original_tonnage = orig_values[1] if len(orig_values) > 1 else "0"
            row_submitter_id = orig_values[10] if len(orig_values) > 10 else ""
            # 权限检查：只能操作自己的数据
            if row_submitter_id and row_submitter_id != submitter_id:
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
        resp = write_order_row(
            write_idx, model, tonnage, customer, expected_date,
            queue_date, submitter, remark, str(write_idx), submitter_id,
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
    """删除订单（row_index是1-based）"""
    try:
        submitter_id = request.args.get('submitter_id', '')

        # 读取原订单检查权限
        grid_data = read_sheet_range(SHEET_ID, f"A{row_index}:L{row_index}")
        rows = grid_data.get("rows", [])
        if rows:
            orig_values = [parse_cell_value(v.get("cellValue")) for v in rows[0].get("values", [])]
            row_submitter_id = orig_values[10] if len(orig_values) > 10 else ""
            # 权限检查：只能操作自己的数据
            if row_submitter_id and row_submitter_id != submitter_id:
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
