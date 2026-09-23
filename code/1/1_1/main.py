from horizontal_dist import calc_dist
from dem_max_elevation import load_dem_mat, max_elevation_between_points
from energy_max_available import max_storage, param_data, uav_data, battery_data

import numpy as np
import pandas as pd

dem_mat, latitude, longitude = load_dem_mat(
    r"D:\SF_Dir\第二十三届中国研究生数学建模竞赛 - 中文题目\中文题目\D题\HUAWEI-CUP\数据\镇龙乡地理空间数据\镇龙乡及周边地理数据\数字高程模型数据（DEM）\镇龙乡及周边30米DEM.mat",
    dem_key="dem",
    latitude_key="latitude",
    longitude_key="longitude"
)

locale_path = r"D:\SF_Dir\第二十三届中国研究生数学建模竞赛 - 中文题目\中文题目\D题\HUAWEI-CUP\数据\无人机应急物资运输基础数据\调度中心与服务区.xlsx"
center_data = pd.read_excel(locale_path, sheet_name="调度中心")
district_data = pd.read_excel(locale_path, sheet_name="服务区")

dems = []
for _, row in district_data.iterrows():
    lat1, lon1 = center_data.loc[0, ["纬度（°）", "经度（°）"]]
    lat2, lon2 = row["纬度（°）"], row["经度（°）"]
    dist = calc_dist(lat1, lon1, lat2, lon2)
    max_height, info = max_elevation_between_points(
        dem_mat,
        latitude,
        longitude,
        (lat1, lon1),
        (lat2, lon2),
        return_details=True
    )
    dems.append(max_height)
print(dems)

storage_mat = []
for j, row in district_data.iterrows():
    print(row["海拔（m）"])
    storage_mat.append(max_storage(
        param_data,
        uav_data,
        battery_data,
        dems[j],
        center_data.loc[0, "海拔（m）"],
        row["海拔（m）"],
        calc_dist(
            center_data.loc[0, "纬度（°）"],
            center_data.loc[0, "经度（°）"],
            row["纬度（°）"],
            row["经度（°）"]
        )
    ))
print(storage_mat)