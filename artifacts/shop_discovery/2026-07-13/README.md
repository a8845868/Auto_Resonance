# 商店自动购买实机采集记录（2026-07-13）

本目录保存 1280×720 模拟器的 ADB/NEMUIPC 截图、逐帧 OCR JSON、滚动结果和用户提供的说明截图。所有自动化验证均保持 GUI 购买总开关关闭；数量弹窗干跑最终点击的是“取消”，没有点击“确定”。

## 已确认的数据语义

- `履历情报`：紫色档案图标，是抽卡重复角色回收后获得的商店货币；用户截图余额为 227。
- `黑月采购券`：紫色 `NIGHT CHAINS` 票券，是可购买物品，不是履历情报；用户道具详情截图持有 72。
- 黑月采购券当前商店条目：每周限购 5、当前 5/5、单件起价 100 桦石。
- 商店数量弹窗可识别 `最少`、`-1`、`+1`、`最多`、`取消`、`确定`，也可触碰空白区域退出。
- 未开启“批量”时，多件商品进入数量弹窗；开启“批量”会跳过该弹窗。自动化执行前会检查并关闭批量模式。
- 多件总价不是“列表起价 × 数量”：仙人掌能量棒棒糖单件起价 10,000，选择 8/8 后弹窗实时总价为 540,000。因此上限模式必须 OCR 读取弹窗总价，不能线性估算。

## 权威实机结果

- [`runtime-full-probe-result.json`](./runtime-full-probe-result.json)：最终只读探测共 7 页，识别目录 23/23 项，`missing=[]`、`reached_bottom=true`、`page_limit_reached=false`。
- [`runtime-full-probe-verified/`](./runtime-full-probe-verified/)：最终探测的 15 个步骤，每步同时保存 PNG 与 OCR JSON。先从游戏记住的位置反向滑到顶，再向下滑到底；最后两次商品区域差异分别为 `0.325` 和 `0.050`（稳定阈值 `4.0`），证明已连续两次触底稳定。
- 本次最终探测先尝试 TCP ADB，但 MuMu 目标实例当时处于 `offline`，随后按受控回退使用 NEMUIPC；结果 JSON 的 `transport=nemu_ipc` 保留了实际传输证据。此前采集目录仍保留 ADB 实机步骤。
- [`runtime-dialog-probe-result.json`](./runtime-dialog-probe-result.json)：新版六键门禁下的仙人掌能量棒棒糖上限模式干跑结果，`quantity=8`、实时 `cost=540000`、`status=validated`、`final_action=cancel`。
- [`runtime-dialog-probe-verified/`](./runtime-dialog-probe-verified/)：打开单件弹窗、OCR 校验四个文字按钮、视觉校验固定位置的 `-1/+1`、点击“最多”、识别 8/8 与实时总价、点击“取消”后的完整截图/OCR。实机 OCR 模型会省略纯符号按钮，因此 `-1/+1` 采用固定按钮区域的亮色字形检测。

## 其他采集

- `headquarters-full-*`：早期总部商店滚动实验。大步滑动曾跳过中间一行，因此仅作为问题复现证据，不作为完整目录依据。
- `runtime-full-probe/`：早期 23/23 探测，尚未包含连续两次触底稳定帧，仅保留作历史对照；权威步骤为 `runtime-full-probe-verified/`。
- `runtime-dialog-probe/`：早期四文字按钮门禁的弹窗干跑，保留作历史对照；权威步骤为 `runtime-dialog-probe-verified/`。
- `04-headquarters-scroll-1.*`、`05-headquarters-scroll-2.*`、`06-headquarters-scroll-3.*`：人工小步滑动补齐的中部与底部商品。
- `bureau-full-*`：赴命商店 6 个有效滚动位置，以及到底后的连续稳定帧。其多材料兑换结构尚未接入自动购买适配器。
- `reference-01` 至 `reference-09`：用户提供的商店、数量弹窗、履历情报与黑月采购券说明截图。
- `shop-planner-gui.png`：商店规划页的离屏布局验证截图；Windows 离屏插件未加载中文字体，因此方框字仅是离屏渲染限制，商品卡片尺寸与滚动布局已验证。

当前结构已为总部黑月商店、赴命商店、家具商店和水族馆分别保留目录与适配器入口。新增店铺只需补目录、图标、导航/识别适配器，不需要重写 GUI 或任务调度框架。
