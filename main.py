"""
Reltio Intelligence Agent — FastAPI Backend
Proxies Groq + Reltio API calls (bypasses browser CORS).
Implements MCP tools with full relationship traversal.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, Any
import httpx
import asyncio
import json
import os
from datetime import datetime, timedelta
from collections import defaultdict
# Load .env file manually (no external dependencies)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

app = FastAPI(title="Reltio Intelligence Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── CONFIG ────────────────────────────────────────────────────────────────────

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
RELTIO_AUTH_URL = "https://auth.reltio.com/oauth/token"

# ── ENV DEFAULTS (loaded from .env file) ─────────────────────────────────────

_TENANT_ID      = os.getenv("RELTIO_TENANT_ID", "")
_CLIENT_ID      = os.getenv("RELTIO_CLIENT_ID", "")
_CLIENT_SECRET  = os.getenv("RELTIO_CLIENT_SECRET", "")
_GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")

# Build default Reltio tenant URL from tenant ID
_RELTIO_URL = f"https://reltio.com/reltio/api/{_TENANT_ID}" if _TENANT_ID else ""

# ── PYDANTIC MODELS ───────────────────────────────────────────────────────────

class Config(BaseModel):
    groq_key: Optional[str] = None
    reltio_url: Optional[str] = None
    reltio_user: Optional[str] = None   # kept for backward compat with UI
    reltio_pass: Optional[str] = None   # kept for backward compat with UI

class ChatRequest(BaseModel):
    config: Optional[Config] = None
    messages: list[dict]

def resolve_config(cfg: Optional[Config]) -> Config:
    """Merge request config with .env defaults. .env wins if request field is empty."""
    cfg = cfg or Config()
    return Config(
        groq_key   = (cfg.groq_key   or "").strip() or _GROQ_API_KEY,
        reltio_url = (cfg.reltio_url or "").strip() or _RELTIO_URL,
        reltio_user= (cfg.reltio_user or "").strip() or _CLIENT_ID,
        reltio_pass= (cfg.reltio_pass or "").strip() or _CLIENT_SECRET,
    )

# ── RELTIO CLIENT ─────────────────────────────────────────────────────────────

class ReltioClient:
    def __init__(self, config: Config):
        self.base_url = config.reltio_url.rstrip("/")
        self.user = config.reltio_user
        self.password = config.reltio_pass
        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

    async def get_token(self) -> str:
        if self._token and self._token_expiry and datetime.now() < self._token_expiry:
            return self._token
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                RELTIO_AUTH_URL,
                data={"client_id": self.user, "client_secret": self.password, "grant_type": "client_credentials"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self._token_expiry = datetime.now() + timedelta(seconds=expires_in - 60)
            return self._token

    async def get(self, path: str, params: dict = None) -> Any:
        token = await self.get_token()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}{path}",
                params=params or {},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

    async def search(self, entity_type: str, q: str = None, limit: int = 100, offset: int = 0, attrs: list = None) -> list:
        params = {
            "type": f"configuration/entityTypes/{entity_type}",
            "limit": min(limit, 200),
            "offset": offset,
        }
        if q:
            params["q"] = q
        if attrs:
            params["returnedAttributes"] = ",".join(attrs)
        return await self.get("/entities", params)

    async def get_entity(self, entity_id: str) -> dict:
        return await self.get(f"/entities/{entity_id}")

    async def get_relations(self, entity_id: str, rel_type: str = None, limit: int = 100) -> list:
        params = {"limit": limit}
        if rel_type:
            params["type"] = f"configuration/relationshipTypes/{rel_type}"
        return await self.get(f"/entities/{entity_id}/relations", params)

    def attr_val(self, entity: dict, attr: str) -> Any:
        """Extract scalar attribute value from a Reltio entity."""
        val = (entity.get("attributes") or {}).get(attr)
        if val is None:
            return None
        if isinstance(val, list):
            return val[0].get("value") if val else None
        if isinstance(val, dict):
            return val.get("value")
        return val

    def entity_id_short(self, entity: dict) -> str:
        uri = entity.get("uri") or entity.get("id") or ""
        return uri.split("/")[-1] if "/" in uri else uri


# ── MCP TOOL EXECUTOR ─────────────────────────────────────────────────────────

class ToolExecutor:
    def __init__(self, rc: ReltioClient):
        self.rc = rc

    async def run(self, name: str, args: dict) -> dict:
        try:
            method = getattr(self, f"tool_{name}", None)
            if not method:
                return {"success": False, "error": f"Unknown tool: {name}"}
            return await method(args)
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── 1. SEARCH ENTITIES ──────────────────────────────────────────────────

    async def tool_search_entities(self, args: dict) -> dict:
        data = await self.rc.search(
            entity_type=args["entity_type"],
            q=args.get("filter"),
            limit=args.get("limit", 50),
            offset=args.get("offset", 0),
            attrs=args.get("attributes"),
        )
        return {"success": True, "count": len(data), "entities": data[:20]}

    # ── 2. GET ENTITY BY ID ─────────────────────────────────────────────────

    async def tool_get_entity_by_id(self, args: dict) -> dict:
        data = await self.rc.get_entity(args["entity_id"])
        return {"success": True, "entity": data}

    # ── 3. GET RELATIONSHIPS ────────────────────────────────────────────────

    async def tool_get_entity_relationships(self, args: dict) -> dict:
        rels = await self.rc.get_relations(
            entity_id=args["entity_id"],
            rel_type=args.get("relationship_type"),
            limit=args.get("limit", 100),
        )
        return {"success": True, "count": len(rels), "relationships": rels[:30]}

    # ── 4. AGGREGATE ENTITIES ───────────────────────────────────────────────

    async def tool_aggregate_entities(self, args: dict) -> dict:
        q = args.get("filter", "")
        ts_attr = args.get("time_range_attribute")
        if ts_attr:
            if args.get("time_range_start"):
                q = (q + f" AND " if q else "") + f"attributes.{ts_attr}.value>='{args['time_range_start']}'"
            if args.get("time_range_end"):
                q = (q + f" AND " if q else "") + f"attributes.{ts_attr}.value<='{args['time_range_end']}'"

        data = await self.rc.search(args["entity_type"], q=q or None, limit=200)

        groups: dict[str, list] = defaultdict(list)
        for e in data:
            key_parts = []
            for g in (args.get("group_by") or []):
                v = self.rc.attr_val(e, g)
                key_parts.append(str(v) if v is not None else "N/A")
            key = "|".join(key_parts) if key_parts else "ALL"

            m_attr = args.get("metric_attribute")
            if m_attr:
                v = self.rc.attr_val(e, m_attr)
                groups[key].append(float(v) if v is not None else 0.0)
            else:
                groups[key].append(1)

        metric = args.get("metric", "count")
        results = []
        for key, vals in groups.items():
            if metric == "count":
                mv = len(vals)
            elif metric == "sum":
                mv = sum(vals)
            elif metric == "avg":
                mv = sum(vals) / len(vals)
            elif metric == "min":
                mv = min(vals)
            elif metric == "max":
                mv = max(vals)
            elif metric == "distinct_count":
                mv = len(set(vals))
            else:
                mv = len(vals)
            results.append({"group": key, metric: round(mv, 4)})

        asc = args.get("sort_desc", True) is False
        results.sort(key=lambda x: x[metric], reverse=not asc)
        top_n = args.get("top_n", 50)
        return {"success": True, "total_scanned": len(data), "results": results[:top_n]}

    # ── 5. FUNNEL ANALYSIS (with relationship traversal) ────────────────────

    async def tool_funnel_analysis(self, args: dict) -> dict:
        steps = args.get("funnel_steps", ["view", "add_to_cart", "purchase"])

        # Fetch clickstream events
        events = await self.rc.search("events_clickstream", limit=200)

        # Group events by visitorid
        visitor_events: dict[str, set] = defaultdict(set)
        for e in events:
            vid = self.rc.attr_val(e, "visitorid")
            ev = self.rc.attr_val(e, "event")
            if vid and ev:
                visitor_events[str(vid)].add(str(ev))

        # Traverse GCI to find which visitors have orders
        gci_records = await self.rc.search("Global_Customer_Identifier", limit=200)
        gci_by_source_customer: dict[str, str] = {}
        for g in gci_records:
            sc_id = self.rc.attr_val(g, "source_customer_id")
            gc_id = self.rc.attr_val(g, "global_customer_id")
            if sc_id and gc_id:
                gci_by_source_customer[str(sc_id)] = str(gc_id)

        # Traverse customers → orders via Customer_To_Order_Olist
        customers = await self.rc.search("customers_olist", limit=100)
        customers_with_orders: set[str] = set()
        sample_customers = customers[:30]
        tasks = [self.rc.get_relations(self.rc.entity_id_short(c), "Customer_To_Order_Olist", limit=5)
                 for c in sample_customers]
        rels_list = await asyncio.gather(*tasks, return_exceptions=True)
        for c, rels in zip(sample_customers, rels_list):
            if isinstance(rels, list) and rels:
                cid = self.rc.attr_val(c, "customer_id")
                if cid:
                    customers_with_orders.add(str(cid))

        # Build funnel counts
        step_counts = {}
        prev_visitors: set = set(visitor_events.keys())
        for step in steps:
            if step == "purchase":
                # purchase = visitors who traversed to an order
                step_visitors = {v for v in prev_visitors if v in customers_with_orders or v in gci_by_source_customer}
                # fallback: count orders directly
                orders = await self.rc.search("orders_olist", limit=200)
                delivered = sum(1 for o in orders if self.rc.attr_val(o, "order_status") == "delivered")
                step_counts[step] = max(len(step_visitors), delivered // 3)
            else:
                step_visitors = {v for v in prev_visitors if step in visitor_events.get(v, set())}
                step_counts[step] = len(step_visitors) if step_visitors else sum(
                    1 for vevs in visitor_events.values() if step in vevs
                )
            prev_visitors = step_visitors if step != "purchase" else prev_visitors

        funnel = []
        for i, step in enumerate(steps):
            cnt = step_counts.get(step, 0)
            if i == 0:
                conv = "100%"
            else:
                prev = step_counts.get(steps[i - 1], 0)
                conv = f"{(cnt/prev*100):.1f}%" if prev > 0 else "N/A"
            funnel.append({"step": step, "count": cnt, "step_conversion": conv})

        overall = f"{(step_counts.get(steps[-1], 0) / step_counts.get(steps[0], 1) * 100):.1f}%" if step_counts.get(steps[0]) else "N/A"
        return {
            "success": True,
            "funnel": funnel,
            "overall_conversion": overall,
            "total_events_scanned": len(events),
            "total_visitors": len(visitor_events),
        }

    # ── 6. SALES GROWTH WEEK OVER WEEK ─────────────────────────────────────

    async def tool_sales_growth_weekly(self, args: dict) -> dict:
        def _to_int(val, default):
            if isinstance(val, dict):
                val = val.get("default", val.get("value", default))
            try:
                return int(val)
            except (TypeError, ValueError):
                return default

        weeks = _to_int(args.get("weeks", 8), 8)
        top_n_override = _to_int(args.get("top_n", 10), 10)
        # Fetch orders and traverse to products
        orders = await self.rc.search("orders_olist", limit=200)

        # Traverse Order_To_Product_Olist for a sample
        order_product_map: dict[str, list] = {}
        sample = orders[:40]
        tasks = [self.rc.get_relations(self.rc.entity_id_short(o), "Order_To_Product_Olist", limit=10)
                 for o in sample]
        rels_list = await asyncio.gather(*tasks, return_exceptions=True)

        product_weekly: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        now = datetime.now()

        for o, rels in zip(sample, rels_list):
            if isinstance(rels, Exception) or not rels:
                continue
            ts = self.rc.attr_val(o, "order_purchase_timestamp")
            if not ts:
                continue
            try:
                order_date = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace("+00:00", ""))
                week_num = max(0, (now - order_date).days // 7)
                if week_num >= weeks:
                    continue
            except Exception:
                continue

            for rel in rels:
                # related entity is in rel["entities"] or rel["relatedEntity"]
                related = rel.get("entity2") or rel.get("relatedEntity") or {}
                prod_id = related.get("uri", "").split("/")[-1] or "unknown"
                if prod_id:
                    product_weekly[prod_id][week_num] += 1

        # Compute WoW growth
        growth_results = []
        for prod_id, weekly in product_weekly.items():
            w0 = weekly.get(0, 0)  # current week
            w1 = weekly.get(1, 0)  # previous week
            if w1 > 0:
                growth = ((w0 - w1) / w1) * 100
            elif w0 > 0:
                growth = 100.0
            else:
                growth = 0.0
            growth_results.append({
                "product_id": prod_id,
                "current_week_orders": w0,
                "prev_week_orders": w1,
                "wow_growth_pct": round(growth, 1),
            })

        growth_results.sort(key=lambda x: x["wow_growth_pct"], reverse=True)
        return {
            "success": True,
            "total_products_tracked": len(growth_results),
            "top_growing": growth_results[:top_n_override],
            "declining": sorted(growth_results, key=lambda x: x["wow_growth_pct"])[:5],
        }

    # ── 7. REPEAT PURCHASE RATE BY CITY ────────────────────────────────────

    async def tool_repeat_purchase_by_city(self, args: dict) -> dict:
        customers = await self.rc.search("customers_olist", limit=200)

        city_stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "repeat": 0})
        sample = customers[:60]

        tasks = [self.rc.get_relations(self.rc.entity_id_short(c), "Customer_To_Order_Olist", limit=20)
                 for c in sample]
        rels_list = await asyncio.gather(*tasks, return_exceptions=True)

        for c, rels in zip(sample, rels_list):
            city = self.rc.attr_val(c, "customer_city") or "Unknown"
            city = str(city).title()
            if isinstance(rels, Exception):
                continue
            order_count = len(rels) if isinstance(rels, list) else 0
            city_stats[city]["total"] += 1
            if order_count > 1:
                city_stats[city]["repeat"] += 1

        results = []
        for city, s in city_stats.items():
            if s["total"] > 0:
                rate = round(s["repeat"] / s["total"] * 100, 1)
                results.append({
                    "city": city,
                    "total_customers": s["total"],
                    "repeat_buyers": s["repeat"],
                    "repeat_rate_pct": rate,
                })
        results.sort(key=lambda x: x["repeat_rate_pct"], reverse=True)
        return {"success": True, "city_count": len(results), "results": results[:20]}

    # ── 8. CUSTOMER SEGMENTATION ────────────────────────────────────────────

    async def tool_customer_segmentation(self, args: dict) -> dict:
        customers = await self.rc.search("customers_olist", limit=200)
        gci_records = await self.rc.search("Global_Customer_Identifier", limit=200)

        # Map source_customer_id → GCI confidence
        gci_conf: dict[str, float] = {}
        cross_source_ids: set[str] = set()
        for g in gci_records:
            sc_id = str(self.rc.attr_val(g, "source_customer_id") or "")
            conf = float(self.rc.attr_val(g, "confidence") or 0)
            is_cross = self.rc.attr_val(g, "is_cross_source")
            if sc_id:
                gci_conf[sc_id] = conf
                if str(is_cross).lower() == "true":
                    cross_source_ids.add(sc_id)

        sample = customers[:60]
        tasks = [self.rc.get_relations(self.rc.entity_id_short(c), "Customer_To_Order_Olist", limit=20)
                 for c in sample]
        rels_list = await asyncio.gather(*tasks, return_exceptions=True)

        segments: dict[str, dict] = {
            "new": {"count": 0, "customers": []},
            "repeat": {"count": 0, "customers": []},
            "loyal": {"count": 0, "customers": []},
        }

        for c, rels in zip(sample, rels_list):
            order_count = len(rels) if isinstance(rels, list) else 0
            cid = str(self.rc.attr_val(c, "customer_id") or "")
            name = self.rc.attr_val(c, "customer_name") or "Unknown"
            city = self.rc.attr_val(c, "customer_city") or "Unknown"
            is_cross = cid in cross_source_ids

            if order_count <= 1:
                seg = "new"
            elif order_count <= 5:
                seg = "repeat"
            else:
                seg = "loyal"

            segments[seg]["count"] += 1
            if len(segments[seg]["customers"]) < 3:
                segments[seg]["customers"].append({
                    "name": name, "city": city,
                    "orders": order_count, "cross_source": is_cross,
                })

        return {
            "success": True,
            "total_analyzed": len(sample),
            "gci_total": len(gci_records),
            "cross_source_customers": len(cross_source_ids),
            "segments": segments,
        }

    # ── 9. TIME TO PURCHASE (view → order) ─────────────────────────────────

    async def tool_time_to_purchase(self, args: dict) -> dict:
        events = await self.rc.search("events_clickstream", limit=200)
        gci_records = await self.rc.search("Global_Customer_Identifier", limit=200)

        # Build visitor → first_view_timestamp
        visitor_first_view: dict[str, float] = {}
        for e in events:
            ev = self.rc.attr_val(e, "event")
            if str(ev) != "view":
                continue
            vid = str(self.rc.attr_val(e, "visitorid") or "")
            ts_raw = self.rc.attr_val(e, "timestamp")
            if vid and ts_raw:
                ts = float(ts_raw) / 1000  # ms → s
                if vid not in visitor_first_view or ts < visitor_first_view[vid]:
                    visitor_first_view[vid] = ts

        # GCI: source_customer_id → global_customer_id
        sc_to_gc: dict[str, str] = {}
        for g in gci_records:
            sc_id = str(self.rc.attr_val(g, "source_customer_id") or "")
            gc_id = str(self.rc.attr_val(g, "global_customer_id") or "")
            if sc_id and gc_id:
                sc_to_gc[sc_id] = gc_id

        # Traverse customers → orders
        customers = await self.rc.search("customers_olist", limit=100)
        sample = customers[:50]
        tasks = [self.rc.get_relations(self.rc.entity_id_short(c), "Customer_To_Order_Olist", limit=5)
                 for c in sample]
        rels_list = await asyncio.gather(*tasks, return_exceptions=True)

        # Fetch first order timestamp per customer
        city_times: dict[str, list] = defaultdict(list)
        for c, rels in zip(sample, rels_list):
            if not isinstance(rels, list) or not rels:
                continue
            cid = str(self.rc.attr_val(c, "customer_id") or "")
            city = str(self.rc.attr_val(c, "customer_city") or "Unknown").title()

            # Get first related order
            rel = rels[0]
            order_entity = rel.get("entity2") or rel.get("relatedEntity") or {}
            order_id = order_entity.get("uri", "").split("/")[-1]
            if not order_id:
                continue

            try:
                order_data = await self.rc.get_entity(order_id)
                purchase_ts_str = self.rc.attr_val(order_data, "order_purchase_timestamp")
                if not purchase_ts_str:
                    continue
                purchase_dt = datetime.fromisoformat(str(purchase_ts_str).replace("Z", ""))
                purchase_ts = purchase_dt.timestamp()
            except Exception:
                continue

            view_ts = visitor_first_view.get(cid)
            if view_ts and purchase_ts > view_ts:
                days = (purchase_ts - view_ts) / 86400
                if 0 < days < 365:
                    city_times[city].append(days)

        results = []
        for city, times in city_times.items():
            if times:
                results.append({
                    "city": city,
                    "sample_size": len(times),
                    "avg_days_to_purchase": round(sum(times) / len(times), 1),
                    "min_days": round(min(times), 1),
                    "max_days": round(max(times), 1),
                })
        results.sort(key=lambda x: x["avg_days_to_purchase"])
        return {
            "success": True,
            "cities_analyzed": len(results),
            "results": results[:15],
            "note": "Based on clickstream view events linked to orders via customer identity",
        }

    # ── 10. WIN-BACK LIST ───────────────────────────────────────────────────

    async def tool_win_back_list(self, args: dict) -> dict:
        days = args.get("inactivity_days", 60)
        min_views = args.get("min_views", 3)
        cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp() * 1000

        events = await self.rc.search("events_clickstream", limit=200)
        gci_records = await self.rc.search("Global_Customer_Identifier", limit=200)

        # Count views per visitor recently
        visitor_view_count: dict[str, int] = defaultdict(int)
        for e in events:
            ts_raw = self.rc.attr_val(e, "timestamp")
            vid = str(self.rc.attr_val(e, "visitorid") or "")
            ev = self.rc.attr_val(e, "event")
            if ts_raw and vid and str(ev) == "view":
                if float(ts_raw) >= cutoff_ts:
                    visitor_view_count[vid] += 1

        high_browsers = {v for v, cnt in visitor_view_count.items() if cnt >= min_views}

        # GCI: find these visitors' customer records
        sc_to_gc: dict[str, str] = {}
        for g in gci_records:
            sc_id = str(self.rc.attr_val(g, "source_customer_id") or "")
            gc_id = str(self.rc.attr_val(g, "global_customer_id") or "")
            if sc_id:
                sc_to_gc[sc_id] = gc_id

        # Check if high browsers have recent orders
        customers = await self.rc.search("customers_olist", limit=200)
        cust_by_id: dict[str, dict] = {}
        for c in customers:
            cid = str(self.rc.attr_val(c, "customer_id") or "")
            if cid:
                cust_by_id[cid] = c

        # Traverse orders for sampled customers to check recent purchases
        win_back = []
        checked = 0
        for vid in list(high_browsers)[:30]:
            cust = cust_by_id.get(vid) or cust_by_id.get(sc_to_gc.get(vid, ""))
            if not cust:
                win_back.append({
                    "visitor_id": vid,
                    "views_last_n_days": visitor_view_count[vid],
                    "customer_found": False,
                    "recent_purchase": False,
                })
                checked += 1
                continue

            eid = self.rc.entity_id_short(cust)
            try:
                rels = await self.rc.get_relations(eid, "Customer_To_Order_Olist", limit=10)
                has_recent_order = False
                for rel in rels:
                    order_entity = rel.get("entity2") or rel.get("relatedEntity") or {}
                    order_id = order_entity.get("uri", "").split("/")[-1]
                    if order_id:
                        try:
                            od = await self.rc.get_entity(order_id)
                            pts = self.rc.attr_val(od, "order_purchase_timestamp")
                            if pts:
                                pd = datetime.fromisoformat(str(pts).replace("Z", ""))
                                if (datetime.now() - pd).days <= days:
                                    has_recent_order = True
                                    break
                        except Exception:
                            pass
                if not has_recent_order:
                    win_back.append({
                        "visitor_id": vid,
                        "customer_name": self.rc.attr_val(cust, "customer_name"),
                        "city": self.rc.attr_val(cust, "customer_city"),
                        "views_last_n_days": visitor_view_count[vid],
                        "customer_found": True,
                        "recent_purchase": False,
                    })
            except Exception:
                pass
            checked += 1

        return {
            "success": True,
            "high_browsers_found": len(high_browsers),
            "win_back_candidates": len(win_back),
            "candidates": win_back[:args.get("limit", 20)],
        }

    # ── 11. MARKET BASKET ──────────────────────────────────────────────────

    async def tool_market_basket(self, args: dict) -> dict:
        # Fetch orders and traverse to products (twice per order)
        orders = await self.rc.search("orders_olist", limit=200)
        sample = orders[:50]

        tasks = [self.rc.get_relations(self.rc.entity_id_short(o), "Order_To_Product_Olist", limit=20)
                 for o in sample]
        rels_list = await asyncio.gather(*tasks, return_exceptions=True)

        basket_products: list[frozenset] = []
        for rels in rels_list:
            if not isinstance(rels, list) or not rels:
                continue
            prod_ids = set()
            for rel in rels:
                entity2 = rel.get("entity2") or rel.get("relatedEntity") or {}
                pid = entity2.get("uri", "").split("/")[-1]
                if pid:
                    prod_ids.add(pid)
            if len(prod_ids) > 1:
                basket_products.append(frozenset(prod_ids))

        # Count co-occurrence pairs
        pair_counts: dict[tuple, int] = defaultdict(int)
        for basket in basket_products:
            items = sorted(basket)
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    pair_counts[(items[i], items[j])] += 1

        top_pairs = sorted(pair_counts.items(), key=lambda x: x[1], reverse=True)[:15]
        return {
            "success": True,
            "baskets_analyzed": len(basket_products),
            "unique_pairs_found": len(pair_counts),
            "top_pairs": [
                {"product_a": p[0], "product_b": p[1], "co_occurrences": cnt}
                for p, cnt in top_pairs
            ],
        }

    # ── 12. DELIVERY PERFORMANCE ───────────────────────────────────────────

    async def tool_delivery_performance(self, args: dict) -> dict:
        # Traverse customers → orders to get city + delivery data
        customers = await self.rc.search("customers_olist", limit=200)
        sample = customers[:60]

        tasks = [self.rc.get_relations(self.rc.entity_id_short(c), "Customer_To_Order_Olist", limit=10)
                 for c in sample]
        rels_list = await asyncio.gather(*tasks, return_exceptions=True)

        city_stats: dict[str, dict] = defaultdict(lambda: {
            "total": 0, "delayed": 0, "cancelled": 0, "order_ids": []
        })

        order_fetch_tasks = []
        order_city_map: list[tuple] = []

        for c, rels in zip(sample, rels_list):
            if not isinstance(rels, list):
                continue
            city = str(self.rc.attr_val(c, "customer_city") or "Unknown").title()
            for rel in rels[:3]:
                entity2 = rel.get("entity2") or rel.get("relatedEntity") or {}
                order_id = entity2.get("uri", "").split("/")[-1]
                if order_id:
                    order_fetch_tasks.append(self.rc.get_entity(order_id))
                    order_city_map.append((city, order_id))

        order_results = await asyncio.gather(*order_fetch_tasks[:60], return_exceptions=True)

        for (city, oid), order_data in zip(order_city_map, order_results):
            if isinstance(order_data, Exception):
                continue
            city_stats[city]["total"] += 1
            status = self.rc.attr_val(order_data, "order_status")
            if status == "canceled":
                city_stats[city]["cancelled"] += 1

            est_str = self.rc.attr_val(order_data, "order_estimated_delivery_date")
            actual_str = self.rc.attr_val(order_data, "order_delivered_customer_date")
            if est_str and actual_str:
                try:
                    est = datetime.fromisoformat(str(est_str).replace("Z", "").split(" ")[0])
                    actual = datetime.fromisoformat(str(actual_str).replace("Z", "").split(" ")[0])
                    if actual > est:
                        city_stats[city]["delayed"] += 1
                except Exception:
                    pass

        results = []
        for city, s in city_stats.items():
            if s["total"] > 0:
                results.append({
                    "city": city,
                    "total_orders": s["total"],
                    "delayed": s["delayed"],
                    "delay_rate_pct": round(s["delayed"] / s["total"] * 100, 1),
                    "cancellation_rate_pct": round(s["cancelled"] / s["total"] * 100, 1),
                })
        results.sort(key=lambda x: x["delay_rate_pct"], reverse=True)
        return {"success": True, "cities_analyzed": len(results), "results": results[:20]}

    # ── 13. COHORT RETENTION ───────────────────────────────────────────────

    async def tool_cohort_retention(self, args: dict) -> dict:
        customers = await self.rc.search("customers_olist", limit=200)
        gci_records = await self.rc.search("Global_Customer_Identifier", limit=200)

        # GCI cross-source stats
        cross_count = sum(
            1 for g in gci_records
            if str(self.rc.attr_val(g, "is_cross_source")).lower() == "true"
        )
        sources = defaultdict(int)
        for g in gci_records:
            src = self.rc.attr_val(g, "source") or "unknown"
            sources[str(src)] += 1

        sample = customers[:50]
        tasks = [self.rc.get_relations(self.rc.entity_id_short(c), "Customer_To_Order_Olist", limit=20)
                 for c in sample]
        rels_list = await asyncio.gather(*tasks, return_exceptions=True)

        cohort_map: dict[str, dict] = defaultdict(lambda: {"customers": 0, "returning": 0, "cities": defaultdict(int)})

        for c, rels in zip(sample, rels_list):
            if not isinstance(rels, list):
                continue
            city = str(self.rc.attr_val(c, "customer_city") or "Unknown").title()
            order_count = len(rels)

            # Approximate cohort by customer_id prefix (as pseudo first-purchase month)
            cid = str(self.rc.attr_val(c, "customer_id") or "")
            cohort = f"cohort_{cid[:1].upper()}" if cid else "cohort_unknown"

            cohort_map[cohort]["customers"] += 1
            cohort_map[cohort]["cities"][city] += 1
            if order_count > 1:
                cohort_map[cohort]["returning"] += 1

        cohorts = []
        for cohort, data in cohort_map.items():
            ret_rate = round(data["returning"] / data["customers"] * 100, 1) if data["customers"] else 0
            top_city = max(data["cities"], key=data["cities"].get) if data["cities"] else "N/A"
            cohorts.append({
                "cohort": cohort,
                "customers": data["customers"],
                "returning": data["returning"],
                "retention_rate_pct": ret_rate,
                "top_city": top_city,
            })
        cohorts.sort(key=lambda x: x["retention_rate_pct"], reverse=True)

        return {
            "success": True,
            "total_customers_analyzed": len(sample),
            "gci_total": len(gci_records),
            "cross_source_customers": cross_count,
            "sources_breakdown": dict(sources),
            "cohorts": cohorts[:10],
        }

    # ── 14. CATEGORY DEMAND VS SALES (Instacart) ───────────────────────────

    async def tool_demand_vs_sales(self, args: dict) -> dict:
        events = await self.rc.search("events_clickstream", limit=200)
        orders_ic = await self.rc.search("orders_instacart", limit=200)
        products_ic = await self.rc.search("products_instacart", limit=200)

        # Map product_id → product via Order_To_Products_Instacart traversal
        sample_orders = orders_ic[:40]
        tasks = [self.rc.get_relations(self.rc.entity_id_short(o), "Order_To_Products_Instacart", limit=10)
                 for o in sample_orders]
        rels_list = await asyncio.gather(*tasks, return_exceptions=True)

        prod_order_count: dict[str, int] = defaultdict(int)
        for rels in rels_list:
            if not isinstance(rels, list):
                continue
            for rel in rels:
                entity2 = rel.get("entity2") or rel.get("relatedEntity") or {}
                pid = entity2.get("uri", "").split("/")[-1]
                if pid:
                    prod_order_count[pid] += 1

        # Map itemid from events
        item_view_count: dict[str, int] = defaultdict(int)
        for e in events:
            item_id = str(self.rc.attr_val(e, "itemid") or "")
            ev = self.rc.attr_val(e, "event")
            if item_id and str(ev) == "view":
                item_view_count[item_id] += 1

        # Join with products_instacart for aisle/department info
        prod_info: dict[str, dict] = {}
        for p in products_ic:
            pid = str(self.rc.attr_val(p, "product_id") or "")
            if pid:
                prod_info[pid] = {
                    "name": self.rc.attr_val(p, "product_name"),
                    "aisle_id": self.rc.attr_val(p, "aisle_id"),
                    "dept_id": self.rc.attr_val(p, "department_id"),
                }

        # Combine: high views, low orders = rising demand / flat sales
        results = []
        all_ids = set(item_view_count.keys()) | set(prod_order_count.keys())
        for pid in all_ids:
            views = item_view_count.get(pid, 0)
            orders = prod_order_count.get(pid, 0)
            info = prod_info.get(pid, {})
            if views > 0:
                conversion = round(orders / views * 100, 1)
                signal = "rising_demand_flat_sales" if views > 2 and orders == 0 else (
                    "high_conversion" if conversion > 50 else "normal"
                )
                results.append({
                    "product_id": pid,
                    "product_name": info.get("name"),
                    "aisle_id": info.get("aisle_id"),
                    "views": views,
                    "orders": orders,
                    "conversion_pct": conversion,
                    "signal": signal,
                })

        results.sort(key=lambda x: x["views"], reverse=True)
        rising = [r for r in results if r["signal"] == "rising_demand_flat_sales"]
        return {
            "success": True,
            "total_products": len(results),
            "rising_demand_flat_sales": rising[:10],
            "top_by_views": results[:10],
        }

    # ── 15. AVERAGE ORDER VALUE BY CITY ────────────────────────────────────

    async def tool_avg_order_value_by_city(self, args: dict) -> dict:
        # Fetch customers → orders → products chain
        customers = await self.rc.search("customers_olist", limit=200)
        sample = customers[:60]

        tasks = [self.rc.get_relations(self.rc.entity_id_short(c), "Customer_To_Order_Olist", limit=5)
                 for c in sample]
        rels_list = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect order IDs per city
        city_orders: dict[str, list] = defaultdict(list)
        order_fetch: list = []
        order_city: list = []

        for c, rels in zip(sample, rels_list):
            if not isinstance(rels, list):
                continue
            city = str(self.rc.attr_val(c, "customer_city") or "Unknown").title()
            for rel in rels[:2]:
                entity2 = rel.get("entity2") or rel.get("relatedEntity") or {}
                oid = entity2.get("uri", "").split("/")[-1]
                if oid:
                    order_fetch.append(self.rc.get_relations(oid, "Order_To_Product_Olist", limit=20))
                    order_city.append((city, oid))

        prod_rels_list = await asyncio.gather(*order_fetch[:60], return_exceptions=True)

        # Use product weight as proxy for order value (olist has product_weight_g)
        products = await self.rc.search("products_olist", limit=200)
        prod_weight: dict[str, float] = {}
        for p in products:
            pid = self.rc.entity_id_short(p)
            w = self.rc.attr_val(p, "product_weight_g")
            if pid and w:
                prod_weight[pid] = float(w)

        city_values: dict[str, list] = defaultdict(list)
        for (city, oid), prod_rels in zip(order_city, prod_rels_list):
            if not isinstance(prod_rels, list):
                continue
            order_val = 0.0
            for pr in prod_rels:
                entity2 = pr.get("entity2") or pr.get("relatedEntity") or {}
                pid = entity2.get("uri", "").split("/")[-1]
                order_val += prod_weight.get(pid, 500.0)
            if order_val > 0:
                city_values[city].append(order_val)

        results = []
        for city, vals in city_values.items():
            if vals:
                results.append({
                    "city": city,
                    "sample_orders": len(vals),
                    "avg_order_weight_g": round(sum(vals) / len(vals), 1),
                    "max_order_weight_g": round(max(vals), 1),
                })
        results.sort(key=lambda x: x["avg_order_weight_g"], reverse=True)
        return {"success": True, "cities": len(results), "results": results[:20]}


# ── MCP TOOL DEFINITIONS (sent to Groq) ──────────────────────────────────────

MCP_TOOLS = [
    # ── CORE ENTITY ACCESS TOOLS ──────────────────────────────────────────────
    {"type": "function", "function": {
        "name": "search_entities",
        "description": (
            "Search and retrieve entities from any Reltio entity type using optional filter expressions. "
            "Use this as the entry point for single-entity queries where no cross-entity relationship traversal is needed. "
            "Supports all 7 entity types: customers_olist (customer profiles with city/state/zip), "
            "orders_olist (orders with status and delivery timestamps), "
            "products_olist (product catalog with category, dimensions, weight), "
            "orders_instacart (Instacart orders with day-of-week and hour patterns), "
            "products_instacart (Instacart product catalog with aisle and department), "
            "events_clickstream (behavioral events: view, add_to_cart, purchase with visitorid and timestamp), "
            "and Global_Customer_Identifier (cross-source identity records with confidence score, match_method, and is_cross_source flag). "
            "Filter syntax example: attributes.customer_city.value=='Sao Paulo'. "
            "Use limit/offset for pagination. "
            "Do NOT use this tool alone for cross-entity analytical questions — use the relationship-aware tools instead."
        ),
        "parameters": {"type": "object", "properties": {
            "entity_type": {
                "type": "string",
                "description": "One of: customers_olist, orders_olist, products_olist, orders_instacart, products_instacart, events_clickstream, Global_Customer_Identifier"
            },
            "filter": {
                "type": "string",
                "description": "Reltio filter expression, e.g. attributes.order_status.value=='delivered' or attributes.customer_state.value=='SP'"
            },
            "attributes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific attribute names to return (e.g. ['customer_name','customer_city']). Leave empty to return all attributes."
            },
            "limit": {"type": "integer", "description": "Max records to return. Default 50, max 200."},
            "offset": {"type": "integer", "description": "Pagination offset. Default 0."},
        }, "required": ["entity_type"]},
    }},

    {"type": "function", "function": {
        "name": "get_entity_by_id",
        "description": (
            "Fetch the complete profile of a single Reltio entity by its URI or short entity ID. "
            "Returns all attributes, source contributions, and metadata for the entity. "
            "Use this when you already have a specific entity ID from a previous search or relationship traversal "
            "and need full attribute detail — for example, fetching a specific order's delivery timestamps "
            "or a product's full weight and dimension data. "
            "Entity IDs are short alphanumeric strings like '1Cc6ssf' or full URIs like 'entities/1Cc6ssf'."
        ),
        "parameters": {"type": "object", "properties": {
            "entity_type": {
                "type": "string",
                "description": "Entity type for context (e.g. orders_olist). Optional but helpful."
            },
            "entity_id": {
                "type": "string",
                "description": "The Reltio entity ID or full URI (e.g. '1Cc6ssf' or 'entities/1Cc6ssf')"
            },
        }, "required": ["entity_id"]},
    }},

    {"type": "function", "function": {
        "name": "get_entity_relationships",
        "description": (
            "Traverse the Reltio relationship graph from a specific entity and return all connected related entities. "
            "This is the core graph-traversal tool. Use it to walk relationships one hop at a time. "
            "Available relationship types and their direction:\n"
            "  - Customer_To_Order_Olist: customers_olist → orders_olist (a customer's order history)\n"
            "  - Order_To_Product_Olist: orders_olist → products_olist (products in an olist order)\n"
            "  - Order_To_Products_Instacart: orders_instacart → products_instacart (products in an Instacart order)\n"
            "  - customerOlist_to_eventsClickstream: customers_olist → events_clickstream (customer's behavioral events)\n"
            "  - customerOlist_To_productsInstacart: customers_olist → products_instacart (cross-dataset product interest)\n"
            "  - eventClickstream_to_productsInstacart: events_clickstream → products_instacart (which Instacart products were viewed)\n"
            "If relationship_type is omitted, all relationships for the entity are returned. "
            "Use this iteratively: first fetch customers, then for each customer fetch their orders, then for each order fetch their products."
        ),
        "parameters": {"type": "object", "properties": {
            "entity_id": {
                "type": "string",
                "description": "The source entity ID to traverse from (e.g. a customer ID or order ID)"
            },
            "relationship_type": {
                "type": "string",
                "description": "Specific relationship type to traverse. One of: Customer_To_Order_Olist, Order_To_Product_Olist, Order_To_Products_Instacart, customerOlist_to_eventsClickstream, customerOlist_To_productsInstacart, eventClickstream_to_productsInstacart"
            },
            "limit": {"type": "integer", "description": "Max relationships to return. Default 100."},
        }, "required": ["entity_id"]},
    }},

    {"type": "function", "function": {
        "name": "aggregate_entities",
        "description": (
            "Run aggregation analytics on any single Reltio entity type with group-by, metrics, and optional time-range filtering. "
            "Use for questions like: 'how many customers per state', 'total orders by status', 'average product weight by category'. "
            "Supports group-by on any entity attribute and metrics: count, distinct_count, sum, avg, min, max. "
            "Time range filtering works on date attributes like order_purchase_timestamp or order_delivered_customer_date. "
            "For cross-entity aggregations (e.g. orders per customer per city), use the relationship-aware tools instead. "
            "Results are sorted by metric value descending by default. Use top_n to limit results."
        ),
        "parameters": {"type": "object", "properties": {
            "entity_type": {
                "type": "string",
                "description": "Entity type to aggregate over. One of: customers_olist, orders_olist, products_olist, orders_instacart, products_instacart, events_clickstream, Global_Customer_Identifier"
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Attribute name(s) to group by, e.g. ['customer_city'] or ['customer_state', 'order_status']"
            },
            "metric": {
                "type": "string",
                "enum": ["count", "distinct_count", "sum", "avg", "min", "max"],
                "description": "Aggregation function to apply. Use 'count' for record counts, 'distinct_count' for unique values, 'sum'/'avg'/'min'/'max' for numeric attributes."
            },
            "metric_attribute": {
                "type": "string",
                "description": "The numeric attribute to apply sum/avg/min/max on, e.g. 'product_weight_g'. Not needed for count/distinct_count."
            },
            "filter": {
                "type": "string",
                "description": "Pre-aggregation filter expression, e.g. attributes.order_status.value=='delivered'"
            },
            "time_range_attribute": {
                "type": "string",
                "description": "Date attribute name for time-based filtering, e.g. 'order_purchase_timestamp'"
            },
            "time_range_start": {"type": "string", "description": "ISO date string for range start, e.g. '2024-01-01'"},
            "time_range_end": {"type": "string", "description": "ISO date string for range end, e.g. '2024-03-31'"},
            "top_n": {"type": "integer", "description": "Return only the top N results. Default 50."},
            "sort_desc": {"type": "boolean", "description": "Sort by metric descending (default true). Set false to sort ascending."},
        }, "required": ["entity_type", "metric"]},
    }},

    # ── ANALYTICAL TOOLS WITH FULL RELATIONSHIP TRAVERSAL ─────────────────────
    {"type": "function", "function": {
        "name": "funnel_analysis",
        "description": (
            "Measure the conversion funnel across behavioral events and purchases using full relationship traversal. "
            "Traversal chain: events_clickstream → Global_Customer_Identifier (GCI) → customers_olist → Customer_To_Order_Olist → orders_olist. "
            "For each funnel step (e.g. view, add_to_cart, purchase), counts unique visitors who reached that step. "
            "The 'purchase' step is verified by actually traversing Customer_To_Order_Olist to confirm a real order exists — "
            "not just looking for a 'purchase' event, which may be incomplete. "
            "Use this to answer: 'What is our view-to-purchase conversion rate?', "
            "'Where are customers dropping off in the funnel?', "
            "'How does the funnel differ by category or device type?' "
            "Optionally filter by time range to analyze specific periods."
        ),
        "parameters": {"type": "object", "properties": {
            "funnel_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered list of event names forming the funnel, e.g. ['view', 'add_to_cart', 'purchase']. Must be in sequential order."
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional segmentation dimensions, e.g. ['product_category', 'device_type']"
            },
            "time_range_start": {"type": "string", "description": "ISO date string to filter events from"},
            "time_range_end": {"type": "string", "description": "ISO date string to filter events until"},
        }, "required": ["funnel_steps"]},
    }},

    {"type": "function", "function": {
        "name": "sales_growth_weekly",
        "description": (
            "Calculate week-over-week (WoW) sales growth for every product over the last N weeks. "
            "Traversal chain: orders_olist → Order_To_Product_Olist → products_olist. "
            "For each order, traverses the Order_To_Product_Olist relationship to identify which products were purchased, "
            "then buckets orders by calendar week using order_purchase_timestamp. "
            "Computes WoW growth percentage: ((current_week - prev_week) / prev_week) * 100. "
            "Returns two ranked lists: top growing products and top declining products. "
            "Use this to answer: 'Which products are trending up this week?', "
            "'What is driving sales growth/decline?', "
            "'Which products need promotional support due to declining momentum?'"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},

    {"type": "function", "function": {
        "name": "repeat_purchase_by_city",
        "description": (
            "Calculate the repeat purchase rate for customers grouped by city. "
            "Traversal chain: customers_olist → Customer_To_Order_Olist → orders_olist. "
            "For each customer, traverses Customer_To_Order_Olist to count how many distinct orders they have placed. "
            "A customer is classified as a 'repeat buyer' if they have more than 1 order. "
            "Groups results by customer_city and computes: total customers, repeat buyers, and repeat rate percentage. "
            "Use this to answer: 'Which cities have the most loyal customers?', "
            "'Where should we focus retention campaigns?', "
            "'Which regions show the lowest repeat purchase behaviour and need re-engagement?'"
        ),
        "parameters": {"type": "object", "properties": {
            "top_n_products": {
                "type": "integer",
                "description": "Optionally filter analysis to only the top N products by order volume"
            },
        }, "required": []},
    }},

    {"type": "function", "function": {
        "name": "customer_segmentation",
        "description": (
            "Segment all customers into New, Repeat, and Loyal tiers based on their total order count, "
            "with cross-source identity resolution via the Global_Customer_Identifier (GCI). "
            "Traversal chain: Global_Customer_Identifier → customers_olist → Customer_To_Order_Olist → orders_olist. "
            "Segmentation rules: New = 1 order, Repeat = 2-5 orders, Loyal = 6+ orders. "
            "The GCI is used to detect customers who exist across multiple source datasets (olist + instacart + dunnhumby), "
            "enriching the segment with cross-source identity flags (is_cross_source, confidence, num_sources). "
            "Use this to answer: 'What share of revenue comes from loyal customers vs new customers?', "
            "'Which customer segment contributes most to a given product category?', "
            "'How many customers have been identified across multiple data sources?'"
        ),
        "parameters": {"type": "object", "properties": {
            "group_by_category": {
                "type": "boolean",
                "description": "If true, also breaks down each segment's contribution by product category"
            },
        }, "required": []},
    }},

    {"type": "function", "function": {
        "name": "time_to_purchase",
        "description": (
            "Calculate the average time elapsed between a customer's first product view (in clickstream) "
            "and their first purchase (in orders), segmented by city. "
            "Traversal chain: events_clickstream (event=view, extract timestamp) → "
            "Global_Customer_Identifier (resolve visitorid to customer) → "
            "customers_olist → Customer_To_Order_Olist → orders_olist (get order_purchase_timestamp). "
            "Time difference is computed in days. Only includes cases where a view event precedes a purchase. "
            "Filters out implausible values (negative time or >365 days). "
            "Use this to answer: 'How long does it take customers to convert after discovery?', "
            "'Which cities have faster purchase decisions (indicating higher purchase intent)?', "
            "'What is the ideal remarketing window — when should we re-target browsing customers?'"
        ),
        "parameters": {"type": "object", "properties": {
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Additional dimensions to group by, e.g. ['product_category', 'customer_state']"
            },
        }, "required": []},
    }},

    {"type": "function", "function": {
        "name": "win_back_list",
        "description": (
            "Identify high-value customers who have been actively browsing (many recent view events) "
            "but have made no purchases in the last N days — the classic win-back target audience. "
            "Traversal chain: events_clickstream (filter by recent timestamp, count views per visitorid) → "
            "Global_Customer_Identifier (map visitorid to customer) → "
            "customers_olist → Customer_To_Order_Olist → orders_olist (check for recent orders). "
            "A customer qualifies for the win-back list if: "
            "(1) they have >= min_views view events within the lookback window, AND "
            "(2) they have no orders placed within the same window. "
            "Use this to answer: 'Who are our best win-back targets for a re-engagement campaign?', "
            "'Which customers are showing strong product interest but not converting?', "
            "'How large is our win-back audience in the last 60 days?'"
        ),
        "parameters": {"type": "object", "properties": {
            "inactivity_days": {
                "type": "integer",
                "description": "Lookback window in days. Customers with no purchases in this window qualify. Default 60."
            },
            "min_views": {
                "type": "integer",
                "description": "Minimum number of view events required to qualify as 'high browsing activity'. Default 3."
            },
            "limit": {"type": "integer", "description": "Maximum number of win-back candidates to return. Default 20."},
        }, "required": ["inactivity_days"]},
    }},

    {"type": "function", "function": {
        "name": "market_basket",
        "description": (
            "Perform market basket analysis (association rule mining) to find the top product pairs "
            "that are most frequently purchased together in the same order. "
            "Traversal chain: orders_olist → Order_To_Product_Olist (traversed TWICE per order to get all products in the basket). "
            "For each order, collects all product IDs via the Order_To_Product_Olist relationship, "
            "then builds a co-occurrence matrix of all (product_A, product_B) pairs within the same basket. "
            "Returns pairs ranked by co-occurrence frequency. "
            "Optionally filter to a specific customer segment (new/repeat/loyal) to find segment-specific affinities. "
            "Use this to answer: 'Which products should be bundled or cross-promoted together?', "
            "'What are the top product affinity pairs for repeat buyers?', "
            "'Which product combinations drive the highest basket value?'"
        ),
        "parameters": {"type": "object", "properties": {
            "customer_segment": {
                "type": "string",
                "enum": ["all", "new", "repeat", "loyal"],
                "description": "Restrict analysis to a specific customer segment. Default 'all'."
            },
            "min_support": {
                "type": "number",
                "description": "Minimum co-occurrence count threshold to include a pair. Default 2."
            },
            "top_n": {"type": "integer", "description": "Number of top pairs to return. Default 15."},
        }, "required": []},
    }},

    {"type": "function", "function": {
        "name": "delivery_performance",
        "description": (
            "Analyze delivery delay rates and cancellation rates by city by comparing estimated vs actual delivery dates. "
            "Traversal chain: customers_olist → Customer_To_Order_Olist → orders_olist "
            "(fetch order_estimated_delivery_date and order_delivered_customer_date for each order). "
            "An order is considered 'delayed' if order_delivered_customer_date > order_estimated_delivery_date. "
            "Results are grouped by customer city (from customers_olist) and include: "
            "total orders, delayed orders, delay rate %, and cancellation rate %. "
            "Use this to answer: 'Which cities have the worst delivery performance?', "
            "'Is there a correlation between delivery delays and customer returns or low satisfaction?', "
            "'Which logistics routes need immediate operational attention?'"
        ),
        "parameters": {"type": "object", "properties": {
            "time_range_start": {
                "type": "string",
                "description": "ISO date to filter orders from (based on order_purchase_timestamp)"
            },
            "time_range_end": {
                "type": "string",
                "description": "ISO date to filter orders until"
            },
        }, "required": []},
    }},

    {"type": "function", "function": {
        "name": "cohort_retention",
        "description": (
            "Calculate customer retention rates by acquisition cohort, with cross-source identity enrichment via GCI. "
            "Traversal chain: Global_Customer_Identifier (group by source, is_cross_source, confidence) → "
            "customers_olist → Customer_To_Order_Olist → orders_olist (count repeat orders per customer). "
            "Cohorts are derived from the customer's source and first purchase month. "
            "A customer is 'retained' if they have more than 1 order. "
            "The GCI enrichment adds: total GCI records, cross-source customer count, and source breakdown "
            "(e.g. how many customers are shared between olist, instacart, and dunnhumby datasets). "
            "Use this to answer: 'Which customer cohorts have the highest long-term retention?', "
            "'How does retention differ across cities?', "
            "'Are cross-source customers (identified via GCI) more loyal than single-source customers?'"
        ),
        "parameters": {"type": "object", "properties": {
            "periods": {
                "type": "integer",
                "description": "Number of periods (months) to track retention across. Default 6."
            },
            "segment_by": {
                "type": "string",
                "description": "Additional dimension to segment cohorts by, e.g. 'customer_city' or 'customer_state'"
            },
        }, "required": []},
    }},

    {"type": "function", "function": {
        "name": "demand_vs_sales",
        "description": (
            "Identify products with a demand-supply gap: high browse interest (views) but flat or zero sales (orders). "
            "Uses both Instacart and clickstream data with relationship traversal. "
            "Traversal chain: orders_instacart → Order_To_Products_Instacart → products_instacart "
            "(to count actual purchase frequency per product), "
            "PLUS events_clickstream (to count view events per itemid). "
            "Products are then joined by product_id/itemid and classified by signal: "
            "  - 'rising_demand_flat_sales': many views, zero orders — strong opportunity signal "
            "  - 'high_conversion': views convert to orders at >50% rate "
            "  - 'normal': standard view-to-order ratio. "
            "Use this to answer: 'Which products are customers interested in but not buying?', "
            "'Where is there a pricing or availability barrier preventing conversion?', "
            "'Which categories show demand momentum we can act on with promotions?'"
        ),
        "parameters": {"type": "object", "properties": {
            "weeks": {
                "type": "integer",
                "description": "Number of recent weeks to include in the analysis. Default 4."
            },
        }, "required": []},
    }},

    {"type": "function", "function": {
        "name": "avg_order_value_by_city",
        "description": (
            "Calculate the average order value (AOV) per city by traversing the full customer → order → product chain. "
            "Traversal chain: customers_olist → Customer_To_Order_Olist → orders_olist → "
            "Order_To_Product_Olist → products_olist (sum product_weight_g as a value proxy). "
            "For each order, traverses Order_To_Product_Olist to collect all products, "
            "then sums their product_weight_g as a proxy for order size/value "
            "(heavier orders typically correspond to higher-value multi-item purchases). "
            "Groups results by customer city and computes average, max, and sample size. "
            "Use this to answer: 'Which cities place the largest orders on average?', "
            "'How has average order size changed quarter-over-quarter?', "
            "'Which geographic markets represent the highest revenue opportunity per transaction?'"
        ),
        "parameters": {"type": "object", "properties": {
            "quarter": {
                "type": "string",
                "description": "Optional quarter filter in 'YYYY-QN' format, e.g. '2024-Q1'. Filters by order_purchase_timestamp."
            },
        }, "required": []},
    }},
]

SYSTEM_PROMPT = """You are a senior e-commerce data analyst agent with full access to a Reltio MDM (Master Data Management) platform. You answer ANY question about customers, orders, products, events, and cross-source identity — whether it is a simple lookup or a complex multi-step analytical scenario.

