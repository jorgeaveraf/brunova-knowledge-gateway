"""Policies applied before calling Google Workspace."""

from fastapi import HTTPException


class DriveReadPolicy:
    MAX_LIST_FILES = 100

    @classmethod
    def validate_list_limit(cls, requested: int) -> int:
        if requested > cls.MAX_LIST_FILES:
            raise HTTPException(
                status_code=422,
                detail=f"limit must be at most {cls.MAX_LIST_FILES}",
            )
        return requested
