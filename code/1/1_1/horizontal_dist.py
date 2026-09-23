import numpy as np

def load_dem_mat(
    mat_path,
    dem_key="dem",
    latitude_key="latitude",
    longitude_key="longitude"
):
    """
    从 MATLAB .mat 文件读取 DEM、latitude、longitude。

    注意：
    变量名必须与 mat 文件中的实际变量名一致。
    """
    from scipy.io import loadmat

    data = loadmat(mat_path)

    if dem_key not in data:
        raise KeyError(
            f"找不到变量 '{dem_key}'。mat 文件中的变量有："
            f"{[k for k in data.keys() if not k.startswith('__')]}"
        )

    if latitude_key not in data:
        raise KeyError(
            f"找不到变量 '{latitude_key}'。mat 文件中的变量有："
            f"{[k for k in data.keys() if not k.startswith('__')]}"
        )

    if longitude_key not in data:
        raise KeyError(
            f"找不到变量 '{longitude_key}'。mat 文件中的变量有："
            f"{[k for k in data.keys() if not k.startswith('__')]}"
        )

    dem = np.asarray(data[dem_key], dtype=float)
    latitude = np.asarray(data[latitude_key]).squeeze()
    longitude = np.asarray(data[longitude_key]).squeeze()

    return dem, latitude, longitude


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