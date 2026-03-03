# NL Query & Visualization Tool — Revised Complete Plan v2
### Incorporating architectural corrections and security hardening

---

## What Changed From v1 (Read This First)

Eight specific problems were identified in v1 and are corrected here. The changes are not cosmetic — two of them are architectural:

**Architectural changes:**
- Phase 2 now goes directly to ECS + ALB. The EC2 stepping stone is removed. It introduced a fatal ACM/HTTPS mismatch and would have required a complete infrastructure rewrite a week later anyway.
- SQL compilation is now deterministic. The LLM no longer writes SQL. It maps natural language to semantic concepts (metric names, dimension names, filters, time range). Your code builds the SQL from those concepts using the semantic layer definition. This is faster, cheaper, more reliable, and makes the semantic layer genuinely central rather than decorative.

**Security additions (non-negotiable even for MVP):**
- SQL safety model: SELECT-only enforcement via AST parser, table allowlist, read-only DB user
- Two-level agent trace: user-safe trace vs. debug trace
- Prompt injection defense

**Infrastructure fixes:**
- Terraform DynamoDB state locking added
- ACM now correctly attached to ALB, not EC2

**Reliability fixes:**
- Golden tests run at `temperature=0` with a pinned model version
- DuckDB/S3 path clarified: local `.duckdb` file for MVP, S3 Parquet for Phase 3+

---

## The Core Architectural Shift: Deterministic SQL Compilation

This is the most important change in v2 and deserves its own explanation before the plan begins.

### v1 Architecture (two LLM calls for SQL):
```
Question → [LLM: Query Planner] → QueryPlan JSON
                                        ↓
                               [LLM: SQL Compiler] → SQL string
```

### v2 Architecture (one LLM call, deterministic compilation):
```
Question → [LLM: Intent Mapper] → SemanticIntent JSON
                                        ↓
                         [Code: SQL Builder] → SQL string
                         (reads semantic YAML, builds SQL deterministically)
```

**Why this matters:**

The LLM's job shrinks dramatically. Instead of "write me SQL for this question against this schema," it becomes "which of these named metrics and dimensions does this question ask about?" The semantic layer already defines exactly how each metric translates to SQL, which tables to join, and which filters apply. Your code assembles those pieces. No more hallucinated column names. No more wrong joins. The LLM is doing intent classification, which it's very good at. SQL generation, which it's unreliable at, is now deterministic.

LLM fallback exists for questions that can't be satisfied by the semantic layer (e.g., ad-hoc column arithmetic). But the happy path — which covers 85%+ of real business questions — never asks the LLM to write SQL.

---

## Final Tech Stack (No Changes From v1)

| Layer | Technology | Justification |
|---|---|---|
| Agent Orchestration | LangGraph | Stateful graph with cycles for retry loop. Industry standard. |
| LLM | Claude API (claude-sonnet-4-5) | Best structured output reliability via Instructor. |
| Structured Outputs | Pydantic + Instructor | Type-safe LLM outputs. Enforces SemanticIntent schema. |
| SQL Safety | `sqlglot` (AST parser) | Parse and validate SQL before execution. SELECT-only enforcement. |
| Backend | Python FastAPI | Async. Your strongest language. |
| Local DB (MVP) | DuckDB (local file) | CSV ingestion, fast analytics, no server. |
| Production DB (app metadata) | AWS RDS PostgreSQL | Query history, users, connectors, feedback. |
| Semantic Layer | Custom YAML + Pydantic + SQL Builder | The compiler. Defines every metric as executable SQL fragments. |
| Charts | Plotly (Python → JSON spec) | Deterministic templates. LLM picks type, code renders. |
| Frontend | Next.js + Tailwind CSS | Portfolio-quality UI. |
| Agent State (production) | ElastiCache Redis | Persistent LangGraph checkpoints. |
| File Storage | AWS S3 | CSV/Parquet uploads in Phase 3+. |
| Secrets | AWS Secrets Manager | API keys, DB creds. Never in env vars. |
| Infrastructure | Terraform | IaC. Module structure mirrors professional infra teams. |
| Container Runtime | AWS ECS Fargate | Serverless containers. No EC2 management. |
| Load Balancer | AWS ALB | TLS termination with ACM. Health checks. Routing. |
| CI/CD | GitHub Actions | Test gate + deploy on merge to main. |
| Observability | CloudWatch + X-Ray | Logs, metrics, distributed tracing per agent node. |
| Auth | AWS Cognito | Managed auth, SSO-ready for enterprise. |
| CDN | CloudFront + ACM | Edge caching for frontend. |

---

## Repository Structure

```
nl-query-tool/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/routes/
│   │   │   ├── chat.py             # POST /chat
│   │   │   ├── schema.py           # GET /schema
│   │   │   └── feedback.py         # POST /feedback
│   │   ├── agent/
│   │   │   ├── state.py            # AgentState TypedDict
│   │   │   ├── graph.py            # LangGraph graph
│   │   │   └── nodes/
│   │   │       ├── schema_retriever.py
│   │   │       ├── intent_mapper.py      # RENAMED: LLM maps NL → SemanticIntent
│   │   │       ├── sql_builder.py        # NEW: deterministic SQL from SemanticIntent
│   │   │       ├── sql_safety.py         # NEW: AST validation before execution
│   │   │       ├── executor.py
│   │   │       ├── validator.py
│   │   │       ├── chart_selector.py
│   │   │       └── explainer.py
│   │   ├── connectors/
│   │   │   ├── base.py             # DataConnector ABC
│   │   │   ├── duckdb_connector.py
│   │   │   ├── postgres_connector.py   # stub
│   │   │   └── redshift_connector.py   # stub
│   │   ├── semantic/
│   │   │   ├── loader.py           # YAML → SemanticRegistry
│   │   │   ├── sql_builder.py      # SemanticIntent → SQL (the compiler)
│   │   │   └── schemas/
│   │   │       └── ecommerce.yaml
│   │   ├── security/
│   │   │   ├── sql_safety.py       # NEW: SELECT-only + allowlist
│   │   │   └── trace_filter.py     # NEW: user-safe vs debug trace
│   │   ├── charts/
│   │   │   ├── templates.py        # 5 Plotly templates
│   │   │   └── selector.py
│   │   └── models/
│   │       ├── semantic_intent.py  # SemanticIntent Pydantic model (replaces QueryPlan)
│   │       └── responses.py
│   ├── tests/
│   │   ├── golden_tests.py         # 20 tests, temperature=0, pinned model
│   │   └── test_sql_safety.py      # NEW: test SQL safety enforcement
│   ├── Dockerfile
│   └── requirements.txt
│
├── frontend/
│   ├── app/
│   │   ├── page.tsx
│   │   └── components/
│   │       ├── ChatPanel.tsx
│   │       ├── ChartPanel.tsx
│   │       ├── TablePanel.tsx
│   │       ├── SQLViewer.tsx
│   │       └── AgentTrace.tsx      # shows user-safe trace only
│   ├── Dockerfile
│   └── package.json
│
├── infrastructure/
│   ├── terraform/
│   │   ├── bootstrap/              # NEW: S3 + DynamoDB for Terraform state
│   │   │   └── main.tf
│   │   ├── environments/
│   │   │   ├── dev/
│   │   │   └── prod/
│   │   └── modules/
│   │       ├── networking/
│   │       ├── ecs/
│   │       ├── alb/                # NEW: separate module (Phase 2 now uses ALB)
│   │       ├── rds/
│   │       ├── elasticache/
│   │       ├── s3/
│   │       ├── cognito/
│   │       ├── secrets/
│   │       └── monitoring/
│   └── docker-compose.yml
│
├── .github/workflows/
│   ├── test.yml
│   └── deploy.yml
│
├── docs/
│   ├── ARCHITECTURE.md
│   └── SEMANTIC_LAYER.md
└── README.md
```

---

# PHASE 1: MVP (Weeks 1–2)
### Goal: Working local demo. End-to-end pipeline. Shippable.

---

## Micro-Goal 1.1 — Project Foundation and Dataset (Day 1)

