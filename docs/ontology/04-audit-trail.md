# 本次审计的过程记录

> 审计本身也是一次操作序列。按 [`03-operation-audit-record.md`](03-operation-audit-record.md)
> 的精神，这里留下方法、证据与边界，让结论可复核、可反驳。

## 审计边界（先说不能做什么）

| 项 | 状态 |
|---|---|
| 静态代码分析 | ✅ 全量，`server.py` 7331 行 + `harness/` 639 行逐段阅读 |
| AST 结构抽取 | ✅ 可复现（`extract_ontology.py`） |
| 连接真实金蝶账套验证 | ❌ **未做**——无凭据、无网络可达性 |
| 运行期行为验证 | ❌ 未做 |
| 依赖库行为验证 | 部分：`error_type` 的 Python 语义用最小复现确认（见下） |

因此：凡结论依赖"金蝶服务端实际如何响应"的，一律标注**存疑**并写明验证方法。
本报告只有 L-2 落入此类。其余发现均可由本仓库代码本身证实。

## 方法

1. **抽取，不假设**——所有清单（97 工具 / 48 名词 / 3 链接 / 端点分布）
   由 AST 从源码抽取，不依赖 README 或文档描述。
   文档与代码不一致的地方本身就是发现（如 R-4 的契约与文档相反）。
2. **以端点为准，不以工具名为准**——工具名会骗人。
   `kingdee_workflow_approve` 听起来是审批，实际打的是 `audit`/`unaudit` 端点。
   原子性判定全部基于 `_post_raw()` 的第一个字面量实参。
3. **区分顺序与分支**——多端点工具要先判断是顺序 Saga 还是互斥派发。
   `audit_atomicity.py:_is_branch_dispatch()` 用 AST 检查端点是否落在同一 `if/elif`
   的互斥分支里。第一版审计器把 `kingdee_workflow_approve` 误判为 Saga，
   加入这个判定后修正为 warning 级的"动词过载"。
4. **可复现优先于篇幅**——能写成检查器的发现就写成检查器（15 项），
   写不成的才写进报告（8 项）。

## 关键证据链

### 复合工具的端点序列（A-1）

```
$ python3 tools/ontology/extract_ontology.py --write
$ python3 -c "import json; ..."
kingdee_create_and_audit    ep=['audit','save','submit']
kingdee_push_and_audit      ep=['audit','push','submit']  loop=True
kingdee_create_lx_billing   ep=['audit','push','save','submit']
```

### 批量语义分裂（A-2）

```
逐条循环 : kingdee_submit_bills / audit_bills / unaudit_bills / delete_bills / push_and_audit
Ids 拼接 : kingdee_cancel_bills / void_bills / close_bill / unclose_bill / forbid_bills / enable_bills
```

前者来自 `for bill_id in params.bill_ids:`，后者来自 `",".join(params.bill_ids)`（`server.py:3333`）。

### 会话锁形同虚设（A-4）

```
$ grep -n "_get_session_lock\|_session_lock" src/kingdee_mcp/server.py
1928:_session_lock: asyncio.Lock = None
1930:def _get_session_lock() -> asyncio.Lock:
1931:    global _session_lock
1932:    if _session_lock is None:
1933:        _session_lock = asyncio.Lock()
1934:    return _session_lock
```

6 行全部在定义处，无任何调用点。

### `error_type` 恒为空（P-3）

`except Exception as e: ... raise` 退出 except 子句时 Python 会隐式 `del e`，
`finally` 中的 `'e' in dir()` 因此恒为 `False`。最小复现：

```python
def f():
    try:
        try: raise ValueError('x')
        except Exception as e: raise
    except Exception: pass
    finally: print('e in dir():', 'e' in dir())
f()
# → e in dir(): False
```

三处相同代码：`server.py:1729`、`:1792`、`:1911`。

### `D` 的双重定义（S-2）

```python
if params.status == "pending":
    status_filter = "FDocumentStatus IN ('A', 'B', 'D')"   # :4739
elif params.status == "rejected":
    status_filter = "FDocumentStatus = 'D'"                # :4743
```

### `next_action` 反推失效（R-2）

`harness/rules.py:87` 的表达式对三处取值的推导结果：

| `next_action` 实际取值 | 出处 | 反推得到 | 存在？ |
|---|---|---|---|
| `"submit"` | `DOC_LIFECYCLE` | `kingdee_submit_bills` | ✅ |
| `"submit+audit"` | `DOC_LIFECYCLE` | `kingdee_submit_bills` | ✅ |
| `"kingdee_submit_bills + kingdee_audit_bills"` | `server.py:7005, 7040` | `kingdee_kingdee_bills` | ❌ |
| `"kingdee_audit_production_orders"` | `server.py:6953` | `kingdee_kingdee_bills` | ❌ |

## 审计器自身的可信度

`audit_atomicity.py` 的 7 项检查中，有两项是启发式，可能产生误差：

- **AT-05（规则覆盖）** 用正则 `"(kingdee_[a-z_]+)"` 提取 `harness/rules.py` 里
  以字符串字面量出现的工具名。如果将来规则改用别的匹配方式（如按端点或正则），
  这项检查会误报。这本身也说明"按工具名硬编码"是脆弱的设计。
- **AT-06（next_action 词表）** 用正则从源码提取 `next_action` 的字面量赋值，
  拿不到运行期动态拼接的值。当前代码里没有动态拼接，但这是它的盲区。

`_endpoints_called()` 只识别 `_post_raw(<字面量>, ...)` 形式。
若某处改用变量传端点，抽取会漏。已核对当前代码全部为字面量。

## 未展开的方向

以下在本次范围之外，列出供后续：

- `kingdee_discover_tables` / `discover_columns` 等 SQL 探查工具的注入面
  （`_escape_sql_like` 只转义 LIKE 通配符，`filter_string` 类参数是否直接拼接未逐条核查）；
- 97 个工具的 `form_id` 与 `FORM_CATALOG` 的一致性（L-3 只指出缺绑定，未逐个比对）；
- `evals/` 与 `tests/` 对写路径的覆盖率——特别是三个复合工具的失败路径是否有测试。
