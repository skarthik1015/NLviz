"""Development set (plan step 0b): 120 cases on three databases, written before running the app on
them. Reference SQL is written by hand against each database's own tables and run there by
build_cases.py, which writes cases/dev.jsonl with the results. Policies: policies_dev.md.

Categories (share of cases): ordinary, paraphrase, ambiguous, unsupported, follow_up. A paraphrase
shares its original's family. `grammar=False` marks shapes the intent grammar cannot express today
(several metrics, shares, top N per group, hour of day, MAX, column-to-column conditions).
"""
from __future__ import annotations


def m(metric: str, by: str | None = None, where: str | None = None, period: str | None = None,
      grain: str | None = None) -> dict:
    """The intended meaning, for people reading a case (the reference SQL is the precise one)."""
    return {k: v for k, v in {"metric": metric, "group_by": by, "filters": where, "period": period,
                              "grain": grain}.items() if v}


def turn(question: str, sql: str | None, meaning: dict, behavior: str = "answer", *, order: bool = False,
         clarify: tuple[dict, ...] = (), refusal: str | None = None, tolerance: float = 0.005) -> dict:
    return {"question": question, "meaning": meaning, "expected_behavior": behavior, "reference_sql": sql,
            "order_matters": order, "tolerance": tolerance, "acceptable_clarifications": list(clarify),
            **({"refusal_reason": refusal} if refusal else {})}


def case(cid: str, family: str, database: str, category: str, *turns: dict, grammar: bool = True) -> dict:
    return {"id": cid, "family": family, "database": database, "category": category,
            "grammar_supported": grammar, "turns": list(turns)}


def ask(about: str, answer: str, **match) -> dict:
    """An acceptable clarification: what it is about, the scripted answer, and (optionally) a machine
    rule matching the app's clarification (score._rule_holds) plus the reply to send."""
    entry = {"about": about, "scripted_answer": answer}
    if match:
        reply = match.pop("reply", None)
        entry["match"] = match
        if reply is not None:
            entry["reply"] = reply
    return entry


def refuse(cid: str, family: str, database: str, question: str, reason: str) -> dict:
    return case(cid, family, database, "unsupported", turn(question, None, m("none"), "refuse", refusal=reason))


# --------------------------------------------------------------------------------------------- tpch
REV = "SUM(l.l_extendedprice * (1 - l.l_discount))"
LO = "lineitem l JOIN orders o ON o.o_orderkey = l.l_orderkey"
LOC = LO + " JOIN customer c ON c.c_custkey = o.o_custkey"
LOCN = LOC + " JOIN nation n ON n.n_nationkey = c.c_nationkey"
LOCNR = LOCN + " JOIN region r ON r.r_regionkey = n.n_regionkey"


def year(column: str, y: int) -> str:
    return f"{column} >= '{y}-01-01' AND {column} < '{y + 1}-01-01'"


def pg_year(column: str) -> str:
    return f"to_char({column}, 'YYYY')"


def pg_quarter(column: str) -> str:
    return f"to_char({column}, 'YYYY-\"Q\"Q')"


def pg_month(column: str) -> str:
    return f"to_char({column}, 'YYYY-MM')"


