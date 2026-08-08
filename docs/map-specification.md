# 地图规范 (map-specification)

版本：1.0.0 ｜ 对应文件：`world_data/maps/tiny_world.tmj`、`tiny_farm.tsj`、`markers.tsj`

## 1. 文件与坐标

| 项       | 值                                                                                                          |
|----------|-------------------------------------------------------------------------------------------------------------|
| 地图尺寸 | 64 × 40 格                                                                                                  |
| 瓦片尺寸 | 16 × 16 px                                                                                                  |
| 朝向     | 正交 (orthogonal)，x 向右、y 向下                                                                           |
| 坐标系   | 程序内一律使用「格坐标 (col, row)」；Tiled 对象层 x/y 为像素，转换：`px = col * 16`                         |
| gid 空间 | `tiny_farm.tsj` firstgid=1（132 个瓦片 → gid 1..132）；`markers.tsj` firstgid=133（1 个标记瓦片 → gid 133） |

生成脚本：`tools/build_map.py`（确定性，固定随机种子 `20260804`，可重复生成）。

## 2. 图层（自底向上）

| 图层              | 类型   | 内容                                            | 渲染         |
|-------------------|--------|-------------------------------------------------|--------------|
| `ground`          | tile   | 草地 / 泥土道路 / 广场地面 / 农田土基           | 始终可见     |
| `ground_detail`   | tile   | 小草、小花点缀                                  | 始终可见     |
| `buildings`       | tile   | 商店、住宅、政务厅、谷仓                        | 始终可见     |
| `decorations_low` | tile   | 树、灌木、栅栏、作物、池塘、井、喷泉            | 始终可见     |
| `foreground`      | tile   | 预留（树冠等遮挡物），当前为空                  | 可选         |
| `collision`       | tile   | 标记瓦片（gid 133）标记**不可通行**格           | 调试模式显示 |
| `navigation`      | tile   | 标记瓦片（gid 133）标记**可行走**格             | 调试模式显示 |
| `locations`       | object | 地点对象（见 §3）                               | 调试模式显示 |
| `interactables`   | object | 可交互对象（见 §4）                             | 调试模式显示 |
| `spawn_points`    | object | 智能体出生点（由角色卡生成，引擎不读取，见 §5） | 调试模式显示 |

### 可行走判定

```
cell_walkable(col, row) =
    navigation 层有标记
    AND collision 层无标记
```

两条不变式（生成脚本自校验）：

- `navigation ∩ collision = ∅`（不重叠）；
- 所有 `locations` / `interactables` 锚点格均为可行走格。

## 3. `locations` 对象层

每个对象 = 一个地点，`type="location"`，对象中心为该地点的锚点格。属性：

| 属性            | 类型   | 说明                                                      |
|-----------------|--------|-----------------------------------------------------------|
| `location_id`   | string | 稳定 ID，全局唯一（如 `village_shop`）                    |
| `name`          | string | 中文显示名（如 `村庄杂货店`）                             |
| `location_type` | string | `plaza` / `store` / `farm` / `office` / `house` / `hotel` / `stall`（M18：个人商店空摊位）/ `field`（M19：野外采集点） |
| `capacity`      | int    | 同时容纳的最大智能体数                                    |
| `open_hour`     | int    | 开门小时（世界时 0..24，`0` 表示 24 小时开放）            |
| `close_hour`    | int    | 关门小时（`24` 表示整日开放）                             |

当前地点清单：

| location_id      | 类型   | 开放 | 容量 |
|------------------|--------|------|------|
| `village_plaza`  | plaza  | 0-24 | 30   |
| `village_shop`   | store  | 8-20 | 8    |
| `village_farm`   | farm   | 6-18 | 12   |
| `town_hall`      | office | 9-17 | 10   |
| `village_hotel`  | hotel  | 0-24 | 10   |
| `village_bakery` | workshop | 6-18 | 6  |
| `carpenter_shop` | workshop | 6-18 | 6  |
| `flower_garden`  | farm   | 6-18 | 8    |
| `stall_plaza_1`  | stall  | 6-22 | 4    |
| `stall_plaza_2`  | stall  | 6-22 | 4    |
| `stall_plaza_3`  | stall  | 6-22 | 4    |
| `forest`         | field  | 6-22 | 8    |
| `river_bank`     | field  | 6-22 | 8    |
| `linxia_home`    | house  | 0-24 | 4    |
| `zhangming_home` | house  | 0-24 | 4    |
| `chenyu_home`    | house  | 0-24 | 4    |
| `wangfang_home`  | house  | 0-24 | 4    |
| `zhoushen_home`  | house  | 0-24 | 4    |
| `limujiang_home` | house  | 0-24 | 4    |
| `sunshen_home`   | house  | 0-24 | 4    |

