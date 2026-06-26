let currentUser = { name: '用户', id: '' };
let allOrders = [];
let cachedMineOrders = [];  // 我的排队缓存
let cachedAllOrders = [];   // 全部排队缓存
let modelOptions = [];
let pendingRowIndex = 0;
let currentPage = 1;
let totalPages = 1;
let isAdmin = false;
let isManager = false;
let accessLevel = 'self';
let viewMode = 'mine'; // 'mine' 或 'all'
let listenersInitialized = false;
let ordersDirty = true;
let recentlyDeletedOrders = new Map();
let filterTimer = null;
let ordersRequestSeq = 0;
let modelsLoadedAt = 0;
const RECENT_DELETE_TTL = 30000;
const API_BASE = '';
const PER_PAGE = 20;
const MODEL_LOCAL_CACHE_KEY = 'queueModelOptionsV1';
const MODEL_LOCAL_CACHE_TTL = 24 * 60 * 60 * 1000;
const ADMIN_EMPLOYEE_ID = '20150465';
const ADMIN_KEY_LABELS = {
    TENCENT_ACCESS_TOKEN: '腾讯 access_token',
    RENDER_API_KEY: 'Render API Key',
    GITHUB_TOKEN: 'GitHub Token'
};

// 计算北京时间次日日期字符串（YYYY-MM-DD）
function getBeijingTomorrow() {
    const now = new Date();
    const utc = now.getTime() + now.getTimezoneOffset() * 60000;
    const beijing = new Date(utc + 8 * 3600000);
    beijing.setDate(beijing.getDate() + 1);
    const y = beijing.getFullYear();
    const m = String(beijing.getMonth() + 1).padStart(2, '0');
    const d = String(beijing.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
}

// 从localStorage读取密码、员工ID和用户名
let accessPassword = localStorage.getItem('accessPassword') || '';
let employeeId = localStorage.getItem('employeeId') || '';
let userName = localStorage.getItem('userName') || '';
if (userName) {
    currentUser.name = userName;
    currentUser.id = employeeId;
}

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
    const modelInput = document.getElementById('modelInput');
    if (modelInput) modelInput.value = '';
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
                localStorage.removeItem('userName');
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
    // 员工号输入模式下，不需要加载下拉列表
    // 仅预取用户列表用于后续验证
    try {
        await fetch(`${API_BASE}/auth/users`);
    } catch (error) {
        console.error('预取用户列表失败', error);
    }
}