**Steps:**
- Initialize full repo structure. Every directory exists. Every stub file is committed.
- Write `docker-compose.yml` with `backend` (FastAPI port 8000) and `frontend` (Next.js port 3000).
- Download the [Brazilian E-Commerce (Olist) dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) — 8 tables, ~500k orders, realistic joins. This is the right choice: it has revenue, customers, sellers, products, reviews, and time dimensions — enough to demonstrate 15+ distinct business questions.
- Write `seed.py`: loads all CSVs into a local DuckDB file (`data/ecommerce.duckdb`). Must be idempotent.
- Manually run 15 SQL queries. Write them down. This is not busywork — this understanding directly determines the quality of your semantic YAML.

**The DuckDB data model for MVP:**
```
# MVP: DuckDB reads a local .duckdb file
# NOT a remote connection. NOT S3.
conn = duckdb.connect("data/ecommerce.duckdb")

# Phase 3 upgrade path (when S3 is introduced):
# DuckDB can query Parquet files stored on S3 directly:
# conn.execute("SELECT * FROM 's3://bucket/orders.parquet'")
# This works for Parquet/CSV files — NOT for a .duckdb database file stored on S3.
# The upgrade path is: export tables to Parquet → upload to S3 → switch connector.
```

**Success criterion:** `python seed.py` loads data. `SELECT COUNT(*) FROM orders` returns 99,441.

---

## Micro-Goal 1.2 — Connector Abstraction (Day 1–2)

**Steps:**
- Write `connectors/base.py`:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
import pandas as pd

@dataclass
class SchemaContext:
    tables: dict[str, list[dict]]   # table_name → [{name, type, nullable, sample_values}]
    row_counts: dict[str, int]
    join_paths: list[dict]          # [{from_table, to_table, from_col, to_col}]

class DataConnector(ABC):
    @abstractmethod
    def get_schema(self) -> SchemaContext: ...

    @abstractmethod
    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame: ...

    @abstractmethod
    def test_connection(self) -> bool: ...

    @abstractmethod
    def get_connector_type(self) -> str: ...
```

- Implement `DuckDBConnector` fully against `ecommerce.duckdb`.
- Write `PostgresConnector` and `RedshiftConnector` as stubs (`raise NotImplementedError`). Their signatures are correct. Their presence signals architectural thinking.

```python
CONNECTOR_REGISTRY = {
    "duckdb":    DuckDBConnector,
    "postgres":  PostgresConnector,   # stub — Phase 4
    "redshift":  RedshiftConnector,   # stub — Phase 4
    "snowflake": None,                # future
    "athena":    None,                # future
}
```

**Success criterion:** `DuckDBConnector().get_schema()` returns a populated `SchemaContext` with all 8 Olist tables and their column metadata.

---

## Micro-Goal 1.3 — Semantic Layer (Day 2)

**This is the most important file in the entire project. Budget half a day.**

The semantic YAML serves two purposes now: it's context for the LLM's intent mapper, and it's the source of truth for the deterministic SQL builder. Every metric you define here is a metric that works perfectly. Every metric you leave out is a question the agent can't answer on the happy path.

**`semantic/schemas/ecommerce.yaml`** — write at minimum:

```yaml
version: "1.0"
dataset: olist_ecommerce
description: "Brazilian e-commerce platform with orders, customers, sellers, products"

tables:
  - name: orders
    description: "One row per order"
    time_columns:
      - name: order_purchase_timestamp
        display: "Order Date"
      - name: order_delivered_customer_date
        display: "Delivery Date"

  - name: order_items
    description: "Line items within each order (one order can have many items)"

  - name: order_payments
    description: "Payment details per order"

  - name: order_reviews
    description: "Customer review scores per order"

  - name: customers
    description: "Customer location and identity"

  - name: sellers
    description: "Seller location"

  - name: products
    description: "Product details and category"

joins:
  - from: orders
    to: order_items
    on: "orders.order_id = order_items.order_id"
    type: LEFT

  - from: orders
    to: order_payments
    on: "orders.order_id = order_payments.order_id"
    type: LEFT

  - from: orders
    to: order_reviews
    on: "orders.order_id = order_reviews.order_id"
    type: LEFT

  - from: orders
    to: customers
    on: "orders.customer_id = customers.customer_id"
    type: LEFT

  - from: order_items
    to: sellers
    on: "order_items.seller_id = sellers.seller_id"
    type: LEFT

  - from: order_items
    to: products
    on: "order_items.product_id = products.product_id"
    type: LEFT

metrics:
  - name: total_revenue
    display_name: "Total Revenue"
    description: "Sum of payment value for all non-cancelled orders"
    aggregation: SUM
    sql_expression: "op.payment_value"
    required_tables: ["orders", "order_payments op"]
    base_filter: "orders.order_status NOT IN ('cancelled', 'unavailable')"

  - name: order_count
    display_name: "Number of Orders"
    description: "Count of distinct orders"
    aggregation: COUNT_DISTINCT
    sql_expression: "orders.order_id"
    required_tables: ["orders"]
    base_filter: null

  - name: average_order_value
    display_name: "Average Order Value"
    description: "Mean payment value per order"
    aggregation: AVG
    sql_expression: "op.payment_value"
    required_tables: ["orders", "order_payments op"]
    base_filter: "orders.order_status NOT IN ('cancelled', 'unavailable')"

  - name: average_review_score
    display_name: "Average Review Score"
    description: "Mean customer review score (1–5)"
    aggregation: AVG
    sql_expression: "r.review_score"
    required_tables: ["orders", "order_reviews r"]
    base_filter: "r.review_score IS NOT NULL"

  - name: average_delivery_days
    display_name: "Average Delivery Time (Days)"
    description: "Mean days from purchase to delivery"
    aggregation: AVG
    sql_expression: "DATEDIFF('day', orders.order_purchase_timestamp, orders.order_delivered_customer_date)"
    required_tables: ["orders"]
    base_filter: "orders.order_delivered_customer_date IS NOT NULL"

  - name: cancellation_rate
    display_name: "Cancellation Rate"
    description: "Percentage of orders that were cancelled"
    aggregation: RATIO
    numerator_sql: "COUNT(CASE WHEN orders.order_status = 'cancelled' THEN 1 END)"
    denominator_sql: "COUNT(orders.order_id)"
    required_tables: ["orders"]
    base_filter: null

dimensions:
  - name: product_category
    display_name: "Product Category"
    sql_expression: "p.product_category_name_english"
    required_tables: ["order_items", "products p"]
    cardinality: medium

  - name: customer_state
    display_name: "Customer State"
    sql_expression: "c.customer_state"
    required_tables: ["orders", "customers c"]
    cardinality: low

  - name: seller_state
    display_name: "Seller State"
    sql_expression: "s.seller_state"
    required_tables: ["order_items", "sellers s"]
    cardinality: low

  - name: payment_type
    display_name: "Payment Method"
    sql_expression: "op.payment_type"
    required_tables: ["orders", "order_payments op"]
    cardinality: low

  - name: order_status
    display_name: "Order Status"
    sql_expression: "orders.order_status"
    required_tables: ["orders"]
    cardinality: low

  - name: review_score
    display_name: "Review Score"
    sql_expression: "r.review_score"
    required_tables: ["orders", "order_reviews r"]
    cardinality: low

time_dimensions:
  - name: order_date
    display_name: "Order Date"
    sql_expression: "orders.order_purchase_timestamp"
    default_granularity: month
    table: orders

  - name: delivery_date
    display_name: "Delivery Date"
    sql_expression: "orders.order_delivered_customer_date"
    default_granularity: month
    table: orders
```

**Write `semantic/loader.py`:** Parses YAML → Pydantic models → `SemanticRegistry` object. The registry exposes:
- `get_metric(name)` → metric definition
- `get_dimension(name)` → dimension definition
- `get_join_path(table_a, table_b)` → join SQL
- `to_prompt_context()` → formatted string injected into every LLM call

**Success criterion:** `SemanticRegistry.to_prompt_context()` produces a clear, formatted string listing all 6 metrics and 6 dimensions with their display names and descriptions. The LLM will use this string to match user questions to semantic concepts.

---

## Micro-Goal 1.4 — SQL Safety Model (Day 2) ← NEW

**This is non-negotiable even for a demo. If a real user can interact with your tool, this must exist.**

Install `sqlglot` — a Python SQL parser with no external dependencies.

**`security/sql_safety.py`:**

```python
import sqlglot
from sqlglot import exp

