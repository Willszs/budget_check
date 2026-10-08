import urllib.request
import json
import time
import logging

logger = logging.getLogger(__name__)

# 缓存汇率，避免频繁请求，缓存 1 小时 (3600 秒)
_cached_rates = {
    "CNY": 7.55,
    "USD": 1.08,
}
_last_fetch_time = 0

def get_exchange_rates() -> dict:
    """返回基准 EUR 对应的 CNY 和 USD 汇率"""
    global _cached_rates, _last_fetch_time
    now = time.time()
    if now - _last_fetch_time < 3600 and _last_fetch_time > 0:
        return _cached_rates

    try:
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/EUR",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            if data.get("result") == "success" and "rates" in data:
                rates = data["rates"]
                _cached_rates["CNY"] = float(rates.get("CNY", 7.55))
                _cached_rates["USD"] = float(rates.get("USD", 1.08))
                _last_fetch_time = now
                logger.info(f"Updated exchange rates: {_cached_rates}")
                return _cached_rates
    except Exception as e:
        logger.error(f"Failed to fetch exchange rate, fallback to cached: {e}")
    
    return _cached_rates

def get_eur_to_cny_rate() -> float:
    return get_exchange_rates()["CNY"]

def get_eur_to_usd_rate() -> float:
    return get_exchange_rates()["USD"]

def format_dual_currency(amount_eur: float, rates: dict = None) -> str:
    """给定欧元金额，输出 欧元 + 人民币 双币种展示"""
    if rates is None:
        rates = get_exchange_rates()
    cny_rate = rates["CNY"]
    amount_cny = amount_eur * cny_rate
    return f"€{amount_eur:,.2f} (¥{amount_cny:,.2f})"

def format_hourly_rate(amount_eur: float, rates: dict = None) -> str:
    """时薪专属展示：支持同时展示 欧元、人民币、美金 三币种"""
    if rates is None:
        rates = get_exchange_rates()
    cny = amount_eur * rates["CNY"]
    usd = amount_eur * rates["USD"]
    return f"€{amount_eur:,.2f} / ¥{cny:,.2f} / ${usd:,.2f}"

def parse_currency_input(text: str, rates: dict = None) -> tuple[float, str]:
    """
    解析用户输入的金额。支持 欧元(EUR)、人民币(CNY)、美金(USD)。
    返回: (amount_in_eur, currency_detected: 'EUR'|'CNY'|'USD')
    """
    if rates is None:
        rates = get_exchange_rates()

    cleaned = text.strip().lower().replace(",", "")
    
    cny_indicators = ["¥", "rmb", "cny", "元", "块", "人民币"]
    usd_indicators = ["$", "usd", "刀", "美金", "美元", "美", "bucks"]
    eur_indicators = ["€", "eur", "euro", "欧", "欧元"]

    for ind in usd_indicators:
        if ind in cleaned:
            cleaned = cleaned.replace(ind, "")
            val = float(cleaned.strip())
            eur_val = val / rates["USD"] if rates["USD"] > 0 else 0
            return eur_val, "USD"

    for ind in cny_indicators:
        if ind in cleaned:
            cleaned = cleaned.replace(ind, "")
            val = float(cleaned.strip())
            eur_val = val / rates["CNY"] if rates["CNY"] > 0 else 0
            return eur_val, "CNY"

    for ind in eur_indicators:
        if ind in cleaned:
            cleaned = cleaned.replace(ind, "")
            break

    val = float(cleaned.strip())
    return val, "EUR"