async function doAuth() {
    const inputEmployeeId = document.getElementById('authUserInput').value.trim();
    const password = document.getElementById('authPassword').value.trim();
    if (!inputEmployeeId) {
        document.getElementById('authError').textContent = '请输入员工号';
        return;
    }
    if (!password) {
        document.getElementById('authError').textContent = '请输入密码';
        return;
    }
    // 先验证员工号是否存在
    try {
        const usersResp = await fetch(`${API_BASE}/auth/users`);
        const usersData = await usersResp.json();
        if (usersData.success && Array.isArray(usersData.users)) {
            const found = usersData.users.some(u => String(u.employee_id) === inputEmployeeId);
            if (!found) {
                document.getElementById('authError').textContent = '员工号不存在';
                return;
            }
        }
    } catch (error) {
        // 验证失败不阻断，继续尝试登录
        console.error('验证员工号失败', error);
    }
    try {
        const response = await fetch(`${API_BASE}/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ employee_id: inputEmployeeId, password })
        });
        const data = await response.json();
        if (data.success) {
            console.log('[doAuth] inputEmployeeId=', inputEmployeeId, 'before: employeeId=', employeeId, 'currentUser.id=', currentUser.id);
            accessPassword = data.access_password || '';
            employeeId = inputEmployeeId;
            const name = data.user?.name || '用户';
            localStorage.setItem('accessPassword', accessPassword);
            localStorage.setItem('employeeId', inputEmployeeId);
            localStorage.setItem('userName', name);
            currentUser.name = name;
            currentUser.id = inputEmployeeId;
            console.log('[doAuth] after: employeeId=', employeeId, 'currentUser.id=', currentUser.id);
            hideAuthOverlay();
            initApp();
        } else {
            document.getElementById('authError').textContent = data.error || '密码错误';
        }
    } catch (error) {
        document.getElementById('authError').textContent = '网络错误';
    }
}

// 未提交排队的临时数据（页面关闭/刷新时清除）
let draftQueue = null;
let lastActivityTime = Date.now();
const IDLE_TIMEOUT = 2 * 60 * 60 * 1000; // 2小时无操作强制退出

function initApp() {
    document.getElementById('userName').textContent = currentUser.name;
    document.getElementById('changePwdBtn').style.display = 'inline-block';
    document.getElementById('logoutBtn').style.display = 'inline-block';
    loadModels();
    setupModelSearch();
    // 先清空所有字段（在绑定事件之前，避免触发计算）
    document.getElementById('model').value = '';
    const modelInput = document.getElementById('modelInput');
    if (modelInput) modelInput.value = '';
    document.getElementById('tonnage').value = '';
    document.getElementById('customer').value = '';
    document.getElementById('calculatedDate').value = '';
    pendingRowIndex = 0;
    // 期望发货日期默认为次日（北京时间）
    document.getElementById('expectedDate').value = getBeijingTomorrow();
    document.getElementById('queueDate').value = '';
    // queueDate 初始为禁用，等 calculatedDate 计算完成后才可编辑
    const queueDateInput = document.getElementById('queueDate');
    queueDateInput.disabled = true;
    queueDateInput.placeholder = '请先选择型号和吨位计算可发货日期';
    queueDateInput.style.cursor = 'not-allowed';
    queueDateInput.style.background = '#e9ecef';
    // 绑定事件监听器
    setupEventListeners();
    setupEditQueueDateListener();
    setupTabSwitchCleanup();
    // 启动无操作检测
    startIdleTimer();
    // 登录后空闲时预取排队明细，用户首次点开更快；不阻塞首屏表单
    const prefetchOrders = () => {
        if (ordersDirty && document.getElementById('listTab') && !document.getElementById('listTab').classList.contains('active')) {
            loadOrders(1, false, { silent: true });
        }
    };
    if ('requestIdleCallback' in window) {
        requestIdleCallback(prefetchOrders, { timeout: 2500 });
    } else {
        setTimeout(prefetchOrders, 1200);
    }
    // 仅李刚（员工号 20150465）启用管理员入口和凭证健康提醒
    if (String(currentUser.id) === ADMIN_EMPLOYEE_ID) {
        const btn = document.getElementById('adminTabBtn');
        if (btn) btn.style.display = '';
        adminHealthCheck();
        setInterval(adminHealthCheck, 10 * 60 * 1000);
    }
}

async function loadModels() {
    const cached = getCachedModels();
    if (cached.length) {
        modelOptions = cached;
        populateModelSelect('model', cached);
        populateModelSelect('editModel', cached);
        populateFilterModelSelect();
    }

    if (cached.length && Date.now() - modelsLoadedAt < MODEL_LOCAL_CACHE_TTL) {
        return;
    }

    try {
        const response = await apiFetch(`${API_BASE}/api/models`);
        const data = await response.json();
        if (data.success) {
            modelOptions = data.models;
            cacheModels(data.models);
            populateModelSelect('model', data.models);
            populateModelSelect('editModel', data.models);
            populateFilterModelSelect();
        } else {
            showToast('加载型号列表失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('网络错误，请检查连接', 'error');
    }
}

function getCachedModels() {
    try {
        const raw = localStorage.getItem(MODEL_LOCAL_CACHE_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed.models)) return [];
        modelsLoadedAt = parsed.time || 0;
        if (Date.now() - modelsLoadedAt > MODEL_LOCAL_CACHE_TTL) return [];
        return parsed.models;
    } catch (e) {
        return [];
    }
}

function cacheModels(models) {
    try {
        localStorage.setItem(MODEL_LOCAL_CACHE_KEY, JSON.stringify({ time: Date.now(), models }));
        modelsLoadedAt = Date.now();
    } catch (e) {}
}

function populateModelSelect(selectId, models) {
    const select = document.getElementById(selectId);
    if (!select) return;
    select.innerHTML = '<option value="">请选择型号</option>';
    models.forEach(model => {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        select.appendChild(option);
    });
    // 如果是主表单的model，同步到combobox
    if (selectId === 'model') {
        initModelCombobox(models);
    }
}

// ============ 型号可搜索下拉框 (combobox) ============
let comboboxModels = [];
let comboboxHighlightIndex = -1;

function initModelCombobox(models) {
    comboboxModels = models;
    const input = document.getElementById('modelInput');
    const hidden = document.getElementById('model');
    if (!input || !hidden) return;
    if (hidden.value && models.includes(hidden.value)) {
        input.value = hidden.value;
    }
}

function setupModelSearch() {
    const input = document.getElementById('modelInput');
    const hidden = document.getElementById('model');
    const dropdown = document.getElementById('modelDropdown');
    const combobox = document.getElementById('modelCombobox');
    if (!input || !hidden || !dropdown || !combobox) return;

    input.addEventListener('focus', function() {
        filterAndShowDropdown(this.value);
    });

    input.addEventListener('input', function() {
        hidden.value = '';
        filterAndShowDropdown(this.value);
    });

    input.addEventListener('keydown', function(e) {
        const items = dropdown.querySelectorAll('li:not(.empty-msg)');
        if (!dropdown.classList.contains('show') || items.length === 0) {
            if (e.key === 'ArrowDown' || e.key === 'Enter') {
                filterAndShowDropdown(this.value);
                e.preventDefault();
            }
            return;
        }
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            comboboxHighlightIndex = Math.min(comboboxHighlightIndex + 1, items.length - 1);
            updateHighlight(items);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            comboboxHighlightIndex = Math.max(comboboxHighlightIndex - 1, 0);
            updateHighlight(items);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (comboboxHighlightIndex >= 0 && items[comboboxHighlightIndex]) {
                selectModel(items[comboboxHighlightIndex].textContent);
            }
        } else if (e.key === 'Escape') {
            closeDropdown();
        }
    });

    dropdown.addEventListener('click', function(e) {
        const li = e.target.closest('li:not(.empty-msg)');
        if (li) selectModel(li.textContent);
    });

    document.addEventListener('click', function(e) {
        if (!combobox.contains(e.target)) closeDropdown();
    });

    function filterAndShowDropdown(keyword) {
        const kw = (keyword || '').trim().toLowerCase();
        const filtered = kw ? comboboxModels.filter(m => m.toLowerCase().includes(kw)) : comboboxModels;
        dropdown.innerHTML = '';
        comboboxHighlightIndex = -1;
        if (filtered.length === 0) {
            const li = document.createElement('li');
            li.className = 'empty-msg';
            li.textContent = '无匹配型号';
            dropdown.appendChild(li);
        } else {
            filtered.forEach(model => {
                const li = document.createElement('li');
                li.textContent = model;
                dropdown.appendChild(li);
            });
        }
        combobox.classList.add('open');
        dropdown.classList.add('show');
    }

    function selectModel(modelName) {
        input.value = modelName;
        hidden.value = modelName;
        closeDropdown();
        // 同时触发modelInput和hidden model的change事件，确保calculateDate能获取最新型号
        input.dispatchEvent(new Event('change', { bubbles: true }));
        hidden.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function closeDropdown() {
        combobox.classList.remove('open');
        dropdown.classList.remove('show');
        comboboxHighlightIndex = -1;
    }

    function updateHighlight(items) {
        items.forEach((item, i) => {
            item.classList.toggle('highlighted', i === comboboxHighlightIndex);
        });
        if (items[comboboxHighlightIndex]) {
            items[comboboxHighlightIndex].scrollIntoView({ block: 'nearest' });
        }
    }
}

function setupEventListeners() {
    if (listenersInitialized) return;
    listenersInitialized = true;

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
    const draftModelInput = document.getElementById('modelInput');
    if (draftModelInput) {
        draftModelInput.addEventListener('input', saveDraft);
        draftModelInput.addEventListener('change', saveDraft);
    }
    // 日期选择器已改用自定义组件（showDatePicker函数），无需自动关闭逻辑
    // 监听用户操作，记录活动时间
    ['click', 'keydown', 'scroll', 'touchstart'].forEach(evt => {
        document.addEventListener(evt, recordActivity, { passive: true });
    });
    // 创建页面自动计算：型号/customer用change，吨位/期望日期用input+change实现即时触发
    const calcFieldsChange = ['model', 'customer'];
    calcFieldsChange.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.addEventListener('change', debounce(calculateDate, 300));
        }
    });
    const calcFieldsInput = ['tonnage', 'expectedDate'];
    calcFieldsInput.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.addEventListener('input', debounce(calculateDate, 300));
            // 日期选择器在某些浏览器中只触发change不触发input，同时监听两者
            field.addEventListener('change', debounce(calculateDate, 300));
        }
    });
    const calcModelInput = document.getElementById('modelInput');
    if (calcModelInput) {
        calcModelInput.addEventListener('change', debounce(calculateDate, 300));
    }
    // 修改页面自动计算
    const editCalcFields = ['editModel', 'editTonnage', 'editCustomer', 'editExpectedDate'];
    editCalcFields.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.addEventListener('change', debounce(calculateDateForEdit, 300));
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

    // 计算开始时，清空可发货日期和输入发货日期排队
    document.getElementById('calculatedDate').value = '计算中...';
    const queueDateInput = document.getElementById('queueDate');
    queueDateInput.value = '';
    queueDateInput.disabled = true;
    queueDateInput.style.background = '#e9ecef';
    queueDateInput.style.cursor = 'not-allowed';
    queueDateInput.placeholder = '计算中...';
    // 清除之前的提示
    const oldHint = queueDateInput.parentNode.querySelector('.queue-date-hint');
    if (oldHint) oldHint.remove();

    try {
        const response = await apiFetch(`${API_BASE}/api/calculate-date`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model, tonnage, customer, expected_date: expectedDate, pending_row_index: pendingRowIndex, submitter_id: currentUser.id, force_refresh: false })
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
            if (!isDate && calcDate) {
                // 公式返回非日期文本（如错误信息）
                queueDateInput.style.display = 'none';
                const parent = queueDateInput.parentNode;
                const oldHint2 = parent.querySelector('.queue-date-hint');
                if (oldHint2) oldHint2.remove();
                const hint = document.createElement('input');
                hint.type = 'text';
                hint.className = 'queue-date-hint';
                hint.value = calcDate || '请联系商务支持';
                hint.disabled = true;
                hint.style.cssText = 'width:100%;padding:12px 15px;border:1px solid #ddd;border-radius:8px;font-size:15px;background:#fff0f0;color:#e74c3c;font-weight:500;';
                parent.insertBefore(hint, queueDateInput.nextSibling);
            } else if (isDate) {
                queueDateInput.style.display = '';
                queueDateInput.disabled = false;
                queueDateInput.style.background = '#fff';
                queueDateInput.style.color = '';
                queueDateInput.style.cursor = 'text';
                queueDateInput.placeholder = '点击选择日期';
                queueDateInput.value = calcDate;
                const oldHint2 = queueDateInput.parentNode.querySelector('.queue-date-hint');
                if (oldHint2) oldHint2.remove();
            } else {
                queueDateInput.style.display = '';
                queueDateInput.disabled = false;
                queueDateInput.style.background = '#fff';
                queueDateInput.style.cursor = 'text';
                queueDateInput.placeholder = '点击选择日期';
                const oldHint2 = queueDateInput.parentNode.querySelector('.queue-date-hint');
                if (oldHint2) oldHint2.remove();
            }
        } else {
            const errMsg = data.error || '计算失败';
            document.getElementById('calculatedDate').value = errMsg;
            pendingRowIndex = 0;
            queueDateInput.disabled = true;
            queueDateInput.style.background = '#e9ecef';
            queueDateInput.style.cursor = 'not-allowed';
            queueDateInput.placeholder = errMsg;
        }
    } catch (error) {
        if (myVersion !== calcVersion) {
            pendingCalcs--;
            return;
        }
        document.getElementById('calculatedDate').value = '计算失败';
        pendingRowIndex = 0;
        queueDateInput.disabled = true;
        queueDateInput.style.background = '#e9ecef';
        queueDateInput.style.cursor = 'not-allowed';
        queueDateInput.placeholder = '计算失败，请重试';
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
        row_index: pendingRowIndex // 使用calculate-date阶段确定的行号
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
            ordersDirty = true;
            allOrders = [];
            document.getElementById('orderForm').reset();
            // 重置型号combobox
            document.getElementById('model').value = '';
            const mi = document.getElementById('modelInput');
            if (mi) mi.value = '';
            // 重置为次日（北京时间）
            document.getElementById('expectedDate').value = getBeijingTomorrow();
            document.getElementById('queueDate').value = '';
            document.getElementById('queueDate').disabled = true;
            document.getElementById('queueDate').placeholder = '请先选择型号和吨位计算可发货日期';
            document.getElementById('queueDate').style.cursor = 'not-allowed';
            document.getElementById('queueDate').style.background = '#e9ecef';
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

async function loadOrders(page = 1, forceRefresh = false, options = {}) {
    const ordersList = document.getElementById('ordersList');
    if (!currentUser.id || currentUser.id === 'auth_user' || currentUser.id === 'test_user_001') {
        ordersList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>登录信息异常，请退出后重新选择员工登录</p></div>';
        showAuthOverlay('登录信息异常，请重新选择员工登录');
        return;
    }
    const silent = Boolean(options.silent);
    if (!silent) {
        ordersList.innerHTML = '<div class="loading">加载中...</div>';
    }
    const requestSeq = ++ordersRequestSeq;

    try {
        currentPage = page;
        const viewModeParam = `&view_mode=${viewMode}`;
        // 始终带时间戳，避免浏览器缓存返回旧数据
        const refreshParam = `&_ts=${Date.now()}`;
        const submitterNameParam = `&submitter_name=${encodeURIComponent(currentUser.name || '')}`;
        const modelFilter = encodeURIComponent(document.getElementById('filterModel')?.value || '');
        const customerFilter = encodeURIComponent(document.getElementById('filterCustomer')?.value.trim() || '');
        const sortType = encodeURIComponent(document.getElementById('sortSelect')?.value || '');
        const filterParams = `&model_filter=${modelFilter}&customer_filter=${customerFilter}&sort=${sortType}`;
        const response = await apiFetch(`${API_BASE}/api/orders?submitter_id=${encodeURIComponent(currentUser.id || '')}${submitterNameParam}&page=${page}&per_page=${PER_PAGE}${viewModeParam}${filterParams}${refreshParam}`, {
            cache: 'no-store'
        });
        const data = await response.json();
        if (requestSeq !== ordersRequestSeq) return;
        if (data.success) {
            const newOrders = (data.orders || []).filter(order => !isRecentlyDeletedOrder(order));
            // 保存到对应viewMode的缓存（使用请求时的viewMode，不用后端返回值覆盖）
            const requestViewMode = viewMode;
            if (requestViewMode === 'mine') {
                cachedMineOrders = newOrders;
            } else {
                cachedAllOrders = newOrders;
            }
            // 如果API返回空但对应缓存有数据，使用对应缓存（降级显示）
            const cachedOrders = requestViewMode === 'mine' ? cachedMineOrders : cachedAllOrders;
            if (newOrders.length === 0 && cachedOrders.length > 0) {
                console.warn('[loadOrders] API返回空，使用' + viewMode + '缓存数据');
                allOrders = cachedOrders;
                renderOrders(allOrders);
                return;
            }
            allOrders = newOrders;
            currentPage = data.pagination?.page || 1;
            totalPages = data.pagination?.total_pages || 1;
            isAdmin = data.is_admin;
            isManager = data.access_level === 'department';
            accessLevel = data.access_level || 'self';
            // 注意：不覆盖 viewMode，保持前端用户选择
            // viewMode = data.view_mode;
            renderOrders(allOrders);
            renderPagination();
            renderAdminFilter();
            populateFilterModelSelect();
            ordersDirty = false;
        } else {
            if (silent) return;
            // 请求失败时，使用对应viewMode的缓存
            const cachedOrders = viewMode === 'mine' ? cachedMineOrders : cachedAllOrders;
            if (cachedOrders.length > 0) {
                allOrders = cachedOrders;
                renderOrders(allOrders);
                showToast('数据加载失败，显示的是缓存数据', 'warning');
                return;
            }
            ordersList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>加载失败: ' + data.error + '</p></div>';
        }
    } catch (error) {
        if (requestSeq !== ordersRequestSeq || silent) return;
        console.error('[loadOrders] error:', error);
        // 网络错误时，使用对应viewMode的缓存
        const cachedOrders = viewMode === 'mine' ? cachedMineOrders : cachedAllOrders;
        if (cachedOrders.length > 0) {
            allOrders = cachedOrders;
            renderOrders(allOrders);
            showToast('网络错误，显示的是缓存数据', 'warning');
            return;
        }
        ordersList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>网络错误，请检查连接</p></div>';
    }
}

function getOrderSignature(order) {
    if (!order) return '';
    return [
        order.model || '',
        order.tonnage || '',
        order.customer || '',
        order.expected_date || '',
        order.queue_date || '',
        order.submitter || '',
        order.submitter_id || '',
        order.submit_time || ''
    ].join('|');
}

function cleanupRecentlyDeletedOrders() {
    const now = Date.now();
    recentlyDeletedOrders.forEach((deletedAt, key) => {
        if (now - deletedAt > RECENT_DELETE_TTL) {
            recentlyDeletedOrders.delete(key);
        }
    });
}

function markOrderDeleted(order) {
    const signature = getOrderSignature(order);
    if (signature) {
        recentlyDeletedOrders.set(signature, Date.now());
    }
}

function isRecentlyDeletedOrder(order) {
    cleanupRecentlyDeletedOrders();
    const signature = getOrderSignature(order);
    return Boolean(signature && recentlyDeletedOrders.has(signature));
}

function populateFilterModelSelect() {
    const select = document.getElementById('filterModel');
    if (!select) return;
    const currentVal = select.value;
    // 筛选下拉使用完整型号表，兼容订单中存在但型号表暂未返回的型号
    const models = [...new Set([
        ...modelOptions,
        ...allOrders.map(o => o.model)
    ].filter(Boolean))].sort();
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
    if (!orders || orders.length === 0) {
        ordersList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><p>暂无排队</p></div>';
        return;
    }

    // 权限判断：业务员（非管理员非经理）在全部排队隐藏客户+禁止操作
    const canSeeCustomer = viewMode === 'mine' || isAdmin || isManager;
    const canOperate = viewMode === 'mine' || isAdmin || isManager;

    let html = `<table class="order-table">
        <thead>
            <tr>
                <th>型号</th>
                <th>吨位</th>
                ${canSeeCustomer ? '<th>客户</th>' : ''}
                ${viewMode === 'all' ? '<th>业务员</th>' : ''}
                <th>排队日期</th>
                ${canOperate ? '<th>操作</th>' : ''}
            </tr>
        </thead>
        <tbody>`;

    orders.forEach(order => {
        // 排队日期只显示月日，过滤Excel空日期默认值1899-12-30
        let queueDateDisplay = escapeHtml(order.queue_date);
        if (queueDateDisplay && queueDateDisplay.match(/^\d{4}-\d{2}-\d{2}$/) && queueDateDisplay !== '1899-12-30') {
            queueDateDisplay = queueDateDisplay.substring(5); // 取 MM-DD
        } else {
            queueDateDisplay = '';
        }
        html += `<tr>
            <td class="td-model">${escapeHtml(order.model)}</td>
            <td>${escapeHtml(order.tonnage)}</td>
            ${canSeeCustomer ? '<td>' + escapeHtml(order.customer) + '</td>' : ''}
            ${viewMode === 'all' ? '<td>' + escapeHtml(order.submitter || '') + '</td>' : ''}
            <td>${queueDateDisplay}</td>
            ${canOperate ? `<td class="td-actions">
                <button class="btn-edit" onclick="openEditModal(${order.row_index})">改</button>
                <button class="btn-copy" onclick="copyOrder(${order.row_index})">复</button>
                <button class="btn-delete" onclick="deleteOrder(${order.row_index})">删</button>
            </td>` : ''}
        </tr>`;
    });

    html += '</tbody></table>';
    ordersList.innerHTML = html;
}

function renderAdminFilter() {
    const filterEl = document.getElementById('adminFilter');
    if (!filterEl) return;

    const mineClass = viewMode === 'mine' ? 'active' : '';
    const allClass = viewMode === 'all' ? 'active' : '';
    
    filterEl.innerHTML = `
        <div class="admin-filter">
            <button class="${mineClass}" onclick="switchViewMode('mine')">我的排队</button>
            <button class="${allClass}" onclick="switchViewMode('all')">全部排队</button>
        </div>
    `;
}

function switchViewMode(mode) {
    viewMode = mode;
    loadOrders(1, true);
}

function renderPagination() {
    const paginationEl = document.getElementById('pagination');
    if (!paginationEl) return;
    
    if (totalPages <= 1) {
        paginationEl.innerHTML = '';
        return;
    }
    
    let html = '<div class="pagination">';
    
    // 上一页
    if (currentPage > 1) {
        html += `<button onclick="loadOrders(${currentPage - 1})">上一页</button>`;
    } else {
        html += `<button disabled>上一页</button>`;
    }
    
    // 页码
    html += `<span class="page-info">${currentPage} / ${totalPages}</span>`;
    
    // 下一页
    if (currentPage < totalPages) {
        html += `<button onclick="loadOrders(${currentPage + 1})">下一页</button>`;
    } else {
        html += `<button disabled>下一页</button>`;
    }
    
    html += '</div>';
    paginationEl.innerHTML = html;
}

const _escapeHtmlCache = new Map();
function escapeHtml(text) {
    if (!text) return '';
    if (_escapeHtmlCache.has(text)) return _escapeHtmlCache.get(text);
    const div = document.createElement('div');
    div.textContent = text;
    const result = div.innerHTML;
    _escapeHtmlCache.set(text, result);
    return result;
}

function filterOrders() {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => loadOrders(1, false), 250);
}

function sortOrders() {
    loadOrders(1, false);
}

async function openEditModal(rowIndex) {
    try {
        const submitterNameParam = `&submitter_name=${encodeURIComponent(currentUser.name || '')}`;
        const response = await apiFetch(`${API_BASE}/api/orders/${rowIndex}?submitter_id=${encodeURIComponent(currentUser.id || '')}${submitterNameParam}`);
        const data = await response.json();
        if (data.success) {
            const order = data.order;
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
                // 如果可发货日期不是日期格式（如"请联系商务支持"），禁止编辑排队日期
                const calcDate = order.calculated_date || '';
                const isDate = calcDate && calcDate.match(/^\d{4}-\d{2}-\d{2}$/);
                const editQueueDateInput = document.getElementById('editQueueDate');
                if (!isDate) {
                    editQueueDateInput.disabled = true;
                    editQueueDateInput.style.background = '#e9ecef';
                    editQueueDateInput.style.cursor = 'not-allowed';
                    editQueueDateInput.title = '可发货日期无效，无法编辑排队日期';
                } else {
                    editQueueDateInput.disabled = false;
                    editQueueDateInput.style.background = '#fff';
                    editQueueDateInput.style.cursor = 'pointer';
                    editQueueDateInput.title = '';
                }
                document.getElementById('editModal').classList.add('show');
            }
        }
    } catch (error) {
        showToast('加载失败', 'error');
    }
}

async function copyOrder(rowIndex) {
    try {
        const submitterNameParam = `&submitter_name=${encodeURIComponent(currentUser.name || '')}`;
        const response = await apiFetch(`${API_BASE}/api/orders/${rowIndex}?submitter_id=${encodeURIComponent(currentUser.id || '')}${submitterNameParam}`);
        const data = await response.json();
        if (!data.success || !data.order) {
            showToast('复制失败: ' + (data.error || '订单不存在'), 'error');
            return;
        }

        const order = data.order;
        showTab('create');
        pendingRowIndex = 0;
        draftQueue = null;

        document.getElementById('model').value = order.model || '';
        const modelInputCopy = document.getElementById('modelInput');
        if (modelInputCopy) modelInputCopy.value = order.model || '';
        document.getElementById('tonnage').value = order.tonnage || '';
        document.getElementById('customer').value = order.customer || '';
        document.getElementById('expectedDate').value = order.expected_date || '';
        document.getElementById('calculatedDate').value = '';
        document.getElementById('queueDate').value = '';

        calculateDate();

        showToast('已复制订单内容，请确认后提交新排队', 'success');
    } catch (error) {
        showToast('网络错误，复制失败', 'error');
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
            body: JSON.stringify({ employee_id: employeeId, old_password: oldPassword, new_password: newPassword })
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
            localStorage.removeItem('userName');
            showAuthOverlay('密码已修改，请重新登录');
        } else {
            showToast(data.error || '密码修改失败', 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

// 自定义日期选择器（解决手机Chrome原生日期选择器无法自动关闭问题）
function showDatePicker(input) {
    // 获取今天的日期
    const today = new Date();
    const currentYear = today.getFullYear();
    const currentMonth = today.getMonth();
    
    // 解析当前值
    let selectedDate = input.value ? new Date(input.value) : null;
    if (selectedDate && isNaN(selectedDate.getTime())) selectedDate = null;
    
    let displayYear = selectedDate ? selectedDate.getFullYear() : currentYear;
    let displayMonth = selectedDate ? selectedDate.getMonth() : currentMonth;
    
    // 创建遮罩层
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:3000;display:flex;justify-content:center;align-items:center;';
    
    // 创建日历容器
    const picker = document.createElement('div');
    picker.style.cssText = 'background:#fff;border-radius:12px;padding:16px;width:280px;box-shadow:0 4px 20px rgba(0,0,0,0.3);';
    
    // 渲染日历函数
    function renderCalendar() {
        const monthNames = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];
        const firstDay = new Date(displayYear, displayMonth, 1).getDay();
        const daysInMonth = new Date(displayYear, displayMonth + 1, 0).getDate();
        
        let html = `
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <button onclick="prevMonth()" style="background:none;border:none;font-size:18px;cursor:pointer;padding:4px 8px;">‹</button>
                <span style="font-weight:600;font-size:16px;">${displayYear}年${monthNames[displayMonth]}</span>
                <button onclick="nextMonth()" style="background:none;border:none;font-size:18px;cursor:pointer;padding:4px 8px;">›</button>
            </div>
            <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:4px;text-align:center;margin-bottom:8px;">
                <div style="color:#999;font-size:12px;padding:4px;">日</div>
                <div style="color:#999;font-size:12px;padding:4px;">一</div>
                <div style="color:#999;font-size:12px;padding:4px;">二</div>
                <div style="color:#999;font-size:12px;padding:4px;">三</div>
                <div style="color:#999;font-size:12px;padding:4px;">四</div>
                <div style="color:#999;font-size:12px;padding:4px;">五</div>
                <div style="color:#999;font-size:12px;padding:4px;">六</div>
            </div>
            <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:4px;text-align:center;">
        `;
        
        // 空白格
        for (let i = 0; i < firstDay; i++) {
            html += '<div></div>';
        }
        
        // 日期
        for (let day = 1; day <= daysInMonth; day++) {
            const dateStr = `${displayYear}-${String(displayMonth + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
            const isSelected = selectedDate && selectedDate.getDate() === day && selectedDate.getMonth() === displayMonth && selectedDate.getFullYear() === displayYear;
            const isToday = today.getDate() === day && today.getMonth() === displayMonth && today.getFullYear() === displayYear;
            
            let bg = isSelected ? '#667eea' : (isToday ? '#e8f0fe' : 'transparent');
            let color = isSelected ? '#fff' : (isToday ? '#667eea' : '#333');
            
            html += `<button onclick="selectDate('${dateStr}')" style="background:${bg};color:${color};border:none;border-radius:50%;width:32px;height:32px;font-size:14px;cursor:pointer;padding:0;">${day}</button>`;
        }
        
        html += '</div>';
        html += `
            <div style="display:flex;justify-content:space-between;margin-top:16px;padding-top:12px;border-top:1px solid #eee;">
                <button onclick="clearDate()" style="background:none;border:none;color:#999;font-size:14px;cursor:pointer;">清除</button>
                <button onclick="cancelPicker()" style="background:none;border:none;color:#667eea;font-size:14px;cursor:pointer;">取消</button>
            </div>
        `;
        
        picker.innerHTML = html;
    }
    
    // 全局函数供HTML调用
    window.prevMonth = function() {
        displayMonth--;
        if (displayMonth < 0) {
            displayMonth = 11;
            displayYear--;
        }
        renderCalendar();
    };
    
    window.nextMonth = function() {
        displayMonth++;
        if (displayMonth > 11) {
            displayMonth = 0;
            displayYear++;
        }
        renderCalendar();
    };
    
    window.selectDate = function(dateStr) {
        input.value = dateStr;
        // 触发change事件
        input.dispatchEvent(new Event('change', { bubbles: true }));
        closePicker();
    };
    
    window.clearDate = function() {
        input.value = '';
        input.dispatchEvent(new Event('change', { bubbles: true }));
        closePicker();
    };
    
    window.cancelPicker = function() {
        closePicker();
    };
    
    function closePicker() {
        document.body.removeChild(overlay);
        delete window.prevMonth;
        delete window.nextMonth;
        delete window.selectDate;
        delete window.clearDate;
        delete window.cancelPicker;
    }
    
    // 点击遮罩关闭
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) closePicker();
    });
    
    renderCalendar();
    overlay.appendChild(picker);
    document.body.appendChild(overlay);
}

