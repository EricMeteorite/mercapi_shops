# mercapi_shops/models.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List

@dataclass
class PageInfo:
    hasNextPage: bool
    endCursor:   str

@dataclass
class ShopProductAsset:
    imageUrl: str

@dataclass
class ShopProduct:
    id:        str
    name:      str
    price:     int
    inStock:   bool
    assets:    List[ShopProductAsset]
    createdAt: str | None = None   # === NEW: 用于新着順（CREATED_AT）本地兜底排序（可缺省）

@dataclass
class ShopSearchResults:
    pageInfo: PageInfo
    items:    List[ShopProduct]
    _request: "ShopsSearchRequestData" = field(init=False, repr=False, compare=False)

    async def next_page(self):
        if not self.pageInfo.hasNextPage:
            raise RuntimeError("Already at last page")
        new_req = self._request.copy_with_cursor(self.pageInfo.endCursor)
        # 保持与 search() 一致：_post_graphql 会附加 _request/_api，并做兜底过滤与排序
        return await self._request._api._post_graphql(new_req)  # pylint: disable=protected-access
