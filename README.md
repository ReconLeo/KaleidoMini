# Kaleido Mini

[![version](https://img.shields.io/badge/version-0.3.0-blue)](CHANGELOG.md)
[![framework](https://img.shields.io/badge/FlaskToolkit-%E2%89%A5%204.21.0-green)](https://github.com/ReconLeo/FlaskToolkit)
[![tests](https://img.shields.io/badge/tests-198%20e2e%20%2B%208%20unit%20suites-brightgreen)](tests/)
[![python](https://img.shields.io/badge/python-3.10%2B-yellow)](https://www.python.org/)

FlaskToolkit 子项目插件：**Kaleido Mini** —— 只读题库 → 练习模式自测工具。

完整版 Kaleido（`H:\Coding\Projects\Kaleido\Project`）的多模块插件化精简版。

## 边界

| 做 | 不做 |
|---|---|
| 学科文件夹 → 单题库上传 / 删除 | 题库编辑、组卷 |
| 读取 v1 / v2 / v3 / v3.1.0 题库 | docx 导入 |
| 媒体托管（`/media/<name>`，含 base64 / url / filename 三模式） | 成绩库、排名 |
| `.kqb` 整包导入（含加密媒体） | 强制签名校验（既有数据大量不通过，只做提示） |
| 练习模式考试页（逐题 + 答案显隐 + 错题汇总 + 选项随机化） | |
| 错题导出为 v3.1.0 题库 / `.kqb` + Markdown / HTML 错题本 | |
| DeepSeek 智能评卷（简答 / 填空，可选） | |

## 安装

前置：FlaskToolkit ≥ 4.21.0（`auth` 插件提供登录与角色，未安装时按单用户全权处理）。

```bash
# 方式一：后台「插件管理」上传 dist/kaleido_mini-<ver>.zip

# 方式二：离线安装（不启动服务）
python <FlaskToolkit>/tools/install_plugin.py install-backend kaleido_mini-0.3.0.zip \
       --base <FlaskToolkit 根> [--update]
```

安装后访问 `/plugin/kaleido_mini`。首次使用建议进「设置」：
配置全题库密钥文件（如需读加密题库）与 DeepSeek API Key（如需 AI 评卷）。

## 状态

**M1 已完成**：加密方案移植、题库解析归一化、`.kqb` 整包读写核对。

**M2 已完成**：存储层、HTML 净化渲染、题库管理页（含 `.kqb` 导入与媒体下发）、
管理员/普通用户权限分级。

**M3 已完成**：练习页（逐题作答 / 答题卡 / 答案显隐 / 错题汇总 / 断点续做）。
练习载荷**不下发答案与解析**（626 条仅 404KB）。

**M4 已完成**：错题导出为 **v3.1.0 题库**（含相对媒体引用时自动打成 `.kqb` 整包），
可附 Markdown / 自包含 HTML 错题本；支持自包含与分离密钥。

**M5 已完成**：DeepSeek 智能评卷（简答/填空）+ 设置页（密钥文件管理、AI 配置、
外链图片策略）。

**M6 已完成**：发布收尾（版本号、变更历史、徽章、全量回归、打包、建仓发布）。

全部里程碑完成。端到端 **198 项**、单元 **8 个测试文件**全绿。
详细版本演进见 [CHANGELOG.md](CHANGELOG.md)，开发中发现的框架缺陷见
[docs/框架协调清单.md](docs/框架协调清单.md)。

## AI 评卷（可选）

| 项 | 说明 |
|---|---|
| 适用范围 | **仅简答题与填空题**；选择题/判断题走本地自动判分 |
| 触发 | 练习页提交后出现「AI 评卷」按钮（未配置时置灰并提示去设置页） |
| 最终判定 | **始终以用户为准**：AI 给出建议分后仍保留「我答对了 / 我答错了」，可随时覆盖 |
| Key 存放 | 仅本机 `plugins/configs/kaleido_mini.json`；界面只显示 `sk-****abcd`；日志/响应/异常全脱敏 |
| 依赖 | 仅标准库 `urllib.request`（不引 pip 依赖，避免框架缺陷 B1） |
| 网络能力 | `plugin.json` 声明 `network:http:https://api.deepseek.com/*` |

设置页（管理员）：`/plugin/kaleido_mini/settings`
- 全题库密钥文件：查看指纹 / 导入已有 / 自动生成 / 下载备份（二次确认）
- DeepSeek：启用开关、API Key、模型、接口地址、超时、连通性自测
- 外链图片策略

## 导出形态

| 场景 | 产物 |
|---|---|
| 题目无 `/media/` 引用 | `<题库>_错题_<日期>.json`（v3.1.0） |
| 题目含相对媒体引用 | `<题库>_错题_<日期>.kqb`（bank + 媒体全加密） |
| 勾选错题本 | 上述 + `.md` + `.html`，多文件自动打成 `.zip` |
| `placement=detached` | 额外附 `.key_obj.json`（分离密钥） |

导出题库特点：**新 uuid + 重新签名**（`valid_key` 可验）、**保留原始选项顺序与
`html_content` 标记**、**只收集被引用的媒体**、带 `exported_by/exported_at/export_source`
溯源字段（完整版会忽略未知字段）。导出的 `.kqb` 能被自家核对器一次通过，可直接再导入。

## 练习页设计要点

| 决定 | 理由 |
|---|---|
| 载荷不含 `correct` / `analysis` | 自测才可信（DevTools 也翻不到答案）；载荷体积减半 |
| 判分只在服务端（`km_parser.grade_item`） | 单一真源，避免 JS 复刻归一化规则产生偏差 |
| **选项随机化只影响展示** | 每题把选项重排并重标号为 A/B/C…（服务端下发 `key`=展示标签、`canonical`=原键）；客户端提交的是 `canonical`，因此判分/汇总/导出全程不污染 |
| **选项键通用 26 进制** | 支持 A…Z, AA…ZZ（对齐 Excel 列号），不再假设只有 A–E；判分按语义序而非字典序排序 |
| 「显示答案」开关 = 预览，**不计战绩** | 与「看答案」按钮语义区分：后者记为 `revealed` 并进入错题 |
| 简答题 `verdict=manual` + 自评按钮 | 不自动判分；M5 起可接 AI 评卷 |
| 复合题按 `group_id` 整体乱序 | 共用题干的子题保持相邻 |
| 进度存 localStorage（键=学科/题库） | 断点续做；与题库对不上时自动重开 |

### 选项随机化 vs 错题导出

随机化是**纯展示层**手段：它打断"第 3 个是正确答案"这类位置记忆，
所以每次进入练习都会重新排序。

**但错题导出始终使用题库原有顺序与原字母**，理由是：

1. 导出产物要能**再导入 Mini 或完整版**，也常被拿去与教材/试卷逐题对照 —— 原字母才对得上；
2. 复习时"这题答案是 C"必须是**稳定**的知识锚点；若每次导出字母都变，反而制造混乱；
3. "按实际作答情况导出"需要额外持久化每次随机化后的排列，产物因人因次而异，
   既增加复杂度，也让同一份错题本无法互相讨论。

因此：**练习时打乱位置，导出时回到原样**，两边各取所需。

快捷键说明：A–Z 覆盖前 26 个选项；选项超过 26 个时（A–Z 之后是 AA/AB…），其后请直接点击。

## 目录

```
src/kaleido_mini/        插件源（打包为 kaleido_mini.zip）
  kaleido_mini.py        主类：路由、页面视图、权限分级
  km_crypto.py           Kaleido 加密方案完整移植 + 二进制扩展
  km_parser.py           v1/v2/v3/3.1.0 归一化、复合题展开、判分原语
  km_package.py          KQB v1 整包读写与核对
  km_store.py            学科/题库/媒体存储、路径安全、.kqb 导入
  km_render.py           HTML 白名单净化 + /media/ 重写 + 缺失占位
  km_practice.py         练习载荷装配、单题判分/揭晓、战绩汇总
  km_export.py           错题 → v3.1.0 题库 / .kqb / Markdown / HTML
  templates/kaleido_mini/  index.html（题库管理）· bank.html（详情与自检）
                           exam.html（练习页）
  static/                km.css · km_common.js · km_index.js · km_exam.js
tools/
  kqb_pack.py            打包 CLI
  kqb_inspect.py         核对 CLI
docs/
  KQB-v1-规范.md         .kqb 格式规范（供完整版采纳）
  框架协调清单.md         开发中发现的框架缺陷（3 条，含最小复现）
tests/                   回归测试（test_*.py 单测 + e2e_runtime.py 端到端）
```

## 权限分级

| 能力 | 管理员 | 登录用户 | 游客 |
|---|---|---|---|
| 浏览学科/题库、详情页、练习、媒体 | ✓ | ✓ | ✗ |
| 上传题库 / `.kqb` 导入 / 新建学科 / 删除题库与学科 | ✓ | ✗（403） | ✗ |
| 密钥文件管理、AI 评卷配置（M5） | ✓ | ✗ | ✗ |

前端按角色隐藏入口，后端每个写接口都有 `@permission_required("admin")` 兜底 ——
端到端用例逐接口比对了 403。

## 快速开始

```bash
# 单元测试（读取 H:\Coding\Projects\Kaleido\Project 下的真实题库与密钥）
python tests/run_all.py

# 若 Kaleido 项目在别处
set KALEIDO_PROJECT_ROOT=D:\somewhere\Kaleido\Project

# 打包一个含媒体的题库
python tools/kqb_pack.py <题库.json> -o <输出.kqb>

# 核对整包
python tools/kqb_inspect.py <输出.kqb>
```

### 隔离运行时端到端测试

```powershell
# 1) 解压 runtime 到 .runtime（FlaskToolkit-<ver>-runtime.zip）
# 2) 打包并安装
python <框架>\tools\package.py pack src\kaleido_mini -o dist\kaleido_mini-0.2.0.zip --type backend
python .runtime\tools\install_plugin.py backend dist\kaleido_mini-0.2.0.zip --base .runtime --update
# 3) 启动服务（另开终端）
cd .runtime; python app.py
# 4) 跑端到端（默认 admin/admin123）
python tests\e2e_runtime.py
```

Python 用系统解释器并清空 `PYTHONHOME` / `PYTHONPATH`（Windows）：

```powershell
$env:PYTHONHOME=''; $env:PYTHONPATH=''
D:\anaconda3\python.exe -X utf8 tests\run_all.py
```

> ⚠️ `package.py pack` 与 `install_plugin.py backend` 在目标已存在时会**交互式提问**，
> 脚本化时需先删除旧产物并加 `--update`（详见 `docs/框架协调清单.md` FTK-KM-03）。

## 加密与密钥

`key_obj.json` 是**母密钥**（全题库密钥文件）：它既能解密题库媒体，
也能解出 Kaleido 的 `auth_config.enc` 登录凭据。因此：

- 绝不提交到版本库（已在 `.gitignore` 排除）
- 绝不经 API 明文返回、绝不写日志
- 生成的密钥需自行备份；**丢失即不可解**

移植细节与兼容性约束见 `docs/KQB-v1-规范.md` §5、§7。