TPCH = [
    case("tpch-01", "tpch-f01", "tpch", "ordinary",
         turn("How many orders were placed in 1995?",
              f"SELECT COUNT(*) FROM orders WHERE {year('o_orderdate', 1995)}",
              m("COUNT(orders)", period="1995 by o_orderdate", grain="one row per order"))),
    case("tpch-02", "tpch-f02", "tpch", "ordinary",
         turn("Number of customers per market segment",
              "SELECT c_mktsegment, COUNT(*) FROM customer GROUP BY c_mktsegment",
              m("COUNT(customer)", by="c_mktsegment"))),
    case("tpch-03", "tpch-f03", "tpch", "ordinary",
         turn("Monthly revenue in 1996",
              f"SELECT {pg_month('o.o_orderdate')}, {REV} FROM {LO} WHERE {year('o.o_orderdate', 1996)} GROUP BY 1",
              m("revenue", by="month of o_orderdate", period="1996"))),
    case("tpch-04", "tpch-f04", "tpch", "ordinary",
         turn("Top 5 customers by revenue",
              f"SELECT c.c_name, {REV} FROM {LOC} GROUP BY c.c_name ORDER BY 2 DESC LIMIT 5",
              m("revenue", by="customer name", grain="customer"), order=True)),
    case("tpch-05", "tpch-f05", "tpch", "ordinary",
         turn("What is the average order value by order priority?",
              "SELECT o_orderpriority, AVG(o_totalprice) FROM orders GROUP BY o_orderpriority",
              m("AVG(o_totalprice)", by="o_orderpriority", grain="one row per order"))),
    case("tpch-06", "tpch-f06", "tpch", "ordinary",
         turn("How many line items were shipped by truck?",
              "SELECT COUNT(*) FROM lineitem WHERE l_shipmode = 'TRUCK'",
              m("COUNT(lineitem)", where="l_shipmode = TRUCK"))),
    case("tpch-07", "tpch-f07", "tpch", "ordinary",
         turn("Which ship mode carried the most line items?",
              "SELECT l_shipmode FROM lineitem GROUP BY l_shipmode ORDER BY COUNT(*) DESC LIMIT 1",
              m("COUNT(lineitem)", by="l_shipmode", where="top 1"))),
    case("tpch-08", "tpch-f08", "tpch", "ordinary",
         turn("Total quantity of parts sold per year",
              f"SELECT {pg_year('o.o_orderdate')}, SUM(l.l_quantity) FROM {LO} GROUP BY 1",
              m("SUM(l_quantity)", by="year of o_orderdate"))),
    case("tpch-09", "tpch-f09", "tpch", "ordinary",
         turn("Average discount by ship mode",
              "SELECT l_shipmode, AVG(l_discount) FROM lineitem GROUP BY l_shipmode",
              m("AVG(l_discount)", by="l_shipmode", grain="one row per line item"))),
    case("tpch-10", "tpch-f10", "tpch", "ordinary",
         turn("How many suppliers are there in each nation?",
              "SELECT n.n_name, COUNT(*) FROM supplier s JOIN nation n ON n.n_nationkey = s.s_nationkey GROUP BY n.n_name",
              m("COUNT(supplier)", by="supplier nation"))),
    case("tpch-11", "tpch-f11", "tpch", "ordinary",
         turn("Number of orders by order status",
              "SELECT o_orderstatus, COUNT(*) FROM orders GROUP BY o_orderstatus",
              m("COUNT(orders)", by="o_orderstatus"))),
    case("tpch-12", "tpch-f12", "tpch", "ordinary",
         turn("Which 10 nations have the most customers?",
              "SELECT n.n_name, COUNT(*) FROM customer c JOIN nation n ON n.n_nationkey = c.c_nationkey "
              "GROUP BY n.n_name ORDER BY 2 DESC LIMIT 10",
              m("COUNT(customer)", by="customer nation", where="top 10"), order=True)),
    case("tpch-13", "tpch-f13", "tpch", "ordinary",
         turn("What is the average account balance of customers in the automobile segment?",
              "SELECT AVG(c_acctbal) FROM customer WHERE c_mktsegment = 'AUTOMOBILE'",
              m("AVG(c_acctbal)", where="c_mktsegment = AUTOMOBILE"))),
    case("tpch-14", "tpch-f14", "tpch", "ordinary",
         turn("Revenue from customers in Germany in 1997",
              f"SELECT {REV} FROM {LOCN} WHERE n.n_name = 'GERMANY' AND {year('o.o_orderdate', 1997)}",
              m("revenue", where="customer nation = GERMANY", period="1997"))),
    case("tpch-15", "tpch-f15", "tpch", "ordinary",
         turn("How many parts does each manufacturer make?",
              "SELECT p_mfgr, COUNT(*) FROM part GROUP BY p_mfgr",
              m("COUNT(part)", by="p_mfgr"))),
    case("tpch-16", "tpch-f16", "tpch", "ordinary",
         turn("Average retail price of parts by brand",
              "SELECT p_brand, AVG(p_retailprice) FROM part GROUP BY p_brand",
              m("AVG(p_retailprice)", by="p_brand"))),
    case("tpch-17", "tpch-f17", "tpch", "ordinary",
         turn("Orders per quarter in 1998",
              f"SELECT {pg_quarter('o_orderdate')}, COUNT(*) FROM orders WHERE {year('o_orderdate', 1998)} GROUP BY 1",
              m("COUNT(orders)", by="quarter of o_orderdate", period="1998"))),
    case("tpch-18", "tpch-f18", "tpch", "ordinary",
         turn("How many urgent orders were placed in 1993?",
              f"SELECT COUNT(*) FROM orders WHERE o_orderpriority = '1-URGENT' AND {year('o_orderdate', 1993)}",
              m("COUNT(orders)", where="o_orderpriority = 1-URGENT", period="1993"))),
    case("tpch-19", "tpch-f19", "tpch", "ordinary",
         turn("Which clerks handled more than 180 orders?",
              "SELECT o_clerk, COUNT(*) FROM orders GROUP BY o_clerk HAVING COUNT(*) > 180",
              m("COUNT(orders)", by="o_clerk", where="count > 180"))),
    # Paraphrases
    case("tpch-20", "tpch-f01", "tpch", "paraphrase",
         turn("Total number of orders placed during 1995",
              f"SELECT COUNT(*) FROM orders WHERE {year('o_orderdate', 1995)}", m("COUNT(orders)", period="1995"))),
    case("tpch-21", "tpch-f02", "tpch", "paraphrase",
         turn("Show me the customer count for each market segment",
              "SELECT c_mktsegment, COUNT(*) FROM customer GROUP BY c_mktsegment", m("COUNT(customer)", by="c_mktsegment"))),
    case("tpch-22", "tpch-f04", "tpch", "paraphrase",
         turn("Which five customers spent the most?",
              f"SELECT c.c_name, {REV} FROM {LOC} GROUP BY c.c_name ORDER BY 2 DESC LIMIT 5",
              m("revenue", by="customer name", where="top 5"), order=True)),
    case("tpch-23", "tpch-f03", "tpch", "paraphrase",
         turn("Sales per month for 1996",
              f"SELECT {pg_month('o.o_orderdate')}, {REV} FROM {LO} WHERE {year('o.o_orderdate', 1996)} GROUP BY 1",
              m("revenue", by="month", period="1996"))),
    case("tpch-24", "tpch-f10", "tpch", "paraphrase",
         turn("For each nation, how many suppliers do we have?",
              "SELECT n.n_name, COUNT(*) FROM supplier s JOIN nation n ON n.n_nationkey = s.s_nationkey GROUP BY n.n_name",
              m("COUNT(supplier)", by="supplier nation"))),
    # Ambiguous (the policy sheet settles them; asking is useful)
    case("tpch-25", "tpch-f25", "tpch", "ambiguous",
         turn("Revenue by region",
              f"SELECT r.r_name, {REV} FROM {LOCNR} GROUP BY r.r_name",
              m("revenue", by="customer region"),
              clarify=(ask("customer's or supplier's region", "the customer's region"),))),
    case("tpch-26", "tpch-f26", "tpch", "ambiguous",
         turn("What were total sales in 1994?",
              f"SELECT {REV} FROM {LO} WHERE {year('o.o_orderdate', 1994)}",
              m("revenue (net line value, not order totals)", period="1994"),
              clarify=(ask("net line revenue or order totals", "net line revenue"),))),
    case("tpch-27", "tpch-f27", "tpch", "ambiguous",
         turn("How many orders arrived late?",
              "SELECT COUNT(DISTINCT l_orderkey) FROM lineitem WHERE l_receiptdate > l_commitdate",
              m("COUNT(DISTINCT orders)", where="any line received after its commit date"),
              clarify=(ask("what counts as late", "an order with any line item received after its commit date"),)),
         grammar=False),
    case("tpch-28", "tpch-f28", "tpch", "ambiguous",
         turn("What is the average order size?",
              "SELECT AVG(o_totalprice) FROM orders",
              m("AVG(o_totalprice)", grain="one row per order"), "clarify",
              clarify=(ask("number of items or order value", "the order value"),))),
    case("tpch-29", "tpch-f29", "tpch", "ambiguous",
         turn("How much revenue came from returned items?",
              f"SELECT {REV} FROM lineitem l WHERE l.l_returnflag = 'R'",
              m("revenue", where="l_returnflag = R"),
              clarify=(ask("which return flag means returned", "R", dimension_like="return", reply="R"),))),
    case("tpch-30", "tpch-f30", "tpch", "ambiguous",
         turn("Who are our top customers?",
              f"SELECT c.c_name, {REV} FROM {LOC} GROUP BY c.c_name ORDER BY 2 DESC LIMIT 10",
              m("revenue", by="customer name", where="top 10"), order=True,
              clarify=(ask("ranked by what, how many", "the top 10 by revenue"),))),
    # Unsupported
    refuse("tpch-31", "tpch-f31", "tpch", "How many website visits led to an order?", "no web traffic data"),
    refuse("tpch-32", "tpch-f32", "tpch", "Which customers are most likely to churn next year?", "a prediction"),
    refuse("tpch-33", "tpch-f33", "tpch", "What is the average salary of our sales staff?", "no employee or salary data"),
    refuse("tpch-34", "tpch-f34", "tpch", "Show total revenue converted to euros", "no currency or exchange rates"),
    refuse("tpch-35", "tpch-f35", "tpch", "What was the shipping cost per order?", "no shipping cost"),
    refuse("tpch-36", "tpch-f36", "tpch", "How satisfied were customers with their orders?", "no satisfaction or review data"),
    # Shapes the grammar cannot express yet
    case("tpch-37", "tpch-f37", "tpch", "ordinary",
         turn("Revenue and number of orders by year",
              f"SELECT {pg_year('o.o_orderdate')}, {REV}, COUNT(DISTINCT o.o_orderkey) FROM {LO} GROUP BY 1",
              m("revenue and COUNT(orders)", by="year")), grammar=False),
    case("tpch-38", "tpch-f38", "tpch", "ordinary",
         turn("What share of revenue comes from each region, in percent?",
              f"SELECT r.r_name, 100.0 * {REV} / SUM(SUM(l.l_extendedprice * (1 - l.l_discount))) OVER () "
              f"FROM {LOCNR} GROUP BY r.r_name",
              m("revenue share %", by="customer region")), grammar=False),
    case("tpch-39", "tpch-f39", "tpch", "ordinary",
         turn("How did revenue in 1997 compare with 1996?",
              f"SELECT {pg_year('o.o_orderdate')}, {REV} FROM {LO} WHERE o.o_orderdate >= '1996-01-01' "
              "AND o.o_orderdate < '1998-01-01' GROUP BY 1",
              m("revenue", by="year", period="1996-1997"))),
    case("tpch-40", "tpch-f40", "tpch", "ordinary",
         turn("Top 3 customers by revenue in each region",
              f"SELECT r_name, c_name, rev FROM (SELECT r.r_name, c.c_name, {REV} AS rev, ROW_NUMBER() OVER "
              f"(PARTITION BY r.r_name ORDER BY {REV} DESC) AS k FROM {LOCNR} GROUP BY r.r_name, c.c_name) t WHERE k <= 3",
              m("revenue", by="region, customer", where="top 3 per region")), grammar=False),
    # Follow-ups
    case("tpch-41", "tpch-f41", "tpch", "follow_up",
         turn("Revenue by year", f"SELECT {pg_year('o.o_orderdate')}, {REV} FROM {LO} GROUP BY 1", m("revenue", by="year")),
         turn("Only for the building segment",
              f"SELECT {pg_year('o.o_orderdate')}, {REV} FROM {LOC} WHERE c.c_mktsegment = 'BUILDING' GROUP BY 1",
              m("revenue", by="year", where="c_mktsegment = BUILDING")),
         turn("Now by quarter for 1997",
              f"SELECT {pg_quarter('o.o_orderdate')}, {REV} FROM {LOC} WHERE c.c_mktsegment = 'BUILDING' "
              f"AND {year('o.o_orderdate', 1997)} GROUP BY 1",
              m("revenue", by="quarter", where="c_mktsegment = BUILDING", period="1997"))),
    case("tpch-42", "tpch-f42", "tpch", "follow_up",
         turn("How many orders were placed for each order priority?",
              "SELECT o_orderpriority, COUNT(*) FROM orders GROUP BY 1", m("COUNT(orders)", by="o_orderpriority")),
         turn("What about in 1998?",
              f"SELECT o_orderpriority, COUNT(*) FROM orders WHERE {year('o_orderdate', 1998)} GROUP BY 1",
              m("COUNT(orders)", by="o_orderpriority", period="1998"))),
    case("tpch-43", "tpch-f43", "tpch", "follow_up",
         turn("Top 5 nations by revenue",
              f"SELECT n.n_name, {REV} FROM {LOCN} GROUP BY n.n_name ORDER BY 2 DESC LIMIT 5",
              m("revenue", by="customer nation", where="top 5"), order=True),
         turn("Show the bottom 5 instead",
              f"SELECT n.n_name, {REV} FROM {LOCN} GROUP BY n.n_name ORDER BY 2 ASC LIMIT 5",
              m("revenue", by="customer nation", where="bottom 5"), order=True)),
    case("tpch-44", "tpch-f44", "tpch", "follow_up",
         turn("Average discount by ship mode",
              "SELECT l_shipmode, AVG(l_discount) FROM lineitem GROUP BY 1", m("AVG(l_discount)", by="l_shipmode")),
         turn("Just for orders placed in 1995",
              f"SELECT l.l_shipmode, AVG(l.l_discount) FROM {LO} WHERE {year('o.o_orderdate', 1995)} GROUP BY 1",
              m("AVG(l_discount)", by="l_shipmode", period="1995 by o_orderdate")),
         turn("Start over: how many parts are there?", "SELECT COUNT(*) FROM part", m("COUNT(part)"))),
    case("tpch-45", "tpch-f45", "tpch", "follow_up",
         turn("Number of customers by nation",
              "SELECT n.n_name, COUNT(*) FROM customer c JOIN nation n ON n.n_nationkey = c.c_nationkey GROUP BY 1",
              m("COUNT(customer)", by="customer nation")),
         turn("Only in the machinery segment",
              "SELECT n.n_name, COUNT(*) FROM customer c JOIN nation n ON n.n_nationkey = c.c_nationkey "
              "WHERE c.c_mktsegment = 'MACHINERY' GROUP BY 1",
              m("COUNT(customer)", by="customer nation", where="c_mktsegment = MACHINERY"))),
]

