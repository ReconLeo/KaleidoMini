/* =============================================================================
   Kaleido Mini —— 练习页（自测）
   ---------------------------------------------------------------------------
   关键约定：
   * 练习载荷 [不含答案]；提交/看答案时才按题向后端取答案与解析。
   * 判分规则只在服务端（km_parser.grade_item 单一真源），前端不复刻。
   * 「显示答案」开关 = 进入每题都展示答案，但 [不计入战绩]（仅预览）；
     「看答案」按钮 = 显式揭晓，会记为 revealed 并进入错题汇总。
   ============================================================================= */
(function () {
    'use strict';
    var KM = window.KM;
    var body = document.body;
    var SUBJECT = body.dataset.subject || '';
    var FILENAME = body.dataset.filename || '';
    var BANK_TITLE = body.dataset.bankTitle || '';
    var STORE_KEY = 'km:practice:' + SUBJECT + '/' + FILENAME;

    var VERDICT_LABEL = {
        correct: '回答正确',
        wrong: '回答错误',
        manual: '待自评',
        revealed: '已看答案'
    };

    // ------------------------------------------------------------------ 状态
    var S = {
        payload: null,
        order: [],          // 本次练习的 item_id 顺序
        byId: {},
        index: 0,
        records: {},        // item_id -> {answer, verdict, result, elapsedMs, at}
        peeks: {},          // item_id -> result（「显示答案」预览，不计入战绩）
        showAnswers: false,
        chapters: null,     // null=全部
        startedAt: 0,
        questionStart: 0,
        finished: false,
        aiAvailable: false      // 由 /meta 探测（未配置时按钮显示为不可用）
    };

    function apiPath(suffix) {
        return '/qbanks/' + KM.enc(SUBJECT) + '/' + KM.enc(FILENAME) + suffix;
    }

    function el(id) { return document.getElementById(id); }
    function current() {
        var id = S.order[S.index];
        return id ? S.byId[id] : null;
    }

    // -------------------------------------------------------------- 本地存储
    function persist() {
        try {
            localStorage.setItem(STORE_KEY, JSON.stringify({
                order: S.order,
                index: S.index,
                records: S.records,
                chapters: S.chapters,
                startedAt: S.startedAt,
                finished: S.finished,
                savedAt: new Date().toISOString()
            }));
        } catch (e) { /* 隐私模式/配额满：忽略，不影响练习 */ }
    }

    function loadSaved() {
        try {
            var raw = localStorage.getItem(STORE_KEY);
            if (!raw) return null;
            var data = JSON.parse(raw);
            if (!data || !Array.isArray(data.order) || !data.order.length) return null;
            return data;
        } catch (e) { return null; }
    }

    function clearSaved() {
        try { localStorage.removeItem(STORE_KEY); } catch (e) {}
    }

    // ---------------------------------------------------------------- 渲染
    function verdictClass(verdict) {
        return verdict === 'correct' ? 'km-dot-ok'
            : verdict === 'wrong' ? 'km-dot-bad'
            : verdict === 'manual' ? 'km-dot-manual'
            : verdict === 'revealed' ? 'km-dot-seen'
            : 'km-dot-none';
    }

    function renderQuestion() {
        var item = current();
        if (!item) return;
        S.questionStart = Date.now();

        // 元信息
        var meta = [];
        meta.push('第 <b>' + (S.index + 1) + '</b> / ' + S.order.length + ' 题');
        meta.push('<span class="km-tag">' + KM.esc(item.type_label) + '</span>');
        meta.push('<span class="km-muted">' + KM.esc(item.chapter_name) + '</span>');
        if (item.group_size > 1) {
            meta.push('<span class="km-tag">共用题干组 ' + item.group_index + '/' + item.group_size + '</span>');
        }
        if (!item.skippable) meta.push('<span class="km-warn km-small">必答（不可跳过）</span>');
        if (item.missing_media && item.missing_media.length) {
            meta.push('<span class="km-warn km-small">含 ' + item.missing_media.length + ' 个缺失媒体</span>');
        }
        el('km-q-meta').innerHTML = meta.join(' · ');

        // 共用题干
        var shared = el('km-q-shared');
        if (item.shared_stem_html) {
            shared.innerHTML = item.shared_stem_html;
            shared.hidden = false;
        } else {
            shared.innerHTML = '';
            shared.hidden = true;
        }

        // 题干
        el('km-q-stem').innerHTML = item.stem_html || '<span class="km-muted">（题干为空）</span>';

        // 作答控件
        el('km-q-answers').innerHTML = buildAnswerControls(item);

        // 题号按钮状态
        el('km-prev').disabled = S.index <= 0;
        el('km-next').disabled = S.index >= S.order.length - 1;

        // 结果区
        var rec = S.records[item.item_id];
        if (rec && rec.result) {
            renderResult(rec.result, true);
        } else if (S.showAnswers) {
            peek(item);
        } else {
            hideResult();
        }

        refreshChrome();
    }

    function buildAnswerControls(item) {
        var dt = item.display_type;
        var opts = item.options || [];
        var rec = S.records[item.item_id];
        var prev = rec ? rec.answer : null;      // 规范键（canonical）
        var h = '';

        if (dt === 'single' || dt === 'multiple') {
            var inputType = dt === 'multiple' ? 'checkbox' : 'radio';
            h += '<div class="km-opts" role="group">';
            opts.forEach(function (opt) {
                var checked = dt === 'multiple'
                    ? (Array.isArray(prev) && prev.indexOf(opt.canonical) >= 0)
                    : (prev === opt.canonical);
                h += '<label class="km-opt' + (checked ? ' km-opt-on' : '') + '">'
                    + '<input type="' + inputType + '" name="km-opt" value="' + KM.esc(opt.canonical) + '"'
                    + (checked ? ' checked' : '') + '>'
                    + '<span class="km-opt-key">' + KM.esc(opt.key) + '</span>'
                    + '<span class="km-opt-text">' + (opt.html || '') + '</span>'
                    + '</label>';
            });
            h += '</div>';
            if (dt === 'multiple') {
                h += '<p class="km-muted km-small">多选题：需与正确答案完全一致才算对。</p>';
            }
            if (opts.length > 26) {
                h += '<p class="km-muted km-small">选项超过 26 个，A–Z 快捷键只覆盖前 26 项，其余请直接点击。</p>';
            }
        } else if (dt === 'judge') {
            h += '<div class="km-opts km-opts-judge">';
            [['true', '正确', prev === true], ['false', '错误', prev === false]].forEach(function (row) {
                h += '<label class="km-opt' + (row[2] ? ' km-opt-on' : '') + '">'
                    + '<input type="radio" name="km-judge" value="' + row[0] + '"'
                    + (row[2] ? ' checked' : '') + '>'
                    + '<span class="km-opt-text">' + row[1] + '</span></label>';
            });
            h += '</div>';
        } else if (dt === 'gap') {
            h += '<label class="km-gap">填空答案'
                + '<input type="text" name="km-gap" autocomplete="off"'
                + ' value="' + KM.esc(prev || '') + '"'
                + ' placeholder="多个空用「 ｜ 」或空格分隔"></label>';
            h += '<p class="km-muted km-small">判分忽略大小写、全半角与空白差异。</p>';
        } else {
            h += '<label class="km-gap">简答'
                + '<textarea name="km-essay" rows="5" placeholder="写下你的答案，提交后可自评（M5 起支持 AI 评卷）">'
                + KM.esc(prev || '') + '</textarea></label>';
            h += '<p class="km-muted km-small">简答题不自动判分：提交后对照参考答案自评。</p>';
        }
        return h;
    }

    /** 规范键 → 当前展示标签（随机化后两者不同） */
    function canonicalToDisplay(item, keys) {
        if (!item || keys === null || keys === undefined) return keys;
        var dt = item.display_type;
        if (dt !== 'single' && dt !== 'multiple') return keys;
        var arr = Array.isArray(keys) ? keys : [keys];
        var opts = item.options || [];
        return arr.map(function (k) {
            for (var i = 0; i < opts.length; i++) {
                if (opts[i].canonical === k) return opts[i].key;
            }
            return k;
        });
    }

    function readAnswer(item) {
        var dt = item.display_type;
        if (dt === 'single') {
            var r = document.querySelector('input[name="km-opt"]:checked');
            return r ? r.value : null;
        }
        if (dt === 'multiple') {
            var picked = [].map.call(
                document.querySelectorAll('input[name="km-opt"]:checked'),
                function (n) { return n.value; });
            return picked.length ? picked : null;
        }
        if (dt === 'judge') {
            var j = document.querySelector('input[name="km-judge"]:checked');
            return j ? (j.value === 'true') : null;
        }
        if (dt === 'gap') {
            var g = document.querySelector('input[name="km-gap"]');
            var v = g ? g.value.trim() : '';
            return v ? v : null;
        }
        var t = document.querySelector('textarea[name="km-essay"]');
        var tv = t ? t.value.trim() : '';
        return tv ? tv : null;
    }

    function hideResult() {
        var box = el('km-result');
        box.hidden = true;
        box.innerHTML = '';
        box.className = 'km-result';
    }

    function renderResult(r, fromCache) {
        var box = el('km-result');
        var item = current();
        // verdict 的 [唯一真源] 是作答记录：自评、AI 评卷都会改写它，
        // 而 r.verdict 是当时那次请求的快照，可能已过期。
        var rec = item ? S.records[item.item_id] : null;
        var verdict = (rec && rec.verdict) || r.verdict;
        var cls = verdict === 'correct' ? 'km-result-ok'
            : verdict === 'wrong' ? 'km-result-bad'
            : verdict === 'manual' ? 'km-result-manual' : 'km-result-seen';
        var isChoice = item && (item.display_type === 'single' || item.display_type === 'multiple');
        // 填空/简答：AI 评卷与人工覆盖都适用
        var isManual = item && (item.display_type === 'gap' || item.display_type === 'essay');
        var parts = [];
        parts.push('<div class="km-result-head km-' + verdict + '">'
            + KM.esc(VERDICT_LABEL[verdict] || verdict) + '</div>');

        // 答案一律按 [当前展示标签] 呈现（随机化后规范键与展示标签不同）
        var mineText = r.user_display || '';
        var correctText = r.correct_display || '';
        var detailText = r.detail || '';
        if (isChoice) {
            var mineKeys = canonicalToDisplay(item, r.user_canonical);
            var expKeys = canonicalToDisplay(item, r.correct);
            if (Array.isArray(mineKeys)) mineText = mineKeys.join(' ');
            if (Array.isArray(expKeys)) correctText = expKeys.join(' ');
            if (r.verdict === 'wrong') {
                detailText = '期望 ' + (correctText || '—')
                    + '，实际 ' + (mineText || '未作答');
            } else {
                detailText = '';
            }
        }

        if (mineText) {
            parts.push('<p class="km-result-row"><span class="km-result-k">你的答案</span>'
                + '<span>' + KM.esc(mineText) + '</span></p>');
        }
        parts.push('<p class="km-result-row"><span class="km-result-k">正确答案</span>'
            + '<span class="km-result-ok-text">' + KM.esc(correctText) + '</span></p>');
        if (detailText) {
            parts.push('<p class="km-muted km-small">' + KM.esc(detailText) + '</p>');
        }
        if (r.analysis_html) {
            parts.push('<div class="km-analysis"><span class="km-result-k">解析</span>'
                + r.analysis_html + '</div>');
        } else {
            parts.push('<p class="km-muted km-small">（无解析）</p>');
        }
        if (r.missing_media && r.missing_media.length) {
            parts.push('<p class="km-warn km-small">解析中含缺失媒体：'
                + r.missing_media.map(function (m) { return '<code>' + KM.esc(m) + '</code>'; }).join(' ')
                + '</p>');
        }
        // 填空/简答始终保留人工覆盖按钮：AI 结论只是建议，最终以用户为准
        if (isManual) {
            var labelTxt = (verdict === 'manual') ? '对照参考答案自评：' : '手动覆盖判定：';
            parts.push('<div class="km-self-assess">'
                + '<span class="km-muted km-small">' + labelTxt + '</span>'
                + '<button type="button" class="km-btn' + (verdict === 'correct' ? ' km-btn-primary' : '')
                + '" data-act="self-ok">我答对了</button>'
                + '<button type="button" class="km-btn' + (verdict === 'wrong' ? ' km-btn-primary' : '')
                + '" data-act="self-bad">我答错了</button>'
                + '</div>');
        }

        // AI 评卷：按钮 + 已有结果
        if (isManual) {
            var rec = S.records[item.item_id];
            var ai = rec ? rec.ai : null;
            if (ai) {
                var verdictTxt = ai.verdict === 'correct' ? '正确'
                    : ai.verdict === 'partial' ? '部分正确' : '不正确';
                parts.push('<div class="km-ai-block">'
                    + '<div class="km-ai-head">AI 评卷 · <b>' + KM.esc(verdictTxt) + '</b>'
                    + (ai.score === null || ai.score === undefined ? '' : ' · ' + KM.esc(String(ai.score)) + ' 分')
                    + '<span class="km-muted km-small"> · ' + KM.esc(ai.model || '') + '</span>'
                    + '</div>'
                    + (ai.comment ? '<div class="km-ai-comment">' + KM.esc(ai.comment) + '</div>' : '')
                    + (ai.applied ? '<p class="km-muted km-small">已按 AI 结论记入战绩，可用上方按钮覆盖。</p>' : '')
                    + '<div class="km-self-assess">'
                    + '<button type="button" class="km-btn km-btn-mini" data-act="ai-grade">重新评卷</button>'
                    + '</div>'
                    + '</div>');
            } else {
                var disabled = !S.aiAvailable;
                parts.push('<div class="km-ai-block">'
                    + '<button type="button" class="km-btn' + (disabled ? '' : ' km-btn-primary') + '"'
                    + ' data-act="ai-grade"' + (disabled ? ' disabled' : '') + '>'
                    + (disabled ? 'AI 评卷（未配置）' : 'AI 评卷') + '</button>'
                    + '<span class="km-muted km-small"> '
                    + (disabled ? '请在设置页配置 DeepSeek API Key（仅管理员可见）'
                                : '把题干与参考答案发给 DeepSeek 评分，可再手动覆盖')
                    + '</span></div>');
            }
        }

        box.className = 'km-result ' + cls;
        box.innerHTML = parts.join('');
        box.hidden = false;
        box.dataset.fromCache = fromCache ? '1' : '';
    }

    /** AI 评卷（填空/简答）：结果记入 records，manual 状态时按建议值落定 */
    function aiGrade() {
        var item = current();
        if (!item) return;
        var rec = S.records[item.item_id];
        var answer = rec ? rec.answer : readAnswer(item);
        if (answer === null || answer === undefined || answer === '') {
            KM.toast('请先作答再评卷', 'error');
            return;
        }
        var btn = el('km-result').querySelector('[data-act="ai-grade"]');
        var label = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = '评卷中…'; }

        KM.post(apiPath('/grade-ai'), { item_id: item.item_id, answer: answer })
            .then(function (res) {
                if (res.code !== 200) {
                    KM.toast(KM.errText(res), 'error');
                    return;
                }
                var d = res.data || {};
                var r2 = S.records[item.item_id] || {};
                r2.answer = answer;
                r2.ai = {
                    score: d.score,
                    verdict: d.verdict,
                    comment: d.comment,
                    model: d.model,
                    suggested: d.suggested_verdict
                };
                // 尚未自评时按 AI 建议落定（用户仍可覆盖）
                if ((!r2.verdict || r2.verdict === 'manual') && d.suggested_verdict) {
                    r2.verdict = d.suggested_verdict;
                    r2.aiApplied = true;
                    r2.ai.applied = true;
                }
                if (!r2.result) {
                    r2.result = d;      // 至少保证有结果对象可渲染
                }
                S.records[item.item_id] = r2;
                renderResult(r2.result, false);
                refreshChrome();
                persist();
                KM.toast('AI 评卷完成'
                    + (d.score === null || d.score === undefined ? '' : '：' + d.score + ' 分'), 'ok');
            })
            .catch(function (e) {
                KM.toast('AI 评卷失败：' + e.message, 'error');
            })
            .then(function () {
                if (btn) { btn.disabled = false; btn.textContent = label; }
            });
    }

    // ------------------------------------------------------------ 请求动作
    function startSession(chapters, mode, restore) {
        S.chapters = chapters;
        var qs = [];
        if (chapters && chapters.length) qs.push('chapters=' + chapters.join(','));
        var shuffleBox = el('km-shuffle-options');
        var shuffleOpts = shuffleBox ? shuffleBox.checked : true;
        qs.push('shuffle_options=' + (shuffleOpts ? '1' : '0'));
        var qsStr = '?' + qs.join('&');
        el('km-start').classList.add('km-loading');
        KM.get(apiPath('/practice' + qsStr)).then(function (res) {
            el('km-start').classList.remove('km-loading');
            if (res.code !== 200) { KM.toast(KM.errText(res), 'error'); return; }
            var p = res.data;
            if (!p.items || !p.items.length) { KM.toast('该范围内没有可练习题', 'error'); return; }

            S.payload = p;
            S.byId = {};
            p.items.forEach(function (it) { S.byId[it.item_id] = it; });
            S.records = {};
            S.peeks = {};
            S.startedAt = Date.now();
            S.finished = false;
            S.index = 0;
            S.order = buildOrder(p.items, mode);

            if (restore) {
                // 只恢复仍存在的条目（题库可能被替换过）
                var keep = restore.order.filter(function (id) { return S.byId[id]; });
                if (!keep.length) {
                    KM.toast('上次进度与当前题库不匹配，已重新开始', 'error');
                } else {
                    S.order = keep;
                    S.records = {};
                    Object.keys(restore.records || {}).forEach(function (id) {
                        if (S.byId[id]) S.records[id] = restore.records[id];
                    });
                    S.index = Math.min(Math.max(restore.index || 0, 0), S.order.length - 1);
                    S.startedAt = restore.startedAt || Date.now();
                }
            }

            el('km-start').hidden = true;
            el('km-session').hidden = false;
            el('km-exam-actions').hidden = false;
            buildProgressBar();
            renderQuestion();
            persist();
            KM.toast('共 ' + S.order.length + ' 题，开始吧', 'ok');
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }).catch(function (e) {
            el('km-start').classList.remove('km-loading');
            KM.toast('加载失败：' + e.message, 'error');
        });
    }

    /** 乱序：按题组整体打乱，保证共用题干组内子题仍相邻 */
    function buildOrder(items, mode) {
        if (mode !== 'shuffle') return items.map(function (i) { return i.item_id; });
        var groups = [], indexOf = {};
        items.forEach(function (it) {
            var key = (it.group_size > 1 && it.group_id) ? it.group_id : ('#' + it.item_id);
            if (!(key in indexOf)) { indexOf[key] = groups.length; groups.push([]); }
            groups[indexOf[key]].push(it.item_id);
        });
        for (var i = groups.length - 1; i > 0; i--) {
            var j = Math.floor(Math.random() * (i + 1));
            var tmp = groups[i]; groups[i] = groups[j]; groups[j] = tmp;
        }
        return [].concat.apply([], groups);
    }

    function submitAnswer(reveal) {
        var item = current();
        if (!item) return;
        var answer = reveal ? null : readAnswer(item);
        if (!reveal) {
            var empty = (answer === null) || (Array.isArray(answer) && !answer.length);
            if (empty) { KM.toast('请先作答；或用「看答案」/「跳过」', 'error'); return; }
        }
        var elapsed = Date.now() - (S.questionStart || Date.now());
        el('km-submit').disabled = true;
        el('km-reveal').disabled = true;
        KM.post(apiPath('/grade'), {
            item_id: item.item_id, answer: answer, reveal: !!reveal
        }).then(function (res) {
            if (res.code !== 200) { KM.toast(KM.errText(res), 'error'); return; }
            var r = res.data;
            var rec = S.records[item.item_id] || {};
            rec.answer = answer;
            rec.verdict = r.verdict;
            rec.result = r;
            rec.elapsedMs = (rec.elapsedMs || 0) + elapsed;
            rec.at = new Date().toISOString();
            S.records[item.item_id] = rec;
            delete S.peeks[item.item_id];
            renderResult(r, false);
            refreshChrome();
            persist();
        }).catch(function (e) {
            KM.toast('提交失败：' + e.message, 'error');
        }).then(function () {
            el('km-submit').disabled = false;
            el('km-reveal').disabled = false;
        });
    }

    /** 「显示答案」开关下的预览：只读展示，不写入战绩 */
    function peek(item) {
        if (S.peeks[item.item_id]) { renderResult(S.peeks[item.item_id], true); return; }
        KM.post(apiPath('/grade'), { item_id: item.item_id, reveal: true })
            .then(function (res) {
                if (res.code !== 200) { hideResult(); return; }
                S.peeks[item.item_id] = res.data;
                if (current() && current().item_id === item.item_id) {
                    renderResult(res.data, true);
                }
            }).catch(function () { hideResult(); });
    }

    function selfAssess(ok) {
        var item = current();
        var rec = item ? S.records[item.item_id] : null;
        if (!rec || !rec.result) { KM.toast('请先提交再自评', 'error'); return; }
        rec.verdict = ok ? 'correct' : 'wrong';
        rec.selfAssessed = true;
        rec.result.verdict = rec.verdict;
        renderResult(rec.result, false);
        refreshChrome();
        persist();
        KM.toast(ok ? '已记为答对' : '已记为答错', 'ok');
    }

    function goto(index) {
        if (index < 0 || index >= S.order.length) return;
        S.index = index;
        renderQuestion();
        persist();
        el('km-sheet').hidden = true;
    }

    function nextQuestion() {
        if (S.index >= S.order.length - 1) {
            KM.toast('已是最后一题，点「结束并汇总」查看战绩');
            return;
        }
        goto(S.index + 1);
    }

    // ---------------------------------------------------------- 进度与答题卡
    function verdictOf(itemId) {
        var rec = S.records[itemId];
        return rec ? rec.verdict : null;
    }

    function refreshChrome() {
        var total = S.order.length;
        var done = 0, ok = 0, bad = 0, manual = 0, seen = 0;
        S.order.forEach(function (id) {
            var v = verdictOf(id);
            if (!v) return;
            done++;
            if (v === 'correct') ok++;
            else if (v === 'wrong') bad++;
            else if (v === 'manual') manual++;
            else if (v === 'revealed') seen++;
        });
        el('km-progress-text').textContent = (S.index + 1) + ' / ' + total;
        el('km-progress-stat').textContent =
            '已作答 ' + done + ' · 对 ' + ok + ' · 错 ' + bad
            + (manual ? ' · 待自评 ' + manual : '')
            + (seen ? ' · 看过答案 ' + seen : '');

        var bar = el('km-progress-bar');
        if (bar.children.length !== total) {
            bar.innerHTML = '';
            for (var i = 0; i < total; i++) {
                var seg = document.createElement('i');
                seg.className = 'km-seg';
                bar.appendChild(seg);
            }
        }
        for (var k = 0; k < total; k++) {
            var segEl = bar.children[k];
            var v2 = verdictOf(S.order[k]);
            segEl.className = 'km-seg'
                + (v2 ? ' km-seg-' + v2 : '')
                + (k === S.index ? ' km-seg-cur' : '');
        }

        var skipBtn = el('km-skip');
        var item = current();
        skipBtn.disabled = !!(item && !item.skippable);

        var toggle = el('km-toggle-answers');
        toggle.setAttribute('aria-pressed', S.showAnswers ? 'true' : 'false');
        toggle.textContent = S.showAnswers ? '隐藏答案' : '显示答案';
    }

    function buildProgressBar() {
        el('km-progress-bar').innerHTML = '';
    }

    function renderSheet() {
        var grid = el('km-sheet-grid');
        var html = [];
        S.order.forEach(function (id, i) {
            var v = verdictOf(id);
            var item = S.byId[id];
            var title = item ? (item.type_label + ' · ' + item.chapter_name) : '';
            html.push('<button type="button" class="km-sheet-cell ' + verdictClass(v)
                + (i === S.index ? ' km-sheet-cur' : '') + '"'
                + ' data-goto="' + i + '" title="' + KM.esc(title) + '">'
                + (i + 1) + '</button>');
        });
        grid.innerHTML = html.join('');
    }

    // -------------------------------------------------------------- 汇总
    function openSummary() {
        var payload = {};
        Object.keys(S.records).forEach(function (id) {
            payload[id] = {
                verdict: S.records[id].verdict,
                answer: S.records[id].answer,
                elapsed_ms: S.records[id].elapsedMs
            };
        });
        KM.post(apiPath('/summary'), {
            records: payload,
            chapters: S.chapters || []
        }).then(function (res) {
            if (res.code !== 200) { KM.toast(KM.errText(res), 'error'); return; }
            renderSummary(res.data);
        }).catch(function (e) {
            KM.toast('汇总失败：' + e.message, 'error');
        });
    }

    function renderSummary(d) {
        S.finished = true;
        persist();
        var c = d.counts || {};
        var acc = (d.accuracy === null || d.accuracy === undefined)
            ? '—' : (Math.round(d.accuracy * 1000) / 10 + '%');

        var html = [];
        html.push('<div class="km-stats km-stats-drawer">');
        [['题数', d.total], ['已作答', d.answered], ['正确', c.correct || 0],
         ['错误', c.wrong || 0], ['待自评', c.manual || 0], ['看过答案', c.revealed || 0],
         ['正确率', acc]].forEach(function (row) {
            html.push('<div class="km-stat"><b>' + row[1] + '</b><span>' + row[0] + '</span></div>');
        });
        html.push('</div>');
        if (d.unanswered) {
            html.push('<p class="km-muted km-small">还有 ' + d.unanswered + ' 题未作答。</p>');
        }

        if (d.by_type && Object.keys(d.by_type).length) {
            html.push('<h3>分题型</h3><table class="km-table km-small"><thead><tr>'
                + '<th>题型</th><th>对</th><th>错</th><th>待自评</th><th>看答案</th></tr></thead><tbody>');
            Object.keys(d.by_type).forEach(function (k) {
                var s = d.by_type[k];
                html.push('<tr><td><code>' + KM.esc(k) + '</code></td><td>' + (s.correct || 0)
                    + '</td><td>' + (s.wrong || 0) + '</td><td>' + (s.manual || 0)
                    + '</td><td>' + (s.revealed || 0) + '</td></tr>');
            });
            html.push('</tbody></table>');
        }

        var wrong = d.wrong_items || [];
        html.push('<h3>错题（' + wrong.length + '）</h3>');
        if (!wrong.length) {
            html.push('<p class="km-ok">没有错题 🎉</p>');
        } else {
            html.push('<ul class="km-list km-small">');
            wrong.slice(0, 200).forEach(function (w) {
                var idx = S.order.indexOf(w.item_id);
                html.push('<li><code>' + KM.esc(w.qtype) + '</code> · '
                    + KM.esc(w.chapter_name) + ' · '
                    + KM.esc(w.item_id)
                    + (idx >= 0 ? ' <button type="button" class="km-btn km-btn-mini" data-goto="' + idx + '">查看</button>' : '')
                    + '</li>');
            });
            if (wrong.length > 200) html.push('<li>… 其余 ' + (wrong.length - 200) + ' 条</li>');
            html.push('</ul>');
        }

        html.push('<div class="km-dialog-actions">');
        if (wrong.length) {
            html.push('<button type="button" class="km-btn" data-act="redo-wrong">只练错题（' + wrong.length + '）</button>');
            html.push('<button type="button" class="km-btn km-btn-primary" data-act="export-wrong">'
                + '导出错题（题库 + 错题本）</button>');
        }
        html.push('<button type="button" class="km-btn km-btn-danger" data-act="restart">从头重做</button>');
        html.push('</div>');
        if (wrong.length) {
            html.push('<p class="km-muted km-small">导出内容：v3.1.0 题库（有图片等媒体时自动打成 .kqb 整包）'
                + ' + Markdown 错题本 + 自包含 HTML。整包默认自包含密钥，可直接再导入。</p>');
        }

        el('km-summary-body').innerHTML = html.join('');
        el('km-summary').hidden = false;
    }

    // -------------------------------------------------------------- 导出（M4）
    /** 从 Content-Disposition 解析文件名（优先 RFC 5987 的 filename*） */
    function filenameFromDisposition(cd) {
        if (!cd) return '';
        var m = /filename\*=\s*UTF-8''([^;]+)/i.exec(cd);
        if (m) {
            try { return decodeURIComponent(m[1].trim()); } catch (e) { return m[1].trim(); }
        }
        m = /filename=\s*"([^"]+)"/i.exec(cd) || /filename=\s*([^;]+)/i.exec(cd);
        return m ? m[1].trim() : '';
    }

    function saveBlob(blob, filename) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
    }

    function exportWrong() {
        var records = {};
        Object.keys(S.records).forEach(function (id) {
            var r = S.records[id];
            if (r.verdict === 'wrong' || r.verdict === 'revealed') {
                records[id] = { verdict: r.verdict, answer: r.answer };
            }
        });
        var count = Object.keys(records).length;
        if (!count) { KM.toast('没有错题可导出', 'error'); return; }

        var btn = el('km-summary').querySelector('[data-act="export-wrong"]');
        var label = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = '导出中…'; }

        // 注意：必须走 KM.requestRaw —— 它负责拼 API 前缀；
        // 若直接用裸 fetch 加相对路径，请求会打到站点根（缺 /api/<name>）而 404。
        KM.requestRaw('POST', apiPath('/export'), {
            json: {
                scope: 'wrong',
                records: records,
                pack: 'auto',                 // 有 /media/ 引用则自动出 .kqb
                encrypt: true,
                placement: 'self_contained',
                extras: ['md', 'html']
            }
        }).then(function (resp) {
            var ct = resp.headers.get('Content-Type') || '';
            if (ct.indexOf('application/json') !== -1) {
                return resp.json().then(function (d) { KM.toast(KM.errText(d), 'error'); });
            }
            if (!resp.ok) { KM.toast('导出失败：HTTP ' + resp.status, 'error'); return; }
            var name = filenameFromDisposition(resp.headers.get('Content-Disposition'))
                || ('错题导出_' + count + '.zip');
            return resp.blob().then(function (blob) {
                saveBlob(blob, name);
                KM.toast('已开始下载 ' + name + '（' + count + ' 道错题）', 'ok');
            });
        }).catch(function (e) {
            KM.toast('导出失败：' + e.message, 'error');
        }).then(function () {
            if (btn) { btn.disabled = false; btn.textContent = label; }
        });
    }

    function redoWrong() {
        var ids = S.order.filter(function (id) {
            var v = verdictOf(id);
            return v === 'wrong' || v === 'revealed';
        });
        if (!ids.length) { KM.toast('没有错题可练', 'error'); return; }
        S.order = ids;
        S.records = {};
        S.peeks = {};
        S.index = 0;
        S.finished = false;
        S.startedAt = Date.now();
        el('km-summary').hidden = true;
        buildProgressBar();
        renderQuestion();
        persist();
        KM.toast('已切换为只练错题（' + ids.length + ' 题）', 'ok');
    }

    function restartFromScratch() {
        if (!window.confirm('从头重做会清空本次作答记录，确定吗？')) return;
        var chapters = S.chapters;
        var mode = 'seq';
        el('km-summary').hidden = true;
        el('km-session').hidden = true;
        el('km-exam-actions').hidden = true;
        el('km-start').hidden = false;
        clearSaved();
        S.records = {}; S.peeks = {}; S.order = []; S.index = 0;
        el('km-resume').hidden = true;
        startSession(chapters, mode, null);
    }

    // -------------------------------------------------------------- 事件绑定
    function bindStartPanel() {
        var allBox = el('km-chapter-all');
        var boxes = [].slice.call(document.querySelectorAll('.km-chapter-box'));

        allBox.addEventListener('change', function () {
            boxes.forEach(function (b) { b.checked = false; });
        });
        boxes.forEach(function (b) {
            b.addEventListener('change', function () {
                if (b.checked) allBox.checked = false;
                if (!boxes.some(function (x) { return x.checked; })) allBox.checked = true;
            });
        });

        function pickedChapters() {
            if (allBox.checked) return null;
            var picked = boxes.filter(function (b) { return b.checked; })
                .map(function (b) { return parseInt(b.value, 10); });
            if (!picked.length) {
                allBox.checked = true;
                return null;
            }
            return picked;
        }

        el('km-start').addEventListener('click', function (ev) {
            var btn = ev.target.closest('[data-act]');
            if (!btn) return;
            var act = btn.dataset.act;
            if (act === 'start') {
                var mode = (document.querySelector('input[name="km-order"]:checked') || {}).value || 'seq';
                startSession(pickedChapters(), mode, null);
            } else if (act === 'resume') {
                var saved = loadSaved();
                if (!saved) { KM.toast('没有可恢复的进度', 'error'); return; }
                startSession(saved.chapters, 'seq', saved);
            }
        });
    }

    function bindSession() {
        el('km-session').addEventListener('click', function (ev) {
            var btn = ev.target.closest('[data-act]');
            if (!btn) return;
            var act = btn.dataset.act;
            if (act === 'submit') submitAnswer(false);
            else if (act === 'reveal') submitAnswer(true);
            else if (act === 'skip') { nextQuestion(); }
            else if (act === 'prev') goto(S.index - 1);
            else if (act === 'next') nextQuestion();
            else if (act === 'self-ok') selfAssess(true);
            else if (act === 'self-bad') selfAssess(false);
            else if (act === 'ai-grade') aiGrade();
        });

        // 选项高亮 + 单选自动前进与否（不自动跳，避免误触）
        el('km-q-answers').addEventListener('change', function (ev) {
            var label = ev.target.closest('.km-opt');
            if (!label) return;
            var group = label.parentElement;
            if (ev.target.type === 'radio') {
                [].forEach.call(group.querySelectorAll('.km-opt'), function (n) {
                    n.classList.remove('km-opt-on');
                });
            }
            label.classList.toggle('km-opt-on', ev.target.checked);
        });

        el('km-exam-actions').addEventListener('click', function (ev) {
            var btn = ev.target.closest('[data-act]');
            if (!btn) return;
            var act = btn.dataset.act;
            if (act === 'toggle-answers') {
                S.showAnswers = !S.showAnswers;
                refreshChrome();
                var item = current();
                if (S.showAnswers) {
                    var rec = item ? S.records[item.item_id] : null;
                    if (rec && rec.result) renderResult(rec.result, true);
                    else if (item) peek(item);
                } else if (!item || !S.records[item.item_id]) {
                    hideResult();
                }
            } else if (act === 'sheet') {
                renderSheet();
                el('km-sheet').hidden = false;
            } else if (act === 'finish') {
                openSummary();
            }
        });

        el('km-sheet').addEventListener('click', function (ev) {
            var cell = ev.target.closest('[data-goto]');
            if (cell) { goto(parseInt(cell.dataset.goto, 10)); return; }
            if (ev.target.closest('[data-act="close-sheet"]')) el('km-sheet').hidden = true;
        });

        el('km-summary').addEventListener('click', function (ev) {
            var go = ev.target.closest('[data-goto]');
            if (go) { goto(parseInt(go.dataset.goto, 10)); el('km-summary').hidden = true; return; }
            var btn = ev.target.closest('[data-act]');
            if (!btn) return;
            var act = btn.dataset.act;
            if (act === 'close-summary') el('km-summary').hidden = true;
            else if (act === 'redo-wrong') redoWrong();
            else if (act === 'restart') restartFromScratch();
            else if (act === 'export-wrong') exportWrong();
        });

        document.addEventListener('keydown', function (ev) {
            if (el('km-session').hidden) return;
            var tag = (ev.target.tagName || '').toLowerCase();
            var typing = tag === 'input' || tag === 'textarea';
            if (ev.key === 'Escape') {
                el('km-sheet').hidden = true;
                el('km-summary').hidden = true;
                return;
            }
            if (typing) {
                if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); submitAnswer(false); }
                return;
            }
            if (ev.key === 'Enter') { ev.preventDefault(); submitAnswer(false); }
            else if (ev.key === 'ArrowLeft') { ev.preventDefault(); goto(S.index - 1); }
            else if (ev.key === 'ArrowRight') { ev.preventDefault(); nextQuestion(); }
            else if (/^[a-zA-Z]$/.test(ev.key)) {
                // 快捷键按 [展示标签] 匹配（A–Z 覆盖前 26 个选项；AA+ 请点击）
                var item = current();
                if (!item || !item.options) return;
                var letter = ev.key.toUpperCase();
                var hit = null;
                for (var i = 0; i < item.options.length; i++) {
                    if (item.options[i].key === letter) { hit = item.options[i]; break; }
                }
                if (!hit) return;
                var box = document.querySelector(
                    'input[name="km-opt"][value="' + hit.canonical + '"]');
                if (box) { box.click(); }
            }
        });
    }

    // ------------------------------------------------------------------ 初始化
    document.addEventListener('DOMContentLoaded', function () {
        bindStartPanel();
        bindSession();
        // 探测 AI 评卷可用性（未配置时按钮置灰并提示去设置页）
        KM.get('/meta').then(function (res) {
            var ai = (res && res.data && res.data.ai) || {};
            S.aiAvailable = !!ai.available;
        }).catch(function () { S.aiAvailable = false; });

        var saved = loadSaved();
        if (saved) {
            var done = Object.keys(saved.records || {}).length;
            var btn = el('km-resume');
            btn.hidden = false;
            btn.textContent = '继续上次进度（已作答 ' + done + '/' + saved.order.length + '）';
        }
        if (body.dataset.mediaMissing && body.dataset.mediaMissing !== '0') {
            KM.toast('本题库有 ' + body.dataset.mediaMissing + ' 个媒体缺失，相关题会显示占位', 'error');
        }
    });
})();