function doLogout() {
    accessPassword = '';
    employeeId = '';
    localStorage.removeItem('accessPassword');
    localStorage.removeItem('employeeId');
    localStorage.removeItem('userName');
    currentUser = { name: '用户', id: '' };
    document.getElementById('changePwdBtn').style.display = 'none';
    document.getElementById('logoutBtn').style.display = 'none';
    document.getElementById('userName').textContent = '未登录';
    // 强制刷新页面，确保所有状态被清除，避免切换用户时ID混乱
    window.location.reload();
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
            document.getElementById('editCalculatedDate').value = data.error || '计算失败';
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
            ordersDirty = true;
            closeEditModal();
            loadOrders(currentPage, true);
        } else {
            showToast('排队修改失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

async function deleteOrder(rowIndex) {
    if (!confirm('确定要删除这个排队吗？')) return;
    const deletedOrder = allOrders.find(order => Number(order.row_index) === Number(rowIndex));
    try {
        const submitterNameParam = `&submitter_name=${encodeURIComponent(currentUser.name || '')}`;
        const response = await apiFetch(`${API_BASE}/api/orders/${rowIndex}?submitter_id=${encodeURIComponent(currentUser.id || '')}${submitterNameParam}`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order: deletedOrder || {} })
        });
        const data = await response.json();
        if (data.success) {
            showToast('排队删除成功！', 'success');
            markOrderDeleted(deletedOrder);
            allOrders = allOrders.filter(order => Number(order.row_index) !== Number(rowIndex));
            renderOrders(allOrders);
            renderPagination();
            populateFilterModelSelect();
            ordersDirty = true;
            loadOrders(currentPage, true);
        } else {
            showToast('排队删除失败: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('网络错误', 'error');
    }
}

function showTab(tabName, evt) {
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    const activeBtn = evt?.target || document.querySelector(`.tab-btn[onclick*="showTab('${tabName}')"]`);
    if (activeBtn) activeBtn.classList.add('active');
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
    document.getElementById(tabName + 'Tab').classList.add('active');
    if (tabName === 'list') loadOrders(1, true);  // 切换tab时强制刷新
    if (tabName === 'admin') loadAdminStatus();
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

// 清空临时行的函数（页面关闭/刷新时立即调用）
function clearTempRowSync() {
    if (pendingRowIndex > 0) {
        try {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', `${API_BASE}/api/clear-temp-row`, false); // 同步请求
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.setRequestHeader('X-Access-Password', accessPassword);
            xhr.setRequestHeader('X-Employee-Id', employeeId);
            xhr.send(JSON.stringify({ row_index: pendingRowIndex }));
        } catch (err) {
            // 忽略错误
        }
        pendingRowIndex = 0;
    }
}

// 页面关闭/刷新前：使用sendBeacon清空临时行（比同步XHR更可靠）
window.addEventListener('beforeunload', function(e) {
    if (hasUnsavedOrder() && pendingRowIndex > 0) {
        // 使用sendBeacon确保请求发送
        if (navigator.sendBeacon) {
            const data = JSON.stringify({ row_index: pendingRowIndex });
            const blob = new Blob([data], { type: 'application/json' });
            navigator.sendBeacon(`${API_BASE}/api/clear-temp-row`, blob);
        } else {
            // 降级：同步XHR
            clearTempRowSync();
        }
        pendingRowIndex = 0;
    }
});

// 页面加载时：清理当前用户之前遗留的临时行（防止刷新后残留）
window.addEventListener('load', function() {
    // 页面加载后，如果有未提交的表单数据，清理之前的临时行
    // 因为刷新后pendingRowIndex会重置为0，但腾讯表格中可能还有残留数据
    setTimeout(() => {
        if (currentUser.id) {
            // 调用API清理当前用户所有过期的临时行
            fetch(`${API_BASE}/api/cleanup-user-temp-rows`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Access-Password': accessPassword,
                    'X-Employee-Id': employeeId
                },
                body: JSON.stringify({ submitter_id: currentUser.id })
            }).catch(() => {});
        }
    }, 1000);
});

