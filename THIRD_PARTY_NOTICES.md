# 第三方声明（THIRD PARTY NOTICES）

本软件（Arkion-Q1 引擎与其服务层）在**编译期**使用了下列第三方组件的**头文件/源码片段**。
按各自许可的要求，其版权声明与许可全文列举如下。

> 本文件随二进制分发。**这一点是强制的**：MIT 许可要求"上述版权声明与本许可声明
> 应包含在本软件的所有副本或重要部分中"。缺了它，闭源分发不合规。

---

## 1. exllamav3（EXL3 trellis 反量化）

- **用途**：编译开关 `-DUSE_EXL3_DQ`，使用 `exllamav3_ext` 提供的 EXL3 trellis 解码路径。
- **来源**：`D:\exllamav3-src\exllamav3-master\exllamav3\exllamav3_ext`
- **许可**：MIT

```
MIT License

Copyright (c) 2025 Turboderp

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. 模型基座（不随本软件分发）

本软件**不含模型权重**。运行所需的基座模型 **Qwen3.8-Flash-Next** 由 **魔搭（ModelScope）**
上游页面分发，适用 **Qwen Community License 1.0**（Copyright (c) 2026 Qwen）——
全文见随包分发的 `LICENSE-qwen-model.txt`，或上游仓库的 `LICENSE`。

我们的 K3 量化包是它的**衍生作品**（重新量化 + 重排，未改动权重语义），
同样按 Qwen Community License 1.0 分发，并已随包附上上游许可全文与其版权声明。

> 提醒（上游许可条件 2）：若要把本软件或其输出用于 **Model as a Service** 或
> **AI Work Assistant** 类商业业务，须先向 Qwen 单独取得许可。
