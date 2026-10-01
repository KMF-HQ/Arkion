# Arkion-Q1 Preview — 消费级推理引擎前瞻版

> **版本**：Preview 0.1（2026-10-01 修订）
> **模型**：Qwen3.8-Flash-Next（112B 总参数 MoE，24,576 专家 / 每 token 激活 10）
> **量化**：K3 3-bit EXL3 trellis（默认）/ K4 4-bit（可选）
> **协议**：OpenAI 兼容 API + SSE 流式
> **实测平台**：**8 GB 显存 / 16 GB 内存** 的单卡 Windows 机器（sm_86）

---

## 这是什么

Arkion-Q1 是一个面向**低配消费级硬件**的生产级 MoE 推理引擎。目标不是跑得最快，而是
**让 8 GB 显存的机器也能跑全量 Qwen3.8-Flash-Next** —— 这一点是当前定位的核心。

**核心设计：三级存储 + 专家流式调度**

```
VRAM 热点缓存 → RAM pinned LRU → 磁盘 IO（冷数据）
```

专家权重按需从磁盘 → 内存 → 显存三级调度，配合预取与重叠，把 42.7 GB 的 3-bit 专家权重
在 **8 GB 显存**上跑起来。代价是速度：详见下方「性能（实测）」，**不要按"快速"预期它**。

---

## 性能（实测，2026-10-01，8 GB / 16 GB 单卡机）

### 生成（单流 decode，K3 默认档）

| 场景 | 实测 | 说明 |
|---|---|---|
| 生成速度 | **83–98 ms/token ≈ 10–12 tok/s** | 直连 82.5 / HTTP 非流式 89.3 / 流式含思考 98 |
| 首字延迟（默认思考模板） | **25.2 s** | 提示 297 token，其中约 200 是模板的 system |
| 首字延迟（`--no-think`） | **1.3–1.4 s** | 提示仅 19 token ⇒ **指令类任务请用 `--no-think`，差 20 倍** |
| 引擎启动到 READY | 10–45 s | 取决于页缓存冷热 |

> ⚠️ **本节数字来自本机实测**，与旧版文档中的「生成 30-40 ms/tok」「首 token ~10s」不符 ——
> 那是错的，已更正。旧文档里的「预填充 ~25s / ~200 token」是**对的**（实测 25.2 s），
> 只是没写清它是 prefill（首字延迟）而不是生成。

### 预填（prefill）

| 场景 | 实测 |
|---|---|
| 单流预填 | ~85 ms/token（297 token → 25.2 s） |
| 批量预填（实验性，见下） | np=83 → **78.4** / np=219 → **60.4** / np=512 → **37.0** ms/位置 |
| 折合吞吐 | 12.8 / 16.6 / **27.0** 位置/s |

> ⚠️ **批量路径是实验性的、不是生产配置**：实测量到 np>6 起质量就明显劣化
> （np=11 时 PPL 10.4）。它目前的用途是容量/大 M 的开发实验，**没有**达到可用于生产的状态。

### 同类实现的量级参考

同一基座模型的其它消费级实现（llama.cpp/ggml 路线）公布的量级是**生成 53–93 tok/s、
提示读取 1600–2200 tok/s**。本引擎与之**不在同一量级**（生成低 5–9 倍，提示读取低约
两个数量级），这是架构差异造成的：对方用常驻内存的专家 + CPU/GPU 并行计算 + 真批量
GEMM + 投机解码，本引擎目前是**专家从磁盘流式读取 + 逐位置 GEMV**，且投机解码（MTP）
代码存在但默认关闭。**对外描述时请不要暗示速度竞争力**；本引擎的价值在硬件下限。

### 上下文与显存

| 项 | 实测 |
|---|---|
| 已扫描验证的最大 `SERVER_CTX` | **65536**（4096/8192/16384/32768 亦通过） |
| **131072** | **会让引擎在第一个请求时死掉**（不是变慢，是进程消失、HTTP 只拿到空 200 流） |
| 码本常驻显存 | 4.035 GiB（按 artifact 实测）+ 余量 → **4.098 GiB 上限** |
| 分配码本后可用显存 | 0.46–0.76 GiB（本机） |

---

## 硬件要求（实测，不是理论值）

| 组件 | **能跑的下限** | 推荐 |
|---|---|---|
| GPU | **8 GB 显存**、sm_86（RTX 30 系） | 12–24 GB（更快，且能开更大 `SERVER_CTX`） |
| RAM | **16 GB** | 32 GB+ |
| 磁盘 | **≈ 385 GB**（见下） | NVMe SSD（专家是流式读取，磁盘直接影响速度） |
| 驱动 / CUDA | 较新的 NVIDIA 驱动；本机 CUDA 13.3 编译 | — |

**磁盘 385 GB 的构成**（这是本版本最大的"隐性门槛"）：

| 内容 | 体积 |
|---|---|
| `experts_k3c/`（48 个专家包，K3 必需） | 42.7 GB |
| `nonexp_fp8.bin` + `nonexp_bf16.bin` + 索引 | 6.9 GB |
| PLE 分片（原始 bf16 仓库 `ARK_PLE_ROOT`，运行必需） | **335.3 GB** |