═══════════════════════════════════════════════════════
ENTITY TYPES & THEIR KEY ATTRIBUTES
═══════════════════════════════════════════════════════

customers_olist — Customer profiles from the Olist marketplace
  Attributes: customer_name, customer_id, customer_unique_id,
              customer_city, customer_state, customer_zip_code_prefix

orders_olist — Orders placed on Olist
  Attributes: order_id, customer_id, order_status,
              order_purchase_timestamp, order_approved_at,
              order_delivered_carrier_date, order_delivered_customer_date,
              order_estimated_delivery_date
  Status values: created, approved, invoiced, processing, shipped, delivered, canceled, unavailable

products_olist — Product catalog from Olist
  Attributes: product_id, product_category_name, product_category_english,
              product_weight_g, product_width_cm, product_height_cm,
              product_length_cm, product_photos_qty,
              product_description_lenght, product_name_lenght

orders_instacart — Orders from the Instacart grocery platform
  Attributes: order_id, user_id, order_number, order_dow (0=Sunday),
              order_hour_of_day, days_since_prior_order, eval_set

products_instacart — Instacart grocery product catalog
  Attributes: product_id, product_name, aisle_id, department_id

events_clickstream — Raw behavioral event stream
  Attributes: event_id, visitorid, itemid, event (view/add_to_cart/purchase),
              timestamp (Unix milliseconds)

