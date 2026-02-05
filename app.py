from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import asyncio
from datetime import datetime
import tempfile
import pandas as pd

from main import T2Scrap, Product, SearchResult, Config

app = FastAPI(title="T2Scrap", version="3.1.0")
templates = Jinja2Templates(directory="templates")
t2scrap_engine = T2Scrap()


class SearchRequest(BaseModel):
    query: str
    use_cache: bool = True


class ExportRequest(BaseModel):
    query: str
    products: List[Dict[str, Any]]
    format: str = "csv"


def product_to_dict(p: Product) -> dict:
    return {
        "platform": p.platform,
        "name": p.name,
        "price": p.price,
        "currency": p.currency,
        "original_price": p.original_price,
        "discount_percent": p.discount_percent,
        "url": p.url if p.url else "",
        "image_url": p.image_url if p.image_url else "",
        "rating": p.rating,
        "reviews_count": p.reviews_count,
        "seller": p.seller,
        "is_prime": p.is_prime,
        "free_shipping": p.free_shipping,
        "in_stock": p.in_stock,
        "condition": p.condition,
        "discount_display": p.discount_display,
        "savings": p.savings
    }


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/search")
async def search(request: SearchRequest):
    if len(request.query.strip()) < 2:
        raise HTTPException(400, "Query too short")
    
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: t2scrap_engine.search(request.query, use_cache=request.use_cache)
    )
    
    products = [product_to_dict(p) for p in result.products]
    best_deal = product_to_dict(result.best_deal) if result.best_deal else None
    
    return {
        "query": result.query,
        "products": products,
        "total_products": result.total_products,
        "platforms_searched": result.platforms_searched,
        "search_time": result.search_time,
        "best_deal": best_deal,
        "price_range": result.price_range,
        "timestamp": result.timestamp
    }


@app.post("/api/export")
async def export_results(request: ExportRequest):
    if not request.products:
        raise HTTPException(400, "No products")
    
    df = pd.DataFrame(request.products)
    suffix = ".xlsx" if request.format == "excel" else f".{request.format}"
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    filename = f"t2scrap_{request.query.replace(' ', '_')}"
    
    if request.format == "csv":
        df.to_csv(temp_file.name, index=False)
        return FileResponse(temp_file.name, media_type="text/csv", filename=f"{filename}.csv")
    elif request.format == "json":
        df.to_json(temp_file.name, orient="records", indent=2)
        return FileResponse(temp_file.name, media_type="application/json", filename=f"{filename}.json")
    else:
        df.to_excel(temp_file.name, index=False)
        return FileResponse(temp_file.name, filename=f"{filename}.xlsx")


@app.get("/api/stats")
async def get_stats():
    cache_stats = t2scrap_engine.cache.get_stats()
    history_stats = t2scrap_engine.history.get_stats()
    return {"cache": cache_stats, "history": history_stats, "platforms": t2scrap_engine.platform_names}


@app.post("/api/clear-cache")
async def clear_cache():
    count = t2scrap_engine.cache.clear()
    return {"cleared": count}


@app.on_event("shutdown")
async def shutdown():
    t2scrap_engine.cleanup()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)