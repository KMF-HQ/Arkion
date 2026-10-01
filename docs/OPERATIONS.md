# Arkion-Q1 操作手册

> 适用：Preview 0.1（2026-10-01 修订）
> 目标读者：拿到这份运行包、要在**自己机器上把它跑起来**的人
> 所有数字都是**本机实测**（8 GB 显存 / 16 GB 内存 / Windows / sm_86），不是理论值

---

## 0. 一页速查

```bat
:: ① 找模型在哪（只报告，不自动采纳）
python serve\locate_model.py --check E:\qw38

:: ② 写成本机配置（所有启动脚本会自动 call 它）
python serve\locate_model.py --write

:: ③ 一键聊天（服务没起会自动拉起，约 45 s）
serve\chat.bat
serve\chat.bat --once "1+1=?" --no-think        :: 单发，且不思考（快 20 倍）

:: ④ 自检
python serve\app.py --selftest                  :: 服务层离线自检
run_gate_k3.bat                                 :: 引擎端到端门禁（期望 15/16 PASS）

:: ⑤ 停掉一切
taskkill /IM arkion-q1.exe /F
```

---

## 1. 环境要求

| 组件 | 下限（实测能跑） | 说明 |
|---|---|---|
| GPU | **8 GB 显存**，sm_86 | 更大显存 ⇒ 更快，且能开更大 `SERVER_CTX` |
| 内存 | **16 GB** | `ENT_RAM_FRAC=0.50` + `K4CAP=75` 是按这台机器调的 |
| 磁盘 | **≈ 385 GB** | 其中 PLE 335 GB（见 `DEPENDENCIES.md`） |
| 系统 | Windows | 当前只有 Windows 编译通过 |
| Python | 3.8+，需 `tokenizers` + `jinja2` | 只服务层用 |

---

## 2. 数据准备（唯一容易出错的一步）

引擎**不做任何搜索**。它只认一个目录，然后在里面读**固定文件名**。三个位置：

| 位置 | 环境变量 | 内容 |
|---|---|---|
| 模型目录 | `ARK_MODEL_DIR`（或命令行第 1 个参数） | 索引 + 两个 .bin + `ple_index.json` + 48 个专家包 |
| golden 目录 | `ARK_GOLDEN_DIR`（或第 2 个参数） | `qw38_golden.json` + `qw38_golden_hidden.npy` |
| PLE 根目录 | `ARK_PLE_ROOT` | 原始 bf16 仓库（PLE 分片） |

优先级：**命令行参数 > 环境变量 > 报错**。没有任何写死的回落路径 —— 这是有意的：
以前它默认 `E:/qw38`，换机器时表现为一句与"路径错了"无关的加载失败。

### 用工具定位并校验

```bat
python serve\locate_model.py                     :: 扫所有固定盘（深度 3），只列候选
python serve\locate_model.py --root D: E: --depth 4
python serve\locate_model.py --check E:\qw38     :: 校验一个目录（手动选择时用）
python serve\locate_model.py --write             :: 写 arkion.local.bat
```

输出会逐项列出缺失/偏小的文件，并**只在完全可用时**给出 `set ARK_MODEL_DIR=...` 的建议。

> ⚠️ **为什么不让引擎自己扫盘找**：实测过一个反例 —— 同一个目录下可能同时躺着
> `experts_k3c`（真，42.7 GB）和 `experts_fake3` / `experts_fake4`（假包，也是 48/48 完整）。
> 挑错包**不报错**，只输出垃圾。所以这里的原则是「探测 + 校验 + 报告」，绝不自动改写配置。
> 同理，`ARK_PLE_ROOT` 指错仓库时会**解码出垃圾**（relRMS 0.996）而不是报错 ——
> 引擎现在会在启动时打印 `[path] ple_root = ... (来源)`，请务必核一眼。

---

## 3. 启动与停止

### 交互命令行（推荐入口）

```bat
serve\chat.bat                      :: 自动探测服务；没起就在独立窗口拉起 run_serve.bat，等就绪再进聊天
serve\chat.bat --once "1+1=?"       :: 单发
serve\chat.bat --no-think           :: 不思考（首字 1.3 s 而不是 25 s）
serve\chat.bat --max-tokens 512
serve\chat.bat --once "..." --url http://127.0.0.1:9000
```

会话内命令：`/help` `/exit` `/clear` `/hist` `/think on|off` `/thinkmode on|off` `/nothink`
`/temp X` `/maxtok N` `/system S` `/save FILE`

### 只起服务

```bat
serve\run_serve.bat                 :: 默认 127.0.0.1:8471
serve\run_serve.bat 9000            :: 换端口
```

启动脚本会打印并应用这些路径与容量：

```
[run_serve] KV reserve=65536 rows | http://127.0.0.1:8471
[run_serve] model=E:\qw38
[run_serve] ple_root=D:\qwen3.8-flash-next  qwen_dir=D:\qwen3.8-flash-next
```

### 停掉

```bat
taskkill /IM arkion-q1.exe /F
```

退出 `chat.bat` **不会**停服务（下次秒进）。要释放 GPU 就执行上面这条。