ALLOWED_TABLES = {
    "orders", "order_items", "order_payments",
    "order_reviews", "customers", "sellers", "products"
}

DENIED_COLUMNS = {
    "customer_unique_id", "customer_zip_code_prefix",
    # Add any PII columns here
}

class SQLSafetyError(Exception):
    pass

class SQLSafetyValidator:
    def validate(self, sql: str) -> str:
        """
        Validates SQL before execution. Returns cleaned SQL or raises SQLSafetyError.
        """
        try:
            parsed = sqlglot.parse_one(sql, dialect="duckdb")
        except Exception as e:
            raise SQLSafetyError(f"SQL parse error: {e}")

        # Rule 1: Only SELECT statements allowed
        if not isinstance(parsed, exp.Select):
            raise SQLSafetyError("Only SELECT statements are permitted")

        # Rule 2: No subqueries that write (INSERT, UPDATE, DELETE inside CTEs)
        for node in parsed.walk():
            if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create)):
                raise SQLSafetyError(f"Forbidden SQL operation: {type(node).__name__}")

        # Rule 3: All referenced tables must be in allowlist
        tables_referenced = {
            table.name.lower()
            for table in parsed.find_all(exp.Table)
        }
        disallowed = tables_referenced - ALLOWED_TABLES
        if disallowed:
            raise SQLSafetyError(f"Tables not permitted: {disallowed}")

        # Rule 4: No denied columns referenced
        columns_referenced = {
            col.name.lower()
            for col in parsed.find_all(exp.Column)
        }
        denied_found = columns_referenced & DENIED_COLUMNS
        if denied_found:
            raise SQLSafetyError(f"Columns not permitted (PII): {denied_found}")

        # Rule 5: LIMIT must be present and <= 5000
        limit = parsed.find(exp.Limit)
        if limit is None:
            # Inject LIMIT rather than reject
            sql = sql.rstrip(";") + " LIMIT 1000"
        else:
            limit_val = int(limit.expression.this)
            if limit_val > 5000:
                sql = sql.replace(str(limit_val), "5000")

        return sql
```

**Key point for the demo:** When the safety validator catches something, that error is logged in the agent trace as a visible, human-readable event: "Safety check: blocked write operation in generated SQL. Retrying with corrected constraints." This turns a security feature into a demo feature.

**Write `tests/test_sql_safety.py`** with at least 10 test cases:
- `SELECT * FROM orders` → passes
- `DROP TABLE orders` → fails with correct error
- `SELECT customer_unique_id FROM customers` → fails (PII column)
- SQL without LIMIT → LIMIT injected automatically
- SQL with LIMIT 999999 → capped to 5000

**Success criterion:** All 10 safety tests pass. The validator is wired as a mandatory step between SQL builder and executor.

---

## Micro-Goal 1.5 — AgentState and LangGraph Skeleton (Day 3)

**Updated AgentState reflecting the deterministic SQL compiler:**

```python
from typing import TypedDict, Any

class AgentState(TypedDict):
    # Input
    user_question: str
    conversation_history: list[dict]

    # Retrieval
    schema_context: str
    semantic_context: str

    # Intent Mapping (LLM output — replaces QueryPlan)
    semantic_intent: dict | None        # SemanticIntent Pydantic model as dict
    intent_confidence: float
    needs_clarification: bool
    clarification_question: str | None

    # SQL Building (deterministic — no LLM)
    generated_sql: str
    sql_build_method: str               # "semantic" | "llm_fallback"
    sql_safety_passed: bool

    # Execution
    result_df: Any                      # pd.DataFrame
    row_count: int
    execution_time_ms: float
    execution_error: str | None

    # Validation
    validation_status: str              # "pass" | "fail" | "needs_clarification"
    validation_errors: list[str]
    retry_count: int
    max_retries: int                    # = 3

    # Visualization
    chart_spec: dict
    chart_type: str

    # Output
    explanation: str
    user_trace: list[str]               # SAFE for user display (no schema details)
    debug_trace: list[str]              # FULL trace (admin/dev only)
    error_message: str | None
```

**LangGraph graph (`agent/graph.py`):**

```
schema_retriever
      ↓
intent_mapper  ──── needs_clarification ──→ [return clarification question]
      ↓
sql_builder  ──── semantic_build_failed ──→ llm_sql_fallback
      ↓                                           ↓
sql_safety_check ←─────────────────────────────────
      ↓ passed
executor
      ↓
validator ──── fail + retry_count < 3 ──→ [back to sql_builder with error context]
      ↓ pass
chart_selector
      ↓
explainer
      ↓
[compose response]
```

Wire all nodes. Every node is stubbed to pass state through unchanged. Confirm the graph compiles and runs.

**Success criterion:** `graph.invoke({"user_question": "test", "conversation_history": []})` completes without exceptions through all nodes.

---

## Micro-Goal 1.6 — SemanticIntent Model and Intent Mapper Node (Day 3)

**`models/semantic_intent.py`:**

```python
from pydantic import BaseModel, Field
from enum import Enum

class TimeGranularity(str, Enum):
    day = "day"
    week = "week"
    month = "month"
    quarter = "quarter"
    year = "year"

class ChartType(str, Enum):
    bar = "bar"
    line = "line"
    scatter = "scatter"
    pie = "pie"
    table = "table"

class FilterCondition(BaseModel):
    dimension_name: str     # must be a valid dimension from the semantic layer
    operator: str           # eq, neq, gt, lt, gte, lte, in, not_in, contains
    value: Any

class SemanticIntent(BaseModel):
    intent_summary: str = Field(description="One sentence: what is the user asking for")
    metrics: list[str] = Field(description="List of metric names from the semantic layer")
    dimensions: list[str] = Field(description="List of dimension names to group by")
    time_dimension_name: str | None = Field(description="Time dimension name from semantic layer")
    time_granularity: TimeGranularity | None
    time_range: str | None = Field(description="e.g. 'last_12_months', '2024', 'Q1_2024'")
    filters: list[FilterCondition] = Field(default_factory=list)
    limit: int = Field(default=50, le=500)
    top_n: int | None = Field(description="If user asks 'top 10', set this to 10")
    order_by_metric: str | None
    order_direction: str = "desc"
    chart_type: ChartType
    confidence: float = Field(ge=0.0, le=1.0)
    needs_clarification: bool = False
    clarification_question: str | None = None
    use_llm_sql_fallback: bool = Field(
        default=False,
        description="Set True only if the question cannot be expressed via semantic layer"
    )
```

**The intent mapper node system prompt (critical — iterate on this):**

```
You are an intent classifier for a business analytics tool.

Available metrics:
{semantic_context.metrics}

Available dimensions:
{semantic_context.dimensions}

Available time dimensions:
{semantic_context.time_dimensions}

The user asks: "{user_question}"

Your job:
1. Map their question to the EXACT metric and dimension names listed above. Do not invent names.
2. Identify any filters (e.g., "only in 2023", "excluding cancelled orders").
3. Identify time range and granularity.
4. Choose the most appropriate chart type.
5. If the question cannot be answered using the metrics and dimensions above, set use_llm_sql_fallback=true.
6. If the question is ambiguous and you cannot determine a single correct mapping, set needs_clarification=true and write a clarifying question.

Output only a valid JSON object matching the SemanticIntent schema.
```

**Use Instructor to enforce Pydantic output.** Temperature = 0.

**Success criterion:** "total revenue by product category last year" → `SemanticIntent(metrics=["total_revenue"], dimensions=["product_category"], time_range="last_year", chart_type="bar", confidence=0.95)`. All field values are exact names from the semantic YAML.

---

## Micro-Goal 1.7 — Deterministic SQL Builder (Day 4)

**`semantic/sql_builder.py` — This is the compiler:**

```python
from .loader import SemanticRegistry
from ..models.semantic_intent import SemanticIntent
from datetime import datetime, timedelta

