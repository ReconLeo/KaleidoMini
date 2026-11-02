# KQB v1 整包格式规范（Kaleido QuizBank Package）

| 项目 | 值 |
|---|---|
| 格式标识 | `kaleido-qbank-package` |
| 版本 | `1.0.0` |
| 外层容器 | 标准 ZIP（推荐扩展名 `.kqb`，同时接受 `.zip`） |
| 参考实现 | `src/kaleido_mini/km_package.py` + `km_crypto.py` |
| 参考工具 | `tools/kqb_pack.py`（打包）、`tools/kqb_inspect.py`（核对） |
| 状态 | v1 已实现并通过真实数据验证；加密二进制部分为**本次新增扩展** |

---

## 1. 目的

Kaleido 既有题库约定是「`<name>.json` + 同名目录 `<name>/` 存放媒体」，媒体以
`/media/<filename>` 相对引用。该约定有两个天生短板：

1. **无法单文件分发**——发一个题库必须同时发一个目录，且目录名必须与 JSON 同名（改名即失效）。
2. **无完整性、无加密**——媒体是裸文件，谁都能看、能改。

KQB 把「题库 + 媒体 + 清单 + 密钥」收进一个可校验、可加密、可整体分发的单文件容器，
并**完全兼容**既有题库加密体系（`key_obj.json` / `valid_key` 签名 / `encrypted_content`），
保证历史加密题库仍可读。

---

## 2. 容器结构

```
<题库标题>_<作者>.kqb
├── manifest.json     必填   清单 + 摘要 + 签名（人类可读，不加密）
├── bank.json         必填   题库本体；明文 或 {uuid, valid_key, encrypted_content}
├── key_obj.json      可选   placement=self_contained 时随包（全题库密钥文件）
├── media/<name>      可选   mode=plain 时的明文媒体，**单段文件名**
├── media.enc         可选   mode=encrypted 时的加密媒体容器
└── README.txt        可选   人类可读说明
```

### 2.1 硬性约束

- zip 内**不得**出现：绝对路径（`/x`、`C:/x`）、`..` 路径段、反斜杠路径、重复条目、带密码加密的条目。
- `media/` 下**只允许单段文件名**，禁止嵌套子目录；文件名不得含 `/ \ : * ? " < > |` 与控制字符。
- 解析方应实施资源上限（建议值，实现可调）：

  | 限制 | 建议值 |
  |---|---|
  | 整包解压总量 | ≤ 512 MB |
  | 条目数 | ≤ 5000 |
  | 单条目解压后 | ≤ 200 MB |
  | 单条目压缩比 | ≤ 100:1 |

---

## 3. `manifest.json`

### 3.1 示例（加密媒体 + 自包含密钥 + 全部校验通过）