# ---------------------------------------------------------------------------------------- employees
CUR = "'9999-01-01'"
CUR_SAL = f"salaries s ON s.emp_no = e.emp_no AND s.to_date = {CUR}"
CUR_DEPT = (f"dept_emp de ON de.emp_no = e.emp_no AND de.to_date = {CUR} "
            "JOIN departments d ON d.dept_no = de.dept_no")
CUR_TITLE = f"titles t ON t.emp_no = e.emp_no AND t.to_date = {CUR}"
ENGINEERS = "t.title LIKE '%Engineer%'"

EMPLOYEES = [
    case("emp-01", "emp-f01", "employees", "ordinary",
         turn("How many employees were hired each year?",
              "SELECT CAST(YEAR(hire_date) AS CHAR), COUNT(*) FROM employees GROUP BY 1",
              m("COUNT(employees)", by="year of hire_date", grain="every employee ever hired"))),
    case("emp-02", "emp-f02", "employees", "ordinary",
         turn("How many departments are there?", "SELECT COUNT(*) FROM departments", m("COUNT(departments)"))),
    case("emp-03", "emp-f03", "employees", "ordinary",
         turn("What is the average current salary by department?",
              f"SELECT d.dept_name, AVG(s.salary) FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT} GROUP BY 1",
              m("AVG(current salary)", by="current department", grain="one row per current employee"))),
    case("emp-04", "emp-f04", "employees", "ordinary",
         turn("Which job title has the highest average current salary?",
              f"SELECT t.title FROM employees e JOIN {CUR_SAL} JOIN {CUR_TITLE} GROUP BY 1 ORDER BY AVG(s.salary) DESC LIMIT 1",
              m("AVG(current salary)", by="current title", where="top 1"))),
    case("emp-05", "emp-f05", "employees", "ordinary",
         turn("How many employees joined in 1990?",
              "SELECT COUNT(*) FROM employees WHERE hire_date >= '1990-01-01' AND hire_date < '1991-01-01'",
              m("COUNT(employees)", period="1990 by hire_date"))),
    case("emp-06", "emp-f06", "employees", "ordinary",
         turn("Rank the departments by their number of current employees",
              f"SELECT d.dept_name, COUNT(*) FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT} GROUP BY 1 ORDER BY 2 DESC",
              m("COUNT(current employees)", by="current department"), order=True)),
    case("emp-07", "emp-f07", "employees", "ordinary",
         turn("What is the highest salary ever paid?", "SELECT MAX(salary) FROM salaries", m("MAX(salary)")),
         grammar=False),
    case("emp-08", "emp-f08", "employees", "ordinary",
         turn("How many salary changes were recorded in 1999?",
              "SELECT COUNT(*) FROM salaries WHERE from_date >= '1999-01-01' AND from_date < '2000-01-01'",
              m("COUNT(salaries)", period="1999 by from_date"))),
    case("emp-09", "emp-f09", "employees", "ordinary",
         turn("How many people in the employee records were born in the 1950s?",
              "SELECT COUNT(*) FROM employees WHERE birth_date >= '1950-01-01' AND birth_date < '1960-01-01'",
              m("COUNT(employees)", where="birth_date in 1950-1959", grain="everyone ever employed"))),
    case("emp-10", "emp-f10", "employees", "ordinary",
         turn("How many current employees hold each title?",
              f"SELECT t.title, COUNT(*) FROM employees e JOIN {CUR_SAL} JOIN {CUR_TITLE} GROUP BY 1",
              m("COUNT(current employees)", by="current title"))),
    case("emp-11", "emp-f11", "employees", "ordinary",
         turn("How many department managers have there been in total?",
              "SELECT COUNT(*) FROM dept_manager", m("COUNT(dept_manager)", grain="every manager appointment"))),
    case("emp-12", "emp-f12", "employees", "ordinary",
         turn("What is the average current salary of Senior Engineers?",
              f"SELECT AVG(s.salary) FROM employees e JOIN {CUR_SAL} JOIN {CUR_TITLE} WHERE t.title = 'Senior Engineer'",
              m("AVG(current salary)", where="current title = Senior Engineer"))),
    case("emp-13", "emp-f13", "employees", "ordinary",
         turn("How many people work in Sales right now?",
              f"SELECT COUNT(*) FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT} WHERE d.dept_name = 'Sales'",
              m("COUNT(current employees)", where="current department = Sales"))),
    case("emp-14", "emp-f14", "employees", "ordinary",
         turn("Hires per month in 1995",
              "SELECT DATE_FORMAT(hire_date, '%Y-%m'), COUNT(*) FROM employees "
              "WHERE hire_date >= '1995-01-01' AND hire_date < '1996-01-01' GROUP BY 1",
              m("COUNT(employees)", by="month of hire_date", period="1995"))),
    case("emp-15", "emp-f15", "employees", "ordinary",
         turn("How many employees were hired between 1985 and 1987?",
              "SELECT COUNT(*) FROM employees WHERE hire_date >= '1985-01-01' AND hire_date < '1988-01-01'",
              m("COUNT(employees)", period="1985-1987 by hire_date"))),
    # Paraphrases
    case("emp-16", "emp-f01", "employees", "paraphrase",
         turn("Show the number of new hires per year",
              "SELECT CAST(YEAR(hire_date) AS CHAR), COUNT(*) FROM employees GROUP BY 1", m("COUNT(employees)", by="year"))),
    case("emp-17", "emp-f03", "employees", "paraphrase",
         turn("What's the mean salary in each department today?",
              f"SELECT d.dept_name, AVG(s.salary) FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT} GROUP BY 1",
              m("AVG(current salary)", by="current department"))),
    case("emp-18", "emp-f13", "employees", "paraphrase",
         turn("What is the current headcount of the Sales department?",
              f"SELECT COUNT(*) FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT} WHERE d.dept_name = 'Sales'",
              m("COUNT(current employees)", where="Sales"))),
    case("emp-19", "emp-f10", "employees", "paraphrase",
         turn("Break down today's workforce by job title",
              f"SELECT t.title, COUNT(*) FROM employees e JOIN {CUR_SAL} JOIN {CUR_TITLE} GROUP BY 1",
              m("COUNT(current employees)", by="current title"))),
    # Ambiguous
    case("emp-20", "emp-f20", "employees", "ambiguous",
         turn("How many employees are there?",
              f"SELECT COUNT(*) FROM employees e JOIN {CUR_SAL}", m("COUNT(current employees)"),
              clarify=(ask("current or everyone ever employed", "current employees"),))),
    case("emp-21", "emp-f21", "employees", "ambiguous",
         turn("Number of employees by gender",
              f"SELECT e.gender, COUNT(*) FROM employees e JOIN {CUR_SAL} GROUP BY 1",
              m("COUNT(current employees)", by="gender"),
              clarify=(ask("current or everyone ever employed", "current employees"),))),
    case("emp-22", "emp-f22", "employees", "ambiguous",
         turn("What is the average salary?",
              f"SELECT AVG(s.salary) FROM salaries s WHERE s.to_date = {CUR}", m("AVG(current salary)"),
              clarify=(ask("current salaries or the whole salary history", "current salaries"),))),
    case("emp-23", "emp-f23", "employees", "ambiguous",
         turn("Which department has the most employees?",
              f"SELECT d.dept_name FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT} GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1",
              m("COUNT(current employees)", by="current department", where="top 1"),
              clarify=(ask("current or all-time employees", "current employees"),))),
    case("emp-24", "emp-f24", "employees", "ambiguous",
         turn("What is the average salary of engineers?",
              f"SELECT AVG(s.salary) FROM employees e JOIN {CUR_SAL} JOIN {CUR_TITLE} WHERE {ENGINEERS}",
              m("AVG(current salary)", where="current title contains Engineer"),
              clarify=(ask("which engineer titles", "all titles containing Engineer"),))),
    case("emp-25", "emp-f25", "employees", "ambiguous",
         turn("How many employees have left the company?",
              f"SELECT COUNT(*) FROM employees e WHERE NOT EXISTS (SELECT 1 FROM salaries s WHERE s.emp_no = e.emp_no "
              f"AND s.to_date = {CUR})",
              m("COUNT(employees without a current salary)"),
              clarify=(ask("what counts as having left", "no current salary row"),)), grammar=False),
    # Unsupported
    refuse("emp-26", "emp-f26", "employees", "What is the average performance rating by department?", "no ratings"),
    refuse("emp-27", "emp-f27", "employees", "How many employees work remotely?", "no work location"),
    refuse("emp-28", "emp-f28", "employees", "What bonus did managers receive in 1999?", "no bonus data"),
    refuse("emp-29", "emp-f29", "employees", "Which employees are likely to quit next year?", "a prediction"),
    refuse("emp-30", "emp-f30", "employees", "What is the office address of the Finance department?", "no addresses"),
    refuse("emp-31", "emp-f31", "employees", "How many sick days did employees take last year?", "no absence data"),
    # Shapes the grammar cannot express yet
    case("emp-32", "emp-f32", "employees", "ordinary",
         turn("Average current salary and headcount by department",
              f"SELECT d.dept_name, AVG(s.salary), COUNT(*) FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT} GROUP BY 1",
              m("AVG(current salary) and COUNT", by="current department")), grammar=False),
    case("emp-33", "emp-f33", "employees", "ordinary",
         turn("What percentage of current employees are women?",
              f"SELECT 100.0 * SUM(e.gender = 'F') / COUNT(*) FROM employees e JOIN {CUR_SAL}",
              m("share of current employees with gender F, %")), grammar=False),
    case("emp-34", "emp-f34", "employees", "ordinary",
         turn("Who are the top 3 earners in each department?",
              f"SELECT dept_name, name, salary FROM (SELECT d.dept_name, CONCAT(e.first_name, ' ', e.last_name) AS name, "
              f"s.salary, ROW_NUMBER() OVER (PARTITION BY d.dept_name ORDER BY s.salary DESC) AS k "
              f"FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT}) t WHERE k <= 3",
              m("current salary", by="current department, employee", where="top 3 per department")), grammar=False),
    # Follow-ups
    case("emp-35", "emp-f35", "employees", "follow_up",
         turn("How many employees were hired each year?",
              "SELECT CAST(YEAR(hire_date) AS CHAR), COUNT(*) FROM employees GROUP BY 1", m("COUNT(employees)", by="year")),
         turn("Only women",
              "SELECT CAST(YEAR(hire_date) AS CHAR), COUNT(*) FROM employees WHERE gender = 'F' GROUP BY 1",
              m("COUNT(employees)", by="year", where="gender = F")),
         turn("What about men?",
              "SELECT CAST(YEAR(hire_date) AS CHAR), COUNT(*) FROM employees WHERE gender = 'M' GROUP BY 1",
              m("COUNT(employees)", by="year", where="gender = M"))),
    case("emp-36", "emp-f36", "employees", "follow_up",
         turn("Average current salary by title",
              f"SELECT t.title, AVG(s.salary) FROM employees e JOIN {CUR_SAL} JOIN {CUR_TITLE} GROUP BY 1",
              m("AVG(current salary)", by="current title")),
         turn("Only in the Research department",
              f"SELECT t.title, AVG(s.salary) FROM employees e JOIN {CUR_SAL} JOIN {CUR_TITLE} JOIN {CUR_DEPT} "
              "WHERE d.dept_name = 'Research' GROUP BY 1",
              m("AVG(current salary)", by="current title", where="current department = Research"))),
    case("emp-37", "emp-f37", "employees", "follow_up",
         turn("Current headcount by department",
              f"SELECT d.dept_name, COUNT(*) FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT} GROUP BY 1",
              m("COUNT(current employees)", by="current department")),
         turn("Show only the top 3",
              f"SELECT d.dept_name, COUNT(*) FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT} GROUP BY 1 ORDER BY 2 DESC LIMIT 3",
              m("COUNT(current employees)", by="current department", where="top 3"), order=True)),
    case("emp-38", "emp-f38", "employees", "follow_up",
         turn("How many employees were hired in 1990?",
              "SELECT COUNT(*) FROM employees WHERE hire_date >= '1990-01-01' AND hire_date < '1991-01-01'",
              m("COUNT(employees)", period="1990")),
         turn("And in 1991?",
              "SELECT COUNT(*) FROM employees WHERE hire_date >= '1991-01-01' AND hire_date < '1992-01-01'",
              m("COUNT(employees)", period="1991"))),
    case("emp-39", "emp-f39", "employees", "follow_up",
         turn("Average current salary of Senior Staff",
              f"SELECT AVG(s.salary) FROM employees e JOIN {CUR_SAL} JOIN {CUR_TITLE} WHERE t.title = 'Senior Staff'",
              m("AVG(current salary)", where="current title = Senior Staff")),
         turn("What about Staff?",
              f"SELECT AVG(s.salary) FROM employees e JOIN {CUR_SAL} JOIN {CUR_TITLE} WHERE t.title = 'Staff'",
              m("AVG(current salary)", where="current title = Staff")),
         turn("Start over: how many departments are there?", "SELECT COUNT(*) FROM departments", m("COUNT(departments)"))),
    case("emp-40", "emp-f40", "employees", "follow_up",
         turn("Current employees by gender",
              f"SELECT e.gender, COUNT(*) FROM employees e JOIN {CUR_SAL} GROUP BY 1",
              m("COUNT(current employees)", by="gender")),
         turn("Now split by department too",
              f"SELECT e.gender, d.dept_name, COUNT(*) FROM employees e JOIN {CUR_SAL} JOIN {CUR_DEPT} GROUP BY 1, 2",
              m("COUNT(current employees)", by="gender, current department"))),
]

