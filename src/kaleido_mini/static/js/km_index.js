/* =============================================================================
   Kaleido Mini —— 题库管理页交互（主入口）
   所有写操作按钮仅在管理员渲染时存在；后端仍有 @permission_required("admin") 兜底。
   ============================================================================= */
(function () {
    'use strict';
    var KM = window.KM;
    var ADMIN = KM.isAdmin;

    function reloadSoon() { setTimeout(function () { location.reload(); }, 700); }

    // ------------------------------------------------ 对话框公共行为
    /**
     * 取消按钮一律是 type=button + [data-role=cancel]，点击只关对话框。
     * （曾经的 bug：取消按钮默认是 submit，触发表单 HTML5 必填校验，
     *   浏览器弹出 "Please fill in this field." 导致窗口关不掉。）
     */
    function bindDialogsCommon() {
        document.querySelectorAll('[data-role="cancel"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var dlg = btn.closest('dialog');
                if (dlg) dlg.close();
            });
        });
        // 打开时重置表单与上次的核对结果
        document.querySelectorAll('dialog.km-dialog').forEach(function (dlg) {
            var form = dlg.querySelector('form');
            dlg.addEventListener('close', function () {
                if (form) form.reset();
                var box = dlg.querySelector('.km-inspect-result');
                if (box) { box.hidden = true; box.innerHTML = ''; }
            });
        });
    }

    /** 取当前聚焦对话框里的表单 */
    function formOf(dlg) { return dlg ? dlg.querySelector('form') : null; }

    // ------------------------------------------------ 新建学科
    function bindNewSubject() {
        var dlg = document.getElementById('dlg-subject');
        if (!dlg) return;
        var form = formOf(dlg);
        // 用 submit 事件驱动：按钮点击与输入框回车都能触发
        form.addEventListener('submit', function (ev) {
            ev.preventDefault();
            var input = form.querySelector('input[name="name"]');
            var name = (input.value || '').trim();
            if (!name) { KM.toast('请填写学科名称', 'error'); input.focus(); return; }
            KM.post('/subjects', { name: name }).then(function (res) {
                if (res.code === 200) {
                    dlg.close();
                    KM.toast('学科「' + name + '」已创建', 'ok');
                    reloadSoon();
                } else {
                    KM.toast(KM.errText(res), 'error');
                }
            }).catch(function (e) { KM.toast('请求失败：' + e.message, 'error'); });
        });
    }

    // ------------------------------------------------ 上传 / 导入（共用）
    function bindUploadDialog(dialogId, endpoint, opts) {
        var dlg = document.getElementById(dialogId);
        if (!dlg) return;
        var form = formOf(dlg);
        var fileInput = form.querySelector('input[name="file"]');

        function buildFormData() {
            var subject = form.querySelector('select[name="subject"]').value;
            if (!subject) { KM.toast('请选择目标学科', 'error'); return null; }
            var file = fileInput.files && fileInput.files[0];
            if (!file) { KM.toast('请选择文件', 'error'); return null; }
            var fd = new FormData();
            fd.append('subject', subject);
            fd.append('file', file);
            var ow = form.querySelector('input[name="overwrite"]');
            if (ow && ow.checked) fd.append('overwrite', '1');
            return fd;
        }

        // 上传前提示大文件
        fileInput.addEventListener('change', function () {
            var f = fileInput.files && fileInput.files[0];
            if (!f) return;
            var mb = f.size / (1024 * 1024);
            KM.toast('已选择 ' + f.name + '（约 ' + mb.toFixed(1) + 'MB）');
        });

        // 「先核对」是 type=button，不参与表单提交
        var inspectBtn = dlg.querySelector('[data-role="inspect"]');
        if (inspectBtn) {
            inspectBtn.addEventListener('click', function () {
                var fd = buildFormData();
                if (!fd) return;
                inspectBtn.disabled = true;
                var label = inspectBtn.textContent;
                inspectBtn.textContent = '核对中…';
                KM.request('POST', '/packages/inspect', { formData: fd }).then(function (res) {
                    renderInspect(dlg, res);
                }).catch(function (e) {
                    KM.toast('核对失败：' + e.message, 'error');
                }).then(function () {
                    inspectBtn.disabled = false;
                    inspectBtn.textContent = label;
                });
            });
        }

        // 提交（按钮点击 / 回车）
        form.addEventListener('submit', function (ev) {
            ev.preventDefault();
            var fd = buildFormData();
            if (!fd) return;
            var btn = form.querySelector('[data-role="submit"]');
            var label = btn.textContent;
            btn.disabled = true;
            btn.textContent = '处理中…';
            KM.request('POST', endpoint, { formData: fd }).then(function (res) {
                if (res.code === 200) {
                    dlg.close();
                    KM.toast(res.message || '操作成功', 'ok');
                    reloadSoon();
                } else if (res.code === 409) {
                    if (window.confirm(KM.errText(res) + '\n\n是否覆盖同名题库？')) {
                        // FormData 已带 file，补 overwrite 后重发
                        fd.set('overwrite', '1');
                        return KM.request('POST', endpoint, { formData: fd }).then(function (r2) {
                            if (r2.code === 200) {
                                dlg.close(); KM.toast('已覆盖导入', 'ok'); reloadSoon();
                            } else { KM.toast(KM.errText(r2), 'error'); }
                        });
                    }
                } else {
                    KM.toast(KM.errText(res), 'error');
                }
            }).catch(function (e) {
                KM.toast('请求失败：' + e.message, 'error');
            }).then(function () {
                btn.disabled = false;
                btn.textContent = label;
            });
        });
    }

    function renderInspect(dlg, res) {
        var box = dlg.querySelector('#kqb-inspect');
        if (!box) return;
        KM.show(box, true);
        if (res.code !== 200 || !res.data) {
            box.innerHTML = '<b class="km-bad">核对失败：</b>' + KM.esc(KM.errText(res));
            return;
        }
        var d = res.data;
        var html = '<p><b>' + (d.ok ? '<span class="km-ok">核对通过</span>'
            : '<span class="km-bad">核对未通过</span>') + '</b>'
            + ' · 致命 ' + (d.fatal_count || 0) + ' 项 · 提示 ' + (d.warn_count || 0) + ' 项</p>';
        var bs = d.bank_summary || {};
        if (bs.item_count != null) {
            html += '<p>' + KM.esc(bs.title || '') + ' · v'
                + ((bs.format_version || []).join('.')) + ' · '
                + bs.chapter_count + ' 章 / ' + bs.item_count + ' 条 · 媒体 '
                + (d.media_names ? d.media_names.length : 0) + ' 个</p>';
        }
        html += '<ul>';
        (d.checks || []).forEach(function (c) {
            var mark = c.status === 'pass' ? '✓' : c.status === 'fail' ? '✗'
                : c.status === 'warn' ? '!' : '–';
            var cls = c.status === 'fail' ? 'km-bad' : c.status === 'warn' ? 'km-warn' : 'km-muted';
            html += '<li class="' + cls + '">[' + mark + '] ' + KM.esc(c.title)
                + (c.detail ? '：' + KM.esc(c.detail) : '') + '</li>';
        });
        html += '</ul>';
        box.innerHTML = html;
    }

    // ------------------------------------------------ 删除
    function bindDeleteSubject() {
        document.querySelectorAll('[data-act="delete-subject"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var subject = btn.dataset.subject;
                if (!window.confirm('确定删除学科「' + subject + '」？')) return;
                var path = '/subjects/' + KM.enc(subject);
                KM.del(path).then(function (res) {
                    if (res.code === 200) {
                        KM.toast('学科已删除', 'ok'); reloadSoon();
                    } else if (res.code === 409) {
                        if (window.confirm(KM.errText(res) + '\n\n确认连同其下全部题库与媒体一并删除？')) {
                            return KM.del(path + '?force=1').then(function (r2) {
                                if (r2.code === 200) {
                                    KM.toast('学科及其题库已删除', 'ok'); reloadSoon();
                                } else { KM.toast(KM.errText(r2), 'error'); }
                            });
                        }
                    } else {
                        KM.toast(KM.errText(res), 'error');
                    }
                }).catch(function (e) { KM.toast('请求失败：' + e.message, 'error'); });
            });
        });
    }

    function bindDeleteBank() {
        document.querySelectorAll('[data-act="delete-bank"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var subject = btn.dataset.subject;
                var filename = btn.dataset.filename;
                if (!window.confirm('确定删除题库「' + filename + '」？\n其同主名媒体目录会一并删除。')) return;
                KM.del('/qbanks/' + KM.enc(subject) + '/' + KM.enc(filename)).then(function (res) {
                    if (res.code === 200) {
                        var removed = (res.data && res.data.removed_media) || 0;
                        KM.toast('题库已删除' + (removed ? '（连带 ' + removed + ' 个媒体）' : ''), 'ok');
                        reloadSoon();
                    } else {
                        KM.toast(KM.errText(res), 'error');
                    }
                }).catch(function (e) { KM.toast('请求失败：' + e.message, 'error'); });
            });
        });
    }

    // ------------------------------------------------ 通用按钮
    function bindMisc() {
        document.querySelectorAll('[data-act="reload"]').forEach(function (btn) {
            btn.addEventListener('click', function () { location.reload(); });
        });
        document.querySelectorAll('[data-act="new-subject"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var dlg = document.getElementById('dlg-subject');
                if (dlg) dlg.showModal();
            });
        });
        document.querySelectorAll('[data-act="upload-json"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var dlg = document.getElementById('dlg-upload');
                if (dlg) dlg.showModal();
            });
        });
        document.querySelectorAll('[data-act="import-kqb"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var dlg = document.getElementById('dlg-kqb');
                if (dlg) dlg.showModal();
            });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        bindDialogsCommon();
        bindMisc();
        if (!ADMIN) return;
        bindNewSubject();
        bindUploadDialog('dlg-upload', '/qbanks');
        bindUploadDialog('dlg-kqb', '/packages');
        bindDeleteSubject();
        bindDeleteBank();
    });
})();
