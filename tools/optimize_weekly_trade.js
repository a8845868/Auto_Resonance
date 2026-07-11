const data = require("../../columba-data-package/node_modules/resonance-data-columba/dist/columbabuild.js");

const CARGO = Number(process.env.CARGO || 1121);
const BOOKS = Number(process.env.BOOKS || 10);
// The normal automated strategy negotiates to the full modifier on both ends.
const BARGAIN = Number(process.env.BARGAIN || 0.20);
const RAISE = Number(process.env.RAISE || 0.20);
const TAX = Number(process.env.TAX || 0.05);
const NEGOTIATION_FATIGUE_PER_LEG = Number(process.env.NEGOTIATION_FATIGUE || 32);
const WEEKLY_FATIGUE = Number(process.env.WEEKLY_FATIGUE || 5292);

const cities = data.CITIES;
const products = Object.values(data.PRODUCTS);
const fatigue = new Map();
for (const row of data.CITY_FATIGUES) {
  if (row.cities.length !== 2 || !row.fatigue) continue;
  fatigue.set(`${row.cities[0]}|${row.cities[1]}`, row.fatigue);
  fatigue.set(`${row.cities[1]}|${row.cities[0]}`, row.fatigue);
}

function leg(from, to, books) {
  const candidates = [];
  for (const product of products) {
    const buy = product.buyPrices?.[from];
    const sell = product.sellPrices?.[to];
    const lot = product.buyLot?.[from];
    if (!Number.isFinite(buy) || buy === 99999 || !Number.isFinite(sell) || !Number.isFinite(lot) || lot <= 0) continue;
    const unitProfit = sell * (1 + RAISE) * (1 - TAX) - buy * (1 - BARGAIN);
    if (unitProfit <= 0) continue;
    candidates.push({ name: product.name, unitProfit, available: lot * (books + 1) });
  }
  candidates.sort((a, b) => b.unitProfit - a.unitProfit);
  let remaining = CARGO;
  let profit = 0;
  const buys = [];
  for (const item of candidates) {
    if (remaining <= 0) break;
    const count = Math.min(remaining, item.available);
    if (count <= 0) continue;
    profit += count * item.unitProfit;
    remaining -= count;
    buys.push([item.name, count, Math.round(item.unitProfit)]);
  }
  return { from, to, books, profit: Math.round(profit), cargo: CARGO - remaining, buys };
}

function compositions(total, parts, prefix = []) {
  if (parts === 1) return [[...prefix, total]];
  const out = [];
  for (let i = 0; i <= total; i++) out.push(...compositions(total - i, parts - 1, [...prefix, i]));
  return out;
}

function evaluateCycle(cycle, allocation) {
  const legs = [];
  let totalProfit = 0;
  let totalFatigue = 0;
  for (let i = 0; i < cycle.length; i++) {
    const from = cycle[i];
    const to = cycle[(i + 1) % cycle.length];
    const travel = fatigue.get(`${from}|${to}`);
    if (!travel) return null;
    const result = leg(from, to, allocation[i]);
    totalProfit += result.profit;
    totalFatigue += travel + NEGOTIATION_FATIGUE_PER_LEG;
    legs.push({ ...result, travelFatigue: travel });
  }
  return {
    cycle,
    allocation,
    totalProfit,
    totalFatigue,
    profitPerFatigue: totalProfit / totalFatigue,
    legs,
  };
}

const results = [];
for (const length of [2, 3]) {
  const allocations = compositions(BOOKS, length);
  const visit = (prefix, remaining) => {
    if (prefix.length === length) {
      // Rotations describe the same cycle; keep only the lexically smallest start.
      if (prefix[0] !== [...prefix].sort()[0]) return;
      for (const allocation of allocations) {
        const result = evaluateCycle(prefix, allocation);
        if (result) results.push(result);
      }
      return;
    }
    for (const city of remaining) visit([...prefix, city], remaining.filter((x) => x !== city));
  };
  visit([], cities);
}

results.sort((a, b) => b.profitPerFatigue - a.profitPerFatigue || b.totalProfit - a.totalProfit);
const baselineOneBook = evaluateCycle(["武林源", "岚心城", "栖羽站"], [1, 0, 0]);
const baselineTenBooks = evaluateCycle(["武林源", "岚心城", "栖羽站"], [10, 0, 0]);
const baselineCycleBest = compositions(BOOKS, 3)
  .map((allocation) => evaluateCycle(["武林源", "岚心城", "栖羽站"], allocation))
  .sort((a, b) => b.profitPerFatigue - a.profitPerFatigue)[0];

