"""Shared price fetcher — uses regularMarketPrice (matches broker last price)."""
import urllib.request, json

def fetch_price(ticker):
    """Fetch last traded price from Yahoo Finance (regularMarketPrice)."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        meta = data["chart"]["result"][0]["meta"]
        return round(meta.get("regularMarketPrice", 0), 2)
    except:
        return None

def fetch_all(tickers):
    """Fetch prices for a list of tickers. Returns dict."""
    prices = {}
    for t in tickers:
        p = fetch_price(t)
        if p and p > 0:
            prices[t] = p
    return prices
