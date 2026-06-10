let currentUser = { name: '用户', id: 'auth_user' };
let allOrders = [];
let modelOptions = [];
let pendingRowIndex = 0;
const API_BASE = '';

// 从localStorage读取密码和员工ID
let accessPassword = localStorage.getItem('accessPassword') || '';
let employeeId = localStorage.getItem('employeeId') || '';

// 所有API请求自动带上密码头和员工ID头
function apiFetch(url, options = {}) {
    options.headers = options.headers || {};
    if (accessPassword) {
        options.headers['X-Access-Password'] = accessPassword;
    }
    if (employeeId) {
        options.headers['X-Employee-Id'] = employeeId;
    }
    return fetch(url, options);
}

// 页面显示时（包括bfcache恢复）强制清除表单
window.addEventListener('pageshow', function(e) {
    if (e.persisted) {
        // 从bfcache恢复，强制清除所有表单
        clearOrderForm();
    }
});

function clearOrderForm() {
    const form = document.getElementById('orderForm');
    if (form) form.reset();
    const model = document.getElementById('model');
    if (model) model.value = '';
    const tonnage = document.getElementById('tonnage');
    if (tonnage) tonnage.value = '';
    const customer = document.getElementById('customer');
    if (customer) customer.value = '';
    const calc = document.getElementById('calculatedDate');
    if (calc) calc.value = '';
    pendingRowIndex = 0;
}

document.addEventListener('DOMContentLoaded', function() {
    // 首次加载也强制清除
    clearOrderForm();
    if (accessPassword && employeeId) {
        // 有密码和员工ID，自动验证
        fetch(`${API_BASE}/auth/check`, {
            headers: { 'X-Access-Password': accessPassword, 'X-Employee-Id': employeeId }
        })
        .then(r => r.json())
        .then(data => {
            if (data.authorized) {
                hideAuthOverlay();
                initApp();
            } else {
                // 密码已变更，清除并弹出登录
                accessPassword = '';
                employeeId = '';
                localStorage.removeItem('accessPassword');
                localStorage.removeItem('employeeId');
                showAuthOverlay('密码已变更，请重新登录');
            }
        })
        .catch(() => showAuthOverlay('网络错误，请重试'));
    } else {
        showAuthOverlay();
    }
});

function showAuthOverlay(errorMsg) {
    document.getElementById('authOverlay').style.display = 'flex';
    if (errorMsg) document.getElementById('authError').textContent = errorMsg;
    loadAuthUsers();
}

function hideAuthOverlay() {
    document.getElementById('authOverlay').style.display = 'none';
}

async function loadAuthUsers() {
    try {
        const response = await fetch(`${API_BASE}/auth/users`);
        const data = await response.json();
        const select = document.getElementById('authUserSelect');
        select.innerHTML = '<option value="">请选择员工</option>';
        if (data.success && Array.isArray(data.users)) {
            data.users.forEach(user => {
                const option = document.createElement('option');
                option.value = user.employee_id;
                option.textContent = user.name + '（' + user.employee_id + '）';
                select.appendChild(option);
            });
        }
    } catch (error) {
        console.error('加载用户列表失败', error);
    }
}