# --------------------------------------------------------------------------------------------- taxi
JAN = "t.tpep_pickup_datetime >= '2024-01-01' AND t.tpep_pickup_datetime < '2024-02-01'"
TZ = "trips t JOIN zones z ON z.LocationID = t.PULocationID"


def day(column: str) -> str:
    return f"CAST(CAST({column} AS DATE) AS VARCHAR)"


TAXI = [
    case("taxi-01", "taxi-f01", "taxi", "ordinary",
         turn("How many trips were there in January 2024?", f"SELECT COUNT(*) FROM trips t WHERE {JAN}",
              m("COUNT(trips)", period="January 2024 by pickup"))),
    case("taxi-02", "taxi-f02", "taxi", "ordinary",
         turn("What is the average trip distance?", f"SELECT AVG(t.trip_distance) FROM trips t WHERE {JAN}",
              m("AVG(trip_distance)", grain="one row per trip"))),
    case("taxi-03", "taxi-f03", "taxi", "ordinary",
         turn("How many trips were there each day?",
              f"SELECT {day('t.tpep_pickup_datetime')}, COUNT(*) FROM trips t WHERE {JAN} GROUP BY 1",
              m("COUNT(trips)", by="pickup day"))),
    case("taxi-04", "taxi-f04", "taxi", "ordinary",
         turn("Total tips by payment type",
              f"SELECT t.payment_type, SUM(t.tip_amount) FROM trips t WHERE {JAN} GROUP BY 1",
              m("SUM(tip_amount)", by="payment_type code"))),
    case("taxi-05", "taxi-f05", "taxi", "ordinary",
         turn("Average fare by number of passengers",
              f"SELECT t.passenger_count, AVG(t.fare_amount) FROM trips t WHERE {JAN} GROUP BY 1",
              m("AVG(fare_amount)", by="passenger_count (unknown counts as its own group)"))),
    case("taxi-06", "taxi-f06", "taxi", "ordinary",
         turn("Which 10 pickup zones had the most trips?",
              f"SELECT z.Zone, COUNT(*) FROM {TZ} WHERE {JAN} GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
              m("COUNT(trips)", by="pickup zone", where="top 10"), order=True)),
    case("taxi-07", "taxi-f07", "taxi", "ordinary",
         turn("Number of trips by pickup borough",
              f"SELECT z.Borough, COUNT(*) FROM {TZ} WHERE {JAN} GROUP BY 1",
              m("COUNT(trips)", by="pickup borough"))),
    case("taxi-08", "taxi-f08", "taxi", "ordinary",
         turn("Total revenue by vendor",
              f"SELECT t.VendorID, SUM(t.total_amount) FROM trips t WHERE {JAN} GROUP BY 1",
              m("SUM(total_amount)", by="VendorID"))),
    case("taxi-09", "taxi-f09", "taxi", "ordinary",
         turn("How many trips had more than 4 passengers?",
              f"SELECT COUNT(*) FROM trips t WHERE {JAN} AND t.passenger_count > 4",
              m("COUNT(trips)", where="passenger_count > 4"))),
    case("taxi-10", "taxi-f10", "taxi", "ordinary",
         turn("What is the average tip on trips longer than 10 miles?",
              f"SELECT AVG(t.tip_amount) FROM trips t WHERE {JAN} AND t.trip_distance > 10",
              m("AVG(tip_amount)", where="trip_distance > 10"))),
    case("taxi-11", "taxi-f11", "taxi", "ordinary",
         turn("How much was paid in tolls in total?", f"SELECT SUM(t.tolls_amount) FROM trips t WHERE {JAN}",
              m("SUM(tolls_amount)"))),
    case("taxi-12", "taxi-f12", "taxi", "ordinary",
         turn("Weekly number of trips",
              f"SELECT CAST(CAST(date_trunc('week', t.tpep_pickup_datetime) AS DATE) AS VARCHAR), COUNT(*) "
              f"FROM trips t WHERE {JAN} GROUP BY 1",
              m("COUNT(trips)", by="week (Monday start)"))),
    case("taxi-13", "taxi-f13", "taxi", "ordinary",
         turn("Average trip distance for trips starting in Brooklyn",
              f"SELECT AVG(t.trip_distance) FROM {TZ} WHERE {JAN} AND z.Borough = 'Brooklyn'",
              m("AVG(trip_distance)", where="pickup borough = Brooklyn"))),
    case("taxi-14", "taxi-f14", "taxi", "ordinary",
         turn("How many trips used the JFK airport rate?",
              f"SELECT COUNT(*) FROM trips t WHERE {JAN} AND t.RatecodeID = 2",
              m("COUNT(trips)", where="RatecodeID = 2 (JFK)"),
              clarify=(ask("which rate code is JFK", "2", dimension_like="rate", reply="2"),))),
    # Paraphrases
    case("taxi-15", "taxi-f01", "taxi", "paraphrase",
         turn("Total trip count for January 2024", f"SELECT COUNT(*) FROM trips t WHERE {JAN}", m("COUNT(trips)"))),
    case("taxi-16", "taxi-f06", "taxi", "paraphrase",
         turn("Top 10 busiest pickup zones",
              f"SELECT z.Zone, COUNT(*) FROM {TZ} WHERE {JAN} GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
              m("COUNT(trips)", by="pickup zone", where="top 10"), order=True)),
    case("taxi-17", "taxi-f07", "taxi", "paraphrase",
         turn("How many rides started in each borough?",
              f"SELECT z.Borough, COUNT(*) FROM {TZ} WHERE {JAN} GROUP BY 1", m("COUNT(trips)", by="pickup borough"))),
    case("taxi-18", "taxi-f02", "taxi", "paraphrase",
         turn("What's the mean distance of a ride?", f"SELECT AVG(t.trip_distance) FROM trips t WHERE {JAN}",
              m("AVG(trip_distance)"))),
    # Ambiguous
    case("taxi-19", "taxi-f19", "taxi", "ambiguous",
         turn("How many trips were paid in cash?", f"SELECT COUNT(*) FROM trips t WHERE {JAN} AND t.payment_type = 2",
              m("COUNT(trips)", where="payment_type = 2 (cash)"),
              clarify=(ask("which payment code is cash", "2", dimension_like="payment", reply="2"),))),
    case("taxi-20", "taxi-f20", "taxi", "ambiguous",
         turn("What was the total revenue in January?", f"SELECT SUM(t.total_amount) FROM trips t WHERE {JAN}",
              m("SUM(total_amount)"), clarify=(ask("total amount or fare only", "total amount"),))),
    case("taxi-21", "taxi-f21", "taxi", "ambiguous",
         turn("What is the average fare in Manhattan?",
              f"SELECT AVG(t.fare_amount) FROM {TZ} WHERE {JAN} AND z.Borough = 'Manhattan'",
              m("AVG(fare_amount)", where="pickup borough = Manhattan"),
              clarify=(ask("pickup or drop-off borough", "pickup"),))),
    case("taxi-22", "taxi-f22", "taxi", "ambiguous",
         turn("How many trips were disputed?", f"SELECT COUNT(*) FROM trips t WHERE {JAN} AND t.payment_type = 4",
              m("COUNT(trips)", where="payment_type = 4 (dispute)"),
              clarify=(ask("which payment code is a dispute", "4", dimension_like="payment", reply="4"),))),
    case("taxi-23", "taxi-f23", "taxi", "ambiguous",
         turn("How many airport trips were there?", f"SELECT COUNT(*) FROM trips t WHERE {JAN} AND t.Airport_fee > 0",
              m("COUNT(trips)", where="Airport_fee > 0"),
              clarify=(ask("what counts as an airport trip", "trips with an airport fee"),))),
    # Unsupported
    refuse("taxi-24", "taxi-f24", "taxi", "What was the average driver rating?", "no ratings"),
    refuse("taxi-25", "taxi-f25", "taxi", "How much fuel did the taxis use?", "no fuel data"),
    refuse("taxi-26", "taxi-f26", "taxi", "Which drivers earned the most?", "no driver identifier"),
    refuse("taxi-27", "taxi-f27", "taxi", "How many trips were booked through a mobile app?", "no booking channel"),
    refuse("taxi-28", "taxi-f28", "taxi", "How did the weather affect the number of trips?", "no weather data"),
    # Shapes the grammar cannot express yet
    case("taxi-29", "taxi-f29", "taxi", "ordinary",
         turn("How many trips start in each hour of the day?",
              f"SELECT EXTRACT(hour FROM t.tpep_pickup_datetime), COUNT(*) FROM trips t WHERE {JAN} GROUP BY 1",
              m("COUNT(trips)", by="hour of pickup")), grammar=False),
    case("taxi-30", "taxi-f30", "taxi", "ordinary",
         turn("Average fare and average tip by pickup borough",
              f"SELECT z.Borough, AVG(t.fare_amount), AVG(t.tip_amount) FROM {TZ} WHERE {JAN} GROUP BY 1",
              m("AVG(fare_amount) and AVG(tip_amount)", by="pickup borough")), grammar=False),
    case("taxi-31", "taxi-f31", "taxi", "ordinary",
         turn("What percentage of trips were paid by credit card?",
              f"SELECT 100.0 * COUNT(*) FILTER (WHERE t.payment_type = 1) / COUNT(*) FROM trips t WHERE {JAN}",
              m("share of trips with payment_type 1, %")), grammar=False),
    case("taxi-32", "taxi-f32", "taxi", "ordinary",
         turn("Top 3 drop-off zones for trips starting in Manhattan, Brooklyn and Queens, for each of those boroughs",
              f"SELECT borough, zone, n FROM (SELECT z.Borough AS borough, dz.Zone AS zone, COUNT(*) AS n, "
              f"ROW_NUMBER() OVER (PARTITION BY z.Borough ORDER BY COUNT(*) DESC) AS k FROM {TZ} "
              f"JOIN zones dz ON dz.LocationID = t.DOLocationID WHERE {JAN} "
              f"AND z.Borough IN ('Manhattan', 'Brooklyn', 'Queens') GROUP BY 1, 2) x WHERE k <= 3",
              m("COUNT(trips)", by="pickup borough, drop-off zone", where="top 3 per borough")), grammar=False),
    # Follow-ups
    case("taxi-33", "taxi-f33", "taxi", "follow_up",
         turn("Trips per day in January",
              f"SELECT {day('t.tpep_pickup_datetime')}, COUNT(*) FROM trips t WHERE {JAN} GROUP BY 1",
              m("COUNT(trips)", by="day")),
         turn("Only trips picked up at JFK Airport",
              f"SELECT {day('t.tpep_pickup_datetime')}, COUNT(*) FROM {TZ} WHERE {JAN} AND z.Zone = 'JFK Airport' GROUP BY 1",
              m("COUNT(trips)", by="day", where="pickup zone = JFK Airport")),
         turn("Show it weekly instead",
              f"SELECT CAST(CAST(date_trunc('week', t.tpep_pickup_datetime) AS DATE) AS VARCHAR), COUNT(*) FROM {TZ} "
              f"WHERE {JAN} AND z.Zone = 'JFK Airport' GROUP BY 1",
              m("COUNT(trips)", by="week", where="pickup zone = JFK Airport"))),
    case("taxi-34", "taxi-f34", "taxi", "follow_up",
         turn("Average tip by pickup borough",
              f"SELECT z.Borough, AVG(t.tip_amount) FROM {TZ} WHERE {JAN} GROUP BY 1", m("AVG(tip_amount)", by="pickup borough")),
         turn("What about the average fare?",
              f"SELECT z.Borough, AVG(t.fare_amount) FROM {TZ} WHERE {JAN} GROUP BY 1", m("AVG(fare_amount)", by="pickup borough"))),
    case("taxi-35", "taxi-f35", "taxi", "follow_up",
         turn("Total revenue by vendor",
              f"SELECT t.VendorID, SUM(t.total_amount) FROM trips t WHERE {JAN} GROUP BY 1", m("SUM(total_amount)", by="VendorID")),
         turn("Just the first week of January",
              "SELECT t.VendorID, SUM(t.total_amount) FROM trips t WHERE t.tpep_pickup_datetime >= '2024-01-01' "
              "AND t.tpep_pickup_datetime < '2024-01-08' GROUP BY 1",
              m("SUM(total_amount)", by="VendorID", period="2024-01-01 to 2024-01-07"))),
]

CASES = [*TPCH, *EMPLOYEES, *TAXI]
