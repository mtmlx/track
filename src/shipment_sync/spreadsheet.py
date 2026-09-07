from typing import Any


def spreadsheet_safe_cell(value: Any) -> Any:
    """Keep external text literal in formula-aware spreadsheet clients."""
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def spreadsheet_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: spreadsheet_safe_cell(value) for key, value in row.items()}
