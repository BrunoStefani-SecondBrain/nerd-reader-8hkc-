#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NERD Reader 🦞 — leitor RSS local, estilo Feedly.

Zero dependências: só a biblioteca padrão do Python (3.9+).

Uso:
    python3 server.py [--port 8484] [--refresh 30]

Depois abra http://localhost:8484 no navegador.

Os feeds vêm do arquivo feeds.opml (na primeira execução) e ficam
guardados em data/reader.db (SQLite), junto com os artigos.
"""

import argparse
import hashlib
import html
import json
import re
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "reader.db"
OPML_PATH = APP_DIR / "feeds.opml"

USER_AGENT = "NERDReader/1.0 (leitor RSS local)"
FETCH_TIMEOUT = 25          # segundos por feed
MAX_WORKERS = 8             # feeds buscados em paralelo
KEEP_PER_FEED = 500         # artigos mantidos por feed (lidos e sem estrela além disso são apagados)
MAX_FEED_BYTES = 20 * 1024 * 1024   # teto de download por feed
MAX_BODY_BYTES = 10 * 1024 * 1024   # teto de corpo de requisição

_ssl_context = ssl.create_default_context()

# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS feeds (
                id            INTEGER PRIMARY KEY,
                title         TEXT NOT NULL,
                xml_url       TEXT NOT NULL UNIQUE,
                html_url      TEXT,
                category      TEXT NOT NULL DEFAULT 'Sem categoria',
                etag          TEXT,
                last_modified TEXT,
                last_fetch    INTEGER,
                last_status   TEXT,
                created_at    INTEGER
            );
            CREATE TABLE IF NOT EXISTS articles (
                id           INTEGER PRIMARY KEY,
                feed_id      INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
                guid         TEXT NOT NULL,
                title        TEXT,
                url          TEXT,
                author       TEXT,
                published    INTEGER,
                fetched_at   INTEGER,
                summary      TEXT,
                content_html TEXT,
                image        TEXT,
                read         INTEGER NOT NULL DEFAULT 0,
                starred      INTEGER NOT NULL DEFAULT 0,
                UNIQUE (feed_id, guid)
            );
            CREATE INDEX IF NOT EXISTS idx_articles_feed_read ON articles (feed_id, read);
            CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_articles_starred ON articles (starred) WHERE starred = 1;
            """
        )


# ---------------------------------------------------------------------------
# OPML
# ---------------------------------------------------------------------------

def import_opml(xml_bytes):
    """Importa um OPML. Retorna quantos feeds novos entraram."""
    root = ET.fromstring(xml_bytes)
    added = 0
    now = int(time.time())

    def walk(node, category):
        nonlocal added
        for child in node.findall("outline"):
            xml_url = child.get("xmlUrl")
            if xml_url:
                title = (child.get("title") or child.get("text") or xml_url).strip()
                html_url = (child.get("htmlUrl") or "").strip() or None
                with _db_lock, db() as conn:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO feeds (title, xml_url, html_url, category, created_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (title, xml_url.strip(), html_url, category, now),
                    )
                    if cur.rowcount:
                        added += 1
            else:
                sub = (child.get("title") or child.get("text") or category).strip()
                walk(child, sub)

    body = root.find("body")
    if body is None:
        raise ValueError("OPML sem <body>")
    walk(body, "Sem categoria")
    return added


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_safe(s):
    return _CTRL_RE.sub("", s or "")


def export_opml():
    with db() as conn:
        feeds = conn.execute("SELECT * FROM feeds ORDER BY category, title").fetchall()
    root = ET.Element("opml", version="1.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "NERD Reader — assinaturas do Bruno"
    body = ET.SubElement(root, "body")
    groups = {}
    for f in feeds:
        cat = _xml_safe(f["category"])
        if cat not in groups:
            groups[cat] = ET.SubElement(body, "outline", text=cat, title=cat)
        title = _xml_safe(f["title"])
        attrs = {"type": "rss", "text": title, "title": title, "xmlUrl": _xml_safe(f["xml_url"])}
        if f["html_url"]:
            attrs["htmlUrl"] = _xml_safe(f["html_url"])
        ET.SubElement(groups[cat], "outline", attrs)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


# ---------------------------------------------------------------------------
# Sanitização de HTML (conteúdo dos feeds é não confiável)
# ---------------------------------------------------------------------------

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "del", "div",
    "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "i", "img", "ins", "li", "mark", "ol", "p", "pre", "q", "s", "small",
    "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "u", "ul",
}
VOID_TAGS = {"br", "hr", "img"}
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
# Tags cujo conteúdo inteiro deve ser descartado
DROP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "svg", "math", "noscript", "form", "textarea"}


