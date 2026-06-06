let currentUser = { name: '测试用户', id: 'test_user_001' };
let allOrders = [];
let modelOptions = [];
let pendingRowIndex = 0; // 由calculate_date返回的row_index
const API_BASE = '';

document.addEventListener('DOMContentLoaded', function() {
    initUser();
    loadModels();
    setupEventListeners();
    const today = new Date().toISOString().split('T')[0];
    document.getElementById('expectedDate').value = today;
    document.getElementById('queueDate').value = today;
});

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
        const response = await fetch(`${API_BASE}/api/models`);
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
    const calcFields = ['model', 'tonnage', 'customer', 'expectedDate'];
    calcFields.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.addEventListener('change', debounce(calculateDate, 500));
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

async function calculateDate() {
    const model = document.getElementById('model').value;
    const tonnage = document.getElementById('tonnage').value;
    const customer = document.getElementById('customer').value;
    const expectedDate = document.getElementById('expectedDate').value;
    if (!model || !tonnage || !customer || !expectedDate) return;

    const calcBtn = document.getElementById('calcBtn');
    calcBtn.textContent = '计算中...';
    calcBtn.disabled = true;

    try {
        const response = await fetch(`${API_BASE}/api/calculate-date`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model, tonnage, customer, expected_date: expectedDate })
        });
        const data = await response.json();
        if (data.success) {
            const calcDate = data.calculated_date || '';
            document.getElementById('calculatedDate').value = calcDate || '计算中...';
            pendingRowIndex = data.row_index || 0;
            
            // 检查E列结果是否为有效日期
            const isDate = calcDate && calcDate.match(/\d{4}-\d{2}-\d{2}/);
            const queueDateInput = document.getElementById('queueDate');
            if (!isDate && calcDate) {
                // 不是日期（如"请联系商务支持"），把date input替换为text input显示提示
                const parent = queueDateInput.parentNode;
                queueDateInput.style.display = 'none';
                // 移除已有的提示元素
                const oldHint = parent.querySelector('.queue-date-hint');
                if (oldHint) oldHint.remove();
                const hint = document.createElement('input');
                hint.type = 'text';
                hint.className = 'queue-date-hint';
                hint.value = '请联系商务支持';
                hint.disabled = true;
                hint.style.cssText = 'width:100%;padding:12px 15px;border:1px solid #ddd;border-radius:8px;font-size:15px;background:#fff0f0;color:#e74c3c;font-weight:500;';
                parent.insertBefore(hint, queueDateInput.nextSibling);
            } else {
                // 是有效日期，恢复date input
                queueDateInput.style.display = '';
                queueDateInput.disabled = false;
                queueDateInput.style.background = '';
                queueDateInput.style.color = '';
                const oldHint = queueDateInput.parentNode.querySelector('.queue-date-hint');
                if (oldHint) oldHint.remove();
            }
            
            showToast('可发货日期已更新' + (pendingRowIndex ? ' (行号:' + pendingRowIndex + ')' : ''), 'success');
        } else {
            showToast('计算失败: ' + data.error, 'error');
            pendingRowIndex = 0;
        }
    } catch (error) {
        showToast('网络错误', 'error');
        pendingRowIndex = 0;
    } finally {
        calcBtn.textContent = '计算可发货日期';
        calcBtn.disabled = false;
    }
}