### 直连引擎（不经服务层，做门禁/基准时用）

```bat
bin\arkion-q1.exe "E:/qw38" "data\golden"
```

---

## 4. 三种用法

### A. 命令行交互 — `serve\chat.bat`

见上。输出会区分「思考」与「正文」，并在每条消息后打印
`[总 X s | 首字(prefill) Y s | ≈N tok | 生成 Z ms/tok | 思考 A 字 / 正文 B 字 | finish=...]`。

### B. HTTP API — OpenAI 兼容

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8471/v1", api_key="sk-arkion-local")

r = client.chat.completions.create(
    model="arkion-q1-k3",
    messages=[{"role": "user", "content": "你好"}],
    max_tokens=512,
    reasoning_effort="medium",       # minimal / low / medium(默认) / high
)
print(r.choices[0].message.content)
print(r.choices[0].message.reasoning_content)
```

端点：`POST /v1/chat/completions`（stream / 非 stream）、`GET /v1/models`、`GET /healthz`、
`/arkion/v1/config`、`/arkion/v1/cancel`。

`GET /healthz` 是排查第一步，一次给出：`started` / `ready_seconds` / `ctx_limit` /
`kv_reuse` / `kv_sys_snap` / `snaps` / `snap_root`。

### C. 配套 CLI — `serve\arkion-cli.py`

```bat
python serve\arkion-cli.py status
python serve\arkion-cli.py config
python serve\arkion-cli.py models
python serve\arkion-cli.py chat "你好"           :: 单次
python serve\arkion-cli.py chat                  :: 交互
python serve\arkion-cli.py bench                 :: 测速
```

---

## 5. 参数入口（三层）

引擎共有约 50 个注册项。**先看当前生效值，再动手**：

```bat
set ARK_DUMP_CONFIG=1
bin\arkion-q1.exe "E:/qw38" "data\golden"
```

会打印每一项的**生效值 + 来源**（`new` / `OLD` / `default`），以及末尾的
`CLAMP 请求值 -> 生效值 (原因)` 汇总 —— **参数被资源闸改写时绝不静默**。

| 层 | 内容 | 你会不会碰 |
|---|---|---|
| **L1 自动** | 码本容量（按 artifact 实测定型 4.098 GiB）、`K4HOT`/`K4_NT` 的显存闸、pinned 池预算 | 不需要碰；被改写时会有 `CLAMP` 提示 |
| **L2 用户** | 见下表 | 会 |
| **L3 专家** | 其余全部（含位级一致开关） | 改之前请确认你能接受结果变化，并用 `run_gate_k3.bat` 复核 |

### L2：常用项

| 变量 | 默认 | 作用 |
|---|---|---|
| `ARK_MODEL_DIR` | 无（必填） | 模型目录 |
| `ARK_PLE_ROOT` | `D:/qwen3.8-flash-next` | PLE 分片根目录（**指错不报错，只出垃圾**） |
| `ARK_GOLDEN_DIR` | `data/golden` | 门禁数据目录 |
| `ARKION_CTX` | `65536` | KV 预留行数 = 上下文上限。**131072 会让引擎首个请求死掉** |
| `ARKION_PORT` / `ARKION_HOST` | `8471` / `127.0.0.1` | 监听 |
| `ARKION_DEFAULT_EFFORT` | `medium` | 请求未指定 `reasoning_effort` 时的档位 |
| `ARKION_DEFAULT_THINK_TEMPLATE` | `1` | `0` = 不注入默认思考模板 |
| `ARKION_NO_WARM` | — | `1` = 启动不预热（调试） |
| `ARKION_MOCK` | — | `1` = Mock 引擎（离线联调，不需要 GPU） |
| `ARKION_EXE` | `bin/arkion-q1.exe` | 换引擎二进制（例如 K4 版） |
| `ARKION_ENGINE_LOG` | — | 引擎 stderr 落盘路径（排查容量/IO 问题必需） |
| `ARKION_SNAP_ROOT` | `serve/_snaps` | KV 快照目录 |
| `K4_PACK_DIR` | `experts_k3c` | 专家包名；**别指到 `*_fake*`** |
| `ENT_RAM_FRAC` / `K4CAP` / `K4HOT` | `0.50` / `75` / `0` | pinned 池与显存热点；按本机内存调 |

### 口径坑（先读，省得误判）

- **np>1（批量）时 `[SEG] moe`、`五段之和`、`[WALL] 缺口` 全部失真**（会出现"五段之和"远大于
  wall 这种物理上不可能的数）。**只看 `[WALL] 真实迭代跨度` + PPL + dump sha1。**
- **`exit=3` 是 PPL 的正常完成码**；`[exit] ... checks=0/0 -> FAIL` 是**显示约定，不是失败**。
- **跨 np 的 TOPI dump 不能直接比 sha1**（布局随步数变），要用 `tools/_topi_cmp.py A.bin B.bin npA npB`。
- `[codecap]` 打印的容量是**按你机器上的 artifact 实测出来的**，不是常数 —— 换模型会变。

---

## 6. 换机验收清单（照顺序做）

```bat
:: 1) 数据对不对
python serve\locate_model.py --check <模型目录>