function evaluateWeeklyCycle(cycle) {
  const routeFatigue = cycle.reduce((sum, from, index) => {
    const to = cycle[(index + 1) % cycle.length];
    return sum + (fatigue.get(`${from}|${to}`) || 0) + NEGOTIATION_FATIGUE_PER_LEG;
  }, 0);
  if (!routeFatigue) return null;
  const repeats = Math.floor(WEEKLY_FATIGUE / routeFatigue);
  if (!repeats) return null;
  let baseProfit = 0;
  const legTypes = cycle.map((from, index) => {
    const to = cycle[(index + 1) % cycle.length];
    const profits = [];
    for (let books = 0; books <= BOOKS; books++) profits.push(leg(from, to, books).profit);
    baseProfit += repeats * profits[0];
    // Best gain from distributing b books over identical visits of this leg.
    let gains = Array(BOOKS + 1).fill(Number.NEGATIVE_INFINITY);
    gains[0] = 0;
    const allocations = Array.from({ length: BOOKS + 1 }, () => []);
    for (let repeat = 0; repeat < repeats; repeat++) {
      const next = Array(BOOKS + 1).fill(Number.NEGATIVE_INFINITY);
      const nextAllocations = Array.from({ length: BOOKS + 1 }, () => []);
      for (let used = 0; used <= BOOKS; used++) {
        if (!Number.isFinite(gains[used])) continue;
        for (let add = 0; used + add <= BOOKS; add++) {
          const gain = profits[add] - profits[0];
          if (gains[used] + gain > next[used + add]) {
            next[used + add] = gains[used] + gain;
            nextAllocations[used + add] = [...allocations[used], add];
          }
        }
      }
      gains = next;
      for (let i = 0; i <= BOOKS; i++) allocations[i] = nextAllocations[i];
    }
    return { from, to, gains, allocations };
  });
  let dp = Array(BOOKS + 1).fill(Number.NEGATIVE_INFINITY);
  dp[0] = 0;
  const paths = Array.from({ length: BOOKS + 1 }, () => []);
  for (const legType of legTypes) {
    const next = Array(BOOKS + 1).fill(Number.NEGATIVE_INFINITY);
    const nextPaths = Array.from({ length: BOOKS + 1 }, () => []);
    for (let used = 0; used <= BOOKS; used++) {
      if (!Number.isFinite(dp[used])) continue;
      for (let add = 0; used + add <= BOOKS; add++) {
        const gain = legType.gains[add];
        if (dp[used] + gain > next[used + add]) {
          next[used + add] = dp[used] + gain;
          nextPaths[used + add] = [
            ...paths[used],
            {
              books: add,
              from: legType.from,
              to: legType.to,
              perVisit: legType.allocations[add].filter((value) => value > 0),
            },
          ];
        }
      }
    }
    dp = next;
    for (let i = 0; i <= BOOKS; i++) paths[i] = nextPaths[i];
  }
  let bestBooks = 0;
  for (let books = 1; books <= BOOKS; books++) if (dp[books] > dp[bestBooks]) bestBooks = books;
  return {
    cycle,
    repeats,
    routeFatigue,
    totalFatigue: repeats * routeFatigue,
    totalProfit: Math.round(baseProfit + dp[bestBooks]),
    profitPerFatigue: (baseProfit + dp[bestBooks]) / (repeats * routeFatigue),
    booksUsed: bestBooks,
    bookPlan: paths[bestBooks].filter((item) => item.books > 0),
  };
}

const weeklyResults = [];
for (const length of [2, 3, 4]) {
  const visit = (prefix, remaining) => {
    if (prefix.length === length) {
      if (prefix[0] !== [...prefix].sort()[0]) return;
      const result = evaluateWeeklyCycle(prefix);
      if (result) weeklyResults.push(result);
      return;
    }
    for (const city of remaining) visit([...prefix, city], remaining.filter((x) => x !== city));
  };
  visit([], cities);
}
weeklyResults.sort((a, b) => b.totalProfit - a.totalProfit || b.profitPerFatigue - a.profitPerFatigue);

console.log(JSON.stringify({
  assumptions: { CARGO, BOOKS, BARGAIN, RAISE, TAX, NEGOTIATION_FATIGUE_PER_LEG, WEEKLY_FATIGUE },
  baselineOneBook,
  baselineTenBooks,
  baselineCycleBest,
  top: results.slice(0, 15),
  weeklyTop: weeklyResults.slice(0, 15),
}, null, 2));
