import pandas as pd

file_path = r"D:\SF_Dir\第二十三届中国研究生数学建模竞赛 - 中文题目\中文题目\D题\HUAWEI-CUP\数据\无人机应急物资运输基础数据\运输无人机数据.xlsx"

param_data = pd.read_excel(file_path, sheet_name="三类机型参数 ")
uav_data = pd.read_excel(file_path, sheet_name="逐架无人机清单 ")
battery_data = pd.read_excel(file_path, sheet_name="共享电池库存 ")

def max_storage(
    param_data,
    uav_data,
    battery_data,
    dem,
    hi,
    hj,
    dist
):
    """
    二分法计算中转站与各服务区间各机型在安全能量消耗内的最大可能载荷

    参数：
        param_data: 三类机型参数数据。
        uav_data: 逐架无人机清单数据。
        battery_data: 共享电池库存数据。
        dem: 最大高程
        hi: 起点海拔
        hj: 终点海拔
        dist: 起点和终点之间的水平距离（单位：米）。
    """
    # max_height = max(hi, hj, dem)
    # details = []

    # for index, row in param_data.iterrows():
    #     uav_type = row["机型编号"]
    #     empty_weight = row["空机重量（kg）"]
    #     max_payload = row["最大载荷（kg）"]
    #     battery_capacity = row["电池容量（Wh）"]
    #     energy_consumption_rate = row["能耗率（Wh/km）"]

    #     # 计算总重量
    #     total_weight = empty_weight + max_payload

    #     # 计算能量消耗
    #     energy_consumed = (dist / 1000) * energy_consumption_rate * (total_weight / empty_weight)

    #     # 检查是否在安全能量消耗范围内
    #     if energy_consumed <= battery_capacity:
    #         details.append({
    #             "机型编号": uav_type,
    #             "最大可能载荷": max_payload,
    #             "能量消耗": energy_consumed,
    #             "电池容量": battery_capacity
    #         })
    # print(details)

    # return max_height, details
    details = []
    for index, kind in param_data.iterrows():
        empty_dist = kind["空载标准航程（m）"]
        # ((dem - hi) * kind["爬升能耗效率"] + dist) / stored_dist(mid) + ((dem - hj) * kind["爬升能耗效率"] + dist) / empty_dist
        while 
    return details


details = max_storage(
    param_data,
    uav_data,
    battery_data,
    223.057,
    127.7,
    154,
    3063.407094930625
)

print(details)