```jsonc
{
  "kqb_format": "kaleido-qbank-package",
  "kqb_version": [1, 0, 0],
  "created_at": "2026-11-02T19:33:47+08:00",
  "generator": { "name": "Kaleido Mini", "plugin": "kaleido_mini", "version": "1.0.0" },
  "kind": "full_bank",                       // full_bank | wrong_answer_export

  "bank": {
    "path": "bank.json",
    "bytes": 433243,
    "encrypted": true,
    "cipher": "AES-256-CBC+PKCS7/key_obj.iv",
    "sha256_ciphertext": "<64 hex>",
    "sha256_plaintext": null,                // 加密时置 null；明文时填实际值
    "uuid": "01e5ac5b-59c2-408c-a397-c10d4622d87d",
    "valid_key": "<base64 344 字符>",
    "format_version": [3, 0, 0],
    "title": "眼科学题库",
    "author": "System",
    "chapter_count": 3,
    "item_count": 626,
    "type_counts": { "A1": 506, "A2": 120 },
    "media_ref_count": 58
  },

  "key": {
    "placement": "self_contained",           // self_contained | detached
    "path": "key_obj.json",                  // detached 时为 null
    "public_key_sha256": "<64 hex>",         // 公钥 SPKI DER 的 sha256，用于识别「是不是同一把钥匙」
    "rsa_bits": 2048
  },

  "media": {
    "mode": "encrypted",                     // none | plain | encrypted
    "dir": null,                             // plain → "media"
    "container": "media.enc",                // encrypted → "media.enc"
    "container_sha256": "<64 hex>",
    "container_bytes": 16838652,
    "count": 58,
    "total_bytes": 16838652,                 // 明文总字节
    "naming": "basename",
    "files": [
      { "name": "img_001.png", "bytes": 10265,
        "mime": "image/png", "sha256_plaintext": "<64 hex>" }
    ],
    "missing_refs": [],                      // 题库引用但未打包（允许非空）
    "unreferenced": []                       // 打包但题库未引用（允许非空）
  },

  "media_encryption": {
    "algorithm": "AES-256-CBC+PKCS7",
    "key_source": "key_obj.SYMMETRIC_KEY",
    "iv_source": "per_file_random",
    "mac": "HMAC-SHA256(name|iv|ciphertext)",
    "mac_key_derive": "SHA256(utf8(SYMMETRIC_KEY + '|KQB-MAC-1'))",
    "crypto_js_compatible": true
  },

  "integrity": { "algorithm": "sha256", "self_excluded": ["manifest.json", "SIGNATURE"] },

  "signature": {
    "algorithm": "RSA-2048-PKCS1v15-SHA256",
    "target": "sha256(manifest_without_signature)",
    "value_b64": "<base64 344 字符>",
    "public_key_fingerprint": "<64 hex>"
  }
}
```

### 3.2 字段语义要点

- `bank.format_version` 是**声明值**；解析方必须与实算值比对，不一致记 **warn**（不是 fatal）。
- `bank.sha256_ciphertext` 对应"包内实际存的那份字节"；`sha256_plaintext` 用于明文包。
  二者**互斥**，按 `encrypted` 选择。
- `media.missing_refs` / `unreferenced` 是**复核信息**：真实题库常有不一致（见 §7.2），
  允许非空，导入方据此降级，不得因此拒绝整包。
- `manifest` 里**不得**出现任何密钥明文（`SYMMETRIC_KEY`、私钥）。

---

## 4. `media.enc` 容器（加密型二进制文件）

刻意**不用 zip 作为内层**，避免嵌套解压炸弹与二次索引开销。

### 4.1 二进制布局（大端）

```
偏移  长度        内容
0     4          magic = "KQBM"
4     2          version (u16) = 1
6     2          flags   (u16) = 0   （保留，必须为 0）
8     4          index_len (u32)
12    index_len  index_json（UTF-8，紧凑 JSON 数组，按 name 升序）
12+index_len …   各文件密文，首尾相接（offset 相对本段起点）
```

### 4.2 `index_json` 元素

```jsonc
{
  "name": "img_001.png",        // 单段文件名，须与题库中 /media/<name> 完全一致
  "offset": 0,                  // 相对密文段起点
  "length": 10272,              // 密文长度（已按 PKCS7 对齐到 16 的倍数）
  "iv_b64": "<16 字节 base64>",
  "mac_b64": "<32 字节 base64>",
  "sha256_plaintext": "<64 hex>",
  "mime": "image/png",
  "plain_bytes": 10265
}
```

设计收益：**可随机访问单个媒体**、分块校验、无需解压全部即可取一张图。

---

## 5. 加密方案

### 5.1 密钥文件 `key_obj.json`（与既有系统**完全一致**）

