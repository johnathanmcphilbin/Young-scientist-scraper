import csv
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

URL = "https://stripeyste.com/qualified-projects"
OUT_CSV = "all_projects.csv"

@dataclass
class Project:
    title: str
    stand_number: int
    county: str
    school: str
    category: str
    project_type_raw: str
    project_type: str  # merged: Group or Individual


_FIELD_PATTERNS = {
    "stand_number": re.compile(r"Stand number:\s*([0-9]+)", re.IGNORECASE),
    "county": re.compile(r"County:\s*(.+)", re.IGNORECASE),
    "school": re.compile(r"School:\s*(.+)", re.IGNORECASE),
    "category": re.compile(r"Category:\s*(.+)", re.IGNORECASE),
    "project_type": re.compile(r"Project type:\s*(.+)", re.IGNORECASE),
}

_COUNTER_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def clean(s: str) -> str:
    return " ".join((s or "").strip().split())


def merge_project_type(raw: str) -> str:
    raw_l = (raw or "").strip().lower()
    return "Group" if raw_l.startswith("group") else "Individual"


def parse_counter(counter_text: str) -> Optional[Tuple[int, int]]:
    m = _COUNTER_RE.search(counter_text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def parse_project_block(text_block: str) -> Optional[Project]:
    text_block = (text_block or "").strip()
    if not text_block:
        return None

    lines = [ln.strip() for ln in text_block.splitlines() if ln.strip()]
    if not lines:
        return None
    title = clean(lines[0])

    extracted = {}
    for key, pat in _FIELD_PATTERNS.items():
        m = pat.search(text_block)
        if not m:
            return None
        extracted[key] = clean(m.group(1))

    try:
        stand_number = int(extracted["stand_number"])
    except ValueError:
        return None

    raw_type = extracted["project_type"]
    return Project(
        title=title,
        stand_number=stand_number,
        county=extracted["county"],
        school=extracted["school"],
        category=extracted["category"],
        project_type_raw=raw_type,
        project_type=merge_project_type(raw_type),
    )


def first_visible_text(page, selector_candidates: List[str], timeout_ms: int = 1500) -> Optional[str]:
    for sel in selector_candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.wait_for(state="visible", timeout=timeout_ms)
                txt = (loc.inner_text(timeout=timeout_ms) or "").strip()
                if txt:
                    return txt
        except PWTimeoutError:
            continue
        except Exception:
            continue
    return None


def click_first_available(page, selector_candidates: List[str], timeout_ms: int = 1500) -> bool:
    for sel in selector_candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=timeout_ms)
            loc.click(timeout=timeout_ms)
            return True
        except PWTimeoutError:
            continue
        except Exception:
            continue
    return False


def get_project_blocks(page) -> List[str]:
    """
    Find project “cards” by locating 'Stand number:' and walking up the DOM
    to an ancestor that contains all labels we need.
    """
    candidates = []
    for node in page.locator("text=Stand number:").all():
        try:
            handle = node.element_handle()
            if not handle:
                continue

            block = page.evaluate(
                """(el) => {
                    function hasAllLabels(n) {
                      const t = (n.innerText || "");
                      return t.includes("Stand number:") &&
                             t.includes("County:") &&
                             t.includes("School:") &&
                             t.includes("Category:") &&
                             t.includes("Project type:");
                    }
                    let cur = el;
                    for (let i=0; i<10 && cur; i++) {
                      if (hasAllLabels(cur)) return cur.innerText;
                      cur = cur.parentElement;
                    }
                    return null;
                }""",
                handle,
            )

            # Python uses "and" (not &&)
            if block and isinstance(block, str):
                candidates.append(block)
        except Exception:
            continue

    seen = set()
    out = []
    for c in candidates:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main():
    # de-dupe across pagination
    collected: Dict[Tuple[str, int, str], Project] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)

        counter_selectors = [
            "text=/\\d+\\s*\\/\\s*\\d+/",
            "[class*='counter']",
            "[class*='pagination'] >> text=/\\d+\\s*\\/\\s*\\d+/",
        ]
        next_selectors = [
            "a[aria-label='Next']",
            "button[aria-label='Next']",
            "a:has-text('Next')",
            "button:has-text('Next')",
            ".w-pagination-next",
            "[class*='next']",
            "[data-direction='next']",
        ]

        last_counter = None
        safety_clicks = 0

        while True:
            for b in get_project_blocks(page):
                pr = parse_project_block(b)
                if not pr:
                    continue
                key = (pr.title, pr.stand_number, pr.school)
                collected[key] = pr

            counter_text = first_visible_text(page, counter_selectors)
            counter = parse_counter(counter_text or "")
            if counter:
                current, total = counter
                if last_counter == counter:
                    break
                last_counter = counter
                if current >= total:
                    break

            if not click_first_available(page, next_selectors):
                break

            safety_clicks += 1
            if safety_clicks > 5000:
                break

            page.wait_for_timeout(700)

        browser.close()

    projects = sorted(
        collected.values(),
        key=lambda x: (x.category.lower(), x.project_type.lower(), x.stand_number),
    )

    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "category",
                "project_type",      # merged Group/Individual
                "stand_number",
                "title",
                "county",
                "school",
                "project_type_raw",  # original Group (2)/(3)
            ],
        )
        w.writeheader()
        for p in projects:
            w.writerow(
                {
                    "category": p.category,
                    "project_type": p.project_type,
                    "stand_number": p.stand_number,
                    "title": p.title,
                    "county": p.county,
                    "school": p.school,
                    "project_type_raw": p.project_type_raw,
                }
            )

    print(f"Wrote {len(projects)} rows to {OUT_CSV}")


if __name__ == "__main__":
    main()
