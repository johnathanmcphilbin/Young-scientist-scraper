import re
import csv
import json
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


URL = "https://stripeyste.com/qualified-projects"
SOCIAL_CATEGORY = "Social & Behavioural Sciences"

# ---- data model ----
@dataclass
class Project:
    title: str
    stand_number: int
    county: str
    school: str
    category: str
    project_type: str


# ---- parsing helpers ----
_FIELD_PATTERNS = {
    "stand_number": re.compile(r"Stand number:\s*([0-9]+)", re.IGNORECASE),
    "county": re.compile(r"County:\s*(.+)", re.IGNORECASE),
    "school": re.compile(r"School:\s*(.+)", re.IGNORECASE),
    "category": re.compile(r"Category:\s*(.+)", re.IGNORECASE),
    "project_type": re.compile(r"Project type:\s*(.+)", re.IGNORECASE),
}

_COUNTER_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def _clean_line(s: str) -> str:
    return " ".join(s.strip().split())


def parse_project_block(text_block: str) -> Optional[Project]:
    """
    The UI block usually looks like:
      <Title>
      Stand number:
      3400
      County:
      Dublin
      School:
      ...
      Category:
      Social & Behavioural Sciences
      Project type:
      Group (2)
      Watch video
    We parse from the whole innerText.
    """
    text_block = text_block.strip()
    if not text_block:
        return None

    # Title is typically the first non-empty line
    lines = [ln.strip() for ln in text_block.splitlines() if ln.strip()]
    if not lines:
        return None
    title = _clean_line(lines[0])

    # Extract fields from the full text block
    extracted: Dict[str, str] = {}
    for key, pat in _FIELD_PATTERNS.items():
        m = pat.search(text_block)
        if not m:
            return None
        extracted[key] = _clean_line(m.group(1))

    # Normalize types
    try:
        stand_number = int(extracted["stand_number"])
    except ValueError:
        return None

    return Project(
        title=title,
        stand_number=stand_number,
        county=extracted["county"],
        school=extracted["school"],
        category=extracted["category"],
        project_type=extracted["project_type"],
    )


def parse_counter(counter_text: str) -> Optional[Tuple[int, int]]:
    m = _COUNTER_RE.search(counter_text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# ---- playwright helpers ----
def first_visible_text(page, selector_candidates: List[str], timeout_ms: int = 1500) -> Optional[str]:
    for sel in selector_candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.wait_for(state="visible", timeout=timeout_ms)
                txt = loc.inner_text(timeout=timeout_ms)
                txt = (txt or "").strip()
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
    Grab likely project card text blocks.
    We look for chunks that contain all key labels.
    """
    # Broad approach: find any element containing "Stand number:" then take a reasonable ancestor.
    # We’ll try a few DOM shapes.
    candidates = []

    # 1) Ancestor containers of nodes containing "Stand number:"
    stand_nodes = page.locator("text=Stand number:").all()
    for node in stand_nodes:
        try:
            # Walk up a few levels and pick the first ancestor with all labels
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
                    for (let i=0; i<8 && cur; i++) {
                      if (hasAllLabels(cur)) return cur.innerText;
                      cur = cur.parentElement;
                    }
                    return null;
                }""",
                handle,
            )
            if block and isinstance(block, str):
                candidates.append(block)
        except Exception:
            continue

    # Deduplicate by exact text
    seen = set()
    out = []
    for c in candidates:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        # Selector guesses for the "X / Y" counter shown on the page (e.g., "1 / 92")
        counter_selectors = [
            "text=/\\d+\\s*\\/\\s*\\d+/",
            "[class*='counter']",
            "[class*='pagination'] >> text=/\\d+\\s*\\/\\s*\\d+/",
        ]

        # Selector guesses for "Next" navigation
        next_selectors = [
            "a[aria-label='Next']",
            "button[aria-label='Next']",
            "a:has-text('Next')",
            "button:has-text('Next')",
            ".w-pagination-next",
            "[class*='next']",
            "[data-direction='next']",
        ]

        # We'll loop until we reach the end (using counter), or until "Next" stops working.
        all_projects: Dict[Tuple[str, int, str], Project] = {}
        last_counter = None
        safety_clicks = 0

        while True:
            # Collect projects visible on this view/page
            blocks = get_project_blocks(page)
            for b in blocks:
                pr = parse_project_block(b)
                if not pr:
                    continue
                # Keep only Social category
                if pr.category.strip() != SOCIAL_CATEGORY:
                    continue
                key = (pr.title, pr.stand_number, pr.school)
                all_projects[key] = pr

            # Read counter (e.g., "1 / 92")
            counter_text = first_visible_text(page, counter_selectors)
            counter = parse_counter(counter_text or "")

            # Stop condition via counter if possible
            if counter:
                current, total = counter
                if last_counter == counter:
                    # counter didn't change; maybe "Next" isn't moving anymore
                    break
                last_counter = counter
                if current >= total:
                    break

            # Try to go next
            moved = click_first_available(page, next_selectors)
            if not moved:
                break

            safety_clicks += 1
            if safety_clicks > 500:  # hard safety
                break

            # small wait for DOM to update
            page.wait_for_timeout(800)

        browser.close()

    projects = list(all_projects.values())

    # Group by project_type (sections) and sort each by stand number
    sections: Dict[str, List[Project]] = {}
    for pr in projects:
        sections.setdefault(pr.project_type, []).append(pr)

    for k in sections:
        sections[k].sort(key=lambda x: x.stand_number)

    # Write JSON (structured by section)
    json_out = {
        "source": URL,
        "filtered_category": SOCIAL_CATEGORY,
        "generated_at_unix": int(time.time()),
        "sections": {
            section: [asdict(p) for p in items]
            for section, items in sorted(sections.items(), key=lambda kv: kv[0])
        },
    }
    with open("social_projects.json", "w", encoding="utf-8") as f:
        json.dump(json_out, f, ensure_ascii=False, indent=2)

    # Write CSV (flat)
    with open("social_projects.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["project_type", "stand_number", "title", "county", "school", "category"],
        )
        w.writeheader()
        for section, items in sorted(sections.items(), key=lambda kv: kv[0]):
            for p in items:
                w.writerow(
                    {
                        "project_type": section,
                        "stand_number": p.stand_number,
                        "title": p.title,
                        "county": p.county,
                        "school": p.school,
                        "category": p.category,
                    }
                )

    # Console summary
    total = sum(len(v) for v in sections.values())
    print(f"Saved {total} '{SOCIAL_CATEGORY}' projects into:")
    print("  - social_projects.json")
    print("  - social_projects.csv")
    for section, items in sorted(sections.items(), key=lambda kv: kv[0]):
        if items:
            print(f"  * {section}: {len(items)} (stand range {items[0].stand_number}–{items[-1].stand_number})")


if __name__ == "__main__":
    main()
