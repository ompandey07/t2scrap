import requests
from bs4 import BeautifulSoup
import time
import re
from urllib.parse import quote_plus, urljoin, urlparse
import random
import hashlib
import pickle
from pathlib import Path
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict, field
from typing import List, Optional, Dict, Any, Tuple
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from datetime import datetime
import logging

# ============================================================
# Configuration
# ============================================================

class Config:
    APP_NAME = "T2Scrap"
    VERSION = "3.2.0"
    TIMEOUT = 15
    MAX_RETRIES = 2
    RETRY_DELAY = 1
    MAX_WORKERS = 5
    RESULTS_PER_SITE = 15
    CACHE_DIR = ".t2scrap_cache"
    CACHE_TTL = 3600
    HISTORY_FILE = "t2scrap_history.json"
    LOG_FILE = "t2scrap.log"
    DEBUG = True  # Enable debug for troubleshooting

logging.basicConfig(
    level=logging.DEBUG if Config.DEBUG else logging.WARNING,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(Config.LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()  # Also log to console
    ]
)
logger = logging.getLogger(Config.APP_NAME)

# ============================================================
# User Agents
# ============================================================

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

def get_random_ua() -> str:
    return random.choice(USER_AGENTS)

# ============================================================
# Data Classes
# ============================================================

@dataclass
class Product:
    platform: str
    name: str
    price: float
    currency: str = "USD"
    original_price: Optional[float] = None
    discount_percent: Optional[float] = None
    url: str = ""
    image_url: str = ""
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    seller: str = ""
    is_prime: bool = False
    free_shipping: bool = False
    in_stock: bool = True
    condition: str = "New"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @property
    def savings(self) -> Optional[float]:
        if self.original_price and self.original_price > self.price:
            return round(self.original_price - self.price, 2)
        return None
    
    @property
    def discount_display(self) -> str:
        if self.discount_percent:
            return f"-{self.discount_percent:.0f}%"
        elif self.savings and self.original_price:
            pct = (self.savings / self.original_price) * 100
            return f"-{pct:.0f}%"
        return ""

@dataclass
class SearchResult:
    query: str
    products: List[Product]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    search_time: float = 0.0
    
    @property
    def total_products(self) -> int:
        return len(self.products)
    
    @property
    def platforms_searched(self) -> List[str]:
        return list(set(p.platform for p in self.products))
    
    @property
    def best_deal(self) -> Optional[Product]:
        if self.products:
            return min(self.products, key=lambda p: p.price)
        return None
    
    @property
    def price_range(self) -> Tuple[float, float]:
        if self.products:
            prices = [p.price for p in self.products]
            return (min(prices), max(prices))
        return (0, 0)

# ============================================================
# Utilities
# ============================================================

def extract_price(text: str, default_currency: str = "USD") -> Tuple[Optional[float], str]:
    if not text:
        return None, default_currency
    
    text = text.strip()
    currency_map = {
        '$': 'USD', '£': 'GBP', '€': 'EUR', '¥': 'JPY',
        '₹': 'INR', 'Rs': 'INR', 'Rs.': 'INR', 'NPR': 'NPR',
        '৳': 'BDT', 'Tk': 'BDT', 'PKR': 'PKR', 'රු': 'LKR'
    }
    
    currency = default_currency
    for symbol, curr in currency_map.items():
        if symbol in text:
            currency = curr
            text = text.replace(symbol, '')
            break
    
    text = re.sub(r'(USD|INR|EUR|GBP|NPR|BDT|PKR|LKR)', '', text, flags=re.IGNORECASE)
    text = text.replace(',', '').replace(' ', '').strip()
    
    match = re.search(r'(\d+(?:\.\d{1,2})?)', text)
    if match:
        try:
            return float(match.group(1)), currency
        except ValueError:
            pass
    return None, currency

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = ' '.join(text.split())
    return text.strip()

def slugify(text: str, max_length: int = 80) -> str:
    """Convert text to URL-friendly slug"""
    # Remove special characters
    slug = re.sub(r'[^a-zA-Z0-9\s-]', '', text.lower())
    # Replace spaces with hyphens
    slug = re.sub(r'\s+', '-', slug)
    # Remove multiple hyphens
    slug = re.sub(r'-+', '-', slug)
    # Trim to max length
    return slug[:max_length].strip('-')

