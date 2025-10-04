from __future__ import annotations
import json, os, re, time, math, logging
from pathlib import Path
from typing import Dict, List, Iterable, Tuple, Optional

import yaml
import requests

log = logging.getLogger(__name__)

# -------------------------
# Text cleaning & chunking
# -------------------------

_MD_FRONTMATTER = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)

def normalize_text(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned = []
    for ln in lines:
        if not ln:
            continue
        if sum(ch.isalnum() for ch in ln) < 3:
            continue
        cleaned.append(ln)
    s = "\n".join(cleaned)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def md_to_text(md: str) -> str:
    md = re.sub(_MD_FRONTMATTER, "", md)
    md = re.sub(r"```.*?```", "", md, flags=re.DOTALL)  # drop fenced code
    md = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", md)        # drop images
    md = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md)    # links -> label
    md = re.sub(r"^\s{0,3}#{1,6}\s*", "", md, flags=re.MULTILINE)
    md = md.replace("`", "")
    md = re.sub(r"^\s*[-*+]\s+", "• ", md, flags=re.MULTILINE)
    md = re.sub(r"^\s*>\s?", "", md, flags=re.MULTILINE)
    return normalize_text(md)

def chunk_text(text: str, max_chars: int = 800, overlap: int = 120) -> List[str]:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out: List[str] = []
    buf = ""
    for p in paras:
        if len(p) > max_chars:
            i = 0
            while i < len(p):
                j = min(i + max_chars, len(p))
                out.append(p[i:j])
                i = j - overlap if j - overlap > i else j
            continue
        if len(buf) + 2 + len(p) <= max_chars:
            buf = (buf + "\n\n" + p) if buf else p
        else:
            if buf:
                out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out

def write_jsonl(records: Iterable[Dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

# -------------------------
# GitHub API helpers
# -------------------------

def gh_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "User-Agent": "matrix-ai-rag-builder/1.0",
    })
    tok = os.getenv("GITHUB_TOKEN")
    if tok:
        s.headers["Authorization"] = f"Bearer {tok}"
    return s

def gh_get_json(url: str, sess: requests.Session, max_retries: int = 3) -> Dict | List:
    backoff = 1.0
    for attempt in range(max_retries):
        r = sess.get(url, timeout=25)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            log.warning("GitHub rate-limited; sleeping %.1fs", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}

def gh_list_org_repos(org: str, sess: requests.Session) -> List[Dict]:
    repos: List[Dict] = []
    page = 1
    while True:
        url = f"https://api.github.com/orgs/{org}/repos?per_page=100&page={page}"
        js = gh_get_json(url, sess)
        if not js:
            break
        repos.extend(js)
        if len(js) < 100:
            break
        page += 1
    return repos

def gh_list_tree(owner: str, repo: str, branch: str, sess: requests.Session) -> List[Dict]:
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    js = gh_get_json(url, sess)
    return js.get("tree", []) if isinstance(js, dict) else []

def gh_fetch_raw(owner: str, repo: str, branch: str, path: str, sess: requests.Session) -> Optional[str]:
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    r = sess.get(raw_url, timeout=25)
    if r.status_code == 404 and branch == "main":  # try master fallback
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/master/{path}"
        r = sess.get(raw_url, timeout=25)
    if r.status_code == 200:
        return r.text
    return None

# -------------------------
# Builders
# -------------------------

def ingest_github_repo(owner: str, name: str, branch: str, docs_paths: List[str],
                       include_readme: bool, exts: Tuple[str,...] = (".md",".mdx",".txt")) -> List[Tuple[str,str]]:
    sess = gh_session()
    out: List[Tuple[str,str]] = []

    # README
    if include_readme:
        for candidate in ("README.md", "readme.md", "README.MD"):
            t = gh_fetch_raw(owner, name, branch, candidate, sess)
            if t:
                out.append((f"github:{owner}/{name}/{candidate}", md_to_text(t)))
                break

    # Tree -> docs paths
    tree = gh_list_tree(owner, name, branch, sess)
    if not tree:
        return out

    wanted_dirs = [p.strip("/").lower() for p in docs_paths]
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path", "")
        lower = path.lower()
        if not lower.endswith(exts):
            continue
        if any(lower.startswith(d + "/") for d in wanted_dirs):
            t = gh_fetch_raw(owner, name, branch, path, sess)
            if not t:
                continue
            txt = md_to_text(t) if lower.endswith((".md",".mdx")) else normalize_text(t)
            if txt:
                out.append((f"github:{owner}/{name}/{path}", txt))
    return out

