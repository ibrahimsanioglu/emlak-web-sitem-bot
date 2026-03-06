import os
import sys
import json
import time
import random
from urllib.parse import urlparse, urlunparse, urlencode
import base64
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import requests
from playwright.sync_api import sync_playwright, TimeoutError
from playwright_stealth import stealth_sync
# Data klasoru
os.makedirs("/data", exist_ok=True)

print("=" * 60, flush=True)
print("BOT BASLATILIYOR...", flush=True)
print(">>> CLOUDFLARE BYPASS v6.3 (URL FIX) <<<", flush=True)
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Admin chat id (Railway env'den alınır)
ADMIN_CHAT_ID = os.getenv("CHAT_ID", "")
REAL_ADMIN_CHAT_ID = ADMIN_CHAT_ID  # Geriye uyumluluk

# Web site API (tek endpoint)
WEBSITE_API_URL = os.getenv("WEBSITE_API_URL", "https://www.diyarbakiremlakmarket.com/admin/bot_api.php")

# Bildirim alacak chat'ler (Kullanılmıyor, geriye dönük uyumluluk için boş bırakıldı)
CHAT_IDS = []

# Komut kabul edecek admin listesi (Kullanılmıyor, geriye dönük uyumluluk için boş bırakıldı)
ADMIN_CHAT_IDS = []
# GitHub ayarlari (veri yedekleme icin)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "ibrahimsanioglu/emlak-web-sitem-bot")

print("BOT_TOKEN mevcut: " + str(bool(BOT_TOKEN)), flush=True)
print("CHAT_ID mevcut: " + str(bool(os.getenv("CHAT_ID"))), flush=True)
print("__main__ basliyor...", flush=True)
# 2026-01-20: Makrolife yeni URL yapısı
URL = "https://www.makrolife.com.tr/ilanlar"
DATA_FILE = "/data/ilanlar.json"
HISTORY_FILE = "/data/history.json"
LAST_SCAN_FILE = "/data/last_scan_time.json"

# Timeout (saniye) - 60 dakika (77 sayfa icin guvenli sure)
SCAN_TIMEOUT = 60 * 60

# === YENİ GLOBAL KONTROLLER ===
SCAN_STOP_REQUESTED = False
ACTIVE_SCAN = False
AUTO_SCAN_ENABLED = None  # Başlangıçta state'ten yüklenecek (None = henüz yüklenmedi)
MANUAL_SCAN_LIMIT = None  # None = tüm sayfalar
WAITING_PAGE_CHOICE = False

# === KORUMA SABİTLERİ ===
# Bellekteki ilanların en az bu oranı bulunmalı (aksi halde tarama geçersiz)
MIN_LISTING_RATIO = 0.4  # %40
# İlk N sayfa boş gelirse site hatası olarak değerlendir
MIN_VALID_PAGES = 10

# === GOOGLE APPS SCRIPT PROXY (Cloudflare Bypass) ===
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbzSZ3QfDNIk7ARRgpV0olOXvgij0TJJCQdAtk5NmkUZ_pcgin3dzHt7_J03IZa_m_f4/exec")
USE_GOOGLE_PROXY = os.getenv("USE_GOOGLE_PROXY", "false").lower() == "true"  # Disabled - blocked by Cloudflare

# === FLARESOLVERR (Cloudflare Turnstile Bypass) ===
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "")
USE_FLARESOLVERR = os.getenv("USE_FLARESOLVERR", "true").lower() == "true"

print(f"FLARESOLVERR_URL: {FLARESOLVERR_URL}", flush=True)
print(f"USE_FLARESOLVERR: {USE_FLARESOLVERR}", flush=True)

def fetch_via_flaresolverr(url, max_timeout=120000):
    """FlareSolverr üzerinden sayfa içeriği al (Cloudflare Turnstile bypass)"""
    if not FLARESOLVERR_URL:
        print("[FLARESOLVERR] URL ayarlanmamış! Railway'de FLARESOLVERR_URL ekleyin.", flush=True)
        return None
    
    api_url = FLARESOLVERR_URL.rstrip("/")
    if not api_url.startswith("http"):
        api_url = "https://" + api_url
        
    if not api_url.endswith("/v1"):
        api_url = api_url + "/v1"
    
    # Retry mekanizması (Connection refused için)
    import time as _time
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                print(f"[FLARESOLVERR] Deneme {attempt+1}/{max_retries}...", flush=True)
            
            print(f"[FLARESOLVERR] Fetch: {url}", flush=True)
            
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": max_timeout
            }
            
            response = requests.post(api_url, json=payload, timeout=max_timeout/1000 + 30)
            
            if response.status_code != 200:
                print(f"[FLARESOLVERR] HTTP hata: {response.status_code}", flush=True)
                return None
            
            data = response.json()
            status = data.get("status", "")
            
            if status != "ok":
                message = data.get("message", "Bilinmeyen hata")
                print(f"[FLARESOLVERR] Hata: {message}", flush=True)
                return None
            
            solution = data.get("solution", {})
            html = solution.get("response", "")
            final_url = solution.get("url", url)
            cookies = solution.get("cookies", [])
            user_agent = solution.get("userAgent", "")
            
            print(f"[FLARESOLVERR] Başarılı! İçerik uzunluğu: {len(html)}, Cookies: {len(cookies)}", flush=True)
            
            if html:
                return {"content": html, "final_url": final_url, "cookies": cookies, "userAgent": user_agent}
            return None

        except requests.exceptions.ConnectionError:
            print(f"[FLARESOLVERR] Bağlantı reddedildi (Connection refused). Servis henüz hazır olmayabilir.", flush=True)
            if attempt < max_retries - 1:
                _time.sleep(5)  # 5 saniye bekle ve tekrar dene
        except requests.exceptions.Timeout:
            print("[FLARESOLVERR] Timeout - FlareSolverr çok uzun sürdü", flush=True)
            return None
        except Exception as e:
            print(f"[FLARESOLVERR] Beklenmeyen Hata: {e}", flush=True)
            return None
            
    print("[FLARESOLVERR] Tüm denemeler başarısız oldu.", flush=True)
    return None