class SQLBuilder:
    def __init__(self, registry: SemanticRegistry):
        self.registry = registry

    def build(self, intent: SemanticIntent, connector_type: str = "duckdb") -> str:
        """
        Deterministically builds SQL from a SemanticIntent.
        Uses the semantic registry to resolve metric expressions and join paths.
        Never calls an LLM.
        """
        # 1. Resolve metrics
        metrics = [self.registry.get_metric(m) for m in intent.metrics]

        # 2. Resolve dimensions
        dimensions = [self.registry.get_dimension(d) for d in intent.dimensions]

        # 3. Collect all required tables
        required_tables = set()
        for m in metrics:
            required_tables.update(m.required_tables)
        for d in dimensions:
            required_tables.update(d.required_tables)

        # 4. Build FROM + JOIN clauses using join graph
        from_clause, join_clauses = self._build_joins(required_tables)

        # 5. Build SELECT clause
        select_parts = []
        for d in dimensions:
            select_parts.append(f"{d.sql_expression} AS {d.name}")
        for m in metrics:
            if m.aggregation == "RATIO":
                select_parts.append(
                    f"ROUND(100.0 * {m.numerator_sql} / NULLIF({m.denominator_sql}, 0), 2) AS {m.name}"
                )
            else:
                select_parts.append(f"{m.aggregation}({m.sql_expression}) AS {m.name}")

        # 6. Build WHERE clause (base filters + user filters + time filter)
        where_parts = []
        for m in metrics:
            if m.base_filter:
                where_parts.append(f"({m.base_filter})")
        if intent.time_dimension_name and intent.time_range:
            td = self.registry.get_time_dimension(intent.time_dimension_name)
            where_parts.append(self._build_time_filter(td, intent.time_range, connector_type))
        for f in intent.filters:
            d = self.registry.get_dimension(f.dimension_name)
            where_parts.append(self._build_filter(d, f))

        # 7. GROUP BY
        group_by = [d.sql_expression for d in dimensions]

        # 8. ORDER BY
        if intent.order_by_metric:
            order_by = f"{intent.order_by_metric} {intent.order_direction.upper()}"
        elif dimensions:
            order_by = f"{metrics[0].name} DESC"
        else:
            order_by = None

        # 9. Assemble
        sql = f"SELECT {', '.join(select_parts)}\n"
        sql += f"FROM {from_clause}\n"
        for j in join_clauses:
            sql += f"{j}\n"
        if where_parts:
            sql += f"WHERE {' AND '.join(where_parts)}\n"
        if group_by:
            sql += f"GROUP BY {', '.join(group_by)}\n"
        if order_by:
            sql += f"ORDER BY {order_by}\n"
        sql += f"LIMIT {intent.limit}"

        return sql

    def _build_time_filter(self, time_dim, time_range: str, dialect: str) -> str:
        """Converts time_range strings to SQL predicates."""
        col = time_dim.sql_expression
        now = datetime.now()

        time_range_map = {
            "last_7_days": f"{col} >= CURRENT_DATE - INTERVAL 7 DAY",
            "last_30_days": f"{col} >= CURRENT_DATE - INTERVAL 30 DAY",
            "last_3_months": f"{col} >= CURRENT_DATE - INTERVAL 3 MONTH",
            "last_6_months": f"{col} >= CURRENT_DATE - INTERVAL 6 MONTH",
            "last_12_months": f"{col} >= CURRENT_DATE - INTERVAL 12 MONTH",
            "last_year": f"YEAR({col}) = {now.year - 1}",
            "this_year": f"YEAR({col}) = {now.year}",
            "2024": f"YEAR({col}) = 2024",
            "2023": f"YEAR({col}) = 2023",
            # Add more patterns as you discover them in testing
        }
        return time_range_map.get(time_range, f"YEAR({col}) = {now.year}")
```

**LLM SQL Fallback:** For intents where `use_llm_sql_fallback=True`, fall through to a constrained LLM call that must use only the tables in the allowlist and must pass the SQL safety validator. Log `sql_build_method = "llm_fallback"` in state and in the debug trace.

**Success criterion:** Build SQL for 15 different SemanticIntents without ever calling the LLM. Each SQL statement is syntactically correct, joins the right tables, and uses the right metric expressions.

---

## Micro-Goal 1.8 — Executor Node (Day 4)

```python
async def executor_node(state: AgentState) -> AgentState:
    sql = state["generated_sql"]

    # Safety check is mandatory — ALWAYS runs before execution
    validator = SQLSafetyValidator()
    try:
        clean_sql = validator.validate(sql)
    except SQLSafetyError as e:
        state["execution_error"] = f"Safety check failed: {e}"
        state["validation_status"] = "fail"
        state["debug_trace"].append(f"SAFETY BLOCK: {e}")
        state["user_trace"].append("⚠ Query blocked by safety check. Retrying...")
        return state

    state["sql_safety_passed"] = True

    # Execute with timeout and row cap
    start = time.time()
    try:
        df = connector.execute_query(clean_sql, limit=5000)
        state["result_df"] = df
        state["row_count"] = len(df)
        state["execution_time_ms"] = (time.time() - start) * 1000
        state["user_trace"].append(f"✓ Query executed. {len(df)} rows in {state['execution_time_ms']:.0f}ms")
    except Exception as e:
        state["execution_error"] = str(e)
        state["debug_trace"].append(f"EXECUTION ERROR: {e}\nSQL: {clean_sql}")
        state["user_trace"].append(f"✗ Query failed. Retrying...")

    return state
```

---

## Micro-Goal 1.9 — Two-Level Agent Trace (Day 4) ← NEW

Every event in the pipeline is logged to **both** traces, with different content:

```python
def log_event(state: AgentState, user_message: str, debug_message: str):
    """
    user_message: safe for display. No schema names, column names, or values.
    debug_message: full detail for developers.
    """
    state["user_trace"].append(user_message)
    state["debug_trace"].append(debug_message)

# Example usage in intent_mapper node:
log_event(
    state,
    user_message="✓ Understood your question. Looking up revenue by product category.",
    debug_message=f"SemanticIntent: metrics=['total_revenue'], dimensions=['product_category'], "
                  f"time_range='last_12_months', confidence=0.95"
)

# Example usage in executor node after SQL error:
log_event(
    state,
    user_message="✗ Query encountered an issue. Attempting to fix and retry...",
    debug_message=f"SQL ERROR: {error_message}\nFailed SQL:\n{sql}"
)
```

**In the API response:**
```python
class ChatResponse(BaseModel):
    chart_spec: dict
    table_data: list[dict]
    explanation: str
    sql_used: str                    # always shown
    user_trace: list[str]            # always shown in UI
    debug_trace: list[str] | None    # only included if request has debug=true header
    semantic_intent: dict            # always shown ("how it was interpreted")
```

**In the frontend:** The `AgentTrace` component displays `user_trace`. An "Admin / Debug" toggle (hidden behind a query param or env var) swaps to `debug_trace`. For the demo, you'll show the debug trace — it's more impressive. For external users, only the user trace is shown.

---

## Micro-Goal 1.10 — Validator Node + Self-Correction Loop (Day 5)

The validator runs after every execution. Checks in priority order:

**Check 1 — Execution error?**
If `execution_error` is set, pass the error back to `sql_builder` with the message. If `sql_build_method == "semantic"`, the semantic builder retries with a corrected interpretation. If `sql_build_method == "llm_fallback"`, the LLM gets the error message and retries.

**Check 2 — Empty result?**
Zero rows returned. Likely causes: time filter too narrow, filter value wrong, metric base_filter too restrictive. Retry with relaxed time range (expand by one period). Log the adjustment.

**Check 3 — Cardinality explosion?**
More than 500 rows for a chart type (not table). Automatically apply `TOP {intent.limit}` logic. Switch `chart_type` to "table" if more than 5 distinct dimension values exist for a pie chart.

**Check 4 — Shape mismatch?**
Scatter chart requires exactly 2 numeric columns. Line chart requires a time column. Bar chart requires at least 1 categorical and 1 numeric. If shape doesn't match the chart type, override `chart_type` to "table" and log the override.

**Check 5 — Semantic sanity?**
Revenue column contains negative values? Date column contains future dates? Log a warning in the user trace: "⚠ Note: some values appear unusual. Please verify the result."

**Retry routing:**
```
retry_count < max_retries (3):
    → increment retry_count
    → add error context to state
    → route back to sql_builder