def ingest_github_sources(cfg: Dict) -> List[Tuple[str,str]]:
    out: List[Tuple[str,str]] = []
    gh = cfg.get("github") or {}
    sess = gh_session()

    # explicit repos
    for repo in (gh.get("repos") or []):
        owner = repo["owner"]
        name = repo["name"]
        branch = repo.get("branch", "main")
        docs_paths = repo.get("docs_paths", ["docs"])
        include_readme = bool(repo.get("include_readme", True))
        out.extend(ingest_github_repo(owner, name, branch, docs_paths, include_readme))

    # whole org scan (README + docs/)
    for org in (gh.get("orgs") or []):
        try:
            repos = gh_list_org_repos(org, sess)
        except Exception as e:
            log.warning("Failed to list org %s: %s", org, e)
            continue
        for r in repos:
            owner = r["owner"]["login"]
            name = r["name"]
            default_branch = r.get("default_branch", "main")
            # README + docs/
            out.extend(ingest_github_repo(owner, name, default_branch, ["docs"], include_readme=True))
    return out

def ingest_local_sources(cfg: Dict) -> List[Tuple[str,str]]:
    out: List[Tuple[str,str]] = []
    local = cfg.get("local") or {}
    paths = local.get("paths") or []
    glob_pat = local.get("glob", "**/*.md")
    for p in paths:
        fp = Path(p)
        if fp.is_file():
            try:
                raw = fp.read_text(encoding="utf-8", errors="ignore")
                txt = md_to_text(raw) if fp.suffix.lower() in {".md",".mdx"} else normalize_text(raw)
                if txt:
                    out.append((str(fp), txt))
            except Exception as e:
                log.warning("Failed reading %s: %s", fp, e)
        elif fp.is_dir():
            for f in fp.rglob(glob_pat):
                try:
                    raw = f.read_text(encoding="utf-8", errors="ignore")
                    txt = md_to_text(raw) if f.suffix.lower() in {".md",".mdx"} else normalize_text(raw)
                    if txt:
                        out.append((str(f), txt))
                except Exception as e:
                    log.warning("Failed reading %s: %s", f, e)
    return out

def build_kb_from_config(config_path: str = "configs/rag_sources.yaml",
                         out_jsonl: str = "data/kb.jsonl",
                         max_chars: int = 800,
                         overlap: int = 120,
                         minlen: int = 200,
                         dedupe: bool = True) -> int:
    cfg: Dict = {}
    p = Path(config_path)
    if p.exists():
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    else:
        log.warning("rag_sources.yaml not found at %s (using defaults)", p)

    records: List[Dict] = []

    # GitHub
    try:
        gh_docs = ingest_github_sources(cfg)
        for src, text in gh_docs:
            for chunk in chunk_text(text, max_chars, overlap):
                if len(chunk) >= minlen:
                    records.append({"text": chunk, "source": src})
    except Exception as e:
        log.warning("GitHub ingest failed: %s", e)

    # Local
    try:
        loc_docs = ingest_local_sources(cfg)
        for src, text in loc_docs:
            for chunk in chunk_text(text, max_chars, overlap):
                if len(chunk) >= minlen:
                    records.append({"text": chunk, "source": src})
    except Exception as e:
        log.warning("Local ingest failed: %s", e)

    # URLs (optional)
    for url in (cfg.get("urls") or []):
        try:
            r = requests.get(url, timeout=25)
            r.raise_for_status()
            txt = normalize_text(r.text)
            for chunk in chunk_text(txt, max_chars, overlap):
                if len(chunk) >= minlen:
                    records.append({"text": chunk, "source": url})
        except Exception as e:
            log.warning("URL ingest failed for %s: %s", url, e)

    if dedupe:
        seen = set()
        deduped: List[Dict] = []
        for rec in records:
            h = hash(rec["text"])
            if h in seen:
                continue
            seen.add(h)
            deduped.append(rec)
        records = deduped

    if not records:
        log.warning("No KB records produced.")
        return 0

    out_path = Path(out_jsonl)
    write_jsonl(records, out_path)
    log.info("Wrote %d chunks to %s", len(records), out_path)
    return len(records)

def ensure_kb(out_jsonl: str = "data/kb.jsonl",
              config_path: str = "configs/rag_sources.yaml",
              skip_if_exists: bool = True) -> bool:
    """
    If kb.jsonl exists -> return True.
    Else -> build from config and return True on success.
    """
    out = Path(out_jsonl)
    if skip_if_exists and out.exists() and out.stat().st_size > 0:
        log.info("KB already present at %s (skipping build)", out)
        return True
    n = build_kb_from_config(config_path=config_path, out_jsonl=out_jsonl)
    return n > 0
