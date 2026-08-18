# 上手指南

*[English](../onboarding.md) | [中文](onboarding.md)*

从 `git clone` 到系统跑起来，全程不需要硬件。预留 30 分钟，其中大部分时间在下载 PyTorch。

## 1. 环境准备

```bash
./bootstrap.sh core     # 运行时 + 测试，不含硬件扩展
./bootstrap.sh all      # 全量，含 CAN、evdev、RealSense、Manus
source .venv/bin/activate
```

**请使用 Python 3.10–3.12。** `requires-python` 写的是 `>=3.10`，但锁定的
`numpy==1.26.0` 没有 3.13 的 wheel，`bootstrap.sh` 会报一个很难懂的
`No matching distribution found for numpy==1.26.0`。如果默认解释器版本更高，显式指定：

```bash
PYTHON=python3.12 ./bootstrap.sh core
```

两种 profile 的区别：

| | `core` | `all` |
|---|---|---|
| numpy、PyYAML、safetensors、torch（CPU） | 有 | 有 |
| import-linter、ruff、mypy | 无¹ | 有 |
| `python-can`（LinkerHand CAN） | 无 | 有 |
| `evdev`（F12 脚踏开关） | 无 | 有 |
| OpenCV + `pyrealsense2`（D435） | 无 | 有 |
| `dex-retargeting`（求解器） | 无 | 有 |
| LinkerHand ROS SDK，按 commit 锁定并打补丁到 `.vendor/` | 无 | 有 |

¹ CI 在 `core` 基础上额外安装这些，见 [`ci.yml`](../../.github/workflows/ci.yml)。

`all` 还会克隆锁定 commit 的 LinkerHand ROS SDK 并应用
`vendor/patches/linkerhand-g20-required.patch`，打补丁前后都会校验驱动文件的
SHA-256。如果驱动内容无法识别，它会拒绝继续，而不是往未知内容上打补丁。

实时 Manus 输入还需要主机上装好 ROS 2 和 `manus_ros2_msgs` 工作区。它们是运行时能力，
在建立订阅前会先校验，不属于 Python 依赖。

## 2. 确认代码树是健康的

```bash
lint-imports              # 三条分层契约
ruff check . && ruff format --check .
mypy
```

## 3. 完全无硬件地跑起来

最快且诚实的端到端验证是 soak 验证器。它用假 transport、合成策略、假 OpenXR 输入、
假 D435 和假机械臂遥测启动**真实的**运行时，驱动它，并报告时延和内存增长。
它被硬编码为全假，**无法**被配置成打开真实硬件。

```bash
python -m tools.control_console.soak_verify --duration-s 30 --viewer-count 1
```

最短时长为 30 秒。stdout 会输出 JSON，末尾是各分位时延，`unhealthy_samples` 应为空。

如果想直接看界面而不是看数据，直接启动控制台并打开 <http://127.0.0.1:8765/>：

```bash
python tools/run_console.py --transport fake --policy synthetic \
    --vr fake --vr-python .venv/bin/python --arm-telemetry fake --camera fake
```

**每一路数据源都要单独指定为 fake。** `--transport` 只选择手；
`--policy`、`--vr`、`--camera`、`--arm-telemetry` 各自都默认为 `real`。
只传 `--transport fake` 会在尝试从一个并不存在的 `dex-forge` 检出里构建真实策略时失败：

```
FileNotFoundError: deploy bundle not found: /home/user/dex-forge/runs/.../deploy.pth
```

这是宿主机上没有 `dex-forge` 检出时的报错。如果有检出，它会走得更远，改为报
`PolicyPackageExportError`。两种情况都说明少传了 `--policy synthetic`，
而不是配置有问题；其余三个参数同理。

本节需要 `all` 档。在 `core` 档下控制台会更早停在
`ModuleNotFoundError: No module named 'dex_retargeting'`，因为重定向求解器与
OpenCV 只有 `all` 会装。

## 4. 命令行

四个子命令，都需要一个**策略包**——一个自描述的目录，而不是裸的 checkpoint。
格式见 [策略接口](interfaces/policy.md)。

```bash
dex-runtime verify-package PACKAGE [--allow-unsigned-local]  # 校验策略包
dex-runtime preflight CONFIG      # 校验部署配置的自洽性，不驱动硬件
dex-runtime list-policies CONFIG  # 查看已配置仓库里有什么
dex-runtime run CONFIG            # 启动运行时
```

