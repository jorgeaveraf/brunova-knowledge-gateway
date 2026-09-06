"""Bounded interpretation of cell validation; never evaluates arbitrary formulas."""

import re
from typing import Any

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.policies.workspace import SpreadsheetMutationPolicy


def bounded_validation_range(value: str, *, max_cells: int):
    prefix, sep, local = value.strip().rpartition("!")
    if not sep:
        local = value.strip()
    if re.fullmatch(r"\$?[A-Za-z]{1,3}\$?[1-9]\d*", local):
        local = f"{local}:{local}"
    return SpreadsheetMutationPolicy.parse_range(
        f"{prefix}!{local}" if sep else local, max_cells=max_cells
    )


def column_name(number: int) -> str:
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def resolve_source_range(
    expression: str, title: str, sheets: list[dict], max_cells: int
):
    value = expression.removeprefix("=")
    prefix, sep, local = value.rpartition("!")
    if not sep:
        prefix, local = title, value
    elif prefix.startswith("'") and prefix.endswith("'"):
        prefix = prefix[1:-1].replace("''", "'")
    sheet = next((s for s in sheets if s["title"] == prefix), None)
    if sheet is None:
        raise WorkspaceAdapterError(
            "validation_source_unresolved",
            "Validation source sheet cannot be resolved.",
            422,
        )
    # Open column ranges are bounded by the actual grid, never truncated.
    match = re.fullmatch(
        r"(\$?[A-Za-z]{1,3})(\$?[1-9]\d*)?:(\$?[A-Za-z]{1,3})(\$?[1-9]\d*)?", local
    )
    if match:
        a, start, b, end = match.groups()
        local = f"{a}{start or '1'}:{b}{end or sheet['row_count']}"
    qualified = "'" + prefix.replace("'", "''") + "'!" + local
    parsed = bounded_validation_range(qualified, max_cells=max_cells)
    if parsed.end_row > sheet["row_count"] or parsed.end_column > sheet["column_count"]:
        raise WorkspaceAdapterError(
            "validation_source_unresolved", "Validation source exceeds its grid.", 422
        )
    return sheet, parsed


def inspect_cells(adapter, resource, range_name: str, read_source) -> dict[str, Any]:
    parsed = bounded_validation_range(range_name, max_cells=adapter.max_cells)
    response = (
        adapter._sheets()
        .spreadsheets()
        .get(
            spreadsheetId=resource.id,
            ranges=[parsed.value],
            fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),data(startRow,startColumn,rowData(values(dataValidation))))",
        )
        .execute()
    )
    sheets = response.get("sheets", [])
    if len(sheets) != 1:
        raise WorkspaceAdapterError(
            "validation_target_unresolved", "Expected exactly one resolved sheet.", 422
        )
    sheet = sheets[0]
    props = sheet["properties"]
    grid = props["gridProperties"]
    if parsed.end_row > grid["rowCount"] or parsed.end_column > grid["columnCount"]:
        raise WorkspaceAdapterError(
            "spreadsheet_range_invalid",
            "Requested validation range exceeds the grid.",
            422,
        )
    rules = {}
    for block in sheet.get("data", []):
        for r, row in enumerate(block.get("rowData", []), block.get("startRow", 0) + 1):
            for c, cell in enumerate(
                row.get("values", []), block.get("startColumn", 0) + 1
            ):
                if cell.get("dataValidation"):
                    rules[r, c] = cell["dataValidation"]
    cells, sources, cache = [], [], {}
    structure = None
    remaining = adapter.max_cells
    for r in range(parsed.start_row, parsed.end_row + 1):
        for c in range(parsed.start_column, parsed.end_column + 1):
            rule = rules.get((r, c))
            cell = {"cell": f"{column_name(c)}{r}", "has_validation": rule is not None}
            if rule:
                condition = rule.get("condition", {})
                criterion = condition.get("type", "UNKNOWN")
                operands = condition.get("values", [])
                cell.update(
                    criterion=criterion,
                    condition_values=operands,
                    strict=bool(rule.get("strict", False)),
                    input_semantics="reject_input"
                    if rule.get("strict", False)
                    else "warning",
                    help_text=rule.get("inputMessage"),
                    domain_status="not_enumerated",
                    allowed_values=None,
                )
                if criterion == "ONE_OF_LIST":
                    cell.update(
                        domain_status="resolved",
                        allowed_values=[v["userEnteredValue"] for v in operands],
                    )
                elif criterion == "ONE_OF_RANGE":
                    expression = (
                        operands[0].get("userEnteredValue", "")
                        if len(operands) == 1
                        else ""
                    )
                    if expression not in cache:
                        source = {
                            "source_range": expression,
                            "status": "unresolved",
                            "resolved_values": None,
                        }
                        try:
                            if len(sources) >= 50:
                                raise WorkspaceAdapterError(
                                    "validation_source_limit",
                                    "At most 50 validation sources may be resolved.",
                                    422,
                                )
                            if structure is None:
                                structure = adapter.get_structure(resource)["sheets"]
                            origin, source_parsed = resolve_source_range(
                                expression, props["title"], structure, remaining
                            )
                            remaining -= source_parsed.cell_count
                            source.update(
                                sheet_id=str(origin["sheet_id"]),
                                sheet_title=origin["title"],
                                resolved_range=source_parsed.value,
                            )
                            values = read_source(source_parsed.value).values
                            resolved = []
                            for row in values:
                                for value in row:
                                    if (
                                        value != ""
                                        and value is not None
                                        and value not in resolved
                                    ):
                                        resolved.append(value)
                            source.update(status="resolved", resolved_values=resolved)
                        except WorkspaceAdapterError as error:
                            source["error_code"] = error.code
                        cache[expression] = len(sources)
                        sources.append(source)
                    cell["validation_source_index"] = cache[expression]
                    origin = sources[cache[expression]]
                    cell.update(domain_status=origin["status"])
            cells.append(cell)
    return {
        "spreadsheet_id": resource.id,
        "range": parsed.value,
        "sheet_id": str(props["sheetId"]),
        "sheet_title": props["title"],
        "cells": cells,
        "validation_sources": sources,
    }
