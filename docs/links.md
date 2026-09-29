# 信件链接字段（links）— v0.7.6 PR2

一封信可以携带 `links`：指向信体之外工件（文件 / 网页 / git 仓库）的结构化指针。
字段在 **发信时做结构校验**（schema + scheme 白名单），在 **读路径现算校验态**
（`ok` / `stale` / `denied` + 拒因）——校验态描述的是"此刻世界的样子"，永不落盘。

## 字段说明

`links` 是一个数组（不发则整个字段缺省，老信零影响），每项：

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `title` | ✅ | str | 人话标题（非空） |
| `uri` | ✅ | str | 链接地址，scheme 限 `file` / `http` / `https` / `git` |
| `kind` | – | str | 自由标注（如 `spec` / `screenshot` / `repo`） |
| `sha256` | – | str | 小写 64 位十六进制；带了这个才做内容比对 |
| `note` | – | str | 补充说明 |

未知 scheme（`ftp://` 等）发信即**结构化拒**（`MailboxError`，`code="invalid_field"`，
A7 错误面同族），信不落箱、sent.log 无行、webhook 不发。未知键、缺 title/uri、
sha256 格式不对，同样发信即拒。

## file:// 与 allowed_roots 安全模型（fail-closed）

`file://` 链接受收件根 `config.json` 的 `allowed_roots` 门禁：

```json
{
  "allowed_roots": [
    "/Users/me/work/exports",
    "~/Media/reports"
  ]
}
```

规则（HS 裁定）：

- **fail-closed**：`allowed_roots` 缺失 / 空数组 / config.json 不存在 ⇒ **全部**
  `file://` links 判 `denied`（`no_allowed_roots`）。门默认是关的。
- **不默认放行 `$HOME`**：只有配置里显式列出的前缀才放行（`~` 会展开）。
- 校验前先 **realpath/symlink 归一** 再做**精确前缀匹配**——`..` 穿越与
  symlink 绕出都到不了 roots 之外（`outside_allowed_roots`）。
- **禁通配符**：配置项里出现 `*` / `?` / `[` 直接 `MailboxError` 大声报错
  （半写成的安全边界绝不静默降级，同 `identity_binding` 教义）。

### 读路径校验顺序（file://）

1. 策略门：realpath 归一后做前缀匹配 → 出界 ⇒ `denied` / `outside_allowed_roots`
2. 文件存在性（regular file）→ 没了 ⇒ `denied` / `missing`
3. sha256 比对（**信里带了才比**；没带只查存在性）→ 不匹配 ⇒ `stale` / `sha256_mismatch`
4. 通过 ⇒ `ok`

## http(s) / git 的 v1 边界

**只做域策略校验（发信时 scheme 白名单），读路径绝不发网络 GET。**
读信不变慢、信箱零内网探测面。host 级 allow/deny 名单是后续版本的范围；
在此之前 http(s)/git 链接一律读作 `ok`。

## 校验态样例

同一封信（文件内容随后被改 / 删）在三次读取里现算出不同态：

```json
{
  "links": [
    {
      "title": "季度报表",
      "uri": "file:///Users/me/work/exports/q3.pdf",
      "sha256": "9f2c…c3d",
      "state": "ok",
      "reason": null
    }
  ]
}
```

内容变了（stale）：

```json
{ "state": "stale", "reason": "sha256_mismatch" }
```

文件删了 / 出界 / 未配置 roots（denied 三种拒因）：

```json
{ "state": "denied", "reason": "missing" }
{ "state": "denied", "reason": "outside_allowed_roots" }
{ "state": "denied", "reason": "no_allowed_roots" }
```

## 密封信同权（判据④）

`sealed=True` 的信对非收件人视角（redact_sealed）**连 links 一并剥除**——
link 表标的就是载荷在哪，剥了 body 留着指针等于没剥。

## 入口同源（判据⑥）

校验与态计算全部收敛在 `store.py` 单点：MCP 工具（`mailbox_send(links=…)`）
与将来任何 CLI `mail send` 子命令共用同一个 `MailStore.send()`——**将来
cli.py 若加 mail 子命令，必须走同一入口，不得旁路校验**。本版本 cli.py 无
mail send 子命令，无需改动。