retry_count >= max_retries:
    → set validation_status = "fail"
    → set graceful error_message
    → route to explainer (which explains the failure, not a chart)
```

**Success criterion:** Deliberately corrupt the semantic intent with a bad time range. Watch the validator detect empty results, relax the time range, retry, and succeed. This is your live demo centerpiece.

---

## Micro-Goal 1.11 — Chart Selector and Explainer Nodes (Day 6)

**Chart Selector:**
Five Plotly templates as pure functions:
```python
def bar_chart(df, x_col, y_col, title) -> dict:
    fig = px.bar(df, x=x_col, y=y_col, title=title)
    return json.loads(fig.to_json())

def line_chart(df, x_col, y_col, title, color_col=None) -> dict: ...
def scatter_chart(df, x_col, y_col, title, size_col=None) -> dict: ...
def pie_chart(df, names_col, values_col, title) -> dict: ...
def data_table(df, title) -> dict: ...
```

The selector maps `semantic_intent.chart_type` → template function → parameters derived from DataFrame column names. **The LLM never writes Plotly code.**

**Explainer:**
One LLM call. Input: question, SemanticIntent, row count, execution time. Output:

```
Sentence 1: What was computed and how.
Sentence 2: Filters, grouping, and time range applied.
Sentence 3: One notable observation from the data.
```

Example: "I calculated total revenue grouped by product category for orders placed in 2018. Results are filtered to exclude cancelled and unavailable orders, and are ranked from highest to lowest revenue. The 'bed_bath_table' category leads with R$1.7M, more than twice the second-highest category."

---

## Micro-Goal 1.12 — FastAPI Endpoints (Day 7)

```python
@app.post("/chat")
async def chat(request: ChatRequest) -> ChatResponse:
    # ChatRequest: { question: str, conversation_history: list, debug: bool = False }
    state = initialize_state(request)
    final_state = await graph.ainvoke(state)
    return compose_response(final_state, include_debug=request.debug)

@app.get("/schema")
async def get_schema() -> SchemaResponse:
    # Returns: available metrics, dimensions, time dimensions (display names only)
    # Used by frontend to show available options to users

@app.post("/feedback")
async def submit_feedback(feedback: FeedbackRequest):
    # Logs: question, rating (positive/negative), comment, semantic_intent
    # Writes to JSONL file for MVP (PostgreSQL in Phase 3)
    pass

@app.get("/health")
async def health():
    return {"status": "ok", "connector": connector.get_connector_type()}
```

---

## Micro-Goal 1.13 — Next.js Frontend (Days 8–9)

**Five panels:**

1. **Chat Panel** — question input, conversation history, 6 example question chips.
2. **Chart Panel** — Plotly React component. Renders from JSON spec. Loading skeleton while agent runs.
3. **Table Panel** — always present below the chart. Shows the raw data behind the chart.
4. **SQL Viewer** — collapsible. Shows the exact SQL that ran. Labeled "View Query."
5. **Agent Trace Panel** — collapsible. Shows `user_trace` steps. Labeled "View Reasoning." This is the differentiator — most tools hide this. You show it.

**Example question chips (pick questions that demonstrate range):**
- "Total revenue by product category"
- "Monthly order volume in 2018"
- "Top 10 sellers by revenue"
- "Average delivery time by customer state"
- "Order cancellation rate by month"
- "Revenue vs average review score by category"

**Prompt injection warning (frontend):** If user input contains `ignore previous instructions` or similar patterns, display a user-friendly message: "That looks like a system instruction, not a data question. Try asking about your data instead." This is a lightweight client-side check — the real defense is in the intent mapper's constrained output schema.

---

## Micro-Goal 1.14 — Golden Tests and Documentation (Day 10)

**`tests/golden_tests.py`:**
```python
import os
os.environ["ANTHROPIC_MODEL"] = "claude-sonnet-4-20250514"  # pinned model
# All LLM calls use temperature=0

GOLDEN_TESTS = [
    {
        "question": "total revenue by product category",
        "expected_metrics": ["total_revenue"],
        "expected_dimensions": ["product_category"],
        "expected_chart_type": "bar",
        "should_return_rows": True,
        "min_rows": 5,
    },
    {
        "question": "how many orders were placed each month in 2018",
        "expected_metrics": ["order_count"],
        "expected_dimensions": [],
        "expected_time_granularity": "month",
        "expected_chart_type": "line",
        "should_return_rows": True,
    },
    # ... 18 more covering all metrics, dimensions, time ranges, filters, edge cases
]

# Accuracy gate: fail CI if < 80% pass
# Run each test once at temperature=0 (deterministic enough for gating)
# Run nightly at temperature=0.3 for 3 iterations each (statistical accuracy eval)
```

**`ARCHITECTURE.md`** must cover:
- Why deterministic SQL compilation over LLM SQL generation (with the accuracy argument)
- Why a semantic layer (16% vs 80%+ accuracy statistic)
- How the self-correction loop works
- The two-level trace design and why
- SQL safety model design
- Connector abstraction and expansion path
- Papers cited: MatPlotAgent, PlotGen, SQL-of-Thought

**Phase 1 Exit Criteria:**
- `docker compose up` works in one command
- Full pipeline runs: NL → intent → SQL → execute → validate → chart → explain
- SQL safety model tested and working
- Self-correction loop demonstrable live
- Two-level trace visible in UI
- 20 golden tests pass at 80%+
- README + ARCHITECTURE.md complete

---

# PHASE 2: AWS Deployment — ECS + ALB Direct (Weeks 3–4)
### Goal: Public HTTPS URL. Real deployment. CI/CD established.

**Architecture correction from v1:** There is no EC2 stepping stone. You go directly to ECS Fargate + ALB. This is not more complex — it's actually cleaner because ALB handles TLS termination natively with ACM, eliminating the fake "ACM on EC2" problem entirely.

---

## Micro-Goal 2.1 — Terraform Bootstrap (Day 11)

**Create `infrastructure/terraform/bootstrap/main.tf`:**

This runs once, manually, before anything else. It creates the infrastructure that Terraform itself needs.

```hcl
# S3 bucket for Terraform state
resource "aws_s3_bucket" "terraform_state" {
  bucket = "${var.project}-terraform-state-${var.account_id}"
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.terraform_state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.terraform_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

# DynamoDB table for state locking ← NEW (prevents concurrent Terraform applies)
resource "aws_dynamodb_table" "terraform_locks" {
  name         = "${var.project}-terraform-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  attribute {
    name = "LockID"
    type = "S"
  }
}
```

**Run bootstrap manually:**
```bash
cd infrastructure/terraform/bootstrap
terraform init
terraform apply
```

**Then all other Terraform environments reference this backend:**
```hcl
terraform {
  backend "s3" {
    bucket         = "nl-query-tool-terraform-state-{account_id}"
    key            = "environments/dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "nl-query-tool-terraform-locks"   # ← state locking
    encrypt        = true
  }
}
```

---

## Micro-Goal 2.2 — Networking Module (Day 11–12)

```hcl
# modules/networking/main.tf

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
}

# 2 public subnets (ALB lives here)
resource "aws_subnet" "public" {
  count                   = 2
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false   # ALB has public IP, subnets don't need to assign it
}

# 2 private subnets (ECS tasks, RDS, Redis — never directly reachable from internet)
resource "aws_subnet" "private" {
  count             = 2
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = data.aws_availability_zones.available.names[count.index]
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

# NAT Gateway: lets ECS tasks in private subnets reach internet (for Claude API calls)
resource "aws_eip" "nat" { count = 1 }
resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
}

# Route tables
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
}