def fetch_listings_via_flaresolverr():
    """FlareSolverr üzerinden tüm ilanları çek ve fiyatları parse et"""
    import re
    
    results = []
    seen_codes = set()  # Sayfa arası deduplication
    failed_pages = []  # Başarısız sayfaları kaydet

    page_num = 0
    consecutive_failures = 0
    MAX_FAILURES = 5  # Art arda hata limiti artırıldı
    MAX_PAGES = 100 # Güvenlik limiti
    RETRY_ATTEMPTS = 3  # Her başarısız sayfa için retry sayısı
    RETRY_WAIT = 30  # Retry öncesi bekleme süresi (saniye)
    
    print("[FLARESOLVERR] İlan taraması başlıyor...", flush=True)
    
    def process_page_html(html, page_num, result_dict=None):
        """Sayfa HTML'inden data-token'ları çıkarıp API'den verileri çeker ve results'a ekler"""
        nonlocal results, seen_codes
        page_new = 0
        
        blocks = html.split('data-token="')[1:]
        tokens = []
        for block in blocks:
            token = block.split('"')[0]
            if 'aria-hidden="true"' in block[:150]:
                continue
            if token not in tokens:
                tokens.append(token)
        
        if not tokens:
            print("[API] Sayfada data-token bulunamadı!", flush=True)
            return 0, False
            
        print(f"[API] {len(tokens)} adet token API'ye (ilan-verileri.php) gönderiliyor...", flush=True)
        
        # X-CSRF-TOKEN bilgisini HTML metninden regex ile çıkarıyoruz
        csrf_token = ""
        csrf_match = re.search(r'meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', html)
        if csrf_match:
            csrf_token = csrf_match.group(1)
        
        # result_dict (FlareSolverr'dan gelen) içindeki çerezleri requests formatına çevirelim
        cookies_dict = {}
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        
        if result_dict:
            if "cookies" in result_dict:
                for c in result_dict["cookies"]:
                    cookies_dict[c["name"]] = c["value"]
            if "userAgent" in result_dict and result_dict["userAgent"]:
                ua = result_dict["userAgent"]
        
        headers = {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
            'User-Agent': ua,
            'Referer': 'https://www.makrolife.com.tr/ilanlar',
            'Origin': 'https://www.makrolife.com.tr',
            'Accept': 'application/json, text/javascript, */*; q=0.01'
        }
        
        if csrf_token:
            headers['X-CSRF-TOKEN'] = csrf_token
            
        # API hem JSON hem de _token bekliyor olabilir
        json_payload = {"tokens": tokens}
        if csrf_token:
            json_payload["_token"] = csrf_token
            
        try:
            api_url = "https://www.makrolife.com.tr/api/ilan-verileri.php"
            resp = requests.post(api_url, json=json_payload, headers=headers, cookies=cookies_dict, timeout=30)
            
            if resp.status_code == 200:
                try:
                    # Bazı durumlarda yanıt başında boşluk veya BOM karakteri olabilir
                    text = resp.text.strip()
                    resp_json = json.loads(text)
                except ValueError:
                    print(f"[API] JSON Parse Hatası: {resp.text[:200]}", flush=True)
                    return page_new, page_new > 0
                    
                if resp_json.get("success") and "data" in resp_json:
                    data = resp_json["data"]
                    for i in range(len(tokens)):
                        d = data.get(str(i))
                        if d and isinstance(d, dict):
                            kod = d.get('ilan_kodu')
                            if not kod or kod in seen_codes:
                                continue
                            seen_codes.add(kod)
                            
                            fiyat = d.get('fiyat', 'Fiyat Yok')
                            href = d.get('seo_url', '')
                            baslik = d.get('baslik') or kod
                            
                            final_href = f"https://www.makrolife.com.tr{href}" if href.startswith("/") else href
                            
                            results.append((
                                kod,
                                fiyat,
                                final_href,
                                baslik,
                                page_num
                            ))
                            page_new += 1
                    return page_new, True
                else:
                    print(f"[API] Başarısız yanıt veya data yok: {str(resp_json)[:100]}", flush=True)
            else:
                print(f"[API] HTTP Hata {resp.status_code}: {resp.text[:200]}", flush=True)
                # 403 alırsak Cloudflare engellemiş demektir
        except Exception as e:
            print(f"[API] Hata: {e}", flush=True)
            
        return page_new, page_new > 0
    
    # ============ ANA TARAMA DÖNGÜSÜ ============
    base_result_dict = None  # Sayfa 1'den gelen FlareSolverr session verilerini saklamak için
    
    while page_num < MAX_PAGES:
        if SCAN_STOP_REQUESTED:
            print("[FLARESOLVERR] Kullanıcı durdurdu", flush=True)
            break
        
        page_num += 1
        page_url = URL if page_num == 1 else URL
        
        if page_num == 1:
            print(f"[FLARESOLVERR SAYFA 1] {URL}", flush=True)
            result = fetch_via_flaresolverr(page_url)
            base_result_dict = result
        else:
            print(f"[FLARESOLVERR SAYFA {page_num}] AJAX tetikleniyor...", flush=True)
            if not base_result_dict:
                print(f"[FLARESOLVERR SAYFA {page_num}] Temel oturum bilgisi eksik", flush=True)
                break
                
            # CSRF token'ı base result'tan alalım
            csrf_token = ""
            base_html = base_result_dict.get("content", "")
            csrf_match = re.search(r'meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', base_html)
            if csrf_match:
                csrf_token = csrf_match.group(1)

            # Çerezleri dict formatına çevir
            cookies_dict = {}
            if "cookies" in base_result_dict:
                for c in base_result_dict["cookies"]:
                    cookies_dict[c["name"]] = c["value"]

            ua = base_result_dict.get("userAgent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
            
            headers = {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'User-Agent': ua,
                'Referer': 'https://www.makrolife.com.tr/ilanlar',
                'Origin': 'https://www.makrolife.com.tr',
                'Accept': 'application/json, text/javascript, */*; q=0.01'
            }
            if csrf_token:
                headers['X-CSRF-TOKEN'] = csrf_token
            
            json_payload = {"sayfa": page_num, "filtreler": {}}
            if csrf_token:
                json_payload["_token"] = csrf_token
                
            try:
                # AJAX Sayfalama API'sine doğrudan JSON+Token ile istek atılıyor
                resp = requests.post("https://www.makrolife.com.tr/api/ilan-sayfalama.php", json=json_payload, headers=headers, cookies=cookies_dict, timeout=30)
                if resp.status_code == 200:
                    try:
                        resp_json = resp.json()
                        if resp_json.get("success"):
                            html = resp_json.get("html", "")
                            result = {"content": html, **base_result_dict}
                        else:
                            print(f"[API SAYFALAMA] Başarısız yanıt: {str(resp_json)[:100]}", flush=True)
                            result = None
                    except ValueError:
                        print(f"[API SAYFALAMA] JSON Parse Hatası: {resp.text[:200]}", flush=True)
                        result = None
                else:
                    print(f"[API SAYFALAMA] HTTP Hata {resp.status_code}: {resp.text[:200]}", flush=True)
                    result = None
            except Exception as e:
                print(f"[API SAYFALAMA] Hata: {e}", flush=True)
                result = None
        
        if not result or not result.get("content"):
            consecutive_failures += 1
            print(f"[FLARESOLVERR SAYFA {page_num}] İçerik alınamadı", flush=True)
            
            if page_num <= 3:
                # İlk 3 sayfada hata: 3 kez retry dene
                retry_ok = False
                for retry_i in range(1, 4):
                    print(f"[FLARESOLVERR] Sayfa {page_num} retry {retry_i}/3...", flush=True)
                    time.sleep(10)
                    
                    if page_num == 1:
                        result = fetch_via_flaresolverr(page_url)
                        base_result_dict = result
                    else:
                        try:
                            # Retry için de aynı headers ve cookies kullanılıyor
                            retry_payload = {"sayfa": page_num}
                            if csrf_token:
                                retry_payload["_token"] = csrf_token
                                
                            resp = requests.post("https://www.makrolife.com.tr/api/ilan-sayfalama.php", data=retry_payload, headers=headers, cookies=cookies_dict, timeout=30)
                            if resp.status_code == 200:
                                resp_json = resp.json()
                                if resp_json.get("success"):
                                    html = resp_json.get("html", "")
                                    result = {"content": html, **base_result_dict}
                                else:
                                    result = None
                            else:
                                result = None
                        except:
                            result = None

                    if result and result.get("content"):
                        retry_ok = True
                        consecutive_failures = 0
                        break
                if not retry_ok:
                    print("[FLARESOLVERR] İlk 3 sayfada 3 retry başarısız - tarama iptal", flush=True)
                    return None
                # Retry başarılı oldu, bu sayfayı işle
                html = result["content"]
                page_new, _ = process_page_html(html, page_num, result)
                print(f"[FLARESOLVERR SAYFA {page_num}] Retry başarılı! {page_new} ilan (toplam: {len(results)})", flush=True)
                if page_num % 10 == 0:
                    time.sleep(3)
                else:
                    time.sleep(1.0)
                continue
            
            # Başarısız sayfayı kaydet
            failed_pages.append(page_num)
            print(f"[FLARESOLVERR] Sayfa {page_num} retry listesine eklendi", flush=True)
            
            if consecutive_failures >= MAX_FAILURES:
                print(f"[FLARESOLVERR] Art arda {MAX_FAILURES} hata - ana taramaya devam ediliyor", flush=True)
                # Devam et ama art arda hata sayacını sıfırlama
            continue
        
        consecutive_failures = 0
        html = result["content"]
        
        # HTML'den data-token'ları çıkar
        token_pattern = r'data-token=["\']([^"\']+)["\']'
        all_matches = re.findall(token_pattern, html)
        
        if not all_matches:
            if page_num <= 1:
                print(f"[FLARESOLVERR] Sayfa {page_num} boş - hata", flush=True)
                return None
            print(f"[FLARESOLVERR SAYFA {page_num}] Son sayfa geçildi", flush=True)
            break
        
        page_new, _ = process_page_html(html, page_num, base_result_dict)
        
        if page_new == 0:
            # Bu sayfada yeni ilan yok, muhtemelen son sayfa
            print(f"[FLARESOLVERR SAYFA {page_num}] Yeni ilan yok - son sayfa", flush=True)
            break
        
        print(f"[FLARESOLVERR SAYFA {page_num}] {page_new} ilan bulundu (toplam: {len(results)})", flush=True)
        
        # Bekleme
        if page_num % 10 == 0:
            time.sleep(3)
        else:
            time.sleep(1.0)
    
    # ============ BAŞARISIZ SAYFALAR İÇİN RETRY ============
    if failed_pages and not SCAN_STOP_REQUESTED:
        print(f"\n[FLARESOLVERR] === RETRY BAŞLIYOR ===", flush=True)
        print(f"[FLARESOLVERR] Başarısız sayfalar: {failed_pages}", flush=True)
        
        for retry_attempt in range(1, RETRY_ATTEMPTS + 1):
            if not failed_pages or SCAN_STOP_REQUESTED:
                break
            
            print(f"\n[FLARESOLVERR] Retry {retry_attempt}/{RETRY_ATTEMPTS} - {len(failed_pages)} sayfa kaldı", flush=True)
            print(f"[FLARESOLVERR] {RETRY_WAIT} saniye bekleniyor...", flush=True)
            time.sleep(RETRY_WAIT)
            
            still_failed = []
            
            for failed_page in failed_pages:
                if SCAN_STOP_REQUESTED:
                    break
                
                if failed_page == 1:
                    result = fetch_via_flaresolverr(URL)
                else:
                    if not base_result_dict:
                        still_failed.append(failed_page)
                        continue
                    
                    # CSRF token'ı base result'tan alalım
                    csrf_token = ""
                    base_html = base_result_dict.get("content", "")
                    csrf_match = re.search(r'meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', base_html)
                    if csrf_match:
                        csrf_token = csrf_match.group(1)
                    
                    headers = {
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'X-Requested-With': 'XMLHttpRequest',
                        'User-Agent': base_result_dict.get("userAgent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
                    }
                    if csrf_token:
                        headers['X-CSRF-TOKEN'] = csrf_token
                    
                    if cookies_str:
                        headers['Cookie'] = cookies_str
                        
                    post_data_str = urlencode([("sayfa", failed_page)])
                    fs_payload = {
                        "cmd": "request.post",
                        "url": "https://www.makrolife.com.tr/api/ilan-sayfalama.php",
                        "maxTimeout": 45000,
                        "postData": post_data_str
                    }
                    
                    try:
                        resp = requests.post(FLARESOLVERR_URL, json=fs_payload, timeout=60)
                        if resp.status_code == 200:
                            fs_res = resp.json()
                            if fs_res.get("status") == "ok":
                                # JSON parselama
                                html_res = fs_res["solution"]["response"]
                                # Veri bazen <pre> içinde bazen direkt gelebilir
                                json_str = html_res
                                if "<body" in html_res:
                                    json_str = html_res.split("<body>")[1].split("</body>")[0]
                                
                                json_str = json_str.replace("<pre>", "").replace("</pre>", "").strip()
                                try:
                                    resp_json = json.loads(json_str)
                                    if resp_json.get("success"):
                                        result = {"content": resp_json.get("html", ""), **base_result_dict}
                                    else:
                                        result = None
                                        print(f"[API SAYFALAMA] Başarısız: {str(resp_json)[:100]}", flush=True)
                                except ValueError:
                                    print(f"[API SAYFALAMA] JSON Parse Hatası (Ham Veri): {json_str[:200]}", flush=True)
                                    result = None
                            else:
                                result = None
                        else:
                            result = None
                    except Exception as e:
                        print(f"[API SAYFALAMA] Hata: {e}", flush=True)
                        result = None
                        
                if result and result.get("content"):
                    html = result["content"]
                    page_new, success = process_page_html(html, failed_page, base_result_dict if failed_page > 1 else result)
                    
                    if success:
                        print(f"[FLARESOLVERR RETRY] Sayfa {failed_page} BAŞARILI! {page_new} ilan eklendi (toplam: {len(results)})", flush=True)
                    else:
                        print(f"[FLARESOLVERR RETRY] Sayfa {failed_page} içerik alındı ama ilan yok", flush=True)
                else:
                    print(f"[FLARESOLVERR RETRY] Sayfa {failed_page} hala başarısız", flush=True)
                    still_failed.append(failed_page)
                
                time.sleep(2)  # Retry'lar arası bekleme
            
            failed_pages = still_failed
        
        if failed_pages:
            print(f"\n[FLARESOLVERR] Retry sonrası hala başarısız sayfalar: {failed_pages}", flush=True)
        else:
            print(f"\n[FLARESOLVERR] Tüm başarısız sayfalar kurtarıldı!", flush=True)
    
    if len(results) == 0:
        print("[FLARESOLVERR] Hiç ilan bulunamadı", flush=True)
        return None
    
    print(f"[FLARESOLVERR] Toplam {len(results)} ilan bulundu", flush=True)
    return results

def fetch_via_google_proxy(url):
    """Google Apps Script üzerinden sayfa içeriği al (Cloudflare bypass)"""
    if not GOOGLE_SCRIPT_URL:
        print("[GOOGLE_PROXY] URL ayarlanmamış!", flush=True)
        return None
    
    proxy_url = f"{GOOGLE_SCRIPT_URL}?url={requests.utils.quote(url)}"
    print(f"[GOOGLE_PROXY] Fetch: {url}", flush=True)
    
    try:
        response = requests.get(proxy_url, timeout=90, headers={"Accept": "application/json"})
        if response.status_code != 200:
            print(f"[GOOGLE_PROXY] HTTP hata: {response.status_code}", flush=True)
            return None
        
        data = response.json()
        http_code = data.get("http_code", 0)
        content = data.get("content", "")
        final_url = data.get("final_url", url)
        
        print(f"[GOOGLE_PROXY] Başarılı! HTTP: {http_code}, İçerik uzunluğu: {len(content)}", flush=True)
        
        if http_code == 200 and content:
            return {"content": content, "final_url": final_url}
        return None
        
    except Exception as e:
        print(f"[GOOGLE_PROXY] Hata: {e}", flush=True)
        return None


def fetch_listings_via_google_proxy():
    """Google Proxy üzerinden tüm ilanları çek"""
    import re
    
    results = []
    seen_codes = set()
    page_num = 0
    consecutive_failures = 0
    MAX_FAILURES = 3
    MAX_PAGES = 100
    
    print("[GOOGLE_PROXY] İlan taraması başlıyor...", flush=True)
    
    while page_num < MAX_PAGES:
        if SCAN_STOP_REQUESTED:
            print("[GOOGLE_PROXY] Kullanıcı durdurdu", flush=True)
            break
        
        page_num += 1
        if page_num == 1:
            page_url = URL
        else:
            page_url = URL + "?page=" + str(page_num)
        
        print(f"[GOOGLE_PROXY SAYFA {page_num}] {page_url}", flush=True)
        
        proxy_result = fetch_via_google_proxy(page_url)
        
        if not proxy_result or not proxy_result.get("content"):
            consecutive_failures += 1
            print(f"[GOOGLE_PROXY SAYFA {page_num}] İçerik alınamadı", flush=True)
            
            if page_num <= 3:
                print("[GOOGLE_PROXY] İlk 3 sayfada hata - tarama iptal", flush=True)
                return None
            
            if consecutive_failures >= MAX_FAILURES:
                print("[GOOGLE_PROXY] Art arda 3 hata - tarama durduruluyor", flush=True)
                break
            continue
        
        consecutive_failures = 0
        html = proxy_result["content"]
        
        page_new, _ = process_page_html(html, page_num)
        
        if page_new == 0:
            print(f"[GOOGLE_PROXY SAYFA {page_num}] İlan yok - son sayfa geçildi", flush=True)
            break
            
        print(f"[GOOGLE_PROXY SAYFA {page_num}] {page_new} ilan bulundu (toplam: {len(results)})", flush=True)
        time.sleep(1)
    
    if len(results) == 0:
        print("[GOOGLE_PROXY] Hiç ilan bulunamadı", flush=True)
        return None
    
    print(f"[GOOGLE_PROXY] Toplam {len(results)} ilan bulundu", flush=True)
    return results


# === CLOUDFLARE BYPASS HELPER ===
def wait_for_cloudflare(page, timeout=45000):
    """Cloudflare JS Challenge'ının tamamlanmasını bekle - AGRESİF YAKLAŞIM"""
    import time as _time
    import random as _random
    
    print("[CF] Sayfa içeriği kontrol ediliyor...", flush=True)
    
    # Sayfa içeriğinin ilk 500 karakterini logla (debug için)
    try:
        page_content = page.content()
        page_title = page.title()
        print(f"[CF] Sayfa başlığı: {page_title}", flush=True)
        print(f"[CF] İçerik önizleme: {page_content[:500]}...", flush=True)
    except Exception as e:
        print(f"[CF] İçerik okunamadı: {e}", flush=True)
    
    # Human-like davranış: rastgele mouse hareketi
    def simulate_human_behavior():
        try:
            # Rastgele mouse hareketi
            for _ in range(3):
                x = _random.randint(100, 800)
                y = _random.randint(100, 600)
                page.mouse.move(x, y)
                _time.sleep(_random.uniform(0.1, 0.3))
            
            # Turnstile checkbox'ı ara ve tıkla
            turnstile_selectors = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[title*="challenge"]',
                '#turnstile-wrapper iframe',
                '.cf-turnstile iframe',
            ]
            for selector in turnstile_selectors:
                try:
                    frames = page.frames
                    for frame in frames:
                        if 'challenges.cloudflare.com' in frame.url:
                            print(f"[CF] Turnstile iframe bulundu: {frame.url}", flush=True)
                            # Checkbox'ı bul ve tıkla
                            checkbox = frame.locator('input[type="checkbox"]')
                            if checkbox.count() > 0:
                                print("[CF] Turnstile checkbox tıklanıyor...", flush=True)
                                checkbox.click()
                                _time.sleep(2)
                                return True
                except:
                    pass
            
            # Alternatif: doğrudan iframe'e tıkla
            for selector in turnstile_selectors:
                try:
                    iframe_elem = page.locator(selector)
                    if iframe_elem.count() > 0:
                        print(f"[CF] Iframe bulundu: {selector}", flush=True)
                        box = iframe_elem.bounding_box()
                        if box:
                            # Checkbox genellikle sol tarafta olur
                            click_x = box['x'] + 30
                            click_y = box['y'] + box['height'] / 2
                            page.mouse.click(click_x, click_y)
                            print(f"[CF] Iframe tıklandı: ({click_x}, {click_y})", flush=True)
                            _time.sleep(2)
                            return True
                except Exception as e:
                    print(f"[CF] Iframe tıklama hatası: {e}", flush=True)
            
        except Exception as e:
            print(f"[CF] Human simulation hatası: {e}", flush=True)
        return False
    
    # İlan linkleri veya konteynerları var mı kontrol et
    try:
        # Yeni yöntem: konteyner ara veya link ara
        ilan_count = page.locator('.cb-list-item, .locationDiv, a[href*="/ilan/"]').count()
        print(f"[CF] Mevcut ilan/konteyner sayısı: {ilan_count}", flush=True)
        
        if ilan_count > 0:
            print("[CF] İlanlar/Konteynerlar yüklü, devam ediliyor", flush=True)
            return True
    except Exception as e:
        print(f"[CF] Locator hatası: {e}", flush=True)
    
    # İlan yoksa bekle (Cloudflare challenge olabilir)
    print("[CF] İlan bulunamadı, Cloudflare challenge bekleniyor...", flush=True)
    
    # İlk deneme: human davranışı simüle et
    simulate_human_behavior()
    
    # 60 saniye boyunca 3 saniyede bir kontrol et (20 deneme)
    max_attempts = 20
    for attempt in range(max_attempts):
        _time.sleep(3)
        
        # Her 5 denemede bir mouse hareketi yap
        if attempt > 0 and attempt % 5 == 0:
            simulate_human_behavior()
        
        try:
            # Sadece konteyner var mı değil, içine veri dolmuş mu (ML- ile başlayan kod var mı) kontrol et
            is_data_populated = page.evaluate("""() => {
                const kodElems = document.querySelectorAll('.ilan-kod-ph');
                for (const el of kodElems) {
                    if (el.textContent && el.textContent.trim().match(/ML-[A-Z0-9-]+/i)) return true;
                }
                // Alternatif: data-token'lardan biri dolu mu?
                const tokens = document.querySelectorAll('[data-token]');
                if (tokens.length > 0 && !document.querySelector('.placeholder')) return true; 
                return false;
            }""")
            
            ilan_count = page.locator('.cb-list-item, .locationDiv, a[href*="/ilan/"]').count()
            print(f"[CF] Deneme {attempt + 1}/{max_attempts}: {ilan_count} konteyner (Dolu mu: {is_data_populated})", flush=True)
            
            if is_data_populated:
                print(f"[CF] Cloudflare bypass ve Veri Yükleme BAŞARILI! ({(attempt + 1) * 3} saniye sonra)", flush=True)
                return True
        except Exception as e:
            print(f"[CF] Deneme {attempt + 1} hatası: {e}", flush=True)
    
    # Son çare: sayfayı yenile ve tekrar dene
    print("[CF] Son çare: Sayfa yenileniyor...", flush=True)
    try:
        page.reload(wait_until="networkidle", timeout=60000)
        _time.sleep(5)
        simulate_human_behavior()
        _time.sleep(3)
        ilan_count = page.locator('.cb-list-item, .locationDiv, a[href*="/ilan/"]').count()
        if ilan_count > 0:
            print(f"[CF] Yenileme sonrası başarılı! {ilan_count} ilan/konteyner", flush=True)
            page.wait_for_timeout(2000)
            return True
    except Exception as e:
        print(f"[CF] Yenileme hatası: {e}", flush=True)
    
    print("[CF] Cloudflare bypass BAŞARISIZ - tüm denemeler tükendi", flush=True)
    return False


def get_turkey_time():
    return datetime.utcnow() + timedelta(hours=3)

# Sabit tarama saatleri (Türkiye saati)
SCHEDULED_SCAN_HOURS = [10, 13, 16, 19]

def get_scheduled_hours():
    """Tarama saatlerini döndür"""
    return SCHEDULED_SCAN_HOURS

def get_next_scan_time():
    """Bir sonraki tarama saatine kadar kalan süreyi saniye olarak döndür"""
    now = get_turkey_time()
    current_hour = now.hour
    current_minute = now.minute
    
    # Bugün için kalan tarama saatlerini bul
    for hour in SCHEDULED_SCAN_HOURS:
        if hour > current_hour or (hour == current_hour and current_minute < 5):
            # Bu saatten önceyiz, bu saate kadar bekle
            minutes_until = (hour - current_hour) * 60 - current_minute
            return max(minutes_until * 60, 60)  # En az 1 dakika
    
    # Bugünkü tüm saatler geçti, yarının ilk saatine kadar bekle
    hours_until_midnight = 24 - current_hour
    hours_from_midnight = SCHEDULED_SCAN_HOURS[0]
    total_hours = hours_until_midnight + hours_from_midnight
    minutes_until = total_hours * 60 - current_minute
    return minutes_until * 60

def should_scan_now():
    """Şu an tarama saati mi kontrol et (±5 dakika tolerans)"""
    now = get_turkey_time()
    current_hour = now.hour
    current_minute = now.minute
    
    if current_hour in SCHEDULED_SCAN_HOURS and current_minute < 5:
        return True
    return False

def get_scan_interval():
    """Geriye uyumluluk için - bir sonraki taramaya kalan süre"""
    return get_next_scan_time()

# Istatistikler
bot_stats = {
    "start_time": None,
    "total_scans": 0,
    "total_new_listings": 0,
    "total_price_changes": 0,
    "total_deleted": 0,
    "last_scan_time": None,
    "last_scan_duration": 0,
    "last_scan_listings": 0,
    "last_scan_pages": 0,
    "errors": 0,
    "timeouts": 0
}

last_update_id = 0

# GitHub state cache (ilanlar.json)
STATE_CACHE = None
STATE_GITHUB_SHA = None


def telegram_api(method: str, data: dict, timeout: int = 10, max_retries: int = 2):
    """Telegram API çağrısı (POST) - retry mekanizmalı."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=data, timeout=timeout)
            
            # 400 Bad Request - callback query expired veya geçersiz
            if resp.status_code == 400:
                error_desc = ""
                try:
                    error_desc = resp.json().get("description", "")
                except:
                    pass
                # Callback query expired - sessizce geç, retry yapma
                if "query is too old" in error_desc or "query_id" in error_desc.lower():
                    print(f"[TELEGRAM] {method} callback expired (normal durum)", flush=True)
                    return None
                print(f"[TELEGRAM] {method} 400 HATA: {error_desc or resp.text[:200]}", flush=True)
                return None
            
            resp.raise_for_status()
            return resp.json()
            
        except requests.exceptions.ConnectionError as e:
            # Network unreachable - retry with backoff
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            print(f"[TELEGRAM] {method} bağlantı hatası (deneme {attempt + 1}/{max_retries}): {e}", flush=True)
            if attempt < max_retries - 1:
                time.sleep(wait_time)
            else:
                print(f"[TELEGRAM] {method} tüm denemeler başarısız", flush=True)
                return None
                
        except requests.exceptions.Timeout as e:
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            print(f"[TELEGRAM] {method} timeout (deneme {attempt + 1}/{max_retries}): {e}", flush=True)
            if attempt < max_retries - 1:
                time.sleep(wait_time)
            else:
                return None
                
        except Exception as e:
            print(f"[TELEGRAM] {method} HATA: {e}", flush=True)
            return None
    
    return None


def send_message(text: str, chat_id: str = None, reply_markup=None, disable_preview: bool = True, include_real_admin: bool = True):
    """Telegram'a mesaj gönder.
    - chat_id verilirse sadece o kişiye gider.
    - chat_id yoksa ADMIN_CHAT_ID'ye gönderir.
    """
    if not BOT_TOKEN:
        print("[TELEGRAM] BOT_TOKEN yok, mesaj atlanıyor", flush=True)
        return False

    payload = {
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True if disable_preview else False,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    def _post(one_chat_id: str):
        payload2 = dict(payload)
        payload2["chat_id"] = one_chat_id
        result = telegram_api("sendMessage", payload2, timeout=30)
        return result is not None

    if chat_id:
        return _post(str(chat_id))

    if ADMIN_CHAT_ID:
        return _post(str(ADMIN_CHAT_ID))

    return False

def answer_callback_query(callback_query_id: str, text: str = None, show_alert: bool = False):
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text[:180]
    telegram_api("answerCallbackQuery", payload, timeout=10)


def edit_message_reply_markup(chat_id: str, message_id: int, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id}
    payload["reply_markup"] = json.dumps(reply_markup or {"inline_keyboard": []}, ensure_ascii=False)
    telegram_api("editMessageReplyMarkup", payload, timeout=10)


def call_site_api(action: str, **params):
    """Web site bot_api.php ile konuş. Hata olursa detay döndür."""
    # add işlemi scraper çağırdığı için daha uzun timeout gerekiyor
    timeout = 60 if action == "add" else 25
    
    def _post(url: str):
        try:
            r = requests.post(url, data={"action": action, **params}, timeout=timeout)
            return r
        except Exception as e:
            return e

    url = WEBSITE_API_URL

    # 1) İlk deneme
    r1 = _post(url)
    # Exception
    if isinstance(r1, Exception):
        return {"success": False, "error": "request_failed", "detail": str(r1), "url": url}

    # 404 ise ve URL'de /admin/ yoksa: /admin/bot_api.php ile bir kere daha dene
    if r1.status_code == 404:
        try:
            pu = urlparse(url)
            path = pu.path or "/"
            if "/admin/" not in path:
                if path.startswith("/"):
                    new_path = "/admin" + path
                else:
                    new_path = "/admin/" + path
                alt = urlunparse((pu.scheme, pu.netloc, new_path, pu.params, pu.query, pu.fragment))
                r2 = _post(alt)
                if not isinstance(r2, Exception):
                    url = alt
                    r1 = r2
        except Exception:
            pass

    # JSON parse
    try:
        data = r1.json()
    except Exception:
        return {
            "success": False,
            "error": "non_json_response",
            "http_status": r1.status_code,
            "url": url,
            "snippet": (r1.text or "")[:400]
        }

    # bot_api.php bazen success=false döndürür; bunu üst katman yorumlar
    if r1.status_code >= 400:
        data["_http_status"] = r1.status_code
        data["_url"] = url
    return data
def site_exists(ilan_kodu: str):
    r = call_site_api("exists", ilan_kodu=ilan_kodu)
    # r her zaman dict döndürmeye çalışır
    if not isinstance(r, dict):
        return {"exists": None, "error": "unexpected_response"}
    if r.get("success") is False and r.get("error"):
        return {"exists": None, **r}
    # normal
    return r

def _site_status_line(exists_resp: dict) -> str:
    # exists True/False/None
    ex = exists_resp.get("exists", None)
    if ex is True:
        ilan_id = exists_resp.get("ilan_id")
        table = exists_resp.get("table") or "ilanlar"
        extra = f" (ID: {ilan_id})" if ilan_id is not None else ""
        if table != "ilanlar":
            extra += f" [{table}]"
        return f"🌐 <b>Sitede:</b> VAR ✅{extra}"
    if ex is False:
        return "🌐 <b>Sitede:</b> YOK ❌"
    # None / bilinmiyor
    err = exists_resp.get("error") or "api_error"
    status = exists_resp.get("_http_status")
    if status:
        return f"🌐 <b>Sitede:</b> BİLİNMİYOR ⚠️ (API HATA: {err}, HTTP {status})"
    return f"🌐 <b>Sitede:</b> BİLİNMİYOR ⚠️ (API HATA: {err})"



def send_real_admin_deleted(kod: str, title: str, fiyat: str):
    ex = site_exists(kod)
    msg = "🗑️ <b>İLAN SİLİNDİ</b>\n\n"
    msg += f"📋 {kod}\n"
    msg += f"🏷️ {title}\n"
    msg += f"💰 {fiyat}\n\n"
    msg += _site_status_line(ex)

    if ex.get("exists") is True:
        kb = _kb([[("✅ SİL", f"site_del:{kod}"), ("❌ SİLME", f"site_cancel:{kod}")]])
        send_message(msg, chat_id=REAL_ADMIN_CHAT_ID, reply_markup=kb)
    else:
        send_message(msg, chat_id=REAL_ADMIN_CHAT_ID)


def send_real_admin_price_change(kod: str, title: str, eski_fiyat: str, yeni_fiyat: str):
    ex = site_exists(kod)
    msg = "💸 <b>FİYAT DEĞİŞTİ</b>\n\n"
    msg += f"📋 {kod}\n"
    msg += f"🏷️ {title}\n"
    msg += f"🔻 Eski: <b>{eski_fiyat}</b>\n"
    msg += f"🔺 Yeni: <b>{yeni_fiyat}</b>\n\n"
    msg += _site_status_line(ex)

    if ex.get("exists") is True:
        yeni_digits = normalize_price(yeni_fiyat)[:24]
        kb = _kb([[("✅ DEĞİŞTİR", f"site_price:{kod}:{yeni_digits}"),
                   ("❌ DEĞİŞTİRME", f"site_cancel:{kod}")]])
        send_message(msg, chat_id=REAL_ADMIN_CHAT_ID, reply_markup=kb)
    else:
        send_message(msg, chat_id=REAL_ADMIN_CHAT_ID)


def send_real_admin_new_listing(kod: str, title: str, fiyat: str, link: str):
    """Gerçek admin için: yeni ilan geldiğinde otomatik işlem yapma, butonla onay iste."""
    ex = site_exists(kod)
    msg = "🏠 <b>YENİ İLAN</b>\n\n"
    msg += f"📋 {kod}\n"
    msg += f"🏷️ {title}\n"
    msg += f"💰 {fiyat}\n\n"
    msg += f"🔗 {link}\n\n"
    msg += _site_status_line(ex)

    if ex.get("exists") is False:
        msg += "\n➕ <b>Siteye ekleme:</b> ONAY BEKLENİYOR ⏳"
        kb = _kb([[("✅ EKLE", f"site_add:{kod}"), ("❌ EKLEME", f"site_cancel:{kod}")]])
        send_message(msg, chat_id=REAL_ADMIN_CHAT_ID, reply_markup=kb)
        return

    if ex.get("exists") is True:
        msg += "\n➕ <b>Siteye ekleme:</b> Atlandı (zaten var) ✅"
    else:
        msg += "\n➕ <b>Siteye ekleme:</b> Atlandı (site durumu bilinmiyor) ⚠️"

    send_message(msg, chat_id=REAL_ADMIN_CHAT_ID)

def handle_callback_query(cb: dict):
    """Inline buton tıklamaları.
    
    ÖNEMLİ: Telegram callback query sadece BİR KEZ cevaplanabilir.
    İşlem sonrası bildirim için send_message kullanılmalı.
    """
    cb_id = cb.get("id")
    callback_answered = False
    
    def safe_answer(text: str = None, show_alert: bool = False):
        """Callback'i sadece bir kez cevapla."""
        nonlocal callback_answered
        if callback_answered:
            return
        callback_answered = True
        answer_callback_query(cb_id, text, show_alert)
    
    try:
        data = cb.get("data", "") or ""
        msg_obj = cb.get("message", {}) or {}
        chat_id = str((msg_obj.get("chat") or {}).get("id", ""))
        message_id = msg_obj.get("message_id")

        # Sadece gerçek adminin butonlarını kabul et
        if chat_id != str(REAL_ADMIN_CHAT_ID):
            safe_answer("Bu buton sadece admin içindir.")
            return

        if not data:
            safe_answer("Geçersiz işlem.")
            return

        parts = data.split(":")
        action = parts[0]
        kod = parts[1] if len(parts) > 1 else ""

        def _clear_buttons():
            try:
                if message_id:
                    edit_message_reply_markup(chat_id, message_id, None)
            except Exception as e:
                print(f"[CALLBACK] buton kaldırma hatası: {e}", flush=True)

        if action == "site_cancel":
            _clear_buttons()
            safe_answer("İşlem iptal edildi.")
            return

        if kod == "":
            safe_answer("İlan kodu yok.")
            return

        if action == "site_add":
            # Önce hemen cevapla (10 saniye limiti için)
            safe_answer("Ekleniyor... ⏳")
            
            # İlan kodunu düzgün formata çevir (ML-XXXX-XX)
            if not kod.upper().startswith("ML-"):
                kod_full = f"ML-{kod}"
            else:
                kod_full = kod.upper()
            
            # NOT: URL gönderMİYORUZ - scraper kendisi arama yapıp yeni format URL'yi bulacak
            # Eski format (ilandetay?ilan_kodu=) artık 404 veriyor
            r = call_site_api("add", ilan_kodu=kod_full, kimden="Web siteden")
            
            # Sonucu bildir
            if r.get("success"):
                if r.get("inserted"):
                    _clear_buttons()  # Sadece butonları kaldır, mesaj gönderme
                elif r.get("already_exists"):
                    _clear_buttons()
                    send_message(f"⚠️ <b>İLAN ZATEN MEVCUT</b>\n\n📋 {kod_full}\n💡 Sitede zaten kayıtlı.", chat_id=chat_id)
                else:
                    send_message(f"⚠️ <b>BEKLENMEDİK SONUÇ</b>\n\n📋 {kod_full}\n📄 Yanıt: {str(r)[:300]}", chat_id=chat_id)
            else:
                # Hata detayını göster - DEBUG: tam yanıtı göster
                error_msg = r.get("error", "bilinmiyor")
                
                # Tüm yanıtı string olarak al (debug için)
                full_response = str(r)[:500]
                
                # Scraper hatası ise daha detaylı göster
                if error_msg == "scraper_failed":
                    detail = r.get("detail", {})
                    scraper_resp = r.get("scraper", {})
                    
                    # Hata mesajını bul
                    error_text = ""
                    if isinstance(detail, dict):
                        error_text = detail.get("error", "") or detail.get("message", "")
                        resp = detail.get("resp", {})
                        if isinstance(resp, dict) and resp.get("message"):
                            error_text = resp.get("message")
                    elif isinstance(scraper_resp, dict):
                        error_text = scraper_resp.get("message", "") or scraper_resp.get("error", "")
                    
                    send_message(f"❌ <b>EKLEME BAŞARISIZ</b>\n\n📋 {kod_full}\n⚠️ Scraper: {error_text}\n\n� DEBUG:\n<code>{full_response}</code>", chat_id=chat_id)
                else:
                    # Diğer hatalar
                    send_message(f"❌ <b>EKLEME BAŞARISIZ</b>\n\n📋 {kod_full}\n⚠️ Hata: {error_msg}\n\n� DEBUG:\n<code>{full_response}</code>", chat_id=chat_id)
            return

        if action == "site_price":
            if len(parts) < 3:
                safe_answer("Yeni fiyat yok.")
                return
            new_price = parts[2]
            
            # Önce hemen cevapla
            safe_answer("Fiyat güncelleniyor... ⏳")
            
            r = call_site_api("update_price", ilan_kodu=kod, new_price=new_price)
            if r.get("success") and r.get("updated"):
                _clear_buttons()
            return

        if action == "site_del":
            # Önce hemen cevapla
            safe_answer("Siliniyor... ⏳")
            
            r = call_site_api("delete", ilan_kodu=kod, reason="Bot: ilan silindi")
            if r.get("success") and r.get("deleted"):
                _clear_buttons()
            return

        safe_answer("Bilinmeyen işlem.")

    except Exception as e:
        print(f"[CALLBACK] Hata: {e}", flush=True)
        if not callback_answered:
            try:
                answer_callback_query(cb_id, "Hata oluştu.")
            except Exception:
                pass

def get_updates(offset=None):
    try:
        url = "https://api.telegram.org/bot" + BOT_TOKEN + "/getUpdates"
        params = {"timeout": 1, "limit": 10}
        if offset:
            params["offset"] = offset
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except:
        return []


def normalize_price(fiyat):
    return "".join(c for c in fiyat if c.isdigit())


def _kb(rows):
    """Inline keyboard helper.
    rows = [[(text, callback_data), ...], ...]
    """
    return {
        "inline_keyboard": [
            [{"text": t, "callback_data": d} for (t, d) in row]
            for row in rows
        ]
    }



def github_get_file(filename):
    """GitHub'dan dosya oku (Contents API). JSON ise parse edip döndürür.
    Dönüş: (parsed_content_or_None, sha_or_None)
    """
    if not GITHUB_TOKEN:
        return None, None

    try:
        url = "https://api.github.com/repos/" + GITHUB_REPO + "/contents/" + filename.lstrip("/")

        headers = {
            "Authorization": "Bearer " + GITHUB_TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": "railway-makrolife-bot"
        }

        resp = requests.get(url, headers=headers, timeout=20)

        if resp.status_code != 200:
            # 404/401 vb. durumlarda sessizce None döndür
            print(f"[GITHUB] Okuma basarisiz: {resp.status_code} {resp.text[:200]}", flush=True)
            return None, None

        data = resp.json()
        sha = data.get("sha")

        # Bazı durumlarda 'content' gelmeyebilir (buyuk dosya vb.), download_url ile indir.
        raw_text = None
        if data.get("type") == "file":
            if data.get("content") and data.get("encoding") == "base64":
                try:
                    raw_bytes = base64.b64decode(data["content"])
                    raw_text = raw_bytes.decode("utf-8", errors="replace")
                except Exception as e:
                    print(f"[GITHUB] Base64 decode hatasi: {e}", flush=True)
                    raw_text = None

            if raw_text is None and data.get("download_url"):
                try:
                    dresp = requests.get(
                        data["download_url"],
                        headers={"Authorization": "Bearer " + GITHUB_TOKEN, "User-Agent": "railway-makrolife-bot"},
                        timeout=20
                    )
                    if dresp.status_code == 200:
                        raw_text = dresp.text
                    else:
                        print(f"[GITHUB] download_url okuma basarisiz: {dresp.status_code}", flush=True)
                except Exception as e:
                    print(f"[GITHUB] download_url okuma hatasi: {e}", flush=True)

        if raw_text is None:
            return None, sha

        # JSON parse (BOM/whitespace/null temizliği)
        cleaned = raw_text.lstrip("\ufeff").replace("\x00", "").strip()
        try:
            parsed = json.loads(cleaned) if cleaned else None
        except Exception as e:
            # JSON bozuksa yine sha döndür; parsed None
            print(f"[GITHUB] JSON parse hatasi ({filename}): {e}", flush=True)
            parsed = None

        return parsed, sha

    except Exception as e:
        print("[GITHUB] Okuma hatasi: " + str(e), flush=True)
        return None, None



def github_save_file(filename, content, sha=None):
    if not GITHUB_TOKEN:
        return False

    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        headers = {
            "Authorization": "token " + GITHUB_TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": "railway-makrolife-bot"
        }

        content_b64 = base64.b64encode(
            json.dumps(content, ensure_ascii=False, indent=2).encode()
        ).decode()

        data = {
            "message": "Update " + filename + " - " + get_turkey_time().strftime("%Y-%m-%d %H:%M") + " [skip deploy]",
            "content": content_b64,
            "branch": "main"
        }

        if sha:
            data["sha"] = sha

        resp = requests.put(url, headers=headers, json=data, timeout=20)

        if resp.status_code in (200, 201):
            print(f"[GITHUB] {filename} kaydedildi", flush=True)
            return True
        elif resp.status_code == 422:
            print(f"[GITHUB] Dosya mevcut, sha aliniyor...", flush=True)
            _, existing_sha = github_get_file(filename)
            if existing_sha:
                data["sha"] = existing_sha
                resp = requests.put(url, headers=headers, json=data, timeout=20)
                if resp.status_code in (200, 201):
                    print(f"[GITHUB] {filename} kaydedildi (retry)", flush=True)
                    return True
            print(f"[GITHUB] Kayit hatasi: {resp.status_code} {resp.text}", flush=True)
            return False
        else:
            print(f"[GITHUB] Kayit hatasi: {resp.status_code} {resp.text}", flush=True)
            return False

    except Exception as e:
        print(f"[GITHUB] Kayit hatasi: {e}", flush=True)
        return False


def load_last_scan_time():
    """Son tarama zamanini hem GitHub hem lokalden kontrol et, en yenisini al."""
    timestamps = []
    
    # 1. GitHub state
    state = load_state()
    github_timestamp = state.get("last_scan_timestamp", 0)
    timestamps.append(github_timestamp)
    
    # 2. Lokal dosya
    if os.path.exists(LAST_SCAN_FILE):
        try:
            with open(LAST_SCAN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                timestamps.append(data.get("last_scan_time", 0))
        except:
            pass
            
    return max(timestamps) if timestamps else 0


def save_last_scan_time(timestamp):
    """Son tarama zamanini hem lokal hem GitHub state'ine kaydet"""
    # Lokal dosyaya kaydet (eski uyumluluk)
    try:
        with open(LAST_SCAN_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_scan_time": timestamp}, f)
    except Exception as e:
        print("[LAST_SCAN] Lokal kayit hatasi: " + str(e), flush=True)
    
    # GitHub state'ine de kaydet (container restart koruması)
    # NOT: Bu save_state'ten sonra çağrılacak, state zaten güncellenmiş olmalı


def load_state(force_refresh=False):
    """State'i GitHub'daki ilanlar.json dosyasından oku.
    NOT: Railway /data/ilanlar.json sadece cache olarak yazılabilir; kaynak GitHub'dır.
    """
    global STATE_CACHE, STATE_GITHUB_SHA

    # Cache kullan (komutlar çok sık load_state çağırıyor)
    if (not force_refresh) and isinstance(STATE_CACHE, dict) and STATE_CACHE.get("items") is not None:
        return STATE_CACHE

    # GitHub ana kaynak
    if GITHUB_TOKEN:
        state, sha = github_get_file("ilanlar.json")
        if isinstance(state, dict) and state.get("items") is not None:
            STATE_GITHUB_SHA = sha
            STATE_CACHE = state
            # Railway cache'e yaz (okuma kaynağı değil, sadece yedek)
            save_state_local(state)
            print("[STATE] GitHub ANA kaynak kullanılıyor", flush=True)
            return state

        # GitHub okunamadıysa cache varsa onu kullan
        if isinstance(STATE_CACHE, dict) and STATE_CACHE.get("items") is not None:
            print("[STATE] GitHub okunamadi, RAM cache kullaniliyor", flush=True)
            return STATE_CACHE

        # Cache de yoksa sıfır state döndür (botun çökmesini engellemek için)
        print("[STATE] GitHub okunamadi, yeni state olusturuldu (lokal state KULLANILMADI)", flush=True)
        send_message("⚠️ <b>UYARI</b>\n\nGitHub'dan ilanlar.json okunamadi. Yeni state ile devam ediliyor (lokal state kullanılmadı).")
        STATE_CACHE = {
            "cycle_start": get_turkey_time().strftime("%Y-%m-%d"),
            "items": {},
            "reported_days": [],
            "first_run_done": False,
            "daily_stats": {},
            "scan_sequence": 0
        }
        return STATE_CACHE

    # Token yoksa: eski davranış (lokal cache -> yeni state)
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                print("[STATE] Lokal cache kullanılıyor (GITHUB_TOKEN yok)", flush=True)
                STATE_CACHE = state
                return state
        except Exception as e:
            print("[STATE] Lokal okuma hatası:", e, flush=True)

    print("[STATE] Yeni state oluşturuldu (GITHUB_TOKEN yok)", flush=True)
    STATE_CACHE = {
        "cycle_start": get_turkey_time().strftime("%Y-%m-%d"),
        "items": {},
        "reported_days": [],
        "first_run_done": False,
        "daily_stats": {},
        "scan_sequence": 0
    }
    return STATE_CACHE



def save_state_local(state):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[STATE] Lokal kayit hatasi: " + str(e), flush=True)


def save_state(state):
    global STATE_CACHE, STATE_GITHUB_SHA

    # Lokal cache (opsiyonel)
    save_state_local(state)
    print("[STATE] Lokal kaydedildi - " + str(len(state.get("items", {}))) + " ilan", flush=True)

    # Cache'i güncelle
    STATE_CACHE = state

    # GitHub'a kaydet
    if GITHUB_TOKEN:
        sha = STATE_GITHUB_SHA
        if not sha:
            # Sadece sha almak için tekrar çek
            _, sha = github_get_file("ilanlar.json")
        ok = github_save_file("ilanlar.json", state, sha)
        # Başarılıysa sha'yı güncellemek için tekrar oku (sha değişir)
        if ok:
            _, new_sha = github_get_file("ilanlar.json")
            if new_sha:
                STATE_GITHUB_SHA = new_sha



def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"deleted": [], "price_changes": [], "new": []}


def save_history(history):
    try:
        for key in ["deleted", "price_changes", "new"]:
            if len(history.get(key, [])) > 1000:
                history[key] = history[key][-1000:]
        
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[HISTORY] Kayit hatasi: " + str(e), flush=True)


def format_duration(seconds):
    if seconds < 60:
        return str(int(seconds)) + " sn"
    elif seconds < 3600:
        return str(int(seconds // 60)) + " dk " + str(int(seconds % 60)) + " sn"
    else:
        return str(int(seconds // 3600)) + " sa " + str(int((seconds % 3600) // 60)) + " dk"


def format_number(num):
    return "{:,}".format(num).replace(",", ".")


def handle_command(chat_id, command, message_text):
    state = load_state()
    history = load_history()
    now = get_turkey_time()
    today = now.strftime("%Y-%m-%d")
    
    print("[KOMUT] " + str(chat_id) + ": " + command, flush=True)
    
    global AUTO_SCAN_ENABLED
    
    if command == "/aktif":
        AUTO_SCAN_ENABLED = True
        # State'e kaydet (kalıcı olsun)
        state["auto_scan_enabled"] = True
        save_state(state)
        send_message("✅ <b>Otomatik Tarama AKTİF edildi.</b>\nBot belirtilen aralıklarla tarama yapmaya devam edecek.", chat_id)
        
    elif command == "/pasif" or command == "/dur":
        AUTO_SCAN_ENABLED = False
        # State'e kaydet (kalıcı olsun)
        state["auto_scan_enabled"] = False
        save_state(state)
        send_message("⛔ <b>Otomatik Tarama PASİF edildi.</b>\nSiz tekrar /aktif diyene kadar veya /tara ile manuel komut verene kadar tarama yapılmayacak.", chat_id)

    elif command == "/start":
        interval = get_scan_interval() // 60
        msg = "<b>Makrolife Ilan Takip Botu</b>\n\n"
        msg += "Tarama araligi: " + str(interval) + " dk\n"
        msg += "Bellekteki ilan: " + str(len(state.get("items", {}))) + "\n\n"
        msg += "<b>Komutlar:</b>\n"
        msg += "/durum - Bot durumu\n"
        msg += "/istatistik - Detayli istatistikler\n"
        msg += "/bellek - Bellek durumu\n"
        msg += "/bugun - Bugunku aktiviteler\n"
        msg += "/hafta - Son 7 gun\n"
        msg += "/son [sayi] - Son ilanlar\n"
        msg += "/ara [kelime] - Ilan ara\n"
        msg += "/ucuz [sayi] - En ucuz ilanlar\n"
        msg += "/pahali [sayi] - En pahali ilanlar\n"
        msg += "/silinenler - Silinen ilanlar\n"
        msg += "/degisimler - Fiyat degisimleri\n"
        msg += "/tara - Manuel tarama\n"
        msg += "/ozellikler - Bot ozellikleri\n"
        msg += "/yardim - Yardim"
        send_message(msg, chat_id)
    
    elif command == "/yardim" or command == "/help":
        msg = "<b>Makrolife Ilan Takip Botu</b>\n\n"
        msg += "<b>Kullanilabilir komutlar:</b>\n\n"
        msg += "<b>Istatistikler</b>\n"
        msg += "/durum - Bot durumu ve ozet bilgiler\n"
        msg += "/istatistik - Detayli istatistikler\n"
        msg += "/bellek - Bellekteki ilan sayisi\n"
        msg += "/bugun - Bugunku aktiviteler\n"
        msg += "/hafta - Son 7 gunluk ozet\n\n"
        msg += "<b>Arama</b>\n"
        msg += "/ara [kelime] - Ilan ara\n"
        msg += "/son [sayi] - Son eklenen ilanlar\n"
        msg += "/ucuz [sayi] - En ucuz ilanlar\n"
        msg += "/pahali [sayi] - En pahali ilanlar\n\n"
        msg += "<b>Yonetim</b>\n"
        msg += "/tara - Manuel tarama baslat"
        send_message(msg, chat_id)
    
    elif command == "/ozellikler" or command == "/features":
        msg = "<b>🤖 Bot Ozellikleri</b>\n\n"
        msg += "<b>📊 Tarama Sistemi:</b>\n"
        msg += "• Otomatik tarama (45-120 dk aralikla)\n"
        msg += "• 53+ sayfa tarama kapasitesi\n"
        msg += "• Akilli timeout yonetimi (25 dk)\n"
        msg += "• Container restart sonrasi sureyi hatirlar\n\n"
        msg += "<b>🔔 Bildirimler:</b>\n"
        msg += "• Yeni ilan (tüm sayfalar)\n"
        msg += "• Fiyat degisiklikleri\n"
        msg += "• Silinen ilanlar\n"
        msg += "• Gunluk ozet (23:30)\n\n"
        msg += "<b>💾 Veri Yonetimi:</b>\n"
        msg += "• Lokal + GitHub yedekleme\n"
        msg += "• 30 gunluk dongu sistemi\n"
        msg += "• Gecmis kayitlari (1000 kayit)\n"
        msg += "• Gunluk istatistikler\n\n"
        msg += "<b>🔍 Arama & Filtreleme:</b>\n"
        msg += "• Kelime bazli arama\n"
        msg += "• Fiyat siralama (ucuz/pahali)\n"
        msg += "• Tarih bazli listeleme\n"
        msg += "• Haftalik raporlar\n\n"
        msg += "<b>⚙️ Teknik:</b>\n"
        msg += "• Platform: Railway\n"
        msg += "• Scraping: Playwright\n"
        msg += "• API: Telegram Bot\n"
        msg += "• Yedek: GitHub API"
        send_message(msg, chat_id)
    
    elif command == "/durum" or command == "/status":
        uptime = ""
        if bot_stats["start_time"]:
            uptime = format_duration((datetime.utcnow() - bot_stats["start_time"]).total_seconds())
        
        interval = get_scan_interval() // 60
        next_scan = "Bilinmiyor"
        if bot_stats["last_scan_time"]:
            next_time = bot_stats["last_scan_time"] + timedelta(seconds=get_scan_interval())
            remaining = (next_time - datetime.utcnow()).total_seconds()
            next_scan = format_duration(remaining) if remaining > 0 else "Simdi"
        
        msg = "<b>Bot Durumu</b>\n\n"
        msg += "Aktif | " + uptime + "\n"
        msg += now.strftime("%H:%M:%S") + " (TR)\n\n"
        msg += "Bellek: " + format_number(len(state.get("items", {}))) + " ilan\n"
        msg += "Tarama araligi: " + str(interval) + " dk\n"
        msg += "Sonraki: " + next_scan + "\n\n"
        msg += "Toplam tarama: " + str(bot_stats["total_scans"]) + "\n"
        msg += "Son tarama: " + str(bot_stats["last_scan_pages"]) + " sayfa\n"
        msg += "Son sure: " + format_duration(bot_stats["last_scan_duration"]) + "\n"
        msg += "Timeout: " + str(bot_stats["timeouts"]) + " | Hata: " + str(bot_stats["errors"])
        send_message(msg, chat_id)
    
    elif command == "/istatistik" or command == "/stats":
        items = state.get("items", {})
        prices = [int(normalize_price(v.get("fiyat", "0"))) for v in items.values() if normalize_price(v.get("fiyat", "0"))]
        
        avg_price = sum(prices) // len(prices) if prices else 0
        min_price = min(prices) if prices else 0
        max_price = max(prices) if prices else 0
        
        msg = "<b>Istatistikler</b>\n\n"
        msg += "Toplam: " + format_number(len(items)) + " ilan\n"
        msg += "Ortalama: " + format_number(avg_price) + " TL\n"
        msg += "En dusuk: " + format_number(min_price) + " TL\n"
        msg += "En yuksek: " + format_number(max_price) + " TL\n\n"
        msg += "Yeni bulunan: " + str(bot_stats["total_new_listings"]) + "\n"
        msg += "Fiyat degisimi: " + str(bot_stats["total_price_changes"]) + "\n"
        msg += "Silinen: " + str(bot_stats["total_deleted"])
        send_message(msg, chat_id)
    
    elif command == "/bellek" or command == "/memory":
        items = state.get("items", {})
        file_size = os.path.getsize(DATA_FILE) if os.path.exists(DATA_FILE) else 0
        
        github_status = "Aktif" if GITHUB_TOKEN else "Kapali"
        
        msg = "<b>Bellek</b>\n\n"
        msg += "Dosya: " + str(round(file_size/1024, 1)) + " KB\n"
        msg += "Ilan: " + format_number(len(items)) + "\n"
        msg += "Dongu: " + state.get("cycle_start", "-") + "\n"
        msg += "Ilk calisma: " + ("Evet" if state.get("first_run_done") else "Hayir") + "\n\n"
        msg += "GitHub yedek: " + github_status
        send_message(msg, chat_id)
    
    elif command == "/bugun" or command == "/today":
        items = state.get("items", {})
        daily = state.get("daily_stats", {}).get(today, {})
        
        # Sitedeki sıraya göre sırala (position küçük = daha yeni)
        all_items = [(k, v) for k, v in items.items()]
        all_items.sort(key=lambda x: x[1].get("position", 999999))
        
        msg = "<b>Bugun</b> (" + today + ")\n\n"
        msg += "Yeni: " + str(daily.get("new", 0)) + "\n"
        msg += "Fiyat degisimi: " + str(daily.get("price_changes", 0)) + "\n"
        msg += "Silinen: " + str(daily.get("deleted", 0)) + "\n"
        
        if all_items[:5]:
            msg += "\n<b>Son eklenenler:</b>\n"
            for kod, item in all_items[:5]:
                msg += kod + " - " + item.get("fiyat", "-") + "\n"
        
        send_message(msg, chat_id)
    
    elif command == "/hafta" or command == "/week":
        daily_stats = state.get("daily_stats", {})
        
        days_tr = {"Monday": "Pzt", "Tuesday": "Sal", "Wednesday": "Car", 
                   "Thursday": "Per", "Friday": "Cum", "Saturday": "Cmt", "Sunday": "Paz"}
        
        msg = "<b>Son 7 Gun</b>\n\n"
        for i in range(7):
            date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            day = days_tr.get((now - timedelta(days=i)).strftime("%A"), "")
            
            # daily_stats'tan verileri al
            stats = daily_stats.get(date, {})
            new_count = stats.get("new", 0)
            price_changes = stats.get("price_changes", 0)
            deleted = stats.get("deleted", 0)
            
            label = "Bugun" if i == 0 else day + " " + date[5:]
            msg += label + ": Yeni:" + str(new_count) + " Fiyat:" + str(price_changes) + " Silinen:" + str(deleted) + "\n"
        
        send_message(msg, chat_id)
    
    elif command.startswith("/silinenler"):
        deleted = history.get("deleted", [])[-10:]
        if deleted:
            msg = "<b>Son Silinen Ilanlar</b>\n\n"
            for item in reversed(deleted):
                msg += "<b>" + item.get("kod", "-") + "</b>\n"
                msg += "  " + item.get("title", "")[:40] + "\n"
                msg += "  " + item.get("fiyat", "-") + " | " + item.get("tarih", "-") + "\n\n"
        else:
            msg = "Henuz silinen ilan yok."
        send_message(msg, chat_id)
    
    elif command.startswith("/degisimler"):
        changes = history.get("price_changes", [])[-10:]
        if changes:
            msg = "<b>Son Fiyat Degisimleri</b>\n\n"
            for item in reversed(changes):
                msg += "<b>" + item.get("kod", "-") + "</b>\n"
                msg += "  " + item.get("eski_fiyat", "-") + " -> " + item.get("yeni_fiyat", "-") + "\n"
                msg += "  " + item.get("tarih", "-") + "\n\n"
        else:
            msg = "Henuz fiyat degisimi yok."
        send_message(msg, chat_id)
    
    elif command.startswith("/ara"):
        parts = message_text.split(None, 1)
        if len(parts) < 2:
            send_message("Kullanim: /ara kelime", chat_id)
            return None
        
        keyword = parts[1].lower()
        items = state.get("items", {})
        results = [(k, v) for k, v in items.items() 
                   if keyword in v.get("title", "").lower() or keyword in k.lower()]
        
        if results:
            msg = "<b>" + str(len(results)) + " sonuc</b> (" + keyword + ")\n\n"
            for kod, item in results[:10]:
                msg += "<b>" + kod + "</b>\n"
                msg += "🏷️ " + item.get("title", "")[:50] + "\n"
                msg += "💰 " + item.get("fiyat", "-") + "\n"
                msg += "🔗 " + item.get("link", "-") + "\n\n"
            if len(results) > 10:
                msg += "... +" + str(len(results)-10) + " sonuc daha"
        else:
            msg = "'" + keyword + "' bulunamadi."
        send_message(msg, chat_id)
    
    elif command.startswith("/son"):
        parts = message_text.split()
        count = min(int(parts[1]), 20) if len(parts) > 1 and parts[1].isdigit() else 5
        
        items = state.get("items", {})
        # Sitedeki sıraya göre sırala (position küçük = daha yeni)
        sorted_items = sorted(
            items.items(),
            key=lambda x: x[1].get("position", 999999)
        )[:count]
        
        msg = "<b>Son " + str(count) + " Eklenen İlan</b>\n\n"
        for kod, item in sorted_items:
            msg += "<b>" + kod + "</b> (" + item.get("tarih", "") + ")\n"
            msg += "  " + item.get("title", "")[:35] + "\n"
            msg += "  " + item.get("fiyat", "-") + "\n\n"
        send_message(msg, chat_id)
    
    elif command.startswith("/ucuz"):
        parts = message_text.split()
        count = min(int(parts[1]), 15) if len(parts) > 1 and parts[1].isdigit() else 10
        
        items = state.get("items", {})
        priced = [(k, v, int(normalize_price(v.get("fiyat", "0")))) 
                  for k, v in items.items() if normalize_price(v.get("fiyat", "0"))]
        sorted_items = sorted(priced, key=lambda x: x[2])[:count]
        
        msg = "<b>En Ucuz " + str(count) + "</b>\n\n"
        for kod, item, _ in sorted_items:
            msg += "<b>" + kod + "</b>\n  " + item.get("title", "")[:35] + "\n  " + item.get("fiyat", "-") + "\n\n"
        send_message(msg, chat_id)
    
    elif command.startswith("/pahali"):
        parts = message_text.split()
        count = min(int(parts[1]), 15) if len(parts) > 1 and parts[1].isdigit() else 10
        
        items = state.get("items", {})
        priced = [(k, v, int(normalize_price(v.get("fiyat", "0")))) 
                  for k, v in items.items() if normalize_price(v.get("fiyat", "0"))]
        sorted_items = sorted(priced, key=lambda x: x[2], reverse=True)[:count]
        
        msg = "<b>En Pahali " + str(count) + "</b>\n\n"
        for kod, item, _ in sorted_items:
            msg += "<b>" + kod + "</b>\n  " + item.get("title", "")[:35] + "\n  " + item.get("fiyat", "-") + "\n\n"
        send_message(msg, chat_id)
    
    elif command == "/tara":
        global MANUAL_SCAN_LIMIT, WAITING_PAGE_CHOICE
        WAITING_PAGE_CHOICE = False
        MANUAL_SCAN_LIMIT = None  # TÜM SAYFALAR

        send_message(
            "✅ <b>Manuel tarama başlatılıyor</b>\n\n"
            "📄 Sayfa limiti: <b>TÜMÜ</b>",
            chat_id
        )
        return "SCAN"
        
    elif command == "/toplu_ekle":
        send_message("🔄 <b>Toplu ekleme başlatılıyor...</b>\n\nTüm ilanlar siteye ekleniyor. Bu işlem 10-15 dakika sürebilir.", chat_id)
        
        # Bellekteki tüm ilanları al
        items = state.get("items", {})
        total = len(items)
        success_count = 0
        fail_count = 0
        already_exists_count = 0
        
        send_message(f"📊 <b>{total} ilan bulundu</b>\n\nEkleme işlemi başladı...", chat_id)
        
        for idx, (kod, item) in enumerate(items.items(), 1):
            link = item.get("link", f"https://www.makrolife.com.tr/ilandetay?ilan_kodu={kod}")
            
            # Website API'ye ekle
            r = call_site_api("add", ilan_kodu=kod, url=link, kimden="Web siteden")
            
            if r.get("success"):
                if r.get("already_exists"):
                    already_exists_count += 1
                else:
                    success_count += 1
            else:
                fail_count += 1
            
            # Her 50 ilandan bir ilerleme bildir
            if idx % 50 == 0:
                progress_msg = f"📈 <b>İlerleme: {idx}/{total}</b>\n\n"
                progress_msg += f"✅ Eklenen: {success_count}\n"
                progress_msg += f"⏭️ Zaten var: {already_exists_count}\n"
                progress_msg += f"❌ Hata: {fail_count}"
                send_message(progress_msg, chat_id)
            
            # Rate limiting için kısa bekleme
            time.sleep(0.2)
        
        # Sonuç özeti
        final_msg = "✅ <b>Toplu ekleme tamamlandı!</b>\n\n"
        final_msg += f"📊 Toplam: {total} ilan\n"
        final_msg += f"✅ Başarıyla eklendi: {success_count}\n"
        final_msg += f"⏭️ Zaten vardı: {already_exists_count}\n"
        final_msg += f"❌ Hata: {fail_count}"
        send_message(final_msg, chat_id)
        
    elif command == "/durdur":
        global SCAN_STOP_REQUESTED
        if ACTIVE_SCAN:
            SCAN_STOP_REQUESTED = True
            send_message("⛔ <b>Tarama durduruluyor...</b>", chat_id)
        else:
            send_message("ℹ️ Aktif tarama yok.", chat_id)


    
    else:
        send_message("Bilinmeyen komut. /yardim yazin.", chat_id)
    
    return None


def check_telegram_commands():
    global last_update_id, MANUAL_SCAN_LIMIT, WAITING_PAGE_CHOICE

    updates = get_updates(last_update_id + 1 if last_update_id else None)

    result = None
    for update in updates:
        last_update_id = update.get("update_id", last_update_id)

        # Inline buton tıklaması
        if "callback_query" in update:
            handle_callback_query(update.get("callback_query") or {})
            continue

        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "")

        if not text or not chat_id:
            continue

        # Sadece admin'den komut al
        if chat_id != str(ADMIN_CHAT_ID):
            continue

        if text.startswith("/"):
            command = text.split()[0].lower()
            cmd_result = handle_command(chat_id, command, text)
            if cmd_result == "SCAN":
                result = "SCAN"

    return result

def fetch_listings_playwright():
    global SCAN_STOP_REQUESTED, ACTIVE_SCAN


    ACTIVE_SCAN = True
    SCAN_STOP_REQUESTED = False

    scan_start = time.time()

    # === 1. FLARESOLVERR İLE DENEME (En güçlü yöntem) ===
    if USE_FLARESOLVERR and FLARESOLVERR_URL:
        print("[FLARESOLVERR] Öncelikli yöntem olarak deneniyor...", flush=True)
        flare_result = fetch_listings_via_flaresolverr()
        if flare_result is not None:
            ACTIVE_SCAN = False
            return flare_result
        print("[FLARESOLVERR] Başarısız, Google Proxy deneniyor...", flush=True)

    # === 2. GOOGLE PROXY İLE DENEME (Cloudflare Bypass) ===
    if USE_GOOGLE_PROXY:
        print("[GOOGLE_PROXY] Deneniyor...", flush=True)
        google_result = fetch_listings_via_google_proxy()
        if google_result is not None:
            ACTIVE_SCAN = False
            return google_result
        print("[GOOGLE_PROXY] Başarısız, Playwright'a geçiliyor...", flush=True)

    print("[PLAYWRIGHT] Baslatiliyor...", flush=True)

    results = []
    seen_codes = set()
    page_num = 0
    consecutive_failures = 0
    MAX_FAILURES = 3

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials",
                "--disable-features=BlockInsecurePrivateNetworkRequests",
                "--ignore-certificate-errors",
                "--allow-running-insecure-content",
                "--disable-extensions",
                "--disable-plugins-discovery",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--metrics-recording-only",
                "--no-first-run",
                "--safebrowsing-disable-auto-update",
            ],
        )

        def new_context():
            return browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                extra_http_headers={
                    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                }
            )

        context = new_context()
        page = context.new_page()
        stealth_sync(page)  # Apply stealth mode to bypass detection
        print("[PLAYWRIGHT] Stealth mode uygulandi", flush=True)

        while True:
            if SCAN_STOP_REQUESTED:
                print("[PLAYWRIGHT] Kullanıcı durdurdu", flush=True)
                send_message("⛔ <b>Tarama kullanıcı tarafından durduruldu</b>")
                break

            if MANUAL_SCAN_LIMIT is not None and page_num >= MANUAL_SCAN_LIMIT:
                print("[PLAYWRIGHT] Manuel sayfa limiti doldu", flush=True)
                break

            page_num += 1
            if page_num == 1:
                page_url = URL
                print("[SAYFA " + str(page_num) + "] " + page_url, flush=True)
            else:
                page_url = URL
                print("[SAYFA " + str(page_num) + "] AJAX (Sayfa " + str(page_num) + ")", flush=True)

            success = False
            selector_found = False
            
            # Sayfa yükle - RETRY MANTİĞİ (3 deneme)
            MAX_PAGE_RETRIES = 3
            page_loaded = False
            
            for retry_attempt in range(MAX_PAGE_RETRIES):
                try:
                    if page_num == 1:
                        # Timeout: 90 saniye (önceki 60'tan artırıldı)
                        page.goto(page_url, timeout=90000, wait_until="networkidle")
                        
                        # Cloudflare challenge kontrolü ve beklemesi
                        if not wait_for_cloudflare(page):
                            print(f"[SAYFA {page_num}] Cloudflare challenge geçilemedi", flush=True)
                            raise TimeoutError("Cloudflare challenge timeout")
                    else:
                        # AJAX sayfalama
                        # Önce mevcut içeriğin bir parçasını alalım ki değişip değişmediğini anlayalım
                        old_content_hash = page.evaluate("document.querySelector('body').innerText.substring(0, 500)")
                        
                        # Sayfa scriptlerinin (sayfaDegistir vb) yüklenmesini bekle
                        try:
                            page.wait_for_function("typeof sayfaDegistir !== 'undefined'", timeout=10000)
                        except:
                            print(f"[SAYFA {page_num}] sayfaDegistir fonksiyonu bulunamadı, bekleniyor...", flush=True)

                        print(f"[SAYFA {page_num}] sayfaDegistir({page_num}) tetikleniyor...", flush=True)
                        try:
                            # Filtreyi daha geniş tutalım (ilan-sayfalama kelimesi geçsin yeter)
                            with page.expect_response(lambda r: "ilan-sayfalama" in r.url, timeout=20000) as response_info:
                                page.evaluate(f"if(typeof sayfaDegistir !== 'undefined') {{ sayfaDegistir({page_num}); }}")
                            print(f"[SAYFA {page_num}] AJAX yanıtı alındı: {response_info.value.status}", flush=True)
                        except:
                            print(f"[SAYFA {page_num}] AJAX yanıtı/fonksiyon zaman aşımı, manuel tıklama denenecek.", flush=True)
                            page.evaluate(f"if(typeof sayfaDegistir !== 'undefined') {{ sayfaDegistir({page_num}); }}")
                        
                        page.wait_for_timeout(5000)
                        new_content_hash = page.evaluate("document.querySelector('body').innerText.substring(0, 500)")
                        
                        if old_content_hash == new_content_hash:
                            print(f"[SAYFA {page_num}] sayfaDegistir etkisiz kaldı, JS-click deneniyor...", flush=True)
                            try:
                                # Overlay/Popup engellerini aşmak için önce kapatmayı deneyelim
                                page.evaluate("let closeBtn = document.querySelector('.img-popup-close'); if(closeBtn) closeBtn.click();")
                                
                                # Overlay engellerini aşmak için JS ile doğrudan elemana tıklayıp sayfalama AJAX'ını tetikleyelim
                                js_click = f"""
                                (function(num) {{
                                    let btn = document.querySelector('a.page-link[data-sayfa="' + num + '"]');
                                    if(!btn) {{
                                       // Alternatif: Metne göre bul
                                       let links = Array.from(document.querySelectorAll('a.page-link, a'));
                                       btn = links.find(a => a.innerText.trim() === String(num));
                                    }}
                                    if(btn) {{
                                        btn.click();
                                        return true;
                                    }}
                                    return false;
                                }})({page_num})
                                """
                                clicked = page.evaluate(js_click)
                                if clicked:
                                    print(f"[SAYFA {page_num}] JS-click başarılı, bekleniyor...", flush=True)
                                    page.wait_for_timeout(5000)
                                else:
                                    print(f"[SAYFA {page_num}] Buton bulunamadı.", flush=True)
                            except Exception as e:
                                print(f"[SAYFA {page_num}] JS-click hatası: {str(e)[:50]}", flush=True)
                        
                    page_loaded = True
                    break
                except TimeoutError:
                    if retry_attempt < MAX_PAGE_RETRIES - 1:
                        print("[SAYFA " + str(page_num) + "] Timeout - yeniden deneniyor (" + str(retry_attempt + 2) + "/" + str(MAX_PAGE_RETRIES) + ")", flush=True)
                        time.sleep(2)  # Kısa bekleme
                        
                        if page_num == 1:
                            # Context yenile
                            try:
                                page.close()
                                context.close()
                                context = new_context()
                                page = context.new_page()
                                stealth_sync(page)  # Apply stealth to new page
                            except:
                                pass
                    else:
                        print("[SAYFA " + str(page_num) + "] Sayfa yüklenemedi - " + str(MAX_PAGE_RETRIES) + " deneme başarısız", flush=True)
                except Exception as e:
                    if retry_attempt < MAX_PAGE_RETRIES - 1:
                        print("[SAYFA " + str(page_num) + "] Hata (" + str(e)[:50] + ") - yeniden deneniyor", flush=True)
                        time.sleep(2)
                    else:
                        print("[SAYFA " + str(page_num) + "] Sayfa yükleme hatası: " + str(e), flush=True)
            
            if not page_loaded:
                consecutive_failures += 1
                # İlk 3 sayfadan birine ulaşılamazsa taramayı tamamen iptal et
                if page_num <= 3:
                    error_msg = f"Sayfa {page_num}'e ulaşılamadı (3 defa denendi) - Web sitesi erişilemez durumda"
                    print(f"[PLAYWRIGHT] {error_msg}", flush=True)
                    browser.close()
                    ACTIVE_SCAN = False
                    return (None, error_msg)  # Hata bilgisi ile birlikte döndür
                if consecutive_failures >= MAX_FAILURES:
                    error_msg = f"Sayfa {page_num}'e ulaşılamadı (3 defa denendi) - Art arda 3 sayfa hatası"
                    print(f"[PLAYWRIGHT] {error_msg}", flush=True)
                    browser.close()
                    ACTIVE_SCAN = False
                    return (None, error_msg)  # Hata bilgisi ile birlikte döndür
                continue
            
            # İlan selector'ı ara (kısa timeout - boş sayfa tespiti için)
            # YENİ SELECTOR: Konteyner veya ilan linki (JS yüklenmemiş olsa bile konteyner vardır)
            try:
                # İlan/konteyner gelene kadar bekle
                page.wait_for_selector('.cb-list-item, .locationDiv, a[href*="/ilan/"]', timeout=20000)
                
                # POPUP KAPATMA - Data yüklenmesini engelleyebilir
                try:
                    page.evaluate("let closeBtn = document.querySelector('.img-popup-close'); if(closeBtn) closeBtn.click();")
                except:
                    pass
                
                # JS'nin verileri doldurması için daha fazla bekle (reCAPTCHA/Verification süreci için)
                print(f"[SAYFA {page_num}] Verilerin dolması bekleniyor (8 sn)...", flush=True)
                page.wait_for_timeout(8000) 
                
                selector_found = True
                success = True
            except TimeoutError:
                # KORUMA: İlk 10 sayfada boş = site hatası, son sayfa olamaz
                if page_num <= MIN_VALID_PAGES:
                    error_msg = f"Sayfa {page_num} boş geldi - site erişim hatası (ilk {MIN_VALID_PAGES} sayfada boş sayfa olamaz)"
                    print(f"[KORUMA] {error_msg}", flush=True)
                    browser.close()
                    ACTIVE_SCAN = False
                    return (None, error_msg)
                # Normal son sayfa tespiti (sayfa 10+)
                print("[SAYFA " + str(page_num) + "] Ilan bulunamadi - son sayfa gecildi, tarama bitti", flush=True)
                break
            except Exception as e:
                print("[SAYFA " + str(page_num) + "] Selector hatası: " + str(e), flush=True)
                break

            if not success:
                consecutive_failures += 1
                if consecutive_failures >= MAX_FAILURES:
                    error_msg = f"Sayfa {page_num} sonrası art arda 3 sayfa hatası - Tarama iptal edildi"
                    print(f"[PLAYWRIGHT] {error_msg}", flush=True)
                    browser.close()
                    ACTIVE_SCAN = False
                    return (None, error_msg)
                continue

            consecutive_failures = 0

            # DEBUG: Konsol çıktılarını yakala
            def handle_console(msg):
                if "[EXTRACT]" in msg.text:
                    print(f"[CONSOLE] {msg.text}", flush=True)

            page.on("console", handle_console)

            listings = page.evaluate(
                r"""() => {
                console.log("[EXTRACT] Basliyor...");
                const out = [];
                const seen = new Set();
                
                // Bot token kontrolü
                const botToken = window.__botToken || "YOK";
                console.log("[EXTRACT] Site Bot Token: " + botToken);

                // aria-hidden="true" olanlar template/gizli kutulardır, onları alma
                const items = Array.from(document.querySelectorAll('.cb-list-item, [data-token], .locationDiv'))
                                   .filter(el => el.getAttribute('aria-hidden') !== 'true');
                
                console.log("[EXTRACT] Bulunan aday eleman sayisi (filtered): " + items.length);

                items.forEach((el, index) => {
                    let kod = "";
                    
                    // 1. .ilan-kod-ph span/div (Sitenin yeni placeholder yapısı)
                    const kodElem = el.querySelector(".ilan-kod-ph");
                    if (kodElem) {
                        const kodText = (kodElem.textContent || "").trim();
                        const m = kodText.match(/ML-[A-Z0-9-]+/i);
                        if (m) {
                            kod = m[0].toUpperCase();
                        } else if (index < 3) {
                            console.log(`[EXTRACT] Kutuda kod bulunamadi [${index}], icerik: ${kodText}`);
                        }
                    }
                    
                    // 2. Text içeriği
                    if (!kod) {
                        const m2 = (el.textContent || "").match(/(ML-[A-Z0-9-]{3,})/i);
                        if (m2) kod = m2[0].toUpperCase();
                    }

                    // 3. Attribute (data-target-href içinden)
                    if (!kod) {
                        const href = el.getAttribute("data-target-href") || "";
                        const m3 = href.match(/(ML-[A-Z0-9-]{3,})/i);
                        if (m3) kod = m3[0].toUpperCase();
                    }
                    if (!kod) return;
                    if (seen.has(kod)) return;
                    seen.add(kod);

                    let fiyat = "Fiyat yok";
                    let title = kod;

                    const dataTitle = el.getAttribute("data-target-title");
                    if (dataTitle && dataTitle !== "#") {
                        title = dataTitle;
                    } else {
                        const h = el.querySelector("h1, h2, h3, h4, h5, h6, .ilan-baslik-ph, .ilan-baslik");
                        if (h) title = (h.textContent || "").trim().replace(/\s*-\s*ML-\d+-\d+\s*$/i, '');
                    }

                    const fElem = el.querySelector(".ilan-fiyat-ph, .ilan-fiyat, .text-primary");
                    if (fElem) {
                        fiyat = (fElem.textContent || "").trim();
                    }

                    let link = el.getAttribute("data-target-href");
                    if (!link || link === "#") {
                        const a = el.querySelector('a');
                        if (a) link = a.getAttribute("href");
                    }
                    
                    if (link && !link.startsWith("http")) {
                        link = "https://www.makrolife.com.tr" + (link.startsWith("/") ? "" : "/") + link;
                    }

                    out.push({
                        kod: kod,
                        fiyat: fiyat,
                        title: title,
                        link: link || `https://www.makrolife.com.tr/ilandetay?ilan_kodu=${kod}`
                    });
                });
                
                console.log("[EXTRACT] Bitti. Basariyla cekilen: " + out.length);
                return out;
            }"""
            )
            
            # İşlem bittiğinde listener'ı kaldır
            try:
                page.remove_listener("console", handle_console)
            except:
                pass

            if not listings:
                print("[SAYFA " + str(page_num) + "] Bos - tarama bitti", flush=True)
                break

            new_on_page = 0
            for item in listings:
                if item["kod"] not in seen_codes:
                    seen_codes.add(item["kod"])
                    new_on_page += 1
                    results.append(
                        (
                            item["kod"],
                            item["fiyat"],
                            item["link"],
                            item["title"],
                            page_num,
                        )
                    )
            
            if new_on_page == 0:
                print("[PLAYWRIGHT] Sayfada yeni ilan yok - döngü tespiti, tarama bitiriliyor", flush=True)
                break

            # İlerleme mesajı (sayfa bazlı)
            if page_num % 25 == 0:
                send_message(
                    "🔄 <b>TARAMA DEVAM EDİYOR</b>\n\n"
                    f"📄 Sayfa: {page_num}\n"
                    f"📊 Toplam ilan: {len(results)}\n"
                    f"⏱️ Süre: {format_duration(time.time() - scan_start)}"
                )

            print(
                "[SAYFA " + str(page_num) + "] " + str(len(listings)) + " ilan | Toplam: " + str(len(results)),
                flush=True,
            )

            if len(listings) == 0:
                print("[PLAYWRIGHT] Son sayfa (liste boş)", flush=True)
                break

            if page_num % 100 == 0:
                try:
                    page.close()
                    context.close()
                except:
                    pass
                context = new_context()
                page = context.new_page()
                stealth_sync(page)
                print("[PLAYWRIGHT] Context yenilendi, siteye geri dönülüyor...", flush=True)
                # Yeni context sonrası siteye geri dön (SAYFA 6+ için kritik)
                try:
                    page.goto(URL, timeout=90000, wait_until="networkidle")
                    wait_for_cloudflare(page)
                except Exception as e:
                    print(f"[PLAYWRIGHT] Yeniden yükleme hatası: {e}", flush=True)

            page.wait_for_timeout(random.randint(2000, 4000))

        browser.close()

    bot_stats["last_scan_pages"] = page_num
    print("[PLAYWRIGHT] Tamamlandi: " + str(len(results)) + " ilan, " + str(page_num) + " sayfa", flush=True)
    return (results, None)  # Başarılı, hata yok

def run_scan_with_timeout():
    global bot_stats, ACTIVE_SCAN, MANUAL_SCAN_LIMIT, SCAN_STOP_REQUESTED

    scan_start = time.time()
    now = get_turkey_time()
    today = now.strftime("%Y-%m-%d")

    print("\n[TARAMA] Basliyor - " + now.strftime("%Y-%m-%d %H:%M:%S"), flush=True)

    state = load_state()
    history = load_history()

    if "daily_stats" not in state:
        state["daily_stats"] = {}
    if today not in state["daily_stats"]:
        state["daily_stats"][today] = {"new": 0, "price_changes": 0, "deleted": 0}

    # YENİ: Tarama sıra numarasını artır
    state["scan_sequence"] = state.get("scan_sequence", 0) + 1
    current_scan_seq = state["scan_sequence"]

    print(f"[TARAMA] Sira numarasi: {current_scan_seq}", flush=True)

    try:
        cycle_start = datetime.strptime(state["cycle_start"], "%Y-%m-%d")
        if (now - cycle_start).days >= 30:
            state = {
                "cycle_start": today,
                "items": {},
                "reported_days": [],
                "first_run_done": False,
                "daily_stats": {today: {"new": 0, "price_changes": 0, "deleted": 0}},
                "scan_sequence": 1,
            }
            current_scan_seq = 1
            print("[DONGU] 30 gun sifirlandi", flush=True)
    except Exception:
        state["cycle_start"] = today

    try:
        result = fetch_listings_playwright()
        listings, error_info = result if isinstance(result, tuple) else (result, None)
        
        # Web siteye ulaşılamadıysa veya tarama yarıda kesildiyse
        if listings is None:
            print("[TARAMA] Tarama başarısız - iptal edildi", flush=True)
            bot_stats["errors"] += 1
            
            # Telegram'a detaylı bildirim gönder
            next_interval = get_scan_interval() // 60
            msg = "⚠️ <b>TARAMA BAŞARISIZ</b>\n\n"
            msg += "🌐 Makrolife web sitesine tarama için ulaşılamadı.\n\n"
            if error_info:
                msg += f"❌ <b>Hata Detayı:</b>\n{error_info}\n\n"
            msg += "📋 <b>Durum:</b>\n"
            msg += "• İlan verileri değiştirilmedi ✅\n"
            msg += "• Silinen ilan işaretlenmedi ✅\n\n"
            msg += f"⏰ Sonraki tarama: {next_interval} dakika sonra"
            send_message(msg)
            
            # FIX: Sonsuz döngüyü engellemek için timestamp güncelle ve kaydet
            state["last_scan_timestamp"] = time.time()
            save_state(state)
            
            ACTIVE_SCAN = False
            MANUAL_SCAN_LIMIT = None
            SCAN_STOP_REQUESTED = False
            return
        
        print("[TARAMA] " + str(len(listings)) + " ilan bulundu", flush=True)
        bot_stats["last_scan_listings"] = len(listings)
    except Exception as e:
        print("[HATA] Playwright: " + str(e), flush=True)
        bot_stats["errors"] += 1
        save_state(state)
        return

    is_first_run = (not state.get("first_run_done", False)) or (len(state.get("items", {})) == 0)

    # === KORUMA: Minimum ilan oranı kontrolü ===
    # Eğer bellekte 100+ ilan varsa ve taramada bunun %40'ından az bulunduysa
    # Bu bir site hatasıdır, state güncellenmemeli
    existing_count = len(state.get("items", {}))
    if not is_first_run and existing_count > 100:
        min_expected = int(existing_count * MIN_LISTING_RATIO)
        if len(listings) < min_expected:
            next_interval = get_scan_interval() // 60
            msg = "⚠️ <b>KORUMA: Anormal Tarama Sonucu</b>\n\n"
            msg += f"📊 Bellekte: <b>{existing_count}</b> ilan\n"
            msg += f"🔍 Taramada bulunan: <b>{len(listings)}</b> ilan\n"
            msg += f"🛡️ Minimum beklenen: <b>{min_expected}</b> ilan (%{int(MIN_LISTING_RATIO*100)})\n\n"
            msg += "❌ <b>Durum:</b> Site erişim hatası olabilir\n"
            msg += "✅ ilanlar.json korundu, değişiklik yapılmadı\n\n"
            msg += f"⏰ Sonraki tarama: {next_interval} dakika sonra"
            send_message(msg)
            print(f"[KORUMA] Anormal tarama: {len(listings)}/{existing_count} ilan (min: {min_expected})", flush=True)
            # FIX: Sonsuz döngüyü engellemek için timestamp güncelle ve kaydet
            state["last_scan_timestamp"] = time.time()
            save_state(state)

            ACTIVE_SCAN = False
            MANUAL_SCAN_LIMIT = None
            SCAN_STOP_REQUESTED = False
            return

    if is_first_run:
        if len(listings) < 50:
            print("[UYARI] Yetersiz ilan: " + str(len(listings)), flush=True)
            save_state(state)
            return

        # İlk çalışmada tüm ilanları kaydet
        for kod, fiyat, link, title, page_num in listings:
            state["items"][kod] = {
                "fiyat": fiyat,
                "tarih": today,
                "link": link,
                "title": title,
                "scan_seq": current_scan_seq,
                "timestamp": time.time(),
            }

        state["first_run_done"] = True

        scan_duration = time.time() - scan_start
        msg = "✅ <b>İlk Tarama Tamamlandı!</b>\n\n"
        msg += "📅 " + today + " " + now.strftime("%H:%M") + "\n"
        msg += "⏱️ Tarama süresi: " + format_duration(scan_duration) + "\n"
        msg += "📄 Taranan sayfa: " + str(bot_stats["last_scan_pages"]) + " sayfa\n"
        msg += "📊 Toplam: <b>" + str(len(listings)) + "</b> ilan\n\n"
        msg += "💾 Tümü belleğe kaydedildi"
        send_message(msg)
        print("[TARAMA] Ilk calisma: " + str(len(listings)) + " ilan", flush=True)

    else:
        new_count = 0
        price_change_count = 0
        current_codes = set()

        # Sitedeki sıralama düzeltmesi:
        # 1. sayfa 1. sıra (index 0) = EN YENİ
        # Son sayfa son sıra (index N) = EN ESKİ
        # listings array'i zaten 1.sayfadan başlıyor, doğru sırada
        position_map = {kod: idx for idx, (kod, _, _, _, _) in enumerate(listings)}

        # Yeni ilanları ve değişiklikleri işle
        for kod, fiyat, link, title, page_num in listings:
            current_codes.add(kod)

            if kod not in state["items"]:
                # YENİ İLAN: Position = sitedeki index (0 = en yeni)
                state["items"][kod] = {
                    "fiyat": fiyat,
                    "tarih": today,
                    "link": link,
                    "title": title,
                    "scan_seq": current_scan_seq,
                    "timestamp": time.time(),
                    "position": position_map[kod],  # 0 = en yeni, 630 = en eski
                    "first_seen_date": today,
                }
                new_count += 1

                # SADECE YENİ İLANLAR için daily_stats artır
                state["daily_stats"][today]["new"] += 1

                history.setdefault("new", []).append(
                    {"kod": kod, "fiyat": fiyat, "title": title, "tarih": today, "link": link}
                )

                msg = "🏠 <b>YENİ İLAN</b>\n\n"
                msg += "📋 " + kod + "\n"
                msg += "🏷️ " + title + "\n"
                msg += "💰 " + fiyat + "\n\n"
                msg += "🔗 " + link
                send_real_admin_new_listing(kod, title, fiyat, link)
                time.sleep(0.3)

            else:
                # MEVCUT İLAN: Position güncelle (ilan yukarı/aşağı kayabilir)
                state["items"][kod]["position"] = position_map[kod]

                eski = state["items"][kod]["fiyat"]
                if normalize_price(eski) != normalize_price(fiyat):
                    history.setdefault("price_changes", []).append(
                        {"kod": kod, "eski_fiyat": eski, "yeni_fiyat": fiyat, "tarih": today}
                    )

                    state["items"][kod]["fiyat"] = fiyat
                    price_change_count += 1

                    # Fiyat değişimi için daily_stats artır
                    state["daily_stats"][today]["price_changes"] += 1

                    eski_num = int(normalize_price(eski)) if normalize_price(eski) else 0
                    yeni_num = int(normalize_price(fiyat)) if normalize_price(fiyat) else 0
                    fark = yeni_num - eski_num

                    if fark > 0:
                        fark_str = "📈 +" + format_number(fark) + " TL"
                        trend = "artış"
                    else:
                        fark_str = "📉 " + format_number(fark) + " TL"
                        trend = "düşüş"

                    msg = "💱 <b>FİYAT DEĞİŞTİ</b>\n\n"
                    msg += "📋 " + kod + "\n"
                    msg += "💰 " + eski + " ➜ " + fiyat + "\n"
                    msg += fark_str + " (" + trend + ")\n\n"
                    msg += "🔗 " + state["items"][kod].get("link", "")
                    send_real_admin_price_change(kod, state["items"][kod].get("title", ""), eski, fiyat)
                    time.sleep(0.3)

        deleted_count = 0
        for kod in list(state["items"].keys()):
            if kod not in current_codes:
                item = state["items"][kod]

                history.setdefault("deleted", []).append(
                    {"kod": kod, "fiyat": item.get("fiyat", ""), "title": item.get("title", ""), "tarih": today}
                )

                # Silinen ilan için daily_stats artır
                state["daily_stats"][today]["deleted"] += 1

                msg = "🗑️ <b>İLAN SİLİNDİ</b>\n\n"
                msg += "📋 " + kod + "\n"
                msg += "🏷️ " + item.get("title", "") + "\n"
                msg += "💰 " + item.get("fiyat", "")
                send_real_admin_deleted(kod, item.get("title", ""), item.get("fiyat", ""))

                del state["items"][kod]
                deleted_count += 1
                time.sleep(0.3)

        bot_stats["total_new_listings"] += new_count
        bot_stats["total_price_changes"] += price_change_count
        bot_stats["total_deleted"] += deleted_count

        print(
            "[OZET] Yeni: " + str(new_count) + ", Fiyat: " + str(price_change_count) + ", Silinen: " + str(deleted_count),
            flush=True,
        )

        # TARAMA TAMAMLANDI MESAJI
        scan_duration = time.time() - scan_start
        msg = "✅ <b>Tarama Tamamlandı!</b>\n\n"
        msg += "⏱️ Tarama süresi: " + format_duration(scan_duration) + "\n"
        msg += "📄 Taranan sayfa: " + str(bot_stats["last_scan_pages"]) + " sayfa\n"
        msg += "📊 Taranan ilan: " + str(len(listings)) + " ilan\n\n"
        msg += "<b>📈 Sonuçlar:</b>\n"

        if new_count > 0:
            msg += "🆕 Yeni ilan: <b>" + str(new_count) + "</b>\n"
        else:
            msg += "🆕 Yeni ilan: Bulunamadı\n"

        if deleted_count > 0:
            msg += "🗑️ Silinen ilan: <b>" + str(deleted_count) + "</b>\n"
        else:
            msg += "🗑️ Silinen ilan: Bulunamadı\n"

        if price_change_count > 0:
            msg += "💱 Fiyat değişimi: <b>" + str(price_change_count) + "</b>"
        else:
            msg += "💱 Fiyat değişimi: Bulunamadı"

        send_message(msg)

    if now.hour == 23 and now.minute >= 30 and today not in state.get("reported_days", []):
        # Sitedeki sıraya göre sırala (position küçük = daha yeni)
        all_items = [(k, v) for k, v in state["items"].items()]
        all_items.sort(key=lambda x: x[1].get("position", 999999))

        # Bugün eklenen ilanları say
        today_new_count = state.get("daily_stats", {}).get(today, {}).get("new", 0)

        msg = "📊 <b>GÜNLÜK RAPOR</b> (" + today + ")\n\n"
        msg += "🆕 Bugün eklenen: <b>" + str(today_new_count) + "</b> ilan\n"
        msg += "💾 Toplam bellekte: " + str(len(state["items"])) + " ilan\n\n"

        if all_items[:20]:
            msg += "📋 <b>Son Eklenen 20 İlan:</b>\n\n"
            for i, (kod, item) in enumerate(all_items[:20], 1):
                msg += str(i) + ". " + kod + "\n"
        else:
            msg += "Sistemde ilan bulunmuyor."

        send_message(msg)
        state.setdefault("reported_days", []).append(today)

    # Container restart koruması: timestamp'ı GitHub state'ine kaydet
    state["last_scan_timestamp"] = time.time()
    
    save_state(state)
    save_history(history)

    scan_duration = time.time() - scan_start
    bot_stats["total_scans"] += 1
    bot_stats["last_scan_time"] = datetime.utcnow()
    bot_stats["last_scan_duration"] = scan_duration

    print("[TARAMA] Tamamlandi (" + format_duration(scan_duration) + ")", flush=True)

    ACTIVE_SCAN = False
    MANUAL_SCAN_LIMIT = None
    SCAN_STOP_REQUESTED = False
def run_scan():
    global bot_stats
    
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_scan_with_timeout)
        try:
            future.result(timeout=SCAN_TIMEOUT)
        except FuturesTimeoutError:
            print("[TIMEOUT] Tarama " + str(SCAN_TIMEOUT//60) + " dakikayi asti!", flush=True)
            bot_stats["timeouts"] += 1
            msg = "<b>TIMEOUT</b>\n\n"
            msg += "Tarama " + str(SCAN_TIMEOUT//60) + " dakikayi asti.\n"
            msg += "Sonraki tarama bekleniyor..."
            send_message(msg)
        except Exception as e:
            print("[HATA] Tarama hatasi: " + str(e), flush=True)
            bot_stats["errors"] += 1


def main():
    global bot_stats
    
    print("=" * 60, flush=True)
    print("ANA DONGU BASLIYOR", flush=True)
    print("=" * 60, flush=True)
    
    bot_stats["start_time"] = datetime.utcnow()
    
    state = load_state()
    item_count = len(state.get("items", {}))
    
    # AUTO_SCAN_ENABLED durumunu state'ten yükle (container restart koruması)
    AUTO_SCAN_ENABLED = state.get("auto_scan_enabled", True)  # Varsayılan: True
    auto_scan_status = "AKTİF" if AUTO_SCAN_ENABLED else "PASİF"
    print(f"[BASLANGIC] Otomatik tarama: {auto_scan_status}", flush=True)
    
    # Son tarama zamanini yukle
    last_scan_time = load_last_scan_time()
    if last_scan_time > 0:
        elapsed = time.time() - last_scan_time
        print(f"[BASLANGIC] Son taramadan {int(elapsed//60)} dakika gecmis", flush=True)
    
    while True:
        try:
            cmd_result = check_telegram_commands()
            force_scan = (cmd_result == "SCAN")
            
            current_time = time.time()
            
            # Son tarama zamanini yukle
            last_scan_time = load_last_scan_time()
            
            # Otomatik tarama kontrolü
            if not force_scan and not AUTO_SCAN_ENABLED:
                # Sadece belirli aralıklarla log bas, sürekli spamlamasın
                if int(current_time) % 60 == 0:
                    print("[AUTO-SCAN PASIF] Manuel komut bekleniyor...", flush=True)
                time.sleep(1)
                continue

            # Tarama saati kontrolü: Sadece belirlenen saatlerde (10:00, 13:00, 16:00, 19:00) tara
            # Son taramadan bu yana en az 30 dakika geçmiş olmalı (aynı saatte tekrar taramayı önle)
            should_scan = False
            if should_scan_now() and (current_time - last_scan_time >= 1800):  # 30 dakika = 1800 saniye
                should_scan = True
                
            if force_scan or should_scan:
                print("\n" + "#" * 50, flush=True)
                scan_type = "(MANUEL)" if force_scan else ""
                print("# TARAMA #" + str(bot_stats["total_scans"] + 1) + " " + scan_type, flush=True)
                print("# " + get_turkey_time().strftime("%Y-%m-%d %H:%M:%S"), flush=True)
                print("#" * 50, flush=True)
                
                # TARAMA BASLADI MESAJI
                schedule_str = ", ".join([f"{h}:00" for h in SCHEDULED_SCAN_HOURS])
                github_status = "Aktif" if GITHUB_TOKEN else "Kapali"
                msg = "🔄 <b>Tarama Başladı!</b>\n\n"
                msg += "⏰ Tarama saatleri: " + schedule_str + "\n"
                msg += "💾 Bellekteki ilan: " + str(len(load_state().get("items", {}))) + "\n"
                msg += "☁️ GitHub yedek: " + github_status
                send_message(msg)
                
                run_scan()
                
                # Tarama sonrasi zamani kaydet
                save_last_scan_time(current_time)
                
                next_minutes = get_scan_interval() // 60
                next_hours = next_minutes // 60
                remaining_mins = next_minutes % 60
                if next_hours > 0:
                    print(f"[BEKLIYOR] Sonraki tarama {next_hours} saat {remaining_mins} dk sonra", flush=True)
                else:
                    print(f"[BEKLIYOR] Sonraki tarama {remaining_mins} dk sonra", flush=True)
            
            time.sleep(1)
            
        except KeyboardInterrupt:
            print("\n[DURDURULDU]", flush=True)
            send_message("Bot durduruldu!")
            break
        except Exception as e:
            print("[KRITIK HATA] " + str(e), flush=True)
            bot_stats["errors"] += 1
            time.sleep(30)


if __name__ == "__main__":
    print("__main__ basliyor...", flush=True)
    main()