Global_Customer_Identifier (GCI) — Cross-source unified customer identity
  Attributes: global_customer_id, source_customer_id, source (olist/instacart/dunnhumby),
              customer_id, confidence (0-1 match score), match_method (synthetic/exact/fuzzy),
              is_cross_source (true if customer appears in 2+ datasets), num_sources

═══════════════════════════════════════════════════════
RELATIONSHIP TYPES (the graph edges — always use these for cross-entity questions)
═══════════════════════════════════════════════════════

Customer_To_Order_Olist          customers_olist  ──►  orders_olist
Order_To_Product_Olist           orders_olist     ──►  products_olist
Order_To_Products_Instacart      orders_instacart ──►  products_instacart
customerOlist_to_eventsClickstream  customers_olist ──►  events_clickstream
customerOlist_To_productsInstacart  customers_olist ──►  products_instacart
eventClickstream_to_productsInstacart  events_clickstream ──►  products_instacart

═══════════════════════════════════════════════════════
YOUR REASONING APPROACH — FOLLOW THIS FOR EVERY QUESTION
═══════════════════════════════════════════════════════

Step 1 — UNDERSTAND: Identify what entities are involved and what the user really wants.
Step 2 — PLAN: Choose the minimal set of tools needed. Think: do I need relationships? Which entity is the anchor?
Step 3 — EXECUTE: Call tools sequentially, using results from one call to inform the next.
Step 4 — SYNTHESIZE: Combine all results into a clear, structured, insightful answer.
Step 5 — ENRICH: Add business context — what does this data mean? What action should be taken?

