# mercapi_shops/mercapi_shops.py
# -*- coding: utf-8 -*-
"""
Enterprise-seller (mercari-shops.com / jp.mercari.com) async client.

Example
-------
    from mercapi_shops import MercapiShops
    api = MercapiShops()
    res = await api.search("アゾン", shop_id="d2uUKgmbjTGT7BzBGUnXxe")

Notes
-----
landing():
    只抓“店铺首页首屏”（不是全量搜索）。
    新域名已从 https://mercari-shops.com/shops/{id} 切到 https://jp.mercari.com/shops/profile/{id}。
    首选 BFF（/services/bff/shops/v1/contents/shops/{id}/products），失败时回退 SSR（HTML/_next/data）。
"""
from __future__ import annotations

import asyncio, random
import httpx, logging, re, json, unicodedata, hashlib, time, uuid, base64
from httpx import Limits
from typing import Optional, Dict, List, Any, Tuple
from urllib.parse import urlparse, parse_qs, unquote

from mercapi_shops.requests import ShopsSearchRequestData
from mercapi_shops.mapping  import map_to_class
from mercapi_shops.models   import ShopSearchResults, ShopProduct, ShopProductAsset

log = logging.getLogger(__name__)

# ───────── Cloudflare/网络瞬时错误的可重试状态 ─────────
_RETRIABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530}

# ───────── DPoP 轻量实现（ES256, P-256） ─────────
_CRYPTO_READY = True
try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.backends import default_backend
except Exception:  # pragma: no cover
    _CRYPTO_READY = False

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def _int_to_32be(n: int) -> bytes:
    b = n.to_bytes(32, "big", signed=False)
    return b[-32:] if len(b) > 32 else (b"\x00" * (32 - len(b)) + b)

class _DPoPKey:
    """极简 DPoP：P-256 私钥 + JWK + JWS(ES256 r||s)"""
    def __init__(self, pem: Optional[str] = None):
        if not _CRYPTO_READY:
            raise RuntimeError("cryptography 未安装，无法自动生成 DPoP。请传入 bff_dpop，或安装 cryptography。")
        if pem:
            priv = serialization.load_pem_private_key(pem.encode("utf-8"), password=None, backend=default_backend())
            if not isinstance(priv, ec.EllipticCurvePrivateKey) or not isinstance(priv.curve, ec.SECP256R1):
                raise ValueError("提供的 PEM 不是 P-256 私钥")
            self._priv = priv
        else:
            self._priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
        pub = self._priv.public_key().public_numbers()
        self._jwk = {"kty": "EC", "crv": "P-256",
                     "x": _b64url(_int_to_32be(pub.x)),
                     "y": _b64url(_int_to_32be(pub.y))}

    def sign_compact(self, header: Dict[str, Any], payload: Dict[str, Any]) -> str:
        h = _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        p = _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        signing_input = f"{h}.{p}".encode("ascii")
        der = self._priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = _int_to_32be(r) + _int_to_32be(s)
        return f"{h}.{p}.{_b64url(sig)}"

    def make_dpop(self, method: str, url: str) -> str:
        now = int(time.time())
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self._jwk}
        payload = {"iat": now, "jti": str(uuid.uuid4()), "htm": method.upper(), "htu": url}
        return self.sign_compact(header, payload)

