#!/usr/bin/env python3
"""
Migrates Domo internal documentation articles to MDX format.

Reads Internal_Docs.csv, filters to articles marked for "Mintlify", fetches
HTML bodies from the Salesforce Knowledge API, converts each article to MDX,
and downloads images.

Outputs:
  s/article/<id>.mdx  — converted MDX articles
  images/kb/          — downloaded Salesforce images

Prerequisites:
  pip install requests markdownify beautifulsoup4 python-dotenv

Setup:
  1. Copy .env.example to .env and fill in SF_ACCESS_TOKEN.
  2. python scripts/migrate_internal_docs.py --dry-run
  3. python scripts/migrate_internal_docs.py [--skip-images] [--limit N]

Articles in the "Triage 101" section (domo.domo.com URLs) cannot be fetched
automatically — they live in an internal Domo page, not Salesforce Knowledge.
The script prints a list of these for manual migration using the /migrate-html
skill in Claude Code.

If the Salesforce object or body field name is wrong for your instance, pass
--describe <ObjectName> to list all available fields on that object, then set
SF_KNOWLEDGE_OBJECT and SF_BODY_FIELD in .env accordingly.
"""

import os
import re
import sys
import csv
import logging
import argparse
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
REPO_ROOT = SCRIPTS_DIR.parent

# Add scripts/ to sys.path so sibling modules can be imported
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import requests
except ImportError:
    print("ERROR: requests required. Run: pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass  # python-dotenv is optional; credentials can be set directly in os.environ

# ---------------------------------------------------------------------------
# Configuration (all overridable via .env)
# ---------------------------------------------------------------------------

INTERNAL_DOCS_CSV = REPO_ROOT / "Internal_Docs.csv"
ARTICLES_DIR = REPO_ROOT / "s" / "article"
IMAGES_DIR = REPO_ROOT / "images" / "kb"

# Salesforce instance URL — typically https://<org>.my.salesforce.com
SF_INSTANCE_URL = os.environ.get("SF_INSTANCE_URL", "https://domo.my.salesforce.com")

# Salesforce API version
SF_API_VERSION = os.environ.get("SF_API_VERSION", "v59.0")

# Knowledge article object name. Use --describe <name> to inspect field names.
SF_KNOWLEDGE_OBJECT = os.environ.get("SF_KNOWLEDGE_OBJECT", "Knowledge__kav")

# Body field candidates tried in order when SF_BODY_FIELD is not set explicitly.
_BODY_FIELD_CANDIDATES = ["Body", "ArticleBody", "Article_Body__c", "ARTICLE_BODY__C"]
SF_BODY_FIELD = os.environ.get("SF_BODY_FIELD")  # set to skip auto-detection

# Optional separate SID for domo.file.force.com image downloads.
# If not set, SF_ACCESS_TOKEN is tried for images too.
# Get this SID by opening a Salesforce image URL in your browser while logged in,
# then copying the 'sid' cookie from the domo.file.force.com domain in devtools.
SF_IMAGE_SID = os.environ.get("SF_IMAGE_SID")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSV loading and filtering
# ---------------------------------------------------------------------------

_SF_SUPPORT_HOST = "domo-support.domo.com"
_DOMO_INTERNAL_HOST = "domo.domo.com"


def load_mintlify_articles(csv_path: Path) -> tuple[list[dict], list[dict]]:
    """
    Load Internal_Docs.csv and return (kb_articles, manual_articles).

    kb_articles     — Mintlify-destined rows with domo-support.domo.com URLs
    manual_articles — Mintlify-destined rows with domo.domo.com URLs (internal pages)
    """
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    kb: list[dict] = []
    manual: list[dict] = []

    for row in rows:
        dest = row.get("Destination", "").strip()
        if not dest.lower().startswith("mintlify"):
            continue
        url = row.get("URL of Location", "").strip()
        if _SF_SUPPORT_HOST in url:
            kb.append(row)
        elif _DOMO_INTERNAL_HOST in url:
            manual.append(row)
        else:
            log.warning("Unknown URL format — skipping: %s", url or "(empty)")

    return kb, manual


# ---------------------------------------------------------------------------
# URL slug extraction and filename normalisation
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"/s/article/([^/?#]+)")


def extract_slug(url: str) -> str | None:
    """Extract the article URL slug from a domo-support.domo.com article URL."""
    m = _SLUG_RE.search(url)
    return m.group(1) if m else None


def normalize_slug(slug: str) -> str:
    """
    Map a URL slug to a repo filename stem.

    Numeric IDs with fewer than 10 digits are zero-padded to 9 characters to
    match the s/article/ naming convention (e.g. "5780" → "000005780").
    Longer numeric IDs and non-numeric slugs are returned unchanged.
    """
    try:
        n = int(slug)
        if 0 < n < 1_000_000_000:
            return str(n).zfill(9)
    except ValueError:
        pass
    return slug