// 系统内切换标签页（订单排队/排队明细/问题反馈）：不清空临时行
// 只在提交成功或页面关闭/刷新时才清空
// 切换标签页后返回，继续在同一临时行操作
function setupTabSwitchCleanup() {
    // 不再在标签页切换时清空临时行
    // pendingRowIndex 保持不变，用户返回后继续在同一行操作
}

// 页面可见性变化时（切换浏览器窗口/标签页）：5分钟后清空
let tabSwitchTimer = null;
document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'hidden' && hasUnsavedOrder()) {
        // 页面隐藏时启动5分钟定时器
        if (!tabSwitchTimer) {
            tabSwitchTimer = setTimeout(() => {
                if (pendingRowIndex > 0) {
                    // 使用sendBeacon确保请求发送
                    if (navigator.sendBeacon) {
                        const data = JSON.stringify({ row_index: pendingRowIndex });
                        const blob = new Blob([data], { type: 'application/json' });
                        navigator.sendBeacon(`${API_BASE}/api/clear-temp-row`, blob);
                    }
                    pendingRowIndex = 0;
                }
                tabSwitchTimer = null;
            }, 5 * 60 * 1000); // 5分钟
        }
    } else if (document.visibilityState === 'visible') {
        // 页面重新可见时，取消定时器
        if (tabSwitchTimer) {
            clearTimeout(tabSwitchTimer);
            tabSwitchTimer = null;
        }
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
            localStorage.removeItem('userName');
            // 如果有未提交的排队，清除
            if (hasUnsavedOrder()) {
                document.getElementById('orderForm').reset();
            }
            showAuthOverlay('长时间未操作，请重新登录');
        }
    }, 30000); // 每30秒检查一次
}