# Security Groups
resource "aws_security_group" "alb" {
  name   = "${var.project}-alb-sg"
  vpc_id = aws_vpc.main.id
  ingress { from_port = 443, to_port = 443, protocol = "tcp", cidr_blocks = ["0.0.0.0/0"] }
  ingress { from_port = 80,  to_port = 80,  protocol = "tcp", cidr_blocks = ["0.0.0.0/0"] }
  egress  { from_port = 0,   to_port = 0,   protocol = "-1",  cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "backend_ecs" {
  name   = "${var.project}-backend-sg"
  vpc_id = aws_vpc.main.id
  # Only accepts traffic from ALB
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress { from_port = 0, to_port = 0, protocol = "-1", cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "frontend_ecs" {
  name   = "${var.project}-frontend-sg"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 3000
    to_port         = 3000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress { from_port = 0, to_port = 0, protocol = "-1", cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "rds" {
  name   = "${var.project}-rds-sg"
  vpc_id = aws_vpc.main.id
  # Only accepts traffic from backend ECS tasks
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.backend_ecs.id]
  }
}

resource "aws_security_group" "redis" {
  name   = "${var.project}-redis-sg"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.backend_ecs.id]
  }
}
```

---

## Micro-Goal 2.3 — ALB Module with ACM (the HTTPS fix) (Day 12)

```hcl
# modules/alb/main.tf

# ACM certificate for your domain
resource "aws_acm_certificate" "main" {
  domain_name               = var.domain_name          # "app.yourdomain.com"
  subject_alternative_names = ["api.${var.domain_name}"]
  validation_method         = "DNS"
  lifecycle { create_before_destroy = true }
}

# DNS validation record in Route 53
resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.main.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }
  zone_id = var.route53_zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.record]
  ttl     = 60
}

resource "aws_acm_certificate_validation" "main" {
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# Application Load Balancer
resource "aws_lb" "main" {
  name               = "${var.project}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [var.alb_sg_id]
  subnets            = var.public_subnet_ids   # ALB must be in public subnets
}

# HTTPS listener (port 443) — TLS terminates HERE at the ALB
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.main.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = var.frontend_target_group_arn
  }
}

# HTTP listener redirects to HTTPS
resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# Path-based routing: /api/* → backend, /* → frontend
resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10
  condition {
    path_pattern { values = ["/api/*"] }
  }
  action {
    type             = "forward"
    target_group_arn = var.backend_target_group_arn
  }
}
```

This is how ACM works in AWS: the certificate is attached to the ALB listener. Traffic from the internet hits the ALB over HTTPS. The ALB decrypts it and forwards plain HTTP to your ECS containers on their internal ports. Your containers never handle TLS.

---

## Micro-Goal 2.4 — ECR Repositories (Day 12)

```hcl
# modules/ecr/main.tf
resource "aws_ecr_repository" "backend" {
  name                 = "${var.project}/backend"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_repository" "frontend" {
  name                 = "${var.project}/frontend"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

# Lifecycle policy: keep only last 10 images
resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
```

---

## Micro-Goal 2.5 — ECS Fargate Services (Day 13)

```hcl
# modules/ecs/main.tf

resource "aws_ecs_cluster" "main" {
  name = "${var.project}-cluster"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# IAM roles
resource "aws_iam_role" "ecs_task_execution" {
  name = "${var.project}-ecs-task-execution"
  # Allows ECS to: pull images from ECR, write logs to CloudWatch, read secrets from Secrets Manager
  managed_policy_arns = [
    "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
  ]
}

resource "aws_iam_role" "ecs_task" {
  name = "${var.project}-ecs-task"
  # Allows the running container to: put CloudWatch metrics, write X-Ray traces
  # Explicitly does NOT include S3 or Secrets Manager access at task level
  # (Secrets Manager access is via task execution role injection at startup)
}

# Backend task definition
resource "aws_ecs_task_definition" "backend" {
  family                   = "${var.project}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024     # 1 vCPU
  memory                   = 2048     # 2 GB
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name  = "backend"
    image = "${var.ecr_backend_url}:${var.image_tag}"
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    # Secrets injected at container startup — never in env vars
    secrets = [
      { name = "ANTHROPIC_API_KEY", valueFrom = "${var.anthropic_secret_arn}:api_key::" },
      { name = "DATABASE_URL",      valueFrom = "${var.db_secret_arn}:connection_string::" }
    ]

    environment = [
      { name = "ENVIRONMENT",  value = var.environment },
      { name = "LOG_LEVEL",    value = "INFO" },
      { name = "CONNECTOR_TYPE", value = "duckdb" }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/${var.project}/backend"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])
}

resource "aws_ecs_service" "backend" {
  name            = "${var.project}-backend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = 2         # 2 tasks for availability
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids   # tasks in private subnets
    security_groups  = [var.backend_sg_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.backend.arn
    container_name   = "backend"
    container_port   = 8000
  }

  # Rolling deployment: new tasks start before old ones stop
  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200
}

# Auto-scaling
resource "aws_appautoscaling_target" "backend" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.backend.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = 1
  max_capacity       = 4
}

resource "aws_appautoscaling_policy" "backend_cpu" {
  name               = "${var.project}-backend-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = "ecs"
  resource_id        = aws_appautoscaling_target.backend.resource_id
  scalable_dimension = aws_appautoscaling_target.backend.scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}
```

---

## Micro-Goal 2.6 — GitHub Actions CI/CD (Day 13–14)

```yaml
# .github/workflows/deploy.yml
name: Test and Deploy

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

env:
  AWS_REGION: us-east-1
  ANTHROPIC_MODEL: claude-sonnet-4-20250514   # pinned model for tests

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v4
        with: { python-version: "3.11" }

      - name: Install dependencies
        run: pip install -r backend/requirements.txt

      - name: Run SQL safety tests
        run: pytest backend/tests/test_sql_safety.py -v
        # These never call the LLM — always fast

      - name: Run golden tests
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          TEMPERATURE: "0"    # deterministic
        run: |
          pytest backend/tests/golden_tests.py -v --tb=short
          # Script exits non-zero if accuracy < 80%

  deploy:
    needs: test
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id:     ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region:            ${{ env.AWS_REGION }}

      - name: Login to ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build and push backend image
        run: |
          docker build -t $ECR_BACKEND_URL:${{ github.sha }} ./backend
          docker push $ECR_BACKEND_URL:${{ github.sha }}
          docker tag $ECR_BACKEND_URL:${{ github.sha }} $ECR_BACKEND_URL:latest
          docker push $ECR_BACKEND_URL:latest

      - name: Build and push frontend image
        run: |
          docker build -t $ECR_FRONTEND_URL:${{ github.sha }} ./frontend
          docker push $ECR_FRONTEND_URL:${{ github.sha }}

      - name: Deploy to ECS
        run: |
          # Update backend service to use new image
          aws ecs update-service \
            --cluster nl-query-tool-cluster \
            --service nl-query-tool-backend \
            --force-new-deployment
          # Wait for deployment to stabilize
          aws ecs wait services-stable \
            --cluster nl-query-tool-cluster \
            --services nl-query-tool-backend

      - name: Smoke test production
        run: |
          curl -f https://api.yourdomain.com/health
          # Run 3 critical golden tests against production endpoint
```

**Phase 2 Exit Criteria:**
- `terraform apply` in `environments/dev` creates full infrastructure (VPC, ALB, ECS, Secrets Manager)
- App is live at `https://app.yourdomain.com` with valid TLS certificate
- API is live at `https://app.yourdomain.com/api`
- GitHub Actions deploys on push to main
- Deployment fails if golden tests fail

---

# PHASE 3: Production Architecture (Month 2)
### Goal: Full managed services. Observable. Resilient. Multi-service.

This phase adds the managed AWS services that replace stateful components currently handled in-container: RDS for app data, Redis for LangGraph state, S3 for file uploads.

---

## Micro-Goal 3.1 — RDS PostgreSQL (Secrets Manager wired)

```hcl
resource "aws_db_instance" "main" {
  identifier        = "${var.project}-postgres"
  engine            = "postgres"
  engine_version    = "15.4"
  instance_class    = "db.t3.small"
  allocated_storage = 20
  storage_encrypted = true

  db_name  = "nlquerytool"
  username = "appuser"
  password = random_password.db.result

  vpc_security_group_ids = [var.rds_sg_id]
  db_subnet_group_name   = aws_db_subnet_group.main.name

  backup_retention_period      = 7
  deletion_protection          = true
  skip_final_snapshot          = false
  performance_insights_enabled = true
  multi_az                     = false   # single-AZ for cost; enable for production
}

# Store full connection info in Secrets Manager
resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    host     = aws_db_instance.main.address
    port     = 5432
    dbname   = "nlquerytool"
    username = "appuser"
    password = random_password.db.result
    connection_string = "postgresql://appuser:${random_password.db.result}@${aws_db_instance.main.address}:5432/nlquerytool"
  })
}
```

**RDS schema (app metadata only — no customer data):**
```sql
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE query_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    question TEXT NOT NULL,
    semantic_intent JSONB,
    generated_sql TEXT,
    sql_build_method TEXT,        -- 'semantic' or 'llm_fallback'
    row_count INTEGER,
    execution_time_ms FLOAT,
    validation_status TEXT,
    retry_count INTEGER,
    chart_type TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_history_id UUID REFERENCES query_history(id),
    rating TEXT CHECK (rating IN ('positive', 'negative')),
    comment TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Micro-Goal 3.2 — ElastiCache Redis (LangGraph State)

```hcl
resource "aws_elasticache_replication_group" "main" {
  replication_group_id       = "${var.project}-redis"
  description                = "LangGraph checkpoint store and session cache"
  node_type                  = "cache.t3.micro"
  port                       = 6379
  num_cache_clusters         = 1      # upgrade to 2 for production
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [var.redis_sg_id]
}
```

**One-line code change in your backend:**
```python
# Phase 1 (in-memory, lost on container restart):
from langgraph.checkpoint.memory import InMemorySaver
checkpointer = InMemorySaver()

# Phase 3 (persistent, shared across containers):
from langgraph.checkpoint.redis import RedisSaver
checkpointer = RedisSaver.from_conn_string(os.environ["REDIS_URL"])
```

This is the architectural decision that enables conversation memory across sessions. A user can ask a question, close the browser, come back the next day, and the agent remembers the context.

---

## Micro-Goal 3.3 — S3 for CSV/Parquet Uploads

```hcl
resource "aws_s3_bucket" "datasets" {
  bucket = "${var.project}-datasets-${var.account_id}"
}

resource "aws_s3_bucket_versioning" "datasets" {
  bucket = aws_s3_bucket.datasets.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "datasets" {
  bucket = aws_s3_bucket.datasets.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
  }
}

# Lifecycle: delete uploaded files after 90 days (cost control)
resource "aws_s3_bucket_lifecycle_configuration" "datasets" {
  bucket = aws_s3_bucket.datasets.id
  rule {
    id     = "expire-uploads"
    status = "Enabled"
    expiration { days = 90 }
  }
}
```

**DuckDB connector upgrade for S3:**
```python
# Upload endpoint: user uploads CSV → S3
# DuckDB reads directly from S3 (no download needed):
conn.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-east-1';")
df = conn.execute(f"SELECT * FROM read_csv_auto('s3://{bucket}/{key}')").df()

# For Parquet (more efficient for large files):
df = conn.execute(f"SELECT * FROM read_parquet('s3://{bucket}/{key}')").df()
```

**Semantic YAML auto-generation:** When a user uploads a CSV, generate a draft semantic YAML by introspecting the schema, inferring numeric columns as potential metrics and string/categorical columns as potential dimensions. Present this to the user for review. This is the "who writes the YAML" problem partially solved for the CSV use case.

---

## Micro-Goal 3.4 — Observability Module

```hcl
# modules/monitoring/main.tf

# CloudWatch Log Groups
resource "aws_cloudwatch_log_group" "backend" {
  name              = "/ecs/${var.project}/backend"
  retention_in_days = 30
}

# Custom metric namespace: "NLQueryTool"
# Metrics emitted by application code:
# - QuerySuccess (count: 1/0)
# - ValidatorRetryCount (count)
# - PipelineLatencyMs (milliseconds)
# - SQLBuildMethod (semantic vs llm_fallback)
# - IntentMappingConfidence (float)

# Dashboard
resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.project}-operations"
  dashboard_body = jsonencode({
    widgets = [
      # Row 1: Health
      { type = "metric", properties = {
        title = "Query Success Rate (%)"
        metrics = [["NLQueryTool", "QuerySuccess"]]
        stat = "Average", period = 300
      }},
      { type = "metric", properties = {
        title = "P95 Pipeline Latency (ms)"
        metrics = [["NLQueryTool", "PipelineLatencyMs"]]
        stat = "p95", period = 300
      }},
      # Row 2: Agent behavior
      { type = "metric", properties = {
        title = "Validator Retries per Query"
        metrics = [["NLQueryTool", "ValidatorRetryCount"]]
        stat = "Average"
      }},
      { type = "metric", properties = {
        title = "SQL Build Method (Semantic vs LLM Fallback)"
        metrics = [
          ["NLQueryTool", "SemanticBuildCount"],
          ["NLQueryTool", "LLMFallbackCount"]
        ]
      }}
    ]
  })
}

