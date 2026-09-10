#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xtenda PBL Loan Contract Checker
================================

Point it at a folder of downloaded PBL loan contracts. For each one it:

  1. locates the three borrower signature blocks by anchor text and geometry
     (never by page number - contracts run 12, 13 or 14 pages);
  2. measures ink inside the borrower's own band only, so a contract signed
     solely by the loan officer or witness cannot pass, and the borrower's
     printed name below the rule is not counted as a signature;
  3. checks the boxed "Initial" areas (typed tokens and hand-drawn both count);
  4. reads the customer name and portal loan number out of the contract itself;
  5. renames to "<First Last> LN<last 6>.pdf" and files into
     Passed / Review Required (byte-identical duplicates to
     Review Required\\_Duplicates);
  6. writes Contract-Review-Log.csv and saves a PNG crop of every signature
     block into "_Review Evidence" so a human can eyeball any call.

All three signature flavours are handled: vector or image stamp,
Fill-and-Sign stroke fragments, and printed-signed-rescanned (OCR).
A scanned contract that is signed PASSES - being a scan is never a flag.

Run the GUI:      pythonw xtenda_contract_check.py
Run headless:     python xtenda_contract_check.py --folder "C:\\path" [--apply]
"""

import os, re, sys, csv, json, shutil, hashlib, argparse, traceback
from datetime import datetime

APP_NAME    = "Xtenda Contract Checker"
APP_VERSION = "1.0"

# --------------------------------------------------------------------------
# Tuning. These numbers were calibrated against a real batch: unsigned fields
# score exactly 0 on vector contracts, and real signatures score 246+ on scans.
# --------------------------------------------------------------------------
DPI              = 150
S                = DPI / 72.0          # points -> pixels
GREY_VECTOR      = 180                 # ink darker than this counts (vector pages)
GREY_SCAN        = 160                 # ...and on scanned pages
INK_MIN_VECTOR   = 25                  # unsigned vector control measures 0
INK_MIN_SCAN     = 150                 # unsigned scan control measures 0
RULE_FRAC        = 0.35                # horizontal run this wide = printed rule
VRULE_FRAC       = 0.50                # vertical run this tall  = box border
MANDATE_DATE_DX  = 369                 # px from "Signatures" to "Date" at 150 dpi

PASSED_DIR    = "Passed"
REVIEW_DIR    = "Review Required"
DUP_DIR       = os.path.join("Review Required", "_Duplicates")
EVIDENCE_DIR  = "_Review Evidence"
LOG_NAME      = "Contract-Review-Log.csv"

CSV_COLUMNS = ["Loan ID", "Customer Name", "File Name", "New File Name",
               "Download DateTime", "Pages", "Scanned", "Signature Found",
               "Signature Blocks", "Initials Found", "Result", "Destination",
               "Action", "Reason", "Note"]

# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------
MISSING = []
try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        pymupdf = None
        MISSING.append("pymupdf")
try:
    import numpy as np
except ImportError:
    np = None
    MISSING.append("numpy")
try:
    from PIL import Image
except ImportError:
    Image = None
    MISSING.append("pillow")
try:
    import pytesseract
except ImportError:
    pytesseract = None
    MISSING.append("pytesseract")


def tesseract_ok():
    """True if the Tesseract binary is actually reachable."""
    if pytesseract is None:
        return False
    for cand in (None,
                 r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")):
        if cand:
            if not os.path.exists(cand):
                continue
            pytesseract.pytesseract.tesseract_cmd = cand
        try:
            pytesseract.get_tesseract_version()
            return True
        except Exception:
            continue
    return False


# ==========================================================================
# Ink measurement
# ==========================================================================
def strip_rules(mask, frac=RULE_FRAC):
    """Zero horizontal dark runs wide enough to be a printed rule or a row of
    underscores. Signature strokes are never that long and straight."""
    m = mask.copy()
    limit = max(40, int(m.shape[1] * frac))
    for y in range(m.shape[0]):
        row = m[y]
        if not row.any():
            continue
        idx = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
        for a, b in zip(idx[0::2], idx[1::2]):
            if b - a > limit:
                row[a:b] = False
    return m


def strip_vrules(mask, frac=VRULE_FRAC):
    """Same for vertical box borders."""
    m = mask.copy()
    limit = max(30, int(m.shape[0] * frac))
    for x in range(m.shape[1]):
        col = m[:, x]
        if not col.any():
            continue
        idx = np.flatnonzero(np.diff(np.concatenate(([0], col.view(np.int8), [0]))))
        for a, b in zip(idx[0::2], idx[1::2]):
            if b - a > limit:
                col[a:b] = False
    return m


def text_free_page(doc, pno):
    """A one-page copy with TEXT removed but line art and images kept.

    This is what lets the vector path measure a signature that crosses the
    printed rule: the rule and the "by client NAME" caption are text objects and
    disappear, while the signature - which is line art or an image - stays.
    Nothing here guesses at what is printed, so nothing legitimate is erased.
    """
    d2 = pymupdf.open()
    d2.insert_pdf(doc, from_page=pno, to_page=pno)
    p = d2[0]
    words = p.get_text("words")
    if words:
        for w in words:
            r = pymupdf.Rect(w[:4]); r.y0 -= 0.5; r.y1 += 0.5
            p.add_redact_annot(r)
        p.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                           graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                           text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    return d2


def band_gray(page, rect):
    pm = page.get_pixmap(dpi=DPI, clip=rect, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width)


def measure(arr, thresh):
    m = arr < thresh
    m = strip_rules(m)
    m = strip_vrules(m)
    return int(m.sum())


def page_img(doc, pno):
    pm = doc[pno].get_pixmap(dpi=DPI)
    return Image.frombytes("RGB", (pm.width, pm.height), pm.samples).convert("L")


def ocr_words(img):
    d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    return [{"t": d["text"][i].strip(), "x": d["left"][i], "y": d["top"][i],
             "w": d["width"][i], "h": d["height"][i]}
            for i in range(len(d["text"])) if d["text"][i].strip()]


# ==========================================================================
# Block geometry
# ==========================================================================
def sb_band_px(lx, ly, lw, lh, client_y=None):
    """Borrower band beside a scanned 'Signed By:' label, in pixels."""
    x0, x1 = lx + lw + 60, lx + int(235 * S)
    y0, y1 = ly - int(14 * S), ly + lh + int(2 * S)
    if client_y is not None:
        y1 = min(y1, client_y - 3)
    return (x0, y0, x1, y1)


def md_band_px(lx, ly):
    """Mandate band, which sits ABOVE the 'Signatures' label."""
    return (lx - int(14 * S), ly - int(26 * S), lx + int(140 * S), ly - 2)


def lone_initial(words, w, span):
    """True if this 'Initial' label is alone on its text line - i.e. it is the
    boxed form label, not the word 'initial' inside a paragraph."""
    for o in words:
        if o is w:
            continue
        oy0, oy1, ox0 = o[1], o[3], o[0]
        if oy0 < w[3] and oy1 > w[1] and abs(ox0 - w[0]) < span:
            # a short neighbour is the initial itself; a long word is prose
            if len(o[4].strip()) > 3:
                return False
    return True


def digital_page_blocks(doc, pno):
    """Signature and initial blocks on a page that carries real text."""
    ws = doc[pno].get_text("words")
    sig, ini = [], []

    # borrower "Signed By:" - identified by the "by client" caption beneath it
    for b in [w for w in ws if w[4].lower() == "by"]:
        if not any(abs(b[1] - c[1]) < 2 and 0 < c[0] - b[2] < 12
                   for c in ws if c[4].lower() == "client"):
            continue
        cand = [w for w in ws if w[4] == "Signed"
                and 0 < b[1] - w[1] < 30 and abs(w[0] - b[0]) < 15]
        if not cand:
            continue
        lab = min(cand, key=lambda w: b[1] - w[1])
        sig.append(("Signed By (borrower)",
                    pymupdf.Rect(lab[2] + 3, lab[1] - 14,
                                 lab[0] + 235, min(lab[3] + 2, b[1] - 1))))

    # direct-debit mandate
    for w in ws:
        if w[4].strip().rstrip(":").lower() == "signatures":
            sig.append(("Mandate Signatures",
                        pymupdf.Rect(w[0] - 14, w[1] - 26, w[0] + 140, w[1] - 1)))

    for w in ws:
        if w[4].strip().rstrip(":") == "Initial" and lone_initial(ws, w, 260):
            # Typed tokens (mc, KP/NC) count as initials. They may sit below the
            # label or, when set in a larger face, share its text line - so look
            # in both places, and ignore the label itself.
            typed = [x[4] for x in ws
                     if x is not w
                     and w[1] - 8 < x[1] < w[3] + 40
                     and abs(x[0] - w[0]) < 230
                     and 0 < len(x[4].strip()) <= 6]
            ini.append((pymupdf.Rect(w[0] - 70, w[3] + 1, w[0] + 200, w[3] + 34),
                        " ".join(typed)))

    out = []
    if sig or ini:
        d2 = text_free_page(doc, pno)
        for kind, r in sig:
            out.append({"kind": kind, "page": pno + 1, "mode": "vector",
                        "rect": [round(v, 1) for v in r],
                        "ink": measure(band_gray(d2[0], r), GREY_VECTOR)})
        for r, typed in ini:
            out.append({"kind": "Initial", "page": pno + 1, "mode": "vector",
                        "rect": [round(v, 1) for v in r], "typed": typed,
                        "ink": measure(band_gray(d2[0], r), GREY_VECTOR)})
        d2.close()
    return out


def scanned_page_blocks(doc, pno):
    """Same, for a page with no extractable text. Anchors come from OCR, but
    nothing OCR reads is ever masked - tesseract reads handwriting too, and
    masking that would erase the very thing being measured."""
    img = page_img(doc, pno)
    ws = ocr_words(img)
    out, bands = [], []

    clients = [w for w in ws if w["t"].lower().strip(".,:") == "client"]
    for s in [w for w in ws if w["t"].lower().startswith("signed")]:
        below = [c for c in clients
                 if 0 < c["y"] - s["y"] < 60 and abs(c["x"] - s["x"]) < 70]
        if below:
            bands.append(("Signed By (borrower)",
                          sb_band_px(s["x"], s["y"], s["w"], s["h"], below[0]["y"])))

    for w in ws:
        if w["t"].lower().strip(".,:") == "signatures":
            bands.append(("Mandate Signatures", md_band_px(w["x"], w["y"])))

    ptxt = " ".join(w["t"] for w in ws).lower()
    on_mandate = ("direct debit" in ptxt or "instruction to debit" in ptxt
                  or "ddacc" in ptxt)
    if on_mandate and not any(k == "Mandate Signatures" for k, _ in bands):
        # A heavy signature often lands on top of the printed word "Signatures"
        # and OCR loses it, silently dropping the mandate block. "Date" sits on
        # the same row a fixed distance to the right - rebuild the anchor there.
        cands = [w for w in ws if w["t"].lower().strip(".,:") == "date"
                 and w["y"] > img.size[1] * 0.6]
        if cands:
            w = max(cands, key=lambda w: w["y"])
            bands.append(("Mandate Signatures*",
                          md_band_px(w["x"] - MANDATE_DATE_DX, w["y"])))

    for kind, (x0, y0, x1, y1) in bands:
        arr = np.array(img.crop((max(0, x0), max(0, y0), x1, y1)))
        out.append({"kind": kind, "page": pno + 1, "mode": "scan",
                    "rect": [x0, y0, x1, y1], "ink": measure(arr, GREY_SCAN)})

    for w in ws:
        if w["t"].strip().rstrip(":") != "Initial":
            continue
        if any(o is not w and o["y"] < w["y"] + w["h"] and o["y"] + o["h"] > w["y"]
               and abs(o["x"] - w["x"]) < 540 and len(o["t"].strip()) > 3 for o in ws):
            continue
        x0, y0 = w["x"] - int(70 * S), w["y"] + w["h"] + 2
        x1, y1 = w["x"] + int(200 * S), w["y"] + w["h"] + int(34 * S)
        arr = np.array(img.crop((max(0, x0), max(0, y0), x1, y1)))
        out.append({"kind": "Initial", "page": pno + 1, "mode": "scan",
                    "rect": [x0, y0, x1, y1], "ink": measure(arr, GREY_SCAN)})
    return out


# ==========================================================================
# Identity
# ==========================================================================
def title_case(s):
    return " ".join(p.capitalize() for p in s.split())


def parse_identity(text):
    d = {}
    m = re.search(r"Surname\s*[\n:]+\s*([A-Za-z'\- ]+)", text)
    if m:
        d["surname"] = m.group(1).strip().split("\n")[0].strip()
    m = re.search(r"First\s*Name\s*[\n:]+\s*([A-Za-z'\- ]+)", text)
    if m:
        d["first"] = m.group(1).strip().split("\n")[0].strip()
    # portal ID is LN plus EXACTLY 17 digits - anchoring on that length stops
    # OCR noise from a neighbouring number gluing on a stray digit
    m = re.search(r"LN\s?(\d{17})(?!\d)", text.replace(" ", ""))
    if m:
        d["ln"] = m.group(1)
    m = re.search(r"(\d{2}-[A-Za-z]{3}-\d{4})\s+(\d{2}:\d{2}:\d{2})", text)
    if m:
        d["stamp"] = m.group(1) + " " + m.group(2)
    return d


def identity_scanned(doc, max_pages=2):
    text = ""
    for pno in range(min(max_pages, doc.page_count)):
        ws = ocr_words(page_img(doc, pno))
        text += " ".join(w["t"] for w in ws) + "\n"
    d = parse_identity(text)
    if "surname" not in d:
        m = re.search(r"Surname\s+([A-Z][A-Za-z'\-]{1,})", text)
        if m:
            d["surname"] = m.group(1)
    if "first" not in d:
        m = re.search(r"First\s*Name\s+([A-Z][A-Za-z'\-]{1,})", text)
        if m:
            d["first"] = m.group(1)
    return d


def filename_loan_digits(name):
    """Loan digits visible in the FILE NAME - used only to cross-check, never
    to name the file. Only a run after 'LN' or a leading 6-digit token counts,
    so a download timestamp does not read as a mismatch."""
    stem = os.path.splitext(os.path.basename(name))[0]
    hits = []
    for m in re.finditer(r"LN\s?(\d{6,17})", stem, re.I):
        hits.append(m.group(1))
    m = re.match(r"^(\d{6})(?!\d)", stem)
    if m:
        hits.append(m.group(1))
    return hits


# ==========================================================================
# Review one contract
# ==========================================================================
def review(path, evidence_root=None):
    doc = pymupdf.open(path)
    blocks, scan_pages = [], 0
    for pno in range(doc.page_count):
        if len(doc[pno].get_text().strip()) > 50:
            blocks += digital_page_blocks(doc, pno)
        else:
            scan_pages += 1
            if not tesseract_ok():
                continue
            blocks += scanned_page_blocks(doc, pno)

    scanned = scan_pages > doc.page_count / 2
    if scanned and tesseract_ok():
        ident = identity_scanned(doc)
    else:
        txt = "\n".join(doc[i].get_text() for i in range(min(4, doc.page_count)))
        txt += "\n" + doc[doc.page_count - 1].get_text()
        ident = parse_identity(txt)

    def signed(b):
        floor = INK_MIN_SCAN if b["mode"] == "scan" else INK_MIN_VECTOR
        if b["kind"] == "Initial" and b.get("typed", "").strip():
            return True          # typed tokens like "KP/NC" count as initials
        return b["ink"] >= floor

    sig_blocks = [b for b in blocks if b["kind"] != "Initial"]
    sig_signed = [b for b in sig_blocks if signed(b)]
    inits      = [b for b in blocks if b["kind"] == "Initial"]
    inits_bad  = [b for b in inits if not signed(b)]

    if evidence_root:
        save_evidence(doc, path, sig_blocks, evidence_root)
    page_count = doc.page_count
    doc.close()

    first  = title_case(ident.get("first", ""))
    last   = title_case(ident.get("surname", ""))
    ln     = ident.get("ln", "")
    name   = (first + " " + last).strip()
    newname = "{} LN{}.pdf".format(name, ln[-6:]) if name and ln else ""

    notes, reasons = [], []
    flavour = "Printed, hand-signed, rescanned" if scanned else "Digital (vector / stamp)"
    if sig_blocks:
        detail = ", ".join("{} p{} ink {}".format(b["kind"], b["page"], b["ink"])
                           for b in sig_blocks)
        notes.append("{}. {}.".format(flavour, detail))
    else:
        notes.append(flavour + ". No signature anchors located.")
    if any(b["kind"].endswith("*") for b in sig_blocks):
        notes.append("Mandate anchor rebuilt from the Date label.")

    fn_digits = filename_loan_digits(path)
    if ln and fn_digits and not any(ln.endswith(d) or d.endswith(ln[-6:]) for d in fn_digits):
        reasons.append("Filename loan number {} does not match contract {}"
                       .format("/".join(fn_digits), ln))
    elif ln and fn_digits:
        notes.append("Filename loan number matches the contract.")

    if len(sig_signed) < 3:
        missing = 3 - len(sig_signed)
        reasons.append("{} of 3 borrower signature blocks {}".format(
            len(sig_signed),
            "found" if len(sig_blocks) < 3 else "signed"))
        if len(sig_blocks) < 3:
            reasons.append("Could not locate all three anchors - check by hand")
    if inits_bad:
        reasons.append("Initial box empty on page " +
                       ", ".join(str(b["page"]) for b in sorted(inits_bad, key=lambda b: b["page"])))
    if not name or not ln:
        reasons.append("Could not read the customer name or loan number")
    if scanned and not tesseract_ok():
        reasons.append("Scanned contract and Tesseract is not installed - not checked")

    ok = not reasons
    return {
        "path": path,
        "Loan ID": ("LN" + ln) if ln else "",
        "Customer Name": name,
        "File Name": os.path.basename(path),
        "New File Name": newname,
        "Download DateTime": ident.get("stamp", ""),
        "Pages": page_count,
        "Scanned": "Yes" if scanned else "No",
        "Signature Found": "Yes" if len(sig_signed) >= 3 else "No",
        "Signature Blocks": "{} of 3".format(len(sig_signed)),
        "Initials Found": ("Yes ({} pages)".format(len(inits) - len(inits_bad))
                           if inits else "None found"),
        "Result": "Pass" if ok else "Review Required",
        "Reason": "; ".join(reasons) if reasons else "All three borrower blocks signed",
        "Note": " ".join(notes),
        "_blocks": blocks,
    }


def save_evidence(doc, path, blocks, root):
    """Write a PNG of every signature block so a person can eyeball any call.
    Never report a pass you have not seen - this is what makes that possible."""
    stem = os.path.splitext(os.path.basename(path))[0][:60]
    out = os.path.join(root, stem)
    os.makedirs(out, exist_ok=True)
    for b in blocks:
        pno = b["page"] - 1
        try:
            if b["mode"] == "vector":
                pm = doc[pno].get_pixmap(dpi=DPI, clip=pymupdf.Rect(b["rect"]))
                im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
            else:
                x0, y0, x1, y1 = b["rect"]
                im = page_img(doc, pno).crop((max(0, x0), max(0, y0), x1, y1))
            im = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
            safe = b["kind"].replace("*", "-star").replace(" ", "_").replace("(", "").replace(")", "")
            im.save(os.path.join(out, "p{}_{}_{}.png".format(b["page"], safe, b["ink"])))
        except Exception:
            pass


# ==========================================================================
# Folder run: review, rename, move, log
# ==========================================================================
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def free_target(dest_dir, name, src):
    """A destination path that never overwrites. Returns None when an identical
    copy is already filed there (then we leave the source alone)."""
    target = os.path.join(dest_dir, name)
    if not os.path.exists(target):
        return target
    if sha256(target) == sha256(src):
        return None
    base, ext = os.path.splitext(name)
    for v in range(2, 100):
        cand = os.path.join(dest_dir, "{} (v{}){}".format(base, v, ext))
        if not os.path.exists(cand):
            return cand
        if sha256(cand) == sha256(src):
            return None
    raise RuntimeError("no free name for " + name)


def run_folder(folder, apply=False, log=print):
    pdfs = sorted(p for p in os.listdir(folder)
                  if p.lower().endswith(".pdf") and
                  os.path.isfile(os.path.join(folder, p)))
    if not pdfs:
        log("No PDFs in that folder.")
        return []

    evidence = os.path.join(folder, EVIDENCE_DIR)
    os.makedirs(evidence, exist_ok=True)

    rows, seen = [], {}
    for i, name in enumerate(pdfs, 1):
        path = os.path.join(folder, name)
        log("[{}/{}] {}".format(i, len(pdfs), name))
        try:
            r = review(path, evidence)
        except Exception as e:
            log("      ! could not read: {}".format(e))
            bad = dict((c, "") for c in CSV_COLUMNS)
            bad.update({"File Name": name, "New File Name": name,
                        "Result": "Review Required", "Destination": REVIEW_DIR,
                        "Action": "Move only",
                        "Reason": "Could not be opened: {}".format(e)})
            rows.append(bad)
            continue

        digest = sha256(path)
        if digest in seen:
            r["Result"] = "Duplicate"
            r["Reason"] = "Byte-identical to {}".format(seen[digest])
            r["Note"] = "Same contract downloaded twice. Kept copy takes the name."
            r["Destination"] = DUP_DIR
            r["Action"] = "Move only"
            r["New File Name"] = name
        else:
            seen[digest] = name
            r["Destination"] = PASSED_DIR if r["Result"] == "Pass" else REVIEW_DIR
            r["Action"] = "Rename and move"
            if not r["New File Name"]:
                r["New File Name"] = name
                r["Action"] = "Move only"

        log("      {} -> {}\\{}".format(r["Result"], r["Destination"], r["New File Name"]))
        if r["Result"] != "Pass":
            log("      {}".format(r["Reason"]))
        rows.append(r)

    # move
    moved = skipped = 0
    for r in rows:
        src = os.path.join(folder, r["File Name"])
        if not os.path.exists(src):
            continue
        dest_dir = os.path.join(folder, r["Destination"])
        target = (free_target(dest_dir, r["New File Name"], src)
                  if os.path.isdir(dest_dir)
                  else os.path.join(dest_dir, r["New File Name"]))
        if target is None:
            r["Action"] = "Skipped - identical copy already filed"
            skipped += 1
            continue
        if os.path.basename(target) != r["New File Name"]:
            r["New File Name"] = os.path.basename(target)
            r["Note"] = (r.get("Note", "") + " Name clash with different content - saved as "
                         + r["New File Name"]).strip()
        if apply:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(src, target)          # never overwrites, never deletes
        moved += 1

    log("")
    log("{}: {} to file, {} skipped as already filed.".format(
        "Applied" if apply else "Dry run", moved, skipped))
    if not apply:
        log("Nothing was changed. Tick 'Rename and move files' to commit.")

    write_log(folder, rows, log)
    return rows


def write_log(folder, rows, log=print):
    path = os.path.join(folder, LOG_NAME)
    if os.path.exists(path):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.replace(".csv", "-{}.csv".format(stamp))
        shutil.copy2(path, backup)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in CSV_COLUMNS})
    log("Log written: {}".format(path))
    log("Signature crops: {}".format(os.path.join(folder, EVIDENCE_DIR)))


# ==========================================================================
# GUI
# ==========================================================================
def gui():
    import tkinter as tk
    from tkinter import filedialog, ttk, messagebox
    import threading, queue

    root = tk.Tk()
    root.title("{} {}".format(APP_NAME, APP_VERSION))
    root.geometry("860x600")
    root.minsize(700, 480)

    folder_var = tk.StringVar()
    apply_var  = tk.BooleanVar(value=False)
    msgs = queue.Queue()

    top = ttk.Frame(root, padding=12); top.pack(fill="x")
    ttk.Label(top, text="Contracts folder", font=("Segoe UI", 10, "bold")).pack(anchor="w")
    row = ttk.Frame(top); row.pack(fill="x", pady=(4, 0))
    entry = ttk.Entry(row, textvariable=folder_var); entry.pack(side="left", fill="x", expand=True)

    def pick():
        d = filedialog.askdirectory(title="Pick the folder with the downloaded contracts")
        if d:
            folder_var.set(os.path.normpath(d))
    ttk.Button(row, text="Browse...", command=pick).pack(side="left", padx=(8, 0))

    opts = ttk.Frame(root, padding=(12, 0)); opts.pack(fill="x")
    ttk.Checkbutton(opts, text="Rename and move files (leave unticked for a dry run)",
                    variable=apply_var).pack(anchor="w")

    btns = ttk.Frame(root, padding=12); btns.pack(fill="x")
    run_btn = ttk.Button(btns, text="Review contracts")
    run_btn.pack(side="left")
    ttk.Button(btns, text="Open folder",
               command=lambda: folder_var.get() and os.startfile(folder_var.get())
               ).pack(side="left", padx=8)

    bar = ttk.Progressbar(root, mode="indeterminate")
    bar.pack(fill="x", padx=12)

    out = tk.Text(root, wrap="word", height=20, font=("Consolas", 9))
    out.pack(fill="both", expand=True, padx=12, pady=12)
    out.configure(state="disabled")

    def emit(s):
        msgs.put(str(s))

    def pump():
        try:
            while True:
                s = msgs.get_nowait()
                out.configure(state="normal")
                out.insert("end", s + "\n")
                out.see("end")
                out.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(120, pump)

    def work():
        folder = folder_var.get().strip()
        try:
            run_folder(folder, apply=apply_var.get(), log=emit)
        except Exception:
            emit("ERROR:\n" + traceback.format_exc())
        finally:
            msgs.put("")
            root.after(0, lambda: (bar.stop(), run_btn.configure(state="normal")))

    def start():
        folder = folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning(APP_NAME, "Pick the folder with the contracts first.")
            return
        if MISSING:
            messagebox.showerror(APP_NAME,
                "Missing Python packages: {}\n\nRun Install-ContractChecker.ps1 once.".format(
                    ", ".join(MISSING)))
            return
        if not tesseract_ok():
            if not messagebox.askyesno(APP_NAME,
                "Tesseract OCR was not found.\n\nScanned contracts cannot be checked "
                "without it and will be sent to Review Required.\n\nCarry on anyway?"):
                return
        out.configure(state="normal"); out.delete("1.0", "end"); out.configure(state="disabled")
        run_btn.configure(state="disabled")
        bar.start(12)
        threading.Thread(target=work, daemon=True).start()

    run_btn.configure(command=start)

    if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
        folder_var.set(os.path.normpath(sys.argv[1]))

    emit("{} {}".format(APP_NAME, APP_VERSION))
    emit("Pick the folder of downloaded contracts, then Review.")
    emit("Dry run first - tick the box to actually rename and move.")
    emit("")
    if MISSING:
        emit("!! Missing packages: {} - run Install-ContractChecker.ps1".format(", ".join(MISSING)))
    elif not tesseract_ok():
        emit("!! Tesseract OCR not found - scanned contracts cannot be checked.")
    else:
        emit("Ready. Vector and scanned contracts both supported.")

    pump()
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("folder_arg", nargs="?", help="folder to prefill in the GUI")
    ap.add_argument("--folder", help="run headless against this folder")
    ap.add_argument("--apply", action="store_true", help="actually rename and move")
    a = ap.parse_args()
    if a.folder:
        if MISSING:
            print("Missing packages:", ", ".join(MISSING)); sys.exit(2)
        run_folder(a.folder, apply=a.apply)
    else:
        gui()


if __name__ == "__main__":
    main()
