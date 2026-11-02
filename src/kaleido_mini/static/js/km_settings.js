/* =============================================================================
   Kaleido Mini —— 设置页（管理员）
   安全约定：API Key 只以掩码形式存在页面上；输入框留空 = 不修改。
   ============================================================================= */
(function () {
    'use strict';
    var KM = window.KM;
    var body = document.body;

    function applyPayload(data) {
        if (!data) return;
        var key = data.key || {};
        var ds = data.deepseek || {};
        if (el('km-key-state')) {
            el('km-key-state').textContent = key.configured ? '已配置' : '未配置';
            el('km-key-state').className = key.configured ? 'km-ok' : 'km-bad';
        }
        if (el('km-key-fp')) el('km-key-fp').textContent = key.fingerprint_short || '—';
        if (el('km-key-bits')) el('km-key-bits').textContent = key.rsa_bits || '—';
        var dl = document.querySelector('[data-act="key-download"]');
        if (dl) dl.disabled = !key.configured;

        if (el('km-ai-enabled')) el('km-ai-enabled').checked = !!ds.enabled;
        if (el('km-ai-model')) el('km-ai-model').value = ds.model || 'deepseek-chat';
        if (el('km-ai-base')) el('km-ai-base').value = ds.base_url || 'https://api.deepseek.com';
        if (el('km-ai-timeout')) el('km-ai-timeout').value = ds.timeout || 30;
        if (el('km-ai-key')) {
            el('km-ai-key').value = '';
            el('km-ai-key').placeholder = ds.configured
                ? (ds.api_key_masked + '（留空则不修改）')
                : 'sk-…';
        }
        if (el('km-ext-media')) el('km-ext-media').checked = !!data.allow_external_media;
    }

    function el(id) { return document.getElementById(id); }

    function collect() {
        var payload = {
            allow_external_media: el('km-ext-media') ? el('km-ext-media').checked : true,
            deepseek: {
                enabled: el('km-ai-enabled') ? el('km-ai-enabled').checked : false,
                model: el('km-ai-model') ? el('km-ai-model').value.trim() : '',
                base_url: el('km-ai-base') ? el('km-ai-base').value.trim() : '',
                timeout: el('km-ai-timeout') ? parseInt(el('km-ai-timeout').value, 10) || 30 : 30
            }
        };
        var keyInput = el('km-ai-key');
        var clearBox = el('km-ai-key-clear');
        if (clearBox && clearBox.checked) {
            payload.deepseek.api_key = '__CLEAR__';
        } else if (keyInput && keyInput.value.trim()) {
            payload.deepseek.api_key = keyInput.value.trim();
        } else {
            payload.deepseek.api_key = '__KEEP__';
        }
        return payload;
    }

    function save() {
        var btn = el('km-save');
        btn.disabled = true;
        KM.post('/settings', collect()).then(function (res) {
            if (res.code === 200) {
                applyPayload(res.data);
                if (el('km-ai-key-clear')) el('km-ai-key-clear').checked = false;
                KM.toast('设置已保存', 'ok');
                if (el('km-save-hint')) el('km-save-hint').textContent = '';
            } else {
                KM.toast(KM.errText(res), 'error');
            }
        }).catch(function (e) {
            KM.toast('保存失败：' + e.message, 'error');
        }).then(function () { btn.disabled = false; });
    }

    function testAi() {
        var btn = document.querySelector('[data-act="ai-test"]');
        var out = el('km-ai-test-box');
        var hint = el('km-ai-test-result');
        btn.disabled = true;
        if (hint) hint.textContent = '测试中…';
        KM.post('/settings/test-ai', {}).then(function (res) {
            if (out) { out.hidden = false; }
            if (res.code === 200) {
                var d = res.data || {};
                if (hint) hint.textContent = '';
                if (out) {
                    out.innerHTML = '<p class="km-ok"><b>连通正常</b></p>'
                        + '<p>模型：<code>' + KM.esc(d.model || '') + '</code></p>'
                        + '<p>自检评分：<b>' + KM.esc(String(d.score)) + '</b> 分（'
                        + KM.esc(d.verdict || '') + '）</p>'
                        + '<p class="km-muted km-small">点评：' + KM.esc(d.comment || '—') + '</p>';
                }
                KM.toast('AI 连通正常', 'ok');
            } else {
                if (hint) hint.textContent = '';
                if (out) {
                    out.innerHTML = '<p class="km-bad"><b>测试失败</b></p><p>'
                        + KM.esc(KM.errText(res)) + '</p>';
                }
                KM.toast(KM.errText(res), 'error');
            }
        }).catch(function (e) {
            if (hint) hint.textContent = '';
            KM.toast('测试失败：' + e.message, 'error');
        }).then(function () { btn.disabled = false; });
    }

    function uploadKey(file) {
        if (!file) return;
        var fd = new FormData();
        fd.append('file', file);
        KM.request('POST', '/crypto/key', { formData: fd }).then(function (res) {
            if (res.code === 200) {
                KM.toast('密钥已导入并生效', 'ok');
                setTimeout(function () { location.reload(); }, 700);
            } else {
                KM.toast(KM.errText(res), 'error');
            }
        }).catch(function (e) { KM.toast('导入失败：' + e.message, 'error'); });
    }

    function generateKey() {
        if (!window.confirm('将按 Kaleido 原有算法生成一套新密钥。\n'
            + '注意：覆盖后，用旧密钥加密的题库与导出包将无法解开。继续？')) return;
        KM.post('/crypto/key', { generate: true, overwrite: true }).then(function (res) {
            if (res.code === 200) {
                KM.toast(res.message + ' —— 请立即「下载密钥」备份', 'ok');
                setTimeout(function () { location.reload(); }, 900);
            } else {
                KM.toast(KM.errText(res), 'error');
            }
        }).catch(function (e) { KM.toast('生成失败：' + e.message, 'error'); });
    }

    function downloadKey() {
        if (!window.confirm('即将下载母密钥文件。\n'
            + '它可解开加密题库与登录凭据配置，请妥善保管、勿经不安全渠道传输。继续？')) return;
        // 用同名 <a> 直接触发浏览器下载（附带会话 Cookie）
        var a = document.createElement('a');
        a.href = KM.apiBase + '/crypto/key/download';
        a.download = 'kaleido_mini_key_obj.json';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        KM.toast('已开始下载密钥文件', 'ok');
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (el('km-save')) el('km-save').addEventListener('click', save);
        var t = document.querySelector('[data-act="ai-test"]');
        if (t) t.addEventListener('click', testAi);

        var up = document.querySelector('[data-act="key-upload"]');
        var fileInput = el('km-key-file');
        if (up && fileInput) {
            up.addEventListener('click', function () { fileInput.click(); });
            fileInput.addEventListener('change', function () {
                uploadKey(fileInput.files && fileInput.files[0]);
                fileInput.value = '';
            });
        }
        var gen = document.querySelector('[data-act="key-generate"]');
        if (gen) gen.addEventListener('click', generateKey);
        var dl = document.querySelector('[data-act="key-download"]');
        if (dl) dl.addEventListener('click', downloadKey);

        // 改动即提示未保存
        ['km-ai-enabled', 'km-ai-model', 'km-ai-base', 'km-ai-timeout',
         'km-ai-key', 'km-ai-key-clear', 'km-ext-media'].forEach(function (id) {
            var node = el(id);
            if (node) {
                node.addEventListener('change', function () {
                    if (el('km-save-hint')) el('km-save-hint').textContent = '有未保存的改动';
                });
            }
        });
    });
})();
