import json
import os
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

# ── Config ──────────────────────────────────────────────────────────────────
HF_DATASET_URL = os.environ.get(
    "ICMR_HF_DATASET_URL",
    "https://huggingface.co/datasets/rehuuuu/icrm-hitek-fulldb/resolve/main",
).rstrip("/")

DUPLICATE_CAP = 2

SEARCH_FIELDS = [
    "name", "fathersName", "phoneNumber", "aadharNumber", "otherNumber",
    "address", "district", "pincode", "state", "town", "source",
]
NUMBER_FIELDS = ["phoneNumber", "aadharNumber", "otherNumber"]

PHONE_FILES = f"{HF_DATASET_URL}/idx_phone.*.parquet"
AADHAR_FILES = f"{HF_DATASET_URL}/idx_aadhar.*.parquet"


# ── DuckDB Remote Query ─────────────────────────────────────────────────────
def _duckdb_search(field: str, value: str, limit: int = 10) -> list[dict]:
    """
    DuckDB directly remote parquet se query karta hai.
    Sirf matching rows download hoti hain (HTTP range requests).
    Vercel 10s timeout me easily fit.
    """
    if field in ("phoneNumber", "otherNumber"):
        file_pattern = PHONE_FILES
        column = field
    elif field == "aadharNumber":
        file_pattern = AADHAR_FILES
        column = "aadharNumber"
    else:
        file_pattern = PHONE_FILES
        column = field

    try:
        con = duckdb.connect()
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("SET enable_progress_bar=false;")
        # Vercel ke liye important
        con.execute("SET http_keep_alive=false;")
        con.execute("SET http_timeout=8000;")  # 8 sec

        query = f"""
            SELECT * FROM read_parquet('{file_pattern}')
            WHERE CAST({column} AS VARCHAR) = ?
            LIMIT ?
        """
        result = con.execute(query, [str(value).strip(), int(limit)]).fetchdf()
        con.close()

        if result.empty:
            return []
        return result.to_dict(orient="records")
    except Exception as e:
        print(f"⚠️ DuckDB error ({field}={value}): {e}")
        return []


# ── Dedup & Connected ───────────────────────────────────────────────────────
def _connected_numbers(row: dict) -> list[dict]:
    connected, seen = [], set()
    for field in NUMBER_FIELDS:
        raw = row.get(field)
        if raw is None:
            continue
        try:
            if str(raw) == "nan":
                continue
        except Exception:
            pass
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        connected.append({"field": field, "value": value})
    return connected


def _cap_duplicates(rows: list[dict]) -> list[dict]:
    seen = {}
    out = []
    for r in rows:
        ph = str(r.get("phoneNumber", "")).strip()
        ad = str(r.get("aadharNumber", "")).strip()
        key = (ph, ad) if ph or ad else (
            str(r.get("name", "")), str(r.get("fathersName", ""))
        )
        n = seen.get(key, 0)
        if n < DUPLICATE_CAP:
            seen[key] = n + 1
            record = dict(r)
            record["connected_numbers"] = _connected_numbers(record)
            out.append(record)
    return out


# ── Search Logic ────────────────────────────────────────────────────────────
def _unified_search(q: str, limit: int = 10) -> dict:
    q = q.strip()
    if not q or not q.isdigit() or len(q) < 8:
        return {"query": q, "searched_fields": [], "count": 0, "results": []}

    all_rows = []
    searched = []

    if len(q) in (10, 11):
        rows = _duckdb_search("phoneNumber", q, limit)
        if rows:
            all_rows.extend(rows)
            searched.append("phoneNumber")

    if len(q) == 12:
        rows = _duckdb_search("aadharNumber", q, limit)
        if rows:
            all_rows.extend(rows)
            searched.append("aadharNumber")

    all_rows = _cap_duplicates(all_rows)[:limit]

    return {
        "query": q,
        "searched_fields": searched,
        "count": len(all_rows),
        "results": all_rows,
    }