// ============ 管理员凭证管理（仅李刚） ============

function isAdminUser() {
    return String(currentUser.id) === ADMIN_EMPLOYEE_ID;
}

function formatRemainingHours(seconds) {
    if (!seconds || seconds <= 0) return '已过期';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟';
    if (seconds < 24 * 3600) return Math.floor(seconds / 3600) + ' 小时';
    return Math.floor(seconds / (24 * 3600)) + ' 天';
}

async function loadAdminStatus() {
    if (!isAdminUser()) return;
    const wrap = document.getElementById('adminItems');
    wrap.innerHTML = '<div class="admin-item-msg">加载中…</div>';
    try {
        const r = await apiFetch(`${API_BASE}/api/admin/status`);
        const data = await r.json();
        if (!data.success) throw new Error(data.error || '加载失败');
        renderAdminItems(data.items);
    } catch (e) {
        wrap.innerHTML = `<div class="admin-item-msg err">加载失败：${e.message}</div>`;
    }
}

function renderAdminItems(items) {
    const wrap = document.getElementById('adminItems');
    wrap.innerHTML = '';
    items.forEach(item => {
        const card = document.createElement('div');
        card.className = 'admin-item';
        const label = ADMIN_KEY_LABELS[item.name] || item.name;
        let statusHtml = '';
        if (!item.present) {
            statusHtml = '<span class="admin-item-status warn">未配置</span>';
        } else if (item.name === 'TENCENT_ACCESS_TOKEN' && typeof item.remaining_seconds === 'number') {
            const remain = item.remaining_seconds;
            if (remain <= 0) {
                statusHtml = '<span class="admin-item-status err">已过期</span>';
            } else if (remain < 24 * 3600) {
                statusHtml = `<span class="admin-item-status warn">即将过期 ${formatRemainingHours(remain)}</span>`;
            } else {
                statusHtml = `<span class="admin-item-status ok">剩余 ${formatRemainingHours(remain)}</span>`;
            }
        } else {
            statusHtml = '<span class="admin-item-status ok">已配置</span>';
        }
        const mask = item.masked ? `当前：<span class="admin-item-mask">${item.masked}</span>` : '<span class="admin-item-mask">尚未保存</span>';
        card.innerHTML = `
            <div class="admin-item-head">
                <span class="admin-item-name">${label}</span>
                ${statusHtml}
            </div>
            <div class="admin-item-mask" style="margin-bottom:8px;">${mask}</div>
            <div class="admin-item-row">
                <input type="password" autocomplete="new-password" placeholder="粘贴新的 ${label}，输入不会回显" data-key="${item.name}" />
                <button type="button" class="admin-btn-validate" data-action="validate">校验</button>
                <button type="button" class="admin-btn-update" data-action="update">保存并部署</button>
            </div>
            <div class="admin-item-msg" data-msg></div>
        `;
        const input = card.querySelector('input');
        const msg = card.querySelector('[data-msg]');
        card.querySelector('[data-action="validate"]').addEventListener('click', () => onAdminValidate(item.name, input, msg));
        card.querySelector('[data-action="update"]').addEventListener('click', () => onAdminUpdate(item.name, input, msg));
        wrap.appendChild(card);
    });
}

