import requests
from bs4 import BeautifulSoup
import time
import re
from urllib.parse import quote_plus, urljoin
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
    VERSION = "3.1.0"
    TIMEOUT = 15
    MAX_RETRIES = 2
    RETRY_DELAY = 1
    MAX_WORKERS = 5
    RESULTS_PER_SITE = 15
    CACHE_DIR = ".t2scrap_cache"
    CACHE_TTL = 3600
    HISTORY_FILE = "t2scrap_history.json"
    LOG_FILE = "t2scrap.log"
    DEBUG = False

logging.basicConfig(
    level=logging.DEBUG if Config.DEBUG else logging.WARNING,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.FileHandler(Config.LOG_FILE, encoding='utf-8')]
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
            except Exception as e:
                logger.debug(f"Request error ({attempt+1}): {e}")
            time.sleep(Config.RETRY_DELAY)
        return None

# ============================================================
# Daraz Scraper (Nepal) - Most Reliable
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
            products = self._search_html(query)
        return products[:Config.RESULTS_PER_SITE]
    
    def _search_api(self, query: str) -> List[Product]:
        products = []
        try:
            api_url = f"{self.base_url}/catalog/?ajax=true&q={quote_plus(query)}"
            headers = self._get_headers()
            headers['X-Requested-With'] = 'XMLHttpRequest'
            headers['Accept'] = 'application/json'
            
            response = self.session.get(api_url, headers=headers, timeout=Config.TIMEOUT)
            
            if response.status_code == 200:
                data = response.json()
                items = data.get('mods', {}).get('listItems', [])
                
                for item in items:
                    try:
                        price = float(item.get('price', 0))
                        if not price:
                            continue
                        
                        name = clean_text(item.get('name', ''))
                        if not name:
                            continue
                        
                        # Build proper URL
                        product_url = item.get('productUrl', '')
                        if product_url:
                            if product_url.startswith('//'):
                                product_url = 'https:' + product_url
                            elif not product_url.startswith('http'):
                                product_url = urljoin(self.base_url, product_url)
                        
                        original = None
                        if item.get('originalPrice'):
                            try:
                                original = float(item.get('originalPrice'))
                            except:
                                pass
                        
                        discount = None
                        if item.get('discount'):
                            match = re.search(r'(\d+)', str(item.get('discount')))
                            if match:
                                discount = float(match.group(1))
                        
                        rating = None
                        if item.get('ratingScore'):
                            try:
                                rating = float(item.get('ratingScore'))
                            except:
                                pass
                        
                        reviews = None
                        if item.get('review'):
                            try:
                                reviews = int(item.get('review'))
                            except:
                                pass
                        
                        image = item.get('image', '')
                        if image and image.startswith('//'):
                            image = 'https:' + image
                        
                        products.append(Product(
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
                            free_shipping=item.get('freeShipping', False)
                        ))
                    except Exception as e:
                        logger.debug(f"Daraz item error: {e}")
                        continue
        except Exception as e:
            logger.debug(f"Daraz API error: {e}")
        
        return products
    
    def _search_html(self, query: str) -> List[Product]:
        products = []
        url = self.search_url.format(query=quote_plus(query))
        response = self._make_request(url)
        
        if not response:
            return products
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Try multiple selectors
        cards = soup.select('[data-qa-locator="product-item"]')
        if not cards:
            cards = soup.select('.gridItem--Yd0sa')
        if not cards:
            cards = soup.select('div[data-tracking="product-card"]')
        
        for card in cards:
            try:
                # Name
                name_elem = card.select_one('.title--wFj93') or card.select_one('[data-qa-locator="product-name"]')
                if not name_elem:
                    continue
                name = clean_text(name_elem.get_text())
                if not name:
                    continue
                
                # Price
                price_elem = card.select_one('.price--NVB62') or card.select_one('[data-qa-locator="product-price"]')
                if not price_elem:
                    continue
                price, _ = extract_price(price_elem.get_text(), self.currency)
                if not price:
                    continue
                
                # URL
                link = card.find('a', href=True)
                url = urljoin(self.base_url, link['href']) if link else ""
                
                # Image
                img = card.find('img')
                image_url = ""
                if img:
                    image_url = img.get('src') or img.get('data-src', '')
                    if image_url.startswith('//'):
                        image_url = 'https:' + image_url
                
                products.append(Product(
                    platform=self.name,
                    name=name[:150],
                    price=price,
                    currency=self.currency,
                    url=url,
                    image_url=image_url
                ))
            except Exception:
                continue
        
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
                
                # URL - IMPORTANT: Get proper URL
                link_elem = card.select_one('a.s-item__link')
                url = ""
                if link_elem:
                    url = link_elem.get('href', '')
                    # Clean eBay tracking params but keep valid URL
                    if '?' in url:
                        url = url.split('?')[0]
                    if not url.startswith('http'):
                        url = urljoin(self.base_url, url)
                
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
                    url=url,
                    image_url=image_url,
                    condition=condition,
                    free_shipping=free_shipping
                ))
            except Exception as e:
                logger.debug(f"eBay parse error: {e}")
                continue
        
        return products[:Config.RESULTS_PER_SITE]

