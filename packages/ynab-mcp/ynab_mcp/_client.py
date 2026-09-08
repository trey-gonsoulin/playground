"""Thin httpx wrapper around the YNAB REST API (api.ynab.com/v1)."""

import os
from datetime import date

import httpx

_YNAB_BASE = "https://api.ynab.com/v1"

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("YNAB_API_KEY", "")
        _client = httpx.Client(
            base_url=_YNAB_BASE,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
    return _client


def _req(method: str, path: str, **kwargs) -> dict:
    resp = _get_client().request(method, path, **kwargs)
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"YNAB API error: {body['error']['detail']}")
    resp.raise_for_status()
    return body["data"]


def _current_month() -> str:
    """Return the first day of the current month as YYYY-MM-01."""
    today = date.today()
    return today.strftime("%Y-%m-01")


def _resolve_month(month: str | None) -> str:
    if not month or month.lower() == "current":
        return _current_month()
    if len(month) == 7:  # YYYY-MM
        return month + "-01"
    return month  # assume YYYY-MM-01


def _dollars(milliunits: int) -> float:
    return round(milliunits / 1000, 2)


def _milliunits(dollars: float) -> int:
    return round(dollars * 1000)


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------


def list_budgets() -> list[dict]:
    data = _req("GET", "/budgets")
    return [{"id": b["id"], "name": b["name"]} for b in data["budgets"]]


def get_accounts(budget_id: str) -> list[dict]:
    data = _req("GET", f"/budgets/{budget_id}/accounts")
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "type": a["type"],
            "balance_dollars": _dollars(a["balance"]),
            "cleared_balance_dollars": _dollars(a["cleared_balance"]),
            "uncleared_balance_dollars": _dollars(a["uncleared_balance"]),
            "on_budget": a["on_budget"],
        }
        for a in data["accounts"]
        if not a["closed"] and not a["deleted"]
    ]


def get_categories(budget_id: str, month: str) -> list[dict]:
    month = _resolve_month(month)

    # Group names from the category structure endpoint
    groups_data = _req("GET", f"/budgets/{budget_id}/categories")
    group_name_by_cat: dict[str, str] = {}
    for group in groups_data["category_groups"]:
        if group.get("deleted") or group.get("hidden"):
            continue
        for cat in group["categories"]:
            group_name_by_cat[cat["id"]] = group["name"]

    # Monthly budget amounts
    month_data = _req("GET", f"/budgets/{budget_id}/months/{month}")

    result = []
    for cat in month_data["month"]["categories"]:
        if cat.get("hidden") or cat.get("deleted"):
            continue
        result.append(
            {
                "id": cat["id"],
                "name": cat["name"],
                "group": group_name_by_cat.get(cat["id"], ""),
                "budgeted_dollars": _dollars(cat["budgeted"]),
                "activity_dollars": _dollars(cat["activity"]),
                "balance_dollars": _dollars(cat["balance"]),
                "goal_type": cat.get("goal_type"),
                "goal_target_dollars": _dollars(cat["goal_target"])
                if cat.get("goal_target")
                else None,
            }
        )
    return result


def get_month_summary(budget_id: str, month: str) -> dict:
    month = _resolve_month(month)
    data = _req("GET", f"/budgets/{budget_id}/months/{month}")
    m = data["month"]
    return {
        "month": m["month"],
        "income_dollars": _dollars(m["income"]),
        "budgeted_dollars": _dollars(m["budgeted"]),
        "activity_dollars": _dollars(m["activity"]),
        "to_be_budgeted_dollars": _dollars(m["to_be_budgeted"]),
        "age_of_money": m.get("age_of_money"),
    }


def get_transactions(
    budget_id: str,
    since_date: str | None = None,
    account_id: str | None = None,
    max_results: int = 50,
) -> list[dict]:
    params: dict = {}
    if since_date:
        params["since_date"] = since_date

    if account_id:
        data = _req(
            "GET",
            f"/budgets/{budget_id}/accounts/{account_id}/transactions",
            params=params,
        )
    else:
        data = _req("GET", f"/budgets/{budget_id}/transactions", params=params)

    txns = data["transactions"][:max_results]
    return [
        {
            "id": t["id"],
            "date": t["date"],
            "amount_dollars": _dollars(t["amount"]),
            "payee_name": t.get("payee_name"),
            "category_name": t.get("category_name"),
            "account_name": t.get("account_name"),
            "memo": t.get("memo"),
            "cleared": t.get("cleared"),
            "approved": t.get("approved"),
        }
        for t in txns
        if not t.get("deleted")
    ]


def create_transaction(
    budget_id: str,
    account_id: str,
    date: str,
    amount_dollars: float,
    payee_name: str,
    category_id: str | None = None,
    memo: str | None = None,
    cleared: bool = False,
) -> dict:
    payload: dict = {
        "account_id": account_id,
        "date": date,
        "amount": _milliunits(amount_dollars),
        "payee_name": payee_name,
        "approved": True,
        "cleared": "cleared" if cleared else "uncleared",
    }
    if category_id:
        payload["category_id"] = category_id
    if memo:
        payload["memo"] = memo

    data = _req(
        "POST", f"/budgets/{budget_id}/transactions", json={"transaction": payload}
    )
    t = data["transaction"]
    return {
        "id": t["id"],
        "date": t["date"],
        "amount_dollars": _dollars(t["amount"]),
        "payee_name": t.get("payee_name"),
        "category_name": t.get("category_name"),
        "account_name": t.get("account_name"),
        "cleared": t.get("cleared"),
    }