def _safe_url(value):
    value = (value or "").strip()
    low = value.lower()
    if low.startswith(("http://", "https://")):
        return value
    if low.startswith("//"):
        return "https:" + value
    return None


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []       # tags abertas permitidas
        self.drop_depth = 0   # dentro de <script>/<style>/...

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT_TAGS:
            self.drop_depth += 1
            return
        if self.drop_depth or tag not in ALLOWED_TAGS:
            return
        clean = []
        allowed = ALLOWED_ATTRS.get(tag, set())
        for name, value in attrs:
            if name not in allowed:
                continue
            if name in ("href", "src"):
                value = _safe_url(value)
                if not value:
                    continue
            clean.append(' %s="%s"' % (name, html.escape(value or "", quote=True)))
        if tag == "a":
            clean.append(' target="_blank" rel="noopener noreferrer"')
        if tag in VOID_TAGS:
            self.out.append("<%s%s>" % (tag, "".join(clean)))
        else:
            self.out.append("<%s%s>" % (tag, "".join(clean)))
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in VOID_TAGS:
            self.handle_starttag(tag, attrs)
        elif tag in DROP_CONTENT_TAGS:
            pass  # auto-fechada: não abre bloco de descarte
        # outras auto-fechadas não permitidas: ignora

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT_TAGS:
            if self.drop_depth:
                self.drop_depth -= 1
            return
        if self.drop_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag in self.stack:
            # fecha até a tag correspondente (recupera aninhamento torto)
            while self.stack:
                top = self.stack.pop()
                self.out.append("</%s>" % top)
                if top == tag:
                    break

    def handle_data(self, data):
        if not self.drop_depth:
            self.out.append(html.escape(data))

    def result(self):
        while self.stack:
            self.out.append("</%s>" % self.stack.pop())
        return "".join(self.out)


def sanitize_html(raw):
    if not raw:
        return ""
    parser = _Sanitizer()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return html.escape(_strip_tags(raw))
    return parser.result()


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.drop = 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT_TAGS:
            self.drop += 1

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT_TAGS and self.drop:
            self.drop -= 1

    def handle_data(self, data):
        if not self.drop:
            self.parts.append(data)


def _strip_tags(raw):
    p = _TextExtractor()
    try:
        p.feed(raw or "")
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw or "")
    return " ".join("".join(p.parts).split())


def make_summary(html_content, limit=320):
    text = _strip_tags(html_content or "")
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def first_image(html_content):
    m = _IMG_RE.search(html_content or "")
    if m:
        return _safe_url(html.unescape(m.group(1)))
    return None


# ---------------------------------------------------------------------------
# Parser de feeds (RSS 2.0, Atom, RDF/RSS 1.0)
# ---------------------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _child(el, *names):
    for name in names:
        for c in el:
            if _local(c.tag) == name:
                return c
    return None


def _text(el):
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _strip_ns_inplace(el):
    """Remove prefixos de namespace de tags e atributos (para serializar XHTML como HTML)."""
    for node in el.iter():
        node.tag = _local(node.tag)
        for key in list(node.attrib):
            plain = key.rsplit("}", 1)[-1]
            if plain != key:
                node.attrib[plain] = node.attrib.pop(key)
    return el


def _atom_content(el):
    """Conteúdo de um elemento Atom <content>/<summary>, respeitando type."""
    if el is None:
        return ""
    ctype = (el.get("type") or "text").lower()
    if ctype == "xhtml":
        parts = []
        for c in el:
            parts.append(ET.tostring(_strip_ns_inplace(c), encoding="unicode", method="html"))
        return "".join(parts).strip()
    return _text(el)


def _person(el):
    """Nome de autor a partir de <author>/<dc:creator>, preferindo o filho <name>."""
    if el is None:
        return ""
    name = _child(el, "name")
    if name is not None:
        el = name
    return " ".join(t.strip() for t in el.itertext() if t.strip())


