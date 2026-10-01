# Arkion-Q1 — v0.0.1-preview

**Arkion** 系列的**闭源前瞻版**。目标不是最快，而是**让低配机器也能跑**：
全量 Qwen3.8-Flash-Next（112B MoE，每 token 激活 10 个专家）在
**8 GB 显存 / 16 GB 内存**的消费级单卡上运行。

> ⚠️ **闭源预览版，按现状提供，无担保。** 使用前请阅读 [`EULA.md`](EULA.md)。
> 模型权重**不在本包内**，由发布者在**魔搭（ModelScope）**单独发布。

> ⚠️ **English**: Arkion-Q1 is a **closed-source preview** of a MoE inference engine that
> runs a 112B-parameter model on an **8 GB VRAM / 16 GB RAM** consumer machine.
> Provided **as-is, no warranty**. Not open source — see `EULA.md`.
> Model weights are published separately on **ModelScope**. This package contains
> only the engine binary, the local serving layer, and the verification data.

---

## 实测性能（RTX 3070 Ti 8 GB + 16 GB RAM，Windows）

| 场景 | 实测 |
|---|---|
| 生成 | **83–98 ms/token ≈ 10–12 tok/s** |
| 首字延迟（默认思考模板 ~200 token system） | **25.2 s** |
| 首字延迟（`--no-think`，提示仅 19 token） | **1.3 s** |
| 上下文上限 | **65536**（已验证；131072 会让引擎在首个请求时退出） |
| 引擎启动到就绪 | 10–45 s |

**诚实说明**：同类实现（开源、常驻内存专家 + 批量 GEMM + 投机解码）公布的是
生成 53–93 tok/s。本引擎与之**不在同一量级**（生成低 5–9 倍，提示读取低约两个数量级），
原因是架构选择不同：本引擎把专家权重放在磁盘/RAM 并**流式读入显存**，因而换来了**更低的内存门槛**。
**请不要按"快"预期它。**

## 需要什么

| 组件 | 要求 |
|---|---|
| GPU | **8 GB 显存**，已编译目标：sm_86（实测）/ sm_89 / sm_120 |
| 内存 | 16 GB |
| 磁盘 | **≈ 385 GB**（模型数据，需自行准备） |
| 系统 | Windows（本预览仅含 Windows 二进制） |

**本包内不含模型数据。** 数据清单与获取方式见 [`DEPENDENCIES.md`](DEPENDENCIES.md)。

## 快速开始

```bat
:: 0) 先读 EULA.md。继续即视为接受。

:: 1) 告诉引擎模型在哪（引擎不会自己找；它只读固定文件名）
python serve\locate_model.py --check E:\qw38     :: 校验目录（逐项列缺失）
python serve\locate_model.py --write             :: 写成本机配置 arkion.local.bat

:: 2) 一键聊天（服务没起会自动拉起，约 45 s）
serve\chat.bat
serve\chat.bat --once "1+1=?" --no-think         :: 单发、不思考（快 20 倍）

:: 3) 起 HTTP 服务（OpenAI 兼容）
serve\run_serve.bat                              :: http://127.0.0.1:8471
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8471/v1", api_key="sk-arkion-local")
r = client.chat.completions.create(
    model="arkion-q1-k3",
    messages=[{"role": "user", "content": "你好"}],
    max_tokens=512, reasoning_effort="medium")   # minimal / low / medium(默认) / high
print(r.choices[0].message.content, r.choices[0].message.reasoning_content)
```

## 你可以自己验证它没坏（看不到源码也能验）

这是本项目的差异点：**闭源，但可复现**。

```bat
python serve\app.py --selftest     :: 服务层离线自检（不需要 GPU），期望"自测全部 PASS"
run_gate_k3.bat                    :: 引擎端到端门禁，期望"15/16 PASS" + pos19 argmax=109455
set ARK_DUMP_CONFIG=1 & arkion-q1.exe "E:\qw38" "data\golden"
                                   :: 打印全量生效参数 + 每个值的来源；被资源闸改写时不会静默
```

门禁里唯一未过的 `pos 11` 是 `margin=0.0000`（两个候选 logit 完全相等的**并列**），
不是数值错误 —— 这一条我们写进文档而不是藏起来。

## 文档

| 文件 | 内容 |
|---|---|
| [`EULA.md`](EULA.md) | **先读这个**（闭源许可条款） |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | 操作手册：数据准备 / 启动 / 用法 / 参数 / 排查 |
| [`docs/PREVIEW_0.1.md`](docs/PREVIEW_0.1.md) | 功能与限制全貌 |
| [`DEPENDENCIES.md`](DEPENDENCIES.md) | 数据清单与验收判据 |
| [`docs/CONFIG_REFERENCE.md`](docs/CONFIG_REFERENCE.md) | 参数总表 |

## 已知限制

- 速度见上（不要按"快"预期）
- **单并发**：一次一个请求
- 无工具调用 / 无 logprobs / 无 embeddings
- 仅 Windows；`sm_89`/`sm_120` 已编译但**未在对应硬件上实测**
- 首个请求付预填代价（默认思考模板约 200 token 的 system）
- 预览版，不建议用于生产
