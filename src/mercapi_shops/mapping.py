# mercapi_shops/mapping.py
from __future__ import annotations
from typing import Dict, Callable, List, Type, TypeVar, Optional
from dataclasses import is_dataclass

from mercapi_shops.models import (
    PageInfo, ShopProductAsset, ShopProduct, ShopSearchResults
)

T = TypeVar("T")

def _get(key: str) -> Callable[[dict], Optional[T]]:
    return lambda src: src.get(key)

def _list_of(model: Type[T]) -> Callable[[list], List[T]]:
    return lambda items: [map_to_class(i, model) for i in items or []]

mapping: Dict[Type, Dict[str, Callable]] = {
    PageInfo: {
        "hasNextPage": _get("hasNextPage"),
        "endCursor":   _get("endCursor"),
    },
    ShopProductAsset: {
        "imageUrl": _get("imageUrl"),
    },
    ShopProduct: {
        "id":        _get("id"),
        "name":      _get("name"),
        "price":     _get("price"),
        "inStock":   _get("inStock"),
        "assets":    lambda src: _list_of(ShopProductAsset)(src.get("assets")),
        "createdAt": _get("createdAt"),  # === NEW: 为“新着順”兜底排序提供时间字段（不强制依赖）
    },
    ShopSearchResults: {
        "pageInfo": lambda src: map_to_class(src["pageInfo"], PageInfo),
        "items":    lambda src: [_edge_to_product(e) for e in src["edges"]],
    },
}

def _edge_to_product(edge: dict) -> ShopProduct:
    return map_to_class(edge["node"], ShopProduct)

def map_to_class(src: dict, clazz: Type[T]) -> T:
    if not is_dataclass(clazz):
        raise TypeError(f"{clazz} must be dataclass")
    if clazz not in mapping:
        raise ValueError(f"No mapping defined for {clazz}")
    kwargs = {k: f(src) for k, f in mapping[clazz].items()}
    return clazz(**kwargs)
