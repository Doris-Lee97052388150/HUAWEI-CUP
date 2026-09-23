import numpy as np


def calc_dist(
    lat1, lon1, lat2, lon2
):
    """
    计算两点之间的地理距离（单位：米）。

    参数：
        lat1, lon1: 第一个点的纬度和经度（单位：度）。
        lat2, lon2: 第二个点的纬度和经度（单位：度）。
    """
    # 使用 Haversine 公式计算距离
    R = 6371000  # 地球半径（米）
    lat1_rad = np.radians(lat1)
    lon1_rad = np.radians(lon1)
    lat2_rad = np.radians(lat2)
    lon2_rad = np.radians(lon2)

    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = np.sin(dlat / 2)**2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon / 2)**2
    c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))

    return R * c

# print(calc_dist(23.0085095, 109.2308517, 23.0335927, 109.2432319))