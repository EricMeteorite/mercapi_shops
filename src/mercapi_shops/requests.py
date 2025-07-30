# mercapi_shops/requests.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

@dataclass
class ShopsSearchRequestData:
    keyword: str
    shop_id: str
    cursor:  str = ""
    in_stock: Optional[bool] = None    # 保留：販売中/売り切れ/全て（仅用于客户端本地过滤）
    order_by: Optional[str] = None     # 保留字段，但不再发给后端；供客户端排序使用

    @property
    def data(self):
        # 只给后端传递 query；不要传 inStock（当前 schema 不支持，会触发 400）
        search = {"query": self.keyword}

        # 注意：不要把 in_stock / order_by 放到 search 里，否则会 400
        # （错误示例：Field "inStock" is not defined by type "ProductSearchCriteria"）

        return {
            "search":  search,
            "cursor":  self.cursor or None,
            "shopIds": [self.shop_id],
        }

    # helper for next_page()
    def copy_with_cursor(self, cursor: str) -> "ShopsSearchRequestData":
        return ShopsSearchRequestData(
            self.keyword, self.shop_id, cursor,
            in_stock=self.in_stock, order_by=self.order_by
        )