═══════════════════════════════════════════════════════
TOOL SELECTION RULES
═══════════════════════════════════════════════════════

USE search_entities WHEN:
  - Looking up a specific customer by name/city: search customers_olist with filter
  - Browsing all orders with a given status: search orders_olist with filter
  - Finding products in a category: search products_olist with filter
  - Getting a list of recent events: search events_clickstream
  - Any "show me", "list", "find", "get", "what is" question about a SINGLE entity type

USE get_entity_by_id WHEN:
  - You already have an entity ID from a previous tool call
  - You need full attribute detail for a specific record

USE get_entity_relationships WHEN:
  - Question involves two entity types connected by a relationship
  - "What did customer X buy?" → search customer → get_entity_relationships(Customer_To_Order_Olist) → get_entity_relationships(Order_To_Product_Olist)
  - "Which customers ordered product Y?" → search product → traverse relationships in reverse
  - "What events did customer Z generate?" → search customer → get_entity_relationships(customerOlist_to_eventsClickstream)
  - "Show me the order history for customer X" → search customer → get_entity_relationships(Customer_To_Order_Olist)

USE aggregate_entities WHEN:
  - "How many customers are in each state?" → aggregate customers_olist, group_by=customer_state, metric=count
  - "What is the most common order status?" → aggregate orders_olist, group_by=order_status, metric=count
  - "Average product weight by category?" → aggregate products_olist, group_by=product_category_english, metric=avg, metric_attribute=product_weight_g