> `hotel`（小镇旅店）与 `house`/`plaza` 一样全天开放（R8）；无家智能体的
> 睡觉地点（R14），入住收取每晚 85 金币房费（`HOTEL_NIGHTLY_FEE`，见 `backend/app/config/gameplay.py`）。

## 4. `interactables` 对象层

每个对象 = 一个可交互点，`type="interactable"`。属性：`object_id`、`object_type`、`location_id`。

| object_id        | object_type   | 所属地点      |
|------------------|---------------|---------------|
| `shop_counter`   | store_counter | village_shop  |
| `farm_field`     | farm_field    | village_farm  |
| `well`           | well          | village_plaza |
| `fountain`       | fountain      | village_plaza |
| `town_hall_desk` | service_desk  | town_hall     |
| `bakery_oven`    | workshop_station | village_bakery |
| `hotel_counter`  | service_desk  | village_hotel |
| `carpenter_bench`| workshop_station | carpenter_shop |
| `garden_bed`     | farm_field    | flower_garden |

## 5. `spawn_points` 对象层

点对象（`point=true`），`type="spawn_point"`， **仅作可视化参考**：引擎不读取该层。每个智能体的出生点由角色卡（
`world_data/identities/agent_xxx.json` 的 `spawn` 字段）决定，本层由 `tools/build_map.py` 从角色卡自动生成。属性：
`spawn_id`、`agent_id`、`direction`（出生朝向）。

| spawn_id        | agent_id        | 位置     |
|-----------------|-----------------|----------|
| spawn_linxia    | agent_linxia    | (18, 27) |
| spawn_zhangming | agent_zhangming | (40, 11) |
| spawn_chenyu    | agent_chenyu    | (12, 19) |
| spawn_wangfang  | agent_wangfang  | (48, 35) |
| spawn_laozhang  | agent_laozhang  | (30, 9)  |
| spawn_touzi     | agent_touzi     | (33, 20) |
| spawn_zhoushen  | agent_zhoushen  | (35, 6)  |
| spawn_limujiang | agent_limujiang | (12, 6)  |
| spawn_sunshen   | agent_sunshen   | (4, 11)  |

角色卡 `home` 字段（location_id/name/col/row）同样由生成脚本派生为 `buildings` 瓦片与 `locations` 对象——新增智能体时无需手工编辑地图。

## 6. 瓦片语义速查（gid = tile_id + 1）

| 类别             | tile_id                                               |
|------------------|-------------------------------------------------------|
| 草地             | 94, 106, 107, 119                                     |
| 泥土（带草角）   | 0, 12, 24, 36                                         |
| 泥土带红花       | 1, 13, 25, 37                                         |
| 草→土过渡        | 60, 61, 62, 63                                        |
| 树               | 64, 65, 66, 67, 68                                    |
| 灌木             | 3, 15, 27, 39                                         |
| 小草/花          | 26, 38, 52, 53, 54, 55, 56                            |
| 作物（生长阶段） | 4, 5, 6, 7, 8, 16, 17, 18, 19, 20, 28, 29, 30, 31, 32 |
| 栅栏（横）       | 69, 70, 71                                            |
| 池塘             | 100, 101                                              |
| 井               | 112, 113                                              |
| 喷泉             | 120, 121, 122, 123, 124                               |
| 谷仓             | 48, 49, 50, 51                                        |
| 红顶房顶         | 102, 103, 104, 114, 115, 116                          |
| 带门房屋（正面） | 108, 109, 110, 111                                    |
| 大建筑           | 125, 126                                              |
| 俯视房屋         | 96, 97, 98, 99, 72, 73, 74, 75, 76                    |
| 标记瓦片         | 133（collision / navigation 专用）                    |

## 7. 命名规范

- ID 一律小写蛇形：`village_shop`、`agent_linxia`、`spawn_linxia`、`shop_counter`。
- 地点/对象 ID 一经发布不可变更（存档与事件依赖 ID 稳定性）。
- 新增地点必须在 `locations` 层添加对象，并在 `navigation` 层标出门口格。