async function onAdminValidate(key, input, msg) {
    const value = (input.value || '').trim();
    if (!value) {
        msg.className = 'admin-item-msg err';
        msg.textContent = '请先粘贴新的值';
        return;
    }
    msg.className = 'admin-item-msg';
    msg.textContent = '校验中…';
    try {
        const r = await apiFetch(`${API_BASE}/api/admin/validate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key, value })
        });
        const data = await r.json();
        if (data.success) {
            msg.className = 'admin-item-msg ok';
            msg.textContent = '校验通过，可保存并部署';
        } else {
            msg.className = 'admin-item-msg err';
            msg.textContent = '校验失败：' + (data.error || '未知错误');
        }
    } catch (e) {
        msg.className = 'admin-item-msg err';
        msg.textContent = '校验异常：' + e.message;
    }
}

async function onAdminUpdate(key, input, msg) {
    const value = (input.value || '').trim();
    if (!value) {
        msg.className = 'admin-item-msg err';
        msg.textContent = '请先粘贴新的值';
        return;
    }
    if (!confirm(`确认更新「${ADMIN_KEY_LABELS[key] || key}」？\n将写入主服务 Render 环境变量，并触发重新部署。`)) return;
    msg.className = 'admin-item-msg';
    msg.textContent = '正在校验、保存并触发部署…';
    try {
        const r = await apiFetch(`${API_BASE}/api/admin/update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key, value })
        });
        const data = await r.json();
        if (data.success) {
            msg.className = 'admin-item-msg ok';
            msg.textContent = (data.message || '已更新') + (data.log ? `（日志：${data.log.key}=${data.log.masked}）` : '');
            input.value = '';
            const log = document.getElementById('adminLog');
            const item = document.createElement('div');
            const t = new Date().toLocaleString();
            item.textContent = `[${t}] ${ADMIN_KEY_LABELS[key] || key} 已更新（${data.log ? data.log.masked : ''}），已写入 Render 环境变量并触发部署`;
            log.prepend(item);
            setTimeout(loadAdminStatus, 2000);
        } else {
            msg.className = 'admin-item-msg err';
            msg.textContent = '更新失败：' + (data.error || '未知错误');
        }
    } catch (e) {
        msg.className = 'admin-item-msg err';
        msg.textContent = '更新异常：' + e.message;
    }
}