:: 2) 服务层自检（不需要 GPU，30 秒）
python serve\app.py --selftest        :: 期望：serve 自测全部 PASS

:: 3) 引擎端到端门禁（约 1 分钟）
run_gate_k3.bat                       :: 期望：=== M3 GPU 端到端门禁: 15/16 PASS ===
                                      ::        pos 19 argmax=109455 golden=109455

:: 4) 端到端对话
serve\chat.bat --once "1+1=?" --no-think
```

**验收通过判据**：门禁 15/16 + `pos 19` 逐位一致（唯一未过的 pos 11 是 `margin=0.0000`
的并列翻转，属已知现象）+ 对话能出正确回答。

---

## 7. 疑难排查

| 症状 | 原因 | 处理 |
|---|---|---|
| 启动脚本立刻退出 / 报 pinned 池错误，随后连小分配都失败 | `ENT_RAM_FRAC` 超过本机 pinned 硬顶（本机 ≈7.0 GiB） | 用默认 `ENT_RAM_FRAC=0.50 K4CAP=75 K4HOT=0` |
| **第一个请求后引擎进程消失**，HTTP 只拿到空 200 流；引擎日志停在 `[M4] GR2 done, enter gate li=0` 且无错误行 | `SERVER_CTX` 过大（本机 131072 必犯） | `set ARKION_CTX=65536` 后重启 |
| `[path] FATAL: no model directory given.`（exit 2） | 既没给参数也没设 `ARK_MODEL_DIR` | `locate_model.py --write` 或手工 `set` |
| `[path] FATAL: cannot open required file: ...`（exit 3） | 目录/文件不对，或盘符没挂 | `locate_model.py --check <目录>` |
| `[codecap] FATAL: the codebook needs X GiB but only Y GiB is free`（exit 12） | 显存装不下码本 | 换更大显存或更小的量化 artifact |
| `k4 bin missing: <path>` | 专家包缺失或名字不对（例如指到 `experts_fake3`） | `locate_model.py --check`；必要时 `set K4_PACK_DIR=experts_k3c` |
| **不报错但输出是垃圾** | `ARK_PLE_ROOT` 指向了错仓库（fp8 镜像等等） | 核对启动日志的 `[path] ple_root = ... (来源)` |
| `finish=length` 且正文为空 | `max_tokens` 太小，预算被思考吃光 | 加大 `max_tokens`，或用 `reasoning_effort=minimal` |
| `无法连接 http://127.0.0.1:8471` | 服务没起 | `serve\chat.bat`（会自动拉起）或 `serve\run_serve.bat` |
| 端口占用 | 已在运行 | 关旧窗口，或 `taskkill /IM arkion-q1.exe /F` |
| 突然变慢约一倍 | 显存被填满（`[VRAM] ... free=0.000`） | 降低 `K4_NT`；可用余量 = free − 码本 4.4 GB − 128 MB |
| `[ARKION-CFG] CLAMP ...` | 你设的值被资源闸改写了 | 按提示调整；这不是错误，是可见性设计 |

**排查三板斧**：`GET /healthz` → `ARKION_ENGINE_LOG=_engine.log` 看引擎侧真实输出 →
`ARK_DUMP_CONFIG=1` 看参数生效值与来源。

---

## 8. 基线读数（对照"有没有回退"）

| 项 | 本机基线 |
|---|---|
| 门禁 | `positions=22/22 checks=15/16`；`pos 19 argmax=109455 margin=1.0000` |
| PPL（批量路径，2047 位置） | `mean_logprob=-0.557593`（三臂逐位一致） |
| 批量墙钟（np=83 / 219 / 512） | **78.4 / 60.4 / 37.0 ms/位置** |
| 单流生成 | 83–98 ms/token（≈10–12 tok/s） |
| 首字（默认思考模板 / `--no-think`） | **25.2 s / 1.3 s** |
| 码本 | 需求 4.035 GiB（按 artifact 实测）→ 容量 4.098 GiB |
| 引擎 READY | 10–45 s |
| 已验证的最大 `SERVER_CTX` | **65536** |

---

## 9. 发布包该含什么

| 含 | 不含 |
|---|---|
| `bin\arkion-q1.exe`（**只放这一个 exe**） | 其它 `bin\*.exe`（K4/历史构建，都是 `src\_b_*.bat` 的产物） |
| `serve\**` + `serve\_snaps\` | `_snaps_test\` / `_snaps_srvtest\`（测试缓存） |
| `data\golden\**` | `tools\`（2.1 GB）、`_archive\`、`session-*\` |
| `docs\**`、`README.md`、`DEPENDENCIES.md` | `logs\`、`*.log`、`*.err`、`__pycache__\` |
| `_serve_env.bat`、`run_gate_k3.bat`、`run_chat_k3.bat` | `_t_*.bat`（实验记录，留在开发树）、`arkion.local.bat`（本机生成） |
| `src\**`（源码，可选） | `src\_snap\`、`*.bak*` |

> 模型数据**不打进包**：靠 `ARK_MODEL_DIR` / `ARK_PLE_ROOT` 引用，约 385 GB。
