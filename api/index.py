import json
import os
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

# ── Config ──────────────────────────────────────────────────────────────────
# 🔑 Token hardcoded — ENV optional hai
HF_TOKEN = os.environ.get("HF_TOKEN", "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)

# 📦 Dataset
HF_DATASET_REPO = os.environ.get(
    "ICMR_HF_REPO",
    "bronx-ultra/icrm-hitek-full-db-mixed-bucket"
)

# DuckDB hf:// paths
PHONE_FILES  = f"hf://datasets/{HF_DATASET_REPO}/*phone*.parquet"
AADHAR_FILES = f"hf://datasets/{HF_DATASET_REPO}/*aadhar*.parquet"
ALL_FILES    = f"hf://datasets/{HF_DATASET_REPO}/**/*.parquet"

DUPLICATE_CAP = 2

SEARCH_FIELDS = [
    "name", "fathersName", "phoneNumber", "aadharNumber", "otherNumber",
    "address", "district", "pincode", "state", "town", "source",
]
NUMBER_FIELDS = ["phoneNumber", "aadharNumber", "otherNumber"]


# ── DuckDB Connection ───────────────────────────────────────────────────────
def _get_conn() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET enable_progress_bar=false;")
    con.execute("SET http_keep_alive=false;")
    con.execute("SET http_timeout=8000;")
    # HF Token — public/private dono ke liye
    if HF_TOKEN and HF_TOKEN.startswith("hf_"):
        con.execute(
            f"CREATE OR REPLACE SECRET hf (TYPE huggingface, TOKEN '{HF_TOKEN}')"
        )
    return con


# ── Core Search ─────────────────────────────────────────────────────────────
def _duckdb_search(field: str, value: str, limit: int = 10) -> list[dict]:
    if field in ("phoneNumber", "otherNumber"):
        file_pattern = PHONE_FILES
        column = field
    elif field == "aadharNumber":
        file_pattern = AADHAR_FILES
        column = "aadharNumber"
    else:
        file_pattern = ALL_FILES
        column = field

    try:
        con = _get_conn()
        query = f"""
            SELECT * FROM read_parquet('{file_pattern}', union_by_name=true)
            WHERE CAST({column} AS VARCHAR) = ?
            LIMIT ?
        """
        result = con.execute(query, [str(value).strip(), int(limit)]).fetchdf()
        con.close()

        if result.empty:
            return []
        return [
            {k: (None if str(v) == "nan" else v) for k, v in row.items()}
            for row in result.to_dict(orient="records")
        ]
    except Exception as e:
        print(f"⚠️ DuckDB error ({field}={value}): {e}")
        return []


# ── Connected Numbers ───────────────────────────────────────────────────────
def _connected_numbers(row: dict) -> list[dict]:
    connected, seen = [], set()
    for field in NUMBER_FIELDS:
        raw = row.get(field)
        if raw is None:
            continue
        value = str(raw).strip()
        if not value or value == "nan" or value in seen:
            continue
        seen.add(value)
        connected.append({"field": field, "value": value})
    return connected


def _cap_duplicates(rows: list[dict]) -> list[dict]:
    seen, out = {}, []
    for r in rows:
        ph = str(r.get("phoneNumber", "") or "").strip()
        ad = str(r.get("aadharNumber", "") or "").strip()
        key = (ph, ad) if (ph or ad) else (
            str(r.get("name", "")), str(r.get("fathersName", ""))
        )
        n = seen.get(key, 0)
        if n < DUPLICATE_CAP:
            seen[key] = n + 1
            record = dict(r)
            record["connected_numbers"] = _connected_numbers(record)
            out.append(record)
    return out


# ── Unified Search ──────────────────────────────────────────────────────────
def _unified_search(q: str, limit: int = 10) -> dict:
    q = q.strip()
    if not q or not q.isdigit() or len(q) < 8:
        return {"query": q, "searched_fields": [], "count": 0, "results": []}

    all_rows, searched = [], []

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

    # Enrich: connected numbers se extra records
    if all_rows:
        connected_searches = set()
        for row in all_rows[:3]:
            for nf in NUMBER_FIELDS:
                nv = row.get(nf)
                if nv and str(nv) != "nan" and str(nv) not in connected_searches:
                    connected_searches.add(str(nv))
                    extra = _duckdb_search(nf, str(nv), limit=2)
                    if extra:
                        all_rows.extend(extra)
                        if nf not in searched:
                            searched.append(nf)

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
        results = _cap_duplicates(_duckdb_search(field, value, limit))[:limit]
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
app = FastAPI(title="ICMR + HITEK Full Info API")


class BatchRequest(BaseModel):
    queries: list[dict[str, Any]]
    limit: int = 10


@app.get("/")
def root():
    return {
        "app": "ICMR + HITEK Full Info API",
        "dataset": HF_DATASET_REPO,
        "runtime": "vercel-serverless",
        "endpoints": [
            "GET /search?q=<number>",
            "GET /search/phone/<number>",
            "GET /search/aadhar/<number>",
            "GET /info/<number>",
            "POST /search/parallel",
            "GET /health",
            "GET /debug",
        ],
        "docs": "/docs",
        "developer": "@kzr0x | channel @api_wallah",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "dataset": HF_DATASET_REPO,
        "token_loaded": bool(HF_TOKEN and HF_TOKEN.startswith("hf_")),
        "searchable_fields": ["phoneNumber", "aadharNumber"],
    }


@app.get("/debug")
def debug():
    """Dataset aur token check karne ke liye."""
    try:
        con = _get_conn()
        result = con.execute(
            f"SELECT * FROM read_parquet('{PHONE_FILES}', union_by_name=true) LIMIT 1"
        ).fetchdf()
        con.close()
        return {
            "status": "connected",
            "dataset": HF_DATASET_REPO,
            "pattern": PHONE_FILES,
            "token_loaded": bool(HF_TOKEN and HF_TOKEN.startswith("hf_")),
            "columns": list(result.columns) if not result.empty else [],
            "sample_rows": len(result),
        }
    except Exception as e:
        return {
            "status": "error",
            "dataset": HF_DATASET_REPO,
            "pattern": PHONE_FILES,
            "token_loaded": bool(HF_TOKEN and HF_TOKEN.startswith("hf_")),
            "error": str(e),
        }


# ── /search ─────────────────────────────────────────────────────────────────
@app.get("/search")
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
        q_val, field = aadhar.strip(), "aadharNumber"
    elif mobile:
        q_val, field = mobile.strip(), "phoneNumber"
    elif q:
        q_val = q.strip()
    else:
        raise HTTPException(422, "Provide q, mobile, or aadhar")

    if not q_val:
        raise HTTPException(422, "Query cannot be empty")

    data = _field_search(field, q_val, mode, limit) if field else _unified_search(q_val, limit)
    result = {"success": bool(data.get("count", 0) > 0), **data, "number": q_val}
    content = json.dumps(result, indent=2 if pretty else None, ensure_ascii=False, default=str)
    return Response(content=content, media_type="application/json")


# ── /search/phone/<number> ──────────────────────────────────────────────────
@app.get("/search/phone/{number}")
def search_phone(number: str, limit: int = Query(10, ge=1, le=50), pretty: bool = Query(True)):
    data = _field_search("phoneNumber", number, "exact", limit)
    result = {"success": bool(data.get("count", 0) > 0), **data, "number": number}
    content = json.dumps(result, indent=2 if pretty else None, ensure_ascii=False, default=str)
    return Response(content=content, media_type="application/json")


# ── /search/aadhar/<number> ─────────────────────────────────────────────────
@app.get("/search/aadhar/{number}")
def search_aadhar(number: str, limit: int = Query(10, ge=1, le=50), pretty: bool = Query(True)):
    data = _field_search("aadharNumber", number, "exact", limit)
    result = {"success": bool(data.get("count", 0) > 0), **data, "number": number}
    content = json.dumps(result, indent=2 if pretty else None, ensure_ascii=False, default=str)
    return Response(content=content, media_type="application/json")


# ── /info/<number> — FULL INFO ──────────────────────────────────────────────
@app.get("/info/{number}")
def full_info(number: str, limit: int = Query(10, ge=1, le=50), pretty: bool = Query(True)):
    number = number.strip()
    data = _unified_search(number, limit)
    result = {
        "success": bool(data.get("count", 0) > 0),
        "number": number,
        "number_type": (
            "phone" if len(number) in (10, 11)
            else "aadhar" if len(number) == 12
            else "unknown"
        ),
        **data,
    }
    content = json.dumps(result, indent=2 if pretty else None, ensure_ascii=False, default=str)
    return Response(content=content, media_type="application/json")


# ── /search/parallel ────────────────────────────────────────────────────────
@app.post("/search/parallel")
def search_parallel(req: BatchRequest):
    if not req.queries:
        raise HTTPException(400, "queries must not be empty")
    if len(req.queries) > 10:
        raise HTTPException(400, "max 10 queries per batch (vercel timeout)")

    results = [
        _field_search(
            item.get("field", "phoneNumber"),
            item.get("value", ""),
            item.get("mode", "exact"),
            int(item.get("limit", req.limit)),
        )
        for item in req.queries
    ]
    return {"searches": len(req.queries), "results": results}
