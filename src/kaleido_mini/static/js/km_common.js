/* =============================================================================
   Kaleido Mini —— 前端公共脚本
   依赖 /static/js/plugin_common.js（已全局包装 fetch：自动注入 X-CSRF-Token、
   401 跳登录、403 跳 403 页）。本文件只做请求封装与 UI 小工具。
   ============================================================================= */
(function () {
    'use strict';

    var body = document.body;
    var API_BASE = body.dataset.apiBase || '/api/kaleido_mini';

    /**
     * 原始请求：返回 fetch 的 Response（不做 JSON 解析），用于文件下载等场景。
     * 路径处理与 request 一致 —— [务必] 用它而不是裸 fetch(apiPath(...))：
     * apiPath 返回的是不带前缀的相对路径，裸 fetch 会打到站点根目录导致 404。
     * @param {string} method
     * @param {string} path   相对 API_BASE 的路径
     * @param {{json?: object, formData?: FormData}} [opts]
     */
    function requestRaw(method, path, opts) {
        opts = opts || {};
        var url = path.indexOf('http') === 0 ? path : API_BASE + path;
        var init = { method: method, credentials: 'include', headers: {} };
        if (opts.formData) {
            init.body = opts.formData;
        } else if (opts.json !== undefined) {
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(opts.json);
        }
        return fetch(url, init);
    }

    /**
     * 统一请求。返回后端 JSON（不抛 HTTP 4xx，交由调用方判断 res.code）。
     * @param {string} method
     * @param {string} path   相对 API_BASE 的路径（需自行 encodeURIComponent）
     * @param {{json?: object, formData?: FormData}} [opts]
     */
    function request(method, path, opts) {
        return requestRaw(method, path, opts).then(function (resp) {
            return resp.text().then(function (text) {
                var data = null;
                try { data = JSON.parse(text); } catch (e) { /* 非 JSON */ }
                if (!data) {
                    throw new Error('服务器返回非 JSON（HTTP ' + resp.status + '）' +
                        (text ? '：' + text.slice(0, 160) : ''));
                }
                return data;
            });
        });
    }

    /** 从响应里取可读错误信息 */
    function errText(res) {
        if (!res) return '未知错误';
        if (res.message) return res.message;
        if (res.error) return res.error;
        return 'HTTP ' + res.code;
    }

    var toastTimer = null;
    function toast(message, kind) {
        var el = document.getElementById('km-toast');
        if (!el) { window.alert(message); return; }
        el.textContent = message;
        el.className = 'km-toast' + (kind === 'error' ? ' km-toast-err'
            : kind === 'ok' ? ' km-toast-ok' : '');
        el.hidden = false;
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { el.hidden = true; }, kind === 'error' ? 6000 : 3200);
    }

    function enc(v) { return encodeURIComponent(String(v)); }

    /** 显示/隐藏元素 */
    function show(el, visible) { if (el) el.hidden = !visible; }

    /** 按 HTML 转义，用于插入纯文本 */
    function esc(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    window.KM = {
        apiBase: API_BASE,
        isAdmin: body.dataset.isAdmin === '1',
        request: request,
        requestRaw: requestRaw,
        errText: errText,
        toast: toast,
        enc: enc,
        show: show,
        esc: esc,
        get: function (p) { return request('GET', p); },
        post: function (p, json) { return request('POST', p, { json: json }); },
        del: function (p) { return request('DELETE', p); }
    };
})();