```jsonc
{
  "SYMMETRIC_KEY": "<32 个 ASCII 字符>",        // 直接作为 AES-256 密钥的 UTF-8 字节
  "ENCRYPTED_PRIVATE_KEY": "<hex>",             // AES-256-CBC(PKCS7) 加密的 RSA 私钥 PEM(PKCS8)
  "iv": "<16 个 ASCII 字符>",                    // 固定 IV，供 bank 加密使用
  "PUBLIC_KEY": "<base64>"                      // 注意：是 SPKI **DER** 的 base64，不是 PEM
}
```

生成算法必须逐字复刻既有实现（否则与历史题库不通用）：

```
SYMMETRIC_KEY = base64url(urandom(32))[:32]      # 注意是「截断」而非标准编码
iv            = base64url(urandom(16))[:16]
RSA           = 2048 bit, 私钥 PKCS8-PEM 无口令
ENCRYPTED_PRIVATE_KEY = hex( AES-256-CBC(SYMMETRIC_KEY, iv, PKCS7, 私钥PEM) )
PUBLIC_KEY    = base64( SPKI-DER(公钥) )
```

### 5.2 `bank.json` 加密（**沿用原方案，必须字节兼容**）

```
1. 取题库对象，摘掉 uuid 与 valid_key
2. content = json.dumps(剩余, sort_keys=False, separators=(",",":"), ensure_ascii=False)
3. encrypted_content = hex( AES-256-CBC(SYMMETRIC_KEY, key_obj.iv, PKCS7, content) )
4. 信封 = {"uuid":…, "valid_key": "", "encrypted_content": …}
5. valid_key = b64( RSA-PKCS1v15-SHA256( f"{uuid}|{md5}" ) )
   md5 = md5hex( compact_json(信封去掉 valid_key) )
```

> **签名归一化细节（必须照搬，否则历史密文/签名不一致）**
> - `md5` 计算前要「预处理」：整数值 float → int；**dict 中值为 `null` 的键被丢弃**。
> - 但 `encrypted_content` 的 JSON 序列化**不做**该预处理（保留 null）。
>   二者不对称是既有行为，不是缺陷，**不得"修正"**。
> - JSON 一律 `separators=(",",":")`、`ensure_ascii=False`、`sort_keys=False`（保持插入序）。
>
> **解密后的验签语义**：`valid_key` 是从信封回填的，签名对象是**信封**，
> 因此明文复原后整体验签**必然为 False**。想得到可验签的明文，需重新签名。

### 5.3 媒体加密（KQB v1 **新增扩展**）

```
key     = SYMMETRIC_KEY 的 UTF-8 字节（32 字节 → AES-256）
iv      = 每文件独立随机 16 字节
ct      = AES-256-CBC(key, iv, PKCS7, plaintext)
mac_key = SHA256( utf8(SYMMETRIC_KEY + "|KQB-MAC-1") )          // 32 字节
mac     = HMAC-SHA256( mac_key, utf8(name) || iv || ct )          // 绑定文件名
```

**为什么不是 AES-GCM**：已实证 `crypto-js@4.2.0` 中 "GCM" 出现 0 次，其 mode 仅有
`CBC / CFB / CTR / CTRGladman / OFB / ECB`。为了让浏览器端能用**纯 CryptoJS**
完成打包/解包，只能走 CBC；认证需求由 **encrypt-then-MAC** 补足（防篡改、防换文件/换名）。
MAC 校验必须用**常量时间比较**。

### 5.4 `manifest.json` 签名

```
digest    = sha256hex( compact_json( manifest 去掉 signature, 键排序, ensure_ascii=False ) )
sign_text = "kaleido-qbank-package|1.0.0|" + digest
value_b64 = b64( RSA-PKCS1v15-SHA256( sign_text ) )
```

**键排序**（`sort_keys=true`）保证与字典插入顺序无关，任何语言都能复现同一 digest。

---

## 6. 核对清单

实现方**必须**在落盘前逐项执行。分级：

- **fatal** —— 任一不过：**绝不落盘**，整包拒绝。
- **warn** —— 记录并继续，UI 提示，媒体按缺失降级为占位。

