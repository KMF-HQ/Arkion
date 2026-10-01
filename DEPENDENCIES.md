# 依赖清单（DEPENDENCIES）

> 2026-10-01 核对：全部条目已对盘逐项验证（体积为实测）。
> 运行包**只含小文件**（源码/可执行/索引/golden，约 35 MB）；
> 模型数据 **≈ 385 GB** 靠路径引用，未打包。

---

## 一、必须存在的外部数据

### 0. 三个位置怎么给（先读这条）

引擎**不做任何搜索**，只认一个目录里固定文件名。三个位置与对应环境变量：

| 用途 | 环境变量 | 命令行 |
|---|---|---|
| 模型目录 | `ARK_MODEL_DIR` | 第 1 个参数 |
| golden 目录 | `ARK_GOLDEN_DIR`（默认 `data/golden`） | 第 2 个参数 |
| PLE 分片根目录 | `ARK_PLE_ROOT` | —（只有环境变量） |

优先级：**命令行 > 环境变量 > 报错**。以前它默认写死 `E:/qw38`，换机器时表现为一句
与"路径错了"无关的加载失败 —— 现在会明确报 `[path] FATAL: ...`。

定位与校验：`python serve\locate_model.py --check <目录>`（详见 `docs/OPERATIONS.md`）。

### 1. `<模型目录>/experts_k3c/` — 42.7 GB ★ K3 核心

> **获取地址**：本节 1/3/4/5 的文件（专家包 + 非专家权重 + 索引）已打包为 **49.1 GB** 的
> 发布件，在魔搭下载：**https://www.modelscope.cn/models/HQsensei/Arkion-Q1-K3**
> 下载后把该目录设为 `ARK_MODEL_DIR` 即可。第 2 节（PLE 基座）仍需从上游页面单独获取。

K3 3-bit EXL3 trellis 专家权重，**48 个文件**（`L00_experts_k3c.bin` … `L47_experts_k3c.bin`）。
- 每专家固定槽 **1,863,680 B**（K4SLOT under `K4_BITS=3`）
- 每层 512 专家 ⇒ 每文件 ≈ 0.95 GB，48 层 ≈ 45.9 GB（= 42.7 GiB，本机实测）
- 包名由 `K4_PACK_DIR` 决定，K3 构建的编译期默认是 `experts_k3c`
- **无此目录 → 启动即 `k4 bin missing: <path>` 抛异常**（可读报错，不是崩）

### 2. `<模型目录>/experts_k4/` — 56.7 GB（仅 K4 构建需要）

K4 4-bit 专家权重，49 个文件。每专家槽 **2,478,080 B**（其中数据区 2,476,800 B，
末尾 1,280 B padding；gate.tr@0 | up.tr@819200 | dn.tr@1638400；scale_in/out f16 @2457600+）。
- 换 K4 走：`set ARKION_EXE=bin\helm_qw38_gpu2.exe`（或对应 K4 构建）

### 3. `<模型目录>/nonexp_*.{bin,json}` — 6.9 GB ★ 必需

非专家权重（GR / GDN / QSA / mixer / lm_head / embed）+ 索引：

| 文件 | 实测大小 |
|---|---|
| `nonexp_pack.json`（索引，store/off/bytes） | 132,605 B |
| `nonexp_fp8.bin` | 2,971,074,560 B |
| `nonexp_bf16.bin` | 3,953,248,280 B |
| `ple_index.json`（PLE 128 分片索引） | 32,033 B |

### 4. `<ARK_PLE_ROOT>/` — 335.3 GB ★ 必需，且是最大的一块

原始 HF 模型仓库（本机为 `D:\qwen3.8-flash-next`，145 个文件）。引擎只用其中：

| 文件 | 用途 |
|---|---|
| `model-*.safetensors`（131 分片） | **PLE n-gram 真表**（按 `ple_index.json` 的 `src_offset` seek 读行，不是副本） |
| `tokenizer.json` | 服务层分词（`QWEN_DIR`） |
| `chat_template.jinja` | 服务层渲染（`QWEN_DIR`） |

> ⚠️ **指错仓库不报错，只出垃圾**：FP8 镜像目录的 PLE 分片曾被换成 bf16 内容，指错时
> 实测 relRMS 0.996、门禁掉到 14/16、pos11 翻边。引擎启动会打印
> `[path] ple_root = ... (来源)`，请核对。

### 5. MTP（仅 `K4_MTP=1` 时需要，默认关）

`mtp_experts_k4.bin`（1.27 GB，**注意是 K4 格式**，与宿主位宽无关）、
`mtp_nonexp_pack.json` / `mtp_nonexp_fp8.bin` / `mtp_nonexp_bf16.bin`（合计 ~125 MB）。

