# 遥操接口

*[English](../../interfaces/teleop.md) | [中文](teleop.md)*

操作者输入如何变成手部目标，以及如何接入一个尚未支持的设备。

目前支持两种设备：走 ROS 2 的 Manus 手套，和走 OpenXR/WiVRn 的 Quest 3S 手部追踪。
它们在重定向之后的一切都是共用的。

## 两个角色

输入被拆成**数据源**（占有 transport，产出校验过的样本）和
**重定向器**（把样本变成手部目标）。两个契约都写在
[`src/dex_teleop_adapters/protocols.py`](../../../src/dex_teleop_adapters/protocols.py)
里，是 `typing.Protocol` 定义。它们是结构化类型：没有任何类继承它们，
运行时也不做检查，现有实现无需修改就已经满足。

```
设备 ──▶ TeleopSource ──TimestampedSample──▶ Retargeter ──TeleopHandCandidate──▶ 运行时
          校验布局/                            求解并投影到
          左右手/有效性                        具名语义关节
```

这个拆分之所以重要，是因为它是设备特异性终止的地方。重定向器的输出是具名关节；
它之上的任何代码都不知道这是手套还是头显产生的。

## 数据源必须产出什么

一个 `TimestampedSample`（`dex_contracts/identity.py`）：

| 字段 | 含义与义务 |
|---|---|
| `payload` | 你自己的 frozen dataclass 关键点，如 `ManusKeypoints`、`OpenXRKeypoints`。位置单位为**米**。 |
| `generated_time_ns` | **设备**产生该样本的时刻（如果设备提供）。未知时填 `None`，**不要**拿本地时间顶替。 |
| `received_time_ns` | 本地单调时钟的到达时刻。新鲜度以此判断。 |
| `sequence` | 每个被接受的样本严格递增。消费者靠断号检测丢包。 |
| `source_health` | 只有在样本持续到达**且**通过校验时才是 `SourceHealth.HEALTHY`。必须因陈旧而降级，而不只是因 transport 断开。 |
| `validity_mask` | 逐节点布尔。如实报告部分追踪丢失，不要填猜测值。 |
| `coordinate_frame_id` | 说明这些点在哪个坐标系，如 `manus-wrist-local-native`。 |
| `units` | `"meter"`。 |
| `diagnostics` | 可选的键值元组，会出现在遥测里。 |

校验是数据源的职责，不是重定向器的：左右手、关节布局、逐节点有效性都在这里拒绝。
数据源绝不能驱动任何东西。

## 重定向器必须做什么

`retarget()` 接收样本加会话身份，返回 `TeleopHandCandidate`，或者**抛异常**。
它不返回降级结果：样本不健康、payload 类型不对、左右手不匹配、求解失败，都是异常，
因为一个「看起来合理但其实是错的」手部姿态，比什么都不给更糟。

两个实现共同遵循的流程：

1. `source_health` 不是 `HEALTHY` 或 payload 类型不符 → 拒绝
2. 对照已加载的 profile 校验左右手
3. 把设备的关节布局重映射到求解器的 21 关节 MANO 布局
4. 平移到腕部原点，估计腕部坐标系，旋转进 MANO 规范系
5. 运行 DexPilot 求解器
6. 把求解结果投影到标定中**具名**的语义关节
7. 应用 profile 的拇指偏置与低通滤波，然后校验限位

重定向器是有状态的——滤波器状态和求解器热启动会跨调用保留。这正是 `reset()` 存在的原因，
也是它必须在会话开始时以及任何追踪丢失之后被调用的原因。

## 坐标系约定

这里最容易出错，而且错了不会报警。两种设备的目标是一样的：21 个关键点，
腕部在原点，旋转进 MANO 规范系——这是 `dex-retargeting` 期望的输入。

**Manus**（`manus_math.py::manus_to_joint_pos`，文件内有中文注释）：

1. Manus Core 输出的是**左手系** → 取反 X 转为右手系。
2. 通过 `MANUS_TO_MP` 把 25 个原生节点重映射到 21 槽位的 MediaPipe 布局。
3. 平移到腕部原点（节点 0 本来就是原点；保留这一步是防御发布端 `HandMotion` 设置变化）。
4. 估计腕部坐标系，再乘 `OPERATOR2MANO_LEFT/RIGHT`。