class MercapiShops:
    # 新店铺页域名
    SHOP_WEB_ORIGIN = "https://jp.mercari.com"

    # GraphQL（search 用；保持旧域名以兼容）
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
    _BUILD_ID_RX  = re.compile(r'"buildId"\s*:\s*"([^"]+)"')

    def __init__(
        self,
        *,
        proxies: Optional[Dict[str, str]] = None,
        user_agent: str = "Mozilla/5.0",
        cookies: Optional[Dict[str, str]] = None,
        # ↓↓↓ BFF 直连可选参数（不影响 search()）
        bff_origin: Optional[str] = None,         # 默认自动用 https://api.mercari.jp
        bff_auth_bearer: Optional[str] = None,    # 如需要可传 Authorization: Bearer xxx
        bff_dpop: Optional[str] = None,           # 可直接传浏览器抓到的 dpop
        dpop_priv_pem: Optional[str] = None,      # 不传 dpop 时，自动用 ES256 现签；可传入固定私钥 PEM
    ) -> None:
        self._client  = httpx.AsyncClient(
            proxies=proxies,
            cookies=cookies or {},
            http2=True,
            limits=Limits(max_connections=50, max_keepalive_connections=20),
        )
        self._headers = {
            "User-Agent":       user_agent,
            "Referer":          f"{self.SHOP_WEB_ORIGIN}/shops",
            "Content-Type":     "application/json; charset=utf-8",
            "x-data-fetch-for": "csr",
            "Accept":           "application/json",
            "Accept-Language":  "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        # BFF 相关
        self._bff_origin = (bff_origin or "").rstrip("/") or None
        if bff_auth_bearer:
            bff_auth_bearer = bff_auth_bearer.strip()
            if not bff_auth_bearer.lower().startswith("bearer "):
                bff_auth_bearer = "Bearer " + bff_auth_bearer
        self._bff_auth_bearer = bff_auth_bearer
        self._bff_dpop_override = (bff_dpop or "").strip() or None
        self._dpop_key: Optional[_DPoPKey] = None
        if not self._bff_dpop_override:
            if dpop_priv_pem:
                self._dpop_key = _DPoPKey(dpop_priv_pem)
            elif _CRYPTO_READY:
                self._dpop_key = _DPoPKey()
            else:
                self._dpop_key = None  # 需要显式传 bff_dpop

        # 极小首屏缓存（减少连续命中时的重复 IO）
        self._landing_cache: Dict[Tuple[str, int], Tuple[float, List[Dict[str, Any]]]] = {}

    # ───────── public API ─────────
    async def search(
        self,
        keyword: str,
        *,
        shop_id: str,
        cursor: str = "",
        in_stock: Optional[bool] = None,
        order_by: Optional[str] = None,
        local_keyword: Optional[str] = None,
    ) -> ShopSearchResults:
        req = ShopsSearchRequestData(
            keyword, shop_id, cursor,
            in_stock=in_stock, order_by=order_by
        )
        res = await self._post_graphql(req)

        # 本地二次过滤（保持旧版行为）
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
        page_size: int = 80,             # 更小首屏体积，加快首屏
        prefer_bff_first: bool = True,   # 优先直连 BFF，SSR 仅兜底
        cache_ttl_seconds: float = 3.0,  # 极小 TTL，抖动期显著降耗
    ) -> List[ShopProduct]:
        """
        拉取店铺总页“首屏最新商品”，并可选做本地关键词过滤。
        - keyword：在 displayName/name/title 上做 NFKC+lower 的子串匹配；多词空白分割 → AND。
        - 不回落 GraphQL。
        """
        products: List[Dict[str, Any]] = []

        # 0) 轻量缓存（仅按 shop_id + page_size 维度）
        cache_key = (shop_id, int(page_size))
        now = time.time()
        cached = self._landing_cache.get(cache_key)
        if cached and (now - cached[0] <= cache_ttl_seconds):
            products = cached[1]
            log.debug("landing cache hit: shop=%s size=%s", shop_id, page_size)

        # 1) BFF 优先（绝大多数情况下更快更稳）
        if prefer_bff_first and not products:
            try:
                products = await self._fetch_bff_products_firstpage(shop_id, page_size=page_size)
                if products:
                    self._landing_cache[cache_key] = (time.time(), products)
            except Exception as e:
                log.debug("landing BFF fetch error, will fallback to SSR: %s", repr(e))

        # 2) SSR 回退（HTML / _next/data）
        if not products:
            try:
                # 2.1 GET HTML
                r = await self._client.get(
                    f"{self.SHOP_WEB_ORIGIN}/shops/profile/{shop_id}",
                    headers={**self._headers, "Accept": "text/html,application/xhtml+xml"},
                    follow_redirects=True,
                    timeout=(5, 30),
                )
                r.raise_for_status()
                html = r.text

                # 2.2 解析 __NEXT_DATA__ / fallback buildId
                m = self._NEXT_DATA_RX.search(html)
                build_id = None
                if m:
                    try:
                        data = json.loads(m.group(1))
                        build_id = data.get("buildId")
                        products = self._collect_from_nextdata(data)
                    except Exception as e:
                        log.debug("landing: parse __NEXT_DATA__ error: %s", e)
                else:
                    m2 = self._BUILD_ID_RX.search(html)
                    if m2:
                        build_id = m2.group(1)

                # 2.3 若还没有，/_next/data/<buildId>/... 再试
                if not products and build_id:
                    for url in (
                        f"{self.SHOP_WEB_ORIGIN}/_next/data/{build_id}/shops/profile/{shop_id}.json",
                        f"{self.SHOP_WEB_ORIGIN}/_next/data/{build_id}/ja/shops/profile/{shop_id}.json",
                        f"{self.SHOP_WEB_ORIGIN}/_next/data/{build_id}/en/shops/profile/{shop_id}.json",
                    ):
                        j = await self._client.get(
                            url,
                            headers={**self._headers, "Accept": "application/json", "x-nextjs-data": "1"},
                            follow_redirects=True,
                            timeout=(5, 30),
                        )
                        if j.status_code == 200:
                            try:
                                jj = j.json()
                                products = self._collect_from_nextdata(jj)
                                if products:
                                    break
                            except Exception as e:
                                log.debug("landing: parse _next data error: %s", e)
            except Exception as e:
                log.debug("landing SSR error: %s", repr(e))

        # 3) 过滤在库/卖空（若 inStock 缺失则不剔除）
        if in_stock is not None and products:
            tmp = []
            for p in products:
                v = p.get("inStock")
                if isinstance(v, bool):
                    if v is in_stock:
                        tmp.append(p)
                else:
                    tmp.append(p)
            products = tmp

        # 4) 关键词本地过滤（优先 displayName；BFF 的 name 多为 'products/<id>'）
        if keyword and products:
            tokens = self._normalize_tokens(keyword)
            if tokens:
                tmp: List[Dict[str, Any]] = []
                for p in products:
                    name = (
                        p.get("displayName") or p.get("name") or p.get("title") or
                        p.get("productName") or p.get("itemName") or ""
                    ).strip()
                    if self._match_tokens(name, tokens):
                        tmp.append(p)
                products = tmp

        # 5) 映射 → ShopProduct（name 优先 displayName；id 兼容 'products/<id>'）
        out: List[ShopProduct] = []
        seen = set()
        for p in products or []:
            pid = p.get("id")
            if not pid:
                n = p.get("name")
                if isinstance(n, str) and n.startswith("products/"):
                    pid = n.split("/", 1)[-1]
            if not pid:
                pid = self._fingerprint(
                    (p.get("displayName") or p.get("name") or p.get("title") or "").strip(),
                    self._first_image_url(p)
                )
            if pid in seen:
                continue
            seen.add(pid)

            name  = (
                p.get("displayName") or p.get("name") or p.get("title") or
                p.get("productName") or p.get("itemName") or ""
            ).strip()

            price = p.get("price")
            if isinstance(price, dict):
                price = price.get("value") or price.get("amount") or 0
            try:
                price = int(price or 0)
            except Exception:
                price = 0

            instock = p.get("inStock")
            instock = bool(instock) if isinstance(instock, bool) else True

            img = self._first_image_url(p)
            assets = [ShopProductAsset(imageUrl=img)] if img else []
            out.append(ShopProduct(id=str(pid), name=name, price=price, inStock=instock, assets=assets))

        return out[:limit]

    # ───────── internal: GraphQL（search 用） ─────────
    async def _post_graphql(self, req: ShopsSearchRequestData) -> ShopSearchResults:
        payload = {
            "query":          self.SEARCH_QUERY,
            "operationName":  "SearchTop",
            "variables":      req.data,
        }
        r = await self._client.post(
            self.GRAPHQL_URL,
            json=payload,
            headers=self._headers,
            timeout=(5, 60),
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = r.text[:500]
            log.error("GraphQL HTTP error: %s, body=%s", e, body)
            raise

        j = r.json()
        if "errors" in j:
            raise RuntimeError(j["errors"])
        res = map_to_class(j["data"]["products"], ShopSearchResults)

        # 保持分页可用
        res._request = req
        res._request._api = self

        # 兜底过滤/排序（createdAt 未在查询中显式请求；CREATED_AT 交给后端）
        items = list(res.items) if res.items else []

        if req.in_stock is True:
            items = [i for i in items if getattr(i, "inStock", None) is True]
        elif req.in_stock is False:
            items = [i for i in items if getattr(i, "inStock", None) is False]

        if req.order_by == "PRICE_ASC":
            items.sort(key=lambda x: (getattr(x, "price", None) is None, getattr(x, "price", None)))
        elif req.order_by == "PRICE_DESC":
            items.sort(key=lambda x: (getattr(x, "price", None) is None, getattr(x, "price", None)), reverse=True)

        res.items = items
        return res

    # ───────── SSR 辅助：只在 Next 的数据槽里找候选商品列表 ─────────
    def _collect_from_nextdata(self, root: Any) -> List[Dict[str, Any]]:
        if not isinstance(root, dict):
            return []
        page = root.get("pageProps") if isinstance(root.get("pageProps"), dict) else root

        buckets: List[List[Dict[str, Any]]] = []

        ds = None
        if isinstance(page.get("dehydratedState"), dict):
            ds = page["dehydratedState"]
        elif isinstance(page.get("reactQueryState"), dict):
            ds = page["reactQueryState"]

        if ds and isinstance(ds.get("queries"), list):
            for q in ds["queries"]:
                state = q.get("state") or {}
                data  = state.get("data")
                if data:
                    buckets.extend(self._candidate_lists_in(data))

        fb = page.get("fallback")
        if isinstance(fb, dict):
            for v in fb.values():
                buckets.extend(self._candidate_lists_in(v))

        buckets.extend(self._candidate_lists_in(page))

        out: List[Dict[str, Any]] = []
        seen = set()
        for lst in buckets:
            for it in lst:
                if not isinstance(it, dict):
                    continue
                # 必须具备“像商品”的强信号，避免把翻译块当商品
                if not self._looks_like_product_relaxed(it):
                    continue
                pid = (it.get("id") or it.get("productId") or it.get("itemId") or
                       it.get("uuid") or it.get("hashId"))
                name = (it.get("displayName") or it.get("name") or it.get("title") or
                        it.get("productName") or it.get("itemName"))
                if not name:
                    continue
                spid = str(pid) if pid else self._fingerprint(name, self._first_image_url(it))
                if spid in seen:
                    continue
                seen.add(spid)
                out.append(it)
        return out

    def _candidate_lists_in(self, obj: Any) -> List[List[Dict[str, Any]]]:
        cands: List[List[Dict[str, Any]]] = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, list) and v:
                    # 只收看起来像“商品列表”的容器键
                    kl = str(k).lower()
                    if any(tok in kl for tok in ("items", "products", "goods", "list", "edges")):
                        row = []
                        for x in v:
                            if isinstance(x, dict) and "node" in x and isinstance(x["node"], dict):
                                row.append(x["node"])
                            elif isinstance(x, dict):
                                row.append(x)
                        if row:
                            cands.append(row)
                elif isinstance(v, dict):
                    sub = self._candidate_lists_in(v)
                    if sub:
                        cands.extend(sub)
        return cands

    @staticmethod
    def _has_price_or_image_or_stock(p: Dict[str, Any]) -> bool:
        # 价格：必须是数值或可解析为数值的 dict/str
        if "price" in p:
            v = p.get("price")
            if isinstance(v, (int, float)):
                return True
            if isinstance(v, dict):
                if any(isinstance(v.get(k), (int, float)) for k in ("value", "amount", "price", "taxIncluded", "raw")):
                    return True
            if isinstance(v, str) and any(ch.isdigit() for ch in v):
                return True
        # 库存：布尔或明显的状态字段
        for k in ("inStock", "soldOut", "isSoldOut", "available", "isAvailable", "status"):
            if k in p:
                return True
        # 图片：仅接受明确缩略图字段，避免把任意“image/images”误判为商品
        if any(k in p for k in ("imageUrl", "thumbnailUrl", "firstImageUrl", "mainImageUrl",
                                "primaryImageUrl", "coverImageUrl")):
            return True
        thumbs = p.get("thumbnails") or p.get("photos") or p.get("images")
        if isinstance(thumbs, list) and any(isinstance(t, dict) and any(x in t for x in ("uri", "url", "src", "path", "imageUrl")) for t in thumbs):
            return True
        return False

    @classmethod
    def _looks_like_product_relaxed(cls, d: Dict[str, Any]) -> bool:
        if not isinstance(d, dict):
            return False
        has_name = any(
            isinstance(d.get(k), str) and d.get(k).strip()
            for k in ("displayName", "name", "title", "productName", "itemName")
        )
        if not has_name:
            return False
        return MercapiShops._has_price_or_image_or_stock(d)

    @classmethod
    def _extract_products_relaxed(cls, obj) -> List[Dict[str, Any]]:
        # 备用：一般不走这里（防止误采翻译块）
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

    # ───────── internal: 直连 BFF 首屏（DPoP） ─────────
    async def _fetch_bff_products_firstpage(self, shop_id: str, *, page_size: int = 100) -> List[Dict[str, Any]]:
        """
        POST /services/bff/shops/v1/contents/shops/{shopId}/products
        - 默认 origin: https://api.mercari.jp （也可通过 bff_origin 覆盖）
        - 需要 DPoP；若未传 bff_dpop，则用 ES256(P-256) 现签
        """
        origins = [self._bff_origin] if self._bff_origin else []
        origins += ["https://api.mercari.jp"]  # 只保留最稳定的域名

        base_headers = {
            "User-Agent": self._headers.get("User-Agent", "Mozilla/5.0"),
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Language": self._headers.get("Accept-Language", "ja"),
            "Origin": self.SHOP_WEB_ORIGIN,
            "Referer": f"{self.SHOP_WEB_ORIGIN}/shops/profile/{shop_id}",
            "X-Platform": "web",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
        }
        if self._bff_auth_bearer:
            base_headers["Authorization"] = self._bff_auth_bearer

        payload = {
            "name": f"shops/{shop_id}",
            "searchOption": {"inStock": False, "inDualPrice": False, "orderBy": "created_at desc"},
            "listOption": {"pageSize": int(page_size), "pageToken": ""},
        }

        timeout = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=5.0)

        last_err = None
        for origin in origins:
            if not origin:
                continue
            base = f"{origin}/services/bff/shops/v1/contents/shops/{shop_id}"
            url_prod = base + "/products"

            # 每个 URL 最多重试 1 次（短抖动时更快恢复）
            for attempt in range(1):
                try:
                    headers = dict(base_headers)
                    # DPoP：每次尝试都签一个新的（iat/jti 新鲜）
                    if self._bff_dpop_override:
                        headers["DPoP"] = self._bff_dpop_override
                    else:
                        if not self._dpop_key:
                            raise RuntimeError("需要 DPoP，但 cryptography 不可用且未传 bff_dpop。")
                        headers["DPoP"] = self._dpop_key.make_dpop("POST", url_prod)

                    pr = await self._client.post(url_prod, json=payload, headers=headers, timeout=timeout)
                    # 直接成功
                    if pr.status_code == 200:
                        j = pr.json()
                        prods = []
                        sp = j.get("shopProducts") if isinstance(j, dict) else None
                        if isinstance(sp, dict) and isinstance(sp.get("products"), list):
                            prods = [it for it in sp["products"] if isinstance(it, dict)]
                        if not prods:
                            prods = self._extract_products_relaxed(j)
                        if prods:
                            return prods
                        return []  # 200 但空，直接返回

                    # 404：路径无货，换域
                    if pr.status_code == 404:
                        break

                    # 401/403：轻微等待后重签一次
                    if pr.status_code in (401, 403):
                        await asyncio.sleep(0.4 + random.random() * 0.3)
                        continue

                    # 可重试 5xx / CF 错误：短回退
                    if pr.status_code in _RETRIABLE_STATUS:
                        backoff = 0.5 + random.random() * 0.3
                        await asyncio.sleep(backoff)
                        continue

                    # 其他状态码：没必要继续
                    break

                except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException) as e:
                    last_err = e
                    backoff = 0.5 + random.random() * 0.3
                    await asyncio.sleep(backoff)
                    continue
                except httpx.ConnectError as e:
                    last_err = e
                    log.debug("BFF connect error on %s: %r", base, e)
                    break
                except Exception as e:
                    last_err = e
                    log.debug("BFF error on %s: %r", base, e)
                    break

        if last_err:
            raise last_err
        return []

    # ───────── utils ─────────
    @staticmethod
    def _normalize(s: str) -> str:
        return unicodedata.normalize("NFKC", s).lower()

    def _normalize_tokens(self, keyword: str) -> List[str]:
        return [self._normalize(t) for t in keyword.split() if t.strip()]

    def _match_tokens(self, text: str, tokens: List[str]) -> bool:
        nt = self._normalize(text or "")
        return all(t in nt for t in tokens)

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
                        return MercapiShops.SHOP_WEB_ORIGIN + raw
                    return raw
                return MercapiShops.SHOP_WEB_ORIGIN + u
            if u.startswith("//"):
                return "https:" + u
            if u.startswith("/"):
                return MercapiShops.SHOP_WEB_ORIGIN + u
            return u
        except Exception:
            return u

    @staticmethod
    def _first_image_url(obj: Dict[str, Any]) -> str:
        # 1) assets：list 或 {"edges":[{"node": {...}}]}
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
            for k in ("imageUrl", "url", "src", "mainImageUrl", "primaryImageUrl",
                      "coverImageUrl", "thumbnailUrl", "path", "uri"):
                v = a.get(k)
                if isinstance(v, str) and v:
                    return MercapiShops._normalize_img_url(v)
            img = a.get("image")
            if isinstance(img, dict):
                u = img.get("url") or img.get("src") or img.get("path") or img.get("uri")
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

        # 2) 商品对象自身字段（含 thumbnails/photos）
        for k in ("imageUrl", "thumbnailUrl", "firstImageUrl", "mainImageUrl",
                  "primaryImageUrl", "coverImageUrl", "image"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return MercapiShops._normalize_img_url(v)
            if isinstance(v, dict):
                u = v.get("url") or v.get("src") or v.get("path") or v.get("uri")
                if isinstance(u, str) and u:
                    return MercapiShops._normalize_img_url(u)

        thumbs = obj.get("thumbnails") or obj.get("images") or obj.get("photos")
        if isinstance(thumbs, list):
            for it in thumbs:
                if isinstance(it, dict):
                    for k in ("uri", "url", "src", "path", "imageUrl"):
                        u = it.get(k)
                        if isinstance(u, str) and u:
                            return MercapiShops._normalize_img_url(u)

        # 3) 兜底：深度扫描拿第一个像图片的 URL
        def looks_like_image(u: str) -> bool:
            if not isinstance(u, str) or not u:
                return False
            s = u.lower()
            return (
                s.startswith("http") and (".jpg" in s or ".jpeg" in s or ".png" in s or ".webp" in s)
            ) or ("image.mercari" in s or "mercdn" in s or ".jpg" in s or ".png" in s or ".webp" in s)

        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k in ("imageUrl", "thumbnailUrl", "url", "src", "path", "uri"):
                    v = cur.get(k)
                    if isinstance(v, str) and looks_like_image(v):
                        return MercapiShops._normalize_img_url(v)
                    if isinstance(v, (dict, list)):
                        stack.append(v)
                for v in cur.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
                    elif isinstance(v, str) and looks_like_image(v):
                        return MercapiShops._normalize_img_url(v)
            elif isinstance(cur, list):
                stack.extend(cur)
        return ""

    @staticmethod
    def _fingerprint(name: str, img: str) -> str:
        base = (name or "").strip() + "|" + (img or "").strip()
        if not base.strip():
            base = "shops-item"
        return hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:16]

    async def __aenter__(self):  # optional context-manager
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._client.aclose()
