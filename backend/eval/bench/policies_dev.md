# Development set: policy sheets

The business decisions a user would confirm when publishing each database (plan step 1c). The
expected results follow them. In Phase 0 the app cannot be told these yet: a question that depends
on one is scored as an answer that follows the policy, and a clarification about it is useful. The
harness answers it as written here.

## tpch (Postgres, TPC-H scale 0.1)

- **Revenue / sales** = `SUM(l_extendedprice * (1 - l_discount))` over line items: net of discount,
  before tax, every line item (returned ones included). The order total `o_totalprice` is not revenue.
- **Order value** = `o_totalprice` (the order's total). "Average order value" averages it per order.
- **Dates**: a sale's date is the order date `o_orderdate`. Years and quarters are calendar ones.
- **Region / nation of a sale or customer** = the customer's (`c_nationkey`); of a supplier, the supplier's.
- **Codes**: order status F = fulfilled, O = open, P = partly fulfilled; return flag R = returned,
  A = accepted, N = none. Priority labels are as stored ("1-URGENT", ...).
- **A late line item** has `l_receiptdate > l_commitdate`; a late order has at least one.
- **"Top customers"** without a number = the top 10 by revenue.

## employees (MySQL, datacharmer/test_db)

- **Employees / headcount / workforce** = current employees: those with a current salary row
  (`salaries.to_date = '9999-01-01'`). "Ever" or "in total" means everyone in `employees`.
- **Salary** = the current salary (`to_date = '9999-01-01'`) unless a period is named.
- **An employee's department and title** = the current ones (`to_date = '9999-01-01'`).
- **Engineers** = every title containing "Engineer" (Assistant Engineer, Engineer, Senior Engineer).
- **Left the company** = in `employees` with no current salary row.
- **Hires** are counted by `hire_date`.

## taxi (Parquet upload + zone lookup CSV, NYC TLC yellow taxi, January 2024)

- **Trips** = every record with a pickup in January 2024 (`2024-01-01 <= tpep_pickup_datetime <
  2024-02-01`); the 18 records outside January are excluded. This applies to every question.
- **Revenue** = `total_amount` (fare, surcharges, tolls and tips), negative adjustments included.
  **Fare** = `fare_amount`. **Tip** = `tip_amount`.
- **Payment type codes**: 0 = flex fare / unknown, 1 = credit card, 2 = cash, 3 = no charge, 4 = dispute.
- **A trip's zone / borough** = its pickup zone's (join `PULocationID` to the zone lookup), unless
  the question says drop-off.
- **Airport trips** = trips with an airport fee (`Airport_fee > 0`: pickups at LaGuardia or JFK).
- **Averages** include every trip (zero distances and fares too).
- **Weeks** start on Monday.