> PLE 是绝对大头。若能把 PLE 折进量化包，磁盘需求可降一个数量级 —— **待办**。

---

## 核心特性

### 完整的思考模型能力
- Qwen3.8-Flash-Next 原生思考模式（`reasoning_content`）
- 默认内置结构化思考模板（任务分级、阶段审查、停止条件）
- `reasoning_effort`：`minimal` / `low` / **`medium`（默认，2026-10-01 起）** / `high`
- 思考段与正文段在流式输出中分离

### K3 3-bit 近无损量化
- 抽象推理测试：96.7%（K4 4-bit 98.3%）
- 专家体积：42.7 GB（K4 为 56.7 GB）
- EXL3 trellis 量化 + 自定义 CUDA kernel

### OpenAI 兼容 API
- `POST /v1/chat/completions`（流式 + 非流式）、`GET /v1/models`、`GET /healthz`
- `stream_options.include_usage`、`stop`、`temperature`、`top_p`、`top_k`
- `reasoning_content`（思考内容）
- 扩展端点：`/arkion/v1/config`（运行时配置查询）、`/arkion/v1/cancel`（取消生成）
- OpenAI Python SDK 零修改接入

### 工程可验证性（本项目的差异点）
- 端到端门禁：22 个位置、16 项 golden 比对，**本机基线 15/16**
  （唯一未过的 pos 11 是 `margin=0.0000` 的**并列翻转**，不是数值错误）
- PPL 逐位判据：批量路径 `mean_logprob=-0.557593` 三臂逐位一致
- `ARK_DUMP_CONFIG=1` 打印**全量生效参数 + 每个值的来源**（new / 旧名 / 默认）
- 参数被资源闸改写时**绝不静默**，打印 `[ARKION-CFG] CLAMP 请求值 -> 生效值 (原因)`

---

## 快速开始

### 1) 准备数据

引擎**不会自己找模型** —— 它只在给定目录里读固定文件名。所以先定位并校验：

```bat
python serve\locate_model.py                     :: 扫盘找候选（只报告，不采纳）
python serve\locate_model.py --check E:\qw38     :: 校验某个目录
python serve\locate_model.py --write             :: 把最佳候选写进 arkion.local.bat
```

手工指定也可以（优先级：**命令行参数 > 环境变量 > 报错**，没有任何写死的回落路径）：

```bat
set ARK_MODEL_DIR=E:\qw38
set ARK_PLE_ROOT=D:\qwen3.8-flash-next
```

必需文件清单见 `DEPENDENCIES.md`。「可用」的判据是：索引 + 两个 .bin + `ple_index.json`
+ 48 个专家包齐全。

### 2) 启动

```bat
serve\chat.bat                  :: 一键：服务没起就自动拉起，然后进交互命令行
serve\run_serve.bat             :: 只起服务（http://127.0.0.1:8471）
```

### 3) 调用

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8471/v1", api_key="sk-arkion-local")

resp = client.chat.completions.create(
    model="arkion-q1-k3",
    messages=[{"role": "user", "content": "解释一下量子纠缠"}],
    max_tokens=500,
    reasoning_effort="medium",      # minimal(不思考) / low / medium(默认) / high
)
print(resp.choices[0].message.content)
print("思考:", resp.choices[0].message.reasoning_content)
```

```bash
curl http://127.0.0.1:8471/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"arkion-q1-k3","messages":[{"role":"user","content":"你好"}],
       "max_tokens":100,"stream":true}'
```

### 4) 自检

```bat
python serve\app.py --selftest     :: 服务层离线自检（Mock，不占 GPU/端口）
run_gate_k3.bat                    :: 引擎端到端门禁，期望 "M3 GPU 端到端门禁: 15/16 PASS"
```

---

## 已知限制

- **速度**：见上文实测。不要按"快"预期它。
- **单并发**：一次处理一个请求，后续排队。
- **上下文上限 65536**：已验证的最大值；131072 会让引擎在首个请求死掉。
- **批量（np>1）不可用于生产**：np>6 起质量劣化，仅用于开发实验。
- **无投机解码**：MTP 代码在，默认关闭。
- **无工具调用**：`tool_calls` / function calling 未实现（P1）。
- **无 logprobs / 无 embeddings**：引擎为 argmax 解码。
- **仅 Windows**：当前只有 Windows 编译通过。
- **首个请求会付预填代价**：默认思考模板约 200 token 的 system，每次新会话都要付一遍。
- **预览版本**：不建议用于生产环境。

---

## 路线图

| 版本 | 核心特性 |
|------|---------|
| **Preview 0.1** ✅ | K3 默认 + OpenAI API + 思考模板 + 低配（8 GB 显存）能跑 |
| P1 | 工具调用 + 真批量 GEMM（预填提速）+ 投机解码启用 + Linux |
| P2 | 多并发 + 视觉输入 + PLE 折进量化包（降磁盘需求） |

---

## 许可证

本项目仅用于研究和学习目的。模型权重归 Qwen 团队所有。
