#!/bin/sh
# With no arguments: read whatever statements are in the PDF folder, then serve
# the dashboard. With arguments: run that finances command instead, so
#   docker compose run --rm finances check
# works the same as `finances check` does outside a container.
set -e

if [ "$#" -gt 0 ]; then
    exec finances "$@"
fi

if [ -n "$(find "$FINANCES_PDF_DIR" -iname '*.pdf' 2>/dev/null | head -n 1)" ]; then
    # Unchanged files are skipped by the parse cache, so restarts are quick.
    finances parse "$FINANCES_PDF_DIR" -o /data/cleanedData/transactions.csv \
        && finances categorize apply \
        && finances check \
        || echo "finances: reading the statements failed; serving what is already stored." >&2
fi

if [ -n "$(find "$FINANCES_PDF_DIR" -type f \( -iname '*.csv' -o -iname '*.ofx' \
        -o -iname '*.qfx' -o -iname '*.qbo' -o -iname '*.xml' -o -iname '*.sta' \
        -o -iname '*.mt940' -o -iname '*.txt' \) 2>/dev/null | head -n 1)" ]; then
    # Bank exports sit beside the PDFs; unchanged files are skipped here too.
    finances import "$FINANCES_PDF_DIR" \
        || echo "finances: some exports could not be read; see above." >&2
fi

if [ -z "$(find "$FINANCES_PDF_DIR" -type f 2>/dev/null | head -n 1)" ]; then
    echo "finances: nothing in ./PDFs yet. Drop statements or bank exports there and restart, or onto the dashboard."
fi

exec finances web