**OpenXR**（`openxr.py::openxr_to_joint_pos`）：26 个 `XR_EXT_hand_tracking` 关节
→ 同样的 21 关节布局，然后是同样的腕部坐标系与 MANO 旋转。输入本身就是右手系，
所以没有取反那一步。

**腕部坐标系估计**（`hand_frame.py::estimate_frame_from_hand_points`）只用三个
MediaPipe 点——腕部 (0)、食指 MCP (5)、中指 MCP (9)。它用 SVD 拟合平面，
构造正交基，并用小指方向来消除法向量的符号歧义。新设备只需要把关键点正确地放进
21 槽位布局，这一步是共用的。

## 遥操 profile

profile 把「设备 + 手」这一组合中所有不允许漂移的东西钉死。见
`configs/teleop/linker_g20_left_openxr_dexpilot_v1.json`。

| 字段 | 作用 |
|---|---|
| `profile_id`、`profile_version` | 身份 |
| `hand_model`、`hand_side` | 必须与部署配置和标定一致 |
| `semantic_schema_id`、`semantic_schema_digest` | 按**内容**绑定到确切的 schema |
| `semantic_joint_names` | 16 个输出关节，**有序**。这个顺序就是全系统的规范关节顺序。 |
| `retargeting_config` | DexPilot 求解器 YAML 的路径 |
| `retargeting_config_sha256` | 该 YAML 的摘要——改了它却不更新这里，会导致加载失败 |
| `low_pass_alpha` | 输出滤波系数 |
| `thumb_cmc_roll_bias_rad` | 显式的、由 profile 拥有的拇指修正（而不是埋在代码里的常量） |
| `source_coordinate_conversion` | 说明所应用的坐标转换，如 `manus-native-left-handed-negate-x-to-right-handed` |
| `filter_reset` | 何时必须调用 `reset()`：`session-start-and-tracking-recovery` |
| `digest_algorithm` | `sha256-canonical-json-excluding-profile-digest` |
| `profile_digest` | 对规范 JSON（排除 `profile_digest` 自身）计算的摘要 |

全都内容寻址是有意为之：被悄悄改过的求解器配置会改变手最终去到哪里，
所以它必须表现为加载失败，而不是一个意外。

## 接入一个新设备

1. **定义关键点 payload。** 一个 frozen dataclass，位置单位为米，并带一个
   `layout_id` 标明你的布局。照着 `OpenXRKeypoints` 写。

2. **写数据源。** 实现 `TeleopSource`：`start(callback)`、`stop(timeout_s)`、
   `status(now_ns)`。自己管理 transport 线程，发布前校验左右手/布局/有效性，
   并保持回调轻量——运行时的回调只往 `LatestValueBuffer` 里塞。ROS 源参照
   `manus.py`，数据报源参照 `tools/control_console/` 下的 UDP 源。

3. **映射进 21 关节布局。** 这是唯一真正新的工作。仿照 `manus_to_joint_pos` 写一个
   `<device>_to_joint_pos()`：修正手系、重映射下标、平移到腕部原点，然后复用
   `estimate_frame_from_hand_points` 和 `OPERATOR2MANO_*` 旋转。
   **不要**自己另写一个腕部坐标系估计。

4. **写重定向器。** 实现 `Retargeter`。大部分是共用结构；复制
   `openxr.py::OpenXRRetargeter` 然后替换第 3 步。求解器参考系复用 `compute_ref_value`。

5. **加一个遥操 profile。** 复制一个现有 JSON，设置你的
   `source_coordinate_conversion`，并对排除 `profile_digest` 后的规范 JSON
   重新计算摘要。

6. **无硬件测试。** 喂入录制的或合成的样本，对产出的候选值做断言。
   `tests/test_manus.py` 覆盖数据源校验（左右手、布局、序号、陈旧），
   `tests/test_openxr_telemetry.py` 覆盖端到端的 UDP 源。这里的惯例是 golden trace
   对比，见 `tests/fixtures/golden/`。

有两件事你**不需要**做：不需要在任何地方注册设备（composition 里显式接线），
也不需要动 `dex_runtime` 或 `dex_hardware_linker` 里的任何东西——
导入契约禁止你的适配器依赖它们，而且接入新设备本来也不应该需要。