async function adminHealthCheck() {
    if (!isAdminUser()) return;
    const bar = document.getElementById('adminAlertBar');
    if (!bar) return;
    try {
        const r = await apiFetch(`${API_BASE}/api/admin/health`);
        const data = await r.json();
        if (!data.success || data.healthy) {
            bar.style.display = 'none';
            return;
        }
        const onlyWarn = (data.issues || []).every(i => i.level === 'warn');
        bar.className = 'admin-alert-bar' + (onlyWarn ? ' warn' : '');
        const lis = (data.issues || []).map(i => `<li>${ADMIN_KEY_LABELS[i.key] || i.key}：${i.message}</li>`).join('');
        bar.innerHTML = `<strong>${onlyWarn ? '提醒' : '凭证异常'}</strong><ul>${lis}</ul>`;
        bar.style.display = '';
    } catch (e) {
        bar.style.display = 'none';
    }
}

async function refreshCapacityData() {
    const btn = document.getElementById('refreshCapacityBtn');
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    const originalHTML = btn.innerHTML;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="animation:spin 1s linear infinite"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg> 刷新中...';
    try {
        const r = await apiFetch(`${API_BASE}/api/refresh-capacity-data`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await r.json();
        if (data.success) {
            const after = data.after_refresh || {};
            const success = after.preloaded_models || 0;
            const total = after.total_models || 0;
            const tokenStatus = after.token_status || 'unknown';
            const errMsg = after.error_msg || '';
            if (success === 0) {
                if (tokenStatus === 'expired') {
                    showToast('产能刷新失败：腾讯AccessToken已过期，请联系管理员在后台更新Token', 'error');
                } else if (errMsg) {
                    showToast('产能刷新失败：' + errMsg, 'error');
                } else {
                    showToast('产能刷新失败：所有型号数据均为空，请检查腾讯表格权限或网络', 'error');
                }
            } else if (success < total) {
                showToast(`产能数据部分加载完成：${success}/${total} 个型号`, 'warning');
                calculateDate();
            } else {
                showToast(`产能数据刷新完成：${success}/${total} 个型号`, 'success');
                calculateDate();
            }
        } else {
            showToast('刷新失败：' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('刷新请求失败：' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalHTML;
    }
}

async function checkCacheStatus() {
    try {
        const r = await apiFetch(`${API_BASE}/api/cache-status`);
        const data = await r.json();
        if (data.success) {
            const count = data.preloaded_models_count || 0;
            const total = data.total_models || 0;
            if (count === 0) {
                showToast(`产能刷新失败：当前缓存仍为 0 个型号，请检查腾讯Token是否过期或网络是否正常`, 'error');
            } else if (count < total) {
                showToast(`产能数据部分加载完成：已缓存 ${count}/${total} 个型号`, 'warning');
            } else {
                showToast(`产能数据刷新完成：已缓存 ${count}/${total} 个型号`, 'success');
            }
        }
    } catch (e) {
        showToast('查询缓存状态失败：' + e.message, 'error');
    }
}



