# Reltio Intelligence Agent

**Groq llama-3.3-70b-versatile + Reltio MDM APIs + Full Relationship Traversal**

## Architecture

```
Browser (index.html)
    │  POST /chat  (no CORS issues — same origin)
    ▼
FastAPI Backend (main.py)  ←─── proxies all API calls
    ├── Groq API  (llama-3.3-70b-versatile)
    └── Reltio API  (auth + entity + relationship queries)
```

## Quick Start

```bash
# Python 3.10+ required
chmod +x start.sh
./start.sh
# Then open http://localhost:8000
```

Or manually:
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# Open frontend/index.html or go to http://localhost:8000
```

## 15 MCP Tools with Relationship Traversal

| Tool | Relationship Chain |
|------|--------------------|
| `sales_growth_weekly` | orders_olist → Order_To_Product_Olist → products_olist |
| `funnel_analysis` | events_clickstream → GCI → customers_olist → orders_olist |
| `repeat_purchase_by_city` | customers_olist → Customer_To_Order_Olist → orders_olist |
| `customer_segmentation` | GCI → customers_olist → Customer_To_Order_Olist |
| `time_to_purchase` | events_clickstream → GCI → customers_olist → orders_olist |
| `win_back_list` | events_clickstream → GCI → customers_olist (order check) |
| `market_basket` | orders_olist → Order_To_Product_Olist × 2 (pair co-occurrence) |
| `delivery_performance` | customers_olist → Customer_To_Order_Olist → orders_olist |
| `cohort_retention` | GCI → customers_olist → Customer_To_Order_Olist |
| `demand_vs_sales` | orders_instacart → Order_To_Products_Instacart → products_instacart |
| `avg_order_value_by_city` | customers_olist → orders_olist → Order_To_Product_Olist → products_olist |
| `aggregate_entities` | Single entity with group-by + time filters |
| `search_entities` | Single entity type search |
| `get_entity_by_id` | Fetch one entity |
| `get_entity_relationships` | Raw relationship fetch |

## Entity Types Supported

- `customers_olist` — olist customer profiles
- `orders_olist` — olist orders with timestamps
- `products_olist` — olist product catalog
- `orders_instacart` — Instacart orders
- `products_instacart` — Instacart product catalog
- `events_clickstream` — behavioral event stream
- `Global_Customer_Identifier` — cross-source identity records

## 19 Supported Analytical Scenarios

1. Week-over-week product sales growth (last 8 weeks)
2. Conversion funnel: view → add-to-cart → purchase
3. Repeat purchase rate by city (top 50 products)
4. Top 20 products by unique customers per state, MTD
5. Customer segment (new/repeat/loyal) × revenue by category
6. Average time from first view to purchase by city
7. Rising demand (views) vs flat sales (orders) week-over-week
8. Customer retention by cohort (first purchase month) by city
9. Most frequently repurchased products within 30 days
10. Average order value by city, quarter-over-quarter
11. Win-back list: high browse, zero purchase (60 days)
12. Market basket: top product pairs for repeat buyers
13. Coupon usage impact on basket size and reorder rate
14. Return/refund rates by city and category
15. Delivery delay rate by city vs returns correlation
16. Top channels driving new customer sales (last 30 days)
17. Revenue leakage: cancellations + refunds by week
18. Share of sales from customers with recent product view (7 days)
19. Category price sensitivity (sales lift on price drops)
