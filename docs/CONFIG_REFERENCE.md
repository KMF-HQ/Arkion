# Arkion 配置参考（自动生成，勿手改）

> 来源：`src/helm_qw38_gpu2.cu` 的 `ARK_CFG_TABLE`——本文件由 `_think/export_config_doc.py` 从代码导出，
> 因此不存在文档与代码漂移。修改配置请改代码里的表，然后重新导出。

已注册参数：**50** 项（分批推进：其余历史 env 待注册）。

## 命名规则

- 推荐名：`ARK_<语义>`；历史名（`K4_*`、通用名）为**兼容别名**，仍可读取并打印一次性弃用提示。
- 读取优先级：`ARK_*` 新名 > 旧名 > 代码内默认。
- 启动加 `ARK_DUMP_CONFIG=1` 打印全部生效值及来源（new/OLD/default）。

## 快速开始（生产推荐）

```
ARK_QSA=1  ARK_TEMP=1.0  ARK_TOPP=0.95  ARK_SAMPLE_TOPK=20   # 官方采样；思考任务禁用 topk=1
ARK_REPEAT_PENALTY=1.0                                        # 长生成护栏（可选）
ARK_DUMP_CONFIG=1                                             # 启动自检
```

## MODE

| 推荐名 | 旧名（兼容） | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `ARK_CHAT` | `CHAT` | flag | `0` | autoregressive chat mode |
| `ARK_PPL` | `PPL` | flag | `0` | teacher-forced PPL baseline |
| `ARK_PROMPT_FILE` | `PROMPT_FILE` | str | `-` | prompt ids file (one id per line) |
| `ARK_MAX_TOKENS` | `GEN_MAX` | int | `64` | generation cap (chat KV = NPRE + this + 8) |
| `ARK_DUMP_CONFIG` | `—` | flag | `0` | print full config snapshot at startup |

## SAMPLE

| 推荐名 | 旧名（兼容） | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `ARK_TEMP` | `QTEMP` | float | `1.0` | temperature (official 1.0) |
| `ARK_TOPP` | `TOPP` | float | `0.95` | top-p (official 0.95) |
| `ARK_SAMPLE_TOPK` | `TOPK` | int | `20` | sample top-k (official 20; 1=greedy, avoid for thinking) |
| `ARK_SEED` | `SEED` | int | `0` | rng seed (0 = default) |
| `ARK_REPEAT_PENALTY` | `K4_RPEN` | float | `0` | repetition penalty subtract amount (0 = off) |
| `ARK_REPEAT_WINDOW` | `K4_RPENW` | int | `256` | repetition penalty window |
| `ARK_NO_EOS` | `K4_NOEOS` | flag | `0` | ignore EOS and keep generating (stress) |

## ATTN

| 推荐名 | 旧名（兼容） | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `ARK_QSA` | `K4_QSA` | flag | `0` | Qwen sparse attention (KV>2048; recommended on) |
| `ARK_QSA_DENSE` | `K4_QSA_ATTN` | flag | `1` | 0 = dense scan attention instead (diagnostic) |
| `ARK_KV_TIER` | `K4_KVTIER` | flag | `0` | 3-tier KV (experimental, known 1e-5 drift) |
| `ARK_KV_RING_BLOCKS` | `K4_KVW` | int | `1024` | KVTIER vram ring blocks (x4 rows) |

## DEBUG

| 推荐名 | 旧名（兼容） | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `ARK_KV_PROBE` | `K4_KVP` | flag | `0` | KV length probe |
| `ARK_KV_CHK` | `K4_KVCHK` | flag | `0` | KVTIER demotion readback check |
| `ARK_PPLDUMP` | `K4_PPLDUMP` | flag | `0` | per-position logprob dump |

## CACHE

