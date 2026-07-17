"""Manus 手套关键点 -> dex_retargeting 输入。

负责两件事：
  1) 把 ManusGlove 消息里的 raw_nodes（25 节点，腕局部系）转成
     dex_retargeting 期望的「MANO 规范系」joint_pos（21 点布局）。
  2) 按重定向器类型，从 joint_pos 算出 ref_value（喂给 retargeting.retarget）。

Manus raw skeleton 布局（node_id 顺序，同一 Core 版本内固定）：
  node 0 = Hand 根节点（手腕）。因发布端 HandMotion_None，它的位置恒为
  (0,0,0)、姿态恒为单位四元数，即所有节点已经在腕局部坐标系里。
  拇指 4 节：1..4；其余四指各 5 节：Index 5..9, Middle 10..14,
  Ring 15..19, Pinky 20..24。

joint_type 命名陷阱：
  发布端按「骨头起点」命名（见 ManusDataPublisher::JointTypeToString），
  比解剖学关节整体靠手腕偏移一级：
    "MCP" = 掌骨根(CMC)，"PIP" = 解剖学 MCP 指根，
    "IP"  = 解剖学 PIP， "DIP" = 解剖学 DIP。
  拇指链只有 4 节："MCP"=CMC, "PIP"=拇指MCP, "DIP"=拇指IP, "TIP"。
  已用骨段长度实测验证（如 Index "MCP"->"PIP" ≈ 7.3cm，正是掌骨长度）。

坐标系手性陷阱：
  发布端 ManusDataPublisher.cpp 在 InitializeSDK 里调 CoordinateSystemVUH_Init，
  该函数把 hpp 成员初始化配置的 {XFromViewer, PositiveZ, Side_Right} 整个清成
  Invalid（反汇编 libManusSDK 验证过），Manus Core 收到 Invalid 配置后回退到
  原生默认：**左手系**（Y-up, Z-forward，Unity 风格）。左手系坐标直接进
  estimate_frame_from_hand_points（内部叉乘/法向消歧假设右手系）会得到镜像的
  规范系——屈曲方向映射反，重定向后表现为"屈曲变侧摆"的乱动。
  已用真实摊平手数据帧数值验证：取反任一轴后规范系输出与合成标准左手完全一致。
  因此 manus_to_joint_pos 先取反 x 轴把数据转回右手系解读。
  注意：若日后把发布端坐标系修成右手系，这里的取反必须同步删除，否则双重镜像。

"""

import numpy as np

from .hand_frame import (
    OPERATOR2MANO_LEFT,
    OPERATOR2MANO_RIGHT,
    estimate_frame_from_hand_points,
)

N_MANUS_NODES = 25

# Manus node_id → MediaPipe 21 点下标（全点映射）。
# 注意右侧注释里写的是【解剖学】关节名；引号里是 Manus 消息的 joint_type 字符串。
MANUS_TO_MP = {
    0: 0,    # Hand 根节点         → MP 0  wrist
    # 拇指（4 节全用）
    1: 1,    # Thumb "MCP"(=CMC)   → MP 1  thumb_cmc
    2: 2,    # Thumb "PIP"(=MCP)   → MP 2  thumb_mcp
    3: 3,    # Thumb "DIP"(=IP)    → MP 3  thumb_ip
    4: 4,    # Thumb "TIP"         → MP 4  thumb_tip
    # 食指（node 5 = 掌骨根，MP 无对应，弃用）
    6: 5,    # Index "PIP"(=MCP)   → MP 5  index_mcp
    7: 6,    # Index "IP"(=PIP)    → MP 6  index_pip
    8: 7,    # Index "DIP"         → MP 7  index_dip
    9: 8,    # Index "TIP"         → MP 8  index_tip
    # 中指（node 10 弃用）
    11: 9,   # Middle "PIP"(=MCP)  → MP 9  middle_mcp
    12: 10,  # Middle "IP"(=PIP)   → MP 10 middle_pip
    13: 11,  # Middle "DIP"        → MP 11 middle_dip
    14: 12,  # Middle "TIP"        → MP 12 middle_tip
    # 无名指（node 15 弃用）
    16: 13,  # Ring "PIP"(=MCP)    → MP 13 ring_mcp
    17: 14,  # Ring "IP"(=PIP)     → MP 14 ring_pip
    18: 15,  # Ring "DIP"          → MP 15 ring_dip
    19: 16,  # Ring "TIP"          → MP 16 ring_tip
    # 小指（node 20 弃用）
    21: 17,  # Pinky "PIP"(=MCP)   → MP 17 pinky_mcp
    22: 18,  # Pinky "IP"(=PIP)    → MP 18 pinky_pip
    23: 19,  # Pinky "DIP"         → MP 19 pinky_dip
    24: 20,  # Pinky "TIP"         → MP 20 pinky_tip
}