def parse_date(value):
    """Converte string de data (RFC 822 ou ISO 8601) em timestamp unix, ou None."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        pass
    v = value.replace("Z", "+00:00")
    # corta frações de segundo longas demais para o fromisoformat
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)
    for candidate in (v, v[:19]):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            continue
    return None


def _find_media_image(item):
    """Procura imagem em media:thumbnail, media:content, enclosure, itunes:image."""
    best = None
    for el in item.iter():
        name = _local(el.tag)
        if name == "thumbnail" and el.get("url"):
            best = best or el.get("url")
        elif name == "content" and el.get("url"):
            medium = (el.get("medium") or "").lower()
            mime = (el.get("type") or "").lower()
            if medium == "image" or mime.startswith("image/"):
                best = best or el.get("url")
        elif name == "enclosure":
            mime = (el.get("type") or "").lower()
            if mime.startswith("image/") and el.get("url"):
                best = best or el.get("url")
        elif name == "image" and el.get("href"):  # itunes:image
            best = best or el.get("href")
    return _safe_url(best) if best else None


def parse_feed(data):
    """Retorna dict {title, link, entries: [...]}. Lança ValueError se não for feed."""
    head = data[:4096] if isinstance(data, bytes) else data[:4096].encode("utf-8", "ignore")
    if b"<!ENTITY" in head:
        raise ValueError("feed com DTD/ENTITY não é suportado")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # tenta limpar caracteres de controle ilegais e reparsear
        if isinstance(data, bytes):
            m = re.match(rb'^[^\n]{0,100}encoding=["\']([A-Za-z0-9._-]+)["\']', data)
            enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
            try:
                text = data.decode(enc, errors="replace")
            except LookupError:
                text = data.decode("utf-8", errors="replace")
            text = re.sub(r"^<\?xml[^>]*\?>", "", text)  # decl. de encoding já resolvida
        else:
            text = data
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        text = text.lstrip("﻿ \r\n\t")
        root = ET.fromstring(text)

    kind = _local(root.tag)
    if kind == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise ValueError("RSS sem <channel>")
        return _parse_rss_items(channel, _children(channel, "item"))
    if kind == "rdf":
        channel = _child(root, "channel")
        items = _children(root, "item") or (_children(channel, "item") if channel is not None else [])
        return _parse_rss_items(channel if channel is not None else root, items)
    if kind == "feed":
        return _parse_atom(root)
    raise ValueError("Formato de feed não reconhecido: <%s>" % kind)


def _parse_rss_items(channel, items):
    feed_title = _text(_child(channel, "title"))
    feed_link = ""
    for link_el in _children(channel, "link"):
        t = _text(link_el)
        if t.startswith("http"):
            feed_link = t
            break
        href = link_el.get("href")
        if href and not feed_link:
            feed_link = href

    entries = []
    for item in items:
        title = _text(_child(item, "title"))
        # <link> do RSS tem texto; atom:link (rel=self/enclosure) só tem href — prefere o texto
        link = ""
        link_els = _children(item, "link")
        for le in link_els:
            t = _text(le)
            if t:
                link = t
                break
        if not link:
            for le in link_els:
                rel = (le.get("rel") or "alternate").lower()
                if rel == "alternate" and le.get("href"):
                    link = le.get("href")
                    break
        content = ""
        # content:encoded tem prioridade sobre description
        for c in item:
            if _local(c.tag) == "encoded":
                content = _text(c)
                break
        description = _text(_child(item, "description", "summary"))
        body = content or description
        guid = _text(_child(item, "guid")) or link
        date = None
        for c in item:
            if _local(c.tag) in ("pubdate", "date", "published", "updated"):
                date = parse_date(_text(c))
                if date:
                    break
        author = ""
        for c in item:
            if _local(c.tag) in ("creator", "author"):
                author = _strip_tags(_person(c))
                if author:
                    break
        entries.append({
            "title": _strip_tags(title) or "(sem título)",
            "link": link.strip(),
            "guid": guid.strip(),
            "published": date,
            "content": body,
            "summary_source": description or content,
            "author": author[:200],
            "image": _find_media_image(item),
        })
    return {"title": feed_title, "link": feed_link, "entries": entries}


def _parse_atom(root):
    feed_title = _text(_child(root, "title"))
    feed_link = ""
    for link_el in _children(root, "link"):
        rel = link_el.get("rel") or "alternate"
        if rel == "alternate" and link_el.get("href"):
            feed_link = link_el.get("href")
            break

    entries = []
    for entry in _children(root, "entry"):
        title = _text(_child(entry, "title"))
        link = ""
        for link_el in _children(entry, "link"):
            rel = link_el.get("rel") or "alternate"
            if rel == "alternate" and link_el.get("href"):
                link = link_el.get("href")
                break
        if not link:
            first = _child(entry, "link")
            link = first.get("href") if first is not None else ""
        content = _atom_content(_child(entry, "content"))
        summary = _atom_content(_child(entry, "summary"))
        if not content and not summary:
            # YouTube: media:group > media:description
            for el in entry.iter():
                if _local(el.tag) == "description":
                    summary = _text(el)
                    break
        guid = _text(_child(entry, "id")) or link
        date = parse_date(_text(_child(entry, "published"))) or parse_date(_text(_child(entry, "updated")))
        author = _person(_child(entry, "author"))
        entries.append({
            "title": _strip_tags(title) or "(sem título)",
            "link": (link or "").strip(),
            "guid": guid.strip(),
            "published": date,
            "content": content or summary,
            "summary_source": summary or content,
            "author": author[:200],
            "image": _find_media_image(entry),
        })
    return {"title": feed_title, "link": feed_link, "entries": entries}


# ---------------------------------------------------------------------------
# Busca de feeds
# ---------------------------------------------------------------------------

def _bounded_decompress(data, wbits):
    """Descomprime com teto de saída — uma gzip bomb não pode estourar a memória."""
    d = zlib.decompressobj(wbits)
    out = d.decompress(data, MAX_FEED_BYTES + 1)
    if len(out) > MAX_FEED_BYTES:
        raise ValueError("feed descomprimido maior que %d MB" % (MAX_FEED_BYTES // 1024 // 1024))
    return out


def http_get(url, etag=None, last_modified=None):
    """GET com gzip e cache condicional. Retorna (status, data, headers). 304 → data None."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=_ssl_context) as resp:
            data = resp.read(MAX_FEED_BYTES + 1)
            if len(data) > MAX_FEED_BYTES:
                raise ValueError("feed maior que %d MB" % (MAX_FEED_BYTES // 1024 // 1024))
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in encoding:
                data = _bounded_decompress(data, 16 + zlib.MAX_WBITS)
            elif "deflate" in encoding:
                try:
                    data = _bounded_decompress(data, zlib.MAX_WBITS)
                except zlib.error:
                    data = _bounded_decompress(data, -zlib.MAX_WBITS)
            # resp.headers é case-insensitive (dict() perderia isso)
            return resp.status, data, resp.headers
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None, e.headers
        raise


def refresh_feed(feed_id):
    """Busca um feed e grava artigos novos. Retorna (novos, status_texto)."""
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    if feed is None:
        return 0, "feed removido"

    now = int(time.time())
    try:
        status, data, headers = http_get(feed["xml_url"], feed["etag"], feed["last_modified"])
    except Exception as e:
        msg = "erro: %s" % (str(e)[:180] or type(e).__name__)
        with _db_lock, db() as conn:
            conn.execute("UPDATE feeds SET last_fetch = ?, last_status = ? WHERE id = ?", (now, msg, feed_id))
        return 0, msg

    if status == 304 or data is None:
        with _db_lock, db() as conn:
            conn.execute("UPDATE feeds SET last_fetch = ?, last_status = 'ok' WHERE id = ?", (now, feed_id))
        return 0, "ok (sem mudanças)"

    try:
        parsed = parse_feed(data)
    except Exception as e:
        msg = "erro ao interpretar: %s" % (str(e)[:160] or type(e).__name__)
        with _db_lock, db() as conn:
            conn.execute("UPDATE feeds SET last_fetch = ?, last_status = ? WHERE id = ?", (now, msg, feed_id))
        return 0, msg

    new_count = 0
    with _db_lock, db() as conn:
        for entry in parsed["entries"]:
            guid = entry["guid"] or entry["link"]
            if not guid:
                digest = hashlib.sha1((entry["content"] or entry["summary_source"] or "").encode("utf-8", "ignore")).hexdigest()[:16]
                guid = "%s|%s|%s" % (entry["title"], entry["published"], digest)
            link = entry["link"]
            if link:
                # resolve links relativos contra a URL do feed
                link = urllib.parse.urljoin(feed["xml_url"], link)
            content_html = sanitize_html(entry["content"])
            summary = make_summary(entry["summary_source"] or entry["content"])
            image = entry["image"] or first_image(entry["content"])
            inserted_at = int(time.time())
            cur = conn.execute(
                """INSERT OR IGNORE INTO articles
                   (feed_id, guid, title, url, author, published, fetched_at,
                    summary, content_html, image)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    feed_id, guid, entry["title"][:500],
                    _safe_url(link) or "", entry["author"],
                    entry["published"] or inserted_at, inserted_at, summary, content_html, image,
                ),
            )
            new_count += cur.rowcount
        updates = {"last_fetch": now, "last_status": "ok",
                   "etag": headers.get("ETag"), "last_modified": headers.get("Last-Modified")}
        if not feed["html_url"] and parsed.get("link"):
            updates["html_url"] = urllib.parse.urljoin(feed["xml_url"], parsed["link"])
        sets = ", ".join("%s = ?" % k for k in updates)
        conn.execute("UPDATE feeds SET %s WHERE id = ?" % sets, (*updates.values(), feed_id))
        # poda: mantém no máximo KEEP_PER_FEED artigos (preserva não lidos e com estrela)
        conn.execute(
            """DELETE FROM articles WHERE feed_id = ? AND read = 1 AND starred = 0 AND id IN (
                   SELECT id FROM articles WHERE feed_id = ?
                   ORDER BY published DESC, id DESC LIMIT -1 OFFSET ?)""",
            (feed_id, feed_id, KEEP_PER_FEED),
        )
    return new_count, "ok"


REFRESH_STATE = {"running": False, "started": None, "finished": None, "new": 0, "errors": 0}
_refresh_lock = threading.Lock()


def refresh_all():
    with _refresh_lock:
        if REFRESH_STATE["running"]:
            return False
        REFRESH_STATE.update(running=True, started=int(time.time()), new=0, errors=0)

    def work():
        total_new = 0
        errors = 0
        try:
            with db() as conn:
                ids = [r["id"] for r in conn.execute("SELECT id FROM feeds").fetchall()]
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {pool.submit(refresh_feed, fid): fid for fid in ids}
                for fut in as_completed(futures):
                    try:
                        new, status = fut.result()
                        total_new += new
                        if status.startswith("erro"):
                            errors += 1
                    except Exception:
                        errors += 1
        finally:
            REFRESH_STATE.update(running=False, finished=int(time.time()),
                                 new=total_new, errors=errors)

    threading.Thread(target=work, daemon=True).start()
    return True


def refresh_loop(interval_minutes):
    while True:
        time.sleep(interval_minutes * 60)
        refresh_all()


# ---------------------------------------------------------------------------
# API HTTP
# ---------------------------------------------------------------------------

def state_payload():
    with db() as conn:
        feeds = conn.execute("SELECT * FROM feeds ORDER BY category, title COLLATE NOCASE").fetchall()
        unread = {r["feed_id"]: r["n"] for r in conn.execute(
            "SELECT feed_id, COUNT(*) AS n FROM articles WHERE read = 0 GROUP BY feed_id")}
        totals = conn.execute(
            """SELECT COUNT(*) AS all_count,
                      SUM(read = 0) AS unread_count,
                      SUM(starred = 1) AS starred_count
               FROM articles"""
        ).fetchone()
        midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE published >= ? AND read = 0", (midnight,)
        ).fetchone()["n"]

    categories = {}
    order = []
    for f in feeds:
        cat = f["category"]
        if cat not in categories:
            categories[cat] = []
            order.append(cat)
        categories[cat].append({
            "id": f["id"],
            "title": f["title"],
            "html_url": f["html_url"],
            "unread": unread.get(f["id"], 0),
            "last_status": f["last_status"],
            "last_fetch": f["last_fetch"],
        })
    return {
        "categories": [
            {"name": cat, "feeds": categories[cat], "unread": sum(x["unread"] for x in categories[cat])}
            for cat in order
        ],
        "totals": {
            "all": totals["all_count"] or 0,
            "unread": totals["unread_count"] or 0,
            "starred": totals["starred_count"] or 0,
            "today": today or 0,
        },
        "refresh": dict(REFRESH_STATE),
    }


def articles_payload(params):
    where = []
    args = []
    scope = params.get("scope", ["all"])[0]
    scope_id = params.get("id", [None])[0]

    if scope == "feed" and scope_id:
        where.append("a.feed_id = ?")
        args.append(int(scope_id))
    elif scope == "category" and scope_id:
        where.append("f.category = ?")
        args.append(scope_id)
    elif scope == "starred":
        where.append("a.starred = 1")
    elif scope == "today":
        midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        where.append("a.published >= ?")
        args.append(midnight)

    if params.get("filter", ["all"])[0] == "unread" and scope != "starred":
        where.append("a.read = 0")

    q = (params.get("q", [""])[0] or "").strip()
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(a.title LIKE ? ESCAPE '\\' OR a.summary LIKE ? ESCAPE '\\')")
        args.extend([like, like])

    before_pub = params.get("before_pub", [None])[0]
    before_id = params.get("before_id", [None])[0]
    if before_pub is not None and before_id is not None:
        where.append("(a.published < ? OR (a.published = ? AND a.id < ?))")
        args.extend([int(before_pub), int(before_pub), int(before_id)])

    limit = max(1, min(int(params.get("limit", ["60"])[0]), 200))
    sql = (
        "SELECT a.id, a.feed_id, a.title, a.url, a.author, a.published, a.summary,"
        " a.image, a.read, a.starred, f.title AS feed_title, f.category"
        " FROM articles a JOIN feeds f ON f.id = a.feed_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.published DESC, a.id DESC LIMIT ?"
    args.append(limit)

    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    return {"articles": rows, "has_more": len(rows) == limit}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NERDReader/1.0"

    # -- helpers -----------------------------------------------------------

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _drain_body(self):
        """Lê o corpo inteiro (keep-alive exige). Retorna bytes, ou None se grande demais."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            return None
        return self.rfile.read(length)

    @staticmethod
    def _parse_json(raw):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _serve_static(self, rel):
        path = (STATIC_DIR / rel).resolve()
        if not path.is_relative_to(STATIC_DIR) or not path.is_file():
            self._send(404, {"error": "não encontrado"})
            return
        ctypes = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                  ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
                  ".png": "image/png", ".ico": "image/x-icon"}
        self._send(200, path.read_bytes(), ctypes.get(path.suffix, "application/octet-stream"))

    def log_message(self, fmt, *args):  # menos ruído no terminal
        pass

    # -- rotas -------------------------------------------------------------

    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._internal_error(e)

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._internal_error(e)

    def _internal_error(self, e):
        self.close_connection = True
        try:
            self._send(500, {"error": "erro interno: %s" % type(e).__name__})
        except Exception:
            pass

    def _route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_static("index.html")
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        elif path == "/api/state":
            self._send(200, state_payload())
        elif path == "/api/articles":
            try:
                self._send(200, articles_payload(params))
            except (ValueError, TypeError):
                self._send(400, {"error": "parâmetros inválidos"})
        elif re.fullmatch(r"/api/article/\d+", path):
            art_id = int(path.rsplit("/", 1)[1])
            with db() as conn:
                row = conn.execute(
                    """SELECT a.*, f.title AS feed_title, f.category, f.html_url AS feed_html_url
                       FROM articles a JOIN feeds f ON f.id = a.feed_id WHERE a.id = ?""",
                    (art_id,),
                ).fetchone()
            if row is None:
                self._send(404, {"error": "artigo não encontrado"})
            else:
                self._send(200, dict(row))
        elif path == "/api/opml/export":
            self._send(200, export_opml(), ctype="text/x-opml; charset=utf-8",
                       extra={"Content-Disposition": 'attachment; filename="nerd-reader.opml"'})
        else:
            self._send(404, {"error": "não encontrado"})

    def _route_post(self):
        path = urllib.parse.urlparse(self.path).path

        # drena o corpo SEMPRE (senão o keep-alive dessincroniza) e valida tamanho
        raw = self._drain_body()
        if raw is None:
            self.close_connection = True
            self._send(413, {"error": "corpo grande demais"})
            return

        # proteção CSRF: só o frontend manda este cabeçalho; um site malicioso
        # não consegue (cabeçalho custom exige preflight CORS, que negamos)
        if self.headers.get("X-NR") != "1":
            self._send(403, {"error": "requisição sem cabeçalho X-NR"})
            return

        if path == "/api/refresh":
            started = refresh_all()
            self._send(200, {"started": started, "refresh": dict(REFRESH_STATE)})

        elif path == "/api/mark":
            body = self._parse_json(raw)
            ids = [int(i) for i in body.get("ids", []) if str(i).lstrip("-").isdigit()][:1000]
            read = 1 if body.get("read", True) else 0
            if ids:
                with _db_lock, db() as conn:
                    conn.executemany("UPDATE articles SET read = ? WHERE id = ?", [(read, i) for i in ids])
            self._send(200, {"ok": True, "count": len(ids)})

        elif path == "/api/mark_all":
            body = self._parse_json(raw)
            scope = body.get("scope", "all")
            where, args = "read = 0", []
            try:
                if scope == "feed" and body.get("id"):
                    where += " AND feed_id = ?"
                    args.append(int(body["id"]))
                elif scope == "category" and body.get("category"):
                    where += " AND feed_id IN (SELECT id FROM feeds WHERE category = ?)"
                    args.append(str(body["category"]))
                elif scope == "today":
                    midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
                    where += " AND published >= ?"
                    args.append(midnight)
                before = body.get("before")
                if before:
                    where += " AND fetched_at <= ?"
                    args.append(int(before))
            except (TypeError, ValueError):
                self._send(400, {"error": "parâmetros inválidos"})
                return
            with _db_lock, db() as conn:
                cur = conn.execute("UPDATE articles SET read = 1 WHERE " + where, args)
            self._send(200, {"ok": True, "count": cur.rowcount})

        elif path == "/api/star":
            body = self._parse_json(raw)
            try:
                art_id = int(body.get("id"))
            except (TypeError, ValueError):
                self._send(400, {"error": "id inválido"})
                return
            starred = 1 if body.get("starred", True) else 0
            with _db_lock, db() as conn:
                conn.execute("UPDATE articles SET starred = ? WHERE id = ?", (starred, art_id))
            self._send(200, {"ok": True})

        elif path == "/api/feeds/add":
            body = self._parse_json(raw)
            url = (body.get("url") or "").strip()
            category = (body.get("category") or "Sem categoria").strip() or "Sem categoria"
            if not url.lower().startswith(("http://", "https://")):
                self._send(400, {"error": "URL inválida (precisa começar com http:// ou https://)"})
                return
            try:
                _, data, _ = http_get(url)
                parsed = parse_feed(data)
            except Exception as e:
                self._send(400, {"error": "não consegui ler esse feed: %s" % str(e)[:200]})
                return
            title = (body.get("title") or "").strip() or parsed["title"] or url
            with _db_lock, db() as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO feeds (title, xml_url, html_url, category, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (title, url, parsed.get("link") or None, category, int(time.time())),
                )
                feed_id = cur.lastrowid if cur.rowcount else None
            if feed_id is None:
                self._send(409, {"error": "esse feed já está assinado"})
                return
            threading.Thread(target=refresh_feed, args=(feed_id,), daemon=True).start()
            self._send(200, {"ok": True, "id": feed_id, "title": title})

        elif path == "/api/feeds/delete":
            body = self._parse_json(raw)
            try:
                feed_id = int(body.get("id"))
            except (TypeError, ValueError):
                self._send(400, {"error": "id inválido"})
                return
            with _db_lock, db() as conn:
                conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
            self._send(200, {"ok": True})

        elif path == "/api/opml/import":
            if not raw:
                self._send(400, {"error": "arquivo vazio"})
                return
            try:
                added = import_opml(raw)
            except Exception as e:
                self._send(400, {"error": "OPML inválido: %s" % str(e)[:200]})
                return
            if added:
                refresh_all()
            self._send(200, {"ok": True, "added": added})

        else:
            self._send(404, {"error": "não encontrado"})


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="NERD Reader — leitor RSS local")
    ap.add_argument("--port", type=int, default=8484, help="porta HTTP (padrão: 8484)")
    ap.add_argument("--host", default="127.0.0.1", help="host (padrão: 127.0.0.1, só a sua máquina)")
    ap.add_argument("--refresh", type=int, default=30, help="minutos entre atualizações automáticas (padrão: 30)")
    args = ap.parse_args()

    init_db()

    with db() as conn:
        n_feeds = conn.execute("SELECT COUNT(*) AS n FROM feeds").fetchone()["n"]
    if n_feeds == 0 and OPML_PATH.exists():
        added = import_opml(OPML_PATH.read_bytes())
        print("🦞 Importados %d feeds de %s" % (added, OPML_PATH.name))

    refresh_all()  # primeira carga em background
    threading.Thread(target=refresh_loop, args=(args.refresh,), daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("🦞 NERD Reader rodando em http://%s:%d  (Ctrl+C para parar)" % (args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAté mais!")


if __name__ == "__main__":
    main()
