# 策略（RL）接口

*[English](../../interfaces/policy.md) | [中文](policy.md)*

训练好的策略如何打包、校验与执行，以及如何部署你自己训练的策略。

本仓库不做训练。训练在 `dex-forge` 里；策略以**策略包**的形式进入这里——
自描述的目录，携带足够的元数据来证明它属于这只手、这份标定、这个控制周期。

## 为什么是「包」而不是 checkpoint

一个裸的 `.pth` 说不出它是为哪只手训练的、关节顺序是什么、控制周期是多少、
假设的动作边界是多少。把它加载到错误的标定上，会产出自信而错误的关节角。

策略包把上面每一条假设都变成显式且可机器校验的，而且它是内容寻址的——
「这是同一个包」是一个可以被**验证**的断言，而不是需要被信任的说法。

```
package/
├── manifest.json        # 所有假设，显式写明
├── actor.safetensors    # 策略网络权重
└── adapter.safetensors  # 历史压缩网络权重
```

随时校验（不会打开任何硬件）：

```bash
dex-runtime verify-package PACKAGE --allow-unsigned-local
```

## manifest

由 [`policy_package.py`](../../../src/dex_runtime/policy_package.py) 中的
`_validate_manifest_structure()` 校验。校验是严格且完全的：未知字段、缺失字段、
维度不一致，全都是加载错误。不存在部分接受。

### 身份

| 字段 | 说明 |
|---|---|
| `package_format` / `package_format_version` | `dex-policy-package` / `1` |
| `protocol_version` | 必须与运行时的 `PROTOCOL_VERSION` 一致 |
| `package_id`、`package_digest` | 对规范 JSON（排除摘要字段自身）取 `sha256:`。加载时重算并比对。 |
| `display_name` | 操作者在武装确认提示里看到的人类可读名称 |
| `task.id`、`task.version` | 会带进每一条命令的身份信息 |
| `supported_runtime_api.min` / `.max` | 该包接受的运行时版本区间 |
| `trust.mode` | `unsigned-local`，这正是加载需要 `--allow-unsigned-local` 的原因 |

### 手部绑定

| 字段 | 说明 |
|---|---|
| `hand.model`、`hand.side` | 必须等于部署配置里的手 |
| `hand.semantic_schema_id`、`hand.semantic_schema_digest` | 按**内容**绑定到确切的关节 schema |
| `calibration_compatibility[]` | `{calibration_id, artifact_digest}` 列表。当前运行的标定必须出现在其中，否则拒绝加载。不允许为空。 |
| `control_period_ns` | 训练时的控制周期。观测必须以完全相同的间隔到达。 |

### 观测编码

`proprio_codec` 是一个 `ProprioCodecSpec`（[`codecs.py`](../../../src/dex_runtime/codecs.py)）：

| 字段 | 含义 |
|---|---|
| `codec_id` | 如 `linker-g20-mounted-proprio-v1` |
| `joint_count` | G20 为 16 |
| `frame_dim` | `2 × joint_count`——测量位置与生效目标拼接 |
| `history_length` | 环形缓冲深度，如 30 |
| `actor_frame_count` | actor 直接消费多少帧 |
| `measured_position_scaling` | `identity-radians` 或 `affine-limits-to-minus-one-one` |
| `measured_lower_rad` / `measured_upper_rad` | 仿射缩放时的边界 |

一帧观测是 `[测量位置, 生效目标]`。测量值可选地按
`2·(x − lower)/(upper − lower) − 1` 归一化到 `[-1, 1]`；生效目标原样传递。

`actor_input_assembler` 必须是 `latest-frames-flatten`，其 `frame_count` 与
`output_width` 必须与 codec 一致。

### 动作解码

`action_transform` 必须是 `bounded-delta-position`：

| 字段 | 含义 |
|---|---|
| `action_clip` | `[-1.0, 1.0]`，原始动作被截断到此范围 |
| `delta_scale_rad` | 每单位动作对应的弧度 |
| `position_lower_rad` / `position_upper_rad` | 最终目标被逐关节截断到的边界 |
| `integration_semantics` | `acknowledged-effective-target-plus-delta` |

即：`target = clamp(effective_target + delta_scale_rad · clamp(action, −1, 1), lower, upper)`。

动作是**在已确认状态上的增量**，不是绝对位置。如果一条命令丢了，
从「我们发了什么」积分会把下一个动作叠加到手从未到达过的位置上；
从已确认的 `EffectiveHandTarget` 积分则会安全降级。

策略包的边界**不是**安全包络。部署配置里的 `HandSafetyLimits` 会在每条命令上独立校验，
策略包只能比部署更保守，绝不能更宽松。

### 网络

| 字段 | 含义 |
|---|---|
| `network.actor` | `mlp_units`、`activation`、`obs_dim`、`proprio_dim`、`latent_dim`、`action_dim`、`normalize_input`、`clip_obs` |
| `network.adapter` | `architecture_id`（`proprio-adapt-tconv-v1`）、`frame_dim`、`history_length`、`output_dim`、`frame_encoder_units`、`temporal_convolutions[]` |
| `weights.actor` / `weights.adapter` | `{path, format: safetensors, sha256}`——加载前会校验摘要 |