### 6. 同目录下**不该被用到**的东西（本机实测存在，容易指错）

| 目录 | 体积 | 说明 |
|---|---|---|
| `experts_fake3/` `experts_fake4/` | 42.7 + 56.7 GB | **测试假包，同样 48/48 完整** ⇒ 挑中它不报错，只出垃圾 |
| `experts_k3/` | 2.4 GB | 不完整（5/48），已按决策作废 |
| `chain/` | 9.2 GB | 引擎**不读**，仅离线脚本用 |
| `mtp_experts_k4.bin.corrupt` / `.nopad` / `mtp_experts_k4_deq.bin` | 1.27 + 1.27 + 10.1 GB | 实验残留，非必需 |

---

## 二、已打包进运行包的（无需外部数据）

| 文件 | 大小 | 用途 |
|---|---|---|
| `bin/arkion-q1.exe` | 3.28 MB | **唯一该发布的引擎**（`-DK4_BITS=3`） |
| `src/helm_qw38_gpu2.cu` | ~1.0 MB | 引擎源码（唯一生产源） |
| `src/_b_prod_k3.bat` | 485 B | K3 编译脚本 |
| `data/golden/qw38_golden.json` | 217 B | 门禁参照（prompt 6 + gen 16） |
| `data/golden/qw38_golden_hidden.npy` | 31,457,408 B | 逐层 hidden 参照（16×48×10240 f32） |
| `serve/*.py` | ~120 KB | 服务层（app/engine/api/tokens/think/context/chat_cli/arkion-cli/locate_model） |
| `serve/*.bat` | ~10 KB | `run_serve.bat`（启动）`chat.bat`（一键聊天）`cli.bat` |
| `_serve_env.bat` | 2 KB | 共享默认值（所有 K3 启动脚本 call 它） |
| `run_gate_k3.bat` / `run_chat_k3.bat` | ~5 KB | 门禁 / 一次性对话 |
| `docs/*.md` | ~60 KB | 本目录全部文档 |

> `bin/` 下另有 10 个历史 exe（K4/实验构建）。它们**都能由 `src/_b_*.bat` 重建**，
> 且 `run_gate.bat` / `run_chat.bat` / `run_ab_k4_k3.bat` / `run_sweep.bat` 用的是
> `bin\helm_qw38_gpu2.exe` ⇒ **开发树里别删**；但**发布包只放 `arkion-q1.exe`**。

---

## 三、编译环境（改了 .cu 才需要）

| 依赖 | 本机版本/路径 |
|---|---|
| CUDA Toolkit | v13.3（`C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3`） |
| MSVC | Visual Studio 18 Community |
| 架构 | `-arch=sm_86` |
| 第三方头 | `D:\exllamav3-src\exllamav3-master\exllamav3\exllamav3_ext`（`-DUSE_EXL3_DQ`） |
| 链接 | `-Xlinker "/STACK:16777216"`（PE 栈 1 MB 不够：`main()` 有 `K4_NT_MAX` 大小的栈数组） |

编译：`cd src && _b_prod_k3.bat` → 输出 `..\bin\arkion-q1.exe`，
打印 `NVCC_EXIT=0` 即成功。

> ⚠️ 这三条路径目前仍**写死在该 bat 里**（运行期路径已全部可配，构建期还没有）——
> 换机器编译前需要先改这三行。

---

## 四、Python 驱动环境

| 包 | 用途 |
|---|---|
| `tokenizers` | 分词（`<QWEN_DIR>/tokenizer.json`） |
| `jinja2` | 渲染官方 chat 模板 |

---

## 五、验证依赖完整性的最快方法

```bat
python serve\locate_model.py --check <模型目录>   :: 逐项校验（推荐，先跑这个）
python serve\app.py --selftest                    :: 服务层离线自检（不需要 GPU）
run_gate_k3.bat                                   :: 引擎端到端门禁
```

判据：

| 输出 | 含义 |
|---|---|
| `=== M3 GPU 端到端门禁: 15/16 PASS ===` + `pos 19 argmax=109455 margin=1.0000` | 依赖完整、引擎正确（唯一未过的 pos 11 是 `margin=0.0000` 的并列翻转） |
| `[path] FATAL: cannot open required file: <file>` | 该文件缺失 / 盘符没挂 |
| `k4 bin missing: <path>` | 专家包缺失或 `K4_PACK_DIR` 名字不对 |
| **不报错但输出是垃圾** | `ARK_PLE_ROOT` 指错仓库 ⇒ 核对 `[path] ple_root` |
| `[codecap] FATAL: ... needs X GiB but only Y GiB free` | 显存装不下码本（4.098 GiB） |
