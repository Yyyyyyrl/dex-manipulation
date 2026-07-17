"""人手关键点 → MANO 规范坐标系（无 mediapipe 依赖）。

这两段代码原样取自 dex-retargeting 例子里的
`example/vector_retargeting/single_hand_detector.py`，只是把它们从依赖
mediapipe 的 SingleHandDetector 中抽离出来，便于 VR 流程复用。

为什么需要它：
  dex_retargeting 期望输入的是「以手腕为原点、对齐到 MANO 约定」的手部关键点，
  而不是原始世界坐标。例子里 SingleHandDetector.detect() 做的就是：
      keypoint -= keypoint[0]                          # 平移到手腕原点
      frame = estimate_frame_from_hand_points(keypoint) # 估计手腕坐标系
      joint_pos = keypoint @ frame @ operator2mano      # 旋转进规范系
  VR 流程必须复刻这一步，否则重定向得到的是乱的结果。

关键点顺序遵循 MediaPipe 21 点约定（estimate_frame 只用到下标 0/5/9）：
  0=wrist, 5=index_mcp, 9=middle_mcp, 4/8/12/16/20=五指指尖。
"""

import numpy as np

# operator(检测器输出系) → MANO 约定 的固定坐标变换，左右手不同。
OPERATOR2MANO_RIGHT = np.array(
    [
        [0, 0, -1],
        [-1, 0, 0],
        [0, 1, 0],
    ]
)

OPERATOR2MANO_LEFT = np.array(
    [
        [0, 0, -1],
        [1, 0, 0],
        [0, -1, 0],
    ]
)


def estimate_frame_from_hand_points(keypoint_3d_array: np.ndarray) -> np.ndarray:
    """从检测到的 3D 关键点估计手腕坐标系（仅朝向）。

    :param keypoint_3d_array: (21,3) 关键点，顺序为 MediaPipe 约定。
                              仅使用 [0]=wrist, [5]=index_mcp, [9]=middle_mcp。
    :return: (3,3) 手腕在 MANO 约定下的坐标系（列向量为 x/normal/z 轴）。
    """
    assert keypoint_3d_array.shape == (21, 3)
    points = keypoint_3d_array[[0, 5, 9], :]

    # 从掌心指向中指根部的向量
    x_vector = points[0] - points[2]

    # 用 SVD 拟合三点所在平面的法向量
    points = points - np.mean(points, axis=0, keepdims=True)
    u, s, v = np.linalg.svd(points)

    normal = v[2, :]

    # Gram–Schmidt 正交化
    x = x_vector - np.sum(x_vector * normal) * normal
    x = x / np.linalg.norm(x)
    z = np.cross(x, normal)

    # 假设从小指到食指方向与 MANO 约定的 z 轴一致，用于消歧法向量正负
    if np.sum(z * (points[1] - points[2])) < 0:
        normal *= -1
        z *= -1
    frame = np.stack([x, normal, z], axis=1)
    return frame
