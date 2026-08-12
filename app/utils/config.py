from typing import Dict

from app.utils.constants import GOODS_PATH
from app.utils.file import read_json

city_sell_data: Dict[str, Dict[str, int]] = read_json(
    GOODS_PATH / "CityGoodsSellData.json"
)
CITYS = list(city_sell_data.keys())
CITY_GOODS = city_sell_data
CITY_POSITIONS = read_json(GOODS_PATH / "CityPosData.json")