| 推荐名 | 旧名（兼容） | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `ARK_HOT_CAP` | `K4CAP` | int | `52` | RAM pinned cache slots/layer (coupled to SKIP_COLD) |
| `ARK_HOT_N` | `K4HOT` | int | `8` | vram hot slots/layer (clamped by vram budget gate) |
| `ARK_ENT_RAM_FRAC` | `ENT_RAM_FRAC` | float | `0.38` | pinned pool max fraction of physical RAM |
| `ARK_POOL_CHUNK` | `K4_POOL_CH` | int | `256` | pinned pool alloc chunk |
| `ARK_EXPERT_SKIP_TAU` | `K4_SKIP_TAU` | float | `0` | tail expert skip threshold (0 = off) |
| `ARK_EXPERT_SKIP_DB` | `K4_SKIP_DB` | float | `0.10` | skip deadband (discrete-decision amplifier) |
| `ARK_EXPERT_SKIP_COLD` | `K4_SKIP_COLD` | int | `2` | cold-expert rule mode (0/1/2) |
| `ARK_ROUTE_QUANT` | `K4_ROUTE_Q` | float | `0` | route decision quantization (0 = bit-identical) |
| `ARK_ROUTE_HYST` | `K4_ROUTE_HYST` | float | `0` | route hysteresis (0 = bit-identical) |

## IO

| 推荐名 | 旧名（兼容） | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `ARK_IO_LEVEL` | `K4ION` | int | `6` | io queue threads |
| `ARK_IO_SORT` | `IO_SORT` | flag | `0` | sort batch by disk offset |
| `ARK_IO_NOBUF` | `IO_NOBUF` | flag | `0` | unbuffered direct read |
| `ARK_IO_PFPRIO` | `IO_PFPRIO` | flag | `1` | demand read high priority |
| `ARK_PRIORITY` | `PRIORITY` | int | `2` | process/gpu scheduling priority |

## PREFETCH

| 推荐名 | 旧名（兼容） | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `ARK_PREFETCH_K` | `XL_K` | int | `0` | cross-layer prefetch slots (0 = off) |
| `ARK_PREFETCH_RATIO` | `XL_PRATIO` | float | `0` | prefetch confidence relative threshold |
| `ARK_PREFETCH_TAU` | `XL_PF_TAU` | float | `0` | prefetch absolute weight threshold |
| `ARK_PREFETCH_DEPTH` | `XL_D` | int | `1` | cross-layer prefetch depth 1..3 |
| `ARK_PREFETCH_MAX` | `XL_PFMAX` | int | `0` | union prefetch budget cap (0 = unlimited) |
| `ARK_PRED_FREQN` | `PRED_FREQN` | int | `10` | predictor frequent candidates |

## COMPUTE

| 推荐名 | 旧名（兼容） | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `ARK_ALL_LEVERS` | `K4_ALL` | flag | `0` | all layers on batch path |
| `ARK_MMA` | `K4_MMA` | flag | `0` | tensor-core path master switch |
| `ARK_MMA_GDN` | `K4_MMA_GDN` | flag | `0` | GDN projection tensor cores |
| `ARK_MMA_ATTN` | `K4_MMA_ATTN` | flag | `0` | attention q/k/v tensor cores |
| `ARK_NT` | `K4_NT` | int | `1` | positions per step 1..8 (batch tier) |
| `ARK_NSPLIT` | `NSPLIT` | int | `5` | sub-reads per expert slot (divides 605, 4K aligned) |
| `ARK_MOE_GRAPH` | `K4_MOEGRAPH` | flag | `0` | MoE CUDA graph capture |
| `ARK_LAYER_GRAPH` | `K4_LGRAPH` | flag | `0` | layer-fragment graph capture |
| `ARK_QKV_FUSE` | `QKV_FUSE` | flag | `0` | fused q/k/v launch |
| `ARK_PLE_GPU` | `PLE_GPU` | flag | `0` | PLE projection on GPU |
| `ARK_RELEASE_CODE` | `RELEASE_CODE` | flag | `1` | release host codebook (saves RAM) |