def update_category_budget(
    budget_id: str,
    month: str,
    category_id: str,
    budgeted_dollars: float,
) -> dict:
    month = _resolve_month(month)
    data = _req(
        "PATCH",
        f"/budgets/{budget_id}/months/{month}/categories/{category_id}",
        json={"category": {"budgeted": _milliunits(budgeted_dollars)}},
    )
    cat = data["category"]
    return {
        "id": cat["id"],
        "name": cat["name"],
        "budgeted_dollars": _dollars(cat["budgeted"]),
        "activity_dollars": _dollars(cat["activity"]),
        "balance_dollars": _dollars(cat["balance"]),
    }


def update_transaction(
    budget_id: str,
    transaction_id: str,
    category_id: str | None = None,
    memo: str | None = None,
    cleared: bool | None = None,
    payee_name: str | None = None,
) -> dict:
    payload: dict = {}
    if category_id is not None:
        payload["category_id"] = category_id
    if memo is not None:
        payload["memo"] = memo
    if cleared is not None:
        payload["cleared"] = "cleared" if cleared else "uncleared"
    if payee_name is not None:
        payload["payee_name"] = payee_name

    data = _req(
        "PUT",
        f"/budgets/{budget_id}/transactions/{transaction_id}",
        json={"transaction": payload},
    )
    t = data["transaction"]
    return {
        "id": t["id"],
        "date": t["date"],
        "amount_dollars": _dollars(t["amount"]),
        "payee_name": t.get("payee_name"),
        "category_name": t.get("category_name"),
        "account_name": t.get("account_name"),
        "memo": t.get("memo"),
        "cleared": t.get("cleared"),
    }


def rename_category(budget_id: str, category_id: str, name: str) -> dict:
    data = _req(
        "PATCH",
        f"/budgets/{budget_id}/categories/{category_id}",
        json={"category": {"name": name}},
    )
    cat = data["category"]
    return {"id": cat["id"], "name": cat["name"]}


def move_category_balance(
    budget_id: str,
    month: str,
    from_category_id: str,
    to_category_id: str,
    amount_dollars: float,
) -> dict:
    month = _resolve_month(month)
    month_data = _req("GET", f"/budgets/{budget_id}/months/{month}")
    cats = {c["id"]: c for c in month_data["month"]["categories"]}

    from_cat = cats.get(from_category_id)
    to_cat = cats.get(to_category_id)
    if not from_cat:
        raise RuntimeError(f"Category {from_category_id!r} not found in {month}.")
    if not to_cat:
        raise RuntimeError(f"Category {to_category_id!r} not found in {month}.")

    amount_mu = _milliunits(amount_dollars)
    from_result = _req(
        "PATCH",
        f"/budgets/{budget_id}/months/{month}/categories/{from_category_id}",
        json={"category": {"budgeted": from_cat["budgeted"] - amount_mu}},
    )
    to_result = _req(
        "PATCH",
        f"/budgets/{budget_id}/months/{month}/categories/{to_category_id}",
        json={"category": {"budgeted": to_cat["budgeted"] + amount_mu}},
    )

    def _cat_summary(c: dict) -> dict:
        return {
            "id": c["id"],
            "name": c["name"],
            "budgeted_dollars": _dollars(c["budgeted"]),
            "balance_dollars": _dollars(c["balance"]),
        }

    return {
        "moved_dollars": amount_dollars,
        "from": _cat_summary(from_result["category"]),
        "to": _cat_summary(to_result["category"]),
    }


def set_category_goal(
    budget_id: str,
    category_id: str,
    goal_target_dollars: float | None,
    goal_target_date: str | None = None,
    goal_frequency: str | None = None,
    refill_up_to: bool = True,
) -> dict:
    if goal_target_dollars is None:
        payload: dict = {"goal_target": None}
    else:
        payload = {"goal_target": _milliunits(goal_target_dollars)}
        if goal_target_date:
            payload["goal_target_date"] = goal_target_date
        if goal_frequency:
            payload["goal_frequency"] = goal_frequency
            payload["goal_needs_whole_amount"] = not refill_up_to

    data = _req(
        "PATCH",
        f"/budgets/{budget_id}/categories/{category_id}",
        json={"category": payload},
    )
    cat = data["category"]
    return {
        "id": cat["id"],
        "name": cat["name"],
        "goal_type": cat.get("goal_type"),
        "goal_target_dollars": _dollars(cat["goal_target"])
        if cat.get("goal_target")
        else None,
        "goal_target_date": cat.get("goal_target_date"),
        "goal_cadence": cat.get("goal_cadence"),
        "goal_cadence_frequency": cat.get("goal_cadence_frequency"),
        "goal_needs_whole_amount": cat.get("goal_needs_whole_amount"),
        "goal_percentage_complete": cat.get("goal_percentage_complete"),
    }
