# dex-manipulation

*[English](README.md) | [中文](README.zh.md)*

灵巧手的中性硬件运行时，支持遥操作与强化学习策略的混合控制。

本仓库负责内部契约、遥操适配、策略执行、独占硬件网关、控制权交接监管与可观测性。
它不做训练，不依赖 Isaac Lab，也不导入 `dex-forge` 的训练代码。

```
遥操设备 ────▶ 重定向 ────▶ ┌──────────────┐ ──▶ 安全校验 ──▶ 网关 ──▶ 灵巧手
                            │  交接监管器   │
训练好的策略 ─▶ 推理  ────▶ │  (handoff)   │ ◀── readiness 证据
                            └──────────────┘ ◀──▶ 机械臂 hold 租约
```

监管器决定谁有权移动手。每一步向策略控制推进都有门禁：readiness 证据、策略观测历史
攒满、机械臂已验证保持静止；任何一步失败都退回到保持不动。

## 快速开始

```bash
./bootstrap.sh               # `all` 档；需要 Python 3.10-3.12
source .venv/bin/activate
# 完全无硬件跑通整套系统
python -m tools.control_console.soak_verify --duration-s 30 --viewer-count 1

# 或者打开界面看：http://127.0.0.1:8765/
python tools/run_console.py --transport fake --policy synthetic \
    --vr fake --vr-python .venv/bin/python --arm-telemetry fake --camera fake
```

`--transport` 只选择**手**。策略、遥操输入、相机、机械臂遥测各自都默认为 real，
必须像上面那样分别指定为 fake。

完整步骤见 [docs/zh/onboarding.md](docs/zh/onboarding.md)。

## 文档

| 文档 | 用途 |
|---|---|
| [上手指南](docs/zh/onboarding.md) · [EN](docs/onboarding.md) | 跑起来、仓库地图、排障 |
| [架构总览](docs/zh/architecture.md) · [EN](docs/architecture.md) | 控制链路、状态机，以及为什么这样设计 |
| [遥操接口](docs/zh/interfaces/teleop.md) · [EN](docs/interfaces/teleop.md) | 接入新的遥操设备 |
| [策略接口](docs/zh/interfaces/policy.md) · [EN](docs/interfaces/policy.md) | 打包并部署训练好的策略 |
| [硬件接口](docs/interfaces/hardware.md) | 接入新的手或机械臂（英文） |
| [工具说明](docs/tools.md) | `tools/` 下每个脚本的用途（英文） |
| [操作手册](docs/operator-runbook.md) | **一切涉及真实硬件的操作以此为准**（英文） |
| [冻结决策](docs/frozen-decisions.md) | 冻结的硬件与格式决策，及产物溯源（英文） |

## 命令行

```bash
dex-runtime preflight CONFIG                             # 校验部署配置，不驱动任何硬件
dex-runtime run CONFIG                                   # 启动运行时
dex-runtime list-policies CONFIG                         # 查看已配置的策略仓库
dex-runtime verify-package PACKAGE [--allow-unsigned-local]
```

## 安全须知

本系统会驱动真实硬件。[`docs/operator-runbook.md`](docs/operator-runbook.md)
是权威，以下是要点：

- 先过 E-stop 和文档列出的前置条件检查。
- fake 模式是真正隔离的：`--transport fake` 和 soak 验证器无法打开 CAN、OpenXR、
  相机或机械臂硬件，可以放心使用。
- 真实机械臂切换默认关闭。`--enable-rl-switch` 仅保留给明确授权的 HIL 流程。
- 每种资源只能有一个占有者。绝不要在运行时旁边再跑 LinkerHand ROS SDK，也绝不要启动
  第二个 Hitbot 占有者。独占的 `LinkerGateway` 是唯一的 CAN/手部命令通路；
  `dex_teleop/main_new.py` 只被导入其读取器与变换代码，绝不作为第二个手部占有者运行。
- 在启动器终端按 Ctrl-C 是受支持的停止方式，它会按顺序关闭各组件。

## 交付边界

已实现的范围是架构文档中的最小关键路径：

- **M0** — 冻结的 Linker 映射、标定、schema 与规范模型
- **M1** — 内部契约、OpenXR/DexPilot 语义重定向、独占 Linker 网关
- **M2** — 精确的策略编解码、持续 shadow、假臂交接、混合过渡与交还
- **M3** — JSONL 事件与轨迹、终端状态显示、F12 PCsensor 切换

感知只供操作员界面、绝不喂给策略；多进程、回放，以及真实策略的机械臂控制仍处于
推迟或由 `--enable-rl-switch` 门禁的状态。可放行范围以
[docs/operator-runbook.md](docs/operator-runbook.md) 为准（英文），不可变更的部分
记录在 [docs/frozen-decisions.md](docs/frozen-decisions.md)（英文）。

## 开发

```bash
lint-imports           # 分层契约（CI 强制）
ruff check . && ruff format --check .
mypy
```

[`.importlinter`](.importlinter) 中的分层是强制的，不是建议：`dex_contracts`
不依赖任何其他包，遥操适配层不得触碰硬件层与运行时，硬件适配层不得触碰监管器。
