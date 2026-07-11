/**
 * Convert the periodically updated resonance-data-columba package into the
 * JSON layout consumed by Auto_Resonance.
 *
 * Usage:
 *   node tools/update_columba_trade_data.js <path-to-columbabuild.js>
 */

const fs = require("fs");
const path = require("path");

const sourcePath = process.argv[2];
if (!sourcePath) {
  throw new Error("missing path to resonance-data-columba/dist/columbabuild.js");
}

const source = require(path.resolve(sourcePath));
const goodsDir = path.resolve(__dirname, "../resources/goods");

const normalizeCity = (name) => (name === "七号自由港" ? "7号自由港" : name);
const normalizeObjectKeys = (input) =>
  Object.fromEntries(Object.entries(input || {}).map(([key, value]) => [normalizeCity(key), value]));
const writeJson = (name, value) =>
  fs.writeFileSync(path.join(goodsDir, name), `${JSON.stringify(value, null, 4)}\n`, "utf8");

const cities = source.CITIES.map(normalizeCity);
const cityGoods = Object.fromEntries(cities.map((city) => [city, {}]));
const cityBuyPrices = Object.fromEntries(cities.map((city) => [city, {}]));

for (const product of Object.values(source.PRODUCTS)) {
  const buyPrices = normalizeObjectKeys(product.buyPrices);
  const buyLots = normalizeObjectKeys(product.buyLot);
  // CityGoodsData is a complete product catalogue repeated per destination.
  // A product that cannot be bought directly keeps the historical fallback 1.
  const baseLot = Object.values(buyLots).find((lot) => Number.isFinite(lot) && lot > 0) || 1;
  for (const city of cities) {
    cityGoods[city][product.name] = {
      isSpeciality: product.type === "Special",
      num: baseLot,
    };
  }
  for (const city of cities) {
    const price = buyPrices[city];
    const lot = buyLots[city];
    // 99999 is the upstream marker for craft-only products, not a shop price.
    if (!Number.isFinite(price) || price === 99999 || !Number.isFinite(lot) || lot <= 0) continue;
    cityBuyPrices[city][product.name] = { price };
  }
}

const fatigue = {};
for (const item of source.CITY_FATIGUES) {
  if (!Number.isFinite(item.fatigue) || item.cities.length !== 2) continue;
  const [left, right] = item.cities.map(normalizeCity);
  fatigue[`${left}-${right}`] = item.fatigue;
  fatigue[`${right}-${left}`] = item.fatigue;
}

const attachedPath = path.join(goodsDir, "AttachedToCityData.json");
const attached = JSON.parse(fs.readFileSync(attachedPath, "utf8"));
const upstreamAttached = normalizeObjectKeys(source.CITY_ATTACH_LIST);
for (const city of cities) {
  attached[city] = normalizeCity(upstreamAttached[city] || city);
}

const cityDataPath = path.join(goodsDir, "CityData.json");
const cityData = JSON.parse(fs.readFileSync(cityDataPath, "utf8"));
const generalPrestigeTemplate = cityData["修格里城"];
for (const city of source.CITY_WITH_PRESTIGE.map(normalizeCity)) {
  if (!cityData[city]) {
    cityData[city] = generalPrestigeTemplate.map((level) => ({ ...level }));
  }
}

writeJson("CityGoodsData.json", cityGoods);
writeJson("CityGoodsSellData.json", cityBuyPrices);
writeJson("CityTiredData.json", fatigue);
writeJson("AttachedToCityData.json", attached);
writeJson("CityData.json", cityData);
writeJson("ColumbaProducts.json", source.PRODUCTS);
writeJson("ColumbaCities.json", source.CITIES);

const totals = {
  cities: cities.length,
  products: Object.values(cityGoods).reduce((sum, goods) => sum + Object.keys(goods).length, 0),
  fatigueRoutes: Object.keys(fatigue).length,
};
console.log(JSON.stringify(totals));