# ============================================================
# AliExpress Scraper - Alternative to Alibaba
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
        
        # Use API-like endpoint
        try:
            api_url = f"https://www.aliexpress.com/fn/search-pc/index?searchText={quote_plus(query)}&catId=0&initiative_id=SB_&origin=y&spm=a2g0o.home.0.0"
            headers = self._get_headers()
            headers['Referer'] = 'https://www.aliexpress.com/'
            
            response = self.session.get(api_url, headers=headers, timeout=Config.TIMEOUT)
            
            if response.status_code == 200:
                # Try to parse embedded JSON
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Look for product cards
                cards = soup.select('[class*="SearchResultContainer"]') or soup.select('[class*="product-card"]')
                
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
                        url = ""
                        if link:
                            url = link['href']
                            if url.startswith('//'):
                                url = 'https:' + url
                            elif not url.startswith('http'):
                                url = urljoin(self.base_url, url)
                        
                        products.append(Product(
                            platform=self.name,
                            name=name[:150],
                            price=price,
                            currency=self.currency,
                            url=url
                        ))
                    except:
                        continue
        except Exception as e:
            logger.debug(f"AliExpress error: {e}")
        
        return products

# ============================================================
# Amazon Scraper - With Better Headers
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
        
        # Add random delay to avoid detection
        time.sleep(random.uniform(0.5, 1.5))
        
        response = self._make_request(url)
        if not response:
            return products
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Try different selectors
        cards = soup.select('[data-component-type="s-search-result"]')
        if not cards:
            cards = soup.select('.s-result-item[data-asin]')
        
        for card in cards:
            try:
                # Skip sponsored
                if card.select_one('.s-sponsored-label-info-icon'):
                    continue
                
                asin = card.get('data-asin', '')
                if not asin:
                    continue
                
                # Name
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
                
                # Price
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
                
                # Original price
                original_price = None
                original_elem = card.select_one('.a-text-price .a-offscreen')
                if original_elem:
                    original_price, _ = extract_price(original_elem.get_text())
                
                # URL - Build from ASIN
                url = f"{self.base_url}/dp/{asin}"
                
                # Also try to get link directly
                link_elem = card.select_one('h2 a')
                if link_elem and link_elem.get('href'):
                    href = link_elem.get('href')
                    if '/dp/' in href or '/gp/' in href:
                        url = urljoin(self.base_url, href)
                
                # Image
                image_url = ""
                img_elem = card.select_one('img.s-image')
                if img_elem:
                    image_url = img_elem.get('src', '')
                
                # Rating
                rating = None
                rating_elem = card.select_one('.a-icon-star-small .a-icon-alt')
                if rating_elem:
                    match = re.search(r'(\d+\.?\d*)', rating_elem.get_text())
                    if match:
                        rating = float(match.group(1))
                
                # Reviews
                reviews = None
                reviews_elem = card.select_one('[data-csa-c-content-id*="reviews"]') or card.select_one('span.a-size-base.s-underline-text')
                if reviews_elem:
                    match = re.search(r'([\d,]+)', reviews_elem.get_text())
                    if match:
                        reviews = int(match.group(1).replace(',', ''))
                
                # Prime
                is_prime = bool(card.select_one('.a-icon-prime'))
                
                # Discount
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
                    url=url,
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
        
        # Multiple selector strategies
        cards = soup.select('div._1AtVbE > div._13oc-S')
        if not cards:
            cards = soup.select('div._2kHMtA')
        if not cards:
            cards = soup.select('div._1xHGtK._373qXS')
        if not cards:
            # Try grid layout
            cards = soup.select('div._4ddWXP')
        
        for card in cards:
            try:
                # Name
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
                
                # Price
                price_elem = (
                    card.select_one('div._30jeq3') or
                    card.select_one('div._1_WHN1')
                )
                if not price_elem:
                    continue
                price, _ = extract_price(price_elem.get_text(), self.currency)
                if not price:
                    continue
                
                # Original price
                original_price = None
                original_elem = card.select_one('div._3I9_wc')
                if original_elem:
                    original_price, _ = extract_price(original_elem.get_text())
                
                # Discount
                discount = None
                discount_elem = card.select_one('div._3Ay6Sb')
                if discount_elem:
                    match = re.search(r'(\d+)', discount_elem.get_text())
                    if match:
                        discount = float(match.group(1))
                
                # URL
                link = card.find('a', href=True)
                url = ""
                if link:
                    href = link.get('href', '')
                    url = urljoin(self.base_url, href)
                
                # Image
                img = card.select_one('img._396cs4') or card.select_one('img._2r_T1I')
                image_url = ""
                if img:
                    image_url = img.get('src') or img.get('data-src', '')
                
                # Rating
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
                    url=url,
                    image_url=image_url,
                    rating=rating
                ))
            except Exception as e:
                logger.debug(f"Flipkart parse error: {e}")
                continue
        
        return products[:Config.RESULTS_PER_SITE]

# ============================================================
# Walmart Scraper - New Addition
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
        
        # Find product grid
        cards = soup.select('[data-item-id]')
        if not cards:
            cards = soup.select('div[data-testid="item-stack"]')
        
        for card in cards:
            try:
                # Name
                name_elem = card.select_one('[data-automation-id="product-title"]') or card.select_one('span.lh-title')
                if not name_elem:
                    continue
                name = clean_text(name_elem.get_text())
                if not name:
                    continue
                
                # Price
                price_elem = card.select_one('[data-automation-id="product-price"]') or card.select_one('[itemprop="price"]')
                if not price_elem:
                    continue
                price, _ = extract_price(price_elem.get_text(), self.currency)
                if not price:
                    continue
                
                # URL
                link = card.find('a', href=True)
                url = ""
                if link:
                    url = urljoin(self.base_url, link['href'])
                
                products.append(Product(
                    platform=self.name,
                    name=name[:150],
                    price=price,
                    currency=self.currency,
                    url=url
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
        return result
    
    def cleanup(self) -> None:
        pass