仓库没有附带示例部署配置，因为一个有效配置必须引用真实的策略包和真实的手部序列号。
想要一个可以试验的策略包，可以自己合成一个：

```bash
python -c "
from pathlib import Path
from tools.demo_policy_factory import write_demo_package
print(write_demo_package(Path('/tmp/dex-demo')))
"
dex-runtime verify-package /tmp/dex-demo --allow-unsigned-local
```

它会打印校验通过的 `PolicyDescriptor`，包含内容寻址的 `package_id`。
部署配置的结构，读 [`tools/switch_demo_backend.py`](../../tools/switch_demo_backend.py)
里的 `_base_config()`——它是权威示例，而且因为控制台依赖它，所以一直是可用的。

`preflight` 任何时候都可以安全执行：它加载并交叉校验部署配置、标定、遥操 profile
和策略包，不打开任何硬件。

## 5. 仓库地图

「我想改 X，该看哪里？」

| 我想…… | 从这里开始 |
|---|---|
| 接入手套 / VR 设备 | [遥操接口](interfaces/teleop.md)，然后看 `src/dex_teleop_adapters/protocols.py` |
| 部署我训练的策略 | [策略接口](interfaces/policy.md)，然后看 `tools/build_demo_policy.py` |
| 改「策略何时可以接管」 | `src/dex_runtime/handoff.py` |
| 改某条安全限位 | 部署配置的 `safety` 段 → `src/dex_runtime/safety.py` |
| 给切换加一个前置条件 | `src/dex_runtime/readiness.py`（新增一个 provider） |
| 支持另一款手 | [硬件接口](../interfaces/hardware.md)，然后看 `src/dex_hardware_linker/` |
| 改控制台显示内容 | `tools/control_console/` |
| 搞清楚某条命令为什么被拒 | `safety.py` 的 reason code，然后查 JSONL 事件日志 |
| 增加一个配置字段 | `src/dex_runtime/deployment.py`（严格模式，未知字段直接报错） |

## 6. 排障

| 现象 | 原因与解决 |
|---|---|
| `No matching distribution found for numpy==1.26.0` | Python 是 3.13。改用 `PYTHON=python3.12 ./bootstrap.sh core`。 |
| CAN 权限被拒 | `can0` 需要处于 up 状态，且当前用户在正确的组里。先用 `ip link show can0` 确认，再怀疑运行时。 |
| 找不到 D435 | 安装 udev 规则：`sudo tools/install_d435_udev.sh`，然后重新插拔。 |
| WiVRn / OpenXR 会话起不来 | 头显会话由本仓库之外管理。`tools/start_live_ui.sh --dry-run` 可以只检查前置条件而不启动硬件。 |
| 手不动，但也没有报错 | 查事件日志里的安全拒绝记录。`sent-to-bus` 级别的确认只表示帧已离开主机，**不代表手动了**。 |
| `refusing unknown G20 driver content` | 锁定的 LinkerHand SDK 驱动与两个预期 SHA-256 都不匹配。不要绕过它，那个补丁包含必需的修复。 |

## 7. 安全

本运行时会驱动真实硬件。在接触真实手或机械臂之前，
[`operator-runbook.md`](../operator-runbook.md)（英文）是权威，而不是本文。

需要提前知道的几点：

- **E-stop 和前置条件检查清单排在最前面**，清单在操作手册里。
- **fake 模式是真正隔离的。** `--transport fake` 和 soak 验证器无法打开 CAN、
  OpenXR、相机或机械臂硬件，可以放心使用。
- **真实机械臂切换默认关闭。** `--enable-rl-switch` 存在，但仅保留给明确授权的
  硬件在环流程。界面上会刻意区分「未授权」和「已授权但仍在等待机械臂验证保持」。
- **每种资源只有一个占有者。** 绝不要在本运行时旁边跑 LinkerHand ROS SDK，也绝不要
  启动第二个 Hitbot 占有者。见[架构总览](architecture.md#线程与独占所有权)。
- **在启动器终端按 Ctrl-C 是受支持的停止方式**，它会按顺序关闭；直接 kill 进程不会。