adapter 把历史窗口压缩成一个小的 latent，actor 消费 `[展平的 proprio, latent]`。
维度必须与 codec 一致，不一致会是加载错误，而不是运行时的 shape 崩溃。

### 时序、历史、溯源

| 字段 | 含义 |
|---|---|
| `history.length`、`.reset_semantics`、`.activation_requires_full_history` | `collect-fresh-effective-targets`；激活要求历史攒满 |
| `state_requirements.fields` | 必须包含 `semantic_position` 与 `last_effective_target` |
| `state_requirements.acknowledgement_level` | 硬件必须能提供的最低确认强度 |
| `state_requirements.maximum_state_age_ns`、`maximum_effective_target_age_ns` | 策略假设的新鲜度 |
| `task_frame` | 腕部/任务坐标系、位置与姿态包络、夹具假设 |
| `provenance` | `training_commit`、`training_dirty`、解析后的配置摘要、URDF 与资产摘要 |
| `evaluation` | `results`、`promotion_status`（如 `commissioning`） |
| `readiness_provider_ids` | 该策略在武装前所需的证据 |

`provenance.training_dirty` 记录训练时代码树是否有未提交改动。它不被强制，
但当策略表现与其评估结果不符时，这是第一个该看的字段。

## 执行

`PolicySession`（[`policy_session.py`](../../../src/dex_runtime/policy_session.py)）：

```
LOADED --reset--> SHADOW --activate--> ACTIVE
                    ^                    |
                    |                deactivate
                    +---- reset ---- DEACTIVATED --close--> CLOSED
```

| 方法 | 可用状态 | 作用 |
|---|---|---|
| `reset(measured, effective_target, …)` | LOADED、DEACTIVATED | 清空历史，播种生效目标，进入 SHADOW |
| `observe(measured, effective_target, tick, scheduled_time_ns, state_sequence)` | SHADOW、ACTIVE | 追加一帧。强制 tick 连续、周期精确、状态序号递增。 |
| `preview()` | SHADOW、ACTIVE | 运行推理，返回 `PolicyHandCandidate`。按 tick 缓存。不下发任何命令。 |
| `activate(tick, control_epoch)` | SHADOW | 提升为 ACTIVE。要求同一 tick 已有 preview，且 epoch 严格递增。 |
| `step(...)` | ACTIVE | 先 `observe()` 再 `preview()` |
| `deactivate()` / `close()` | — | 交还控制权，或结束 |

这里有两点是刻意设计的。

**推理发生在 SHADOW。** 在遥操仍然持有手的时候，策略就在运行、填充历史，
它提出的目标会被安全监管器校验并写入 trace。等到它被激活时，
第一个目标是从当前已生效的目标延续出来的——这正是切换无跳变的原因——
而且一个会违反包络的策略，在它拿到手**之前**就已经暴露了。

**激活复用已 preview 的候选值。** `activate()` 要求同一 tick 的 preview 并返回它，
而不是重新跑一次推理。真正交出控制权的那条命令，就是刚刚被校验过的那一条。

周期违规是硬错误。历史上的时序卷积假设窗口是均匀采样的，
所以静默接受一个跳过或重复的 tick，会在毫无信号的情况下改变策略看到的东西。

## 部署你自己的策略

1. **从训练侧导出。** 你需要一个 `deploy.pth` 以及导出器产生的元数据。

2. **重新打包。** `tools/build_demo_policy.py::build_g20_demo_package` 是完整范例。
   它调用 `dex-forge` 的导出器，然后做那些「落到本运行时」所特有的事：
   剥掉运行时会拒绝的字段，把标定兼容性从训练用手重新绑定到部署用手，
   校验动作边界落在安全包络之内，并重算内容寻址的 id 与摘要。

3. **校验。** `dex-runtime verify-package PACKAGE --allow-unsigned-local`。
   它拒绝什么就修什么；它不会接受一个部分有效的 manifest。

4. **注册。** 把该目录放进部署配置 `policies.stores` 列出的某个仓库，然后确认
   运行时能看到它：`dex-runtime list-policies CONFIG`。

5. **Preflight。** `dex-runtime preflight CONFIG` 在不打开硬件的前提下证明兼容性。

6. **先跑 shadow。** 让策略停在 `RL_SHADOW` 并检查 trace。此时策略在真实运行、
   在被安全校验，但不下发任何命令。这一步值得多花时间。

7. **然后，且只在授权流程下，才切换。** 见[操作手册](../../operator-runbook.md)（英文）。

新策略不需要改任何代码。如果你发现自己在为了加载一个策略而修改 `dex_runtime`，
那说明 manifest 写错了。

## 兼容性门禁

`PolicyCompatibilityProvider`（[`readiness.py`](../../../src/dex_runtime/readiness.py)）
在以下情况阻止激活：

- 运行时 API 版本落在 `supported_runtime_api` 之外
- `hand.model` / `hand.side` 与部署配置不符
- 语义 schema 摘要与当前运行的 schema 不匹配
- 当前标定不在 `calibration_compatibility` 中
- `control_period_ns` 与配置的控制周期不符
- 硬件无法提供所需的确认强度

这些比较的是摘要，不是版本字符串：版本字符串只能说明两个产物**声称**相同，
摘要才能**证明**。
