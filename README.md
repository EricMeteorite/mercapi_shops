# mercapi-shops

Async client for **mercari-shops.com** (enterprise sellers on Mercari).  
非官方 Mercari Shops（企业商户）GraphQL 异步客户端。

> ✅ 已在生产脚本中验证：支持按店铺 + 关键词抓取、分页、可选在库筛选、客户端价格排序。  
> ⚠️ 需要时可传入站点 cookies（如 `__cf_bm`, `_cfuvid`）。

---

## Installation

**From source (editable):**
```bash
pip install -U pip build
pip install -e .
```
## Quick Start
```bash
import asyncio
from mercapi_shops import MercapiShops

async def main():
    # 可选：传入 cookies（如 Cloudflare `__cf_bm`, `_cfuvid`）
    cookies = {
        # "__cf_bm": "...",
        # "_cfuvid": "..."
    }

    api = MercapiShops(cookies=cookies)
    # 关键词搜索（按店内搜索），支持 in_stock 与 order_by（客户端价格排序）
    res = await api.search(
        "アゾン",
        shop_id="d2uUKgmbjTGT7BzBGUnXxe",
        in_stock=True,               # True: 販売中のみ, False: 売り切れのみ, None: 全て
        order_by="CREATED_AT"         # "PRICE_ASC" / "PRICE_DESC" / "CREATED_AT"(交由后端默认)
    )

    print("count:", len(res.items))
    for p in res.items[:5]:
        first_img = p.assets[0].imageUrl if p.assets else ""
        print(p.id, p.name, p.price, p.inStock, first_img)

    # 分页
    if res.pageInfo.hasNextPage:
        res2 = await res.next_page()
        print("next page count:", len(res2.items))

asyncio.run(main())

```