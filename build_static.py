#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gera o site estático do NERD Reader (para GitHub Pages).

Busca todos os feeds do feeds.opml, junta com o histórico anterior (cache)
e escreve um site pronto em --out:

    out/
      index.html, app.js, style.css   (copiados de web/)
      feeds.opml                      (para exportar/backup)
      data/meta.json                  (feeds + metadados de todos os artigos)
      data/content-<feed>.json        (HTML sanitizado dos artigos, por feed)

Uso:  python3 build_static.py --cache .nr-cache --out _site
"""

import argparse
import hashlib
import json
import shutil
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.etree import ElementTree as ET

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import server  # parser/sanitizador do NERD Reader (stdlib only)

MAX_PER_FEED = 100      # artigos por feed mantidos no site
SUMMARY_CHARS = 220


def feed_id_for(xml_url):
    return hashlib.sha1(xml_url.encode("utf-8")).hexdigest()[:10]


def article_id_for(feed_id, guid):
    return hashlib.sha1((feed_id + "|" + guid).encode("utf-8", "ignore")).hexdigest()[:12]


def load_opml(path):
    """Lê o OPML preservando a ordem das categorias. Retorna lista de feeds."""
    root = ET.fromstring(Path(path).read_bytes())
    feeds = []

    def walk(node, category):
        for child in node.findall("outline"):
            xml_url = (child.get("xmlUrl") or "").strip()
            if xml_url:
                feeds.append({
                    "id": feed_id_for(xml_url),
                    "title": (child.get("title") or child.get("text") or xml_url).strip(),
                    "xml_url": xml_url,
                    "html_url": (child.get("htmlUrl") or "").strip() or None,
                    "category": category,
                })
            else:
                walk(child, (child.get("title") or child.get("text") or category).strip())

    body = root.find("body")
    if body is None:
        raise SystemExit("feeds.opml sem <body>")
    walk(body, "Sem categoria")
    # remove duplicatas de xml_url mantendo a primeira ocorrência
    seen, unique = set(), []
    for f in feeds:
        if f["xml_url"] not in seen:
            seen.add(f["xml_url"])
            unique.append(f)
    return unique


def fetch_feed(feed):
    """Busca e interpreta um feed. Retorna (feed_id, artigos, status)."""
    now = int(time.time())
    try:
        _, data, _ = server.http_get(feed["xml_url"])
        parsed = server.parse_feed(data)
    except Exception as e:
        return feed["id"], None, "erro: %s" % (str(e)[:160] or type(e).__name__)

    articles = []
    for entry in parsed["entries"]:
        guid = entry["guid"] or entry["link"]
        if not guid:
            digest = hashlib.sha1((entry["content"] or entry["summary_source"] or "").encode("utf-8", "ignore")).hexdigest()[:16]
            guid = "%s|%s|%s" % (entry["title"], entry["published"], digest)
        link = entry["link"]
        if link:
            link = urllib.parse.urljoin(feed["xml_url"], link)
        articles.append({
            "id": article_id_for(feed["id"], guid),
            "feed": feed["id"],
            "title": entry["title"][:500],
            "url": server._safe_url(link) or "",
            "author": entry["author"],
            "published": entry["published"] or now,
            "summary": server.make_summary(entry["summary_source"] or entry["content"], SUMMARY_CHARS),
            "image": entry["image"] or server.first_image(entry["content"]),
            "content": server.sanitize_html(entry["content"]),
        })
    return feed["id"], articles, "ok"


def merge(prev, new):
    """Junta artigos novos com o histórico: id novo entra, id conhecido mantém o published original."""
    by_id = {a["id"]: a for a in prev}
    for art in new:
        old = by_id.get(art["id"])
        if old:
            art["published"] = old["published"]  # estabilidade de ordenação
        by_id[art["id"]] = art
    merged = sorted(by_id.values(), key=lambda a: (a["published"], a["id"]), reverse=True)
    return merged[:MAX_PER_FEED]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".nr-cache", help="pasta de histórico entre execuções")
    ap.add_argument("--out", default="_site", help="pasta de saída do site")
    ap.add_argument("--opml", default=str(APP_DIR / "feeds.opml"))
    args = ap.parse_args()

    cache_dir = Path(args.cache)
    cache_file = cache_dir / "articles.json"
    out = Path(args.out)

    feeds = load_opml(args.opml)
    print("feeds no OPML: %d" % len(feeds))

    history = {}
    if cache_file.exists():
        try:
            history = json.loads(cache_file.read_text())
            print("histórico carregado: %d feeds" % len(history))
        except Exception as e:
            print("histórico ignorado (%s)" % e)

    results = {}
    statuses = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_feed, f): f for f in feeds}
        for fut in as_completed(futures):
            fid, articles, status = fut.result()
            statuses[fid] = status
            prev = history.get(fid, [])
            if articles is None:
                # falha na busca: mantém o histórico para os artigos não sumirem
                results[fid] = prev
            else:
                results[fid] = merge(prev, articles)

    ok = sum(1 for s in statuses.values() if s == "ok")
    print("busca: %d ok, %d com erro" % (ok, len(statuses) - ok))
    for fid, s in statuses.items():
        if s != "ok":
            title = next((f["title"] for f in feeds if f["id"] == fid), fid)
            print("  ⚠️ %s: %s" % (title, s))

    # grava o histórico para a próxima execução
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(results, ensure_ascii=False))

    # ---- monta o site ----
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)

    web = APP_DIR / "web"
    for f in web.iterdir():
        if f.is_file():
            shutil.copy(f, out / f.name)
    shutil.copy(args.opml, out / "feeds.opml")
    (out / ".nojekyll").write_text("")

    meta_articles = []
    for fid, arts in results.items():
        content_map = {}
        for a in arts:
            meta_articles.append({k: a[k] for k in ("id", "feed", "title", "url", "author", "published", "summary", "image")})
            if a["content"]:
                content_map[a["id"]] = a["content"]
        (out / "data" / ("content-%s.json" % fid)).write_text(
            json.dumps(content_map, ensure_ascii=False))

    meta_articles.sort(key=lambda a: (a["published"], a["id"]), reverse=True)

    categories = []
    for f in feeds:
        if not categories or categories[-1]["name"] != f["category"]:
            existing = next((c for c in categories if c["name"] == f["category"]), None)
            if existing is None:
                categories.append({"name": f["category"], "feeds": []})
        cat = next(c for c in categories if c["name"] == f["category"])
        cat["feeds"].append({
            "id": f["id"], "title": f["title"], "html_url": f["html_url"],
            "status": statuses.get(f["id"], "?"),
        })

    meta = {
        "generated_at": int(time.time()),
        "categories": categories,
        "articles": meta_articles,
    }
    (out / "data" / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))

    total = len(meta_articles)
    size_mb = sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 1024 / 1024
    print("site pronto: %d artigos, %.1f MB em %s" % (total, size_mb, out))


if __name__ == "__main__":
    main()