| # | 核对项 | 级别 | 判定内容 |
|---|---|---|---|
| 1 | zip 路径安全 | **fatal** | 无绝对路径/盘符/`..`/反斜杠/重复条目/加密条目；`media/` 下单段 |
| 2 | 资源上限 | **fatal** | 总量、条目数、单文件、压缩比均在限内 |
| 3 | manifest 存在与合法 | **fatal** | 存在、JSON 合法、`kqb_format` 匹配、`kqb_version` 主版本受支持 |
| 4 | manifest 签名 | **fatal**（有签名时）| 用可用密钥验 `value_b64`；无签名 → warn；无密钥 → warn 跳过 |
| 5 | bank 摘要 | **fatal** | 实际字节 sha256 与 `sha256_ciphertext`/`sha256_plaintext` 一致；长度与 `bytes` 一致 |
| 6 | bank 解密与验签 | **fatal**（加密时）| 信封验签通过 + AES 解密成功；缺密钥必须给出"请提供密钥文件"的明确提示 |
| 7 | 媒体完整性 | **fatal** | 容器摘要 → 每文件 MAC → 每文件 `sha256_plaintext`；清单声明的文件必须都在 |
| 8 | 密钥指纹一致性 | warn | `manifest.signature.public_key_fingerprint` 与所用密钥不符 → 提示"来自另一套密钥" |
| 9 | 格式与统计一致性 | warn | `format_version`、`item_count`、`chapter_count`、`type_counts` 与实算比对 |
| 10 | 媒体引用覆盖 | warn | 题库 `/media/<name>` 引用是否都能在包内解析；包内是否有未被引用的媒体 |
| 11 | 明文题库签名 | warn | 验签失败**不得拒绝**（见 §7.1） |
| 12 | 目标冲突（导入层）| warn | 目标学科已存在同 `uuid` 题库 / 同名文件 → 覆盖需显式确认 |

> 核对项 8、11 之所以是 warn 而非 fatal，都由真实数据倒逼：见 §7。

---

## 7. 兼容性事实（来自对既有数据的实测）

### 7.1 明文题库的 `valid_key` 不可信，验签只能提示

对 33 个既有题库实测：

| 情形 | 数量 | 结论 |
|---|---|---|
| 256B RSA 签名且验签**通过** | 21 | 正常 |
| 256B 但验签**失败**（他钥签名或签后被改） | 7 | 含 `眼科学题库_System` 等 |
| 24 字符占位随机值 | 2 | 早期提取脚本产物 |
| 128B 旧式签名 | 3 | `standardization` 样例 |

因此：**明文题库验签失败一律放行**，仅在 UI 打「签名无效」角标。

### 7.2 媒体引用与文件常不一致

- `眼科学随堂测试合集_眼科学教研室.json` 引用 `/media/1.jpg`，同名目录里只有 `1.png`
  → 解析方应支持**同主名换扩展名回退**，仍失败才占位。
- `眼科题库（合并）_大连医科大学附属第一医院眼科_1.json` 引用 **73** 个媒体，
  但同名目录**根本不存在** → 必须可导入（warn），不可 fatal。
- `眼科学题库_System.json` 引用 58 个、实际文件 58 个，完全一致（可作为正向基准）。

---

## 8. 自包含 vs 分离密钥

| placement | 包内 `key_obj.json` | 导入要求 | 适用场景 |
|---|---|---|---|
| `self_contained` | 有 | 无需外部密钥即可读 | 自用错题本、内部分发 |
| `detached` | 无 | **必须**同时提供 `.key_obj.json` | 真正保密：包泄露 ≠ 内容泄露 |

`self_contained` 的语义是「防翻看、防篡改」，**不是**对抗拿到包的人（密钥就在包里）。
需要对抗时必须用 `detached`，并注意：**密钥丢失 = 数据永久不可解**。

---

## 9. 浏览器端纯 CryptoJS 参考实现