function doAuth() {
    const selectedEmployeeId = document.getElementById('authUserSelect').value;
    const password = document.getElementById('authPassword').value.trim();
    if (!selectedEmployeeId) {
        document.getElementById('authError').textContent = '请选择员工';
        return;
    }
    if (!password) {
        document.getElementById('authError').textContent = '请输入密码';
        return;
    }
    fetch(`${API_BASE}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ employee_id: selectedEmployeeId, password })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            accessPassword = data.access_password || '';
            employeeId = selectedEmployeeId;
            localStorage.setItem('accessPassword', accessPassword);
            localStorage.setItem('employeeId', selectedEmployeeId);
            currentUser.name = data.user?.name || '用户';
            currentUser.id = selectedEmployeeId;
            hideAuthOverlay();
            initApp();
        } else {
            document.getElementById('authError').textContent = data.error || '密码错误';
        }
    })
    .catch(() => {
        document.getElementById('authError').textContent = '网络错误';
    });
}

// 未提交排队的临时数据（页面关闭/刷新时清除）
let draftQueue = null;
let lastActivityTime = Date.now();
const IDLE_TIMEOUT = 10 * 60 * 1000; // 10分钟无操作强制退出

function initApp() {
    document.getElementById('userName').textContent = currentUser.name;
    document.getElementById('changePwdBtn').style.display = 'inline-block';
    document.getElementById('logoutBtn').style.display = 'inline-block';
    loadModels();
    // 先清空所有字段（在绑定事件之前，避免触发计算）
    document.getElementById('model').value = '';
    document.getElementById('tonnage').value = '';
    document.getElementById('customer').value = '';
    document.getElementById('calculatedDate').value = '';
    pendingRowIndex = 0;
    // 期望发货日期默认为次日
    const tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    const tomorrowStr = tomorrow.toISOString().split('T')[0];
    document.getElementById('expectedDate').value = tomorrowStr;
    document.getElementById('queueDate').value = tomorrowStr;
    // 绑定事件监听器
    setupEventListeners();
    setupEditQueueDateListener();
    // 启动无操作检测
    startIdleTimer();
}

function initUser() {
    const urlParams = new URLSearchParams(window.location.search);
    const userName = urlParams.get('userName') || localStorage.getItem('userName') || '测试用户';
    const userId = urlParams.get('userId') || localStorage.getItem('userId') || 'test_user_001';
    currentUser.name = userName;
    currentUser.id = userId;
    document.getElementById('userName').textContent = userName;
    localStorage.setItem('userName', userName);
    localStorage.setItem('userId', userId);
}

async function loadModels() {
    try {
        const response = await apiFetch(`${API_BASE}/api/models`);
        const data = await response.json();
        if (data.success) {
            modelOptions = data.models;
            populateModelSelect('model', data.models);
            populateModelSelect('editModel', data.models);
        } else {
            showToast('加载型号列表失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('网络错误，请检查连接', 'error');
    }
}

function populateModelSelect(selectId, models) {
    const select = document.getElementById(selectId);
    select.innerHTML = '<option value="">请选择型号</option>';
    models.forEach(model => {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        select.appendChild(option);
    });
}

function setupEventListeners() {
    document.getElementById('orderForm').addEventListener('submit', handleCreateOrder);
    document.getElementById('editForm').addEventListener('submit', handleUpdateOrder);
    document.getElementById('changePwdForm').addEventListener('submit', handleChangePassword);
    // 监听表单字段变化，记录草稿
    const draftFields = ['model', 'tonnage', 'customer', 'expectedDate', 'queueDate'];
    draftFields.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.addEventListener('input', saveDraft);
            field.addEventListener('change', saveDraft);
        }
    });
    // 日期选择器：点击日期后自动关闭（通过blur实现）
    ['expectedDate', 'queueDate', 'editExpectedDate', 'editQueueDate'].forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.addEventListener('change', function() {
                this.blur();
            });
        }
    });
    // 监听用户操作，记录活动时间
    ['click', 'keydown', 'scroll', 'touchstart'].forEach(evt => {
        document.addEventListener(evt, recordActivity, { passive: true });
    });
    // 创建页面自动计算
    const calcFields = ['model', 'tonnage', 'customer', 'expectedDate'];
    calcFields.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.addEventListener('change', debounce(calculateDate, 500));
        }
    });
    // 修改页面自动计算
    const editCalcFields = ['editModel', 'editTonnage', 'editCustomer', 'editExpectedDate'];
    editCalcFields.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.addEventListener('change', debounce(calculateDateForEdit, 500));
        }
    });
}

function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// 版本号机制：确保最后一次字段变化一定会触发计算
let calcVersion = 0;
let pendingCalcs = 0; // 正在进行的计算数量

async function calculateDate() {
    calcVersion++;
    const myVersion = calcVersion;
    pendingCalcs++;
    
    const model = document.getElementById('model').value;
    const tonnage = document.getElementById('tonnage').value;
    const customer = document.getElementById('customer').value;
    const expectedDate = document.getElementById('expectedDate').value;
    if (!model || !tonnage || !customer || !expectedDate) {
        pendingCalcs--;
        return;
    }

    document.getElementById('calculatedDate').value = '计算中...';

    try {
        const response = await apiFetch(`${API_BASE}/api/calculate-date`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model, tonnage, customer, expected_date: expectedDate, pending_row_index: pendingRowIndex, submitter_id: currentUser.id })
        });
        const data = await response.json();
        
        // 如果期间有更新的计算请求，丢弃本次结果
        if (myVersion !== calcVersion) {
            pendingCalcs--;
            return;
        }
        
        if (data.success) {
            const calcDate = data.calculated_date || '';
            document.getElementById('calculatedDate').value = calcDate || '计算失败';
            pendingRowIndex = data.row_index || 0;

            // 检查E列结果是否为有效日期
            const isDate = calcDate && calcDate.match(/\d{4}-\d{2}-\d{2}/);
            const queueDateInput = document.getElementById('queueDate');
            if (!isDate && calcDate) {
                queueDateInput.style.display = 'none';
                const parent = queueDateInput.parentNode;
                const oldHint = parent.querySelector('.queue-date-hint');
                if (oldHint) oldHint.remove();
                const hint = document.createElement('input');
                hint.type = 'text';
                hint.className = 'queue-date-hint';
                hint.value = '请联系商务支持';
                hint.disabled = true;
                hint.style.cssText = 'width:100%;padding:12px 15px;border:1px solid #ddd;border-radius:8px;font-size:15px;background:#fff0f0;color:#e74c3c;font-weight:500;';
                parent.insertBefore(hint, queueDateInput.nextSibling);
            } else if (isDate) {
                queueDateInput.style.display = '';
                queueDateInput.disabled = false;
                queueDateInput.style.background = '';
                queueDateInput.style.color = '';
                queueDateInput.value = calcDate;
                const oldHint = queueDateInput.parentNode.querySelector('.queue-date-hint');
                if (oldHint) oldHint.remove();
            } else {
                queueDateInput.style.display = '';
                queueDateInput.disabled = false;
                const oldHint = queueDateInput.parentNode.querySelector('.queue-date-hint');
                if (oldHint) oldHint.remove();
            }
        } else {
            document.getElementById('calculatedDate').value = '计算失败';
            pendingRowIndex = 0;
        }
    } catch (error) {
        if (myVersion !== calcVersion) {
            pendingCalcs--;
            return;
        }
        document.getElementById('calculatedDate').value = '计算失败';
        pendingRowIndex = 0;
    }
    pendingCalcs--;
}

async function handleCreateOrder(e) {
    e.preventDefault();
    
    // 等待当前计算完成（最多等5秒）
    const startWait = Date.now();
    while (pendingCalcs > 0 && Date.now() - startWait < 5000) {
        await new Promise(r => setTimeout(r, 300));
    }
    
    const calculatedDate = document.getElementById('calculatedDate').value;
    const queueDateInput = document.getElementById('queueDate');
    
    // 确定queue_date的值
    let queueDate = '';
    const isCalcDate = calculatedDate && calculatedDate.match(/\d{4}-\d{2}-\d{2}/);
    
    if (!isCalcDate && calculatedDate && calculatedDate !== '计算中...') {
        // E列不是有效日期（如"请联系商务支持"），F列也写入相同文本
        queueDate = calculatedDate;
    } else {
        // E列是有效日期，使用F列输入框的值
        queueDate = queueDateInput.value;
    }
    
    // 校验：F列（排队日期）必须 >= E列（可发货日期）
    if (isCalcDate && queueDate) {
        const calcDateObj = new Date(calculatedDate);
        const queueDateObj = new Date(queueDate);
        if (queueDateObj < calcDateObj) {
            showToast('排队日期不能早于可发货日期（' + calculatedDate + '）', 'error');
            return;
        }
    }
    
    if (!queueDate) {
        showToast('请填写排队日期', 'error');
        return;
    }
    
    const orderData = {
        model: document.getElementById('model').value,
        tonnage: document.getElementById('tonnage').value,
        customer: document.getElementById('customer').value,
        expected_date: document.getElementById('expectedDate').value,
        queue_date: queueDate,
        submitter: currentUser.name,
        submitter_id: currentUser.id,
        row_index: pendingRowIndex // 如果有预计算行号，则更新该行
    };

    try {
        const response = await apiFetch(`${API_BASE}/api/orders`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(orderData)
        });
        const data = await response.json();
        if (data.success) {
            showToast('排队创建成功！', 'success');
            document.getElementById('orderForm').reset();
            // 重置为次日
            const tomorrow = new Date();
            tomorrow.setDate(tomorrow.getDate() + 1);
            const tomorrowStr = tomorrow.toISOString().split('T')[0];
            document.getElementById('expectedDate').value = tomorrowStr;
            document.getElementById('queueDate').value = tomorrowStr;
            document.getElementById('calculatedDate').value = '';
            pendingRowIndex = 0; // 清空
            draftQueue = null; // 清除草稿
        } else {
            showToast('排队创建失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

async function loadOrders() {
    const ordersList = document.getElementById('ordersList');
    ordersList.innerHTML = '<div class="loading">加载中...</div>';

    try {
        const response = await apiFetch(`${API_BASE}/api/orders?submitter_id=${currentUser.id}`);
        const data = await response.json();
        if (data.success) {
            allOrders = data.orders;
            renderOrders(allOrders);
            populateFilterModelSelect();
        } else {
            ordersList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>加载失败: ' + data.error + '</p></div>';
        }
    } catch (error) {
        ordersList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>网络错误，请检查连接</p></div>';
    }
}

function populateFilterModelSelect() {
    const select = document.getElementById('filterModel');
    const currentVal = select.value;
    // 收集所有唯一型号
    const models = [...new Set(allOrders.map(o => o.model).filter(Boolean))].sort();
    select.innerHTML = '<option value="">全部型号</option>';
    models.forEach(model => {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        select.appendChild(option);
    });
    select.value = currentVal;
}

function renderOrders(orders) {
    const ordersList = document.getElementById('ordersList');
    if (orders.length === 0) {
        ordersList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>暂无排队</p></div>';
        return;
    }

    let html = `<table class="order-table">
        <thead>
            <tr>
                <th>型号</th>
                <th>吨位</th>
                <th>客户</th>
                <th>排队日期</th>
                <th>操作</th>
            </tr>
        </thead>
        <tbody>`;

    orders.forEach(order => {
        // 排队日期只显示月日
        let queueDateDisplay = escapeHtml(order.queue_date);
        if (queueDateDisplay && queueDateDisplay.match(/^\d{4}-\d{2}-\d{2}$/)) {
            queueDateDisplay = queueDateDisplay.substring(5); // 取 MM-DD
        }
        html += `<tr>
            <td class="td-model">${escapeHtml(order.model)}</td>
            <td>${escapeHtml(order.tonnage)}</td>
            <td>${escapeHtml(order.customer)}</td>
            <td>${queueDateDisplay}</td>
            <td class="td-actions">
                <button class="btn-edit" onclick="openEditModal(${order.row_index})">改</button>
                <button class="btn-delete" onclick="deleteOrder(${order.row_index})">删</button>
            </td>
        </tr>`;
    });

    html += '</tbody></table>';
    ordersList.innerHTML = html;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function filterOrders() {
    const keyword = document.getElementById('filterInput').value.toLowerCase();
    const modelFilter = document.getElementById('filterModel').value;
    const customerFilter = document.getElementById('filterCustomer').value.toLowerCase();

    let filtered = allOrders;

    if (keyword) {
        filtered = filtered.filter(order =>
            (order.model && order.model.toLowerCase().includes(keyword)) ||
            (order.customer && order.customer.toLowerCase().includes(keyword)) ||
            (order.tonnage && order.tonnage.toString().includes(keyword))
        );
    }

    if (modelFilter) {
        filtered = filtered.filter(order => order.model === modelFilter);
    }

    if (customerFilter) {
        filtered = filtered.filter(order =>
            order.customer && order.customer.toLowerCase().includes(customerFilter)
        );
    }

    renderOrders(filtered);
}

function sortOrders() {
    const sortType = document.getElementById('sortSelect').value;
    if (!sortType) {
        renderOrders(allOrders);
        return;
    }
    const sorted = [...allOrders].sort((a, b) => {
        switch(sortType) {
            case 'model': return (a.model || '').localeCompare(b.model || '');
            case 'queueDate': return new Date(a.queue_date || 0) - new Date(b.queue_date || 0);
            case 'tonnage': return parseFloat(a.tonnage || 0) - parseFloat(b.tonnage || 0);
            default: return 0;
        }
    });
    renderOrders(sorted);
}

async function openEditModal(rowIndex) {
    try {
        const response = await apiFetch(`${API_BASE}/api/orders?submitter_id=${currentUser.id}`);
        const data = await response.json();
        if (data.success) {
            const order = data.orders.find(o => o.row_index === rowIndex);
            if (order) {
                document.getElementById('editRowIndex').value = rowIndex;
                document.getElementById('editModel').value = order.model || '';
                document.getElementById('editTonnage').value = order.tonnage || '';
                document.getElementById('editCustomer').value = order.customer || '';
                document.getElementById('editExpectedDate').value = order.expected_date || '';
                document.getElementById('editCalculatedDate').value = order.calculated_date || '';
                document.getElementById('editQueueDate').value = order.queue_date || '';
                // 清除提示
                document.getElementById('editDateHint').textContent = '';
                document.getElementById('editDateHint').style.color = '';
                document.getElementById('editModal').classList.add('show');
            }
        }
    } catch (error) {
        showToast('加载失败', 'error');
    }
}

function closeEditModal() {
    document.getElementById('editModal').classList.remove('show');
}

function openChangePwdModal() {
    document.getElementById('changePwdForm').reset();
    document.getElementById('changePwdModal').classList.add('show');
}

function closeChangePwdModal() {
    document.getElementById('changePwdModal').classList.remove('show');
}

async function handleChangePassword(e) {
    e.preventDefault();
    const oldPassword = document.getElementById('oldPassword').value;
    const newPassword = document.getElementById('newPassword').value;
    const confirmPassword = document.getElementById('confirmPassword').value;

    if (!oldPassword || !newPassword || !confirmPassword) {
        showToast('请填写所有密码字段', 'error');
        return;
    }
    if (newPassword !== confirmPassword) {
        showToast('两次输入的新密码不一致', 'error');
        return;
    }
    if (newPassword.length < 6) {
        showToast('新密码至少6位', 'error');
        return;
    }
    if (!/[a-zA-Z]/.test(newPassword) || !/[0-9]/.test(newPassword)) {
        showToast('密码必须同时包含字母和数字', 'error');
        return;
    }

    try {
        const response = await apiFetch(`${API_BASE}/api/users/password`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_password: oldPassword, new_password: newPassword })
        });
        const data = await response.json();
        if (data.success) {
            showToast('密码修改成功，请重新登录', 'success');
            closeChangePwdModal();
            // 清除登录状态并重新登录
            accessPassword = '';
            employeeId = '';
            localStorage.removeItem('accessPassword');
            localStorage.removeItem('employeeId');
            showAuthOverlay('密码已修改，请重新登录');
        } else {
            showToast(data.error || '密码修改失败', 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

function doLogout() {
    accessPassword = '';
    employeeId = '';
    localStorage.removeItem('accessPassword');
    localStorage.removeItem('employeeId');
    currentUser = { name: '用户', id: '' };
    document.getElementById('changePwdBtn').style.display = 'none';
    document.getElementById('logoutBtn').style.display = 'none';
    document.getElementById('userName').textContent = '未登录';
    showAuthOverlay();
}

async function calculateDateForEdit() {
    const model = document.getElementById('editModel').value;
    const tonnage = document.getElementById('editTonnage').value;
    const customer = document.getElementById('editCustomer').value;
    const expectedDate = document.getElementById('editExpectedDate').value;
    if (!model || !tonnage || !customer || !expectedDate) return;

    document.getElementById('editCalculatedDate').value = '计算中...';

    const rowIndex = parseInt(document.getElementById('editRowIndex').value) || 0;

    try {
        const response = await apiFetch(`${API_BASE}/api/calculate-date`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model, tonnage, customer, expected_date: expectedDate, pending_row_index: rowIndex })
        });
        const data = await response.json();
        if (data.success) {
            const calcDate = data.calculated_date || '';
            document.getElementById('editCalculatedDate').value = calcDate || '计算失败';

            const isDate = calcDate && calcDate.match(/\d{4}-\d{2}-\d{2}/);
            if (isDate) {
                document.getElementById('editQueueDate').value = calcDate;
                document.getElementById('editDateHint').textContent = '';
            }
        } else {
            document.getElementById('editCalculatedDate').value = '计算失败';
        }
    } catch (error) {
        document.getElementById('editCalculatedDate').value = '计算失败';
    }
}

// 监听修改弹窗中排队日期变更，如果改早了提示重新计算
function setupEditQueueDateListener() {
    const editQueueDate = document.getElementById('editQueueDate');
    if (editQueueDate) {
        editQueueDate.addEventListener('change', function() {
            const calcDate = document.getElementById('editCalculatedDate').value;
            const queueDate = this.value;
            const hint = document.getElementById('editDateHint');
            if (calcDate && calcDate.match(/\d{4}-\d{2}-\d{2}/) && queueDate) {
                if (new Date(queueDate) < new Date(calcDate)) {
                    hint.textContent = '排队日期不能早于可发货日期';
                    hint.style.color = '#e74c3c';
                } else {
                    hint.textContent = '';
                }
            } else {
                hint.textContent = '';
            }
        });
    }
}

async function handleUpdateOrder(e) {
    e.preventDefault();
    const rowIndex = document.getElementById('editRowIndex').value;
    const queueDate = document.getElementById('editQueueDate').value;
    const calcDate = document.getElementById('editCalculatedDate').value;

    // 校验：排队日期不能早于可发货日期
    if (calcDate && calcDate.match(/\d{4}-\d{2}-\d{2}/) && queueDate) {
        if (new Date(queueDate) < new Date(calcDate)) {
            showToast('排队日期不能早于可发货日期', 'error');
            return;
        }
    }

    const orderData = {
        model: document.getElementById('editModel').value,
        tonnage: document.getElementById('editTonnage').value,
        customer: document.getElementById('editCustomer').value,
        expected_date: document.getElementById('editExpectedDate').value,
        queue_date: queueDate,
        submitter: currentUser.name,
        submitter_id: currentUser.id
    };

    try {
        const response = await apiFetch(`${API_BASE}/api/orders/${rowIndex}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(orderData)
        });
        const data = await response.json();
        if (data.success) {
            showToast('排队修改成功！', 'success');
            closeEditModal();
            loadOrders();
        } else {
            showToast('排队修改失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

async function deleteOrder(rowIndex) {
    if (!confirm('确定要删除这个排队吗？')) return;
    try {
        const response = await apiFetch(`${API_BASE}/api/orders/${rowIndex}`, { method: 'DELETE' });
        const data = await response.json();
        if (data.success) {
            showToast('排队删除成功！', 'success');
            loadOrders();
        } else {
            showToast('排队删除失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

function showTab(tabName) {
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    event.target.classList.add('active');
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
    document.getElementById(tabName + 'Tab').classList.add('active');
    if (tabName === 'list') loadOrders();
}

function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = 'toast show ' + type;
    setTimeout(() => toast.classList.remove('show'), 3000);
}

window.onclick = function(event) {
    const modal = document.getElementById('editModal');
    if (event.target === modal) closeEditModal();
    const changePwdModal = document.getElementById('changePwdModal');
    if (event.target === changePwdModal) closeChangePwdModal();
}

// ============ 草稿管理：未提交排队退出页面时清除 ============

function saveDraft() {
    draftQueue = {
        model: document.getElementById('model').value,
        tonnage: document.getElementById('tonnage').value,
        customer: document.getElementById('customer').value,
        expectedDate: document.getElementById('expectedDate').value,
        queueDate: document.getElementById('queueDate').value,
        calculatedDate: document.getElementById('calculatedDate').value,
        pendingRowIndex: pendingRowIndex
    };
}

function restoreDraft() {
    // 页面加载时不恢复草稿（刷新/重新进入 = 清除）
    // 只在页面内切换标签时保留
    draftQueue = null;
}

function hasUnsavedOrder() {
    const model = document.getElementById('model').value;
    const tonnage = document.getElementById('tonnage').value;
    const customer = document.getElementById('customer').value;
    return model || tonnage || customer;
}

// 页面关闭/刷新前，如果有未提交的排队，清除表单
window.addEventListener('beforeunload', function(e) {
    if (hasUnsavedOrder()) {
        // 清除表单数据，不保存
        document.getElementById('orderForm').reset();
    }
});

// ============ 空闲检测：5分钟无操作强制退出 ============

function recordActivity() {
    lastActivityTime = Date.now();
}

function startIdleTimer() {
    setInterval(() => {
        const idleTime = Date.now() - lastActivityTime;
        if (idleTime >= IDLE_TIMEOUT) {
            // 强制退出：清除密码并要求重新登录
            accessPassword = '';
            employeeId = '';
            localStorage.removeItem('accessPassword');
            localStorage.removeItem('employeeId');
            // 如果有未提交的排队，清除
            if (hasUnsavedOrder()) {
                document.getElementById('orderForm').reset();
            }
            showAuthOverlay('长时间未操作，请重新登录');
        }
    }, 30000); // 每30秒检查一次
}
