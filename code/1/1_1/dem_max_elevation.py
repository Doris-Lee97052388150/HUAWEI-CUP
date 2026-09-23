# -*- coding: utf-8 -*-
"""
快速计算 DEM 中两已知经纬度点之间线段经过区域的最大高程。

适用数据结构：
    DEM.shape = (m, n)
    latitude.shape = (m,)      # 每一行对应一个纬度
    longitude.shape = (n,)     # 每一列对应一个经度

核心思路：
1. 将起点、终点经纬度分别转换为 DEM 中最近的行列号；
2. 使用 Bresenham 栅格直线算法找出线段穿过的像元；
3. 读取这些像元的高程并求最大值。

仅依赖：
    numpy
    scipy（如果需要直接读取 .mat 文件）
"""

import numpy as np


def nearest_index(arr, value):
    """
    返回一维数组 arr 中最接近 value 的下标。

    适用于 latitude / longitude 无论升序还是降序的情况。
    """
    arr = np.asarray(arr).squeeze()
    return int(np.abs(arr - value).argmin())


def bresenham_line(row0, col0, row1, col1):
    """
    Bresenham 栅格直线算法。

    输入：
        (row0, col0) 起点像元
        (row1, col1) 终点像元

    返回：
        rows, cols
        即线段经过的 DEM 行列号数组。
    """
    rows = []
    cols = []

    dr = abs(row1 - row0)
    dc = abs(col1 - col0)

    sr = 1 if row0 < row1 else -1
    sc = 1 if col0 < col1 else -1

    r, c = row0, col0

    if dc > dr:
        err = dc / 2.0

        while c != col1:
            rows.append(r)
            cols.append(c)

            err -= dr
            if err < 0:
                r += sr
                err += dc

            c += sc

    else:
        err = dr / 2.0

        while r != row1:
            rows.append(r)
            cols.append(c)

            err -= dc
            if err < 0:
                c += sc
                err += dr

            r += sr

    rows.append(row1)
    cols.append(col1)

    return np.asarray(rows, dtype=int), np.asarray(cols, dtype=int)


def max_elevation_between_points(
    dem,
    latitude,
    longitude,
    point_i,
    point_j,
    return_details=False
):
    """
    计算两点之间 DEM 栅格线段上的最大高程。

    参数
    ----------
    dem : ndarray, shape (m, n)
        DEM 高程矩阵。

    latitude : ndarray, shape (m,)
        DEM 每一行对应的纬度。

    longitude : ndarray, shape (n,)
        DEM 每一列对应的经度。

    point_i : tuple
        起点 (lat_i, lon_i)。

    point_j : tuple
        终点 (lat_j, lon_j)。

    return_details : bool
        True 时额外返回最大高程所在经纬度、行列号等信息。

    返回
    ----
    max_height : float
        两点连线经过栅格的最大高程。

    details : dict, 可选
        return_details=True 时返回。
    """

    dem = np.asarray(dem, dtype=float)
    latitude = np.asarray(latitude).squeeze()
    longitude = np.asarray(longitude).squeeze()

    if dem.ndim != 2:
        raise ValueError("DEM 必须是二维矩阵。")

    m, n = dem.shape

    if latitude.size != m:
        raise ValueError(
            f"latitude 长度为 {latitude.size}，但 DEM 行数为 {m}，二者应一致。"
        )

    if longitude.size != n:
        raise ValueError(
            f"longitude 长度为 {longitude.size}，但 DEM 列数为 {n}，二者应一致。"
        )

    lat_i, lon_i = point_i
    lat_j, lon_j = point_j

    # 经纬度 -> 最近的 DEM 行列号
    row_i = nearest_index(latitude, lat_i)
    col_i = nearest_index(longitude, lon_i)

    row_j = nearest_index(latitude, lat_j)
    col_j = nearest_index(longitude, lon_j)

    # 获取直线经过的所有 DEM 像元
    rows, cols = bresenham_line(row_i, col_i, row_j, col_j)

    heights = dem[rows, cols]

    # 忽略 NaN
    valid = np.isfinite(heights)

    if not np.any(valid):
        raise ValueError("两点之间经过的 DEM 栅格高程全部为 NaN 或无效值。")

    valid_heights = heights[valid]
    valid_rows = rows[valid]
    valid_cols = cols[valid]

    k = int(np.argmax(valid_heights))

    max_height = float(valid_heights[k])
    max_row = int(valid_rows[k])
    max_col = int(valid_cols[k])

    if not return_details:
        return max_height

    details = {
        "max_height": max_height,
        "max_latitude": float(latitude[max_row]),
        "max_longitude": float(longitude[max_col]),
        "max_row": max_row,
        "max_col": max_col,
        "start_row_col": (row_i, col_i),
        "end_row_col": (row_j, col_j),
        "num_cells": int(len(rows)),
    }

    return max_height, details


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


if __name__ == "__main__":

    # ============================================================
    # 示例 1：直接从 .mat 文件读取
    # ============================================================

    mat_path = r"D:\SF_Dir\第二十三届中国研究生数学建模竞赛 - 中文题目\中文题目\D题\数据\镇龙乡地理空间数据\镇龙乡及周边地理数据\数字高程模型数据（DEM）\镇龙乡及周边30米DEM.mat"

    # 如果你的 MATLAB 文件中的变量名不是 dem / latitude / longitude，
    # 请修改下面三个 key。
    dem, latitude, longitude = load_dem_mat(
        mat_path,
        dem_key="dem",
        latitude_key="latitude",
        longitude_key="longitude"
    )

    # ============================================================
    # 输入两个点：
    # 格式为 (纬度, 经度)
    # ============================================================

    point_i = (23.0000, 113.0000)
    point_j = (23.0100, 113.0200)

    max_height, info = max_elevation_between_points(
        dem,
        latitude,
        longitude,
        point_i,
        point_j,
        return_details=True
    )

    print("======================================")
    print("两点连线最大高程计算结果")
    print("======================================")
    print(f"起点：{point_i}")
    print(f"终点：{point_j}")
    print(f"最大高程：{max_height:.3f} m")
    print(
        "最大高程位置："
        f"lat={info['max_latitude']:.8f}, "
        f"lon={info['max_longitude']:.8f}"
    )
    print(f"DEM 行列号：({info['max_row']}, {info['max_col']})")
    print(f"线段经过像元数：{info['num_cells']}")
