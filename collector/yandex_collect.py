"""Сбор данных из Яндекс Вебмастера и Метрики (только чтение).

Токен берётся из переменной окружения YANDEX_OAUTH_TOKEN
(права: webmaster:hostinfo, metrika:read). В чат токен не вставлять.

Запуск:  python3 collector/yandex_collect.py --site vbi.ru
Результат: data/<site>/<дата>/*.json — сырые ответы API.
Скрипт ничего не меняет ни на сайте, ни в кабинетах.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

WM = "https://api.webmaster.yandex.net/v4"
MT = "https://api-metrika.yandex.net"


def call(url, token, params=None, body=None):
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"OAuth {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"{e.code} {url}: {e.read()[:300]!r}") from None


def save(outdir, name, obj):
    with open(os.path.join(outdir, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    print("  сохранено:", name)


def webmaster(token, site, outdir, days):
    uid = call(f"{WM}/user", token)["user_id"]
    hosts = call(f"{WM}/user/{uid}/hosts", token)["hosts"]
    host = next((h for h in hosts if site in h.get("unicode_host_url", h["host_id"])), None)
    if not host:
        raise RuntimeError(f"Сайт {site} не найден в Вебмастере. Доступные: "
                           + ", ".join(h["host_id"] for h in hosts))
    base = f"{WM}/user/{uid}/hosts/{urllib.parse.quote(host['host_id'], safe='')}"
    today = dt.date.today()
    d_from = (today - dt.timedelta(days=days)).isoformat()
    d_to = today.isoformat()

    jobs = {
        "wm_summary": (f"{base}/summary", None),
        "wm_diagnostics": (f"{base}/diagnostics", None),
        "wm_sitemaps": (f"{base}/sitemaps", None),
        "wm_indexing_history": (f"{base}/indexing/history",
                                {"date_from": d_from, "date_to": d_to}),
    }
    for name, (url, params) in jobs.items():
        try:
            save(outdir, name, call(url, token, params))
        except RuntimeError as e:
            print("  пропуск", name, "-", e)

    # Популярные запросы (топ-3000), постранично по 500
    rows, offset = [], 0
    while offset < 3000:
        page = call(f"{base}/search-queries/popular", token, {
            "order_by": "TOTAL_SHOWS",
            "query_indicator": ["TOTAL_SHOWS", "TOTAL_CLICKS",
                                "AVG_SHOW_POSITION", "AVG_CLICK_POSITION"],
            "date_from": d_from, "date_to": d_to,
            "limit": 500, "offset": offset})
        batch = page.get("queries", [])
        rows += batch
        if len(batch) < 500:
            break
        offset += 500
    save(outdir, "wm_popular_queries", {"date_from": d_from, "date_to": d_to, "queries": rows})

    # Мониторинг «запрос — страница»: история всего 2 недели, поэтому сохраняем регулярно
    try:
        rows, offset = [], 0
        while True:
            page = call(f"{base}/query-analytics/list", token, body={
                "offset": offset, "limit": 500,
                "device_type_indicator": "ALL", "text_indicator": "QUERY"})
            batch = page.get("text_indicator_to_statistics", [])
            rows += batch
            if len(batch) < 500 or offset > 20000:
                break
            offset += 500
        save(outdir, "wm_query_analytics", {"count": len(rows), "rows": rows})
    except RuntimeError as e:
        print("  пропуск wm_query_analytics -", e)


def metrika(token, site, outdir, days):
    counters = call(f"{MT}/management/v1/counters", token)["counters"]
    counter = next((c for c in counters if site in c.get("site", "")), None)
    if not counter:
        raise RuntimeError(f"Счётчик для {site} не найден. Доступные: "
                           + ", ".join(f"{c['id']} {c.get('site')}" for c in counters))
    cid = counter["id"]
    d1 = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    common = {"ids": cid, "date1": d1, "date2": "yesterday", "accuracy": "full", "limit": 10000}
    # Только агрегированные отчёты — без сырых логов, IP и ClientID (персональные данные не берём)
    reports = {
        "mt_by_source": {"metrics": "ym:s:visits,ym:s:users,ym:s:bounceRate,ym:s:pageDepth,ym:s:avgVisitDurationSeconds",
                         "dimensions": "ym:s:lastTrafficSource,ym:s:lastSearchEngineRoot"},
        "mt_organic_landing": {"metrics": "ym:s:visits,ym:s:users,ym:s:bounceRate,ym:s:pageDepth,ym:s:avgVisitDurationSeconds",
                               "dimensions": "ym:s:startURL,ym:s:lastSearchEngineRoot",
                               "filters": "ym:s:lastTrafficSource=='organic'"},
        "mt_organic_by_week": {"metrics": "ym:s:visits,ym:s:users",
                               "dimensions": "ym:s:startOfWeek,ym:s:lastSearchEngineRoot",
                               "filters": "ym:s:lastTrafficSource=='organic'"},
        "mt_organic_devices": {"metrics": "ym:s:visits,ym:s:bounceRate",
                               "dimensions": "ym:s:deviceCategory,ym:s:lastSearchEngineRoot",
                               "filters": "ym:s:lastTrafficSource=='organic'"},
    }
    save(outdir, "mt_counter", counter)
    for name, p in reports.items():
        try:
            save(outdir, name, call(f"{MT}/stat/v1/data", token, {**common, **p}))
        except RuntimeError as e:
            print("  пропуск", name, "-", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    token = os.environ.get("YANDEX_OAUTH_TOKEN")
    if not token:
        sys.exit("Нет переменной YANDEX_OAUTH_TOKEN")
    outdir = os.path.join(a.out, a.site, dt.date.today().isoformat())
    os.makedirs(outdir, exist_ok=True)
    print("Вебмастер:")
    webmaster(token, a.site, outdir, a.days)
    print("Метрика:")
    metrika(token, a.site, outdir, max(a.days, 365))
    print("Готово:", outdir)


if __name__ == "__main__":
    main()