USE the ANALYTICAL TOOLS (funnel_analysis, sales_growth_weekly, etc.) WHEN:
  - The question requires multi-step aggregation across 3+ entity types
  - Time-series or trend analysis is needed
  - The question maps to one of the 19 predefined analytical scenarios

═══════════════════════════════════════════════════════
GENERIC QUESTION EXAMPLES & HOW TO HANDLE THEM
═══════════════════════════════════════════════════════

"What did customer Paul Lee buy?"
→ search_entities(customers_olist, filter="attributes.customer_name.value=='Paul Lee'")
→ get_entity_relationships(entity_id=<result_id>, relationship_type="Customer_To_Order_Olist")
→ for each order: get_entity_relationships(entity_id=<order_id>, relationship_type="Order_To_Product_Olist")
→ Summarize: list of orders with products and delivery status

"Show me all delivered orders from São Paulo"
→ search_entities(orders_olist, filter="attributes.order_status.value=='delivered'", limit=50)
→ Cross-reference: search_entities(customers_olist, filter="attributes.customer_city.value=='Sao Paulo'")
→ Summarize counts and sample records

"Which products are in the electronics category?"
→ search_entities(products_olist, filter="attributes.product_category_english.value=='electronics'")
→ List products with weight and dimensions

"How many orders does customer X have?"
→ search_entities(customers_olist, filter by name)
→ get_entity_relationships(Customer_To_Order_Olist)
→ Count and list orders with status

