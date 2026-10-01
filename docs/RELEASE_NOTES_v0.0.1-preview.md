# Arkion-Q1 v0.0.1-preview

**Arkion** 系列的**闭源前瞻版**。目标不是最快，而是**让低配机器也能跑**：
全量 **Qwen3.8-Flash-Next**（112B 总参 MoE，每 token 激活 10 / 512 专家）跑在
**8 GB 显存 / 16 GB 内存**的消费级单卡上。

> ⚠️ **闭源、预览版、按现状提供、无担保。** 下载即表示接受 [`EULA.md`](EULA.md)。
> 模型权重**不在本附件内**（体积原因），见下方【数据准备】。

---

## 懒人三步

1. **下载下面的 `Arkion-Q1-v0.0.1-preview.zip`**（约 37 MB），解压到任意目录
2. **双击 `START.bat`** —— 会依次：装 Python 依赖 → 引导下载权重 → 校验数据 →
   写好路径配置 → 直接进对话
3. 第一次会问你权重放哪，并显示需要下载多少（下面有清单）

> 没装 Python 的话它会提示你去装（需 3.8+，安装时记得勾 "Add python.exe to PATH"）。

## 数据准备（必须，约 385 GB）

`START.bat` 会自动下载，也可以手动：

| 从哪下 | 内容 | 体积 | 设为什么 |
|---|---|---|---|
| **[魔搭 · HQsensei/Arkion-Q1-K3](https://www.modelscope.cn/models/HQsensei/Arkion-Q1-K3)** | 我们量化的 K3 专家包 + 非专家权重 + 索引 | **49.1 GB** | `ARK_MODEL_DIR` |
| 魔搭上游的 **Qwen3.8-Flash-Next** 官方页 | PLE 真表（131 个 safetensors）+ tokenizer + 模板 | **335.3 GB** | `ARK_PLE_ROOT` |

```bat
modelscope download HQsensei/Arkion-Q1-K3 --local-dir E:\arkion-q1-k3
modelscope download <上游 owner/name> --local-dir D:\qwen3.8-flash-next
python serve\locate_model.py --write      :: 校验并写成本机配置
```

> ⚠️ **上游基座必须版本对得上。** `ple_index.json` 记的是到上游分片的**精确字节偏移**，
> 下错 revision **不会报错，只会解码出垃圾**。本包带 `MANIFEST.json`（含每个分片长度与
> 关键小文件 sha256），`START.bat` 会自动拿它校验并明确告诉你结果。

## 实测性能（RTX 3070 Ti 8 GB + 16 GB RAM，Windows）

| 场景 | 实测 |
|---|---|
| 生成 | **83–98 ms/token ≈ 10–12 tok/s** |
| 首字（默认思考模板） | **25.2 s** |
| 首字（`--no-think`） | **1.3 s**（快 20 倍，指令类任务建议用这个） |
| 上下文上限 | **65536**（已验证；131072 会让引擎在首个请求时退出） |
| 引擎启动到就绪 | 10–45 s |

**诚实说明**：同类实现（开源、专家常驻内存 + 批量 GEMM + 投机解码）公布的是生成
**53–93 tok/s**。本引擎与之**不在同一量级**（生成低 5–9 倍，提示读取低约两个数量级）——
我们换到的是**更低的内存门槛**（8 GB 显存 / 16 GB 内存）。**请不要按"快"预期它。**

## 你能自己验证它没坏（看不到源码也能验）

```bat
python serve\app.py --selftest      :: 服务层离线自检，期望 "自测全部 PASS"
run_gate_k3.bat                     :: 引擎端到端门禁，期望 "15/16 PASS"
                                    ::   唯一未过的 pos 11 是 margin=0.0000 的并列，非数值错误
set ARK_DUMP_CONFIG=1 & bin\arkion-q1.exe "E:\arkion-q1-k3" "data\golden"
                                    :: 打印全量生效参数 + 每个值的来源
```

## 这个包里有什么

```
START.bat                  双击这里（首次运行向导）
EULA.md                    先读（闭源许可）
THIRD_PARTY_NOTICES.md     第三方声明（exllamav3 MIT）
LICENSE-qwen-model.txt     上游模型许可原文（Qwen Community License 1.0）
DEPENDENCIES.md            数据清单与验收判据
MANIFEST.json              本包 sha256 + 上游指纹（版本校验用）
README.md                  本文
bin\arkion-q1.exe          引擎，多架构：sm_86 SASS+PTX / sm_89 / sm_120
serve\                     本地服务层（OpenAI 兼容 API + 命令行）
data\golden\               门禁验证数据（31 MB，非模型权重）
docs\                      操作手册 / 功能说明 / 参数表
run_gate_k3.bat            门禁自检
serve\chat.bat             直接对话
```

**不含**：模型权重、引擎源码、MTP 权重（本预览不启用投机解码）、视觉塔（引擎只用文本路径）。

## 已知限制

- **速度**：见上。不要按"快"预期。
- **单并发**：一次处理一个请求。
- **仅 Windows**：本预览只提供 Windows 二进制。
- `sm_89` / `sm_120` 已编译但**未在对应硬件上实测**（我们只有 sm_86 的卡）。
- 无工具调用 / 无 logprobs / 无 embeddings。
- 首个请求要付预填代价（默认思考模板约 200 token 的 system）。
- **预览版**：不建议用于生产。

## 许可

- 引擎与服务层（本附件）：**闭源**，适用 `EULA.md`。
- 编译期使用 MIT 许可的第三方组件（exllamav3，Copyright (c) 2025 Turboderp）：声明见 `THIRD_PARTY_NOTICES.md`。
- 模型权重：**Qwen3.8-Flash-Next**，适用 **Qwen Community License 1.0**（Copyright (c) 2026 Qwen），
  原文随包为 `LICENSE-qwen-model.txt`。我们是它的**衍生作品**（重新量化 + 重排，未改动权重语义）。
  上游许可条件 2 提醒：用于 **Model as a Service** 或 **AI Work Assistant** 类商业业务前，
  须先向 Qwen 单独取得许可。