async function handleCreateOrder(e) {
    e.preventDefault();
    
    const queueDateInput = document.getElementById('queueDate');
    
    // 如果F列被禁用（E列不是有效日期），阻止提交
    if (queueDateInput.disabled) {
        showToast('可发货日期不是有效日期，请联系商务支持后再提交', 'error');
        return;
    }
    
    const calculatedDate = document.getElementById('calculatedDate').value;
    const queueDate = queueDateInput.value;
    
    // 校验：F列（排队日期）必须 >= E列（可发货日期）
    if (calculatedDate && calculatedDate !== '计算中...' && queueDate) {
        // 尝试解析可发货日期（可能是日期格式或文本如"请联系商务支持"）
        const calcParts = calculatedDate.match(/(\d{4})-(\d{2})-(\d{2})/);
        if (calcParts) {
            const calcDateObj = new Date(calcParts[1], parseInt(calcParts[2]) - 1, calcParts[3]);
            const queueDateObj = new Date(queueDate);
            if (queueDateObj < calcDateObj) {
                showToast('排队日期不能早于可发货日期（' + calculatedDate + '）', 'error');
                return;
            }
        }
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
        const response = await fetch(`${API_BASE}/api/orders`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(orderData)
        });
        const data = await response.json();
        if (data.success) {
            showToast('订单创建成功！', 'success');
            document.getElementById('orderForm').reset();
            const today = new Date().toISOString().split('T')[0];
            document.getElementById('expectedDate').value = today;
            document.getElementById('queueDate').value = today;
            document.getElementById('calculatedDate').value = '';
            pendingRowIndex = 0; // 清空
        } else {
            showToast('创建失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

async function loadOrders() {
    const ordersList = document.getElementById('ordersList');
    ordersList.innerHTML = '<div class="loading">加载中...</div>';

    try {
        const response = await fetch(`${API_BASE}/api/orders?submitter_id=${currentUser.id}`);
        const data = await response.json();
        if (data.success) {
            allOrders = data.orders;
            renderOrders(allOrders);
        } else {
            ordersList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>加载失败: ' + data.error + '</p></div>';
        }
    } catch (error) {
        ordersList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>网络错误，请检查连接</p></div>';
    }
}

function renderOrders(orders) {
    const ordersList = document.getElementById('ordersList');
    if (orders.length === 0) {
        ordersList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>暂无订单</p></div>';
        return;
    }

    ordersList.innerHTML = orders.map(order => `
        <div class="order-card">
            <div class="order-header">
                <span class="order-title">${escapeHtml(order.model)}</span>
                <span class="order-serial">#${order.serial_no || order.row_index}</span>
            </div>
            <div class="order-info">
                <div class="info-item">
                    <div class="info-label">吨位</div>
                    <div class="info-value">${escapeHtml(order.tonnage)}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">客户</div>
                    <div class="info-value">${escapeHtml(order.customer)}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">期望发货日期</div>
                    <div class="info-value">${escapeHtml(order.expected_date)}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">可发货日期</div>
                    <div class="info-value">${escapeHtml(order.calculated_date) || '-'}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">排队日期</div>
                    <div class="info-value">${escapeHtml(order.queue_date)}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">提交时间</div>
                    <div class="info-value">${escapeHtml(order.submit_time) || '-'}</div>
                </div>
            </div>
            <div class="order-actions">
                <button class="btn-edit" onclick="openEditModal(${order.row_index})">修改</button>
                <button class="btn-delete" onclick="deleteOrder(${order.row_index})">删除</button>
            </div>
        </div>
    `).join('');
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function filterOrders() {
    const keyword = document.getElementById('filterInput').value.toLowerCase();
    if (!keyword) {
        renderOrders(allOrders);
        return;
    }
    const filtered = allOrders.filter(order =>
        (order.model && order.model.toLowerCase().includes(keyword)) ||
        (order.customer && order.customer.toLowerCase().includes(keyword)) ||
        (order.tonnage && order.tonnage.toString().includes(keyword))
    );
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
        const response = await fetch(`${API_BASE}/api/orders?submitter_id=${currentUser.id}`);
        const data = await response.json();
        if (data.success) {
            const order = data.orders.find(o => o.row_index === rowIndex);
            if (order) {
                document.getElementById('editRowIndex').value = rowIndex;
                document.getElementById('editModel').value = order.model || '';
                document.getElementById('editTonnage').value = order.tonnage || '';
                document.getElementById('editCustomer').value = order.customer || '';
                document.getElementById('editExpectedDate').value = order.expected_date || '';
                document.getElementById('editQueueDate').value = order.queue_date || '';
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

async function handleUpdateOrder(e) {
    e.preventDefault();
    const rowIndex = document.getElementById('editRowIndex').value;
    const orderData = {
        model: document.getElementById('editModel').value,
        tonnage: document.getElementById('editTonnage').value,
        customer: document.getElementById('editCustomer').value,
        expected_date: document.getElementById('editExpectedDate').value,
        queue_date: document.getElementById('editQueueDate').value,
        submitter: currentUser.name,
        submitter_id: currentUser.id
    };

    try {
        const response = await fetch(`${API_BASE}/api/orders/${rowIndex}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(orderData)
        });
        const data = await response.json();
        if (data.success) {
            showToast('订单修改成功！', 'success');
            closeEditModal();
            loadOrders();
        } else {
            showToast('修改失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

async function deleteOrder(rowIndex) {
    if (!confirm('确定要删除这个订单吗？')) return;
    try {
        const response = await fetch(`${API_BASE}/api/orders/${rowIndex}`, { method: 'DELETE' });
        const data = await response.json();
        if (data.success) {
            showToast('订单删除成功！', 'success');
            loadOrders();
        } else {
            showToast('删除失败: ' + data.error, 'error');
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
}