以下片段仅使用已在 `crypto-js@4.2.0` 中确认存在的原语
（`AES` / `CBC` / `Pkcs7` / `WordArray.random` / `SHA256` / `HmacSHA256`）。

```js
const KEY = CryptoJS.enc.Utf8.parse(SYMMETRIC_KEY);          // 32 字符 = 32 字节
const MAC_KEY = CryptoJS.SHA256(CryptoJS.enc.Utf8.parse(SYMMETRIC_KEY + '|KQB-MAC-1'));

// —— 媒体加密 ——
function sealMedia(name, arrayBuffer) {
  const words = CryptoJS.lib.WordArray.create(arrayBuffer);
  const iv = CryptoJS.lib.WordArray.random(16);              // 每文件随机 IV
  const out = CryptoJS.AES.encrypt(words, KEY,
                { iv, mode: CryptoJS.mode.CBC, padding: CryptoJS.pad.Pkcs7 });
  const ct = out.ciphertext;                                  // WordArray
  // MAC 覆盖：utf8(name) || iv(16B) || ct
  const msg = CryptoJS.enc.Utf8.parse(name)
      .concat(iv)
      .concat(ct);
  const mac = CryptoJS.HmacSHA256(msg, MAC_KEY);
  return {
    ciphertext: ct,
    iv_b64: CryptoJS.enc.Base64.stringify(iv),
    mac_b64: CryptoJS.enc.Base64.stringify(mac),
  };
}

// —— 媒体解密（先验 MAC，必须常量时间比较）——
function openMedia(name, ctWords, ivB64, macB64) {
  const iv = CryptoJS.enc.Base64.parse(ivB64);
  const expect = CryptoJS.enc.Base64.parse(macB64);
  const actual = CryptoJS.HmacSHA256(
      CryptoJS.enc.Utf8.parse(name).concat(iv).concat(ctWords), MAC_KEY);
  if (actual.toString() !== expect.toString()) throw new Error('MAC 校验失败');
  const plain = CryptoJS.AES.decrypt(
      CryptoJS.lib.CipherParams.create({ ciphertext: ctWords }), KEY,
      { iv, mode: CryptoJS.mode.CBC, padding: CryptoJS.pad.Pkcs7 });
  return plain.toString(CryptoJS.enc.Base64);                 // 再转 Uint8Array
}
```

> **注意**：`bank.json` 的加密使用**固定 IV**（`key_obj.iv`），与媒体不同，
> 这是为兼容既有加密题库而保留的既有行为。

---

## 10. 版本演进

| 版本 | 变更 | 兼容策略 |
|---|---|---|
| 1.0.0 | 首版：zip 容器 + manifest + 明文/加密媒体 + 自包含/分离密钥 | — |

- 读取方按 `kqb_version` **主版本**判定兼容：主版本相同即可读，次版本字段缺失按默认处理。
- 新增字段一律**可选**；未知字段必须**忽略而非报错**。
- `media_encryption.algorithm` 预留 `AES-256-CBC`（已用）与未来可能的替换算法；
  读取方必须按该字段分派，不得写死。

---

## 11. 安全注意事项

1. `key_obj.json` 是**母密钥**：实测它不仅能解题库，还能解出 `auth_config.enc` 中的登录凭据。
   任何实现都不得把它写进日志、明文回传 API 或放进 manifest。
2. `bank` 的固定 IV 是既有方案的固有弱点（同密钥下 IV 复用）；KQB v1 为兼容不动它，
   但**媒体一律随机 IV**，不要把这个弱点扩大。
3. MAC 比较必须常量时间；媒体文件名参与 MAC，换名/替换必被发现。
4. 解密顺序固定为：**先验签/验 MAC，后解密**，绝不解密不可信密文。
5. 核对清单的 fatal 项必须在**落盘前**全部通过；建议先在内存完成核对再写文件。
