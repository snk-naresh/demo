#====================================================================
# Trial 4 
#====================================================================

#!/usr/bin/env python3
"""
search_eci_rolls.py

Download a numbered range of ECI electoral-roll PDFs, search each page for a
target name, and save a screenshot (image crop) of the entire matching row.

Usage examples:
    python search_eci_rolls.py --start 1 --end 500 --name "krishna rao"
    python search_eci_rolls.py --start 1 --end 500 --name "krishna rao" --ocr
    python search_eci_rolls.py --start 1 --end 20 --keep-pdfs --delay 1.0

Dependencies:
    pip install requests pymupdf
    # optional, only needed for --ocr (scanned pages with no text layer):
    pip install pytesseract pillow
    # and the Tesseract binary itself, e.g.:
    #   Windows: https://github.com/UB-Mannheim/tesseract/wiki
    #   macOS:   brew install tesseract
    #   Linux:   sudo apt install tesseract-ocr

Output:
    hits/                  -- cropped row screenshots (PNG)
    hits/hits.csv           -- log of every hit: source pdf/url, page, snippet, image path
    hits/report.html        -- browsable report: hit screenshots + a collapsible
                                dropdown listing every URL that returned 404 / failed
"""

import argparse
import csv
import io
import re
import sys
import time
from pathlib import Path

import requests
import fitz  # PyMuPDF

try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

BASE_URL = "https://www.eci.gov.in/sir/f4/S29/data/OLDSIRROLL/S29/210/S29_210_{n}.pdf"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def download_pdf(session, url, dest, retries=3, timeout=30):
    """Download a PDF, return True if saved, False if it doesn't exist (404) or all retries fail."""
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                dest.write_bytes(r.content)
                return True
            elif r.status_code == 404:
                return False
            else:
                print(f"  [warn] {url} -> HTTP {r.status_code} (attempt {attempt})")
        except requests.RequestException as e:
            print(f"  [warn] {url} -> {e} (attempt {attempt})")
        time.sleep(1.5 * attempt)
    return False


def normalize(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def find_hits_text_layer(page, target_tokens):
    """Locate the target name using the PDF's embedded text layer (fast path)."""
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, wordno)
    if not words:
        return []

    hits = []
    n = len(words)
    lower_words = [normalize(w[4]) for w in words]
    window = len(target_tokens) + 2  # slack for middle initials / punctuation

    for i in range(n):
        joined = " ".join(lower_words[i:i + window])
        if all(tok in joined for tok in target_tokens):
            span = words[i:i + window]
            x0 = min(w[0] for w in span)
            y0 = min(w[1] for w in span)
            x1 = max(w[2] for w in span)
            y1 = max(w[3] for w in span)
            hits.append((fitz.Rect(x0, y0, x1, y1), " ".join(w[4] for w in span)))
    return hits


def find_hits_ocr(page, target_tokens, zoom=3):
    """Fallback OCR pass for scanned (image-only) pages with no text layer."""
    if not OCR_AVAILABLE:
        return []
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    hits = []
    n = len(data["text"])
    lower_words = [normalize(w) for w in data["text"]]
    window = len(target_tokens) + 2

    for i in range(n):
        joined = " ".join(lower_words[i:i + window])
        if all(tok in joined for tok in target_tokens):
            xs, ys, xe, ye = [], [], [], []
            for j in range(i, min(i + window, n)):
                if not data["text"][j].strip():
                    continue
                x, y, w, h = (data["left"][j], data["top"][j],
                               data["width"][j], data["height"][j])
                xs.append(x); ys.append(y); xe.append(x + w); ye.append(y + h)
            if not xs:
                continue
            rect = fitz.Rect(min(xs) / zoom, min(ys) / zoom,
                              max(xe) / zoom, max(ye) / zoom)
            snippet = " ".join(t for t in data["text"][i:i + window] if t.strip())
            hits.append((rect, snippet))
    return hits