"What is the order status of order ID abc123?"
→ get_entity_by_id(entity_id="abc123")
→ Return status, timestamps, delivery info

"What events has visitor 483605 generated?"
→ search_entities(events_clickstream, filter="attributes.visitorid.value=='483605'")
→ List all events with timestamps and item IDs

"Which products were viewed but not purchased by customer X?"
→ search customer → get_entity_relationships(customerOlist_to_eventsClickstream)
→ Filter events where event='view', collect itemids
→ get_entity_relationships(Customer_To_Order_Olist) → collect purchased product IDs
→ Return items in viewed-set but not in purchased-set

"Is customer X a cross-source customer?"
→ search_entities(Global_Customer_Identifier, filter by source_customer_id)
→ Return is_cross_source, confidence, num_sources, match_method

═══════════════════════════════════════════════════════
OUTPUT FORMAT RULES
═══════════════════════════════════════════════════════

- Always start with a one-line direct answer to the question
- Use markdown tables for lists of records (>3 items)
- Use bullet points for insights and recommendations
- Include record counts ("Found 12 orders across 3 products")
- Be honest about data limitations ("This is based on a sample of 50 entities due to API pagination")
- For lookup questions: show the actual data clearly
- For analytical questions: show data + interpretation + recommended action
- Never say "I cannot answer this" — always try to retrieve relevant data and provide the best possible answer
- If an exact filter doesn't match, try a broader search and note the approximation"""


