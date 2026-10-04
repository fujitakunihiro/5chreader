import html
import json
import re
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

MENU_URL = "https://menu.5ch.io/bbsmenu.json"
USER_AGENT = "Mozilla/5.0 (compatible; Local5chReader/1.0)"
_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, factory):
    now = time.time()
    with _cache_lock:
        item = _cache.get(key)
        if item and item[0] > now:
            return item[1]
    value = factory()
    with _cache_lock:
        _cache[key] = (now + ttl, value)
    return value


def get_bytes(url):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
    with urlopen(request, timeout=20) as response:
        return response.read(8_000_000), response.headers.get_content_charset()


def get_text(url):
    data, charset = get_bytes(url)
    if charset:
        try:
            if charset.lower().replace("-", "_") in {"shift_jis", "shiftjis", "sjis", "cp932"}:
                return data.decode("cp932", errors="replace")
            return data.decode(charset, errors="replace")
        except LookupError:
            pass
    for encoding in ("utf-8", "cp932", "shift_jis"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("cp932", errors="replace")


def allowed_5ch_url(value):
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host.endswith(".5ch.io") or host.endswith(".5ch.net") or host == "5ch.io")


def clean_text(fragment):
    fragment = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", "", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)</(?:div|p|li|blockquote)\s*>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = html.unescape(fragment).replace("\xa0", " ")
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", fragment)).strip()


class BoardLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.active = {"href": dict(attrs).get("href", ""), "text": []}

    def handle_data(self, data):
        if self.active is not None:
            self.active["text"].append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.active is not None:
            self.links.append(self.active)
            self.active = None


def load_menu():
    raw = json.loads(get_text(MENU_URL))
    categories = []
    for group in raw.get("menu_list", []):
        boards = []
        seen = set()
        for item in group.get("category_content", []):
            name, url = item.get("board_name"), item.get("url")
            if not name or not url or not allowed_5ch_url(url):
                continue
            parsed = urlparse(url)
            key = f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
            if key in seen or parsed.path.rstrip("/").split("/")[-1] in {"subback", "public", "test"}:
                continue
            seen.add(key)
            boards.append({"name": name.strip(), "url": url.rstrip("/") + "/"})
        if boards:
            categories.append({"name": group.get("category_name", "板"), "boards": boards})
    return {"categories": categories, "updatedAt": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}


def board_list_url(board_url):
    if not allowed_5ch_url(board_url):
        raise ValueError("5ちゃんねるの板URLではありません")
    parsed = urlparse(board_url)
    board_path = parsed.path.rstrip("/")
    board_name = board_path.split("/")[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", board_name):
        raise ValueError("板URLを確認できません")
    return f"{parsed.scheme}://{parsed.netloc}{board_path}/subback.html", parsed.netloc, board_name


def load_threads(board_url):
    list_url, host, board_name = board_list_url(board_url)
    source = get_text(list_url)
    parser = BoardLinkParser()
    parser.feed(source)
    result, seen = [], set()
    for link in parser.links:
        href = html.unescape(link.get("href", ""))
        match = re.search(r"(?:^|/)(\d{8,})(?:/|$)", href)
        if not match:
            continue
        thread_id = match.group(1)
        if thread_id in seen:
            continue
        seen.add(thread_id)
        title = clean_text("".join(link["text"]))
        title = re.sub(r"^\s*\d+\s*[:：.]\s*", "", title)
        count_match = re.search(r"\(([\d,]+)\)\s*$", title)
        count = int(count_match.group(1).replace(",", "")) if count_match else None
        if count_match:
            title = title[:count_match.start()].strip()
        if not title:
            continue
        result.append({
            "id": thread_id,
            "title": title,
            "posts": count,
            "url": f"https://{host}/test/read.cgi/{board_name}/{thread_id}/l50",
        })
    return {"boardUrl": board_url, "threads": result, "updatedAt": time.strftime("%H:%M UTC", time.gmtime())}


def thread_page_url(value, start, end):
    if not allowed_5ch_url(value):
        raise ValueError("5ちゃんねるのスレッドURLではありません")
    parsed = urlparse(value)
    match = re.fullmatch(r"/test/read\.cgi/([A-Za-z0-9_-]+)/([0-9]+)(?:/(?:l50|[0-9]+-[0-9]+))?/?", parsed.path)
    if not match:
        raise ValueError("スレッドURLを確認できません")
    board, thread_id = match.groups()
    suffix = "l50" if start <= 0 or end <= 0 else f"{start}-{end}"
    return f"https://{parsed.netloc}/test/read.cgi/{board}/{thread_id}/{suffix}"


def load_posts(thread_url, start=0, end=0):
    fetch_url = thread_page_url(thread_url, start, end)
    source = get_text(fetch_url)
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", source)
    title = clean_text(title_match.group(1)) if title_match else "5ch スレッド"
    starts = list(re.finditer(r'<div\b(?=[^>]*\bid="(\d+)")(?=[^>]*\bclass="[^"]*\bpost\b[^"]*")[^>]*>', source, re.I))
    posts = []
    for index, marker in enumerate(starts):
        stop = starts[index + 1].start() if index + 1 < len(starts) else len(source)
        block = source[marker.start():stop]
        number = marker.group(1)
        user_match = re.search(r'(?is)class="postusername"[^>]*>.*?<b[^>]*>(.*?)</b>', block)
        date_match = re.search(r'(?is)class="date"[^>]*>(.*?)</span>', block)
        body_match = re.search(r'(?is)<div\b[^>]*class="post-content"[^>]*>', block)
        body = ""
        if body_match:
            content_start = body_match.end()
            depth = 1
            content_end = len(block)
            for tag in re.finditer(r"(?is)</?div\b[^>]*>", block[content_start:]):
                if tag.group(0).startswith("</"):
                    depth -= 1
                else:
                    depth += 1
                if depth == 0:
                    content_end = content_start + tag.start()
                    break
            body = clean_text(block[content_start:content_end])
        if body:
            posts.append({
                "number": int(number),
                "name": clean_text(user_match.group(1)) if user_match else "名無しさん",
                "date": clean_text(date_match.group(1)) if date_match else "",
                "body": body,
            })
    numbers = [post["number"] for post in posts]
    if 1 in numbers and ((start <= 0 and end <= 0 and len(numbers) > 50) or start > 1):
        numbers = [number for number in numbers if number != 1]
    if start > 1:
        posts = [post for post in posts if post["number"] != 1]
    range_start = min(numbers) if numbers else 0
    range_end = max(numbers) if numbers else 0
    return {"url": fetch_url, "title": title, "posts": posts, "rangeStart": range_start, "rangeEnd": range_end}


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/menu":
                self.respond(200, cached("menu", 600, load_menu))
                return
            if parsed.path == "/api/board":
                board_url = unquote(params.get("url", [""])[0])
                self.respond(200, cached("board:" + board_url, 60, lambda: load_threads(board_url)))
                return
            if parsed.path == "/api/thread":
                thread_url = unquote(params.get("url", [""])[0])
                start = max(0, int(params.get("start", [0])[0]))
                end = max(0, int(params.get("end", [0])[0]))
                cache_key = f"thread:{thread_url}:{start}:{end}"
                self.respond(200, cached(cache_key, 30, lambda: load_posts(thread_url, start, end)))
                return
            self.respond(404, {"error": "Not found"})
        except (ValueError, HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            self.respond(502, {"error": str(error)})
        except Exception as error:
            self.respond(500, {"error": "読み込みに失敗しました: " + str(error)})

    def log_message(self, fmt, *args):
        print("5ch reader API: " + (fmt % args))


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