def crop_row(page, rect, pad_y=8, zoom=3, full_width=True):
    """Render a screenshot of the full row containing `rect`."""
    page_rect = page.rect
    x0 = page_rect.x0 if full_width else max(page_rect.x0, rect.x0 - 20)
    x1 = page_rect.x1 if full_width else min(page_rect.x1, rect.x1 + 20)
    y0 = max(page_rect.y0, rect.y0 - pad_y)
    y1 = min(page_rect.y1, rect.y1 + pad_y)
    clip = fitz.Rect(x0, y0, x1, y1)
    mat = fitz.Matrix(zoom, zoom)
    return page.get_pixmap(matrix=mat, clip=clip)


def generate_html_report(out_dir, log_rows, not_found, search_name, scanned_count):
    """Write a self-contained HTML report: hit table (with inline screenshots) plus
    a collapsible <details> dropdown listing every PDF that was not found / failed."""

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    hit_rows_html = []
    for row in log_rows:
        img_rel = Path(row["screenshot"]).name
        hit_rows_html.append(f"""
        <tr>
          <td>{esc(row['pdf_file'])}</td>
          <td>{esc(row['page'])}</td>
          <td>{esc(row['match_source'])}</td>
          <td>{esc(row['row_text'])}</td>
          <td><a href="{esc(row['pdf_url'])}" target="_blank">source pdf</a></td>
          <td><img src="{esc(img_rel)}" alt="row screenshot" style="max-width:600px;"></td>
        </tr>""")

    not_found_items = "\n".join(
        f'          <li><a href="{esc(u)}" target="_blank">{esc(u)}</a></li>' for u in not_found
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>ECI roll search: "{esc(search_name)}"</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; margin: 24px; color: #222; }}
  h1 {{ font-size: 20px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
  th, td {{ border: 1px solid #ddd; padding: 8px; font-size: 13px; vertical-align: top; }}
  th {{ background: #f5f5f5; text-align: left; }}
  details {{ margin-top: 16px; border: 1px solid #ddd; border-radius: 6px; padding: 8px 12px; }}
  summary {{ cursor: pointer; font-weight: 600; }}
  ul {{ columns: 2; max-height: 400px; overflow-y: auto; }}
  .summary-line {{ color: #555; margin-bottom: 4px; }}
</style>
</head>
<body>
  <h1>Search results for "{esc(search_name)}"</h1>
  <p class="summary-line">PDFs scanned: {scanned_count} &nbsp;|&nbsp;
     Hits: {len(log_rows)} &nbsp;|&nbsp;
     Files not found / failed: {len(not_found)}</p>

  <table>
    <thead>
      <tr><th>PDF file</th><th>Page</th><th>Match source</th><th>Row text</th><th>Link</th><th>Screenshot</th></tr>
    </thead>
    <tbody>
      {''.join(hit_rows_html) if hit_rows_html else '<tr><td colspan="6">No hits found.</td></tr>'}
    </tbody>
  </table>

  <details>
    <summary>Files not found ({len(not_found)})</summary>
    <ul>
{not_found_items if not_found_items else "      <li>None</li>"}
    </ul>
  </details>
</body>
</html>
"""
    report_path = out_dir / "report.html"
    report_path.write_text(html, encoding="utf-8")
    return report_path


def main():
    ap = argparse.ArgumentParser(description="Search ECI electoral roll PDFs for a name.")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, default=500)
    ap.add_argument("--name", default="krishna rao", help="Name to search for (case-insensitive).")
    ap.add_argument("--base-url", default=BASE_URL,
                     help="URL template with {n} placeholder for the file number.")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--out-dir", default="hits")
    ap.add_argument("--keep-pdfs", action="store_true",
                     help="Keep downloaded PDFs instead of deleting each after it's scanned.")
    ap.add_argument("--ocr", action="store_true",
                     help="Also OCR pages that have no embedded text layer (slower, needs Tesseract).")
    ap.add_argument("--delay", type=float, default=0.5,
                     help="Seconds to sleep between downloads (be polite to the server).")
    args = ap.parse_args()

    target_tokens = normalize(args.name).split()
    if not target_tokens:
        print("Provide a non-empty --name to search for.")
        sys.exit(1)

    if args.ocr and not OCR_AVAILABLE:
        print("--ocr requested but pytesseract/Pillow aren't installed. "
              "Run: pip install pytesseract pillow  (and install the Tesseract binary).")
        sys.exit(1)

    pdf_dir = Path(args.pdf_dir)
    out_dir = Path(args.out_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / "hits.csv"
    log_rows = []
    not_found = []
    scanned_count = 0
    session = requests.Session()

    for n in range(args.start, args.end + 1):
        url = args.base_url.format(n=n)
        pdf_path = pdf_dir / f"S29_210_{n}.pdf"

        print(f"[{n}] downloading {url}")
        if not download_pdf(session, url, pdf_path):
            print("  -> not found / failed (added to report dropdown)")
            not_found.append(url)
            continue
        scanned_count += 1

        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            print(f"  -> could not open PDF: {e}")
            continue

        for page_index in range(len(doc)):
            page = doc[page_index]
            hits = find_hits_text_layer(page, target_tokens)
            source = "text"
            if not hits and args.ocr:
                hits = find_hits_ocr(page, target_tokens)
                source = "ocr"

            for hit_no, (rect, snippet) in enumerate(hits, start=1):
                pix = crop_row(page, rect)
                img_name = f"S29_210_{n}_p{page_index + 1}_hit{hit_no}.png"
                img_path = out_dir / img_name
                pix.save(img_path)

                print(f"  HIT -> file={pdf_path.name} page={page_index + 1} "
                      f"({source}) snippet='{snippet}' -> {img_path}")

                log_rows.append({
                    "pdf_file": pdf_path.name,
                    "pdf_url": url,
                    "page": page_index + 1,
                    "match_source": source,
                    "row_text": snippet,
                    "screenshot": str(img_path),
                })

        doc.close()
        if not args.keep_pdfs:
            pdf_path.unlink(missing_ok=True)

        time.sleep(args.delay)

    if log_rows:
        with open(log_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(log_rows[0].keys()))
            writer.writeheader()
            writer.writerows(log_rows)

    report_path = generate_html_report(out_dir, log_rows, not_found, args.name, scanned_count)

    print(f"\nDone. Scanned {scanned_count} PDF(s), {len(log_rows)} hit(s), "
          f"{len(not_found)} not found/failed.")
    if log_rows:
        print(f"Hit log: {log_path}")
    print(f"Report (open in a browser): {report_path}")


if __name__ == "__main__":
    main()



# #======================================================================
# #Trial 3:
# #======================================================================

# import requests
# import re
# import os
# from html import escape
# from datetime import datetime

# def search_word_in_websites(base_url, start, end, search_term, output_file="results.html"):
    # """
    # Searches for a word/character in a range of websites and appends results in HTML format.
    # Adds timestamps for each batch of results.
    # """
    # results = []
    # search_pattern = re.compile(re.escape(search_term), re.IGNORECASE)

    # for i in range(start, end + 1):
        # url = base_url.format(i)
        # try:
            # response = requests.get(url, timeout=10)
            # response.raise_for_status()
            # content = response.text
            # count = len(search_pattern.findall(content))
            # results.append((url, count))
            # print(f"[OK] {url} → {count} occurrence(s)")
        # except requests.RequestException as e:
            # results.append((url, f"Error: {e}"))
            # print(f"[ERROR] {url} → {e}")

    # # Create HTML file if it doesn't exist
    # if not os.path.exists(output_file):
        # with open(output_file, "w", encoding="utf-8") as f:
            # f.write("<html><head><title>Search Results</title></head><body>")
            # f.write(f"<h1>Search Results for '{escape(search_term)}'</h1>")
            # f.write("<table border='1' cellpadding='5'><tr><th>Timestamp</th><th>Website</th><th>Occurrences</th></tr>")
            # f.write("</table></body></html>")

    # # Append results before closing table tag
    # with open(output_file, "r+", encoding="utf-8") as f:
        # content = f.read()
        # insert_pos = content.rfind("</table>")
        # if insert_pos == -1:
            # print("❌ Invalid HTML file format.")
            # return

        # timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # new_rows = ""
        # for url, count in results:
            # count_display = (
                # f"<span style='background-color:yellow'>{count}</span>"
                # if isinstance(count, int) and count > 0 else escape(str(count))
            # )
            # new_rows += (
                # f"<tr><td>{escape(timestamp)}</td>"
                # f"<td><a href='{escape(url)}'>{escape(url)}</a></td>"
                # f"<td>{count_display}</td></tr>"
            # )

        # updated_content = content[:insert_pos] + new_rows + content[insert_pos:]
        # f.seek(0)
        # f.write(updated_content)
        # f.truncate()

    # print(f"\n✅ Results appended to {output_file}")


# if __name__ == "__main__":
    # # Example usage
    # base_url = "https://example.com/page{}.html"  # '{}' will be replaced by numbers
    # start_num = 1
    # end_num = 3
    # search_term = "Python"

    # search_word_in_websites(base_url, start_num, end_num, search_term)

# Meaningful changes from previous version:

# Added timestamp column for each batch of appended results.
# Table header now includes Timestamp.
# Each row logs the exact time the search was performed.
# Maintains HTML validity while appending.

# If you want, I can further enhance this so that duplicate URLs are merged and counts updated instead of repeated in the HTML log. Would you like me to add that?



#======================================================================
#Trial 2:
#======================================================================

# import requests
# from bs4 import BeautifulSoup
# import re
# import sys
# import html

# def fetch_website_content(url):
    # """Fetch website HTML content with error handling."""
    # try:
        # response = requests.get(url, timeout=10)
        # response.raise_for_status()
        # return response.text
    # except requests.exceptions.RequestException as e:
        # print(f"Error fetching website: {e}")
        # sys.exit(1)

# def highlight_matches(text, word):
    # """Highlight all matches of the word in HTML-safe text."""
    # escaped_word = re.escape(word)
    # pattern = re.compile(escaped_word, re.IGNORECASE)
    # return pattern.sub(lambda m: f"<mark>{html.escape(m.group(0))}</mark>", html.escape(text))

# def main():
    # # Get user inputs
    # website = input("Enter website URL (include http/https): ").strip()
    # search_word = input("Enter the word or character to search: ").strip()

    # if not website or not search_word:
        # print("Both website and search word are required.")
        # sys.exit(1)

    # # Fetch and parse website
    # html_content = fetch_website_content(website)
    # soup = BeautifulSoup(html_content, "html.parser")
    # text_content = soup.get_text(separator=" ")

    # # Count occurrences
    # matches = re.findall(re.escape(search_word), text_content, re.IGNORECASE)
    # count = len(matches)

    # # Highlight matches in text
    # highlighted_text = highlight_matches(text_content, search_word)

    # # Generate HTML output
    # output_html = f"""
    # <html>
    # <head>
        # <title>Search Results for '{html.escape(search_word)}'</title>
        # <style>
            # body {{ font-family: Arial, sans-serif; margin: 20px; }}
            # mark {{ background-color: yellow; }}
        # </style>
    # </head>
    # <body>
        # <h1>Search Results</h1>
        # <p><strong>Website:</strong> {html.escape(website)}</p>
        # <p><strong>Search Term:</strong> {html.escape(search_word)}</p>
        # <p><strong>Total Occurrences:</strong> {count}</p>
        # <hr>
        # <h2>Highlighted Text:</h2>
        # <div style="white-space: pre-wrap;">{highlighted_text}</div>
    # </body>
    # </html>
    # """

    # # Save to file
    # output_file = "search_results.html"
    # with open(output_file, "w", encoding="utf-8") as f:
        # f.write(output_html)

    # print(f"Search complete! Results saved to '{output_file}'.")

# if __name__ == "__main__":
    # main()



#======================================================================
#Trial1
#======================================================================
# import os
# import html
# import fitz  # PyMuPDF

# # ===========================
# # Configuration
# # ===========================

# PDF_FOLDER = r"pdfs"
# OUTPUT_FOLDER = r"output"
# SCREENSHOT_FOLDER = os.path.join(OUTPUT_FOLDER, "screenshots")

# SEARCH_TEXT = "John Smith"
# CASE_SENSITIVE = False

# os.makedirs(OUTPUT_FOLDER, exist_ok=True)
# os.makedirs(SCREENSHOT_FOLDER, exist_ok=True)

# REPORT_FILE = os.path.join(OUTPUT_FOLDER, "report.html")

# # ===========================

# total_hits = 0

# with open(REPORT_FILE, "w", encoding="utf-8") as report:

    # report.write("""
# <!DOCTYPE html>
# <html>
# <head>
# <meta charset="utf-8">
# <title>PDF Search Report</title>

# <style>

# body{
    # font-family:Arial;
    # margin:30px;
    # background:#f4f4f4;
# }

# table{
    # border-collapse:collapse;
    # width:100%;
    # background:white;
# }

# th,td{
    # border:1px solid #ccc;
    # padding:8px;
    # vertical-align:top;
# }

# th{
    # background:#004a99;
    # color:white;
# }

# img{
    # max-width:500px;
    # border:1px solid black;
# }

# .highlight{
    # background:yellow;
    # font-weight:bold;
# }

# </style>

# </head>

# <body>

# <h2>PDF Search Report</h2>

# <p><b>Search String:</b> %s</p>

# <table>

# <tr>
# <th>File</th>
# <th>Page</th>
# <th>Line</th>
# <th>Matching Text</th>
# <th>Screenshot</th>
# </tr>

# """ % html.escape(SEARCH_TEXT))

    # for pdf_file in os.listdir(PDF_FOLDER):

        # if not pdf_file.lower().endswith(".pdf"):
            # continue

        # pdf_path = os.path.join(PDF_FOLDER, pdf_file)

        # print("Searching:", pdf_file)

        # doc = fitz.open(pdf_path)

        # for page_number in range(len(doc)):

            # page = doc.load_page(page_number)

            # page_text = page.get_text()

            # if CASE_SENSITIVE:
                # compare_page = page_text
                # search = SEARCH_TEXT
            # else:
                # compare_page = page_text.lower()
                # search = SEARCH_TEXT.lower()

            # if search not in compare_page:
                # continue

            # lines = page_text.splitlines()

            # for line_number, line in enumerate(lines, start=1):

                # compare_line = line if CASE_SENSITIVE else line.lower()

                # if search in compare_line:

                    # total_hits += 1

                    # rects = page.search_for(SEARCH_TEXT)

                    # image_name = ""

                    # if len(rects):

                        # rect = rects[0]

                        # margin = 40

                        # clip = fitz.Rect(
                            # rect.x0-margin,
                            # rect.y0-margin,
                            # rect.x1+margin,
                            # rect.y1+margin
                        # )

                        # pix = page.get_pixmap(
                            # clip=clip,
                            # dpi=300
                        # )

                        # image_name = (
                            # f"{os.path.splitext(pdf_file)[0]}"
                            # f"_page{page_number+1}"
                            # f"_line{line_number}.png"
                        # )

                        # image_path = os.path.join(
                            # SCREENSHOT_FOLDER,
                            # image_name
                        # )

                        # pix.save(image_path)

                    # display_line = html.escape(line)

                    # display_line = display_line.replace(
                        # SEARCH_TEXT,
                        # f'<span class="highlight">{html.escape(SEARCH_TEXT)}</span>'
                    # )

                    # report.write(f"""
# <tr>

# <td>{html.escape(pdf_file)}</td>

# <td>{page_number+1}</td>

# <td>{line_number}</td>

# <td>{display_line}</td>

# <td>
# <a href="screenshots/{image_name}" target="_blank">
# <img src="screenshots/{image_name}">
# </a>
# </td>

# </tr>
# """)

        # doc.close()

    # report.write(f"""
# </table>

# <br><br>

# <h3>Total Matches : {total_hits}</h3>

# </body>
# </html>
# """)

# print("Finished.")
# print("HTML Report:", REPORT_FILE)