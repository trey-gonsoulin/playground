"""MCP server exposing YNAB budget tools via Streamable HTTP transport."""

from mcp.server.fastmcp import FastMCP

from ynab_mcp import _client as ynab

mcp = FastMCP(
    "ynab",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    host="0.0.0.0",
)


@mcp.tool()
def list_budgets() -> list[dict]:
    """List all YNAB budgets accessible with the configured API key.

    Returns a list of budgets with their id and name. Pass the id to other
    tools. The special value 'last-used' can be used as budget_id in any
    other tool to automatically select the most recently accessed budget.
    """
    return ynab.list_budgets()


@mcp.tool()
def get_accounts(budget_id: str = "last-used") -> list[dict]:
    """List all open accounts in a YNAB budget with current balances.

    Args:
        budget_id: Budget ID from list_budgets(), or 'last-used' (default).

    Returns a list of accounts with balance_dollars (positive = assets,
    negative = liabilities/credit cards).
    """
    return ynab.get_accounts(budget_id)


@mcp.tool()
def get_categories(budget_id: str = "last-used", month: str = "current") -> list[dict]:
    """List budget categories for a given month with budgeted/spent/remaining amounts.

    Args:
        budget_id: Budget ID from list_budgets(), or 'last-used' (default).
        month: Month in 'YYYY-MM' or 'YYYY-MM-DD' format, or 'current' (default).

    Returns categories grouped by category group, with dollars for:
    - budgeted_dollars: amount allocated this month
    - activity_dollars: amount spent (negative = spending)
    - balance_dollars: remaining balance
    """
    return ynab.get_categories(budget_id, month)


@mcp.tool()
def get_month_summary(budget_id: str = "last-used", month: str = "current") -> dict:
    """Get a high-level monthly budget summary: income, budgeted, spent, and to-be-budgeted.

    Args:
        budget_id: Budget ID from list_budgets(), or 'last-used' (default).
        month: Month in 'YYYY-MM' format, or 'current' (default).

    Returns:
        income_dollars: Total income received this month.
        budgeted_dollars: Total amount allocated to categories.
        activity_dollars: Total spending (negative value).
        to_be_budgeted_dollars: Unallocated funds available to budget.
        age_of_money: Days since the oldest dollar was earned (YNAB metric).
    """
    return ynab.get_month_summary(budget_id, month)


@mcp.tool()
def get_transactions(
    budget_id: str = "last-used",
    since_date: str | None = None,
    account_id: str | None = None,
    max_results: int = 50,
) -> list[dict]:
    """List transactions from a YNAB budget, optionally filtered by date or account.

    Args:
        budget_id: Budget ID from list_budgets(), or 'last-used' (default).
        since_date: Only return transactions on or after this date (YYYY-MM-DD).
        account_id: Limit to a specific account (ID from get_accounts()).
        max_results: Maximum number of transactions to return (default 50).

    Returns transactions with amount_dollars — negative means spending (outflow),
    positive means income (inflow).
    """
    return ynab.get_transactions(budget_id, since_date, account_id, max_results)


@mcp.tool()
def create_transaction(
    account_id: str,
    date: str,
    amount_dollars: float,
    payee_name: str,
    budget_id: str = "last-used",
    category_id: str | None = None,
    memo: str | None = None,
    cleared: bool = False,
) -> dict:
    """Create a new transaction in YNAB.

    Args:
        account_id: Account to record the transaction on (ID from get_accounts()).
        date: Transaction date in YYYY-MM-DD format.
        amount_dollars: Dollar amount — negative for spending/outflow (e.g. -45.00),
            positive for income/inflow (e.g. 1200.00).
        payee_name: Name of the payee (merchant, person, etc.).
        budget_id: Budget ID from list_budgets(), or 'last-used' (default).
        category_id: Category to assign (ID from get_categories()). Optional.
        memo: Optional note for the transaction.
        cleared: Whether to mark as cleared (default False = uncleared/pending).

    Returns the created transaction with its assigned id and resolved names.
    """
    return ynab.create_transaction(
        budget_id,
        account_id,
        date,
        amount_dollars,
        payee_name,
        category_id,
        memo,
        cleared,
    )


