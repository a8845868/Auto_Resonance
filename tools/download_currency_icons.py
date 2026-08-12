"""Download documented item icons from the Resonance BWIKI MediaWiki API."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, urlretrieve


NAMES = (
    "铁盟币", "桦石", "里程点数", "赴命奖章", "绝命奖章", "黑月采购券",
    "星云物质（4钛）", "自观测胶卷", "一般武装改造凭证", "特殊武装改造特许",
    "进货采买书", "广告投放券", "银枝薄荷糖", "“一元二次”", "“豆蔻姜百合”",
    "再交涉请求书", "诱饵气球", "追加注资申请书", "胡尔顿气球", "世界团结",
    "清醒梦纤维", "错峰出行",
)


def main():
    output = Path(__file__).resolve().parents[1] / "resources" / "currency"
    output.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name in NAMES:
        query = urlencode({
            "action": "query", "format": "json", "prop": "imageinfo",
            "iiprop": "url", "iiurlwidth": 96, "titles": f"File:{name}.png",
        })
        with urlopen(f"https://wiki.biligame.com/resonance/api.php?{query}", timeout=20) as response:
            data = json.load(response)
        page = next(iter(data["query"]["pages"].values()))
        if "imageinfo" not in page:
            print(f"missing: {name}")
            continue
        url = page["imageinfo"][0].get("thumburl") or page["imageinfo"][0]["url"]
        filename = f"{name}.png".replace("/", "_")
        urlretrieve(url, output / filename)
        manifest[name] = filename
        print(f"downloaded: {name}")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