class CacheManager:
    def __init__(self, cache_dir: str = Config.CACHE_DIR, ttl: int = Config.CACHE_TTL):
        self.cache_dir = Path(cache_dir)
        self.ttl = ttl
        self.cache_dir.mkdir(exist_ok=True)
    
    def _get_cache_path(self, key: str) -> Path:
        hash_key = hashlib.md5(key.encode()).hexdigest()
        return self.cache_dir / f"{hash_key}.pkl"
    
    def get(self, platform: str, query: str) -> Optional[List[Product]]:
        key = f"{platform}:{query.lower().strip()}"
        cache_path = self._get_cache_path(key)
        
        if cache_path.exists():
            try:
                with open(cache_path, 'rb') as f:
                    data = pickle.load(f)
                    if time.time() - data['timestamp'] < self.ttl:
                        return data['products']
                    else:
                        cache_path.unlink()
            except Exception:
                pass
        return None
    
    def set(self, platform: str, query: str, products: List[Product]) -> None:
        key = f"{platform}:{query.lower().strip()}"
        cache_path = self._get_cache_path(key)
        try:
            with open(cache_path, 'wb') as f:
                pickle.dump({'timestamp': time.time(), 'products': products}, f)
        except Exception:
            pass
    
    def clear(self) -> int:
        count = 0
        for file in self.cache_dir.glob("*.pkl"):
            try:
                file.unlink()
                count += 1
            except Exception:
                pass
        return count
    
    def get_stats(self) -> Dict[str, Any]:
        files = list(self.cache_dir.glob("*.pkl"))
        total_size = sum(f.stat().st_size for f in files)
        return {'entries': len(files), 'size_bytes': total_size, 'size_mb': round(total_size / (1024 * 1024), 2)}

class SearchHistory:
    def __init__(self, filepath: str = Config.HISTORY_FILE):
        self.filepath = Path(filepath)
        self.history: List[Dict] = self._load()
    
    def _load(self) -> List[Dict]:
        if self.filepath.exists():
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return []
        return []
    
    def _save(self) -> None:
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, indent=2, ensure_ascii=False)
    
    def add(self, result: SearchResult) -> None:
        entry = {
            'query': result.query,
            'timestamp': result.timestamp,
            'total_products': result.total_products,
            'platforms': result.platforms_searched,
            'best_price': result.best_deal.price if result.best_deal else None,
            'best_platform': result.best_deal.platform if result.best_deal else None,
            'search_time': result.search_time
        }
        self.history.append(entry)
        self._save()
    
    def get_recent(self, limit: int = 10) -> List[Dict]:
        return self.history[-limit:][::-1]
    
    def get_stats(self) -> Dict[str, Any]:
        if not self.history:
            return {'total_searches': 0}
        return {
            'total_searches': len(self.history),
            'unique_queries': len(set(h['query'].lower() for h in self.history)),
        }

# ============================================================
# Base Scraper
# ============================================================