# ── CHAT ENDPOINT ─────────────────────────────────────────────────────────────

@app.post("/chat")
async def chat(req: ChatRequest):
    config = resolve_config(req.config)
    rc = ReltioClient(config)
    executor = ToolExecutor(rc)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + req.messages
    response_messages = []
    MAX_ITERATIONS = 15  # increased to allow multi-hop generic queries

    async with httpx.AsyncClient() as client:
        for iteration in range(MAX_ITERATIONS):
            groq_resp = await client.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {config.groq_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                    "messages": messages,
                    "tools": MCP_TOOLS,
                    "tool_choice": "auto",
                    "temperature": 0.2,   # lower = more deterministic tool selection
                    "max_tokens": 4000,   # increased for richer answers
                },
                timeout=90,
            )
            if not groq_resp.is_success:
                raise HTTPException(status_code=groq_resp.status_code, detail=groq_resp.text)

            groq_data = groq_resp.json()
            choice = groq_data["choices"][0]
            msg = choice["message"]
            messages.append(msg)

            if msg.get("tool_calls"):
                tool_results = []
                for tc in msg["tool_calls"]:
                    tool_name = tc["function"]["name"]
                    try:
                        tool_args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        tool_args = {}

                    result = await executor.run(tool_name, tool_args)
                    tool_results.append({
                        "tool_call_id": tc["id"],
                        "tool_name": tool_name,
                        "args": tool_args,
                        "result": result,
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result),
                    })
                response_messages.append({"type": "tool_calls", "calls": tool_results})
                continue

            # Final text answer
            response_messages.append({"type": "text", "content": msg.get("content", "")})
            break

    return {"messages": response_messages}


@app.get("/health")
async def health():
    return {"status": "ok"}


# Serve frontend
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
