`AttachedToCityData.json`: 记录各站点的附属城市  
`CityData.json`: 记录城市声望加成  
`CityGoodsData.json`: 记录站点商品基础数量，用于在线商品价值计算  
`CityGoodsSellData.json`: 记录站点商品基础价格，用于在线商品价值计算和端点跑商商品列表  
 以及端点跑商的站点显示  
`CityTiredData.json`: 记录站点间疲劳，用于在线商品价值计算  
`SkillData.json`: 记录角色生活技能的商品加成，用于在线商品价值计算

## 更新数据

商品、基础买价、基础数量、站点疲劳和城市归属可通过科伦巴商会维护的
[`resonance-data-columba`](https://www.npmjs.com/package/resonance-data-columba)
数据包更新：

```powershell
node tools/update_columba_trade_data.js path\to\resonance-data-columba\dist\columbabuild.js
```

脚本会统一将上游的“七号自由港”转换为本项目使用的“7号自由港”。
站点地图坐标和 `resources/stations` 下的识别模板属于游戏画面资源，不由
该数据包提供，需要在对应游戏版本中单独采集和验证。