# ---------------------------------------------------------------------------
# Salesforce Knowledge API client
# ---------------------------------------------------------------------------

class SalesforceKnowledgeClient:
    """
    Fetches Knowledge article HTML from Salesforce using a session token.

    Salesforce accepts both OAuth access tokens and browser session IDs (SID)
    as Bearer token values — either works here.
    """

    def __init__(self, access_token: str, instance_url: str = SF_INSTANCE_URL):
        self.instance_url = instance_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )
        self._api_base = f"{self.instance_url}/services/data/{SF_API_VERSION}"
        # Cached once the correct field name is found
        self._resolved_body_field: str | None = SF_BODY_FIELD

    def _soql(self, query: str) -> dict:
        resp = self._session.get(
            f"{self._api_base}/query/",
            params={"q": query},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_article(
        self,
        url_slug: str,
        object_name: str = SF_KNOWLEDGE_OBJECT,
        language: str = "en_US",
    ) -> tuple[str, str] | None:
        """
        Fetch a Knowledge article by its URL slug.

        If SF_BODY_FIELD is not set, tries each candidate field name until one
        succeeds. The resolved field name is cached for subsequent calls.

        Returns (title, html_body) on success, None if not found or on error.
        """
        candidates = (
            [self._resolved_body_field]
            if self._resolved_body_field
            else _BODY_FIELD_CANDIDATES
        )

        for body_field in candidates:
            query = (
                f"SELECT Title, UrlName, {body_field} "
                f"FROM {object_name} "
                f"WHERE UrlName = '{url_slug}' "
                f"AND PublishStatus = 'Online' "
                f"AND Language = '{language}' "
                f"LIMIT 1"
            )
            try:
                result = self._soql(query)
                records = result.get("records", [])
                if records:
                    self._resolved_body_field = body_field
                    if body_field != SF_BODY_FIELD and not self._resolved_body_field:
                        log.info("Resolved body field: %s.%s", object_name, body_field)
                    rec = records[0]
                    return rec.get("Title", url_slug), rec.get(body_field) or ""
                # Article not found with this field — don't try other fields
                return None
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status == 400:
                    # Invalid field name — try the next candidate
                    log.debug("Field '%s' not valid on %s, trying next…", body_field, object_name)
                    continue
                log.error(
                    "HTTP %s fetching '%s': %s",
                    status,
                    url_slug,
                    exc.response.text[:200] if exc.response is not None else str(exc),
                )
                return None
            except Exception as exc:
                log.error("Error fetching '%s': %s", url_slug, exc)
                return None

        log.error(
            "Could not find a valid body field on %s. "
            "Set SF_BODY_FIELD in .env (run --describe %s to list available fields).",
            object_name,
            object_name,
        )
        return None

    def describe_object(self, object_name: str) -> None:
        """Print all fields on a Salesforce object — useful for finding the body field."""
        try:
            resp = self._session.get(
                f"{self._api_base}/sobjects/{object_name}/describe",
                timeout=30,
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            print(f"ERROR: {exc}")
            if exc.response is not None:
                print(exc.response.text[:500])
            return

        fields = resp.json().get("fields", [])
        print(f"\nFields on {object_name} ({len(fields)} total):")
        for f in sorted(fields, key=lambda x: x["name"]):
            print(f"  {f['name']:<45}  {f['type']}")
        print(
            f"\nIf the HTML body field is not obvious from the name, look for a 'textarea' or "
            f"'richTextarea' type. Set SF_BODY_FIELD=<name> in .env."
        )


# ---------------------------------------------------------------------------
# Core migration
# ---------------------------------------------------------------------------

_IMG_SRC_RE = re.compile(r"""<img[^>]*?\ssrc=["']([^"']+)["'][^>]*?>""", re.IGNORECASE)


def extract_image_urls(html: str) -> list[str]:
    """Return a deduplicated list of image src URLs found in the HTML."""
    seen: set[str] = set()
    result: list[str] = []
    for url in _IMG_SRC_RE.findall(html):
        canonical = url.replace("&amp;", "&")
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def _write_mdx(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Images-only patch pass (no API token needed)
# ---------------------------------------------------------------------------

_TODO_RE = re.compile(
    r"<!-- TODO: embed image → (https?://[^\s>]+) -->", re.IGNORECASE
)
_IMAGE_BASE_PATH = "/images/kb"


def patch_images(image_sid: str | None = None) -> None:
    """
    Scan every MDX file in s/article/ for '<!-- TODO: embed image → <url> -->'
    comments, download those images using SF_IMAGE_SID, and replace the comments
    with proper <Frame>![Screenshot](/images/kb/<filename>)</Frame> blocks.

    Does not need an API token — only SF_IMAGE_SID (or --image-sid flag).
    """
    from image_downloader import SalesforceImageDownloader

    sid = image_sid or SF_IMAGE_SID
    if not sid:
        log.error(
            "No image SID available. Set SF_IMAGE_SID in .env or pass --image-sid."
        )
        return

    mdx_files = sorted(ARTICLES_DIR.glob("*.mdx"))
    if not mdx_files:
        log.info("No MDX files found in %s", ARTICLES_DIR)
        return

    # Collect all unique image URLs across all files
    url_to_files: dict[str, list[Path]] = {}
    for mdx_path in mdx_files:
        text = mdx_path.read_text(encoding="utf-8")
        for m in _TODO_RE.finditer(text):
            url = m.group(1)
            url_to_files.setdefault(url, []).append(mdx_path)

    if not url_to_files:
        log.info("No TODO image comments found in any MDX file.")
        return

    all_urls = list(url_to_files)
    log.info("Found %d unique TODO image URLs across %d files", len(all_urls), len(mdx_files))

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    downloader = SalesforceImageDownloader(sid, IMAGES_DIR)
    url_to_local = downloader.download_all(all_urls)
    downloader.report()

    if not url_to_local:
        log.error("No images downloaded — check that SF_IMAGE_SID is valid.")
        return

    # Patch each MDX file that had at least one successful download
    files_patched = 0
    for mdx_path in mdx_files:
        text = mdx_path.read_text(encoding="utf-8")
        original = text

        def _replace(m: re.Match) -> str:
            url = m.group(1)
            local = url_to_local.get(url)
            if not local:
                return m.group(0)  # keep the comment if download failed
            return f"<Frame>![Screenshot]({_IMAGE_BASE_PATH}/{local})</Frame>"

        text = _TODO_RE.sub(_replace, text)
        if text != original:
            _write_mdx(mdx_path, text)
            replaced = len(_TODO_RE.findall(original)) - len(_TODO_RE.findall(text))
            log.info("  Patched %s  (%d image(s) embedded)", mdx_path.name, replaced)
            files_patched += 1

    remaining = sum(1 for f in ARTICLES_DIR.glob("*.mdx")
                    for _ in _TODO_RE.findall(f.read_text(encoding="utf-8")))
    log.info("")
    log.info("=== IMAGES-ONLY SUMMARY ===")
    log.info("  Files patched:            %d", files_patched)
    log.info("  TODO comments remaining:  %d", remaining)
    if remaining:
        log.info("  (remaining TODOs = images that still failed to download)")


def migrate(
    access_token: str | None,
    *,
    csv_path: Path = INTERNAL_DOCS_CSV,
    dry_run: bool = False,
    skip_images: bool = False,
    limit: int | None = None,
) -> None:
    from html_to_mdx import html_to_mdx
    from image_downloader import SalesforceImageDownloader

    kb_articles, manual_articles = load_mintlify_articles(csv_path)

    log.info(
        "Mintlify articles: %d KB (Salesforce) + %d manual (Triage 101 / internal Domo pages)",
        len(kb_articles),
        len(manual_articles),
    )

    # ---- Report manual articles that need /migrate-html ----
    if manual_articles:
        log.info("")
        log.info(
            "The following %d articles require manual migration using /migrate-html",
            len(manual_articles),
        )
        log.info("(They live in internal Domo pages, not Salesforce Knowledge)")
        for row in manual_articles:
            log.info(
                "  [%s]  %s",
                row.get("Category", "?"),
                row.get("Documentation Name", "?"),
            )

    if not kb_articles:
        log.info("No KB articles to process.")
        return

    if limit:
        kb_articles = kb_articles[:limit]
        log.info("Limiting to first %d KB articles (--limit)", limit)

    # ---- Set up Salesforce client and image downloader ----
    client = SalesforceKnowledgeClient(access_token, SF_INSTANCE_URL) if access_token else None
    downloader = None
    image_sid = SF_IMAGE_SID or access_token
    if image_sid and not skip_images and not dry_run:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        downloader = SalesforceImageDownloader(image_sid, IMAGES_DIR)

    stats = {"written": 0, "skipped": 0, "failed": 0}
    log.info("")
    log.info("Processing %d KB articles…", len(kb_articles))

    for row in kb_articles:
        name = row.get("Documentation Name", "?")
        url = row.get("URL of Location", "")
        notes = row.get("Notes", "").strip()

        slug = extract_slug(url)
        if not slug:
            log.warning("  SKIP — no slug found in URL: %s  (%s)", url, name)
            stats["skipped"] += 1
            continue

        filename = normalize_slug(slug)
        output_path = ARTICLES_DIR / f"{filename}.mdx"

        label = f"[{filename}] {name[:55]}"
        log.info("  %s", label)

        if notes:
            log.info("    Note: %s", notes[:100])

        if dry_run:
            log.info("    → %s", output_path.relative_to(REPO_ROOT))
            stats["written"] += 1
            continue

        if client is None:
            log.warning("    No access token — cannot fetch. Pass --access-token or set SF_ACCESS_TOKEN.")
            stats["skipped"] += 1
            continue

        result = client.fetch_article(slug)
        if result is None:
            log.error("    Fetch failed. Article not found or API error.")
            stats["failed"] += 1
            continue

        title, html = result

        image_local_map: dict[str, str] = {}
        if html and downloader:
            img_urls = extract_image_urls(html)
            if img_urls:
                log.info("    Downloading %d image(s)…", len(img_urls))
                image_local_map = downloader.download_all(img_urls)
        elif html and skip_images:
            img_count = len(extract_image_urls(html))
            if img_count:
                log.info("    Skipping %d image(s) (--skip-images)", img_count)

        try:
            mdx = html_to_mdx(html, title, image_local_map, language="en_US")
        except Exception as exc:
            log.error("    Conversion failed: %s", exc)
            stats["failed"] += 1
            continue

        _write_mdx(output_path, mdx)
        log.info("    Written: %s", output_path.name)
        stats["written"] += 1

    if downloader:
        downloader.report()

    log.info("")
    log.info("=== SUMMARY ===")
    log.info("  Written:  %d", stats["written"])
    log.info("  Skipped:  %d (no slug or no token)", stats["skipped"])
    log.info("  Failed:   %d (fetch or conversion error)", stats["failed"])

    if not dry_run and stats["written"]:
        log.info("")
        log.info("Next steps:")
        log.info(
            "  1. Review TODO comments in converted files "
            "(images that couldn't be downloaded appear as <!-- TODO: embed image --> comments)."
        )
        log.info(
            "  2. Run /csv-to-mdx <path> in Claude Code to audit each article "
            "against the style guide."
        )
        log.info(
            "  3. Commit: git add s/article/ images/kb/ && git commit"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate Domo internal KB articles from Salesforce to MDX. "
            "Reads Internal_Docs.csv, filters for Mintlify-destined articles, "
            "and converts each to s/article/<id>.mdx."
        )
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("SF_ACCESS_TOKEN"),
        metavar="TOKEN",
        help=(
            "Salesforce access token or session ID (SID). "
            "Can also be set via SF_ACCESS_TOKEN in .env."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without fetching or creating any files.",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help=(
            "Skip image downloads. Images appear as "
            "'<!-- TODO: embed image → … -->' comments in the MDX."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Process at most N KB articles (useful for testing a small batch first).",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=str(INTERNAL_DOCS_CSV),
        help=f"Path to the planning CSV (default: {INTERNAL_DOCS_CSV.name}).",
    )
    parser.add_argument(
        "--describe",
        metavar="OBJECT",
        help=(
            "Print all fields on a Salesforce object and exit "
            "(e.g. --describe Knowledge__kav). "
            "Requires --access-token."
        ),
    )
    parser.add_argument(
        "--images-only",
        action="store_true",
        help=(
            "Skip article fetching entirely. Scan existing MDX files in s/article/ "
            "for '<!-- TODO: embed image -->' comments, download those images using "
            "SF_IMAGE_SID (or --image-sid), and patch the files in place. "
            "No SF_ACCESS_TOKEN required."
        ),
    )
    parser.add_argument(
        "--image-sid",
        default=os.environ.get("SF_IMAGE_SID"),
        metavar="SID",
        help=(
            "Session ID for domo.file.force.com image downloads. "
            "Can also be set via SF_IMAGE_SID in .env."
        ),
    )

    args = parser.parse_args()

    if args.describe:
        if not args.access_token:
            parser.error("--access-token is required with --describe")
        SalesforceKnowledgeClient(args.access_token).describe_object(args.describe)
        return

    if args.images_only:
        patch_images(image_sid=args.image_sid)
        return

    if not args.dry_run and not args.access_token:
        log.warning(
            "No access token provided — articles cannot be fetched. "
            "Set SF_ACCESS_TOKEN in .env or pass --access-token. "
            "Use --dry-run to preview the migration plan without credentials."
        )

    migrate(
        access_token=args.access_token,
        csv_path=Path(args.csv),
        dry_run=args.dry_run,
        skip_images=args.skip_images,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
