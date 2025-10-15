# mercapi-shops

![PyPI](https://img.shields.io/pypi/v/mercapi_shops)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/mercapi_shops)

Async client for **mercari-shops.com** (enterprise sellers on Mercari).  
非官方 Mercari Shops（企业商户）GraphQL 异步客户端。

- Python 3.9+
- 基于 `httpx` 异步请求
- 类型完备（PEP 561，内置 `py.typed`）

> ✅ 面向企业商家（mercari-shops.com）的**异步** Python API。  
> 
> ✅ 提供 GraphQL `search()`（可分页，全量）与店铺总页首屏抓取 `landing()`（低延迟）两种能力。
> 
> ✅ 已在生产脚本中验证：支持按店铺 + 关键词抓取、分页、可选在库筛选、客户端价格排序。  
> 
> ✅ 需要时可传入站点 cookies（如 `__cf_bm`, `_cfuvid`）。

---

## Installation

**From source (editable):**
```bash
#Local installation
pip install -U pip build
pip install -e .
```
**From PyPi:**
```bash
#pip installation
pip install mercapi-shops
```
## Quick Start
```bash
import asyncio
from mercapi_shops import MercapiShops

SHOP_ID = "d2uUKgmbjTGT7BzBGUnXxe"  # 示例：MANDARAKE

async def main():
    async with MercapiShops() as api:
        # 1) GraphQL 搜索（可分页、全量）
        res = await api.search(
            "アゾン",
            shop_id=SHOP_ID,
            in_stock=True,            # 仅在库（本地过滤）
            order_by="PRICE_ASC",     # 价格升序（本地兜底排序）
            # local_keyword="..."     # 可选：二次本地过滤（NFKC+lower，空白分词 AND）
        )
        print("First page:", len(res.items))
        for p in res.items[:5]:
            print(p.id, p.name, p.price, p.inStock, p.assets[0].imageUrl if p.assets else None)

        if res.pageInfo.hasNextPage:
            res2 = await res.next_page()
            print("Next page:", len(res2.items))

        # 2) 店铺总页首屏（低延迟，更快发现“新上架”，但非全量）
        latest = await api.landing(
            shop_id=SHOP_ID,
            in_stock=None,           # True / False / None
            keyword="アゾン",         # 本地关键词过滤（NFKC+lower，空白分词 AND）
            limit=120
        )
        print("Landing size:", len(latest))
        for p in latest[:5]:
            print(p.id, p.name, p.price, p.inStock, p.assets[0].imageUrl if p.assets else None)

asyncio.run(main())


```

## 功能特性
**GraphQL search()**

> - 支持分页（pageInfo.hasNextPage / await results.next_page()）。
> 
> - 支持本地在库过滤 in_stock=True/False/None。
> 
> - 支持价格本地排序兜底：PRICE_ASC / PRICE_DESC（CREATED_AT 依赖后端，不做本地兜底）。
> 
> - 可选 local_keyword 二次本地过滤，不影响后端请求。

**landing() 店铺总页首屏抓取**

> - 通常更快出现最新上架商品（低延迟）。
> 
> - 仅抓取店铺总页首屏，不分页，非全量。
> 
> - 支持 in_stock 本地过滤与 keyword 本地关键词过滤。

**图片 URL 归一化**

> - 自动解析/展开 /_next/image?url=...、相对地址等，输出可直连的绝对 URL，方便下载与消息推送。

**类型完备**

> - 内置 py.typed，对 ShopProduct / ShopProductAsset / ShopSearchResults / PageInfo 等提供静态类型。


## 版本变更
**v0.0.4**
- 修复：mercari-shops.com 2025-10-15 前端更新导致的搜索失败问题。

**v0.0.2**
- 新增：landing(shop_id, in_stock, keyword, limit)，快速抓取店铺总页首屏；
- 新增：search(..., local_keyword=...) 二次本地过滤；
- 改进：图片 URL 归一化（解析 /_next/image?url=... 与相对链接 → 绝对直链）；
- 改进：更健壮地解析 Next.js / Apollo 存储与 __ref，提升首屏解析成功率；
- 完善类型与文档。


## 许可协议
**MIT License**

## 免责声明

>本项目为非官方客户端，仅用于技术研究与个人工具整合，请勿用于任何商业用途。
> 
>请遵守目标站点的服务条款与法律法规，自行承担使用风险。