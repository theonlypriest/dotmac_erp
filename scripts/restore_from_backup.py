#!/usr/bin/env python3
"""
Extract only COPY (data) blocks from a pg_dump plain-SQL file.

THIS IS NOT A RESTORE TOOL, despite the filename. It cannot rebuild a
database: it skips every CREATE, ALTER, DROP, GRANT and REVOKE, so it produces
no schema, no roles and no privileges. It reloads data into a database that
already exists and is already migrated.

For an actual restore -- cluster globals first, then a verified pg_restore of
the database, with the resulting roles asserted -- use
``scripts/restore_erp_db.sh``. The runbook is
``docs/runbooks/database-restore.md``.

The filename is retained because operators and scripts already reference it;
the correction is this docstring, not a rename that would break them.

Reads gzipped SQL dump, writes a clean SQL file containing:
  - SET statements (encoding, search_path, etc.)
  - All COPY ... FROM stdin blocks with their data
  - Sequence setval() calls

Skips: CREATE, ALTER, DROP, COMMENT, GRANT, REVOKE, and \restrict.
"""

from __future__ import annotations

import gzip
import sys


def extract_data(input_path: str, output_path: str) -> None:
    in_copy = False
    copy_count = 0
    line_count = 0

    with (
        gzip.open(input_path, "rt", encoding="utf-8") as fin,
        open(output_path, "w", encoding="utf-8") as fout,
    ):
        # Write preamble
        fout.write("SET client_encoding = 'UTF8';\n")
        fout.write("SET standard_conforming_strings = on;\n")
        fout.write("SET check_function_bodies = false;\n")
        fout.write("SET client_min_messages = warning;\n")
        fout.write("SET row_security = off;\n")
        fout.write(
            "SET session_replication_role = 'replica';\n\n"
        )  # Disable FK triggers

        for line in fin:
            line_count += 1

            if in_copy:
                fout.write(line)
                if line.strip() == "\\.":
                    in_copy = False
                    fout.write("\n")
                continue

            if line.startswith("COPY "):
                in_copy = True
                copy_count += 1
                fout.write(line)
                continue

            # Also capture SELECT pg_catalog.setval() for sequences
            if line.startswith("SELECT pg_catalog.setval("):
                fout.write(line)
                continue

        # Re-enable triggers
        fout.write("\nSET session_replication_role = 'origin';\n")

    print(f"Processed {line_count:,} lines, extracted {copy_count} COPY blocks")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.sql.gz> <output.sql>")
        sys.exit(1)
    extract_data(sys.argv[1], sys.argv[2])