# 期望的 (chain_type, joint_type) 布局，按 node_id 顺序。
# 用于首帧校验：万一换了 Core 版本导致节点顺序变化，能立刻报错而不是悄悄错映射。
_FINGER_CHAINS = ("Thumb", "Index", "Middle", "Ring", "Pinky")
EXPECTED_LAYOUT = [("Hand", "Invalid")]
EXPECTED_LAYOUT += [("Thumb", j) for j in ("MCP", "PIP", "DIP", "TIP")]
for _chain in _FINGER_CHAINS[1:]:
    EXPECTED_LAYOUT += [(_chain, j) for j in ("MCP", "PIP", "IP", "DIP", "TIP")]


def validate_layout(msg) -> None:
    """校验 ManusGlove 消息的节点布局与 MANUS_TO_MP 的假设一致。

    生产者进程收到第一帧时调用一次即可；不一致时抛 ValueError。

    :param msg: manus_ros2_msgs/msg/ManusGlove 消息。
    """
    if msg.raw_node_count != N_MANUS_NODES:
        raise ValueError(
            f"Manus raw_node_count={msg.raw_node_count}，期望 {N_MANUS_NODES}；"
            "Core 版本可能改变了骨架布局，请重新核对 MANUS_TO_MP。")
    for node in msg.raw_nodes:
        expected = EXPECTED_LAYOUT[node.node_id]
        actual = (node.chain_type, node.joint_type)
        if actual != expected:
            raise ValueError(
                f"node_id={node.node_id} 布局不符：期望 {expected}，实际 {actual}；"
                "请重新核对 MANUS_TO_MP。")


def msg_to_keypoints(msg) -> np.ndarray:
    """ManusGlove 消息 -> (25,3) 关节位置数组（米，腕局部系，按 node_id 下标）。

    生产者进程用：ROS 消息对象不要直接进 multiprocessing.Queue（pickle 又大又慢），
    先转成纯 numpy 再入队。

    :param msg: manus_ros2_msgs/msg/ManusGlove 消息。
    :return: (25,3) float64，缺失节点为 nan（正常情况不会缺）。
    """
    kp25 = np.full((N_MANUS_NODES, 3), np.nan, dtype=np.float64)
    for node in msg.raw_nodes:
        p = node.pose.position
        kp25[node.node_id] = (p.x, p.y, p.z)
    return kp25


def keypoints_valid(kp25: np.ndarray) -> bool:
    """检查 (25,3) 关键点是否可用于重定向（所有映射节点均为有限值）。

    Manus 是模型估计输出，节点总是存在，这里主要防御空帧/缺节点。
    """
    kp25 = np.asarray(kp25)
    idx = list(MANUS_TO_MP.keys())
    return bool(np.isfinite(kp25[idx]).all())


def manus_to_joint_pos(kp25: np.ndarray, hand_type: str = "left") -> np.ndarray:
    """Manus 25 节点坐标 → MANO 规范系 joint_pos (21,3)。

    与 vr_adapter.openxr_to_joint_pos 同构：
      左手系转右手系 → 下标重映射 → 平移到手腕原点 → 估计手腕坐标系
      → 旋转进 MANO 规范系。
    数据本身已是腕局部系（node 0 恒为原点），平移一步实为冗余保护，
    留着以防发布端 HandMotion 设置改变。

    :param kp25: (25,3) Manus 节点位置（米），下标为 node_id。
    :param hand_type: "left" / "right"，决定 operator2mano 矩阵。
    :return: (21,3) 规范化关键点，可直接用于 compute_ref_value。
    """
    kp25 = np.asarray(kp25, dtype=np.float64)

    # 0) 左手系 → 右手系：Manus Core 默认输出左手系坐标
    kp25 = kp25 * np.array([-1.0, 1.0, 1.0])

    # 1) 全点重映射到 MediaPipe 21 点布局（21 槽位全满，无零点）
    keypoint = np.zeros((21, 3), dtype=np.float64)
    for manus_idx, mp_idx in MANUS_TO_MP.items():
        keypoint[mp_idx] = kp25[manus_idx]

    # 2) 平移到手腕原点
    keypoint = keypoint - keypoint[0:1, :]

    # 3) 估计手腕坐标系并旋转进 MANO 规范系
    # operator2mano = OPERATOR2MANO_LEFT if hand_type == "left" else OPERATOR2MANO_RIGHT
    operator2mano = OPERATOR2MANO_LEFT if hand_type == "left" else OPERATOR2MANO_RIGHT
    wrist_rot = estimate_frame_from_hand_points(keypoint)
    joint_pos = keypoint @ wrist_rot @ operator2mano
    return joint_pos


def compute_ref_value(retargeting, joint_pos: np.ndarray) -> np.ndarray:
    """从 joint_pos 算出 ref_value（喂给 retargeting.retarget）。

    支持 POSITION / VECTOR / DexPilot 三种重定向器。

    :param retargeting: SeqRetargeting（RetargetingConfig.build() 的返回）。
    :param joint_pos: (21,3) MANO 规范系关键点。
    :return: ref_value，形状随重定向器类型而定。
    """
    indices = retargeting.optimizer.target_link_human_indices
    retargeting_type = retargeting.optimizer.retargeting_type
    if retargeting_type == "POSITION":
        return joint_pos[indices, :]
    origin_indices = indices[0, :]
    task_indices = indices[1, :]
    return joint_pos[task_indices, :] - joint_pos[origin_indices, :]
