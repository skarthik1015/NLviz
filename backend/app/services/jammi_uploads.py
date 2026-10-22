"""Uploaded files served through Jammi: convert to Parquet, store where jammi-server can read it,
and register one source per file.

Users never hand Jammi a URL. Jammi opens file and cloud sources with the server's own identity
(issue J25), so NLviz writes every upload to storage it manages, under a path it generates
(`<prefix>/w<workspace tag>/<connection>/<table>.parquet`), and registers only that path.

One file per source: a directory source silently unions files with different schemas (J9), and a
single-file source exposes its table under the file's stem, `<source>.public.<stem>`.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from app.services.jammi_sources import workspace_tag

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
UPLOAD_SUFFIXES = (".csv", ".xlsx", ".parquet")
UNSUPPORTED_FORMAT = "Only CSV, Excel (.xlsx) and Parquet files are supported"
# An .xlsx is a zip: refuse one that expands past this before parsing it (a zip bomb).
_XLSX_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
_MAX_IDENTIFIER = 63


class UploadError(ValueError):
    """The file cannot be served (unreadable, empty, no columns). Safe to show to the user."""


def normalize_identifier(raw: str, *, fallback: str, digit_prefix: str) -> str:
    """Lowercase SQL identifier that Jammi resolves unquoted (mixed case would need quoting)."""
    safe = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower())
    safe = re.sub(r"_+", "_", safe).strip("_") or fallback
    if safe[0].isdigit():
        safe = f"{digit_prefix}{safe}"
    return safe[:_MAX_IDENTIFIER]


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def snake_identifier(raw: str, *, fallback: str, digit_prefix: str) -> str:
    """`normalize_identifier` that first splits camelCase: InvoiceLineId -> invoice_line_id,
    PlaylistTrack -> playlist_track (lowercasing alone gives invoicelineid, which hides the _id
    that join inference and the LLM read). snake_case names are unchanged."""
    return normalize_identifier(_CAMEL_BOUNDARY.sub("_", raw.strip()), fallback=fallback, digit_prefix=digit_prefix)


def table_name_for(filename: str) -> str:
    stem = PurePosixPath(filename.replace("\\", "/")).stem
    return normalize_identifier(stem, fallback="uploaded_table", digit_prefix="t_")


@dataclass
class ParquetFile:
    path: Path
    num_rows: int
    columns: list[str]
    renamed_columns: dict[str, str] = field(default_factory=dict)  # original -> normalized
    notes: list[str] = field(default_factory=list)  # shown to the user (sheets left out)


def convert_to_parquet(source: Path, suffix: str, dest: Path) -> ParquetFile:
    """Read a CSV, Excel or Parquet upload and write it as Parquet with normalized column names."""
    notes: list[str] = []
    try:
        if suffix == ".csv":
            table = _read_csv(source)
        elif suffix == ".xlsx":
            table, notes = _read_xlsx(source)
        elif suffix == ".parquet":
            table = pq.read_table(source)
        else:
            raise UploadError(UNSUPPORTED_FORMAT)
    except UploadError:
        raise
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, UnicodeError, OSError) as exc:
        raise UploadError(f"Could not read the {suffix.lstrip('.').upper()} file: {exc}") from None

    if table.num_columns == 0:
        raise UploadError("The file has no columns")
    if table.num_rows == 0:
        raise UploadError("The file has no rows")

    names: list[str] = []
    renamed: dict[str, str] = {}
    for index, original in enumerate(table.column_names):
        name = normalize_identifier(str(original), fallback=f"column_{index + 1}", digit_prefix="c_")
        base, n = name, 2
        while name in names:
            suffix_tag = f"_{n}"
            name = f"{base[:_MAX_IDENTIFIER - len(suffix_tag)]}{suffix_tag}"
            n += 1
        names.append(name)
        if name != original:
            renamed[str(original)] = name
    table = table.rename_columns(names)

    dest.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, dest, compression="zstd")
    return ParquetFile(path=dest, num_rows=table.num_rows, columns=names, renamed_columns=renamed, notes=notes)


def _read_csv(source: Path) -> pa.Table:
    """UTF-8 first; spreadsheet exports are often Windows-1252 instead. pyarrow does not fail on
    those: the header fails to decode only when read, and invalid text columns become binary."""
    table = pacsv.read_csv(source)
    try:
        table.column_names  # decodes the header
        utf8 = not any(pa.types.is_binary(f.type) or pa.types.is_large_binary(f.type) for f in table.schema)
    except UnicodeDecodeError:
        utf8 = False
    if utf8:
        return table
    return pacsv.read_csv(source, read_options=pacsv.ReadOptions(encoding="cp1252"))


def _read_xlsx(source: Path) -> tuple[pa.Table, list[str]]:
    """The workbook's first sheet with data: header in its first non-empty row, empty rows and
    unnamed empty columns dropped, a column of mixed numbers and text read as text. Cells hold their
    last computed values (formulas are not evaluated). Parsed with defusedxml (installed), read-only."""
    import zipfile

    import openpyxl
    import pandas as pd

    try:
        with zipfile.ZipFile(source) as archive:
            if sum(info.file_size for info in archive.infolist()) > _XLSX_MAX_EXPANDED_BYTES:
                raise UploadError("The Excel file expands to more than 1 GB")
        book = openpyxl.load_workbook(source, read_only=True, data_only=True)
    except UploadError:
        raise
    except Exception as exc:  # not a zip, not a workbook, encrypted
        raise UploadError(f"Could not read the Excel file: {str(exc)[:200]}") from None
    try:
        for index, sheet in enumerate(book.worksheets):
            rows = [row for row in sheet.iter_rows(values_only=True)
                    if any(cell is not None and str(cell).strip() != "" for cell in row)]
            if len(rows) < 2:
                continue
            width = max(len(row) for row in rows)
            header = [("" if cell is None else str(cell).strip()) for cell in rows[0]] + [""] * width
            body = [list(row) + [None] * (width - len(row)) for row in rows[1:]]
            keep = [i for i in range(width) if header[i] or any(row[i] is not None for row in body)]
            frame = pd.DataFrame([[row[i] for i in keep] for row in body], columns=[header[i] for i in keep])
            for column in frame.columns[frame.dtypes == object]:
                kinds = {type(v) for v in frame[column] if v is not None}
                if len(kinds) > 1:  # "N/A" among numbers, dates among text
                    frame[column] = frame[column].map(lambda v: None if v is None else str(v))
            others = [s.title for s in book.worksheets if s.title != sheet.title]
            notes = ([f"Read the sheet '{sheet.title}'; the other sheets ({', '.join(others[:5])}"
                      f"{', ...' if len(others) > 5 else ''}) were not loaded. Upload each as its own file."]
                     if others else [])
            try:
                return pa.Table.from_pandas(frame, preserve_index=False), notes
            except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
                as_text = frame.astype(object).map(lambda v: None if v is None or v != v else str(v))
                return pa.Table.from_pandas(as_text, preserve_index=False), notes
        raise UploadError("The workbook has no sheet with a header and at least one row")
    finally:
        book.close()


class ManagedUploadStore(Protocol):
    def put(self, local_file: Path, *, workspace_id: str, connection_id: str, table: str) -> str:
        """Store the file; return its URL as jammi-server sees it."""

    def url_for(self, *, workspace_id: str, connection_id: str, table: str) -> str:
        """The URL put() returns for this file, without writing it."""

    def delete(self, url: str) -> None:
        """Delete a file previously returned by put (a no-op if it is already gone)."""


def _relative_key(workspace_id: str, connection_id: str, table: str) -> PurePosixPath:
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError(f"Invalid upload table name {table!r}")
    connection = re.sub(r"[^a-z0-9]", "", connection_id.lower())[:32]
    if not connection:
        raise ValueError("Invalid connection id")
    return PurePosixPath(f"w{workspace_tag(workspace_id)}", connection, f"{table}.parquet")


class LocalUploadStore:
    """A directory shared with jammi-server (a Compose volume): NLviz writes it at `root`, the
    server reads the same files under `jammi_root` (e.g. root=/srv/uploads, jammi_root=/uploads)."""

    def __init__(self, root: Path, jammi_root: str):
        self._root = Path(root).resolve()
        self._jammi_root = jammi_root.rstrip("/")
        if not self._jammi_root.startswith("/") and not self._jammi_root.startswith("file:///"):
            raise ValueError("jammi_root must be an absolute path as jammi-server sees it")

    def url_for(self, *, workspace_id: str, connection_id: str, table: str) -> str:
        return f"{self._jammi_root}/{_relative_key(workspace_id, connection_id, table)}"

    def put(self, local_file: Path, *, workspace_id: str, connection_id: str, table: str) -> str:
        key = _relative_key(workspace_id, connection_id, table)
        target = self._root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_file, target)
        return f"{self._jammi_root}/{key}"

    def delete(self, url: str) -> None:
        prefix = f"{self._jammi_root}/"
        if not url.startswith(prefix):
            raise ValueError("URL is not in this upload store")
        target = (self._root / PurePosixPath(url[len(prefix):])).resolve()
        if self._root not in target.parents:
            raise ValueError("URL is not in this upload store")
        target.unlink(missing_ok=True)
        for parent in (target.parent, target.parent.parent):
            try:
                parent.rmdir()  # only removes empty connection/workspace directories
            except OSError:
                break


class S3UploadStore:
    """NLviz's upload bucket. jammi-server must be able to read `s3://<bucket>/<prefix>/` with its
    own role; that role should be scoped to this prefix, since Jammi will read any object it can
    (issue J25)."""

    def __init__(self, s3, prefix: str = "jammi-uploads"):
        self._s3 = s3
        self._prefix = prefix.strip("/")

    def url_for(self, *, workspace_id: str, connection_id: str, table: str) -> str:
        return f"s3://{self._s3.bucket}/{self._prefix}/{_relative_key(workspace_id, connection_id, table)}"

    def put(self, local_file: Path, *, workspace_id: str, connection_id: str, table: str) -> str:
        key = f"{self._prefix}/{_relative_key(workspace_id, connection_id, table)}"
        with open(local_file, "rb") as fobj:
            return self._s3.upload_fileobj(key, fobj)

    def delete(self, url: str) -> None:
        key = self._s3.key_from_url(url)
        if not key.startswith(f"{self._prefix}/"):
            raise ValueError("URL is not in this upload store")
        self._s3.delete(key)
