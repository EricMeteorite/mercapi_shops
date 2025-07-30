# mercapi_shops/mercapi_shops.py
# -*- coding: utf-8 -*-
"""
Enterprise-seller (mercari-shops.com) async client.

Example
-------
    from mercapi_shops import MercapiShops
    api = MercapiShops()
    res = await api.search("アゾン", shop_id="d2uUKgmbjTGT7BzBGUnXxe")
"""
from __future__ import annotations

import httpx, logging, re, json, unicodedata
from typing import Optional, Dict, List, Any
# === NEW: URL 解析用于展开 /_next/image 和相对链接 ===
from urllib.parse import urlparse, parse_qs, unquote

from mercapi_shops.requests import ShopsSearchRequestData
from mercapi_shops.mapping  import map_to_class
from mercapi_shops.models   import ShopSearchResults, ShopProduct, ShopProductAsset

log = logging.getLogger(__name__)

class MercapiShops:
    GRAPHQL_URL = "https://mercari-shops.com/graphql"
    SEARCH_QUERY = """
    query SearchTop($search: ProductSearchCriteria!, $cursor: String, $shopIds: [String!]) {
      products(search: $search, after: $cursor, shopIds: $shopIds, first: 100){
        pageInfo { hasNextPage endCursor }
        edges    { node { id name price inStock assets { imageUrl } } }
      }
    }
    """

    _NEXT_DATA_RX = re.compile(
        r'<script id="__NEXT_DATA__" type="application/json">([\s\S]+?)</script>',
        re.S
    )

    def __init__(
        self,
        *,
        proxies: Optional[Dict[str, str]] = None,
        user_agent: str = "Mozilla/5.0",
        cookies: Optional[Dict[str, str]] = None,
    ) -> None:
        self._client  = httpx.AsyncClient(proxies=proxies, cookies=cookies or {})
        self._headers = {
            "User-Agent":       user_agent,
            "Referer":          "https://mercari-shops.com/",
            "Content-Type":     "application/json; charset=utf-8",
            "x-data-fetch-for": "csr",
            "Accept":           "application/json",  # === NEW: 兼容性更好
        }

    # ───────── public API ─────────
    async def search(
        self,
        keyword: str,
        *,
        shop_id: str,
        cursor: str = "",
        in_stock: Optional[bool] = None,       # 保持你上版新增的参数
        order_by: Optional[str] = None,        # 保持你上版新增的参数
        local_keyword: Optional[str] = None,   # === NEW: 可选“本地二次过滤”，默认不启用
    ) -> ShopSearchResults:
        req = ShopsSearchRequestData(
            keyword, shop_id, cursor,
            in_stock=in_stock, order_by=order_by
        )
        res = await self._post_graphql(req)

        # === NEW: 本地二次过滤（可选；不破坏原有行为）
        if local_keyword:
            tokens = self._normalize_tokens(local_keyword)
            if tokens:
                res.items = [it for it in (res.items or [])
                             if self._match_tokens(it.name, tokens)]

        return res

    async def landing(
        self,
        shop_id: str,
        *,
        in_stock: Optional[bool] = None,
        keyword: Optional[str] = None,   # 本地关键词过滤（首屏集合上）
        limit: int = 150,                # 首屏一般几十到 100；150 足够
    ) -> List[ShopProduct]:
        """
        拉取店铺总页“首屏最新商品”，并可选做本地关键词过滤。
        - 比 GraphQL 搜索更快发现新上架，但只覆盖首屏（非全量）。
        - keyword：在 name/title 上做 NFKC+lower 的子串匹配；多词空白分割 → AND。
        """
        # 1) GET HTML
        r = await self._client.get(
            f"https://mercari-shops.com/shops/{shop_id}",
            headers={**self._headers, "Accept": "text/html,application/xhtml+xml"},
            timeout=(5, 30),
        )
        r.raise_for_status()
        html = r.text

        # 2) 解析 __NEXT_DATA__
        m = self._NEXT_DATA_RX.search(html)
        build_id = None
        products: List[Dict[str, Any]] = []
        if m:
            try:
                data = json.loads(m.group(1))
                build_id = data.get("buildId")
                store = self._find_apollo_store(data)
                if store:
                    products = self._apollo_collect_products(store)
                if not products:
                    products = self._extract_products_relaxed(data)
            except Exception as e:
                log.debug("landing: parse __NEXT_DATA__ error: %s", e)

        # 3) 若还没有，/_next/data/<buildId>/shops/<id>.json 尝试一次
        if not products and build_id:
            j = await self._client.get(
                f"https://mercari-shops.com/_next/data/{build_id}/shops/{shop_id}.json",
                headers={**self._headers, "Accept": "application/json"},
                timeout=(5, 30),
            )
            if j.status_code == 200:
                try:
                    jj = j.json()
                    store = self._find_apollo_store(jj)
                    if store:
                        products = self._apollo_collect_products(store)
                    if not products:
                        products = self._extract_products_relaxed(jj)
                except Exception as e:
                    log.debug("landing: parse _next data error: %s", e)

        # 4) 过滤在库/卖空（若 inStock 缺失则不剔除）
        if in_stock is not None:
            tmp = []
            for p in products:
                v = p.get("inStock")
                if isinstance(v, bool):
                    if v is in_stock:
                        tmp.append(p)
                else:
                    tmp.append(p)
            products = tmp

        # 5) 关键词本地过滤
        if keyword:
            tokens = self._normalize_tokens(keyword)
            if tokens:
                tmp: List[Dict[str, Any]] = []
                for p in products:
                    name = (p.get("name") or p.get("title") or "").strip()
                    if self._match_tokens(name, tokens):
                        tmp.append(p)
                products = tmp

        # 6) 映射
        out: List[ShopProduct] = []
        seen = set()
        for p in products:
            pid = p.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            name  = (p.get("name") or p.get("title") or "").strip()
            price = p.get("price")
            if isinstance(price, dict):
                price = price.get("value") or price.get("amount") or 0
            try:
                price = int(price or 0)
            except Exception:
                price = 0
            instock = p.get("inStock")
            instock = bool(instock) if isinstance(instock, bool) else True
            img = self._first_image_url(p)  # ← 会归一化到可直连绝对链接
            assets = [ShopProductAsset(imageUrl=img)] if img else []
            out.append(ShopProduct(id=pid, name=name, price=price, inStock=instock, assets=assets))

        return out[:limit]

    # ───────── internal: GraphQL ─────────
    async def _post_graphql(self, req: ShopsSearchRequestData) -> ShopSearchResults:
        payload = {
            "query":          self.SEARCH_QUERY,
            "operationName":  "SearchTop",   # 明确 operationName（有些后端偏好）
            "variables":      req.data,
        }
        r = await self._client.post(
            self.GRAPHQL_URL,
            json=payload,
            headers=self._headers,
            timeout=(5, 60),
        )
        # 如果返回 4xx/5xx，先打印 body 便于排错，再抛异常
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = r.text[:500]
            log.error("GraphQL HTTP error: %s, body=%s", e, body)
            raise

        j = r.json()
        if "errors" in j:
            # GraphQL 语义错误：抛出以便外层重试逻辑接手
            raise RuntimeError(j["errors"])
        res = map_to_class(j["data"]["products"], ShopSearchResults)

        # 保持分页可用
        res._request = req          # for next_page()
        res._request._api = self    # weak ref back to api

        # 兜底过滤/排序（createdAt 未在查询中显式请求；CREATED_AT 依赖后端）
        items = list(res.items) if res.items else []

        if req.in_stock is True:
            items = [i for i in items if getattr(i, "inStock", None) is True]
        elif req.in_stock is False:
            items = [i for i in items if getattr(i, "inStock", None) is False]

        if req.order_by == "PRICE_ASC":
            items.sort(key=lambda x: (getattr(x, "price", None) is None, getattr(x, "price", None)))
        elif req.order_by == "PRICE_DESC":
            items.sort(key=lambda x: (getattr(x, "price", None) is None, getattr(x, "price", None)), reverse=True)
        # elif req.order_by == "CREATED_AT":  # 不做本地兜底，交给后端

        res.items = items
        return res

    # ───────── utils ─────────
    @staticmethod
    def _normalize(s: str) -> str:
        return unicodedata.normalize("NFKC", s).lower()

    def _normalize_tokens(self, keyword: str) -> List[str]:
        return [self._normalize(t) for t in keyword.split() if t.strip()]

    def _match_tokens(self, text: str, tokens: List[str]) -> bool:
        nt = self._normalize(text or "")
        return all(t in nt for t in tokens)

    # === NEW: 统一把图片链接归一化为可直连的绝对 URL（展开 /_next/image、处理 // 与 / 开头）
    @staticmethod
    def _normalize_img_url(u: str) -> str:
        if not u:
            return ""
        try:
            if u.startswith("/_next/image"):
                qs = parse_qs(urlparse(u).query)
                raw = unquote(qs.get("url", [""])[0])
                if raw:
                    if raw.startswith("//"):
                        return "https:" + raw
                    if raw.startswith("/"):
                        return "https://mercari-shops.com" + raw
                    return raw
                return "https://mercari-shops.com" + u
            if u.startswith("//"):
                return "https:" + u
            if u.startswith("/"):
                return "https://mercari-shops.com" + u
            return u
        except Exception:
            return u

    @staticmethod
    def _first_image_url(obj: Dict[str, Any]) -> str:
        # 1) 处理 assets：可能是 list，也可能是 {"edges":[{"node": {...}}]}
        assets = obj.get("assets") or []
        if isinstance(assets, dict) and isinstance(assets.get("edges"), list):
            cand_list = []
            for e in assets["edges"]:
                if isinstance(e, dict):
                    nd = e.get("node")
                    if isinstance(nd, dict):
                        cand_list.append(nd)
                    elif isinstance(e.get("__ref"), dict):
                        cand_list.append(e["__ref"])
            assets = cand_list

        def _from_asset_dict(a: Dict[str, Any]) -> str:
            # 常见直连字段
            for k in (
            "imageUrl", "url", "src", "mainImageUrl", "primaryImageUrl", "coverImageUrl", "thumbnailUrl", "path"):
                v = a.get(k)
                if isinstance(v, str) and v:
                    return MercapiShops._normalize_img_url(v)
            # a.image.url
            img = a.get("image")
            if isinstance(img, dict):
                u = img.get("url") or img.get("src") or img.get("path")
                if isinstance(u, str) and u:
                    return MercapiShops._normalize_img_url(u)
            return ""

        # 1.1) assets 里找
        if isinstance(assets, list):
            for a in assets:
                if isinstance(a, dict):
                    u = _from_asset_dict(a)
                    if u:
                        return u

        # 2) 商品对象自身的常见字段
        for k in (
        "imageUrl", "thumbnailUrl", "firstImageUrl", "mainImageUrl", "primaryImageUrl", "coverImageUrl", "image"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return MercapiShops._normalize_img_url(v)
            if isinstance(v, dict):
                u = v.get("url") or v.get("src") or v.get("path")
                if isinstance(u, str) and u:
                    return MercapiShops._normalize_img_url(u)

        # 2.1) 有些结构是 images: [{url: ...}, {src: ...}]
        images = obj.get("images")
        if isinstance(images, list):
            for it in images:
                if isinstance(it, dict):
                    for k in ("url", "src", "path", "imageUrl"):
                        u = it.get(k)
                        if isinstance(u, str) and u:
                            return MercapiShops._normalize_img_url(u)

        # 3) 兜底：深度扫描，拿到第一个“像图片”的 URL
        def looks_like_image(u: str) -> bool:
            if not isinstance(u, str) or not u:
                return False
            s = u.lower()
            # 同时兼容 next/image 包裹和直接域名
            return (
                    s.startswith("http") and (".jpg" in s or ".jpeg" in s or ".png" in s or ".webp" in s)
            ) or ("image.mercari" in s or "mercdn" in s or ".jpg" in s or ".png" in s or ".webp" in s)

        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                # 先看常见 key
                for k in ("imageUrl", "thumbnailUrl", "url", "src", "path"):
                    v = cur.get(k)
                    if isinstance(v, str) and looks_like_image(v):
                        return MercapiShops._normalize_img_url(v)
                    if isinstance(v, (dict, list)):
                        stack.append(v)
                # 再看所有值
                for v in cur.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
                    elif isinstance(v, str) and looks_like_image(v):
                        return MercapiShops._normalize_img_url(v)
            elif isinstance(cur, list):
                stack.extend(cur)

        return ""

    @staticmethod
    def _looks_like_product_relaxed(d: Dict[str, Any]) -> bool:
        if not isinstance(d, dict):
            return False
        if "id" not in d:
            return False
        if not (isinstance(d.get("name"), str) or isinstance(d.get("title"), str)):
            return False
        # 价格可缺省；有图或有 price 即可
        if d.get("price") is None and not MercapiShops._first_image_url(d):
            return False
        return True

    @classmethod
    def _extract_products_relaxed(cls, obj) -> List[Dict[str, Any]]:
        res: List[Dict[str, Any]] = []
        if isinstance(obj, dict):
            if cls._looks_like_product_relaxed(obj):
                res.append(obj)
            for v in obj.values():
                res.extend(cls._extract_products_relaxed(v))
        elif isinstance(obj, list):
            for it in obj:
                res.extend(cls._extract_products_relaxed(it))
        return res

    @classmethod
    def _find_apollo_store(cls, obj) -> Optional[Dict[str, Any]]:
        # 递归找一个看起来像 Apollo 缓存的 dict（包含 ROOT_QUERY 或大量 "Xxx:id"）
        best, best_score = None, -1
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                keys = list(cur.keys())
                score = 0
                if "ROOT_QUERY" in cur:
                    score += 5
                score += sum(1 for k in keys if isinstance(k, str) and ":" in k)
                if score > best_score:
                    best, best_score = cur, score
                for v in cur.values():
                    stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)
        return best

    @classmethod
    def _apollo_collect_products(cls, store: Dict[str, Any]) -> List[Dict[str, Any]]:
        # 把 apollo store 变成可索引的字典
        index = {k: v for k, v in store.items() if isinstance(v, dict)}
        out: List[Dict[str, Any]] = []

        def _deep_deref(o, depth: int = 6):
            """递归把任意层的 {'__ref': 'Key'} 解成实体；同时递归处理 list/dict。"""
            if depth <= 0:
                return o
            if isinstance(o, dict):
                # 先解当前层
                if "__ref" in o and isinstance(o["__ref"], str) and o["__ref"] in index:
                    return _deep_deref(index[o["__ref"]], depth - 1)
                # 再解子项
                return {k: _deep_deref(v, depth - 1) for k, v in o.items()}
            if isinstance(o, list):
                return [_deep_deref(v, depth - 1) for v in o]
            return o

        # 1) 优先从含有 edges 的节点收集 node（可能是产品或产品片段）
        for v in index.values():
            edges = v.get("edges")
            if isinstance(edges, list) and edges:
                for e in edges:
                    if not isinstance(e, dict):
                        continue
                    node = e.get("node")
                    cand = None
                    if isinstance(node, dict) and node:
                        cand = node
                    elif isinstance(node, str) and node in index:
                        cand = index[node]
                    elif isinstance(e.get("__ref"), str) and e["__ref"] in index:
                        cand = index[e["__ref"]]
                    if cand:
                        out.append(_deep_deref(cand))  # 关键：递归解引用

        if out:
            seen, dedup = set(), []
            for n in out:
                pid = n.get("id")
                if pid and pid not in seen:
                    seen.add(pid)
                    # 保险：确保 assets 被递归解引用（如有）
                    if "assets" in n:
                        n["assets"] = _deep_deref(n["assets"])
                    dedup.append(n)
            return dedup

        # 2) 无 edges 时：全库扫描“像商品”的实体，并递归解引用
        seen, dedup = set(), []
        for v in index.values():
            if cls._looks_like_product_relaxed(v):
                n = _deep_deref(v)
                pid = n.get("id")
                if pid and pid not in seen:
                    seen.add(pid)
                    if "assets" in n:
                        n["assets"] = _deep_deref(n["assets"])
                    dedup.append(n)
        return dedup

    async def __aenter__(self):  # optional context-manager
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._client.aclose()