# Alerts
resource "aws_cloudwatch_metric_alarm" "high_error_rate" {
  alarm_name          = "${var.project}-high-error-rate"
  metric_name         = "QuerySuccess"
  namespace           = "NLQueryTool"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 0.7    # alert if success rate drops below 70%
  comparison_operator = "LessThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
```

**In your FastAPI application:**
```python
import boto3

cloudwatch = boto3.client("cloudwatch", region_name=os.environ["AWS_REGION"])

def emit_pipeline_metrics(state: AgentState):
    metrics = [
        {"MetricName": "QuerySuccess",
         "Value": 1.0 if state["validation_status"] == "pass" else 0.0,
         "Unit": "Count"},
        {"MetricName": "ValidatorRetryCount",
         "Value": float(state["retry_count"]),
         "Unit": "Count"},
        {"MetricName": "PipelineLatencyMs",
         "Value": state["execution_time_ms"],
         "Unit": "Milliseconds"},
        {"MetricName": "SemanticBuildCount" if state["sql_build_method"] == "semantic"
                        else "LLMFallbackCount",
         "Value": 1.0, "Unit": "Count"},
    ]
    cloudwatch.put_metric_data(Namespace="NLQueryTool", MetricData=metrics)
```

**Phase 3 Exit Criteria:**
- RDS storing real query history — you can query it and see actual usage patterns
- Redis enabling cross-session conversation memory
- S3 accepting CSV uploads — users can bring their own data
- CloudWatch dashboard showing live pipeline metrics
- Alerts configured and tested

---

# PHASE 4: Enterprise Features (Month 3+)
### Goal: Multi-tenant, auth, warehouse integrations, enterprise data access

---

## Micro-Goal 4.1 — Cognito Authentication

```hcl
resource "aws_cognito_user_pool" "main" {
  name = "${var.project}-users"

  password_policy {
    minimum_length    = 12
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"

  # Token validity
  # (configured in user pool client)
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "web-app"
  user_pool_id = aws_cognito_user_pool.main.id

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = ["https://${var.domain_name}/auth/callback"]
  logout_urls                          = ["https://${var.domain_name}"]
  supported_identity_providers         = ["COGNITO"]
  allowed_oauth_flows_user_pool_client = true

  access_token_validity  = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    refresh_token = "days"
  }
}

# Enterprise SSO — add identity providers when enterprise customers request it
resource "aws_cognito_identity_provider" "okta" {
  count         = var.enable_okta_sso ? 1 : 0
  user_pool_id  = aws_cognito_user_pool.main.id
  provider_name = "Okta"
  provider_type = "SAML"
  provider_details = {
    MetadataURL = var.okta_metadata_url
  }
}
```

**FastAPI auth middleware:**
```python
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer
import jwt

security = HTTPBearer()

async def get_current_user(token = Depends(security)) -> dict:
    try:
        # Verify JWT against Cognito JWKS
        payload = jwt.decode(
            token.credentials,
            options={"verify_signature": True},
            algorithms=["RS256"],
            audience=COGNITO_APP_CLIENT_ID,
        )
        return {"user_id": payload["sub"], "email": payload["email"]}
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

# Applied to all routes:
@app.post("/chat")
async def chat(request: ChatRequest, user = Depends(get_current_user)):
    ...
```

---

## Micro-Goal 4.2 — Multi-Tenancy Pattern

Given your answers (CSV for now, external users later, SOC2 out of scope), the right pattern is **pool model with tenant_id row isolation**:

```sql
-- Add tenant_id to all app tables
ALTER TABLE query_history ADD COLUMN tenant_id UUID NOT NULL;
ALTER TABLE feedback ADD COLUMN tenant_id UUID NOT NULL;

-- Row-level security (enforced in app code AND at DB level)
CREATE POLICY tenant_isolation ON query_history
    USING (tenant_id = current_setting('app.tenant_id')::UUID);

ALTER TABLE query_history ENABLE ROW LEVEL SECURITY;
```

**Application-level enforcement:**
```python
async def get_connector_for_request(user: dict, db: Database) -> DataConnector:
    tenant_id = user["tenant_id"]  # from JWT claims

    # Get tenant's connector config
    config = await db.fetch_one(
        "SELECT connector_type, secret_name FROM tenant_connectors WHERE tenant_id = $1",
        tenant_id
    )
    if not config:
        # Default: DuckDB with tenant's uploaded files
        return DuckDBConnector(tenant_id=tenant_id)

    # Get credentials from Secrets Manager
    secret = secrets_client.get_secret_value(
        SecretId=f"{PROJECT_NAME}/tenant/{tenant_id}/warehouse"
    )
    creds = json.loads(secret["SecretString"])

    ConnectorClass = CONNECTOR_REGISTRY[config["connector_type"]]
    return ConnectorClass(**creds)
```

---

## Micro-Goal 4.3 — Warehouse Connector Implementations

Implement the Phase 1 stubs:

```python
class RedshiftConnector(DataConnector):
    def __init__(self, host, port, database, username, password, schema="public"):
        import redshift_connector
        self.conn = redshift_connector.connect(
            host=host, port=port, database=database,
            user=username, password=password
        )
        self.schema = schema

    def get_schema(self) -> SchemaContext:
        # Query information_schema for tables/columns
        # Redshift-specific: also query SVV_TABLE_INFO for row counts
        pass

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        # Enforce LIMIT, run query, return DataFrame
        pass

class AthenaConnector(DataConnector):
    def __init__(self, region, database, s3_output_location, workgroup="primary"):
        self.athena = boto3.client("athena", region_name=region)
        # Athena is async: submit → poll → read from S3
        pass

class SnowflakeConnector(DataConnector):
    def __init__(self, account, warehouse, database, schema, username, password, role=None):
        import snowflake.connector
        self.conn = snowflake.connector.connect(...)
        pass
```

---

## Micro-Goal 4.4 — Cost Controls for Cloud Warehouses

```python
class QueryCostEstimator:
    async def estimate_athena_bytes(self, sql: str, database: str) -> int:
        # Submit EXPLAIN query to Athena (free, estimates bytes scanned)
        response = self.athena.start_query_execution(
            QueryString=f"EXPLAIN {sql}",
            QueryExecutionContext={"Database": database},
        )
        # Parse data scanned from execution statistics
        return bytes_scanned

    def athena_cost_usd(self, bytes_scanned: int) -> float:
        return (bytes_scanned / 1e12) * 5.0  # $5 per TB

# In the executor node, before executing on Athena:
estimated_cost = await estimator.estimate_athena_bytes(sql, database)
if estimated_cost > 1.0:  # $1 threshold
    state["needs_clarification"] = True
    state["clarification_question"] = (
        f"This query will scan approximately {bytes_scanned/1e9:.1f} GB "
        f"(estimated cost: ${estimated_cost:.2f}). Proceed?"
    )
    return state
```

---

# PHASE 5: Operational Maturity (Ongoing)
### Goal: The system is trustworthy, monitorable, and maintainable over time

---

## Micro-Goal 5.1 — Semantic Layer Versioning

```yaml
# ecommerce.yaml
version: "1.2"       # increment on any metric definition change
dataset: olist_ecommerce
changelog:
  - version: "1.0"
    date: "2025-01-15"
    changes: "Initial definitions"
  - version: "1.1"
    date: "2025-02-01"
    changes: "Added cancellation_rate metric, fixed total_revenue base filter"
  - version: "1.2"
    date: "2025-02-15"
    changes: "Added product_subcategory dimension"
```

Store `semantic_version` in every `query_history` row. If a metric definition changes and a user re-runs an old query, warn them: "Note: the definition of 'Total Revenue' changed in v1.1 — historical results may differ."

---

## Micro-Goal 5.2 — Nightly Accuracy Evaluation

Separate from the CI golden test gate (which runs at `temperature=0`), run a nightly job:

```yaml
# .github/workflows/nightly_eval.yml
on:
  schedule:
    - cron: "0 2 * * *"   # 2am UTC daily

jobs:
  eval:
    steps:
      - name: Run full golden tests at temperature 0.3, 3 iterations each
        run: |
          python backend/tests/golden_tests.py --temperature 0.3 --iterations 3
          # Output: accuracy per question (majority vote), overall accuracy
          # Writes results to eval_results/{date}.json
      - name: Post results to CloudWatch
        run: python scripts/post_eval_metrics.py
```

This gives you a statistical accuracy metric over time. You'll see if a model update or prompt change improved or degraded real-world performance. This is what "operating an AI system" looks like.

---

## Micro-Goal 5.3 — Disaster Recovery Documentation

Even for a portfolio project, write a one-page DR plan in `docs/DISASTER_RECOVERY.md`:

```markdown
## RTO and RPO

RTO (Recovery Time Objective): 2 hours
RPO (Recovery Point Objective): 24 hours (last automated backup)

## What is backed up

- RDS PostgreSQL: automated daily snapshots, 7-day retention
- S3 datasets: versioned, no deletion
- Semantic YAML files: version-controlled in git

## What is NOT backed up (and why it's okay)

- LangGraph Redis state: sessions can be re-run; no critical data
- CloudWatch logs: can be re-ingested from ECS if needed

## Recovery procedure

1. RDS failure: restore from latest snapshot (~15 min, RTO 30 min)
2. ECS service failure: ECS auto-heals; manual: `aws ecs update-service --force-new-deployment`
3. Full region failure: deploy in us-west-2 using same Terraform, restore RDS snapshot cross-region
```

---

## Honest Final Assessment of v2

### What This Plan Fixes

The architectural shift to deterministic SQL compilation is not a minor tweak. It changes the reliability profile of the entire system. On the happy path (a question expressible via the semantic layer), the LLM now does only one thing: classify intent. No hallucinated column names. No wrong joins. No SQL syntax errors from the LLM. The validator retry loop still exists for edge cases, but it fires far less often.

The SQL safety model with `sqlglot` AST parsing is lightweight (one pip install, ~50 lines of code) but closes a real security gap. Without it, a user could theoretically ask "show me all user passwords" and a naive SQL generator might try to comply. With it, that table isn't in the allowlist and the query never runs.

The two-level trace is a small change with large implications. The debug trace is your demo asset. The user trace is safe for production.

### What Is Still Hard

The semantic YAML is still the most fragile dependency. A poorly written metric definition (wrong SQL expression, missing join, incorrect base filter) produces wrong results silently — the pipeline succeeds, the chart renders, the data is wrong. Automated testing of semantic definitions (running each metric against known expected values) belongs in your test suite and is not yet in this plan.

Prompt injection is partially addressed (client-side check, constrained Pydantic output schema). A determined adversary could still craft inputs that confuse the intent mapper. Full prompt injection defense is a deeper problem that this plan acknowledges but does not fully solve at MVP scope.

### What To Say in Interviews

"I built a seven-node LangGraph agent for natural language analytics. The key architectural decision was separating LLM intent classification from SQL generation — the LLM maps questions to semantic concepts, and a deterministic compiler builds the SQL from governed metric definitions. This reduced reliance on LLM SQL generation, which is the primary source of inaccuracy in most NL-to-SQL systems. The infrastructure is Terraform-managed on AWS with ECS Fargate, ALB for TLS termination, RDS for query history, and ElastiCache for agent state persistence. The CI/CD pipeline gates deployment on a golden test accuracy threshold."

Every word of that is technically precise, defensible, and based on real architectural decisions with clear reasoning behind them.

---

*Plan v2 — February 2026*
*Changes from v1: Deterministic SQL compilation, SQL safety model, two-level trace, Phase 2 direct to ECS+ALB, DynamoDB Terraform locking, golden test determinism*
> Current implementation note: the section below is the long-form target architecture plan. The current repo state is summarized first so the document matches what is actually implemented today.