def _field_search(field: str, value: str, mode: str, limit: int) -> dict:
    if field not in SEARCH_FIELDS:
        return {
            "field": field, "value": value, "mode": mode,
            "count": 0, "results": [], "error": "Unknown field",
        }
    try:
        results = _duckdb_search(field, value, limit)
        results = _cap_duplicates(results)[:limit]
        return {
            "field": field, "value": value, "mode": mode,
            "count": len(results), "results": results,
        }
    except Exception as e:
        return {
            "field": field, "value": value, "mode": mode,
            "count": 0, "results": [], "error": str(e),
        }


# ── FastAPI App ─────────────────────────────────────────────────────────────
app = FastAPI(title="ICMR + HITEK Search API")


class BatchRequest(BaseModel):
    queries: list[dict[str, Any]]
    limit: int = 10


@app.get("/")
def root():
    return {
        "app": "ICMR + HITEK Search API",
        "dataset": "rehuuuu/icrm-hitek-fulldb",
        "runtime": "vercel-serverless",
        "columns": SEARCH_FIELDS,
        "docs": "/docs",
        "developer": "@kzr0x | channel @api_wallah",
    }


@app.get("/api/health")
@app.get("/health")
def health():
    return {
        "status": "ok",
        "dataset": "rehuuuu/icrm-hitek-fulldb",
        "searchable_fields": ["phoneNumber", "aadharNumber"],
    }


@app.get("/search")
@app.get("/api/search")
def search(
    q: str | None = Query(None),
    mobile: str | None = Query(None),
    aadhar: str | None = Query(None),
    field: str | None = Query(None),
    mode: str = Query("exact"),
    limit: int = Query(10, ge=1, le=50),
    pretty: bool = Query(True),
):
    if aadhar:
        q_val = aadhar.strip()
        field = "aadharNumber"
    elif mobile:
        q_val = mobile.strip()
        field = "phoneNumber"
    elif q:
        q_val = q.strip()
    else:
        raise HTTPException(422, "Provide q, mobile, or aadhar")

    if not q_val:
        raise HTTPException(422, "Query cannot be empty")

    if field:
        data = _field_search(field, q_val, mode, limit)
    else:
        data = _unified_search(q_val, limit)

    result = {"success": bool(data.get("count", 0) > 0), **data, "number": q_val}
    content = json.dumps(result, indent=2 if pretty else None, ensure_ascii=False)
    return Response(content=content, media_type="application/json")


@app.get("/search/phone/{number}")
@app.get("/api/search/phone/{number}")
def search_phone(
    number: str,
    limit: int = Query(10, ge=1, le=50),
    pretty: bool = Query(True),
):
    data = _field_search("phoneNumber", number, "exact", limit)
    result = {"success": bool(data.get("count", 0) > 0), **data, "number": number}
    content = json.dumps(result, indent=2 if pretty else None, ensure_ascii=False)
    return Response(content=content, media_type="application/json")


@app.get("/search/aadhar/{number}")
@app.get("/api/search/aadhar/{number}")
def search_aadhar(
    number: str,
    limit: int = Query(10, ge=1, le=50),
    pretty: bool = Query(True),
):
    data = _field_search("aadharNumber", number, "exact", limit)
    result = {"success": bool(data.get("count", 0) > 0), **data, "number": number}
    content = json.dumps(result, indent=2 if pretty else None, ensure_ascii=False)
    return Response(content=content, media_type="application/json")


@app.post("/search/parallel")
@app.post("/api/search/parallel")
def search_parallel(req: BatchRequest):
    if not req.queries:
        raise HTTPException(400, "queries must not be empty")
    if len(req.queries) > 10:  # Vercel pe 10 sec limit — chhota rakho
        raise HTTPException(400, "max 10 queries per batch (vercel timeout)")

    results = []
    for item in req.queries:
        results.append(
            _field_search(
                item.get("field", "phoneNumber"),
                item.get("value", ""),
                item.get("mode", "exact"),
                int(item.get("limit", req.limit)),
            )
        )
    return {"searches": len(req.queries), "results": results}


# Vercel handler entry point
handler = app
