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
else
    echo "finances: no PDFs in ./PDFs yet. Drop them there and restart, or onto the dashboard."
fi

exec finances web