class BaseScraper(ABC):
    def __init__(self):
        self.name: str = "Base"
        self.base_url: str = ""
        self.search_url: str = ""
        self.currency: str = "USD"
        self.session = requests.Session()
    
    def _get_headers(self) -> Dict[str, str]:
        return {
            'User-Agent': get_random_ua(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        }
    
    @abstractmethod
    def search(self, query: str) -> List[Product]:
        pass
    
    def _make_request(self, url: str) -> Optional[requests.Response]:
        for attempt in range(Config.MAX_RETRIES):
            try:
                headers = self._get_headers()
                response = self.session.get(url, headers=headers, timeout=Config.TIMEOUT)
                if response.status_code == 200:
                    return response
                logger.debug(f"Request to {url} returned status {response.status_code}")
            except Exception as e:
                logger.debug(f"Request error ({attempt+1}): {e}")
            time.sleep(Config.RETRY_DELAY)
        return None

# ============================================================
# Daraz Scraper (Nepal) - FIXED VERSION
# ============================================================

class DarazScraper(BaseScraper):
    def __init__(self, country: str = "np"):
        super().__init__()
        self.name = "Daraz"
        self.country = country.lower()
        
        country_config = {
            'np': ('https://www.daraz.com.np', 'NPR'),
            'pk': ('https://www.daraz.pk', 'PKR'),
            'bd': ('https://www.daraz.com.bd', 'BDT'),
            'lk': ('https://www.daraz.lk', 'LKR'),
        }
        
        self.base_url, self.currency = country_config.get(country, country_config['np'])
        self.search_url = self.base_url + "/catalog/?q={query}"
    
    def search(self, query: str) -> List[Product]:
        products = self._search_api(query)
        if not products:
            logger.info("API search failed, trying HTML scraping...")
            products = self._search_html(query)
        return products[:Config.RESULTS_PER_SITE]
    
    def _build_product_url(self, item: dict, name: str) -> str:
        """Build proper Daraz product URL from API response item"""
        
        # Method 1: Try to get direct URL from various fields
        url_fields = ['itemUrl', 'productUrl', 'href', 'link', 'url']
        
        for field in url_fields:
            url = item.get(field, '')
            if url and isinstance(url, str):
                # Fix protocol-relative URLs
                if url.startswith('//'):
                    url = 'https:' + url
                # Fix relative URLs
                elif url.startswith('/'):
                    url = self.base_url + url
                # Add base URL if needed
                elif not url.startswith('http'):
                    url = self.base_url + '/' + url.lstrip('/')
                
                # Validate it's a product URL (contains /products/ or item ID pattern)
                if '/products/' in url and ('-i' in url or '.html' in url):
                    logger.debug(f"Found valid URL from {field}: {url}")
                    return url
        
        # Method 2: Build URL from item ID and SKU
        item_id = item.get('itemId') or item.get('nid') or item.get('id')
        sku_id = item.get('skuId') or item.get('sku')
        
        if item_id:
            # Create URL-friendly slug from product name
            slug = slugify(name)
            
            if sku_id:
                url = f"{self.base_url}/products/{slug}-i{item_id}-s{sku_id}.html"
            else:
                url = f"{self.base_url}/products/{slug}-i{item_id}.html"
            
            logger.debug(f"Built URL from item ID: {url}")
            return url
        
        # Method 3: Try to extract from other data
        # Some responses have the URL embedded in clickTrackInfo or similar
        click_info = item.get('clickTrackInfo', '')
        if click_info and 'itemId' in str(click_info):
            match = re.search(r'itemId[:\s]*(\d+)', str(click_info))
            if match:
                item_id = match.group(1)
                slug = slugify(name)
                url = f"{self.base_url}/products/{slug}-i{item_id}.html"
                logger.debug(f"Built URL from clickTrackInfo: {url}")
                return url
        
        logger.warning(f"Could not build URL for item: {name[:50]}")
        return ""
    
    def _search_api(self, query: str) -> List[Product]:
        products = []
        try:
            # Use the catalog search endpoint
            api_url = f"{self.base_url}/catalog/?ajax=true&q={quote_plus(query)}"
            
            headers = self._get_headers()
            headers.update({
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'Referer': f"{self.base_url}/catalog/?q={quote_plus(query)}",
            })
            
            logger.info(f"Fetching Daraz API: {api_url}")
            response = self.session.get(api_url, headers=headers, timeout=Config.TIMEOUT)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    logger.error("Failed to parse Daraz API response as JSON")
                    return products
                
                # Get items from the response
                items = data.get('mods', {}).get('listItems', [])
                logger.info(f"Found {len(items)} items in Daraz API response")
                
                # Log first item structure for debugging
                if items and Config.DEBUG:
                    logger.debug(f"Sample item keys: {list(items[0].keys())}")
                
                for item in items:
                    try:
                        # Get price
                        price = None
                        price_val = item.get('price')
                        if price_val:
                            try:
                                price = float(str(price_val).replace(',', ''))
                            except ValueError:
                                continue
                        
                        if not price or price <= 0:
                            continue
                        
                        # Get name
                        name = clean_text(item.get('name', ''))
                        if not name:
                            continue
                        
                        # Build proper product URL
                        product_url = self._build_product_url(item, name)
                        
                        # Get original price
                        original = None
                        orig_price = item.get('originalPrice')
                        if orig_price:
                            try:
                                original = float(str(orig_price).replace(',', ''))
                            except ValueError:
                                pass
                        
                        # Get discount
                        discount = None
                        disc = item.get('discount')
                        if disc:
                            match = re.search(r'(\d+)', str(disc))
                            if match:
                                discount = float(match.group(1))
                        
                        # Get rating
                        rating = None
                        rating_val = item.get('ratingScore')
                        if rating_val:
                            try:
                                rating = float(rating_val)
                            except ValueError:
                                pass
                        
                        # Get reviews count
                        reviews = None
                        reviews_val = item.get('review') or item.get('reviewCount')
                        if reviews_val:
                            try:
                                reviews = int(str(reviews_val).replace(',', ''))
                            except ValueError:
                                pass
                        
                        # Get image URL
                        image = item.get('image', '') or item.get('thumbUrl', '')
                        if image:
                            if image.startswith('//'):
                                image = 'https:' + image
                            elif not image.startswith('http'):
                                image = 'https://' + image.lstrip('/')
                        
                        # Get seller info
                        seller = item.get('sellerName', '') or item.get('brandName', '')
                        
                        # Check shipping
                        free_shipping = item.get('freeShipping', False)
                        if not free_shipping:
                            # Check in icons or tags
                            icons = item.get('icons', [])
                            for icon in icons:
                                if 'free' in str(icon).lower() and 'ship' in str(icon).lower():
                                    free_shipping = True
                                    break
                        
                        product = Product(
                            platform=self.name,
                            name=name[:150],
                            price=price,
                            currency=self.currency,
                            original_price=original,
                            discount_percent=discount,
                            url=product_url,
                            image_url=image,
                            rating=rating,
                            reviews_count=reviews,
                            seller=seller,
                            free_shipping=free_shipping
                        )
                        
                        products.append(product)
                        logger.debug(f"Added product: {name[:50]}... URL: {product_url[:80]}...")
                        
                    except Exception as e:
                        logger.debug(f"Error parsing Daraz item: {e}")
                        continue
                        
            else:
                logger.error(f"Daraz API returned status {response.status_code}")
                
        except Exception as e:
            logger.error(f"Daraz API error: {e}")
        
        logger.info(f"Daraz API returned {len(products)} products")
        return products
    
    def _search_html(self, query: str) -> List[Product]:
        """Fallback HTML scraping method"""
        products = []
        url = self.search_url.format(query=quote_plus(query))
        
        logger.info(f"Fetching Daraz HTML: {url}")
        response = self._make_request(url)
        
        if not response:
            logger.error("Failed to fetch Daraz HTML page")
            return products
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Try to find embedded JSON data (Daraz often embeds product data in script tags)
        script_data = None
        for script in soup.find_all('script'):
            script_text = script.string or ''
            if 'listItems' in script_text or 'window.pageData' in script_text:
                # Try to extract JSON from script
                match = re.search(r'window\.pageData\s*=\s*(\{.*?\});', script_text, re.DOTALL)
                if match:
                    try:
                        script_data = json.loads(match.group(1))
                        items = script_data.get('mods', {}).get('listItems', [])
                        if items:
                            logger.info(f"Found {len(items)} items in embedded JSON")
                            for item in items:
                                try:
                                    price = float(item.get('price', 0))
                                    if not price:
                                        continue
                                    name = clean_text(item.get('name', ''))
                                    if not name:
                                        continue
                                    
                                    product_url = self._build_product_url(item, name)
                                    
                                    image = item.get('image', '')
                                    if image and image.startswith('//'):
                                        image = 'https:' + image
                                    
                                    products.append(Product(
                                        platform=self.name,
                                        name=name[:150],
                                        price=price,
                                        currency=self.currency,
                                        url=product_url,
                                        image_url=image
                                    ))
                                except:
                                    continue
                    except json.JSONDecodeError:
                        pass
        
        if products:
            return products
        
        # Fallback: Try HTML selectors
        selectors = [
            '[data-qa-locator="product-item"]',
            '.gridItem--Yd0sa',
            'div[data-tracking="product-card"]',
            '.Bm3ON',
            'div[data-item-id]'
        ]
        
        cards = []
        for selector in selectors:
            cards = soup.select(selector)
            if cards:
                logger.info(f"Found {len(cards)} cards with selector: {selector}")
                break
        
        for card in cards:
            try:
                # Find name
                name_elem = (
                    card.select_one('.title--wFj93') or
                    card.select_one('[data-qa-locator="product-name"]') or
                    card.select_one('a[title]') or
                    card.select_one('h2') or
                    card.select_one('.title')
                )
                
                if not name_elem:
                    continue
                
                name = clean_text(name_elem.get_text() or name_elem.get('title', ''))
                if not name:
                    continue
                
                # Find price
                price_elem = (
                    card.select_one('.price--NVB62') or
                    card.select_one('[data-qa-locator="product-price"]') or
                    card.select_one('.price') or
                    card.select_one('[class*="price"]')
                )
                
                if not price_elem:
                    continue
                
                price, _ = extract_price(price_elem.get_text(), self.currency)
                if not price:
                    continue
                
                # Find URL - this is the key part
                link = card.find('a', href=True)
                product_url = ""
                
                if link:
                    href = link.get('href', '')
                    if href:
                        if href.startswith('//'):
                            product_url = 'https:' + href
                        elif href.startswith('/'):
                            product_url = self.base_url + href
                        elif href.startswith('http'):
                            product_url = href
                        else:
                            product_url = self.base_url + '/' + href
                
                # Find image
                img = card.find('img')
                image_url = ""
                if img:
                    image_url = img.get('src') or img.get('data-src', '')
                    if image_url and image_url.startswith('//'):
                        image_url = 'https:' + image_url
                
                products.append(Product(
                    platform=self.name,
                    name=name[:150],
                    price=price,
                    currency=self.currency,
                    url=product_url,
                    image_url=image_url
                ))
                
            except Exception as e:
                logger.debug(f"Error parsing HTML card: {e}")
                continue
        
        logger.info(f"Daraz HTML returned {len(products)} products")
        return products

# ============================================================
# eBay Scraper - Works Well
# ============================================================

class EbayScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.name = "eBay"
        self.base_url = "https://www.ebay.com"
        self.search_url = "https://www.ebay.com/sch/i.html?_nkw={query}&_sacat=0"
        self.currency = "USD"
    
    def search(self, query: str) -> List[Product]:
        products = []
        url = self.search_url.format(query=quote_plus(query))
        response = self._make_request(url)
        
        if not response:
            return products
        
        soup = BeautifulSoup(response.content, 'html.parser')
        cards = soup.select('.s-item')
        
        for card in cards:
            try:
                # Skip non-product items
                classes = card.get('class', [])
                if 's-item__pl-on-bottom' in classes:
                    continue
                
                # Name
                name_elem = card.select_one('.s-item__title span') or card.select_one('.s-item__title')
                if not name_elem:
                    continue
                name = clean_text(name_elem.get_text())
                if not name or 'shop on ebay' in name.lower():
                    continue
                
                # Price
                price_elem = card.select_one('.s-item__price')
                if not price_elem:
                    continue
                price_text = price_elem.get_text()
                
                # Handle price ranges
                if ' to ' in price_text.lower():
                    price_text = price_text.split(' to ')[0]
                
                price, _ = extract_price(price_text, self.currency)
                if not price:
                    continue
                
                # URL
                link_elem = card.select_one('a.s-item__link')
                product_url = ""
                if link_elem:
                    product_url = link_elem.get('href', '')
                    # Keep the full URL, just remove tracking params if needed
                    if product_url and not product_url.startswith('http'):
                        product_url = urljoin(self.base_url, product_url)
                
                # Image
                img_elem = card.select_one('.s-item__image-img')
                image_url = ""
                if img_elem:
                    image_url = img_elem.get('src') or img_elem.get('data-src', '')
                
                # Condition
                condition = "New"
                condition_elem = card.select_one('.SECONDARY_INFO')
                if condition_elem:
                    condition = clean_text(condition_elem.get_text())
                
                # Free shipping
                free_shipping = False
                shipping_elem = card.select_one('.s-item__shipping, .s-item__freeXDays')
                if shipping_elem:
                    shipping_text = shipping_elem.get_text().lower()
                    free_shipping = 'free' in shipping_text
                
                products.append(Product(
                    platform=self.name,
                    name=name[:150],
                    price=price,
                    currency=self.currency,
                    url=product_url,
                    image_url=image_url,
                    condition=condition,
                    free_shipping=free_shipping
                ))
            except Exception as e:
                logger.debug(f"eBay parse error: {e}")
                continue
        
        return products[:Config.RESULTS_PER_SITE]

# ============================================================
# AliExpress Scraper
# ============================================================

class AliExpressScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.name = "AliExpress"
        self.base_url = "https://www.aliexpress.com"
        self.search_url = "https://www.aliexpress.com/w/wholesale-{query}.html"
        self.currency = "USD"
    
    def search(self, query: str) -> List[Product]:
        products = []
        url = self.search_url.format(query=quote_plus(query.replace(' ', '-')))
        
        logger.info(f"Fetching AliExpress: {url}")
        response = self._make_request(url)
        
        if not response:
            return products
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Try to find JSON data in script tags
        for script in soup.find_all('script'):
            script_text = script.string or ''
            if 'window._dida_config_' in script_text or 'runParams' in script_text:
                # Try to extract product data
                matches = re.findall(r'"productId":"(\d+)"', script_text)
                for product_id in matches[:Config.RESULTS_PER_SITE]:
                    products.append(Product(
                        platform=self.name,
                        name=f"AliExpress Product {product_id}",
                        price=0.01,  # Placeholder
                        currency=self.currency,
                        url=f"https://www.aliexpress.com/item/{product_id}.html"
                    ))
        
        # Try HTML selectors
        if not products:
            cards = soup.select('[class*="SearchResult"]') or soup.select('[class*="product-card"]')
            
            for card in cards[:Config.RESULTS_PER_SITE]:
                try:
                    name_elem = card.select_one('h1') or card.select_one('h3') or card.select_one('[class*="title"]')
                    if not name_elem:
                        continue
                    name = clean_text(name_elem.get_text())
                    
                    price_elem = card.select_one('[class*="price"]')
                    if not price_elem:
                        continue
                    price, _ = extract_price(price_elem.get_text(), self.currency)
                    if not price:
                        continue
                    
                    link = card.find('a', href=True)
                    product_url = ""
                    if link:
                        product_url = link['href']
                        if product_url.startswith('//'):
                            product_url = 'https:' + product_url
                        elif not product_url.startswith('http'):
                            product_url = urljoin(self.base_url, product_url)
                    
                    img = card.find('img')
                    image_url = ""
                    if img:
                        image_url = img.get('src') or img.get('data-src', '')
                        if image_url and image_url.startswith('//'):
                            image_url = 'https:' + image_url
                    
                    products.append(Product(
                        platform=self.name,
                        name=name[:150],
                        price=price,
                        currency=self.currency,
                        url=product_url,
                        image_url=image_url
                    ))
                except:
                    continue
        
        return products

# ============================================================
# Amazon Scraper
# ============================================================

class AmazonScraper(BaseScraper):
    def __init__(self, domain: str = "com"):
        super().__init__()
        self.name = "Amazon"
        self.domain = domain
        self.base_url = f"https://www.amazon.{domain}"
        self.search_url = f"https://www.amazon.{domain}/s?k={{query}}"
        self.currency = "USD" if domain == "com" else "INR" if domain == "in" else "USD"
    
    def _get_headers(self) -> Dict[str, str]:
        headers = super()._get_headers()
        headers.update({
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': self.base_url,
            'sec-ch-ua': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
        })
        return headers
    
    def search(self, query: str) -> List[Product]:
        products = []
        url = self.search_url.format(query=quote_plus(query))
        
        time.sleep(random.uniform(0.5, 1.5))
        
        response = self._make_request(url)
        if not response:
            return products
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        cards = soup.select('[data-component-type="s-search-result"]')
        if not cards:
            cards = soup.select('.s-result-item[data-asin]')
        
        for card in cards:
            try:
                if card.select_one('.s-sponsored-label-info-icon'):
                    continue
                
                asin = card.get('data-asin', '')
                if not asin:
                    continue
                
                name_elem = (
                    card.select_one('h2 a span') or
                    card.select_one('h2 span') or
                    card.select_one('.a-text-normal')
                )
                if not name_elem:
                    continue
                name = clean_text(name_elem.get_text())
                if not name or len(name) < 5:
                    continue
                
                price = None
                price_elem = card.select_one('.a-price .a-offscreen')
                if price_elem:
                    price, _ = extract_price(price_elem.get_text(), self.currency)
                
                if not price:
                    whole = card.select_one('.a-price-whole')
                    if whole:
                        try:
                            price_str = whole.get_text().replace(',', '').replace('.', '')
                            price = float(price_str)
                            fraction = card.select_one('.a-price-fraction')
                            if fraction:
                                price += float(fraction.get_text()) / 100
                        except:
                            pass
                
                if not price:
                    continue
                
                original_price = None
                original_elem = card.select_one('.a-text-price .a-offscreen')
                if original_elem:
                    original_price, _ = extract_price(original_elem.get_text())
                
                # Build URL from ASIN
                product_url = f"{self.base_url}/dp/{asin}"
                
                image_url = ""
                img_elem = card.select_one('img.s-image')
                if img_elem:
                    image_url = img_elem.get('src', '')
                
                rating = None
                rating_elem = card.select_one('.a-icon-star-small .a-icon-alt')
                if rating_elem:
                    match = re.search(r'(\d+\.?\d*)', rating_elem.get_text())
                    if match:
                        rating = float(match.group(1))
                
                reviews = None
                reviews_elem = card.select_one('[data-csa-c-content-id*="reviews"]') or card.select_one('span.a-size-base.s-underline-text')
                if reviews_elem:
                    match = re.search(r'([\d,]+)', reviews_elem.get_text())
                    if match:
                        reviews = int(match.group(1).replace(',', ''))
                
                is_prime = bool(card.select_one('.a-icon-prime'))
                
                discount = None
                if original_price and original_price > price:
                    discount = round(((original_price - price) / original_price) * 100)
                
                products.append(Product(
                    platform=self.name,
                    name=name[:150],
                    price=price,
                    currency=self.currency,
                    original_price=original_price,
                    discount_percent=discount,
                    url=product_url,
                    image_url=image_url,
                    rating=rating,
                    reviews_count=reviews,
                    is_prime=is_prime,
                    free_shipping=is_prime
                ))
            except Exception as e:
                logger.debug(f"Amazon parse error: {e}")
                continue
        
        return products[:Config.RESULTS_PER_SITE]

# ============================================================
# Flipkart Scraper
# ============================================================

class FlipkartScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.name = "Flipkart"
        self.base_url = "https://www.flipkart.com"
        self.search_url = "https://www.flipkart.com/search?q={query}"
        self.currency = "INR"
    
    def search(self, query: str) -> List[Product]:
        products = []
        url = self.search_url.format(query=quote_plus(query))
        response = self._make_request(url)
        
        if not response:
            return products
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        cards = soup.select('div._1AtVbE > div._13oc-S')
        if not cards:
            cards = soup.select('div._2kHMtA')
        if not cards:
            cards = soup.select('div._1xHGtK._373qXS')
        if not cards:
            cards = soup.select('div._4ddWXP')
        
        for card in cards:
            try:
                name_elem = (
                    card.select_one('a.s1Q9rs') or
                    card.select_one('div._4rR01T') or
                    card.select_one('a.IRpwTa') or
                    card.select_one('a.wjcEIp')
                )
                if not name_elem:
                    continue
                name = clean_text(name_elem.get_text() or name_elem.get('title', ''))
                if not name:
                    continue
                
                price_elem = (
                    card.select_one('div._30jeq3') or
                    card.select_one('div._1_WHN1')
                )
                if not price_elem:
                    continue
                price, _ = extract_price(price_elem.get_text(), self.currency)
                if not price:
                    continue
                
                original_price = None
                original_elem = card.select_one('div._3I9_wc')
                if original_elem:
                    original_price, _ = extract_price(original_elem.get_text())
                
                discount = None
                discount_elem = card.select_one('div._3Ay6Sb')
                if discount_elem:
                    match = re.search(r'(\d+)', discount_elem.get_text())
                    if match:
                        discount = float(match.group(1))
                
                link = card.find('a', href=True)
                product_url = ""
                if link:
                    href = link.get('href', '')
                    product_url = urljoin(self.base_url, href)
                
                img = card.select_one('img._396cs4') or card.select_one('img._2r_T1I')
                image_url = ""
                if img:
                    image_url = img.get('src') or img.get('data-src', '')
                
                rating = None
                rating_elem = card.select_one('div._3LWZlK')
                if rating_elem:
                    try:
                        rating = float(rating_elem.get_text())
                    except:
                        pass
                
                products.append(Product(
                    platform=self.name,
                    name=name[:150],
                    price=price,
                    currency=self.currency,
                    original_price=original_price,
                    discount_percent=discount,
                    url=product_url,
                    image_url=image_url,
                    rating=rating
                ))
            except Exception as e:
                logger.debug(f"Flipkart parse error: {e}")
                continue
        
        return products[:Config.RESULTS_PER_SITE]

# ============================================================
# Walmart Scraper
# ============================================================

class WalmartScraper(BaseScraper):
    def __init__(self):
        super().__init__()
        self.name = "Walmart"
        self.base_url = "https://www.walmart.com"
        self.search_url = "https://www.walmart.com/search?q={query}"
        self.currency = "USD"
    
    def search(self, query: str) -> List[Product]:
        products = []
        url = self.search_url.format(query=quote_plus(query))
        
        headers = self._get_headers()
        headers['Accept'] = 'text/html,application/xhtml+xml'
        
        response = self._make_request(url)
        if not response:
            return products
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        cards = soup.select('[data-item-id]')
        if not cards:
            cards = soup.select('div[data-testid="item-stack"]')
        
        for card in cards:
            try:
                name_elem = card.select_one('[data-automation-id="product-title"]') or card.select_one('span.lh-title')
                if not name_elem:
                    continue
                name = clean_text(name_elem.get_text())
                if not name:
                    continue
                
                price_elem = card.select_one('[data-automation-id="product-price"]') or card.select_one('[itemprop="price"]')
                if not price_elem:
                    continue
                price, _ = extract_price(price_elem.get_text(), self.currency)
                if not price:
                    continue
                
                link = card.find('a', href=True)
                product_url = ""
                if link:
                    product_url = urljoin(self.base_url, link['href'])
                
                products.append(Product(
                    platform=self.name,
                    name=name[:150],
                    price=price,
                    currency=self.currency,
                    url=product_url
                ))
            except:
                continue
        
        return products[:Config.RESULTS_PER_SITE]

# ============================================================
# Main T2Scrap Engine
# ============================================================

class T2Scrap:
    def __init__(self):
        self.scrapers: List[BaseScraper] = [
            DarazScraper(country='np'),
            EbayScraper(),
            AmazonScraper(domain='com'),
            FlipkartScraper(),
            AliExpressScraper(),
        ]
        self.cache = CacheManager()
        self.history = SearchHistory()
        self._lock = threading.Lock()
        self._current_results: List[Product] = []
    
    @property
    def platform_names(self) -> List[str]:
        return [s.name for s in self.scrapers]
    
    def search(self, query: str, use_cache: bool = True) -> SearchResult:
        self._current_results = []
        start_time = time.time()
        
        print(f"\n{'='*50}")
        print(f"Searching for: {query}")
        print(f"{'='*50}")
        
        def search_platform(scraper: BaseScraper) -> Tuple[str, List[Product], bool]:
            if use_cache:
                cached = self.cache.get(scraper.name, query)
                if cached:
                    return (scraper.name, cached, True)
            
            try:
                products = scraper.search(query)
                if products and use_cache:
                    self.cache.set(scraper.name, query, products)
                return (scraper.name, products, False)
            except Exception as e:
                logger.error(f"Error searching {scraper.name}: {e}")
                return (scraper.name, [], False)
        
        with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as executor:
            futures = {executor.submit(search_platform, s): s for s in self.scrapers}
            
            for future in as_completed(futures):
                try:
                    platform, products, from_cache = future.result()
                    with self._lock:
                        self._current_results.extend(products)
                    
                    status = "✓" if products else "✗"
                    cache_tag = " (cached)" if from_cache else ""
                    print(f"  {status} {platform}: {len(products)} products{cache_tag}")
                    
                    # Log sample URL for debugging
                    if products and Config.DEBUG:
                        print(f"    Sample URL: {products[0].url[:80]}...")
                        
                except Exception as e:
                    logger.error(f"Future error: {e}")
        
        self._current_results.sort(key=lambda p: p.price)
        
        search_time = time.time() - start_time
        
        result = SearchResult(
            query=query,
            products=self._current_results.copy(),
            search_time=search_time
        )
        
        self.history.add(result)
        
        print(f"\nTotal: {result.total_products} products in {search_time:.2f}s")
        if result.best_deal:
            print(f"Best deal: {result.best_deal.currency} {result.best_deal.price} on {result.best_deal.platform}")
        print(f"{'='*50}\n")
        
        return result
    
    def cleanup(self) -> None:
        pass


# Test the scraper directly
if __name__ == "__main__":
    scraper = DarazScraper(country='np')
    products = scraper.search("laptop")
    
    print(f"\nFound {len(products)} products:")
    for i, p in enumerate(products[:5], 1):
        print(f"\n{i}. {p.name[:60]}...")
        print(f"   Price: {p.currency} {p.price}")
        print(f"   URL: {p.url}")
        print(f"   Image: {p.image_url[:60] if p.image_url else 'None'}...")