@mcp.tool()
def update_transaction(
    transaction_id: str,
    budget_id: str = "last-used",
    category_id: str | None = None,
    memo: str | None = None,
    cleared: bool | None = None,
    payee_name: str | None = None,
) -> dict:
    """Update an existing YNAB transaction — recategorize it, fix the memo, mark cleared, etc.

    Only the fields you provide are changed; omitted fields are left as-is.

    Args:
        transaction_id: Transaction ID from get_transactions().
        budget_id: Budget ID from list_budgets(), or 'last-used' (default).
        category_id: New category ID from get_categories(), or null to uncategorize.
        memo: New memo/note text.
        cleared: True to mark cleared, False to mark uncleared.
        payee_name: New payee name.

    Returns the updated transaction with its resolved names.
    """
    return ynab.update_transaction(
        budget_id, transaction_id, category_id, memo, cleared, payee_name
    )


@mcp.tool()
def rename_category(
    category_id: str,
    name: str,
    budget_id: str = "last-used",
) -> dict:
    """Rename a YNAB category.

    Args:
        category_id: Category ID from get_categories().
        name: New name for the category.
        budget_id: Budget ID from list_budgets(), or 'last-used' (default).

    Returns the updated category id and name.
    """
    return ynab.rename_category(budget_id, category_id, name)


@mcp.tool()
def move_category_balance(
    from_category_id: str,
    to_category_id: str,
    amount_dollars: float,
    budget_id: str = "last-used",
    month: str = "current",
) -> dict:
    """Move money between two budget categories in a given month.

    Reduces the 'from' category's assigned amount and increases the 'to'
    category's assigned amount by the same amount — equivalent to YNAB's
    "Move Money" action. To move money to/from Ready to Assign, use
    update_category_budget on just one category instead.

    Args:
        from_category_id: Source category ID (from get_categories()).
        to_category_id: Destination category ID (from get_categories()).
        amount_dollars: Dollar amount to move (must be > 0).
        budget_id: Budget ID from list_budgets(), or 'last-used' (default).
        month: Month in 'YYYY-MM' format, or 'current' (default).

    Returns moved_dollars and updated budgeted/balance for both categories.
    """
    return ynab.move_category_balance(
        budget_id, month, from_category_id, to_category_id, amount_dollars
    )


@mcp.tool()
def set_category_goal(
    category_id: str,
    goal_target_dollars: float | None,
    budget_id: str = "last-used",
    goal_target_date: str | None = None,
    goal_frequency: str | None = None,
    refill_up_to: bool = True,
) -> dict:
    """Set or change the savings goal on a YNAB category.

    Pass goal_target_dollars=null to clear an existing goal entirely.

    Goal type is determined by the combination of arguments:
    - goal_target_dollars only → one-time "Needed for Spending" (NEED) goal
    - goal_target_dollars + goal_target_date → "Target Balance by Date" (TBD) goal
    - goal_target_dollars + goal_frequency → recurring NEED goal (monthly/weekly/yearly)
      Use refill_up_to=true (default) for "Refill up to" behavior (balance carries over);
      use refill_up_to=false for "Set aside another" (always asks for the full amount).

    Args:
        category_id: Category ID from get_categories().
        goal_target_dollars: Target dollar amount, or null to clear the goal.
        budget_id: Budget ID from list_budgets(), or 'last-used' (default).
        goal_target_date: Target completion date in YYYY-MM-DD format (for TBD goals).
        goal_frequency: Recurrence — 'monthly', 'weekly', or 'yearly' (for recurring goals).
        refill_up_to: Only used with goal_frequency. True (default) = "Refill up to" the
            target (previous balance counts toward the goal). False = "Set aside another"
            (always requires the full target regardless of balance carried over).

    Returns the updated category with goal_type, goal_target_dollars, and related fields.
    """
    return ynab.set_category_goal(
        budget_id,
        category_id,
        goal_target_dollars,
        goal_target_date,
        goal_frequency,
        refill_up_to,
    )


@mcp.tool()
def update_category_budget(
    category_id: str,
    budgeted_dollars: float,
    budget_id: str = "last-used",
    month: str = "current",
) -> dict:
    """Set the budget amount for a category in a given month.

    Args:
        category_id: Category ID from get_categories().
        budgeted_dollars: New budget amount in dollars (must be >= 0).
        budget_id: Budget ID from list_budgets(), or 'last-used' (default).
        month: Month in 'YYYY-MM' format, or 'current' (default).

    Returns the updated category with new budgeted/activity/balance amounts.
    """
    return ynab.update_category_budget(budget_id, month, category_id, budgeted_dollars)
