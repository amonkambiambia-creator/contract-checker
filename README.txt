XTENDA CONTRACT CHECKER
=======================

What it does
------------
Point it at a folder of downloaded PBL loan contracts. For each one it:

  * finds the three borrower signature blocks by anchor text and geometry,
    never by page number - contracts run 12, 13 or 14 pages;
  * measures ink inside the borrower's own band only, so a contract signed
    only by the loan officer or the witness cannot pass, and the borrower's
    printed name below the rule is not mistaken for a signature;
  * checks the boxed "Initial" areas - typed tokens (mc, KP/NC) and
    hand-drawn strokes both count;
  * reads the customer name and the portal loan number out of the contract
    itself, and cross-checks them against the filename;
  * renames to "<First Last> LN<last 6>.pdf" and files into
    Passed \ Review Required, with byte-identical duplicates going to
    Review Required\_Duplicates;
  * writes Contract-Review-Log.csv and saves a PNG of every signature block
    into "_Review Evidence" so you can eyeball any call it made.

All three signature flavours are handled: vector or image stamps,
Fill-and-Sign stroke fragments, and printed-signed-rescanned contracts (OCR).
A scanned contract that is signed PASSES. Being a scan is never a flag.

Everything runs on your laptop. No contract leaves the machine.


Setup - once
------------
    powershell -ExecutionPolicy Bypass -File ".\Install-ContractChecker.ps1"

That installs Python, the packages, and Tesseract OCR, and puts a
"Contract Checker" shortcut on your Desktop.

Tesseract matters: without it, scanned contracts cannot be checked and are
sent to Review Required untouched.


Using it
--------
Double-click the Desktop shortcut (or drag a folder of contracts onto it).

  1. Pick the contracts folder.
  2. Leave "Rename and move files" UNTICKED and press Review contracts.
     Nothing is changed - you get the log and the crops to look at.
  3. Happy? Tick the box and run it again to commit.

From a command line:

    py xtenda_contract_check.py --folder "C:\...\Loan Contracts"
    py xtenda_contract_check.py --folder "C:\...\Loan Contracts" --apply


Safety
------
  * Dry run by default. Nothing moves until you tick the box or pass --apply.
  * Never deletes anything.
  * Never overwrites. A name clash with different content is saved as
    "... (v2).pdf"; an identical copy already filed is skipped and the source
    is left where it is.
  * Safe to re-run on the same folder.
  * The old CSV log is backed up with a timestamp before a new one is written.


Reading the log
---------------
"Signature Blocks" says how many of the three borrower blocks are signed.
Anything short of "3 of 3" goes to Review Required with a reason.

"Note" carries the ink score for each block. For reference, on this batch:
unsigned fields score 0, and the faintest genuine signature seen scored 97.
The thresholds are at the top of xtenda_contract_check.py (INK_MIN_VECTOR and
INK_MIN_SCAN) if you ever need to tune them - change one, then re-run against
a folder holding one contract you know is signed and one you know is not, and
check the unsigned one still scores 0.


Known limits
------------
  * A scanned contract with no Tesseract installed is flagged, not judged.
  * If OCR cannot find all three anchors on a scan, the contract goes to
    Review Required saying so, rather than guessing.
  * Initial boxes are checked where the label is found; on heavy scans OCR
    misses some labels, so "Initials Found" can undercount. It never
    invents one.
