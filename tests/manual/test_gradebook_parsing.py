"""Manual test for gradebook HTML parsing."""

import re

from bs4 import BeautifulSoup


def test_parse_register_total():
    with open("/mnt/extra/github/filoutil/tests/manual/fetched_page.html", "r") as f:
        html = f.read()

    soup = BeautifulSoup(html, "html.parser")

    tbody = soup.find("tbody")
    assert tbody, "tbody not found"

    # Find rows by looking for th with id="row_X_Y" pattern
    # This is more reliable than class-based matching
    all_ths_with_id = tbody.find_all("th", id=re.compile(r"^row_\d+_\d+$"))

    rows = []
    seen_rows = set()
    for th in all_ths_with_id:
        row = th.find_parent("tr")
        if row and id(row) not in seen_rows:
            rows.append(row)
            seen_rows.add(id(row))

    print(f"Found {len(rows)} rows with item/categoryitem class")

    grades = []
    for row in rows:
        if "spacer" in row.get("class", []):
            continue

        row_th = row.find("th", id=re.compile(r"^row_\d+_\d+$"))
        if not row_th or not row_th.get("id"):
            print(f"Skipping row: no row_th or id")
            continue

        row_id = row_th.get("id")
        match = re.match(r"row_(\d+)_\d+", row_id)
        if not match:
            print(f"Skipping row: no match for row_id={row_id}")
            continue

        grade_item_id = int(match.group(1))

        item_name_elem = row_th.find("span", class_="gradeitemheader") or row_th.find(
            "a", class_="gradeitemheader"
        )
        if not item_name_elem:
            print(f"Skipping row: no item_name_elem")
            continue

        item_name = item_name_elem.get_text(strip=True)

        grade_cell = row.find("td", class_=re.compile(r"column-grade\b"))
        grade_text = grade_cell.get_text(strip=True) if grade_cell else "-"

        if "Grade analysis" in grade_text:
            grade_text = grade_text.replace("Grade analysis", "").strip()

        grades.append(
            {
                "grade_item_id": grade_item_id,
                "item_name": item_name,
                "grade": grade_text,
            }
        )

        print(f"Parsed: {item_name} -> {grade_text} (id={grade_item_id})")

    print(f"\n=== TOTAL: {len(grades)} grades ===")

    register_items = [g for g in grades if "register" in g["item_name"].lower()]
    print(f"\n=== Register items ({len(register_items)}) ===")
    for g in register_items:
        print(f"  {g['item_name']} -> {g['grade']}")

    assert any(
        "register total" in g["item_name"].lower()
        or "register(not to edit) total" in g["item_name"].lower()
        for g in grades
    ), "Register total not found in parsed grades!"


if __name__ == "__main__":
    test_parse_register_total()
